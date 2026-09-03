"""Unit-тесты для ``lib.utils.skill_catalog.SkillCatalog``.

Изолированы от ApplicationContext и runtime-БД: тесты работают только
с ``os.environ`` (auto-populated upstream'ом) и проверяют чистый
markdown-рендеринг.
"""
from __future__ import annotations

import os

import pytest

from lib.utils.skill_catalog import SkillCatalog


@pytest.fixture(autouse=True)
def _isolate_env():
    """Сохраняем и восстанавливаем ``SKILL_*`` env-vars между тестами."""
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


class TestRenderScriptsCatalog:
    """``{{SCRIPTS_CATALOG}}`` → markdown-таблица predefined scripts."""

    def test_renders_scripts_table(self) -> None:
        os.environ["SKILL_AUDIT_ANALYZER_SCRIPTS"] = (
            "audit_status_summary,top_violations_by_type"
        )
        os.environ["SKILL_AUDIT_ANALYZER_SCRIPT_DESCRIPTIONS"] = (
            "audit_status_summary=Сводка по статусам;"
            "top_violations_by_type=Топ кодов"
        )
        template = (
            "## Predefined scripts\n\n{{SCRIPTS_CATALOG}}\n\n## end"
        )
        out = SkillCatalog.render_expanded_skill("audit_analyzer", template)
        assert "## Predefined scripts" in out
        assert "| Script | Описание |" in out
        assert "|---|---|" in out
        assert "| `audit_status_summary` | Сводка по статусам |" in out
        assert "| `top_violations_by_type` | Топ кодов |" in out
        assert "{{SCRIPTS_CATALOG}}" not in out

    def test_empty_scripts_renders_placeholder(self) -> None:
        template = "before\n{{SCRIPTS_CATALOG}}\nafter"
        out = SkillCatalog.render_expanded_skill("audit_analyzer", template)
        assert "*(нет зарегистрированных ресурсов)*" in out
        assert "{{SCRIPTS_CATALOG}}" not in out

    def test_missing_description_falls_back_to_empty(self) -> None:
        os.environ["SKILL_AUDIT_ANALYZER_SCRIPTS"] = "foo,bar"
        out = SkillCatalog.render_expanded_skill(
            "audit_analyzer", "{{SCRIPTS_CATALOG}}"
        )
        assert "| `foo` |  |" in out
        assert "| `bar` |  |" in out

    def test_special_chars_escaped(self) -> None:
        os.environ["SKILL_AUDIT_ANALYZER_SCRIPTS"] = "name_one"
        os.environ["SKILL_AUDIT_ANALYZER_SCRIPT_DESCRIPTIONS"] = (
            "name_one=desc with | pipe and \\n newline"
        )
        out = SkillCatalog.render_expanded_skill(
            "audit_analyzer", "{{SCRIPTS_CATALOG}}"
        )
        assert "with \\| pipe" in out
        assert "newline" in out
        assert "\\n newline" in out


class TestRenderVectorsCatalog:
    """``{{VECTORS_CATALOG}}`` → markdown-таблица vector indexes."""

    def test_renders_vectors_table(self) -> None:
        os.environ["SKILL_AUDIT_ANALYZER_VECTORS"] = (
            "audits_index,violations_index"
        )
        os.environ["SKILL_AUDIT_ANALYZER_VECTOR_DESCRIPTIONS"] = (
            "audits_index=Поиск проверок по смыслу;"
            "violations_index=Поиск нарушений"
        )
        out = SkillCatalog.render_expanded_skill(
            "audit_analyzer", "{{VECTORS_CATALOG}}"
        )
        assert "| Index | Описание |" in out
        assert "| `audits_index` | Поиск проверок по смыслу |" in out
        assert "| `violations_index` | Поиск нарушений |" in out

    def test_empty_vectors_renders_placeholder(self) -> None:
        out = SkillCatalog.render_expanded_skill(
            "audit_analyzer", "{{VECTORS_CATALOG}}"
        )
        assert "*(нет зарегистрированных ресурсов)*" in out


class TestRenderTablesCatalog:
    """``{{TABLES_CATALOG}}`` → простая markdown-таблица таблиц."""

    def test_renders_tables_table_without_descriptions(self) -> None:
        os.environ["SKILL_AUDIT_ANALYZER_TABLES"] = (
            "oarb.audits,oarb.violations"
        )
        out = SkillCatalog.render_expanded_skill(
            "audit_analyzer", "{{TABLES_CATALOG}}"
        )
        assert "| Table |" in out
        assert "|---|" in out
        assert "| `oarb.audits` |" in out
        assert "| `oarb.violations` |" in out


