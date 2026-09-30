"""Политика режима доступа к снимку: классификация SQL и второй уровень защиты.

Порт разделов «query_sql assertion guard» из ``tests/test_cache_provider_mode.py``
и классификатора из ``tests/test_duckdb_cache_store.py``. Миграция
``enterprise-mcp-platform``, фаза 3; удаление агентских тестов — фазы 4/5/9.

Отдельное внимание — «сторож на заведомо плохих данных»: классификатор
смотрит на произвольный текст, поэтому набор ``BAD_SQL`` проверен явно. Если
бы «проверка режима» пропускала мусор, её существование было бы иллюзией
защиты: агент отправил бы произвольный текст в СУБД.
"""

from __future__ import annotations

import pytest

from libs.enterprise_data.snapshot.contracts import (
    CacheAccessMode,
    ReadOnlyAssertionError,
    UnsupportedSqlError,
)
from libs.enterprise_data.snapshot.sql_guard import (
    DDL_KEYWORDS,
    DML_KEYWORDS,
    assert_query_allowed,
    classify_sql,
)


class TestClassifySql:
    @pytest.mark.parametrize(
        "statement",
        [
            "SELECT 1",
            "select * from t",
            "(SELECT 1)",
            "  SELECT a FROM t WHERE b = 1",
            "WITH x AS (SELECT 1) SELECT * FROM x",
            "EXPLAIN SELECT 1",
            "PRAGMA database_list",
        ],
    )
    def test_reads_are_select(self, statement: str) -> None:
        assert classify_sql(statement) == "SELECT"

    @pytest.mark.parametrize("keyword", ["INSERT", "UPDATE", "DELETE", "MERGE"])
    def test_mutations_are_dml(self, keyword: str) -> None:
        assert classify_sql(f"{keyword} INTO t VALUES (1)") == "DML"

    @pytest.mark.parametrize("keyword", ["insert", "update", "delete"])
    def test_dml_is_case_insensitive(self, keyword: str) -> None:
        assert classify_sql(f"{keyword} t") == "DML"

    def test_garbage_is_other_not_select(self) -> None:
        """Мусор не должен проходить как чтение."""
        assert classify_sql("не SQL вовсе") == "OTHER"

    def test_empty_string_is_other(self) -> None:
        assert classify_sql("") == "OTHER"

    def test_whitespace_only_is_other(self) -> None:
        assert classify_sql("   \n\t ") == "OTHER"

    def test_statement_injection_via_leading_comment_is_other(self) -> None:
        """``-- комментарий`` сбивал бы разбор первого слова."""
        assert classify_sql("-- SELECT 1\nDROP TABLE t") == "OTHER"

    def test_non_string_raises(self) -> None:
        with pytest.raises(UnsupportedSqlError):
            classify_sql(None)  # type: ignore[arg-type]

    def test_non_string_raises_for_numbers(self) -> None:
        with pytest.raises(UnsupportedSqlError):
            classify_sql(42)  # type: ignore[arg-type]


class TestDdlAlwaysRejected:
    @pytest.mark.parametrize("keyword", DDL_KEYWORDS)
    def test_ddl_raises(self, keyword: str) -> None:
        with pytest.raises(UnsupportedSqlError):
            classify_sql(f"{keyword} TABLE t")

    def test_ddl_message_names_the_keyword(self) -> None:
        with pytest.raises(UnsupportedSqlError) as excinfo:
            classify_sql("DROP TABLE t")
        assert "DROP" in str(excinfo.value)

    def test_ddl_lowercase_also_raises(self) -> None:
        with pytest.raises(UnsupportedSqlError):
            classify_sql("create table t(a integer)")


class TestAssertQueryAllowed:
    def test_select_allowed_in_read_only(self) -> None:
        assert assert_query_allowed("SELECT 1", read_only=True) == "SELECT"

    def test_select_allowed_in_read_write(self) -> None:
        assert assert_query_allowed("SELECT 1", read_only=False) == "SELECT"

    @pytest.mark.parametrize("statement", DML_KEYWORDS)
    def test_dml_allowed_in_read_write(self, statement: str) -> None:
        assert assert_query_allowed(f"{statement} t", read_only=False) == "DML"

    @pytest.mark.parametrize("statement", DML_KEYWORDS)
    def test_dml_rejected_in_read_only(self, statement: str) -> None:
        with pytest.raises(ReadOnlyAssertionError):
            assert_query_allowed(f"{statement} t", read_only=True)

    @pytest.mark.parametrize(
        "statement",
        [
            "не SQL вовсе",
            "",
            "   ",
            "-- SELECT 1",
            "GRANT ALL ON t TO x",
            "COPY t FROM 'f.csv'",
            "SET GLOBAL x = 1",
            "ATTACH 'other.duckdb'",
            "INSTALL httpfs",
            "LOAD httpfs",
        ],
    )
    def test_garbage_rejected_in_both_modes(self, statement: str) -> None:
        """Сторож на мусоре: неизвестный текст не проходит ни в одном режиме."""
        with pytest.raises(UnsupportedSqlError):
            assert_query_allowed(statement, read_only=True)
        with pytest.raises(UnsupportedSqlError):
            assert_query_allowed(statement, read_only=False)

    def test_multi_statement_injection_is_rejected(self) -> None:
        """``SELECT 1; DROP TABLE t`` начинается с SELECT — но выполняется как цепочка.

        Проверка по первому слову такой текст пропустила бы; отвергает его
        ``classify_sql``, чтобы политика режима не зависела от того, разберёт ли
        текст AST-слой.
        """
        with pytest.raises(UnsupportedSqlError) as excinfo:
            assert_query_allowed("SELECT 1; DROP TABLE t", read_only=False)
        assert "multi-statement" in str(excinfo.value)

    def test_trailing_semicolon_is_allowed(self) -> None:
        """Один оператор с завершающей ``;`` — не цепочка."""
        assert assert_query_allowed("SELECT 1;", read_only=False) == "SELECT"

    def test_error_code_is_stable(self) -> None:
        with pytest.raises(ReadOnlyAssertionError) as excinfo:
            assert_query_allowed("DELETE FROM t", read_only=True)
        assert excinfo.value.code == "read_only_assertion"

    def test_unsupported_error_code_is_stable(self) -> None:
        with pytest.raises(UnsupportedSqlError) as excinfo:
            assert_query_allowed("GRANT ALL ON t TO x", read_only=False)
        assert excinfo.value.code == "unsupported_sql"


class TestModeEnumIsSingleSource:
    def test_guard_receives_mode_from_enum_only(self) -> None:
        """Второе определение режима молча ломало бы READ_ONLY-защиту."""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / "libs"
        owners = [
            p
            for p in root.rglob("*.py")
            if "class CacheAccessMode" in p.read_text(encoding="utf-8")
        ]
        assert len(owners) == 1
        assert owners[0].name == "contracts.py"

    def test_mode_values_unchanged(self) -> None:
        assert CacheAccessMode.READ_WRITE.value == "READ_WRITE"
        assert CacheAccessMode.READ_ONLY.value == "READ_ONLY"
