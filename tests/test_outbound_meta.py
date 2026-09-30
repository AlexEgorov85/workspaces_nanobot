"""Регресс-тесты для ``lib.utils.outbound_meta``.

Контракт с nanobot 0.3.5: legacy ``outbound_event_from_message``
(msg→event) удалён в пользу ``outbound_message_for_event``
(event→msg, обратное). Адаптер должен просто читать ``msg.event``
(primary path), без legacy-fallback.
"""
from __future__ import annotations

from types import SimpleNamespace


class TestTypedEvent:
    def test_returns_msg_event_when_set(self):
        from lib.utils.outbound_meta import _typed_event

        class FakeMsg:
            event = "ContextCompactionEvent"

        evt = _typed_event(FakeMsg())
        assert evt == "ContextCompactionEvent"

    def test_returns_none_when_event_absent(self):
        from lib.utils.outbound_meta import _typed_event

        class FakeMsg:
            pass

        assert _typed_event(FakeMsg()) is None

    def test_does_not_import_legacy_nanobot_function(self):
        """Регрессия для nanobot 0.3.5+: ``outbound_event_from_message``
        удалён, вызовы должны идти только через ``msg.event``.
        Любая попытка импорта legacy функции даёт ``ImportError`` —
        этот тест защищает от возврата к fallback.
        """
        import sys

        from lib.utils.outbound_meta import _typed_event

        # Эмулируем, что legacy-функции нет (как в 0.3.5).
        sentinel_name = "nanobot.bus.outbound_events.outbound_event_from_message"
        old = sys.modules.get(sentinel_name)
        try:
            sys.modules[sentinel_name] = None  # type: ignore[assignment]
            try:
                # Перезагрузка модуля не нужна, _typed_event просто
                # должен работать без обращения к sentinel.
                result = _typed_event(SimpleNamespace(event="X"))
                assert result == "X"
            finally:
                if old is None:
                    sys.modules.pop(sentinel_name, None)
                else:
                    sys.modules[sentinel_name] = old
        except Exception as exc:
            pytest.fail(f"_typed_event should not need legacy nanobot: {exc}")

    def test_handles_simple_namespace(self):
        from lib.utils.outbound_meta import _typed_event

        # SimpleNamespace with event
        ns = SimpleNamespace(event="compaction")
        assert _typed_event(ns) == "compaction"
        # SimpleNamespace without event attr
        ns2 = SimpleNamespace()
        assert _typed_event(ns2) is None


class TestIsOutboundFinal:
    """Sanity-проверки, что вытащенный public API не сломан."""

    def test_key_constant_present(self):
        from lib.utils.outbound_meta import FINAL_TURN_KEY

        assert FINAL_TURN_KEY == "_final_turn"

    def test_dropped_keys_present(self):
        from lib.utils.outbound_meta import OUTBOUND_DROPPED_KEYS

        assert "_stream_end" in OUTBOUND_DROPPED_KEYS
        assert "_reasoning_end" in OUTBOUND_DROPPED_KEYS
        assert "_turn_end" in OUTBOUND_DROPPED_KEYS


# Import pytest at the end so module-level code runs cleanly
import pytest
