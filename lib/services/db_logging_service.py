"""DbLoggingService — структурированное логирование событий агента в PostgreSQL.

Импортируется БЕЗ nanobot (psycopg2 импортируется лениво — модуль годен
для тестов с мок-подключением и для сред без psycopg2).

Архитектура:
  * единственный worker-поток (``self._thread``) владеет psycopg2-соединением;
  * неблокирующие ``log_*`` методы ставят события в ``queue.Queue``;
  * worker батчем вставляет записи по ``flush_interval_sec`` или ``batch_size``;
  * если подключение к БД недоступно — лог пишется в JSONL-файл
    (``fallback_path``), сервис продолжает работу;
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
from pathlib import Path
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
    """Фоновый writer событий агента в PostgreSQL с fallback-JSONL."""

    def __init__(
        self,
        dsn: Optional[str] = None,
        *,
        table_name: str = "agent_gateway_logs",
        question_runs_table: str = "agent_question_runs",
        schema: str = "public",
        dialect: str = "postgres",
        flush_interval_sec: float = 5.0,
        batch_size: int = 100,
        queue_maxsize: int = 10000,
        min_level: str = "INFO",
        fallback_path: Optional[Path] = None,
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
        self._fallback_path = Path(fallback_path) if fallback_path else None
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
            "fallback_written": 0,
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
          * есть соединение → ``_flush_batch(conn, buffer)`` (psycopg2 INSERT);
          * нет соединения → ``_flush_to_fallback(buffer)`` (JSONL-файл);
          * при ошибке batch — fallback принимает весь батч (не теряем).

        В блоке ``finally`` — финальный flush (иначе при штатной остановке
        теряем события из буфера).
        """
        conn = self._connect()
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
                    self._flush_batch(conn, buffer)
                    buffer.clear()
                    if not self._running:
                        return
                    continue

                # Контекст вопроса — отдельный upsert в agent_question_runs,
                # не смешивается с батчем событий agent_gateway_logs.
                if isinstance(item, _QuestionRunRecord):
                    self._handle_question_run(conn, item)
                    continue

                if item is not None:
                    buffer.append(item)

                if len(buffer) >= self._batch_size:
                    if conn is None:
                        conn = self._connect()
                    if conn is not None:
                        self._flush_batch(conn, buffer)
                        buffer.clear()
                    else:
                        # fallback: dump buffer to file
                        self._flush_to_fallback(buffer)
                        buffer.clear()
                    deadline = time.time() + self._flush_interval
                elif time.time() >= deadline:
                    if buffer:
                        if conn is None:
                            conn = self._connect()
                        if conn is not None:
                            self._flush_batch(conn, buffer)
                        else:
                            self._flush_to_fallback(buffer)
                        buffer.clear()
                    deadline = time.time() + self._flush_interval
        finally:
            # Финальный флаш
            if buffer:
                if conn is None:
                    conn = self._connect()
                if conn is not None:
                    self._flush_batch(conn, buffer)
                else:
                    self._flush_to_fallback(buffer)
            self._close(conn)

    # ------------------------------------------------------------------
    # Подключение / запись / fallback
    # ------------------------------------------------------------------

    _SQL_DIR = Path(__file__).parents[2] / "sql" / "logs"
    _MIGRATIONS_DIR = Path(__file__).parents[2] / "sql" / "migrations"

    _DDL_PATH = _SQL_DIR / "create_logs_table.sql"
    _MIGRATION_PATH = _MIGRATIONS_DIR / "migrate_logs_v1.sql"
    _DDL_GP_PATH = _SQL_DIR / "create_logs_table_gp.sql"
    _MIGRATION_GP_PATH = _MIGRATIONS_DIR / "migrate_logs_v1_gp.sql"

    def _connect(self):
        """Открыть psycopg2-соединение к ``self._dsn`` и создать таблицу.

        Returns:
            ``psycopg2.connection`` (с ``autocommit=True``) или ``None``,
            если DSN не задан, psycopg2 не установлен или ``_running == False``.

        После успешного ``connect`` выполняется ``_ensure_schema`` —
        таблица ``agent_gateway_logs`` (и индексы) создаются через
        ``CREATE TABLE/INDEX IF NOT EXISTS``. Если DDL падает — соединение
        закрывается и попытка повторяется с backoff (создание таблицы не
        обязано происходить мгновенно, ретрай самодостаточен).

        При неудаче ``connect`` — экспоненциальный backoff
        (1с → 2с → 4с → ... → 60с) внутри ``while self._running``,
        ``last_error`` пишется в статистику.
        """
        if not self._dsn:
            return None
        try:
            import psycopg2
            import psycopg2.extras
        except Exception as exc:
            with self._state_lock:
                self._stats["last_error"] = f"psycopg2 import: {exc}"
            return None

        backoff = self._connect_backoff_sec
        while self._running:
            try:
                conn = psycopg2.connect(self._dsn, gssencmode="disable")
                conn.autocommit = True
            except Exception as exc:
                with self._state_lock:
                    self._stats["last_error"] = f"connect: {exc}"
                    self._stats["connected"] = False
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, self._connect_backoff_max_sec)
                continue
            try:
                self._ensure_schema(conn)
            except Exception as exc:
                try:
                    conn.close()
                except Exception:
                    pass
                logger.error(
                    "DbLoggingService: таблицу %s.%s не удалось создать "
                    "(DDL из %s): %s",
                    self._schema, self._table_name,
                    self._schema_files()[0], exc,
                )
                with self._state_lock:
                    self._stats["last_error"] = f"ensure schema: {exc}"
                    self._stats["connected"] = False
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, self._connect_backoff_max_sec)
                continue
            with self._state_lock:
                self._stats["connected"] = True
            return conn
        return None

    def _ensure_schema(self, conn: Any) -> None:
        """Создать таблицы ``agent_question_runs``/``agent_gateway_logs`` и индексы.

        DDL берётся ТОЛЬКО из отдельных SQL-файлов
        ``lib/services/sql/*.sql``. Выбор набора файлов зависит от диалекта:

          * ``postgres`` (по умолчанию, PostgreSQL 13+) —
            ``create_logs_table.sql`` + ``migrate_logs_v1.sql``, идемпотентность
            через ``IF NOT EXISTS``;
          * ``greenplum`` (Greenplum 6.x, база PostgreSQL 9.4) —
            ``create_logs_table_gp.sql`` + ``migrate_logs_v1_gp.sql``:
            ``IF NOT EXISTS`` для индексов/колонок там НЕ поддержан, поэтому
            идемпотентность через DO-блоки с проверкой каталога, а обе
            таблицы получают ``DISTRIBUTED BY (request_id)`` для co-located
            join'ов.

        В файлах имя таблицы логов подставляется через плейсхолдеры:
          * PostgreSQL: строка ``agent_gateway_logs`` заменяется на
            schema-qualified имя ``"schema"."table_name"``;
          * Greenplum: ``@@SCHEMA@@`` / ``@@TABLE@@`` / ``@@TABLE_DDL@@``
            (иначе ``agent_gateway_logs`` внутри каталог-запросов DO-блоков
            сломает подстановку).

        Inline-DDL в коде нет — файлы являются единственным источником
        структуры. Если файла нет или выполнение падает — поднимается
        исключение. ``_connect`` логирует ошибку (``last_error`` +
        ``logger.error``), а события уходят в fallback-JSONL
        (``_flush_to_fallback``), т.е. таблица не создаётся — пишем в файл.
        """
        ddl_path, migration_path = self._schema_files()
        if not ddl_path.exists():
            raise FileNotFoundError(f"agent_gateway_logs DDL file not found: {ddl_path}")
        table = f'"{self._schema}"."{self._table_name}"'
        cur = conn.cursor()
        try:
            # 1) Базовая схема (CREATE TABLE/INDEX [IF NOT EXISTS])
            sql = self._render_sql(ddl_path, table)
            cur.execute(sql)
            # 2) Миграция существующей таблицы. Игнорируем отсутствие файла —
            #    для чистой установки он не нужен.
            if migration_path.exists():
                msql = self._render_sql(migration_path, table)
                cur.execute(msql)
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def _schema_files(self) -> Tuple[Path, Optional[Path]]:
        """Набор SQL-файлов (DDL, миграция) для текущего диалекта."""
        if self._dialect == "greenplum":
            return self._DDL_GP_PATH, self._MIGRATION_GP_PATH
        return self._DDL_PATH, self._MIGRATION_PATH

    def _render_sql(self, path: Path, table: str) -> str:
        """Прочитать SQL-файл и подставить имя таблицы логов.

        Для ``postgres``-диалекта (без плейсхолдеров) — замена
        ``agent_gateway_logs`` → ``"schema"."table"``, но НЕ выше тех
        вхождений, где это имя нельзя квалифицировать: цель ``RENAME TO``
        и уже schema-qualified ``public.agent_gateway_logs``. Иначе наивная
        замена ломает ``RENAME TO "public"."agent_gateway_logs"`` (невалидно)
        и даёт ``public."public"."agent_gateway_logs"`` (дублирование схемы).
        Для ``greenplum`` — подстановка ``@@SCHEMA@@``/``@@TABLE@@``/``@@TABLE_DDL@@``.
        """
        import re
        sql = path.read_text(encoding="utf-8")
        if self._dialect == "greenplum":
            return (
                sql
                .replace("@@SCHEMA@@", self._schema)
                .replace("@@TABLE@@", self._table_name)
                .replace("@@TABLE_DDL@@", table)
            )
        needle = re.escape("agent_gateway_logs")
        return re.sub(
            rf"(?<!public\.)(?<!RENAME TO ){needle}",
            lambda _m: table,
            sql,
        )

    def _flush_batch(self, conn: Any, batch: List[LogEvent]) -> None:
        """Вставить батч в PostgreSQL через ``psycopg2.extras.execute_batch``.

        Использует ``page_size=self._batch_size`` для chunked-вставки —
        ``execute_batch`` сам режет список на страницы и выполняет несколько
        ``INSERT`` с одним statement. На каждой строке — ``id`` (UUID),
        ``level``/``event_type``/``summary`` (простые VARCHAR/TEXT), и
        ``payload``/``metadata`` как ``psycopg2.extras.Json`` (→ JSONB).

        При исключении (битый JSONB, отвалившееся соединение, deadlock):
          * батч целиком уходит в ``_flush_to_fallback`` (не теряем);
          * ``connected = False`` → следующий ``_flush_batch`` начнётся
            с попытки ``_connect()``.

        No-op при пустом батче.
        """
        if not batch:
            return
        try:
            import psycopg2.extras
            cur = conn.cursor()
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
            cur.close()
            with self._state_lock:
                self._stats["written"] += len(batch)
                self._stats["batch_count"] += 1
        except Exception as exc:
            with self._state_lock:
                self._stats["failed"] += len(batch)
                self._stats["last_error"] = f"flush: {exc}"
                self._stats["connected"] = False
            # Fallback в файл — события не потеряются
            self._flush_to_fallback(batch)

    def _handle_question_run(self, conn: Any, rec: _QuestionRunRecord) -> None:
        """Обработать контекст вопроса: upsert в agent_question_runs.

        Если соединения нет — попробуем подключиться; при неудаче пишем
        запись в fallback-JSONL (``_question_run_fallback``), не теряя её.
        При ошибке upsert — тоже в fallback, ``connected = False``.
        """
        if conn is None:
            conn = self._connect()
        if conn is not None:
            try:
                self._upsert_question_run(conn, rec)
                return
            except Exception as exc:
                with self._state_lock:
                    self._stats["last_error"] = f"question_run upsert: {exc}"
                    self._stats["connected"] = False
        self._question_run_fallback(rec)

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
            with self._state_lock:
                self._stats["question_runs"] += 1
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def _question_run_fallback(self, rec: _QuestionRunRecord) -> None:
        """Записать контекст вопроса в fallback-JSONL (если путь задан).

        Используется, когда БД недоступна или upsert упал. Формат строки —
        JSON с префиксом-маркером ``{"_qr": true, ...}``, чтобы можно было
        отличить от событий agent_gateway_logs при re-import.
        """
        if not self._fallback_path:
            return
        try:
            self._fallback_path.parent.mkdir(parents=True, exist_ok=True)
            with self._fallback_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "_qr": True,
                    "request_id": rec.request_id,
                    "session_id": rec.session_id,
                    "user_id": rec.user_id,
                    "chat_id": rec.chat_id,
                    "channel": rec.channel,
                    "parent_request_id": rec.parent_request_id,
                    "agent_id": rec.agent_id,
                    "parent_agent_id": rec.parent_agent_id,
                    "is_subagent": rec.is_subagent,
                    "status": rec.status,
                    "summary": rec.summary,
                    "question": rec.question,
                    "response": rec.response,
                    "media": rec.media,
                    "update_only": rec.update_only,
                }, ensure_ascii=False) + "\n")
            with self._state_lock:
                self._stats["fallback_written"] += 1
        except Exception as exc:
            with self._state_lock:
                self._stats["failed"] += 1
                self._stats["last_error"] = f"question_run fallback: {exc}"

    def _flush_to_fallback(self, batch: List[LogEvent]) -> None:
        """Сбросить батч в JSONL-файл ``self._fallback_path``.

        Используется, когда:
          * БД недоступна (psycopg2.connect упал);
          * batch-вставка упала (битый JSON, deadlock, отвал соединения).

        Формат строки — JSON (каждая ``LogEvent.id`` сохраняется, чтобы
        можно было дедуплицировать при последующем re-import).

        No-op если ``fallback_path`` не задан (тогда события просто
        теряются — в логах это будет видно по ``stats["failed"]``).
        """
        if not self._fallback_path:
            return
        try:
            self._fallback_path.parent.mkdir(parents=True, exist_ok=True)
            with self._fallback_path.open("a", encoding="utf-8") as f:
                for e in batch:
                    f.write(json.dumps({
                        "id": e.id,
                        "level": e.level,
                        "event_type": e.event_type,
                        "session_id": e.session_id,
                        "channel": e.channel,
                        "actor": e.actor,
                        "summary": e.summary,
                        "payload": e.payload,
                        "metadata": e.metadata,
                        "request_id": e.request_id,
                        "name": e.name,
                    }, ensure_ascii=False) + "\n")
            with self._state_lock:
                self._stats["fallback_written"] += len(batch)
        except Exception as exc:
            with self._state_lock:
                self._stats["failed"] += len(batch)
                self._stats["last_error"] = f"fallback: {exc}"

    def _close(self, conn: Any) -> None:
        """Закрыть psycopg2-соединение (если оно было).

        Исключения глотаются — некритично. После закрытия
        ``stats["connected"] = False``, чтобы следующий ``_flush_batch``
        пошёл через ``_connect()``.
        """
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            pass
        with self._state_lock:
            self._stats["connected"] = False
