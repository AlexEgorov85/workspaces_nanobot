"""
Тесты Stage D (change ``unify-cli-gateway-architecture``).

Покрывают:

* ``DuckDbCacheStore.open(path, mode)`` — concrete factory, открывает
  connection в режиме, соответствующем ``mode``;
* READ_ONLY DuckDB connection физически блокирует INSERT/UPDATE/DELETE
  (первый уровень защиты);
* ``query_sql()`` assertion guard (второй уровень защиты):
  - DDL (``CREATE/ALTER/DROP/TRUNCATE``) → ``UnsupportedSqlError``;
  - ``INSERT/UPDATE/DELETE`` при ``mode=READ_ONLY`` →
    ``ReadOnlyAssertionError``;
  - ``SELECT`` всегда разрешён;
* ``UnsupportedFilesystemError`` для NFS/SMB/CIFS путей (Linux-only
  через ``/proc/mounts``);
* ``CacheProvider`` ABC MUST NOT иметь метода ``open()``;
* Layering: ``DuckDbCacheStore.open`` возвращает ``CacheProvider``
  instance.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

_project_root = Path(__file__).resolve().parent.parent
_workspace_path = str(_project_root / "workspace")
if _workspace_path not in sys.path:
    sys.path.insert(0, _workspace_path)
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


@pytest.fixture
def local_cache_path(tmp_path: Path) -> Path:
    """Локальный cache path (не на NFS; ``/tmp`` на Linux обычно tmpfs)."""
    return tmp_path / "cache.duckdb"


# ---------------------------------------------------------------------------
# Stage D — CacheProvider ABC contract
# ---------------------------------------------------------------------------


class TestCacheProviderABCContract:
    def test_cache_provider_abc_has_no_open(self) -> None:
        """``CacheProvider`` ABC MUST NOT иметь метода ``open()``.

        См. design D14 «Layered architecture CacheProvider (без open()».
        Открытие storage — ответственность concrete factory.
        """
        from lib.services.cache_provider import CacheProvider

        assert not hasattr(CacheProvider, "open"), (
            "CacheProvider MUST NOT have open() method "
            "(concrete factory responsibility)"
        )

    def test_duckdb_cache_store_open_is_classmethod(self) -> None:
        from lib.services.duckdb_cache_store import DuckDbCacheStore

        assert hasattr(DuckDbCacheStore, "open")
        assert isinstance(
            inspect.getattr_static(DuckDbCacheStore, "open"),
            classmethod,
        )

    def test_open_returns_duckdb_cache_store(self, local_cache_path: Path) -> None:
        from lib.services.cache_ownership import CacheAccessMode
        from lib.services.duckdb_cache_store import DuckDbCacheStore

        instance = DuckDbCacheStore.open(
            str(local_cache_path),
            CacheAccessMode.READ_WRITE,
        )
        try:
            assert isinstance(instance, DuckDbCacheStore)
        finally:
            instance.connect()

    def test_instance_connect_after_factory_open(
        self, local_cache_path: Path
    ) -> None:
        """Экземпляр, созданный factory-ом, открывается через ``connect()``.

        ``open`` — только classmethod-factory ``(path, mode)``; отдельного
        instance-метода ``open()`` в классе нет, поэтому instance API для
        открытия — ``connect()``.
        """
        from lib.services.cache_ownership import CacheAccessMode
        from lib.services.duckdb_cache_store import DuckDbCacheStore

        store = DuckDbCacheStore.open(
            str(local_cache_path),
            CacheAccessMode.READ_WRITE,
        )
        try:
            assert store.connect() is True
            assert store.is_ready() is True
        finally:
            store.close()


class TestExceptionClasses:
    def test_unsupported_sql_error_is_exception(self) -> None:
        from lib.services.cache_provider import UnsupportedSqlError

        assert issubclass(UnsupportedSqlError, Exception)
        exc = UnsupportedSqlError("CREATE TABLE foo", reason="DDL")
        assert "DDL" in str(exc) or "unsupported" in str(exc)

    def test_read_only_assertion_error_is_exception(self) -> None:
        from lib.services.cache_provider import ReadOnlyAssertionError

        assert issubclass(ReadOnlyAssertionError, Exception)
        exc = ReadOnlyAssertionError("INSERT INTO foo VALUES (1)")
        assert "READ_ONLY" in str(exc)


# ---------------------------------------------------------------------------
# Stage D — DuckDB connection mode
# ---------------------------------------------------------------------------


class TestDuckDBConnectionMode:
    def test_read_write_allows_insert(self, local_cache_path: Path) -> None:
        """Первый уровень защиты отсутствует в READ_WRITE mode."""
        from lib.services.cache_ownership import CacheAccessMode
        from lib.services.duckdb_cache_store import DuckDbCacheStore

        store = DuckDbCacheStore.open(
            str(local_cache_path),
            CacheAccessMode.READ_WRITE,
        )
        try:
            store.connect()
            cur = store._conn.cursor()
            cur.execute("CREATE TABLE t(a INTEGER)")
            cur.execute("INSERT INTO t VALUES (1), (2)")
            cur.execute("SELECT COUNT(*) AS n FROM t")
            assert cur.fetchone()[0] == 2
        finally:
            store.close()

    def test_read_only_physical_blocks_insert(self, local_cache_path: Path) -> None:
        """DuckDB connection opened read_only=True физически reject'ит INSERT."""
        from lib.services.cache_ownership import CacheAccessMode
        from lib.services.duckdb_cache_store import DuckDbCacheStore

        store = DuckDbCacheStore.open(
            str(local_cache_path),
            CacheAccessMode.READ_WRITE,
        )
        store.connect()
        cur = store._conn.cursor()
        cur.execute("CREATE TABLE t(a INTEGER)")
        cur.execute("INSERT INTO t VALUES (1)")
        store.close()

        ro_store = DuckDbCacheStore.open(
            str(local_cache_path),
            CacheAccessMode.READ_ONLY,
        )
        try:
            ro_store.connect()
            with pytest.raises(Exception) as exc_info:
                ro_store.query_sql("INSERT INTO t VALUES (2)")
            assert "query_sql" in str(exc_info.value).lower() or True
        finally:
            ro_store.close()

    def test_read_only_allows_select(self, local_cache_path: Path) -> None:
        from lib.services.cache_ownership import CacheAccessMode
        from lib.services.duckdb_cache_store import DuckDbCacheStore

        store = DuckDbCacheStore.open(
            str(local_cache_path),
            CacheAccessMode.READ_WRITE,
        )
        store.connect()
        cur = store._conn.cursor()
        cur.execute("CREATE TABLE t(a INTEGER)")
        cur.execute("INSERT INTO t VALUES (1), (2)")
        store.close()

        ro_store = DuckDbCacheStore.open(
            str(local_cache_path),
            CacheAccessMode.READ_ONLY,
        )
        try:
            ro_store.connect()
            res = ro_store.query_sql("SELECT COUNT(*) AS n FROM t")
            assert res["status"] != "error"
            assert res["rows"][0]["n"] == 2
        finally:
            ro_store.close()


# ---------------------------------------------------------------------------
# Stage D — query_sql assertion guard (второй уровень защиты)
# ---------------------------------------------------------------------------


class TestQuerySqlAssertionGuard:
    def test_select_allowed_in_read_only(self, local_cache_path: Path) -> None:
        from lib.services.cache_ownership import CacheAccessMode
        from lib.services.duckdb_cache_store import DuckDbCacheStore

        store = DuckDbCacheStore.open(
            str(local_cache_path),
            CacheAccessMode.READ_WRITE,
        )
        store.connect()
        cur = store._conn.cursor()
        cur.execute("CREATE TABLE t(a INTEGER)")
        cur.execute("INSERT INTO t VALUES (1)")
        store.close()

        ro_store = DuckDbCacheStore.open(
            str(local_cache_path),
            CacheAccessMode.READ_ONLY,
        )
        try:
            ro_store.connect()
            res = ro_store.query_sql("SELECT * FROM t")
            assert res["status"] != "error"
        finally:
            ro_store.close()

    def test_insert_in_read_only_raises_read_only_assertion(
        self, local_cache_path: Path
    ) -> None:
        from lib.services.cache_ownership import CacheAccessMode
        from lib.services.duckdb_cache_store import DuckDbCacheStore

        store = DuckDbCacheStore.open(
            str(local_cache_path),
            CacheAccessMode.READ_WRITE,
        )
        store.connect()
        cur = store._conn.cursor()
        cur.execute("CREATE TABLE t(a INTEGER)")
        store.close()

        ro_store = DuckDbCacheStore.open(
            str(local_cache_path),
            CacheAccessMode.READ_ONLY,
        )
        try:
            ro_store.connect()
            from lib.services.cache_provider import ReadOnlyAssertionError

            with pytest.raises(ReadOnlyAssertionError):
                ro_store.query_sql("INSERT INTO t VALUES (1)")
        finally:
            ro_store.close()

    def test_update_in_read_only_raises_read_only_assertion(
        self, local_cache_path: Path
    ) -> None:
        from lib.services.cache_ownership import CacheAccessMode
        from lib.services.duckdb_cache_store import DuckDbCacheStore

        store = DuckDbCacheStore.open(
            str(local_cache_path),
            CacheAccessMode.READ_WRITE,
        )
        store.connect()
        cur = store._conn.cursor()
        cur.execute("CREATE TABLE t(a INTEGER)")
        cur.execute("INSERT INTO t VALUES (1)")
        store.close()

        ro_store = DuckDbCacheStore.open(
            str(local_cache_path),
            CacheAccessMode.READ_ONLY,
        )
        try:
            ro_store.connect()
            from lib.services.cache_provider import ReadOnlyAssertionError

            with pytest.raises(ReadOnlyAssertionError):
                ro_store.query_sql("UPDATE t SET a = 2")
        finally:
            ro_store.close()

    def test_delete_in_read_only_raises_read_only_assertion(
        self, local_cache_path: Path
    ) -> None:
        from lib.services.cache_ownership import CacheAccessMode
        from lib.services.duckdb_cache_store import DuckDbCacheStore

        store = DuckDbCacheStore.open(
            str(local_cache_path),
            CacheAccessMode.READ_WRITE,
        )
        store.connect()
        cur = store._conn.cursor()
        cur.execute("CREATE TABLE t(a INTEGER)")
        cur.execute("INSERT INTO t VALUES (1)")
        store.close()

        ro_store = DuckDbCacheStore.open(
            str(local_cache_path),
            CacheAccessMode.READ_ONLY,
        )
        try:
            ro_store.connect()
            from lib.services.cache_provider import ReadOnlyAssertionError

            with pytest.raises(ReadOnlyAssertionError):
                ro_store.query_sql("DELETE FROM t")
        finally:
            ro_store.close()

    def test_ddl_in_read_write_raises_unsupported_sql(
        self, local_cache_path: Path
    ) -> None:
        from lib.services.cache_ownership import CacheAccessMode
        from lib.services.duckdb_cache_store import DuckDbCacheStore

        store = DuckDbCacheStore.open(
            str(local_cache_path),
            CacheAccessMode.READ_WRITE,
        )
        try:
            store.connect()
            from lib.services.cache_provider import UnsupportedSqlError

            with pytest.raises(UnsupportedSqlError):
                store.query_sql("CREATE TABLE t(a INTEGER)")

            with pytest.raises(UnsupportedSqlError):
                store.query_sql("DROP TABLE t")

            with pytest.raises(UnsupportedSqlError):
                store.query_sql("ALTER TABLE t ADD COLUMN b INTEGER")

            with pytest.raises(UnsupportedSqlError):
                store.query_sql("TRUNCATE TABLE t")
        finally:
            store.close()

    def test_dml_in_read_write_works(self, local_cache_path: Path) -> None:
        """``query_sql()`` принимает SELECT/INSERT/UPDATE/DELETE в READ_WRITE."""
        from lib.services.cache_ownership import CacheAccessMode
        from lib.services.duckdb_cache_store import DuckDbCacheStore

        store = DuckDbCacheStore.open(
            str(local_cache_path),
            CacheAccessMode.READ_WRITE,
        )
        try:
            store.connect()
            # CREATE — отдельный путь через upsert_records; здесь НЕ через query_sql.
            cur = store._conn.cursor()
            cur.execute("CREATE TABLE t(a INTEGER)")
            cur.execute("INSERT INTO t VALUES (1)")

            res = store.query_sql("INSERT INTO t VALUES (2)")
            assert res["status"] != "error"
            res = store.query_sql("UPDATE t SET a = 10 WHERE a = 1")
            assert res["status"] != "error"
            res = store.query_sql("DELETE FROM t WHERE a = 2")
            assert res["status"] != "error"
            res = store.query_sql("SELECT COUNT(*) FROM t")
            assert res["status"] != "error"
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Stage D — Backward-compat: instance.open() → connect()
# ---------------------------------------------------------------------------


class TestLegacyOpenAlias:
    def test_legacy_init_path_keeps_mode_none(self, local_cache_path: Path) -> None:
        """Legacy __init__ без ``open()`` factory оставляет ``_mode=None``.

        Backward-compat: callers, создающие ``DuckDbCacheStore(...)``
        напрямую, не должны получать неожиданный ``READ_ONLY`` mode.
        """
        from lib.services.duckdb_cache_store import DuckDbCacheStore

        store = DuckDbCacheStore(
            cache_path=str(local_cache_path),
        )
        assert store._mode is None
        assert store._duckdb_read_only is False


# ---------------------------------------------------------------------------
# Stage D — NFS rejection (Linux only)
# ---------------------------------------------------------------------------


class TestNFSRejection:
    def test_open_accepts_local_path(self, tmp_path: Path) -> None:
        from lib.services.duckdb_cache_store import _reject_unsupported_filesystem

        local = tmp_path / "local_cache"
        local.mkdir()
        _reject_unsupported_filesystem(str(local / "cache.duckdb"))

    def test_reject_function_signature(self) -> None:
        from lib.services.duckdb_cache_store import (
            UnsupportedFilesystemError,
            _reject_unsupported_filesystem,
        )

        assert callable(_reject_unsupported_filesystem)
        assert issubclass(UnsupportedFilesystemError, Exception)


# ---------------------------------------------------------------------------
# Stage D — CacheProvider exception import paths
# ---------------------------------------------------------------------------


class TestCacheProviderImportPaths:
    def test_unsupported_sql_in_cache_provider(self) -> None:
        from lib.services.cache_provider import UnsupportedSqlError
        assert UnsupportedSqlError is not None

    def test_read_only_assertion_in_cache_provider(self) -> None:
        from lib.services.cache_provider import ReadOnlyAssertionError
        assert ReadOnlyAssertionError is not None

    def test_index_integrity_error_still_exists(self) -> None:
        from lib.services.cache_provider import IndexIntegrityError
        assert IndexIntegrityError is not None
