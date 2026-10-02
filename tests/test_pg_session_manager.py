"""Тесты хранилища сессий: ``SanitizingSessionStore`` + ``build_session_manager``.

Менеджер сессий — класс библиотеки ``nanobot.session.manager.SessionManager``.
Собственная семантика агента живёт в слое ``SessionStore``
(``SanitizingSessionStore``): санитизация NUL/control-символов на границе
записи. Никаких прямых SQL-операций в ``agent_session_meta`` /
``agent_session_messages`` в hot path (см. design D6 / R6 и
``tests/test_storage_hybridization.py``).

Эти тесты проверяют:

- ``build_session_manager`` возвращает ИМЕННО класс библиотеки и один
  store на обеих ссылках (``_store is _jsonl_store`` — от него зависит
  fast-path ``save_runtime_checkpoint``);
- санитизация происходит до записи на диск;
- архитектурный инвариант «no direct SQL» (см. AST-проход в
  ``test_storage_hybridization.py``).
"""

from __future__ import annotations

import sys
from pathlib import Path

from config import runtime_table  # noqa: F401

_project_root = Path(__file__).resolve().parent.parent
_workspace_path = str(_project_root / "workspace")
if _workspace_path not in sys.path:
    sys.path.insert(0, _workspace_path)


class TestBuildSessionManager:
    """Менеджер берётся из библиотеки; наш вклад — только store-слой."""

    def test_returns_library_session_manager(self, tmp_path: Path) -> None:
        from nanobot.session.manager import SessionManager

        from lib.session.pg_session_manager import build_session_manager

        mgr = build_session_manager(tmp_path)
        assert type(mgr) is SessionManager, (
            "менеджер сессий должен быть классом библиотеки, а не подклассом"
        )

    def test_store_is_sanitizing_store(self, tmp_path: Path) -> None:
        from lib.session.pg_session_manager import (
            SanitizingSessionStore,
            build_session_manager,
        )

        mgr = build_session_manager(tmp_path)
        assert isinstance(mgr._store, SanitizingSessionStore)

    def test_store_and_jsonl_store_are_the_same_object(self, tmp_path: Path) -> None:
        """Fast-path ``save_runtime_checkpoint`` зависит от тождества.

        ``SessionManager.save_runtime_checkpoint`` (manager.py:1798)
        деградирует до полной перезаписи транскрипта, если
        ``self._store is not self._jsonl_store``. Проверяем на заведомо
        плохих данных: страж, который ни разу не срабатывал, неотличим
        от стража, который ничего не проверяет.
        """
        from lib.session.pg_session_manager import build_session_manager

        mgr = build_session_manager(tmp_path)
        assert mgr._store is mgr._jsonl_store, (
            "save_runtime_checkpoint потерял fast-path: store и _jsonl_store "
            "разошлись"
        )

    def test_single_store_instance(self, tmp_path: Path) -> None:
        """Ровно один store: дубли не должны плодить лишние миграции."""
        from lib.session.pg_session_manager import build_session_manager

        mgr = build_session_manager(tmp_path)
        assert mgr._jsonl_store is mgr._store


class TestCleanSessionContent:
    """Санитизация NUL на границе записи.

    Нативная замена патча ``patch_session_content_cleanup``, который
    оборачивал ``Session.add_message`` (change
    ``enterprise-mcp-platform``, фаза 6, п. 6.5). Санитизация переехала
    → в ``SanitizingSessionStore.save``.

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
    """``SanitizingSessionStore.save`` вычищает контент до записи на диск.

    Файлы сессий пишутся upstream-стором в runtime-каталог сессий
    (``JsonlSessionStore`` игнорирует ``workspace`` как корень хранилища),
    поэтому читать надо из ``mgr.sessions_dir``, а файлы удалять после
    теста — иначе тесты засоряют общий runtime-каталог.
    """

    @staticmethod
    def _manager(tmp_path: Path):
        from lib.session.pg_session_manager import build_session_manager

        return build_session_manager(tmp_path)

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

    def test_save_via_store_api_also_cleans(self, tmp_path: Path) -> None:
        """Санитизация живёт в store-слое, а не в менеджере.

        Вызываем ``store.save()`` напрямую (как это делает
        ``SessionManager.save``) — NUL всё равно не должен попасть
        на диск. Проверяет, что семантика не «приклеена» к подклассу
        менеджера.
        """
        from nanobot.session.manager import Session

        mgr = self._manager(tmp_path)
        store = mgr._store
        path = self._session_file(mgr, "clean-4")
        try:
            session = Session(key="clean-4", messages=[])
            session.add_message("tool", "через-store\x00")

            store.save(session, fsync=False)

            assert path.exists(), f"JSONL не создан: {path}"
            assert "через-store" in path.read_text(encoding="utf-8")
            assert "\x00" not in path.read_text(encoding="utf-8")
        finally:
            path.unlink(missing_ok=True)
