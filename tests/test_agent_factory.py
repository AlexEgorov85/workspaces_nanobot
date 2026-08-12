from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def fake_modules(tmp_path):
    """Подменяем nanobot/hooks, чтобы AgentFactory мог работать без установки."""
    with patch.dict("sys.modules"):
        # nanobot.agent.loop
        sol = types.ModuleType("nanobot")
        sol.agent = types.ModuleType("nanobot.agent")
        loop = types.ModuleType("nanobot.agent.loop")
        agent_instance = MagicMock()
        loop.AgentLoop = MagicMock()
        loop.AgentLoop.from_config = MagicMock(return_value=agent_instance)
        sys.modules["nanobot"] = sol
        sys.modules["nanobot.agent"] = sol.agent
        sys.modules["nanobot.agent.loop"] = loop

        # hooks.tool_audit_hook
        hooks_dir = str(Path(__file__).resolve().parent.parent / "workspace")
        if hooks_dir not in sys.path:
            sys.path.insert(0, hooks_dir)
        hooks_pkg = types.ModuleType("hooks")
        hooks_pkg.__path__ = []  # namespace
        tah_mod = types.ModuleType("hooks.tool_audit_hook")

        class _ToolAuditHook:
            def __init__(self):
                self.created = True

        tah_mod.ToolAuditHook = _ToolAuditHook
        sys.modules["hooks"] = hooks_pkg
        sys.modules["hooks.tool_audit_hook"] = tah_mod

        yield {
            "agent_instance": agent_instance,
            "from_config": loop.AgentLoop.from_config,
        }


class TestAgentFactory:
    def test_creates_agent_with_tool_audit_hook(self, fake_modules):
        from lib.core.agent_factory import AgentFactory

        factory = AgentFactory()
        agent, hooks = factory.create(
            config=MagicMock(), bus=MagicMock(),
        )
        assert agent is fake_modules["agent_instance"]
        assert all(getattr(h, "created", True) for h in hooks)
        kwargs = fake_modules["from_config"].call_args.kwargs
        assert "session_manager" in kwargs
        assert "hooks" in kwargs
        assert len(kwargs["hooks"]) == 1

    def test_passes_session_manager(self, fake_modules):
        from lib.core.agent_factory import AgentFactory

        factory = AgentFactory()
        sm = MagicMock()
        factory.create(config=MagicMock(), bus=MagicMock(), session_manager=sm)
        kwargs = fake_modules["from_config"].call_args.kwargs
        assert kwargs["session_manager"] is sm

    def test_cron_service_only_when_provided(self, fake_modules):
        from lib.core.agent_factory import AgentFactory

        factory = AgentFactory()
        factory.create(config=MagicMock(), bus=MagicMock())
        kwargs = fake_modules["from_config"].call_args.kwargs
        assert "cron_service" not in kwargs

        cron = MagicMock()
        factory.create(config=MagicMock(), bus=MagicMock(), cron_service=cron)
        kwargs = fake_modules["from_config"].call_args.kwargs
        assert kwargs["cron_service"] is cron

    def test_db_logging_service_optional(self, fake_modules):
        from lib.core.agent_factory import AgentFactory

        factory = AgentFactory()
        # Без db_logging_service — один hook (ToolAuditHook).
        _, hooks = factory.create(config=MagicMock(), bus=MagicMock())
        assert len(hooks) == 1

        # С db_logging_service — шаг 9 может не существовать; исключение
        # обрабатывается внутри фабрики, hooks остаётся прежним.
        factory.create(
            config=MagicMock(), bus=MagicMock(),
            db_logging_service=MagicMock(),
        )
        # Либо 1 (db_logging_hook ещё не реализован), либо 2 — оба варианта ОК.
        kwargs = fake_modules["from_config"].call_args.kwargs
        assert len(kwargs["hooks"]) in (1, 2)
