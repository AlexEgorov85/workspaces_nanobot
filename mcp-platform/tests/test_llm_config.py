"""Контракт ``resolve_llm_config`` — источник конфигурации вместо ``config.json``.

Портировано из агентских тестов ``tests/test_llm_config.py``. Проверяются все
ветки, включая отказ при незаданной модели и незаданном ``api_base``: подстановка
значения по умолчанию отправила бы запрос не туда и выглядела бы как «модель
не отвечает».

Отдельно проверяется, что резолв **не читает конфиг агента**: модуль ``config``
подменяется мусорным, и разрешение всё равно обязано упасть. Пока эта проверка
проходит, платформа не может незаметно вернуться к чужому источнику.
"""

from __future__ import annotations

import sys
import types
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
if str(PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(PLATFORM_ROOT))

from libs.enterprise_common.errors import InfrastructureError  # noqa: E402
from libs.llm.config import (  # noqa: E402
    DEFAULT_MAX_TOKENS,
    DEFAULT_PROVIDER,
    DEFAULT_TEMPERATURE,
    LlmConfig,
    ensure_llm_env,
    resolve_llm_config,
)

#: Полный набор переменных платформы. Дублируется здесь намеренно: тест
#: должен сломаться, если у резолва поменяется имя переменной, — иначе
#: сервер поднимется «без конфигурации» и упадёт на первом же вызове.
FULL_ENV = {
    "ENTERPRISE_LLM_PROVIDER": "minimax",
    "ENTERPRISE_LLM_MODEL": "MiniMax-M3",
    "ENTERPRISE_LLM_API_BASE": "https://api.minimax.io/v1",
    "ENTERPRISE_LLM_API_KEY": "sk-cp-secret",
}


@pytest.fixture
def env() -> dict[str, str]:
    """Собственное окружение теста: общий ``conftest`` не редактируется."""
    return dict(FULL_ENV)


class TestResolution:
    def test_resolves_from_platform_environment(self, env: dict[str, str]) -> None:
        cfg = resolve_llm_config(env=env)
        assert cfg.provider == "minimax"
        assert cfg.model == "MiniMax-M3"
        assert cfg.api_base == "https://api.minimax.io/v1"
        assert cfg.api_key == "sk-cp-secret"
        assert cfg.max_tokens == DEFAULT_MAX_TOKENS
        assert cfg.temperature == DEFAULT_TEMPERATURE

    def test_defaults_match_agent(self, env: dict[str, str]) -> None:
        """Дефолты — это стоимость каждого вызова в системе."""
        assert (DEFAULT_PROVIDER, DEFAULT_MAX_TOKENS, DEFAULT_TEMPERATURE) == (
            "openai-compatible",
            8192,
            0.1,
        )

    def test_short_aliases_are_accepted(self) -> None:
        """Агент экспортирует ``LLM_API_KEY`` в своё окружение, а клиент
        enterprise-mcp наследует окружение целиком: ключ не дублируется."""
        cfg = resolve_llm_config(
            env={
                "LLM_MODEL": "alias-model",
                "LLM_API_BASE": "https://alias.invalid/v1",
                "LLM_API_KEY": "sk-alias",
            }
        )
        assert (cfg.model, cfg.api_key, cfg.provider) == (
            "alias-model",
            "sk-alias",
            DEFAULT_PROVIDER,
        )

    def test_platform_variable_wins_over_short_alias(self) -> None:
        cfg = resolve_llm_config(
            env={
                "ENTERPRISE_LLM_MODEL": "platform",
                "LLM_MODEL": "alias",
                "ENTERPRISE_LLM_API_BASE": "https://x.invalid/v1",
            }
        )
        assert cfg.model == "platform"

    def test_overrides_win_over_environment(self, env: dict[str, str]) -> None:
        cfg = resolve_llm_config(
            {
                "llm_model": "Other-Model",
                "llm_max_tokens": 4096,
                "llm_temperature": 0.7,
            },
            env=env,
        )
        assert cfg.model == "Other-Model"
        assert cfg.max_tokens == 4096
        assert cfg.temperature == 0.7
        # провайдер/ключ — всё ещё из конфигурации
        assert cfg.provider == "minimax"
        assert cfg.api_key == "sk-cp-secret"

    def test_overrides_can_change_provider_and_key(self, env: dict[str, str]) -> None:
        cfg = resolve_llm_config(
            {"llm_provider": "other", "llm_api_key": "sk-custom", "llm_api_base": "https://o.invalid/v1"},
            env=env,
        )
        assert (cfg.provider, cfg.api_key, cfg.api_base) == (
            "other",
            "sk-custom",
            "https://o.invalid/v1",
        )

    def test_key_is_optional(self) -> None:
        """Провайдер без авторизации — не ошибка конфигурации."""
        cfg = resolve_llm_config(
            env={"ENTERPRISE_LLM_MODEL": "m", "ENTERPRISE_LLM_API_BASE": "https://x.invalid/v1"}
        )
        assert cfg.api_key == ""

    def test_numeric_parameters_from_environment(self, env: dict[str, str]) -> None:
        env["ENTERPRISE_LLM_MAX_TOKENS"] = "2048"
        env["ENTERPRISE_LLM_TEMPERATURE"] = "0.35"
        cfg = resolve_llm_config(env=env)
        assert (cfg.max_tokens, cfg.temperature) == (2048, 0.35)

    def test_blank_environment_values_count_as_unset(self) -> None:
        """Пробелы от ``${VAR}`` не должны выглядеть как заданная модель."""
        with pytest.raises(InfrastructureError, match="не задана модель"):
            resolve_llm_config(
                env={"ENTERPRISE_LLM_MODEL": "   ", "ENTERPRISE_LLM_API_BASE": "https://x.invalid/v1"}
            )

    def test_does_not_read_process_environment_when_env_given(self) -> None:
        """Явный ``env`` — единственный источник: тест не должен зависеть от
        машины, на которой прогоняется."""
        cfg = resolve_llm_config(
            env={"ENTERPRISE_LLM_MODEL": "m", "ENTERPRISE_LLM_API_BASE": "https://x.invalid/v1"}
        )
        assert cfg.model == "m"


