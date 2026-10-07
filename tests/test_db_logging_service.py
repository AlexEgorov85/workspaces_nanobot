from __future__ import annotations

import re
import threading
import time
import types
from pathlib import Path

import pytest

from lib.services.db_logging_service import DbLoggingService, LogEvent
from config import runtime_table  # noqa: F401

SERVICE_PATH = Path(__file__).resolve().parent.parent / "lib" / "services" / "db_logging_service.py"


def _svc(**kw):
    kwargs = dict(
        dsn="postgresql://x",
        table_name=runtime_table("gateway_logs"),
        question_runs_table=runtime_table("question_runs"),
    )
    kwargs.update(kw)
    return DbLoggingService(**kwargs)


class RecordingWriter:
    """Писатель-заглушка: у сервиса больше нет своей записи в базу.

    Повторяет те три метода, которые ``DbLoggingService`` вызывает у
    ``McpLogWriter``. Проверяемое поведение переехало сюда целиком: кто пишет,
    каким вызовом и с каким телом. Собственного соединения у сервиса не
    осталось, поэтому подменять тут нечего — и подменять было бы нечего.
    """

    def __init__(
        self,
        *,
        purge_counters: dict[str, int] | None = None,
        question_run_written: bool = True,
        purge_error: Exception | None = None,
    ) -> None:
        self.batches: list[list] = []
        self.runs: list = []
        self.purges: list[dict] = []
        self.purge_counters = (
            purge_counters
            if purge_counters is not None
            else {"empty_outbound": 0, "events": 0, "question_runs": 0}
        )
        self.question_run_written = question_run_written
        self.purge_error = purge_error
        self.on_fallback = None

    def write_events(self, batch):
        from lib.services.log_transport import WriteResult

        self.batches.append(list(batch))
        return WriteResult(accepted=len(batch), dropped=0)

    def upsert_question_run(self, record) -> bool:
        self.runs.append(record)
        return self.question_run_written

    def purge_logs(self, *, retention_days, remove_empty_outbound=None):
        self.purges.append(
            {
                "retention_days": retention_days,
                "remove_empty_outbound": remove_empty_outbound,
            }
        )
        if self.purge_error is not None:
            raise self.purge_error
        return dict(self.purge_counters)


@pytest.fixture
def writer() -> RecordingWriter:
    """Свежий писатель на тест: батчи не должны течь между тестами."""
    return RecordingWriter()


