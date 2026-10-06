"""Contract tests on upstream `SessionManager` API (nanobot 0.3.5).

Фиксирует публичный API, используемый в storage-hybridization
(см. ``openspec/changes/storage-hybridization/specs/sessions/session-hybridization``):
имена методов, сигнатуры, поведение default-стора (``JsonlSessionStore``).

Тесты MUST падать при несовместимом изменении upstream API —
это страховка от регрессий при следующих минорных апдейдах nanobot.

Запускается первым в ``tests/contract`` через ``openspec.cmd validate``.
"""

from __future__ import annotations

import inspect
from typing import Callable

import pytest

import nanobot.agent  # noqa: F401 — фикс import-order

pytestmark = pytest.mark.contract


def _has_method(cls: type, name: str) -> bool:
    return callable(getattr(cls, name, None))


def _signature(cls: type, name: str) -> inspect.Signature:
    return inspect.signature(getattr(cls, name))


class TestSessionManagerAPIShape:
    """Проверяет наличие и сигнатуры публичных методов SessionManager."""

    def test_session_manager_default_store_is_jsonl(self, tmp_path) -> None:
        """SessionManager(workspace) без явного `store` использует JsonlSessionStore."""
        from nanobot.session.manager import SessionManager

        manager = SessionManager(tmp_path)
        session = manager.get_or_create("cli:contract-default")
        assert session is not None
        session.add_message("user", "hello")
        manager.save(session)

        sessions = manager.list_sessions()
        assert any(s["key"] == "cli:contract-default" for s in sessions)

    def test_session_manager_has_get_or_create(self) -> None:
        from nanobot.session.manager import SessionManager

        assert _has_method(SessionManager, "get_or_create")
        sig = _signature(SessionManager, "get_or_create")
        params = list(sig.parameters)
        assert "key" in params
        assert sig.parameters["key"].annotation is str

    def test_session_manager_has_save(self) -> None:
        from nanobot.session.manager import SessionManager

        assert _has_method(SessionManager, "save")
        sig = _signature(SessionManager, "save")
        params = list(sig.parameters)
        assert "session" in params
        assert "fsync" in sig.parameters
        assert sig.parameters["fsync"].default is False

    def test_session_manager_has_get_cached(self) -> None:
        from nanobot.session.manager import SessionManager

        assert _has_method(SessionManager, "get_cached")

    def test_session_manager_has_list_sessions(self) -> None:
        from nanobot.session.manager import SessionManager

        assert _has_method(SessionManager, "list_sessions")

    def test_session_manager_has_read_session_metadata(self) -> None:
        from nanobot.session.manager import SessionManager

        assert _has_method(SessionManager, "read_session_metadata")

    def test_session_manager_has_read_session_file(self) -> None:
        from nanobot.session.manager import SessionManager

        assert _has_method(SessionManager, "read_session_file")

    def test_session_manager_has_update_session_metadata(self) -> None:
        from nanobot.session.manager import SessionManager

        assert _has_method(SessionManager, "update_session_metadata")

    def test_session_manager_has_set_delete_observer(self) -> None:
        from nanobot.session.manager import SessionManager

        assert _has_method(SessionManager, "set_delete_observer")

    def test_session_manager_has_save_runtime_checkpoint(self) -> None:
        from nanobot.session.manager import SessionManager

        assert _has_method(SessionManager, "save_runtime_checkpoint")

    def test_session_manager_has_restore_sessions_to_workspace(self) -> None:
        from nanobot.session.manager import SessionManager

        assert _has_method(SessionManager, "restore_sessions_to_workspace")

    def test_session_manager_has_fork_session_before_user_index(self) -> None:
        from nanobot.session.manager import SessionManager

        assert _has_method(SessionManager, "fork_session_before_user_index")

    def test_session_manager_has_rename_model_preset(self) -> None:
        from nanobot.session.manager import SessionManager

        assert _has_method(SessionManager, "rename_model_preset")

    def test_session_manager_has_delete_session(self) -> None:
        from nanobot.session.manager import SessionManager

        assert _has_method(SessionManager, "delete_session")


class TestSessionManagerPersistence:
    """Round-trip через JsonlSessionStore: get_or_create → read_session_metadata."""

    def test_jsonl_store_persists_session(self, tmp_path) -> None:
        from nanobot.session.manager import SessionManager

        manager = SessionManager(tmp_path)
        session = manager.get_or_create("cli:contract-roundtrip")
        session.add_message("user", "round-trip question")
        session.add_message("assistant", "round-trip answer")
        manager.save(session)

        meta = manager.read_session_metadata("cli:contract-roundtrip")
        assert meta is not None
        assert meta["key"] == "cli:contract-roundtrip"

    def test_list_sessions_returns_after_save(self, tmp_path) -> None:
        from nanobot.session.manager import SessionManager

        manager = SessionManager(tmp_path)
        for i in range(3):
            session = manager.get_or_create(f"cli:contract-list-{i}")
            session.add_message("user", f"q{i}")
            manager.save(session)

        keys = {s["key"] for s in manager.list_sessions()}
        assert {f"cli:contract-list-{i}" for i in range(3)}.issubset(keys)


class TestSessionManagerDeleteObserver:
    """set_delete_observer должен принимать Callable[[str], None]."""

    def test_set_delete_observer_callable(self, tmp_path) -> None:
        from nanobot.session.manager import SessionManager

        manager = SessionManager(tmp_path)
        observer_calls: list[str] = []

        def observer(key: str) -> None:
            observer_calls.append(key)

        manager.set_delete_observer(observer)
        session = manager.get_or_create("cli:contract-del")
        session.add_message("user", "x")
        manager.save(session)
        manager.delete_session("cli:contract-del")
        assert "cli:contract-del" in observer_calls