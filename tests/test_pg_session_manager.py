from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add workspace to sys.path so utils.db can be imported
_project_root = Path(__file__).resolve().parent.parent
_workspace_path = str(_project_root / "workspace")
if _workspace_path not in sys.path:
    sys.path.insert(0, _workspace_path)


@pytest.fixture(autouse=True)
def mock_db_and_psycopg():
    """Mock utils.db and psycopg2.extras before importing pg_session_manager."""
    with (
        patch.dict("sys.modules"),
        patch("psycopg2.extras.Json", lambda x: x),
        patch("psycopg2.extras.execute_values"),
    ):
        # Force fresh import of pg_session_manager to pick up mocked deps
        sys.modules.pop("lib.session.pg_session_manager", None)
        sys.modules.pop("lib.session", None)

        import types

        utils_db = types.ModuleType("utils.db")
        utils_db.DB_RETRYABLE_ERRORS = (Exception,)
        utils_db.transaction = MagicMock()
        sys.modules["utils.db"] = utils_db
        sys.modules["utils"] = types.ModuleType("utils")

        from lib.session.pg_session_manager import PGSessionManager

        def _make(**kwargs):
            defaults = {
                "messages_table": kwargs.pop("messages_table", "agent_session_messages"),
                "meta_table": kwargs.pop("meta_table", "agent_session_meta"),
            }
            kwargs.setdefault("workspace", Path("/tmp/ws"))
            return PGSessionManager(workspace=kwargs.pop("workspace"), **defaults, **kwargs)

        yield _make


class TestPGSessionManagerPure:
    """Tests for pure (static) methods that don't need DB."""

    def test_safe_key(self):
        from lib.session.pg_session_manager import PGSessionManager

        assert PGSessionManager.safe_key("hello:world") != ""
        assert ":" not in PGSessionManager.safe_key("test:key")
        assert PGSessionManager.safe_key("simple") == "simple"

    def test_validate_ident_valid(self):
        from lib.session.pg_session_manager import PGSessionManager

        PGSessionManager._validate_ident("public")  # no error
        PGSessionManager._validate_ident("session_meta")
        PGSessionManager._validate_ident("a1$b2")

    def test_validate_ident_invalid(self):
        from lib.session.pg_session_manager import PGSessionManager

        with pytest.raises(ValueError, match="Unsafe SQL identifier"):
            PGSessionManager._validate_ident("")
        with pytest.raises(ValueError, match="Unsafe SQL identifier"):
            PGSessionManager._validate_ident("table; DROP")
        with pytest.raises(ValueError, match="Unsafe SQL identifier"):
            PGSessionManager._validate_ident("a-b")

    def test_quote_simple(self):
        from lib.session.pg_session_manager import PGSessionManager

        assert PGSessionManager._quote("public.session_meta") == '"public"."session_meta"'

    def test_quote_single(self):
        from lib.session.pg_session_manager import PGSessionManager

        assert PGSessionManager._quote("session_meta") == '"session_meta"'

    def test_quote_invalid_raises(self):
        from lib.session.pg_session_manager import PGSessionManager

        with pytest.raises(ValueError):
            PGSessionManager._quote("public;.table")

    def test_session_payload(self):
        from lib.session.pg_session_manager import PGSessionManager, Session

        session = Session(
            key="test-key",
            messages=[{"role": "user", "content": "hi"}],
        )
        payload = PGSessionManager._session_payload(session)
        assert payload["key"] == "test-key"
        assert len(payload["messages"]) == 1
        assert "created_at" in payload
        assert "updated_at" in payload
        assert "metadata" in payload

    def test_init_defaults(self, mock_db_and_psycopg):
        mgr = mock_db_and_psycopg(workspace=Path("/tmp/ws"))
        assert mgr.workspace == Path("/tmp/ws").resolve()
        assert mgr._schema == "public"
        assert mgr._cache == {}

    def test_init_custom_schema(self, mock_db_and_psycopg):
        mgr = mock_db_and_psycopg(
            workspace=Path("/tmp/ws"),
            schema="custom",
            messages_table="msgs",
            meta_table="meta",
        )
        assert '"custom"."msgs"' in mgr._fq_messages
        assert '"custom"."meta"' in mgr._fq_meta

    def test_close_noop(self, mock_db_and_psycopg):
        mgr = mock_db_and_psycopg(workspace=Path("/tmp/ws"))
        mgr.close()  # should not raise

    def test_get_session_path(self, mock_db_and_psycopg):
        mgr = mock_db_and_psycopg(workspace=Path("/tmp/ws"))
        path = mgr._get_session_path("my-key")
        assert "my-key" in str(path)
        assert str(path).endswith(".jsonl")


