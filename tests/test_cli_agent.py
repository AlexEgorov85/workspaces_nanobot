from __future__ import annotations

import argparse
import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

_project_root = __import__("pathlib").Path(__file__).resolve().parent.parent
_workspace_path = str(_project_root / "workspace")
if _workspace_path not in sys.path:
    sys.path.insert(0, _workspace_path)
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


@pytest.fixture(autouse=True)
def mock_all():
    with patch.dict("sys.modules"):
        _setup_fake_modules()
        yield


def _setup_fake_modules():
    sol = types.ModuleType("nanobot")
    sol.agent = types.ModuleType("nanobot.agent")
    sol.agent.AgentHook = type("AgentHook", (), {})
    sys.modules["nanobot"] = sol
    sys.modules["nanobot.agent"] = sol.agent

    loop = types.ModuleType("nanobot.agent.loop")
    loop.AgentLoop = MagicMock()
    loop.AgentLoop.from_config = MagicMock()
    sys.modules["nanobot.agent.loop"] = loop

    hook_mod = types.ModuleType("nanobot.agent.hook")
    hook_mod.AgentHook = type("AgentHook", (), {"__init__": lambda self, reraise=False: None})
    sys.modules["nanobot.agent.hook"] = hook_mod

    bus = types.ModuleType("nanobot.bus")
    queue = types.ModuleType("nanobot.bus.queue")
    queue.MessageBus = MagicMock()
    events = types.ModuleType("nanobot.bus.events")
    events.InboundMessage = MagicMock()
    events.OutboundMessage = MagicMock()
    sys.modules["nanobot.bus"] = bus
    sys.modules["nanobot.bus.queue"] = queue
    sys.modules["nanobot.bus.events"] = events

    utils = types.ModuleType("nanobot.utils")
    helpers = types.ModuleType("nanobot.utils.helpers")
    helpers.sync_workspace_templates = MagicMock()
    sys.modules["nanobot.utils"] = utils
    sys.modules["nanobot.utils.helpers"] = helpers

    cli = types.ModuleType("nanobot.cli")
    commands = types.ModuleType("nanobot.cli.commands")
    runtime_config = MagicMock()
    runtime_config.workspace_path = _project_root / "workspace"
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
    sys.modules["nanobot.cli"] = cli
    sys.modules["nanobot.cli.commands"] = commands

    cron_mod = types.ModuleType("nanobot.cron")
    cron_svc = types.ModuleType("nanobot.cron.service")
    cron_svc.CronService = MagicMock()
    sys.modules["nanobot.cron"] = cron_mod
    sys.modules["nanobot.cron.service"] = cron_svc

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
    settings.cli = {"log_level": "WARNING"}
    settings.providers = MagicMock()
    cfg.SETTINGS = settings
    # После Phase B cli_agent/gateway/streamlit_app делают
    # ``from config import ConfigurationError`` на module-level.
    # Подменённый модуль ``config`` должен предоставлять этот символ,
    # иначе import падает до входа в module body.
    from config import ConfigurationError as _real_CE
    cfg.ConfigurationError = _real_CE
    sys.modules["config"] = cfg

    ws = str(_project_root / "workspace")
    if ws not in sys.path:
        sys.path.insert(0, ws)

    utils_pkg = types.ModuleType("utils")
    utils_db = types.ModuleType("utils.db")
    utils_db.configure = MagicMock()
    utils_pkg.db = utils_db
    sys.modules["utils"] = utils_pkg
    sys.modules["utils.db"] = utils_db


# =================================================================
# display_config
# =================================================================


class TestDisplayConfig:
    def test_from_settings(self):
        from lib.cli.display_config import DisplayConfig

        cfg = DisplayConfig.from_settings({"show_reasoning": False, "typewriter_speed": 0.05})
        assert cfg.show_reasoning is False
        assert cfg.typewriter_speed == 0.05


# =================================================================
# hook_loader
# =================================================================


