"""Contract tests on upstream `LLMUsageStore` API (nanobot 0.3.5).

Фиксирует публичный API, используемый в storage-hybridization
(см. ``openspec/changes/storage-hybridization/specs/storage/usage-store``):
конструктор ``(path: Path)``, методы ``record`` / ``record_many`` /
``recent_calls`` / ``usage_payload`` / ``count`` / ``close``,
а также форму ``LLMCallRecord`` (frozen dataclass).

Тесты MUST падать при несовместимом изменении upstream API —
это страховка от регрессий при следующих минорных апдейдах nanobot.
"""

from __future__ import annotations

import dataclasses
import inspect
from pathlib import Path

import pytest

import nanobot.agent  # noqa: F401 — фикс import-order

pytestmark = pytest.mark.contract


class TestLLMUsageStoreAPIShape:
    """Проверяет наличие и сигнатуры публичных методов LLMUsageStore."""

    def test_usage_store_init_signature(self) -> None:
        from nanobot.llm_usage.store import LLMUsageStore

        sig = inspect.signature(LLMUsageStore.__init__)
        params = list(sig.parameters)
        assert "path" in params
        assert params.index("path") == 1
        annotations = LLMUsageStore.__init__.__annotations__
        assert annotations.get("path") == "Path"

    def test_usage_store_has_record_method(self) -> None:
        from nanobot.llm_usage.store import LLMUsageStore

        sig = inspect.signature(LLMUsageStore.record)
        params = list(sig.parameters)
        assert "call" in params
        assert params.index("call") == 1

    def test_usage_store_has_record_many_method(self) -> None:
        from nanobot.llm_usage.store import LLMUsageStore

        assert callable(getattr(LLMUsageStore, "record_many"))

    def test_usage_store_has_recent_calls_method(self) -> None:
        from nanobot.llm_usage.store import LLMUsageStore

        sig = inspect.signature(LLMUsageStore.recent_calls)
        assert "limit" in sig.parameters
        assert sig.parameters["limit"].default == 100

    def test_usage_store_has_usage_payload_method(self) -> None:
        from nanobot.llm_usage.store import LLMUsageStore

        sig = inspect.signature(LLMUsageStore.usage_payload)
        assert "days" in sig.parameters
        assert "timezone_name" in sig.parameters
        assert "now" in sig.parameters

    def test_usage_store_has_count_method(self) -> None:
        from nanobot.llm_usage.store import LLMUsageStore

        assert callable(getattr(LLMUsageStore, "count"))

    def test_usage_store_has_close_method(self) -> None:
        from nanobot.llm_usage.store import LLMUsageStore

        assert callable(getattr(LLMUsageStore, "close"))


class TestLLMCallRecordShape:
    """LLMCallRecord — frozen dataclass с upstream-полями."""

    def test_llm_call_record_is_frozen_dataclass(self) -> None:
        from nanobot.llm_usage.models import LLMCallRecord

        assert dataclasses.is_dataclass(LLMCallRecord)
        kwargs = getattr(LLMCallRecord, "__dataclass_params__", None)
        assert kwargs is not None
        assert kwargs.frozen is True

    def test_llm_call_record_required_fields(self) -> None:
        from nanobot.llm_usage.models import LLMCallRecord

        names = {f.name for f in dataclasses.fields(LLMCallRecord)}
        for required in ("started_at_ms", "duration_ms", "provider", "model",
                         "source", "stream", "finish_reason"):
            assert required in names, f"missing required field: {required}"


class TestLLMUsageStoreRoundTrip:
    """Round-trip: create store → record → recent_calls → count."""

    def _make_call(self, started_at_ms: int, model: str) -> object:
        from nanobot.llm_usage.models import LLMCallRecord

        return LLMCallRecord(
            started_at_ms=started_at_ms,
            duration_ms=100,
            provider="test-provider",
            model=model,
            source="user",
            stream=False,
            finish_reason="stop",
        )

    def test_round_trip_records(self, tmp_path: Path) -> None:
        from nanobot.llm_usage.store import LLMUsageStore

        db = tmp_path / "usage.db"
        store = LLMUsageStore(db)
        try:
            assert store.count() == 0

            call1 = self._make_call(started_at_ms=1_700_000_000_000, model="m-a")
            call2 = self._make_call(started_at_ms=1_700_000_001_000, model="m-b")
            store.record(call1)
            store.record(call2)

            assert store.count() >= 1

            recent = store.recent_calls(limit=10)
            assert isinstance(recent, list)
            assert len(recent) >= 1
            models = {row.get("model") for row in recent}
            assert models & {"m-a", "m-b"}
        finally:
            store.close()

    def test_record_many(self, tmp_path: Path) -> None:
        from nanobot.llm_usage.store import LLMUsageStore

        db = tmp_path / "usage-many.db"
        store = LLMUsageStore(db)
        try:
            calls = [
                self._make_call(started_at_ms=1_700_000_000_000 + i * 1000,
                                model=f"m-{i}")
                for i in range(5)
            ]
            store.record_many(calls)
            assert store.count() >= 1
        finally:
            store.close()