class TestBasicLifecycle:
    def test_start_stop(self):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=0.05)
        svc.start()
        try:
            assert svc.is_running()
        finally:
            svc.stop(timeout_sec=2.0)
        assert not svc.is_running()

    def test_no_writer_drops_events(self, tmp_path):
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
    def test_log_inbound_enqueue(self):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        assert svc.log_inbound("cli:1", "cli", "hello") is True
        assert svc.get_stats()["queued"] >= 1

    def test_log_inbound_sender_and_chat(self):
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

    def test_log_inbound_default_actor_is_user(self):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.log_inbound("cli:1", "cli", "hello")
        assert svc._queue.queue[0].actor == "user"

    def test_log_inbound_request_id_defaults_to_message_id(self):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.log_inbound("cli:1", "cli", "hello", message_id="m1")
        event = svc._queue.queue[0]
        assert event.request_id == "m1"
        assert event.payload["message_id"] == "m1"

    def test_log_outbound_request_id(self):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.log_outbound("cli:1", "cli", "ok", request_id="m1")
        event = svc._queue.queue[0]
        assert event.request_id == "m1"

    def test_log_tool_event_request_id(self):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.log_tool_call("cli:1", "read", {"p": 1}, tool_call_id="t1", request_id="m1")
        svc.log_tool_result("cli:1", "read", "r", 10.0, tool_call_id="t1", request_id="m1")
        call, result = list(svc._queue.queue)
        assert call.request_id == "m1" and call.name == "read"
        assert result.request_id == "m1" and result.name == "read"

    def test_question_binding_lifecycle(self):
        """Жизненный цикл привязки вопроса: снятие убирает ``request_id``,
        но НЕ снимок входа (финальный ответ приходит после этого вызова)."""
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

    def test_question_run_records(self):
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

    def test_log_tool_event_dimensions(self):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.log_tool_call(
            "cli:1", "read", {"p": 1}, tool_call_id="t1", request_id="m1",
        )
        event = svc._queue.queue[0]
        assert event.name == "read"
        assert event.request_id == "m1"

    def test_log_event_min_level(self):
        svc = _svc(dsn="postgresql://x", min_level="WARN")
        assert svc.log_event(LogEvent("x", "DEBUG")) is False
        assert svc.log_event(LogEvent("x", "INFO")) is False
        assert svc.log_event(LogEvent("x", "ERROR")) is True

    def test_log_outbound_with_meta(self):
        svc = _svc(dsn="postgresql://x")
        assert svc.log_outbound(
            "cli:1", "cli", "ok", latency_ms=12.5, tokens_used=42
        ) is True

    def test_log_media_in_payload(self):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.log_inbound("cli:1", "cli", "привет", media=["doc.pdf"])
        svc.log_outbound("cli:1", "cli", "ответ", media=["report.xlsx"])
        inbound, outbound = list(svc._queue.queue)
        assert inbound.payload["media"] == ["doc.pdf"]
        assert outbound.payload["media"] == ["report.xlsx"]

    def test_log_tool_call_and_result(self):
        svc = _svc(dsn="postgresql://x")
        assert svc.log_tool_call("cli:1", "read", {"path": "x"}) is True
        assert svc.log_tool_result(
            "cli:1", "read", "content", latency_ms=15.0
        ) is True

    def test_tool_result_error_summary_carrier(self):
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

    def test_log_llm_call_fields(self):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        prompt = [{"role": "user", "content": "привет"}]
        response = {"content": "ответ", "tool_calls": [], "finish_reason": "stop"}
        svc.log_llm_call(
            "cli:1", prompt, response,
            iteration=2, model="mini", finish_reason="stop",
            usage={"total_tokens": 10}, request_id="m1",
        )
        event = svc._queue.queue[0]
        assert event.event_type == "llm.exchanged"
        assert event.actor == "agent"
        assert event.request_id == "m1"
        assert event.summary == "stop"
        assert event.payload["prompt"] == prompt
        assert event.payload["response"] == response
        assert event.metadata["iteration"] == 2
        assert event.metadata["model"] == "mini"
        assert event.metadata["finish_reason"] == "stop"
        assert event.metadata["usage"] == {"total_tokens": 10}

    def test_log_llm_call_sanitizes_non_json(self):
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

    def test_queue_full_returns_false(self):
        svc = _svc(dsn="postgresql://x", queue_maxsize=2)
        # Не запускаем worker — очередь наполнится до запуска.
        for _ in range(2):
            assert svc.log_event(LogEvent("x")) is True
        assert svc.log_event(LogEvent("x")) is False
        assert svc.get_stats()["queue_full"] == 1


class TestFlush:
    def test_batch_reaches_the_writer(self, writer):
        """Батч уходит единственному писателю, а не в базу из агента."""
        svc = _svc(flush_interval_sec=0.05, batch_size=3, mcp_writer=writer)
        svc.start()
        try:
            for i in range(3):
                svc.log_inbound("cli:1", "cli", f"msg{i}")
            time.sleep(0.3)
        finally:
            svc.stop(timeout_sec=2.0)

        assert writer.batches, "писатель не вызван"
        assert sum(len(b) for b in writer.batches) >= 3
        written = svc.get_stats()["written"]
        assert written >= 3

    def test_drop_when_no_writer(self, tmp_path):
        """Писателя нет — события потеряны, и потеря названа.

        Раньше тест назывался ``test_drop_when_no_dsn``: DSN был условием
        доступа к базе. Теперь доступа к базе нет вообще, и условие потери —
        отсутствие писателя. Название оставлено бы прежним, оно бы врало.
        """
        svc = _svc(flush_interval_sec=0.05, batch_size=2)
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

    def test_writer_failure_drops_and_names_the_reason(self, writer, tmp_path):
        """Отказ писателя виден и назван, а не проглочен.

        Путь прежний: падение записи должно оставить след в счётчиках. Раньше
        источником отказа был «не поднялся PostgreSQL», теперь — платформа,
        и подменять его отказ писателя проверяет ровно то же самое, что раньше
        проверял отказ соединения.
        """

        def _boom(batch):
            raise RuntimeError("платформа не отвечает")

        writer.write_events = _boom  # type: ignore[method-assign]
        svc = _svc(flush_interval_sec=0.05, batch_size=1, mcp_writer=writer)
        svc.start()
        try:
            svc.log_inbound("cli:1", "cli", "x")
            time.sleep(0.2)
        finally:
            svc.stop(timeout_sec=2.0)
        stats = svc.get_stats()
        assert stats["failed"] >= 1
        assert not (tmp_path / "log.jsonl").exists()

    def test_stop_flushes_remaining(self, writer):
        svc = _svc(flush_interval_sec=5.0, batch_size=100, mcp_writer=writer)
        svc.start()
        try:
            svc.log_inbound("cli:1", "cli", "left")
        finally:
            svc.stop(timeout_sec=2.0)
        assert svc.get_stats()["written"] >= 1


