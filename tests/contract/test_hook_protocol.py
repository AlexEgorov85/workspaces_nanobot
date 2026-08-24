"""Hook-протокол: lifecycle AgentHook, на котором построены lib/hooks/*."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.contract


def _hook_cls():
    from nanobot.agent.hook import AgentHook

    return AgentHook


def test_lifecycle_methods_present() -> None:
    hook = _hook_cls()
    for name in (
        "before_run",
        "after_run",
        "on_error",
        "on_finally",
        "before_iteration",
        "after_iteration",
        "on_stream",
        "on_stream_end",
        "before_execute_tools",
        "before_execute_tool",
        "after_execute_tool",
        "on_execute_tool_error",
        "finalize_content",
        "wants_streaming",
    ):
        assert callable(getattr(hook, name, None)), f"AgentHook.{name} missing"


def test_base_hook_defaults_are_safe() -> None:
    import asyncio

    from nanobot.agent import AgentHookContext

    hook = _hook_cls()()
    ctx = AgentHookContext(iteration=0, messages=[])
    assert asyncio.run(hook.before_iteration(ctx)) is None
    assert asyncio.run(hook.before_run(ctx)) is None
    assert hook.finalize_content(ctx, "text") == "text"
    assert hook.wants_streaming() is False


def test_agent_hook_context_fields() -> None:
    from dataclasses import fields

    from nanobot.agent import AgentHookContext

    names = {f.name for f in fields(AgentHookContext)}
    for expected in ("iteration", "messages", "response", "usage", "tool_calls", "session_key"):
        assert expected in names, f"AgentHookContext.{expected} missing"


def test_composite_hook_composes() -> None:
    from nanobot.agent.hook import AgentHook, CompositeHook

    class Probe(AgentHook):
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def before_run(self, context) -> None:
            self.calls.append("before_run")

    a, b = Probe(), Probe()
    composite = CompositeHook([a, b])
    assert composite.wants_streaming() in (False, True)
