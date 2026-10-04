"""Транспорт записи журнала: PostgreSQL напрямую или ``enterprise-mcp``.

Зачем отдельный слой. Пока ``DbLoggingService`` писал в PostgreSQL сам, пул
записи журнала принадлежал агенту — тому же, что держит каналы. С появлением
``enterprise-mcp`` у разделяемого ресурса появился и второй возможный владелец,
и change ``enterprise-mcp-platform`` (фаза 7) ставит целью ровно это: агент
перестаёт владеть записью журнала, а спрашивает платформу операцией
``log_events``.

**Главное препятствие, из-за которого батч нельзя отправить одним вызовом.**
Операция ``log_events`` берёт ``session_id``/``user_id``/``request_id`` **из
контекста вызова**, а не из тела батча: в схеме элемента этих полей нет
вовсе. Это сделано на стороне платформы намеренно — иначе батч под видом
журналирования оборота записал бы события в чужую сессию. Но у агента батч
смешанный: события разных сессий, оборотов и пользователей лежат в одной
очереди, и у части событий ``user_id`` нет вовсе (канальные события, ошибки,
``log_sync_event``). Отправить такой батч одним вызовом нельзя физически:
сервер проставил бы всем событиям личность одного вызова.

Поэтому :func:`group_by_identity` разбивает батч на группы по
``(session_id, user_id, request_id)`` и каждая группа уходит своим вызовом.
События без полной личности в группу не попадают: подставлять выдуманный
``user_id`` — значит записать событие в чужую личность, ровно то, от чего
платформа специально защищается. Их судьбу решает локальный fallback.

**Почему fallback локальный, а не второй вызов PostgreSQL.** Второй пул
записи — это ровно тот «второй владелец», которого change и устраняет. Если
MCP-ленты нет, события уходят в файл и в счётчик ``dropped``: потеря видна
и измерима, но пул записи остаётся один.

Fallback срабатывает в двух случаях, и оба — про то, что журнал переживает
отказ платформы: событие **нечем подписать** (``session_id``/``user_id``
пусты) и транспорт **недоступен** (сервер не поднялся, loop не ответил). Второй
случай обязателен: иначе отказ платформы стирал бы журнал целиком — счётчик
``dropped`` умирает вместе с процессом и виден только тому, кто уже смотрит,
а след самого отказа не остаётся нигде, и расследование начинается с того,
чего не случилось.

Синхронность. ``DbLoggingService`` работает в отдельном потоке, а клиент
``enterprise-mcp`` — асинхронный и привязан к event loop агента. Отсюда
мост :class:`LoopCallRunner`: корутина ставится в чужой loop через
``asyncio.run_coroutine_threadsafe`` и ждётся с таймаутом. Без этого worker
не смог бы обратиться к платформе вовсе.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from lib.services.enterprise_mcp_client import (
    CallIdentity,
    EnterpriseMcpUnavailable,
    EnterpriseOperationError,
)

logger = logging.getLogger(__name__)

#: Операция батчевой записи событий журнала.
OP_LOG_EVENTS = "log_events"
#: Операция записи контекста вопроса.
OP_UPSERT_QUESTION_RUN = "upsert_question_run"


class LogWriteUnavailable(RuntimeError):
    """Транспорт записи недоступен: сервер не поднялся или не ответил.

    Отдельный тип, потому что вызывающий обяжен вести себя с ним иначе, чем с
    доменной ошибкой записи: недоступность — это «повторить позже и посчитать
    потерю», а отказ сервера — это «батч негоден, повтор не поможет».
    """


@dataclass
class WriteResult:
    """Итог одной отправки батча.

    ``dropped`` — события, которые до сервера не дошли ни по какой причине:
    переполнение его буфера, недоступность транспорта, отсутствие личности.
    Всё это должно быть видно вызывающему, иначе агент решит, что журнал
    наполняется, а он три дня молча пустой.
    """

    accepted: int = 0
    dropped: int = 0

    def merge(self, other: WriteResult) -> WriteResult:
        return WriteResult(
            accepted=self.accepted + other.accepted,
            dropped=self.dropped + other.dropped,
        )


@dataclass
class _IdentityKey:
    """Ключ группировки: личность вызова, а не личность события.

    Пустые значения сохраняются как есть — они не «неизвестны», они
    отсутствуют, и их отсутствие обязано дойти до вызова (или до отказа), а не
    подмениться догадкой.
    """

    session_id: str | None
    user_id: str | None
    request_id: str | None

    def complete(self) -> bool:
        """Есть ли все три ключа, которые ``CallIdentity.as_meta`` требует.

        ``request_id`` клиент дополняет сам, поэтому для группировки он не
        обязателен: события одного оборота без ``request_id`` — это нормальная
        ситуация, а не дефект. ``session_id`` и ``user_id`` обязательны, и их
        нельзя выдумать.
        """
        return bool(
            (self.session_id or "").strip() and (self.user_id or "").strip()
        )


@dataclass
class IdentityGroup:
    """Группа событий, у которых совпадает личность вызова."""

    key: _IdentityKey
    events: list[Any] = field(default_factory=list)


def group_by_identity(events: Iterable[Any]) -> list[IdentityGroup]:
    """Разбить батч по личности вызова, сохраняя порядок первого появления.

    Порядок групп важен не для красоты: журнал читают по времени, и перестановка
    батча переставляет события оборота относительно друг друга. Внутри группы
    порядок исходный.

    События с неполной личностью (``session_id`` или ``user_id`` пуст)
    получают собственную группу с неполным ключом. Их нельзя отправить
    подписанным вызовом, и это должно быть видно вызывающему, а не раствориться
    в группе с чужой личностью.
    """
    groups: dict[tuple[str | None, str | None, str | None], IdentityGroup] = {}
    for event in events:
        key = _IdentityKey(
            session_id=getattr(event, "session_id", None),
            user_id=getattr(event, "user_id", None),
            request_id=getattr(event, "request_id", None),
        )
        raw = (key.session_id, key.user_id, key.request_id)
        group = groups.get(raw)
        if group is None:
            group = IdentityGroup(key=key)
            groups[raw] = group
        group.events.append(event)
    return list(groups.values())


def event_to_wire(event: Any) -> dict[str, Any]:
    """Перевести ``LogEvent`` в элемент батча операции ``log_events``.

    Поля личности (``session_id``/``user_id``/``request_id``) сюда **не**
    попадают намеренно: операция их не читает из тела батча, берёт из контекста
    вызова. Отправлять их всё равно — значит предлагать платформе второй
    источник идентичности, которого у неё нет.
    """
    return {
        "id": event.id,
        "event_type": event.event_type,
        "name": event.name or "",
        "level": event.level or "INFO",
        "summary": event.summary or "",
        "payload": event.payload or {},
        "metadata": event.metadata or {},
        "channel": event.channel,
        "actor": event.actor,
    }


class CallRunner(Protocol):
    """Синхронный запуск корутины в event loop агента."""

    def __call__(self, coro: Any) -> Any:  # pragma: no cover - протокол
        ...


@dataclass
class LoopCallRunner:
    """Мост из потока worker'а в event loop, где живёт MCP-клиент.

    ``DbLoggingService`` работает в своём потоке, а сессия ``enterprise-mcp``
    привязана к loop, на котором поднята. Прямой вызов из потока невозможен
    (сессия принадлежит чужому loop), а ``asyncio.run`` в потоке поднял бы
    второй loop и тот же второй пул записи. Остаётся единственная честная
    форма — отдать корутину живому loop и дождаться результата.

    ``timeout_sec`` обязателен: ожидание корутины без предела — это worker,
    который больше не выходит, а вместе с ним ``stop()`` ждёт его до конца
    таймаута остановки. Живой loop, который не отвечает, должен приводить к
    потере батча со счётчиком, а не к висящему потоку.
    """

    loop: Any
    timeout_sec: float = 30.0

    def __call__(self, coro: Any) -> Any:
        # ``run_coroutine_threadsafe`` лежит ВНУТРИ try: на закрытом или
        # не запущенном loop он поднимает RuntimeError сам, до того как future
        # появится. try вокруг одного лишь ``future.result`` этот случай
        # пропустил бы, и недоступность платформы уехала бы наружу необработанной
        # — то есть вместо потери со счётчиком упал бы весь worker.
        try:
            future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        except RuntimeError as exc:
            coro.close()
            raise LogWriteUnavailable(f"event loop недоступен: {exc}") from exc
        try:
            return future.result(timeout=self.timeout_sec)
        except TimeoutError as exc:
            future.cancel()
            raise LogWriteUnavailable(
                f"вызов в loop не ответил за {self.timeout_sec:g}с"
            ) from exc
        except EnterpriseOperationError:
            # Доменный отказ платформы доезжает до сюда через ``future.result`` и
            # НЕ является «отказом loop'а», хотя оба — ``RuntimeError``: сервер
            # ответил, просто отказал. Раньше этот отказ переписывался в
            # ``LogWriteUnavailable("event loop отказал: [code] ...")``, то есть
            # отказ платформы выдавался за недоступность транспорта, а причина
            # терялась среди «сервера нет». Поднимаем как есть: вызывающий сам
            # решит, что с доменным отказом делать.
            raise
        except RuntimeError as exc:
            # Тот же класс отказа с другой стороны: loop отвечает, но
            # корутину выполнить не смог (loop остановился между отправкой и
            # запуском). Для вызывающего это тот же «сервера нет».
            raise LogWriteUnavailable(f"event loop отказал: {exc}") from exc


@dataclass
class McpLogWriter:
    """Запись журнала в ``enterprise-mcp`` операциями ``log_events``.

    Единственный владелец вызовов журнала: агент больше не формирует ``INSERT``
    в таблицу, а платформа — второй писатель, который знает про этот журнал.

    Fail-fast без ретрая. Операция ``log_events`` валидирует батч целиком и
    бросает на первом негодном событии; повтор того же батча упал бы тем же
    самым. Поэтому повтор здесь был бы не «устойчивостью», а способом
    дважды потратить круговой оборот на заведомо обречённый вызов.
    """

    call: Callable[..., Any]
    run: CallRunner
    #: Куда уходят события, которые в журнал не попали. По умолчанию ``None`` —
    #: они только считаются потерянными. ``DbLoggingService`` подставляет
    #: сюда запись в локальный fallback-файл, потому что он владеет
    #: счётчиками статистики.
    on_fallback: Callable[[Sequence[Any]], None] | None = None

    def write_events(self, batch: Sequence[Any]) -> WriteResult:
        """Отправить батч, разбив его по личности вызова.

        Returns:
            ``WriteResult`` с принятым и потерянным. Пустой батч — нулевой
            результат без вызова: обращаться к серверу не с чем.
        """
        if not batch:
            return WriteResult()

        result = WriteResult()
        for group in group_by_identity(batch):
            if not group.key.complete():
                # Нет ``session_id``/``user_id`` — вызова, который можно
                # подписать, не существует. Событие не теряется молча.
                result = result.merge(WriteResult(dropped=len(group.events)))
                self._to_fallback(group.events)
                continue
            result = result.merge(self._write_group(group))
        return result

    def _to_fallback(self, events: Sequence[Any]) -> None:
        """Отдать события локальному следу, если он заведён."""
        if self.on_fallback is not None:
            self.on_fallback(events)

    def _write_group(self, group: IdentityGroup) -> WriteResult:
        """Отправить одну группу событий одним вызовом ``log_events``."""
        identity = CallIdentity(
            session_id=str(group.key.session_id),
            user_id=str(group.key.user_id),
            request_id=group.key.request_id,
        )
        payload = {
            "events": [event_to_wire(event) for event in group.events],
        }
        try:
            text = self.run(self._invoke(identity, payload))
        except (EnterpriseMcpUnavailable, LogWriteUnavailable) as exc:
            # Отказ транспорта не отменяет локальный след: без него отказ
            # платформы неотличим от тишины — счётчик пережил бы только процесс,
            # который и так уже мёртв к моменту расследования. Событие уходит и
            # в файл, и в счётчик потерь: до журнала платформы оно не дошло.
            logger.warning(
                "log_events: транспорт недоступен, событий потеряно %d: %s",
                len(group.events),
                exc,
            )
            self._to_fallback(group.events)
            return WriteResult(dropped=len(group.events))
        return self._counters(text, len(group.events))

    async def _invoke(self, identity: CallIdentity, payload: dict[str, Any]) -> str:
        return await self.call(OP_LOG_EVENTS, payload, identity=identity)

    def _counters(self, text: str, sent: int) -> WriteResult:
        """Разобрать счётчики ответа ``log_events``.

        Нераспознанный ответ считается полным приёмом (``accepted = sent``), а не
        провалом: запись к этому моменту уже исполнилась, и потеря батча была бы
        ложью в обратную сторону. Но молчаливо это не проходит — расхождение
        попадает в журнал: молчаливая подмена счётчика здесь означала бы, что
        переполнение буфера платформы никогда не будет замечено.
        """
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            data = None
        if isinstance(data, dict):
            accepted = data.get("accepted")
            dropped = data.get("dropped")
            if isinstance(accepted, int) and isinstance(dropped, int):
                return WriteResult(accepted=accepted, dropped=dropped)
        logger.warning(
            "log_events: счётчики не разобраны (ответ %r), принято %d",
            str(text)[:200],
            sent,
        )
        return WriteResult(accepted=sent)

    def upsert_question_run(self, record: Any) -> bool:
        """Записать контекст вопроса операцией ``upsert_question_run``.

        ``request_id`` в аргументы не попадает: операция берёт его из контекста
        вызова. Отдельная операция, а не событие журнала: контекст пишется один
        раз на оборот, и не должен оседать в батче событий.

        Returns:
            ``True`` — вызов ушёл платформе и она его приняла. ``False`` —
            записи не было: подписать вызов нечем либо транспорт недоступен.
            Различать эти два исхода обязан вызывающий: раньше метод возвращал
            ``None`` на всех путях, и ``DbLoggingService`` увеличивал
            ``question_runs`` даже после раннего ``return``, то есть статистика
            показывала запись, которой не было.
        """
        if not record.request_id:
            logger.warning(
                "upsert_question_run: запись без request_id пропущена"
            )
            return False
        session_id = (record.session_id or "").strip()
        user_id = (record.user_id or "").strip()
        if not session_id or not user_id:
            # Подписать вызов нечем, а контекст чужой сессии выдумывать нельзя.
            logger.warning(
                "upsert_question_run: нет личности (session_id=%r user_id=%r), "
                "контекст вопроса потерян",
                record.session_id,
                record.user_id,
            )
            return False
        identity = CallIdentity(
            session_id=session_id, user_id=user_id, request_id=record.request_id
        )
        payload: dict[str, Any] = {
            "status": record.status,
            "summary": record.summary,
            "chat_id": record.chat_id,
            "channel": record.channel,
            "agent_id": record.agent_id,
            "parent_agent_id": record.parent_agent_id,
            "parent_request_id": record.parent_request_id,
            "is_subagent": bool(record.is_subagent),
            "question": record.question,
            "response": record.response,
            "media": list(record.media) if record.media else None,
            "update_only": bool(record.update_only),
        }
        try:
            self.run(self._invoke_question_run(identity, payload))
        except (LogWriteUnavailable, EnterpriseMcpUnavailable) as exc:
            logger.warning("upsert_question_run: контекст потерян: %s", exc)
            return False
        return True

    async def _invoke_question_run(
        self, identity: CallIdentity, payload: dict[str, Any]
    ) -> str:
        return await self.call(OP_UPSERT_QUESTION_RUN, payload, identity=identity)


class LocalFallbackSink:
    """Локальный файл на случай недоступного ``enterprise-mcp``.

    Петля не замыкается: если бы падение платформы означало «нигде не писать»,
    то отказ платформы стирал бы журнал целиком, и расследование отказа
    начиналось бы с отсутствия следов. Файл — не второй пул записи и не
    конкурент платформе: это локальный след, который переживает недоступность
    сервера.

    Запись неблокирующая и ограниченная: при остановленном сервере источник
    событий не останавливается, и растущий хвост файла рано или поздно съел бы
    диск. При превышении предела новые события не пишутся, а счётчик
    ``dropped`` растёт — потеря видна, а не маскируется файлом.
    """

    def __init__(self, path: str, *, max_bytes: int = 32 * 1024 * 1024) -> None:
        self._path = str(path)
        self._max_bytes = int(max_bytes)
        self._lock = threading.Lock()
        self._written = 0
        self._dropped = 0
        self._last_error: str | None = None

    @property
    def path(self) -> str:
        return self._path

    def _ensure_parent(self) -> None:
        """Создать каталог файла, если его нет.

        Без этого последний след события не появлялся никогда: ``open(..., "a")``
        не создаёт каталоги, а ``data_store/logs`` на свежей машине не
        существует. Живой прогон 2026-10-04 показывал ровно это — по
        ``No such file or directory`` на каждый батч, то есть обещание «петля не
        замыкается, след остаётся» не выполнялось ровно тогда, когда след был
        нужен. Каталог создаётся лениво, при первой записи, а не в
        конструкторе: создание файла на старте сделало бы след заметным даже
        тогда, когда писать нечего.
        """
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    def write(self, events: Sequence[Any]) -> None:
        """Дописать события одной строкой JSON на событие."""
        if not events:
            return
        dropped = 0
        with self._lock:
            try:
                if self._exceeded():
                    dropped = len(events)
                else:
                    self._ensure_parent()
                    with open(self._path, "a", encoding="utf-8") as handle:
                        for event in events:
                            handle.write(
                                json.dumps(
                                    {
                                        "logged_at": time.time(),
                                        "event_type": getattr(
                                            event, "event_type", None
                                        ),
                                        "level": getattr(event, "level", None),
                                        "session_id": getattr(
                                            event, "session_id", None
                                        ),
                                        "user_id": getattr(event, "user_id", None),
                                        "request_id": getattr(
                                            event, "request_id", None
                                        ),
                                        "summary": getattr(event, "summary", None),
                                        "payload": getattr(event, "payload", None),
                                    },
                                    ensure_ascii=False,
                                    default=str,
                                )
                                + "\n"
                            )
                    self._written += len(events)
            except OSError as exc:
                self._last_error = f"fallback: {exc}"
                dropped = len(events)
            self._dropped += dropped
        if dropped:
            logger.warning(
                "журнал: локальный fallback не записал %d событий (%s)",
                dropped,
                self._last_error or "файл переполнен",
            )

    def _exceeded(self) -> bool:
        try:
            return os.path.getsize(self._path) >= self._max_bytes
        except OSError:
            return False

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "path": self._path,
                "written": self._written,
                "dropped": self._dropped,
                "last_error": self._last_error,
            }
