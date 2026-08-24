"""ToolContext/RequestContext/Tool/_SubagentHook/prompt_templates."""

from __future__ import annotations

from dataclasses import fields

import pytest

pytestmark = pytest.mark.contract


def test_tool_context_fields() -> None:
    from nanobot.agent.tools.context import ToolContext

    names = {f.name for f in fields(ToolContext)}
    for expected in (
        "config",
        "workspace",
        "bus",
        "subagent_manager",
        "cron_service",
        "exec_session_manager",
        "sessions",
        "image_generation_provider_configs",
        "timezone",
    ):
        assert expected in names, f"ToolContext.{expected} missing"


def test_request_context_fields() -> None:
    from nanobot.agent.tools.context import RequestContext

    names = {f.name for f in fields(RequestContext)}
    for expected in ("channel", "chat_id", "message_id", "session_key", "runtime"):
        assert expected in names, f"RequestContext.{expected} missing"


def test_subagent_hook_surface() -> None:
    from nanobot.agent.subagent import _SubagentHook

    hook = _SubagentHook
    for name in ("before_execute_tools", "after_iteration"):
        assert callable(getattr(hook, name, None)), f"_SubagentHook.{name} missing"


def test_tool_base_contract() -> None:
    from nanobot.agent.tools.base import Tool

    for name in ("name", "description", "parameters"):
        prop = getattr(Tool, name, None)
        assert isinstance(prop, property), f"Tool.{name} must be a property"
    assert hasattr(Tool, "execute")
    assert hasattr(Tool, "config_key")


def test_prompt_templates_environment() -> None:
    from nanobot.utils import prompt_templates as pt

    env = pt._environment()
    assert hasattr(env, "get_template")
    assert hasattr(env, "loader"), "Jinja2 Environment must expose .loader (consolidator_locale)"
