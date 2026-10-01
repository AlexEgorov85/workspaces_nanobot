"""Писатель снимка: ``ensure_schema`` / ``upsert_records`` / ``replace_records``.

Порт ``tests/test_duckdb_cache_store.py`` (классы ``TestUpsert``, ``TestSchema``,
``TestReplace``, ``test_map_pg_type``, счётчики ``TestStats``) на платформенные
примитивы. Миграция ``enterprise-mcp-platform``, фаза 5 пункт 5.1; удаление
агентских тестов — фазы 5.10/9.

Фикстуры локальные для модуля: ``conftest.py`` платформы общий и правится не
здесь, а снимок — это не PostgreSQL, ему нужен свой файл.

Покрытие мусорных входов вынесено в ``test_snapshot_writer_garbage.py``: этот
модуль проверяет поведение на корректных данных, тот — что оно громкое.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from libs.enterprise_data.snapshot import (
    CacheAccessMode,
    DuckDbSnapshotStore,
    open_snapshot_store,
)
from libs.enterprise_data.snapshot.writer import (
    infer_duckdb_type,
    map_pg_type,
    resolve_column_specs,
)

SCHEMA = "oarb"
TABLE = f"{SCHEMA}.audits"
SCRIPT_TABLE = f"{SCHEMA}.script_registry"
KEYLESS_TABLE = f"{SCHEMA}.keyless"

_COLS = [
    {"name": "__table__", "type": "", "not_null": False, "comment": "Аудиторские проверки"},
    {"name": "id", "type": "integer", "not_null": True, "comment": "Идентификатор"},
    {
        "name": "title",
        "type": "character varying(500)",
        "not_null": False,
        "comment": "Название проверки",
    },
    {"name": "amount", "type": "numeric(10,2)", "not_null": False, "comment": None},
    {"name": "checked_on", "type": "date", "not_null": False, "comment": None},
]


@pytest.fixture
def store(tmp_path: Path):
    """Хранилище, открытое на запись — стадия загрузки снимка."""
    instance = open_snapshot_store(
        str(tmp_path / "cache.duckdb"),
        CacheAccessMode.READ_WRITE,
        schema=SCHEMA,
    )
    try:
        yield instance
    finally:
        instance.close()


def _rows(store: DuckDbSnapshotStore, sql: str) -> list[dict]:
    result = store.query_sql(sql)
    assert result["status"] == "success", result.get("error")
    return result["rows"]


# ---------------------------------------------------------------------------
# Приём данных
# ---------------------------------------------------------------------------


class TestUpsert:
    def test_creates_table_and_inserts(self, store: DuckDbSnapshotStore) -> None:
        assert store.upsert_records(
            TABLE,
            [
                {"id": 1, "title": "Проверка А", "status": "open"},
                {"id": 2, "title": "Проверка Б", "status": "closed"},
            ],
        )
        rows = _rows(store, f"SELECT * FROM {TABLE} ORDER BY id")
        assert [row["id"] for row in rows] == [1, 2]

    def test_upsert_by_key_updates_not_duplicates(self, store: DuckDbSnapshotStore) -> None:
        store.upsert_records(
            TABLE,
            [
                {"id": 1, "title": "Старый", "status": "open"},
                {"id": 2, "title": "Б", "status": "closed"},
            ],
        )
        store.upsert_records(
            TABLE,
            [
                {"id": 1, "title": "Новый", "status": "open"},
                {"id": 3, "title": "В", "status": "pending"},
            ],
        )
        rows = _rows(store, f"SELECT id, title FROM {TABLE} ORDER BY id")
        assert [row["id"] for row in rows] == [1, 2, 3]
        assert rows[0]["title"] == "Новый"

    def test_new_column_added_with_inferred_type(self, store: DuckDbSnapshotStore) -> None:
        store.upsert_records(TABLE, [{"id": 1, "title": "А", "status": "open"}])
        store.upsert_records(TABLE, [{"id": 2, "title": "Б", "status": "closed", "amount": 100}])
        rows = _rows(store, f"SELECT id, amount FROM {TABLE} WHERE id = 2")
        assert rows[0]["amount"] == 100

    def test_upsert_with_explicit_key_column(self, store: DuckDbSnapshotStore) -> None:
        """Явный ``key_column`` MUST использоваться вместо дефолта ``id``.

        Таблицы вроде ``public.agent_predefined_scripts`` имеют PK ``name``,
        а не ``id``. Без явного ключа store уходит в CREATE OR REPLACE,
        что для дельты удаляет несвязанные строки.
        """
        assert store.upsert_records(
            SCRIPT_TABLE,
            [{"name": "a", "sql": "SELECT 1"}, {"name": "b", "sql": "SELECT 2"}],
            key_column="name",
        )
        assert store.upsert_records(
            SCRIPT_TABLE, [{"name": "a", "sql": "SELECT 3"}], key_column="name"
        )
        rows = _rows(store, f"SELECT name, sql FROM {SCRIPT_TABLE} ORDER BY name")
        assert [row["name"] for row in rows] == ["a", "b"]
        assert rows[0]["sql"] == "SELECT 3"

    def test_upsert_without_key_recreates_table(self, store: DuckDbSnapshotStore) -> None:
        """Без ключа и без ``id`` — таблица пересоздаётся (documented fallback)."""
        assert store.upsert_records(KEYLESS_TABLE, [{"x": 1}, {"x": 2}])
        assert store.upsert_records(KEYLESS_TABLE, [{"x": 9}])
        rows = _rows(store, f"SELECT x FROM {KEYLESS_TABLE}")
        assert [row["x"] for row in rows] == [9]

    def test_empty_batch_is_noop(self, store: DuckDbSnapshotStore) -> None:
        assert store.upsert_records(TABLE, []) is True
        # таблица не создана — запрос падает с ошибкой, не с исключением
        result = store.query_sql(f"SELECT COUNT(*) AS c FROM {TABLE}")
        assert result["status"] == "error"

    def test_first_batch_needs_no_key_warning(
        self, store: DuckDbSnapshotStore, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Первый батч создаёт таблицу — это не «потерянный ключ».

        Таблицы в снимке создаёт ``ensure_schema``; предупреждение о
        пересоздании на пустом месте только мешало бы читать лог.
        """
        with caplog.at_level("WARNING", logger="libs.enterprise_data.snapshot.store"):
            assert store.upsert_records(TABLE, [{"id": 1, "title": "А"}])
        assert "нет ключа upsert" not in caplog.text

    def test_nested_values_survive_arrow_path(self, store: DuckDbSnapshotStore) -> None:
        """Эмбеддинг (``list[float]``) должен лечь в колонку списком, а не строкой."""
        assert store.upsert_records(
            f"{SCHEMA}.embeddings",
            [{"id": 1, "source": "idx_a", "embedding": [0.1, 0.2, 0.3]}],
        )
        rows = _rows(store, f"SELECT embedding FROM {SCHEMA}.embeddings")
        assert [float(v) for v in rows[0]["embedding"]] == pytest.approx([0.1, 0.2, 0.3])


