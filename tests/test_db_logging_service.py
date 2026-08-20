from __future__ import annotations

import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lib.services.db_logging_service import DbLoggingService, LogEvent


def _svc(**kw):
    kwargs = dict(
        dsn="postgresql://x",
        table_name="agent_gateway_logs",
        question_runs_table="agent_question_runs",
    )
    kwargs.update(kw)
    return DbLoggingService(**kwargs)


@pytest.fixture
def fake_psycopg2(monkeypatch):
    """Подменяем connect/session в реальном psycopg2, чтобы не поднимать БД."""
    real = __import__("psycopg2")
    __import__("psycopg2.extras")
    __import__("psycopg2.extensions")
    real_extras = sys.modules["psycopg2.extras"]
    real_extensions = sys.modules["psycopg2.extensions"]

    cursor = MagicMock()
    cursor.close = MagicMock()
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cursor)
    conn.close = MagicMock()
    conn.closed = False

    execute_batch = MagicMock()

    monkeypatch.setattr(real, "connect", MagicMock(return_value=conn), raising=False)
    monkeypatch.setattr(real_extras, "Json", lambda x: x, raising=False)
    monkeypatch.setattr(real_extras, "execute_batch", execute_batch, raising=False)
    monkeypatch.setattr(real_extras, "register_json", MagicMock(), raising=False)
    monkeypatch.setattr(real_extensions, "register_adapter", MagicMock(), raising=False)

    ws = str(Path(__file__).resolve().parent.parent / "workspace")
    if ws not in sys.path:
        sys.path.insert(0, ws)
    import utils.db as _db

    yield {
        "conn": conn,
        "cursor": cursor,
        "execute_batch": execute_batch,
    }

    # Каждый тест получает свежее соединение/pool: воркер закрывается, конфиг
    # и менеджер сбрасываются, чтобы не переиспользовать mock-conn и настройки
    # из прошлого теста.
    _db.shutdown()
    _db._manager = None
    _db._pool_cfg = dict(_db._DEFAULT_POOL)


class TestBasicLifecycle:
    def test_start_stop(self, fake_psycopg2):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=0.05)
        svc.start()
        try:
            assert svc.is_running()
        finally:
            svc.stop(timeout_sec=2.0)
        assert not svc.is_running()

    def test_no_dsn_drops_events(self, tmp_path):
        svc = _svc(dsn="", flush_interval_sec=0.05)
        svc.start()
        try:
            assert svc.log_inbound("cli:1", "cli", "hi") is True
            time.sleep(0.2)
        finally:
            svc.stop(timeout_sec=2.0)
        # БД нет — события выбрасываются, JSONL-файл не создаётся
        assert not (tmp_path / "log.jsonl").exists()
        assert svc.get_stats()["failed"] >= 1


