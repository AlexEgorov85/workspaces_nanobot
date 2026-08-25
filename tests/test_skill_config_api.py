"""Тесты для ``lib/core/skill_config.py`` — единый API для всех skill'ов."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


def _settings_with(skills: dict):
    return {"skills": skills}


class TestSkillLookup:
    def test_unknown_skill_raises(self) -> None:
        from lib.core import skill_config

        with patch("config.SETTINGS", _settings_with({"audit_analyzer": {}})):
            with pytest.raises(KeyError, match="unknown"):
                skill_config.get_db_tables("unknown")

    def test_non_dict_cfg_raises(self) -> None:
        from lib.core import skill_config

        with patch("config.SETTINGS", _settings_with({"audit_analyzer": "not-a-dict"})):
            with pytest.raises(KeyError):
                skill_config.get_db_tables("audit_analyzer")


class TestDbTables:
    def test_returns_unlabeled(self) -> None:
        from lib.core import skill_config

        cfg = {
            "tables": [
                {"name": "oarb.audits"},
                {"name": "public.scripts", "label": "scripts_registry"},
                {"name": "oarb.violations"},
            ]
        }
        with patch("config.SETTINGS", _settings_with({"audit_analyzer": cfg})):
            assert skill_config.get_db_tables("audit_analyzer") == [
                "oarb.audits",
                "oarb.violations",
            ]

    def test_string_table_entries_supported(self) -> None:
        from lib.core import skill_config

        cfg = {"tables": ["oarb.audits", "oarb.violations"]}
        with patch("config.SETTINGS", _settings_with({"audit_analyzer": cfg})):
            assert skill_config.get_db_tables("audit_analyzer") == [
                "oarb.audits",
                "oarb.violations",
            ]

    def test_empty_tables_returns_empty(self) -> None:
        from lib.core import skill_config

        with patch("config.SETTINGS", _settings_with({"audit_analyzer": {}})):
            assert skill_config.get_db_tables("audit_analyzer") == []


class TestDbSchema:
    def test_schema_from_first_table(self) -> None:
        from lib.core import skill_config

        cfg = {"tables": [{"name": "oarb.audits"}, {"name": "oarb.violations"}]}
        with patch("config.SETTINGS", _settings_with({"audit_analyzer": cfg})):
            assert skill_config.get_db_schema("audit_analyzer") == "oarb"

    def test_empty_tables_raises(self) -> None:
        from lib.core import skill_config

        with patch("config.SETTINGS", _settings_with({"audit_analyzer": {}})):
            with pytest.raises(ValueError, match="пуст"):
                skill_config.get_db_schema("audit_analyzer")

    def test_unqualified_name_raises(self) -> None:
        from lib.core import skill_config

        cfg = {"tables": [{"name": "audits"}]}
        with patch("config.SETTINGS", _settings_with({"audit_analyzer": cfg})):
            with pytest.raises(ValueError, match="fully qualified"):
                skill_config.get_db_schema("audit_analyzer")


class TestVectorDbTable:
    def test_from_gateway_config(self) -> None:
        from lib.core import skill_config

        settings = {
            "skills": {"audit_analyzer": {"tables": []}},
            "gateway": {"vector_index": {"storage_table": "oarb.audit_vectors"}},
        }
        with patch("config.SETTINGS", settings):
            assert skill_config.get_vector_db_table("audit_analyzer") == "oarb.audit_vectors"

    def test_fallback_to_tables_type_vector(self) -> None:
        from lib.core import skill_config

        settings = {
            "skills": {"audit_analyzer": {"tables": [
                {"name": "oarb.audit_vectors", "type": "vector"},
            ]}},
            "gateway": {},
        }
        with patch("config.SETTINGS", settings):
            assert skill_config.get_vector_db_table("audit_analyzer") == "oarb.audit_vectors"

    def test_returns_empty_when_no_storage(self) -> None:
        from lib.core import skill_config

        settings = {"skills": {"audit_analyzer": {"tables": []}}, "gateway": {}}
        with patch("config.SETTINGS", settings):
            assert skill_config.get_vector_db_table("audit_analyzer") == ""


class TestMultiSkill:
    def test_two_skills_independent(self) -> None:
        """Два skill'а в одном SETTINGS — каждый получает свои таблицы."""
        from lib.core import skill_config

        settings = {
            "skills": {
                "audit_analyzer": {"tables": [{"name": "oarb.audits"}]},
                "office_files": {"tables": [{"name": "ofx.docs"}]},
            }
        }
        with patch("config.SETTINGS", settings):
            assert skill_config.get_db_tables("audit_analyzer") == ["oarb.audits"]
            assert skill_config.get_db_tables("office_files") == ["ofx.docs"]
            assert skill_config.get_db_schema("audit_analyzer") == "oarb"
            assert skill_config.get_db_schema("office_files") == "ofx"

    def test_cli_config_per_skill(self) -> None:
        from lib.core import skill_config

        settings = {
            "skills": {
                "audit_analyzer": {"cli": {"max_retries": 5}},
                "office_files": {"cli": {"default_mode": "auto"}},
            }
        }
        with patch("config.SETTINGS", settings):
            assert skill_config.get_max_retries("audit_analyzer") == 5
            assert skill_config.get_max_retries("office_files") == 3
            assert skill_config.get_cli_config("office_files")["default_mode"] == "auto"


class TestVectorStoreTable:
    def test_infra_constant(self) -> None:
        from lib.core import skill_config

        assert skill_config.get_vector_store_table() == "public.agent_vector_index_store"


class TestPredefinedScripts:
    def test_lookup_from_table_registry(self) -> None:
        """``get_predefined_scripts_table`` идёт через TableRegistry, не через конфиг."""
        from lib.core import skill_config
        from lib.services.table_registry import (
            SkillRegistration,
            TableResource,
            table_registry,
        )

        table_registry.clear()
        table_registry.register(SkillRegistration(
            name="audit_analyzer",
            resources=(TableResource(name="public.scripts", label="scripts_registry"),),
        ))
        try:
            with patch("config.SETTINGS", _settings_with({"audit_analyzer": {}})):
                assert (
                    skill_config.get_predefined_scripts_table("audit_analyzer")
                    == "public.scripts"
                )
        finally:
            table_registry.clear()

    def test_raises_when_no_registry_label(self) -> None:
        from lib.core import skill_config
        from lib.services.table_registry import table_registry

        table_registry.clear()
        try:
            with patch("config.SETTINGS", _settings_with({"audit_analyzer": {}})):
                with pytest.raises(ValueError, match="scripts_registry"):
                    skill_config.get_predefined_scripts_table("audit_analyzer")
        finally:
            table_registry.clear()
