from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add workspace to sys.path so utils.db can be found
_workspace_path = str(Path(__file__).resolve().parent.parent / "workspace")
if _workspace_path not in sys.path:
    sys.path.insert(0, _workspace_path)
# Also add project root for config
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
# Add user site-packages for nanobot (needed because pg_session_manager imports it)
_user_site = r"C:\Users\Алексей\AppData\Roaming\Python\Python314\site-packages"
if _user_site not in sys.path:
    sys.path.insert(0, _user_site)


@pytest.fixture(autouse=True)
def mock_psycopg2():
    """Mock psycopg2 before importing utils.db."""
    with (
        patch.dict("sys.modules"),
        patch("psycopg2.connect") as mock_connect,
        patch("psycopg2.extras.Json", lambda x: x),
        patch("psycopg2.extras.RealDictCursor") as mock_rdc,
        patch("psycopg2.extras.register_json"),
        patch("psycopg2.extensions.register_adapter"),
    ):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.__enter__.return_value = mock_cur
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value = mock_conn
        mock_rdc.return_value = MagicMock()

        # Import the real utils.db (with psycopg2 mocked)
        from utils.db import (
            DB_RETRYABLE_ERRORS,
            _connect,
            _disconnect,
            _get_dsn,
            _retry,
            async_execute,
            async_fetch,
            async_fetchone,
            async_fetchval,
            async_transaction,
            configure,
            execute,
            fetch,
            fetchone,
            fetchval,
            transaction,
        )

        # Reset _dsn before each test
        configure("")

        yield {
            "mock_connect": mock_connect,
            "mock_conn": mock_conn,
            "mock_cur": mock_cur,
            "configure": configure,
            "_connect": _connect,
            "_disconnect": _disconnect,
            "_get_dsn": _get_dsn,
            "execute": execute,
            "fetch": fetch,
            "fetchone": fetchone,
            "fetchval": fetchval,
            "transaction": transaction,
            "async_execute": async_execute,
            "async_fetch": async_fetch,
            "async_fetchone": async_fetchone,
            "async_fetchval": async_fetchval,
            "async_transaction": async_transaction,
            "DB_RETRYABLE_ERRORS": DB_RETRYABLE_ERRORS,
            "_retry": _retry,
        }


