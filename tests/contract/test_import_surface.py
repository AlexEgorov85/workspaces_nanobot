"""Импорт-поверхность nanobot, на которую опирается проект."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.contract


def test_agent_reexports() -> None:
    from nanobot import agent

    for name in ("AgentHook", "AgentHookContext", "AgentRunHookContext"):
        assert hasattr(agent, name), f"nanobot.agent.{name} missing"


def test_core_modules_importable() -> None:
    from nanobot.agent.autocompact import AutoCompact
    from nanobot.agent.hook import AgentHook, CompositeHook
    from nanobot.agent.loop import AgentLoop
    from nanobot.agent.memory import Consolidator
    from nanobot.agent.subagent import _SubagentHook
    from nanobot.bus.queue import MessageBus
    from nanobot.channels.base import BaseChannel
    from nanobot.command.router import CommandContext, CommandRouter
    from nanobot.config.schema import AgentDefaults, Config, ToolsConfig
    from nanobot.session.manager import Session, SessionManager
    from nanobot.utils.prompt_templates import _environment

    for obj in (
        AgentLoop,
        AgentHook,
        CompositeHook,
        AutoCompact,
        Consolidator,
        _SubagentHook,
        MessageBus,
        SessionManager,
        Session,
        BaseChannel,
        CommandRouter,
        CommandContext,
        Config,
        AgentDefaults,
        ToolsConfig,
        _environment,
    ):
        assert obj is not None


def test_tools_module_imports() -> None:
    from nanobot.agent.tools.base import Tool, ToolResult
    from nanobot.agent.tools.context import RequestContext, ToolContext

    assert Tool is not None
    assert ToolResult is not None
    assert ToolContext is not None
    assert RequestContext is not None


def test_bus_events() -> None:
    from nanobot.bus.events import InboundMessage, OutboundMessage

    for field in ("channel", "sender_id", "chat_id", "content"):
        assert field in InboundMessage.__dataclass_fields__
    for field in ("channel", "chat_id", "content"):
        assert field in OutboundMessage.__dataclass_fields__
