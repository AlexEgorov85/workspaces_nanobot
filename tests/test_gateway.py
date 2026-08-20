from __future__ import annotations

import asyncio
import re
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_project_root = Path(__file__).resolve().parent.parent
_workspace_path = str(_project_root / "workspace")
if _workspace_path not in sys.path:
    sys.path.insert(0, _workspace_path)
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


@pytest.fixture(autouse=True)
def mock_all():
    """Mock nanobot/agent/hooks/bus/channels and freeze sys.modules before any
    gateway import (gateway.py импортирует ApplicationContext, который подтягивает
    nanobot.bus.queue / nanobot.agent.loop)."""
    with patch.dict("sys.modules"):
        _setup_fake_modules()
        yield


def _setup_fake_modules():
    # nanobot.agent.loop
    sol = types.ModuleType("nanobot")
    sol.agent = types.ModuleType("nanobot.agent")
    # lib/hooks/* импортируют имена из nanobot.agent при реальном импорте.
    sol.agent.AgentHook = type("AgentHook", (), {"__init__": lambda self: None})
    sol.agent.AgentHookContext = MagicMock()
    sol.agent.AgentRunHookContext = MagicMock()
    loop = types.ModuleType("nanobot.agent.loop")
    agent = MagicMock()
    agent.run = AsyncMock()
    agent.close_mcp = AsyncMock()
    agent.stop = MagicMock()
    agent.sessions = MagicMock()
    agent.sessions.flush_all = MagicMock(return_value=0)
    agent._assemble_outbound = MagicMock()
    loop.AgentLoop = MagicMock()
    loop.AgentLoop.from_config = MagicMock(return_value=agent)
    sys.modules["nanobot"] = sol
    sys.modules["nanobot.agent"] = sol.agent
    sys.modules["nanobot.agent.loop"] = loop

    # nanobot.bus
    sol.bus = types.ModuleType("nanobot.bus")
    bus = types.ModuleType("nanobot.bus.queue")
    bus.MessageBus = MagicMock()
    events = types.ModuleType("nanobot.bus.events")
    events.InboundMessage = MagicMock()
    events.OutboundMessage = MagicMock()
    sys.modules["nanobot.bus"] = sol.bus
    sys.modules["nanobot.bus.queue"] = bus
    sys.modules["nanobot.bus.events"] = events

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
    runtime_config.workspace_path = Path(__file__).resolve().parent.parent
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
    commands.__logo__ = "nanobot"
    commands.__version__ = "0.1.0"
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

    # config
    cfg = types.ModuleType("config")
    settings = MagicMock()
    settings.gateway = MagicMock()
    settings.gateway.storage = "file"
    settings.gateway.persist_threshold = 0
    settings.gateway.llm_timeout = -1
    settings.gateway.exec_timeout = -1
    settings.gateway.log_level = "INFO"
    settings.channels = {"postgres": {"dsn": ""}, "redis": {"enabled": False}}
    settings.skills = MagicMock()
    settings.skills.audit_analyzer = MagicMock()
    settings.skills.audit_analyzer.get = MagicMock(return_value=False)
    settings.cli = {}
    settings.providers = MagicMock()
    def _fake_get_setting(*keys, default=None):
        cur = settings
        for k in keys:
            cur = cur[k] if isinstance(cur, dict) else getattr(cur, k, None)
            if cur is None:
                return default
        return cur

    cfg.get_setting = _fake_get_setting
    cfg.SETTINGS = settings
    cfg.ENV_REF_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
    sys.modules["config"] = cfg

    # workspace
    ws = str(Path(__file__).resolve().parent.parent / "workspace")
    if ws not in sys.path:
        sys.path.insert(0, ws)
    # Фреймворковые хуки живут в lib/hooks/ и импортируются как реальные
    # модули (при фейковом nanobot.agent они импортируются успешно).

    # utils.db
    utils_mod = types.ModuleType("utils")
    utils_db = types.ModuleType("utils.db")
    utils_db.configure = MagicMock()
    utils_mod.db = utils_db
    sys.modules["utils"] = utils_mod
    sys.modules["utils.db"] = utils_db


def _get_ctx():
    """Создать и вернуть ApplicationContext при уже настроенных mock'ах."""
    from lib.core.application_context import ApplicationContext

    return ApplicationContext.create(
        script_dir=_project_root,
        workspace_dir=_workspace_path,
        enable_db_logging=False,
        enable_audit=False,
    )


