"""Unit-тесты для ``lib/utils/sql_safety.py``."""

from __future__ import annotations

import pytest

from lib.utils.sql_safety import format_schema, validate_sql


class TestValidateSql:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1",
            "select 1",
            "  SELECT * FROM audits",
            "WITH t AS (SELECT 1) SELECT * FROM t",
            "EXPLAIN SELECT 1",
        ],
    )
    def test_allowed(self, sql: str) -> None:
        assert validate_sql(sql) is None

    @pytest.mark.parametrize(
        "sql,expected_word",
        [
            ("INSERT INTO t VALUES (1)", "INSERT"),
            ("UPDATE t SET a=1", "UPDATE"),
            ("DELETE FROM t", "DELETE"),
            ("DROP TABLE t", "DROP"),
            ("CREATE TABLE t (a int)", "CREATE"),
            ("ALTER TABLE t ADD COLUMN x int", "ALTER"),
            ("TRUNCATE t", "TRUNCATE"),
            ("EXECUTE sp", "EXECUTE"),
            ("CALL sp()", "CALL"),
            ("MERGE INTO t", "MERGE"),
            ("REPLACE INTO t VALUES (1)", "REPLACE"),
        ],
    )
    def test_ddl_dml_rejected(self, sql: str, expected_word: str) -> None:
        err = validate_sql(sql)
        assert err is not None
        assert expected_word in err

    def test_empty_rejected(self) -> None:
        assert validate_sql("") is not None
        assert validate_sql("   \n  ") is not None

    def test_multiple_statements_rejected(self) -> None:
        sql = "SELECT 1; SELECT 2;"
        err = validate_sql(sql)
        assert err is not None
        assert "Multiple" in err

    def test_single_trailing_semicolon_allowed(self) -> None:
        assert validate_sql("SELECT 1;") is None


class TestFormatSchema:
    def test_basic(self) -> None:
        schema = {
            "schema": "oarb",
            "tables": {
                "audits": {
                    "comment": "Аудиторские проверки",
                    "columns": {
                        "id": {"type": "integer", "not_null": True, "comment": "ID"},
                        "title": {"type": "varchar(500)", "not_null": False},
                    },
                },
            },
        }
        out = format_schema(schema)
        assert "=== Schema: oarb ===" in out
        assert '"oarb".audits — Аудиторские проверки' in out
        assert "id: integer NOT NULL — ID" in out
        assert "title: varchar(500)" in out
        assert "NOT NULL" in out

    def test_empty_schema(self) -> None:
        out = format_schema({"schema": "x", "tables": {}})
        assert "=== Schema: x ===" in out

    def test_missing_comment(self) -> None:
        schema = {
            "schema": "s",
            "tables": {
                "t": {"columns": {"a": {"type": "int", "not_null": False}}},
            },
        }
        out = format_schema(schema)
        assert '"s".t — ' in out
        assert "a: int" in out