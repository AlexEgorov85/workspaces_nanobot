"""Тесты для ``predefined.run()``: 5 скриптов + крайние случаи.

Покрывают:
  * реальное выполнение на DuckDB-fixture с in-memory DB-снимком;
  * валидацию параметров (Step 7 — ``ParameterValidator``);
  * правильную сборку SQL (Step 8 — ``DynamicQueryBuilder``).

Скрипты читаются из ``public.agent_predefined_scripts`` через preload-
фикстуру в ``conftest.py`` — production runtime path (DB-first lookup
через ``predefined_table=PREDEFINED_TABLE``). Python ``REGISTRY``
больше не существует (Phase 7).

Не тестируют LLM-Agent — только Python-API ``predefined.run`` и
``DuckDBServiceProtocol``.
"""

from __future__ import annotations

import pytest

from workspace.skills.audit_analyzer.scripts.predefined import (
    ParameterValidator,
    load_all,
    load_script,
)


PREDEFINED_TABLE = "public.agent_predefined_scripts"


# ---------------------------------------------------------------------------
# 1. audit_status_summary — no params, реальный GROUP BY
# ---------------------------------------------------------------------------


class TestPredefinedAuditStatusSummary:
    def test_no_params(self, db_service) -> None:
        script = load_script(db_service, PREDEFINED_TABLE, "audit_status_summary")
        assert script is not None
        assert script.parameters == {}

    def test_execute_real(self, db_service) -> None:
        from workspace.skills.audit_analyzer.scripts.predefined import run

        result = run(
            "audit_status_summary",
            db_service,
            predefined_table=PREDEFINED_TABLE,
        )
        assert result["mode"] == "predefined"
        assert result["status"] == "success"
        rows_by_status = {r["status"]: r["cnt"] for r in result["data"]["result"]["rows"]}
        assert rows_by_status == {
            "В работе": 1,
            "Завершена": 2,
            "Запланирована": 1,
        }

    def test_unknown_param_caught(self, db_service) -> None:
        from workspace.skills.audit_analyzer.scripts.predefined import run

        result = run(
            "audit_status_summary",
            db_service,
            {"foo": "bar"},
            predefined_table=PREDEFINED_TABLE,
        )
        assert result["status"] == "error"
        assert "foo" in result["data"]["message"]


# ---------------------------------------------------------------------------
# 2. top_violations_by_type — no params, ORDER BY count DESC
# ---------------------------------------------------------------------------


class TestPredefinedTopViolationsByType:
    def test_no_params(self, db_service) -> None:
        script = load_script(db_service, PREDEFINED_TABLE, "top_violations_by_type")
        assert script is not None
        assert script.parameters == {}

    def test_execute_real(self, db_service) -> None:
        from workspace.skills.audit_analyzer.scripts.predefined import run

        result = run(
            "top_violations_by_type",
            db_service,
            predefined_table=PREDEFINED_TABLE,
        )
        assert result["status"] == "success"
        rows = result["data"]["result"]["rows"]
        assert len(rows) == 3
        counts = [r["cnt"] for r in rows]
        assert counts == sorted(counts, reverse=True)


# ---------------------------------------------------------------------------
# 3. violations_by_period — 2 required date params
# ---------------------------------------------------------------------------