class TestPGSessionManagerGetOrCreate:
    def test_returns_cached(self, mock_db_and_psycopg):
        from lib.session.pg_session_manager import Session

        mgr = mock_db_and_psycopg(workspace=Path("/tmp/ws"))
        session = Session(key="cached-key")
        mgr._cache["cached-key"] = session
        assert mgr.get_or_create("cached-key") is session

    @patch("lib.session.pg_session_manager.transaction")
    def test_creates_new_when_not_found(self, mock_trans, mock_db_and_psycopg):
        mgr = mock_db_and_psycopg(workspace=Path("/tmp/ws"))
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.__enter__.return_value = mock_cur
        mock_cur.fetchone.return_value = None  # no meta row
        mock_conn.cursor.return_value = mock_cur
        mock_trans.return_value.__enter__.return_value = mock_conn

        session = mgr.get_or_create("new-key")
        assert session.key == "new-key"
        assert mgr._cache["new-key"] is session


class TestPGSessionManagerSave:
    @patch("lib.session.pg_session_manager.transaction")
    @patch("lib.session.pg_session_manager.execute_values")
    def test_save_new_session(self, mock_exec_vals, mock_trans, mock_db_and_psycopg):
        from lib.session.pg_session_manager import Session

        mgr = mock_db_and_psycopg(workspace=Path("/tmp/ws"))
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.rowcount = 0  # UPDATE found no rows → will INSERT
        mock_conn.cursor = MagicMock(return_value=mock_cur)
        mock_trans.return_value.__enter__.return_value = mock_conn

        session = Session(key="s1", messages=[{"role": "user", "content": "hi"}])
        mgr.save(session)

        # Should call execute_values for messages
        mock_exec_vals.assert_called_once()

    @patch("lib.session.pg_session_manager.transaction")
    @patch("lib.session.pg_session_manager.execute_values")
    def test_save_existing_session(self, mock_exec_vals, mock_trans, mock_db_and_psycopg):
        from lib.session.pg_session_manager import Session

        mgr = mock_db_and_psycopg(workspace=Path("/tmp/ws"))
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.rowcount = 1  # UPDATE succeeded
        mock_conn.cursor.return_value = mock_cur
        mock_trans.return_value.__enter__.return_value = mock_conn

        session = Session(key="s1")
        mgr.save(session)
        # Should only call UPDATE, no INSERT
        insert_calls = [
            c for c in mock_cur.execute.call_args_list
            if "INSERT" in str(c)
        ]
        assert len(insert_calls) == 0

    @patch("lib.session.pg_session_manager.transaction")
    def test_save_db_fallback(self, mock_trans, mock_db_and_psycopg):
        from lib.session.pg_session_manager import Session

        mgr = mock_db_and_psycopg(workspace=Path("/tmp/ws"))
        mock_trans.side_effect = Exception("DB down")

        with patch.object(mgr, "save", wraps=mgr.save) as spy:
            # We need to test that fallback to super().save() happens
            # Since we mock the parent's save, just verify the DB error path
            session = Session(key="s1")
            with patch.object(type(mgr).__bases__[0], "save") as super_save:
                mgr.save(session)
                super_save.assert_called_once()


class TestPGSessionManagerDelete:
    @patch("lib.session.pg_session_manager.transaction")
    def test_delete_success(self, mock_trans, mock_db_and_psycopg):
        mgr = mock_db_and_psycopg(workspace=Path("/tmp/ws"))
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.rowcount = 1
        # cursor() called twice: first for messages DELETE, then for meta DELETE
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_trans.return_value.__enter__.return_value = mock_conn

        mgr._cache["del-key"] = "dummy"
        result = mgr.delete_session("del-key")
        assert result is True
        assert "del-key" not in mgr._cache

    @patch("lib.session.pg_session_manager.transaction")
    def test_delete_not_found(self, mock_trans, mock_db_and_psycopg):
        mgr = mock_db_and_psycopg(workspace=Path("/tmp/ws"))
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.rowcount = 0
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_trans.return_value.__enter__.return_value = mock_conn

        result = mgr.delete_session("non-existent")
        assert result is False


