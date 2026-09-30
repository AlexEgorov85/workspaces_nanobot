"""Тесты ``PGSessionManager`` как compatibility layer (post-storage-hybridization).

После change ``storage-hybridization`` ``PGSessionManager`` — это
тонкая обёртка над upstream ``SessionManager``. Hot-path методы
делегируются в ``super()``; никаких прямых SQL-операций в
``agent_session_meta`` / ``agent_session_messages`` в hot path
(см. design D6 / R6 и ``tests/test_storage_hybridization.py``).

Эти тесты проверяют:

- конструктор сохраняет параметры (``dsn``, ``schema``,
  ``messages_table``, ``meta_table``);
- ``get_or_create`` / ``save`` / ``list_sessions`` /
  ``read_session_metadata`` / ``read_session_file`` /
  ``delete_session`` делегируются в upstream ``SessionManager``;
- ``flush_all()`` — no-op (upstream сам управляет JSONL);
- архитектурный инвариант «no direct SQL» (см. AST-проход в
  ``test_storage_hybridization.py``).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_project_root = Path(__file__).resolve().parent.parent
_workspace_path = str(_project_root / "workspace")
if _workspace_path not in sys.path:
    sys.path.insert(0, _workspace_path)


@pytest.fixture(autouse=True)
def mock_utils_db():
    """Mock ``utils.db`` для тестов, которым он не нужен."""
    with patch.dict("sys.modules"), patch("utils.db.configure", MagicMock()):
        yield


class TestPGSessionManagerInit:
    def test_init_requires_meta_and_messages_tables(self, tmp_path: Path) -> None:
        from lib.session.pg_session_manager import PGSessionManager

        with pytest.raises(ValueError, match="messages_table"):
            PGSessionManager(
                workspace=tmp_path,
                messages_table="",
                meta_table="agent_session_meta",
            )

    def test_init_accepts_constructor_params(self, tmp_path: Path) -> None:
        from lib.session.pg_session_manager import PGSessionManager

        mgr = PGSessionManager(
            workspace=tmp_path,
            schema="custom",
            messages_table="msgs",
            meta_table="meta",
            dsn="postgresql://x",
        )
        assert mgr._schema == "custom"
        assert mgr._meta_table == "meta"
        assert mgr._messages_table == "msgs"
        assert '"custom"."meta"' in mgr._fq_meta
        assert '"custom"."msgs"' in mgr._fq_messages

    def test_init_default_schema(self, tmp_path: Path) -> None:
        from lib.session.pg_session_manager import PGSessionManager

        mgr = PGSessionManager(
            workspace=tmp_path,
            messages_table="m",
            meta_table="t",
        )
        assert mgr._schema == "public"


class TestPGSessionManagerAsMirror:
    """Главный контракт: hot-path делегируется в upstream ``SessionManager``."""

    def test_super_get_or_create_delegates_to_upstream(self, tmp_path: Path) -> None:
        from lib.session.pg_session_manager import PGSessionManager

        mgr = PGSessionManager(
            workspace=tmp_path,
            messages_table="m",
            meta_table="t",
        )
        with patch.object(PGSessionManager.__bases__[0], "get_or_create",
                          return_value="UPSTREAM_SESSION") as mock_super:
            result = mgr.get_or_create("k1")
        assert result == "UPSTREAM_SESSION"
        mock_super.assert_called_once_with("k1")

    def test_super_save_delegates_to_upstream(self, tmp_path: Path) -> None:
        from lib.session.pg_session_manager import PGSessionManager, Session

        mgr = PGSessionManager(
            workspace=tmp_path,
            messages_table="m",
            meta_table="t",
        )
        session = Session(key="k1")
        with patch.object(PGSessionManager.__bases__[0], "save") as mock_super:
            mgr.save(session, fsync=True)
        mock_super.assert_called_once_with(session, fsync=True)

    def test_super_list_sessions_delegates_to_upstream(self, tmp_path: Path) -> None:
        from lib.session.pg_session_manager import PGSessionManager

        mgr = PGSessionManager(
            workspace=tmp_path,
            messages_table="m",
            meta_table="t",
        )
        with patch.object(PGSessionManager.__bases__[0], "list_sessions",
                          return_value=[{"key": "a"}]) as mock_super:
            result = mgr.list_sessions()
        assert result == [{"key": "a"}]
        mock_super.assert_called_once_with()

    def test_super_read_session_metadata_delegates_to_upstream(self, tmp_path: Path) -> None:
        from lib.session.pg_session_manager import PGSessionManager

        mgr = PGSessionManager(
            workspace=tmp_path,
            messages_table="m",
            meta_table="t",
        )
        with patch.object(PGSessionManager.__bases__[0],
                          "read_session_metadata",
                          return_value={"key": "k"}) as mock_super:
            result = mgr.read_session_metadata("k")
        assert result == {"key": "k"}
        mock_super.assert_called_once_with("k")

    def test_super_read_session_file_delegates_to_upstream(self, tmp_path: Path) -> None:
        from lib.session.pg_session_manager import PGSessionManager

        mgr = PGSessionManager(
            workspace=tmp_path,
            messages_table="m",
            meta_table="t",
        )
        with patch.object(PGSessionManager.__bases__[0], "read_session_file",
                          return_value={"payload": True}) as mock_super:
            result = mgr.read_session_file("k")
        assert result == {"payload": True}
        mock_super.assert_called_once_with("k")

    def test_super_delete_session_delegates_to_upstream(self, tmp_path: Path) -> None:
        from lib.session.pg_session_manager import PGSessionManager

        mgr = PGSessionManager(
            workspace=tmp_path,
            messages_table="m",
            meta_table="t",
        )
        with patch.object(PGSessionManager.__bases__[0], "delete_session",
                          return_value=True) as mock_super:
            result = mgr.delete_session("k")
        assert result is True
        mock_super.assert_called_once_with("k")


class TestPGSessionManagerNoOps:
    def test_flush_all_returns_zero(self, tmp_path: Path) -> None:
        from lib.session.pg_session_manager import PGSessionManager

        mgr = PGSessionManager(
            workspace=tmp_path,
            messages_table="m",
            meta_table="t",
        )
        assert mgr.flush_all() == 0

    def test_invalidate_is_noop(self, tmp_path: Path) -> None:
        from lib.session.pg_session_manager import PGSessionManager

        mgr = PGSessionManager(
            workspace=tmp_path,
            messages_table="m",
            meta_table="t",
        )
        assert mgr.invalidate("anything") is None

    def test_close_is_noop(self, tmp_path: Path) -> None:
        from lib.session.pg_session_manager import PGSessionManager

        mgr = PGSessionManager(
            workspace=tmp_path,
            messages_table="m",
            meta_table="t",
        )
        assert mgr.close() is None


class TestPGSessionManagerSQLSafety:
    def test_quote_simple(self) -> None:
        from lib.session.pg_session_manager import PGSessionManager

        assert (
            PGSessionManager._quote("public.session_meta")
            == '"public"."session_meta"'
        )

    def test_quote_invalid_raises(self) -> None:
        from lib.session.pg_session_manager import PGSessionManager

        with pytest.raises(ValueError):
            PGSessionManager._quote("public;.table")

    def test_validate_ident_valid(self) -> None:
        from lib.session.pg_session_manager import PGSessionManager

        PGSessionManager._validate_ident("public")
        PGSessionManager._validate_ident("a1$b2")

    def test_validate_ident_invalid(self) -> None:
        from lib.session.pg_session_manager import PGSessionManager

        with pytest.raises(ValueError, match="Unsafe SQL identifier"):
            PGSessionManager._validate_ident("")
        with pytest.raises(ValueError, match="Unsafe SQL identifier"):
            PGSessionManager._validate_ident("a-b")


class TestPGSessionManagerDocstring:
    def test_docstring_says_mirror(self) -> None:
        from lib.session.pg_session_manager import PGSessionManager

        doc = PGSessionManager.__doc__ or ""
        assert "mirror" in doc
        assert "upstream" in doc
        assert "hot-path" in doc.lower() or "hot path" in doc.lower()