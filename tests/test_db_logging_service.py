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

    def test_tool_result_error_summary_carrier(self, fake_psycopg2):
        svc = _svc(dsn="postgresql://x")
        svc.log_tool_result(
            "cli:1", "exec", None, latency_ms=10.0,
            status="error", error="Command timed out after 60 seconds",
            tool_call_id="t1",
        )
        event = svc._queue.queue[0]
        # summary несёт текст ошибки — видно сразу, без раскрытия payload
        assert event.summary == "Command timed out after 60 seconds"
        assert event.level == "ERROR"
        assert event.payload["error"] == "Command timed out after 60 seconds"
        assert event.payload["status"] == "error"
        # успешный результат — summary = имя инструмента (как раньше)
        svc.log_tool_result("cli:1", "exec", "ok", latency_ms=1.0, tool_call_id="t2")
        assert svc._queue.queue[1].summary == "exec"

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


class TestWrittenByType:
    def test_written_by_type_empty_on_start(self, fake_psycopg2):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        assert svc.get_stats()["written_by_type"] == {}

    def test_enqueue_does_not_increment_written_by_type(self, fake_psycopg2):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        before = time.time()
        svc.log_tool_call("cli:1", "read", {})
        svc.log_tool_result("cli:1", "read", "ok", 1.0)
        # Счётчик written_by_type НЕ растёт в _enqueue — только после flush'а.
        assert svc.get_stats()["written_by_type"] == {}
        # queued_at заполнен для каждого LogEvent (≈ время enqueue).
        for item in svc._queue.queue:
            if isinstance(item, LogEvent):
                assert item.queued_at is not None
                assert item.queued_at >= before

    def test_written_by_type_grows_after_flush(self, fake_psycopg2):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=0.05,
                                batch_size=8)
        svc.start()
        try:
            for _ in range(5):
                svc.log_tool_call("cli:1", "read", {})
            for _ in range(3):
                svc.log_tool_result("cli:1", "read", "ok", 1.0)
            time.sleep(0.3)
        finally:
            svc.stop(timeout_sec=2.0)

        counter = svc.get_stats()["written_by_type"]
        assert counter.get("tool_call") == 5
        assert counter.get("tool_result") == 3

    def test_written_by_type_does_not_grow_on_flush_failure(
        self, fake_psycopg2,
    ):
        from utils.db import set_pool_config

        set_pool_config({"connect_max_retries": 1, "reconnect_backoff_sec": 0.05})
        psycopg2 = sys.modules["psycopg2"]
        psycopg2.connect = MagicMock(side_effect=RuntimeError("no db"))
        svc = _svc(dsn="postgresql://x", flush_interval_sec=0.05, batch_size=2)
        svc.start()
        try:
            for _ in range(3):
                svc.log_tool_call("cli:1", "read", {})
            time.sleep(0.3)
        finally:
            svc.stop(timeout_sec=2.0)
        # При падении flush'а written_by_type не должен инкрементироваться.
        assert svc.get_stats()["written_by_type"] == {}
        assert svc.get_stats()["failed"] >= 3

    def test_written_by_type_not_reset_by_restart(self, fake_psycopg2):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=0.05, batch_size=4)
        svc.start()
        try:
            for _ in range(2):
                svc.log_tool_call("cli:1", "read", {})
            time.sleep(0.3)
        finally:
            svc.stop(timeout_sec=2.0)

        first = svc.get_stats()["written_by_type"]
        assert first.get("tool_call") == 2

        # Повторный start() — счётчик written_by_type НЕ сбрасывается.
        svc.start()
        try:
            svc.log_tool_call("cli:1", "read", {})
            time.sleep(0.3)
        finally:
            svc.stop(timeout_sec=2.0)
        second = svc.get_stats()["written_by_type"]
        assert second.get("tool_call") == 3


