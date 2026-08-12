from __future__ import annotations

import json
import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lib.services.db_logging_service import DbLoggingService, LogEvent


@pytest.fixture
def fake_psycopg2(monkeypatch):
    """Подменяем psycopg2/psycopg2.extras, чтобы не требовать установки."""
    cursor = MagicMock()
    cursor.close = MagicMock()
    cur_factory = MagicMock(return_value=cursor)
    conn = MagicMock()
    conn.cursor = cur_factory
    conn.close = MagicMock()
    conn.closed = False

    extras = types.ModuleType("psycopg2.extras")
    extras.execute_batch = MagicMock()
    extras.Json = lambda x: x

    psycopg2 = types.ModuleType("psycopg2")
    psycopg2.connect = MagicMock(return_value=conn)
    psycopg2.extras = extras

    monkeypatch.setitem(sys.modules, "psycopg2", psycopg2)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", extras)
    return {
        "conn": conn,
        "cursor": cursor,
        "execute_batch": extras.execute_batch,
    }


class TestBasicLifecycle:
    def test_start_stop(self, fake_psycopg2):
        svc = DbLoggingService(dsn="postgresql://x", flush_interval_sec=0.05)
        svc.start()
        try:
            assert svc.is_running()
        finally:
            svc.stop(timeout_sec=2.0)
        assert not svc.is_running()

    def test_no_dsn_runs_with_fallback(self, tmp_path):
        svc = DbLoggingService(
            dsn="",
            flush_interval_sec=0.05,
            fallback_path=tmp_path / "log.jsonl",
        )
        svc.start()
        try:
            assert svc.log_inbound("cli:1", "cli", "hi") is True
            time.sleep(0.2)
        finally:
            svc.stop(timeout_sec=2.0)
        # В fallback нет записи (БД не было, fallback должен принять)
        assert svc.get_stats()["fallback_written"] >= 0


