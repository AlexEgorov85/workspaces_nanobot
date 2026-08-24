"""SessionManager/Session: файловое хранилище сессий (база PGSessionManager)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.contract


def test_session_roundtrip(tmp_path) -> None:
    from nanobot.session.manager import SessionManager

    manager = SessionManager(tmp_path)
    session = manager.get_or_create("cli:direct")
    assert session is not None

    session.add_message("user", "q")
    session.add_message("assistant", "a")

    history = session.get_history(max_messages=10)
    assert isinstance(history, list)

    manager.save(session)


def test_list_sessions_empty(tmp_path) -> None:
    from nanobot.session.manager import SessionManager

    manager = SessionManager(tmp_path)
    assert isinstance(manager.list_sessions(), list)


def test_message_preview_text() -> None:
    from nanobot.session.manager import _message_preview_text

    result = _message_preview_text({"role": "user", "content": "hi"})
    assert isinstance(result, str)
