"""Тесты для ``lib/core/skill_config.py`` — единый API для всех skill'ов.

Модуль перестал выдавать доступ к снимку (фаза 5, п. 5.6): ``build_cache_provider``,
``get_in_memory_cache_path``, ``get_vector_index_path``, ``get_vector_db_table``,
``get_vector_indexes``, ``get_embedding_config``/``get_embedding_model`` убраны
вместе с их тестами. Осталось конфиг-навыка и состав данных через TableRegistry.

Класс ``TestVectorIndexConfigFromSettings`` удалён 2026-10-01 вместе с
``cache_provider_impl``: он проверял, что объявление индексов читается из
``project.json``. Индексы объявляет платформа (``platform.json → vectors``), и
читает их ``libs/vectors/config.py``; держать второй источник правды в
``project.json`` было бы ровно тем расхождением, которое породило баг с
расхождением путей к снимку.
"""

from __future__ import annotations
from tests.conftest import TEST_TABLE, TEST_TABLE_2

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
                {"name": TEST_TABLE},
                {"name": "public.scripts", "label": "scripts_registry"},
                {"name": TEST_TABLE_2},
            ]
        }
        with patch("config.SETTINGS", _settings_with({"audit_analyzer": cfg})):
            assert skill_config.get_db_tables("audit_analyzer") == [
                TEST_TABLE,
                TEST_TABLE_2,
            ]

    def test_string_table_entries_supported(self) -> None:
        from lib.core import skill_config

        cfg = {"tables": [TEST_TABLE, TEST_TABLE_2]}
        with patch("config.SETTINGS", _settings_with({"audit_analyzer": cfg})):
            assert skill_config.get_db_tables("audit_analyzer") == [
                TEST_TABLE,
                TEST_TABLE_2,
            ]

    def test_empty_tables_returns_empty(self) -> None:
        from lib.core import skill_config

        with patch("config.SETTINGS", _settings_with({"audit_analyzer": {}})):
            assert skill_config.get_db_tables("audit_analyzer") == []


class TestDbSchema:
    def test_schema_from_first_table(self) -> None:
        from lib.core import skill_config

        cfg = {"tables": [{"name": TEST_TABLE}, {"name": TEST_TABLE_2}]}
        with patch("config.SETTINGS", _settings_with({"audit_analyzer": cfg})):
            assert skill_config.get_db_schema("audit_analyzer") == TEST_TABLE.split(".", 1)[0]

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


class TestMultiSkill:
    def test_two_skills_independent(self) -> None:
        """Два skill'а в одном SETTINGS — каждый получает свои таблицы."""
        from lib.core import skill_config

        settings = {
            "skills": {
                "audit_analyzer": {"tables": [{"name": TEST_TABLE}]},
                "office_files": {"tables": [{"name": "ofx.docs"}]},
            }
        }
        with patch("config.SETTINGS", settings):
            assert skill_config.get_db_tables("audit_analyzer") == [TEST_TABLE]
            assert skill_config.get_db_tables("office_files") == ["ofx.docs"]
            assert skill_config.get_db_schema("audit_analyzer") == TEST_TABLE.split(".", 1)[0]
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


# После change ``remove-vector-index-store`` функция
# ``skill_config.get_vector_store_table`` удалена (persisted FAISS-кеш
# больше не существует). Класс ``TestVectorStoreTable`` удалён вместе с
# ней.


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


class TestNoCacheApiRemains:
    """Cache-API не должен вернуться в skill-side API (фаза 5, п. 5.6).

    Проверка на **отсутствие**, а не на поведение: у снятой функции нет
    предмета для вызова, но её можно снова объявить — и тогда навык снова
    начнёт доставать снимок у агента, то есть у файла появятся два
    владельца: capability ``data`` платформы и агент. Расхождение при этом
    молчаливое, поэтому нужен явный страж.

    Проверено мутацией: возврат ``build_cache_provider`` в
    ``lib/core/skill_config.py`` ловится этим тестом, а не падением
    какого-нибудь импорта.
    """

    #: Все функции, выдававшие доступ к снимку, векторам и эмбеддингам.
    GONE = (
        "build_cache_provider",
        "get_in_memory_cache_path",
        "get_vector_index_path",
        "get_vector_db_table",
        "get_vector_indexes",
        "get_embedding_config",
        "get_embedding_model",
        "_vector_indexes_list",
    )

    def test_no_skill_module_exposes_cache_api(self) -> None:
        from lib.core import skill_config

        for name in self.GONE:
            assert not hasattr(skill_config, name), (
                f"{name!r} возвращён в skill-side API — снимком владеет "
                "capability data платформы, а агент был бы вторым владельцем"
            )

    def test_skill_config_does_not_import_cache_provider(self) -> None:
        """Модуль не должен тянуть реализацию хранилища даже лениво.

        Даже без объявленных функций остаточная ссылка на
        ``lib.services.cache_provider`` означала бы, что skill-side API
        всё ещё считает хранилище своим делом.
        """
        import ast
        import inspect

        source = inspect.getsource(
            __import__("lib.core.skill_config", fromlist=["skill_config"])
        )
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)

        for module in sorted(imported):
            assert "cache_provider" not in module, (
                f"skill_config импортирует {module!r} — доступ к хранилищу "
                "снят вместе с cache-API"
            )