class TestGetStats:
    def test_keys_present(self):
        svc = _svc(dsn="postgresql://x")
        stats = svc.get_stats()
        for k in ("running", "queued", "written", "failed", "queue_size",
                  "batch_count", "queue_full",
                  "connected", "last_error"):
            assert k in stats


class TestWrittenByType:
    def test_written_by_type_empty_on_start(self):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        assert svc.get_stats()["written_by_type"] == {}

    def test_enqueue_does_not_increment_written_by_type(self):
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

    def test_written_by_type_grows_after_flush(self, writer):
        svc = _svc(flush_interval_sec=0.05, batch_size=8, mcp_writer=writer)
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
        assert counter.get("tool.started") == 5
        assert counter.get("tool.completed") == 3

    def test_written_by_type_does_not_grow_on_flush_failure(self, writer):
        def _boom(batch):
            raise RuntimeError("платформа не отвечает")

        writer.write_events = _boom  # type: ignore[method-assign]
        svc = _svc(flush_interval_sec=0.05, batch_size=2, mcp_writer=writer)
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

    def test_written_by_type_not_reset_by_restart(self, writer):
        svc = _svc(flush_interval_sec=0.05, batch_size=4, mcp_writer=writer)
        svc.start()
        try:
            for _ in range(2):
                svc.log_tool_call("cli:1", "read", {})
            time.sleep(0.3)
        finally:
            svc.stop(timeout_sec=2.0)

        first = svc.get_stats()["written_by_type"]
        assert first.get("tool.started") == 2

        # Повторный start() — счётчик written_by_type НЕ сбрасывается.
        svc.start()
        try:
            svc.log_tool_call("cli:1", "read", {})
            time.sleep(0.3)
        finally:
            svc.stop(timeout_sec=2.0)
        second = svc.get_stats()["written_by_type"]
        assert second.get("tool.started") == 3


class TestOldestQueuedAge:
    def test_oldest_queued_age_none_when_empty(self):
        svc = _svc(dsn="postgresql://x")
        assert svc.get_stats()["oldest_queued_age_sec"] is None

    def test_oldest_queued_age_only_counts_log_events(
        self,
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

    def test_oldest_queued_age_returns_max_age(self):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        # Два LogEvent с разным queued_at — старший даёт max возраста.
        older = LogEvent(event_type="tool.started")
        older.queued_at = time.time() - 0.3
        newer = LogEvent(event_type="tool.completed")
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
        self,
    ):
        from lib.services.db_logging_service import _QuestionRunRecord

        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        le = LogEvent(event_type="tool.started")
        le.queued_at = time.time() - 0.2
        # _QuestionRunRecord без queued_at — должно игнорироваться.
        svc._queue.put_nowait(le)
        svc._queue.put_nowait(_QuestionRunRecord(request_id="x"))
        age = svc.get_stats()["oldest_queued_age_sec"]
        assert age is not None
        assert age >= 0.15
        assert age < 0.4


