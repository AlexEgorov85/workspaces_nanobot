from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def fake_config_module():
    """Подменяем sys.modules['config'] фейковым SETTINGS на время теста."""
    with patch.dict("sys.modules"):
        mod = types.ModuleType("config")
        mod.SETTINGS = MagicMock()
        sys.modules["config"] = mod
        yield mod


def _make_dict_settings():
    return {
        "channels": {"postgres": {"dsn": ""}},
        "gateway": {"log_level": "INFO"},
    }


class TestImportableWithoutNanobot:
    def test_module_imports(self):
        import importlib

        mod = importlib.import_module("lib.services.config_service")
        assert hasattr(mod, "ConfigService")

    def test_class_constructs_without_nanobot(self):
        from lib.services.config_service import ConfigService

        svc = ConfigService()
        assert svc.script_dir is None


class TestSettingsSection:
    def test_dict_settings(self, fake_config_module):
        from lib.services.config_service import ConfigService

        fake_config_module.SETTINGS = _make_dict_settings()
        svc = ConfigService()
        assert svc.settings_section("channels")["postgres"]["dsn"] == ""
        assert svc.settings_section("missing", {"a": 1}) == {"a": 1}

    def test_object_settings(self, fake_config_module):
        from lib.services.config_service import ConfigService

        class _Settings:
            channels = {"redis": {"enabled": False}}

        fake_config_module.SETTINGS = _Settings()
        svc = ConfigService()
        assert svc.settings_section("channels")["redis"]["enabled"] is False

    def test_missing_section_returns_default(self, fake_config_module):
        from lib.services.config_service import ConfigService

        fake_config_module.SETTINGS = _make_dict_settings()
        svc = ConfigService()
        assert svc.settings_section("nope") == {}

    def test_non_dict_node_returns_default(self, fake_config_module):
        from lib.services.config_service import ConfigService

        fake_config_module.SETTINGS = {"channels": None}
        svc = ConfigService()
        assert svc.settings_section("channels", {"fallback": 1}) == {"fallback": 1}


class TestApplyProviderKeys:
    def test_injects_key_into_config(self, fake_config_module):
        from lib.services.config_service import ConfigService

        settings = MagicMock()
        openai_cfg = {"api_key": "sk-secret"}
        settings.providers = {"openai": openai_cfg}
        fake_config_module.SETTINGS = settings

        config = MagicMock()
        config.providers.openai.api_key = None

        svc = ConfigService()
        svc.apply_provider_keys(config)
        assert config.providers.openai.api_key == "sk-secret"

    def test_no_providers_attribute_noop(self, fake_config_module):
        from lib.services.config_service import ConfigService

        settings = MagicMock()
        del settings.providers
        fake_config_module.SETTINGS = settings

        config = MagicMock()
        svc = ConfigService()
        svc.apply_provider_keys(config)  # не должно упасть

    def test_missing_provider_section_skipped(self, fake_config_module):
        from lib.services.config_service import ConfigService

        settings = MagicMock()
        settings.providers = {"openai": {"api_key": "sk-secret"}}
        fake_config_module.SETTINGS = settings

        config = MagicMock()
        config.providers.openai = None
        svc = ConfigService()
        svc.apply_provider_keys(config)  # не должно упасть


class TestApplyTimeouts:
    def test_llm_timeout_sets_env(self, fake_config_module):
        from lib.services.config_service import ConfigService

        svc = ConfigService()
        with patch.dict("os.environ", clear=True):
            svc.apply_timeouts(MagicMock(), llm_timeout=42)
            assert os.environ["NANOBOT_LLM_TIMEOUT_S"] == "42"

    def test_negative_llm_timeout_noop(self, fake_config_module):
        from lib.services.config_service import ConfigService

        svc = ConfigService()
        with patch.dict("os.environ", clear=True):
            svc.apply_timeouts(MagicMock(), llm_timeout=-1)
            assert "NANOBOT_LLM_TIMEOUT_S" not in os.environ

    def test_exec_timeout_applied(self, fake_config_module):
        from lib.services.config_service import ConfigService

        config = MagicMock()
        svc = ConfigService()
        svc.apply_timeouts(config, exec_timeout=99)
        assert config.tools.exec.timeout == 99

    def test_max_iterations_applied(self, fake_config_module):
        from lib.services.config_service import ConfigService

        config = MagicMock()
        svc = ConfigService()
        svc.apply_timeouts(config, max_iterations=10)
        assert config.agents.defaults.max_tool_iterations == 10

    def test_exec_timeout_errors_suppressed(self, fake_config_module):
        from lib.services.config_service import ConfigService

        config = MagicMock()
        config.tools.exec.timeout.side_effect = AttributeError("no tools")
        svc = ConfigService()
        svc.apply_timeouts(config, exec_timeout=99)  # не должно упасть


