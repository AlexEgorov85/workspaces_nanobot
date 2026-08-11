from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_workspace_path = str(Path(__file__).resolve().parent.parent / "workspace")
if _workspace_path not in sys.path:
    sys.path.insert(0, _workspace_path)
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
_user_site = r"C:\Users\Алексей\AppData\Roaming\Python\Python314\site-packages"
if _user_site not in sys.path:
    sys.path.insert(0, _user_site)


def _make_settings(**overrides):
    """Create a SETTINGS-like object with defaults for gateway tests."""
    s = MagicMock()
    s.channels = {
        "postgres": {
            "enabled": False,
            "dsn": "",
            "schema": "public",
            "messages_table": "session_messages",
            "meta_table": "session_meta",
        },
        "redis": {"enabled": False},
    }
    s.gateway = MagicMock()
    s.gateway.storage = "file"
    s.gateway.persist_threshold = 0
    s.gateway.persist_max_files = 100
    s.gateway.persist_max_age_hours = 24
    s.gateway.llm_timeout = -1
    s.gateway.exec_timeout = -1
    s.gateway.log_level = "INFO"
    s.providers = MagicMock()
    s.providers.__iter__.return_value = []
    s.skills = MagicMock()
    s.skills.audit_analyzer = MagicMock()
    s.skills.audit_analyzer.get = MagicMock(return_value=False)
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


@pytest.fixture(autouse=True)
def mock_all():
    """Mock all gateway dependencies before importing."""
    with patch.dict("sys.modules"):
        import types

        # nanobot submodules
        nanobot = types.ModuleType("nanobot")
        nanobot.agent = types.ModuleType("nanobot.agent")
        nanobot.agent.loop = types.ModuleType("nanobot.agent.loop")
        nanobot.agent.loop.AgentLoop = MagicMock()
        nanobot.agent.runner = types.ModuleType("nanobot.agent.runner")
        nanobot.agent.runner.AgentRunner = MagicMock()
        nanobot.bus = types.ModuleType("nanobot.bus")
        nanobot.bus.events = types.ModuleType("nanobot.bus.events")
        nanobot.bus.events.OutboundMessage = MagicMock()
        nanobot.bus.queue = types.ModuleType("nanobot.bus.queue")
        nanobot.bus.queue.MessageBus = MagicMock()
        nanobot.channels = types.ModuleType("nanobot.channels")
        nanobot.channels.manager = types.ModuleType("nanobot.channels.manager")
        nanobot.channels.manager.ChannelManager = MagicMock()
        nanobot.cli = types.ModuleType("nanobot.cli")
        nanobot.cli.commands = types.ModuleType("nanobot.cli.commands")
        nanobot.session = types.ModuleType("nanobot.session")
        nanobot.session.manager = types.ModuleType("nanobot.session.manager")
        nanobot.session.manager.Session = MagicMock()
        nanobot.session.manager.SessionManager = MagicMock()
        nanobot.session.manager._message_preview_text = MagicMock(return_value="")
        nanobot.utils = types.ModuleType("nanobot.utils")
        nanobot.utils.helpers = types.ModuleType("nanobot.utils.helpers")
        nanobot.utils.helpers.sync_workspace_templates = MagicMock()
        nanobot.utils.runtime = types.ModuleType("nanobot.utils.runtime")
        nanobot.utils.runtime.ensure_nonempty_tool_result = MagicMock()
        nanobot.config = types.ModuleType("nanobot.config")
        nanobot.config.paths = types.ModuleType("nanobot.config.paths")

        nanobot.cli.commands._load_runtime_config = MagicMock(return_value=MagicMock())
        nanobot.cli.commands.console = MagicMock()
        nanobot.cli.commands.__logo__ = "nanobot"
        nanobot.cli.commands.__version__ = "0.1.0"

        for name, mod in {
            "nanobot": nanobot,
            "nanobot.agent": nanobot.agent,
            "nanobot.agent.loop": nanobot.agent.loop,
            "nanobot.agent.runner": nanobot.agent.runner,
            "nanobot.bus": nanobot.bus,
            "nanobot.bus.events": nanobot.bus.events,
            "nanobot.bus.queue": nanobot.bus.queue,
            "nanobot.channels": nanobot.channels,
            "nanobot.channels.manager": nanobot.channels.manager,
            "nanobot.cli": nanobot.cli,
            "nanobot.cli.commands": nanobot.cli.commands,
            "nanobot.session": nanobot.session,
            "nanobot.session.manager": nanobot.session.manager,
            "nanobot.utils": nanobot.utils,
            "nanobot.utils.helpers": nanobot.utils.helpers,
            "nanobot.utils.runtime": nanobot.utils.runtime,
            "nanobot.config": nanobot.config,
            "nanobot.config.paths": nanobot.config.paths,
        }.items():
            sys.modules[name] = mod

        for mod_name in [
            "utils", "utils.db", "utils.session_file_store",
            "lib", "lib.channels", "lib.channels.postgres_channel",
            "lib.channels.redis_channel", "lib.session", "lib.session.pg_session_manager",
            "hooks", "hooks.tool_audit_hook",
        ]:
            m = types.ModuleType(mod_name)
            m.PostgresChannel = MagicMock()
            m.RedisChannel = MagicMock()
            m.SessionFileStore = MagicMock()
            m.prepare_content = MagicMock()
            m.PGSessionManager = MagicMock()
            m.ToolAuditHook = MagicMock()
            m.configure = MagicMock()
            sys.modules[mod_name] = m

        cfg = types.ModuleType("config")
        cfg.SETTINGS = _make_settings()
        sys.modules["config"] = cfg

        from gateway import (
            _resolve_transcription_key,
            _resolve_transcription_base,
            main,
        )

        yield {
            "cfg": cfg,
            "_resolve_transcription_key": _resolve_transcription_key,
            "_resolve_transcription_base": _resolve_transcription_base,
            "main": main,
        }


