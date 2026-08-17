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
_workspace = str(Path(__file__).resolve().parent.parent / "workspace")
if _workspace not in sys.path:
    sys.path.insert(0, _workspace)

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

    def fetchone(self):
        return self._rows[0] if self._rows else None

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


@pytest.fixture
def mock_pool(monkeypatch):
    """Подменяем utils.db.run/configure/get_stats, чтобы весь SQL шёл на ScriptedConn."""
    fake = {"conn": None, "configured": [], "runs": 0, "connected": False}

    monkeypatch.setattr("utils.db.configure",
                        lambda dsn: fake["configured"].append(dsn) or None)
    monkeypatch.setattr(
        "utils.db.run",
        lambda fn: fn(fake["conn"]),
    )
    monkeypatch.setattr(
        "utils.db.get_stats",
        lambda: {"connected": fake["connected"]},
    )
    return fake


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

    def test_start_stop_with_invalid_dsn_no_hang(self, mock_pool):
        mock_pool["connected"] = False
        s = AuditSyncService(
            dsn="postgresql://bad:bad@127.0.0.1:1/none",
            tables=["audits"],
            poll_interval_sec=0.2,
            reconnect_backoff=0.05,
        )
        s._conn = None
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
    def test_initial_load_dispatches_all_tables(self, mock_pool):
        mock_pool["connected"] = True
        mock_pool["conn"] = ScriptedConn(_standard_rows_for)
        received = []
        s = AuditSyncService(
            dsn="postgresql://u@h/db",
            tables=["audits", "violations"],
            poll_interval_sec=0.05,
            reconnect_backoff=0.01,
        )
        s.set_on_new_records_callback(lambda table, records: received.append((table, [dict(r) for r in records])))
        s.start(initial_load=True)
        time.sleep(0.6)
        s.stop(timeout_sec=2.0)

        tables_received = {t for t, _ in received}
        assert {"audits", "violations"} <= tables_received
        st = s.get_stats()
        assert st["polls"] >= 1
        # после инкрементального поллинга last_sync продвинулся на MAX(track)
        assert s._last_sync["audits"] == _T2

    def test_incremental_poll_tracks_new_rows(self, mock_pool):
        mock_pool["connected"] = True
        mock_pool["conn"] = ScriptedConn(_standard_rows_for)
        received = []
        s = AuditSyncService(
            dsn="postgresql://u@h/db",
            tables=["audits"],
            poll_interval_sec=0.05,
            reconnect_backoff=0.01,
        )
        s.set_on_new_records_callback(lambda table, records: received.append((table, [dict(r) for r in records])))
        s.start(initial_load=True)
        time.sleep(0.6)
        s.stop(timeout_sec=2.0)

        audit_rows = [r for t, rows in received if t == "audits" for r in rows]
        assert [r["id"] for r in audit_rows] == [1, 2]
        # после инкрементального поллинга last_sync продвинулся на MAX(track)
        assert s._last_sync["audits"] == _T2

    def test_incremental_query_uses_track_column_and_last(self, mock_pool):
        conn = ScriptedConn(_standard_rows_for)
        mock_pool["conn"] = conn
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
    def test_write_answer_inserts_into_table(self, mock_pool):
        conn = ScriptedConn()
        mock_pool["conn"] = conn
        s = AuditSyncService(dsn="postgresql://u@h/db", write_table="audit_interactions")
        s._conn = conn
        s._write_answer(
            {"session_id": "s1", "query_text": "вопрос", "answer_text": "ответ", "metadata": {"k": 1}}
        )
        sql = conn.executed[-1][0]
        assert 'INSERT INTO "oarb"."audit_interactions"' in sql
        assert s.get_stats()["writes_written"] == 1

    def test_submitted_write_processed_by_worker(self, mock_pool):
        def rows_for(sql, params):
            low = sql.lower()
            if "information_schema.tables" in low:
                # write-таблица существует (сервис DDL не выполняет)
                return [("1",)]
            return _standard_rows_for(sql, params)

        mock_pool["connected"] = True
        mock_pool["conn"] = ScriptedConn(rows_for)
        s = AuditSyncService(
            dsn="postgresql://u@h/db",
            tables=["audits"],
            write_table="audit_interactions",
            poll_interval_sec=0.05,
            reconnect_backoff=0.01,
        )
        s.start(initial_load=False)
        assert s.submit_write("s1", "вопрос", "ответ") is True
        time.sleep(0.4)
        s.stop(timeout_sec=2.0)

        st = s.get_stats()
        assert st["writes_queued"] == 1
        assert st["writes_written"] == 1
        assert any('INSERT INTO "oarb"."audit_interactions"' in sql for sql, _ in mock_pool["conn"].executed)


# ---------------------------------------------------------------------------
# Переподключение
# ---------------------------------------------------------------------------


class TestSyncCallback:
    def test_on_sync_callback_invoked_after_load_and_polls(self, mock_pool):
        mock_pool["connected"] = True
        mock_pool["conn"] = ScriptedConn(_standard_rows_for)
        calls = []
        s = AuditSyncService(
            dsn="postgresql://u@h/db",
            tables=["audits"],
            poll_interval_sec=0.05,
            reconnect_backoff=0.01,
        )
        s.set_on_sync_callback(lambda: calls.append(1))
        s.start(initial_load=True)
        time.sleep(0.4)
        s.stop(timeout_sec=2.0)
        # минимум: 1 после initial load + >=1 после поллинга
        assert len(calls) >= 2

    def test_callback_exception_does_not_stop_worker(self, mock_pool):
        mock_pool["connected"] = True
        mock_pool["conn"] = ScriptedConn(_standard_rows_for)

        def boom():
            raise RuntimeError("test")

        s = AuditSyncService(
            dsn="postgresql://u@h/db",
            tables=["audits"],
            poll_interval_sec=0.05,
            reconnect_backoff=0.01,
        )
        s.set_on_sync_callback(boom)
        s.start(initial_load=True)
        time.sleep(0.2)
        s.stop(timeout_sec=2.0)
        assert s.get_stats()["errors"] >= 1