class TestOldestQueuedAge:
    def test_oldest_queued_age_none_when_empty(self, fake_psycopg2):
        svc = _svc(dsn="postgresql://x")
        assert svc.get_stats()["oldest_queued_age_sec"] is None

    def test_oldest_queued_age_only_counts_log_events(
        self, fake_psycopg2,
    ):
        from lib.services.db_logging_service import _QuestionRunRecord

        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        # Только _QuestionRunRecord — LogEvent'ов нет.
        svc.register_request(
            "cli:1", "m1", user_id="u1", chat_id="c1",
            agent_id="main", question="q",
        )
        assert all(
            isinstance(it, _QuestionRunRecord) for it in svc._queue.queue
        )
        assert svc.get_stats()["oldest_queued_age_sec"] is None

    def test_oldest_queued_age_returns_max_age(self, fake_psycopg2):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        # Два LogEvent с разным queued_at — старший даёт max возраста.
        older = LogEvent(event_type="tool_call")
        older.queued_at = time.time() - 0.3
        newer = LogEvent(event_type="tool_result")
        newer.queued_at = time.time() - 0.1
        # Добавляем напрямую в очередь, минуя _enqueue (чтобы queued_at
        # не переписался на текущий time).
        svc._queue.put_nowait(older)
        svc._queue.put_nowait(newer)
        age = svc.get_stats()["oldest_queued_age_sec"]
        assert age is not None
        # Возраст самого старого — ≈ 0.3 (не 0.1).
        assert age >= 0.25
        assert age < 0.5

    def test_oldest_queued_age_ignores_records_without_queued_at(
        self, fake_psycopg2,
    ):
        from lib.services.db_logging_service import _QuestionRunRecord

        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        le = LogEvent(event_type="tool_call")
        le.queued_at = time.time() - 0.2
        # _QuestionRunRecord без queued_at — должно игнорироваться.
        svc._queue.put_nowait(le)
        svc._queue.put_nowait(_QuestionRunRecord(request_id="x"))
        age = svc.get_stats()["oldest_queued_age_sec"]
        assert age is not None
        assert age >= 0.15
        assert age < 0.4


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


class TestPurge:
    def test_purge_empty_outbound_deletes(self, fake_psycopg2):
        svc = _svc(dsn="postgresql://x")
        fake_psycopg2["cursor"].rowcount = 7
        removed = svc.purge_empty_outbound()
        assert removed == 7
        sqls = [str(c.args[0]) for c in fake_psycopg2["cursor"].execute.call_args_list]
        delete = [s for s in sqls if s.lstrip().upper().startswith("DELETE")]
        assert delete
        assert "outbound_final" in delete[0] and "outbound_delta" in delete[0]
        assert "btrim(payload->>'content')" in delete[0]

    def test_purge_empty_outbound_no_dsn(self, fake_psycopg2):
        svc = _svc(dsn="")
        assert svc.purge_empty_outbound() == 0

    def test_purge_old_respects_retention(self, fake_psycopg2):
        svc = _svc(dsn="postgresql://x", retention_days=10)
        fake_psycopg2["cursor"].rowcount = 4
        ev, runs = svc.purge_old(10)
        assert ev == 4 and runs == 4
        sqls = [str(c.args[0]) for c in fake_psycopg2["cursor"].execute.call_args_list]
        delete = [s for s in sqls if s.lstrip().upper().startswith("DELETE")]
        assert len(delete) == 2  # события + question_runs
        assert "days" in delete[0]

    def test_purge_old_disabled_when_zero(self, fake_psycopg2):
        svc = _svc(dsn="postgresql://x", retention_days=0)
        assert svc.purge_old(0) == (0, 0)

    def test_stats_expose_purge_counters(self, fake_psycopg2):
        svc = _svc(dsn="postgresql://x")
        stats = svc.get_stats()
        for k in ("last_purged_events", "last_purged_runs", "last_purge_at"):
            assert k in stats


class TestNamePopulation:
    """Поле name несёт сущность события (не NULL для нетool-событий)."""

    def test_inbound_name_is_sender(self):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.log_inbound("cli:1", "cli", "hi", sender_id="u42")
        assert svc._queue.queue[0].name == "u42"

    def test_inbound_name_defaults_to_user(self):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.log_inbound("cli:1", "cli", "hi")
        assert svc._queue.queue[0].name == "user"

    def test_outbound_name_is_assistant(self):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.log_outbound("cli:1", "cli", "ok")
        assert svc._queue.queue[0].name == "assistant"
        svc.log_outbound("cli:1", "cli", "ok", kind="outbound_intermediate")
        assert svc._queue.queue[1].name == "assistant"

    def test_llm_call_name_is_model(self):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.log_llm_call("cli:1", "p", "r", model="mini")
        assert svc._queue.queue[0].name == "mini"
        svc.log_llm_call("cli:1", "p", "r", model=None)
        assert svc._queue.queue[1].name == "llm"

    def test_error_name_is_error(self):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.log_error("boom")
        assert svc._queue.queue[0].name == "error"

    def test_tool_events_name_is_tool(self):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.log_tool_call("cli:1", "read", {})
        svc.log_tool_result("cli:1", "read", "r", 1.0)
        call, result = list(svc._queue.queue)
        assert call.name == "read"
        assert result.name == "read"


