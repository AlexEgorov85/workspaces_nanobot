"""Читающая половина хранилища снимка.

Порт ``tests/test_duckdb_cache_store.py`` (режим соединения, ``query_sql``,
``explain``, ``get_schema``, векторные чтения) и раздела «Layering» из
``tests/test_cache_provider_mode.py``. Миграция ``enterprise-mcp-platform``,
фаза 3; удаление агентских тестов — фазы 4/5/9.

Фикстуры локальные для модуля: ``conftest.py`` платформы общий и правится не
здесь, а снимок — это не PostgreSQL, ему нужен свой файл.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from libs.enterprise_common.errors import InfrastructureError
from libs.enterprise_data.snapshot import (
    CacheAccessMode,
    DuckDbSnapshotStore,
    open_snapshot_store,
    resolve_snapshot_path,
)
from libs.enterprise_data.snapshot.contracts import (
    ReadOnlyAssertionError,
    UnsupportedSqlError,
)
from libs.enterprise_data.snapshot.query import (
    build_schema,
    explain_query,
    rewrite_duck_sql,
    run_query,
)

SCHEMA = "oarb"
VECTOR_TABLE = "agent_embeddings"
STORAGE = f"{SCHEMA}.{VECTOR_TABLE}"
DIM = 4


def _vector(i: int, dim: int = DIM) -> str:
    """Эмбеддинг строкой — так колонка ``embedding`` лежит в снимке."""
    return "[" + ",".join(str(0.1 * (i + 1) * (j + 1)) for j in range(dim)) + "]"


def make_snapshot(path: Path, *, rows: int = 3, dim: int = DIM) -> Path:
    """Создать файл снимка с таблицей и данными векторного хранилища."""
    conn = duckdb.connect(str(path))
    conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')
    conn.execute(
        f'CREATE TABLE "{SCHEMA}"."{VECTOR_TABLE}" ('
        "id INTEGER, source VARCHAR, content VARCHAR, search_text VARCHAR, "
        '"table" VARCHAR, pk_value VARCHAR, chunk_index INTEGER, '
        "chunk_count INTEGER, row_data VARCHAR, embedding VARCHAR)"
    )
    for i in range(rows):
        conn.execute(
            f'INSERT INTO "{SCHEMA}"."{VECTOR_TABLE}" VALUES (?,?,?,?,?,?,?,?,?,?)',
            [
                i,
                "idx_a",
                f"текст документа {i}",
                f"поисковый текст {i}",
                "public.audits",
                f"pk-{i}",
                0,
                1,
                f'{{"id": {i}}}',
                _vector(i, dim),
            ],
        )
    conn.execute(f'CREATE TABLE "{SCHEMA}"."audits" (id INTEGER, title VARCHAR)')
    conn.execute(f'INSERT INTO "{SCHEMA}"."audits" VALUES (1, \'аудит\')')
    conn.close()
    return path


@pytest.fixture
def snapshot_path(tmp_path: Path) -> Path:
    return make_snapshot(tmp_path / "cache.duckdb")


@pytest.fixture
def store(snapshot_path: Path) -> DuckDbSnapshotStore:
    """Хранилище в READ_ONLY — обычное состояние процесса."""
    instance = open_snapshot_store(
        str(snapshot_path),
        CacheAccessMode.READ_ONLY,
        schema=SCHEMA,
        vector_db_table=STORAGE,
    )
    try:
        yield instance
    finally:
        instance.close()


class TestFactoryAndLifecycle:
    def test_factory_verifies_and_returns_ready_store(self, snapshot_path: Path) -> None:
        store = open_snapshot_store(
            str(snapshot_path), CacheAccessMode.READ_ONLY, schema=SCHEMA
        )
        try:
            assert store.is_ready() is True
        finally:
            store.close()

    def test_store_is_a_cache_store_instance(self, snapshot_path: Path) -> None:
        from libs.enterprise_data.snapshot.contracts import CacheStore

        store = open_snapshot_store(str(snapshot_path), schema=SCHEMA)
        try:
            assert isinstance(store, CacheStore)
        finally:
            store.close()

    def test_connect_reports_read_only_mode(self, snapshot_path: Path) -> None:
        instance = DuckDbSnapshotStore(
            path=str(snapshot_path), mode=CacheAccessMode.READ_ONLY
        )
        try:
            assert instance.connect() is True
            assert instance._duckdb_read_only is True
        finally:
            instance.close()

    def test_close_makes_store_not_ready(self, snapshot_path: Path) -> None:
        instance = open_snapshot_store(str(snapshot_path), schema=SCHEMA)
        instance.close()
        assert instance.is_ready() is False

    def test_works_as_context_manager(self, snapshot_path: Path) -> None:
        """Прямая конструкция не проверяет файл — это делает фабрика.

        Иначе «открытый» экземпляр протёк бы наружу с ``is_ready() == False``,
        и первый же запрос упал бы вместо fail-fast на старте.
        """
        instance = DuckDbSnapshotStore(path=str(snapshot_path), schema=SCHEMA)
        assert instance.is_ready() is False
        instance.connect()
        with instance as ctx:
            assert ctx.is_ready() is True
        assert instance.is_ready() is False

    def test_path_is_a_constructor_parameter(self) -> None:
        """Ни project.json агента, ни абсолютных путей в коде: путь — параметр."""
        import inspect

        params = inspect.signature(DuckDbSnapshotStore.__init__).parameters
        assert "path" in params
        source = inspect.getsource(resolve_snapshot_path)
        assert "config" not in source.lower() or "SETTINGS" not in source

    def test_resolve_snapshot_path_expands_directory(self, tmp_path: Path) -> None:
        """Каталог + имя файла — как ``gateway.cache.local_path`` у агента."""
        resolved = resolve_snapshot_path(str(tmp_path))
        assert Path(resolved) == tmp_path / "cache.duckdb"

    def test_resolve_snapshot_path_custom_filename(self, tmp_path: Path) -> None:
        assert Path(resolve_snapshot_path(str(tmp_path), "other.duckdb")).name == (
            "other.duckdb"
        )

    def test_resolve_snapshot_path_without_dir_is_an_error(self) -> None:
        from libs.enterprise_data.snapshot import CacheOpenError

        with pytest.raises(CacheOpenError):
            resolve_snapshot_path("")


class TestQuerySql:
    def test_select_allowed_in_read_only(self, store: DuckDbSnapshotStore) -> None:
        res = store.query_sql(f'SELECT COUNT(*) AS n FROM "{SCHEMA}"."audits"')
        assert res["status"] == "success"
        assert res["rows"][0]["n"] == 1

    def test_select_with_params(self, store: DuckDbSnapshotStore) -> None:
        res = store.query_sql(
            f'SELECT content FROM "{SCHEMA}"."{VECTOR_TABLE}" WHERE source = %s',
            ["idx_a"],
        )
        assert res["status"] == "success"
        assert res["row_count"] == 3

    def test_percent_s_is_rewritten_for_duckdb(self, store: DuckDbSnapshotStore) -> None:
        res = store.query_sql(
            f'SELECT id FROM "{SCHEMA}"."{VECTOR_TABLE}" WHERE source = %s', ["idx_a"]
        )
        assert res["rows"][0]["id"] == 0

    def test_read_only_rejects_insert(self, store: DuckDbSnapshotStore) -> None:
        with pytest.raises(ReadOnlyAssertionError):
            store.query_sql(f'INSERT INTO "{SCHEMA}"."audits" VALUES (2, \'x\')')

    def test_read_only_rejects_update(self, store: DuckDbSnapshotStore) -> None:
        with pytest.raises(ReadOnlyAssertionError):
            store.query_sql(f'UPDATE "{SCHEMA}"."audits" SET id = 2')

    def test_read_only_rejects_delete(self, store: DuckDbSnapshotStore) -> None:
        with pytest.raises(ReadOnlyAssertionError):
            store.query_sql(f'DELETE FROM "{SCHEMA}"."audits"')

    @pytest.mark.parametrize(
        "statement",
        [
            'CREATE TABLE "oarb"."t"(a INTEGER)',
            'DROP TABLE "oarb"."audits"',
            'ALTER TABLE "oarb"."audits" ADD COLUMN b INTEGER',
            'TRUNCATE TABLE "oarb"."audits"',
        ],
    )
    def test_ddl_rejected_in_any_mode(
        self, store: DuckDbSnapshotStore, statement: str
    ) -> None:
        with pytest.raises(UnsupportedSqlError):
            store.query_sql(statement)

    def test_error_result_is_a_value_not_an_exception(
        self, store: DuckDbSnapshotStore
    ) -> None:
        """Ошибка запроса возвращается значением: снимок доступен, запрос — нет."""
        res = store.query_sql("SELECT * FROM table_that_does_not_exist")
        assert res["status"] == "error"
        assert res["error"]


class TestExplain:
    def test_valid_statement(self, store: DuckDbSnapshotStore) -> None:
        res = store.explain(f'SELECT id FROM "{SCHEMA}"."audits"')
        assert res["valid"] is True
        assert res["plan"]

    def test_invalid_statement(self, store: DuckDbSnapshotStore) -> None:
        res = store.explain("SELECT FROM WHERE")
        assert res["valid"] is False
        assert res["error"]


class TestGetSchema:
    def test_lists_requested_tables(self, store: DuckDbSnapshotStore) -> None:
        res = store.get_schema(SCHEMA, ["audits"])
        assert res["schema"] == SCHEMA
        assert "audits" in res["tables"]
        assert "id" in res["tables"]["audits"]["columns"]

    def test_order_follows_requested_tables(self, store: DuckDbSnapshotStore) -> None:
        res = store.get_schema(SCHEMA, ["audits", VECTOR_TABLE])
        assert list(res["tables"]) == ["audits", VECTOR_TABLE]

    def test_column_metadata_present(self, store: DuckDbSnapshotStore) -> None:
        res = store.get_schema(SCHEMA, ["audits"])
        assert res["tables"]["audits"]["columns"]["id"]["type"] == "INTEGER"


class TestVectorReads:
    def test_vector_sources(self, store: DuckDbSnapshotStore) -> None:
        assert store.vector_sources() == ["idx_a"]

    def test_vector_source_stats_without_building(self, store: DuckDbSnapshotStore) -> None:
        rows = store.vector_source_stats()
        assert rows == [{"source": "idx_a", "vector_count": 3,
                         "embedding_sample": rows[0]["embedding_sample"]}]
        assert rows[0]["vector_count"] == 3

    def test_fetch_source_vectors(self, store: DuckDbSnapshotStore) -> None:
        rows = store.fetch_source_vectors("idx_a")
        assert len(rows) == 3
        assert rows[0]["source"] == "idx_a"
        assert rows[0]["table"] == "public.audits"
        assert rows[0]["pk_value"] == "pk-0"
        assert rows[0]["content"] == "текст документа 0"

    def test_fetch_source_vectors_unknown_source(self, store: DuckDbSnapshotStore) -> None:
        assert store.fetch_source_vectors("nope") == []

    def test_fetch_chunk_payload(self, store: DuckDbSnapshotStore) -> None:
        payload = store.fetch_chunk_payload("idx_a", "pk-1", 0)
        assert payload["content"] == "текст документа 1"
        assert payload["row"] == {"id": 1}
        assert payload["chunk_count"] == 1

    def test_fetch_chunk_payload_missing_chunk(self, store: DuckDbSnapshotStore) -> None:
        assert store.fetch_chunk_payload("idx_a", "pk-1", 99) == {}

    def test_reads_work_without_vector_table_config(self, snapshot_path: Path) -> None:
        """Без ``vector_db_table`` векторных чтений нет, а падения тоже нет."""
        store = open_snapshot_store(str(snapshot_path), schema=SCHEMA)
        try:
            assert store.vector_sources() == []
            assert store.vector_source_stats() == []
        finally:
            store.close()

    def test_missing_vector_table_is_empty_not_a_failure(
        self, snapshot_path: Path
    ) -> None:
        """Нет таблицы-хранилища = «индексов нет», а не ошибка чтения."""
        store = open_snapshot_store(
            str(snapshot_path), schema=SCHEMA, vector_db_table=f"{SCHEMA}.absent"
        )
        try:
            assert store.vector_sources() == []
        finally:
            store.close()


class TestSearchVectorWithoutAccessor:
    def test_raises_infrastructure_error(self, store: DuckDbSnapshotStore) -> None:
        """Индекс не подключён — это сбой инфраструктуры, а не пустая выдача."""
        with pytest.raises(InfrastructureError) as excinfo:
            store.search_vector("запрос", "idx_a")
        assert excinfo.value.code == "index_not_built"

    def test_preload_without_accessor_is_empty(self, store: DuckDbSnapshotStore) -> None:
        assert store.preload_indexes() == []


class TestGetStats:
    def test_reports_tables_and_mode(self, store: DuckDbSnapshotStore) -> None:
        stats = store.get_stats()
        assert stats["is_ready"] is True
        assert stats["mode"] == "READ_ONLY"
        assert stats["tables"]["audits"]["rows"] == 1
        assert stats["vector_sources"]["idx_a"]["rows"] == 3


class TestQueryHelpersOnFakeConnection:
    """Исполнитель запросов работает на переданном соединении."""

    def test_rewrite_placeholder(self) -> None:
        assert rewrite_duck_sql("SELECT %s") == "SELECT ?"

    def test_rewrite_to_char_month(self) -> None:
        assert "strftime" in rewrite_duck_sql("SELECT TO_CHAR(d, 'Month')")

    def test_run_query_normalizes_rows(self) -> None:
        import duckdb as real_duckdb

        conn = real_duckdb.connect()
        conn.execute("CREATE TABLE t(a INTEGER, b VARCHAR)")
        conn.execute("INSERT INTO t VALUES (1, 'x')")
        res = run_query(conn, "SELECT a, b FROM t")
        assert res["status"] == "success"
        assert res["rows"] == [{"a": 1, "b": "x"}]
        assert res["columns"] == ["a", "b"]
        conn.close()

    def test_run_query_on_empty_result(self) -> None:
        import duckdb as real_duckdb

        conn = real_duckdb.connect()
        conn.execute("CREATE TABLE t(a INTEGER)")
        res = run_query(conn, "SELECT a FROM t")
        assert res["status"] == "success"
        assert res["row_count"] == 0
        conn.close()

    def test_explain_query_reports_invalid(self) -> None:
        import duckdb as real_duckdb

        conn = real_duckdb.connect()
        assert explain_query(conn, "SELECT FROM WHERE")["valid"] is False
        conn.close()

    def test_build_schema_without_meta(self) -> None:
        import duckdb as real_duckdb

        conn = real_duckdb.connect()
        conn.execute("CREATE SCHEMA s")
        conn.execute("CREATE TABLE s.t(a INTEGER, b VARCHAR)")
        res = build_schema(conn, "s", ["t"], lambda _schema: {})
        assert res["tables"]["t"]["columns"]["a"]["type"] == "INTEGER"
        assert res["tables"]["t"]["comment"] is None
        conn.close()

    def test_build_schema_prefers_pg_type_from_meta(self) -> None:
        import duckdb as real_duckdb

        conn = real_duckdb.connect()
        conn.execute("CREATE SCHEMA s")
        conn.execute("CREATE TABLE s.t(a VARCHAR)")
        res = build_schema(
            conn,
            "s",
            ["t"],
            lambda _schema: {("t", "a"): ("комментарий", "character varying(500)")},
        )
        column = res["tables"]["t"]["columns"]["a"]
        assert column["type"] == "character varying(500)"
        assert column["comment"] == "комментарий"
        conn.close()

    def test_build_schema_strips_empty_table_list(self) -> None:
        import duckdb as real_duckdb

        conn = real_duckdb.connect()
        conn.execute("CREATE SCHEMA s")
        res = build_schema(conn, "s", [], lambda _schema: {})
        assert res["tables"] == {}
        conn.close()
