"""Загрузчик снимка: разовая синхронная загрузка из PostgreSQL.

Порт ``tests/test_cache_load_service.py`` на платформенные примитивы. Миграция
``enterprise-mcp-platform``, фаза 5 пункт 5.2; удаление агентских тестов — фазы
5.10/9.

Что перенесено без изменений: выбор track-колонки (состав снимка → дефолт →
векторная таблица), разбор схемы, различение «таблицы нет» и «соединение
потеряно», и главное — время актуальности снимка в результате и в событии.
Покрытие снятой машинерии (инкрементальный опрос, колбэки, периодический
full-resync) удалено вместе с ней.

Отличие от агентского теста: пул не подменяется через ``monkeypatch`` глобалов
``utils.db``, а **внедряется** в загрузчик параметром ``pool`` — загрузчик не
владеет пулом и не должен знать, как он устроен.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import psycopg2
import psycopg2.errors
import pytest

from libs.enterprise_data.loader import (
    SnapshotLoadError,
    SnapshotLoadService,
)
from libs.enterprise_data.snapshot import CacheAccessMode, open_snapshot_store

_T1 = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)

VECTOR_TABLE = "test.agent_embeddings"
DSN = "postgresql://u@h/db"

INITIAL_ROWS = {
    "audits": [{"id": 1, "updated_at": _T1}],
    "violations": [{"id": 1, "updated_at": _T1}],
}

COLUMN_ROWS = {
    "audits": [
        {
            "column_name": "id",
            "data_type": "integer",
            "is_nullable": "NO",
            "character_maximum_length": None,
            "numeric_precision": 32,
            "numeric_scale": 0,
            "column_comment": None,
        }
    ]
}


class ScriptedCursor:
    def __init__(self, conn: ScriptedConn, cursor_factory=None) -> None:
        self._conn = conn
        self.cursor_factory = cursor_factory
        self.sql: str | None = None
        self.params: Any = None
        self._rows: list = []
        self.closed = False

    def execute(self, sql: str, params: Any = None) -> None:
        self.sql = sql
        self.params = params
        self._conn.executed.append((sql, params))
        self._rows = self._conn.rows_for(sql, params)

    def fetchall(self) -> list:
        return self._rows

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def close(self) -> None:
        self.closed = True


class ScriptedConn:
    def __init__(self, rows_for=None) -> None:
        self.autocommit = False
        self.closed = False
        self.executed: list[tuple[str, Any]] = []
        self.rows_for = rows_for or (lambda sql, params: [])

    def cursor(self, cursor_factory=None) -> ScriptedCursor:
        return ScriptedCursor(self, cursor_factory)

    def close(self) -> None:
        self.closed = True


class ScriptedPool:
    """Внедрённый источник соединений: весь SQL идёт в ``ScriptedConn``."""

    def __init__(self, rows_for=None) -> None:
        self.conn = ScriptedConn(rows_for)
        self.configured: list[str] = []
        self.runs = 0
        #: Классы работы, которыми загрузчик пометил каждый вызов. Проверяется
        #: тестом: загрузка идёт при подъёме платформы, и ушедшая в класс
        #: модели работа отказала бы при нескольких нитях загрузки.
        self.audiences: list[str] = []

    def configure(self, dsn: str) -> None:
        self.configured.append(dsn)

    def run(self, fn, *, audience: str):
        self.runs += 1
        self.audiences.append(audience)
        return fn(self.conn)


class RecordingStore:
    """Хранилище-заглушка: единственный писатель, вызовы фиксируются."""

    def __init__(self) -> None:
        self.ensure_schema_calls: list[tuple[str, list[dict]]] = []
        self.replace_calls: list[tuple[str, list[dict]]] = []
        self.refuse = False

    def ensure_schema(self, table: str, columns, schema_meta=None) -> bool:
        self.ensure_schema_calls.append((table, columns))
        return not self.refuse

    def replace_records(self, table: str, records) -> bool:
        self.replace_calls.append((table, records))
        return not self.refuse

    def upsert_records(self, table: str, records, *, key_column=None) -> bool:
        raise AssertionError("загрузчик не должен вызывать частичную запись")


def _table_from_sql(sql: str) -> str:
    match = re.search(r'FROM\s+"(\w+)"\."(\w+)"', sql, re.IGNORECASE)
    return match.group(2) if match else ""


def _rows_for(sql: str, params: Any) -> list:
    low = sql.lower()
    if "information_schema.columns" in low:
        # Имя таблицы приходит параметром: ``[schema, schema, name]``.
        name = params[2] if params and len(params) >= 3 else ""
        return COLUMN_ROWS.get(name, [])
    if "obj_description" in low:
        return [("table comment",)]
    if low.lstrip().startswith(("insert", "create")):
        return []
    return INITIAL_ROWS.get(_table_from_sql(sql), [])


@pytest.fixture
def store() -> RecordingStore:
    return RecordingStore()


def _loader(store: RecordingStore, pool: ScriptedPool, **kwargs) -> SnapshotLoadService:
    return SnapshotLoadService(
        dsn=DSN,
        store=store,  # type: ignore[arg-type] - подставной писатель в тесте
        pool=pool,
        schema="oarb",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Выбор track-колонки
# ---------------------------------------------------------------------------


class TestTrackColumn:
    def test_default_selection(self, store: RecordingStore) -> None:
        s = _loader(store, ScriptedPool(), tables=["audits"], vector_table=VECTOR_TABLE)
        assert s._track_column_for("audits") == "updated_at"
        assert s._track_column_for(VECTOR_TABLE) == "id"

    def test_from_snapshot_composition(self, store: RecordingStore) -> None:
        """Состав снимка задаёт per-table track-колонку (реестр — фаза 5.6)."""
        s = _loader(
            store,
            ScriptedPool(),
            tables=["oarb.report_items", "oarb.audits"],
            vector_table=VECTOR_TABLE,
            track_columns={"oarb.report_items": "modified_at"},
        )
        assert s._track_column_for("oarb.report_items") == "modified_at"
        assert s._track_column_for("oarb.audits") == "updated_at"
        assert s._track_column_for(VECTOR_TABLE) == "id"

    def test_unknown_table_falls_back_to_updated_at(self, store: RecordingStore) -> None:
        s = _loader(store, ScriptedPool(), tables=["oarb.unknown"])
        assert s._track_column_for("oarb.unknown") == "updated_at"


# ---------------------------------------------------------------------------
# Загрузка
# ---------------------------------------------------------------------------


class TestLoad:
    def test_empty_table_list_is_noop(self, store: RecordingStore) -> None:
        pool = ScriptedPool()
        s = _loader(store, pool, tables=[])
        result = s.load()
        assert result.total_tables == 0
        assert result.loaded_tables == 0
        assert store.replace_calls == []
        assert pool.runs == 0

    def test_loads_every_table_through_replace(self, store: RecordingStore) -> None:
        pool = ScriptedPool(_rows_for)
        s = _loader(store, pool, tables=["oarb.audits", "oarb.violations"])

        result = s.load()

        assert result.loaded_tables == 2
        assert result.errors == 0
        assert result.missing_tables == []
        assert result.rows_total == 2
        assert {table for table, _ in store.replace_calls} == {"oarb.audits", "oarb.violations"}

    def test_uses_single_write_path(self, store: RecordingStore) -> None:
        """Единственный путь записи — ``replace_records``.

        ``upsert_records`` в рантайме не вызывается: снимок — не дельта.
        (``RecordingStore.upsert_records`` упадёт, если позвать.)
        """
        s = _loader(store, ScriptedPool(_rows_for), tables=["oarb.audits"])
        s.load()

    def test_schema_passed_to_store(self, store: RecordingStore) -> None:
        """Схема передаётся в store нормализованной: ``[{"name","type",...}]``.

        ``_fetch_schema`` разбирает ``information_schema`` и приводит
        PG-типы к строковым, плюс добавляет псевдоколонку ``__table__``
        с комментарием таблицы.
        """
        s = _loader(store, ScriptedPool(_rows_for), tables=["oarb.audits"])
        s.load()
        assert len(store.ensure_schema_calls) == 1
        table, columns = store.ensure_schema_calls[0]
        assert table == "oarb.audits"
        assert columns[0]["name"] == "__table__"
        assert columns[0]["comment"] == "table comment"
        assert [c["name"] for c in columns][1] == "id"
        assert columns[1]["not_null"] is True

    def test_varchar_and_numeric_types_are_stringified(self, store: RecordingStore) -> None:
        pool = ScriptedPool(
            lambda sql, params: (
                [
                    {
                        "column_name": "title",
                        "data_type": "character varying",
                        "is_nullable": "YES",
                        "character_maximum_length": 255,
                        "numeric_precision": None,
                        "numeric_scale": None,
                        "column_comment": "заголовок",
                    },
                    {
                        "column_name": "amount",
                        "data_type": "numeric",
                        "is_nullable": "YES",
                        "character_maximum_length": None,
                        "numeric_precision": 10,
                        "numeric_scale": 2,
                        "column_comment": None,
                    },
                ]
                if "information_schema.columns" in sql.lower()
                else []
            )
        )
        s = _loader(store, pool, tables=["oarb.audits"])
        s.load()
        _table, columns = store.ensure_schema_calls[0]
        assert columns[0]["type"] == "character varying(255)"
        assert columns[1]["type"] == "numeric(10,2)"
        assert columns[0]["comment"] == "заголовок"

    def test_records_load_time(self, store: RecordingStore) -> None:
        s = _loader(store, ScriptedPool(_rows_for), tables=["oarb.audits"])
        result = s.load()

        assert result.loaded_at is not None
        assert result.started_at is not None
        assert result.loaded_at >= result.started_at
        assert "loaded_at" in s.get_stats()
        assert s.get_stats()["rows_total"] == 1

    def test_snapshot_time_published_in_event(self, store: RecordingStore) -> None:
        """Время снимка обязано попасть в долговечный журнал.

        Потребитель журнала — оператор в логах и агент через ``history_search``,
        поэтому время MUST быть в payload события ``cache_load_done``, а не
        только в ``get_stats()``.
        """
        events: list[tuple[str, dict]] = []

        def _sink(event_type, *, level="INFO", summary="", payload=None) -> None:
            events.append((event_type, {"level": level, "summary": summary, **(payload or {})}))

        s = _loader(
            store, ScriptedPool(_rows_for), tables=["oarb.audits"], event_sink=_sink
        )
        result = s.load()

        done = [payload for event_type, payload in events if event_type == "cache_load_done"]
        assert done, "событие cache_load_done не отправлено"
        payload = done[0]
        assert payload["loaded_at"] == result.loaded_at.isoformat()
        assert payload["started_at"] == result.started_at.isoformat()
        assert payload["duration_sec"] >= 0
        assert payload["rows_total"] == 1

    def test_per_table_events(self, store: RecordingStore) -> None:
        events: list[str] = []
        s = _loader(
            store,
            ScriptedPool(_rows_for),
            tables=["oarb.audits", "oarb.violations"],
            event_sink=lambda event_type, **_: events.append(event_type),
        )
        s.load()
        assert events[0] == "cache_load_started"
        assert events.count("sync_table_loaded") == 2
        assert events[-1] == "cache_load_done"

    def test_broken_event_sink_does_not_break_load(self, store: RecordingStore) -> None:
        """Журнал не должен ронять загрузку снимка."""

        def _boom(*args, **kwargs) -> None:
            raise RuntimeError("журнал недоступен")

        s = _loader(store, ScriptedPool(_rows_for), tables=["oarb.audits"], event_sink=_boom)
        result = s.load()
        assert result.loaded_tables == 1

    def test_configures_pool_with_dsn(self, store: RecordingStore) -> None:
        pool = ScriptedPool(_rows_for)
        s = _loader(store, pool, tables=["oarb.audits"])
        s.load()
        assert pool.configured == [DSN]

    def test_pool_is_never_reconfigured_per_query(self, store: RecordingStore) -> None:
        """Один ``configure`` на загрузку: DSN — настройка пула, а не запроса."""
        pool = ScriptedPool(_rows_for)
        s = _loader(store, pool, tables=["oarb.audits", "oarb.violations"])
        s.load()
        assert pool.configured == [DSN]
        assert pool.runs == 4  # 2 таблицы × (схема + данные)

    def test_missing_table_recorded_not_raised(self, store: RecordingStore) -> None:
        """Отсутствующая в PG таблица — запись в ``missing_tables``, не исключение."""

        def _raise_missing(sql: str, params: Any) -> list:
            if "information_schema.columns" in sql.lower():
                name = params[2] if params and len(params) >= 3 else ""
                return COLUMN_ROWS.get(name, [])
            if "obj_description" in sql.lower():
                return [("c",)]
            raise psycopg2.errors.UndefinedTable("nope")

        s = _loader(store, ScriptedPool(_raise_missing), tables=["oarb.ghost"])
        result = s.load()

        assert result.missing_tables == ["oarb.ghost"]
        assert result.loaded_tables == 0
        assert result.errors == 0
        assert store.replace_calls == []

    def test_connection_loss_raises(self, store: RecordingStore) -> None:
        """Потеря соединения MUST поднимать ``SnapshotLoadError``.

        Иначе вызывающий выставит готовность, а снимок останется недогруженным.
        """

        def _boom(sql: str, params: Any) -> list:
            raise psycopg2.OperationalError("connection lost")

        s = _loader(store, ScriptedPool(_boom), tables=["oarb.audits"])
        with pytest.raises(SnapshotLoadError) as excinfo:
            s.load()
        assert "PostgreSQL недоступен при загрузке oarb.audits" in str(excinfo.value)
        assert excinfo.value.code == "snapshot_load_error"

    def test_interface_error_also_raises(self, store: RecordingStore) -> None:
        def _boom(sql: str, params: Any) -> list:
            raise psycopg2.InterfaceError("connection already closed")

        s = _loader(store, ScriptedPool(_boom), tables=["oarb.audits"])
        with pytest.raises(SnapshotLoadError):
            s.load()

    def test_per_table_error_counted(self, store: RecordingStore) -> None:
        def _boom(sql: str, params: Any) -> list:
            if "information_schema" in sql.lower():
                return []
            raise RuntimeError("bad row")

        s = _loader(store, ScriptedPool(_boom), tables=["oarb.audits"])
        result = s.load()

        assert result.errors == 1
        assert result.loaded_tables == 0
        assert result.missing_tables == []

    def test_store_refusal_is_counted_as_error(self, store: RecordingStore) -> None:
        """Отказ хранилища не должен выглядеть как «загрузилось ноль строк»."""
        events: list[tuple[str, dict]] = []
        store.refuse = True
        s = _loader(
            store,
            ScriptedPool(_rows_for),
            tables=["oarb.audits"],
            event_sink=lambda event_type, **kwargs: events.append((event_type, kwargs)),
        )
        result = s.load()
        assert result.loaded_tables == 0
        assert result.errors == 1
        assert result.rows_total == 0
        errors = [p for event_type, p in events if event_type == "cache_load_error"]
        assert errors and errors[0]["payload"]["error_type"] == "SnapshotWriteError"

    def test_max_track_reported_per_table(self, store: RecordingStore) -> None:
        s = _loader(store, ScriptedPool(_rows_for), tables=["oarb.audits"])
        result = s.load()
        assert result.per_table["oarb.audits"] == {"rows": 1, "max_track": _T1}

    def test_load_is_blocking_and_leaves_no_threads(self, store: RecordingStore) -> None:
        """После возврата из ``load()`` ни одного потока загрузки не остаётся."""
        import threading

        before = {t.name for t in threading.enumerate()}
        s = _loader(store, ScriptedPool(_rows_for), tables=["oarb.audits"], max_workers=4)
        s.load()
        after = {t.name for t in threading.enumerate()}
        assert not any(name.startswith("snapshot-load") for name in after - before)

    def test_get_stats_before_load(self, store: RecordingStore) -> None:
        s = _loader(store, ScriptedPool(_rows_for), tables=["oarb.audits"])
        stats = s.get_stats()
        assert stats["loaded_at"] is None
        assert stats["loaded_ok"] == 0
        assert stats["tables"] == ["oarb.audits"]


# ---------------------------------------------------------------------------
# Сквозной путь: загрузчик + настоящее хранилище снимка
# ---------------------------------------------------------------------------


class TestLoadAgainstRealStore:
    """Единственное место, где загрузчик встречается с настоящим DuckDB.

    Остальные тесты модуля подменяют хранилище: связность «загрузчик →
    ``ensure_schema`` → ``replace_records`` → чтение снимка» иначе проверять
    нечем, а именно на ней держится утверждение «снимок — производный
    ресурс, а не второй источник истины».
    """

    def test_snapshot_becomes_readable(self, tmp_path) -> None:
        store = open_snapshot_store(
            str(tmp_path / "cache.duckdb"),
            CacheAccessMode.READ_WRITE,
            schema="oarb",
        )
        try:
            s = SnapshotLoadService(
                store=store,
                pool=ScriptedPool(_rows_for),
                schema="oarb",
                tables=["oarb.audits"],
            )
            result = s.load()
            assert result.loaded_tables == 1
            assert result.errors == 0
            rows = store.query_sql("SELECT id FROM oarb.audits")
            assert [row["id"] for row in rows["rows"]] == [1]
            assert store.get_stats()["upserts"] == 1
        finally:
            store.close()

    def test_garbage_table_name_is_an_error_not_a_silent_zero(self, tmp_path) -> None:
        store = open_snapshot_store(
            str(tmp_path / "cache.duckdb"),
            CacheAccessMode.READ_WRITE,
            schema="oarb",
        )
        try:
            s = SnapshotLoadService(
                store=store, pool=ScriptedPool(_rows_for), schema="oarb", tables=["oarb."]
            )
            result = s.load()
            assert result.errors == 1
            assert result.loaded_tables == 0
        finally:
            store.close()

    def test_read_only_store_makes_the_load_fail(self, tmp_path) -> None:
        """Загрузка обязана идти через ``READ_WRITE``: тихий отказ — не загрузка."""
        path = tmp_path / "cache.duckdb"
        writer = open_snapshot_store(
            str(path), CacheAccessMode.READ_WRITE, schema="oarb", verify=True
        )
        writer.close()
        reader = open_snapshot_store(
            str(path), CacheAccessMode.READ_ONLY, schema="oarb", verify=True
        )
        try:
            s = SnapshotLoadService(
                store=reader, pool=ScriptedPool(_rows_for), schema="oarb", tables=["oarb.audits"]
            )
            result = s.load()
            assert result.loaded_tables == 0
            assert result.errors == 1
        finally:
            reader.close()