class TestNonBlocking:
    def test_log_inbound_enqueue(self, fake_psycopg2):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        assert svc.log_inbound("cli:1", "cli", "hello") is True
        assert svc.get_stats()["queued"] >= 1

    def test_log_inbound_sender_and_chat(self, fake_psycopg2):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
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
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.log_inbound("cli:1", "cli", "hello")
        assert svc._queue.queue[0].actor == "user"

    def test_log_inbound_request_id_defaults_to_message_id(self, fake_psycopg2):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.log_inbound("cli:1", "cli", "hello", message_id="m1")
        event = svc._queue.queue[0]
        assert event.request_id == "m1"
        assert event.payload["message_id"] == "m1"

    def test_log_outbound_request_id(self, fake_psycopg2):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.log_outbound("cli:1", "cli", "ok", request_id="m1")
        event = svc._queue.queue[0]
        assert event.request_id == "m1"

    def test_log_tool_event_request_id(self, fake_psycopg2):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.log_tool_call("cli:1", "read", {"p": 1}, tool_call_id="t1", request_id="m1")
        svc.log_tool_result("cli:1", "read", "r", 10.0, tool_call_id="t1", request_id="m1")
        call, result = list(svc._queue.queue)
        assert call.request_id == "m1" and call.name == "read"
        assert result.request_id == "m1" and result.name == "read"

    def test_request_index_lifecycle(self, fake_psycopg2):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
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
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        # контекст вопроса + финиш — это _QuestionRunRecord'ы, не LogEvent'ы
        assert svc.register_request(
            "cli:1", "m1", user_id="u1", chat_id="c1",
            agent_id="main", parent_agent_id=None,
            question="привет", media=["file1.png"],
        ) is True
        assert svc.finish_request("m1", status="finished", summary="ok",
                                  response="полный ответ") is True
        records = [i for i in svc._queue.queue if type(i).__name__ == "_QuestionRunRecord"]
        assert len(records) == 2
        assert records[0].request_id == "m1" and records[0].user_id == "u1"
        assert records[0].question == "привет"
        assert records[0].media == ["file1.png"]
        assert records[1].update_only is True and records[1].status == "finished"
        assert records[1].response == "полный ответ"

    def test_log_tool_event_dimensions(self, fake_psycopg2):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.log_tool_call(
            "cli:1", "read", {"p": 1}, tool_call_id="t1", request_id="m1",
        )
        event = svc._queue.queue[0]
        assert event.name == "read"
        assert event.request_id == "m1"

    def test_log_event_min_level(self, fake_psycopg2):
        svc = _svc(dsn="postgresql://x", min_level="WARN")
        assert svc.log_event(LogEvent("x", "DEBUG")) is False
        assert svc.log_event(LogEvent("x", "INFO")) is False
        assert svc.log_event(LogEvent("x", "ERROR")) is True

    def test_log_outbound_with_meta(self, fake_psycopg2):
        svc = _svc(dsn="postgresql://x")
        assert svc.log_outbound(
            "cli:1", "cli", "ok", latency_ms=12.5, tokens_used=42
        ) is True

    def test_log_media_in_payload(self, fake_psycopg2):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.log_inbound("cli:1", "cli", "привет", media=["doc.pdf"])
        svc.log_outbound("cli:1", "cli", "ответ", media=["report.xlsx"])
        inbound, outbound = list(svc._queue.queue)
        assert inbound.payload["media"] == ["doc.pdf"]
        assert outbound.payload["media"] == ["report.xlsx"]

    def test_log_tool_call_and_result(self, fake_psycopg2):
        svc = _svc(dsn="postgresql://x")
        assert svc.log_tool_call("cli:1", "read", {"path": "x"}) is True
        assert svc.log_tool_result(
            "cli:1", "read", "content", latency_ms=15.0
        ) is True

    def test_log_error(self, fake_psycopg2):
        svc = _svc(dsn="postgresql://x")
        assert svc.log_error("boom", session_id="k", context={"k": "v"}) is True

    def test_log_llm_call_fields(self, fake_psycopg2):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        prompt = [{"role": "user", "content": "привет"}]
        response = {"content": "ответ", "tool_calls": [], "finish_reason": "stop"}
        svc.log_llm_call(
            "cli:1", prompt, response,
            iteration=2, model="mini", finish_reason="stop",
            usage={"total_tokens": 10}, request_id="m1",
        )
        event = svc._queue.queue[0]
        assert event.event_type == "llm_call"
        assert event.actor == "agent"
        assert event.request_id == "m1"
        assert event.summary == "stop"
        assert event.payload["prompt"] == prompt
        assert event.payload["response"] == response
        assert event.metadata["iteration"] == 2
        assert event.metadata["model"] == "mini"
        assert event.metadata["finish_reason"] == "stop"
        assert event.metadata["usage"] == {"total_tokens": 10}

    def test_log_llm_call_sanitizes_non_json(self, fake_psycopg2):
        from pathlib import Path

        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        prompt = [{
            "role": "tool",
            "content": Path("x.txt"),  # несеризуемый объект
        }]
        response = {"content": "ок", "finish_reason": "stop"}
        svc.log_llm_call("cli:1", prompt, response)
        event = svc._queue.queue[0]
        assert event.payload["prompt"] == [{"role": "tool", "content": "x.txt"}]
        assert event.payload["response"] == {"content": "ок", "finish_reason": "stop"}

    def test_queue_full_returns_false(self, fake_psycopg2):
        svc = _svc(dsn="postgresql://x", queue_maxsize=2)
        # Не запускаем worker — очередь наполнится до запуска.
        for _ in range(2):
            assert svc.log_event(LogEvent("x")) is True
        assert svc.log_event(LogEvent("x")) is False
        assert svc.get_stats()["queue_full"] == 1