class TestPredefinedViolationsByPeriod:
    def test_required_params_declared(self, db_service) -> None:
        script = load_script(db_service, PREDEFINED_TABLE, "violations_by_period")
        assert script.parameters["date_from"].required is True
        assert script.parameters["date_to"].required is True
        assert script.parameters["date_from"].type == "date"
        assert script.parameters["date_to"].type == "date"

    def test_execute_with_valid_dates(self, db_service) -> None:
        from workspace.skills.audit_analyzer.scripts.predefined import run

        result = run(
            "violations_by_period",
            db_service,
            {"date_from": "2024-01-01", "date_to": "2024-12-31"},
            predefined_table=PREDEFINED_TABLE,
        )
        assert result["status"] == "success"
        result_rows = result["data"]["result"]["rows"]
        assert len(result_rows) == 3
        codes = sorted(r["violation_code"] for r in result_rows)
        assert codes == ["D-1", "F-1", "F-2"]

    def test_missing_required_param(self, db_service) -> None:
        from workspace.skills.audit_analyzer.scripts.predefined import run

        result = run(
            "violations_by_period",
            db_service,
            predefined_table=PREDEFINED_TABLE,
        )
        assert result["status"] == "error"
        assert "дата" in result["data"]["message"].lower() or (
            "date_from" in result["data"]["message"]
        )

    def test_missing_only_date_to(self, db_service) -> None:
        from workspace.skills.audit_analyzer.scripts.predefined import run

        result = run(
            "violations_by_period",
            db_service,
            {"date_from": "2024-01-01"},
            predefined_table=PREDEFINED_TABLE,
        )
        assert result["status"] == "error"
        assert "date_to" in result["data"]["message"]

    def test_wrong_type_int_date(self, db_service) -> None:
        from workspace.skills.audit_analyzer.scripts.predefined import run

        result = run(
            "violations_by_period",
            db_service,
            {"date_from": 123, "date_to": "2024-12-31"},
            predefined_table=PREDEFINED_TABLE,
        )
        assert result["status"] == "error"
        assert "date_from" in result["data"]["message"]

    def test_boundary_dates_inclusive(self, db_service) -> None:
        from workspace.skills.audit_analyzer.scripts.predefined import run

        result = run(
            "violations_by_period",
            db_service,
            {"date_from": "2024-08-15", "date_to": "2024-08-15"},
            predefined_table=PREDEFINED_TABLE,
        )
        assert result["status"] == "success"
        assert result["data"]["result"]["row_count"] == 1
        assert result["data"]["result"]["rows"][0]["violation_code"] == "D-1"

    def test_unknown_param_caught(self, db_service) -> None:
        from workspace.skills.audit_analyzer.scripts.predefined import run

        result = run(
            "violations_by_period",
            db_service,
            {"date_from": "2024-01-01", "date_to": "2024-12-31", "extra": "x"},
            predefined_table=PREDEFINED_TABLE,
        )
        assert result["status"] == "error"
        assert "extra" in result["data"]["message"]


# ---------------------------------------------------------------------------
# 4. audits_by_period — 2 required date params
# ---------------------------------------------------------------------------


class TestPredefinedAuditsByPeriod:
    def test_required_params(self, db_service) -> None:
        script = load_script(db_service, PREDEFINED_TABLE, "audits_by_period")
        assert script.parameters["date_from"].required is True
        assert script.parameters["date_to"].required is True

    def test_execute_real(self, db_service) -> None:
        from workspace.skills.audit_analyzer.scripts.predefined import run

        result = run(
            "audits_by_period",
            db_service,
            {"date_from": "2024-01-01", "date_to": "2024-12-31"},
            predefined_table=PREDEFINED_TABLE,
        )
        assert result["status"] == "success"
        rows = result["data"]["result"]["rows"]
        ids = sorted(r["id"] for r in rows)
        assert ids == [2, 3]

    def test_exclude_null_dates(self, db_service) -> None:
        from workspace.skills.audit_analyzer.scripts.predefined import run

        result = run(
            "audits_by_period",
            db_service,
            {"date_from": "1900-01-01", "date_to": "2100-01-01"},
            predefined_table=PREDEFINED_TABLE,
        )
        assert result["status"] == "success"
        ids = [r["id"] for r in result["data"]["result"]["rows"]]
        assert 1 not in ids


# ---------------------------------------------------------------------------
# 5. audit_effectiveness_summary — no params, GROUP BY audits LEFT JOIN violations
# ---------------------------------------------------------------------------


class TestPredefinedAuditEffectivenessSummary:
    def test_no_required_params(self, db_service) -> None:
        script = load_script(db_service, PREDEFINED_TABLE, "audit_effectiveness_summary")
        assert set(script.parameters.keys()) == {"min_violations"}
        assert script.parameters["min_violations"].required is False

    def test_execute_real(self, db_service) -> None:
        from workspace.skills.audit_analyzer.scripts.predefined import run

        result = run(
            "audit_effectiveness_summary",
            db_service,
            predefined_table=PREDEFINED_TABLE,
        )
        assert result["status"] == "success"
        rows = result["data"]["result"]["rows"]
        severity_by_audit = {
            r["audit_id"]: (r["violations_count"], r["severity_level"])
            for r in rows
        }
        assert severity_by_audit[2] == (2, "Допустимые нарушения")
        assert severity_by_audit[3] == (1, "Допустимые нарушения")
        assert severity_by_audit[4] == (0, "Без нарушений")