def _setup_channels():
    from nanobot.channels.manager import ChannelManager

    cm = MagicMock()
    cm.start_all = AsyncMock()
    cm.stop_all = AsyncMock()
    cm.enabled_channels = []
    ChannelManager.return_value = cm
    return cm


class TestMain:
    def test_clean_shutdown(self):
        from nanobot.agent.loop import AgentLoop

        AgentLoop.from_config.return_value.run = AsyncMock()
        _setup_channels()

        with patch("gateway.GatewayRunner") as MockRunner:
            MockRunner.return_value.run_forever = MagicMock()
            from gateway import main

            main()
            MockRunner.return_value.run_forever.assert_called_once()

    def test_storage_postgres_without_dsn_falls_back(self):
        from config import SETTINGS

        SETTINGS.gateway.storage = "postgres"
        ctx = _get_ctx()
        assert ctx.storage_mode == "file"

    def test_file_path_no_dsn(self):
        ctx = _get_ctx()
        assert ctx.storage_mode == "file"

    def test_redis_channel_enabled(self):
        from config import SETTINGS

        SETTINGS.channels = {"redis": {"enabled": True}, "postgres": {"dsn": ""}}

        # Подменяем только redis_channel (lazy import).
        fake_redis = types.ModuleType("lib.channels.redis_channel")
        fake_redis.RedisChannel = MagicMock()
        sys.modules["lib.channels.redis_channel"] = fake_redis

        # Делаем ChannelManager().channels — настоящим dict
        from nanobot.channels.manager import ChannelManager

        ChannelManager.return_value = MagicMock()
        ChannelManager.return_value.channels = {}
        ChannelManager.return_value.enabled_channels = []

        from lib.services.channel_factory import ChannelFactory
        from lib.services.transcription_service import TranscriptionService

        ctx = _get_ctx()
        factory = ChannelFactory(transcription=TranscriptionService(ctx.config))
        channels, _ = factory.create_all(
            ctx.config, SETTINGS, ctx.bus, ctx.session_manager
        )
        assert "redis" in channels.channels

    def test_postgres_channel_enabled_no_dsn_errors(self):
        from config import SETTINGS

        SETTINGS.channels = {"postgres": {"enabled": True, "dsn": ""}, "redis": {"enabled": False}}

        fake_redis = types.ModuleType("lib.channels.redis_channel")
        fake_redis.RedisChannel = MagicMock()
        fake_pg = types.ModuleType("lib.channels.postgres_channel")
        fake_pg.PostgresChannel = MagicMock()
        sys.modules["lib.channels.redis_channel"] = fake_redis
        sys.modules["lib.channels.postgres_channel"] = fake_pg

        from nanobot.channels.manager import ChannelManager

        ChannelManager.return_value = MagicMock()
        ChannelManager.return_value.channels = {}
        ChannelManager.return_value.enabled_channels = []

        from lib.services.channel_factory import ChannelFactory
        from lib.services.transcription_service import TranscriptionService

        ctx = _get_ctx()
        factory = ChannelFactory(transcription=TranscriptionService(ctx.config))
        channels, messages = factory.create_all(
            ctx.config, SETTINGS, ctx.bus, ctx.session_manager
        )
        assert any("no DSN" in m for m in messages)

    def test_persist_threshold_zero_no_patch(self):
        from lib.services.runtime_patcher import RuntimePatcher

        ctx = _get_ctx()
        # При threshold=0 патч не должен ничего менять. Проверяем через
        # возвращаемое значение: skipped, не applied.
        ok, detail = RuntimePatcher().patch_context_governor(
            ctx.config, ctx.settings, ctx.workspace_dir
        )
        assert not ok


class TestStreamlitEnabled:
    """Флаг ``streamlit.enabled`` — когда поднимать UI на :8501."""

    def test_default_true(self, mock_all):
        from gateway import _streamlit_enabled

        with patch("lib.services.config_service.ConfigService") as M:
            M.return_value.settings_section.return_value.get.side_effect = (
                lambda k, d=None: d
            )
            assert _streamlit_enabled() is True

    def test_false_when_disabled(self, mock_all):
        from gateway import _streamlit_enabled

        with patch("lib.services.config_service.ConfigService") as M:
            M.return_value.settings_section.return_value.get.return_value = False
            assert _streamlit_enabled() is False

    def test_true_when_enabled(self, mock_all):
        from gateway import _streamlit_enabled

        with patch("lib.services.config_service.ConfigService") as M:
            M.return_value.settings_section.return_value.get.return_value = True
            assert _streamlit_enabled() is True