class TestPGSessionManagerList:
    @patch("lib.session.pg_session_manager.transaction")
    def test_list_sessions_empty(self, mock_trans, mock_db_and_psycopg):
        mgr = mock_db_and_psycopg(workspace=Path("/tmp/ws"))
        mock_conn = MagicMock()

        mock_cur1 = MagicMock()
        mock_cur1.description = [("session_key",), ("created_at",), ("updated_at",), ("metadata",)]
        mock_cur1.fetchall.return_value = []
        mock_cur1.__enter__.return_value = mock_cur1

        mock_conn.cursor.return_value.__enter__.return_value = mock_cur1
        mock_trans.return_value.__enter__.return_value = mock_conn

        result = mgr.list_sessions()
        assert result == []

    @patch("lib.session.pg_session_manager.transaction")
    def test_list_sessions_with_data(self, mock_trans, mock_db_and_psycopg):
        mgr = mock_db_and_psycopg(workspace=Path("/tmp/ws"))
        mock_conn = MagicMock()

        now = datetime(2024, 1, 1, 12, 0, 0)

        mock_cur1 = MagicMock()
        mock_cur1.__enter__.return_value = mock_cur1
        mock_cur1.description = [
            ("session_key",), ("created_at",), ("updated_at",), ("metadata",)
        ]
        mock_cur1.fetchall.return_value = [
            ("s1", now, now, '{"title": "Chat 1"}'),
        ]

        mock_cur2 = MagicMock()
        mock_cur2.__enter__.return_value = mock_cur2
        mock_cur2.__iter__.return_value = iter([("user", "Hello!")])

        mock_conn.cursor.side_effect = [mock_cur1, mock_cur2]
        mock_trans.return_value.__enter__.return_value = mock_conn

        result = mgr.list_sessions()
        assert len(result) == 1
        assert result[0]["key"] == "s1"
        assert result[0]["title"] == "Chat 1"
        assert result[0]["preview"] == "Hello!"


class TestPGSessionManagerInvalidate:
    def test_invalidate_removes_from_cache(self, mock_db_and_psycopg):
        from lib.session.pg_session_manager import Session

        mgr = mock_db_and_psycopg(workspace=Path("/tmp/ws"))
        mgr._cache["k"] = Session(key="k")
        mgr.invalidate("k")
        assert "k" not in mgr._cache

    def test_invalidate_missing(self, mock_db_and_psycopg):
        mgr = mock_db_and_psycopg(workspace=Path("/tmp/ws"))
        mgr.invalidate("nonexistent")  # should not raise


class TestPGSessionManagerFlushAll:
    def test_flush_all_empty(self, mock_db_and_psycopg):
        mgr = mock_db_and_psycopg(workspace=Path("/tmp/ws"))
        assert mgr.flush_all() == 0

    def test_flush_all_cached(self, mock_db_and_psycopg):
        from lib.session.pg_session_manager import Session

        mgr = mock_db_and_psycopg(workspace=Path("/tmp/ws"))
        mgr._cache = {"a": Session(key="a"), "b": Session(key="b")}
        with patch.object(mgr, "save") as mock_save:
            count = mgr.flush_all()
            assert count == 2
            assert mock_save.call_count == 2


class TestPGSessionManagerReadSessionFile:
    @patch("lib.session.pg_session_manager.PGSessionManager._load")
    def test_read_session_file_found(self, mock_load, mock_db_and_psycopg):
        from lib.session.pg_session_manager import Session

        mgr = mock_db_and_psycopg(workspace=Path("/tmp/ws"))
        mock_load.return_value = Session(key="k", messages=[{"role": "user", "content": "hi"}])
        result = mgr.read_session_file("k")
        assert result is not None
        assert result["key"] == "k"

    @patch("lib.session.pg_session_manager.PGSessionManager._load")
    def test_read_session_file_not_found(self, mock_load, mock_db_and_psycopg):
        mgr = mock_db_and_psycopg(workspace=Path("/tmp/ws"))
        mock_load.return_value = None
        assert mgr.read_session_file("k") is None
