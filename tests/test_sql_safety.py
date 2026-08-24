"""Unit-тесты для ``lib/utils/sql_safety.py``."""

from __future__ import annotations

import pytest

from lib.utils.sql_safety import (
    SqlPolicy,
    format_schema,
    normalize_sql,
    query_hash,
    validate_sql,
    validate_sql_report,
)


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


class TestAstPolicy:
    """AST-политика: SELECT INTO, опасные функции, системные каталоги."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * INTO backups FROM audits",
            "SELECT pg_read_file('/etc/passwd')",
            "SELECT pg_sleep(10)",
            "SELECT dblink('dbname=x', 'SELECT 1')",
            "SELECT nextval('seq')",
            "SELECT setval('seq', 100)",
            "SELECT * FROM information_schema.tables",
            "SELECT * FROM pg_catalog.pg_tables",
        ],
    )
    def test_ast_violations_rejected(self, sql: str) -> None:
        err = validate_sql(sql)
        assert err is not None

    def test_select_into_reason(self) -> None:
        assert "INTO" in (validate_sql("SELECT 1 INTO x") or "")

    def test_function_reason(self) -> None:
        assert "PG_SLEEP" in (validate_sql("SELECT pg_sleep(1)") or "")

    def test_catalog_reason(self) -> None:
        assert "information_schema" in (
            validate_sql("SELECT * FROM information_schema.columns") or ""
        )

    def test_union_allowed(self) -> None:
        assert validate_sql("SELECT 1 UNION SELECT 2") is None

    def test_explain_inner_statement_validated(self) -> None:
        assert validate_sql("EXPLAIN SELECT 1") is None
        assert "PG_SLEEP" in (validate_sql("EXPLAIN SELECT pg_sleep(1)") or "")

    def test_explain_of_ddl_rejected(self) -> None:
        assert validate_sql("EXPLAIN INSERT INTO t VALUES (1)") is not None

    def test_policy_allow_catalog(self) -> None:
        policy = SqlPolicy(allow_catalog_access=True)
        report = validate_sql_report(
            "SELECT * FROM information_schema.tables", policy=policy
        )
        assert report.allowed is True

    def test_report_structure(self) -> None:
        report = validate_sql_report("SELECT pg_sleep(1)")
        assert report.allowed is False
        assert report.issues
        assert report.normalized_sql == "SELECT pg_sleep(1)"
        assert len(report.query_hash) == 64
        payload = report.to_dict()
        assert payload["allowed"] is False
        assert isinstance(payload["issues"], list)

    def test_normalize_and_hash_stable(self) -> None:
        a = normalize_sql("SELECT /* c */\n   1")
        b = normalize_sql("SELECT 1")
        assert a == b
        assert query_hash(a) == query_hash(b)


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
