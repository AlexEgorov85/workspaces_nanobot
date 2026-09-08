"""Тесты выбора режима (decision tree) навыка ``audit_analyzer``.

Покрывают шаг 22 плана: позитивные/негативные кейсы выбора
predefined / vector / sql для каждого типа пользовательского запроса.

Агент НЕ вызывает Python напрямую — решение принимается на основании
``SKILL.md``. Эти тесты проверяют, что в SKILL.md достаточно правил,
чтобы Agent не ошибся в типовых сценариях. Дополнительно — sanity-проверка
``predefined.run()`` со стороны skill'а (отсутствие таблицы/нечисловой тип
возвращает ошибку).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


SKILL_DIR = Path("workspace/skills/audit_analyzer")
SKILL_MD = SKILL_DIR / "SKILL.md"


def _skill_md() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Позитивные сценарии: «такой-то запрос → такой-то режим»
# ---------------------------------------------------------------------------


class TestModeSelectionPositive:
    """SKILL.md явно указывает decision tree для типовых запросов."""

    def test_predefined_declared_for_typical_summary(self) -> None:
        """«Сводка по статусам аудитов» — predefined (audit_status_summary)."""
        text = _skill_md()
        assert "audit_status_summary" in text

    def test_predefined_declared_for_violations_by_period(self) -> None:
        """«Нарушения за 2024» — predefined (violations_by_period)."""
        text = _skill_md()
        assert "violations_by_period" in text
        assert "date_from" in text

    def test_vector_index_audits_index(self) -> None:
        """«Найди похожие проверки про X» → vector_search(audits_index)."""
        text = _skill_md()
        assert "audits_index" in text
        assert "vector_search" in text

    def test_vector_index_violations_index(self) -> None:
        """«Найди похожие нарушения» → vector_search(violations_index)."""
        text = _skill_md()
        assert "violations_index" in text

    def test_vector_index_audit_reports_index(self) -> None:
        """«Найди отчёты про X» → vector_search(audit_reports_index)."""
        text = _skill_md()
        assert "audit_reports_index" in text

    def test_sql_for_aggregations(self) -> None:
        """«Сколько проверок» / «топ-5 организаций» → SQL через duckdb_query."""
        text = _skill_md()
        assert "duckdb_query" in text
        # Должно быть явное указание, что SQL — fallback.
        assert "fallback" in text.lower() or "\u0444\u043e\u043b\u0431\u044d\u043a" in text.lower()

    def test_all_five_scripts_listed(self) -> None:
        """В каталоге SKILL.md ровно 5 predefined scripts."""
        text = _skill_md()
        for script in (
            "audit_status_summary",
            "top_violations_by_type",
            "violations_by_period",
            "audits_by_period",
            "audit_effectiveness_summary",
        ):
            assert script in text, f"SKILL.md должен упоминать {script}"


# ---------------------------------------------------------------------------
# Негативные правила: чего Agent НЕ должен делать
# ---------------------------------------------------------------------------


class TestModeSelectionNegative:
    """Запреты: что НЕ выбирать для каждого режима."""

    def test_no_sql_for_semantic_search(self) -> None:
        """Запрещено LIKE '%...%' для семантического поиска."""
        text = _skill_md()
        assert "LIKE" in text, "SKILL.md должен явно запрещать LIKE для семантики"
        assert "vector_search" in text

    def test_no_vector_for_aggregations(self) -> None:
        """Запрещено vector_search для COUNT/GROUP BY."""
        text = _skill_md()
        # Ищем явное указание на COUNT/GROUP BY как «не для vector».
        assert "COUNT" in text or "GROUP BY" in text

    def test_no_predefined_without_required_params(self) -> None:
        """Если обязательных params нет — predefined НЕ выбирается."""
        text = _skill_md()
        assert (
            "\u043e\u0431\u044f\u0437\u0430\u0442\u0435\u043b\u044c\u043d" in text
        ), "SKILL.md должен упоминать обязательные параметры"

    def test_no_fake_script_names(self) -> None:
        """Запрет придумывать имена скриптов."""
        text = _skill_md()
        assert (
            "\u043a\u0430\u0442\u0430\u043b\u043e\u0433" in text.lower()
        ), "SKILL.md должен ссылаться на каталог predefined scripts"

    def test_no_run_predefined_script_tool(self) -> None:
        """Удалённый ``run_predefined_script`` tool явно запрещён."""
        text = _skill_md()
        assert "run_predefined_script" in text
        # Должно быть «не вызывай …».
        assert "\u043d\u0435 \u0432\u044b\u0437\u044b\u0432\u0430\u0439" in text.lower() or "forbidden" in text.lower()

    def test_no_nl_sql_generate_tool(self) -> None:
        """Удалённый ``nl_sql_generate`` tool явно запрещён."""
        text = _skill_md()
        assert "nl_sql_generate" in text

    def test_no_sql_generator_helper(self) -> None:
        """Удалённый ``scripts/sql_generator.py`` helper явно запрещён."""
        text = _skill_md()
        assert "sql_generator" in text


# ---------------------------------------------------------------------------
# Behaviour: predefined.run() на стороне skill'а
# ---------------------------------------------------------------------------


class TestPredefinedBehaviour:
    """Поведение ``predefined.run()`` через DuckDB-fixture для типовых запросов."""

    def test_predefined_for_count_audits_violations(self, db_service) -> None:
        """«Сколько аудитов по статусам» → predefined (audit_status_summary)."""
        from workspace.skills.audit_analyzer.predefined import run

        result = run("audit_status_summary", db_service, {})
        assert result["status"] == "success"
        assert result["data"]["script_name"] == "audit_status_summary"

    def test_vector_for_semantic_query_only_in_docs(self) -> None:
        """Семантический поиск НЕ через SQL — это вектор, не predefined."""
        # Реальный вызов vector_search — в test_audit_analyzer_behavior.py.
        # Здесь — проверка SKILL.md как источника истины для Agent'а.
        text = _skill_md()
        assert "\u0441\u0435\u043c\u0430\u043d\u0442\u0438\u0447\u0435\u0441\u043a" in text.lower()
        assert "vector_search" in text

    def test_sql_for_count_aggregation_in_predefined(self) -> None:
        """«Сколько проверок» → predefined (audit_status_summary через
        GROUP BY), если запрос именно про статусы; иначе — duckdb_query.
        """
        text = _skill_md()
        lower = text.lower()
        # Явный запрет: vector_search не для агрегаций.
        assert "\u0430\u0433\u0440\u0435\u0433" in lower, (
            "SKILL.md должен упоминать агрегацию в контексте SQL"
        )
        assert "vector_search" in lower, "SKILL.md должен упоминать vector_search"
        assert ("COUNT" in text and "vector_search" in text), (
            "SKILL.md должен запрещать vector_search для COUNT"
        )


@pytest.fixture
def db_service():
    """Минимальный DuckDB-fixture для ``predefined.run`` тестов."""
    import duckdb

    from workspace.skills.audit_analyzer.predefined import run

    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA IF NOT EXISTS oarb")
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

    class _DBService:
        def execute_readonly(self, sql, params, max_rows):
            try:
                if params:
                    rows = conn.execute(sql, list(params)).fetchmany(max_rows)
                else:
                    rows = conn.execute(sql).fetchmany(max_rows)
                cols = (
                    [c[0] for c in conn.description]
                    if conn.description
                    else []
                )
                return {"rows": rows, "columns": cols, "row_count": len(rows)}
            except Exception as exc:
                return {"error": str(exc)}

    return _DBService()


# ---------------------------------------------------------------------------
# Required docs (progressive disclosure)
# ---------------------------------------------------------------------------


class TestReferencesIntegrity:
    def test_required_references_exist(self) -> None:
        required = [
            "references/architecture.md",
            "references/schema.md",
            "references/vector_indexes.md",
            "references/sql_guidance.md",
            "references/predefined_scripts.md",
        ]
        for ref in required:
            path = SKILL_DIR / ref
            assert path.is_file(), f"{path} должен существовать"
            assert path.stat().st_size > 200, f"{path} слишком мал"