class TestFailures:
    def test_missing_model_is_rejected(self, env: dict[str, str]) -> None:
        env.pop("ENTERPRISE_LLM_MODEL")
        with pytest.raises(InfrastructureError, match="не задана модель"):
            resolve_llm_config(env=env)

    def test_missing_api_base_names_provider(self, env: dict[str, str]) -> None:
        env.pop("ENTERPRISE_LLM_API_BASE")
        with pytest.raises(InfrastructureError) as excinfo:
            resolve_llm_config(env=env)
        assert "не задан api_base" in excinfo.value.message
        assert "'minimax'" in excinfo.value.message
        assert excinfo.value.code == "infrastructure_error"

    def test_error_text_names_real_sources(self) -> None:
        """Ошибка обязана называть то, что администратор и правда задаёт."""
        with pytest.raises(InfrastructureError) as excinfo:
            resolve_llm_config(env={})
        message = excinfo.value.message
        assert message.startswith("resolve_llm_config:")
        assert "ENTERPRISE_LLM_MODEL" in message
        assert "agents.defaults" not in message

    def test_non_numeric_max_tokens_names_variable(self, env: dict[str, str]) -> None:
        env["ENTERPRISE_LLM_MAX_TOKENS"] = "много"
        with pytest.raises(InfrastructureError) as excinfo:
            resolve_llm_config(env=env)
        assert "ENTERPRISE_LLM_MAX_TOKENS" in excinfo.value.message
        assert excinfo.value.code == "infrastructure_error"

    def test_non_numeric_temperature_names_source(self, env: dict[str, str]) -> None:
        with pytest.raises(InfrastructureError, match="llm_temperature в overrides"):
            resolve_llm_config({"llm_temperature": "тепло"}, env=env)

    def test_agent_config_module_is_not_a_source(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Мусорная фикстура: конфиг агента доступен и валиден — и всё равно
        не используется. Иначе возврат к ``import config`` был бы незаметен."""
        module = types.ModuleType("config")
        module.SETTINGS = {  # type: ignore[attr-defined]
            "agents": {"defaults": {"provider": "minimax", "model": "M"}},
            "providers": {"minimax": {"apiBase": "https://legacy.invalid/v1", "apiKey": "sk"}},
        }
        monkeypatch.setitem(sys.modules, "config", module)
        with pytest.raises(InfrastructureError, match="не задана модель"):
            resolve_llm_config(env={})


class TestLlmConfig:
    def test_from_mapping_accepts_agent_dict(self) -> None:
        cfg = LlmConfig.from_mapping(
            {
                "provider": "minimax",
                "model": "M",
                "api_base": "https://x.invalid/v1",
                "api_key": "sk",
                "max_tokens": 128,
                "temperature": 0.3,
            }
        )
        assert isinstance(cfg, LlmConfig)
        assert cfg.max_tokens == 128
        assert cfg.temperature == 0.3

    def test_from_mapping_is_idempotent(self) -> None:
        cfg = LlmConfig(provider="p", model="m", api_base="https://x.invalid/v1")
        assert LlmConfig.from_mapping(cfg) is cfg

    def test_to_dict_keeps_agent_keys(self) -> None:
        """Ключи агентской формы не теряются, эмбеддерные добавляются.

        Именно «не теряются», а не «равны этому множеству»: словарь уходит
        потребителям, пришедшим из агента (логирование, дочерний процесс), и
        исчезновение там ключа сломало бы их. Обратное — молчаливое появление
        ключа, которого потребитель не ждёт, — тоже плохо, поэтому множество
        проверяется целиком, а не «содержит».
        """
        payload = LlmConfig(
            provider="p", model="m", api_base="https://x.invalid/v1", api_key="sk"
        ).to_dict()
        assert set(payload) == {
            "provider",
            "model",
            "api_base",
            "api_key",
            "max_tokens",
            "temperature",
            "embed_api_base",
            "embed_api_key",
            "embed_model",
            "embed_path",
        }
        assert payload["max_tokens"] == DEFAULT_MAX_TOKENS

    def test_embed_defaults_to_chat_provider(self) -> None:
        """Пустые эмбеддерные поля означают «тот же провайдер».

        Конфигурация с одним провайдером не должна требовать лишних
        переменных окружения, иначе перенос ломал бы её в момент, когда
        эмбеддингов не касается вовсе.
        """
        cfg = LlmConfig(provider="p", model="m", api_base="https://x.invalid/v1")
        assert cfg.embed_api_base == ""
        assert cfg.embed_model == ""
        assert cfg.embed_path == ""

    def test_embed_settings_are_read_from_environment(self) -> None:
        """Эмбеддер читается из своих переменных, а не из чатных."""
        cfg = resolve_llm_config(
            env={
                "ENTERPRISE_LLM_MODEL": "chat-model",
                "ENTERPRISE_LLM_API_BASE": "https://chat.invalid/v1",
                "ENTERPRISE_EMBED_API_BASE": "http://localhost:11434",
                "ENTERPRISE_EMBED_PATH": "api/embed",
                "ENTERPRISE_EMBED_MODEL": "mxbai-embed-large:latest",
            }
        )
        assert cfg.model == "chat-model"
        assert cfg.api_base == "https://chat.invalid/v1"
        assert cfg.embed_api_base == "http://localhost:11434"
        assert cfg.embed_path == "api/embed"
        assert cfg.embed_model == "mxbai-embed-large:latest"

    @pytest.mark.parametrize(
        "garbage",
        [
            {},
            {"model": "m"},
            {"api_base": "https://x.invalid/v1"},
            {"model": "", "api_base": "https://x.invalid/v1"},
            {"model": None, "api_base": "https://x.invalid/v1"},
        ],
        ids=["empty", "no-model", "no-api-base", "blank-model", "null-model"],
    )
    def test_from_mapping_rejects_incomplete_dict(self, garbage: dict[str, Any]) -> None:
        with pytest.raises(InfrastructureError) as excinfo:
            LlmConfig.from_mapping(garbage)
        assert excinfo.value.code == "infrastructure_error"

    def test_frozen(self) -> None:
        """Конфиг переживает несколько вызовов подряд: правка на середине
        ретрая отправила бы первый и последний запрос к разным провайдерам."""
        cfg = LlmConfig(provider="p", model="m", api_base="https://x.invalid/v1")
        with pytest.raises(FrozenInstanceError):
            cfg.model = "другая"  # type: ignore[misc]


class TestEnsureLlmEnv:
    def test_sets_alias_key_from_platform_config(self) -> None:
        target: dict[str, str] = dict(FULL_ENV)
        ensure_llm_env(target)
        assert target["LLM_API_KEY"] == "sk-cp-secret"

    def test_does_not_override_existing_alias_key(self) -> None:
        target: dict[str, str] = {**FULL_ENV, "LLM_API_KEY": "already-set"}
        ensure_llm_env(target)
        assert target["LLM_API_KEY"] == "already-set"

    def test_no_key_configured_sets_nothing(self) -> None:
        target: dict[str, str] = {
            "ENTERPRISE_LLM_MODEL": "m",
            "ENTERPRISE_LLM_API_BASE": "https://x.invalid/v1",
        }
        ensure_llm_env(target)
        assert "LLM_API_KEY" not in target

    def test_misconfigured_process_raises_instead_of_exporting_junk(self) -> None:
        """Не подставлять ключ из «ниоткуда»: падение здесь честнее
        экспортированного мусора, который потом уедет в дочерний процесс."""
        with pytest.raises(InfrastructureError):
            ensure_llm_env({})