# ---------------------------------------------------------------------------
# Структура из PG (schema callback) и полная пересинхронизация (удаления)
# ---------------------------------------------------------------------------

_SCHEMA_COLUMNS = {
    "audits": [
        {"column_name": "id", "data_type": "integer", "is_nullable": "NO",
         "character_maximum_length": None, "numeric_precision": None,
         "numeric_scale": None, "column_comment": "Идентификатор"},
        {"column_name": "title", "data_type": "character varying", "is_nullable": "YES",
         "character_maximum_length": 500, "numeric_precision": None,
         "numeric_scale": None, "column_comment": "Название проверки"},
        {"column_name": "amount", "data_type": "numeric", "is_nullable": "YES",
         "character_maximum_length": None, "numeric_precision": 10,
         "numeric_scale": 2, "column_comment": None},
    ],
}
_TABLE_COMMENTS = {"audits": "Аудиторские проверки"}


def _schema_and_rows_for(sql, params):
    low = sql.lower()
    if "information_schema.columns" in low:
        tbl = params[1] if params and len(params) > 1 else _table_from_sql(sql)
        return _SCHEMA_COLUMNS.get(tbl, [])
    if "obj_description" in low:
        tbl = params[1] if params and len(params) > 1 else ""
        comment = _TABLE_COMMENTS.get(tbl)
        return [(comment,)] if comment else []
    return _standard_rows_for(sql, params)


class TestSchemaAndResync:
    def test_schema_callback_receives_pg_columns(self, mock_pool):
        received = []
        mock_pool["connected"] = True
        mock_pool["conn"] = ScriptedConn(_schema_and_rows_for)
        s = AuditSyncService(
            dsn="postgresql://u@h/db", tables=["audits"],
            poll_interval_sec=0.05, reconnect_backoff=0.01, full_resync_every=0,
        )
        s.set_on_schema_callback(lambda table, columns: received.append((table, columns)))
        s.start(initial_load=True)
        time.sleep(0.25)
        s.stop(timeout_sec=2.0)
        assert received
        table, columns = received[0]
        assert table == "audits"
        by_name = {c["name"]: c for c in columns}
        assert by_name["id"]["type"] == "integer"
        assert by_name["id"]["comment"] == "Идентификатор"
        assert by_name["title"]["type"] == "character varying(500)"
        assert by_name["amount"]["type"] == "numeric(10,2)"
        # комментарий таблицы приходит псевдоколонкой __table__
        assert by_name["__table__"]["comment"] == "Аудиторские проверки"

    def test_schema_callback_not_called_without_schema_rows(self, mock_pool):
        received = []
        mock_pool["connected"] = True
        mock_pool["conn"] = ScriptedConn(_standard_rows_for)
        s = AuditSyncService(
            dsn="postgresql://u@h/db", tables=["audits"],
            poll_interval_sec=0.05, reconnect_backoff=0.01, full_resync_every=0,
        )
        s.set_on_schema_callback(lambda table, columns: received.append((table, columns)))
        s.start(initial_load=True)
        time.sleep(0.2)
        s.stop(timeout_sec=2.0)
        assert received == []

    def test_periodic_full_resync_invokes_replace(self, mock_pool):
        replaced = []
        mock_pool["connected"] = True
        mock_pool["conn"] = ScriptedConn(_schema_and_rows_for)
        s = AuditSyncService(
            dsn="postgresql://u@h/db", tables=["audits"],
            poll_interval_sec=0.05, reconnect_backoff=0.01, full_resync_every=1,
        )
        s.set_on_replace_records_callback(
            lambda table, rows: replaced.append((table, [dict(r) for r in rows]))
        )
        s.start(initial_load=True)
        time.sleep(0.4)
        s.stop(timeout_sec=2.0)
        assert replaced
        assert any(table == "audits" for table, _ in replaced)
        assert s.get_stats()["full_resyncs"] >= 1

    def test_full_resync_disabled_by_zero(self, mock_pool):
        replaced = []
        mock_pool["connected"] = True
        mock_pool["conn"] = ScriptedConn(_schema_and_rows_for)
        s = AuditSyncService(
            dsn="postgresql://u@h/db", tables=["audits"],
            poll_interval_sec=0.05, reconnect_backoff=0.01, full_resync_every=0,
        )
        s.set_on_replace_records_callback(
            lambda table, rows: replaced.append((table, [dict(r) for r in rows]))
        )
        s.start(initial_load=True)
        time.sleep(0.3)
        s.stop(timeout_sec=2.0)
        assert replaced == []
        assert s.get_stats()["full_resyncs"] == 0


class TestReconnect:
    def test_reconnect_clears_last_sync(self, mock_pool):
        mock_pool["conn"] = ScriptedConn()
        s = AuditSyncService(dsn="postgresql://u@h/db", reconnect_backoff=0.01)
        s._last_sync["audits"] = _T1
        s._running = True
        s._reconnect()
        assert s._last_sync == {}
        assert s._conn is None

    def test_ensure_connected_configures_pool_dsn(self, mock_pool):
        s = AuditSyncService(
            dsn="postgresql://u@h/db", reconnect_backoff=0.01
        )
        s._running = True
        s._ensure_connected()
        assert mock_pool["configured"] == ["postgresql://u@h/db"]
        assert s.get_stats()["reconnects"] >= 0
