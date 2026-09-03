"""Acceptance-тесты: инвариант «skill независим от datasource».

Проверяем, что добавление таблицы/скрипта/индекса в runtime состояние
автоматически попадает в rendered SKILL.md — без правок Python-кода,
tools, prompts. Это финальный acceptance-критерий плана.

Сценарий:
1. Регистрируем базовый набор ресурсов (1 таблица, 1 скрипт, 1 индекс);
2. Заполняем auto-populated env-vars через _populate_skill_catalog_env;
3. Рендерим SKILL.md — каталог содержит базовые ресурсы;
4. Добавляем новую таблицу/скрипт/индекс в runtime;
5. Re-populate env-vars;
6. Рендерим SKILL.md — каталог содержит новые ресурсы;
7. SKILL.md template не менялся между шагами 3 и 6.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lib.services.table_registry import (
    SkillRegistration,
    TableResource,
    table_registry,
)
from lib.utils.skill_catalog import SkillCatalog


@pytest.fixture(autouse=True)
def _isolate_env():
    saved = {k: v for k, v in os.environ.items() if k.startswith("SKILL_")}
    for k in list(os.environ):
        if k.startswith("SKILL_"):
            del os.environ[k]
    yield
    for k in list(os.environ):
        if k.startswith("SKILL_"):
            del os.environ[k]
    for k, v in saved.items():
        os.environ[k] = v


@pytest.fixture(autouse=True)
def _reset_registry():
    table_registry.clear()
    yield
    table_registry.clear()


_SKILL_TEMPLATE = """\
# My Skill

Описание навыка.

## Predefined scripts

{{SCRIPTS_CATALOG}}

## Vector indexes

{{VECTORS_CATALOG}}

## Tables

