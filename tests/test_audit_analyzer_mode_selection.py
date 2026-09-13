"""Тесты выбора режима (decision tree) навыка ``audit_analyzer``.

SKILL.md — источник истины для Agent'а: описывает три способа получения
данных (predefined / vector / Core Data) и правила выбора между ними.

Эти тесты проверяют, что в SKILL.md достаточно правил, чтобы Agent
не ошибся в типовых сценариях. Дополнительно — sanity-проверка
``predefined.run()`` со стороны skill'а (отсутствие таблицы/нечисловой тип
возвращает ошибку).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


try:
    from conftest import AUDIT_SKILL_DIR as SKILL_DIR, AUDIT_SKILL_MD as SKILL_MD
except ImportError:
    # Fallback для запуска теста вне pytest (например, прямой импорт).
    SKILL_DIR = Path(__file__).resolve().parent.parent / "workspace" / "skills" / "audit_analyzer"
    SKILL_MD = SKILL_DIR / "SKILL.md"


def _skill_md() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Позитивные сценарии: «такой-то запрос → такой-то режим»
# ---------------------------------------------------------------------------


class TestModeSelectionPositive:
    """SKILL.md явно указывает decision tree для типовых запросов."""

    def test_predefined_declared_for_typical_summary(self) -> None:
        """«Сводка по типам проверок» — predefined (audit_types_stats)."""
        text = _skill_md()
        assert "audit_types_stats" in text

    def test_predefined_declared_for_violations_by_period(self) -> None:
        """«Нарушения по типу» — predefined (violations_by_type)."""
        text = _skill_md()
        assert "violations_by_type" in text
        assert "violation_code" in text

    def test_vector_index_audits_index(self) -> None:
        """«Найди похожие проверки про X» → vector capability (audits_index)."""
        text = _skill_md()
        assert "audits_index" in text

    def test_vector_index_violations_index(self) -> None:
        """«Найди похожие нарушения» → vector capability (violations_index)."""
        text = _skill_md()
        assert "violations_index" in text

    def test_vector_index_audit_reports_index(self) -> None:
        """«Найди отчёты про X» → vector capability (audit_reports_index)."""
        text = _skill_md()
        assert "audit_reports_index" in text

    def test_sql_for_aggregations(self) -> None:
        """«Сколько проверок» / «топ-5 организаций» → SQL через Core Data."""
        text = _skill_md()
        # Core Data/DuckDB capability упоминается в SKILL.md как путь
        # для analytical SQL.
        assert "Core" in text or "core" in text
        assert "DuckDB" in text or "Data" in text

    def test_all_six_scripts_listed(self) -> None:
        """В каталоге SKILL.md все 6 predefined scripts из БД."""
        text = _skill_md()
        for script in (
            "analytics_by_year_month",
            "audit_dynamics",
            "audit_effectiveness",
            "audit_types_stats",
            "top_audited_objects",
            "violations_by_type",
        ):
            assert script in text, f"SKILL.md должен упоминать {script}"


# ---------------------------------------------------------------------------
# Негативные правила: чего Agent НЕ должен делать
# ---------------------------------------------------------------------------


class TestModeSelectionNegative:
    """Запреты: что НЕ выбирать для каждого режима."""

    def test_no_sql_for_semantic_search(self) -> None:
        """Семантический поиск и вектор вне контракта агента.

        Phase 8: tools удалены, агент обращается к данным только через
        predefined-скрипты CLI. SKILL.md должен явно ограничивать доступ
        агента predefined-режимом и помечать vector-режим как CLI-only.
        """
        text = _skill_md()
        # Predefined-only для агента.
        assert "только predefined" in text
        # Vector/generated_sql — не часть контракта агента.
        assert "не являются частью контракта агента" in text

    def test_no_vector_for_aggregations(self) -> None:
        """Агgregations — только через predefined, вектор вне контракта.

        Phase 8: действительный контракт — predefined-only, /неподдерживаемое.
        """
        text = _skill_md()
        assert "только predefined" in text

    def test_no_predefined_without_required_params(self) -> None:
        """Если обязательных params нет — predefined НЕ выбирается."""
        text = _skill_md()
        assert (
            "обязательн" in text
        ), "SKILL.md должен упоминать обязательные параметры"

    def test_no_fake_script_names(self) -> None:
        """Запрет придумывать имена скриптов."""
        text = _skill_md()
        assert (
            "каталог" in text.lower()
        ), "SKILL.md должен ссылаться на каталог predefined scripts"


# ---------------------------------------------------------------------------
# Behaviour: predefined.run() на стороне skill'а
# ---------------------------------------------------------------------------


class TestPredefinedBehaviour:
    """Поведение ``predefined.run()`` через DuckDB-fixture для типовых запросов."""

    def test_predefined_for_count_audits_violations(self, db_service) -> None:
        """«Сколько аудитов по статусам» → predefined (audit_status_summary)."""
        from workspace.skills.audit_analyzer.scripts.predefined import run

        result = run(
            "audit_status_summary",
            db_service,
            predefined_table="public.agent_predefined_scripts",
        )
        assert result["status"] == "success"
        assert result["data"]["script_name"] == "audit_status_summary"

    def test_vector_for_semantic_query_only_in_docs(self) -> None:
        """Семантический поиск НЕ через SQL — это вектор, не predefined."""
        # Реальный вызов vector — в test_audit_analyzer_behavior.py.
        # Здесь — проверка SKILL.md как источника истины для Agent'а.
        text = _skill_md()
        assert "семантическ" in text.lower()

    def test_no_vector_for_count_aggregation_in_skill(self) -> None:
        """«Сколько проверок» через vector — не по контракту.

        Phase 8: vector-режим — CLI-only, для агента существует только
        predefined. SKILL.md должен явно отделять agent-контракт (predefined)
        от CLI-режимов.
        """
        text = _skill_md()
        # Predefined-only для агента.
        assert "только predefined" in text
        # CLI-режимы не являются контрактом агента.
        assert "не являются частью контракта агента" in text


@pytest.fixture
def db_service():
    """Минимальный DuckDB-fixture для ``predefined.run`` тестов.

    Phase 7: ``predefined.run()`` теперь DB-only (Phase 7 — REGISTRY удалён).
    Фикстура создаёт in-memory DB с ``oarb.*`` (domain) и
    ``public.agent_predefined_scripts`` (реестр скриптов). Скрипт
    ``audit_status_summary`` засеян с минимальным SQL.
    """
    import duckdb
    import json

    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA IF NOT EXISTS oarb")
    conn.execute("CREATE SCHEMA IF NOT EXISTS public")
    conn.execute(
        "CREATE TABLE oarb.audits ("
        "id INTEGER, title VARCHAR, audit_type VARCHAR, "
        "actual_date DATE, status VARCHAR)"
    )
    conn.executemany(
        "INSERT INTO oarb.audits VALUES (?, ?, ?, ?, ?)",
        [
            (1, "Fire safety", "planned", None, "planned"),
            (2, "Financial", "planned", "2024-05-01", "done"),
            (3, "Compliance", "extra", "2024-08-15", "done"),
        ],
    )
    conn.execute(
        "CREATE TABLE oarb.violations ("
        "id INTEGER, audit_id INTEGER, violation_code VARCHAR, "
        "description VARCHAR, severity VARCHAR)"
    )
    conn.executemany(
        "INSERT INTO oarb.violations VALUES (?, ?, ?, ?, ?)",
        [
            (10, 2, "F-1", "Fire exit", "high"),
            (11, 2, "F-2", "No ext", "medium"),
            (12, 3, "D-1", "Missing sig", "low"),
        ],
    )
    conn.execute(
        "CREATE TABLE public.agent_predefined_scripts ("
        "name VARCHAR, description VARCHAR, returns VARCHAR, "
        "long_description VARCHAR, sql_template VARCHAR, "
        "parameters VARCHAR, max_rows_default INTEGER)"
    )
    conn.executemany(
        "INSERT INTO public.agent_predefined_scripts VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "audit_status_summary",
                "Сводка по статусам",
                "status, cnt",
                "Phase 7 fixture.",
                "SELECT status, COUNT(*) AS cnt "
                "FROM oarb.audits WHERE status IS NOT NULL "
                "GROUP BY status ORDER BY status",
                json.dumps({}),
                100,
            )
        ],
    )

    class _DBService:
        def query_sql(self, sql, params=None):
            try:
                if params:
                    result = conn.execute(sql, list(params))
                else:
                    result = conn.execute(sql)
                columns = [c[0] for c in result.description] if result.description else []
                rows = [dict(zip(columns, r, strict=False)) for r in result.fetchall()]
                return {
                    "status": "success",
                    "row_count": len(rows),
                    "columns": columns,
                    "rows": rows,
                }
            except Exception as exc:
                return {
                    "status": "error",
                    "row_count": 0,
                    "columns": [],
                    "rows": [],
                    "error": str(exc),
                }

    return _DBService()


# ---------------------------------------------------------------------------
# Phase 8: SKILL.md self-contained (references/ удалены)
# ---------------------------------------------------------------------------


class TestSkillSelfContained:
    def test_skill_md_carries_full_content(self) -> None:
        """SKILL.md — единственный источник документации по skill'у.

        ``references/*.md`` удалены: каталог скриптов, описание индексов,
        схема домена и SQL guidance перенесены в SKILL.md.
        """
        skill_text = _skill_md()
        # Каталог скриптов из БД.
        for script in (
            "analytics_by_year_month",
            "audit_dynamics",
            "audit_effectiveness",
            "audit_types_stats",
            "top_audited_objects",
            "violations_by_type",
        ):
            assert script in skill_text, f"SKILL.md должен упоминать {script}"
        # Каталог FAISS-индексов.
        for index in ("audits_index", "violations_index", "audit_reports_index"):
            assert index in skill_text
        # Схема домена (минимум 4 таблицы).
        for table in ("oarb.audits", "oarb.violations",
                      "oarb.audit_reports", "oarb.report_items"):
            assert table in skill_text, f"SKILL.md должен описывать {table}"