class TestSingleWriter:
    """Прямого SQL у сервиса нет: писатель один, и он не агент.

    Раньше здесь стояли проверки текста ``INSERT``/``UPDATE`` и схемы таблиц.
    Проверяемое поведение не исчезло, а уехало туда, где теперь пишут:
    двухшаговый ``upsert`` без ``ON CONFLICT`` (Greenplum 6.5) и правила
    чистки охраняет ``mcp-platform/tests/test_data_journal_operations.py``.
    Дублировать его тут было бы проверкой чужого файла.

    Что осталось за агентом — и что проверяется здесь — это передача: сервис
    зовёт операцию платформы и не строит SQL сам.
    """

    def test_no_sql_is_built_anywhere_in_the_service(self):
        """В модуле writer'а не осталось ни одного SQL-выражения.

        Разбор дерева, а не поиск подстроки: мёртвый код, оставшийся после
        переноса, не поймал бы ни один функциональный тест — он просто не
        звался бы. Именно такой остаток и возвращает путь записи в базу,
        когда его начинают звать снова.

        Проверяется код, а не текст: в docstring'ах слова ``psycopg2`` и
        ``lib.utils.db`` остаться обязаны — они объясняют, почему их нет в коде.
        Проверка словом искала бы не код, а упоминание.
        """
        import ast

        tree = ast.parse(SERVICE_PATH.read_text(encoding="utf-8"))
        imported: set[str] = set()
        sql_literals: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if re.search(
                    r"\b(INSERT\s+INTO|DELETE\s+FROM|UPDATE\s+\"|information_schema|"
                    r"ON\s+CONFLICT|SELECT\s+1\s+FROM)",
                    node.value,
                    re.IGNORECASE,
                ):
                    sql_literals.append(node.value)
        assert "lib.utils.db" not in imported, "пул записи журнала в дереве агента не нужен"
        assert not any(name.split(".")[0] == "psycopg2" for name in imported), (
            "драйвер БД в дереве агента не нужен вовсе"
        )
        assert not sql_literals, (
            "db_logging_service.py вернул SQL: писатель один, и он не агент: "
            + repr(sql_literals)
        )

    def test_question_run_goes_through_the_operation(self, writer):
        """Контекст вопроса уходит операцией, а не ``INSERT``-ом агента."""
        from lib.services.db_logging_service import _QuestionRunRecord

        svc = _svc(mcp_writer=writer)
        svc._handle_question_run(
            _QuestionRunRecord(
                request_id="m1", session_id="cli:1", user_id="u1",
                chat_id="c1", channel="cli", agent_id="main", status="running",
            )
        )
        assert [r.request_id for r in writer.runs] == ["m1"]
        assert svc.get_stats()["question_runs"] == 1

    def test_question_run_without_writer_is_a_named_loss(self):
        """Нет писателя — потеря названа, а не замаскирована под запись."""
        from lib.services.db_logging_service import _QuestionRunRecord

        svc = _svc()
        svc._handle_question_run(_QuestionRunRecord(request_id="m1"))
        stats = svc.get_stats()
        assert stats["question_runs"] == 0
        assert stats["failed"] == 1
        assert any("писатель" in reason for reason in stats["loss_reasons"]), (
            f"причина потери не названа: {stats['loss_reasons']}"
        )


class TestPurge:
    """Чистка журнала — операция платформы, и агент выбирает только режим.

    Сами ``DELETE`` и список «пустых» типов событий охраняет
    ``mcp-platform/tests/test_data_journal_operations.py``. Здесь проверяется
    то, что реально решает агент: какой режим уходит в вызов.
    """

    def test_purge_empty_outbound_asks_for_that_mode_only(self):
        writer = RecordingWriter(purge_counters={"empty_outbound": 7})
        svc = _svc(mcp_writer=writer)
        assert svc.purge_empty_outbound() == 7
        assert writer.purges == [
            {"retention_days": 0, "remove_empty_outbound": True}
        ], "retention выключен — иначе подчистилось бы лишнее"

    def test_purge_old_sends_retention_and_not_empty_outbound(self):
        writer = RecordingWriter(purge_counters={"events": 4, "question_runs": 3})
        svc = _svc(retention_days=10, mcp_writer=writer)
        assert svc.purge_old(10) == (4, 3)
        assert writer.purges == [
            {"retention_days": 10, "remove_empty_outbound": False}
        ]

    def test_purge_old_disabled_when_zero_calls_nothing(self):
        writer = RecordingWriter()
        svc = _svc(retention_days=0, mcp_writer=writer)
        assert svc.purge_old(0) == (0, 0)
        assert writer.purges == [], (
            "выключенный retention не должен ходить в платформу на каждом тике"
        )

    def test_purge_without_writer_does_not_delete_anything(self):
        svc = _svc()
        assert svc.purge_empty_outbound() == 0
        assert svc.purge_old(10) == (0, 0)
        assert "purge" in (svc.get_stats()["last_error"] or "")

    def test_purge_failure_is_reported_not_swallowed(self):
        writer = RecordingWriter(purge_error=RuntimeError("отказ платформы"))
        svc = _svc(mcp_writer=writer)
        assert svc.purge_empty_outbound() == 0
        assert "отказ платформы" in (svc.get_stats()["last_error"] or "")

    def test_stats_expose_purge_counters(self):
        svc = _svc()
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
        # Раньше вторая строка зову́ла ``log_outbound(..., kind=
        # "outbound_intermediate")``: тест проверял, что ``name`` не зависит от
        # ВИДА исходящего. Промежуточные ``message(...)`` больше не пишутся
        # вовсе (не-событие), а параметра ``kind`` не существует — сравнивать
        # нечего. Повторный вызов проверяет то же самое на единственном
        # существующем виде: заполнение ``name`` стабильно.
        svc.log_outbound("cli:1", "cli", "ещё один ответ")
        assert svc._queue.queue[1].name == "assistant"

    def test_llm_call_name_is_model(self):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.log_llm_call("cli:1", "p", "r", model="mini")
        assert svc._queue.queue[0].name == "mini"
        svc.log_llm_call("cli:1", "p", "r", model=None)
        assert svc._queue.queue[1].name == "llm"

    def test_tool_events_name_is_tool(self):
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.log_tool_call("cli:1", "read", {})
        svc.log_tool_result("cli:1", "read", "r", 1.0)
        call, result = list(svc._queue.queue)
        assert call.name == "read"
        assert result.name == "read"


