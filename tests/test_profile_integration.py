"""Тесты приложения профиля через ApplicationContext.

Специализированные тесты на согласованность ``ApplicationContext`` и
``SETTINGS`` (после Phase A lifecycle-gate). Каждый тест явно
инициализирует profile через ``_initialize_settings`` — autouse-fixture
для этого НЕ используется (см. design.md Decision 6: lifecycle-ошибки
должны всплывать, а не маскироваться).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import config
import config as config_mod
from config import (
    ConfigurationError,
    resolve_application_config,
    validate_profile_overlay,
    validate_runtime_isolation,
)


# ---------------------------------------------------------------------------
# 1. validate_profile_overlay — только 6 разрешённых ключей
# ---------------------------------------------------------------------------


def test_validate_profile_overlay_rejects_dsn():
    """Профиль не имеет права менять DSN или другие не-runtime ключи."""
    overlay = {
        "channels": {
            "postgres": {
                "table_name":     "agent_conversation_messages_test",
                "messages_table": "agent_session_messages_test",
                "meta_table":     "agent_session_meta_test",
                "claims_table":   "agent_worker_claims_test",
                "dsn":            "postgresql://other_db/test",  # ЗАПРЕЩЕНО
            }
        },
        "logging": {
            "db": {
                "table_name":          "agent_gateway_logs_test",
                "question_runs_table": "agent_question_runs_test",
            }
        },
    }
    with pytest.raises(ConfigurationError, match="dsn"):
        validate_profile_overlay(overlay, "test")


def test_validate_profile_overlay_accepts_only_allowed_keys():
    """Шесть разрешённых runtime-ключей — без посторонних."""
    overlay = {
        "channels": {
            "postgres": {
                "table_name":     "agent_conversation_messages_test",
                "messages_table": "agent_session_messages_test",
                "meta_table":     "agent_session_meta_test",
                "claims_table":   "agent_worker_claims_test",
            }
        },
        "logging": {
            "db": {
                "table_name":          "agent_gateway_logs_test",
                "question_runs_table": "agent_question_runs_test",
            }
        },
    }
    validate_profile_overlay(overlay, "test")  # не должно бросить


def test_validate_profile_overlay_rejects_vector_storage():
    """Запрещено менять vector-storage и skill-domain данные."""
    overlay = {
        "channels": {"postgres": {
            "table_name":     "agent_conversation_messages_test",
            "messages_table": "agent_session_messages_test",
            "meta_table":     "agent_session_meta_test",
            "claims_table":   "agent_worker_claims_test",
        }},
        "logging": {"db": {
            "table_name":          "agent_gateway_logs_test",
            "question_runs_table": "agent_question_runs_test",
        }},
        "gateway": {
            "vector": {
                "index": {
                    "storage_table": "oarb.audit_vectors_test",  # ЗАПРЕЩЕНО
                }
            }
        },
    }
    with pytest.raises(ConfigurationError, match="storage_table"):
        validate_profile_overlay(overlay, "test")


# ---------------------------------------------------------------------------
# 2. validate_runtime_isolation — точное соответствие ожидаемым именам
# ---------------------------------------------------------------------------


def test_validate_runtime_isolation_test_mode_matches():
    """В test все 6 runtime-таблиц должны иметь точные test-имена."""
    cfg = {
        "channels": {"postgres": {
            "table_name":     "agent_conversation_messages_test",
            "messages_table": "agent_session_messages_test",
            "meta_table":     "agent_session_meta_test",
            "claims_table":   "agent_worker_claims_test",
        }},
        "logging": {"db": {
            "table_name":          "agent_gateway_logs_test",
            "question_runs_table": "agent_question_runs_test",
        }},
    }
    validate_runtime_isolation(cfg, "test")


def test_validate_runtime_isolation_test_mode_rejects_foo_test():
    """Суффикс _test сам по себе недостаточен — нужно точное соответствие."""
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
        validate_runtime_isolation(cfg, "test")


def test_validate_runtime_isolation_prod_mode_matches():
    """В prod runtime-таблицы должны быть prod-именами (без _test)."""
    cfg = {
        "channels": {"postgres": {
            "table_name":     "agent_conversation_messages",
            "messages_table": "agent_session_messages",
            "meta_table":     "agent_session_meta",
            "claims_table":   "agent_worker_claims",
        }},
        "logging": {"db": {
            "table_name":          "agent_gateway_logs",
            "question_runs_table": "agent_question_runs",
        }},
    }
    validate_runtime_isolation(cfg, "prod")


def test_validate_runtime_isolation_prod_mode_rejects_test_suffix():
    """В prod суффикс _test запрещён."""
    cfg = {
        "channels": {"postgres": {
            "table_name":     "agent_conversation_messages_test",  # неправильно для prod
            "messages_table": "agent_session_messages",
            "meta_table":     "agent_session_meta",
            "claims_table":   "agent_worker_claims",
        }},
        "logging": {"db": {
            "table_name":          "agent_gateway_logs",
            "question_runs_table": "agent_question_runs",
        }},
    }
    with pytest.raises(ConfigurationError, match="_test"):
        validate_runtime_isolation(cfg, "prod")


# ---------------------------------------------------------------------------
# 3. Merge-order — профиль побеждает session_manager.json и config.json
# ---------------------------------------------------------------------------


def test_profile_wins_over_session_manager_and_config_json(isolated_project):
    """Даже если session_manager.json и config.json пытаются перетереть
    runtime-таблицы prod-именами, profile overlay (последний) должен
    оставить test-имена."""
    tmp_path = isolated_project
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
    _write(tmp_path / "session_manager.json", {
        "messages_table": "agent_session_messages",  # PROD-попытка
    })
    _write(tmp_path / "config.json", {
        "channels": {"postgres": {
            "messages_table": "agent_session_messages",  # PROD-попытка
        }},
    })
    _write(tmp_path / "profiles" / "test.jsonc", {
        "channels": {"postgres": {
            "table_name":     "agent_conversation_messages_test",
            "messages_table": "agent_session_messages_test",
            "meta_table":     "agent_session_meta_test",
            "claims_table":   "agent_worker_claims_test",
        }},
        "logging": {"db": {
            "table_name":          "agent_gateway_logs_test",
            "question_runs_table": "agent_question_runs_test",
        }},
    })

    cfg = resolve_application_config(profile="test")
    assert cfg["channels"]["postgres"]["messages_table"] == "agent_session_messages_test"
    assert cfg["channels"]["postgres"]["table_name"] == "agent_conversation_messages_test"
    assert cfg["channels"]["postgres"]["meta_table"] == "agent_session_meta_test"
    assert cfg["channels"]["postgres"]["claims_table"] == "agent_worker_claims_test"
    assert cfg["logging"]["db"]["table_name"] == "agent_gateway_logs_test"
    assert cfg["logging"]["db"]["question_runs_table"] == "agent_question_runs_test"


def test_profile_wins_when_session_manager_uses_test_but_config_uses_prod(isolated_project):
    """Даже если session_manager.json уже содержит test-имя, config.json
    с prod-именем не должен сломать изоляцию."""
    tmp_path = isolated_project
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
    _write(tmp_path / "session_manager.json", {
        "messages_table": "agent_session_messages_test",  # уже test
    })
    _write(tmp_path / "config.json", {
        "channels": {"postgres": {
            "messages_table": "agent_session_messages",  # пытается prod
        }},
    })
    _write(tmp_path / "profiles" / "test.jsonc", {
        "channels": {"postgres": {
            "table_name":     "agent_conversation_messages_test",
            "messages_table": "agent_session_messages_test",
            "meta_table":     "agent_session_meta_test",
            "claims_table":   "agent_worker_claims_test",
        }},
        "logging": {"db": {
            "table_name":          "agent_gateway_logs_test",
            "question_runs_table": "agent_question_runs_test",
        }},
    })

    cfg = resolve_application_config(profile="test")
    assert cfg["channels"]["postgres"]["messages_table"] == "agent_session_messages_test"


def test_session_manager_can_override_pool_but_not_runtime_tables(isolated_project):
    """session_manager.json (per-deploy override) сохраняет роль для pool,
    но НЕ может сломать profile-owned runtime-таблицы (профиль идёт позже)."""
    tmp_path = isolated_project
    _write(tmp_path / "project.json", {
        "channels": {"postgres": {
            "dsn": "${DATABASE_URL}",
            "table_name":     "agent_conversation_messages",
            "messages_table": "agent_session_messages",
            "meta_table":     "agent_session_meta",
            "claims_table":   "agent_worker_claims",
            "pool": {"min_conn": 1, "max_conn": 4, "pool_timeout": 5.0},
        }},
        "logging": {"db": {
            "table_name":          "agent_gateway_logs",
            "question_runs_table": "agent_question_runs",
        }},
    })
    _write(tmp_path / "session_manager.json", {
        "min_conn": 2,
        "max_conn": 16,
        # session_manager НЕ МОЖЕТ перетереть runtime-таблицы:
        "messages_table": "agent_session_messages",  # PROD-попытка
    })
    _write(tmp_path / "profiles" / "test.jsonc", {
        "channels": {"postgres": {
            "table_name":     "agent_conversation_messages_test",
            "messages_table": "agent_session_messages_test",
            "meta_table":     "agent_session_meta_test",
            "claims_table":   "agent_worker_claims_test",
        }},
        "logging": {"db": {
            "table_name":          "agent_gateway_logs_test",
            "question_runs_table": "agent_question_runs_test",
        }},
    })

    cfg = resolve_application_config(profile="test")
    assert cfg["channels"]["postgres"]["messages_table"] == "agent_session_messages_test"


def test_prod_profile_uses_pure_project_json(isolated_project):
    """Для prod profiles/<mode>.jsonc не нужен — берётся чистый project.json."""
    tmp_path = isolated_project
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

    cfg = resolve_application_config(profile="prod")
    assert cfg["channels"]["postgres"]["table_name"] == "agent_conversation_messages"
    assert cfg["channels"]["postgres"]["messages_table"] == "agent_session_messages"
    assert cfg["logging"]["db"]["table_name"] == "agent_gateway_logs"
    assert cfg["logging"]["db"]["question_runs_table"] == "agent_question_runs"


# ---------------------------------------------------------------------------
# 4. Fail-fast — отсутствие profiles/test.jsonc при mode=test
# ---------------------------------------------------------------------------


def test_test_mode_without_overlay_fails(isolated_project):
    """Без profiles/test.jsonc в test-режиме процесс не стартует."""
    tmp_path = isolated_project
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

    with pytest.raises(ConfigurationError, match="test.jsonc"):
        resolve_application_config(profile="test")


# ---------------------------------------------------------------------------
# 5. Whitelist профилей — тесты после ужесточения (Phase A)
# ---------------------------------------------------------------------------


def test_unsupported_profile_in_resolver_via_subprocess():
    """``resolve_application_config('foo bar')`` теперь падает в resolver
    (был regex раньше, теперь whitelist в _initialize_settings).
    Для проверки используем subprocess — иначе reload модуля ломает
    тесты из-за идентичности старого vs нового ConfigurationError."""
    script = (
        "import config\n"
        "try:\n"
        "    config.resolve_application_config('foo bar')\n"
        "except config.ConfigurationError as e:\n"
        "    print('caught:', str(e))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    # Проверяем что ошибка ConfigurationError была поднята и поймана
    assert "caught:" in result.stdout
    assert "profiles/foo bar.jsonc" in result.stdout


def test_resolver_accepts_whitelist_prod() -> None:
    """``resolve_application_config('prod')`` — принят."""
    cfg = resolve_application_config(profile="prod")
    assert "channels" in cfg


def test_resolver_accepts_whitelist_test(tmp_path_factory) -> None:
    """``resolve_application_config('test')`` с минимальным изолированным
    profiles/test.jsonc — принят.

    Использует локальные tmp paths вместо monkeypatch всего проекта,
    чтобы не зависеть от глобального состояния pytest-сеанса.
    """
    # Создаём минимальный изолированный profiles/test.jsonc
    # прямо рядом с нашим profiles/.
    # Подменяем пути через monkeypatch на временные.
    from unittest.mock import patch

    tmp_dir = tmp_path_factory.mktemp("resolver_test")
    (tmp_dir / "project.json").parent.mkdir(parents=True, exist_ok=True)
    (tmp_dir / "project.json").write_text(json.dumps({
        "channels": {"postgres": {
            "table_name":     "agent_conversation_messages",
            "messages_table": "agent_session_messages",
            "meta_table":     "agent_session_meta",
            "claims_table":   "agent_worker_claims",
        }},
        "logging": {"db": {
            "table_name":          "agent_gateway_logs",
            "question_runs_table": "agent_question_runs",
        }},
    }), encoding="utf-8")
    profiles_dir = tmp_dir / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "test.jsonc").write_text(json.dumps({
        "channels": {"postgres": {
            "table_name":     "agent_conversation_messages_test",
            "messages_table": "agent_session_messages_test",
            "meta_table":     "agent_session_meta_test",
            "claims_table":   "agent_worker_claims_test",
        }},
        "logging": {"db": {
            "table_name":          "agent_gateway_logs_test",
            "question_runs_table": "agent_question_runs_test",
        }},
    }), encoding="utf-8")

    with patch.object(config_mod, "_PROJECT_FILE", tmp_dir / "project.json"), \
         patch.object(config_mod, "_CONFIG_FILE", tmp_dir / "config.json"), \
         patch.object(config_mod, "_SECRETS_FILE", None), \
         patch.object(config_mod, "_SESSION_MANAGER_FILE", tmp_dir / "session_manager.json"), \
         patch.object(config_mod, "_PROFILES_DIR", profiles_dir):
        cfg = resolve_application_config(profile="test")
    assert cfg["channels"]["postgres"]["messages_table"] == "agent_session_messages_test"


# ---------------------------------------------------------------------------
# 6. .secrets.env integration — критичный контракт плана
# ---------------------------------------------------------------------------


def test_secrets_env_loaded_and_exported_to_env(monkeypatch, tmp_path):
    """Секреты из .secrets.env попадают в os.environ через Resolver
    ДО резолва ${VAR} — иначе ${VAR} не разрешились бы.

    Сценарий:
      * .secrets.env содержит ``SECRET_TOKEN=foo123``
      * os.environ НЕ содержит ``SECRET_TOKEN`` (только что стартовали)
      * project.json содержит ``{"some_key": "${SECRET_TOKEN}"}``
      * после ``resolve_application_config``: cfg["some_key"] == "foo123"
        и os.environ["SECRET_TOKEN"] == "foo123".
    """
    monkeypatch.delenv("SECRET_TOKEN", raising=False)
    (tmp_path / "secrets.env").write_text(
        "SECRET_TOKEN=foo123\n", encoding="utf-8"
    )
    (tmp_path / "project.json").write_text(json.dumps({
        "some_key": "${SECRET_TOKEN}",
        "channels": {"postgres": {
            "dsn": "postgresql://placeholder/x",
            "table_name":     "agent_conversation_messages",
            "messages_table": "agent_session_messages",
            "meta_table":     "agent_session_meta",
            "claims_table":   "agent_worker_claims",
        }},
        "logging": {"db": {
            "table_name":          "agent_gateway_logs",
            "question_runs_table": "agent_question_runs",
        }},
    }))
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "test.jsonc").write_text(json.dumps({
        "channels": {"postgres": {
            "table_name":     "agent_conversation_messages_test",
            "messages_table": "agent_session_messages_test",
            "meta_table":     "agent_session_meta_test",
            "claims_table":   "agent_worker_claims_test",
        }},
        "logging": {"db": {
            "table_name":          "agent_gateway_logs_test",
            "question_runs_table": "agent_question_runs_test",
        }},
    }))

    monkeypatch.setattr(config_mod, "_PROJECT_FILE", tmp_path / "project.json")
    monkeypatch.setattr(config_mod, "_CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config_mod, "_SECRETS_FILE", tmp_path / "secrets.env")
    monkeypatch.setattr(config_mod, "_SESSION_MANAGER_FILE", tmp_path / "session_manager.json")
    monkeypatch.setattr(config_mod, "_PROFILES_DIR", tmp_path / "profiles")

    cfg = resolve_application_config(profile="test")
    assert cfg["some_key"] == "foo123"
    assert os.environ.get("SECRET_TOKEN") == "foo123"


def test_secrets_env_optional(monkeypatch, tmp_path):
    """Без .secrets.env Resolver всё равно работает (если ${VAR} не нужны)."""
    monkeypatch.setattr(config_mod, "_PROJECT_FILE", tmp_path / "project.json")
    monkeypatch.setattr(config_mod, "_CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config_mod, "_SECRETS_FILE", tmp_path / "secrets.env")
    monkeypatch.setattr(config_mod, "_SESSION_MANAGER_FILE", tmp_path / "session_manager.json")
    monkeypatch.setattr(config_mod, "_PROFILES_DIR", tmp_path / "profiles")

    (tmp_path / "project.json").write_text(json.dumps({
        "channels": {"postgres": {
            "dsn": "postgresql://direct/x",
            "table_name":     "agent_conversation_messages",
            "messages_table": "agent_session_messages",
            "meta_table":     "agent_session_meta",
            "claims_table":   "agent_worker_claims",
        }},
        "logging": {"db": {
            "table_name":          "agent_gateway_logs",
            "question_runs_table": "agent_question_runs",
        }},
    }))
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "test.jsonc").write_text(json.dumps({
        "channels": {"postgres": {
            "table_name":     "agent_conversation_messages_test",
            "messages_table": "agent_session_messages_test",
            "meta_table":     "agent_session_meta_test",
            "claims_table":   "agent_worker_claims_test",
        }},
        "logging": {"db": {
            "table_name":          "agent_gateway_logs_test",
            "question_runs_table": "agent_question_runs_test",
        }},
    }))

    cfg = resolve_application_config(profile="test")
    assert cfg["channels"]["postgres"]["dsn"] == "postgresql://direct/x"
    assert cfg["channels"]["postgres"]["messages_table"] == "agent_session_messages_test"


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
# 8. validate_profile_overlay симметричная (требует все 6 ключей)
# ---------------------------------------------------------------------------


def test_validate_profile_overlay_requires_all_six_keys():
    """Симметричная проверка: profiles/<mode>.jsonc должен содержать
    ВСЕ 6 profile-owned runtime-ключей — отсутствие любого из них =
    fail-fast, а не молчаливая подмена дефолтами."""
    partial_overlay = {
        "channels": {"postgres": {
            "messages_table": "agent_session_messages_test",
            # остальные 5 ключей отсутствуют
        }},
    }
    with pytest.raises(ConfigurationError, match="обязательн"):
        config_mod.validate_profile_overlay(partial_overlay, "test")


def test_validate_profile_overlay_accepts_complete_overlay():
    """Полный оверлей (все 6 ключей) — проходит."""
    complete = {
        "channels": {"postgres": {
            "table_name":     "agent_conversation_messages_test",
            "messages_table": "agent_session_messages_test",
            "meta_table":     "agent_session_meta_test",
            "claims_table":   "agent_worker_claims_test",
        }},
        "logging": {"db": {
            "table_name":          "agent_gateway_logs_test",
            "question_runs_table": "agent_question_runs_test",
        }},
    }
    config_mod.validate_profile_overlay(complete, "test")


def test_validate_profile_overlay_rejects_extra_keys():
    """Запрещено менять dsn, vector storage и т.п."""
    overlay = {
        "channels": {"postgres": {
            "table_name":     "agent_conversation_messages_test",
            "messages_table": "agent_session_messages_test",
            "meta_table":     "agent_session_meta_test",
            "claims_table":   "agent_worker_claims_test",
            "dsn":            "postgresql://other_db/test",  # ЗАПРЕЩЕНО
        }},
        "logging": {"db": {
            "table_name":          "agent_gateway_logs_test",
            "question_runs_table": "agent_question_runs_test",
        }},
    }
    with pytest.raises(ConfigurationError, match="dsn"):
        config_mod.validate_profile_overlay(overlay, "test")


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


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_raw(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