# ---------------------------------------------------------------------------
# Схема
# ---------------------------------------------------------------------------


class TestSchema:
    def test_ensure_schema_creates_table_with_pg_types(self, store: DuckDbSnapshotStore) -> None:
        assert store.ensure_schema(TABLE, _COLS)
        columns = store.get_schema()["tables"]["audits"]["columns"]
        # исходные PG-типы сохранены в __schema_meta и возвращаются в get_schema
        assert columns["id"]["type"] == "integer"
        assert columns["title"]["type"] == "character varying(500)"
        assert columns["amount"]["type"] == "numeric(10,2)"
        assert columns["checked_on"]["type"] == "date"
        # псевдоколонка __table__ не попадает в реальную структуру
        assert "__table__" not in columns

    def test_ensure_schema_returns_comments(self, store: DuckDbSnapshotStore) -> None:
        store.ensure_schema(TABLE, _COLS)
        table = store.get_schema()["tables"]["audits"]
        assert table["comment"] == "Аудиторские проверки"
        assert table["columns"]["id"]["comment"] == "Идентификатор"
        assert table["columns"]["title"]["comment"] == "Название проверки"
        assert table["columns"]["amount"]["comment"] is None

    def test_empty_table_created_via_schema(self, store: DuckDbSnapshotStore) -> None:
        store.ensure_schema(TABLE, _COLS)
        assert _rows(store, f"SELECT COUNT(*) AS n FROM {TABLE}")[0]["n"] == 0

    def test_ensure_schema_adds_new_columns(self, store: DuckDbSnapshotStore) -> None:
        store.upsert_records(TABLE, [{"id": 1, "title": "А", "status": "open"}])
        store.ensure_schema(
            TABLE,
            [
                {"name": "id", "type": "integer", "not_null": False, "comment": None},
                {"name": "title", "type": "character varying(500)", "not_null": False, "comment": None},
                {"name": "status", "type": "character varying(50)", "not_null": False, "comment": None},
                {"name": "new_col", "type": "bigint", "not_null": False, "comment": "Новая колонка"},
            ],
        )
        columns = store.get_schema()["tables"]["audits"]["columns"]
        assert columns["new_col"]["type"] == "bigint"
        assert columns["new_col"]["comment"] == "Новая колонка"
        # старые данные на месте
        assert _rows(store, f"SELECT title FROM {TABLE} WHERE id = 1")[0]["title"] == "А"

    def test_upsert_after_ensure_schema_preserves_types(self, store: DuckDbSnapshotStore) -> None:
        store.ensure_schema(TABLE, _COLS)
        assert store.upsert_records(
            TABLE,
            [{"id": 1, "title": "П1", "amount": 123.45, "checked_on": "2024-05-21"}],
        )
        assert str(_rows(store, f"SELECT amount FROM {TABLE}")[0]["amount"]) == "123.45"
        assert (
            store.get_schema()["tables"]["audits"]["columns"]["amount"]["type"] == "numeric(10,2)"
        )

    def test_ensure_schema_from_row_batch_infers_types(
        self, store: DuckDbSnapshotStore
    ) -> None:
        """Второе прочтение контракта: батч строк вместо описания колонок.

        Типы выводятся по значениям — «привести схему в соответствие с батчем».
        """
        assert store.ensure_schema(TABLE, [{"id": 1, "title": "П1", "flag": True}])
        columns = store.get_schema()["tables"]["audits"]["columns"]
        assert set(columns) == {"id", "title", "flag"}
        assert columns["flag"]["type"] == "BOOLEAN"

    def test_ensure_schema_applies_schema_meta(self, store: DuckDbSnapshotStore) -> None:
        """``schema_meta`` дописывает комментарий и колонку к описанию."""
        assert store.ensure_schema(
            TABLE,
            _COLS,
            {("oarb.audits", "amount"): ("Сумма штрафа", "numeric(12,4)")},
        )
        columns = store.get_schema()["tables"]["audits"]["columns"]
        assert columns["amount"]["comment"] == "Сумма штрафа"
        assert columns["amount"]["type"] == "numeric(12,4)"

    def test_ensure_schema_meta_can_add_column(self, store: DuckDbSnapshotStore) -> None:
        assert store.ensure_schema(
            TABLE,
            [{"name": "id", "type": "integer", "not_null": True, "comment": None}],
            {("oarb.audits", "weight"): ("Вес", "double precision")},
        )
        columns = store.get_schema()["tables"]["audits"]["columns"]
        assert columns["weight"]["type"] == "double precision"
        assert columns["weight"]["comment"] == "Вес"

    def test_empty_description_is_noop(self, store: DuckDbSnapshotStore) -> None:
        assert store.ensure_schema(TABLE, []) is True
        assert store.get_schema()["tables"] == {}


