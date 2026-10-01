"""Конфигурация capability ``audit``, уезжающая в процесс сервера.

Дефект, который закрывает этот файл: агент не экспортировал
``ENTERPRISE_SCRIPTS_REGISTRY_TABLE`` и ``ENTERPRISE_AUDIT_TABLES``. У обеих
переменных на стороне платформы пустой дефолт, поэтому capability ``audit``
отвечала ``registry_unavailable`` на **каждую** операцию — то есть была мертва
в развёртывании, при этом её юнит-тесты оставались зелёными: они ставят
переменные через ``monkeypatch.setenv`` и сами же создают реестр.

Проверяется не «значение верное», а **контракт доезда**: любая переменная,
которую платформа читает без дефолта, обязана иметь путь из конфигурации
агента. Иначе capability выключается молча.
"""

from __future__ import annotations

from typing import Any

from lib.services import enterprise_mcp_client as mod

#: Переменные платформы, без которых capability ``audit`` не работает вовсе.
#: Пустой дефолт + неэкспортируемая переменная = мёртвая capability.
REQUIRED_BY_AUDIT = ("ENTERPRISE_SCRIPTS_REGISTRY_TABLE", "ENTERPRISE_AUDIT_TABLES")


def _project(tables: list[dict[str, Any]]) -> Any:
    """Подменить чтение project.json таблицами навыка audit_analyzer."""
    from lib.services import enterprise_mcp_client as module

    class _FakeConfig:
        @staticmethod
        def load_config_json(_name: str) -> dict:
            return {"skills": {"audit_analyzer": {"tables": tables}}}

    original = module.__dict__.get("load_config_json")
    return _FakeConfig, original


class TestAuditEnvReachesChildEnv:
    def test_required_variables_are_exported(self, monkeypatch) -> None:
        import config

        monkeypatch.setattr(
            config,
            "load_config_json",
            lambda _name: {
                "skills": {
                    "audit_analyzer": {
                        "tables": [
                            {"name": "oarb.audits"},
                            {"name": "oarb.violations"},
                            {
                                "name": "public.agent_predefined_scripts",
                                "label": "scripts_registry",
                            },
                        ]
                    }
                }
            },
        )
        env = mod._audit_env_from_project()
        for name in REQUIRED_BY_AUDIT:
            assert name in env, f"{name} не доезжает — capability audit мертва"

    def test_registry_table_is_found_by_label_not_by_name(self, monkeypatch) -> None:
        """Реестр опознаётся по ``label``, а не по имени таблицы.

        Имя в конфиге — деталь реализации; ``label`` и есть его роль.
        """
        import config

        monkeypatch.setattr(
            config,
            "load_config_json",
            lambda _name: {
                "skills": {
                    "audit_analyzer": {
                        "tables": [
                            {"name": "some.other.registry", "label": "scripts_registry"}
                        ]
                    }
                }
            },
        )
        env = mod._audit_env_from_project()
        assert env["ENTERPRISE_SCRIPTS_REGISTRY_TABLE"] == "some.other.registry"

    def test_registry_is_excluded_from_audit_tables(self, monkeypatch) -> None:
        """``label`` означает «реестр метаданных, не для схемы модели».

        Выдав его как доменную таблицу, мы разрешили бы аудиту читать свои
        предустановленные скрипты в обход проверки строк.
        """
        import config

        monkeypatch.setattr(
            config,
            "load_config_json",
            lambda _name: {
                "skills": {
                    "audit_analyzer": {
                        "tables": [
                            {"name": "oarb.audits"},
                            {
                                "name": "public.agent_predefined_scripts",
                                "label": "scripts_registry",
                            },
                        ]
                    }
                }
            },
        )
        env = mod._audit_env_from_project()
        tables = env["ENTERPRISE_AUDIT_TABLES"].split(",")
        assert tables == ["oarb.audits"]
        assert "public.agent_predefined_scripts" not in tables

    def test_child_env_includes_audit(self, monkeypatch) -> None:
        """Проверяется именно ``_child_env`` — то, что реально видит процесс."""
        monkeypatch.setattr(mod, "_audit_env_from_project", lambda: {
            "ENTERPRISE_SCRIPTS_REGISTRY_TABLE": "r",
            "ENTERPRISE_AUDIT_TABLES": "t1,t2",
        })
        monkeypatch.setattr(mod, "_vectors_env_from_settings", lambda _p: {})
        client = mod.EnterpriseMcpClient(command="python")
        env = client._child_env()
        assert env["ENTERPRISE_SCRIPTS_REGISTRY_TABLE"] == "r"
        assert env["ENTERPRISE_AUDIT_TABLES"] == "t1,t2"

    def test_broken_project_json_does_not_break_startup(self, monkeypatch) -> None:
        """Отсутствие конфига аудита не имеет права ронять старт агента."""

        def _boom(_name: str) -> dict:
            raise ValueError("битый project.json")

        import config

        monkeypatch.setattr(config, "load_config_json", _boom)
        assert mod._audit_env_from_project() == {}


class TestAuditEnvEdgeCases:
    def test_empty_tables_produces_no_variables(self, monkeypatch) -> None:
        import config

        monkeypatch.setattr(
            config, "load_config_json", lambda _n: {"skills": {"audit_analyzer": {}}}
        )
        assert mod._audit_env_from_project() == {}

    def test_malformed_entries_are_skipped_not_fatal(self, monkeypatch) -> None:
        """Кривой элемент списка не должен уносить с собой весь экспорт."""
        import config

        monkeypatch.setattr(
            config,
            "load_config_json",
            lambda _n: {
                "skills": {
                    "audit_analyzer": {
                        "tables": [
                            "просто строка",
                            {"без имени": 1},
                            {"name": "  "},
                            {"name": "oarb.audits"},
                        ]
                    }
                }
            },
        )
        env = mod._audit_env_from_project()
        assert env["ENTERPRISE_AUDIT_TABLES"] == "oarb.audits"
