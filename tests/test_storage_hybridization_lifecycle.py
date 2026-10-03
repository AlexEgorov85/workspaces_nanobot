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

import asyncio
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from config import runtime_table  # noqa: F401


# Add workspace to sys.path so utils.db / config can be imported.
_project_root = Path(__file__).resolve().parent.parent
_workspace_path = str(_project_root / "workspace")
if _workspace_path not in sys.path:
    sys.path.insert(0, _workspace_path)


class _FakeMcp:
    """Клиент платформы: отвечает пустым, но валидным JSON."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call(self, operation: str, arguments: dict) -> str:
        self.calls.append((operation, arguments))
        return '{"count": 0, "sessions": {}, "deleted_sessions": 0}'


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
    """Зеркало стало задачей event loop, а не фоновым потоком.

    Проверять его lifecycle в потоке больше нельзя: ``start``/``stop`` —
    корутины, привязанные к loop'у, на котором поднята сессия платформы.
    """

    @pytest.mark.asyncio
    async def test_start_and_stop_does_not_crash(self) -> None:
        from lib.services.session_cold_sync_service import SessionColdSyncService

        svc = SessionColdSyncService(
            session_manager=_FakeSessionManager(),
            enterprise_mcp=_FakeMcp(),
            replica_id="gw-test",
            sync_interval_sec=0.05,
            enabled=True,
        )
        await svc.start()
        await asyncio.sleep(0.15)
        await svc.stop(timeout_sec=2.0)
        assert svc._task is None

    @pytest.mark.asyncio
    async def test_enabled_false_start_is_noop(self) -> None:
        from lib.services.session_cold_sync_service import SessionColdSyncService

        svc = SessionColdSyncService(
            session_manager=_FakeSessionManager(),
            enterprise_mcp=_FakeMcp(),
            enabled=False,
        )
        await svc.start()
        assert svc._task is None

    @pytest.mark.asyncio
    async def test_stop_idempotent(self) -> None:
        from lib.services.session_cold_sync_service import SessionColdSyncService

        svc = SessionColdSyncService(
            session_manager=_FakeSessionManager(),
            enterprise_mcp=_FakeMcp(),
        )
        # Двойной stop не падает.
        await svc.stop(timeout_sec=1.0)
        await svc.stop(timeout_sec=1.0)

    def test_without_platform_the_mirror_is_off_and_says_why(self) -> None:
        """Отсутствие платформы — не поломка, но и не «работает молча»:
        зеркало обязано быть выключено с названной причиной, иначе оператор
        видит исправный сервис, который ничего не пишет."""
        from lib.services.session_cold_sync_service import SessionColdSyncService

        svc = SessionColdSyncService(
            session_manager=_FakeSessionManager(),
            enterprise_mcp=None,
        )
        assert svc.enabled is False
        assert "платформа" in svc.disabled_reason
        assert svc.get_stats()["disabled_reason"] == svc.disabled_reason


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
    """Подписка observer'а живёт в ``AgentFactory`` (инлайн, без своего модуля).

    Покрываем тот же контракт, что и раньше: snapshot-обёртка подписывает
    провайдера учётом вызовов LLM и переживает ``store=None``.
    """

    def test_snapshot_loader_attaches_observer_to_provider(
        self, tmp_path: Path,
    ) -> None:
        from lib.core.agent_factory import AgentFactory
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

        config = SimpleNamespace(build_provider_snapshot=_base_loader)
        wrapped = AgentFactory._wrap_provider_snapshot_loader(config, store, None)
        snap = wrapped(preset_name="main")
        assert snap.provider.observer == store.record
        store.close()

    def test_snapshot_loader_works_with_none_store(self) -> None:
        from lib.core.agent_factory import AgentFactory

        class _StubSnapshot:
            provider = None

        def _base_loader(*, preset_name=None, **kwargs):
            return _StubSnapshot()

        config = SimpleNamespace(build_provider_snapshot=_base_loader)
        wrapped = AgentFactory._wrap_provider_snapshot_loader(config, None, None)
        snap = wrapped()
        assert snap is not None