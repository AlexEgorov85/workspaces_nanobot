from __future__ import annotations

import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from lib.services.audit_sync_service import AuditSyncService

_T1 = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
_T2 = datetime(2026, 1, 2, 0, 0, tzinfo=timezone.utc)

INITIAL_ROWS = {
    "audits": [{"id": 1, "updated_at": _T1}],
    "violations": [{"id": 1, "updated_at": _T1}],
}
INCREMENTAL_ROWS = {
    "audits": [{"id": 2, "updated_at": _T2}],
}


class ScriptedCursor:
    def __init__(self, conn, cursor_factory=None):
        self._conn = conn
        self.cursor_factory = cursor_factory
        self.sql = None
        self.params = None
        self._rows = []
        self.closed = False

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params
        self._conn.executed.append((sql, params))
        self._rows = self._conn.rows_for(sql, params)

    def fetchall(self):
        return self._rows

    def close(self):
        self.closed = True


class ScriptedConn:
    def __init__(self, rows_for=None):
        self.autocommit = False
        self.closed = False
        self.executed: list[tuple[str, list]] = []
        self.rows_for = rows_for or (lambda sql, params: [])

    def cursor(self, cursor_factory=None):
        return ScriptedCursor(self, cursor_factory)

    def close(self):
        self.closed = True


def _table_from_sql(sql: str) -> str:
    m = re.search(r"FROM\s+\"(\w+)\"\.\"(\w+)\"", sql, re.IGNORECASE)
    if m:
        return m.group(2)
    return ""


def _standard_rows_for(sql, params):
    low = sql.lower()
    if low.lstrip().startswith(("insert", "create")):
        return []
    if "updated_at" in low:
        last = params[0] if params else None
        # инкрементальный поллинг: строки строго больше последней метки
        if last is not None and last >= _T2:
            return []
        return INCREMENTAL_ROWS.get(_table_from_sql(sql), [])
    return INITIAL_ROWS.get(_table_from_sql(sql), [])


# ---------------------------------------------------------------------------
# Очередь и жизненный цикл
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_submit_write_rejected_before_start(self):
        s = AuditSyncService(dsn="postgresql://u@h/db")
        assert s.submit_write("s1", "q", "a") is False

    def test_submit_write_accepted_while_running(self):
        s = AuditSyncService(dsn="postgresql://u@h/db")
        s._running = True
        assert s.submit_write("s1", "вопрос", "ответ") is True
        st = s.get_stats()
        assert st["writes_queued"] == 1
        assert st["queue_size"] == 1

    def test_start_stop_with_invalid_dsn_no_hang(self):
        s = AuditSyncService(
            dsn="postgresql://bad:bad@127.0.0.1:1/none",
            tables=["audits"],
            poll_interval_sec=0.2,
            reconnect_backoff=0.05,
        )
        s.start(initial_load=True)
        time.sleep(0.3)
        st = s.get_stats()
        assert st["running"] is True
        s.stop(timeout_sec=3.0)
        st = s.get_stats()
        assert st["running"] is False
        assert st["connected"] is False

    def test_track_column_selection(self):
        s = AuditSyncService(dsn="postgresql://u@h/db", vector_table="oarb.audit_vectors")
        assert s._track_column_for("audits") == "updated_at"
        assert s._track_column_for("oarb.audit_vectors") == "id"


# ---------------------------------------------------------------------------
# Worker: поллинг и диспатч
# ---------------------------------------------------------------------------


class TestWorker:
    def test_initial_load_dispatches_all_tables(self):
        conn = ScriptedConn(_standard_rows_for)
        received = []
        s = AuditSyncService(
            dsn="postgresql://u@h/db",
            tables=["audits", "violations"],
            poll_interval_sec=0.05,
            reconnect_backoff=0.01,
        )
        s.set_on_new_records_callback(lambda table, records: received.append((table, [dict(r) for r in records])))
        with patch("psycopg2.connect", return_value=conn):
            s.start(initial_load=True)
            time.sleep(0.6)
            s.stop(timeout_sec=2.0)

        tables_received = {t for t, _ in received}
        assert {"audits", "violations"} <= tables_received
        st = s.get_stats()
        assert st["polls"] >= 1
        # после инкрементального поллинга last_sync продвинулся на MAX(track)
        assert s._last_sync["audits"] == _T2

    def test_incremental_poll_tracks_new_rows(self):
        conn = ScriptedConn(_standard_rows_for)
        received = []
        s = AuditSyncService(
            dsn="postgresql://u@h/db",
            tables=["audits"],
            poll_interval_sec=0.05,
            reconnect_backoff=0.01,
        )
        s.set_on_new_records_callback(lambda table, records: received.append((table, [dict(r) for r in records])))
        with patch("psycopg2.connect", return_value=conn):
            s.start(initial_load=True)
            time.sleep(0.6)
            s.stop(timeout_sec=2.0)

        audit_rows = [r for t, rows in received if t == "audits" for r in rows]
        assert [r["id"] for r in audit_rows] == [1, 2]
        # после инкрементального поллинга last_sync продвинулся на MAX(track)
        assert s._last_sync["audits"] == _T2

    def test_incremental_query_uses_track_column_and_last(self):
        conn = ScriptedConn(_standard_rows_for)
        s = AuditSyncService(dsn="postgresql://u@h/db", tables=["audits"])
        s._conn = conn
        s._last_sync["audits"] = _T1
        s._poll_table("audits")
        sql, params = conn.executed[-1]
        assert '"updated_at" > %s' in sql
        assert params == [_T1]
        assert s._last_sync["audits"] == _T2