class TestLoad:
    def test_load_with_mocked_nanobot(self, fake_config_module):
        """load() должен вызвать _load_runtime_config и sync_workspace_templates."""
        fake_nanobot = types.ModuleType("nanobot")
        fake_cli = types.ModuleType("nanobot.cli")
        fake_commands = types.ModuleType("nanobot.cli.commands")
        fake_utils = types.ModuleType("nanobot.utils")
        fake_helpers = types.ModuleType("nanobot.utils.helpers")

        runtime_config = MagicMock()
        runtime_config.workspace_path = Path(r"ws")
        fake_commands._load_runtime_config = MagicMock(return_value=runtime_config)
        fake_helpers.sync_workspace_templates = MagicMock()

        fake_cli.commands = fake_commands
        fake_utils.helpers = fake_helpers
        for name, mod in {
            "nanobot": fake_nanobot,
            "nanobot.cli": fake_cli,
            "nanobot.cli.commands": fake_commands,
            "nanobot.utils": fake_utils,
            "nanobot.utils.helpers": fake_helpers,
        }.items():
            sys.modules[name] = mod

        from lib.services.config_service import ConfigService

        svc = ConfigService()
        result = svc.load(script_dir=Path("proj"), workspace_dir=Path("ws"))

        assert result is runtime_config
        fake_commands._load_runtime_config.assert_called_once()
        fake_helpers.sync_workspace_templates.assert_called_once_with(Path(r"ws"))


class TestPreResolveEnvRefs:
    def _setup(self, tmp_path):
        """Создать config.json с ${VAR} плейсхолдерами."""
        cfg = tmp_path / "config.json"
        cfg.write_text(
            '{"providers": {"minimax": {"apiKey": "${LLM_API_KEY}"}}}',
            encoding="utf-8",
        )
        return cfg

    def test_resolves_missing_api_key_from_settings(self, fake_config_module, tmp_path):
        from lib.services.config_service import ConfigService

        cfg_path = self._setup(tmp_path)
        # settings.providers.<любой>.api_key задан (из .secrets.env)
        fake_config_module.SETTINGS = {
            "providers": {"minimax": {"api_key": "XavGPsHjtNt3uOtFGUhbUuad5PRm2D0W"}}
        }

        with patch.dict("os.environ", clear=True):
            svc = ConfigService()
            svc._pre_resolve_env_refs(script_dir=tmp_path)
            assert os.environ["LLM_API_KEY"] == "XavGPsHjtNt3uOtFGUhbUuad5PRm2D0W"

    def test_does_not_override_existing_env(self, fake_config_module, tmp_path):
        from lib.services.config_service import ConfigService

        self._setup(tmp_path)
        fake_config_module.SETTINGS = {
            "providers": {"minimax": {"api_key": "from-settings"}}
        }

        with patch.dict("os.environ", {"LLM_API_KEY": "from-shell"}):
            svc = ConfigService()
            svc._pre_resolve_env_refs(script_dir=tmp_path)
            assert os.environ["LLM_API_KEY"] == "from-shell"

    def test_ignores_non_api_key_placeholders(self, fake_config_module, tmp_path):
        from lib.services.config_service import ConfigService

        cfg = tmp_path / "config.json"
        cfg.write_text('{"x": "${DATABASE_URL}"}', encoding="utf-8")
        fake_config_module.SETTINGS = {}

        with patch.dict("os.environ", clear=True):
            svc = ConfigService()
            svc._pre_resolve_env_refs(script_dir=tmp_path)
            assert "DATABASE_URL" not in os.environ

    def test_ignores_placeholder_keys_in_settings(self, fake_config_module, tmp_path):
        from lib.services.config_service import ConfigService

        self._setup(tmp_path)
        fake_config_module.SETTINGS = {
            "providers": {"minimax": {"api_key": "${LLM_API_KEY}"}}
        }

        with patch.dict("os.environ", clear=True):
            svc = ConfigService()
            svc._pre_resolve_env_refs(script_dir=tmp_path)
            assert "LLM_API_KEY" not in os.environ

    def test_no_config_json_noop(self, fake_config_module, tmp_path):
        from lib.services.config_service import ConfigService

        fake_config_module.SETTINGS = {}
        with patch.dict("os.environ", clear=True):
            svc = ConfigService()
            svc._pre_resolve_env_refs(script_dir=tmp_path)  # не должно упасть

    def test_invalid_json_noop(self, fake_config_module, tmp_path):
        from lib.services.config_service import ConfigService

        cfg = tmp_path / "config.json"
        cfg.write_text("{broken", encoding="utf-8")
        with patch.dict("os.environ", clear=True):
            svc = ConfigService()
            svc._pre_resolve_env_refs(script_dir=tmp_path)  # не должно упасть