class TestConfigure:
    def test_sets_dsn(self, mock_psycopg2):
        mock_psycopg2["configure"]("postgresql://u:p@h/db")
        assert mock_psycopg2["_get_dsn"]() == "postgresql://u:p@h/db"

    def test_empty_dsn_skips_set(self, mock_psycopg2):
        import utils.db as _db

        _db._dsn = "existing"
        mock_psycopg2["configure"]("")
        assert mock_psycopg2["_get_dsn"]() == "existing"

    def test_same_dsn_idempotent(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn1")
        mock_psycopg2["configure"]("dsn1")
        assert mock_psycopg2["_get_dsn"]() == "dsn1"

    def test_changes_dsn(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn1")
        mock_psycopg2["configure"]("dsn2")
        assert mock_psycopg2["_get_dsn"]() == "dsn2"


class TestConnect:
    def test_connect_success(self, mock_psycopg2):
        mock_psycopg2["configure"]("postgresql://u:p@h/db")
        conn = mock_psycopg2["_connect"]()
        assert conn is mock_psycopg2["mock_conn"]
        mock_psycopg2["mock_connect"].assert_called_with(
            "postgresql://u:p@h/db", gssencmode="disable"
        )

    def test_connect_no_dsn_raises(self, mock_psycopg2):
        import utils.db as _db

        _db._dsn = ""
        with patch("utils.db._get_dsn", return_value=""):
            with pytest.raises(RuntimeError, match="не инициализирован"):
                mock_psycopg2["_connect"]()

    def test_connect_non_retryable_error(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        mock_psycopg2["mock_connect"].side_effect = ValueError("bad")
        with pytest.raises(ValueError, match="bad"):
            mock_psycopg2["_connect"]()


class TestDisconnect:
    def test_closes_connection(self, mock_psycopg2):
        conn = MagicMock()
        mock_psycopg2["_disconnect"](conn)
        conn.close.assert_called_once()


class TestExecute:
    def test_execute_returns_status(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        mock_psycopg2["mock_cur"].statusmessage = "INSERT 0 1"
        result = mock_psycopg2["execute"]("INSERT INTO t VALUES (%s)", 42)
        assert result == "INSERT 0 1"
        mock_psycopg2["mock_cur"].execute.assert_called_with(
            "INSERT INTO t VALUES (%s)", (42,)
        )

    def test_execute_empty_sql(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        mock_psycopg2["mock_cur"].statusmessage = None
        result = mock_psycopg2["execute"]("")
        assert result is None

    def test_execute_retry_on_operational_error(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        mock_psycopg2["mock_connect"].side_effect = [
            __import__("psycopg2").OperationalError("conn lost"),
            mock_psycopg2["mock_conn"],
        ]
        mock_psycopg2["mock_cur"].statusmessage = "UPDATE 5"
        result = mock_psycopg2["execute"]("UPDATE t SET x=1")
        assert result == "UPDATE 5"


class TestFetch:
    def test_fetch_returns_dicts(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        mock_psycopg2["mock_cur"].fetchall.return_value = [
            {"id": 1, "name": "foo"},
        ]
        result = mock_psycopg2["fetch"]("SELECT * FROM t")
        assert result == [{"id": 1, "name": "foo"}]

    def test_fetch_empty(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        mock_psycopg2["mock_cur"].fetchall.return_value = []
        result = mock_psycopg2["fetch"]("SELECT * FROM t WHERE 1=0")
        assert result == []


class TestFetchOne:
    def test_fetchone_returns_row(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        mock_psycopg2["mock_cur"].fetchone.return_value = {"id": 1}
        result = mock_psycopg2["fetchone"]("SELECT * FROM t WHERE id=%s", 1)
        assert result == {"id": 1}

    def test_fetchone_returns_none(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        mock_psycopg2["mock_cur"].fetchone.return_value = None
        result = mock_psycopg2["fetchone"]("SELECT * FROM t WHERE 1=0")
        assert result is None


class TestFetchVal:
    def test_fetchval_returns_value(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        mock_psycopg2["mock_cur"].fetchone.return_value = (42,)
        result = mock_psycopg2["fetchval"]("SELECT count(*) FROM t")
        assert result == 42

    def test_fetchval_no_rows(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        mock_psycopg2["mock_cur"].fetchone.return_value = None
        result = mock_psycopg2["fetchval"]("SELECT max(id) FROM t")
        assert result is None


class TestTransaction:
    def test_transaction_commits(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        with mock_psycopg2["transaction"]() as conn:
            conn.cursor().execute("INSERT INTO t VALUES (1)")
        mock_psycopg2["mock_conn"].commit.assert_called_once()
        mock_psycopg2["mock_conn"].close.assert_called_once()

    def test_transaction_rollback_on_error(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        with pytest.raises(ValueError):
            with mock_psycopg2["transaction"]() as conn:
                conn.cursor().execute("INSERT INTO t VALUES (1)")
                raise ValueError("fail")
        mock_psycopg2["mock_conn"].rollback.assert_called_once()
        mock_psycopg2["mock_conn"].close.assert_called_once()

    def test_transaction_sets_autocommit_false(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        with mock_psycopg2["transaction"]():
            pass
        assert mock_psycopg2["mock_conn"].autocommit is False


class TestRetry:
    def test_retry_succeeds_first_time(self, mock_psycopg2):
        fn = MagicMock(return_value="ok")
        result = mock_psycopg2["_retry"](fn)
        assert result == "ok"
        fn.assert_called_once()

    def test_retry_eventually_succeeds(self, mock_psycopg2):
        fn = MagicMock()
        fn.side_effect = [
            __import__("psycopg2").OperationalError("fail"),
            __import__("psycopg2").OperationalError("fail"),
            "ok",
        ]
        result = mock_psycopg2["_retry"](fn)
        assert result == "ok"

    def test_retry_non_retryable_raises(self, mock_psycopg2):
        fn = MagicMock(side_effect=ValueError("bad"))
        with pytest.raises(ValueError, match="bad"):
            mock_psycopg2["_retry"](fn)

    def test_retry_exhausted_raises(self, mock_psycopg2):
        import utils.db as _db

        _db._MAX_RETRIES = 2
        _db._RETRY_DELAY = 0.001
        fn = MagicMock(side_effect=__import__("psycopg2").OperationalError("persistent"))
        with pytest.raises(__import__("psycopg2").OperationalError):
            mock_psycopg2["_retry"](fn)


class TestAsyncAPI:
    @pytest.mark.asyncio
    async def test_async_execute(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        mock_psycopg2["mock_cur"].statusmessage = "INSERT 0 1"
        result = await mock_psycopg2["async_execute"]("INSERT INTO t VALUES (%s)", 1)
        assert result == "INSERT 0 1"

    @pytest.mark.asyncio
    async def test_async_fetch(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        mock_psycopg2["mock_cur"].fetchall.return_value = [{"id": 1}]
        result = await mock_psycopg2["async_fetch"]("SELECT * FROM t")
        assert result == [{"id": 1}]

    @pytest.mark.asyncio
    async def test_async_fetchone(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        mock_psycopg2["mock_cur"].fetchone.return_value = {"id": 1}
        result = await mock_psycopg2["async_fetchone"]("SELECT * FROM t WHERE id=%s", 1)
        assert result == {"id": 1}

    @pytest.mark.asyncio
    async def test_async_fetchval(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        mock_psycopg2["mock_cur"].fetchone.return_value = (42,)
        result = await mock_psycopg2["async_fetchval"]("SELECT count(*) FROM t")
        assert result == 42


class TestAsyncTransaction:
    @pytest.mark.asyncio
    async def test_async_transaction_commits(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        async with mock_psycopg2["async_transaction"]() as wrapper:
            await wrapper.execute("INSERT INTO t VALUES (1)")
        mock_psycopg2["mock_conn"].commit.assert_called_once()
        mock_psycopg2["mock_conn"].close.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_transaction_rollback(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        with pytest.raises(ValueError):
            async with mock_psycopg2["async_transaction"]() as wrapper:
                await wrapper.execute("INSERT INTO t VALUES (1)")
                raise ValueError("fail")
        mock_psycopg2["mock_conn"].rollback.assert_called_once()
        mock_psycopg2["mock_conn"].close.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_connection_wrapper_fetch(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        async with mock_psycopg2["async_transaction"]() as wrapper:
            mock_psycopg2["mock_cur"].fetchall.return_value = [{"id": 1}]
            result = await wrapper.fetch("SELECT * FROM t")
            assert result == [{"id": 1}]

    @pytest.mark.asyncio
    async def test_async_connection_wrapper_fetchrow(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        async with mock_psycopg2["async_transaction"]() as wrapper:
            mock_psycopg2["mock_cur"].fetchone.return_value = {"id": 1}
            result = await wrapper.fetchrow("SELECT * FROM t WHERE id=%s", 1)
            assert result == {"id": 1}

    @pytest.mark.asyncio
    async def test_async_connection_wrapper_fetchrow_none(self, mock_psycopg2):
        mock_psycopg2["configure"]("dsn")
        async with mock_psycopg2["async_transaction"]() as wrapper:
            mock_psycopg2["mock_cur"].fetchone.return_value = None
            result = await wrapper.fetchrow("SELECT * FROM t WHERE 1=0")
            assert result is None
