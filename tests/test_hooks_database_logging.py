from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def sys_path():
    import sys
    from pathlib import Path
    p = str(Path(__file__).resolve().parent.parent / "workspace")
    if p not in sys.path:
        sys.path.insert(0, p)
    return p


class TestDatabaseLoggingHook:
    def test_before_execute_tool(self, sys_path):
        from workspace.hooks.database_logging_hook import DatabaseLoggingHook

        service = MagicMock()
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

    def test_after_execute_tool_records_latency(self, sys_path):
        from workspace.hooks.database_logging_hook import DatabaseLoggingHook

        service = MagicMock()
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
            "workspace.hooks.database_logging_hook.time.time", return_value=0.1
        ):
            asyncio.run(hook.after_execute_tool(ctx, tool_call, tool, params, "ok"))

        service.log_tool_result.assert_called_once()
        kwargs = service.log_tool_result.call_args.kwargs
        assert kwargs["latency_ms"] == pytest.approx(100.0)
        assert kwargs["status"] == "ok"

    def test_on_execute_tool_error(self, sys_path):
        from workspace.hooks.database_logging_hook import DatabaseLoggingHook

        service = MagicMock()
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
        from workspace.hooks.database_logging_hook import DatabaseLoggingHook

        service = MagicMock()
        hook = DatabaseLoggingHook(service)
        ctx = MagicMock()
        ctx.final_content = "hello"
        ctx.tools_used = ["read", "write"]
        ctx.stop_reason = "stop"
        ctx.had_injections = False
        ctx.error = None
        ctx.usage = {"total_tokens": 123}

        asyncio.run(hook.after_run(ctx))
        service.log_event.assert_called_once()
        event = service.log_event.call_args[0][0]
        assert event.event_type == "run_finished"
        assert event.summary == "hello"
        assert event.payload["tools_used"] == ["read", "write"]


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
        asyncio.run(logger(msg))
        service.log_inbound.assert_called_once_with(
            session_id="cli:42", channel="cli", content="hi",
            message_id="m1",
        )

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
        logger = make_outbound_logger(service)
        msg = MagicMock()
        msg.channel = "cli"
        msg.content = "final answer"
        msg.metadata = {}
        asyncio.run(logger(msg))
        service.log_outbound.assert_called_once()
        kwargs = service.log_outbound.call_args.kwargs
        assert kwargs["kind"] == "outbound_final"
        assert kwargs["content"] == "final answer"