class TestHookLoader:
    def test_no_hooks_dir(self, tmp_path):
        from lib.cli.hook_loader import scan_and_register

        hooks = scan_and_register(tmp_path / "nope", _project_root / "workspace")
        assert hooks == []

    def test_empty_dir(self, tmp_path):
        from lib.cli.hook_loader import scan_and_register

        hooks = scan_and_register(tmp_path, _project_root / "workspace")
        assert hooks == []

    def test_skips_underscore_files(self, tmp_path):
        from lib.cli.hook_loader import scan_and_register

        (tmp_path / "_skip.py").write_text("")
        (tmp_path / "real.py").write_text("class AgentHook: pass")
        # Module import of real.py will fail (no AgentHook base), but the
        # underscore file should be skipped without raising.
        hooks = scan_and_register(tmp_path, _project_root / "workspace")
        assert hooks == []  # real.py fails to import → ignored

    def test_finds_workspace_hooks_without_hooks_dir_in_syspath(self, tmp_path):
        """Регрессия: importlib.import_module(path.name[:-3]) требовал,
        чтобы ``hooks_dir`` был в sys.path как top-level. В gateway это
        не выполнялось (в sys.path только workspace/), и в логе шли
        warning'и ``No module named 'session_file_redirect_hook'``.
        Фикс: spec_from_file_location под именем ``hooks.<stem>``.
        """
        from lib.cli.hook_loader import scan_and_register

        # Создать поддельный ``hooks/`` с одним валидным хуком и убедиться,
        # что sys.path НЕ содержит tmp_path (имитируем реальный gateway).
        real_path = sys.path[:]
        try:
            sys.path[:] = [p for p in sys.path if str(tmp_path) not in p]
            fake_hook = tmp_path / "my_hook.py"
            fake_hook.write_text(
                "from nanobot.agent import AgentHook\n"
                "class MyHook(AgentHook):\n"
                "    def __init__(self, workspace_dir=None):\n"
                "        super().__init__()\n"
                "        self.workspace_dir = workspace_dir\n"
            )
            hooks = scan_and_register(tmp_path, tmp_path)
            assert any(type(h).__name__ == "MyHook" for h in hooks), (
                f"Хук должен найтись даже без tmp_path в sys.path. "
                f"Найдено: {[type(h).__name__ for h in hooks]}"
            )
        finally:
            sys.path[:] = real_path

    def test_finds_real_workspace_hooks(self):
        """Интеграционная проверка: реальные хуки в workspace/hooks/
        должны находиться через scan_and_register. До фикса в gateway
        они не находились.
        """
        from lib.cli.hook_loader import scan_and_register

        hooks_dir = _project_root / "workspace" / "hooks"
        if not hooks_dir.is_dir():
            import pytest
            pytest.skip("workspace/hooks не существует")

        # Убираем workspace/hooks из sys.path, чтобы убедиться, что
        # фикс работает без зависимости от path.
        real_path = sys.path[:]
        try:
            sys.path[:] = [
                p for p in sys.path
                if not p.endswith("workspace") and not p.endswith("hooks")
            ]
            hooks = scan_and_register(hooks_dir, _project_root / "workspace")
            names = sorted(type(h).__name__ for h in hooks)
            assert "SessionFileRedirectHook" in names, (
                f"SessionFileRedirectHook должен загружаться из workspace/hooks/. "
                f"Найдены: {names}"
            )
            assert "RecentFilesHook" in names, (
                f"RecentFilesHook должен загружаться из workspace/hooks/. "
                f"Найдены: {names}"
            )
        finally:
            sys.path[:] = real_path

    def test_plugin_dir_has_only_self_instantiable_hooks(self):
        """Вариант А: workspace/hooks/ — только плагины. Фреймворковые
        хуки (ToolAuditHook, DatabaseLoggingHook, BaseToolTrackingHook)
        переехали в lib/hooks/ и не должны сканироваться как плагины.
        Раньше это было причиной warning'ов
        ``__init__() got an unexpected keyword argument 'workspace_dir'``
        и ``missing required positional argument: 'db_logging_service'``
        в gateway-логе.
        """
        from lib.cli.hook_loader import scan_and_register

        hooks_dir = _project_root / "workspace" / "hooks"
        if not hooks_dir.is_dir():
            import pytest
            pytest.skip("workspace/hooks не существует")

        files = {py.name for py in hooks_dir.glob("*.py") if not py.name.startswith("_")}
        assert "tool_audit_hook.py" not in files, files
        assert "database_logging_hook.py" not in files, files
        assert "base_tool_tracking_hook.py" not in files, files

        hooks = scan_and_register(hooks_dir, _project_root / "workspace")
        loaded = sorted(type(h).__name__ for h in hooks)
        assert "ToolAuditHook" not in loaded, loaded
        assert "DatabaseLoggingHook" not in loaded, loaded
        assert "BaseToolTrackingHook" not in loaded, loaded
        # Оба реальных плагина должны загрузиться без warning'ов.
        assert "SessionFileRedirectHook" in loaded, loaded
        assert "RecentFilesHook" in loaded, loaded


# =================================================================
# console_loop.typewriter
# =================================================================