class TestMarkerHandling:
    """Поведение подстановки маркеров."""

    def test_unknown_marker_preserved(self) -> None:
        template = "before {{UNKNOWN}} after {{SCRIPTS_CATALOG}}"
        out = SkillCatalog.render_expanded_skill("audit_analyzer", template)
        assert "{{UNKNOWN}}" in out

    def test_multiple_markers_in_one_template(self) -> None:
        os.environ["SKILL_X_SCRIPTS"] = "s1"
        os.environ["SKILL_X_VECTORS"] = "v1"
        os.environ["SKILL_X_TABLES"] = "t1"
        template = (
            "S: {{SCRIPTS_CATALOG}}\n"
            "V: {{VECTORS_CATALOG}}\n"
            "T: {{TABLES_CATALOG}}"
        )
        out = SkillCatalog.render_expanded_skill("X", template)
        assert "`s1`" in out
        assert "`v1`" in out
        assert "`t1`" in out
        assert "{{" not in out

    def test_dash_in_skill_name_normalized(self) -> None:
        os.environ["SKILL_AUDIT_ANALYZER_V2_SCRIPTS"] = "s1"
        out = SkillCatalog.render_expanded_skill(
            "audit-analyzer-v2", "{{SCRIPTS_CATALOG}}"
        )
        assert "`s1`" in out


class TestCsvAndDescriptionsParsing:
    """Граничные случаи парсинга CSV и ``key=value;...``."""

    def test_csv_whitespace_trimmed(self) -> None:
        os.environ["SKILL_X_SCRIPTS"] = " a , b , c "
        names = SkillCatalog.read_scripts("X")
        assert names == ["a", "b", "c"]

    def test_descriptions_split_only_by_first_equals(self) -> None:
        os.environ["SKILL_X_SCRIPTS"] = "a,b"
        os.environ["SKILL_X_SCRIPT_DESCRIPTIONS"] = "a=1=2;b=3=4"
        out = SkillCatalog.render_expanded_skill("X", "{{SCRIPTS_CATALOG}}")
        assert "| `a` | 1=2 |" in out
        assert "| `b` | 3=4 |" in out

    def test_descriptions_empty_segments_skipped(self) -> None:
        os.environ["SKILL_X_SCRIPTS"] = "a,b"
        os.environ["SKILL_X_SCRIPT_DESCRIPTIONS"] = ";;a=1;;b=2;;"
        out = SkillCatalog.render_expanded_skill("X", "{{SCRIPTS_CATALOG}}")
        assert "| `a` | 1 |" in out
        assert "| `b` | 2 |" in out

    def test_descriptions_without_equals_skipped(self) -> None:
        os.environ["SKILL_X_SCRIPTS"] = "a"
        os.environ["SKILL_X_SCRIPT_DESCRIPTIONS"] = "no_equals_here;a=ok"
        out = SkillCatalog.render_expanded_skill("X", "{{SCRIPTS_CATALOG}}")
        assert "| `a` | ok |" in out
        assert "no_equals_here" not in out


class TestClearSkillEnv:
    """``clear_skill_env`` для graceful shutdown / изоляции тестов."""

    def test_clear_specific_skill(self) -> None:
        os.environ["SKILL_AUDIT_ANALYZER_TABLES"] = "x"
        os.environ["SKILL_LEGAL_SUMMARIZER_TABLES"] = "y"
        removed = SkillCatalog.clear_skill_env("audit_analyzer")
        assert removed == 1
        assert "SKILL_AUDIT_ANALYZER_TABLES" not in os.environ
        assert "SKILL_LEGAL_SUMMARIZER_TABLES" in os.environ

    def test_clear_all_skill_envs(self) -> None:
        os.environ["SKILL_FOO_TABLES"] = "1"
        os.environ["SKILL_BAR_SCRIPTS"] = "2"
        os.environ["NOT_SKILL"] = "keep"
        removed = SkillCatalog.clear_skill_env(None)
        assert removed == 2
        assert "NOT_SKILL" in os.environ

    def test_clear_no_env_returns_zero(self) -> None:
        assert SkillCatalog.clear_skill_env("nonexistent") == 0


class TestReadHelpers:
    """Прямой read для тестов/отладки."""

    def test_read_tables_empty_when_unset(self) -> None:
        assert SkillCatalog.read_tables("missing") == []

    def test_read_vectors_empty_when_unset(self) -> None:
        assert SkillCatalog.read_vectors("missing") == []

    def test_read_scripts_empty_when_unset(self) -> None:
        assert SkillCatalog.read_scripts("missing") == []
