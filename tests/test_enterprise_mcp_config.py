"""Тесты объявления MCP-сервера ``enterprise-mcp``.

Сервер поднимает ``fail-fast`` по ``DATABASE_URL`` и живёт на интерпретаторе,
в котором установлены его зависимости. Обе предпосылки приходят из
``config.json`` как ``${VAR}`` и резолвятся в ``os.environ`` — если этот
механизм отвалится, сервер не поднимется, а агент сообщит об этом
структурной ошибкой в момент первого вызова.

Проверяем на заведомо плохих данных: переменная не экспортирована,
раздел неполный, путь остался нерезолвленным.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import config
from lib.core.project_settings import (
    EnterpriseMcpSettings,
    ProjectSettings,
    validate_project_settings,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# --- экспорт фактов о запуске -------------------------------------------


class TestRuntimeEnvExport:
    def test_export_sets_interpreter_and_root(self, monkeypatch) -> None:
        monkeypatch.delenv("NANOBOT_PYTHON", raising=False)
        monkeypatch.delenv("NANOBOT_PROJECT_ROOT", raising=False)

        config._export_runtime_env()

        assert os.environ["NANOBOT_PYTHON"] == os.sys.executable
        assert Path(os.environ["NANOBOT_PROJECT_ROOT"]) == PROJECT_ROOT

    def test_external_value_wins(self, monkeypatch) -> None:
        """Оператор может указать другой интерпретатор осознанно."""
        monkeypatch.setenv("NANOBOT_PYTHON", "/custom/python")
        config._export_runtime_env()
        assert os.environ["NANOBOT_PYTHON"] == "/custom/python"

    def test_export_runs_before_config_resolution(self) -> None:
        """Порядок обязателен: иначе ``${NANOBOT_PYTHON}`` остался бы
        литералом и сервер не поднялся бы."""
        source = (PROJECT_ROOT / "config.py").read_text(encoding="utf-8")
        assert source.index("_export_runtime_env()") < source.index(
            "cfg = _resolve_env_refs(cfg)"
        )


# --- секция project.json -------------------------------------------------


def _load_project_raw() -> dict:
    """project.json — JSONC (с комментариями), голый json.loads не берёт."""
    return dict(config.load_config_json(PROJECT_ROOT / "project.json"))


class TestProjectSection:
    def test_section_exists_and_is_typed(self) -> None:
        section = _load_project_raw().get("enterprise_mcp")
        assert section, "раздел enterprise_mcp обязателен: без него адаптер не работает"
        validate_project_settings({"enterprise_mcp": section})

    def test_no_machine_specific_path_is_committed(self) -> None:
        """Пути конкретной машины в конфиге — это то, что ломает перенос."""
        raw = (PROJECT_ROOT / "project.json").read_text(encoding="utf-8")
        assert "C:\\" not in raw
        assert "C:/" not in raw

    def test_paths_are_env_references(self) -> None:
        section = _load_project_raw()["enterprise_mcp"]
        assert section["command"] == "${NANOBOT_PYTHON}"
        assert section["cwd"].startswith("${NANOBOT_PROJECT_ROOT}")

    def test_unresolved_reference_is_visible_as_literal(self) -> None:
        """Страж: если резолв сломается, в конфиге останется ``${...}``.

        Клиент не должен молча запустить сервер с путём-пустышкой.
        """
        client_like = {"command": "${NANOBOT_PYTHON}", "cwd": "${NANOBOT_PROJECT_ROOT}/x"}
        from lib.services.enterprise_mcp_client import client_from_settings

        client = client_from_settings(
            {"enterprise_mcp": {"enabled": True, **client_like}}
        )
        assert client is not None
        # Неразрезолвенная переменная видна как есть — её видно в баннере
        # запуска, и это лучше, чем запуск несуществующего интерпретатора.
        assert client.describe()["command"].startswith("$")

    def test_mcp_servers_in_nanobot_config_stays_empty(self) -> None:
        """Пока операции не отдаются модели, вторая копия процесса не нужна.

        Владелец пула PostgreSQL должен быть один — это и есть регистрация
        клиента агента плюс пустой ``mcpServers``.
        """
        raw = json.loads((PROJECT_ROOT / "config.json").read_text(encoding="utf-8"))
        assert raw.get("tools", {}).get("mcpServers") == {}


# --- валидация модели ----------------------------------------------------


class TestSettingsModel:
    def test_defaults_are_all_optional(self) -> None:
        """Отсутствие раздела не должно быть ошибкой конфигурации."""
        assert EnterpriseMcpSettings() is not None
        validate_project_settings({})

    def test_tool_timeout_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            ProjectSettings(enterprise_mcp={"tool_timeout_sec": 0})

    def test_extra_keys_allowed_but_known_ones_typed(self) -> None:
        """Секция расширяема (extra=allow), но известные ключи типизированы."""
        section = EnterpriseMcpSettings(enabled=True, future_knob=5)
        assert section.enabled is True
        assert section.future_knob == 5
