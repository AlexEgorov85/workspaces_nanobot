from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.conftest import TEST_TABLE, TEST_TABLE_2, TEST_VECTOR_TABLE

# Single source of truth for the test schema name: derived from
# TEST_VECTOR_TABLE so renaming fixtures propagates automatically.
_test_schema = TEST_VECTOR_TABLE.split(".", 1)[0]
_test_table_q = TEST_TABLE
_test_vector_table_q = TEST_VECTOR_TABLE

_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from lib.services.duckdb_cache_store import DuckDbCacheStore
from lib.services.table_registry import SkillRegistration, VectorResource, table_registry
from lib.services.cache_provider_impl import read_vector_store_table

_DIM = 1024


def _vec(index: int) -> list[float]:
    """Единичный вектор размерности _DIM с 1.0 на позиции index."""
    v = [0.0] * _DIM
    v[index] = 1.0
    return v


_VECTOR_RECORDS = [
    {
        "id": 10,
        "source": "audits_index",
        "content": "Записи о проверке",
        "search_text": "проверка: текст",
        "table": "audits",
        "pk_value": 1,
        "chunk_index": 0,
        "chunk_count": 1,
        "row_data": json.dumps({"id": 1}),
        "embedding": _vec(0),
    },
    {
        "id": 11,
        "source": "audits_index",
        "content": "Вторая запись",
        "search_text": "вторая: текст",
        "table": "audits",
        "pk_value": 2,
        "chunk_index": 0,
        "chunk_count": 1,
        "row_data": json.dumps({"id": 2}),
        "embedding": _vec(1),
    },
]


@pytest.fixture(autouse=True)
def _reset_registry():
    table_registry.clear()
    yield
    table_registry.clear()


@pytest.fixture
def store(tmp_path):
    # Регистрируем vector_db_table как VectorResource, потому что
    # DuckDbCacheStore._mark_vector_sources_dirty теперь делает lookup
    # через table_registry.vector_resources() вместо сравнения имён.
    # Схема test.* соответствует TEST_TABLE/TEST_VECTOR_TABLE из conftest.
    table_registry.register(SkillRegistration(
        name="audit_analyzer",
        resources=(VectorResource(name=TEST_VECTOR_TABLE, tracking_column="id"),),
    ))
    st = DuckDbCacheStore(
        cache_path=str(tmp_path / "audit.duckdb"),
        schema=_test_schema,
        tables=[TEST_TABLE.split(".", 1)[1], TEST_TABLE_2.split(".", 1)[1]],
        vector_db_table=TEST_VECTOR_TABLE,
        embedding_base_url="",
    )
    assert st.open()
    yield st
    st.close()


# ---------------------------------------------------------------------------
# Приём данных
# ---------------------------------------------------------------------------


class TestUpsert:
    def test_creates_table_and_inserts(self, store):
        assert store.upsert_records(TEST_TABLE, [
            {"id": 1, "title": "Проверка А", "status": "open"},
            {"id": 2, "title": "Проверка Б", "status": "closed"},
        ])
        r = store.query_sql(f"SELECT * FROM {TEST_TABLE} ORDER BY id")
        assert r["status"] == "success"
        assert [row["id"] for row in r["rows"]] == [1, 2]

    def test_upsert_by_key_updates_not_duplicates(self, store):
        store.upsert_records("audits", [
            {"id": 1, "title": "Старый", "status": "open"},
            {"id": 2, "title": "Б", "status": "closed"},
        ])
        store.upsert_records("audits", [
            {"id": 1, "title": "Новый", "status": "open"},
            {"id": 3, "title": "В", "status": "pending"},
        ])
        r = store.query_sql(f"SELECT id, title FROM {TEST_TABLE} ORDER BY id")
        assert [row["id"] for row in r["rows"]] == [1, 2, 3]
        assert r["rows"][0]["title"] == "Новый"

    def test_new_column_added_with_inferred_type(self, store):
        store.upsert_records(TEST_TABLE, [{"id": 1, "title": "А", "status": "open"}])
        store.upsert_records(TEST_TABLE, [
            {"id": 2, "title": "Б", "status": "closed", "amount": 100},
        ])
        r = store.query_sql(f"SELECT id, amount FROM {TEST_TABLE} WHERE id = 2")
        assert r["rows"][0]["amount"] == 100

    def test_empty_batch_is_noop(self, store):
        assert store.upsert_records(TEST_TABLE, []) is True
        # таблица не создана — запрос падает с ошибкой, не с исключением
        r = store.query_sql(f"SELECT COUNT(*) AS c FROM {TEST_TABLE}")
        assert r["status"] == "error"


