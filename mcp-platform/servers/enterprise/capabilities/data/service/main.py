"""Capability ``data`` — владелец доступа к PostgreSQL.

Сервис владеет пулом. Операции к нему не создают соединений и не импортируют
``psycopg2``: единственное место, где есть ``psycopg2``, — ``libs/enterprise_data``.

**Два входа (2.12).** ``submit`` — блокирующий, для работы с данными.
``accept`` — неблокирующий, буфер писателя журнала. Одна очередь означала бы,
что запрос модели конкурирует с записью журнала за воркеры пула; потеря события
безвозвратна и не сопровождается ошибкой, поэтому у них разные входы.

**Серверный предел стоимости (2.13).** ``statement_timeout`` выставляется на
сессии в момент выполнения задания и сбрасывается в ``finally``. Пул работает
с ``autocommit=True``, поэтому ``SET LOCAL`` здесь — нооп, и «надёжная» версия
на ``connect options`` потребовала бы изменения владельца пула.

Профиль вызывающей стороны, к которой относится операция, передан
параметром ``audience`` и не выводится по умолчанию: логирование не должно
писать в журнал входа в журнал.

**Снимок — тоже здесь.** Добавлено при миграции ``enterprise-mcp-platform``
(фаза 3): снимок DuckDB читается через переданный снаружи владелец
(``libs/enterprise_data/snapshot``), а наружу отдаются только методы чтения.
Так capability ``vectors`` получает строки отсюда и не знает ни про путь к
файлу, ни про DuckDB. Собственных tool'ов у чтения снимка нет: операции
принимают текст и имя индекса, а не SQL.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from libs.enterprise_common.errors import InfrastructureError, InvalidRequestError
from libs.enterprise_data.jsonb import decode_jsonb

from servers.enterprise.capabilities.data.service.writer import EventBuffer

logger = logging.getLogger(__name__)

#: Профили вызывающих. ``model`` — то, что видит агент; ``runtime`` — внутренние
#: потоки процесса (очередь задач, канал). Права операций различаются по
#: профилю, а не по имени вызывающего: иначе право на очередь появится у модели.
AUDIENCE_MODEL = "model"
AUDIENCE_RUNTIME = "runtime"

#: Уровни, которые принимает ``valid_level`` (CHECK в DDL журнала).
#: Журнал хранит верхний регистр, и всё, что с ним сравнивается, приводится
#: к нему же через :func:`normalize_level`.
_LOG_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")


def normalize_level(value: str | None) -> str:
    """Привести уровень к тому, что принимает CHECK-ограничение журнала.

    Пустое значение — ``INFO``. Неизвестное — отказ, а не тихая замена:
    опечатка в уровне, съеденная молча, выглядит в журнале как будто
    событие было важнее или менее важным, чем на самом деле.
    """
    candidate = (value or "").strip().upper()
    if not candidate:
        return "INFO"
    if candidate == "WARNING":
        candidate = "WARN"
    if candidate not in _LOG_LEVELS:
        raise InvalidRequestError(
            f"уровень {value!r} недопустим: журнал принимает {', '.join(_LOG_LEVELS)}"
        )
    return candidate


@dataclass(frozen=True)
class SearchHit:
    """Одна строка журнала в доменном виде."""

    id: str
    timestamp: Any
    event_type: str
    name: str
    level: str
    summary: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class SearchPage:
    """Страница результатов с признаком наличия следующей."""

    hits: tuple[SearchHit, ...]
    next_offset: int | None
    truncated: bool = False


class DataService:
    """Доступ к данным инфраструктуры и очереди задач."""

    def __init__(
        self,
        *,
        db: ModuleType | None = None,
        log_table: tuple[str, str] | None = None,
        question_runs_table: tuple[str, str] | None = None,
        expected_tables: tuple[str, ...] = (),
        statement_timeout_ms: int = 30_000,
        max_rows: int = 1000,
        buffer_maxlen: int = 2048,
        buffer_flush_interval: float = 5.0,
        snapshot: Any | None = None,
    ) -> None:
        self._db = db
        # Имена таблиц приходят из platform.json (ENTERPRISE_LOG_TABLE и
        # вопросы прогонов). Дефолта-имени здесь нет намеренно: литерал в
        # подписи делал бы значение декоративным — сервер поднимался бы и
        # писал бы не туда, если файл забыт.
        self._log_table = tuple(log_table) if log_table else None
        self._question_runs_table = (
            tuple(question_runs_table) if question_runs_table else None
        )
        self._expected_tables = tuple(expected_tables)
        self._statement_timeout_ms = int(statement_timeout_ms)
        self._max_rows = int(max_rows)
        self._buffer = EventBuffer(
            self._write_events,
            maxlen=buffer_maxlen,
            flush_interval=buffer_flush_interval,
        )
        # Владелец снимка (DuckDB) передаётся снаружи: сам сервис файл не
        # открывает и пути к нему не знает. Открывает его composition root
        # (server.py) через libs.enterprise_data.snapshot.open_snapshot_store.
        self._snapshot = snapshot

    def _require_log_table(self, operation: str) -> tuple[str, str]:
        """Таблица журнала — из настройки, иначе явная ошибка.

        ``None`` означает «операция не настроена», а не «писать в таблицу
        по умолчанию»: молчаливая подстановка здесь означала бы, что
        переименование таблицы в ``platform.json`` не мешает работе.
        """
        if not self._log_table:
            raise InfrastructureError(
                f"{operation}: ENTERPRISE_LOG_TABLE не задан — "
                f"операция журнала недоступна"
            )
        return self._log_table

    def _require_question_runs_table(self, operation: str) -> tuple[str, str]:
        if not self._question_runs_table:
            raise InfrastructureError(
                f"{operation}: таблица прогонов вопросов не задана — "
                f"операция недоступна"
            )
        return self._question_runs_table

    # -- снимок -------------------------------------------------------------
    #
    # Единственная точка, через которую другие capability читают снимок.
    # Capability ``vectors`` получает эти методы отсюда (container.get("data"))
    # и не знает ни про путь к файлу, ни про DuckDB. Прямого доступа к
    # ``self._snapshot`` наружу нет: второй путь к данным — это ровно то, чего
    # граница «один владелец» и не должна допускать.

    def _snapshot_store(self) -> Any:
        """Хранилище снимка.

        Raises:
            InfrastructureError: снимок в сервис не подключён. Молчаливый
                «снимка нет» выглядел бы как «индексов нет».
        """
        if self._snapshot is None:
            raise InfrastructureError(
                "снимок недоступен: хранилище не подключено к capability data "
                "(проверьте регистрацию в server.py)"
            )
        return self._snapshot

    def snapshot_is_ready(self) -> bool:
        """Готов ли снимок к чтению."""
        return bool(self._snapshot_store().is_ready())

    def snapshot_stats(self) -> dict[str, Any]:
        """Состояние хранилища снимка для диагностики."""
        return dict(self._snapshot_store().get_stats())

    def snapshot_query(
        self,
        sql: str,
        params: list[Any] | None = None,
    ) -> dict[str, Any]:
        """SELECT к снимку.

        Для **внутренних** потребителей (другие capability через контейнер);
        capability ``data`` не выставляет это в tool. Политика режима и запрет
        DDL обеспечиваются владельцем (``CacheProvider.query_sql``), здесь
        текст только переадресуется.
        """
        return dict(self._snapshot_store().query_sql(sql, params))

    def snapshot_explain(self, sql: str) -> dict[str, Any]:
        """Синтаксическая проверка SQL к снимку, без выполнения.

        Отдельный метод, а не ``snapshot_query("EXPLAIN ...")``: соединение с
        снимком открывает владелец снимка, и вызывающая сторона его не имеет.
        Capability ``audit`` проверяет сгенерированный SQL именно этим швом —
        раньше он дотягивался до ``explain_query`` напрямую и передавал
        туда вызываемый объект вместо соединения.
        """
        return dict(self._snapshot_store().explain(sql))

    def snapshot_schema(
        self,
        schema_name: str | None = None,
        table_names: list[str] | None = None,
    ) -> dict[str, Any]:
        """Структура таблиц снимка (``information_schema``).

        Для внутренних потребителей; в tool capability ``data`` не выставляется.
        """
        return dict(self._snapshot_store().get_schema(schema_name, table_names))

    def vector_source_stats(self) -> list[dict[str, Any]]:
        """Источники векторного индекса со счётчиками, **без** сборки FAISS.

        Отдаёт capability ``vectors`` для ``list_indexes``/``index_stats``.
        """
        return list(self._snapshot_store().vector_source_stats())

    def fetch_source_vectors(self, source: str) -> list[dict[str, Any]]:
        """Строки векторного хранилища одного источника (сборка индекса)."""
        return list(self._snapshot_store().fetch_source_vectors(source))

    def fetch_chunk_payload(
        self,
        source: str,
        pk_value: Any,
        chunk_index: int,
    ) -> dict[str, Any]:
        """Текст и исходная строка одного чанка (гидратация результата поиска)."""
        return dict(
            self._snapshot_store().fetch_chunk_payload(source, pk_value, chunk_index)
        )

    # -- доступ к пулу ------------------------------------------------------

    def _pool(self) -> ModuleType:
        if self._db is None:
            from libs.enterprise_data import db as real_db

            return real_db
        return self._db

    def submit(self, job: Any, *, audience: str = AUDIENCE_RUNTIME) -> Any:
        """Блокирующий вход: выполнить задание на воркере пула.

        Только работа с данными. Всё, что можно потерять, идёт через ``accept``.
        """
        pool = self._pool()
        try:
            return pool.run(lambda conn: self._guarded(conn, job))
        except InfrastructureError:
            raise
        except Exception as exc:  # noqa: BLE001 - наружу уходит доменная ошибка
            raise InfrastructureError(f"задание в пуле не выполнено: {exc}") from exc

    def accept(self, event: dict[str, Any]) -> str | None:
        """Неблокирующий вход: событие в буфер журнала.

        Возвращает ``None`` либо маркер отброшенного события. Исключений не
        бросает: потеря события не должна останавливать ход.
        """
        return self._buffer.accept(event)

    def _guarded(self, conn: Any, job: Any) -> Any:
        """Выставить предел стоимости, выполнить, сбросить предел."""
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {self._statement_timeout_ms}")
        try:
            return job(conn)
        finally:
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = 0")

    def start(self) -> None:
        self._buffer.start()

    def stop(self) -> None:
        self._buffer.stop()

    def stats(self) -> dict[str, Any]:
        return {"event_buffer": self._buffer.stats(), "max_rows": self._max_rows}

    # -- запись журнала -----------------------------------------------------

    def _write_events(self, events: list[dict[str, Any]]) -> None:
        """Записать батч событий журнала.

        Батч уходит циклом ``execute`` по одному соединению, а не одним
        вызовом со списком строк: контракт ``db.execute(sql, *args)`` — один
        параметр на плейсхолдер. Список строк в этом месте попадал в
        санитизацию как единственный параметр, ``clean_text`` на нём падал, и
        сброс молча терял весь батч — операция ``log_event`` отвечала
        «accepted» и при этом ничего не писала.

        Все строки батча идут в одном задании пула, то есть в одной
        транзакции: половина батча в журнале хуже, чем ничего.
        """
        schema, table = self._require_log_table("log_events")
        # Полный конверт события. Список колонок раньше обрывался на девяти
        # полях, и события, написанные платформой, теряли `request_id`,
        # `metadata`, `channel` и `actor`: без `request_id` оборот не
        # коррелировался, без `metadata` терялись source/component. Агентский
        # писатель (`lib/services/db_logging_service.py`) пишет те же двенадцать
        # полей, поэтому две половины журнала читались по разным схемам.
        sql = (
            f'INSERT INTO "{schema}"."{table}" '
            "(id, \"timestamp\", event_type, name, level, summary, payload, "
            "session_id, user_id, request_id, channel, actor, metadata) "
            "VALUES (%s, now(), %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s::jsonb)"
        )
        rows = [
            (
                # ``agent_gateway_logs.id`` — UUID NOT NULL без DEFAULT:
                # ключ события рождается в приложении, а не в базе. Раньше
                # сюда уходил NULL, и весь батч откатывался.
                event.get("id") or str(uuid.uuid4()),
                event.get("event_type", ""),
                event.get("name", ""),
                event.get("level", "info"),
                event.get("summary", ""),
                json.dumps(event.get("payload") or {}),
                event.get("session_id"),
                event.get("user_id"),
                event.get("request_id"),
                event.get("channel"),
                event.get("actor"),
                json.dumps(event.get("metadata") or {}),
            )
            for event in events
        ]
        if not rows:
            return

        def _work(conn: Any) -> None:
            with conn.cursor() as cur:
                for row in rows:
                    cur.execute(sql, row)

        self.submit(_work)

    def log_event(
        self,
        event_type: str,
        name: str = "",
        level: str = "INFO",
        summary: str = "",
        payload: dict[str, Any] | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        request_id: str | None = None,
        *,
        channel: str | None = None,
        actor: str | None = None,
        metadata: dict[str, Any] | None = None,
        event_id: str | None = None,
        audience: str = AUDIENCE_MODEL,
    ) -> str:
        """Записать событие журнала. Неблокирующий вход.

        Возвращает ``"accepted"`` либо ``"dropped"``: агент должен видеть
        переполнение, иначе он решит, что событие записано.

        Конверт события (§ ``runtime/event-model``) собирается целиком: кроме
        ``payload`` пишутся ``request_id``, ``channel``, ``actor`` и
        ``metadata``. Раньше здесь были только ``payload`` и идентичность, и
        событие, записанное агентом, нельзя было связать с оборотом по
        ``request_id``.
        """
        del audience  # логирование не пишет в журнал входа в журнал
        if not event_type.strip():
            raise InvalidRequestError("event_type не должен быть пустым")
        result = self.accept(
            {
                "id": event_id or str(uuid.uuid4()),
                "event_type": event_type,
                "name": name,
                # Нормализация здесь, на границе запроса: в буфер уходит уже
                # валидное значение, и сброс не может упасть из-за уровня.
                "level": normalize_level(level),
                "summary": summary,
                "payload": payload or {},
                "session_id": session_id,
                "user_id": user_id,
                "request_id": request_id,
                "channel": channel,
                "actor": actor,
                "metadata": metadata or {},
            }
        )
        return "dropped" if result is not None else "accepted"

    def log_events(
        self,
        events: list[dict[str, Any]],
        session_id: str | None = None,
        user_id: str | None = None,
        request_id: str | None = None,
        *,
        audience: str = AUDIENCE_MODEL,
    ) -> dict[str, int]:
        """Записать батч событий журнала одним вызовом.

        Зачем батч, если есть ``log_event``: агент копит события у себя
        (пункт 7.2 плана миграции) и за один MCP-вызов отправляет их пачкой.
        Без батча каждый чих оборота платил бы круговым оборотом по stdio.

        Здесь важна другая ступень, чем у буфера: ``log_events`` кладёт в буфер
        N событий одним заходом, а когда писать в базу — по-прежнему решает
        ``EventBuffer``.

        Валидация — fail-fast на первом негодном событии, как у ``log_event``:
        негодное событие означает ошибку на стороне агента, и молча выбросить
        его — значит похоронить дефект. Частичный приём здесь был бы хуже:
        вызывающий увидел бы «принято 99 из 100» и не узнал бы, что потерял.

        Идентичность приходит **на уровень вызова**, а не внутри события:
        ``session_id``, ``user_id`` и ``request_id`` — параметры метода, а в
        теле батча они игнорируются. Иначе батч, присланный под видом
        журналирования оборота, записал бы события в чужую сессию, и след в
        журнале был бы правдоподобным и неверным. Событие получает ровно ту
        личность, с которой пришёл вызов.

        Уровень проходит через ``normalize_level`` на границе запроса — до
        буфера. Иначе недопустимый уровень уехал бы в сброс и упал бы там на
        ``valid_level CHECK``, унося с собой весь батч, хотя вызывающий уже
        получил бы «accepted».

        Returns:
            ``{"accepted": n, "dropped": m}``. Переполнение буфера видно
            вызывающему, а не растворяется внутри.
        """
        del audience  # логирование не пишет в журнал входа в журнал
        if not isinstance(events, list) or not events:
            raise InvalidRequestError("events должен быть непустым списком")
        prepared: list[dict[str, Any]] = []
        for position, event in enumerate(events):
            if not isinstance(event, dict):
                raise InvalidRequestError(f"events[{position}] должен быть объектом")
            event_type = event.get("event_type")
            if not isinstance(event_type, str) or not event_type.strip():
                raise InvalidRequestError(
                    f"events[{position}].event_type не должен быть пустым"
                )
            prepared.append(
                {
                    "id": event.get("id") or str(uuid.uuid4()),
                    "event_type": event_type,
                    "name": str(event.get("name") or ""),
                    "level": normalize_level(event.get("level", "info")),
                    "summary": str(event.get("summary") or ""),
                    "payload": event.get("payload") or {},
                    "session_id": session_id,
                    "user_id": user_id,
                    "request_id": request_id,
                    "channel": event.get("channel"),
                    "actor": event.get("actor"),
                    "metadata": event.get("metadata") or {},
                }
            )
        dropped = self._buffer.accept_many(prepared)
        return {"accepted": len(prepared) - dropped, "dropped": dropped}

    # -- чтение журнала -----------------------------------------------------

    def history_search(
        self,
        query: str = "",
        event_type: str | None = None,
        level: str | None = None,
        tool_name: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
        offset: int = 0,
        *,
        audience: str = AUDIENCE_MODEL,
    ) -> SearchPage:
        """Поиск по журналу в пределах области видимости.

        Изоляция — часть контракта, а не рекомендация: поиск без ``user_id`` и
        без ``session_id`` отклоняется. В журнале лежат вопросы пользователей
        и внутренние события, и «покажи всё» — это утечка, а не удобство.
        """
        if not user_id and not session_id:
            raise InvalidRequestError(
                "нужен user_id или session_id: поиск по журналу без области видимости запрещён"
            )
        limit = max(1, min(int(limit), self._max_rows))
        offset = max(0, int(offset))

        clauses: list[str] = []
        params: list[Any] = []
        if user_id:
            clauses.append("user_id = %s")
            params.append(user_id)
        if session_id:
            clauses.append("session_id = %s")
            params.append(session_id)
        if event_type:
            clauses.append("event_type = %s")
            params.append(event_type)
        if level:
            clauses.append("level = %s")
            # Тот же регистр, что и при записи: фильтр, приведённый к
            # другой графе, молча не находит ничего.
            params.append(normalize_level(level))
        if tool_name:
            # ``name`` в журнале — имя инструмента для tool_call/tool_result.
            # Смысленно только вместе с ними, но фильтровать можно и без
            # event_type: не ограничиваем вызывающего его знанием схемы журнала.
            clauses.append("name = %s")
            params.append(tool_name)
        if since:
            clauses.append('"timestamp" >= %s')
            params.append(since)
        if until:
            clauses.append('"timestamp" <= %s')
            params.append(until)
        if query.strip():
            clauses.append("(summary ILIKE %s OR payload::text ILIKE %s)")
            needle = f"%{query.strip()}%"
            params.extend([needle, needle])

        schema, table = self._require_log_table("search_logs")
        # ``LIMIT N+1`` — лишняя строка детектирует наличие следующей страницы
        # одним запросом, без отдельного счётчика.
        sql = (
            f'SELECT id, "timestamp", event_type, name, level, summary, payload '
            f'FROM "{schema}"."{table}" '
            f"WHERE {' AND '.join(clauses)} "
            'ORDER BY "timestamp" DESC, id DESC '
            "LIMIT %s OFFSET %s"
        )
        params.extend([limit + 1, offset])

        rows = self.submit(lambda conn: _fetch(conn, sql, params), audience=audience)
        has_more = len(rows) > limit
        visible = rows[:limit]
        hits = tuple(
            SearchHit(
                id=str(row[0]),
                timestamp=row[1],
                event_type=row[2] or "",
                name=row[3] or "",
                level=row[4] or "",
                summary=row[5] or "",
                payload=decode_jsonb(row[6]) if row[6] is not None else {},
            )
            for row in visible
        )
        return SearchPage(
            hits=hits,
            next_offset=offset + limit if has_more else None,
            truncated=len(visible) >= self._max_rows,
        )

    # -- проверка схемы -----------------------------------------------------

    def schema_check(
        self,
        expected: tuple[str, ...] | None = None,
        *,
        audience: str = AUDIENCE_MODEL,
    ) -> dict[str, Any]:
        """Проверить наличие обязательных таблиц.

        Один запрос к ``information_schema`` вместо попытки открыть каждую
        таблицу: отсутствие таблицы должно отличаться от ошибки соединения, и
        различать их дешевле по одному списку, чем по N неудачным попыткам.
        """
        wanted = tuple(expected or self._expected_tables)
        if not wanted:
            raise InvalidRequestError("не задано ни одной ожидаемой таблицы")
        sql = (
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE (table_schema || '.' || table_name) = ANY(%s)"
        )
        rows = self.submit(lambda conn: _fetch(conn, sql, [list(wanted)]), audience=audience)
        found = {f"{row[0]}.{row[1]}" for row in rows}
        missing = sorted(name for name in wanted if name not in found)
        return {
            "expected": len(wanted),
            "found": len(found),
            "missing": missing,
            "ok": not missing,
        }

    # -- очередь задач ------------------------------------------------------
    #
    # Эти операции обслуживают канал агента, а не модель. Право на них есть
    # только у профиля ``runtime``: модель, захватившая задачу, увела бы её
    # у живого воркера.

    def _require_runtime(self, audience: str, operation: str) -> None:
        if audience != AUDIENCE_RUNTIME:
            raise InvalidRequestError(
                f"{operation} доступна только рантайму агента, профиль вызова: {audience}"
            )

    # ------------------------------------------------------------------
    # Контекст вопроса и очистка журнала (фаза 7)
    # ------------------------------------------------------------------

    def upsert_question_run(
        self,
        request_id: str,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        channel: str | None = None,
        agent_id: str | None = None,
        parent_agent_id: str | None = None,
        parent_request_id: str | None = None,
        is_subagent: bool = False,
        status: str | None = None,
        summary: str | None = None,
        question: str | None = None,
        response: str | None = None,
        media: list[Any] | None = None,
        update_only: bool = False,
        question_runs_table: tuple[str, str] | None = None,
        audience: str = AUDIENCE_RUNTIME,
    ) -> bool:
        """Upsert контекста вопроса в ``agent_question_runs``.

        Без ``ON CONFLICT``: Greenplum 6.5 (PostgreSQL 9.4) его не умеет, а
        целевая база именно такая. Паттерн переносимый и работает на
        PostgreSQL 13: UPDATE по ``request_id``, затем INSERT ... SELECT ...
        WHERE NOT EXISTS — второй шаг закрывает гонку «строки нет и после
        UPDATE».

        ``update_only`` (завершение прогона) трогает только
        ``updated_at``/``status``/``summary``/``response``/``media`` и через
        ``COALESCE`` не затирает ранее записанный контекст вопроса. Полный
        upsert (регистрация нового вопроса) перезаписывает контекст.

        Args:
            request_id: Идентификатор вопроса; пустой — отказ, а не запись.
            update_only: Обновлять ли только статус/ответ, не затирая контекст.
            question_runs_table: Таблица с указанием схемы; ``None`` — та, что
                задана при сборке сервера.

        Returns:
            ``True`` — запись выполнена.
        """
        self._require_runtime(audience, "upsert_question_run")
        if not request_id or not str(request_id).strip():
            raise InvalidRequestError("upsert_question_run: не задан request_id")

        table = _qualified(question_runs_table or self._require_question_runs_table("upsert_question_run"))
        # media хранится JSON-строкой в TEXT-колонке.
        media_json = json.dumps(media, ensure_ascii=False) if media else None

        if update_only:
            update_sql = (
                f"UPDATE {table} SET updated_at = now(), status = %s, summary = %s, "
                "response = COALESCE(%s, response), media = COALESCE(%s, media) "
                "WHERE request_id = %s"
            )
            update_params: list[Any] = [status, summary, response, media_json, request_id]
            insert_sql = (
                f"INSERT INTO {table} (request_id, status, summary, response, media) "
                "SELECT %s, %s, %s, %s, %s "
                f"WHERE NOT EXISTS (SELECT 1 FROM {table} WHERE request_id = %s)"
            )
            insert_params: list[Any] = [request_id, status, summary, response, media_json]
            insert_params.append(request_id)
        else:
            update_sql = (
                f"UPDATE {table} SET session_id = %s, user_id = %s, chat_id = %s, "
                "channel = %s, parent_request_id = %s, agent_id = %s, "
                "parent_agent_id = %s, is_subagent = %s, status = %s, summary = %s, "
                "question = %s, media = %s, updated_at = now() "
                "WHERE request_id = %s"
            )
            update_params = [
                session_id, user_id, chat_id, channel, parent_request_id, agent_id,
                parent_agent_id, bool(is_subagent), status, summary, question,
                media_json, request_id,
            ]
            insert_sql = (
                f"INSERT INTO {table} (request_id, session_id, user_id, chat_id, "
                "channel, parent_request_id, agent_id, parent_agent_id, is_subagent, "
                "status, summary, question, media) "
                "SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s "
                f"WHERE NOT EXISTS (SELECT 1 FROM {table} WHERE request_id = %s)"
            )
            insert_params = [
                request_id, session_id, user_id, chat_id, channel, parent_request_id,
                agent_id, parent_agent_id, bool(is_subagent), status, summary,
                question, media_json, request_id,
            ]

        def _work(conn: Any) -> None:
            with conn.cursor() as cur:
                cur.execute(update_sql, update_params)
                cur.execute(insert_sql, insert_params)

        self.submit(_work, audience=audience)
        return True

    def purge_logs(
        self,
        retention_days: int = 0,
        *,
        remove_empty_outbound: bool = True,
        log_table: tuple[str, str] | None = None,
        question_runs_table: tuple[str, str] | None = None,
        audience: str = AUDIENCE_RUNTIME,
    ) -> dict[str, int]:
        """Очистить журнал: пустые outbound-чанки и всё старше retention.

        Интервал считается как ``NOW() - (%s || ' days')::interval`` — без
        ``make_interval``, которого нет в Greenplum 6.5.

        Args:
            retention_days: Сколько дней хранить. ``0`` — старые записи не
                трогаются; пустой outbound-мусор чистится всегда, потому что он
                не несёт смысла ни в какой момент.
            remove_empty_outbound: Удалять ли пустые stream-чанки.
            log_table: Таблица журнала; ``None`` — заданная при сборке.
            question_runs_table: Таблица контекста; ``None`` — заданная при сборке.

        Returns:
            Счётчики удаления по таблицам.
        """
        self._require_runtime(audience, "purge_logs")
        try:
            days = int(retention_days)
        except (TypeError, ValueError) as exc:
            raise InvalidRequestError(
                f"purge_logs: retention_days должен быть целым, получено {retention_days!r}"
            ) from exc
        if days < 0:
            raise InvalidRequestError(
                f"purge_logs: retention_days не может быть отрицательным ({days})"
            )

        logs = _qualified(log_table or self._require_log_table("purge_logs"))
        # Таблица прогонов требуется только когда реально чистим по сроку:
        # ``days == 0`` её не касается, и отсутствие настройки тут не ошибка.
        runs = (
            _qualified(
                question_runs_table or self._require_question_runs_table("purge_logs")
            )
            if days > 0
            else None
        )
        counters = {"empty_outbound": 0, "events": 0, "question_runs": 0}

        def _work(conn: Any) -> dict[str, int]:
            result = dict(counters)
            with conn.cursor() as cur:
                if remove_empty_outbound:
                    # Реальные доставки файлов с пустым текстом сохраняются:
                    # у них есть media, и удаление стёрло бы сам факт отправки.
                    cur.execute(
                        f"DELETE FROM {logs} "
                        "WHERE event_type IN ('outbound_final', 'outbound_delta') "
                        "AND coalesce(btrim(payload->>'content'), '') = '' "
                        "AND (payload->'media') IS NULL"
                    )
                    result["empty_outbound"] = int(cur.rowcount)
                if days > 0:
                    cur.execute(
                        f'DELETE FROM {logs} WHERE "timestamp" < NOW() - (%s || \' days\')::interval',
                        (str(days),),
                    )
                    result["events"] = int(cur.rowcount)
                    cur.execute(
                        f"DELETE FROM {runs} WHERE updated_at < NOW() - (%s || ' days')::interval",
                        (str(days),),
                    )
                    result["question_runs"] = int(cur.rowcount)
            return result

        return dict(self.submit(_work, audience=audience) or counters)


def _qualified(table: tuple[str, str] | str) -> str:
    """Имя таблицы со схемой в кавычках.

    Схема и имя приходят аргументом операции, поэтому подставлять их в текст
    можно только через идентификатор в кавычках. Значения параметров идут
    плейсхолдерами — это другой случай и он не смешивается с этим.
    """
    if isinstance(table, str):
        schema, name = "public", table
    else:
        schema, name = table
    schema = str(schema).strip().strip('"')
    name = str(name).strip().strip('"')
    if not schema or not name:
        raise InvalidRequestError(f"не задано имя таблицы журнала: {table!r}")
    return f'"{schema}"."{name}"'

def _fetch(conn: Any, sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        if cur.description is None:
            return []
        return list(cur.fetchall())
