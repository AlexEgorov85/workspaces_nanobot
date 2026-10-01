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
from config import runtime_table  # noqa: F401

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
                meta_table=runtime_table("session_meta"),
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


class TestCleanSessionContent:
    """Санитизация NUL на границе записи.

    Нативная замена патча ``patch_session_content_cleanup``, который
    оборачивал ``Session.add_message`` (change
    ``enterprise-mcp-platform``, фаза 6, п. 6.5). Санитизация переехала
    на границу записи — ``PGSessionManager.save`` вызывает
    ``clean_session_content`` перед делегированием в upstream.

    Проверяется на заведомо плохих данных: мусорные сообщения, NUL,
    литеральные ``\\u0000``, отсутствующие атрибуты. Страж, который ни
    разу не срабатывал, неотличим от стража, который ничего не проверяет.
    """

    @staticmethod
    def _session(messages):
        from nanobot.session.manager import Session

        return Session(key="s1", messages=messages)

    def test_strips_real_nul(self) -> None:
        from lib.session.pg_session_manager import clean_session_content

        session = self._session([{"role": "tool", "content": "a\x00b"}])
        clean_session_content(session)
        assert session.messages[0]["content"] == "ab"

    def test_strips_literal_unicode_escapes(self) -> None:
        """psycopg2 не может отправить литеральные ``\\u0000``..``\\u0003``."""
        from lib.session.pg_session_manager import clean_session_content

        session = self._session([{"role": "tool", "content": "a\\u0000b\\u0003c"}])
        clean_session_content(session)
        assert session.messages[0]["content"] == "abc"

    def test_keeps_ordinary_whitespace_and_unicode(self) -> None:
        """Санитизация не должна съедать переводы строк и кириллицу."""
        from lib.session.pg_session_manager import clean_session_content

        payload = "строка 1\n\tстрока 2\r\nпусто"
        session = self._session([{"role": "user", "content": payload}])
        clean_session_content(session)
        assert session.messages[0]["content"] == payload

    def test_idempotent(self) -> None:
        from lib.session.pg_session_manager import clean_session_content

        session = self._session([{"role": "tool", "content": "a\x00b"}])
        clean_session_content(session)
        first = session.messages[0]["content"]
        clean_session_content(session)
        assert session.messages[0]["content"] == first == "ab"

    def test_mutates_in_place(self) -> None:
        """Сессия мутируется на месте — снимок не делается."""
        from lib.session.pg_session_manager import clean_session_content

        session = self._session([{"role": "tool", "content": "a\x00b"}])
        clean_session_content(session)
        assert session.messages[0]["content"] == "ab"

    def test_non_dict_messages_skipped(self) -> None:
        """Не-dict сообщения (битые данные) пропускаются, а не роняют save."""
        from lib.session.pg_session_manager import clean_session_content

        session = self._session(["строка-вместо-dict", None, 42])
        clean_session_content(session)  # не должно бросить
        assert session.messages == ["строка-вместо-dict", None, 42]

    def test_message_without_content_key_untouched(self) -> None:
        from lib.session.pg_session_manager import clean_session_content

        session = self._session([{"role": "user"}])
        clean_session_content(session)
        assert session.messages[0] == {"role": "user"}

    def test_none_content_does_not_crash(self) -> None:
        from lib.session.pg_session_manager import clean_session_content

        session = self._session([{"role": "tool", "content": None}])
        clean_session_content(session)
        assert session.messages[0]["content"] is None

    def test_non_list_messages_does_not_crash(self) -> None:
        """``messages`` не список — страж молча выходит."""
        from lib.session.pg_session_manager import clean_session_content

        session = self._session([])
        session.messages = "не список"  # type: ignore[assignment]
        clean_session_content(session)  # не должно бросить
        assert session.messages == "не список"

    def test_nested_content_is_cleaned_recursively(self) -> None:
        """Вложенные структуры внутри ``content`` тоже вычищаются."""
        from lib.session.pg_session_manager import clean_session_content

        session = self._session(
            [{"role": "tool", "content": {"text": "a\x00b", "parts": ["c\x00d"]}}]
        )
        clean_session_content(session)
        content = session.messages[0]["content"]
        assert content["text"] == "ab"
        assert content["parts"] == ["cd"]


class TestSaveCleansContent:
    """``PGSessionManager.save`` вычищает контент до записи на диск.

    Файлы сессий пишутся upstream-стором в runtime-каталог сессий
    (``JsonlSessionStore`` игнорирует ``workspace`` как корень хранилища),
    поэтому читать надо из ``mgr.sessions_dir``, а файлы удалять после
    теста — иначе тесты засоряют общий runtime-каталог.
    """

    @staticmethod
    def _manager(tmp_path: Path):
        from lib.session.pg_session_manager import PGSessionManager

        return PGSessionManager(
            workspace=tmp_path,
            messages_table=runtime_table("session_messages"),
            meta_table=runtime_table("session_meta"),
        )

    @staticmethod
    def _session_file(mgr, key: str) -> Path:
        return mgr._jsonl_store.get_session_path(key)

    def test_save_writes_clean_content(self, tmp_path: Path) -> None:
        from nanobot.session.manager import Session

        mgr = self._manager(tmp_path)
        path = self._session_file(mgr, "clean-1")
        try:
            session = Session(key="clean-1", messages=[])
            session.add_message("tool", "плохой\x00текст")

            mgr.save(session)

            assert path.exists(), f"JSONL не создан: {path}"
            on_disk = path.read_text(encoding="utf-8")
            assert "плохойтекст" in on_disk
            assert "\x00" not in on_disk
        finally:
            path.unlink(missing_ok=True)

    def test_save_delegates_to_upstream(self, tmp_path: Path) -> None:
        """Запись по-прежнему идёт в upstream JSONL (никакого hot-path SQL).

        Архитектурный инвариант «нет прямого SQL в hot path» проверяется
        отдельно и целиком в ``test_storage_hybridization.py``
        (``TestNoDirectSQLToSessionTables``); здесь достаточно убедиться,
        что санитизация не подменила путь записи.
        """
        from nanobot.session.manager import Session

        mgr = self._manager(tmp_path)
        path = self._session_file(mgr, "clean-2")
        try:
            session = Session(key="clean-2", messages=[])
            session.add_message("user", "привет")

            mgr.save(session)

            assert path.exists(), f"upstream JSONL не создан: {path}"
            assert "привет" in path.read_text(encoding="utf-8")
        finally:
            path.unlink(missing_ok=True)

    def test_save_cleans_whole_session(self, tmp_path: Path) -> None:
        """Все сообщения сессии вычищаются, а не только последнее."""
        from nanobot.session.manager import Session

        mgr = self._manager(tmp_path)
        path = self._session_file(mgr, "clean-3")
        try:
            session = Session(key="clean-3", messages=[])
            session.add_message("tool", "первый\x00")
            session.add_message("tool", "второй\\u0001")
            session.add_message("user", "третий")

            mgr.save(session)

            assert path.exists(), f"JSONL не создан: {path}"
            on_disk = path.read_text(encoding="utf-8")
            assert "первый" in on_disk and "второй" in on_disk and "третий" in on_disk
            assert "\x00" not in on_disk
            assert "\\u0001" not in on_disk
        finally:
            path.unlink(missing_ok=True)