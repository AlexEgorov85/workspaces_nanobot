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

        class _AgentHook:
            def __init__(self, reraise=False):
                self._reraise = reraise

        sol.agent.AgentHook = _AgentHook
        # workspace.hooks.database_logging_hook импортирует эти имена —
        # фейковый nanobot.agent должен их предоставлять, чтобы фабрика
        # оборота реально построилась (иначе import вернёт None).
        sol.agent.AgentHookContext = types.SimpleNamespace
        sol.agent.AgentRunHookContext = types.SimpleNamespace

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
        agent, hooks, hook_factories = factory.create(
            config=MagicMock(), bus=MagicMock(),
        )
        assert agent is fake_modules["agent_instance"]
        assert all(getattr(h, "created", True) for h in hooks)
        assert hook_factories == []
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
        # Без db_logging_service — один hook (ToolAuditHook), без фабрик.
        _, hooks, hook_factories = factory.create(
            config=MagicMock(), bus=MagicMock(),
        )
        assert len(hooks) == 1
        assert hook_factories == []
        kwargs = fake_modules["from_config"].call_args.kwargs
        assert kwargs["hook_factories"] == []

        # С db_logging_service — фабрика оборота идёт в hook_factories,
        # а не как общий инстанс в hooks (набор hooks не меняется).
        factory.create(
            config=MagicMock(), bus=MagicMock(),
            db_logging_service=MagicMock(),
        )
        kwargs = fake_modules["from_config"].call_args.kwargs
        assert len(kwargs["hooks"]) == 1
        assert len(kwargs["hook_factories"]) == 1

    def test_db_logging_factory_creates_per_turn_hook(self, fake_modules):
        from lib.core.agent_factory import AgentFactory

        from workspace.hooks.database_logging_hook import DatabaseLoggingHook

        service = MagicMock()
        # get_request_id возвращает request_id по session_key
        def fake_get(sk):
            return {"cli:1": "m1", "cli:2": "m2"}.get(sk)

        service.get_request_id.side_effect = fake_get

        factory = AgentFactory()
        _, hooks, hook_factories = factory.create(
            config=MagicMock(), bus=MagicMock(),
            db_logging_service=service, agent_id="agent-7",
        )
        # hooks содержит только ToolAuditHook
        assert len(hooks) == 1
        assert len(hook_factories) == 1

        kwargs = fake_modules["from_config"].call_args.kwargs
        assert len(kwargs["hook_factories"]) == 1
        factory_fn = kwargs["hook_factories"][0]

        from types import SimpleNamespace

        hook_a = factory_fn(SimpleNamespace(session_key="cli:1"))
        hook_b = factory_fn(SimpleNamespace(session_key="cli:2"))

        # Разные обороты -> разные инстансы (не разделяют состояние)
        assert hook_a is not hook_b
        assert isinstance(hook_a, DatabaseLoggingHook)

        # Каждый запекает свой контекст вопроса
        assert hook_a._request_id == "m1"
        assert hook_b._request_id == "m2"
        assert hook_a._run_session_key == "cli:1"
        assert hook_b._run_session_key == "cli:2"
        assert hook_a._agent_id == "agent-7"