# ---------------------------------------------------------------------------
# SQL-интерфейс
# ---------------------------------------------------------------------------


class TestQuery:
    def test_query_sql_error_returns_status(self, store):
        store.upsert_records(TEST_TABLE, [{"id": 1, "title": "А", "status": "open"}])
        r = store.query_sql("SELECT * FROM test.missing_table")
        assert r["status"] == "error"
        assert r["error"]

    def test_get_schema(self, store):
        store.upsert_records(TEST_TABLE, [{"id": 1, "title": "А", "status": "open"}])
        s = store.get_schema()
        assert "audits" in s["tables"]
        assert "id" in s["tables"]["audits"]["columns"]

    def test_explain(self, store):
        store.upsert_records(TEST_TABLE, [{"id": 1, "title": "А", "status": "open"}])
        e = store.explain(f"SELECT * FROM {TEST_TABLE} LIMIT 1")
        assert e["valid"] is True
        assert e["plan"]


# ---------------------------------------------------------------------------
# Векторные индексы
# ---------------------------------------------------------------------------


class TestVector:
    def test_index_built_from_upserts(self, store):
        store.upsert_records(TEST_VECTOR_TABLE, _VECTOR_RECORDS)
        idx, meta = store._load_source_index("audits_index")
        assert idx is not None
        assert idx.ntotal == 2
        assert meta["metadata"]["0"]["pk_value"] == 1
        assert meta["metadata"]["1"]["source"] == "audits_index"

    def test_search_without_embedder_returns_empty(self, store, monkeypatch):
        import lib.services.cache_provider_impl as cp

        monkeypatch.setattr(cp, "get_embedding", lambda *a, **k: None)
        store.upsert_records(TEST_VECTOR_TABLE, _VECTOR_RECORDS)
        assert store.search_vector("тест", index_name="audits_index") == []

    def test_search_vector_faiss_returns_top_hit(self, store, monkeypatch):
        import lib.services.cache_provider_impl as cp

        monkeypatch.setattr(cp, "get_embedding", lambda *a, **k: _vec(0))
        store.upsert_records(TEST_VECTOR_TABLE, _VECTOR_RECORDS)
        res = store.search_vector("тест", index_name="audits_index", top_k=1)
        assert len(res) == 1
        assert res[0].pk_value == 1
        assert res[0].source == "audits_index"
        assert res[0].score == pytest.approx(1.0)

    def test_search_vector_faiss_threshold_filters(self, store, monkeypatch):
        import lib.services.cache_provider_impl as cp

        monkeypatch.setattr(cp, "get_embedding", lambda *a, **k: _vec(1))
        store.upsert_records(TEST_VECTOR_TABLE, _VECTOR_RECORDS)
        # запись 2 (единичный на позиции 1) совпадает полностью; запись 0 — ортогональна
        res = store.search_vector("тест", index_name="audits_index", threshold=0.5)
        assert len(res) == 1
        assert res[0].pk_value == 2

    def test_dirty_marking_invalidates_cache(self, store):
        store.upsert_records(TEST_VECTOR_TABLE, _VECTOR_RECORDS)
        assert store.preload_indexes()  # warm cache
        store.upsert_records(TEST_VECTOR_TABLE, [dict(_VECTOR_RECORDS[0], embedding=[0.9, 0.9, 0.9])])
        assert "audits_index" in store._dirty_sources
        assert "audits_index" not in store._index_cache

    def test_preload_indexes(self, store):
        store.upsert_records(TEST_VECTOR_TABLE, _VECTOR_RECORDS)
        loaded = store.preload_indexes()
        assert [x["index_name"] for x in loaded] == ["audits_index"]
        assert loaded[0]["vectors"] == 2

    def test_non_vector_table_ignored(self, store):
        store.upsert_records(TEST_TABLE, [{"id": 1, "title": "А", "status": "open"}])
        assert store._dirty_sources == set()
        assert store.preload_indexes() == []