class TestUserIdPropagation:
    """``LogEvent.user_id`` доходит до INSERT и автозаполняется из индекса."""

    def test_log_event_user_id_reaches_insert(self, fake_psycopg2):
        """Явно заданный producer'ом ``LogEvent.user_id`` доходит до INSERT."""
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.log_event(LogEvent(
            event_type="tool_call",
            session_id="cli:1",
            request_id="r1",
            user_id="alice",
        ))
        event = svc._queue.queue[0]
        assert event.user_id == "alice"
        # _insert_batch использует psycopg2.extras.execute_batch
        # (мокается в fake_psycopg2), который вызывается с SQL и
        # списком параметров-строк. Проверяем, что user_id попал в оба.
        svc._insert_batch(fake_psycopg2["conn"], [event])
        call = fake_psycopg2["execute_batch"].call_args
        sql, rows = call.args[1], call.args[2]
        assert "user_id" in sql
        # Параметры — список кортежей: первый кортеж содержит user_id
        # на позиции сразу после event_type.
        assert "alice" in rows[0]

    def test_auto_filled_user_id_reaches_insert_via_index(self, fake_psycopg2):
        """End-to-end прокидывание auto-filled ``user_id`` в INSERT.

        Полная security-boundary цепочка:

          register_request(alice)
                ↓
          _enqueue(LogEvent(user_id=None, request_id=req-A))
                ↓ _resolve_event_user_id
          event.user_id = alice  (через request_id matching)
                ↓
          _insert_batch()
                ↓
          SQL params содержат ``alice`` в позиции user_id

        Без этого теста покрытие было бы разорвано: explicit value
        проверялся отдельно (test_log_event_user_id_reaches_insert),
        auto-fill — отдельно (test_enqueue_fills_user_id_when_request_id_matches),
        но именно «auto-filled → INSERT» — нет. Это критично для
        history_search(session_scope="all") как security boundary:
        если бы между ``_enqueue`` и ``_insert_batch`` значение
        терялось, фильтр ``user_id = %s`` возвращал бы 0 строк.
        """
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.register_request(
            "cli:1", "req-A", user_id="alice", chat_id="c1",
        )
        # Producer создаёт событие БЕЗ user_id — auto-fill путь.
        event = LogEvent(
            event_type="tool_call",
            session_id="cli:1",
            request_id="req-A",
            user_id=None,
        )
        svc._enqueue(event)
        # После _enqueue event.user_id заполнен индексом.
        assert event.user_id == "alice"

        # Полный путь в INSERT: execute_batch должен получить SQL с
        # колонкой user_id и параметры с ``alice`` в нужной позиции.
        fake_psycopg2["execute_batch"].reset_mock()
        svc._insert_batch(fake_psycopg2["conn"], [event])

        call = fake_psycopg2["execute_batch"].call_args
        sql = call.args[1]
        rows = call.args[2]
        assert "user_id" in sql
        # В execute_batch первый аргумент — SQL, второй — список
        # кортежей; в каждом кортеже позиция user_id — сразу после
        # event_type (порядок колонок фиксирован в _insert_batch).
        assert "alice" in rows[0], (
            f"alice должна быть в позиции user_id INSERT-параметров; "
            f"получено: {rows[0]!r}"
        )

    def test_auto_filled_user_id_does_not_reach_insert_when_request_mismatch(
        self, fake_psycopg2,
    ):
        """End-to-end: stale-event auto-fill не «протекает» в INSERT.

        register A/alice → LogEvent(req-A, user_id=None) →
        register B/bob → _enqueue того же события →
        _insert_batch: SQL params содержат ``None`` в позиции user_id,
        НЕ ``bob``. Это primary logging-security acceptance на уровне
        реальной INSERT-цепочки (не только очереди).
        """
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.register_request(
            "cli:1", "req-A", user_id="alice", chat_id="c1",
        )
        stale_event = LogEvent(
            event_type="tool_call",
            session_id="cli:1",
            request_id="req-A",
            user_id=None,
        )
        # Между созданием и _enqueue — перерегистрация индекса.
        svc.register_request(
            "cli:1", "req-B", user_id="bob", chat_id="c1",
        )
        svc._enqueue(stale_event)
        # Stale event остался без user_id (не подхватил bob).
        assert stale_event.user_id is None

        # INSERT содержит None в позиции user_id.
        fake_psycopg2["execute_batch"].reset_mock()
        svc._insert_batch(fake_psycopg2["conn"], [stale_event])
        rows = fake_psycopg2["execute_batch"].call_args.args[2]
        # Позиция user_id — после event_type: (id, level, event_type, user_id, ...)
        user_id_value = rows[0][3]
        assert user_id_value is None, (
            f"stale event должен сохранить user_id=None, "
            f"получено: {user_id_value!r}"
        )

    def test_enqueue_fills_user_id_when_request_id_matches(self, fake_psycopg2):
        """register_request + LogEvent(user_id=None) с тем же request_id
        автозаполняет ``user_id`` из индекса при ``_enqueue``."""
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.register_request(
            "cli:1", "r1", user_id="alice", chat_id="c1",
        )
        # Producer создаёт событие с явным request_id, но без user_id.
        event = LogEvent(
            event_type="tool_call",
            session_id="cli:1",
            request_id="r1",
        )
        assert event.user_id is None
        ok = svc._enqueue(event)
        assert ok is True
        # После _enqueue event.user_id подставлен из индекса.
        assert event.user_id == "alice"

    def test_explicit_user_id_overrides_index(self, fake_psycopg2):
        """Явный ``LogEvent.user_id`` от producer'а побеждает индекс."""
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.register_request(
            "cli:1", "r1", user_id="alice", chat_id="c1",
        )
        event = LogEvent(
            event_type="tool_call",
            session_id="cli:1",
            request_id="r1",
            user_id="bob",
        )
        svc._enqueue(event)
        # Явное значение победило — никакой подмены из индекса.
        assert event.user_id == "bob"

    def test_stale_event_does_not_inherit_next_request_user_id(
        self, fake_psycopg2,
    ):
        """Primary logging-security тест: stale event с request_id=A,
        созданный до ``register_request(B, user_id='bob')``, остаётся с
        ``user_id=None`` при постановке в очередь. Это закрывает security
        окно вида «отложенное событие req-A получает user_id следующего
        request req-B»."""
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.register_request(
            "cli:1", "req-A", user_id="alice", chat_id="c1",
        )
        # Producer создал событие req-A с пустым user_id — НЕ в очередь.
        stale_event = LogEvent(
            event_type="tool_call",
            session_id="cli:1",
            request_id="req-A",
        )
        # Регистрация следующего request'а той же session_key.
        svc.register_request(
            "cli:1", "req-B", user_id="bob", chat_id="c1",
        )
        # Теперь ставим stale_event в очередь.
        svc._enqueue(stale_event)
        # Stale event НЕ подхватил bob — индекс уже под req-B, но
        # request_id у события = req-A, не совпадает.
        assert stale_event.user_id is None

    def test_event_without_request_id_does_not_inherit_user_id(
        self, fake_psycopg2,
    ):
        """Событие без ``request_id`` НЕ получает ``user_id`` из индекса,
        даже если для session_key индекс заполнен."""
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.register_request(
            "cli:1", "req-A", user_id="alice", chat_id="c1",
        )
        event = LogEvent(
            event_type="tool_call",
            session_id="cli:1",
            request_id=None,
        )
        svc._enqueue(event)
        # request_id=None → никакого matching → user_id остаётся None.
        assert event.user_id is None

    def test_register_request_updates_pair_atomically(self, fake_psycopg2):
        """Атомарность пары ``{request_id, user_id}``: параллельный
        reader во время ``register_request`` видит либо полностью старое
        состояние, либо полностью новое — не смесь."""
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.register_request(
            "cli:1", "req-A", user_id="alice", chat_id="c1",
        )
        # Запускаем register_request(req-B, user_id=bob) параллельно с
        # reader'ом, который читает индекс 100 раз через lock.
        errors: list[str] = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                with svc._request_index_lock:
                    entry = svc._request_index.get("cli:1")
                if not isinstance(entry, dict):
                    continue
                rid = entry.get("request_id")
                uid = entry.get("user_id")
                if rid == "req-A" and uid != "alice":
                    errors.append(
                        f"A mismatch: rid={rid} uid={uid}"
                    )
                elif rid == "req-B" and uid != "bob":
                    errors.append(
                        f"B mismatch: rid={rid} uid={uid}"
                    )
                elif rid not in ("req-A", "req-B"):
                    errors.append(f"unknown rid: {rid}")

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        try:
            svc.register_request(
                "cli:1", "req-B", user_id="bob", chat_id="c1",
            )
            time.sleep(0.05)
        finally:
            stop.set()
            t.join(timeout=2.0)
        assert not errors, errors

    def test_no_public_get_request_user_id(self):
        """У ``DbLoggingService`` НЕТ публичного ``get_request_user_id``
        (или эквивалента вроде ``lookup_user_id``/``resolve_user_id``).
        ``user_id`` из индекса читается ТОЛЬКО внутри ``_enqueue``
        через request_id matching — ни один компонент не получает
        способ резолвить чужой identity по session_key."""
        forbidden = {
            "get_request_user_id",
            "lookup_user_id",
            "resolve_user_id",
        }
        for name in forbidden:
            assert not hasattr(DbLoggingService, name), (
                f"DbLoggingService.{name} не должен существовать "
                "(security boundary)"
            )

    def test_clear_request_removes_pair(self, fake_psycopg2):
        """``clear_request`` удаляет всю парную запись {request_id, user_id}."""
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.register_request(
            "cli:1", "r1", user_id="alice", chat_id="c1",
        )
        assert svc.get_request_id("cli:1") == "r1"
        svc.clear_request("cli:1")
        assert svc.get_request_id("cli:1") is None
        # После clear новые события НЕ получают user_id из индекса.
        event = LogEvent(
            event_type="tool_call",
            session_id="cli:1",
            request_id="r1",
        )
        svc._enqueue(event)
        assert event.user_id is None
