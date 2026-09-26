"""Background mirror для upstream SessionManager (JSONL) → PostgreSQL.

См. спеку ``openspec/specs/storage/session-hybridization/spec.md``
и правила пула в ``openspec/changes/storage-hybridization/design.md``
§ «Connection pool».

Upstream ``SessionManager`` (JSONL) — единственный hot-path writer и
единственный source of truth; PostgreSQL — cold-storage mirror для
multi-instance deploy и observability, обслуживается этим сервисом.

Архитектурные инварианты:

- Сервис работает в ``daemon=True`` потоке (sync-код с
  ``threading.Lock`` блокирует event loop; см. обоснование в
  ``PgDuckDbSyncService``);
- Внутри — ``threading.Lock`` вокруг ``_sync_cycle()`` (single-flight);
- **Пул — единый (utils.db)**, соединение НЕ создаётся в модуле.
  Acquire/release соединения происходит в одном worker-потоке пула,
  что соответствует правилам пула (D-Pool.3);
- Leader-election через ``pg_try_advisory_xact_lock`` (per-transaction
  lock; автоматически освобождается на COMMIT/ROLLBACK — никакого
  долгоживущего соединения);
- ``last-write-wins`` по ``updated_at`` (через ``UPDATE ... WHERE
  updated_at > existing.updated_at``); колонка ``version`` НЕ вводится;
- Upstream JSONL — единственный source of truth. Cleanup удаляет из
  PG любую строку без upstream-двойника;
- ``enabled=false`` (``gateway.session_cold_sync.enabled``) — escape
  hatch, поток не запускается вообще;
- Ошибки логируются через ``DbLoggingService.try_log_event`` (по
  контракту ``logging-db``); никаких прямых ``INSERT INTO
  agent_gateway_logs``.

Shutdown-порядок (D21): ``SessionColdSyncService.stop()`` вызывается
ДО закрытия ``SessionManager``, чтобы успеть синхронизировать
последние dirty-сессии.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lib.services.db_logging_service import LogEvent, try_log_event

if TYPE_CHECKING:
    from lib.services.db_logging_service import DbLoggingService
    from nanobot.session.manager import SessionManager


_ADVISORY_LOCK_KEY = "storage_hybridization_session_cold_sync"
_BACKOFF_BASE_SEC = 1.0
_BACKOFF_CAP_SEC = 16 * 60.0
_POOL_BUSY_BACKOFF_SEC = 5.0


class SessionColdSyncService:
    """Зеркалирует upstream JSONL → PG в фоне.

    Получает пул через DI: в ``ApplicationContext`` создаётся
    ``utils.db`` (через ``utils.db.configure(dsn)`` — синглтон),
    ``SessionColdSyncService`` использует ``utils.db.transaction()``
    и ``utils.db.run(...)`` — никаких собственных psycopg2-пулов.

    Аргументы конструктора — все опциональные с дефолтами; PG DSN берётся
    из ``config`` через ``ApplicationContext`` (см. ``_make_*``).
    """

    def __init__(
        self,
        session_manager: "SessionManager",
        pg_dsn: str,
        schema: str = "public",
        meta_table: str = "agent_session_meta",
        messages_table: str = "agent_session_messages",
        sync_interval_sec: float = 30.0,
        batch_size: int = 50,
        enabled: bool = True,
        db_logging_service: "DbLoggingService | None" = None,
        pool_acquire_timeout_sec: float = 10.0,
    ) -> None:
        self._session_manager = session_manager
        self._pg_dsn = pg_dsn
        self._schema = schema
        self._meta_table = meta_table
        self._messages_table = messages_table
        self._sync_interval_sec = max(1.0, float(sync_interval_sec))
        self._batch_size = max(1, int(batch_size))
        self._enabled = bool(enabled)
        self._pool_acquire_timeout_sec = max(0.1, float(pool_acquire_timeout_sec))

        self._db_logging = db_logging_service

        self._fq_meta = self._quote(f"{schema}.{meta_table}")
        self._fq_messages = self._quote(f"{schema}.{messages_table}")

        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        self._cycles_total = 0
        self._cycles_failed_total = 0
        self._consecutive_failures = 0
        self._cycles_skipped_lock_busy = 0
        self._cycles_skipped_pool_busy = 0
        self._rows_synced_total = 0
        self._messages_synced_total = 0
        self._last_success_ts: float | None = None
        self._last_pool_wait_seconds: float | None = None
        self._last_upstream_session_count: int = 0
        self._last_pg_session_count: int = 0

        self._pool_size: int | None = None
        self._pool_available: int | None = None

        if pg_dsn:
            from utils.db import configure as _cfg
            _cfg(pg_dsn)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def start(self) -> None:
        """Запустить фоновый sync-поток. No-op если ``enabled=False``."""
        if not self._enabled:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._worker,
            name="session-cold-sync",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout_sec: float = 30.0) -> None:
        """Корректно остановить поток.

        Перед teardown пытается выполнить финальный ``_sync_cycle()``
        (для D21 shutdown order), если lock свободен в пределах
        ``timeout_sec``.
        """
        if self._thread is None:
            return
        self._stop_event.set()
        acquired = self._state_lock.acquire(timeout=timeout_sec)
        if acquired:
            try:
                try:
                    self._sync_cycle()
                except Exception:
                    logging.getLogger(__name__).exception(
                        "session_cold_sync: final flush failed",
                    )
            finally:
                self._state_lock.release()
        self._thread.join(timeout_sec)
        self._thread = None

    def _worker(self) -> None:
        while not self._stop_event.is_set():
            try:
                with self._state_lock:
                    self._sync_cycle()
            except Exception as exc:
                self._consecutive_failures += 1
                self._cycles_failed_total += 1
                self._log_failure(exc)
            delay = self._compute_delay()
            if self._stop_event.wait(timeout=delay):
                break

    def _compute_delay(self) -> float:
        if self._consecutive_failures <= 0:
            return self._sync_interval_sec
        backoff = min(
            self._sync_interval_sec,
            _BACKOFF_BASE_SEC * (2 ** min(self._consecutive_failures, 5)),
        )
        return min(backoff, _BACKOFF_CAP_SEC)

    def _sync_cycle(self) -> None:
        """Один цикл sync: leader-election → read upstream → write PG.

        Все исключения пробрасываются наверх (вызывающий инкрементирует
        счётчики и логирует).
        """
        self._cycles_total += 1
        t_start = time.monotonic()

        acquired = self._try_advisory_xact_lock()
        if not acquired:
            self._cycles_skipped_lock_busy += 1
            self._last_pool_wait_seconds = time.monotonic() - t_start
            return

        try:
            upstream_sessions = self._read_upstream()
            upstream_keys = {s["key"] for s in upstream_sessions}
            self._last_upstream_session_count = len(upstream_keys)

            sorted_sessions = sorted(upstream_sessions, key=lambda s: s["key"])
            self._sync_batches(sorted_sessions)
            self._cleanup_missing(upstream_keys)

            pg_count = self._count_pg_sessions()
            self._last_pg_session_count = pg_count
            self._last_success_ts = time.time()
            self._consecutive_failures = 0
        finally:
            self._last_pool_wait_seconds = time.monotonic() - t_start

    def _read_upstream(self) -> list[dict[str, Any]]:
        return self._session_manager.list_sessions() or []

    def _sync_batches(self, sorted_sessions: list[dict[str, Any]]) -> None:
        for start in range(0, len(sorted_sessions), self._batch_size):
            batch = sorted_sessions[start:start + self._batch_size]
            for sm in batch:
                key = sm.get("key")
                if not key:
                    continue
                self._sync_session(key)

    def _sync_session(self, key: str) -> None:
        """Mirror одной сессии: meta + messages (если свежее)."""
        try:
            snapshot = self._session_manager.read_session_snapshot(key)
        except Exception:
            return

        if snapshot is None:
            return

        upstream_updated_at = getattr(snapshot, "updated_at", None)
        if upstream_updated_at is None:
            return

        existing_updated_at = self._read_pg_updated_at(key)
        if existing_updated_at is not None and existing_updated_at >= upstream_updated_at:
            return

        self._upsert_meta(key, snapshot, upstream_updated_at)
        self._replace_messages(key, snapshot)
        self._rows_synced_total += 1
        self._messages_synced_total += len(getattr(snapshot, "messages", []) or [])

    def _upsert_meta(self, key: str, snapshot: Any, updated_at: datetime) -> None:
        metadata_val = getattr(snapshot, "metadata", None) or {}
        last_consolidated = getattr(snapshot, "last_consolidated", None)
        created_at = getattr(snapshot, "created_at", updated_at)

        def _work(conn) -> None:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {self._fq_meta} SET "
                    f"updated_at = %s, last_consolidated = %s, metadata = %s "
                    f"WHERE session_key = %s "
                    f"AND updated_at < %s "
                    f"RETURNING session_key",
                    (updated_at, last_consolidated, json.dumps(metadata_val),
                     key, updated_at),
                )
                if cur.fetchone() is None:
                    cur.execute(
                        f"SELECT 1 FROM {self._fq_meta} "
                        f"WHERE session_key = %s",
                        (key,),
                    )
                    if cur.fetchone() is None:
                        cur.execute(
                            f"INSERT INTO {self._fq_meta} "
                            f"(session_key, created_at, updated_at, "
                            f"last_consolidated, metadata) "
                            f"VALUES (%s, %s, %s, %s, %s)",
                            (key, created_at, updated_at,
                             last_consolidated, json.dumps(metadata_val)),
                        )

        self._run_in_tx(_work)

    def _replace_messages(self, key: str, snapshot: Any) -> None:
        messages = getattr(snapshot, "messages", []) or []
        from psycopg2.extras import execute_values

        def _work(conn) -> None:
            with conn.cursor() as cur:
                cur.execute(
                    f"DELETE FROM {self._fq_messages} WHERE session_key = %s",
                    (key,),
                )
                if not messages:
                    return
                rows: list[tuple] = []
                for seq, msg in enumerate(messages):
                    rows.append((
                        key,
                        seq,
                        msg.get("role", "user"),
                        msg.get("content", "") or "",
                        msg.get("timestamp"),
                    ))
                execute_values(
                    cur,
                    f"INSERT INTO {self._fq_messages} "
                    f"(session_key, seq, role, content, msg_timestamp) VALUES %s",
                    rows,
                    page_size=self._batch_size,
                )

        self._run_in_tx(_work)

    def _cleanup_missing(self, upstream_keys: set[str]) -> None:
        """Удалить из PG сессии, которых больше нет в upstream JSONL.

        Upstream JSONL — единственный source of truth. Любая строка
        в PG без upstream-двойника считается устаревшей и удаляется.
        """
        try:
            pg_rows = self._select_all_pg_keys()
        except Exception:
            return

        to_delete = sorted(row for row in pg_rows if row not in upstream_keys)
        if not to_delete:
            return

        def _work(conn) -> None:
            with conn.cursor() as cur:
                cur.execute(
                    f"DELETE FROM {self._fq_messages} WHERE session_key = ANY(%s)",
                    (to_delete,),
                )
                cur.execute(
                    f"DELETE FROM {self._fq_meta} WHERE session_key = ANY(%s)",
                    (to_delete,),
                )

        try:
            self._run_in_tx(_work)
        except Exception:
            return

        for key in to_delete:
            try:
                self._log_deleted(key)
            except Exception:
                pass

    def _read_pg_updated_at(self, key: str) -> datetime | None:
        def _work(conn) -> datetime | None:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT updated_at FROM {self._fq_meta} "
                    f"WHERE session_key = %s",
                    (key,),
                )
                row = cur.fetchone()
                return row[0] if row else None

        return self._run_in_tx(_work)

    def _count_pg_sessions(self) -> int:
        def _work(conn) -> int:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM {self._fq_meta}")
                return cur.fetchone()[0]
        return self._run_in_tx(_work)

    def _select_all_pg_keys(self) -> list[str]:
        def _work(conn) -> list[str]:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT session_key FROM {self._fq_meta}"
                )
                return [r[0] for r in cur.fetchall()]
        return self._run_in_tx(_work)

    def _try_advisory_xact_lock(self) -> bool:
        """Per-transaction advisory lock.

        ``pg_try_advisory_xact_lock`` держит lock до конца транзакции
        (COMMIT/ROLLBACK); нет риска «зависшего» lock на соединении.
        См. D-Pool.2 «Advisory lock — на выделенном соединении»:
        вместо долгоживущего соединения используем короткий lease с
        xact-scoped lock.
        """
        def _work(conn) -> bool:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtext(%s)::bigint)",
                    (_ADVISORY_LOCK_KEY,),
                )
                row = cur.fetchone()
                return bool(row and row[0])
        try:
            return self._run_in_tx(_work)
        except Exception:
            return False

    def _run_in_tx(self, fn):
        """Выполнить ``fn(conn)`` в короткой транзакции через utils.db.

        ``utils.db.transaction()`` сам управляет COMMIT/ROLLBACK
        (autocommit=False, COMMIT на выходе без ошибки, ROLLBACK на
        исключении); lease на соединение выдаётся на один job и
        возвращается в worker-потоке — D-Pool.3 выполняется.
        """
        from utils.db import transaction
        with transaction() as conn:
            return fn(conn)

    def _log_failure(self, exc: Exception) -> None:
        if self._db_logging is None:
            return
        event = LogEvent(
            event_type="session_cold_sync_failed",
            level="WARNING",
            summary=f"session cold sync cycle failed: {exc.__class__.__name__}",
            payload={"error": str(exc)[:500]},
            metadata={"consecutive_failures": self._consecutive_failures},
        )
        try_log_event(
            self._db_logging,
            event,
            producer="SessionColdSyncService",
            event_type=event.event_type,
        )

    def _log_deleted(self, key: str) -> None:
        if self._db_logging is None:
            return
        event = LogEvent(
            event_type="session_cold_sync_deleted",
            level="INFO",
            summary=f"session_cold_sync: deleted mirror row for {key}",
            payload={"session_key": key},
        )
        try_log_event(
            self._db_logging,
            event,
            producer="SessionColdSyncService",
            event_type=event.event_type,
        )

    def get_stats(self) -> dict[str, Any]:
        """Метрики для health-check.

        D-Pool.6: ``pool_size`` / ``pool_available`` / ``pool_wait_seconds``
        публикуются в stats, чтобы отличить «sync не работает, потому что
        реплика не лидер» от «sync не работает, потому что пул занят».

        Размер пула и доступные соединения подтягиваются из
        ``utils.db.get_stats()`` (D-Pool.3: единый пул, метрики
        публикуются его владельцем).
        """
        last_success_lag = None
        if self._last_success_ts is not None:
            last_success_lag = max(0.0, time.time() - self._last_success_ts)
        pool_size, pool_available = self._read_pool_size()
        return {
            "enabled": self._enabled,
            "cycles_total": self._cycles_total,
            "cycles_failed_total": self._cycles_failed_total,
            "cycles_skipped_lock_busy": self._cycles_skipped_lock_busy,
            "cycles_skipped_pool_busy": self._cycles_skipped_pool_busy,
            "consecutive_failures": self._consecutive_failures,
            "last_success_ts": self._last_success_ts,
            "last_success_lag_seconds": last_success_lag,
            "pool_size": pool_size,
            "pool_available": pool_available,
            "pool_wait_seconds": self._last_pool_wait_seconds,
            "rows_synced_total": self._rows_synced_total,
            "messages_synced_total": self._messages_synced_total,
            "upstream_session_count": self._last_upstream_session_count,
            "pg_session_count": self._last_pg_session_count,
        }

    def _read_pool_size(self) -> tuple[int | None, int | None]:
        """Читает pool_size / pool_available из utils.db.get_stats().

        Если utils.db.get_stats недоступен (например, в тестах без
        реального пула) — возвращает ``(None, None)``; это нормальное
        состояние, не ошибка.
        """
        try:
            from utils.db import get_stats as _db_stats
            stats = _db_stats()
        except Exception:
            return self._pool_size, self._pool_available
        if not isinstance(stats, dict):
            return self._pool_size, self._pool_available
        size = stats.get("pool_size")
        available = stats.get("pool_available")
        if isinstance(size, int):
            self._pool_size = size
        if isinstance(available, int):
            self._pool_available = available
        return self._pool_size, self._pool_available

    @staticmethod
    def _validate_ident(part: str) -> None:
        if not part or not part.replace("_", "").replace("$", "").isalnum():
            raise ValueError(f"Unsafe SQL identifier part: {part!r}")

    @classmethod
    def _quote(cls, ident: str) -> str:
        parts = ident.split(".")
        for part in parts:
            cls._validate_ident(part)
        return ".".join(f'"{p}"' for p in parts)


def resolve_default_sqlite_path() -> Path:
    """Дефолтный путь к SQLite-файлу ``LLMUsageStore`` из design D4.

    Используется в ``lib.services.llm_usage_store_factory``.
    """
    return Path.home() / ".cache" / "nanobot" / "usage" / "usage.db"