# ---------------------------------------------------------------------------
# Навык: поиск только из опубликованного снимка (без PostgreSQL)
# ---------------------------------------------------------------------------


class TestSkillVectorFromCache:
    def test_publish_includes_vector_table(self, tmp_path):
        target = tmp_path / "out.duckdb"
        store = DuckDbCacheStore(
            cache_path="",
            publish_path=str(target),
            schema=_test_schema,
            tables=["audits"],
            vector_db_table=TEST_VECTOR_TABLE,
        )
        store.open()
        store.upsert_records(TEST_VECTOR_TABLE, _VECTOR_RECORDS)
        assert store.publish() is True
        store.close()

        import duckdb
        ro = duckdb.connect(str(target), read_only=True)
        rows = ro.execute(
            f"SELECT COUNT(*) AS c FROM {TEST_VECTOR_TABLE}"
        ).fetchall()
        ro.close()
        assert rows == [(2,)]

    def test_search_vector_builds_index_from_published_cache(self, tmp_path, monkeypatch):
        import lib.services.cache_provider_impl as cp

        target = tmp_path / "out.duckdb"
        store = DuckDbCacheStore(
            cache_path="",
            publish_path=str(target),
            schema=_test_schema,
            tables=["audits"],
            vector_db_table=TEST_VECTOR_TABLE,
        )
        store.open()
        store.upsert_records(TEST_VECTOR_TABLE, _VECTOR_RECORDS)
        assert store.publish() is True
        store.close()

        provider = cp.PostgresDuckDbProvider(
            schema=_test_schema,
            cache_path=str(target),
            vector_db_table=TEST_VECTOR_TABLE,
            vector_store_table="",  # у навыка нет PG store
            embedding_base_url="",
        )
        monkeypatch.setattr(cp, "get_embedding", lambda *a, **k: _vec(0))
        try:
            res = provider.search_vector("тест", index_name="audits_index", top_k=1)
            assert provider._search_error is None
            assert len(res) == 1
            assert res[0].pk_value == 1
            assert res[0].source == "audits_index"
            assert res[0].score == pytest.approx(1.0)
        finally:
            provider.close()

    def test_search_vector_dimension_mismatch(self, tmp_path, monkeypatch):
        import lib.services.cache_provider_impl as cp

        target = tmp_path / "out.duckdb"
        store = DuckDbCacheStore(
            cache_path="",
            publish_path=str(target),
            schema=_test_schema,
            tables=["audits"],
            vector_db_table=TEST_VECTOR_TABLE,
        )
        store.open()
        store.upsert_records(TEST_VECTOR_TABLE, _VECTOR_RECORDS)
        assert store.publish() is True
        store.close()

        provider = cp.PostgresDuckDbProvider(
            schema=_test_schema,
            cache_path=str(target),
            vector_db_table=TEST_VECTOR_TABLE,
            vector_store_table="",
            embedding_base_url="",
        )
        monkeypatch.setattr(cp, "get_embedding", lambda *a, **k: [0.1, 0.2, 0.3])
        try:
            res = provider.search_vector("тест", index_name="audits_index")
            assert res == []
            assert "Размерность" in (provider._search_error or "")
        finally:
            provider.close()


# ---------------------------------------------------------------------------
# Публикация снимка для навыка (CLI)
# ---------------------------------------------------------------------------


