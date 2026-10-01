"""Писатель снимка на заведомо плохих данных.

Каждый валидатор писателя проверяется дважды: в
``test_snapshot_writer_records.py`` — на корректных данных, здесь — на мусоре.
Тест, который не падает на мусорной фикстуре, не проверяет валидатор.

Что обязано быть громким, а не тихим: битый JSON в строке, несуществующая
таблица, недоступный и занятый файл, неподдерживаемая ФС, ``None`` вместо
списка строк, непроверенное хранилище и запись в снимок на чтение.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from libs.enterprise_data.snapshot import (
    CacheAccessMode,
    UnsupportedFilesystemError,
    open_snapshot_store,
)
from libs.enterprise_data.snapshot.contracts import CacheBusyError, CacheOpenError
from libs.enterprise_data.snapshot.writer import (
    nested_columns,
    resolve_column_specs,
    validate_records,
)

SCHEMA = "oarb"
TABLE = f"{SCHEMA}.audits"
STORAGE = f"{SCHEMA}.agent_embeddings"


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "cache.duckdb"


@pytest.fixture
def store(path: Path):
    instance = open_snapshot_store(
        str(path), CacheAccessMode.READ_WRITE, schema=SCHEMA, vector_db_table=STORAGE
    )
    try:
        yield instance
    finally:
        instance.close()


# ---------------------------------------------------------------------------
# Мусорный вход
# ---------------------------------------------------------------------------


class TestGarbageBatch:
    @pytest.mark.parametrize(
        "records",
        [
            pytest.param(None, id="none-instead-of-list"),
            pytest.param("oops", id="string-instead-of-list"),
            pytest.param({"id": 1}, id="dict-instead-of-list"),
            pytest.param([1, 2, 3], id="list-of-numbers"),
            pytest.param([["id", 1]], id="list-of-lists"),
            pytest.param([None], id="list-with-none"),
        ],
    )
    def test_upsert_refuses_and_says_why(self, store, records) -> None:
        assert store.upsert_records(TABLE, records) is False
        assert "Ожидался список словарей" in store.get_stats()["last_error"]

    def test_replace_refuses_none(self, store) -> None:
        assert store.replace_records(TABLE, None) is False
        assert store.get_stats()["upsert_errors"] == 1

    def test_ensure_schema_refuses_string(self, store) -> None:
        assert store.ensure_schema(TABLE, "id integer") is False
        assert "Ожидался список словарей" in store.get_stats()["last_error"]

    def test_column_specs_without_name_are_rows_not_columns(self, store) -> None:
        """``[{"foo": 1}]`` — это батч строк, а не описание колонок.

        Тихий отказ здесь означал бы пустую таблицу в снимке, а это уже
        вторая копия источника истины.
        """
        assert store.ensure_schema(TABLE, [{"foo": 1}])
        assert "foo" in store.get_schema()["tables"]["audits"]["columns"]

    def test_validate_records_reports_the_type(self) -> None:
        with pytest.raises(ValueError, match="список словарей") as excinfo:
            validate_records(None)
        assert "NoneType" in str(excinfo.value)

    def test_validate_records_names_the_offenders(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            validate_records([{"id": 1}, "строка"], table="oarb.audits")
        message = str(excinfo.value)
        assert "oarb.audits" in message
        assert "str" in message

    def test_resolve_column_specs_rejects_scalars(self) -> None:
        with pytest.raises(ValueError, match="список словарей"):
            resolve_column_specs(["id", "title"])


# ---------------------------------------------------------------------------
# Несуществующая таблица
# ---------------------------------------------------------------------------


class TestMissingTable:
    def test_empty_name_is_refused(self, store) -> None:
        for bad in ("", f"{SCHEMA}."):
            assert store.upsert_records(bad, [{"id": 1}]) is False
            assert "Некорректное имя таблицы" in store.get_stats()["last_error"]

    def test_reading_absent_table_is_error_status_not_exception(self, store) -> None:
        result = store.query_sql(f"SELECT * FROM {SCHEMA}.never_loaded")
        assert result["status"] == "error"
        assert result["rows"] == []

    def test_ensure_schema_without_columns_creates_nothing(self, store) -> None:
        """У источника нет колонок (например, таблица есть, а ``information_schema`` пуст).

        Загрузчик в этом случае не вызывает ``ensure_schema`` вовсе, а при
        прямом вызове структура не выдумывается.
        """
        assert store.ensure_schema(TABLE, []) is True
        assert store.get_schema()["tables"] == {}
        assert store.query_sql(f"SELECT * FROM {TABLE}")["status"] == "error"


# ---------------------------------------------------------------------------
# Битая строка в данных
# ---------------------------------------------------------------------------


class TestBrokenRowData:
    def test_broken_json_row_survives_write_and_reads_as_empty(self, store) -> None:
        """``row_data`` — текст: битый JSON не должен обнулять загрузку таблицы."""
        assert store.replace_records(
            STORAGE,
            [
                {
                    "id": 1,
                    "source": "idx_a",
                    "content": "а",
                    "search_text": "а",
                    "pk_value": "1",
                    "chunk_index": 0,
                    "chunk_count": 1,
                    "row_data": "{не json",
                },
                {
                    "id": 2,
                    "source": "idx_a",
                    "content": "б",
                    "search_text": "б",
                    "pk_value": "2",
                    "chunk_index": 0,
                    "chunk_count": 1,
                    "row_data": '{"id": 2}',
                },
            ],
        )
        assert store.vector_sources() == ["idx_a"]
        assert store.fetch_chunk_payload("idx_a", 1, 0)["row"] == {}
        assert store.fetch_chunk_payload("idx_a", 2, 0)["row"] == {"id": 2}

    def test_heterogeneous_row_values_do_not_break_the_batch(self, store) -> None:
        """Разные типы в одной колонке: приведение к строке, а не отказ батча."""
        assert store.upsert_records(
            TABLE, [{"id": 1, "payload": {"a": 1}}, {"id": 2, "payload": ["x", "y"]}]
        )
        rows = store.query_sql(f"SELECT id FROM {TABLE} ORDER BY id")
        assert [row["id"] for row in rows["rows"]] == [1, 2]


# ---------------------------------------------------------------------------
# Файл: недоступный, занятый, на сети
# ---------------------------------------------------------------------------


class TestUnusableFile:
    def test_garbage_file_fails_to_open(self, path: Path) -> None:
        path.write_bytes("это не база duckdb".encode() * 16)
        with pytest.raises(CacheOpenError):
            open_snapshot_store(str(path), CacheAccessMode.READ_WRITE, schema=SCHEMA)

    def test_writer_on_garbage_file_returns_false(self, path: Path) -> None:
        """``verify=False`` не даёт «полуготовому» писателю появиться.

        Вызов записи обязан упасть локально, с текстом про открытие, а не
        записать что-то в файл, который DuckDB не понимает.
        """
        path.write_bytes(b"not a duckdb file")
        store = open_snapshot_store(
            str(path), CacheAccessMode.READ_WRITE, schema=SCHEMA, verify=False
        )
        try:
            assert store.is_ready() is False
            assert store.upsert_records(TABLE, [{"id": 1}]) is False
            assert "не проверено" in store.get_stats()["last_error"]
        finally:
            store.close()

    def test_unverified_store_refuses_every_write(self, path: Path) -> None:
        store = open_snapshot_store(
            str(path), CacheAccessMode.READ_WRITE, schema=SCHEMA, verify=False
        )
        try:
            assert store.replace_records(TABLE, [{"id": 1}]) is False
            assert store.ensure_schema(TABLE, [{"name": "id", "type": "integer"}]) is False
            assert store.get_stats()["upserts"] == 0
        finally:
            store.close()

    def test_busy_file_is_refused(
        self, path: Path, monkeypatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Файл снимка держит другой процесс — второй писатель не проходит.

        Настоящий конфликт блокировок межпроцессный, а DuckDB переиспользует
        один экземпляр на путь внутри процесса, поэтому «занятость» подменяется
        на уровне ``duckdb.connect``: проверяется путь писателя (классификация
        ошибки и отказ), а не классификатор текста — он покрыт в
        ``test_snapshot_no_file_hold.py``.
        """
        import duckdb

        real_connect = duckdb.connect

        def _busy(*args, **kwargs):
            raise RuntimeError("Conflicting lock is held by process with PID 4242")

        monkeypatch.setattr(duckdb, "connect", _busy)
        with pytest.raises(CacheBusyError) as excinfo:
            open_snapshot_store(str(path), CacheAccessMode.READ_WRITE, schema=SCHEMA)
        assert "PID 4242" in str(excinfo.value)

        monkeypatch.setattr(duckdb, "connect", real_connect)
        store = open_snapshot_store(str(path), CacheAccessMode.READ_WRITE, schema=SCHEMA)
        try:
            # Файл захвачен другим процессом уже после открытия хранилища:
            # запись обязана отказать, а не писать в чужой снимок.
            store._conn.close()
            store._conn = None
            monkeypatch.setattr(duckdb, "connect", _busy)
            with caplog.at_level("WARNING", logger="libs.enterprise_data.snapshot.store"):
                assert store.replace_records(TABLE, [{"id": 1}]) is False
            assert "PID 4242" in caplog.text
        finally:
            monkeypatch.setattr(duckdb, "connect", real_connect)
            store.close()

    def test_read_only_store_refuses_write(self, path: Path) -> None:
        writer = open_snapshot_store(str(path), CacheAccessMode.READ_WRITE, schema=SCHEMA)
        try:
            writer.replace_records(TABLE, [{"id": 1, "content": "а", "row_data": "{}"}])
        finally:
            writer.close()
        reader = open_snapshot_store(str(path), CacheAccessMode.READ_ONLY, schema=SCHEMA)
        try:
            assert reader.replace_records(TABLE, [{"id": 2}]) is False
            assert "READ_ONLY" in reader.get_stats()["last_error"]
            assert reader.upsert_records(TABLE, [{"id": 3}]) is False
            assert reader.ensure_schema(TABLE, [{"name": "id", "type": "integer"}]) is False
        finally:
            reader.close()

    def test_network_filesystem_is_refused_before_open(self, path: Path, monkeypatch) -> None:
        """Проверка ФС обязана сработать и на стадии записи.

        Сам классификатор ФС проверяется в ``test_snapshot_no_file_hold.py``
        (там, где есть подставной ``/proc/mounts``); здесь важно, что
        ``READ_WRITE``-открытие не обходит его и не создаёт файл.
        """
        from libs.enterprise_data.snapshot import store as store_module

        def _refuse(p: str) -> None:
            raise UnsupportedFilesystemError(f"cache path {p!r} is on nfs4")

        monkeypatch.setattr(store_module, "reject_unsupported_filesystem", _refuse)
        with pytest.raises(UnsupportedFilesystemError):
            open_snapshot_store(str(path), CacheAccessMode.READ_WRITE, schema=SCHEMA)
        assert not path.exists()


# ---------------------------------------------------------------------------
# Вложенные значения без pyarrow
# ---------------------------------------------------------------------------


class TestNestedValuesOnDegradedPath:
    def test_nested_columns_finds_every_offender(self) -> None:
        assert nested_columns(
            [{"id": 1, "v": [1.0]}, {"id": 2, "v": "ok", "d": {"a": 1}}], ["v", "d"]
        ) == ["d", "v"]

    def test_nested_values_fail_loudly_without_pyarrow(self, store, monkeypatch) -> None:
        """Молчаливый перевод списка в строку — это порча данных, а не деградация."""
        from libs.enterprise_data.snapshot import writer

        def _absent() -> None:
            raise ImportError("no pyarrow")

        monkeypatch.setattr(writer, "arrow_module", _absent)
        assert store.upsert_records(TABLE, [{"id": 1, "embedding": [0.1, 0.2]}]) is False
        assert "pyarrow" in store.get_stats()["last_error"]
        # и таблица не осталась пустой заготовкой после неудачной записи
        assert store.query_sql(f"SELECT * FROM {TABLE}")["status"] == "error"
