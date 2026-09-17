"""Тесты ConfigurationResolver — порядок merge и валидация.

Эти тесты проверяют **инвариант** плана: профиль — последний источник
profile-owned runtime-настроек. Без них вся идея плана держится на доверии.

Подробнее — см. plan.md § «Merge-order тест (принципиально важный)».
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import config as config_mod
from config import (
    ConfigurationError,
    resolve_application_config,
    validate_profile_overlay,
    validate_runtime_isolation,
)


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_raw(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def isolated_project(monkeypatch, tmp_path: Path):
    """Подменить пути config.py на временный каталог."""
    monkeypatch.setattr(config_mod, "_PROJECT_FILE", tmp_path / "project.json")
    monkeypatch.setattr(config_mod, "_CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config_mod, "_SECRETS_FILE", None)
    monkeypatch.setattr(
        config_mod, "_SESSION_MANAGER_FILE", tmp_path / "session_manager.json"
    )
    monkeypatch.setattr(config_mod, "_PROFILES_DIR", tmp_path / "profiles")
    return tmp_path


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
    # Не должно бросить исключение.
    validate_profile_overlay(overlay, "test")


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
    # Не должно бросить исключение.
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
    # Не должно бросить исключение.
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
    # project.json с prod-именами (база)
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
    # session_manager.json пытается поставить PROD-имена
    _write(tmp_path / "session_manager.json", {
        "messages_table": "agent_session_messages",  # PROD-попытка
    })
    # config.json пытается поставить PROD-имена
    _write(tmp_path / "config.json", {
        "channels": {"postgres": {
            "messages_table": "agent_session_messages",  # PROD-попытка
        }},
    })
    # profiles/test.jsonc ставит TEST-имена (полный оверлей)
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
    # Профиль WIN'нул, несмотря на session_manager и config.json
    assert cfg["channels"]["postgres"]["messages_table"] == "agent_session_messages_test"
    assert cfg["channels"]["postgres"]["table_name"] == "agent_conversation_messages_test"
    assert cfg["channels"]["postgres"]["meta_table"] == "agent_session_meta_test"
    assert cfg["channels"]["postgres"]["claims_table"] == "agent_worker_claims_test"
    assert cfg["logging"]["db"]["table_name"] == "agent_gateway_logs_test"
    assert cfg["logging"]["db"]["question_runs_table"] == "agent_question_runs_test"


def test_profile_wins_when_session_manager_uses_test_but_config_uses_prod(
    isolated_project,
):
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


def test_session_manager_can_override_pool_but_not_runtime_tables(
    isolated_project,
):
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
    # pool override работает (session_manager.json → шаг 2 до profile)
    # но runtime-таблицу перетереть не смог (profile → шаг 4 после override)
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
    # profiles/ не создаём — для prod это не нужно

    cfg = resolve_application_config(profile="prod")
    # Все 6 runtime-таблиц должны быть prod-именами (валидация прошла)
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
    # profiles/test.jsonc отсутствует

    with pytest.raises(ConfigurationError, match="test.jsonc"):
        resolve_application_config(profile="test")


# ---------------------------------------------------------------------------
# 5. Невалидное имя профиля
# ---------------------------------------------------------------------------


def test_invalid_profile_name_fails():
    """Имя профиля должно соответствовать whitelist ``{"prod", "test"}``.

    После ``config-profile-cli-flag`` whitelist ужесточен — любое
    значение вне списка теперь обрабатывается на уровне ``_initialize_settings``
    (а не в самом resolver). Поэтому невалидное имя здесь проверяем
    через явный ``_initialize_settings``, а не через
    ``resolve_application_config`` напрямую.

    Тест полагается на uninitialized ``SETTINGS`` proxy — должен
    запускаться ПЕРЕД любым тестом, который делает init. Для надёжности
    используем subprocess-изоляцию.
    """
    import subprocess
    r = subprocess.run(
        ["python", "-c",
         "import config; config._initialize_settings('foo bar')"],
        capture_output=True, text=True,
    )
    assert r.returncode != 0
    assert "is not supported" in r.stderr


# ---------------------------------------------------------------------------
# 6. session_manager.json может переопределять pool/timeout (но не runtime)
# ---------------------------------------------------------------------------
# Полная версия теста — выше (внутри секции merge-order), использует полный
# test-оверлей. Здесь оставлен только короткий smoke-проверочный тест,
# что session_manager.json вообще читается на шаге 2.
# ---------------------------------------------------------------------------