class TestPublish:
    def test_publish_creates_readable_snapshot(self, tmp_path):
        target = tmp_path / "out.duckdb"
        store = DuckDbCacheStore(
            cache_path="",
            publish_path=str(target),
            schema=_test_schema,
            tables=["audits", "violations"],
        )
        store.open()
        store.upsert_records(TEST_TABLE, [{"id": 1, "title": "П1", "status": "open"}])
        assert store.get_stats()["dirty"] is True
        assert store.publish() is True
        assert target.exists()

        import duckdb
        ro = duckdb.connect(str(target), read_only=True)
        rows = ro.execute(f"SELECT id, title FROM {TEST_TABLE}").fetchall()
        ro.close()
        assert rows == [(1, "П1")]
        st = store.get_stats()
        assert st["dirty"] is False
        assert st["publishes"] == 1
        store.close()

    def test_publish_replaces_file_on_update(self, tmp_path):
        target = tmp_path / "out.duckdb"
        store = DuckDbCacheStore(
            cache_path="",
            publish_path=str(target),
            schema=_test_schema,
            tables=["audits"],
        )
        store.open()
        store.upsert_records(TEST_TABLE, [{"id": 1, "title": "А", "status": "open"}])
        store.publish()
        store.upsert_records(TEST_TABLE, [{"id": 1, "title": "Б", "status": "open"}])
        store.publish()

        import duckdb
        ro = duckdb.connect(str(target), read_only=True)
        title = ro.execute(f"SELECT title FROM {TEST_TABLE} WHERE id = 1").fetchall()[0][0]
        ro.close()
        assert title == "Б"
        assert store.get_stats()["publishes"] == 2
        store.close()

    def test_publish_noop_when_not_dirty(self, tmp_path):
        target = tmp_path / "out.duckdb"
        store = DuckDbCacheStore(cache_path="", publish_path=str(target), schema=_test_schema)
        store.open()
        assert store.publish() is True
        assert not target.exists()
        store.close()

    def test_publish_force_recreates_when_not_dirty(self, tmp_path):
        target = tmp_path / "out.duckdb"
        store = DuckDbCacheStore(
            cache_path="",
            publish_path=str(target),
            schema=_test_schema,
            tables=["audits"],
        )
        store.open()
        # нет данных (store не грязный) — обычный publish no-op, force — создаёт снимок
        assert store.publish() is True
        assert not target.exists()
        assert store.publish(force=True) is True
        assert target.exists()
        assert store.get_stats()["dirty"] is False
        store.close()

    def test_publish_force_noop_without_publish_path(self):
        store = DuckDbCacheStore(cache_path="", schema=_test_schema)
        store.open()
        assert store.publish(force=True) is True
        store.close()

    def test_publish_skips_missing_tables(self, tmp_path):
        target = tmp_path / "out.duckdb"
        store = DuckDbCacheStore(
            cache_path="",
            publish_path=str(target),
            schema=_test_schema,
            tables=["audits", "violations"],  # violations не заполнена
        )
        store.open()
        store.upsert_records(TEST_TABLE, [{"id": 1, "title": "А", "status": "open"}])
        assert store.publish() is True
        import duckdb
        ro = duckdb.connect(str(target), read_only=True)
        tables = [r[0] for r in ro.execute(
            f"SELECT table_name FROM information_schema.tables WHERE table_schema='{_test_schema}'"
        ).fetchall()]
        ro.close()
        assert tables == ["audits"]
        store.close()

    def test_publish_without_publish_path_is_noop(self):
        store = DuckDbCacheStore(cache_path="", schema=_test_schema)
        store.open()
        store.upsert_records(TEST_TABLE, [{"id": 1, "title": "А", "status": "open"}])
        assert store.publish() is True
        store.close()


# ---------------------------------------------------------------------------
# Структура из PG (ensure_schema) и сверка удалений (replace_records)
# ---------------------------------------------------------------------------

_COLS = [
    {"name": "__table__", "type": "", "not_null": False, "comment": "Аудиторские проверки"},
    {"name": "id", "type": "integer", "not_null": True, "comment": "Идентификатор"},
    {"name": "title", "type": "character varying(500)", "not_null": False, "comment": "Название проверки"},
    {"name": "amount", "type": "numeric(10,2)", "not_null": False, "comment": None},
    {"name": "checked_on", "type": "date", "not_null": False, "comment": None},
]


