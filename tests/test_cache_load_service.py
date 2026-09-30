"""Тесты ``CacheLoadService`` — разовой синхронной загрузки кэша из PostgreSQL.

Change ``drop-local-cache-read-from-pg``. Заменяет тесты удалённого
``PgDuckDbSyncService``.

Перенесено без изменений покрытие того, что загрузчик **сохранил**:
выбор track-колонки (реестр → дефолт → vector-таблица) и разбор схемы.
Покрытие снятой машинерии (инкрементальный поллинг, колбэки, ``key_column_for``,
reconnect, периодический full-resync) удалено вместе с ней.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
_workspace = str(Path(__file__).resolve().parent.parent / "workspace")
if _workspace not in sys.path:
    sys.path.insert(0, _workspace)

from lib.services.cache_load_service import CacheLoadError, CacheLoadService
from tests.conftest import TEST_TABLE, TEST_VECTOR_TABLE

_T1 = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)

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
    ],
}


class ScriptedCursor:
    def __init__(self, conn, cursor_factory=None):
        self._conn = conn
        self.cursor_factory = cursor_factory
        self.sql = None
        self.params = None
        self._rows = []
        self.closed = False

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params
        self._conn.executed.append((sql, params))
        self._rows = self._conn.rows_for(sql, params)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        self.closed = True


class ScriptedConn:
    def __init__(self, rows_for=None):
        self.autocommit = False
        self.closed = False
        self.executed: list[tuple[str, list]] = []
        self.rows_for = rows_for or (lambda sql, params: [])

    def cursor(self, cursor_factory=None):
        return ScriptedCursor(self, cursor_factory)

    def close(self):
        self.closed = True


def _table_from_sql(sql: str) -> str:
    m = re.search(r'FROM\s+"(\w+)"\."(\w+)"', sql, re.IGNORECASE)
    return m.group(2) if m else ""


@pytest.fixture
def mock_pool(monkeypatch):
    """Подменить ``utils.db.run``/``configure``, чтобы весь SQL шёл на ScriptedConn."""
    fake = {"conn": None, "configured": []}

    monkeypatch.setattr(
        "utils.db.configure", lambda dsn: fake["configured"].append(dsn) or None
    )
    monkeypatch.setattr("utils.db.run", lambda fn: fn(fake["conn"]))
    return fake


def _rows_for(sql, params):
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


def _loader(**kwargs) -> CacheLoadService:
    return CacheLoadService(
        dsn="postgresql://u@h/db",
        store=MagicMock(name="store"),
        schema="oarb",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Выбор track-колонки (перенесено без изменений)
# ---------------------------------------------------------------------------


class TestTrackColumn:
    def test_default_selection(self):
        s = _loader(tables=["audits"], vector_table=TEST_VECTOR_TABLE)
        assert s._track_column_for("audits") == "updated_at"
        assert s._track_column_for(TEST_VECTOR_TABLE) == "id"

    def test_from_registry_resources(self):
        from lib.services.table_registry import (
            SkillRegistration,
            TableResource,
            VectorResource,
            table_registry,
        )

        table_registry.register(
            SkillRegistration(
                name="audit_analyzer",
                resources=(
                    TableResource(name=TEST_TABLE),
                    TableResource(name="test.report_items", tracking_column="modified_at"),
                    VectorResource(name=TEST_VECTOR_TABLE),
                ),
            )
        )
        try:
            s = _loader(tables=[TEST_TABLE], vector_table=TEST_VECTOR_TABLE)
            assert s._track_column_for("test.report_items") == "modified_at"
            assert s._track_column_for(TEST_TABLE) == "updated_at"
            assert s._track_column_for(TEST_VECTOR_TABLE) == "id"
            assert s._track_column_for("oarb.unknown") == "updated_at"
        finally:
            table_registry.unregister("audit_analyzer")

    def test_disabled_skill_falls_back(self):
        """Выключенный skill: ресурсы игнорируются → дефолт ``updated_at``."""
        from lib.services.table_registry import (
            SkillRegistration,
            TableResource,
            table_registry,
        )

        table_registry.register(
            SkillRegistration(
                name="audit_analyzer",
                enabled=False,
                resources=(TableResource(name="test.report_items", tracking_column="modified_at"),),
            )
        )
        try:
            s = _loader(tables=["test.report_items"])
            assert s._track_column_for("test.report_items") == "updated_at"
        finally:
            table_registry.unregister("audit_analyzer")


# ---------------------------------------------------------------------------
# Загрузка
# ---------------------------------------------------------------------------


class TestLoad:
    def test_empty_table_list_is_noop(self, mock_pool):
        s = _loader(tables=[])
        result = s.load()
        assert result.total_tables == 0
        assert result.loaded_tables == 0
        s._store.replace_records.assert_not_called()

    def test_loads_every_table_through_replace(self, mock_pool):
        mock_pool["conn"] = ScriptedConn(_rows_for)
        s = _loader(tables=["oarb.audits", "oarb.violations"])

        result = s.load()

        assert result.loaded_tables == 2
        assert result.errors == 0
        assert result.missing_tables == []
        assert result.rows_total == 2
        called = {c.args[0] for c in s._store.replace_records.call_args_list}
        assert called == {"oarb.audits", "oarb.violations"}

    def test_uses_single_write_path(self, mock_pool):
        """Единственный путь записи — ``replace_records``.

        ``upsert_records`` в рантайме не вызывается: кэш — снимок, а не дельта.
        """
        mock_pool["conn"] = ScriptedConn(_rows_for)
        s = _loader(tables=["oarb.audits"])
        s.load()
        s._store.upsert_records.assert_not_called()

    def test_schema_passed_to_store(self, mock_pool):
        """Схема передаётся в store нормализованной: ``[{"name","type",...}]``.

        ``_fetch_schema`` разбирает ``information_schema`` и приводит
        PG-типы к строковым (``character varying(255)``, ``numeric(10,2)``),
        плюс добавляет псевдоколонку ``__table__`` с комментарием таблицы.
        """
        mock_pool["conn"] = ScriptedConn(_rows_for)
        s = _loader(tables=["oarb.audits"])
        s.load()
        s._store.ensure_schema.assert_called_once()
        table, columns = s._store.ensure_schema.call_args.args
        assert table == "oarb.audits"
        assert columns[0]["name"] == "__table__"
        assert [c["name"] for c in columns][1] == "id"

    def test_records_load_time(self, mock_pool):
        mock_pool["conn"] = ScriptedConn(_rows_for)
        s = _loader(tables=["oarb.audits"])
        result = s.load()

        assert result.loaded_at is not None
        assert result.started_at is not None
        assert result.loaded_at >= result.started_at
        assert "loaded_at" in s.get_stats()
        assert s.get_stats()["rows_total"] == 1

    def test_snapshot_time_published_in_event(self, mock_pool):
        """Время снимка обязано попасть в долговечный журнал.

        Требование спеки (``runtime/entrypoints``): «Загрузка фиксирует время
        снимка» — оно не просто вычисляется в объекте, а доступно потребителям
        для оценки актуальности данных. Потребитель журнала — оператор в логах
        и агент через ``history_search``, поэтому время MUST быть в payload
        события ``cache_load_done``, а не только в ``get_stats()``.
        """
        from unittest.mock import MagicMock

        mock_pool["conn"] = ScriptedConn(_rows_for)
        db_logging_service = MagicMock()
        s = _loader(tables=["oarb.audits"], db_logging_service=db_logging_service)
        result = s.load()

        done_events = [
            c.args[0]
            for c in db_logging_service.log_event.call_args_list
            if c.args and getattr(c.args[0], "event_type", None) == "cache_load_done"
        ]
        assert done_events, "событие cache_load_done не записано в журнал"
        payload = done_events[0].payload

        assert payload["loaded_at"] == result.loaded_at.isoformat()
        assert payload["started_at"] == result.started_at.isoformat()
        assert payload["duration_sec"] >= 0

    def test_configures_pool_with_dsn(self, mock_pool):
        """DSN обязан попасть в общий пул до обращения к нему.

        ``configure`` вызывается и в ``load()``, и в каждом ``_db_run`` —
        достаточно того, что все вызовы несут правильный DSN.
        """
        mock_pool["conn"] = ScriptedConn(_rows_for)
        s = _loader(tables=["oarb.audits"])
        s.load()
        assert mock_pool["configured"]
        assert set(mock_pool["configured"]) == {"postgresql://u@h/db"}

    def test_missing_table_recorded_not_raised(self, mock_pool):
        """Отсутствующая в PG таблица — запись в ``missing_tables``, не исключение."""
        import psycopg2.errors

        def _raise_missing(sql, params):
            if "information_schema.columns" in sql.lower():
                name = params[2] if params and len(params) >= 3 else ""
                return COLUMN_ROWS.get(name, [])
            if "obj_description" in sql.lower():
                return [("c",)]
            raise psycopg2.errors.UndefinedTable("nope")

        mock_pool["conn"] = ScriptedConn(_raise_missing)
        s = _loader(tables=["oarb.ghost"])

        result = s.load()

        assert result.missing_tables == ["oarb.ghost"]
        assert result.loaded_tables == 0

    def test_connection_loss_raises_cache_load_error(self, mock_pool):
        """Потеря соединения MUST поднимать ``CacheLoadError``.

        Раньше worker молча останавливался, и кэш мог остаться недогруженным.
        Теперь вызывающий обязан узнать об этом и не выставлять готовность.
        """
        import psycopg2

        def _boom(sql, params):
            raise psycopg2.OperationalError("connection lost")

        mock_pool["conn"] = ScriptedConn(_boom)
        s = _loader(tables=["oarb.audits"])

        with pytest.raises(CacheLoadError):
            s.load()

    def test_per_table_error_counted(self, mock_pool):
        def _boom(sql, params):
            if "information_schema" in sql.lower():
                return []
            raise RuntimeError("bad row")

        mock_pool["conn"] = ScriptedConn(_boom)
        s = _loader(tables=["oarb.audits"])

        result = s.load()

        assert result.errors == 1
        assert result.loaded_tables == 0


# ---------------------------------------------------------------------------
# Границы числа потоков
# ---------------------------------------------------------------------------


class TestMaxWorkers:
    def test_bounded_by_pool_size(self):
        s = _loader(tables=[f"oarb.t{i}" for i in range(20)], max_workers=4)
        assert s._effective_workers() == 4

    def test_not_more_than_table_count(self):
        s = _loader(tables=["oarb.audits"], max_workers=8)
        assert s._effective_workers() == 1

    def test_at_least_one(self):
        s = _loader(tables=["oarb.audits"], max_workers=0)
        assert s._effective_workers() == 1
