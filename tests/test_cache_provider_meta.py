from __future__ import annotations

from unittest.mock import MagicMock, patch

import duckdb
import pytest

from lib.services.cache_provider_impl import (
    _META_SCHEMA,
    _META_TABLE,
    _capture_schema_meta,
    load_cache_from_postgres,
)


def _pg_rows():
    """Строки SELECT в _capture_schema_meta:
    (table_name, column_name, data_type, character_maximum_length,
     column_comment, table_comment).
    """
    return [
        ("audits", "id", "integer", None, "Идентификатор", "Аудиторские проверки"),
        ("audits", "title", "character varying", 500, "Название проверки", "Аудиторские проверки"),
        ("audits", "actual_date", "date", None, "Дата проверки", "Аудиторские проверки"),
        ("violations", "id", "bigint", None, None, None),
    ]


def _read_meta(conn):
    rows = conn.execute(
        f'SELECT schema_name, table_name, column_name, comment, pg_type '
        f'FROM "{_META_SCHEMA}"."{_META_TABLE}"'
    ).fetchall()
    return sorted(rows, key=lambda r: (r[0], r[1], r[2] or ""))


def test_capture_schema_meta_populates_duckdb(tmp_path):
    conn = duckdb.connect(str(tmp_path / "cache.duckdb"))
    pg_conn = MagicMock()
    cur = MagicMock()
    cur.fetchall.return_value = _pg_rows()
    pg_conn.cursor.return_value = cur

    _capture_schema_meta(conn, pg_conn, [("oarb", ["audits", "violations"])])

    rows = _read_meta(conn)
    assert rows == [
        ("oarb", "audits", None, "Аудиторские проверки", None),
        ("oarb", "audits", "actual_date", "Дата проверки", "date"),
        ("oarb", "audits", "id", "Идентификатор", "integer"),
        ("oarb", "audits", "title", "Название проверки", "varchar(500)"),
        ("oarb", "violations", "id", None, "bigint"),
    ]
    conn.close()


def test_capture_schema_meta_multiple_schemas(tmp_path):
    conn = duckdb.connect(str(tmp_path / "cache.duckdb"))
    pg_conn = MagicMock()
    cur = MagicMock()
    cur.fetchall.side_effect = [
        [("audits", "id", "integer", None, "Идентификатор", "Аудиторские проверки")],
        [("predefined_scripts", "name", "text", None, "Имя скрипта", "Реестр скриптов")],
    ]
    pg_conn.cursor.return_value = cur

    _capture_schema_meta(
        conn, pg_conn,
        [("oarb", ["audits"]), ("public", ["predefined_scripts"])],
    )

    rows = _read_meta(conn)
    assert len(rows) == 4
    assert ("oarb", "audits", None, "Аудиторские проверки", None) in rows
    assert ("public", "predefined_scripts", "name", "Имя скрипта", "text") in rows
    conn.close()


def test_capture_schema_meta_drops_previous(tmp_path):
    conn = duckdb.connect(str(tmp_path / "cache.duckdb"))
    pg_conn = MagicMock()
    cur = MagicMock()
    cur.fetchall.return_value = [("audits", "id", "integer", None, "Идентификатор", "Аудиторские проверки")]
    pg_conn.cursor.return_value = cur
    _capture_schema_meta(conn, pg_conn, [("oarb", ["audits"])])

    # второй прогон — старая таблица должна быть пересоздана, а не дополнена
    cur.fetchall.return_value = []
    _capture_schema_meta(conn, pg_conn, [("oarb", ["audits"])])
    assert _read_meta(conn) == []
    conn.close()


def test_capture_schema_meta_empty_tables_ok(tmp_path):
    conn = duckdb.connect(str(tmp_path / "cache.duckdb"))
    pg_conn = MagicMock()
    _capture_schema_meta(conn, pg_conn, [("oarb", [])])
    assert _read_meta(conn) == []
    conn.close()


def test_load_cache_from_postgres_captures_meta(tmp_path):
    cache_path = str(tmp_path / "cache.duckdb")
    db_config = {
        "schema": "oarb",
        "tables": ["audits"],
        "additional_tables": [["public", "predefined_scripts"]],
    }
    pg_conn = MagicMock()
    cur = MagicMock()
    cur.fetchall.side_effect = [
        [("audits", "id", "integer", None, "Идентификатор", "Аудиторские проверки")],
        [("predefined_scripts", "name", "text", None, "Имя скрипта", "Реестр скриптов")],
    ]
    pg_conn.cursor.return_value = cur
    with patch("utils.db.resolve_dsn",
               return_value="postgres://user:pass@localhost/db"), \
         patch("psycopg2.connect", return_value=pg_conn), \
         patch("lib.services.cache_provider_impl._copy_table"), \
         patch("lib.services.cache_provider_impl._store_meta"):
        load_cache_from_postgres(cache_path, db_config)

    conn = duckdb.connect(cache_path)
    rows = _read_meta(conn)
    assert ("oarb", "audits", None, "Аудиторские проверки", None) in rows
    assert ("public", "predefined_scripts", "name", "Имя скрипта", "text") in rows
    conn.close()
