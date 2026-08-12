"""
AuditSyncService — единственный владелец подключения к PostgreSQL в gateway.

Отвечает за:
  * инкрементальную синхронизацию данных из PG в in-memory кэш (AuditMemoryStore);
  * неблокирующую запись ответов/взаимодействий навыка обратно в PG через очередь;
  * автоматическое переподключение при обрывах связи;
  * корректное завершение (graceful shutdown) с гарантией сохранения очереди.

Вся работа выполняется в одном worker-потоке, который держит единственное
psycopg2-подключение (``self._conn``). Публичный API безопасен для вызова
из asyncio/любого потока:

    sync_service = AuditSyncService(dsn=dsn, tables=[...])
    sync_service.set_on_new_records_callback(memory_store.upsert_records)
    sync_service.start(initial_load=True)
    ...
    sync_service.submit_write(session_id=..., query_text=..., answer_text=...)
    ...
    sync_service.stop(timeout_sec=10.0)

Команды в очереди: ``WRITE_ANSWER`` (запись в PG), ``POLL_CHANGES``
(немедленный поллинг), ``SHUTDOWN`` (sentinel завершения).
"""

from __future__ import annotations

import datetime
import queue
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import psycopg2
import psycopg2.extras

COMMAND_WRITE = "WRITE_ANSWER"
COMMAND_POLL = "POLL_CHANGES"
COMMAND_SHUTDOWN = "SHUTDOWN"