# ===================================================================
# _resolve_transcription_key
# ===================================================================

class TestResolveTranscriptionKey:
    def test_openai_provider(self, mock_all):
        config = MagicMock()
        config.channels.transcription_provider = "openai"
        config.providers.openai.api_key = "sk-openai-key"
        result = mock_all["_resolve_transcription_key"](config)
        assert result == "sk-openai-key"

    def test_groq_provider(self, mock_all):
        config = MagicMock()
        config.channels.transcription_provider = "groq"
        config.providers.groq.api_key = "gsk-groq-key"
        result = mock_all["_resolve_transcription_key"](config)
        assert result == "gsk-groq-key"

    def test_missing_provider_returns_empty(self, mock_all):
        config = MagicMock()
        config.channels.transcription_provider = "unknown"
        config.providers = MagicMock(spec=[])
        result = mock_all["_resolve_transcription_key"](config)
        assert result == ""

    def test_missing_attribute_returns_empty(self, mock_all):
        config = MagicMock()
        config.channels.transcription_provider = "openai"
        type(config.providers).openai = MagicMock()
        del config.providers.openai.api_key
        result = mock_all["_resolve_transcription_key"](config)
        assert result == ""


# ===================================================================
# _resolve_transcription_base
# ===================================================================

class TestResolveTranscriptionBase:
    def test_openai_base_url(self, mock_all):
        config = MagicMock()
        config.channels.transcription_provider = "openai"
        config.providers.openai.api_base = "https://api.openai.com/v1"
        result = mock_all["_resolve_transcription_base"](config)
        assert result == "https://api.openai.com/v1"

    def test_groq_base_url(self, mock_all):
        config = MagicMock()
        config.channels.transcription_provider = "groq"
        config.providers.groq.api_base = "https://api.groq.com/v1"
        result = mock_all["_resolve_transcription_base"](config)
        assert result == "https://api.groq.com/v1"

    def test_empty_base_returns_empty(self, mock_all):
        config = MagicMock()
        config.channels.transcription_provider = "openai"
        config.providers.openai.api_base = ""
        result = mock_all["_resolve_transcription_base"](config)
        assert result == ""

    def test_missing_attribute_returns_empty(self, mock_all):
        config = MagicMock()
        config.channels.transcription_provider = "openai"
        type(config.providers).openai = MagicMock()
        del config.providers.openai.api_base
        result = mock_all["_resolve_transcription_base"](config)
        assert result == ""


# ===================================================================
# main()
# ===================================================================

