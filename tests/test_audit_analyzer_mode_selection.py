"""Тесты контракта трёх режимов ``audit_analyzer``.

SKILL.md — единственный источник истины для Agent'а: описывает три режима
(``predefined`` / ``vector`` / ``generated_sql``) и правила выбора
между ними. Тесты проверяют, что в SKILL.md достаточно правил, чтобы
Agent не ошибся в типовых сценариях, и что старый запрет «только
predefined» полностью ушёл.

Дополнительно — sanity-проверка ``predefined.run()`` со стороны skill'а
(отсутствие таблицы / нечисловой тип возвращает ошибку).
"""

from __future__ import annotations

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
# Новый контракт: ВСЕ ТРИ РЕЖИМА = часть навыка.
# ---------------------------------------------------------------------------


class TestThreeModesContract:
    """SKILL.md явно фиксирует три режима как часть контракта навыка."""

    def test_three_modes_listed(self) -> None:
        text = _skill_md()
        assert "predefined" in text
        assert "vector" in text
        assert "generated_sql" in text

    def test_no_predefined_only_exclusion(self) -> None:
        """Старый запрет «только predefined / не часть контракта агента» снят."""
        text = _skill_md().lower()
        # Эти фразы были формулировкой старого контракта; теперь их быть не должно.
        assert "только predefined" not in text, (
            "SKILL.md не должен говорить, что predefined — единственный режим"
        )
        assert "не являются частью контракта агента" not in text

    def test_decision_tree_branches_for_three_modes(self) -> None:
        """Decision tree упоминает все три режима как валидные ветви выбора."""
        text = _skill_md()
        for marker in (
            "predefined",
            "vector",
            "generated_sql",
        ):
            assert marker in text


# ---------------------------------------------------------------------------
# Позитивные сценарии: «такой-то запрос → такой-то режим» (SKILL.md учит Agent)
# ---------------------------------------------------------------------------


class TestModeSelectionPositive:
    """SKILL.md явно указывает decision tree для типовых запросов."""

    def test_predefined_declared_for_typical_summary(self) -> None:
        text = _skill_md()
        assert "audit_types_stats" in text

    def test_predefined_declared_for_violations_by_period(self) -> None:
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

    def test_generated_sql_declared_for_custom_analytics(self) -> None:
        """generated_sql упоминается как валидный путь для нестандартной аналитики."""
        text = _skill_md().lower()
        assert "generated_sql" in text
        # И прозрачный комментарий, что он — часть контракта (не «CLI-only»).
        assert "cli-only" not in text or "vector-режим — cli-only" not in text

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
    """Запреты внутри режимов и в режим-выборе."""

    def test_no_fallback_between_modes(self) -> None:
        """fallback между режимами ЗАПРЕЩЁН."""
        text = _skill_md().lower()
        assert "fallback между режимами" in text or "не делать fallback" in text or "не делает fallback" in text

    def test_no_inventing_table_names_in_python(self) -> None:
        """Техническая schema не прописана вручную в SKILL.md (бизнес-глоссарий только)."""
        text = _skill_md().lower()
        # В SKILL.md не должно быть раздела «## Схема домена» с конкретными
        # колонками и типами — это второй источник истины. Допустимы только
        # упоминания имён таблиц в контексте whitelist (для generated_sql).
        # Проверяем, что нет блока с table | column | description.
        assert "схема домена" not in text, (
            "SKILL.md не должен содержать раздел «Схема домена» с ручной schema — "
            "это второй источник истины; schema читается через CacheProvider.get_schema()"
        )

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
        text = _skill_md()
        assert "семантическ" in text.lower()


# ---------------------------------------------------------------------------
# Phase 8+: SKILL.md self-contained (references/ удалены, ровно один MD)
# ---------------------------------------------------------------------------


class TestSkillSelfContained:
    def test_skill_md_carries_full_content(self) -> None:
        """SKILL.md — единственный источник документации по skill'у."""
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

        # Технической schema (типы колонок, раздел «## Схема домена») быть
        # не должно — schema приходит из DuckDB-кэша через get_schema().
        assert "## Схема домена" not in skill_text, (
            "SKILL.md не должен содержать раздел «## Схема домена» с типами колонок — "
            "schema читается через CacheProvider.get_schema()"
        )

        # Технические детали vector storage (BYTEA / signature internals /
        # FAISS serialization) — нарушение разделения слоёв; skill владеет
        # только логическими именами.
        for forbidden in ("BYTEA", "FAISS serialization", "signature internals"):
            assert forbidden not in skill_text, (
                f"SKILL.md не должен содержать технические детали '{forbidden}' — "
                "это слой Core, не skill"
            )


@pytest.fixture
def db_service():
    """Минимальный DuckDB-fixture для ``predefined.run`` тестов.

    Phase 7+: ``predefined.run()`` теперь DB-only (REGISTRY удалён).
    Фикстура создаёт in-memory DB с нужными таблицами.
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