class AuditSyncService:
    """Фоновая синхронизация audit-данных из PostgreSQL в in-memory кэш.

    Worker-поток владеет единственным подключением к PG. Поллинг таблиц
    инкрементален (по track-колонке), новые/изменённые строки передаются
    в callback ``on_new_records(table, records)`` — обычно это
    ``AuditMemoryStore.upsert_records``.
    """

    def __init__(
        self,
        dsn: str,
        schema: str = "oarb",
        tables: Optional[List[str]] = None,
        vector_table: str = "",
        poll_interval_sec: float = 60.0,
        write_table: str = "audit_interactions",
        write_schema: str = "oarb",
        max_queue_size: int = 10000,
        reconnect_backoff: float = 1.0,
    ) -> None:
        self._dsn = dsn
        self._schema = schema
        self._tables = [t for t in (tables or []) if t]
        self._vector_table = vector_table
        self._poll_interval = float(poll_interval_sec)
        self._write_table = write_table
        self._write_schema = write_schema
        self._max_queue_size = max_queue_size
        self._reconnect_backoff = reconnect_backoff

        self._queue: "queue.Queue[tuple[str, Any]]" = queue.Queue(maxsize=max_queue_size)
        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()

        self._conn: Optional[psycopg2.extensions.connection] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._initial_load = True

        # Инкрементальный поллинг: {table: последнее значение track-колонки}
        self._last_sync: Dict[str, Any] = {}
        self._on_new_records: Optional[Callable[[str, List[dict]], None]] = None
        self._on_sync_callback: Optional[Callable[[], None]] = None

        self._stats: Dict[str, Any] = {
            "started_at": None,
            "polls": 0,
            "writes_queued": 0,
            "writes_written": 0,
            "writes_failed": 0,
            "queue_full": 0,
            "reconnects": 0,
            "errors": 0,
            "tables": list(self._tables),
        }

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def set_on_new_records_callback(
        self, callback: Callable[[str, List[dict]], None]
    ) -> None:
        """Задать callback для новых/изменённых строк: ``callback(table, records)``."""
        self._on_new_records = callback

    def set_on_sync_callback(self, callback: Callable[[], None]) -> None:
        """Задать callback по завершении цикла синхронизации.

        Вызывается из worker-потока после initial load и после каждого
        поллинга — удобно для публикации снимка кеша (store.publish).
        """
        self._on_sync_callback = callback

    def start(self, initial_load: bool = True) -> None:
        """Запустить worker-поток.

        Args:
            initial_load: если True — сначала полная загрузка всех таблиц.
        """
        if self._thread is not None and self._thread.is_alive():
            return
        self._initial_load = bool(initial_load)
        self._running = True
        self._stop_event.clear()
        self._stats["started_at"] = time.time()
        self._thread = threading.Thread(
            target=self._worker, name="audit-sync", daemon=True
        )
        self._thread.start()

    def stop(self, timeout_sec: float = 10.0) -> None:
        """Остановить worker-поток, сохранив оставшиеся записи из очереди."""
        self._running = False
        self._stop_event.set()
        try:
            self._queue.put_nowait((COMMAND_SHUTDOWN, None))
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout_sec)
            self._thread = None
        self._close_connection()

    def submit_write(
        self,
        session_id: str,
        query_text: str,
        answer_text: str,
        metadata: Optional[dict] = None,
    ) -> bool:
        """Неблокирующе поставить запись в очередь (worker запишет в PG).

        Returns:
            True если запись принята в очередь, False если очередь переполнена.
        """
        if not self._running:
            return False
        payload = {
            "session_id": session_id,
            "query_text": query_text,
            "answer_text": answer_text,
            "metadata": metadata or {},
        }
        try:
            self._queue.put_nowait((COMMAND_WRITE, payload))
        except queue.Full:
            with self._state_lock:
                self._stats["queue_full"] += 1
            return False
        with self._state_lock:
            self._stats["writes_queued"] += 1
        return True

    def get_stats(self) -> Dict[str, Any]:
        """Мониторинг: размер очереди, счётчики, состояние подключения."""
        with self._state_lock:
            stats = dict(self._stats)
        connected = self._conn is not None and not self._conn.closed
        stats.update(
            {
                "running": self._running,
                "connected": connected,
                "queue_size": self._queue.qsize(),
                "last_sync": dict(self._last_sync),
            }
        )
        return stats

    def get_sync_stats(self) -> Dict[str, Any]:
        """Псевдоним ``get_stats`` (используется в мониторинге/логах)."""
        return self.get_stats()

    # ------------------------------------------------------------------
    # Worker-цикл
    # ------------------------------------------------------------------

    def _worker(self) -> None:
        try:
            self._ensure_connected()
            self._ensure_write_table()
            if self._initial_load:
                self._do_initial_load()
            self._fire_sync_callback()
            while self._running:
                self._drain_queue()
                if not self._running:
                    break
                self._poll_changes()
                self._fire_sync_callback()
                # Ждём интервал поллинга или сигнал остановки
                self._stop_event.wait(self._poll_interval)
        finally:
            self._running = False
            # Финальная попытка дописать оставшиеся записи
            try:
                self._drain_queue()
            except Exception:
                pass
            self._close_connection()

    def _fire_sync_callback(self) -> None:
        """Уведомить о завершении цикла синхронизации (после load/поллинга)."""
        cb = self._on_sync_callback
        if cb is None:
            return
        try:
            cb()
        except Exception:
            with self._state_lock:
                self._stats["errors"] += 1

    def _drain_queue(self) -> None:
        """Обработать все команды из очереди (неблокирующе)."""
        while True:
            try:
                cmd, payload = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                if cmd == COMMAND_WRITE:
                    self._write_answer(payload)
                elif cmd == COMMAND_POLL:
                    self._poll_changes()
                elif cmd == COMMAND_SHUTDOWN:
                    self._running = False
            except Exception:
                with self._state_lock:
                    self._stats["errors"] += 1
                    self._stats["writes_failed"] += 1
                self._reconnect()
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------
    # Поллинг таблиц
    # ------------------------------------------------------------------

    def _track_column_for(self, table: str) -> str:
        """Вернуть колонку для инкрементального отслеживания изменений."""
        if table == self._vector_table or table.endswith(".audit_vectors"):
            return "id"
        return "updated_at"

    def _do_initial_load(self) -> None:
        for table in self._tables:
            if not self._running:
                return
            try:
                rows, last = self._fetch_all(table)
                self._dispatch(table, rows)
                if last is not None:
                    self._last_sync[table] = last
                else:
                    # Пустая таблица: запоминаем "сейчас", чтобы дальше
                    # поллить инкрементально, а не перечитывать всё.
                    self._last_sync[table] = datetime.datetime.now(
                        datetime.timezone.utc
                    )
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                self._reconnect()
                return
            except Exception:
                with self._state_lock:
                    self._stats["errors"] += 1

    def _poll_changes(self) -> None:
        if not self._running:
            return
        for table in self._tables:
            if not self._running:
                return
            try:
                self._poll_table(table)
                with self._state_lock:
                    self._stats["polls"] += 1
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                self._reconnect()
                return
            except Exception:
                with self._state_lock:
                    self._stats["errors"] += 1

    def _poll_table(self, table: str) -> None:
        track_col = self._track_column_for(table)
        last = self._last_sync.get(table)
        if last is None:
            rows, last = self._fetch_all(table)
        else:
            rows, new_last = self._fetch_incremental(table, track_col, last)
            if new_last is not None:
                last = new_last
        self._dispatch(table, rows)
        if last is not None:
            self._last_sync[table] = last

    def _dispatch(self, table: str, rows: List[dict]) -> None:
        if not rows:
            return
        callback = self._on_new_records
        if callback is not None:
            try:
                callback(table, rows)
            except Exception:
                with self._state_lock:
                    self._stats["errors"] += 1

    # ------------------------------------------------------------------
    # SQL-доступ (единственное подключение)
    # ------------------------------------------------------------------

    def _fq_table(self, table: str) -> str:
        """Полное имя таблицы ``schema.table`` (без точки — схема из конфига)."""
        if "." in table:
            return f'"{table.split(".", 1)[0]}"."{table.split(".", 1)[1]}"'
        return f'"{self._schema}"."{table}"'

    def _fetch_all(self, table: str) -> tuple[List[dict], Any]:
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute(f'SELECT * FROM {self._fq_table(table)}')
            rows = [dict(r) for r in cur.fetchall()]
        finally:
            cur.close()
        track_col = self._track_column_for(table)
        last = self._max_track(rows, track_col)
        return rows, last

    def _fetch_incremental(
        self, table: str, track_col: str, last: Any
    ) -> tuple[List[dict], Any]:
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute(
                f'SELECT * FROM {self._fq_table(table)} '
                f'WHERE "{track_col}" > %s ORDER BY "{track_col}"',
                [last],
            )
            rows = [dict(r) for r in cur.fetchall()]
        finally:
            cur.close()
        return rows, self._max_track(rows, track_col)

    @staticmethod
    def _max_track(rows: List[dict], track_col: str) -> Any:
        values = [r.get(track_col) for r in rows if r.get(track_col) is not None]
        return max(values) if values else None

    # ------------------------------------------------------------------
    # Запись ответов (через очередь)
    # ------------------------------------------------------------------

    def _ensure_write_table(self) -> None:
        if not self._write_table or self._conn is None:
            return
        cur = self._conn.cursor()
        try:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{self._write_schema}"')
            cur.execute(
                f'CREATE TABLE IF NOT EXISTS "{self._write_schema}"."{self._write_table}" ('
                "id BIGSERIAL PRIMARY KEY, "
                "session_id TEXT, "
                "query_text TEXT, "
                "answer_text TEXT, "
                "metadata JSONB, "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
            )
        finally:
            cur.close()

    def _write_answer(self, payload: dict) -> None:
        if not self._write_table or self._conn is None:
            return
        cur = self._conn.cursor()
        try:
            cur.execute(
                f'INSERT INTO "{self._write_schema}"."{self._write_table}" '
                "(session_id, query_text, answer_text, metadata, created_at) "
                "VALUES (%s, %s, %s, %s, NOW())",
                [
                    payload.get("session_id"),
                    payload.get("query_text"),
                    payload.get("answer_text"),
                    psycopg2.extras.Json(payload.get("metadata") or {}),
                ],
            )
        finally:
            cur.close()
        with self._state_lock:
            self._stats["writes_written"] += 1

    # ------------------------------------------------------------------
    # Подключение / переподключение
    # ------------------------------------------------------------------

    def _ensure_connected(self) -> None:
        if self._conn is not None and not self._conn.closed:
            return
        backoff = self._reconnect_backoff
        while self._running and (self._conn is None or self._conn.closed):
            try:
                self._conn = psycopg2.connect(self._dsn, gssencmode="disable")
                self._conn.autocommit = True
                with self._state_lock:
                    self._stats["reconnects"] += 1
                return
            except Exception:
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, 60.0)

    def _reconnect(self) -> None:
        """Закрыть соединение и сбросить инкрементальные метки.

        После обрыва перезагружаем таблицы целиком, чтобы не пропустить
        изменения, произошедшие во время недоступности БД.
        """
        self._close_connection()
        self._last_sync.clear()
        self._ensure_connected()

    def _close_connection(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
