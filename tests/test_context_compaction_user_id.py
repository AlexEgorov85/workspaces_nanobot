"""Регрессионные тесты на user_id прокидывание для context_compaction.

Этот файл — дополнение к ``test_context_compaction.py`` для change
``fix-history-search-user-isolation``. Закрывает требование 4.3:
``context_compacted`` пишется через ``LogEvent.user_id`` из
identity-store текущего request.

Тесты не пересекаются с pre-existing тестами ``_record_event_log``
(относятся к ``unify-agent-event-logging-pipeline``): мы проверяем
только user_id прокидывание через ``LogEvent``, не весь pipeline.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from lib.services.context_compaction import (
    ContextCompactionService,
    _current_request_sender_id,
)


def _settings(**overrides):
    gw = {"compact": {}}
    gw["compact"].update(overrides)
    return SimpleNamespace(gateway=gw)


class TestContextCompactedUserId:
    """``context_compacted`` event несёт ``user_id`` из identity-store."""

    @pytest.mark.asyncio
    async def test_context_compacted_event_carries_user_id(self):
        """При наличии ``RequestContext.sender_id`` событие
        ``context_compacted`` создаётся с явным ``user_id`` в LogEvent."""
        from lib.services.db_logging_service import LogEvent

        agent = MagicMock()
        agent.sessions = MagicMock()
        svc = ContextCompactionService(
            agent, settings=_settings(), db_logging_service=None,
        )

        captured: dict = {}

        def _capture_try_log_event(_svc, log_event, **kw):
            captured["event"] = log_event
            return True

        with patch(
            "lib.services.db_logging_service.try_log_event",
            side_effect=_capture_try_log_event,
        ), patch(
            "lib.services.context_compaction._current_request_sender_id",
            return_value="alice",
        ):
            await svc._record_event_log(
                "postgres:1",
                {
                    "mode": "idle",
                    "archived_msgs": 5,
                    "kept_msgs": 5,
                    "tokens_before": 100,
                    "tokens_after": 50,
                    "summary": "x",
                    "raw_dump": False,
                },
                "Итог: заархивировано 5 сообщений, 100 → 50 токенов.",
            )

        event = captured.get("event")
        assert isinstance(event, LogEvent)
        assert event.event_type == "context_compacted"
        assert event.user_id == "alice"

    @pytest.mark.asyncio
    async def test_context_compacted_user_id_none_without_identity(self):
        """Без identity-store → ``user_id=None`` (безопасный default;
        событие НЕ попадёт в ``session_scope='all'``)."""
        from lib.services.db_logging_service import LogEvent

        agent = MagicMock()
        agent.sessions = MagicMock()
        svc = ContextCompactionService(
            agent, settings=_settings(), db_logging_service=None,
        )

        captured: dict = {}

        def _capture(_svc, log_event, **kw):
            captured["event"] = log_event
            return True

        with patch(
            "lib.services.db_logging_service.try_log_event",
            side_effect=_capture,
        ), patch(
            "lib.services.context_compaction._current_request_sender_id",
            return_value=None,
        ):
            await svc._record_event_log(
                "postgres:1",
                {
                    "mode": "idle",
                    "archived_msgs": 5,
                    "kept_msgs": 5,
                    "tokens_before": 100,
                    "tokens_after": 50,
                    "summary": "x",
                    "raw_dump": False,
                },
                "Итог: заархивировано 5 сообщений, 100 → 50 токенов.",
            )

        event = captured.get("event")
        assert isinstance(event, LogEvent)
        assert event.user_id is None


class TestCurrentRequestSenderIdHelper:
    """Helper ``_current_request_sender_id()`` инкапсулирует доступ
    к ``RequestContext.sender_id`` (зависимость от nanobot 0.3.0)."""

    def test_returns_sender_id_from_request_context(self):
        with patch(
            "nanobot.agent.tools.context.current_request_context",
            return_value=SimpleNamespace(sender_id="alice"),
        ):
            assert _current_request_sender_id() == "alice"

    def test_returns_none_when_no_request_context(self):
        with patch(
            "nanobot.agent.tools.context.current_request_context",
            return_value=None,
        ):
            assert _current_request_sender_id() is None

    def test_returns_none_when_sender_id_is_none(self):
        with patch(
            "nanobot.agent.tools.context.current_request_context",
            return_value=SimpleNamespace(sender_id=None),
        ):
            assert _current_request_sender_id() is None

    def test_returns_none_when_sender_id_is_empty_string(self):
        with patch(
            "nanobot.agent.tools.context.current_request_context",
            return_value=SimpleNamespace(sender_id=""),
        ):
            assert _current_request_sender_id() is None

    def test_returns_none_on_import_error(self):
        with patch(
            "nanobot.agent.tools.context.current_request_context",
            side_effect=ImportError("no nanobot"),
        ):
            assert _current_request_sender_id() is None
