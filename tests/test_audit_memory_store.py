from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from lib.services.audit_memory_store import AuditMemoryStore

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
        "embedding": [0.1, 0.2, 0.3],
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
        "embedding": [0.4, 0.5, 0.6],
    },
]


@pytest.fixture
def store(tmp_path):
    st = AuditMemoryStore(
        cache_path=str(tmp_path / "audit.duckdb"),
        schema="oarb",
        tables=["audits", "violations"],
        vector_db_table="oarb.audit_vectors",
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
        assert store.upsert_records("oarb.audits", [
            {"id": 1, "title": "Проверка А", "status": "open"},
            {"id": 2, "title": "Проверка Б", "status": "closed"},
        ])
        r = store.query_sql("SELECT * FROM oarb.audits ORDER BY id")
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
        r = store.query_sql("SELECT id, title FROM oarb.audits ORDER BY id")
        assert [row["id"] for row in r["rows"]] == [1, 2, 3]
        assert r["rows"][0]["title"] == "Новый"

    def test_new_column_added_with_inferred_type(self, store):
        store.upsert_records("oarb.audits", [{"id": 1, "title": "А", "status": "open"}])
        store.upsert_records("oarb.audits", [
            {"id": 2, "title": "Б", "status": "closed", "amount": 100},
        ])
        r = store.query_sql("SELECT id, amount FROM oarb.audits WHERE id = 2")
        assert r["rows"][0]["amount"] == 100

    def test_empty_batch_is_noop(self, store):
        assert store.upsert_records("oarb.audits", []) is True
        # таблица не создана — запрос падает с ошибкой, не с исключением
        r = store.query_sql("SELECT COUNT(*) AS c FROM oarb.audits")
        assert r["status"] == "error"


# ---------------------------------------------------------------------------
# SQL-интерфейс
# ---------------------------------------------------------------------------


class TestQuery:
    def test_query_sql_error_returns_status(self, store):
        store.upsert_records("oarb.audits", [{"id": 1, "title": "А", "status": "open"}])
        r = store.query_sql("SELECT * FROM oarb.missing_table")
        assert r["status"] == "error"
        assert r["error"]

    def test_get_schema(self, store):
        store.upsert_records("oarb.audits", [{"id": 1, "title": "А", "status": "open"}])
        s = store.get_schema()
        assert "audits" in s["tables"]
        assert "id" in s["tables"]["audits"]["columns"]

    def test_explain(self, store):
        store.upsert_records("oarb.audits", [{"id": 1, "title": "А", "status": "open"}])
        e = store.explain("SELECT * FROM oarb.audits LIMIT 1")
        assert e["valid"] is True
        assert e["plan"]


# ---------------------------------------------------------------------------
# Векторные индексы
# ---------------------------------------------------------------------------


class TestVector:
    def test_index_built_from_upserts(self, store):
        store.upsert_records("oarb.audit_vectors", _VECTOR_RECORDS)
        idx, meta = store._load_source_index("audits_index")
        assert idx is not None
        assert idx.ntotal == 2
        assert meta["metadata"]["0"]["pk_value"] == 1
        assert meta["metadata"]["1"]["source"] == "audits_index"

    def test_search_without_embedder_returns_empty(self, store):
        store.upsert_records("oarb.audit_vectors", _VECTOR_RECORDS)
        assert store.search_vector("тест", index_name="audits_index") == []

    def test_dirty_marking_invalidates_cache(self, store):
        store.upsert_records("oarb.audit_vectors", _VECTOR_RECORDS)
        assert store.preload_indexes()  # warm cache
        store.upsert_records("oarb.audit_vectors", [dict(_VECTOR_RECORDS[0], embedding=[0.9, 0.9, 0.9])])
        assert "audits_index" in store._dirty_sources
        assert "audits_index" not in store._index_cache

    def test_preload_indexes(self, store):
        store.upsert_records("oarb.audit_vectors", _VECTOR_RECORDS)
        loaded = store.preload_indexes()
        assert [x["index_name"] for x in loaded] == ["audits_index"]
        assert loaded[0]["vectors"] == 2

    def test_non_vector_table_ignored(self, store):
        store.upsert_records("oarb.audits", [{"id": 1, "title": "А", "status": "open"}])
        assert store._dirty_sources == set()
        assert store.preload_indexes() == []


# ---------------------------------------------------------------------------
# Публикация снимка для навыка (CLI)
# ---------------------------------------------------------------------------


class TestPublish:
    def test_publish_creates_readable_snapshot(self, tmp_path):
        target = tmp_path / "out.duckdb"
        store = AuditMemoryStore(
            cache_path="",
            publish_path=str(target),
            schema="oarb",
            tables=["audits", "violations"],
        )
        store.open()
        store.upsert_records("oarb.audits", [{"id": 1, "title": "П1", "status": "open"}])
        assert store.get_stats()["dirty"] is True
        assert store.publish() is True
        assert target.exists()

        import duckdb
        ro = duckdb.connect(str(target), read_only=True)
        rows = ro.execute("SELECT id, title FROM oarb.audits").fetchall()
        ro.close()
        assert rows == [(1, "П1")]
        st = store.get_stats()
        assert st["dirty"] is False
        assert st["publishes"] == 1
        store.close()

    def test_publish_replaces_file_on_update(self, tmp_path):
        target = tmp_path / "out.duckdb"
        store = AuditMemoryStore(
            cache_path="",
            publish_path=str(target),
            schema="oarb",
            tables=["audits"],
        )
        store.open()
        store.upsert_records("oarb.audits", [{"id": 1, "title": "А", "status": "open"}])
        store.publish()
        store.upsert_records("oarb.audits", [{"id": 1, "title": "Б", "status": "open"}])
        store.publish()

        import duckdb
        ro = duckdb.connect(str(target), read_only=True)
        title = ro.execute("SELECT title FROM oarb.audits WHERE id = 1").fetchall()[0][0]
        ro.close()
        assert title == "Б"
        assert store.get_stats()["publishes"] == 2
        store.close()

    def test_publish_noop_when_not_dirty(self, tmp_path):
        target = tmp_path / "out.duckdb"
        store = AuditMemoryStore(cache_path="", publish_path=str(target), schema="oarb")
        store.open()
        assert store.publish() is True
        assert not target.exists()
        store.close()

    def test_publish_skips_missing_tables(self, tmp_path):
        target = tmp_path / "out.duckdb"
        store = AuditMemoryStore(
            cache_path="",
            publish_path=str(target),
            schema="oarb",
            tables=["audits", "violations"],  # violations не заполнена
        )
        store.open()
        store.upsert_records("oarb.audits", [{"id": 1, "title": "А", "status": "open"}])
        assert store.publish() is True
        import duckdb
        ro = duckdb.connect(str(target), read_only=True)
        tables = [r[0] for r in ro.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='oarb'"
        ).fetchall()]
        ro.close()
        assert tables == ["audits"]
        store.close()

    def test_publish_without_publish_path_is_noop(self):
        store = AuditMemoryStore(cache_path="", schema="oarb")
        store.open()
        store.upsert_records("oarb.audits", [{"id": 1, "title": "А", "status": "open"}])
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
        assert store.ensure_schema("oarb.audits", _COLS)
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
        store.ensure_schema("oarb.audits", _COLS)
        t = store.get_schema()["tables"]["audits"]
        assert t["comment"] == "Аудиторские проверки"
        assert t["columns"]["id"]["comment"] == "Идентификатор"
        assert t["columns"]["title"]["comment"] == "Название проверки"
        assert t["columns"]["amount"]["comment"] is None

    def test_empty_table_created_via_schema(self, store):
        store.ensure_schema("oarb.audits", _COLS)
        r = store.query_sql("SELECT COUNT(*) AS n FROM oarb.audits")
        assert r["status"] == "success"
        assert r["rows"][0]["n"] == 0

    def test_ensure_schema_adds_new_columns(self, store):
        store.upsert_records("oarb.audits", [{"id": 1, "title": "А", "status": "open"}])
        store.ensure_schema("oarb.audits", [
            {"name": "id", "type": "integer", "not_null": False, "comment": None},
            {"name": "title", "type": "character varying(500)", "not_null": False, "comment": None},
            {"name": "status", "type": "character varying(50)", "not_null": False, "comment": None},
            {"name": "new_col", "type": "bigint", "not_null": False, "comment": "Новая колонка"},
        ])
        cols = store.get_schema()["tables"]["audits"]["columns"]
        assert cols["new_col"]["type"] == "bigint"
        assert cols["new_col"]["comment"] == "Новая колонка"
        # старые данные на месте
        r = store.query_sql("SELECT title FROM oarb.audits WHERE id = 1")
        assert r["rows"][0]["title"] == "А"

    def test_upsert_after_ensure_schema_preserves_types(self, store):
        store.ensure_schema("oarb.audits", _COLS)
        assert store.upsert_records("oarb.audits", [
            {"id": 1, "title": "П1", "amount": 123.45, "checked_on": "2024-05-21"},
        ])
        r = store.query_sql("SELECT id, amount FROM oarb.audits")
        assert str(r["rows"][0]["amount"]) == "123.45"
        assert store.get_schema()["tables"]["audits"]["columns"]["amount"]["type"] == "numeric(10,2)"


