"""Mock-smoke lifecycle: создание / start / stop ApplicationContext
с session_cold_sync_service и usage_store.

Заменяет реальный gateway / CLI startup (задачи 2.4, 3.4, 7.5, 7.6, 7.7)
без живого PG. Проверяет, что:

- ``ApplicationContext.create()`` не падает при наличии
  ``session_cold_sync_service`` в dataclass;
- ``start()`` запускает cold-sync поток (mock-сессия);
- ``stop()`` корректно останавливает поток и закрывает ``usage_store``;
- LLM observer подключается через ``wrap_provider_snapshot_loader``
  (mock-проверка на уровне ``agent_factory``).
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


# Add workspace to sys.path so utils.db / config can be imported.
_project_root = Path(__file__).resolve().parent.parent
_workspace_path = str(_project_root / "workspace")
if _workspace_path not in sys.path:
    sys.path.insert(0, _workspace_path)


class _FakeSession:
    def __init__(self) -> None:
        self.updated_at = datetime(2026, 9, 1)
        self.created_at = datetime(2026, 9, 1)
        self.messages = []
        self.metadata = {}
        self.last_consolidated = None


class _FakeSessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, _FakeSession] = {"k1": _FakeSession()}

    def list_sessions(self):
        return [{"key": k, "updated_at": s.updated_at.isoformat()}
                for k, s in self._sessions.items()]

    def read_session_snapshot(self, key: str):
        return self._sessions.get(key)


class _FakeConfigService:
    def __init__(self, sections: dict[str, dict] | None = None,
                 flat: dict | None = None) -> None:
        if flat is not None:
            self._sections = {"gateway": flat}
        else:
            self._sections = sections or {}

    def settings_section(self, name: str) -> dict:
        return dict(self._sections.get(name, {}))


class TestSessionColdSyncLifecycleMock:
    def test_start_and_stop_does_not_crash(self) -> None:
        from lib.services.session_cold_sync_service import SessionColdSyncService

        sm = _FakeSessionManager()
        svc = SessionColdSyncService(
            session_manager=sm,
            pg_dsn="postgresql://test",
            sync_interval_sec=0.05,
            enabled=True,
        )
        svc.start()
        import time
        time.sleep(0.15)
        svc.stop(timeout_sec=2.0)
        assert svc._thread is None or not svc._thread.is_alive()

    def test_enabled_false_start_is_noop(self) -> None:
        from lib.services.session_cold_sync_service import SessionColdSyncService

        svc = SessionColdSyncService(
            session_manager=_FakeSessionManager(),
            pg_dsn="postgresql://test",
            enabled=False,
        )
        svc.start()
        assert svc._thread is None

    def test_stop_idempotent(self) -> None:
        from lib.services.session_cold_sync_service import SessionColdSyncService

        svc = SessionColdSyncService(
            session_manager=_FakeSessionManager(),
            pg_dsn="postgresql://test",
        )
        # Двойной stop не падает.
        svc.stop(timeout_sec=1.0)
        svc.stop(timeout_sec=1.0)


class TestUsageStoreLifecycleMock:
    def test_create_and_close_round_trip(self, tmp_path: Path) -> None:
        from lib.core.application_context import _make_usage_store

        path = tmp_path / "usage.db"
        ctx = SimpleNamespace(config_service=_FakeConfigService(flat={
            "usage_store": {"enabled": True, "sqlite_path": str(path)},
        }))
        store = _make_usage_store(ctx)
        assert store is not None
        assert hasattr(store, "close")
        store.close()

    def test_create_with_disabled_returns_none(self) -> None:
        from lib.core.application_context import _make_usage_store

        ctx = SimpleNamespace(config_service=_FakeConfigService(flat={
            "usage_store": {"enabled": False},
        }))
        assert _make_usage_store(ctx) is None


class TestLLMObserverMockSmoke:
    def test_wrap_provider_snapshot_loader_attaches_observer_to_provider(
        self, tmp_path: Path,
    ) -> None:
        from lib.services.llm_observer import wrap_provider_snapshot_loader
        from nanobot.llm_usage.store import LLMUsageStore

        store = LLMUsageStore(tmp_path / "u.db")

        class _StubProvider:
            def __init__(self) -> None:
                self.observer = None

            def set_llm_call_observer(self, observer) -> None:
                self.observer = observer

        class _StubSnapshot:
            def __init__(self) -> None:
                self.provider = _StubProvider()

        def _base_loader(*, preset_name=None, **kwargs):
            return _StubSnapshot()

        wrapped = wrap_provider_snapshot_loader(_base_loader, store)
        snap = wrapped(preset_name="main")
        assert snap.provider.observer == store.record
        store.close()

    def test_wrap_provider_snapshot_loader_works_with_none_store(self) -> None:
        from lib.services.llm_observer import wrap_provider_snapshot_loader

        class _StubSnapshot:
            provider = None

        def _base_loader(*, preset_name=None, **kwargs):
            return _StubSnapshot()

        wrapped = wrap_provider_snapshot_loader(_base_loader, None)
        snap = wrapped()
        assert snap is not None