class TestFlush:
    def test_batch_writes_to_db(self, fake_psycopg2):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=0.05,
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

    def test_drop_when_no_dsn(self, tmp_path):
        svc = _svc(
            dsn="",
            flush_interval_sec=0.05,
            batch_size=2,
        )
        svc.start()
        try:
            svc.log_inbound("cli:1", "cli", "a")
            svc.log_inbound("cli:1", "cli", "b")
            time.sleep(0.3)
        finally:
            svc.stop(timeout_sec=2.0)

        # Файл не создаётся, события помечаются как потерянные
        assert not (tmp_path / "log.jsonl").exists()
        assert svc.get_stats()["failed"] >= 2

    def test_connect_failure_drops(self, fake_psycopg2, tmp_path):
        from utils.db import set_pool_config

        set_pool_config({"connect_max_retries": 1, "reconnect_backoff_sec": 0.05})
        psycopg2 = sys.modules["psycopg2"]
        psycopg2.connect = MagicMock(side_effect=RuntimeError("no db"))
        svc = _svc(
            dsn="postgresql://x",
            flush_interval_sec=0.05,
            batch_size=1,
        )
        svc.start()
        try:
            svc.log_inbound("cli:1", "cli", "x")
            time.sleep(0.2)
        finally:
            svc.stop(timeout_sec=2.0)
        assert svc.get_stats()["failed"] >= 1
        assert not (tmp_path / "log.jsonl").exists()

    def test_stop_flushes_remaining(self, fake_psycopg2):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0,
                                batch_size=100)
        svc.start()
        try:
            svc.log_inbound("cli:1", "cli", "left")
        finally:
            svc.stop(timeout_sec=2.0)
        assert svc.get_stats()["written"] >= 1


class TestGetStats:
    def test_keys_present(self, fake_psycopg2):
        svc = _svc(dsn="postgresql://x")
        stats = svc.get_stats()
        for k in ("running", "queued", "written", "failed", "queue_size",
                  "batch_count", "queue_full",
                  "connected", "last_error"):
            assert k in stats


class TestSchemaCheck:
    def test_ensure_schema_raises_when_missing_tables(self, fake_psycopg2):
        svc = _svc(dsn="postgresql://x")
        conn = fake_psycopg2["conn"]
        cursor = fake_psycopg2["cursor"]
        cursor.fetchone.return_value = None  # таблиц нет ни в одном information_schema запросе
        conn.cursor.reset_mock()
        with pytest.raises(RuntimeError, match="таблица не найдена"):
            svc._ensure_schema(conn)

    def test_ensure_schema_passes_when_tables_exist(self, fake_psycopg2):
        svc = _svc(dsn="postgresql://x")
        conn = fake_psycopg2["conn"]
        cursor = fake_psycopg2["cursor"]
        cursor.fetchone.return_value = ("1",)
        conn.cursor.reset_mock()
        svc._ensure_schema(conn)  # не должно падать
        # проверяем обе таблицы
        assert cursor.execute.call_count == 2

    def test_no_ddl_executed(self, fake_psycopg2):
        svc = _svc(dsn="postgresql://x")
        conn = fake_psycopg2["conn"]
        cursor = fake_psycopg2["cursor"]
        cursor.fetchone.return_value = ("1",)
        conn.cursor.reset_mock()
        svc._ensure_schema(conn)
        for call in cursor.execute.call_args_list:
            assert "CREATE" not in call.args[0].upper()

    def test_upsert_question_run_no_on_conflict(self, fake_psycopg2):
        from lib.services.db_logging_service import _QuestionRunRecord

        svc = _svc(dsn="postgresql://x")
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

        svc = _svc(dsn="postgresql://x")
        conn = fake_psycopg2["conn"]
        conn.cursor.reset_mock()
        cursor = fake_psycopg2["cursor"]
        svc._upsert_question_run(conn, _QuestionRunRecord(
            request_id="m1", status="finished", summary="ok",
            response="полный ответ", update_only=True,
        ))
        calls = [c.args[0] for c in cursor.execute.call_args_list]
        assert len(calls) == 2
        assert calls[0].lstrip().startswith("UPDATE")
        assert "status = %s" in calls[0]
        assert "response = COALESCE(%s, response)" in calls[0]
        assert "media = COALESCE(%s, media)" in calls[0]
        assert calls[1].lstrip().startswith("INSERT")
        assert "WHERE NOT EXISTS" in calls[1]

    def test_upsert_question_run_question_media(self, fake_psycopg2):
        from lib.services.db_logging_service import _QuestionRunRecord

        svc = _svc(dsn="postgresql://x")
        conn = fake_psycopg2["conn"]
        conn.cursor.reset_mock()
        cursor = fake_psycopg2["cursor"]
        svc._upsert_question_run(conn, _QuestionRunRecord(
            request_id="m1", session_id="cli:1", user_id="u1",
            status="running", question="вопрос", media=["f.pdf"],
        ))
        calls = [c.args[0] for c in cursor.execute.call_args_list]
        assert len(calls) == 2
        assert "question = %s" in calls[0]
        assert "media = %s" in calls[0]
        assert "media" in calls[1]
