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
import logging
import queue
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

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
        write_table: str = "",
        write_schema: str = "oarb",
        max_queue_size: int = 10000,
        reconnect_backoff: float = 1.0,
        reconnect_backoff_max: float = 60.0,
        full_resync_every: int = 10,
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
        self._reconnect_backoff_max = reconnect_backoff_max
        self._full_resync_every = max(0, int(full_resync_every))
        self._resync_counter = 0

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
        self._on_replace_records: Optional[Callable[[str, List[dict]], None]] = None
        self._on_schema: Optional[Callable[[str, List[dict]], None]] = None
        self._on_sync_callback: Optional[Callable[[], None]] = None

        self._stats: Dict[str, Any] = {
            "started_at": None,
            "polls": 0,
            "full_resyncs": 0,
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

    def set_on_replace_records_callback(
        self, callback: Callable[[str, List[dict]], None]
    ) -> None:
        """Задать callback для полной пересинхронизации: ``callback(table, records)``.

        Вызывается при периодической полной перезагрузке таблицы (сверка
        удалённых строк). Обычно это ``AuditMemoryStore.replace_records``.
        """
        self._on_replace_records = callback

    def set_on_schema_callback(
        self, callback: Callable[[str, List[dict]], None]
    ) -> None:
        """Задать callback для описания колонок таблицы из PG information_schema.

        Вызывается перед загрузкой/полной пересинхронизацией таблицы:
        ``callback(table, columns)``, где ``columns`` — список описаний
        ``[{"name", "type", "not_null", "comment"}, ...]``. Обычно это
        ``AuditMemoryStore.ensure_schema``.
        """
        self._on_schema = callback

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
                "full_resync_every": self._full_resync_every,
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
        if table == self._vector_table:
            return "id"
        return "updated_at"

    def _do_initial_load(self) -> None:
        for table in self._tables:
            if not self._running:
                return
            try:
                self._ensure_table_schema(table)
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
            except psycopg2.errors.UndefinedTable:
                logger.error(
                    "AuditSyncService: таблица-источник не найдена: %s "
                    "— пропускаю. Проверьте настройки skills.audit_analyzer.db_tables/"
                    "db_additional_tables и создайте таблицу в PG.",
                    table,
                )
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
            except psycopg2.errors.UndefinedTable:
                logger.error(
                    "AuditSyncService: таблица-источник не найдена при поллинге: %s "
                    "— пропускаю. Проверьте настройки skills.audit_analyzer.db_tables/"
                    "db_additional_tables.",
                    table,
                )
            except Exception:
                with self._state_lock:
                    self._stats["errors"] += 1

    def _poll_table(self, table: str) -> None:
        # Периодическая полная пересинхронизация — сверка удалённых строк.
        if self._full_resync_every > 0:
            self._resync_counter += 1
            if self._resync_counter >= self._full_resync_every:
                self._resync_counter = 0
                with self._state_lock:
                    self._stats["full_resyncs"] += 1
                self._ensure_table_schema(table)
                rows, last = self._fetch_all(table)
                self._dispatch_replace(table, rows)
                # курсор не откатываем: новое значение только если оно больше
                prev = self._last_sync.get(table)
                if last is not None and (prev is None or last > prev):
                    self._last_sync[table] = last
                return
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

    def _dispatch_replace(self, table: str, rows: List[dict]) -> None:
        """Полная пересинхронизация: заменить содержимое таблицы целиком."""
        callback = self._on_replace_records
        if callback is None:
            return
        try:
            callback(table, rows)
        except Exception:
            with self._state_lock:
                self._stats["errors"] += 1

    def _ensure_table_schema(self, table: str) -> None:
        """Передать описание колонок таблицы (PG information_schema) в store."""
        callback = self._on_schema
        if callback is None:
            return
        columns = self._fetch_schema(table)
        if not columns:
            return
        try:
            callback(table, columns)
        except Exception:
            with self._state_lock:
                self._stats["errors"] += 1

    def _fetch_schema(self, table: str) -> List[dict]:
        """Описание колонок таблицы из PG: типы, NOT NULL, комментарии."""
        schema, name = self._split_table(table)
        if not name:
            return []
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute(
                "SELECT c.column_name, c.data_type, c.is_nullable, "
                "c.character_maximum_length, c.numeric_precision, c.numeric_scale, "
                "pgd.description AS column_comment "
                "FROM information_schema.columns c "
                "JOIN pg_class pc ON pc.relname = c.table_name "
                "AND pc.relnamespace = (SELECT oid FROM pg_namespace WHERE nspname = %s) "
                "LEFT JOIN pg_catalog.pg_description pgd "
                "ON pgd.objsubid = c.ordinal_position AND pgd.objoid = pc.oid "
                "WHERE c.table_schema = %s AND c.table_name = %s "
                "ORDER BY c.ordinal_position",
                [schema, name],
            )
            col_rows = [dict(r) for r in cur.fetchall()]
        finally:
            cur.close()

        cur = self._conn.cursor()
        try:
            cur.execute(
                "SELECT obj_description(pc.oid) FROM pg_class pc "
                "JOIN pg_namespace n ON n.oid = pc.relnamespace "
                "WHERE n.nspname = %s AND pc.relname = %s",
                [schema, name],
            )
            row = cur.fetchone()
        finally:
            cur.close()
        table_comment = row[0] if row else None

        columns: List[dict] = []
        if table_comment:
            columns.append({
                "name": "__table__", "type": "", "not_null": False,
                "comment": table_comment,
            })
        for r in col_rows:
            dt = r["data_type"]
            if dt == "character varying" and r["character_maximum_length"]:
                dt = f"character varying({r['character_maximum_length']})"
            elif dt == "character" and r["character_maximum_length"]:
                dt = f"character({r['character_maximum_length']})"
            elif dt == "numeric" and r["numeric_precision"]:
                dt = f"numeric({r['numeric_precision']},{r.get('numeric_scale') or 0})"
            columns.append({
                "name": r["column_name"],
                "type": dt,
                "not_null": r["is_nullable"] == "NO",
                "comment": r["column_comment"],
            })
        return columns

    def _split_table(self, table: str) -> tuple[str, str]:
        """Разбить 'oarb.audits' на (schema, table)."""
        if "." in table:
            schema, name = table.split(".", 1)
            return schema, name
        return self._schema, table

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
        """Проверить существование целевой таблицы записи (без авто-создания).

        Сервис не провижинит схему: если ``write_schema.write_table`` отсутствует
        в PostgreSQL — логируем явную ошибку и помечаем write недоступным.
        Создание таблицы — задача миграций/бootstrap, а не синхрон-сервиса.
        """
        if not self._write_table or self._conn is None:
            return
        cur = self._conn.cursor()
        try:
            cur.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name = %s",
                [self._write_schema, self._write_table],
            )
            exists = cur.fetchone()
        finally:
            cur.close()
        if exists:
            return
        missing = f"{self._write_schema}.{self._write_table}"
        self._write_table = ""
        logger.error(
            "AuditSyncService: таблица записи не найдена: %s "
            "— запись ответов навыка в PG отключена. Создайте таблицу "
            "(см. sql/created_tables.sql), сервис DDL не выполняет.",
            missing,
        )

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
        attempt = 0
        while self._running and (self._conn is None or self._conn.closed):
            attempt += 1
            try:
                self._conn = psycopg2.connect(
                    self._dsn, gssencmode="disable", connect_timeout=10,
                )
                self._conn.autocommit = True
                with self._state_lock:
                    self._stats["reconnects"] += 1
                if attempt > 1:
                    logger.info(
                        "AuditSyncService connected to PG on attempt %d", attempt
                    )
                return
            except Exception as exc:
                with self._state_lock:
                    self._stats["errors"] += 1
                logger.warning(
                    "AuditSyncService PG connect failed (attempt %d, "
                    "retry in %.1fs): %s",
                    attempt, backoff, exc,
                )
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, self._reconnect_backoff_max)

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