# ---------------------------------------------------------------------------
# Cross-cutting: validator/builder, none-shown behaviour
# ---------------------------------------------------------------------------


class TestPredefinedKnownScripts:
    def test_all_known_scripts_return(self, db_service) -> None:
        from workspace.skills.audit_analyzer.scripts.predefined import run

        scripts = load_all(db_service, PREDEFINED_TABLE)
        for name in scripts:
            result = run(
                name,
                db_service,
                {},
                predefined_table=PREDEFINED_TABLE,
            )
            assert result["status"] in ("success", "error")
            if result["status"] == "error":
                assert name in ("violations_by_period", "audits_by_period")

    def test_unknown_script(self, db_service) -> None:
        from workspace.skills.audit_analyzer.scripts.predefined import run

        result = run(
            "nonexistent",
            db_service,
            predefined_table=PREDEFINED_TABLE,
        )
        assert result["status"] == "error"
        assert "не найден" in result["data"]["message"]

    def test_no_fallback_without_predefined_table(self, db_service) -> None:
        """Phase 7: без ``predefined_table`` нет fallback на Python REGISTRY.

        ``run()`` обязан вернуть error (``error_type="missing_predefined_table"``),
        а не пытаться найти скрипт в старом Python-литерале. Это контракт
        DB-only source of truth.
        """
        from workspace.skills.audit_analyzer.scripts.predefined import run

        result = run("audit_status_summary", db_service, {})
        assert result["status"] == "error"
        assert result["data"]["error_type"] == "missing_predefined_table"
        # Сообщение должно явно говорить про DB, не про «скрипт не найден».
        assert "predefined_table" in result["data"]["message"]
        assert "REGISTRY" not in result["data"]["message"]

    def test_no_fallback_when_db_table_missing(self) -> None:
        """Phase 7: если таблица registry отсутствует в DB — ошибка, не fallback.

        Даже при передаче ``predefined_table`` если таблицы нет в DuckDB —
        скрипт не находится (без скрытого перехода на Python REGISTRY).
        """
        import duckdb

        from workspace.skills.audit_analyzer.scripts.predefined import run

        conn = duckdb.connect(":memory:")
        conn.execute("CREATE SCHEMA IF NOT EXISTS oarb")
        conn.execute(
            "CREATE TABLE oarb.audits ("
            "id INTEGER, title VARCHAR, audit_type VARCHAR, "
            "actual_date DATE, status VARCHAR)"
        )
        conn.execute("INSERT INTO oarb.audits VALUES (1, 'x', 'y', NULL, 'z')")
        # ``public.agent_predefined_scripts`` НЕ создана.

        class _Bare:
            def query_sql(self, sql, params=None):
                cur = conn.execute(sql, list(params) if params else [])
                columns = (
                    [c[0] for c in cur.description] if cur.description else []
                )
                rows = [dict(zip(columns, r, strict=False)) for r in cur.fetchall()]
                return {
                    "status": "success",
                    "row_count": len(rows),
                    "columns": columns,
                    "rows": rows,
                }

        result = run(
            "audit_status_summary",
            _Bare(),  # type: ignore[arg-type]
            predefined_table="public.agent_predefined_scripts",
        )
        assert result["status"] == "error"
        assert "не найден" in result["data"]["message"]


class TestParameterValidatorDirect:
    """Тесты ``ParameterValidator`` напрямую (без run)."""

    def test_required_param_missing(self, db_service) -> None:
        script = load_script(db_service, PREDEFINED_TABLE, "violations_by_period")
        merged, err = ParameterValidator.validate(script, {})
        assert merged == {}
        assert err is not None
        assert "date_from" in err

    def test_wrong_type(self, db_service) -> None:
        script = load_script(db_service, PREDEFINED_TABLE, "violations_by_period")
        merged, err = ParameterValidator.validate(
            script, {"date_from": 123, "date_to": "2024-12-31"}
        )
        assert err is not None

    def test_success(self, db_service) -> None:
        script = load_script(db_service, PREDEFINED_TABLE, "violations_by_period")
        merged, err = ParameterValidator.validate(
            script, {"date_from": "2024-01-01", "date_to": "2024-12-31"}
        )
        assert err is None
        assert merged == {"date_from": "2024-01-01", "date_to": "2024-12-31"}