class TestSchema:
    def test_ensure_schema_creates_table_with_pg_types(self, store):
        assert store.ensure_schema(TEST_TABLE, _COLS)
        sch = store.get_schema()
        cols = sch["tables"]["audits"]["columns"]
        # исходные PG-типы сохранены в __schema_meta и возвращаются в get_schema
        assert cols["id"]["type"] == "integer"
        assert cols["title"]["type"] == "character varying(500)"
        assert cols["amount"]["type"] == "numeric(10,2)"
        assert cols["checked_on"]["type"] == "date"
        # псевдоколонка __table__ не попадает в реальную структуру
        assert "__table__" not in cols

    def test_ensure_schema_returns_comments(self, store):
        store.ensure_schema(TEST_TABLE, _COLS)
        t = store.get_schema()["tables"]["audits"]
        assert t["comment"] == "Аудиторские проверки"
        assert t["columns"]["id"]["comment"] == "Идентификатор"
        assert t["columns"]["title"]["comment"] == "Название проверки"
        assert t["columns"]["amount"]["comment"] is None

    def test_empty_table_created_via_schema(self, store):
        store.ensure_schema(TEST_TABLE, _COLS)
        r = store.query_sql(f"SELECT COUNT(*) AS n FROM {TEST_TABLE}")
        assert r["status"] == "success"
        assert r["rows"][0]["n"] == 0

    def test_ensure_schema_adds_new_columns(self, store):
        store.upsert_records(TEST_TABLE, [{"id": 1, "title": "А", "status": "open"}])
        store.ensure_schema(TEST_TABLE, [
            {"name": "id", "type": "integer", "not_null": False, "comment": None},
            {"name": "title", "type": "character varying(500)", "not_null": False, "comment": None},
            {"name": "status", "type": "character varying(50)", "not_null": False, "comment": None},
            {"name": "new_col", "type": "bigint", "not_null": False, "comment": "Новая колонка"},
        ])
        cols = store.get_schema()["tables"]["audits"]["columns"]
        assert cols["new_col"]["type"] == "bigint"
        assert cols["new_col"]["comment"] == "Новая колонка"
        # старые данные на месте
        r = store.query_sql(f"SELECT title FROM {TEST_TABLE} WHERE id = 1")
        assert r["rows"][0]["title"] == "А"

    def test_upsert_after_ensure_schema_preserves_types(self, store):
        store.ensure_schema(TEST_TABLE, _COLS)
        assert store.upsert_records(TEST_TABLE, [
            {"id": 1, "title": "П1", "amount": 123.45, "checked_on": "2024-05-21"},
        ])
        r = store.query_sql(f"SELECT id, amount FROM {TEST_TABLE}")
        assert str(r["rows"][0]["amount"]) == "123.45"
        assert store.get_schema()["tables"]["audits"]["columns"]["amount"]["type"] == "numeric(10,2)"