# ---------------------------------------------------------------------------
# Полная замена содержимого
# ---------------------------------------------------------------------------


class TestReplace:
    def test_replace_reconciles_deletions(self, store: DuckDbSnapshotStore) -> None:
        store.upsert_records(
            TABLE,
            [
                {"id": 1, "title": "А", "status": "open"},
                {"id": 2, "title": "Б", "status": "closed"},
            ],
        )
        assert store.replace_records(TABLE, [{"id": 1, "title": "А", "status": "open"}])
        assert [row["id"] for row in _rows(store, f"SELECT id FROM {TABLE} ORDER BY id")] == [1]

    def test_replace_empty_keeps_schema(self, store: DuckDbSnapshotStore) -> None:
        store.upsert_records(TABLE, [{"id": 1, "title": "А", "status": "open"}])
        assert store.replace_records(TABLE, [])
        assert _rows(store, f"SELECT COUNT(*) AS n FROM {TABLE}")[0]["n"] == 0
        assert "audits" in store.get_schema()["tables"]

    def test_replace_preserves_typed_schema(self, store: DuckDbSnapshotStore) -> None:
        store.ensure_schema(TABLE, _COLS)
        store.upsert_records(
            TABLE,
            [
                {"id": 1, "title": "П1", "amount": 1.5, "checked_on": "2024-05-21"},
                {"id": 2, "title": "П2", "amount": 2.5, "checked_on": "2024-06-24"},
            ],
        )
        store.replace_records(
            TABLE, [{"id": 2, "title": "П2", "amount": 2.5, "checked_on": "2024-06-24"}]
        )
        columns = store.get_schema()["tables"]["audits"]["columns"]
        assert columns["amount"]["type"] == "numeric(10,2)"
        assert [row["id"] for row in _rows(store, f"SELECT id FROM {TABLE}")] == [2]

    def test_replace_creates_table_from_batch(self, store: DuckDbSnapshotStore) -> None:
        """Таблицы, которой в снимке не было, замена создаёт из батча."""
        assert store.replace_records(KEYLESS_TABLE, [{"x": 1}, {"x": 2}])
        assert sorted(row["x"] for row in _rows(store, f"SELECT x FROM {KEYLESS_TABLE}")) == [1, 2]

    def test_replace_is_transactional(self, store: DuckDbSnapshotStore) -> None:
        """Упавший INSERT не должен оставить таблицу пустой.

        Путь без ``pyarrow`` — единственный способ уронить вставку на
        скалярной колонке: список туда не биндится.
        """
        store.ensure_schema(TABLE, _COLS)
        store.upsert_records(TABLE, [{"id": 1, "title": "А", "amount": 1.0}])
        assert store.replace_records(
            TABLE, [{"id": 2, "title": "Б", "amount": [1.0, 2.0], "checked_on": "2024-01-01"}]
        ) is False
        assert [row["id"] for row in _rows(store, f"SELECT id FROM {TABLE}")] == [1]


