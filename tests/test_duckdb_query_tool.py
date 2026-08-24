"""Тесты для ``workspace/tools/duckdb_query_tool.py``.

Используется in-memory DuckDB через ``set_connection_factory`` (DI),
без реального PG/CacheProvider. Это сохраняет тесты hermetic и
позволяет покрывать все ветки SQL-policy.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from workspace.tools.duckdb_query_tool import DuckdbQueryTool, DuckdbQueryToolConfig


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _make_tool(**config_kwargs) -> DuckdbQueryTool:
    config = DuckdbQueryToolConfig(**config_kwargs)
    tool = DuckdbQueryTool(config=config)

    def _factory():
        conn = duckdb.connect(":memory:")
        conn.execute(
            "CREATE TABLE audits (id INTEGER, year INTEGER, title VARCHAR)"
        )
        conn.executemany(
            "INSERT INTO audits VALUES (?, ?, ?)",
            [
                (1, 2024, "Audit A"),
                (2, 2024, "Audit B"),
                (3, 2025, "Audit C"),
                (4, 2025, "Audit D"),
            ],
        )
        return conn

    tool.set_connection_factory(_factory)
    return tool


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _exec(tool: DuckdbQueryTool, **kwargs) -> str:
    return _run(tool.execute(**kwargs))


def _make_ctx(settings: dict | None = None) -> SimpleNamespace:
    section = SimpleNamespace(**settings) if settings else None
    gateway = SimpleNamespace(duckdb_query=section) if section else SimpleNamespace(duckdb_query=None)
    settings_obj = SimpleNamespace(gateway=gateway)
    return SimpleNamespace(_settings_ref=settings_obj)


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------


class TestConfigAndDiscovery:
    def test_config_key(self) -> None:
        assert DuckdbQueryTool.config_key == "duckdb_query"

    def test_config_cls(self) -> None:
        from pydantic import BaseModel

        assert issubclass(DuckdbQueryTool.config_cls(), BaseModel)
        assert DuckdbQueryTool.config_cls() is DuckdbQueryToolConfig

    def test_default_config(self) -> None:
        c = DuckdbQueryToolConfig()
        assert c.enable is True
        assert c.max_rows == 1000
        assert c.max_result_chars == 50_000
        assert c.query_timeout_sec == 30
        assert c.schema_name == "oarb"

    def test_enabled_default_true(self) -> None:
        assert DuckdbQueryTool.enabled(_make_ctx()) is True

    def test_enabled_explicit_false(self) -> None:
        assert DuckdbQueryTool.enabled(_make_ctx({"enable": False})) is False

    def test_create_no_settings(self) -> None:
        tool = DuckdbQueryTool.create(_make_ctx())
        assert isinstance(tool, DuckdbQueryTool)

    def test_create_with_settings(self) -> None:
        tool = DuckdbQueryTool.create(_make_ctx({"max_rows": 10}))
        assert tool.config.max_rows == 10

    def test_create_invalid_settings_fallback(self) -> None:
        tool = DuckdbQueryTool.create(_make_ctx({"max_rows": "not-an-int"}))
        assert tool.config.max_rows == 1000

    def test_create_no_settings_ref(self) -> None:
        tool = DuckdbQueryTool.create(SimpleNamespace(_settings_ref=None))
        assert tool.config.max_rows == 1000


class TestNameAndDescription:
    def test_name(self) -> None:
        tool = _make_tool()
        assert tool.name == "duckdb_query"

    def test_description_no_domain(self) -> None:
        tool = _make_tool()
        desc = tool.description.lower()
        for word in ("audit", "violations", "audits_index", "audit_analyzer"):
            assert word not in desc, f"description contains forbidden domain word: {word}"


# ---------------------------------------------------------------------
# Execute — успешные сценарии
# ---------------------------------------------------------------------


class TestExecuteSuccess:
    def test_simple_select(self) -> None:
        tool = _make_tool()
        payload = json.loads(_exec(tool, sql="SELECT COUNT(*) FROM audits"))
        assert payload["status"] == "success"
        assert payload["columns"] == ["count_star()"]
        assert payload["rows"] == [[4]]
        assert payload["row_count"] == 1
        assert payload["truncated"] is False

    def test_group_by(self) -> None:
        tool = _make_tool()
        payload = json.loads(_exec(
            tool, sql="SELECT year, COUNT(*) FROM audits GROUP BY year ORDER BY year"
        ))
        assert payload["status"] == "success"
        assert payload["rows"] == [[2024, 2], [2025, 2]]

    def test_with_cte(self) -> None:
        tool = _make_tool()
        payload = json.loads(_exec(
            tool, sql="WITH t AS (SELECT year FROM audits) SELECT COUNT(*) FROM t"
        ))
        assert payload["status"] == "success"
        assert payload["rows"] == [[4]]

    def test_empty_result(self) -> None:
        tool = _make_tool()
        payload = json.loads(_exec(tool, sql="SELECT * FROM audits WHERE id > 100"))
        assert payload["status"] == "success"
        assert payload["rows"] == []
        assert payload["row_count"] == 0

    def test_max_rows_param(self) -> None:
        tool = _make_tool()
        payload = json.loads(_exec(tool, sql="SELECT * FROM audits", max_rows=2))
        assert payload["row_count"] == 2
        assert len(payload["rows"]) == 2

    def test_max_rows_param_overrides_config(self) -> None:
        tool = _make_tool(max_rows=10)
        payload = json.loads(_exec(tool, sql="SELECT * FROM audits", max_rows=3))
        assert payload["row_count"] == 3


# ---------------------------------------------------------------------
# Execute — отказы SQL-policy
# ---------------------------------------------------------------------


class TestExecutePolicy:
    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO audits VALUES (5, 2026, 'X')",
            "UPDATE audits SET year = 2026",
            "DELETE FROM audits",
            "DROP TABLE audits",
            "CREATE TABLE x (a int)",
            "ALTER TABLE audits ADD COLUMN foo int",
            "TRUNCATE audits",
            "CALL sp()",
        ],
    )
    def test_ddl_dml_rejected(self, sql: str) -> None:
        tool = _make_tool()
        payload = json.loads(_exec(tool, sql=sql))
        assert payload["status"] == "error"
        assert payload["error_type"] == "sql_error"

    def test_multiple_statements_rejected(self) -> None:
        tool = _make_tool()
        payload = json.loads(_exec(tool, sql="SELECT 1; DROP TABLE audits;"))
        assert payload["status"] == "error"
        assert "Multiple" in payload["message"]

    def test_empty_sql_rejected(self) -> None:
        tool = _make_tool()
        payload = json.loads(_exec(tool, sql="   "))
        assert payload["status"] == "error"
        assert payload["error_type"] == "sql_error"

    def test_max_rows_over_config(self) -> None:
        tool = _make_tool(max_rows=2)
        payload = json.loads(_exec(tool, sql="SELECT * FROM audits", max_rows=10))
        assert payload["status"] == "error"
        assert "exceeds configured limit" in payload["message"]

    def test_malformed_sql_structured_error(self) -> None:
        tool = _make_tool()
        payload = json.loads(_exec(tool, sql="SELECT FROM WHERE BAD"))
        assert payload["status"] == "error"
        assert payload["error_type"] == "sql_error"
        assert "message" in payload
        assert "traceback" not in payload["message"].lower()


# ---------------------------------------------------------------------
# Sanitize / truncate
# ---------------------------------------------------------------------


class TestSanitizeAndTruncate:
    def test_sanitize_date_in_result(self) -> None:
        tool = _make_tool()
        payload = json.loads(_exec(
            tool, sql="SELECT CAST('2024-01-15' AS DATE) AS d"
        ))
        assert payload["rows"] == [["2024-01-15"]]

    def test_truncate_large_result(self) -> None:
        tool = _make_tool(max_result_chars=200)
        out = _exec(tool, sql="SELECT range FROM range(0, 1000)")
        assert len(out) <= 350
        assert "truncated" in out.lower()

    def test_truncated_flag_set(self) -> None:
        tool = _make_tool(max_result_chars=300)
        payload = json.loads(_exec(tool, sql="SELECT range FROM range(0, 1000)"))
        assert payload["status"] == "success"
        assert payload["truncated"] is True


# ---------------------------------------------------------------------
# Architectural: tool code не должен импортировать workspace.skills
# ---------------------------------------------------------------------


class TestArchitectureIndependence:
    def test_no_skills_import(self) -> None:
        source = Path("workspace/tools/duckdb_query_tool.py").read_text(encoding="utf-8")
        assert "workspace.skills" not in source

    def test_no_audit_strings_in_code(self) -> None:
        """Проверяем только имена/идентификаторы, не docstring и не import-пути."""
        source = Path("workspace/tools/duckdb_query_tool.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden_names = {"audit", "violations", "audits_index", "audit_analyzer"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name in forbidden_names:
                    raise AssertionError(
                        f"forbidden name {node.name!r} in definition at line {node.lineno}"
                    )
            elif isinstance(node, ast.Name):
                if node.id in forbidden_names:
                    raise AssertionError(
                        f"forbidden identifier {node.id!r} at line {node.lineno}"
                    )
            elif isinstance(node, ast.arg):
                if node.arg in forbidden_names:
                    raise AssertionError(
                        f"forbidden arg {node.arg!r} at line {node.lineno}"
                    )
            elif isinstance(node, ast.Attribute):
                if node.attr in forbidden_names:
                    raise AssertionError(
                        f"forbidden attribute {node.attr!r} at line {node.lineno}"
                    )