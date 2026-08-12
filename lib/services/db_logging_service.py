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
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class LogEvent:
    """Одно событие для записи в БД."""

    event_type: str
    level: str = "INFO"
    session_id: Optional[str] = None
    channel: Optional[str] = None
    actor: Optional[str] = None
    summary: Optional[str] = None
    payload: Optional[dict] = None
    metadata: Optional[dict] = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class _FlushSentinel:
    pass


class DbLoggingService:
    """Фоновый writer событий агента в PostgreSQL с fallback-JSONL."""

    def __init__(
        self,
        dsn: Optional[str] = None,
        *,
        table_name: str = "gateway_logs",
        schema: str = "public",
        flush_interval_sec: float = 5.0,
        batch_size: int = 100,
        queue_maxsize: int = 10000,
        min_level: str = "INFO",
        fallback_path: Optional[Path] = None,
        min_conn: int = 1,
        max_conn: int = 4,
    ) -> None:
        self._dsn = dsn or ""
        self._table_name = table_name
        self._schema = schema
        self._flush_interval = float(flush_interval_sec)
        self._batch_size = int(batch_size)
        self._min_level = min_level
        self._fallback_path = Path(fallback_path) if fallback_path else None
        self._min_conn = min_conn
        self._max_conn = max_conn

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
        }

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

    def log_inbound(
        self,
        session_id: str,
        channel: str,
        content: str,
        *,
        message_id: Optional[str] = None,
        actor: str = "user",
        level: str = "INFO",
    ) -> bool:
        return self.log_event(LogEvent(
            event_type="inbound",
            level=level,
            session_id=session_id,
            channel=channel,
            actor=actor,
            summary=content[:200] if content else "",
            payload={"content": content, "message_id": message_id},
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
        level: str = "INFO",
    ) -> bool:
        return self.log_event(LogEvent(
            event_type=kind,
            level=level,
            session_id=session_id,
            channel=channel,
            actor="agent",
            summary=content[:200] if content else "",
            payload={"content": content},
            metadata={"latency_ms": latency_ms, "tokens_used": tokens_used},
        ))

    def log_tool_call(
        self,
        session_id: str,
        tool_name: str,
        args: Optional[dict] = None,
        *,
        tool_call_id: Optional[str] = None,
        level: str = "INFO",
    ) -> bool:
        return self.log_event(LogEvent(
            event_type="tool_call",
            level=level,
            session_id=session_id,
            actor="agent",
            summary=tool_name,
            payload={"tool": tool_name, "args": args or {}, "tool_call_id": tool_call_id},
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
        ))

    def log_error(
        self,
        error: str,
        *,
        session_id: Optional[str] = None,
        context: Optional[dict] = None,
        level: str = "ERROR",
    ) -> bool:
        return self.log_event(LogEvent(
            event_type="error",
            level=level,
            session_id=session_id,
            summary=error[:200],
            payload={"error": error, "context": context or {}},
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

    def _connect(self):
        """Открыть psycopg2-соединение к ``self._dsn``.

        Returns:
            ``psycopg2.connection`` (с ``autocommit=True``) или ``None``,
            если DSN не задан, psycopg2 не установлен или ``_running == False``.

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

        backoff = 1.0
        while self._running:
            try:
                conn = psycopg2.connect(self._dsn, gssencmode="disable")
                conn.autocommit = True
                with self._state_lock:
                    self._stats["connected"] = True
                return conn
            except Exception as exc:
                with self._state_lock:
                    self._stats["last_error"] = f"connect: {exc}"
                    self._stats["connected"] = False
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, 60.0)
        return None

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
                '(id, level, event_type, session_id, channel, actor, summary, payload, metadata) '
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [(
                    e.id, e.level, e.event_type, e.session_id, e.channel, e.actor,
                    e.summary,
                    psycopg2.extras.Json(e.payload or {}),
                    psycopg2.extras.Json(e.metadata or {}),
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
