"""Тесты ApplicationContext — с обильным мокингом nanobot/psycopg2/PGSessionManager."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def full_fake_modules(tmp_path):
    """Подменяем ВСЕ модули, от которых зависит ApplicationContext.create()."""
    with patch.dict("sys.modules"):

        # nanobot.agent
        sol = types.ModuleType("nanobot")
        sol.agent = types.ModuleType("nanobot.agent")
        loop = types.ModuleType("nanobot.agent.loop")
        hook = types.ModuleType("nanobot.agent.hook")
        hook.AgentHook = type("AgentHook", (), {})
        hook.AgentHookContext = type("AgentHookContext", (), {})
        hook.AgentRunHookContext = type("AgentRunHookContext", (), {})
        agent_instance = MagicMock()
        loop.AgentLoop = MagicMock()
        loop.AgentLoop.from_config = MagicMock(return_value=agent_instance)
        sys.modules["nanobot"] = sol
        sys.modules["nanobot.agent"] = sol.agent
        sys.modules["nanobot.agent.loop"] = loop
        sys.modules["nanobot.agent.hook"] = hook

        # nanobot.bus
        sol.bus = types.ModuleType("nanobot.bus")
        bus = types.ModuleType("nanobot.bus.queue")
        bus.MessageBus = MagicMock()
        sys.modules["nanobot.bus"] = sol.bus
        sys.modules["nanobot.bus.queue"] = bus

        # nanobot.channels
        sol.channels = types.ModuleType("nanobot.channels")
        cm = types.ModuleType("nanobot.channels.manager")
        cm.ChannelManager = MagicMock()
        sys.modules["nanobot.channels"] = sol.channels
        sys.modules["nanobot.channels.manager"] = cm

        # nanobot.utils
        sol.utils = types.ModuleType("nanobot.utils")
        helpers = types.ModuleType("nanobot.utils.helpers")
        helpers.sync_workspace_templates = MagicMock()
        sys.modules["nanobot.utils"] = sol.utils
        sys.modules["nanobot.utils.helpers"] = helpers

        # nanobot.cli
        sol.cli = types.ModuleType("nanobot.cli")
        commands = types.ModuleType("nanobot.cli.commands")
        runtime_config = MagicMock()
        runtime_config.workspace_path = tmp_path
        runtime_config.providers.openai.api_key = None
        runtime_config.providers.groq.api_key = None
        runtime_config.providers.openai.api_base = None
        runtime_config.providers.groq.api_base = None
        runtime_config.channels.send_progress = True
        runtime_config.channels.send_tool_hints = False
        runtime_config.channels.show_reasoning = True
        runtime_config.channels.transcription_provider = "groq"
        runtime_config.channels.transcription_language = None
        runtime_config.agents.defaults.max_tool_iterations = 200
        runtime_config.tools.exec.timeout = 60
        commands._load_runtime_config = MagicMock(return_value=runtime_config)
        sys.modules["nanobot.cli"] = sol.cli
        sys.modules["nanobot.cli.commands"] = commands

        # nanobot.cron
        sol.cron = types.ModuleType("nanobot.cron")
        cron_svc = types.ModuleType("nanobot.cron.service")
        cron_svc.CronService = MagicMock()
        sys.modules["nanobot.cron"] = sol.cron
        sys.modules["nanobot.cron.service"] = cron_svc

        # nanobot.session
        sol.session = types.ModuleType("nanobot.session")
        sm = types.ModuleType("nanobot.session.manager")
        sm.SessionManager = MagicMock()
        sys.modules["nanobot.session"] = sol.session
        sys.modules["nanobot.session.manager"] = sm

        # config (SETTINGS)
        cfg_mod = types.ModuleType("config")
        settings = MagicMock()
        settings.gateway = MagicMock()
        settings.gateway.storage = "file"
        settings.gateway.persist_threshold = 0
        settings.gateway.llm_timeout = -1
        settings.gateway.exec_timeout = -1
        settings.channels = {"postgres": {"dsn": ""}, "redis": {"enabled": False}}
        settings.skills = MagicMock()
        settings.skills.audit_analyzer = MagicMock()
        settings.skills.audit_analyzer.get = MagicMock(return_value=False)
        settings.cli = {}
        settings.providers = MagicMock()
        cfg_mod.SETTINGS = settings
        sys.modules["config"] = cfg_mod

        # workspace
        ws = str(Path(__file__).resolve().parent.parent / "workspace")
        if ws not in sys.path:
            sys.path.insert(0, ws)
        hooks = types.ModuleType("hooks")
        hooks.__path__ = []
        tah = types.ModuleType("hooks.tool_audit_hook")

        class _ToolAuditHook:
            def __init__(self):
                self.drained = []

        tah.ToolAuditHook = _ToolAuditHook
        sys.modules["hooks"] = hooks
        sys.modules["hooks.tool_audit_hook"] = tah

        # lib.services
        for name in [
            "lib.session.pg_session_manager",
            "lib.channels.redis_channel",
            "lib.channels.postgres_channel",
        ]:
            m = types.ModuleType(name)
            sys.modules[name] = m

        # utils.db
        utils_mod = types.ModuleType("utils")
        utils_db = types.ModuleType("utils.db")
        utils_db.configure = MagicMock()
        utils_mod.db = utils_db
        sys.modules["utils"] = utils_mod
        sys.modules["utils.db"] = utils_db

        # utils.session_file_store
        sfs = types.ModuleType("utils.session_file_store")
        sfs.SessionFileStore = MagicMock()
        sfs.prepare_content = MagicMock()
        sys.modules["utils.session_file_store"] = sfs

        yield {
            "settings": settings,
            "agent_instance": agent_instance,
            "config": runtime_config,
        }


class TestCreate:
    def test_creates_context_with_all_services(self, full_fake_modules):
        from lib.core.application_context import ApplicationContext

        script = Path(__file__).resolve().parent.parent
        ctx = ApplicationContext.create(
            script_dir=script,
            workspace_dir=script / "workspace",
            enable_db_logging=False,
            enable_audit=False,
        )
        assert ctx.config is not None
        assert ctx.settings is not None
        assert ctx.bus is not None
        assert ctx.agent is not None
        assert ctx.hooks
        assert ctx.tool_audit_hook in ctx.hooks
        assert ctx.runtime_patcher is not None
        assert ctx.transcription_service is not None
        assert ctx.preload_service is not None
        assert ctx.db_logging_service is None
        assert ctx.audit_sync_service is None

    def test_storage_file_when_no_dsn(self, full_fake_modules):
        from lib.core.application_context import ApplicationContext

        script = Path(__file__).resolve().parent.parent
        ctx = ApplicationContext.create(
            script_dir=script,
            workspace_dir=script / "workspace",
            enable_db_logging=False,
            enable_audit=False,
        )
        assert ctx.storage_mode == "file"

    def test_storage_override_postgres_without_dsn(self, full_fake_modules):
        from lib.core.application_context import ApplicationContext

        script = Path(__file__).resolve().parent.parent
        full_fake_modules["settings"].gateway.storage = "file"
        # Override, но DSN пуст → SessionStorageService должен упасть,
        # но ApplicationContext делает fallback на "file".
        ctx = ApplicationContext.create(
            script_dir=script,
            workspace_dir=script / "workspace",
            enable_db_logging=False,
            enable_audit=False,
            storage_override="postgres",
        )
        assert ctx.storage_mode == "file"

    def test_enable_cron_creates_cron_service(self, full_fake_modules):
        from lib.core.application_context import ApplicationContext

        script = Path(__file__).resolve().parent.parent
        ctx = ApplicationContext.create(
            script_dir=script,
            workspace_dir=script / "workspace",
            enable_db_logging=False,
            enable_audit=False,
            enable_cron=True,
        )
        # cron_service passed to AgentLoop
        kwargs = __import__("nanobot.agent.loop", fromlist=["AgentLoop"]).AgentLoop.from_config.call_args.kwargs
        assert "cron_service" in kwargs


class TestStartStop:
    def test_start_runs_and_stops(self, full_fake_modules):
        from lib.core.application_context import ApplicationContext

        script = Path(__file__).resolve().parent.parent
        ctx = ApplicationContext.create(
            script_dir=script,
            workspace_dir=script / "workspace",
            enable_db_logging=False,
            enable_audit=False,
        )
        ctx.start()
        ctx.stop()  # не должно упасть

    def test_double_start_is_safe(self, full_fake_modules):
        from lib.core.application_context import ApplicationContext

        script = Path(__file__).resolve().parent.parent
        ctx = ApplicationContext.create(
            script_dir=script,
            workspace_dir=script / "workspace",
            enable_db_logging=False,
            enable_audit=False,
        )
        ctx.start()
        ctx.start()  # no-op
        ctx.stop()

    def test_double_stop_is_safe(self, full_fake_modules):
        from lib.core.application_context import ApplicationContext

        script = Path(__file__).resolve().parent.parent
        ctx = ApplicationContext.create(
            script_dir=script,
            workspace_dir=script / "workspace",
            enable_db_logging=False,
            enable_audit=False,
        )
        ctx.start()
        ctx.stop()
        ctx.stop()  # no-op
