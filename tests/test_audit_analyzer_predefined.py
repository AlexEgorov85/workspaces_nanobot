"""Тесты для ``predefined.run()``: 5 скриптов + крайние случаи.

Покрывают:
  * реальное выполнение на DuckDB-fixture (Step 21: «определение → params →
    SQL builder → DuckDBService → реальный результат»);
  * валидацию параметров (Step 7 — ``ParameterValidator``);
  * правильную сборку SQL (Step 8 — ``DynamicQueryBuilder``).

Не тестируют LLM-Agent — только Python-API ``predefined.run`` и
``DuckDBServiceProtocol``.
"""

from __future__ import annotations

from typing import Any

import duckdb
import pytest

from workspace.skills.audit_analyzer.predefined import (
    REGISTRY,
    ParameterValidator,
    run,
)


def _seed_db() -> duckdb.DuckDBPyConnection:
    """Создать минимальный DuckDB-fixture со схемой ``oarb``.

    Используется в тестах через ``make_db_service`` (см.ниже).
    """
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
            (1, "Fire safety", "planned", None, "\u0417\u0430\u043f\u043b\u0430\u043d\u0438\u0440\u043e\u0432\u0430\u043d\u0430"),
            (2, "Financial audit", "planned", "2024-05-01", "\u0417\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0430"),
            (3, "Compliance review", "extra", "2024-08-15", "\u0417\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0430"),
            (4, "School check", "extra", "2025-02-10", "\u0412 \u0440\u0430\u0431\u043e\u0442\u0435"),
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
            (10, 2, "F-1", "Fire exit blocked", "high"),
            (11, 2, "F-2", "No extinguisher", "medium"),
            (12, 3, "D-1", "Missing signature", "low"),
        ],
    )
    return conn


class _DBService:
    """Адаптер in-memory DuckDB к ``DuckDBServiceProtocol``.

    Используется в тестах как подставной generic-сервис (без зависимости от
    production ``DuckDbCacheStore``/``CacheProvider``). Реализует единый
    ``query_sql``-контракт generic Core: результат — ``{status, row_count,
    columns, rows}``, где ``rows`` — список dict с ключами-именами колонок.
    """

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn

    def query_sql(self, sql: str, params: list[Any] | None = None) -> dict[str, Any]:
        try:
            if params:
                result = self._conn.execute(sql, list(params))
            else:
                result = self._conn.execute(sql)
            columns = [c[0] for c in result.description] if result.description else []
            rows = [
                dict(zip(columns, r, strict=False))
                for r in result.fetchall()
            ]
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


@pytest.fixture
def db_service():
    return _DBService(_seed_db())


# ---------------------------------------------------------------------------
# 1. audit_status_summary — no params, реальный GROUP BY
# ---------------------------------------------------------------------------


class TestPredefinedAuditStatusSummary:
    def test_no_params(self) -> None:
        assert "audit_status_summary" in REGISTRY
        script = REGISTRY["audit_status_summary"]
        assert script.parameters == {}

    def test_execute_real(self, db_service) -> None:
        result = run("audit_status_summary", db_service)
        assert result["mode"] == "predefined"
        assert result["status"] == "success"
        rows_by_status = {r["status"]: r["cnt"] for r in result["data"]["result"]["rows"]}
        assert rows_by_status == {
            "\u0412 \u0440\u0430\u0431\u043e\u0442\u0435": 1,
            "\u0417\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0430": 2,
            "\u0417\u0430\u043f\u043b\u0430\u043d\u0438\u0440\u043e\u0432\u0430\u043d\u0430": 1,
        }

    def test_unknown_param_caught(self, db_service) -> None:
        result = run("audit_status_summary", db_service, {"foo": "bar"})
        assert result["status"] == "error"
        assert "foo" in result["data"]["message"]


# ---------------------------------------------------------------------------
# 2. top_violations_by_type — no params, ORDER BY count DESC
# ---------------------------------------------------------------------------


class TestPredefinedTopViolationsByType:
    def test_no_params(self) -> None:
        script = REGISTRY["top_violations_by_type"]
        assert script.parameters == {}

    def test_execute_real(self, db_service) -> None:
        result = run("top_violations_by_type", db_service)
        assert result["status"] == "success"
        rows = result["data"]["result"]["rows"]
        assert len(rows) == 3
        # Первые строки отсортированы по cnt DESC.
        counts = [r["cnt"] for r in rows]
        assert counts == sorted(counts, reverse=True)


# ---------------------------------------------------------------------------
# 3. violations_by_period — 2 required date params
# ---------------------------------------------------------------------------


class TestPredefinedViolationsByPeriod:
    def test_required_params_declared(self) -> None:
        script = REGISTRY["violations_by_period"]
        assert script.parameters["date_from"].required is True
        assert script.parameters["date_to"].required is True
        assert script.parameters["date_from"].type == "date"
        assert script.parameters["date_to"].type == "date"

    def test_execute_with_valid_dates(self, db_service) -> None:
        result = run(
            "violations_by_period",
            db_service,
            {"date_from": "2024-01-01", "date_to": "2024-12-31"},
        )
        assert result["status"] == "success"
        result_rows = result["data"]["result"]["rows"]
        assert len(result_rows) == 3
        codes = sorted(r["violation_code"] for r in result_rows)
        assert codes == ["D-1", "F-1", "F-2"]

    def test_missing_required_param(self, db_service) -> None:
        result = run("violations_by_period", db_service)
        assert result["status"] == "error"
        assert "\u0434\u0430\u0442\u0430" in result["data"]["message"].lower() or (
            "date_from" in result["data"]["message"]
        )

    def test_missing_only_date_to(self, db_service) -> None:
        result = run(
            "violations_by_period", db_service, {"date_from": "2024-01-01"}
        )
        assert result["status"] == "error"
        assert "date_to" in result["data"]["message"]

    def test_wrong_type_int_date(self, db_service) -> None:
        result = run(
            "violations_by_period",
            db_service,
            {"date_from": 123, "date_to": "2024-12-31"},
        )
        assert result["status"] == "error"
        assert "date_from" in result["data"]["message"]

    def test_boundary_dates_inclusive(self, db_service) -> None:
        """date_to включительно — крайняя дата даёт нарушение."""
        result = run(
            "violations_by_period",
            db_service,
            {"date_from": "2024-08-15", "date_to": "2024-08-15"},
        )
        assert result["status"] == "success"
        assert result["data"]["result"]["row_count"] == 1
        assert result["data"]["result"]["rows"][0]["violation_code"] == "D-1"

    def test_unknown_param_caught(self, db_service) -> None:
        result = run(
            "violations_by_period",
            db_service,
            {"date_from": "2024-01-01", "date_to": "2024-12-31", "extra": "x"},
        )
        assert result["status"] == "error"
        assert "extra" in result["data"]["message"]


