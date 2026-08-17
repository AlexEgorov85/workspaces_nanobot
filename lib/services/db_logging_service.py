"""DbLoggingService — структурированное логирование событий агента в PostgreSQL.

Импортируется БЕЗ nanobot (psycopg2 импортируется лениво — модуль годен
для тестов с мок-подключением и для сред без psycopg2).

Архитектура:
  * единственный worker-поток (``self._thread``) дренит очередь батчами;
  * сам сервис НЕ держит psycopg2-соединение: вставки идут через общий
    пул ``utils.db`` (``run(lambda conn: …)``) — воркер пула владеет
    соединением, сервис не плодит лишних подключений;
  * неблокирующие ``log_*`` методы ставят события в ``queue.Queue``;
  * worker батчем вставляет записи по ``flush_interval_sec`` или ``batch_size``;
  * если подключение к БД недоступно или вставка падает — события НЕ пишутся
    в JSONL-файл: они выбрасываются, а ошибка фиксируется в ``stats``
    (``failed`` / ``last_error``). Скрытой записи в файл нет;
  * ``stop(timeout_sec=15)`` отправляет SHUTDOWN-сентинел и дожидается
    опустошения очереди.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class LogEvent:
    """Одно событие для записи в БД (стройная таблица agent_gateway_logs).

    Контекст вопроса (user_id/agent_id/is_subagent/parent_*) живёт в
    отдельной таблице agent_question_runs (см. upsert_question_run) и здесь
    не дублируется — только request_id для связи.
    """

    event_type: str
    level: str = "INFO"
    session_id: Optional[str] = None
    channel: Optional[str] = None
    actor: Optional[str] = None
    summary: Optional[str] = None
    payload: Optional[dict] = None
    metadata: Optional[dict] = None
    request_id: Optional[str] = None
    name: Optional[str] = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class _FlushSentinel:
    pass


@dataclass
class _QuestionRunRecord:
    """Контекст вопроса для upsert в agent_question_runs (не в agent_gateway_logs)."""

    request_id: str
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    chat_id: Optional[str] = None
    channel: Optional[str] = None
    parent_request_id: Optional[str] = None
    agent_id: Optional[str] = None
    parent_agent_id: Optional[str] = None
    is_subagent: bool = False
    status: Optional[str] = None
    summary: Optional[str] = None
    question: Optional[str] = None
    response: Optional[str] = None
    media: Optional[list] = None
    update_only: bool = False


class DbLoggingService:
    """Фоновый writer событий агента в PostgreSQL (без fallback-JSONL)."""

    def __init__(
        self,
        dsn: Optional[str] = None,
        *,
        table_name: str,
        question_runs_table: str,
        schema: str = "public",
        dialect: str = "postgres",
        flush_interval_sec: float = 5.0,
        batch_size: int = 100,
        queue_maxsize: int = 10000,
        min_level: str = "INFO",
        connect_backoff_sec: float = 1.0,
        connect_backoff_max_sec: float = 60.0,
        summary_max_chars: int = 200,
    ) -> None:
        self._dsn = dsn or ""
        self._table_name = table_name
        self._question_runs_table = question_runs_table
        self._schema = schema
        self._dialect = (dialect or "postgres").lower()
        self._flush_interval = float(flush_interval_sec)
        self._batch_size = int(batch_size)
        self._min_level = min_level
        self._connect_backoff_sec = float(connect_backoff_sec)
        self._connect_backoff_max_sec = float(connect_backoff_max_sec)
        self._summary_max_chars = int(summary_max_chars)

        self._queue: "queue.Queue[Any]" = queue.Queue(maxsize=queue_maxsize)
        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._running = False

        self._stats: Dict[str, Any] = {
            "started_at": None,
            "written": 0,
            "queued": 0,
            "failed": 0,
            "batch_count": 0,
            "queue_full": 0,
            "connected": False,
            "last_error": None,
            "question_runs": 0,
        }

        # Индекс «текущий вопрос»: session_key -> контекст вопроса.
        # Позволяет пронести request_id/user_id/chat_id/parent_request_id
        # на все события вопроса (tool_call/run_finished/outbound),
        # даже если сами события не несут этих полей.
        # В рамках сессии прогоны последовательны, разные сессии имеют
        # разные ключи — коллизий нет.
        self._request_index: Dict[str, Dict[str, Optional[str]]] = {}
        self._request_index_lock = threading.Lock()
        self._schema_ok = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Запустить worker-поток."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._running = True
        with self._state_lock:
            self._stats["started_at"] = time.time()
        self._thread = threading.Thread(
            target=self._worker, name="db-logging", daemon=True
        )
        self._thread.start()

    def stop(self, timeout_sec: float = 15.0) -> None:
        """Остановить worker, дождавшись опустошения очереди."""
        self._running = False
        self._stop_event.set()
        try:
            self._queue.put_nowait(_FlushSentinel())
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout_sec)
            self._thread = None

    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # Публичный API (неблокирующий)
    # ------------------------------------------------------------------

    def log_event(self, event: LogEvent) -> bool:
        if not self._should_log(event.level):
            return False
        return self._enqueue(event)

    # ------------------------------------------------------------------
    # Контекст вопроса (таблица agent_question_runs) + индекс session_key→request_id
    # ------------------------------------------------------------------
    #
    # Контекст вопроса (user_id/agent_id/is_subagent/parent_*) пишется ОДИН
    # раз в отдельную таблицу agent_question_runs (upsert), а не дублируется на
    # каждое событие. В agent_gateway_logs хранится только request_id для связи.
    #
    # Индекс session_key → request_id позволяет tool/run/outbound-событиям
    # узнать request_id текущего вопроса. При параллельной обработке вопросов
    # разных пользователей session_key (= channel:chat_id) уникален для
    # каждого чата → коллизий нет.

    def register_request(
        self,
        session_key: Optional[str],
        request_id: Optional[str],
        *,
        user_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        channel: Optional[str] = None,
        parent_request_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        parent_agent_id: Optional[str] = None,
        is_subagent: bool = False,
        status: Optional[str] = "running",
        summary: Optional[str] = None,
        question: Optional[str] = None,
        media: Optional[list] = None,
    ) -> bool:
        """Зарегистрировать контекст вопроса (upsert в agent_question_runs).

        Также сохраняет session_key → request_id в индексе, чтобы последующие
        tool/run/outbound-события знали request_id текущего вопроса.
        """
        if not request_id:
            return False
        if session_key:
            with self._request_index_lock:
                self._request_index[session_key] = request_id
        return self._enqueue(_QuestionRunRecord(
            request_id=request_id,
            session_id=session_key,
            user_id=user_id,
            chat_id=chat_id,
            channel=channel,
            parent_request_id=parent_request_id,
            agent_id=agent_id,
            parent_agent_id=parent_agent_id,
            is_subagent=is_subagent,
            status=status,
            summary=summary,
            question=question,
            media=media,
        ))

    def get_request_id(self, session_key: Optional[str]) -> Optional[str]:
        """Получить request_id текущего вопроса для сессии."""
        if not session_key:
            return None
        with self._request_index_lock:
            return self._request_index.get(session_key)

    def clear_request(self, session_key: Optional[str]) -> None:
        """Снять привязку вопроса по завершении прогона."""
        if not session_key:
            return
        with self._request_index_lock:
            self._request_index.pop(session_key, None)

    def finish_request(
        self,
        request_id: Optional[str],
        *,
        status: str = "finished",
        summary: Optional[str] = None,
        response: Optional[str] = None,
        media: Optional[list] = None,
    ) -> bool:
        """Обновить статус/summary/response вопроса (upsert в agent_question_runs)."""
        if not request_id:
            return False
        return self._enqueue(_QuestionRunRecord(
            request_id=request_id,
            status=status,
            summary=summary,
            response=response,
            media=media,
            update_only=True,
        ))

    def log_inbound(
        self,
        session_id: str,
        channel: str,
        content: str,
        *,
        message_id: Optional[str] = None,
        sender_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        actor: Optional[str] = None,
        request_id: Optional[str] = None,
        level: str = "INFO",
        media: Optional[list] = None,
    ) -> bool:
        actor_val = actor or sender_id or "user"
        payload: Dict[str, Any] = {"content": content, "message_id": message_id}
        if sender_id:
            payload["sender_id"] = sender_id
        if chat_id:
            payload["chat_id"] = chat_id
        if media:
            payload["media"] = list(media)
        return self.log_event(LogEvent(
            event_type="inbound",
            level=level,
            session_id=session_id,
            channel=channel,
            actor=actor_val,
            summary=content[: self._summary_max_chars] if content else "",
            payload=payload,
            request_id=request_id or message_id,
        ))

    def log_outbound(
        self,
        session_id: str,
        channel: str,
        content: str,
        *,
        latency_ms: Optional[float] = None,
        tokens_used: Optional[int] = None,
        kind: str = "outbound_final",
        request_id: Optional[str] = None,
        level: str = "INFO",
        media: Optional[list] = None,
    ) -> bool:
        payload: Dict[str, Any] = {"content": content}
        if media:
            payload["media"] = list(media)
        return self.log_event(LogEvent(
            event_type=kind,
            level=level,
            session_id=session_id,
            channel=channel,
            actor="agent",
            summary=content[: self._summary_max_chars] if content else "",
            payload=payload,
            metadata={"latency_ms": latency_ms, "tokens_used": tokens_used},
            request_id=request_id,
        ))

    def log_tool_call(
        self,
        session_id: str,
        tool_name: str,
        args: Optional[dict] = None,
        *,
        tool_call_id: Optional[str] = None,
        request_id: Optional[str] = None,
        level: str = "INFO",
    ) -> bool:
        return self.log_event(LogEvent(
            event_type="tool_call",
            level=level,
            session_id=session_id,
            actor="agent",
            summary=tool_name,
            payload={"tool": tool_name, "args": args or {}, "tool_call_id": tool_call_id},
            request_id=request_id,
            name=tool_name,
        ))

    def log_tool_result(
        self,
        session_id: str,
        tool_name: str,
        result: Any,
        latency_ms: float,
        *,
        tool_call_id: Optional[str] = None,
        status: str = "ok",
        error: Optional[str] = None,
        request_id: Optional[str] = None,
        level: str = "INFO",
    ) -> bool:
        return self.log_event(LogEvent(
            event_type="tool_result",
            level=level,
            session_id=session_id,
            actor="agent",
            summary=tool_name,
            payload={"tool": tool_name, "status": status, "result": result, "error": error},
            metadata={"latency_ms": latency_ms, "tool_call_id": tool_call_id},
            request_id=request_id,
            name=tool_name,
        ))

    def log_error(
        self,
        error: str,
        *,
        session_id: Optional[str] = None,
        context: Optional[dict] = None,
        request_id: Optional[str] = None,
        level: str = "ERROR",
    ) -> bool:
        return self.log_event(LogEvent(
            event_type="error",
            level=level,
            session_id=session_id,
            summary=error[:200],
            payload={"error": error, "context": context or {}},
            request_id=request_id,
        ))

    def get_stats(self) -> Dict[str, Any]:
        with self._state_lock:
            s = dict(self._stats)
        s.update({
            "running": self.is_running(),
            "queue_size": self._queue.qsize(),
        })
        return s

    # ------------------------------------------------------------------
    # Внутренние
    # ------------------------------------------------------------------

    def _should_log(self, level: str) -> bool:
        """Проверить, что ``level`` не ниже ``self._min_level``.

        Сравнение по числовой шкале (``DEBUG=0``, ``INFO=1``, ``WARN=2``,
        ``ERROR=3``). Неизвестные уровни считаются как ``INFO``.
        """
        order = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3}
        return order.get(level, 1) >= order.get(self._min_level, 1)

    def _enqueue(self, event: LogEvent) -> bool:
        """Неблокирующе положить событие в очередь.

        Returns:
            ``True`` — событие в очереди, ``False`` — очередь переполнена
            (``queue_full++`` в статистике). Никогда не блокирует.
        """
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            with self._state_lock:
                self._stats["queue_full"] += 1
            return False
        with self._state_lock:
            self._stats["queued"] += 1
        return True

    def _worker(self) -> None:
        """Главный цикл worker-потока: drain очереди → батч → flush.

        Алгоритм:
          1. Получить из очереди элемент с таймаутом до ``flush_interval``
             (так цикл «просыпается» хотя бы раз в ``flush_interval``
             для флаша мелких батчей);
          2. ``_FlushSentinel`` → финальный флаш и выход (если ``_running == False``);
          3. Обычный ``LogEvent`` → в буфер;
          4. Если буфер заполнился (``>= batch_size``) ИЛИ
             ``time.time() >= deadline`` — выполнить flush.

        При flush:
          * запись идёт через общий пул ``utils.db`` (``run(lambda conn: …)``) —
            сервис своего psycopg2-соединения не держит;
          * нет DSN → события выбрасываются (счётчик ``failed``),
            JSONL-файл не пишется;
          * при ошибке batch — события выбрасываются (``failed``),
            ``connected = False``.

        В блоке ``finally`` — финальный flush (иначе при штатной остановке
        теряем события из буфера).
        """
        buffer: List[LogEvent] = []
        deadline = time.time() + self._flush_interval

        try:
            while True:
                timeout = max(0.0, deadline - time.time())
                try:
                    item = self._queue.get(timeout=timeout)
                except queue.Empty:
                    item = None

                if isinstance(item, _FlushSentinel):
                    self._flush_batch(buffer)
                    buffer.clear()
                    if not self._running:
                        return
                    continue

                # Контекст вопроса — отдельный upsert в agent_question_runs,
                # не смешивается с батчем событий agent_gateway_logs.
                if isinstance(item, _QuestionRunRecord):
                    self._handle_question_run(item)
                    continue

                if item is not None:
                    buffer.append(item)

                if len(buffer) >= self._batch_size:
                    self._flush_batch(buffer)
                    buffer.clear()
                    deadline = time.time() + self._flush_interval
                elif time.time() >= deadline:
                    if buffer:
                        self._flush_batch(buffer)
                        buffer.clear()
                    deadline = time.time() + self._flush_interval
        finally:
            # Финальный флаш
            if buffer:
                self._flush_batch(buffer)

    # ------------------------------------------------------------------
    # Запись через общий пул utils.db
    # ------------------------------------------------------------------

    def _db_run(self, fn):
        """Выполнить ``fn(conn)`` на свободном соединении общего пула ``utils.db``."""
        from utils.db import configure, run

        if self._dsn:
            configure(self._dsn)
        return run(fn)

    def _ensure_schema(self, conn: Any) -> None:
        """Проверить существование таблиц логов/контекста вопросов.

        Сервис НЕ провижинит схему: таблицы ``agent_question_runs`` /
        ``agent_gateway_logs`` (и индексы) должны быть созданы миграциями
        заранее (``sql/created_tables.sql`` / ``lib/services/sql/*.sql``).
        Если таблица логов отсутствует — поднимается исключение; вызывающий
        ``_flush_batch`` логирует его (``last_error`` + ``logger.error``),
        а события выбрасываются (счётчик ``failed``).

        Имя таблицы берётся из конструктора (``table_name``, параметр), а не
        хардкодится. Инлайн-DDL в коде нет и не выполняется.
        """
        check_tables = [self._table_name]
        if self._question_runs_table:
            check_tables.append(self._question_runs_table)
        cur = conn.cursor()
        try:
            for tbl in check_tables:
                cur.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = %s AND table_name = %s",
                    [self._schema, tbl],
                )
                if cur.fetchone() is None:
                    raise RuntimeError(
                        f"DbLoggingService: таблица не найдена: "
                        f"{self._schema}.{tbl} — создайте её миграцией, "
                        "сервис DDL не выполняет"
                    )
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def _flush_batch(self, batch: List[LogEvent]) -> None:
        """Вставить батч через общий пул ``utils.db`` (без своего соединения).

        Использует ``psycopg2.extras.execute_batch`` с
        ``page_size=self._batch_size`` для chunked-вставки — ``execute_batch``
        сам режет список на страницы и выполняет несколько ``INSERT`` с одним
        statement. На каждой строке — ``id`` (UUID), ``level``/``event_type``/
        ``summary`` (простые VARCHAR/TEXT), и ``payload``/``metadata`` как
        ``psycopg2.extras.Json`` (→ JSONB).

        При исключении (битый JSONB, отвалившееся соединение, deadlock):
          * батч целиком выбрасывается (``failed += len(batch)``);
          * ``connected = False``.

        No-op при пустом батче.
        """
        if not batch:
            return
        if not self._dsn:
            self._drop_batch(batch)
            return
        try:

            def _work(conn: Any) -> None:
                if not self._schema_ok:
                    self._ensure_schema(conn)
                    self._schema_ok = True
                self._insert_batch(conn, batch)

            self._db_run(_work)
            with self._state_lock:
                self._stats["written"] += len(batch)
                self._stats["batch_count"] += 1
                self._stats["connected"] = True
        except Exception as exc:
            self._schema_ok = False
            with self._state_lock:
                self._stats["failed"] += len(batch)
                self._stats["last_error"] = f"flush: {exc}"
                self._stats["connected"] = False

    def _insert_batch(self, conn: Any, batch: List[LogEvent]) -> None:
        """Выполнить ``execute_batch`` INSERT на данном соединении."""
        import psycopg2.extras

        cur = conn.cursor()
        try:
            psycopg2.extras.execute_batch(
                cur,
                f'INSERT INTO "{self._schema}"."{self._table_name}" '
                '(id, level, event_type, session_id, channel, actor, summary, payload, metadata, '
                'request_id, name) '
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [(
                    e.id, e.level, e.event_type, e.session_id, e.channel, e.actor,
                    e.summary,
                    psycopg2.extras.Json(e.payload or {}),
                    psycopg2.extras.Json(e.metadata or {}),
                    e.request_id, e.name,
                ) for e in batch],
                page_size=self._batch_size,
            )
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def _handle_question_run(self, rec: _QuestionRunRecord) -> None:
        """Обработать контекст вопроса: upsert в agent_question_runs.

        Через общий пул ``utils.db``. При неудаче запись выбрасывается
        (``failed++``), JSONL-файл не пишется. При ошибке upsert —
        ``connected = False``.
        """
        if not self._dsn:
            with self._state_lock:
                self._stats["failed"] += 1
                self._stats["last_error"] = "question_run: нет соединения с БД"
            return
        try:

            def _work(conn: Any) -> None:
                if not self._schema_ok:
                    self._ensure_schema(conn)
                    self._schema_ok = True
                self._upsert_question_run(conn, rec)

            self._db_run(_work)
            with self._state_lock:
                self._stats["question_runs"] += 1
                self._stats["connected"] = True
        except Exception as exc:
            self._schema_ok = False
            with self._state_lock:
                self._stats["failed"] += 1
                self._stats["last_error"] = f"question_run upsert: {exc}"
                self._stats["connected"] = False

    def _upsert_question_run(self, conn: Any, rec: _QuestionRunRecord) -> None:
        """Upsert контекста вопроса в agent_question_runs (без ON CONFLICT).

        Greenplum 6.x (база PostgreSQL 9.4) НЕ поддерживает
        ``INSERT ... ON CONFLICT (…) DO UPDATE`` — он появился только в
        Greenplum 7. Поэтому используем переносимый двухшаговый паттерн,
        работающий и на PostgreSQL 13, и на Greenplum 6.5:

          1. ``UPDATE ... WHERE request_id = …`` — обновить существующую строку;
          2. ``INSERT ... SELECT … WHERE NOT EXISTS (…)`` — вставить, если
             строки ещё нет (закрывает гонку "нет строки после UPDATE").

        ``update_only`` (вызов finish_request) обновляет только
        ``updated_at``/``status``/``summary``, не затирая контекст вопроса.
        Обычная регистрация upsert-ит все поля (новый вопрос — вставка,
        повторная регистрация — перезапись контекста).
        """
        cur = conn.cursor()
        try:
            # media хранится как JSON-строка в TEXT-колонке
            media_json = json.dumps(rec.media, ensure_ascii=False) if rec.media else None
            if rec.update_only:
                cur.execute(
                    f'UPDATE "{self._schema}"."{self._question_runs_table}" '
                    "SET updated_at = now(), status = %s, summary = %s, "
                    "response = COALESCE(%s, response), media = COALESCE(%s, media) "
                    "WHERE request_id = %s",
                    (rec.status, rec.summary, rec.response, media_json, rec.request_id),
                )
                cur.execute(
                    f'INSERT INTO "{self._schema}"."{self._question_runs_table}" '
                    "(request_id, status, summary, response, media) "
                    "SELECT %s, %s, %s, %s, %s "
                    f'WHERE NOT EXISTS (SELECT 1 FROM "{self._schema}"."{self._question_runs_table}" '
                    "WHERE request_id = %s)",
                    (rec.request_id, rec.status, rec.summary, rec.response, media_json, rec.request_id),
                )
            else:
                cur.execute(
                    f'UPDATE "{self._schema}"."{self._question_runs_table}" '
                    "SET session_id = %s, user_id = %s, chat_id = %s, "
                    "channel = %s, parent_request_id = %s, agent_id = %s, "
                    "parent_agent_id = %s, is_subagent = %s, status = %s, "
                    "summary = %s, question = %s, media = %s, updated_at = now() "
                    "WHERE request_id = %s",
                    (
                        rec.session_id, rec.user_id, rec.chat_id, rec.channel,
                        rec.parent_request_id, rec.agent_id,
                        rec.parent_agent_id, rec.is_subagent, rec.status,
                        rec.summary, rec.question, media_json, rec.request_id,
                    ),
                )
                cur.execute(
                    f'INSERT INTO "{self._schema}"."{self._question_runs_table}" '
                    "(request_id, session_id, user_id, chat_id, channel, "
                    "parent_request_id, agent_id, parent_agent_id, is_subagent, "
                    "status, summary, question, media) "
                    "SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s "
                    f'WHERE NOT EXISTS (SELECT 1 FROM "{self._schema}"."{self._question_runs_table}" '
                    "WHERE request_id = %s)",
                    (
                        rec.request_id, rec.session_id, rec.user_id, rec.chat_id,
                        rec.channel, rec.parent_request_id, rec.agent_id,
                        rec.parent_agent_id, rec.is_subagent, rec.status,
                        rec.summary, rec.question, media_json, rec.request_id,
                    ),
                )
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def _drop_batch(self, batch: List[LogEvent]) -> None:
        """Выбросить батч, когда БД недоступна (без записи в JSONL-файл).

        Увеличивает ``stats["failed"]`` и фиксирует ``last_error`` — скрытая
        запись в файл не производится, событие считается потерянным.
        """
        with self._state_lock:
            self._stats["failed"] += len(batch)
            self._stats["last_error"] = "flush: БД недоступна, батч выброшен"