class TestMain:
    def _setup_agent_run(self, mock_agent_cls=None):
        """Configure AgentLoop mock so that 'agent.run()' is an async no-op."""
        agent = MagicMock()
        agent.run = AsyncMock()
        agent.close_mcp = AsyncMock()
        agent.stop = MagicMock()
        agent.sessions = MagicMock()
        agent.sessions.flush_all = MagicMock(return_value=0)
        agent._assemble_outbound = MagicMock(return_value=None)
        if mock_agent_cls:
            mock_agent_cls.from_config = MagicMock(return_value=agent)
        return agent

    def _setup_channels(self):
        """Configure ChannelManager mock."""
        from nanobot.channels.manager import ChannelManager
        cm = MagicMock()
        cm.start_all = AsyncMock()
        cm.stop_all = AsyncMock()
        cm.enabled_channels = []
        ChannelManager.return_value = cm
        return cm

    def test_clean_shutdown(self, mock_all):
        """main() should start up, run agent, and exit cleanly."""
        from nanobot.agent.loop import AgentLoop
        agent = self._setup_agent_run(AgentLoop)
        self._setup_channels()

        mock_all["main"]()

        # Setup phase completed
        AgentLoop.from_config.assert_called_once()
        agent.run.assert_called_once()
        agent.close_mcp.assert_called_once()
        agent.stop.assert_called_once()

    def test_keyboard_interrupt_exits(self, mock_all):
        """main() should exit on KeyboardInterrupt."""
        from nanobot.agent.loop import AgentLoop
        agent = MagicMock()
        agent.run = AsyncMock(side_effect=KeyboardInterrupt)
        agent.close_mcp = AsyncMock()
        agent.stop = MagicMock()
        agent.sessions = MagicMock()
        agent.sessions.flush_all = MagicMock(return_value=0)
        AgentLoop.from_config = MagicMock(return_value=agent)
        self._setup_channels()

        mock_all["main"]()

        agent.close_mcp.assert_called_once()
        agent.stop.assert_called_once()

    def test_session_manager_file_when_no_dsn(self, mock_all):
        """Without pg.dsn, SessionManager should be used."""
        from nanobot.agent.loop import AgentLoop
        agent = self._setup_agent_run(AgentLoop)
        self._setup_channels()

        mock_all["main"]()

        from nanobot.session.manager import SessionManager
        SessionManager.assert_called_once()

    def test_pg_session_manager_when_dsn_provided(self, mock_all):
        """With pg.dsn, PGSessionManager should be used."""
        from nanobot.agent.loop import AgentLoop
        from lib.session.pg_session_manager import PGSessionManager
        agent = self._setup_agent_run(AgentLoop)
        self._setup_channels()

        mock_all["cfg"].SETTINGS.channels["postgres"]["dsn"] = "postgresql://user@host/db"
        mock_all["cfg"].SETTINGS.gateway.storage = "postgres"

        mock_all["main"]()

        PGSessionManager.assert_called_once()

    def test_redis_channel_enabled(self, mock_all):
        """When redis.enabled is True, RedisChannel should be registered."""
        from nanobot.agent.loop import AgentLoop
        from lib.channels.redis_channel import RedisChannel
        agent = self._setup_agent_run(AgentLoop)
        cm = self._setup_channels()

        mock_all["cfg"].SETTINGS.channels["redis"]["enabled"] = True

        mock_all["main"]()

        RedisChannel.assert_called_once()

    def test_postgres_channel_enabled(self, mock_all):
        """When postgres channel is enabled, PostgresChannel should be registered."""
        from nanobot.agent.loop import AgentLoop
        from lib.channels.postgres_channel import PostgresChannel
        agent = self._setup_agent_run(AgentLoop)
        self._setup_channels()

        mock_all["cfg"].SETTINGS.channels["postgres"]["dsn"] = "postgresql://user@host/db"
        mock_all["cfg"].SETTINGS.channels["postgres"]["enabled"] = True

        mock_all["main"]()

        PostgresChannel.assert_called_once()

    def test_persist_threshold_creates_store(self, mock_all):
        """When persist_threshold > 0, SessionFileStore should be created."""
        from nanobot.agent.loop import AgentLoop
        from utils.session_file_store import SessionFileStore
        agent = self._setup_agent_run(AgentLoop)
        self._setup_channels()

        mock_all["cfg"].SETTINGS.gateway.persist_threshold = 1024

        mock_all["main"]()

        SessionFileStore.assert_called_once()

    def test_cleanup_on_shutdown(self, mock_all):
        """main() should call cleanup sequence on shutdown."""
        from nanobot.agent.loop import AgentLoop
        agent = self._setup_agent_run(AgentLoop)
        cm = self._setup_channels()

        mock_all["main"]()

        agent.close_mcp.assert_called_once()
        agent.stop.assert_called_once()
        cm.stop_all.assert_called_once()
