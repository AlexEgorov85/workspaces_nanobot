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

    def test_export_sets_workspace(self, monkeypatch) -> None:
        """Третий факт о запуске: рабочий каталог агента.

        Объявляется не в ``config.json``, а в ``platform.json`` — им разворачивается
        ``execution.session_root``, корень файлов сессии. Каталог обязан лежать
        внутри рабочего каталога агента: при включённой границе файловых
        инструментов запись в папку сессии вне него отклоняется, и агент не смог
        бы положить туда ни одного файла.
        """
        monkeypatch.delenv("NANOBOT_WORKSPACE", raising=False)

        config._export_runtime_env()

        assert Path(os.environ["NANOBOT_WORKSPACE"]) == PROJECT_ROOT / "workspace"

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


# --- объявление корня файлов сессии --------------------------------------


def _session_root_declared() -> str:
    """``platform.json → execution.session_root`` как он записан в файле."""
    raw = json.loads(
        (PROJECT_ROOT / "mcp-platform" / "platform.json").read_text(encoding="utf-8")
    )
    return raw["execution"]["session_root"]


class TestSessionRootDeclaration:
    """Корень объявлен один раз, платформой, и лежит внутри рабочего каталога.

    Требование change ``2026-10-03-session-files`` (п. 1.1). Проверяется объявление,
    а не работающая платформа: подстановку разворачивает сервер, и для этого
    хватает строки в файле.
    """

    def test_declared_through_the_workspace_variable(self) -> None:
        """Объявление обязано быть развёрнутым до абсолютного пути подстановкой.

        Литерал или ``${NANOBOT_PROJECT_ROOT}`` вернули бы корень, который агент
        не пишет: ``workspace/`` — это не корень проекта, а каталог внутри него.
        """
        declared = _session_root_declared()
        assert declared == "${NANOBOT_WORKSPACE}/data_store/sessions", declared

    def test_expanded_root_stays_inside_the_workspace(self, tmp_path) -> None:
        """Резолв обязана привести внутрь рабочего каталога агента.

        Иначе граница файловых инструментов (``allowed_root`` = корень проекта)
        отклонит запись в папку сессии, и объявление окажется верным только на
        бумаге.
        """
        workspace = tmp_path / "workspace"
        expanded = _session_root_declared().replace(
            "${NANOBOT_WORKSPACE}", str(workspace)
        )
        assert Path(expanded).is_relative_to(workspace), expanded


# --- секция enterprise_mcp в config.json --------------------------------


def _load_enterprise_mcp_raw() -> dict:
    """Секция объявления сервера — как она лежит в файле.

    В корне ``config.json`` её быть не может: корневой объект разбирает
    схема nanobot, а она отвергает неизвестный ключ верхнего уровня. Поэтому
    секция живёт под ``gateway.agent.enterprise_mcp`` и поднимается в
    ``SETTINGS`` функцией ``config._lift_agent_sections`` — здесь читается
    именно файл, чтобы проверять объявление как оно записано, а не то, что
    из него получилось после мерджа с профилями.
    """
    raw = json.loads((PROJECT_ROOT / "config.json").read_text(encoding="utf-8"))
    return raw["gateway"]["agent"]["enterprise_mcp"]


class TestEnterpriseMcpSection:
    def test_section_exists_and_is_typed(self) -> None:
        section = _load_enterprise_mcp_raw()
        assert section, "раздел enterprise_mcp обязателен: без него адаптер не работает"
        validate_project_settings({"enterprise_mcp": section})

    def test_no_machine_specific_path_is_committed(self) -> None:
        """Пути конкретной машины в конфиге — это то, что ломает перенос."""
        raw = (PROJECT_ROOT / "config.json").read_text(encoding="utf-8")
        assert "C:\\" not in raw
        assert "C:/" not in raw

    def test_paths_are_env_references(self) -> None:
        section = _load_enterprise_mcp_raw()
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

    def test_operations_reach_the_model_through_mcp_servers(self) -> None:
        """Операции отдаются модели штатным MCP, а не самописными обёртками.

        Раньше ``mcpServers`` обязан был быть пустым: платформа была видна
        агенту только через собственный клиент, а модели — через
        ``audit_analyzer_query``/``legal_summarizer_query``/``history_search``
        с переписанными схемами. Теперь объявление есть, и модель видит
        настоящие ``inputSchema`` платформы.

        Состав объявления проверяет ``tests/test_mcp_platform_declaration.py``;
        здесь — только сам факт и честность про второй процесс.
        """
        raw = json.loads((PROJECT_ROOT / "config.json").read_text(encoding="utf-8"))
        servers = raw.get("tools", {}).get("mcpServers", {})
        assert "enterprise" in servers

    def test_second_process_is_declared_not_accidental(self) -> None:
        """Процессов платформы два, и это объявлено, а не случайно вышло.

        Один поднимает нанобот — для модели (это объявление выше). Второй
        поднимает агент для фоновых служб: ``queue_ops`` воркера канала,
        ``session_cold_sync``, журнал через ``log_events`` и запись о сжатии
        контекста ходят в платформу ВНЕ оборота, где MCP-инструмента модели
        нет и быть не может — там нужен клиент, а не tool.

        Пока оба объявления существуют, страж на «одного владельца пула» был бы
        неправдой; он и удалён. Обратно объединять их можно будет не правкой
        конфига, а снятием клиента агента — отдельной задачей.
        """
        raw = json.loads((PROJECT_ROOT / "config.json").read_text(encoding="utf-8"))
        assert raw["tools"]["mcpServers"]["enterprise"]["enabled_tools"]
        section = _load_enterprise_mcp_raw()
        assert section["enabled"] is True
        # Клиент агента остаётся: фоновые службы ходят в платформу вне оборота.
        assert section["command"] == "${NANOBOT_PYTHON}"


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