class TestTypewriter:
    @pytest.mark.asyncio
    async def test_zero_speed_prints(self):
        from lib.cli.console_loop import _typewriter

        with patch("lib.cli.console_loop.console") as mc:
            await _typewriter("hello", "bold", 0)
            mc.print.assert_called_once()


# =================================================================
# parse_args
# =================================================================


class TestParseArgs:
    def test_defaults(self):
        from cli_agent import _parse_args

        # Stage F (change ``unify-cli-gateway-architecture``, design D8):
        # CLI = фиксированный профиль ``test``, ``--profile`` НЕ
        # принимается. Тест вызывает _parse_args без ``--profile``.
        with patch("sys.argv", ["cli_agent.py"]):
            args = _parse_args()
            assert args.patched is False
            assert args.storage == "auto"
            assert args.session is None
            assert args.profile == "test"

    def test_patched_flag(self):
        from cli_agent import _parse_args

        with patch("sys.argv", ["cli_agent.py", "--patched"]):
            args = _parse_args()
            assert args.patched is True
            assert args.profile == "test"

    def test_storage_postgres(self):
        from cli_agent import _parse_args

        with patch("sys.argv", ["cli_agent.py", "-P", "-S", "postgres"]):
            args = _parse_args()
            assert args.patched is True
            assert args.storage == "postgres"
            assert args.profile == "test"

    def test_session_key(self):
        from cli_agent import _parse_args

        with patch("sys.argv", ["cli_agent.py", "-s", "my-session"]):
            args = _parse_args()
            assert args.session == "my-session"
            assert args.profile == "test"


