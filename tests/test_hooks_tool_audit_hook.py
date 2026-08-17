from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


@pytest.fixture(autouse=True)
def mock_nanobot_agent():
    """Mock nanobot.agent before importing tool_audit_hook."""
    with patch.dict("sys.modules"):
        import types

        nanobot = types.ModuleType("nanobot")
        nanobot.agent = types.ModuleType("nanobot.agent")
        nanobot.agent.AgentHook = type("AgentHook", (), {"__init__": lambda self: None})
        nanobot.agent.AgentHookContext = MagicMock()
        sys.modules["nanobot"] = nanobot
        sys.modules["nanobot.agent"] = nanobot.agent

        from workspace.hooks.tool_audit_hook import ToolAuditHook, format_tool_params

        yield {"ToolAuditHook": ToolAuditHook, "format_tool_params": format_tool_params}


def make_tc(name, arguments=None, args_str=None):
    tc = MagicMock()
    tc.name = name
    if arguments is not None:
        tc.arguments = arguments
    else:
        tc.arguments = args_str or "{}"
    return tc


def make_ev(status="ok", detail=""):
    return {"status": status, "detail": detail}


class TestToolAuditHookInit:
    def test_empty_state(self, mock_nanobot_agent):
        hook = mock_nanobot_agent["ToolAuditHook"]()
        assert hook._entries == {}
        assert hook._calls == {}
        assert hook._pending_start == {}


class TestBeforeExecuteTools:
    def test_adds_entries(self, mock_nanobot_agent):
        hook = mock_nanobot_agent["ToolAuditHook"]()
        ctx = MagicMock()
        ctx.tool_calls = [make_tc("read_file", arguments={"path": "/tmp/f"}),
                          make_tc("write_file", arguments={"content": "hi"})]
        ctx.iteration = 1

        import asyncio
        asyncio.run(hook.before_execute_tools(ctx))

        # ctx без session_key → bucket ""
        assert len(hook._entries[""]) == 2
        assert hook._entries[""][0]["name"] == "read_file"
        assert hook._entries[""][0]["status"] == "started"
        assert hook._entries[""][0]["iteration"] == 1
        assert hook._entries[""][1]["name"] == "write_file"

    def test_captures_calls(self, mock_nanobot_agent):
        hook = mock_nanobot_agent["ToolAuditHook"]()
        ctx = MagicMock()
        ctx.tool_calls = [make_tc("read", arguments={"path": "/x"})]
        ctx.iteration = 0

        import asyncio
        asyncio.run(hook.before_execute_tools(ctx))

        assert len(hook._calls[""]) == 1
        assert hook._calls[""][0]["name"] == "read"

    def test_empty_tool_calls(self, mock_nanobot_agent):
        hook = mock_nanobot_agent["ToolAuditHook"]()
        ctx = MagicMock()
        ctx.tool_calls = []
        ctx.iteration = 0

        import asyncio
        asyncio.run(hook.before_execute_tools(ctx))

        assert hook._entries[""] == []


class TestAfterIteration:
    def test_updates_status(self, mock_nanobot_agent):
        hook = mock_nanobot_agent["ToolAuditHook"]()
        ctx = MagicMock()
        ctx.tool_calls = [make_tc("read")]
        ctx.iteration = 0

        import asyncio
        asyncio.run(hook.before_execute_tools(ctx))

        ctx.tool_events = [make_ev("ok", "file content")]
        asyncio.run(hook.after_iteration(ctx))

        assert hook._entries[""][0]["status"] == "ok"
        assert hook._entries[""][0]["result_preview"] == "file content"

    def test_sets_error(self, mock_nanobot_agent):
        hook = mock_nanobot_agent["ToolAuditHook"]()
        ctx = MagicMock()
        ctx.tool_calls = [make_tc("bad_tool")]
        ctx.iteration = 0

        import asyncio
        asyncio.run(hook.before_execute_tools(ctx))

        ctx.tool_events = [make_ev("error", "permission denied")]
        asyncio.run(hook.after_iteration(ctx))

        assert hook._entries[""][0]["status"] == "error"
        assert hook._entries[""][0]["error"] == "permission denied"

    def test_truncates_long_preview(self, mock_nanobot_agent):
        hook = mock_nanobot_agent["ToolAuditHook"]()
        ctx = MagicMock()
        ctx.tool_calls = [make_tc("read")]
        ctx.iteration = 0

        import asyncio
        asyncio.run(hook.before_execute_tools(ctx))

        long_detail = "x" * 500
        ctx.tool_events = [make_ev("ok", long_detail)]
        asyncio.run(hook.after_iteration(ctx))

        assert len(hook._entries[""][0]["result_preview"]) == 200

    def test_no_tool_events(self, mock_nanobot_agent):
        hook = mock_nanobot_agent["ToolAuditHook"]()
        ctx = MagicMock()
        ctx.tool_calls = [make_tc("read")]
        ctx.iteration = 0

        import asyncio
        asyncio.run(hook.before_execute_tools(ctx))

        ctx.tool_events = []
        asyncio.run(hook.after_iteration(ctx))

        assert hook._entries[""][0]["status"] == "started"


