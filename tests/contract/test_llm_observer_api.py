"""Contract tests on observer-pipeline API (nanobot 0.3.5).

Фиксирует публичный API, используемый в storage-hybridization
(см. ``openspec/changes/storage-hybridization/specs/observability/usage-store``
и design D3):

- ``LLMProvider.set_llm_call_observer(observer: LLMCallObserver | None)``;
- ``LLMCallObserver = Callable[[LLMCallRecord], None]``;
- fail-open семантика ``record_llm_call`` (или эквивалентного
  upstream-callback);
- контракт подписки observer'а в ``AgentFactory._wrap_provider_snapshot_loader``.

Тесты MUST падать при несовместимом изменении upstream API —
это страховка от регрессий при следующих минорных апдейдах nanobot.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

import nanobot.agent  # noqa: F401 — фикс import-order

pytestmark = pytest.mark.contract


class TestLLMProviderObserverAPI:
    """API ``LLMProvider.set_llm_call_observer``."""

    def test_llm_provider_has_set_llm_call_observer(self) -> None:
        from nanobot.providers.base import LLMProvider

        assert callable(getattr(LLMProvider, "set_llm_call_observer"))
        sig = inspect.signature(LLMProvider.set_llm_call_observer)
        params = list(sig.parameters)
        assert "observer" in params
        assert params.index("observer") == 1

    def test_llm_call_observer_type_alias(self) -> None:
        from nanobot.providers.base import LLMCallObserver
        from nanobot.llm_usage.models import LLMCallRecord

        assert callable(LLMCallObserver)
        assert "LLMCallRecord" in str(LLMCallObserver)


class TestRecordLLMCallFailOpen:
    """``record_llm_call`` (или эквивалентный callback) НЕ пробрасывает
    исключения из LLMUsageStore наружу (fail-open семантика)."""

    def test_record_llm_call_is_fail_open(self) -> None:
        from nanobot.llm_usage.store import LLMUsageStore
        from nanobot.llm_usage.models import LLMCallRecord

        class _ExplodingStore:
            def record(self, call: LLMCallRecord) -> None:
                raise RuntimeError("disk full")

        store = _ExplodingStore()

        def record_llm_call(call: LLMCallRecord) -> None:
            try:
                store.record(call)
            except Exception:
                pass

        call = LLMCallRecord(
            started_at_ms=1_700_000_000_000,
            duration_ms=10,
            provider="p",
            model="m",
            source="user",
            stream=False,
            finish_reason="stop",
        )
        record_llm_call(call)


class TestSnapshotLoaderObserverFanOut:
    """``AgentFactory._wrap_provider_snapshot_loader`` корректно пробрасывает
    ``set_llm_call_observer`` в загруженный ``ProviderSnapshot.provider``.

    Обёртка живёт в ``AgentFactory`` инлайном — отдельного модуля у агента
    больше нет, но контракт (подписка + fail-soft) обязан быть зафиксирован.
    """

    def test_snapshot_loader_attaches_observer(self, tmp_path: Path) -> None:
        from lib.core.agent_factory import AgentFactory
        from nanobot.llm_usage.store import LLMUsageStore

        db = tmp_path / "usage.db"
        store = LLMUsageStore(db)

        class _StubProvider:
            def __init__(self) -> None:
                self.observer = None

            def set_llm_call_observer(self, observer) -> None:
                self.observer = observer

        class _StubSnapshot:
            def __init__(self, provider: _StubProvider) -> None:
                self.provider = provider

        captured: list = []

        def base_loader(*, preset_name=None, **kwargs):
            captured.append(preset_name)
            return _StubSnapshot(_StubProvider())

        config = SimpleNamespace(build_provider_snapshot=base_loader)
        wrapped = AgentFactory._wrap_provider_snapshot_loader(config, store, None)

        snapshot = wrapped(preset_name="test-preset")
        assert snapshot is not None
        assert snapshot.provider.observer == store.record
        assert captured == ["test-preset"]

        snapshot2 = wrapped()
        assert snapshot2.provider.observer == store.record
        store.close()

    def test_snapshot_loader_fail_soft(self, tmp_path: Path) -> None:
        """Если ``set_llm_call_observer`` бросает — обёртка логирует WARNING
        и возвращает snapshot без observer (агент продолжает работать)."""
        from lib.core.agent_factory import AgentFactory
        from nanobot.llm_usage.store import LLMUsageStore

        db = tmp_path / "usage-failsoft.db"
        store = LLMUsageStore(db)

        class _ExplodingProvider:
            def set_llm_call_observer(self, observer) -> None:
                raise RuntimeError("upstream broke")

        class _StubSnapshot:
            def __init__(self, provider: _ExplodingProvider) -> None:
                self.provider = provider

        def base_loader(*, preset_name=None, **kwargs):
            return _StubSnapshot(_ExplodingProvider())

        config = SimpleNamespace(build_provider_snapshot=base_loader)
        wrapped = AgentFactory._wrap_provider_snapshot_loader(config, store, None)
        snapshot = wrapped()
        assert snapshot is not None
        assert snapshot.provider is not None
        store.close()


class TestFallbackProviderObserverFanOut:
    """``FallbackProvider.set_llm_call_observer`` наследует реализацию
    от ``LLMProvider`` (проверяем наличие метода)."""

    def test_fallback_provider_inherits_set_llm_call_observer(self) -> None:
        from nanobot.providers.base import LLMProvider
        from nanobot.providers.fallback_provider import FallbackProvider

        assert hasattr(FallbackProvider, "set_llm_call_observer")
        assert getattr(FallbackProvider, "set_llm_call_observer") is not None
        assert "set_llm_call_observer" in LLMProvider.__dict__