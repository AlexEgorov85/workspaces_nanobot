"""Файл кэша не удерживается процессом между операциями чтения.

Change ``drop-local-cache-read-from-pg``, фаза 3.

Основание требования: DuckDB допускает несколько параллельных читателей и
блокирует только writer (проверено на DuckDB 1.5.4 двумя реальными
процессами). Поскольку writer'ов после стадии загрузки не остаётся, файл
свободен всё время работы процесса.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from lib.services.cache_provider import CacheAccessMode
from lib.services.duckdb_cache_store import DuckDbCacheStore

_ROOT = Path(__file__).resolve().parent.parent


def _build_cache(path: Path) -> None:
    """Создать файл кэша с одной таблицей через режим записи."""
    writer = DuckDbCacheStore.open(str(path), CacheAccessMode.READ_WRITE)
    try:
        assert writer.connect() is True
        writer.ensure_schema(
            "oarb.t",
            [{"name": "id", "type": "integer", "not_null": True, "comment": None}],
        )
        writer.replace_records("oarb.t", [{"id": 1}, {"id": 2}, {"id": 3}])
    finally:
        writer.close()


@pytest.fixture
def cache_file(tmp_path: Path) -> Path:
    path = tmp_path / "cache.duckdb"
    _build_cache(path)
    assert path.exists()
    return path


class TestFileNotHeldBetweenOperations:
    def test_connection_closed_after_each_read(self, cache_file: Path) -> None:
        store = DuckDbCacheStore.open(str(cache_file), CacheAccessMode.READ_ONLY)
        try:
            assert store.connect() is True
            # connect() в READ_ONLY проверяет файл и сразу его отпускает
            assert store._conn is None, "connect() не должен удерживать файл"

            for _ in range(3):
                res = store.query_sql("SELECT id FROM oarb.t ORDER BY id")
                assert res["status"] != "error", res
                assert [r["id"] for r in res["rows"]] == [1, 2, 3]
                assert store._conn is None, "соединение не закрыто после чтения"
        finally:
            store.close()

    def test_schema_and_explain_also_release(self, cache_file: Path) -> None:
        store = DuckDbCacheStore.open(str(cache_file), CacheAccessMode.READ_ONLY)
        try:
            store.connect()
            store.get_schema()
            assert store._conn is None
            store.explain("SELECT id FROM oarb.t")
            assert store._conn is None
        finally:
            store.close()

    def test_file_readable_by_another_process(self, cache_file: Path) -> None:
        """Внешний процесс открывает тот же файл, пока gateway работает."""
        store = DuckDbCacheStore.open(str(cache_file), CacheAccessMode.READ_ONLY)
        try:
            store.connect()
            assert store.query_sql("SELECT 1")["status"] != "error"
            proc = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import duckdb,sys;"
                    "c=duckdb.connect(sys.argv[1], read_only=True);"
                    "print(c.execute('SELECT count(*) FROM oarb.t').fetchone()[0]);"
                    "c.close()",
                    str(cache_file),
                ],
                capture_output=True,
                text=True,
                cwd=str(_ROOT),
            )
            assert proc.returncode == 0, proc.stderr
            assert proc.stdout.strip() == "3"
        finally:
            store.close()

    def test_write_mode_holds_connection(self, cache_file: Path) -> None:
        """Режим записи держит соединение — это окно стадии загрузки."""
        writer = DuckDbCacheStore.open(str(cache_file), CacheAccessMode.READ_WRITE)
        try:
            writer.connect()
            assert writer._conn is not None
        finally:
            writer.close()
        assert writer._conn is None


class TestReadOnlyGuardStillHolds:
    def test_dml_rejected_before_file_is_opened(self, cache_file: Path) -> None:
        from lib.services.cache_provider import ReadOnlyAssertionError

        store = DuckDbCacheStore.open(str(cache_file), CacheAccessMode.READ_ONLY)
        try:
            store.connect()
            with pytest.raises(ReadOnlyAssertionError):
                store.query_sql("INSERT INTO oarb.t VALUES (9)")
            assert store._conn is None, "отказ не должен оставлять файл открытым"
        finally:
            store.close()

    def test_ddl_rejected_in_read_write(self, cache_file: Path) -> None:
        from lib.services.cache_provider import UnsupportedSqlError

        writer = DuckDbCacheStore.open(str(cache_file), CacheAccessMode.READ_WRITE)
        try:
            writer.connect()
            with pytest.raises(UnsupportedSqlError):
                writer.query_sql("CREATE TABLE oarb.other (id integer)")
        finally:
            writer.close()


class TestOpenCost:
    """Замер платы формы «открыть на операцию» (задача 3.2)."""

    def test_per_operation_open_cost(self, cache_file: Path) -> None:
        store = DuckDbCacheStore.open(str(cache_file), CacheAccessMode.READ_ONLY)
        try:
            store.connect()
            # прогрев
            for _ in range(3):
                store.query_sql("SELECT id FROM oarb.t")

            n = 50
            t0 = time.perf_counter()
            for _ in range(n):
                store.query_sql("SELECT id FROM oarb.t")
            per_op = (time.perf_counter() - t0) / n

            # для сравнения — удержание соединения
            store._open_locked()
            t0 = time.perf_counter()
            for _ in range(n):
                store.query_sql("SELECT id FROM oarb.t")
            persistent = (time.perf_counter() - t0) / n
            store._close_locked()
        finally:
            store.close()

        ratio = per_op / persistent if persistent else float("inf")
        print(
            f"\nquery_sql: per-operation={per_op * 1000:.3f} ms, "
            f"persistent={persistent * 1000:.3f} ms, x{ratio:.1f}"
        )
        # Порог зафиксирован как запас: открытие файла — операция ФС,
        # порядок — единицы миллисекунд на локальной FS.
        assert per_op < 0.5, f"открытие на операцию слишком дорого: {per_op * 1000:.1f} ms"
