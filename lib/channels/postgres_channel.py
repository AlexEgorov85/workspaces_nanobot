"""PostgreSQL / Greenplum канал — связывает БД веб-сервера с nanobot-агентом.

Канал опрашивает таблицу ``agent_conversation_messages``, забирает входящие
сообщения от пользователей (status='pending'), отправляет их агенту,
и записывает ответы обратно в ту же таблицу.

Пример конфига (config.json → channels.postgres)::

    {
        "enabled": true,
        "dsn": "postgresql://user:pass@localhost:5432/nanobot",
        "schema": "public",
        "table_name": "agent_conversation_messages",
        "poll_interval": 2.0,
        "max_concurrent": 1,
        "processing_timeout": 300,
        "error_retry_delay": 60.0,
        "max_stuck_retries": 3,
        "worker_id": ""
    }
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from utils.jsonb import decode_jsonb as _decode_jsonb
from utils.media import (
    deserialize as media_deserialize,
)
from utils.media import (
    resolve_paths_and_hints as media_resolve_paths_and_hints,
)
from utils.media import (
    serialize as media_serialize,
)
from utils.session_file_store import SessionFileStore

from lib.channels.message_exchange import MessageExchange
from lib.channels.queue_ops import QueueOps
from lib.services.session_files import (
    SessionFileResolver,
    SessionFilesUnavailable,
    current_session_file_resolver,
)
from lib.utils.outbound_meta import FINAL_TURN_KEY, is_dropped


def _session_dir_resolver() -> SessionFileResolver:
    """Резолвер каталога сессии текущего процесса; отказ, если его нет.

    Каталог вложений приходит оттуда же, откуда его берёт хук перенаправления:
    одна папка на сессию, один корень, одно имя. Отдельного пути у канала нет и
    быть не должно — расхождение двух путей уже стоило того, что вложения из
    PostgreSQL были агенту недоступны.
    """
    resolver = current_session_file_resolver()
    if resolver is None:
        raise SessionFilesUnavailable(
            "резолвер каталога сессии не опубликован: вложение не сохранено"
        )
    return resolver


async def _ensure_session_dir(session_key: str) -> None:
    """Дождаться каталога сессии до синхронного разбора вложений."""
    await _session_dir_resolver().ensure(session_key)


def _resolved_session_dir(session_key: str) -> Path:
    """Каталог сессии из уже полученного ответа резолвера, синхронно.

    Кодек ``utils.media.deserialize`` синхронен, а резолвер асинхронен, поэтому
    канал дожидается каталога один раз (см. :func:`_ensure_session_dir`), и
    дальше хранилище читает уже полученный ответ. Вычислять путь на стороне
    канала или хранилища нельзя: это была бы вторая копия корня сессии.
    """
    return _session_dir_resolver().resolved_session_dir(session_key)


class PostgresChannel(BaseChannel):
    name = "postgres"
    """Опрашивает ``agent_conversation_messages`` и отправляет ответы агенту.

    Жизненный цикл сообщения:

        1. Пользователь пишет сообщение → INSERT с status='pending'
        2. ``_claim_one`` атомарно захватывает задачу:
           ``UPDATE ... WHERE id = (SELECT ... WHERE status='pending' ..)``
           — если задача уже переведена в processing, повторный UPDATE
           не срабатывает, и второй захват невозможен
        3. ``_handle_message`` отправляет в шину → агенту
        4. Агент формирует ответ → ``send()`` пишет status='completed'
           и удаляет claim
        5. Web-сервер видит completed и показывает ответ

    Рассуждения агента (reasoning) пишутся в real-time через
    ``send_reasoning_delta`` → буферизируются → ``_flush_reasoning``
    периодически сбрасывает в ``metadata.reasoning``.

    Параллельность ограничена ``max_concurrent`` через asyncio.Semaphore.
    Мульти-машинная аренда задач: claim+lease/heartbeat, reclaim+heal,
    статусы ``error`` (повторяется) и ``failed`` (терминал). См. Документация
    docs/ARCHITECTURE.md » «Мульти-машинный пул воркеров».
    """

    def __init__(
        self,
        config: dict,
        bus: MessageBus,
        *,
        db_logging_service: Any | None = None,
        compaction_event_subscriber: Any | None = None,
        enterprise_mcp: Any | None = None,
    ) -> None:
        super().__init__(config, bus)
        # Опциональный ``DbLoggingService`` для долговечного журнала
        # ``agent_gateway_logs``: «тихие» ошибки циклов опроса БД
        # (poll/lease/unstick) пишутся туда в дополнение к loguru-логгеру
        # (терминал). ``None`` (тесты, standalone) — журналирование
        # отключается, остаётся только терминальный вывод.
        self._db_logging_service = db_logging_service
        # Опциональный ``CompactionEventSubscriber``: единый наблюдатель
        # upstream-событий ``ContextCompactionEvent``. ``None`` (тесты,
        # standalone) — observer отключён.
        self._compaction_event_subscriber = compaction_event_subscriber
        _get = config.get

        # ---- настройки подключения к БД ----
        self._dsn: str = _get("dsn", "")
        self._schema: str = _get("schema", "public")
        self._table_name: str = _get("table_name", "")
        if not self._table_name:
            raise ValueError(
                "PostgresChannel: channels.postgres.table_name обязателен "
                "(нет авто-дефолтов в коде)"
            )
        self._fq_table: str = f"{self._schema}.{self._table_name}"

        # ---- тайминги ----
        # как часто опрашивать БД на новые сообщения (сек)
        self._poll_interval: float = float(_get("poll_interval", 2.0))
        # через сколько секунд сообщение в processing считается зависшим
        self._processing_timeout: int = int(_get("processing_timeout", 120))
        # сколько раз retry'ить зависшее сообщение до отказа
        self._max_stuck_retries: int = int(_get("max_stuck_retries", 3))
        # защитный лимит размера _msg_ctx
        self._msg_ctx_max_size: int = int(_get("msg_ctx_max_size", 100))
        # как часто сбрасывать буферы reasoning в БД (сек)
        self._flush_interval: float = float(_get("flush_interval", 2.0))

        # ---- мульти-машинный пул воркеров (аренда задач через claims) ----
        # ---- идентификация воркера (только для логов и вывода активности) ----
        # Уникальный идентификатор этого инстанса: либо явный из конфига,
        # либо авто-генерируемый {hostname}:{pid}:{rand8}.
        self._worker_id: str = (_get("worker_id") or "").strip()
        if not self._worker_id:
            self._worker_id = (
                f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
            )
        # пауза перед повторным захватом задачи со статусом error (сек)
        self._error_retry_delay: int = int(_get("error_retry_delay", 60))
        # ---- операции очереди ----
        # Единственный путь к данным задач. Своего пула PostgreSQL у канала
        # больше нет: платформа объявлена его владельцем, и второй пул
        # означал бы второй набор соединений к той же таблице. Идентичность
        # служебных вызовов несёт ``worker_id`` - он и раньше был у воркера,
        # просто шёл только в лог.
        self._ops = QueueOps(enterprise_mcp, worker_id=self._worker_id)
        # task_id задач, которые захвачены этим инстансом прямо сейчас.
        # Множество локальное: состояние захвата хранится в самой строке
        # задачи (status='processing'), поэтому heartbeat и таблица
        # аренды не нужны. Используется для возврата незавершённых задач
        # в пул при остановке.
        self._claimed_ids: set[str] = set()

        # ---- вывод активности пула воркеров в терминал ----
        # Глубину вывода объявляет ОДИН ключ ``gateway.console_level``
        # (``quiet|turn|trace``, дефолт ``turn``). Активность воркеров — факт
        # оборота, поэтому она видна на ``turn`` и ``trace``: прежний дефолт
        # ``print_worker_activity=false`` означал, что за оборотом при
        # ``log_level=INFO`` не видно ничего, и это был дефект наблюдаемости.
        # Явный ключ в конфиге канала — только ручка тестов и standalone.
        _declared_activity = _get("print_worker_activity", None)
        if _declared_activity is None:
            from lib.services.operator_console import (
                CONSOLE_LEVEL_TURN,
                depth_visible,
            )

            self._print_worker_activity: bool = depth_visible(CONSOLE_LEVEL_TURN)
        else:
            self._print_worker_activity = bool(_declared_activity)
        # последний напечатанный (pending, error) — чтобы не спамить строку очереди
        self._last_queue_summary: tuple[int, int] | None = None

        # ---- параллельность ----
        self._max_concurrent: int = int(_get("max_concurrent", 1))
        self._error_backoff_sec: float = float(_get("error_backoff_sec", 1.0))
        # Общий движок обмена: поллинг, конкуренция, кодек media.
        self.exchange = MessageExchange(
            self,
            max_concurrent=self._max_concurrent,
            poll_interval=self._poll_interval,
            error_backoff=self._error_backoff_sec,
        )
        # chat_id, которые сейчас заняты (чтобы не диспатчить второе
        # сообщение в тот же чат, пока первое не завершено)
        self._chat_inflight: set[str] = set()

        # ---- единое хранилище файлов сессии ----
        # Каталог сессии у хранилища — от резолвера, того же, что у хука
        # перенаправления: одна папка на сессию и один корень на процесс.
        # Настройка ``channels.postgres.media_cache_dir`` больше не читается —
        # это был второй ответ на вопрос «где лежат вложения», и он уже разошёлся
        # с хуком (см. ``PENDING-DELETIONS.md``).
        injected_store = _get("_file_store")
        if isinstance(injected_store, SessionFileStore):
            # Инжектированное хранилище само владеет своими путями, поэтому
            # канал не ждёт резолвер: иначе тест с подменённым хранилищем падал
            # бы не из-за вложения, а из-за отсутствия платформы.
            self._file_store: SessionFileStore = injected_store
            self._prepare_session_dir: Callable[[str], Awaitable[None]] | None = None
        else:
            self._file_store = SessionFileStore(
                _resolved_session_dir, attachments_subdir="attachments"
            )
            self._prepare_session_dir = _ensure_session_dir

        self._msg_chat: dict[str, str] = {}

        # ---- стриминг (потоковая передача ответа) ----
        # stream_id → накопленный текст (для send_delta)
        self._stream_buffers: dict[str, str] = {}

        # ---- рассуждения (reasoning) ----
        # assistant_msg_id → накопленный текст рассуждений
        self._reasoning_buffers: dict[str, str] = {}
        # Блокировки на сброс рассуждений больше нет: дописывание атомарно на
        # платформе, и локальный замо́к был нужен ровно потому, что раньше
        # атомарности не было.
        self._flush_task: asyncio.Task | None = None

        # ---- откат зависших processing (single-режим) ----
        # ``_unstick_processing`` запускается в фоновой задаче с интервалом
        # ``unstick_interval`` (по дефолту = processing_timeout / 5, минимум
        # 60 сек). Это убирает SELECT+UPDATE каждые poll_interval на
        # пустом столе (когда ничего зависшего нет).
        self._unstick_interval: float = max(
            60.0,
            float(_get("unstick_interval", max(60.0, self._processing_timeout / 5))),
        )
        self._unstick_task: asyncio.Task | None = None

        # ---- контекст сообщения ----
        # user_msg_id → {assistant_msg_id, tool_events, reasoning_buf}
        self._msg_ctx: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Кодирование/декодирование медиа-файлов для передачи через БД
    # ------------------------------------------------------------------

    async def _embed_media_for_db(self, media: list[str]) -> list[Any]:
        """Прочитать локальные файлы и закодировать для БД (AW-формат).

        Делегирует общему ``utils.media.serialize`` — единая схема
        ``{"filename", "file_id", "mime_type", "file_size"}`` для всех каналов.
        """
        return media_serialize(media)

    async def decode_media(self, media: list[Any], session_key: str) -> list[Any]:
        """Декодировать storage-медиа обратно в локальные файлы сессии.

        Публичная точка входа канала: ``MessageExchange`` ходит в неё, а
        ``_decode_media_from_db`` — обёртка для вызовов внутри канала. Одна
        функция на разбор вложений, потому что только здесь дожидается каталога
        сессии.

        Делегирует общему ``utils.media.deserialize`` — терпит legacy
        ``{filename, data}``, новый AW ``{filename, file_id, ...}`` и
        ``{filename, path}``. Файлы пишутся через ``SessionFileStore`` в
        ``files/attachments/`` каталога сессии, который отдал резолвер.

        Недоступный резолвер — отказ **сохранения**, а не отказ разбора: запись
        вложения не состоялась, но опрос не должен из-за этого терять сообщение.
        Поэтому каталог не запрашивается на весь список, а кодек отказывает
        поштучно, и неразобранное вложение уходит агенту как есть. В другой
        каталог оно при этом не попадает: путь у хранилища один, и он от
        резолвера.
        """
        if self._prepare_session_dir is not None:
            try:
                await self._prepare_session_dir(session_key)
            except SessionFilesUnavailable as exc:
                self.logger.warning(
                    "каталог сессии недоступен, вложения не сохраняются: {}", exc
                )
        return media_deserialize(media, self._file_store, session_key)

    async def _decode_media_from_db(
        self, media: list[Any], session_key: str = "default"
    ) -> list[Any]:
        """То же, что :meth:`decode_media`, с дефолтным ключом сессии."""
        return await self.decode_media(media, session_key)

    @staticmethod
    def _resolve_media_paths_and_hints(
        media: list[Any],
    ) -> tuple[list[str], list[str]]:
        """Из декодированных media (строки-пути или dict filename/path)
        извлечь пути для агента и подсказки «файл лежит там-то»."""
        return media_resolve_paths_and_hints(media)

    # ------------------------------------------------------------------
    # Жизненный цикл (start / stop)
    # ------------------------------------------------------------------

    @property
    def file_store(self) -> SessionFileStore:
        """Хранилище вложений для ``MessageExchange`` (кодек media)."""
        return self._file_store

    async def start(self) -> None:
        """Запустить циклы опроса БД и сброса рассуждений."""
        self._running = True
        self._flush_task = asyncio.create_task(self._flush_reasoning_loop())
        # Фоновый unstick с интервалом unstick_interval (по дефолту
        # значительно больше poll_interval — чтобы не дёргать БД каждые
        # 10 сек на пустом столе).
        self._unstick_task = asyncio.create_task(self._unstick_loop())
        await self.exchange.start()
        self.logger.info(
            "Polling {} every {}s (processing timeout {}s, worker_id={}, "
            "unstick_interval={}s)",
            self._fq_table,
            self._poll_interval,
            self._processing_timeout,
            self._worker_id,
            self._unstick_interval,
        )

    async def stop(self) -> None:
        """Остановить все циклы и сбросить рассуждения."""
        self._running = False
        await self.exchange.stop()
        await self._flush_reasoning()
        if self._flush_task:
            self._flush_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._flush_task
        if self._unstick_task:
            self._unstick_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._unstick_task
            self._unstick_task = None
        # Вернуть незавершённые задачи в пул (их подберёт следующий цикл).
        await self._return_claimed_to_pool()
        # Пул PostgreSQL принадлежит платформе и живёт в её процессе: у канала
        # своего соединения больше нет, закрывать нечего. Сессия
        # ``enterprise-mcp`` закрывается владельцем клиента.

    async def _return_claimed_to_pool(self) -> None:
        """Вернуть незавершённые задачи этого инстанса в пул при остановке.

        Задачи возвращаются в ``pending`` (если всё ещё ``processing``), и их
        assistant-placeholder удаляется, чтобы следующий захват не нашёл
        оборванный ответ. Таблица аренды не используется: состояние захвата
        хранится в самой строке задачи.
        """
        if not self._claimed_ids:
            return
        # Один вызов на весь список, а не цикл по задачам: между вызовами
        # процесс можно убить, и тогда часть задач осталась бы в processing
        # до таймаута, а их заглушки — до следующего захвата.
        try:
            await self._ops.release_claimed_tasks(list(self._claimed_ids))
        except Exception as exc:  # noqa: BLE001 - остановка не должна падать
            self.logger.error(
                "не удалось вернуть захваченные задачи в пул: {}", exc
            )
        self._claimed_ids.clear()

    # ------------------------------------------------------------------
    # Активность пула воркеров (опциональный вывод в терминал gateway)
    # ------------------------------------------------------------------
    #
    # Включается объявленной глубиной вывода ``gateway.console_level``
    # (``operator_console.depth_visible("turn")``): факты оборота видны на
    # ``turn`` и ``trace``. Явный ``print_worker_activity`` в конфиге канала
    # остаётся только как ручка для тестов и standalone-запуска, где
    # ``configure_loguru`` не вызывался; в gateway ключ выводится из уровня,
    # поэтому второй ручки выбора глубины не остаётся. Печатает: взял
    # задачу / закончил / простой / размер очереди. Форматом повторяет вывод
    # токенов LLM.

    def _activity_print(
        self,
        phase: str,
        *,
        task: str | None = None,
        chat: str | None = None,
        detail: str = "",
        extra: str = "",
    ) -> None:
        """Отдать консоли факт активности воркера.

        Канал НЕ печатает факт сам: он собирает объект и отдаёт общий
        рендер (``lib/services/operator_console.py``). Раньше здесь был
        ``rich.console.print`` с собственной подстановкой стрелок и
        ``markup=False`` — это был второй формат построчного вывода, и
        колонки «кто»/«задача» у такой строки не было вовсе.
        """
        if not self._print_worker_activity:
            return
        try:
            from lib.services.operator_console import emit, worker_fact

            emit(worker_fact(
                worker=self._worker_id,
                phase=phase,
                task=task,
                chat=chat,
                detail=detail,
                extra=extra,
            ))
        except Exception:
            # Активность — не факт оборота: её потеря не должна ронять
            # обработку задачи.
            pass

    def _lifecycle_log(
        self,
        phase: str,
        user_msg_id: str | None,
        *,
        chat_id: str | None = None,
        assistant_msg_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Одна строка lifecycle-лога на каждую фазу задачи.

        Формат: ``TASK lifecycle task=<id> phase=<phase> chat=<id> assistant=<id> ...``.
        По одной строке на фазу легко грепать и реконструировать сценарий
        зависшего процесса. ``final_received`` дополнительно логирует
        маркеры outbound (``_final_turn``/``_turn_end``/``_stream_end``/
        ``streamed``/``event``), без них невозможно понять, какой именно
        путь финализации сработал.
        """
        parts = [f"task={user_msg_id}", f"phase={phase}"]
        if chat_id:
            parts.append(f"chat={chat_id}")
        if assistant_msg_id:
            parts.append(f"assistant={assistant_msg_id}")
        if extra:
            for k, v in extra.items():
                if isinstance(v, str) and len(v) > 80:
                    v = v[:77] + "..."
                parts.append(f"{k}={v}")
        self.logger.debug("TASK lifecycle {}", " ".join(parts))

    def _journal_event(
        self,
        event_type: str,
        summary: str,
        payload: dict[str, Any] | None = None,
        *,
        level: str = "WARN",
    ) -> None:
        """Долговечно записать ошибку канала в ``agent_gateway_logs``.

        Дублирует loguru-строку из циклов опроса БД (``poll_inbound`` /
        ``_unstick_loop``) в журнал ``DbLoggingService``,
        чтобы «тихие» сбои были видны и post-factum (``history_search``,
        дашборды), а не только в терминале. Нет ``DbLoggingService``
        (``None`` — тесты/standalone) или он не запущен — no-op:
        терминальный вывод loguru остаётся единственным источником.
        Все ошибки внутри глотаются — канал не должен падать из-за
        журналирования.
        """
        svc = self._db_logging_service
        if svc is None or not getattr(svc, "is_running", lambda: False)():
            return
        try:
            from lib.services.db_logging_service import LogEvent

            svc.log_event(LogEvent(
                event_type=event_type,
                level=level,
                session_id=None,
                channel=self.name,
                actor="channel",
                name=event_type,
                summary=summary,
                payload=payload or {},
            ))
        except Exception:
            pass

    @staticmethod
    def _preview(content: Any, limit: int = 60) -> str:
        """Короткий однострочный превью контента задачи для лога."""
        if not content:
            return ""
        text = " ".join(str(content).split())
        return text if len(text) <= limit else text[: limit - 1] + "…"

    async def _report_queue(self) -> None:
        """Одноразовая (по изменению) печать размера очереди задач.

        Считает по ``agent_conversation_messages`` число ожидающих
        (``pending``) и повторяемых (``error``) user-задач. Печатает строку
        только когда суммарное значение изменилось с прошлого опроса.
        """
        try:
            if not self._print_worker_activity:
                return
            stats = await self._ops.queue_stats()
            pending = stats.get("pending", 0)
            error = stats.get("error", 0)
            summary = (pending, error)
            if summary != self._last_queue_summary:
                self._last_queue_summary = summary
                total = pending + error
                if total == 0:
                    # «Работать не над чем» — САМ ФАКТ, а не отсутствие
                    # строк. Без него «очередь пуста» и «воркер завис» в
                    # терминале неразличимы, и это ровно тот дефект
                    # наблюдаемости, который change закрывает. Носитель —
                    # существующий маркер ``TASK lifecycle``: записи в журнале
                    # у этого факта нет, и выдумывать имя события нельзя.
                    self._activity_print(
                        "idle",
                        extra=f"pending={pending} error={error} (итого {total})",
                    )
                else:
                    self._activity_print(
                        "queue",
                        extra=f"pending={pending} error={error} (итого {total})",
                    )
        except Exception as e:
            self.logger.debug("Worker queue stats error: {}", e)

    # ------------------------------------------------------------------
    # Сброс рассуждений (reasoning flush)
    # ------------------------------------------------------------------

    async def _flush_reasoning_loop(self) -> None:
        """Фоновая задача: каждые ``_flush_interval`` секунд сбрасывает
        накопленные буферы рассуждений в ``metadata.reasoning`` в БД."""
        while self._running:
            await asyncio.sleep(self._flush_interval)
            try:
                await self._flush_reasoning()
            except Exception as e:
                self.logger.error("Flush reasoning error: {}", e)
            try:
                await self._flush_live_context()
            except Exception as e:
                self.logger.debug("Flush live context error: {}", e)

    async def _flush_reasoning(self) -> None:
        """Сбросить все грязные буферы рассуждений одной пачкой.

        Дописывание выполняется на платформе (``append_reasoning``), где
        конкатенация происходит в SQL поверх уже обновлённого значения.
        Раньше здесь стоял ``_reasoning_io_lock`` поверх чтения-склейки-записи:
        блокировка была нужна ровно потому, что операция не была атомарной, и
        это признавалось молча. Теперь гонки нет — и блокировки тоже.
        """
        if not self._reasoning_buffers:
            return
        buffers = self._reasoning_buffers
        self._reasoning_buffers = {}
        for assistant_msg_id, delta in buffers.items():
            if not delta:
                continue
            try:
                await self._ops.append_reasoning(assistant_msg_id, delta)
            except Exception as exc:  # noqa: BLE001 - сброс не роняет цикл
                self.logger.warning(
                    "не удалось дописать рассуждение в {}: {}",
                    assistant_msg_id, exc,
                )

    async def _flush_live_context(self) -> None:
        """Живое обновление занятости контекста в processing-строки.

        Каждые ``_flush_interval`` секунд читает блок ``context_window`` из
        моста per-iteration usage (``lib.hooks.database_logging_hook``) и
        пишет его в metadata processing assistant-строки. UI
        через свой поллинг видит его ДО финализации ответа — прогресс-бар
        заполняется «вживую» по мере роста промпта.

        Блок собирается патчем ``agent._assemble_outbound`` на финале
        оборота, а на лету — из usage последней итерации и лимита,
        засеянного на старте оборота (патч ``agent._state_build``). После
        финализации/ошибки мост очищается (``_drop_context_bridge``), и
        финальный ответ перезаписывает блок тем же значением.
        """
        from lib.hooks.database_logging_hook import get_context_window
        for msg_id, chat_id in list(self._msg_chat.items()):
            ctx = self._msg_ctx.get(msg_id) or {}
            assistant_msg_id = ctx.get("assistant_msg_id")
            if not assistant_msg_id:
                continue
            block = get_context_window(f"postgres:{chat_id}")
            if not block:
                continue
            # Полная замена значения, а не дописывание: патч здесь и уместен.
            # Сверка с прежним значением ушла вместе с чтением - зато не
            # осталось окна, где два сброса решают по-разному, кто прав.
            try:
                await self._ops.patch_message_metadata(
                    assistant_msg_id,
                    {"context_window": block},
                    role="assistant",
                )
            except Exception as exc:  # noqa: BLE001 - прогресс-бар не критичен
                self.logger.debug(
                    "не удалось обновить context_window для {}: {}",
                    assistant_msg_id, exc,
                )

    # ------------------------------------------------------------------
    # Цикл опроса БД
    # ------------------------------------------------------------------

    async def poll_priority_inbound(self, exchange: MessageExchange) -> bool:
        """Priority polling path: забрать priority-кандидат из БД.

        Семантика:
          * независим от состояния обычных слотов (``acquire_slot`` /
            ``chat_inflight``); если все слоты заняты — priority всё равно
            пройдёт в AgentLoop;
          * ищет сообщения с ``content`` из списка priority-команд nanobot
            (``/stop``, ``/restart``, ``/status`` — через
            ``lib.channels.message_exchange.priority_command_contents()``,
            который читает реестр библиотеки);
          * не создаёт assistant-placeholder (команда не ответ);
          * не блокируется ``_chat_inflight`` (priority должен пройти даже
            для chat'а, у которого уже активна обычная задача);
          * после диспатча чистит claim/lease/msg_ctx; **не** делает
            ``release_slot`` (slot не занимался).

        Вызывается ``MessageExchange._poll_loop`` всегда (до проверки
        ``is_slot_free()``). Возвращает ``True`` если priority-сообщение
        обработано, ``False`` если кандидатов нет.
        """
        if self._print_worker_activity:
            await self._report_queue()
        try:
            return await self._poll_priority_once(exchange)
        except Exception as exc:
            self._journal_event(
                event_type="agent.degraded",
                summary=f"poll_priority_inbound failed: {exc}",
                payload={
                    "component": "_poll_priority_once",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            raise

    async def _poll_priority_once(self, exchange: MessageExchange) -> bool:
        """Реализация priority claim + dispatch.

        Шаги:
          1. ``_claim_one(priority_contents=...)`` — атомарный claim
             (та же логика, что в ``_poll_once``, плюс фильтр
             ``content = ANY(%s)`` для всех priority-команд).
          2. re-check статуса (race-fix из user_stop_signal).
          3. assistant-заглушка + ``metadata["answer_id"]`` — РОВНО как на
             общем пути ``_poll_once``, с тем же откатом при неудаче.
          4. Диспатчим через ``_handle_message`` с
             ``metadata["priority"]=True``, минуя ``acquire_slot`` и
             ``chat_inflight`` (объявленное исключение, обоснование ниже).
          5. Снимаем claim + msg_ctx + msg_chat.

        **Что здесь объявленное исключение, а что — снятый обход.** Различаются
        ОТБОРЫ, а не доставка: приоритет — свойство выборки, дальше задача идёт
        тем же путём, что и любая.

        Слот и ``chat_inflight`` НЕ берутся, и это исключение объявлено
        (решение владельца от 2026-10-04), потому что иначе команда
        прерывания не работает: priority-опрос зовётся ДО ``is_slot_free()``
        (``message_exchange._poll_loop:188``, комментарий ``:166-167`` прямо
        называет этот путь доставкой команд, «которые должны прерывать
        активные обычные turn'ы той же сессии»), а ``acquire_slot`` — это
        ``await self._semaphore.acquire()``. При ``max_concurrent=1`` и
        активном turn'е ``/stop`` ждал бы слот, который освободит только тот
        самый turn, который он и должен прервать: взаимоблокировка, а не
        задержка, плюс встаёт цикл опроса. **Предел ожидания на слоте здесь
        равен нулю** — ни ожидания, ни таймаута ожидания в коде не
        появляется. Лишний ``_release_slot`` из отката при сбое диспатча
        безопасен: ``exchange.release_slot`` идемпотентен по ключу (ранний
        ``return``, если ключа нет в ``_inflight``), поэтому счётчик занятых
        слотов не рассинхронизируется.

        Снятый обход — assistant-заглушка и ``answer_id``. Раньше здесь шли с
        ``assistant_msg_id=None``, и ответ на команду не доходил до чата
        вовсе: ``_resolve_turn_context`` не находил ни ``answer_id``
        (``:1780``), ни ``_msg_ctx`` (``:1784-1788``), срабатывал warning
        «cannot resolve turn context» и ``_cleanup_unresolvable_turn``, то
        есть пользователь команды ответа не видел. С откатом по умолчанию
        ``_mark_failed(..., None, "dispatch_error")`` платформа ещё и не могла
        удалить заглушку — ``assistant_msg_id`` ей не передавался.
        """
        from lib.channels.message_exchange import priority_command_contents

        row = await self._claim_one(priority_contents=priority_command_contents())
        if row is None:
            return False

        user_msg_id = str(row["id"])
        self._claimed_ids.add(user_msg_id)
        chat_id = str(row["chat_id"]) if row["chat_id"] else str(row["user_id"])
        user_id = str(row["user_id"]) if row["user_id"] else chat_id
        self._lifecycle_log("priority_claimed", user_msg_id, chat_id=chat_id)

        # Race-fix: после claim повторно проверяем статус (между SELECT
        # подзапроса и UPDATE захвата AW мог пометить cancelled).
        cur_status = await self._status_of(user_msg_id)
        if cur_status == "cancelled":
            self.logger.info(
                "user_stop_signal: priority skipping cancelled msg {} (chat={})",
                user_msg_id, chat_id,
            )
            self._claimed_ids.discard(user_msg_id)
            self._msg_ctx.pop(user_msg_id, None)
            return False

        content = row["content"] or ""

        raw_meta = _decode_jsonb(row["metadata"])
        raw_media = row["media"] or []
        if isinstance(raw_media, str):
            raw_media = json.loads(raw_media) if raw_media else []
        media: list[str] = raw_media if isinstance(raw_media, list) else []
        session_key = raw_meta.get("session_key") or f"postgres:{chat_id}"
        media = await self._decode_media_from_db(media, session_key)
        media_paths, _ = self._resolve_media_paths_and_hints(media)
        media = media_paths

        # Assistant-заглушка — до диспатча и тем же способом, что на общем
        # пути (``:1045``): ответ должен попасть в очередь по ``reply_to`` от
        # этой строки, а без её ``id`` исходящее не резолвится и ответ
        # теряется. Откат при неудаче — тоже общий (``:1054-1058``): задача
        # возвращается в ``pending`` и ждёт следующего опроса.
        try:
            assistant_msg_id = await self._insert_assistant_message(
                user_msg_id, chat_id
            )
            self._lifecycle_log(
                "assistant_created", user_msg_id, chat_id=chat_id,
                assistant_msg_id=assistant_msg_id,
            )
        except Exception:
            self.logger.exception(
                "Failed to insert assistant placeholder for {}", user_msg_id,
            )
            await self._ops.update_task_status(
                user_msg_id, "pending", role="user",
                session_id=f"chat:{chat_id}", user_id=user_id,
            )
            self._claimed_ids.discard(user_msg_id)
            return False

        # Priority не занимает обычный slot и не блокирует chat для
        # последующих сообщений (исключение объявлено в докстринге выше).
        # ``_msg_chat`` нужен: отсюда ``_mark_failed`` берёт чат.
        self._msg_chat[user_msg_id] = chat_id
        self._activity_print(
            "claimed_priority",
            task=user_msg_id,
            chat=chat_id,
            detail=self._preview(content),
        )

        meta: dict[str, Any] = {
            "message_id": user_msg_id,
            "answer_id": assistant_msg_id,
            "priority": True,
            **raw_meta,
        }

        try:
            await self._handle_message(
                sender_id=user_id,
                chat_id=chat_id,
                content=content,
                media=media,
                metadata=meta,
            )
        except Exception:
            self.logger.exception(
                "Failed to dispatch priority message {}", user_msg_id,
            )
            # Тот же откат, что на общем пути (``:1086``), и с настоящим
            # ``assistant_msg_id`` — платформа по нему удаляет заглушку,
            # чтобы пользователь не видел ошибочный статус.
            await self._mark_failed(user_msg_id, assistant_msg_id, "dispatch_error")
            return True

        # Claim и msg_chat снимаем сразу, как и раньше: этот вызов не держит
        # слот, а ответ придёт по ``meta["answer_id"]`` — якорь теперь в
        # meta, а не в ``_msg_ctx``, поэтому ранний ``pop`` последствий не
        # имеет. ``_chat_inflight`` тут не трогаем: флагом чата владеет
        # обычный путь, и снимать его чужой рукой нельзя.
        # ``_handle_message`` через ``bus.publish_inbound`` доставит ``/stop``
        # в AgentLoop.run(), где ``commands.is_priority(raw)`` инициирует
        # ``cmd_stop`` → ``_cancel_active_tasks(effective_key)``.
        self._claimed_ids.discard(user_msg_id)
        self._msg_ctx.pop(user_msg_id, None)
        self._msg_chat.pop(user_msg_id, None)
        self._activity_print(
            "handled_priority", task=user_msg_id, chat=chat_id,
        )
        return True

    async def poll_inbound(self, exchange: MessageExchange) -> bool:
        """Хук транспорта для ``MessageExchange``: берет новое сообщение из БД.

        Откат зависших ``processing``:
          * фоновая задача ``_unstick_loop`` каждые ``unstick_interval``
            секунд (по дефолту значительно больше ``poll_interval``, чтобы
            не дёргать БД на пустом столе).

        Возвращает True, если сообщение обработано.
        """
        if self._print_worker_activity:
            await self._report_queue()
        if not exchange.is_slot_free():
            return False
        try:
            had = await self._poll_once(exchange)
            return bool(had)
        except Exception as exc:
            self._journal_event(
                event_type="agent.degraded",
                summary=f"poll_inbound/_poll_once failed: {exc}",
                payload={
                    "component": "_poll_once",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            raise

    async def _unstick_processing(self) -> list[str]:
        """Освободить сообщения, зависшие в ``processing`` дольше таймаута.

        Вызывается из фоновой ``_unstick_loop`` раз в ``unstick_interval``
        секунд (по дефолту ~processing_timeout/5, минимум 60 сек).

        Механизм:
          — ``processing`` дольше ``processing_timeout`` → повторная попытка.
          — ``retry_count >= max_stuck_retries`` → ``failed`` (терминал).
          — Старый assistant-placeholder удаляется, чтобы пользователь не
            видел ошибочный статус до повторной обработки.

        Возвращает список ``user_msg_id``, которые были фактически
        восстановлены (возвращены в ``pending`` или терминально ``failed``).
        Это позволяет вызывающему коду очистить локальное состояние
        (``_msg_ctx``, ``_claimed_ids``, ``_msg_chat``, ``_chat_inflight``,
        ``exchange.inflight``) для задач, которые этот воркер уже
        «забыл» — иначе после ``unstick`` локал остался бы занятым, и
        polling не взял бы новые сообщения.
        """
        max_retries = self._max_stuck_retries
        timeout_s = self._processing_timeout

        # Отбор зависших, счётчик попыток, терминальный переход, правка
        # assistant-строки и зачистка осиротевших ответов — одна транзакция на
        # платформе. Разнести это на вызовы значило бы вернуть в канал ровно
        # ту гонку, ради устранения которой операция и вынесена: счётчик
        # попыток растёт, ответ удаляется, а задача остаётся в обработке.
        recovered = await self._ops.unstick_tasks(
            processing_timeout_sec=timeout_s,
            max_stuck_retries=max_retries,
        )
        for msg_id in recovered:
            self.logger.warning(
                "Released or failed stuck user msg {} (max retries {})",
                msg_id, max_retries,
            )

        return recovered

    async def _unstick_loop(self) -> None:
        """Фоновая задача: периодически откатывает зависшие ``processing``.

        Интервал — значительно больше ``poll_interval``, чтобы на пустом
        столе ``SELECT зависших`` не выполнялся каждые ``poll_interval``
        секунд.

        Для каждого восстановленного ``user_msg_id`` снимает локальное
        состояние воркера (``_msg_ctx``, ``_claimed_ids``, ``_msg_chat``,
        ``_chat_inflight``, ``exchange.inflight``). Без этого воркер
        остался бы с заполненным слотом, и polling не поднял бы новые
        сообщения даже после восстановления БД.
        """
        while self._running:
            await asyncio.sleep(self._unstick_interval)
            if not self._running:
                break
            try:
                recovered = await self._unstick_processing()
                for msg_id in recovered:
                    if msg_id in self._msg_ctx or msg_id in self.exchange.inflight:
                        self._msg_ctx.pop(msg_id, None)
                        self._claimed_ids.discard(msg_id)
                        self._release_slot(msg_id)
                        self.logger.info(
                            "Cleared local state for unstuck msg {}", msg_id,
                        )
            except Exception as e:
                self.logger.error("Unstick loop error: {}", e)
                self._journal_event(
                    event_type="agent.degraded",
                    summary=f"unstick (восстановление processing) failed: {e}",
                    payload={
                        "component": "_unstick_loop",
                        "error_type": type(e).__name__,
                        "error": str(e),
                    },
                )

    async def _claim_one(
        self,
        *,
        priority_contents: tuple[str, ...] | None = None,
    ) -> dict | None:
        """Атомарно захватить одну задачу и перевести её в ``processing``.

        Захват идёт одним ``UPDATE ... RETURNING``: подзапрос выбирает самую
        старую подходящую задачу, внешний ``WHERE`` требует, чтобы её статус
        всё ещё был захватываемым. Если задачу параллельно взял другой захват,
        статус уже ``processing``, и повторный UPDATE не срабатывает — двойная
        обработка невозможна. Таблица аренды не используется: состояние захвата
        хранится в самой строке задачи. Дополнительный фильтр — чат без
        активной ``processing`` user-задачи.

        **Внешний ``WHERE`` повторяет условие подзапроса, а не сужает его.**
        Раньше внешний фильтр был ``status = 'pending'``, и этим он делал
        ветку повтора ``status='error'`` в подзапросе недостижимой: подзапрос
        выбирал задачу с просроченным backoff'ом, а внешний ``WHERE`` её
        отсекал. ``_mark_failed`` обещает вернуть задачу в пул после
        ``error_retry_delay``, и обещание не выполнялось никогда — задача
        оставалась в ``error`` навсегда. Условие в двух местах должно быть
        одинаковым ещё и потому, что внешний ``WHERE`` — это перепроверка под
        конкурентный UPDATE: сужение до ``pending`` означало бы, что задача,
        только что помеченная соседним воркером в ``error`` (с обновлённым
        ``updated_at``, то есть с ещё не истёкшим backoff'ом), забиралась бы
        немедленно и без паузы.

        user_stop_signal: ``status != 'cancelled'`` в обоих подзапросах — если
        AW пометил user-сообщение как ``cancelled`` ДО того, как polling успел
        его захватить, polling его пропускает (race-free: ``UPDATE ... WHERE
        id = (...)`` сам по себе атомарен, а условие ``status != 'cancelled'``
        в обоих WHERE гарантирует, что захват не произойдёт).

        Если ``priority_contents`` задан (кортеж строк) — добавляется фильтр
        ``AND content = ANY(%s)`` в обоих WHERE. Используется для priority
        polling path (например, ``/stop``, ``/restart``, ``/status``).

        Параметры позиционные, и их порядок — это порядок плейсхолдеров в
        тексте: backoff подзапроса, затем (опционально) список priority,
        затем backoff внешнего ``WHERE``. Расхождение порядка здесь не
        синтаксическая ошибка, а тихая подмена: ``error_retry_delay``
        попал бы в ``ANY(%s)``, и priority-путь отсекался бы всегда.

        SQL переехал на платформу операцией ``claim_task`` — там же и
        гарантия атомарности захвата. Здесь важно другое: возвращается ровно
        одна задача или ``None``, и ``None`` — это «очередь пуста», а не
        «запрос не разобран». Второе раньше выглядело бы так же.
        """
        return await self._ops.claim_task(
            error_retry_delay_sec=self._error_retry_delay,
            priority_contents=priority_contents,
        )

    def _session_id_for(self, msg_id: str | None) -> str | None:
        """Идентификатор сессии оборота для подписи вызова.

        Известен по чату, к которому принадлежит задача. ``None`` — оборот
        не разобран, и подпись соберётся из идентификатора сообщения: лучше
        слабая, но честная привязка, чем отказ обслуживать очередь.
        """
        if not msg_id:
            return None
        chat_id = self._msg_chat.get(msg_id)
        return f"chat:{chat_id}" if chat_id else f"task:{msg_id}"

    def _user_id_for(self, msg_id: str | None) -> str | None:
        """Пользователь оборота для подписи вызова."""
        if not msg_id:
            return None
        ctx = self._msg_ctx.get(msg_id) or {}
        return ctx.get("user_id") or None

    async def _status_of(self, user_msg_id: str) -> str | None:
        """Текущий статус задачи. ``None`` — строки нет.

        Нужна перепроверке отмены после захвата: AW может пометить задачу
        ``cancelled`` между отбором кандидата и ``UPDATE`` захвата, и тогда
        наш ``SELECT`` уже прошёл, а захват состоялся.
        """
        row = await self._ops.get_message(user_msg_id)
        return str(row.get("status")) if row else None

    async def _poll_once(self, exchange: MessageExchange) -> bool:
        """Забрать самое старое сообщение (через клейм) и отправить агенту.

        Алгоритм:
          1. ``_claim_one`` — атомарный клейм задачи (INSERT claim + processing)
          2. user_stop_signal: re-check статуса — если AW пометил
             user-сообщение как ``cancelled`` МЕЖДУ ``_claim_one`` и
             ``_handle_message`` (race window ~миллисекунды, но возможен
             при сетевой задержке), polling НЕ диспатчит и освобождает claim
          3. Проверяем, не занят ли chat_id в этом процессе (chat_inflight)
          4. Создаём assistant-placeholder (чтобы web-клиент мог опрашивать)
          5. Захватываем слот (exchange) → _handle_message

        Если из этого chat_id уже есть активное сообщение в этом процессе,
        возвращаем claim и статус в 'pending' — не диспатчим второе.
        Эксклюзивность клейма (PK claims) гарантирует, что одна задача не
        обрабатывается двумя воркерами одновременно.

        Возвращает True, если сообщение взято в обработку.
        """
        row = await self._claim_one()
        if row is None:
            return False  # нет новых сообщений

        user_msg_id = str(row["id"])
        self._claimed_ids.add(user_msg_id)
        chat_id = str(row["chat_id"]) if row["chat_id"] else str(row["user_id"])
        user_id = str(row["user_id"]) if row["user_id"] else chat_id
        self._lifecycle_log("claimed", user_msg_id, chat_id=chat_id)

        # user_stop_signal: re-check после claim. Если user-сообщение уже
        # было помечено как 'cancelled' в момент polling'а — откатываем claim
        # и пропускаем. Без этого проверка в claim'е (status != 'cancelled'
        # в WHERE) спасает только от race ДО claim; если AW пишет
        # 'cancelled' ПОСЛЕ SELECT подзапроса, но ДО UPDATE захвата — наш
        # SELECT уже прошёл, и мы захватили запись. Эта повторная проверка
        # закрывает окно race.
        cur_status = await self._status_of(user_msg_id)
        if cur_status == "cancelled":
            self.logger.info(
                "user_stop_signal: skipping cancelled msg {} (chat={})",
                user_msg_id, chat_id,
            )
            # Снимаем задачу с локального учёта захвата; статус уже
            # 'cancelled' (AW поставил), не трогаем его.
            self._claimed_ids.discard(user_msg_id)
            self._msg_ctx.pop(user_msg_id, None)
            return False

        content = row["content"] or ""

        # Не диспатчим, если из этого chat_id уже есть активное сообщение
        # в этом же процессе (в БД chat уже считается занятым, но защищаемся
        # от гонки между клеймом и фактическим диспатчем).
        if chat_id in self._chat_inflight:
            await self._ops.update_task_status(
                user_msg_id, "pending", role="user",
                session_id=f"chat:{chat_id}", user_id=user_id,
            )
            self._claimed_ids.discard(user_msg_id)
            self.logger.debug(
                "Deferred msg {} from busy chat {}", user_msg_id, chat_id,
            )
            return False

        raw_meta = _decode_jsonb(row["metadata"])

        raw_media = row["media"] or []
        if isinstance(raw_media, str):
            raw_media = json.loads(raw_media) if raw_media else []
        media: list[str] = raw_media if isinstance(raw_media, list) else []
        # Декодируем data URL из БД обратно в локальные файлы сессии
        session_key = raw_meta.get("session_key") or f"postgres:{chat_id}"
        media = await self._decode_media_from_db(media, session_key)
        # Агенту передаём только пути к файлам. Текстовое описание вложений
        # (с путём) — единая ответственность ``extract_documents`` (см.
        # ``RuntimePatcher.patch_document_text_threshold``); сюда НЕ
        # дописываем хинты, чтобы не было дублирования «файл там-то».
        media_paths, _ = self._resolve_media_paths_and_hints(media)
        media = media_paths

        # Создаём assistant-placeholder, чтобы web-сервер мог начать опрос.
        try:
            assistant_msg_id = await self._insert_assistant_message(user_msg_id, chat_id)
            self._lifecycle_log(
                "assistant_created", user_msg_id, chat_id=chat_id,
                assistant_msg_id=assistant_msg_id,
            )
        except Exception:
            self.logger.exception(
                "Failed to insert assistant placeholder for {}", user_msg_id,
            )
            await self._ops.update_task_status(
                user_msg_id, "pending", role="user",
                session_id=f"chat:{chat_id}", user_id=user_id,
            )
            self._claimed_ids.discard(user_msg_id)
            return False

        await exchange.acquire_slot()
        exchange.add_inflight(user_msg_id)
        self._chat_inflight.add(chat_id)
        self._msg_chat[user_msg_id] = chat_id
        self._activity_print(
            "claimed", task=user_msg_id, chat=chat_id,
            detail=self._preview(content),
        )

        meta: dict[str, Any] = {
            "message_id": user_msg_id,
            "answer_id": assistant_msg_id,
            **raw_meta,
        }

        try:
            await self._handle_message(
                sender_id=user_id,
                chat_id=chat_id,
                content=content,
                media=media,
                metadata=meta,
            )
        except Exception:
            self.logger.exception("Failed to dispatch user message {}", user_msg_id)
            await self._mark_failed(user_msg_id, assistant_msg_id, "dispatch_error")
        return True

    async def _insert_assistant_message(self, user_msg_id: str, chat_id: str) -> str:
        """Создать assistant-заглушку (status='processing') и сохранить её id.

        Зачем: чтобы web-сервер мог начать опрашивать ответ
        ДО того, как агент закончит генерацию. Как только агент завершит,
        ``send()`` обновит эту запись: content + status='completed'.

        Возвращает ``assistant_msg_id`` — ID созданной записи.
        """
        assistant_msg_id = await self._ops.append_assistant_message(
            chat_id=chat_id, reply_to=user_msg_id,
        )
        self._msg_ctx[user_msg_id] = {
            "assistant_msg_id": assistant_msg_id,
            "tool_events": [],
            "reasoning_buf": [],
        }
        self.logger.debug(
            "Inserted assistant placeholder {} for user msg {}",
            assistant_msg_id, user_msg_id,
        )
        return assistant_msg_id

    async def _mark_failed(self, user_msg_id: str, assistant_msg_id: str | None, reason: str) -> None:
        """Зафиксировать ошибку обработки пользовательского сообщения.

        Вызывается при:
          — ошибке диспетчеризации (\"dispatch_error\")
          — ошибке записи ответа (\"write_error\")

        Семантика статусов:
          — пока ``retry_count < max_stuck_retries`` → user='error'
            (повторяемая ошибка; задача вернётся в пул после
            ``error_retry_delay``), assistant-placeholder удаляется, чтобы
            пользователь не видел ошибочный статус до повторной обработки;
          — иначе → user='failed' (терминальный, не повторяется),
            assistant помечается failed с текстом ошибки.

        Дополнительно:
          — удаляет claim задачи (аренда завершена)
          — удаляет контекст из ``_msg_ctx``
          — освобождает слот (``_release_slot``)
          — чистит буфер рассуждений для этого assistant_msg_id
        """
        chat_id = self._msg_chat.get(user_msg_id)
        # Счётчик попыток, терминальный переход и правка assistant-строки —
        # одна транзакция на платформе. Разнести их на вызовы значило бы
        # оставить задачу с увеличенным счётчиком и старым статусом: лимит
        # повторов исчерпается, а обработана задача будет ни разу.
        try:
            outcome = await self._ops.fail_task(
                user_msg_id,
                assistant_msg_id,
                reason,
                self._max_stuck_retries,
                session_id=f"chat:{chat_id}" if chat_id else None,
                user_id=(self._msg_ctx.get(user_msg_id) or {}).get("user_id"),
            )
        except Exception as exc:  # noqa: BLE001 - ошибка не должна течь дальше
            self.logger.error(
                "не удалось зафиксировать ошибку задачи {}: {}", user_msg_id, exc,
            )
            return

        status = str(outcome.get("status") or "error")
        retry_count = int(outcome.get("retry_count") or 0)
        if status == "failed":
            self.logger.error(
                "User msg {} failed ({}/{}) [{}]",
                user_msg_id, retry_count, self._max_stuck_retries, reason,
            )
        else:
            self.logger.warning(
                "User msg {} error ({}/{}) [{}]",
                user_msg_id, retry_count, self._max_stuck_retries, reason,
            )
        self._lifecycle_log(
            "failed", user_msg_id, chat_id=chat_id,
            assistant_msg_id=assistant_msg_id,
            extra={"reason": reason, "status": status},
        )
        self._claimed_ids.discard(user_msg_id)
        self._msg_ctx.pop(user_msg_id, None)
        self._release_slot(user_msg_id)
        if assistant_msg_id:
            self._reasoning_buffers.pop(assistant_msg_id, None)
        self._drop_context_bridge(chat_id)
        self._activity_print(
            "finished", task=user_msg_id, chat=chat_id,
            detail=f"исход={status} причина={reason}",
        )

    def _drop_context_bridge(self, chat_id: str | None) -> None:
        """Снять per-iteration мост контекста для чата (анти-stale).

        Вызывается в финалах оборота (``_finalize_turn``, ``_mark_failed``):
        мост живёт ровно столько, сколько идёт активный оборот. Без
        ``pop`` следующий оборот того же чата унаследовал бы data от
        предыдущего (старый лимит/usage) при гонке рестарта воркера.
        """
        if not chat_id:
            return
        try:
            from lib.hooks.database_logging_hook import pop_context_bridge
            pop_context_bridge(f"postgres:{chat_id}")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Рассуждения (reasoning) — потоковая запись в metadata.reasoning
    # ------------------------------------------------------------------
    #
    # Агент может отправлять промежуточные рассуждения (chain-of-thought)
    # чанками через ``send_reasoning_delta``. Эти чанки:
    #   1. Буферизируются в ``_reasoning_buffers[assistant_msg_id]``
    #   2. Периодически сбрасываются в БД через ``_flush_reasoning``
    #   3. При финальном ответе ``send()`` остатки дописываются atomic
    #
    # Если assistant_msg_id ещё не известен (не создан placeholder),
    # данные временно складируются в ``_msg_ctx[msg_id][\"reasoning_buf\"]``.
    # ------------------------------------------------------------------

    async def send_reasoning_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
        *,
        stream_id: str | None = None,
    ) -> None:
        """Получить чанк рассуждений от агента и добавить в буфер.

        Параметры:
            chat_id — ID чата (не используется, т.к. берём из metadata)
            delta — текст очередного чанка рассуждений
            metadata — может содержать answer_id (assistant_msg_id)
            stream_id — наноtracing: идентификатор потока (на будущее,
                сейчас ключом буфера остаётся assistant_msg_id)

        Поведение:
            — Если известен assistant_msg_id → пишем в ``_reasoning_buffers``
            — Иначе → буферизируем в ``_msg_ctx`` (будет поднят позже)
        """
        del stream_id  # nanobot 0.3.0 передаёт; канал ключует по assistant_msg_id
        assistant_msg_id = self._resolve_assistant_msg_id(metadata)
        if assistant_msg_id:
            buf = self._reasoning_buffers.get(assistant_msg_id, "")
            self._reasoning_buffers[assistant_msg_id] = buf + delta
            return
        # Fallback: assistant_msg_id ещё не известен — буфер в _msg_ctx
        msg_id = (metadata or {}).get("origin_message_id") or (metadata or {}).get("message_id")
        if msg_id:
            ctx = self._msg_ctx.setdefault(msg_id, {})
            ctx.setdefault("reasoning_buf", []).append(delta)

    async def send_reasoning_end(
        self,
        chat_id: str,
        metadata: dict[str, Any] | None = None,
        *,
        stream_id: str | None = None,
    ) -> None:
        """Сигнал конца рассуждений. Не используется — финализация в send().

        Принимает ``stream_id`` для совместимости с ``nanobot 0.3.0``
        (ChannelManager._send_reasoning_end передаёт его kwarg).
        """
        del stream_id

    # ------------------------------------------------------------------
    # Отправка ответа (outbound) — принимает сообщения от агента
    # ------------------------------------------------------------------
    #
    # Через этот метод проходят все исходящие сообщения от агента.
    # В зависимости от флагов в metadata сообщение может быть:
    #
    #   ``_reasoning_delta`` — чанк рассуждений → буфер → БД
    #   ``_reasoning_end``   — конец рассуждений (игнорируется)
    #   ``_progress``        — промежуточный прогресс (тул-коллы)
    #   ``FINAL_TURN_KEY`` (``_final_turn``) — МАРКЕР КОНЦА ОБОРОТА → финал
    #   ``_record_channel_delivery`` — промежуточная публикация тула
    #                          ``message(...)`` → только merge, не финал
    #   ``latency_ms``       — признак legacy-финала без ``_final_turn``
    #   (без флагов)         — merge (промежуточная публикация)
    #
    # Ключевой контракт: финализировать (статус completed + удалить claim +
    # освободить слот + убрать ``_msg_ctx``) можно ТОЛЬКО один раз за оборот
    # и только на финальном outbound. Публикации тула ``message(...)``
    # приходят ПОСЛЕДОВАТЕЛЬНО в течение оборота (до его завершения) и НЕ
    # должны трогать слот/клейм/аренду/_msg_ctx — иначе оборот рвётся и
    # задача уходит в ``failed`` через reclaim. Поэтому промежуточные
    # публикации merge'ятся в существующую assistant-строку, а завершение
    # делается на маркере ``_final_turn`` (ставится патчем
    # ``_assemble_outbound``, см. ``runtime_patcher``).
    # ------------------------------------------------------------------

    async def send(self, msg: OutboundMessage) -> None:
        meta = dict(msg.metadata or {})
        msg_id = meta.get("origin_message_id") or meta.get("message_id")

        # v0.3.0: runtime-события (прогресс тулов, рассуждения, стриминг)
        # переносятся типизированным полем OutboundMessage.event, а не
        # legacy-флагами в metadata. Такие события обрабатываются отдельными
        # методами (send_delta / send_reasoning_delta), а в send() они
        # попадают только как побочный прогресс (например, ProgressEvent
        # c пустым content после выполнения тула message). Если их
        # обработать как финальный ответ — они перезапишут уже записанные
        # content/media пустыми значениями, и вложение тула message
        # пропадёт из БД.
        #
        # До раннего return фильтруем ``ContextCompactionEvent`` через
        # ``CompactionEventSubscriber``: канал остаётся «тупым» транспортом
        # (SRP), а сервис-подписчик занимается бизнес-логикой компакции
        # (history-notice + agent_gateway_logs запись).
        if msg.event is not None:
            subscriber = getattr(self, "_compaction_event_subscriber", None)
            if subscriber is not None:
                try:
                    await subscriber.feed(msg)
                except Exception:
                    self.logger.opt(exception=True).warning(
                        "compaction_event_subscriber feed failed for {}",
                        getattr(msg, "session_key", None),
                    )
            return

        # --- Чанк рассуждений — буферизируем, в БД попадёт через flush ---
        if meta.get("_reasoning_delta"):
            if msg.content:
                assistant_msg_id = self._resolve_assistant_msg_id(meta)
                if assistant_msg_id:
                    buf = self._reasoning_buffers.get(assistant_msg_id, "")
                    self._reasoning_buffers[assistant_msg_id] = buf + msg.content
                elif msg_id:
                    ctx = self._msg_ctx.setdefault(msg_id, {})
                    ctx.setdefault("reasoning_buf", []).append(msg.content)
            return

        if meta.get("_reasoning_end"):
            return

        # --- Промежуточный прогресс (тул-коллы) — копим, не пишем ---
        if meta.get("_progress"):
            if msg_id:
                # защита от утечки _msg_ctx: удаляем только те записи,
                # которые уже не в полёте (не в _inflight)
                if len(self._msg_ctx) > 100:
                    stale = [k for k in self._msg_ctx if k not in self.exchange.inflight]
                    for k in stale:
                        self._msg_ctx.pop(k, None)
                ctx = self._msg_ctx.setdefault(msg_id, {"reasoning_buf": []})
            return

        # --- Конец оборота — финализируем слот/клейм/статус ---
        # Маркер ``_final_turn`` ставит патч ``_assemble_outbound`` (или
        # приходит как синтетический outbound при подавленном финале после
        # message(...)). ``_turn_end`` — legacy-сигнал runner'а, тоже финал.
        if meta.get(FINAL_TURN_KEY) or meta.get("_turn_end"):
            await self._finalize_turn(msg, meta, msg_id)
            return

        # --- Промежуточная публикация тула message(...) — merge, НЕ финал ---
        # Сообщение пришло до завершения оборота: дописываем content/media
        # в assistant-строку, но не трогаем слот/клейм/аренду/_msg_ctx.
        if meta.get("_record_channel_delivery"):
            await self._merge_tool_delivery(msg, meta, msg_id)
            return

        # --- Прочие служебные сигналы runner'а (_tool_hint, _stream_end...) ---
        if is_dropped(meta):
            return

        # --- Legacy-финал без ``_turn_end`` (патч не применился) ---
        # Реальный ``_assemble_outbound`` ВСЕГДА кладёт ``latency_ms``;
        # промежуточные публикации ``message(...)`` его не несут.
        if meta.get("latency_ms") is not None:
            await self._finalize_turn(msg, meta, msg_id)
            return

        # --- Обычная промежуточная публикация без явных флагов ---
        # (напр. ``message("text")`` без media, где ``_record_channel_delivery``
        # не выставлен). До завершения оборота — только merge.
        await self._merge_tool_delivery(msg, meta, msg_id)

    async def _merge_tool_delivery(
        self, msg: OutboundMessage, meta: dict[str, Any], msg_id: str | None,
    ) -> None:
        """Дописать промежуточную публикацию тула ``message(...)`` в сроку.

        Вызывается на outbound, пришедших ДО завершения оборота. В отличие
        от ``_finalize_turn`` НЕ трогает слот, клейм, аренду и ``_msg_ctx``:
        оборот ещё продолжается, финализация произойдёт на ``_turn_end``.

        content накапливается (через newline), media — мержится без дублей,
        metadata обновляется. status остаётся ``processing``.
        """
        assistant_msg_id = self._resolve_assistant_msg_id(meta)
        if not assistant_msg_id and msg_id:
            assistant_msg_id = (self._msg_ctx.get(msg_id) or {}).get("assistant_msg_id")
        if not assistant_msg_id:
            self.logger.warning(
                "send: merge skipped, no assistant_msg_id for msg_id={}", msg_id,
            )
            return

        db_media = await self._embed_media_for_db(msg.media or [])
        try:
            # Накопление контента и слияние media - read-modify-write по
            # строке, и он обязан быть одним вызовом: два конкурирующих
            # merge'а прочитали бы одно и то же старое значение, и правка
            # одного потерялась бы молча - обе выглядели бы удачными.
            await self._ops.merge_tool_delivery(
                assistant_msg_id,
                content=msg.content or "",
                metadata_patch=meta,
                buttons=list(msg.buttons or []),
                media=db_media,
                session_id=self._session_id_for(msg_id),
                user_id=self._user_id_for(msg_id),
            )
        except Exception:
            self.logger.exception(
                "Failed to merge tool delivery for msg_id={}", msg_id,
            )

    async def _finalize_turn(
        self, msg: OutboundMessage, meta: dict[str, Any], msg_id: str | None,
    ) -> None:
        """Зафинализировать оборот: записать ответ, закрыть claim и слот.

        Единственное место, где снимается ``_msg_ctx``, ставится
        ``status='completed'``, удаляется claim и освобождается слот.

        Инвариант порядка (P0 — «DB-first»):
          1. ``_resolve_turn_context`` — собрать user/assistant/chat
          2. **DB transaction** (UPDATE assistant → UPDATE user)
          3. Только после успешного commit:
             ``_msg_ctx.pop`` → ``_claimed_ids.discard`` → ``_release_slot``.
          4. На исключении — ``_mark_failed`` (он сам управляет cleanup).

        Это исключает ситуацию «локально отпустили, а БД всё ещё processing».
        """
        ctx_meta = await self._resolve_turn_context(
            meta,
            chat_id=msg.chat_id,
            explicit_msg_id=msg_id,
        )
        user_msg_id = ctx_meta["user_msg_id"]
        assistant_msg_id = ctx_meta["assistant_msg_id"]
        chat_id = ctx_meta["chat_id"] or msg.chat_id

        if not user_msg_id or not assistant_msg_id:
            self.logger.warning(
                "send: cannot resolve turn context (user={}, assistant={}, "
                "meta_keys={}); forcing deterministic failure",
                user_msg_id, assistant_msg_id, sorted(meta.keys()),
            )
            await self._cleanup_unresolvable_turn(chat_id, user_msg_id, assistant_msg_id)
            return

        ctx = self._msg_ctx.get(user_msg_id) or {}

        self._lifecycle_log(
            "final_received", user_msg_id, chat_id=chat_id,
            assistant_msg_id=assistant_msg_id,
            extra={
                "origin_message_id": meta.get("origin_message_id"),
                "message_id": meta.get("message_id"),
                "answer_id": meta.get("answer_id"),
                "_final_turn": meta.get("_final_turn"),
                "_turn_end": meta.get("_turn_end"),
                "_stream_end": meta.get("_stream_end"),
                "streamed": meta.get("streamed"),
                "has_content": bool(msg.content),
                "resolver": ctx_meta.get("source"),
            },
        )

        # Проверки отмены здесь больше нет: она выполняется внутри
        # ``finalize_turn`` одной транзакцией с записью ответа. Отдельный
        # шаг оставлял окно - между «прочитал status» и «записал ответ»
        # отмена успевала прийти, и ответ ложился поверх задачи, которую
        # пользователь уже отменил. Теперь этот порядок невозможен.

        # Дописываем остатки рассуждений перед финальным ответом. Отдельно от
        # финализации: дописывание атомарно само по себе, а смешивать его с
        # закрытием оборота незачем.
        reasoning_delta = ""
        if assistant_msg_id in self._reasoning_buffers:
            delta = self._reasoning_buffers.pop(assistant_msg_id, "")
            if delta:
                reasoning_delta = delta
        if ctx.get("reasoning_buf"):
            buf = " ".join(ctx["reasoning_buf"])
            reasoning_delta = buf + (" " if reasoning_delta else "") + reasoning_delta
        if reasoning_delta:
            # Вне финализации и без локки: конкатенация атомарна на платформе.
            # Гонка, которую раньше прикрывал ``_reasoning_io_lock``, больше не
            # существует - блокировка была нужна ровно потому, что операция
            # не была атомарной.
            try:
                await self._ops.append_reasoning(assistant_msg_id, reasoning_delta)
            except Exception as exc:  # noqa: BLE001 - рассуждение не критично
                self.logger.warning(
                    "не удалось дописать рассуждение перед финализацией: {}", exc,
                )

        db_media = await self._embed_media_for_db(msg.media or [])

        try:
            # Ответ и статус задачи закрываются одной транзакцией на
            # платформе. Отдельной проверки отмены ДО этого вызова больше
            # нет: она перенесена внутрь ``finalize_turn``, потому что как
            # внешний шаг оставляла окно - отмена успевала прийти между
            # проверкой и записью, и ответ ложился поверх отменённой задачи.
            outcome = await self._ops.finalize_turn(
                user_msg_id,
                assistant_msg_id,
                content=msg.content or "",
                metadata_patch=meta,
                buttons=list(msg.buttons or []),
                media=db_media,
                session_id=f"chat:{chat_id}" if chat_id else None,
                user_id=(ctx or {}).get("user_id"),
            )
            if str(outcome.get("outcome")) == "cancelled_drop":
                self.logger.info(
                    "user_stop_signal: dropping final response for cancelled "
                    "user msg {} (chat={}, assistant={})",
                    user_msg_id, chat_id, assistant_msg_id,
                )
                self._lifecycle_log(
                    "cancelled_drop", user_msg_id, chat_id=chat_id,
                    assistant_msg_id=assistant_msg_id,
                )
                self._msg_ctx.pop(user_msg_id, None)
                self._claimed_ids.discard(user_msg_id)
                self._release_slot(user_msg_id)
                if chat_id:
                    self._drop_context_bridge(chat_id)
                self._activity_print(
                    "cancelled", task=user_msg_id, chat=chat_id,
                    detail="отменена пользователем",
                )
                return
            self._lifecycle_log(
                "db_committed", user_msg_id, chat_id=chat_id,
                assistant_msg_id=assistant_msg_id,
            )
        except Exception:
            self.logger.exception(
                "Failed to write response for user={} chat={}",
                user_msg_id, chat_id,
            )
            self._lifecycle_log(
                "finalization_error", user_msg_id, chat_id=chat_id,
                assistant_msg_id=assistant_msg_id, extra={"reason": "write_error"},
            )
            await self._mark_failed(
                user_msg_id, assistant_msg_id, "write_error",
            )
            return

        self._lifecycle_log(
            "local_released", user_msg_id, chat_id=chat_id,
            assistant_msg_id=assistant_msg_id,
        )
        self._msg_ctx.pop(user_msg_id, None)
        self._claimed_ids.discard(user_msg_id)
        self._release_slot(user_msg_id)
        if chat_id:
            self._drop_context_bridge(chat_id)
        self._activity_print(
            "finished", task=user_msg_id, chat=chat_id, detail="исход=completed",
        )

    async def send_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
        *,
        stream_id: str | None = None,
        stream_end: bool = False,
        resuming: bool = False,
    ) -> None:
        """Получить очередной чанк стримингового ответа от агента.

        Когда агент использует стриминг (потоковую генерацию),
        каждый фрагмент текста приходит через ``send_delta``.

        Поведение:
          — **накопление**: текст дописывается в ``_stream_buffers[stream_id]``
            (или по ``_stream_id`` из metadata, или по ``chat_id``);
          — **stream_end=True** → формируется синтетический финальный
            OutboundMessage с накопленным контентом и пробрасывается в
            единый ``_finalize_turn`` (тот же путь, что ``_turn_end``).

        ``stream_end`` всегда завершает оборот, даже если контент пустой:
        в этом случае ``_finalize_turn`` возьмёт уже накопленный текст из
        assistant-строки (через ``existing_content``), либо запишет пустую
        строку как content (но lifecycle закроется в БД).

        ``send_delta`` НЕ управляет lifecycle финализации напрямую — это
        исключает двойную запись в БД и рассинхрон с ``_finalize_turn``.
        """
        del resuming  # буфер ключуется по stream_id; resuming не нужен
        meta = dict(metadata or {})
        buf_key = stream_id or meta.get("_stream_id") or chat_id

        if not (stream_end or meta.get("_stream_end")):
            buf = self._stream_buffers.get(buf_key, "")
            self._stream_buffers[buf_key] = buf + delta
            return

        content = self._stream_buffers.pop(buf_key, "")
        # Прокидываем «streamed» в metadata, чтобы финализатор не потерял
        # признак стриминга в журнале.
        final_meta = dict(meta)
        final_meta["streamed"] = True
        final_meta["_final_turn"] = True
        # msg_id достаём так же, как в resolve, чтобы пустой контент не
        # приводил к потере контекста.
        msg_id_hint = (
            meta.get("origin_message_id") or meta.get("message_id")
            or final_meta.get("origin_message_id")
        )

        synthetic = OutboundMessage(
            channel=self.name,
            chat_id=chat_id,
            content=content,
            media=[],
            metadata=final_meta,
            buttons=[],
        )
        await self._finalize_turn(synthetic, final_meta, msg_id_hint)

    # ------------------------------------------------------------------
    # Управление слотами параллельности
    # ------------------------------------------------------------------
    #
    # Семафор ``_semaphore`` ограничивает количество сообщений,
    # которые одновременно обрабатываются агентом (max_concurrent).
    #
    # Каждое сообщение при входе захватывает слот (semaphore.acquire),
    # при выходе отпускает (semaphore.release).
    #
    # ``_release_slot`` идемпотентен: если слот уже отпущен,
    # повторный вызов — no-op. Это защищает от двойного отпуска
    # при ошибках (send → _mark_failed → _release_slot).
    # ------------------------------------------------------------------

    def _release_slot(self, user_msg_id: str) -> None:
        """Освободить слот параллельности для указанного сообщения.

        Идемпотентен: можно вызывать多次 для одного id —
        второй вызов будет no-op (проверка в ``exchange.release_slot``).

        Дополнительно:
          — удаляет chat_id из ``_chat_inflight`` (если был)
          — удаляет запись из ``_msg_chat``
          — снимает задачу с локального учёта захвата (``_claimed_ids``)
        """
        if not user_msg_id:
            return
        self.exchange.release_slot(user_msg_id)
        self._claimed_ids.discard(user_msg_id)
        chat_id = self._msg_chat.pop(user_msg_id, None)
        if chat_id:
            self._chat_inflight.discard(chat_id)

    async def _cleanup_unresolvable_turn(
        self,
        chat_id: str | None,
        user_msg_id: str | None,
        assistant_msg_id: str | None,
    ) -> None:
        """Очистить локальное состояние при нерезолвенном контексте оборота.

        Используется, когда ``_resolve_turn_context`` не смог однозначно
        связать outbound с задачей (нет ``origin_message_id``/``answer_id``,
        не найден ``_msg_ctx``). В этом случае мы не можем корректно
        финализировать БД, но обязаны снять локальные хвосты, иначе
        слот/лист_инфлайт останутся занятыми → polling зависнет.

        Алгоритм:
          1. Если есть хоть какой-то ``user_msg_id`` — ``_mark_failed``;
             на ошибке БД всё равно чистим локал.
          2. Иначе — ищем все user_msg_id в ``_msg_chat`` для ``chat_id``
             и чистим их напрямую (для одного чата воркер держит
             не более одной задачи).
          3. ``_drop_context_bridge`` для chat_id.
        """
        candidates: list[str] = []
        if user_msg_id:
            candidates.append(user_msg_id)
        elif chat_id:
            for mid, cid in list(self._msg_chat.items()):
                if cid == chat_id:
                    candidates.append(mid)

        for mid in candidates:
            self._lifecycle_log(
                "unresolvable_cleanup", mid, chat_id=chat_id,
                assistant_msg_id=assistant_msg_id,
            )
            await self._mark_failed(
                mid, assistant_msg_id, "unresolvable_context",
            )
            if mid in self._msg_ctx or mid in self.exchange.inflight:
                self._msg_ctx.pop(mid, None)
                self._claimed_ids.discard(mid)
                self._release_slot(mid)
        if chat_id:
            self._chat_inflight.discard(chat_id)
            self._drop_context_bridge(chat_id)

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    def _resolve_assistant_msg_id(self, metadata: dict[str, Any] | None) -> str | None:
        """Извлечь ``assistant_msg_id`` из metadata или ``_msg_ctx``.

        Приоритет:
          1. ``metadata["answer_id"]`` — напрямую
          2. ``_msg_ctx[msg_id]["assistant_msg_id"]`` — по message_id

        Возвращает None, если не удалось найти.
        """
        meta = metadata or {}
        answer_id = meta.get("answer_id")
        if answer_id:
            return str(answer_id)
        msg_id = meta.get("origin_message_id") or meta.get("message_id")
        if msg_id:
            ctx = self._msg_ctx.get(msg_id)
            if ctx:
                return ctx.get("assistant_msg_id")
        return None

    async def _resolve_turn_context(
        self,
        meta: dict[str, Any] | None,
        *,
        chat_id: str | None = None,
        explicit_msg_id: str | None = None,
        explicit_assistant_msg_id: str | None = None,
    ) -> dict[str, Any]:
        """Единый резолвер контекста оборота: ``user_msg_id`` +
        ``assistant_msg_id`` + ``chat_id``.

        Приоритет для ``user_msg_id`` (одно поле outbound):
          1. ``meta.origin_message_id`` / ``meta.message_id``;
          2. ``explicit_msg_id`` (если резолвер вызван из ``send_delta``);
          3. ``_msg_ctx`` (по ``origin_message_id`` → ``message_id`` → ``answer_id``);
          4. ``answer_id`` → SELECT assistant.reply_to (восстановление владельца).

        Приоритет для ``assistant_msg_id``:
          1. ``explicit_assistant_msg_id``;
          2. ``meta.answer_id``;
          3. ``_msg_ctx[user_msg_id]["assistant_msg_id"]``.

        Возвращает dict::

            {
                "user_msg_id": str | None,
                "assistant_msg_id": str | None,
                "chat_id": str | None,
                "source": "meta" | "ctx" | "reply_to" | None,
            }

        Гарантия: если оба ``user_msg_id`` и ``assistant_msg_id`` найдены,
        оборот можно финализировать; иначе — детерминированный ``failed``
        (см. ``_finalize_turn``).
        """
        m = dict(meta or {})
        result: dict[str, Any] = {
            "user_msg_id": None,
            "assistant_msg_id": None,
            "chat_id": chat_id,
            "source": None,
        }

        result["user_msg_id"] = (
            explicit_msg_id
            or m.get("origin_message_id")
            or m.get("message_id")
        )

        aid = explicit_assistant_msg_id or m.get("answer_id")
        if aid:
            result["assistant_msg_id"] = str(aid)

        if result["user_msg_id"] and not result["assistant_msg_id"]:
            ctx = self._msg_ctx.get(result["user_msg_id"]) or {}
            if ctx.get("assistant_msg_id"):
                result["assistant_msg_id"] = ctx["assistant_msg_id"]
                result["source"] = "ctx"

        if result["user_msg_id"] and not result["chat_id"]:
            chat_from_ctx = (self._msg_chat.get(result["user_msg_id"]))
            if chat_from_ctx:
                result["chat_id"] = chat_from_ctx

        if not result["user_msg_id"] and result["assistant_msg_id"]:
            aid = result["assistant_msg_id"]
            try:
                row = await self._ops.get_message(aid)
            except Exception:
                row = None
            if row and row.get("reply_to"):
                result["user_msg_id"] = str(row["reply_to"])
                result["source"] = "reply_to"

        if result["user_msg_id"] and not result["assistant_msg_id"]:
            ctx = self._msg_ctx.get(result["user_msg_id"]) or {}
            if ctx.get("assistant_msg_id"):
                result["assistant_msg_id"] = ctx["assistant_msg_id"]
                if not result["source"]:
                    result["source"] = "ctx"

        if result["user_msg_id"] and not result["source"]:
            result["source"] = "meta"

        return result

    # ------------------------------------------------------------------
    # Конфиг по умолчанию (для ``nanobot onboard``)
    # ------------------------------------------------------------------

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        """Пример конфигурации канала (вставляется в config.json)."""
        return {
            "enabled": True,
            "dsn": "postgresql://user:pass@localhost:5432/nanobot",
            "schema": "public",
            "table_name": "agent_conversation_messages",
            "poll_interval": 2.0,
            "flush_interval": 2.0,
            "max_concurrent": 1,
            "processing_timeout": 600,
            "error_retry_delay": 60.0,
            "max_stuck_retries": 3,
            "worker_id": "",
            "allow_from": ["*"],
            "unstick_interval": 120.0,
        }