class TestDrain:
    def test_returns_and_clears_entries(self, mock_nanobot_agent):
        hook = mock_nanobot_agent["ToolAuditHook"]()
        hook._entries = {"": [{"name": "t1"}]}
        entries = hook.drain()
        assert entries == [{"name": "t1"}]
        assert hook._entries == {}

    def test_drain_empty(self, mock_nanobot_agent):
        hook = mock_nanobot_agent["ToolAuditHook"]()
        entries = hook.drain()
        assert entries == []

    def test_drain_by_session(self, mock_nanobot_agent):
        hook = mock_nanobot_agent["ToolAuditHook"]()
        hook._entries = {"s1": [{"name": "a"}], "s2": [{"name": "b"}]}
        assert hook.drain("s1") == [{"name": "a"}]
        # s2 не затронута чужим дренажом
        assert hook.drain("s2") == [{"name": "b"}]
        assert hook._entries == {}


class TestDrainCalls:
    def test_returns_and_clears_calls(self, mock_nanobot_agent):
        hook = mock_nanobot_agent["ToolAuditHook"]()
        hook._calls = {"": [{"name": "t1"}]}
        calls = hook.drain_calls()
        assert calls == [{"name": "t1"}]
        assert hook._calls == {}

    def test_drain_calls_empty(self, mock_nanobot_agent):
        hook = mock_nanobot_agent["ToolAuditHook"]()
        calls = hook.drain_calls()
        assert calls == []


class TestFullCycle:
    def test_before_after_drain(self, mock_nanobot_agent):
        hook = mock_nanobot_agent["ToolAuditHook"]()
        ctx = MagicMock()
        ctx.tool_calls = [make_tc("search", arguments={"q": "hello"})]
        ctx.iteration = 0

        import asyncio
        asyncio.run(hook.before_execute_tools(ctx))

        ctx.tool_events = [make_ev("ok", "found 3 results")]
        asyncio.run(hook.after_iteration(ctx))

        entries = hook.drain()
        assert len(entries) == 1
        assert entries[0]["name"] == "search"
        assert entries[0]["status"] == "ok"
        assert entries[0]["result_preview"] == "found 3 results"

        calls = hook.drain_calls()
        assert len(calls) == 1
        assert calls[0]["name"] == "search"


class TestConcurrentSessionsIsolated:
    """Регрессионный тест: конкурентные вопросы (разные сессии) не путают
    ``_tool_audit`` — каждый дренируется отдельно."""

    def test_drain_isolates_sessions(self, mock_nanobot_agent):
        hook = mock_nanobot_agent["ToolAuditHook"]()
        import asyncio

        ctx_a = MagicMock()
        ctx_a.session_key = "telegram:1"
        ctx_a.tool_calls = [make_tc("read", arguments={"path": "/a"})]
        ctx_a.iteration = 0

        ctx_b = MagicMock()
        ctx_b.session_key = "telegram:2"
        ctx_b.tool_calls = [make_tc("write", arguments={"content": "b"})]
        ctx_b.iteration = 0

        asyncio.run(hook.before_execute_tools(ctx_a))
        asyncio.run(hook.before_execute_tools(ctx_b))

        # каждый оборот видит только свои вызовы
        assert len(hook.drain("telegram:1")) == 1
        assert hook.drain("telegram:1") == []
        assert hook.drain("telegram:2")[0]["name"] == "write"
        assert hook.drain("telegram:2") == []

    def test_after_iteration_updates_only_own_session(self, mock_nanobot_agent):
        hook = mock_nanobot_agent["ToolAuditHook"]()
        import asyncio

        ctx_a = MagicMock()
        ctx_a.session_key = "s1"
        ctx_a.tool_calls = [make_tc("read")]
        ctx_a.iteration = 0

        ctx_b = MagicMock()
        ctx_b.session_key = "s2"
        ctx_b.tool_calls = [make_tc("write")]
        ctx_b.iteration = 1

        asyncio.run(hook.before_execute_tools(ctx_a))
        asyncio.run(hook.before_execute_tools(ctx_b))

        ctx_a.tool_events = [make_ev("ok", "A result")]
        ctx_b.tool_events = [make_ev("ok", "B result")]

        asyncio.run(hook.after_iteration(ctx_a))
        asyncio.run(hook.after_iteration(ctx_b))

        entries_a = hook.drain("s1")
        entries_b = hook.drain("s2")
        assert entries_a[0]["result_preview"] == "A result"
        assert entries_b[0]["result_preview"] == "B result"
        assert entries_a[0]["iteration"] == 0
        assert entries_b[0]["iteration"] == 1


class TestFormatToolParams:
    def test_formats_simple_params(self, mock_nanobot_agent):
        result = mock_nanobot_agent["format_tool_params"]([
            {"name": "read", "arguments": '{"path": "/tmp/f"}'}
        ])
        assert "read" in result
        assert '/tmp/f' in result["read"]

    def test_handles_invalid_json(self, mock_nanobot_agent):
        result = mock_nanobot_agent["format_tool_params"]([
            {"name": "bad", "arguments": "{invalid}"}
        ])
        assert "bad" in result

    def test_handles_list_args(self, mock_nanobot_agent):
        result = mock_nanobot_agent["format_tool_params"]([
            {"name": "multi", "arguments": '{"items": [1, 2, 3]}'}
        ])
        assert "multi" in result
        assert "1, 2, 3" in result["multi"]

    def test_multiple_tools(self, mock_nanobot_agent):
        result = mock_nanobot_agent["format_tool_params"]([
            {"name": "a", "arguments": '{"x": 1}'},
            {"name": "b", "arguments": '{"y": 2}'},
        ])
        assert "a" in result and "b" in result

    def test_empty_params(self, mock_nanobot_agent):
        result = mock_nanobot_agent["format_tool_params"]([])
        assert result == {}