class TestReplace:
    def test_replace_reconciles_deletions(self, store):
        store.upsert_records(TEST_TABLE, [
            {"id": 1, "title": "А", "status": "open"},
            {"id": 2, "title": "Б", "status": "closed"},
        ])
        assert store.replace_records(TEST_TABLE, [
            {"id": 1, "title": "А", "status": "open"},
        ])
        r = store.query_sql(f"SELECT id FROM {TEST_TABLE} ORDER BY id")
        assert [row["id"] for row in r["rows"]] == [1]

    def test_replace_empty_keeps_schema(self, store):
        store.upsert_records(TEST_TABLE, [{"id": 1, "title": "А", "status": "open"}])
        assert store.replace_records(TEST_TABLE, [])
        r = store.query_sql(f"SELECT COUNT(*) AS n FROM {TEST_TABLE}")
        assert r["rows"][0]["n"] == 0
        assert "audits" in store.get_schema()["tables"]

    def test_replace_preserves_typed_schema(self, store):
        store.ensure_schema(TEST_TABLE, _COLS)
        store.upsert_records(TEST_TABLE, [
            {"id": 1, "title": "П1", "amount": 1.5, "checked_on": "2024-05-21"},
            {"id": 2, "title": "П2", "amount": 2.5, "checked_on": "2024-06-24"},
        ])
        store.replace_records(TEST_TABLE, [
            {"id": 2, "title": "П2", "amount": 2.5, "checked_on": "2024-06-24"},
        ])
        cols = store.get_schema()["tables"]["audits"]["columns"]
        assert cols["amount"]["type"] == "numeric(10,2)"
        r = store.query_sql(f"SELECT id FROM {TEST_TABLE}")
        assert [row["id"] for row in r["rows"]] == [2]

    def test_publish_includes_schema_meta(self, tmp_path):
        target = tmp_path / "out.duckdb"
        st = DuckDbCacheStore(
            cache_path="", publish_path=str(target), schema=_test_schema, tables=["audits"],
        )
        st.open()
        st.ensure_schema(TEST_TABLE, _COLS)
        st.upsert_records(TEST_TABLE, [{"id": 1, "title": "П1", "amount": 1.5, "checked_on": "2024-05-21"}])
        assert st.publish() is True

        import duckdb
        ro = duckdb.connect(str(target), read_only=True)
        meta = ro.execute(
            "SELECT column_name, comment FROM __nanobot_meta.__schema_meta "
            f"WHERE schema_name = '{_test_schema}' AND table_name = '{TEST_TABLE.split('.', 1)[1]}'"
        ).fetchall()
        ro.close()
        comments = {row[0]: row[1] for row in meta}
        assert comments[None] == "Аудиторские проверки"
        assert comments["id"] == "Идентификатор"
        assert comments["title"] == "Название проверки"
        st.close()


def test_map_pg_type():
    from lib.services.duckdb_cache_store import _map_pg_type

    assert _map_pg_type("integer") == "INTEGER"
    assert _map_pg_type("bigint") == "BIGINT"
    assert _map_pg_type("boolean") == "BOOLEAN"
    assert _map_pg_type("double precision") == "DOUBLE"
    assert _map_pg_type("text") == "VARCHAR"
    assert _map_pg_type("character varying") == "VARCHAR"
    assert _map_pg_type("character varying(500)") == "character varying(500)"
    assert _map_pg_type("numeric(10,2)") == "DECIMAL(10,2)"
    assert _map_pg_type("numeric") == "DOUBLE"
    assert _map_pg_type("timestamp with time zone") == "TIMESTAMPTZ"
    assert _map_pg_type("jsonb") == "JSON"
    assert _map_pg_type("uuid") == "UUID"
    assert _map_pg_type("something_exotic") == "VARCHAR"
    assert _map_pg_type("") == "VARCHAR"


# ---------------------------------------------------------------------------
# Статистика
# ---------------------------------------------------------------------------


class TestStats:
    def test_stats(self, store):
        store.upsert_records(TEST_TABLE, [{"id": 1, "title": "А", "status": "open"}])
        store.upsert_records(TEST_VECTOR_TABLE, _VECTOR_RECORDS)
        st = store.get_stats()
        assert st["is_ready"] is True
        assert st["tables"]["audits"]["rows"] == 1
        assert st["vector_sources"]["audits_index"]["rows"] == 2
        assert st["upserts"] == 2
        assert st["upsert_errors"] == 0
        assert st["last_upsert_at"]

    def test_close_clears_state(self, store):
        store.upsert_records(TEST_TABLE, [{"id": 1, "title": "А", "status": "open"}])
        store.close()
        assert store.is_ready() is False
        st = store.get_stats()
        assert st["is_ready"] is False


# ---------------------------------------------------------------------------
# execute_readonly — точка интеграции DuckdbQueryTool
# ---------------------------------------------------------------------------


class TestExecuteReadonly:
    def test_readonly_returns_rows(self, store):
        store.upsert_records(TEST_TABLE, [
            {"id": 1, "title": "А", "status": "open"},
            {"id": 2, "title": "Б", "status": "closed"},
        ])
        res = store.execute_readonly(f"SELECT id, title FROM {TEST_TABLE} ORDER BY id")
        assert "error" not in res
        assert res["columns"] == ["id", "title"]
        assert [r[0] for r in res["rows"]] == [1, 2]

    def test_readonly_propagates_error(self, store):
        res = store.execute_readonly("SELECT * FROM test.missing_table")
        assert "error" in res
        assert res["error"]

    def test_readonly_not_ready(self):
        st = DuckDbCacheStore(cache_path="")
        res = st.execute_readonly("SELECT 1")
        assert res == {"error": "DuckDbCacheStore is not ready"}


