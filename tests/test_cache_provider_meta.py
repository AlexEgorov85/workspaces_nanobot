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
    with patch("utils.db.run", lambda fn: fn(pg_conn)), \
         patch("lib.services.cache_provider_impl._copy_table"), \
         patch("lib.services.cache_provider_impl._store_meta"):
        load_cache_from_postgres(cache_path, db_config)

    conn = duckdb.connect(cache_path)
    rows = _read_meta(conn)
    assert ("oarb", "audits", None, "Аудиторские проверки", None) in rows
    assert ("public", "predefined_scripts", "name", "Имя скрипта", "text") in rows


class TestIndexSignature:
    """``compute_index_signature`` + ``verify_index_signature`` —
    integrity-метаданные для ``agent_vector_index_store``.
    """

    def test_compute_deterministic_same_input(self):
        from lib.services.cache_provider_impl import compute_index_signature

        cfg = {
            "src_table": "oarb.audits",
            "pk_column": "id",
            "content_cols": ["title", "description"],
            "embedding_cols": [{"col": "title", "text_chunk_size": 500}],
            "track_column": "updated_at",
            "embedding_model": "mxbai-embed-large:latest",
            "embedding_dimension": 1024,
            "chunk_size": 500,
            "chunk_overlap": 80,
        }
        sig1 = compute_index_signature(cfg)
        sig2 = compute_index_signature(cfg)
        assert sig1 == sig2
        assert len(sig1) == 64
        assert all(c in "0123456789abcdef" for c in sig1)

    def test_compute_changes_on_model_change(self):
        from lib.services.cache_provider_impl import compute_index_signature

        cfg1 = {"embedding_model": "mxbai", "embedding_dimension": 1024}
        cfg2 = {"embedding_model": "nomic", "embedding_dimension": 1024}
        assert compute_index_signature(cfg1) != compute_index_signature(cfg2)

    def test_compute_changes_on_dimension_change(self):
        from lib.services.cache_provider_impl import compute_index_signature

        cfg1 = {"embedding_dimension": 1024}
        cfg2 = {"embedding_dimension": 768}
        assert compute_index_signature(cfg1) != compute_index_signature(cfg2)

    def test_compute_changes_on_chunk_change(self):
        from lib.services.cache_provider_impl import compute_index_signature

        cfg1 = {"chunk_size": 500, "chunk_overlap": 80}
        cfg2 = {"chunk_size": 600, "chunk_overlap": 80}
        cfg3 = {"chunk_size": 500, "chunk_overlap": 100}
        sig1 = compute_index_signature(cfg1)
        assert compute_index_signature(cfg2) != sig1
        assert compute_index_signature(cfg3) != sig1

    def test_compute_handles_missing_keys_as_empty(self):
        from lib.services.cache_provider_impl import compute_index_signature

        sig_empty = compute_index_signature({})
        sig_same = compute_index_signature({})
        assert sig_empty == sig_same

    def test_verify_current_when_signatures_match(self):
        from lib.services.cache_provider_impl import (
            compute_index_signature,
            verify_index_signature,
        )

        cfg = {"embedding_model": "mxbai", "embedding_dimension": 1024}
        stored_sig = compute_index_signature(cfg)
        assert verify_index_signature({"signature": stored_sig}, cfg) == "CURRENT"

    def test_verify_stale_when_model_changed(self):
        from lib.services.cache_provider_impl import (
            compute_index_signature,
            verify_index_signature,
        )

        stored_cfg = {"embedding_model": "mxbai", "embedding_dimension": 1024}
        current_cfg = {"embedding_model": "nomic", "embedding_dimension": 1024}
        stored_sig = compute_index_signature(stored_cfg)
        assert verify_index_signature({"signature": stored_sig}, current_cfg) == "STALE"

    def test_verify_invalid_when_no_signature(self):
        from lib.services.cache_provider_impl import verify_index_signature

        assert verify_index_signature({}, {"embedding_model": "mxbai"}) == "INVALID"
        assert verify_index_signature(
            {"signature": None}, {"embedding_model": "mxbai"},
        ) == "INVALID"

    def test_verify_invalid_when_signature_corrupt(self):
        from lib.services.cache_provider_impl import verify_index_signature

        assert verify_index_signature(
            {"signature": "not-hex"}, {},
        ) == "INVALID"
        assert verify_index_signature(
            {"signature": "a" * 32}, {},
        ) == "INVALID"

    def test_verify_current_when_no_metadata(self):
        """Без stored_meta — нет данных для проверки, трактуем как CURRENT
        (индекс ещё не был сохранён / нечего перепроверять).
        """
        from lib.services.cache_provider_impl import verify_index_signature

        assert verify_index_signature(None, {}) == "CURRENT"