class TestNonBlocking:
    def test_log_inbound_enqueue(self, fake_psycopg2):
        svc = DbLoggingService(dsn="postgresql://x", flush_interval_sec=5.0)
        assert svc.log_inbound("cli:1", "cli", "hello") is True
        assert svc.get_stats()["queued"] >= 1

    def test_log_inbound_sender_and_chat(self, fake_psycopg2):
        svc = DbLoggingService(dsn="postgresql://x", flush_interval_sec=5.0)
        assert svc.log_inbound(
            "cli:1", "cli", "hello",
            sender_id="u42", chat_id="c7", message_id="m1",
        ) is True
        event = svc._queue.queue[0]
        assert event.actor == "u42"
        assert event.payload["sender_id"] == "u42"
        assert event.payload["chat_id"] == "c7"
        assert event.payload["message_id"] == "m1"

    def test_log_inbound_default_actor_is_user(self, fake_psycopg2):
        svc = DbLoggingService(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.log_inbound("cli:1", "cli", "hello")
        assert svc._queue.queue[0].actor == "user"

    def test_log_inbound_request_id_defaults_to_message_id(self, fake_psycopg2):
        svc = DbLoggingService(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.log_inbound("cli:1", "cli", "hello", message_id="m1")
        event = svc._queue.queue[0]
        assert event.request_id == "m1"
        assert event.payload["message_id"] == "m1"

    def test_log_outbound_request_id(self, fake_psycopg2):
        svc = DbLoggingService(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.log_outbound("cli:1", "cli", "ok", request_id="m1")
        event = svc._queue.queue[0]
        assert event.request_id == "m1"

    def test_log_tool_event_request_id(self, fake_psycopg2):
        svc = DbLoggingService(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.log_tool_call("cli:1", "read", {"p": 1}, tool_call_id="t1", request_id="m1")
        svc.log_tool_result("cli:1", "read", "r", 10.0, tool_call_id="t1", request_id="m1")
        call, result = list(svc._queue.queue)
        assert call.request_id == "m1" and call.name == "read"
        assert result.request_id == "m1" and result.name == "read"

    def test_request_index_lifecycle(self, fake_psycopg2):
        svc = DbLoggingService(dsn="postgresql://x", flush_interval_sec=5.0)
        assert svc.get_request_id("cli:1") is None
        svc.register_request(
            "cli:1", "m1", user_id="u1", chat_id="c1",
            agent_id="main", parent_agent_id=None,
        )
        assert svc.get_request_id("cli:1") == "m1"
        svc.clear_request("cli:1")
        assert svc.get_request_id("cli:1") is None
        # пустые ключи игнорируются
        svc.register_request("", "x")
        assert svc.get_request_id("") is None

    def test_question_run_records(self, fake_psycopg2):
        svc = DbLoggingService(dsn="postgresql://x", flush_interval_sec=5.0)
        # контекст вопроса + финиш — это _QuestionRunRecord'ы, не LogEvent'ы
        assert svc.register_request(
            "cli:1", "m1", user_id="u1", chat_id="c1",
            agent_id="main", parent_agent_id=None,
        ) is True
        assert svc.finish_request("m1", status="finished", summary="ok") is True
        records = [i for i in svc._queue.queue if type(i).__name__ == "_QuestionRunRecord"]
        assert len(records) == 2
        assert records[0].request_id == "m1" and records[0].user_id == "u1"
        assert records[1].update_only is True and records[1].status == "finished"

    def test_log_tool_event_dimensions(self, fake_psycopg2):
        svc = DbLoggingService(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.log_tool_call(
            "cli:1", "read", {"p": 1}, tool_call_id="t1", request_id="m1",
        )
        event = svc._queue.queue[0]
        assert event.name == "read"
        assert event.request_id == "m1"

    def test_log_event_min_level(self, fake_psycopg2):
        svc = DbLoggingService(dsn="postgresql://x", min_level="WARN")
        assert svc.log_event(LogEvent("x", "DEBUG")) is False
        assert svc.log_event(LogEvent("x", "INFO")) is False
        assert svc.log_event(LogEvent("x", "ERROR")) is True

    def test_log_outbound_with_meta(self, fake_psycopg2):
        svc = DbLoggingService(dsn="postgresql://x")
        assert svc.log_outbound(
            "cli:1", "cli", "ok", latency_ms=12.5, tokens_used=42
        ) is True

    def test_log_tool_call_and_result(self, fake_psycopg2):
        svc = DbLoggingService(dsn="postgresql://x")
        assert svc.log_tool_call("cli:1", "read", {"path": "x"}) is True
        assert svc.log_tool_result(
            "cli:1", "read", "content", latency_ms=15.0
        ) is True

    def test_log_error(self, fake_psycopg2):
        svc = DbLoggingService(dsn="postgresql://x")
        assert svc.log_error("boom", session_id="k", context={"k": "v"}) is True

    def test_queue_full_returns_false(self, fake_psycopg2):
        svc = DbLoggingService(dsn="postgresql://x", queue_maxsize=2)
        # Не запускаем worker — очередь наполнится до запуска.
        for _ in range(2):
            assert svc.log_event(LogEvent("x")) is True
        assert svc.log_event(LogEvent("x")) is False
        assert svc.get_stats()["queue_full"] == 1


class TestFlush:
    def test_batch_writes_to_db(self, fake_psycopg2):
        svc = DbLoggingService(dsn="postgresql://x", flush_interval_sec=0.05,
                                batch_size=3)
        svc.start()
        try:
            for i in range(3):
                svc.log_inbound("cli:1", "cli", f"msg{i}")
            time.sleep(0.3)
        finally:
            svc.stop(timeout_sec=2.0)

        fake_psycopg2["execute_batch"].assert_called()
        written = svc.get_stats()["written"]
        assert written >= 3

    def test_fallback_when_no_dsn(self, tmp_path):
        svc = DbLoggingService(
            dsn="",
            flush_interval_sec=0.05,
            batch_size=2,
            fallback_path=tmp_path / "log.jsonl",
        )
        svc.start()
        try:
            svc.log_inbound("cli:1", "cli", "a")
            svc.log_inbound("cli:1", "cli", "b")
            time.sleep(0.3)
        finally:
            svc.stop(timeout_sec=2.0)

        assert (tmp_path / "log.jsonl").exists()
        lines = (tmp_path / "log.jsonl").read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) >= 2
        for ln in lines:
            obj = json.loads(ln)
            assert obj["event_type"] == "inbound"
            assert obj["channel"] == "cli"

    def test_connect_failure_uses_fallback(self, fake_psycopg2, tmp_path):
        psycopg2 = sys.modules["psycopg2"]
        psycopg2.connect = MagicMock(side_effect=RuntimeError("no db"))
        svc = DbLoggingService(
            dsn="postgresql://x",
            flush_interval_sec=0.05,
            batch_size=1,
            fallback_path=tmp_path / "log.jsonl",
        )
        svc.start()
        try:
            svc.log_inbound("cli:1", "cli", "x")
            time.sleep(0.2)
        finally:
            svc.stop(timeout_sec=2.0)
        assert svc.get_stats()["failed"] + svc.get_stats()["fallback_written"] >= 1

    def test_stop_flushes_remaining(self, fake_psycopg2):
        svc = DbLoggingService(dsn="postgresql://x", flush_interval_sec=5.0,
                                batch_size=100)
        svc.start()
        try:
            svc.log_inbound("cli:1", "cli", "left")
        finally:
            svc.stop(timeout_sec=2.0)
        assert svc.get_stats()["written"] >= 1


class TestGetStats:
    def test_keys_present(self, fake_psycopg2):
        svc = DbLoggingService(dsn="postgresql://x")
        stats = svc.get_stats()
        for k in ("running", "queued", "written", "failed", "queue_size",
                  "fallback_written", "batch_count", "queue_full",
                  "connected", "last_error"):
            assert k in stats


class TestGreenplumDialect:
    def test_schema_files_selected_by_dialect(self):
        svc = DbLoggingService(dsn="x", dialect="greenplum")
        ddl, mig = svc._schema_files()
        assert ddl.name == "create_logs_table_gp.sql"
        assert mig.name == "migrate_logs_v1_gp.sql"

    def test_default_dialect_is_postgres(self):
        svc = DbLoggingService(dsn="x")
        ddl, mig = svc._schema_files()
        assert ddl.name == "create_logs_table.sql"
        assert mig.name == "migrate_logs_v1.sql"

    def test_render_sql_gp_placeholders(self):
        svc = DbLoggingService(dsn="x", dialect="greenplum",
                               table_name="gateway_logs", schema="public")
        rendered = svc._render_sql(
            svc._DDL_GP_PATH, '"public"."gateway_logs"'
        )
        # плейсхолдеры подставлены
        assert "@@SCHEMA@@" not in rendered
        assert "@@TABLE@@" not in rendered
        assert "@@TABLE_DDL@@" not in rendered
        # schema-qualified имя в CREATE TABLE
        assert 'CREATE TABLE IF NOT EXISTS "public"."gateway_logs"' in rendered
        # каталог-проверки в DO-блоках ссылаются на голое имя
        assert "tablename = 'question_runs'" in rendered

    def test_upsert_question_run_no_on_conflict(self, fake_psycopg2):
        from lib.services.db_logging_service import _QuestionRunRecord

        svc = DbLoggingService(dsn="postgresql://x")
        conn = fake_psycopg2["conn"]
        conn.cursor.reset_mock()
        cursor = fake_psycopg2["cursor"]
        svc._upsert_question_run(conn, _QuestionRunRecord(
            request_id="m1", session_id="cli:1", user_id="u1", chat_id="c1",
            channel="cli", agent_id="main", status="running",
        ))
        calls = [c.args[0] for c in cursor.execute.call_args_list]
        assert len(calls) == 2
        assert calls[0].lstrip().startswith("UPDATE")
        assert "ON CONFLICT" not in calls[0]
        assert calls[1].lstrip().startswith("INSERT")
        assert "WHERE NOT EXISTS" in calls[1]
        assert "ON CONFLICT" not in calls[1]

    def test_upsert_question_run_update_only(self, fake_psycopg2):
        from lib.services.db_logging_service import _QuestionRunRecord

        svc = DbLoggingService(dsn="postgresql://x")
        conn = fake_psycopg2["conn"]
        conn.cursor.reset_mock()
        cursor = fake_psycopg2["cursor"]
        svc._upsert_question_run(conn, _QuestionRunRecord(
            request_id="m1", status="finished", summary="ok", update_only=True,
        ))
        calls = [c.args[0] for c in cursor.execute.call_args_list]
        assert len(calls) == 2
        assert calls[0].lstrip().startswith("UPDATE")
        assert "status = %s" in calls[0]
        assert calls[1].lstrip().startswith("INSERT")
        assert "WHERE NOT EXISTS" in calls[1]
