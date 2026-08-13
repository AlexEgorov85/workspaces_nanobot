from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def hook():
    from benchmarks.hooks import BenchmarkHook

    return BenchmarkHook()


def make_context(iteration=0, tool_calls=None, usage=None, tool_events=None):
    ctx = MagicMock()
    ctx.iteration = iteration
    ctx.tool_calls = tool_calls or []
    ctx.usage = usage
    ctx.tool_events = tool_events or []
    return ctx


class TestBenchmarkHookInit:
    def test_defaults(self, hook):
        assert hook.tool_calls == []
        assert hook.iterations == 0
        assert hook.skills == set()
        assert hook.start_time == 0.0
        assert hook.end_time == 0.0
        assert hook.usage == {}
        assert hook.tools_used == []


class TestBenchmarkHookBeforeIteration:
    @pytest.mark.asyncio
    async def test_first_iteration_starts_timer(self, hook):
        ctx = make_context(iteration=0)
        await hook.before_iteration(ctx)
        assert hook.iterations == 1
        assert hook.start_time > 0

    @pytest.mark.asyncio
    async def test_captures_usage(self, hook):
        ctx = make_context(iteration=0, usage={"prompt_tokens": 10, "completion_tokens": 20})
        await hook.before_iteration(ctx)
        assert hook.usage == {"prompt_tokens": 10, "completion_tokens": 20}

    @pytest.mark.asyncio
    async def test_increments_iterations(self, hook):
        await hook.before_iteration(make_context(iteration=0))
        await hook.before_iteration(make_context(iteration=1))
        assert hook.iterations == 2

    @pytest.mark.asyncio
    async def test_does_not_overwrite_start_time(self, hook):
        ctx = make_context(iteration=0)
        await hook.before_iteration(ctx)
        start = hook.start_time
        await hook.before_iteration(ctx)
        assert hook.start_time == start


class TestBenchmarkHookAfterIteration:
    @pytest.mark.asyncio
    async def test_collects_tool_calls(self, hook):
        call = MagicMock()
        call.name = "read_file"
        call.arguments = {"path": "/tmp/x"}
        ctx = make_context(iteration=0, tool_calls=[call])
        await hook.after_iteration(ctx)
        assert len(hook.tool_calls) == 1
        assert hook.tool_calls[0]["name"] == "read_file"
        assert hook.tool_calls[0]["params"] == {"path": "/tmp/x"}
        assert hook.tool_calls[0]["iteration"] == 0

    @pytest.mark.asyncio
    async def test_tracks_tool_names(self, hook):
        call1 = MagicMock()
        call1.name = "read_file"
        call2 = MagicMock()
        call2.name = "write_file"
        ctx = make_context(iteration=0, tool_calls=[call1, call2])
        await hook.after_iteration(ctx)
        assert hook.tools_used == ["read_file", "write_file"]

    @pytest.mark.asyncio
    async def test_deduplicates_tool_names(self, hook):
        call = MagicMock()
        call.name = "read_file"
        ctx = make_context(iteration=0, tool_calls=[call, call])
        await hook.after_iteration(ctx)
        assert hook.tools_used == ["read_file"]

    @pytest.mark.asyncio
    async def test_empty_tool_calls(self, hook):
        ctx = make_context(iteration=0, tool_calls=[])
        await hook.after_iteration(ctx)
        assert hook.tool_calls == []

    @pytest.mark.asyncio
    async def test_records_ok_status(self, hook):
        call = MagicMock()
        call.name = "read_file"
        call.arguments = {"path": "/tmp/x"}
        ctx = make_context(iteration=0, tool_calls=[call],
                           tool_events=[{"status": "ok", "detail": "ok result"}])
        await hook.after_iteration(ctx)
        assert hook.tool_calls[0]["status"] == "ok"

    @pytest.mark.asyncio
    async def test_records_error_status(self, hook):
        call = MagicMock()
        call.name = "bad_tool"
        call.arguments = {}
        ctx = make_context(iteration=0, tool_calls=[call],
                           tool_events=[{"status": "error", "detail": "boom"}])
        await hook.after_iteration(ctx)
        assert hook.tool_calls[0]["status"] == "error"
        assert hook.tool_calls[0]["error"] == "boom"

    @pytest.mark.asyncio
    async def test_defaults_to_unknown_without_events(self, hook):
        call = MagicMock()
        call.name = "read_file"
        call.arguments = {}
        ctx = make_context(iteration=0, tool_calls=[call])
        await hook.after_iteration(ctx)
        assert hook.tool_calls[0]["status"] == "unknown"

    @pytest.mark.asyncio
    async def test_before_execute_tools_tracks_names(self, hook):
        call = MagicMock()
        call.name = "exec"
        call.arguments = {}
        ctx = make_context(iteration=0, tool_calls=[call])
        await hook.before_execute_tools(ctx)
        assert hook.tools_used == ["exec"]


class TestBenchmarkHookFinalizeContent:
    def test_sets_end_time(self, hook):
        ctx = make_context()
        hook.start_time = 100.0
        result = hook.finalize_content(ctx, "hello")
        assert result == "hello"
        assert hook.end_time > 0

    def test_returns_content_unchanged(self, hook):
        result = hook.finalize_content(make_context(), "world")
        assert result == "world"

    def test_handles_none_content(self, hook):
        result = hook.finalize_content(make_context(), None)
        assert result is None


class TestBenchmarkHookProperties:
    def test_duration_sec_no_start(self, hook):
        assert hook.duration_sec == 0.0

    def test_duration_sec_no_end(self, hook):
        hook.start_time = 100.0
        assert hook.duration_sec == 0.0

    def test_duration_sec_complete(self, hook):
        hook.start_time = 100.0
        hook.end_time = 105.0
        assert hook.duration_sec == 5.0

    def test_tools_used_empty(self, hook):
        assert hook.tools_used == []

    def test_tools_used_sorted(self, hook):
        hook._tool_names = {"z_tool", "a_tool"}
        assert hook.tools_used == ["a_tool", "z_tool"]
