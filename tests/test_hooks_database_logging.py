from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# db_logging_bus импортирует utils.media (workspace на sys.path).
_workspace_path = str(Path(__file__).resolve().parent.parent / "workspace")
if _workspace_path not in sys.path:
    sys.path.insert(0, _workspace_path)


@pytest.fixture
def sys_path():
    import sys
    from pathlib import Path
    p = str(Path(__file__).resolve().parent.parent)
    if p not in sys.path:
        sys.path.insert(0, p)
    return p


class TestDatabaseLoggingHook:
    def test_before_execute_tool(self, sys_path):
        from lib.hooks.database_logging_hook import DatabaseLoggingHook

        service = MagicMock()
        service.get_request_id.return_value = "m1"
        hook = DatabaseLoggingHook(service)
        ctx = MagicMock()
        ctx.session_key = "cli:1"
        tool_call = MagicMock()
        tool_call.id = "tc1"
        tool_call.name = "read"
        tool = MagicMock()
        params = {"path": "x"}

        asyncio.run(hook.before_execute_tool(ctx, tool_call, tool, params))
        service.log_tool_call.assert_called_once()
        kwargs = service.log_tool_call.call_args.kwargs
        assert kwargs["session_id"] == "cli:1"
        assert kwargs["tool_name"] == "read"
        assert kwargs["args"] == {"path": "x"}
        assert kwargs["tool_call_id"] == "tc1"
        assert kwargs["request_id"] == "m1"

    def test_after_execute_tool_records_latency(self, sys_path):
        from lib.hooks.database_logging_hook import DatabaseLoggingHook

        service = MagicMock()
        service.get_request_id.return_value = "m1"
        hook = DatabaseLoggingHook(service)
        ctx = MagicMock()
        ctx.session_key = "cli:1"
        tool_call = MagicMock()
        tool_call.id = "tc2"
        tool_call.name = "write"
        tool = MagicMock()
        params = {}
        # Seed tool start time
        hook._tool_start_times["tc2"] = 0.0

        with __import__("unittest.mock").mock.patch(
            "lib.hooks.database_logging_hook.time.time", return_value=0.1
        ):
            asyncio.run(hook.after_execute_tool(ctx, tool_call, tool, params, "ok"))

        service.log_tool_result.assert_called_once()
        kwargs = service.log_tool_result.call_args.kwargs
        assert kwargs["latency_ms"] == pytest.approx(100.0)
        assert kwargs["status"] == "ok"

    def test_on_execute_tool_error(self, sys_path):
        from lib.hooks.database_logging_hook import DatabaseLoggingHook

        service = MagicMock()
        service.get_request_id.return_value = "m1"
        hook = DatabaseLoggingHook(service)
        ctx = MagicMock()
        ctx.session_key = "cli:1"
        tool_call = MagicMock()
        tool_call.id = "tc3"
        tool_call.name = "exec"
        tool = MagicMock()
        params = {}

        asyncio.run(
            hook.on_execute_tool_error(ctx, tool_call, tool, params, RuntimeError("boom"))
        )
        kwargs = service.log_tool_result.call_args.kwargs
        assert kwargs["status"] == "error"
        assert "boom" in kwargs["error"]
        assert kwargs["level"] == "ERROR"

    def test_after_run_emits_event(self, sys_path):
        from lib.hooks.database_logging_hook import DatabaseLoggingHook

        service = MagicMock()
        service.get_request_id.return_value = "m1"
        hook = DatabaseLoggingHook(service)
        ctx = MagicMock()
        ctx.final_content = "hello"
        ctx.tools_used = ["read", "write"]
        ctx.stop_reason = "stop"
        ctx.had_injections = False
        ctx.error = None
        ctx.usage = {"total_tokens": 123}

        asyncio.run(hook.before_iteration(MagicMock(session_key="cli:1")))
        asyncio.run(hook.after_run(ctx))
        service.log_event.assert_called_once()
        event = service.log_event.call_args[0][0]
        assert event.event_type == "run_finished"
        assert event.summary == "hello"
        assert event.payload["tools_used"] == ["read", "write"]
        assert event.session_id == "cli:1"
        assert event.request_id == "m1"
        assert event.payload["request_id"] == "m1"
        service.finish_request.assert_called_once_with(
            "m1", status="finished", summary="hello", response="hello"
        )
        service.clear_request.assert_called_once_with("cli:1")

    def test_before_execute_tool_captures_session_key(self, sys_path):
        from lib.hooks.database_logging_hook import DatabaseLoggingHook

        service = MagicMock()
        service.get_request_id.return_value = "m1"
        hook = DatabaseLoggingHook(service)
        ctx = MagicMock()
        ctx.session_key = "cli:1"
        tool_call = MagicMock()
        tool_call.id = "tc1"
        tool_call.name = "read"
        tool = MagicMock()
        asyncio.run(hook.before_execute_tool(ctx, tool_call, tool, {}))
        assert hook._run_session_key == "cli:1"
        assert hook._request_id == "m1"