# ---------------------------------------------------------------------------
# Запись без pyarrow (деградация манифеста)
# ---------------------------------------------------------------------------


class TestWriterWithoutPyarrow:
    """Манифест платформы объявляет ``duckdb``, но не ``pyarrow`` (задача 5.14).

    Поэтому запись обязана работать и без ``pyarrow``: иначе снимок просто
    остался бы пустым, а вызов ``replace_records`` возвращал бы ``False`` на
    каждой таблице.
    """

    @pytest.fixture
    def no_pyarrow(self, monkeypatch: pytest.MonkeyPatch):
        from libs.enterprise_data.snapshot import writer

        def _absent() -> None:
            raise ImportError("pyarrow is not installed")

        monkeypatch.setattr(writer, "arrow_module", _absent)

    def test_replace_works(self, store: DuckDbSnapshotStore, no_pyarrow) -> None:
        assert store.replace_records(TABLE, [{"id": 1, "title": "А"}, {"id": 2, "title": "Б"}])
        assert [row["id"] for row in _rows(store, f"SELECT id FROM {TABLE} ORDER BY id")] == [1, 2]

    def test_upsert_works(self, store: DuckDbSnapshotStore, no_pyarrow) -> None:
        store.replace_records(TABLE, [{"id": 1, "title": "А"}])
        assert store.upsert_records(TABLE, [{"id": 2, "title": "Б"}])
        assert sorted(row["id"] for row in _rows(store, f"SELECT id FROM {TABLE}")) == [1, 2]

    def test_upsert_without_key_recreates_table(
        self, store: DuckDbSnapshotStore, no_pyarrow
    ) -> None:
        assert store.upsert_records(KEYLESS_TABLE, [{"x": 1}, {"x": 2}])
        assert store.upsert_records(KEYLESS_TABLE, [{"x": 9}])
        assert [row["x"] for row in _rows(store, f"SELECT x FROM {KEYLESS_TABLE}")] == [9]

    def test_nested_value_fails_loudly(self, store: DuckDbSnapshotStore, no_pyarrow) -> None:
        """Граница деградации: вложенный список не переносится, но и не теряется молча."""
        assert store.upsert_records(
            f"{SCHEMA}.embeddings", [{"id": 1, "embedding": [0.1, 0.2]}]
        ) is False
        assert "upsert" in store.get_stats()["last_error"]


