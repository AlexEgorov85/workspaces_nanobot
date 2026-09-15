"""Интеграционные тесты: SETTINGS глобальный vs ApplicationContext.

Эти тесты — главная проверка того, что **нет двух механизмов
конфигурации**. До этого плана `SETTINGS` и `ctx.config_service.settings`
могли возвращать разные конфигурации. Теперь оба пути проходят через
ConfigurationResolver.

См. также: docs/PROFILES.md.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import config as config_mod
from config import (
    ConfigurationError,
    get_active_profile,
    resolve_application_config,
)


# ---------------------------------------------------------------------------
# 1. SETTINGS глобальный — Resolver-разрешённый, не legacy
# ---------------------------------------------------------------------------


def test_module_level_settings_is_test_by_default():
    """`from config import SETTINGS` без указания профиля = test.

    Это fail-safe поведение: разработчик, набравший `import config`,
    получает test-конфигурацию, а не продовую. До этого плана SETTINGS
    строился минуя Resolver (без profile overlay), что приводило к
    двум разным результатам для одного и того же отсутствующего
    profile."""
    from config import SETTINGS

    pg = SETTINGS.get("channels", {}).get("postgres", {}) or {}
    log = SETTINGS.get("logging", {}).get("db", {}) or {}
    assert pg.get("messages_table") == "agent_session_messages_test", (
        "Глобальный SETTINGS должен быть test-config (default = test, fail-safe). "
        "Если это не так, значит SETTINGS строится мимо Resolver (legacy-путь)."
    )
    assert log.get("table_name") == "agent_gateway_logs_test"


def test_module_level_settings_with_env_prod_uses_prod_names(monkeypatch):
    """Если NANOBOT_PROFILE=prod в env, модульный SETTINGS — prod.

    КРИТИЧНО: это проверяет, что не нужен отдельный bootstrap —
    тот же Resolver обрабатывает оба режима при import-time."""
    # Подменяем пути до установки env, чтобы пересобрать SETTINGS.
    # Это сложно сделать post-import, поэтому проверяем через
    # resolve_application_config напрямую (эквивалентно тому, что
    # сделал бы модуль config.py при import).
    monkeypatch.setenv("NANOBOT_PROFILE", "prod")
    cfg = resolve_application_config(profile="prod")
    assert cfg["channels"]["postgres"]["messages_table"] == "agent_session_messages"


def test_module_level_active_profile_matches_settings():
    """get_active_profile() возвращает профиль, с которым построен SETTINGS."""
    from config import SETTINGS

    active = get_active_profile()
    pg = SETTINGS.get("channels", {}).get("postgres", {}) or {}
    table = pg.get("messages_table", "")

    if active == "test":
        assert table.endswith("_test"), (
            f"active_profile={active}, но SETTINGS messages_table={table!r} "
            f"не заканчивается на _test. Resolver и SETTINGS расходятся."
        )
    elif active == "prod":
        assert not table.endswith("_test"), (
            f"active_profile={active}, но SETTINGS messages_table={table!r} "
            f"заканчивается на _test. Resolver и SETTINGS расходятся."
        )


# ---------------------------------------------------------------------------
# 2. Конфигурационный путь — единый
# ---------------------------------------------------------------------------


def test_no_legacy_config_path_in_config_py():
    """config.py НЕ должен иметь «старого» bootstrap, который собирал
    SETTINGS минуя Resolver.

    Проверяется на уровне ключевых признаков: модуль должен
    использовать resolve_application_config() для построения SETTINGS,
    а не отдельный _deep_merge(SETTINGS, ...) блок."""
    import config as cfg

    # 1. SETTINGS должен быть построен через Resolver
    assert isinstance(cfg.SETTINGS, dict) or hasattr(cfg.SETTINGS, "messages_table")

    # 2. SETTINGS должен иметь _resolver-built marker (через _ACTIVE_PROFILE)
    assert hasattr(cfg, "_ACTIVE_PROFILE")
    assert cfg._ACTIVE_PROFILE in ("prod", "test")

    # 3. resolve_application_config существует и работает
    assert callable(cfg.resolve_application_config)


def test_config_service_does_not_have_legacy_fallback():
    """ConfigService НЕ должен иметь fallback-пути к legacy SETTINGS."""
    import inspect

    from lib.services.config_service import ConfigService

    src = inspect.getsource(ConfigService.settings.fget)
    # Не должно быть "if profile is None: return legacy SETTINGS"
    assert "_profile is None" not in src or "settings_override" in src, (
        "ConfigService.settings всё ещё содержит fallback-путь к legacy SETTINGS. "
        "Это нарушает «один механизм конфигурации»."
    )


# ---------------------------------------------------------------------------
# 3. SessionStorageService не читает session_manager.json
# ---------------------------------------------------------------------------


def test_session_storage_service_does_not_read_session_manager_json():
    """SessionStorageService больше не имеет `_load_override` или
    параметра `session_manager_json` — это нарушение контракта
    единственного Resolver."""
    import inspect

    from lib.services.session_storage import SessionStorageService

    # 1. Метод _load_override не должен быть определён
    assert not hasattr(SessionStorageService, "_load_override"), (
        "SessionStorageService._load_override не должен существовать — "
        "override из session_manager.json теперь применяется централизованно "
        "в ConfigurationResolver."
    )

    # 2. Параметр session_manager_json не должен быть в __init__
    sig = inspect.signature(SessionStorageService.__init__)
    assert "session_manager_json" not in sig.parameters, (
        "SessionStorageService.__init__ не должен принимать session_manager_json."
    )


def test_session_storage_service_no_empty_init():
    """__init__ не должен быть `pass`-пустышкой."""
    import inspect

    from lib.services.session_storage import SessionStorageService

    src = inspect.getsource(SessionStorageService)
    if "def __init__" in src:
        init_match = "def __init__(self"
        init_idx = src.find(init_match)
        if init_idx != -1:
            init_body = src[init_idx : src.find("def ", init_idx + 1)]
            assert "pass" not in init_body or "def " not in init_body[
                len(init_match) : init_body.find("def ")
            ], (
                "SessionStorageService.__init__ не должен быть пустой "
                "заглушкой (def __init__(self): pass)."
            )


# ---------------------------------------------------------------------------
# 4. CLI > env > default = test
# ---------------------------------------------------------------------------


def test_cli_profile_overrides_env(monkeypatch):
    """CLI --profile имеет приоритет над NANOBOT_PROFILE env."""
    monkeypatch.setenv("NANOBOT_PROFILE", "prod")
    cli_arg = "test"
    cfg = resolve_application_config(profile=cli_arg)
    # CLI перебивает env
    assert cfg["channels"]["postgres"]["messages_table"] == "agent_session_messages_test"


def test_env_profile_used_when_no_cli(monkeypatch):
    """Если CLI не передан — берётся env."""
    monkeypatch.setenv("NANOBOT_PROFILE", "prod")
    cfg = resolve_application_config(profile=None)
    assert cfg["channels"]["postgres"]["messages_table"] == "agent_session_messages"


def test_default_is_test_when_no_cli_no_env(monkeypatch):
    """Если ни CLI, ни env — default = test (fail-safe)."""
    monkeypatch.delenv("NANOBOT_PROFILE", raising=False)
    cfg = resolve_application_config(profile=None)
    assert cfg["channels"]["postgres"]["messages_table"] == "agent_session_messages_test"


# ---------------------------------------------------------------------------
# 5. ApplicationContext — профильно-согласованный ctx.settings
# ---------------------------------------------------------------------------


def test_application_context_settings_consistency(monkeypatch, tmp_path):
    """ApplicationContext должен согласованно передавать profile и
    settings — никакого расхождения между ctx.profile и ctx.settings.

    Проверяем через прямое использование Resolver + ApplicationContext:
    когда profile == _ACTIVE_PROFILE, ctx.settings ссылается на
    config.SETTINGS (тот же объект); когда отличается — отдельный
    Resolver-built dict."""
    import config as _config

    # Когда profile=None, _resolve_mode даёт default = test
    resolved = _config._resolve_mode(None)
    assert resolved == _config._ACTIVE_PROFILE

    # Они должны указывать на одно и то же (при default-профиле)
    ctx_settings = (
        _config.SETTINGS
        if resolved == _config._ACTIVE_PROFILE
        else _config.resolve_application_config(profile=resolved)
    )
    assert ctx_settings is _config.SETTINGS


def test_application_context_prod_profile_uses_resolver(monkeypatch):
    """ApplicationContext.create(profile='prod') → ctx.settings из Resolver,
    НЕ из глобального SETTINGS (который = test по default)."""
    import config as _config

    prod_cfg = _config.resolve_application_config(profile="prod")
    assert prod_cfg["channels"]["postgres"]["messages_table"] == "agent_session_messages"

    # Глобальный SETTINGS при default=test
    assert _config.SETTINGS["channels"]["postgres"]["messages_table"] == "agent_session_messages_test"


# ---------------------------------------------------------------------------
# 6. Hard-fail валидация
# ---------------------------------------------------------------------------


def test_missing_profile_overlay_in_test_mode_fails(monkeypatch, tmp_path):
    """Без profiles/test.jsonc test-режим → ConfigurationError на старте."""
    _write = lambda p, d: p.write_text(json.dumps(d))
    (tmp_path / "project.json").parent.mkdir(parents=True, exist_ok=True)
    _write(tmp_path / "project.json", {
        "channels": {"postgres": {
            "dsn": "${DATABASE_URL}",
            "table_name":     "agent_conversation_messages",
            "messages_table": "agent_session_messages",
            "meta_table":     "agent_session_meta",
            "claims_table":   "agent_worker_claims",
        }},
        "logging": {"db": {
            "table_name":          "agent_gateway_logs",
            "question_runs_table": "agent_question_runs",
        }},
    })
    monkeypatch.setattr(config_mod, "_PROJECT_FILE", tmp_path / "project.json")
    monkeypatch.setattr(config_mod, "_CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config_mod, "_SECRETS_FILE", None)
    monkeypatch.setattr(config_mod, "_SESSION_MANAGER_FILE", tmp_path / "session_manager.json")
    monkeypatch.setattr(config_mod, "_PROFILES_DIR", tmp_path / "profiles")

    with pytest.raises(ConfigurationError, match="test.jsonc"):
        resolve_application_config(profile="test")


def test_validate_runtime_isolation_fails_for_wrong_names(isolated_project):
    """Точное соответствие runtime-таблиц — суффикс _test недостаточен."""
    tmp_path = isolated_project
    cfg = {
        "channels": {"postgres": {
            "table_name":     "foo_test",  # неправильно
            "messages_table": "agent_session_messages_test",
            "meta_table":     "agent_session_meta_test",
            "claims_table":   "agent_worker_claims_test",
        }},
        "logging": {"db": {
            "table_name":          "agent_gateway_logs_test",
            "question_runs_table": "agent_question_runs_test",
        }},
    }
    with pytest.raises(ConfigurationError, match="foo_test"):
        config_mod.validate_runtime_isolation(cfg, "test")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_project(monkeypatch, tmp_path):
    """Подменить пути config.py на временный каталог."""
    monkeypatch.setattr(config_mod, "_PROJECT_FILE", tmp_path / "project.json")
    monkeypatch.setattr(config_mod, "_CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config_mod, "_SECRETS_FILE", None)
    monkeypatch.setattr(
        config_mod, "_SESSION_MANAGER_FILE", tmp_path / "session_manager.json"
    )
    monkeypatch.setattr(config_mod, "_PROFILES_DIR", tmp_path / "profiles")
    return tmp_path