class TestDatabaseLoggingHookFactory:
    def _factory(self, service):
        from lib.hooks.database_logging_hook import make_db_logging_hook_factory

        return make_db_logging_hook_factory(service, agent_id="agent-9")

    def _turn(self, session_key):
        from types import SimpleNamespace

        return SimpleNamespace(session_key=session_key)

    def test_creates_fresh_instance_per_turn(self, sys_path):
        from lib.hooks.database_logging_hook import DatabaseLoggingHook

        service = MagicMock()
        service.get_request_id.side_effect = lambda sk: {"cli:1": "m1", "cli:2": "m2"}.get(sk)
        factory = self._factory(service)

        hook_a = factory(self._turn("cli:1"))
        hook_b = factory(self._turn("cli:2"))

        assert isinstance(hook_a, DatabaseLoggingHook)
        assert hook_a is not hook_b
        assert hook_a._request_id == "m1"
        assert hook_b._request_id == "m2"
        assert hook_a._run_session_key == "cli:1"
        assert hook_b._run_session_key == "cli:2"
        assert hook_a._agent_id == "agent-9"

    def test_instance_without_session_key_has_no_context(self, sys_path):
        service = MagicMock()
        service.get_request_id.return_value = None
        factory = self._factory(service)
        hook = factory(self._turn(None))
        assert hook._run_session_key is None
        assert hook._request_id is None

    def test_concurrent_sessions_do_not_mix_request_id(self, sys_path):
        """Регрессия: после т.зр. общей shared-инстанса после_execute_tool
        читал self._request_id «по памяти» и мог взять чужой вопрос.

        При per-turn инстансах состояние изолировано — tool_result и
        after_run сессии A не затрагивают сессию B.
        """
        service = MagicMock()
        service.get_request_id.side_effect = lambda sk: {"cli:1": "ridA", "cli:2": "ridB"}.get(sk)
        factory = self._factory(service)

        # Переплетение A↔B: B регистрирует свой контекст (before_execute_tool)
        # ДО того, как A завершает свой tool (после_execute_tool).
        hook_b = factory(self._turn("cli:2"))
        hook_a = factory(self._turn("cli:1"))

        ctx_a = MagicMock()
        ctx_a.session_key = "cli:1"
        tb_a = MagicMock()
        tb_a.id = "tcA"
        tb_a.name = "read"
        tc_b = MagicMock()
        tc_b.id = "tcB"
        tc_b.name = "write"

        asyncio.run(hook_b.before_execute_tool(ctx_a, tc_b, MagicMock(), {}))
        # Теперь B «загрязнил» свой next контекст — но НЕ общий инстанс.
        # A завершает свой вызов: результат должен нести ridA, а не ridB.
        asyncio.run(hook_a.after_execute_tool(ctx_a, tb_a, MagicMock(), {}, "result"))
        service.log_tool_result.assert_called_once()
        kwargs = service.log_tool_result.call_args.kwargs
        assert kwargs["request_id"] == "ridA"

        # after_run A не трогает вопрос B
        run_ctx = MagicMock()
        run_ctx.final_content = "done"
        run_ctx.tools_used = ["read"]
        run_ctx.stop_reason = "stop"
        run_ctx.had_injections = False
        run_ctx.error = None
        run_ctx.usage = {"total_tokens": 1}

        asyncio.run(hook_a.after_run(run_ctx))
        service.finish_request.assert_called_once_with(
            "ridA", status="finished", summary="done", response="done"
        )
        service.clear_request.assert_called_once_with("cli:1")