# ---------------------------------------------------------------------------
# Маппинг типов
# ---------------------------------------------------------------------------


def test_map_pg_type() -> None:
    assert map_pg_type("integer") == "INTEGER"
    assert map_pg_type("bigint") == "BIGINT"
    assert map_pg_type("boolean") == "BOOLEAN"
    assert map_pg_type("double precision") == "DOUBLE"
    assert map_pg_type("text") == "VARCHAR"
    assert map_pg_type("character varying") == "VARCHAR"
    assert map_pg_type("character varying(500)") == "character varying(500)"
    assert map_pg_type("numeric(10,2)") == "DECIMAL(10,2)"
    assert map_pg_type("numeric") == "DOUBLE"
    assert map_pg_type("timestamp with time zone") == "TIMESTAMPTZ"
    assert map_pg_type("jsonb") == "JSON"
    assert map_pg_type("uuid") == "UUID"
    assert map_pg_type("something_exotic") == "VARCHAR"
    assert map_pg_type("") == "VARCHAR"
    assert map_pg_type("text[]") == "VARCHAR"


def test_infer_duckdb_type() -> None:
    assert infer_duckdb_type([None, None]) == "VARCHAR"
    assert infer_duckdb_type([True, False]) == "BOOLEAN"
    assert infer_duckdb_type([1, 2]) == "BIGINT"
    assert infer_duckdb_type([1.5, 2.0]) == "DOUBLE"
    assert infer_duckdb_type([{"a": 1}]) == "JSON"
    assert infer_duckdb_type([1, "x"]) == "VARCHAR"
    assert infer_duckdb_type([True, 1]) == "VARCHAR"


def test_resolve_column_specs_detects_shapes() -> None:
    assert resolve_column_specs(_COLS)[1] == {
        "name": "id",
        "type": "integer",
        "not_null": True,
        "comment": "Идентификатор",
    }
    assert resolve_column_specs([{"name": "x"}]) == [
        {"name": "name", "type": "VARCHAR", "not_null": False, "comment": None}
    ]
    assert resolve_column_specs([]) == []


# ---------------------------------------------------------------------------
# Статистика
# ---------------------------------------------------------------------------


class TestStats:
    def test_write_counters(self, store: DuckDbSnapshotStore) -> None:
        store.upsert_records(TABLE, [{"id": 1, "title": "А"}])
        store.replace_records(TABLE, [{"id": 1, "title": "А"}])
        stats = store.get_stats()
        assert stats["upserts"] == 2
        assert stats["upsert_errors"] == 0
        assert stats["last_upsert_at"]
        assert stats["last_error"] is None
        assert stats["tables"]["audits"]["rows"] == 1

    def test_error_counted(self, store: DuckDbSnapshotStore) -> None:
        assert store.replace_records(f"{SCHEMA}.", [{"id": 1}]) is False
        stats = store.get_stats()
        assert stats["upsert_errors"] == 1
        assert "Некорректное имя таблицы" in stats["last_error"]

    def test_close_clears_state(self, store: DuckDbSnapshotStore) -> None:
        store.upsert_records(TABLE, [{"id": 1, "title": "А"}])
        store.close()
        assert store.is_ready() is False
        assert store.get_stats()["is_ready"] is False