class TestUserIdPropagation:
    """``LogEvent.user_id`` доходит до писателя и автозаполняется из индекса.

    Проверяется граница «сервис → писатель», а не текст SQL: писателя у
    сервиса больше нет. Дальше ``user_id`` едет не в теле батча, а в
    контексте вызова — операция ``log_events`` берёт личность оттуда, и
    ``event_to_wire`` её в батч не кладёт намеренно. Этот шаг охраняет
    ``tests/test_log_transport.py`` (разбиение по личности вызова).
    """

    def test_log_event_user_id_reaches_the_writer(self, writer):
        """Явно заданный producer'ом ``LogEvent.user_id`` доходит до писателя."""
        svc = _svc(mcp_writer=writer)
        svc.log_event(LogEvent(
            event_type="tool.started",
            session_id="cli:1",
            request_id="r1",
            user_id="alice",
        ))
        svc._flush_batch([i for i in svc._queue.queue if isinstance(i, LogEvent)])
        assert writer.batches[0][0].user_id == "alice"

    def test_auto_filled_user_id_reaches_the_writer(self, writer):
        """End-to-end прокидывание auto-filled ``user_id`` писателю.

        Полная security-boundary цепочка:

          register_request(alice)
                ↓
          log_event(LogEvent(user_id=None, request_id=req-A))
                ↓ _resolve_event_user_id
          event.user_id = alice  (через request_id matching)
                ↓
          write_events()
                ↓
          событие, ушедшее платформе, помечено user_id = ``alice``

        Без этого теста покрытие было бы разорвано: explicit value
        проверялся отдельно (test_log_event_user_id_reaches_the_writer),
        auto-fill — отдельно (test_enqueue_fills_user_id_when_request_id_matches),
        но именно «auto-filled → у писателя» — нет. Это критично для
        history_search(session_scope="all") как security boundary:
        если бы между ``log_event`` и ``write_events`` значение
        терялось, фильтр ``user_id = %s`` возвращал бы 0 строк.

        Событие идёт через ``log_event``, а не напрямую в ``_enqueue``:
        ``log_event`` — единственная точка входа в журнал, и именно она
        ставит событию момент (``_stamp_event_time``). Обход точки входа
        проверял бы путь, которого в жизни нет.
        """
        svc = _svc(mcp_writer=writer)
        svc.register_request(
            "cli:1", "req-A", user_id="alice", chat_id="c1",
        )
        # Producer создаёт событие БЕЗ user_id — auto-fill путь.
        event = LogEvent(
            event_type="tool.started",
            session_id="cli:1",
            request_id="req-A",
            user_id=None,
        )
        assert svc.log_event(event) is True
        # После разрешения event.user_id заполнен индексом.
        assert event.user_id == "alice"

        svc._flush_batch([i for i in svc._queue.queue if isinstance(i, LogEvent)])

        assert writer.batches[0][0].user_id == "alice", (
            f"alice обязана дойти до писателя; получено: "
            f"{writer.batches[0][0].user_id!r}"
        )

    def test_auto_filled_user_id_does_not_reach_the_writer_when_request_mismatch(
        self, writer,
    ):
        """End-to-end: stale-event auto-fill не «протекает» к платформе.

        register A/alice → LogEvent(req-A, user_id=None) →
        register B/bob → log_event того же события →
        write_events: ушедшее событие помечено ``user_id=None``, НЕ ``bob``.
        Это primary logging-security acceptance на уровне реальной цепочки
        записи (не только очереди).
        """
        svc = _svc(mcp_writer=writer)
        svc.register_request(
            "cli:1", "req-A", user_id="alice", chat_id="c1",
        )
        stale_event = LogEvent(
            event_type="tool.started",
            session_id="cli:1",
            request_id="req-A",
            user_id=None,
        )
        # Между созданием и постановкой в очередь — перерегистрация индекса.
        svc.register_request(
            "cli:1", "req-B", user_id="bob", chat_id="c1",
        )
        assert svc.log_event(stale_event) is True
        # Stale event остался без user_id (не подхватил bob).
        assert stale_event.user_id is None

        svc._flush_batch([i for i in svc._queue.queue if isinstance(i, LogEvent)])

        # Событие уходит платформе вообще — и уходит БЕЗ чужого user_id.
        assert writer.batches, "событие должно уйти платформе, а не затеряться"
        assert writer.batches[0][0].user_id is None, (
            f"stale event должен сохранить user_id=None, получено: "
            f"{writer.batches[0][0].user_id!r}"
        )

    def test_enqueue_fills_user_id_when_request_id_matches(self):
        """register_request + LogEvent(user_id=None) с тем же request_id
        автозаполняет ``user_id`` из индекса при ``_enqueue``."""
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.register_request(
            "cli:1", "r1", user_id="alice", chat_id="c1",
        )
        # Producer создаёт событие с явным request_id, но без user_id.
        event = LogEvent(
            event_type="tool.started",
            session_id="cli:1",
            request_id="r1",
        )
        assert event.user_id is None
        ok = svc._enqueue(event)
        assert ok is True
        # После _enqueue event.user_id подставлен из индекса.
        assert event.user_id == "alice"

    def test_explicit_user_id_overrides_index(self):
        """Явный ``LogEvent.user_id`` от producer'а побеждает индекс."""
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.register_request(
            "cli:1", "r1", user_id="alice", chat_id="c1",
        )
        event = LogEvent(
            event_type="tool.started",
            session_id="cli:1",
            request_id="r1",
            user_id="bob",
        )
        svc._enqueue(event)
        # Явное значение победило — никакой подмены из индекса.
        assert event.user_id == "bob"

    def test_stale_event_does_not_inherit_next_request_user_id(
        self,
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
            event_type="tool.started",
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
        self,
    ):
        """Событие без ``request_id`` НЕ получает ``user_id`` из индекса,
        даже если для session_key индекс заполнен."""
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.register_request(
            "cli:1", "req-A", user_id="alice", chat_id="c1",
        )
        event = LogEvent(
            event_type="tool.started",
            session_id="cli:1",
            request_id=None,
        )
        svc._enqueue(event)
        # request_id=None → никакого matching → user_id остаётся None.
        assert event.user_id is None

    def test_register_request_updates_pair_atomically(self):
        """Атомарность пары ``{request_id, user_id}``: параллельный
        reader во время ``register_request`` видит либо полностью старое
        состояние, либо полностью новое — не смесь.

        Пара приходит ОДНИМ чтением записи под её собственным замком. Раньше
        читатель брал словарь индекса под замком службы, а снимок входа лежал
        в другом словаре под другим, и пара «индекс + снимок» не была
        атомарной — оба читателя сверялись сами. Теперь сверять нечего.
        """
        svc = _svc(dsn="postgresql://x", flush_interval_sec=5.0)
        svc.register_request(
            "cli:1", "req-A", user_id="alice", chat_id="c1",
        )
        # Запускаем register_request(req-B, user_id=bob) параллельно с
        # reader'ом, который читает запись личности по кругу.
        errors: list[str] = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                entry = svc.turn_identities.question_of("cli:1")
                if entry is None:
                    continue
                rid = entry.request_id
                uid = entry.user_id
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

    def test_clear_request_removes_pair(self):
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
            event_type="tool.started",
            session_id="cli:1",
            request_id="r1",
        )
        svc._enqueue(event)
        assert event.user_id is None