class TestBusLoggers:
    def test_inbound_logger(self):
        from lib.services.db_logging_bus import make_inbound_logger

        service = MagicMock()
        logger = make_inbound_logger(service)
        msg = MagicMock()
        msg.session_key = "cli:42"
        msg.channel = "cli"
        msg.content = "hi"
        msg.metadata = {"message_id": "m1"}
        msg.sender_id = "u1"
        msg.chat_id = "c1"
        msg.media = []
        asyncio.run(logger(msg))
        service.register_request.assert_called_once_with(
            "cli:42", "m1", user_id="u1", chat_id="c1", channel="cli",
            agent_id=None, question="hi", media=None,
        )
        service.log_inbound.assert_called_once_with(
            session_id="cli:42", channel="cli", content="hi",
            message_id="m1", sender_id="u1", chat_id="c1",
            request_id="m1", media=None,
        )

    def test_inbound_logger_with_media(self):
        from lib.services.db_logging_bus import make_inbound_logger

        service = MagicMock()
        logger = make_inbound_logger(service)
        msg = MagicMock()
        msg.session_key = "cli:42"
        msg.channel = "cli"
        msg.content = "см. файл"
        msg.metadata = {"message_id": "m1"}
        msg.sender_id = "u1"
        msg.chat_id = "c1"
        msg.media = ["doc.pdf", ""]
        asyncio.run(logger(msg))
        kwargs = service.register_request.call_args.kwargs
        assert kwargs["question"] == "см. файл"
        expected = [{
            "filename": "doc.pdf",
            "file_id": "doc.pdf",
            "mime_type": "",
            "file_size": 0,
        }]
        assert kwargs["media"] == expected
        kwargs = service.log_inbound.call_args.kwargs
        assert kwargs["media"] == expected

    def test_outbound_logger_drops_reasoning(self):
        from lib.services.db_logging_bus import make_outbound_logger

        service = MagicMock()
        logger = make_outbound_logger(service)
        msg = MagicMock()
        msg.channel = "cli"
        msg.content = "ignored"
        msg.metadata = {"_reasoning_delta": True}
        asyncio.run(logger(msg))
        service.log_outbound.assert_not_called()

    def test_outbound_logger_final(self):
        from lib.services.db_logging_bus import make_outbound_logger

        service = MagicMock()
        service.get_request_id.return_value = "m1"
        logger = make_outbound_logger(service)
        msg = MagicMock()
        msg.channel = "cli"
        msg.chat_id = "42"
        msg.content = "final answer"
        msg.metadata = {"message_id": "m1"}
        msg.media = []
        asyncio.run(logger(msg))
        service.log_outbound.assert_called_once()
        kwargs = service.log_outbound.call_args.kwargs
        assert kwargs["kind"] == "outbound_final"
        assert kwargs["content"] == "final answer"
        assert kwargs["session_id"] == "cli:42"
        assert kwargs["request_id"] == "m1"
        assert kwargs["media"] is None

    def test_outbound_logger_with_media(self):
        from lib.services.db_logging_bus import make_outbound_logger

        service = MagicMock()
        service.get_request_id.return_value = "m1"
        logger = make_outbound_logger(service)
        msg = MagicMock()
        msg.channel = "cli"
        msg.chat_id = "42"
        msg.content = "final answer"
        msg.metadata = {"message_id": "m1"}
        msg.media = ["https://example.com/out.png"]
        asyncio.run(logger(msg))
        kwargs = service.log_outbound.call_args.kwargs
        assert kwargs["media"] == [{
            "filename": "",
            "file_id": "https://example.com/out.png",
            "mime_type": "",
            "file_size": 0,
        }]
