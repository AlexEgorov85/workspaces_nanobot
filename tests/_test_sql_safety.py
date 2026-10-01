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

    def test_table_order_preserved(self) -> None:
        """Порядок таблиц в schema["tables"] сохраняется в выводе.

        Этап 12: ``build_schema`` упорядочивает результат по входному списку
        ``tables``. ``format_schema`` итерирует в этом порядке — для
        стабильного LLM-промпта. Контракт: ``format_schema`` сохраняет
        insertion order dict, и этот порядок == порядку входного ``tables``.
        """
        # Когда входной список в одном порядке, формат стабильный.
        schema = {
            "schema": "oarb",
            "tables": {
                "audits": {"columns": {"id": {"type": "int", "not_null": True}}},
                "violations": {"columns": {"id": {"type": "int", "not_null": True}}},
                "audit_reports": {"columns": {"id": {"type": "int", "not_null": True}}},
            },
        }
        out = format_schema(schema)
        i_audits = out.index('"oarb".audits —')
        i_violations = out.index('"oarb".violations —')
        i_reports = out.index('"oarb".audit_reports —')
        # insertion order dict → audits первая.
        assert i_audits < i_violations < i_reports


class TestBuildSchemaTableOrder:
    """Этап 12: ``build_schema`` упорядочивает результат по входному списку.

    Регресс: до правки таблицы сортировались по ``information_schema`` ORDER BY
    ``table_name`` — то есть алфавитно, не по тому, как пользователь передал
    ``tables``. Это давало нестабильный LLM-промпт и могло сбивать few-shot
    reasoning. После фикса порядок == порядку входа.
    """

    def _make_db(self) -> Any:
        import duckdb

        conn = duckdb.connect(":memory:")
        conn.execute("CREATE SCHEMA IF NOT EXISTS main")
        for tbl in ("audits", "violations", "audit_reports"):
            conn.execute(f'CREATE TABLE main."{tbl}" (id INTEGER, title VARCHAR)')
            conn.execute(f'INSERT INTO main."{tbl}" VALUES (?, ?)', [1, "x"])
        return conn

    def test_input_order_preserved_in_output(self) -> Any:
        from lib.utils.duckdb_query import build_schema

        conn = self._make_db()
        schema = build_schema(
            conn,
            schema="main",
            tables=["audits", "violations", "audit_reports"],
            meta_reader=lambda s: {},
        )
        keys = list(schema["tables"].keys())
        assert keys == ["audits", "violations", "audit_reports"]

    def test_reverse_input_order_preserved(self) -> Any:
        from lib.utils.duckdb_query import build_schema

        conn = self._make_db()
        schema = build_schema(
            conn,
            schema="main",
            tables=["audit_reports", "violations", "audits"],
            meta_reader=lambda s: {},
        )
        keys = list(schema["tables"].keys())
        # Входной список в обратном порядке → result тоже в обратном.
        assert keys == ["audit_reports", "violations", "audits"]

    def test_subset_preserved_in_input_order(self) -> Any:
        from lib.utils.duckdb_query import build_schema

        conn = self._make_db()
        # Подмножество таблиц в не-алфавитном порядке.
        schema = build_schema(
            conn,
            schema="main",
            tables=["violations", "audits"],
            meta_reader=lambda s: {},
        )
        keys = list(schema["tables"].keys())
        assert keys == ["violations", "audits"]
