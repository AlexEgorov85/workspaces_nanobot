"""Тесты ``CompactionEventSubscriber`` (lib/services/compaction_event_subscriber.py).

Подписчик фильтрует ``OutboundMessage.event`` типа
``nanobot.events.ContextCompactionEvent`` и зовёт публичный API
``ContextCompactionService.notify_session_compacted``. Канал передаёт
каждый ``OutboundMessage`` через ``feed()`` — этот класс и есть
единственная точка, где Upstream-фаза превращается в запись факта в
``agent_gateway_logs``/``agent_conversation_messages``.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _run(coro):
    return asyncio.run(coro)


class TestCompactionEventSubscriber:
    def _make_outbound(self, event, session_key="postgres:1"):
        return SimpleNamespace(event=event, session_key=session_key)

    def test_no_event_is_noop(self):
        from lib.services.compaction_event_subscriber import CompactionEventSubscriber

        svc = MagicMock()
        sub = CompactionEventSubscriber(compaction_service=svc)
        out = self._make_outbound(event=None, session_key="x")
        _run(sub.feed(out))
        svc.notify_session_compacted.assert_not_called()

    def test_other_event_type_is_ignored(self):
        from lib.services.compaction_event_subscriber import CompactionEventSubscriber

        svc = MagicMock()
        sub = CompactionEventSubscriber(compaction_service=svc)
        out = self._make_outbound(event=SimpleNamespace())
        _run(sub.feed(out))
        svc.notify_session_compacted.assert_not_called()

    def test_context_compaction_event_calls_service(self):
        from nanobot.events import ContextCompactionEvent
        from lib.services.compaction_event_subscriber import CompactionEventSubscriber

        captured: dict = {}

        async def _capture(**kw):
            captured.update(kw)

        svc = MagicMock()
        svc.notify_session_compacted = _capture

        sub = CompactionEventSubscriber(compaction_service=svc)
        event = ContextCompactionEvent(
            compaction_id="cmp-1", phase="succeeded",
        )
        out = self._make_outbound(event=event, session_key="postgres:42")
        _run(sub.feed(out))
        assert captured == {
            "session_key": "postgres:42",
            "phase": "succeeded",
            "compaction_id": "cmp-1",
        }

    def test_missing_service_does_not_raise(self):
        from nanobot.events import ContextCompactionEvent
        from lib.services.compaction_event_subscriber import CompactionEventSubscriber

        sub = CompactionEventSubscriber()  # без сервиса — это намеренный сценарий
        event = ContextCompactionEvent(compaction_id="cmp-x", phase="started")
        out = self._make_outbound(event=event, session_key="cli:1")
        _run(sub.feed(out))

    def test_missing_session_key_skipped(self):
        from nanobot.events import ContextCompactionEvent
        from lib.services.compaction_event_subscriber import CompactionEventSubscriber

        svc = MagicMock()
        sub = CompactionEventSubscriber(compaction_service=svc)
        event = ContextCompactionEvent(compaction_id="cmp-z", phase="succeeded")
        out = SimpleNamespace(event=event, session_key=None)
        _run(sub.feed(out))
        svc.notify_session_compacted.assert_not_called()

    def test_set_service_after_init(self):
        from lib.services.compaction_event_subscriber import CompactionEventSubscriber

        sub = CompactionEventSubscriber()
        assert sub._service is None
        svc = MagicMock()
        sub.set_service(svc)
        assert sub._service is svc

    def test_service_exception_is_swallowed(self):
        from nanobot.events import ContextCompactionEvent
        from lib.services.compaction_event_subscriber import CompactionEventSubscriber

        svc = MagicMock()

        async def _boom(**_kw):
            raise RuntimeError("downstream failed")

        svc.notify_session_compacted = _boom
        sub = CompactionEventSubscriber(compaction_service=svc)
        event = ContextCompactionEvent(
            compaction_id="c", phase="failed",
        )
        out = self._make_outbound(event=event, session_key="p:1")
        _run(sub.feed(out))


class TestNotifySessionCompactedPublicAPI:
    """Публичный API ``ContextCompactionService.notify_session_compacted``
    пишет ``event_type=\"context_compacted\"`` в ``agent_gateway_logs``;
    history-notice в ``agent_conversation_messages`` — только для
    ``succeeded`` (при ``notify_in_history=True``).
    """

    def _settings(self, **overrides):
        gw = {"compact": {}}
        gw["compact"].update(overrides)
        return SimpleNamespace(gateway=gw)

    def test_succeeded_writes_event_log_and_history_notice(self, monkeypatch):
        from lib.services.context_compaction import ContextCompactionService

        agent = MagicMock()
        agent.sessions = MagicMock()
        svc = ContextCompactionService(
            agent, settings=self._settings(notify_in_history=True),
        )

        record_calls: list = []
        history_calls: list = []

        async def _record_event_log(**kw):
            record_calls.append(kw)

        async def _write_history_notice(**kw):
            history_calls.append(kw)

        monkeypatch.setattr(svc, "_record_event_log", _record_event_log)
        monkeypatch.setattr(svc, "_write_history_notice", _write_history_notice)

        _run(svc.notify_session_compacted(
            session_key="postgres:42",
            phase="succeeded",
            compaction_id="cmp-1",
        ))

        assert len(record_calls) == 1
        assert len(history_calls) == 1

    def test_non_succeeded_skips_history_notice(self, monkeypatch):
        from lib.services.context_compaction import ContextCompactionService

        agent = MagicMock()
        agent.sessions = MagicMock()
        svc = ContextCompactionService(
            agent, settings=self._settings(notify_in_history=True),
        )

        called: list = []
        async def _record_event_log(**_):
            called.append("record")

        async def _write_history_notice(**_):
            called.append("history")

        monkeypatch.setattr(svc, "_record_event_log", _record_event_log)
        monkeypatch.setattr(svc, "_write_history_notice", _write_history_notice)

        for phase in ("started", "failed", "cancelled"):
            _run(svc.notify_session_compacted(
                session_key="postgres:1", phase=phase, compaction_id="c",
            ))

        assert called.count("record") == 3
        assert "history" not in called

    def test_notify_in_history_false_skips_history_notice(self, monkeypatch):
        from lib.services.context_compaction import ContextCompactionService

        agent = MagicMock()
        agent.sessions = MagicMock()
        svc = ContextCompactionService(
            agent, settings=self._settings(notify_in_history=False),
        )

        called: list = []
        async def _record_event_log(**_):
            called.append("record")

        async def _write_history_notice(**_):
            called.append("history")

        monkeypatch.setattr(svc, "_record_event_log", _record_event_log)
        monkeypatch.setattr(svc, "_write_history_notice", _write_history_notice)

        _run(svc.notify_session_compacted(
            session_key="postgres:1", phase="succeeded", compaction_id="c",
        ))

        assert called == ["record"]

    def test_empty_session_key_is_noop(self, monkeypatch):
        from lib.services.context_compaction import ContextCompactionService

        agent = MagicMock()
        agent.sessions = MagicMock()
        svc = ContextCompactionService(agent, settings=self._settings())

        called: list = []
        async def _record_event_log(**_):
            called.append("record")

        monkeypatch.setattr(svc, "_record_event_log", _record_event_log)

        _run(svc.notify_session_compacted(
            session_key="", phase="succeeded", compaction_id="c",
        ))

        assert called == []