# ---------------------------------------------------------------------------
# Regression: date validator (strict ISO YYYY-MM-DD) + named params/casts
# ---------------------------------------------------------------------------


class TestParameterValidatorDate:
    """Этап 4: строгая ISO-дата — ``YYYY-MM-DD`` с валидным месяцем/днём."""

    @pytest.mark.parametrize(
        "good",
        ["2026-01-01", "2026-12-31", "2000-02-29", "1999-01-01"],
    )
    def test_valid_iso_dates_accepted(self, good: str, db_service) -> None:
        script = load_script(db_service, PREDEFINED_TABLE, "violations_by_period")
        merged, err = ParameterValidator.validate(
            script, {"date_from": good, "date_to": "2024-12-31"}
        )
        assert err is None
        assert merged["date_from"] == good

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "2026-1-1",
            "2026-1-01",
            "2026-01-1",
            "hello",
            "2026-99-99",
            "2026-13-01",
            "2026-02-30",
            "not-a-date",
            "2026/01/01",
        ],
    )
    def test_invalid_dates_rejected(self, bad: str, db_service) -> None:
        script = load_script(db_service, PREDEFINED_TABLE, "violations_by_period")
        merged, err = ParameterValidator.validate(
            script, {"date_from": bad, "date_to": "2024-12-31"}
        )
        assert err is not None, f"{bad!r} should be rejected"
        assert "date_from" in err
        assert merged == {}

    def test_non_string_date_rejected(self, db_service) -> None:
        script = load_script(db_service, PREDEFINED_TABLE, "violations_by_period")
        merged, err = ParameterValidator.validate(
            script, {"date_from": 12345, "date_to": "2024-12-31"}
        )
        assert err is not None
        assert "date_from" in err


class TestDynamicQueryBuilderEdgeCases:
    """Этап 4: named params + ``::`` casts + ``:`` внутри строк + ``min_violations=0``."""

    def test_double_colon_cast_not_replaced(self) -> None:
        from workspace.skills.audit_analyzer.scripts.predefined.builder import (
            DynamicQueryBuilder,
        )

        sql_t = (
            "SELECT id::TEXT AS label, value::INTEGER, :date_from "
            "FROM oarb.audits WHERE actual_date >= :date_from"
        )
        sql, values = DynamicQueryBuilder._convert_to_positional(
            sql_t, {"date_from": "2024-01-01"}
        )
        assert "id::TEXT" in sql
        assert "value::INTEGER" in sql
        # ``:date_from`` встречается в SQL дважды → ``values`` должен
        # содержать значение дважды (для двух ``?`` после конвертации).
        assert values == ["2024-01-01", "2024-01-01"]
        assert sql.count("?") == 2

    def test_colon_inside_string_literal_preserved(self) -> None:
        from workspace.skills.audit_analyzer.scripts.predefined.builder import (
            DynamicQueryBuilder,
        )

        sql_t = (
            "SELECT 'a: b' AS label, :date_from "
            "FROM oarb.audits WHERE actual_date >= :date_from"
        )
        sql, values = DynamicQueryBuilder._convert_to_positional(
            sql_t, {"date_from": "2024-01-01"}
        )
        assert "'a: b'" in sql
        assert values == ["2024-01-01", "2024-01-01"]

    def test_min_violations_zero_keeps_having_clause(self, db_service) -> None:
        """``min_violations=0`` — валидное намерение «все, включая без нарушений»."""
        from workspace.skills.audit_analyzer.scripts.predefined.builder import (
            DynamicQueryBuilder,
        )

        script = load_script(
            db_service, PREDEFINED_TABLE, "audit_effectiveness_summary"
        )
        assert script is not None
        sql_zero, values_zero = DynamicQueryBuilder.build(script, {"min_violations": 0})
        assert "HAVING COUNT(v.id) >= ?" in sql_zero
        assert 0 in values_zero

        sql_absent, values_absent = DynamicQueryBuilder.build(script, {})
        assert "HAVING" not in sql_absent
        assert 0 not in values_absent

        sql_one, values_one = DynamicQueryBuilder.build(script, {"min_violations": 1})
        assert "HAVING COUNT(v.id) >= ?" in sql_one
        assert 1 in values_one