# ---------------------------------------------------------------------------
# Запись ответов
# ---------------------------------------------------------------------------


class TestWrite:
    def test_write_answer_inserts_into_table(self):
        conn = ScriptedConn()
        s = AuditSyncService(dsn="postgresql://u@h/db", write_table="audit_interactions")
        s._conn = conn
        s._write_answer(
            {"session_id": "s1", "query_text": "вопрос", "answer_text": "ответ", "metadata": {"k": 1}}
        )
        sql = conn.executed[-1][0]
        assert 'INSERT INTO "oarb"."audit_interactions"' in sql
        assert s.get_stats()["writes_written"] == 1

    def test_submitted_write_processed_by_worker(self):
        conn = ScriptedConn(_standard_rows_for)
        s = AuditSyncService(
            dsn="postgresql://u@h/db",
            tables=["audits"],
            poll_interval_sec=0.05,
            reconnect_backoff=0.01,
        )
        with patch("psycopg2.connect", return_value=conn):
            s.start(initial_load=False)
            assert s.submit_write("s1", "вопрос", "ответ") is True
            time.sleep(0.4)
            s.stop(timeout_sec=2.0)

        st = s.get_stats()
        assert st["writes_queued"] == 1
        assert st["writes_written"] == 1
        assert any('INSERT INTO "oarb"."audit_interactions"' in sql for sql, _ in conn.executed)


# ---------------------------------------------------------------------------
# Переподключение
# ---------------------------------------------------------------------------


class TestSyncCallback:
    def test_on_sync_callback_invoked_after_load_and_polls(self):
        conn = ScriptedConn(_standard_rows_for)
        calls = []
        s = AuditSyncService(
            dsn="postgresql://u@h/db",
            tables=["audits"],
            poll_interval_sec=0.05,
            reconnect_backoff=0.01,
        )
        s.set_on_sync_callback(lambda: calls.append(1))
        with patch("psycopg2.connect", return_value=conn):
            s.start(initial_load=True)
            time.sleep(0.4)
            s.stop(timeout_sec=2.0)
        # минимум: 1 после initial load + >=1 после поллинга
        assert len(calls) >= 2

    def test_callback_exception_does_not_stop_worker(self):
        conn = ScriptedConn(_standard_rows_for)

        def boom():
            raise RuntimeError("test")

        s = AuditSyncService(
            dsn="postgresql://u@h/db",
            tables=["audits"],
            poll_interval_sec=0.05,
            reconnect_backoff=0.01,
        )
        s.set_on_sync_callback(boom)
        with patch("psycopg2.connect", return_value=conn):
            s.start(initial_load=True)
            time.sleep(0.2)
            s.stop(timeout_sec=2.0)
        assert s.get_stats()["errors"] >= 1


class TestReconnect:
    def test_reconnect_clears_last_sync(self):
        conn = ScriptedConn()
        s = AuditSyncService(dsn="postgresql://u@h/db", reconnect_backoff=0.01)
        s._last_sync["audits"] = _T1
        s._running = True
        with patch("psycopg2.connect", return_value=conn):
            s._reconnect()
        assert s._last_sync == {}
        assert s._conn is conn

    def test_ensure_connected_sets_autocommit(self):
        conn = ScriptedConn()
        s = AuditSyncService(dsn="postgresql://u@h/db", reconnect_backoff=0.01)
        s._running = True
        with patch("psycopg2.connect", return_value=conn):
            s._ensure_connected()
        assert conn.autocommit is True
        assert s.get_stats()["reconnects"] >= 1