# ---------------------------------------------------------------------------
# _check_index_integrity — signature enforcement (P0)
# ---------------------------------------------------------------------------


def _fake_cfg_module(monkeypatch, cfg, emb, meta_rows):
    """Подменить чтение PG-signature и конфигов индекса для DuckDbCacheStore."""
    import lib.services.cache_provider_impl as impl

    _ws = str(Path(__file__).resolve().parent.parent / "workspace")
    if _ws not in sys.path:
        sys.path.insert(0, _ws)
    import utils.db as dbmod

    def _read_vector_index_config(_cfg):
        return cfg

    def _read_embedding_config():
        return emb

    def _fetch(sql, *args):
        return meta_rows

    monkeypatch.setattr(impl, "read_vector_index_config", _read_vector_index_config)
    monkeypatch.setattr(impl, "read_embedding_config", _read_embedding_config)
    monkeypatch.setattr(dbmod, "fetch", _fetch)


class TestIndexIntegrity:
    def test_skips_without_vector_store_table(self):
        st = DuckDbCacheStore(cache_path="", vector_store_table="")
        # не падает и не стучится в БД
        st._check_index_integrity("audits_index")

    def test_stale_raises(self, monkeypatch):
        from lib.services.cache_provider import IndexIntegrityError

        cfg = {"audits_index": {
            "table": "audits", "pk": "id",
            "content_columns": ["content"], "embedding_columns": {"c": "col"},
            "track_column": "updated_at",
        }}
        emb = {"model": "mxbai-embed-large:latest", "dimension": 1024}
        # заведомо несовпадающая 64-символьная hex-сигнатура
        meta_rows = [{"metadata": {"signature": "0" * 64}}]

        st = DuckDbCacheStore(cache_path="", vector_store_table=read_vector_store_table())
        _fake_cfg_module(monkeypatch, cfg, emb, meta_rows)
        with pytest.raises(IndexIntegrityError) as exc:
            st._check_index_integrity("audits_index")
        assert exc.value.status == "STALE"

    def test_current_signature_ok(self, monkeypatch):
        from lib.services.cache_provider_impl import compute_index_signature

        cfg_data = {
            "table": "audits", "pk": "id",
            "content_columns": ["content"], "embedding_columns": {"c": "col"},
            "track_column": "updated_at",
        }
        cfg = {"audits_index": cfg_data}
        emb = {"model": "mxbai-embed-large:latest", "dimension": 1024}
        current_cfg = {
            "src_table": cfg_data["table"],
            "pk_column": cfg_data["pk"],
            "content_cols": cfg_data["content_columns"],
            "embedding_cols": cfg_data["embedding_columns"],
            "track_column": cfg_data["track_column"],
            "embedding_model": emb["model"],
            "embedding_dimension": emb["dimension"],
        }
        sig = compute_index_signature(current_cfg)
        meta_rows = [{"metadata": {"signature": sig}}]

        st = DuckDbCacheStore(cache_path="", vector_store_table=read_vector_store_table())
        _fake_cfg_module(monkeypatch, cfg, emb, meta_rows)
        # CURRENT → не бросает
        st._check_index_integrity("audits_index")

    def test_invalid_legacy_no_signature_skips(self, monkeypatch):
        cfg = {"audits_index": {
            "table": "audits", "pk": "id",
            "content_columns": ["content"], "embedding_columns": {"c": "col"},
            "track_column": "updated_at",
        }}
        emb = {"model": "mxbai-embed-large:latest", "dimension": 1024}
        # legacy-индекс без signature в metadata → проверка пропускается
        meta_rows = [{"metadata": {}}]

        st = DuckDbCacheStore(cache_path="", vector_store_table=read_vector_store_table())
        _fake_cfg_module(monkeypatch, cfg, emb, meta_rows)
        st._check_index_integrity("audits_index")