{{TABLES_CATALOG}}
"""


def _fake_scripts_provider(scripts: list[tuple[str, str]]) -> MagicMock:
    """Создаёт mock DuckDB-provider с указанными (name, description) скриптами."""
    provider = MagicMock()
    provider.query_sql.return_value = {
        "status": "success",
        "rows": [
            {"name": name, "description": desc}
            for name, desc in scripts
        ],
    }
    provider.close = MagicMock()
    return provider


class TestAddedTableVisibleInRenderedSkill:
    """Добавление таблицы → каталог обновляется без правки кода."""

    def test_added_table_appears_in_catalog(self, tmp_path) -> None:
        table_registry.register(SkillRegistration(
            name="audit_analyzer",
            resources=(TableResource(name="oarb.audits"),),
        ))

        fake_provider = _fake_scripts_provider([])
        fake_cache = tmp_path / "cache.duckdb"
        fake_cache.write_bytes(b"")
        fake_vec_cfg = {}

        from lib.core.application_context import _populate_skill_catalog_env

        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value=fake_vec_cfg,
        ), patch(
            "lib.services.table_registry.table_registry.snapshot_path",
            return_value=fake_cache,
        ), patch(
            "lib.services.cache_provider_impl.PostgresDuckDbProvider",
            return_value=fake_provider,
        ):
            _populate_skill_catalog_env()

        rendered_before = SkillCatalog.render_expanded_skill(
            "audit_analyzer", _SKILL_TEMPLATE
        )
        assert "`oarb.audits`" in rendered_before
        assert "`oarb.audit_comments`" not in rendered_before

        table_registry.unregister("audit_analyzer")
        table_registry.register(SkillRegistration(
            name="audit_analyzer",
            resources=(
                TableResource(name="oarb.audits"),
                TableResource(name="oarb.audit_comments"),
            ),
        ))

        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value=fake_vec_cfg,
        ), patch(
            "lib.services.table_registry.table_registry.snapshot_path",
            return_value=fake_cache,
        ), patch(
            "lib.services.cache_provider_impl.PostgresDuckDbProvider",
            return_value=fake_provider,
        ):
            _populate_skill_catalog_env()

        rendered_after = SkillCatalog.render_expanded_skill(
            "audit_analyzer", _SKILL_TEMPLATE
        )
        assert "`oarb.audits`" in rendered_after
        assert "`oarb.audit_comments`" in rendered_after

        assert rendered_before != rendered_after


class TestAddedScriptVisibleInRenderedSkill:
    """Добавление скрипта в agent_predefined_scripts → каталог обновляется."""

    def test_added_script_appears_in_catalog(self, tmp_path) -> None:
        fake_provider = _fake_scripts_provider([
            ("audit_status_summary", "Сводка"),
        ])
        fake_cache = tmp_path / "cache.duckdb"
        fake_cache.write_bytes(b"")

        table_registry.register(SkillRegistration(
            name="audit_analyzer",
            resources=(
                TableResource(name="oarb.audits"),
                TableResource(
                    name="public.agent_predefined_scripts",
                    label="scripts_registry",
                ),
            ),
        ))

        from lib.core.application_context import _populate_skill_catalog_env

        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value={},
        ), patch(
            "lib.services.table_registry.table_registry.snapshot_path",
            return_value=fake_cache,
        ), patch(
            "lib.services.cache_provider_impl.PostgresDuckDbProvider",
            return_value=fake_provider,
        ):
            _populate_skill_catalog_env()

        rendered_before = SkillCatalog.render_expanded_skill(
            "audit_analyzer", _SKILL_TEMPLATE
        )
        assert "`audit_status_summary`" in rendered_before
        assert "`critical_findings`" not in rendered_before

        fake_provider.query_sql.return_value = {
            "status": "success",
            "rows": [
                {"name": "audit_status_summary", "description": "Сводка"},
                {"name": "critical_findings", "description": "Критические"},
            ],
        }

        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value={},
        ), patch(
            "lib.services.table_registry.table_registry.snapshot_path",
            return_value=fake_cache,
        ), patch(
            "lib.services.cache_provider_impl.PostgresDuckDbProvider",
            return_value=fake_provider,
        ):
            _populate_skill_catalog_env()

        rendered_after = SkillCatalog.render_expanded_skill(
            "audit_analyzer", _SKILL_TEMPLATE
        )
        assert "`audit_status_summary`" in rendered_after
        assert "`critical_findings`" in rendered_after
        assert "Критические" in rendered_after


class TestAddedVectorVisibleInRenderedSkill:
    """Добавление vector-index в agent_vector_index_config → каталог обновляется."""

    def test_added_vector_appears_in_catalog(self) -> None:
        fake_vec_cfg = {
            "audits_index": {"description": "Поиск проверок"},
        }

        table_registry.register(SkillRegistration(
            name="audit_analyzer",
            resources=(TableResource(name="oarb.audits"),),
        ))

        from lib.core.application_context import _populate_skill_catalog_env

        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value=fake_vec_cfg,
        ):
            _populate_skill_catalog_env()

        rendered_before = SkillCatalog.render_expanded_skill(
            "audit_analyzer", _SKILL_TEMPLATE
        )
        assert "`audits_index`" in rendered_before
        assert "`findings_semantic`" not in rendered_before

        fake_vec_cfg["findings_semantic"] = {
            "description": "Поиск по findings",
        }

        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value=fake_vec_cfg,
        ):
            _populate_skill_catalog_env()

        rendered_after = SkillCatalog.render_expanded_skill(
            "audit_analyzer", _SKILL_TEMPLATE
        )
        assert "`audits_index`" in rendered_after
        assert "`findings_semantic`" in rendered_after
        assert "Поиск по findings" in rendered_after


class TestRemovedTableDoesNotAppear:
    """Удаление таблицы → каталог НЕ содержит её."""

    def test_removed_table_disappears_from_catalog(self) -> None:
        table_registry.register(SkillRegistration(
            name="audit_analyzer",
            resources=(
                TableResource(name="oarb.audits"),
                TableResource(name="oarb.violations"),
            ),
        ))

        from lib.core.application_context import _populate_skill_catalog_env

        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value={},
        ):
            _populate_skill_catalog_env()

        rendered_before = SkillCatalog.render_expanded_skill(
            "audit_analyzer", _SKILL_TEMPLATE
        )
        assert "`oarb.audits`" in rendered_before
        assert "`oarb.violations`" in rendered_before

        table_registry.unregister("audit_analyzer")
        table_registry.register(SkillRegistration(
            name="audit_analyzer",
            resources=(TableResource(name="oarb.audits"),),
        ))

        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value={},
        ):
            _populate_skill_catalog_env()

        rendered_after = SkillCatalog.render_expanded_skill(
            "audit_analyzer", _SKILL_TEMPLATE
        )
        assert "`oarb.audits`" in rendered_after
        assert "`oarb.violations`" not in rendered_after


class TestTemplateUnchangedAcrossChanges:
    """Главный инвариант: template SKILL.md не меняется при изменении datasource."""

    def test_template_unmodified_when_resources_change(self) -> None:
        template = _SKILL_TEMPLATE
        original_template = template

        table_registry.register(SkillRegistration(
            name="audit_analyzer",
            resources=(TableResource(name="oarb.audits"),),
        ))

        from lib.core.application_context import _populate_skill_catalog_env

        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value={},
        ):
            _populate_skill_catalog_env()
        r1 = SkillCatalog.render_expanded_skill("audit_analyzer", template)

        table_registry.unregister("audit_analyzer")
        table_registry.register(SkillRegistration(
            name="audit_analyzer",
            resources=(
                TableResource(name="oarb.audits"),
                TableResource(name="oarb.violations"),
                TableResource(name="oarb.audit_reports"),
            ),
        ))

        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value={},
        ):
            _populate_skill_catalog_env()
        r2 = SkillCatalog.render_expanded_skill("audit_analyzer", template)

        assert r1 != r2
        assert template == original_template


class TestAcceptanceScenarioEndToEnd:
    """Полный сценарий: добавляем ресурсы через конфиг/БД — SKILL.md обновляется."""

    def test_full_acceptance(self, tmp_path) -> None:
        """Эмуляция §43 acceptance-теста: добавляем таблицу/скрипт/индекс."""
        table_registry.register(SkillRegistration(
            name="audit_analyzer",
            resources=(
                TableResource(name="oarb.audits"),
                TableResource(
                    name="public.agent_predefined_scripts",
                    label="scripts_registry",
                ),
            ),
        ))

        fake_provider = _fake_scripts_provider([
            ("audit_status_summary", "Сводка по статусам"),
        ])
        fake_cache = tmp_path / "cache.duckdb"
        fake_cache.write_bytes(b"")
        fake_vec_cfg = {"audits_index": {"description": "Поиск проверок"}}

        from lib.core.application_context import _populate_skill_catalog_env

        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value=fake_vec_cfg,
        ), patch(
            "lib.services.table_registry.table_registry.snapshot_path",
            return_value=fake_cache,
        ), patch(
            "lib.services.cache_provider_impl.PostgresDuckDbProvider",
            return_value=fake_provider,
        ):
            _populate_skill_catalog_env()

        rendered_initial = SkillCatalog.render_expanded_skill(
            "audit_analyzer", _SKILL_TEMPLATE
        )
        assert "`oarb.audits`" in rendered_initial
        assert "`oarb.audit_comments`" not in rendered_initial
        assert "`audit_status_summary`" in rendered_initial
        assert "`critical_findings`" not in rendered_initial
        assert "`audits_index`" in rendered_initial
        assert "`findings_semantic`" not in rendered_initial

        table_registry.unregister("audit_analyzer")
        table_registry.register(SkillRegistration(
            name="audit_analyzer",
            resources=(
                TableResource(name="oarb.audits"),
                TableResource(name="oarb.audit_comments"),
                TableResource(
                    name="public.agent_predefined_scripts",
                    label="scripts_registry",
                ),
            ),
        ))
        fake_provider.query_sql.return_value = {
            "status": "success",
            "rows": [
                {"name": "audit_status_summary", "description": "Сводка по статусам"},
                {"name": "critical_findings", "description": "Критические находки"},
            ],
        }
        fake_vec_cfg["findings_semantic"] = {"description": "Поиск findings"}

        with patch(
            "lib.services.cache_provider_impl.read_vector_index_config",
            return_value=fake_vec_cfg,
        ), patch(
            "lib.services.table_registry.table_registry.snapshot_path",
            return_value=fake_cache,
        ), patch(
            "lib.services.cache_provider_impl.PostgresDuckDbProvider",
            return_value=fake_provider,
        ):
            _populate_skill_catalog_env()

        rendered_after = SkillCatalog.render_expanded_skill(
            "audit_analyzer", _SKILL_TEMPLATE
        )

        assert "`oarb.audit_comments`" in rendered_after
        assert "`critical_findings`" in rendered_after
        assert "Критические находки" in rendered_after
        assert "`findings_semantic`" in rendered_after
        assert "Поиск findings" in rendered_after

        assert "`oarb.audits`" in rendered_after
        assert "`audit_status_summary`" in rendered_after
        assert "`audits_index`" in rendered_after
