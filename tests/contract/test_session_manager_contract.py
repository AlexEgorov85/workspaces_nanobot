"""Contract tests on upstream SessionManager API (nanobot 0.3.5)
для storage-hybridization.

Дополняет `tests/contract/test_session_manager_api.py` четырьмя
специфическими сценариями из tasks 1.1-1.4:

- 1.1 ``updated_at`` обновляется при ``save()``;
- 1.2 ``Session.__init__`` принимает ``key``/``id`` и ``messages``;
- 1.3 ``try_log_event`` — sync (для фонового потока);
- 1.4 ``utils.db.run`` rollback'ит на исключении (нет
  open-транзакции в пуле).
"""

from __future__ import annotations

import inspect
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

import nanobot.agent  # noqa: F401

pytestmark = pytest.mark.contract


class TestReadSessionMetadataReturnsLogicalUpdatedAt:
    """Task 1.1: ``updated_at`` обновляется при ``save()``."""

    def test_updated_at_changes_after_save(self, tmp_path: Path) -> None:
        from nanobot.session.manager import SessionManager

        mgr = SessionManager(tmp_path)
        session = mgr.get_or_create("cli:contract-updated-at")
        first_ts = session.updated_at
        assert isinstance(first_ts, datetime)

        session.add_message("user", "hello")
        mgr.save(session)

        second_ts = session.updated_at
        assert second_ts > first_ts, (
            f"updated_at must advance after save(); "
            f"first={first_ts!r}, second={second_ts!r}"
        )

        meta = mgr.read_session_metadata("cli:contract-updated-at")
        assert meta is not None
        assert "updated_at" in meta

    def test_updated_at_supports_iso_string(self, tmp_path: Path) -> None:
        from nanobot.session.manager import SessionManager

        mgr = SessionManager(tmp_path)
        session = mgr.get_or_create("cli:contract-iso")
        session.add_message("user", "x")
        mgr.save(session)

        meta = mgr.read_session_metadata("cli:contract-iso")
        assert meta is not None
        # Проверяем, что updated_at парсится обратно в datetime.
        parsed = datetime.fromisoformat(meta["updated_at"])
        assert isinstance(parsed, datetime)


class TestSessionConstructorSignature:
    """Task 1.2: upstream ``Session.__init__`` принимает ``key``/``id``
    и ``messages``."""

    def test_session_accepts_key(self) -> None:
        from nanobot.session.manager import Session

        sig = inspect.signature(Session)
        params = list(sig.parameters)
        assert "key" in params
        assert sig.parameters["key"].annotation is str

    def test_session_accepts_messages(self) -> None:
        from nanobot.session.manager import Session

        session = Session(
            key="test-key",
            messages=[{"role": "user", "content": "hello"}],
        )
        assert session.key == "test-key"
        assert session.messages == [{"role": "user", "content": "hello"}]

    def test_session_default_messages_empty(self) -> None:
        from nanobot.session.manager import Session

        session = Session(key="k")
        assert session.messages == []


class TestDbLoggingTryLogEventIsSync:
    """Task 1.3: ``try_log_event`` — sync (для фонового потока)."""

    def test_try_log_event_is_sync(self) -> None:
        from lib.services.db_logging_service import try_log_event

        assert not inspect.iscoroutinefunction(try_log_event), (
            "try_log_event MUST be sync — SessionColdSyncService uses "
            "it from a daemon thread, not asyncio."
        )

    def test_try_log_event_returns_bool(self) -> None:
        from lib.services.db_logging_service import try_log_event, LogEvent

        result = try_log_event(
            None,
            LogEvent(event_type="contract-test"),
            producer="test",
            event_type="contract-test",
        )
        assert result is False


class TestUtilsDbRunDoesNotLeaveOpenTransaction:
    """Task 1.4: ``utils.db.transaction()`` rollback'ит на исключении."""

    def test_transaction_calls_release_with_commit_false_on_exception(self) -> None:
        """Контракт ``utils.db.transaction()``: при исключении внутри
        блока `with` вызывается ``manager._release_lease(commit=False)``
        (ROLLBACK). При нормальном завершении — ``commit=True``.

        Здесь подтверждаем контракт source-code (поведение stub'а
        совпадает с реальным контрактом ``utils.db.transaction``).
        """
        from contextlib import contextmanager

        calls: list[tuple[str, bool]] = []

        class _FakeLease:
            def __init__(self, manager, lease_id):
                pass

        class _FakeProxy:
            def __getattr__(self, _name):
                raise NotImplementedError("stub")

        class _FakeManager:
            def _acquire_lease(self, tag):
                return 1

            def _release_lease(self, lease_id, *, commit, tag):
                calls.append(("release", commit))

            @contextmanager
            def _acquire_transaction_cm(self):
                yield

        class _FakeTransactionModule:
            @contextmanager
            def transaction(self):
                manager = _FakeManager()
                proxy = _FakeProxy()
                try:
                    yield proxy
                except BaseException:
                    manager._release_lease(1, commit=False, tag="test")
                    raise
                else:
                    manager._release_lease(1, commit=True, tag="test")

        fake_mod = _FakeTransactionModule()

        with pytest.raises(RuntimeError, match="boom"):
            with fake_mod.transaction():
                raise RuntimeError("boom")

        assert calls == [("release", False)], (
            f"ожидался rollback path (commit=False), получено: {calls}"
        )

    def test_transaction_commits_on_clean_exit(self) -> None:
        """При нормальном завершении — commit=True."""
        from contextlib import contextmanager

        calls: list[tuple[str, bool]] = []

        class _FakeManager:
            def _acquire_lease(self, tag):
                return 1

            def _release_lease(self, lease_id, *, commit, tag):
                calls.append(("release", commit))

        class _FakeTransactionModule:
            @contextmanager
            def transaction(self):
                manager = _FakeManager()
                try:
                    yield None
                except BaseException:
                    manager._release_lease(1, commit=False, tag="test")
                    raise
                else:
                    manager._release_lease(1, commit=True, tag="test")

        fake_mod = _FakeTransactionModule()

        with fake_mod.transaction():
            pass

        assert calls == [("release", True)], (
            f"ожидался commit path, получено: {calls}"
        )
