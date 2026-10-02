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
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lib.services.db_logging_service import LogEvent, try_log_event

if TYPE_CHECKING:
    from nanobot.session.manager import SessionManager

    from lib.services.db_logging_service import DbLoggingService


_ADVISORY_LOCK_KEY = "storage_hybridization_session_cold_sync"
_BACKOFF_BASE_SEC = 1.0
_BACKOFF_CAP_SEC = 16 * 60.0
_POOL_BUSY_BACKOFF_SEC = 5.0
_STALE_LOG_DEDUP_TTL = timedelta(seconds=60.0)


class SessionColdSyncService:
    """Зеркалирует upstream JSONL → PG в фоне.

    Получает пул через DI: в ``ApplicationContext`` создаётся
    ``utils.db`` (через ``utils.db.configure(dsn)`` — синглтон),
    ``SessionColdSyncService`` использует ``utils.db.transaction()``
    и ``utils.db.run(...)`` — никаких собственных psycopg2-пулов.

    Аргументы конструктора — все опциональные с дефолтами, кроме
    ``meta_table``/``messages_table``: имена таблиц читаются из конфигурации
    (``ApplicationContext`` → ``require_setting``) и в коде не зашиты.
    PG DSN берётся из ``config`` через ``ApplicationContext`` (см. ``_make_*``).
    """

    def __init__(
        self,
        session_manager: SessionManager,
        pg_dsn: str,
        *,
        meta_table: str,
        messages_table: str,
        schema: str = "public",
        sync_interval_sec: float = 30.0,
        batch_size: int = 50,
        enabled: bool = True,
        db_logging_service: DbLoggingService | None = None,
        pool_acquire_timeout_sec: float = 10.0,
        stale_tolerance_seconds: int = 120,
        sync_lag_threshold_seconds: int = 3600,
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
        self._stale_tolerance = timedelta(seconds=max(0, int(stale_tolerance_seconds)))
        self._sync_lag_threshold = timedelta(
            seconds=max(0, int(sync_lag_threshold_seconds))
        )
        if sync_lag_threshold_seconds < stale_tolerance_seconds:
            raise ValueError(
                f"sync_lag_threshold_seconds ({sync_lag_threshold_seconds}) "
                f"must be >= stale_tolerance_seconds ({stale_tolerance_seconds})"
            )

        self._db_logging = db_logging_service

        self._fq_meta = self._quote(f"{schema}.{meta_table}")
        self._fq_messages = self._quote(f"{schema}.{messages_table}")

        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = True

        self._cycles_total = 0
        self._cycles_failed_total = 0
        self._consecutive_failures = 0
        self._cycles_skipped_lock_busy = 0
        self._cycles_skipped_pool_busy = 0
        self._stale_detected_counter = 0
        self._sync_lag_exceeded_counter = 0
        self._stale_sync_skipped_counter = 0
        self._rows_synced_total = 0
        self._messages_synced_total = 0
        self._last_success_ts: float | None = None
        self._last_pool_wait_seconds: float | None = None
        self._last_upstream_session_count: int = 0
        self._last_pg_session_count: int = 0

        self._stale_logged_at: dict[str, datetime] = {}

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

        Устанавливает ``self._running = False`` и ждёт завершения
        текущего цикла в пределах ``timeout_sec``. Перед teardown
        пытается выполнить финальный ``_sync_cycle()`` для D21
        shutdown order (если lock свободен).
        """
        if self._thread is None:
            return
        self._running = False
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
        while self._running and not self._stop_event.is_set():
            try:
                with self._state_lock:
                    self._do_sync_batch()
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

    def _do_sync_batch(self) -> None:
        """Один цикл sync: leader-election → read upstream → D23/D11 sync.

        Реализует:
        - leader-election через ``pg_try_advisory_xact_lock``;
        - per-iteration ``self._running`` проверку (graceful
          shutdown по ``stop()``);
        - D23 stale-detection (если PG свежее JSONL + tolerance —
          пропустить sync для этой сессии);
        - reverse-lag detection (если JSONL свежее PG + threshold
          — залогировать ``sync_lag_exceeded``);
        - last-write-wins для нормальных сессий.

        Исключения пробрасываются наверх (вызывающий инкрементирует
        счётчики и логирует).
        """
        self._cycles_total += 1
        t_start = time.monotonic()

        if not self._try_advisory_xact_lock():
            self._cycles_skipped_lock_busy += 1
            self._last_pool_wait_seconds = time.monotonic() - t_start
            return

        try:
            upstream_sessions = self._read_upstream()
            upstream_keys = {s["key"] for s in upstream_sessions}
            self._last_upstream_session_count = len(upstream_keys)

            sorted_sessions = sorted(upstream_sessions, key=lambda s: s["key"])
            for sm in sorted_sessions:
                if not self._running:
                    return
                key = sm.get("key")
                if not key:
                    continue
                self._sync_session_with_detection(key)
            self._cleanup_missing(upstream_keys)

            pg_count = self._count_pg_sessions()
            self._last_pg_session_count = pg_count
            self._last_success_ts = time.time()
            self._consecutive_failures = 0
        finally:
            self._last_pool_wait_seconds = time.monotonic() - t_start

    def _sync_cycle(self) -> None:
        """Backward-compat alias: финальный flush при ``stop()``.

        Реализация идентична ``_do_sync_batch`` (вызывается при
        shutdown для D21).
        """
        self._do_sync_batch()

    def _sync_session_with_detection(self, key: str) -> None:
        """Один ключ: D23 stale-check + reverse-lag + LWW sync.

        Структура (согласно tasks.md 3.4):
          1. ``existing is None`` — нормальный sync (новая сессия).
          2. ``existing > jsonl + tolerance`` — STALE, log
             ``session_stale_detected``, skip.
          3. ``existing >= jsonl`` (EQUAL/PG-WITHIN-TOLERANCE) — silent
             skip (no-op).
          4. else — нормальный sync + проверка sync_lag_exceeded.
        """
        try:
            snapshot = self._session_manager.read_session_snapshot(key)
        except Exception:
            return
        if snapshot is None:
            return

        jsonl_updated_at = getattr(snapshot, "updated_at", None)
        if jsonl_updated_at is None:
            return

        pg_meta = self._read_pg_updated_at(key)
        pg_updated_at = pg_meta if pg_meta is None else pg_meta.get("updated_at")

        if pg_updated_at is None:
            # 1. Новая сессия — нормальный sync.
            self._do_lww_sync(key, snapshot, jsonl_updated_at)
            return

        if pg_updated_at > jsonl_updated_at + self._stale_tolerance:
            # 2. STALE: PG свежее JSONL + tolerance → пропуск.
            self._stale_sync_skipped_counter += 1
            if not self._is_stale_logged_recently(key):
                self._log_stale(key, jsonl_updated_at, pg_updated_at)
                self._stale_logged_at[key] = datetime.now()
                self._stale_detected_counter += 1
            return

        if pg_updated_at >= jsonl_updated_at:
            # 3. EQUAL / PG-WITHIN-TOLERANCE — silent skip (current behavior).
            # 3. EQUAL / PG-WITHIN-TOLERANCE — silent skip (current behavior).
            return

        # 4. JSONL > PG → нормальный sync + проверка reverse-lag.
        self._do_lww_sync(key, snapshot, jsonl_updated_at)
        if jsonl_updated_at > pg_updated_at + self._sync_lag_threshold:
            self._log_lag_exceeded(key, jsonl_updated_at, pg_updated_at)
            self._sync_lag_exceeded_counter += 1

    def _do_lww_sync(self, key: str, snapshot: Any, jsonl_updated_at: datetime) -> None:
        """LWW-sync: записать meta + messages в PG."""
        self._upsert_meta(key, snapshot, jsonl_updated_at)
        self._replace_messages(key, snapshot)
        self._rows_synced_total += 1
        self._messages_synced_total += len(getattr(snapshot, "messages", []) or [])

    def _is_stale_logged_recently(self, key: str) -> bool:
        """True, если для ``key`` уже логировали stale-detected
        за последние ``_STALE_LOG_DEDUP_TTL`` секунд."""
        last = self._stale_logged_at.get(key)
        if last is None:
            return False
        if datetime.now() - last > _STALE_LOG_DEDUP_TTL:
            self._stale_logged_at.pop(key, None)
            return False
        return True

    def _read_upstream(self) -> list[dict[str, Any]]:
        return self._session_manager.list_sessions() or []

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

    def _log_stale(
        self,
        key: str,
        jsonl_updated_at: datetime,
        pg_updated_at: datetime,
    ) -> None:
        """D23: PG свежее JSONL + tolerance → логируем ``session_stale_detected``.

        Используется in-memory dedup ``_stale_logged_at`` (TTL 60s),
        чтобы не флудить БД на каждом sync-цикле.
        """
        if self._db_logging is None:
            return
        event = LogEvent(
            event_type="session_stale_detected",
            level="WARNING",
            summary=f"session_stale_detected: {key} (PG newer than JSONL + tolerance)",
            session_id=key,
            payload={
                "session_key": key,
                "jsonl_updated_at": jsonl_updated_at.isoformat(),
                "pg_updated_at": pg_updated_at.isoformat(),
                "tolerance_seconds": self._stale_tolerance.total_seconds(),
            },
        )
        try_log_event(
            self._db_logging,
            event,
            producer="SessionColdSyncService",
            event_type=event.event_type,
        )

    def _log_lag_exceeded(
        self,
        key: str,
        jsonl_updated_at: datetime,
        pg_updated_at: datetime,
    ) -> None:
        """Reverse-lag: JSONL свежее PG + threshold → логируем ``sync_lag_exceeded``."""
        if self._db_logging is None:
            return
        event = LogEvent(
            event_type="sync_lag_exceeded",
            level="WARNING",
            summary=f"sync_lag_exceeded: {key} (JSONL newer than PG + threshold)",
            session_id=key,
            payload={
                "session_key": key,
                "jsonl_updated_at": jsonl_updated_at.isoformat(),
                "pg_updated_at": pg_updated_at.isoformat(),
                "threshold_seconds": self._sync_lag_threshold.total_seconds(),
            },
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
            "stale_detected_total": self._stale_detected_counter,
            "sync_lag_exceeded_total": self._sync_lag_exceeded_counter,
            "stale_sync_skipped_total": self._stale_sync_skipped_counter,
            "stale_tolerance_seconds": int(self._stale_tolerance.total_seconds()),
            "sync_lag_threshold_seconds": int(self._sync_lag_threshold.total_seconds()),
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

    Хранилище создаёт библиотека: ``nanobot.llm_usage.get_llm_usage_store()``.
    """
    return Path.home() / ".cache" / "nanobot" / "usage" / "usage.db"