# ---------------------------------------------------------------------------
# 4. audits_by_period — 2 required date params
# ---------------------------------------------------------------------------


class TestPredefinedAuditsByPeriod:
    def test_required_params(self) -> None:
        script = REGISTRY["audits_by_period"]
        assert script.parameters["date_from"].required is True
        assert script.parameters["date_to"].required is True

    def test_execute_real(self, db_service) -> None:
        result = run(
            "audits_by_period",
            db_service,
            {"date_from": "2024-01-01", "date_to": "2024-12-31"},
        )
        assert result["status"] == "success"
        rows = result["data"]["result"]["rows"]
        # 2 аудита в 2024 (id=2, id=3); id=4 в 2025, id=1 NULL.
        ids = sorted(r["id"] for r in rows)
        assert ids == [2, 3]

    def test_exclude_null_dates(self, db_service) -> None:
        """Аудит с NULL actual_date (id=1) не попадает в результат."""
        result = run(
            "audits_by_period",
            db_service,
            {"date_from": "1900-01-01", "date_to": "2100-01-01"},
        )
        assert result["status"] == "success"
        ids = [r["id"] for r in result["data"]["result"]["rows"]]
        assert 1 not in ids


# ---------------------------------------------------------------------------
# 5. audit_effectiveness_summary — no params, GROUP BY audits LEFT JOIN violations
# ---------------------------------------------------------------------------


class TestPredefinedAuditEffectivenessSummary:
    def test_no_required_params(self) -> None:
        """Скрипт имеет только опциональный фильтр ``min_violations``."""
        script = REGISTRY["audit_effectiveness_summary"]
        assert set(script.parameters.keys()) == {"min_violations"}
        assert script.parameters["min_violations"].required is False

    def test_execute_real(self, db_service) -> None:
        result = run("audit_effectiveness_summary", db_service)
        assert result["status"] == "success"
        rows = result["data"]["result"]["rows"]
        # audits: id=1 (NULL date — excluded), id=2 (2 violations),
        # id=3 (1 violation), id=4 (0 violations).
        severity_by_audit = {
            r["audit_id"]: (r["violations_count"], r["severity_level"])
            for r in rows
        }
        assert severity_by_audit[2] == (2, "\u0414\u043e\u043f\u0443\u0441\u0442\u0438\u043c\u044b\u0435 \u043d\u0430\u0440\u0443\u0448\u0435\u043d\u0438\u044f")
        assert severity_by_audit[3] == (1, "\u0414\u043e\u043f\u0443\u0441\u0442\u0438\u043c\u044b\u0435 \u043d\u0430\u0440\u0443\u0448\u0435\u043d\u0438\u044f")
        assert severity_by_audit[4] == (0, "\u0411\u0435\u0437 \u043d\u0430\u0440\u0443\u0448\u0435\u043d\u0438\u0439")


# ---------------------------------------------------------------------------
# Cross-cutting: validator/builder, none-shown behaviour
# ---------------------------------------------------------------------------


class TestPredefinedKnownScripts:
    def test_all_known_scripts_return(self, db_service) -> None:
        for name in REGISTRY:
            result = run(name, db_service, {})
            assert result["status"] in ("success", "error")
            if result["status"] == "error":
                # Скрипты без required params не падают; c required — пустые params.
                assert name in ("violations_by_period", "audits_by_period")

    def test_unknown_script(self, db_service) -> None:
        result = run("nonexistent", db_service)
        assert result["status"] == "error"
        assert "\u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d" in result["data"]["message"]


class TestParameterValidatorDirect:
    """Тесты ``ParameterValidator`` напрямую (без run)."""

    def test_required_param_missing(self) -> None:
        from workspace.skills.audit_analyzer.predefined import REGISTRY

        script = REGISTRY["violations_by_period"]
        merged, err = ParameterValidator.validate(script, {})
        assert merged == {}
        assert err is not None
        assert "date_from" in err

    def test_wrong_type(self) -> None:
        from workspace.skills.audit_analyzer.predefined import REGISTRY

        script = REGISTRY["violations_by_period"]
        merged, err = ParameterValidator.validate(
            script, {"date_from": 123, "date_to": "2024-12-31"}
        )
        assert err is not None

    def test_success(self) -> None:
        from workspace.skills.audit_analyzer.predefined import REGISTRY

        script = REGISTRY["violations_by_period"]
        merged, err = ParameterValidator.validate(
            script, {"date_from": "2024-01-01", "date_to": "2024-12-31"}
        )
        assert err is None
        assert merged == {"date_from": "2024-01-01", "date_to": "2024-12-31"}
