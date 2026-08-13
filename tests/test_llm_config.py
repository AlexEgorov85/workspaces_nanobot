from __future__ import annotations

import os
import sys
import types
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


def _settings(**overrides):
    base = {
        "agents": {
            "defaults": {"provider": "minimax", "model": "MiniMax-M3"},
        },
        "providers": {
            "minimax": {"apiBase": "https://api.minimax.io/v1", "apiKey": "sk-cp-secret"},
        },
    }
    base.update(overrides)
    return base


class TestResolveLlmConfig:
    def test_resolves_from_agent_defaults(self, fake_config_module):
        from lib.services.llm_config import resolve_llm_config

        fake_config_module.SETTINGS = _settings()
        cfg = resolve_llm_config()
        assert cfg["provider"] == "minimax"
        assert cfg["model"] == "MiniMax-M3"
        assert cfg["api_base"] == "https://api.minimax.io/v1"
        assert cfg["api_key"] == "sk-cp-secret"
        assert cfg["max_tokens"] == 8192
        assert cfg["temperature"] == 0.1

    def test_overrides_take_precedence(self, fake_config_module):
        from lib.services.llm_config import resolve_llm_config

        fake_config_module.SETTINGS = _settings()
        cfg = resolve_llm_config({
            "llm_model": "Other-Model",
            "llm_max_tokens": 4096,
            "llm_temperature": 0.7,
        })
        assert cfg["model"] == "Other-Model"
        assert cfg["max_tokens"] == 4096
        assert cfg["temperature"] == 0.7
        # провайдер/ключ — всё ещё из дефолтов агента
        assert cfg["provider"] == "minimax"
        assert cfg["api_key"] == "sk-cp-secret"

    def test_overrides_can_change_provider_and_key(self, fake_config_module):
        from lib.services.llm_config import resolve_llm_config

        fake_config_module.SETTINGS = _settings(
            providers={
                "minimax": {"apiKey": "sk-a"},
                "other": {"apiKey": "sk-b", "apiBase": "https://x/v1"},
            }
        )
        cfg = resolve_llm_config({"llm_provider": "other", "llm_api_key": "sk-custom"})
        assert cfg["provider"] == "other"
        assert cfg["api_key"] == "sk-custom"
        assert cfg["api_base"] == "https://x/v1"

    def test_does_not_require_environ(self, fake_config_module):
        from lib.services.llm_config import resolve_llm_config

        fake_config_module.SETTINGS = _settings()
        with patch.dict("os.environ", clear=True):
            cfg = resolve_llm_config()
            assert cfg["api_key"] == "sk-cp-secret"


class TestEnsureLlmEnv:
    def test_sets_env_from_resolved_key(self, fake_config_module):
        from lib.services.llm_config import ensure_llm_env

        fake_config_module.SETTINGS = _settings()
        with patch.dict("os.environ", clear=True):
            ensure_llm_env()
            assert os.environ["LLM_API_KEY"] == "sk-cp-secret"

    def test_does_not_override_existing_env(self, fake_config_module):
        from lib.services.llm_config import ensure_llm_env

        fake_config_module.SETTINGS = _settings()
        with patch.dict("os.environ", {"LLM_API_KEY": "already-set"}):
            ensure_llm_env()
            assert os.environ["LLM_API_KEY"] == "already-set"