class TestRunVanillaForwardsStorageAndSession:
    """Stage F contract: ``_run_vanilla`` MUST передавать ``storage_override``
    и ``session_override`` в ``ApplicationContext.create()``.

    Регрессия: до правки ``_run_vanilla`` теряла ``args.storage`` — CLI
    принимал ``--storage=file``, но пробрасывал его только в
    ``_run_patched``. Это нарушало спецификацию
    (``openspec/specs/runtime/entrypoints/spec.md``: «CLI entrypoint MUST
    принимать runtime-флаги ``--storage`` и ``--session``») и приводило к
    тихой потере режима хранилища при vanilla-запуске.
    """

    def _capture_create_kwargs(self, monkeypatch):
        """Подменить ``ApplicationContext.create`` через ``__new__``
        construction-time shim нельзя (это classmethod). Используем
        прямой monkeypatch на ``ApplicationContext.create``."""
        captured: dict = {}

        def _fake_create(cls, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            # Возвращаем мок-инстанс, чтобы _run_vanilla мог позвать .stop()
            mock = MagicMock()
            mock.settings = {"logging": {"db": {"table_name": "agent_gateway_logs_test"}}}
            mock.config = MagicMock()
            mock.config_service = MagicMock()
            mock.config_service.settings_section.return_value = {}
            return mock

        from lib.core import application_context as ac

        monkeypatch.setattr(ac.ApplicationContext, "create", classmethod(_fake_create))
        return captured

    def test_vanilla_passes_storage_override(self, monkeypatch) -> None:
        from argparse import Namespace
        import cli_agent

        captured = self._capture_create_kwargs(monkeypatch)

        args = Namespace(
            smoke=False,
            session=None,
            storage="file",
            patched=False,
        )
        # Run до первой ``await`` в run_repl — нам нужно только убедиться,
        # что ctx создан с правильным kwargs. Используем прямой вызов
        # внутренней логики, минуя asyncio.run/run_repl.
        cli_agent._configure_logging({"cli": {}})
        # Без запуска REPL напрямую подменим порядок: вызываем тело
        # _run_vanilla до asyncio.run, отлавливая kwargs.
        # Для этого выдёргиваем ApplicationContext.create уже подменённым.

        # Простой способ: запустить _run_vanilla в фоне, перехватив
        # ApplicationContext.create — мокинстанс не блокирует REPL,
        # потому что REPL вызовет ``asyncio.run(run_repl(ctx.agent, ...))``
        # и провалится на MagicMock.agent. Поэтому прервём выполнение
        # через monkeypatch на asyncio.run.
        from unittest.mock import patch as _patch
        with _patch("cli_agent.asyncio.run", side_effect=SystemExit(0)):
            try:
                cli_agent._run_vanilla(args)
            except SystemExit:
                pass

        assert "kwargs" in captured, "ApplicationContext.create не был вызван"
        assert captured["kwargs"].get("storage_override") == "file", (
            f"_run_vanilla не пробросил --storage=file в create(); "
            f"kwargs={captured['kwargs']}"
        )
        assert captured["kwargs"].get("role") == "cli"
        assert "profile" not in captured["kwargs"], (
            "_run_vanilla не должен передавать profile — "
            "профиль уже разрешён через _initialize_settings"
        )

    def test_vanilla_passes_session_override(self, monkeypatch) -> None:
        from argparse import Namespace
        import cli_agent
        from unittest.mock import patch as _patch

        captured = self._capture_create_kwargs(monkeypatch)
        args = Namespace(
            smoke=False,
            session="my-session",
            storage="auto",
            patched=False,
        )
        with _patch("cli_agent.asyncio.run", side_effect=SystemExit(0)):
            try:
                cli_agent._run_vanilla(args)
            except SystemExit:
                pass

        assert captured["kwargs"].get("session_override") == "my-session", (
            f"_run_vanilla не пробросил --session=my-session; "
            f"kwargs={captured['kwargs']}"
        )


# =================================================================
# RuntimePatcher — patch_assemble_outbound (consumed by cli_agent)
# =================================================================


class TestPatchAssembleOutbound:
    def test_wraps_and_injects_audit(self, monkeypatch):
        from lib.hooks.database_logging_hook import (
            _CONTEXT_BRIDGE,
            _CONTEXT_BRIDGE_LOCK,
            seed_context_window,
        )
        from lib.services.runtime_patcher import RuntimePatcher

        session_key = "test:cli:patch_assemble_outbound"
        seed_context_window(session_key, limit=40000, model="test-model")
        monkeypatch.setattr(
            "lib.services.runtime_patcher._session_key_of",
            lambda msg: session_key,
        )

        agent = MagicMock()
        original = MagicMock()
        original.metadata = {}
        agent._assemble_outbound.return_value = original
        hook = MagicMock()
        hook.drain.return_value = [{"name": "read"}]

        RuntimePatcher().patch_assemble_outbound(agent, hook)
        result = agent._assemble_outbound(MagicMock(), "content", "stop", False)
        assert result.metadata["_tool_audit"] == [{"name": "read"}]

        with _CONTEXT_BRIDGE_LOCK:
            _CONTEXT_BRIDGE.pop(session_key, None)


# =================================================================
# migrate_cron_store
# =================================================================


class TestMigrateCronStore:
    def test_moves_legacy(self, tmp_path):
        from cli_agent import _migrate_cron_store

        config = MagicMock()
        config.workspace_path = tmp_path / "workspace"
        legacy = tmp_path / "legacy" / "jobs.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text('{"jobs": []}')

        # Подменяем get_cron_dir через sys.modules.nanobot.config.paths
        sol = types.ModuleType("nanobot")
        cfg = types.ModuleType("nanobot.config")
        paths = types.ModuleType("nanobot.config.paths")
        paths.get_cron_dir = MagicMock(return_value=legacy.parent)
        sys.modules["nanobot"] = sol
        sys.modules["nanobot.config"] = cfg
        sys.modules["nanobot.config.paths"] = paths

        _migrate_cron_store(config)

        new_path = tmp_path / "workspace" / "cron" / "jobs.json"
        assert new_path.exists()

    def test_no_legacy_noop(self, tmp_path):
        from cli_agent import _migrate_cron_store

        config = MagicMock()
        config.workspace_path = tmp_path / "workspace"
        legacy_dir = tmp_path / "legacy"
        legacy_dir.mkdir(parents=True)

        sol = types.ModuleType("nanobot")
        cfg = types.ModuleType("nanobot.config")
        paths = types.ModuleType("nanobot.config.paths")
        paths.get_cron_dir = MagicMock(return_value=legacy_dir)
        sys.modules["nanobot"] = sol
        sys.modules["nanobot.config"] = cfg
        sys.modules["nanobot.config.paths"] = paths

        _migrate_cron_store(config)
        assert not (tmp_path / "workspace" / "cron" / "jobs.json").exists()


# =================================================================
# configure_logging
# =================================================================


class TestConfigureLogging:
    def test_set_warn_level(self):
        from cli_agent import _configure_logging

        settings = {"cli": {"log_level": "WARNING"}}
        with patch.dict("os.environ", clear=True):
            _configure_logging(settings)
            assert os.environ["NANOBOT_LOG_LEVEL"] == "WARNING"

    def test_defaults_to_warning(self):
        from cli_agent import _configure_logging

        with patch.dict("os.environ", clear=True):
            _configure_logging({})
            assert os.environ["NANOBOT_LOG_LEVEL"] == "WARNING"