class TestReplace:
    def test_replace_reconciles_deletions(self, store):
        store.upsert_records("oarb.audits", [
            {"id": 1, "title": "А", "status": "open"},
            {"id": 2, "title": "Б", "status": "closed"},
        ])
        assert store.replace_records("oarb.audits", [
            {"id": 1, "title": "А", "status": "open"},
        ])
        r = store.query_sql("SELECT id FROM oarb.audits ORDER BY id")
        assert [row["id"] for row in r["rows"]] == [1]

    def test_replace_empty_keeps_schema(self, store):
        store.upsert_records("oarb.audits", [{"id": 1, "title": "А", "status": "open"}])
        assert store.replace_records("oarb.audits", [])
        r = store.query_sql("SELECT COUNT(*) AS n FROM oarb.audits")
        assert r["rows"][0]["n"] == 0
        assert "audits" in store.get_schema()["tables"]

    def test_replace_preserves_typed_schema(self, store):
        store.ensure_schema("oarb.audits", _COLS)
        store.upsert_records("oarb.audits", [
            {"id": 1, "title": "П1", "amount": 1.5, "checked_on": "2024-05-21"},
            {"id": 2, "title": "П2", "amount": 2.5, "checked_on": "2024-06-24"},
        ])
        store.replace_records("oarb.audits", [
            {"id": 2, "title": "П2", "amount": 2.5, "checked_on": "2024-06-24"},
        ])
        cols = store.get_schema()["tables"]["audits"]["columns"]
        assert cols["amount"]["type"] == "numeric(10,2)"
        r = store.query_sql("SELECT id FROM oarb.audits")
        assert [row["id"] for row in r["rows"]] == [2]

    def test_publish_includes_schema_meta(self, tmp_path):
        target = tmp_path / "out.duckdb"
        st = AuditMemoryStore(
            cache_path="", publish_path=str(target), schema="oarb", tables=["audits"],
        )
        st.open()
        st.ensure_schema("oarb.audits", _COLS)
        st.upsert_records("oarb.audits", [{"id": 1, "title": "П1", "amount": 1.5, "checked_on": "2024-05-21"}])
        assert st.publish() is True

        import duckdb
        ro = duckdb.connect(str(target), read_only=True)
        meta = ro.execute(
            "SELECT column_name, comment FROM __nanobot_meta.__schema_meta "
            "WHERE schema_name = 'oarb' AND table_name = 'audits'"
        ).fetchall()
        ro.close()
        comments = {row[0]: row[1] for row in meta}
        assert comments[None] == "Аудиторские проверки"
        assert comments["id"] == "Идентификатор"
        assert comments["title"] == "Название проверки"
        st.close()


def test_map_pg_type():
    from lib.services.audit_memory_store import _map_pg_type

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
        store.upsert_records("oarb.audits", [{"id": 1, "title": "А", "status": "open"}])
        store.upsert_records("oarb.audit_vectors", _VECTOR_RECORDS)
        st = store.get_stats()
        assert st["is_ready"] is True
        assert st["tables"]["audits"]["rows"] == 1
        assert st["vector_sources"]["audits_index"]["rows"] == 2
        assert st["upserts"] == 2
        assert st["upsert_errors"] == 0
        assert st["last_upsert_at"]

    def test_close_clears_state(self, store):
        store.upsert_records("oarb.audits", [{"id": 1, "title": "А", "status": "open"}])
        store.close()
        assert store.is_ready() is False
        st = store.get_stats()
        assert st["is_ready"] is False
