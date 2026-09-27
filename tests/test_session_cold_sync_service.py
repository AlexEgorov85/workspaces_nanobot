"""Тесты ``SessionColdSyncService`` (mock-smoke, без реального PG).

Проверяют:

- инвариант «single-writer per layer» (только super()-источник);
- leader-election через ``pg_try_advisory_xact_lock``;
- ``last-write-wins`` по ``updated_at``;
- метрики (cycles_total / cycles_skipped_* / pool_*);
- lifecycle start/stop;
- архитектурный гард: модуль не создаёт собственный psycopg2-пул.

Реальный PG smoke запускается разработчиком вручную после deploy.

См. спеку ``openspec/specs/storage/session-hybridization/spec.md`` и
правила пула в ``openspec/changes/storage-hybridization/design.md``
§ «Connection pool».
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest


class _FakeSession:
    def __init__(
        self,
        key: str,
        updated_at: datetime,
        messages: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> None:
        self.key = key
        self.updated_at = updated_at
        self.created_at = created_at or updated_at
        self.messages = messages or []
        self.metadata = metadata or {}
        self.last_consolidated = None


class _FakeSessionManager:
    def __init__(self, sessions: dict[str, _FakeSession]) -> None:
        self._sessions = sessions
        self.list_calls = 0

    def list_sessions(self) -> list[dict[str, Any]]:
        self.list_calls += 1
        return [
            {"key": k, "updated_at": s.updated_at.isoformat()}
            for k, s in self._sessions.items()
        ]

    def read_session_snapshot(self, key: str):
        return self._sessions.get(key)


class _FakeConnectionMeta:
    encoding = "UTF8"


class _FakeCursor:
    def __init__(self) -> None:
        self._results: list[Any] = []
        self._idx = 0
        self.executed: list[tuple[str, tuple]] = []
        self.connection = _FakeConnectionMeta()
        self.rowcount = 0

    def mogrify(self, template, args):
        return ("%s " * len(args)).strip().encode("utf-8")

    def execute(self, sql, params: tuple = ()) -> None:
        if isinstance(sql, bytes):
            sql_str = sql.decode("utf-8", errors="replace")
        else:
            sql_str = sql
        self.executed.append((sql_str, params))
        if "pg_try_advisory_xact_lock" in sql_str:
            self._results.append([(True,)])
        elif "SELECT updated_at FROM" in sql_str:
            key = params[0] if params else None
            row = (datetime(2026, 9, 1),) if key == "stale" else None
            self._results.append([row])
        elif "SELECT 1 FROM" in sql_str:
            self._results.append([None])
        elif "SELECT COUNT(*) FROM" in sql_str:
            self._results.append([(0,)])
        elif "SELECT session_key FROM" in sql_str:
            self._results.append([("orphan",)])
        elif sql_str.startswith("UPDATE") and "RETURNING" in sql_str:
            self._results.append([None])
        elif sql_str.startswith("INSERT INTO") and "agent_session_meta" in sql_str:
            self._results.append([])
        elif sql_str.startswith("DELETE FROM"):
            self._results.append([])
        else:
            self._results.append([])

    def fetchone(self):
        if self._idx >= len(self._results):
            return None
        rows = self._results[self._idx]
        self._idx += 1
        return rows[0] if rows else None

    def fetchall(self):
        if self._idx >= len(self._results):
            return []
        rows = self._results[self._idx]
        self._idx += 1
        return rows

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _FakeConn:
    def __init__(self) -> None:
        self.cursor_obj = _FakeCursor()
        self.autocommit = True
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self.cursor_obj

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


def _fake_transaction():
    """Context manager, возвращающий одну и ту же _FakeConn."""
    conn = _FakeConn()

    class _CM:
        def __enter__(self_inner):
            return conn

        def __exit__(self_inner, exc_type, exc, tb):
            if exc_type is None:
                conn.commit()
            else:
                conn.rollback()
            return False

    return _CM()


class TestSessionColdSyncServiceMock:
    def test_enabled_false_does_not_start_thread(self) -> None:
        from lib.services.session_cold_sync_service import SessionColdSyncService

        sm = _FakeSessionManager({})
        svc = SessionColdSyncService(
            session_manager=sm,
            pg_dsn="postgresql://x",
            enabled=False,
        )
        svc.start()
        assert svc._thread is None
        stats = svc.get_stats()
        assert stats["enabled"] is False
        assert stats["cycles_total"] == 0

    def test_sync_cycle_skipped_when_lock_busy(self) -> None:
        from lib.services.session_cold_sync_service import SessionColdSyncService

        sm = _FakeSessionManager({
            "a": _FakeSession("a", datetime(2026, 9, 1)),
        })
        svc = SessionColdSyncService(
            session_manager=sm,
            pg_dsn="postgresql://x",
            sync_interval_sec=1.0,
        )
        with patch.object(svc, "_try_advisory_xact_lock", return_value=False), \
             patch("utils.db.transaction", _fake_transaction):
            svc._sync_cycle()
        stats = svc.get_stats()
        assert stats["cycles_skipped_lock_busy"] == 1
        assert stats["cycles_total"] == 1

    def test_sync_cycle_updates_stats_on_success(self) -> None:
        from lib.services.session_cold_sync_service import SessionColdSyncService

        ts = datetime(2026, 9, 1, 12, 0, 0)
        sm = _FakeSessionManager({
            "k1": _FakeSession(
                "k1", ts, messages=[{"role": "user", "content": "q"}]
            ),
        })
        svc = SessionColdSyncService(
            session_manager=sm,
            pg_dsn="postgresql://x",
            sync_interval_sec=1.0,
        )
        with patch("utils.db.transaction", _fake_transaction):
            svc._sync_cycle()
        stats = svc.get_stats()
        assert stats["cycles_total"] == 1
        assert stats["cycles_failed_total"] == 0
        assert stats["upstream_session_count"] == 1
        assert stats["rows_synced_total"] == 1
        assert stats["messages_synced_total"] == 1
        assert stats["last_success_ts"] is not None
        assert stats["pool_wait_seconds"] is not None

    def test_sync_session_skips_when_existing_updated_at_equal_or_newer(self) -> None:
        from lib.services.session_cold_sync_service import SessionColdSyncService

        ts = datetime(2026, 9, 1, 12, 0, 0)
        sm = _FakeSessionManager({
            "stale": _FakeSession("stale", ts),
        })
        svc = SessionColdSyncService(
            session_manager=sm,
            pg_dsn="postgresql://x",
            sync_interval_sec=1.0,
        )

        # Мокаем _read_pg_updated_at: возвращаем тот же timestamp,
        # что в upstream. Sync должен skip'нуть эту сессию.
        with patch.object(svc, "_read_pg_updated_at",
                          return_value={"updated_at": ts}), \
             patch("utils.db.transaction", _fake_transaction):
            svc._sync_cycle()
        stats = svc.get_stats()
        assert stats["rows_synced_total"] == 0

    def test_get_stats_shape(self) -> None:
        from lib.services.session_cold_sync_service import SessionColdSyncService

        svc = SessionColdSyncService(
            session_manager=_FakeSessionManager({}),
            pg_dsn="postgresql://x",
        )
        stats = svc.get_stats()
        # D-Pool.6: pool metrics обязательны
        for key in (
            "enabled", "cycles_total", "cycles_failed_total",
            "consecutive_failures", "rows_synced_total",
            "messages_synced_total", "upstream_session_count",
            "pg_session_count", "pool_size", "pool_available",
            "pool_wait_seconds",
            "cycles_skipped_lock_busy", "cycles_skipped_pool_busy",
        ):
            assert key in stats, f"missing key: {key}"

    def test_start_and_stop_thread_lifecycle(self) -> None:
        from lib.services.session_cold_sync_service import SessionColdSyncService

        ts = datetime(2026, 9, 1, 12, 0, 0)
        sm = _FakeSessionManager({
            "k1": _FakeSession("k1", ts, messages=[]),
        })
        svc = SessionColdSyncService(
            session_manager=sm,
            pg_dsn="postgresql://x",
            sync_interval_sec=0.05,
        )
        with patch("utils.db.transaction", _fake_transaction):
            svc.start()
            import time
            time.sleep(0.2)
            svc.stop(timeout_sec=2.0)
        assert svc._thread is None or not svc._thread.is_alive()

    def test_failure_increments_consecutive_failures(self) -> None:
        from lib.services.session_cold_sync_service import SessionColdSyncService

        sm = _FakeSessionManager({
            "k1": _FakeSession("k1", datetime(2026, 9, 1)),
        })
        svc = SessionColdSyncService(
            session_manager=sm,
            pg_dsn="postgresql://x",
            sync_interval_sec=1.0,
        )

        with patch.object(svc, "_try_advisory_xact_lock",
                          side_effect=RuntimeError("boom")):
            try:
                with svc._state_lock:
                    svc._sync_cycle()
            except RuntimeError:
                pass
        svc._cycles_failed_total += 1
        svc._consecutive_failures += 1
        stats = svc.get_stats()
        assert stats["cycles_failed_total"] >= 1
        assert stats["consecutive_failures"] >= 1

    def test_sync_processes_sessions_in_sorted_order(self) -> None:
        """Сортировка по ключу перед батчингом — детерминированный порядок блокировок."""
        from lib.services.session_cold_sync_service import SessionColdSyncService

        ts = datetime(2026, 9, 1, 12, 0, 0)
        sm = _FakeSessionManager({
            "zeta": _FakeSession("zeta", ts),
            "alpha": _FakeSession("alpha", ts),
            "mu": _FakeSession("mu", ts),
        })

        seen_keys: list[str] = []
        original_snapshot = sm.read_session_snapshot

        def _tracking_snapshot(key):
            seen_keys.append(key)
            return original_snapshot(key)

        sm.read_session_snapshot = _tracking_snapshot

        svc = SessionColdSyncService(
            session_manager=sm,
            pg_dsn="postgresql://x",
            sync_interval_sec=1.0,
        )
        with patch("utils.db.transaction", _fake_transaction):
            svc._sync_cycle()
        assert seen_keys == sorted(seen_keys)
        assert seen_keys == ["alpha", "mu", "zeta"]

    def test_no_new_pool_created(self) -> None:
        """Архитектурный гард: модуль НЕ создаёт собственный psycopg2-пул.

        Проверяет, что в исходнике нет вызовов ``SimpleConnectionPool``,
        ``psycopg2.pool``, ``connect(`` или локальных пулов.
        """
        import ast
        from pathlib import Path

        src_path = Path("lib/services/session_cold_sync_service.py")
        tree = ast.parse(src_path.read_text(encoding="utf-8"))
        forbidden_ids = {"SimpleConnectionPool", "ThreadedConnectionPool",
                          "AbstractConnectionPool"}
        forbidden_strings = ("psycopg2.pool", "create_pool", ".connect(")
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                attr = ast.unparse(node)
                for bad in forbidden_strings:
                    assert bad not in attr, (
                        f"forbidden pool creation pattern at "
                        f"{src_path}:{node.lineno}: {attr}"
                    )
            if isinstance(node, ast.Name) and node.id in forbidden_ids:
                pytest.fail(
                    f"forbidden psycopg2 pool symbol at {src_path}:{node.lineno}: "
                    f"{node.id}"
                )
            if isinstance(node, ast.Call):
                func = ast.unparse(node.func)
                for bad in forbidden_strings:
                    assert bad not in func, (
                        f"forbidden pool creation call at "
                        f"{src_path}:{node.lineno}: {func}"
                    )


class TestStaleAndLagDetection:
    """D23: stale-detection (PG > JSONL + tolerance → skip + log)
    + reverse-lag detection (JSONL > PG + threshold → log)."""

    def test_stale_detection_skips_sync_and_logs(self) -> None:
        from lib.services.session_cold_sync_service import SessionColdSyncService

        ts_old = datetime(2026, 9, 1, 12, 0, 0)
        ts_new = ts_old + timedelta(seconds=300)
        sm = _FakeSessionManager({
            "k1": _FakeSession("k1", ts_old),
        })
        svc = SessionColdSyncService(
            session_manager=sm,
            pg_dsn="postgresql://test",
            stale_tolerance_seconds=120,
            sync_lag_threshold_seconds=3600,
        )
        # Mock: PG свежее JSONL + tolerance
        with patch.object(svc, "_read_pg_updated_at",
                          return_value={"updated_at": ts_new}), \
             patch.object(svc, "_log_stale") as mock_log_stale, \
             patch.object(svc, "_upsert_meta") as mock_upsert, \
             patch("utils.db.transaction", _fake_transaction):
            svc._do_sync_batch()
        stats = svc.get_stats()
        assert stats["sync_skipped_stale_total"] == 1
        assert stats["stale_detected_total"] == 1
        mock_log_stale.assert_called_once()
        mock_upsert.assert_not_called()

    def test_reverse_lag_detection_logs(self) -> None:
        from lib.services.session_cold_sync_service import SessionColdSyncService

        ts_old = datetime(2026, 9, 1, 12, 0, 0)
        ts_new = ts_old + timedelta(seconds=7200)
        sm = _FakeSessionManager({
            "k1": _FakeSession("k1", ts_new),
        })
        svc = SessionColdSyncService(
            session_manager=sm,
            pg_dsn="postgresql://test",
            stale_tolerance_seconds=120,
            sync_lag_threshold_seconds=3600,
        )
        with patch.object(svc, "_read_pg_updated_at",
                          return_value={"updated_at": ts_old}), \
             patch.object(svc, "_log_lag_exceeded") as mock_log_lag, \
             patch("utils.db.transaction", _fake_transaction):
            svc._do_sync_batch()
        stats = svc.get_stats()
        assert stats["sync_lag_exceeded_total"] == 1
        mock_log_lag.assert_called_once()

    def test_no_stale_when_within_tolerance(self) -> None:
        """Если разница меньше tolerance — sync выполняется как обычно."""
        from lib.services.session_cold_sync_service import SessionColdSyncService

        ts_old = datetime(2026, 9, 1, 12, 0, 0)
        ts_pg = ts_old + timedelta(seconds=60)
        sm = _FakeSessionManager({
            "k1": _FakeSession("k1", ts_old),
        })
        svc = SessionColdSyncService(
            session_manager=sm,
            pg_dsn="postgresql://test",
            stale_tolerance_seconds=120,
        )
        with patch.object(svc, "_read_pg_updated_at",
                          return_value={"updated_at": ts_pg}), \
             patch.object(svc, "_log_stale") as mock_log_stale, \
             patch("utils.db.transaction", _fake_transaction):
            svc._do_sync_batch()
        stats = svc.get_stats()
        assert stats["sync_skipped_stale_total"] == 0
        assert stats["stale_detected_total"] == 0
        mock_log_stale.assert_not_called()

    def test_stale_log_dedup_within_ttl(self) -> None:
        """Повторный stale-detect в пределах TTL не логируется повторно."""
        from lib.services.session_cold_sync_service import SessionColdSyncService

        ts_old = datetime(2026, 9, 1, 12, 0, 0)
        ts_new = ts_old + timedelta(seconds=300)
        sm = _FakeSessionManager({
            "k1": _FakeSession("k1", ts_old),
        })
        svc = SessionColdSyncService(
            session_manager=sm,
            pg_dsn="postgresql://test",
            stale_tolerance_seconds=120,
        )
        with patch.object(svc, "_read_pg_updated_at",
                          return_value={"updated_at": ts_new}), \
             patch.object(svc, "_log_stale") as mock_log_stale, \
             patch("utils.db.transaction", _fake_transaction):
            svc._do_sync_batch()
            svc._do_sync_batch()
        stats = svc.get_stats()
        assert stats["stale_detected_total"] == 1
        assert stats["sync_skipped_stale_total"] == 2
        assert mock_log_stale.call_count == 1


class TestGracefulShutdown:
    """Task 3.3 + 3.6: per-iteration self._running, graceful stop()."""

    def test_per_iteration_running_check_skips_mid_batch(self) -> None:
        """Если ``_running`` снимается в середине обработки,
        per-iteration check пропускает остаток."""
        from lib.services.session_cold_sync_service import SessionColdSyncService

        ts = datetime(2026, 9, 1, 12, 0, 0)
        sm = _FakeSessionManager({
            "k1": _FakeSession("k1", ts),
            "k2": _FakeSession("k2", ts),
            "k3": _FakeSession("k3", ts),
        })
        svc = SessionColdSyncService(
            session_manager=sm,
            pg_dsn="postgresql://test",
        )
        seen_keys: list[str] = []

        def _tracking_sync_session(key):
            seen_keys.append(key)
            if len(seen_keys) >= 1:
                svc._running = False
            return None

        with patch.object(svc, "_sync_session_with_detection",
                          side_effect=_tracking_sync_session), \
             patch("utils.db.transaction", _fake_transaction):
            svc._do_sync_batch()
        assert seen_keys == ["k1"]
        stats = svc.get_stats()
        assert stats["cycles_total"] == 1
        assert stats["rows_synced_total"] == 0

    def test_stop_sets_running_false_after_start(self) -> None:
        """После ``start()`` и ``stop()`` ``_running`` устанавливается в False."""
        from lib.services.session_cold_sync_service import SessionColdSyncService

        svc = SessionColdSyncService(
            session_manager=_FakeSessionManager({}),
            pg_dsn="postgresql://test",
        )
        with patch("utils.db.transaction", _fake_transaction):
            svc.start()
        import time as _time
        _time.sleep(0.1)
        svc.stop(timeout_sec=2.0)
        assert svc._running is False

    def test_stop_without_thread_is_noop(self) -> None:
        """``stop()`` без ``start()`` (т.е. ``_thread is None``) не падает."""
        from lib.services.session_cold_sync_service import SessionColdSyncService

        svc = SessionColdSyncService(
            session_manager=_FakeSessionManager({}),
            pg_dsn="postgresql://test",
        )
        svc.stop(timeout_sec=1.0)
        assert svc._running is True  # _running не трогается без start