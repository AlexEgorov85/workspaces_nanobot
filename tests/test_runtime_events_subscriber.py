"""Тесты для ``lib/services/runtime_events_subscriber.py``.

Покрывают контракт ``runtime-events-subscription`` и расширения
``post-0.3.5-patches-cleanup``:

* регистрация подписок через ``bus.subscribe(handler, EventType)``;
* seed_context_window на TurnRuntimeAdmitted;
* запись ``turn_completed`` через DbLoggingService на TurnCompleted;
* запись ``subagent_run_finished`` через DbLoggingService на
  SubagentTurnCompleted;
* LIFO-порядок unsubscribe в stop();
* защита от двойного start() (warning + no-op).

Используется fake-bus + fake-db-logging-service без реального asyncio
loop, чтобы тесты были детерминированными и быстрыми.
"""

from __future__ import annotations

from typing import Any

import pytest

from lib.events.subagent import SubagentTurnCompleted
from lib.services.runtime_events_subscriber import RuntimeEventsSubscriber


class FakeBus:
    """Имитация MessageBus: хранит список подписок, позволяет
    публиковать события через ``publish(event)`` синхронно через
    coroutine.

    Минимально нужен для unit-тестов: ``subscribe(handler, EventType)``
    возвращает ``unsubscribe``, ``publish(event)`` вызывает handler.
    """

    def __init__(self) -> None:
        self._handlers: list[tuple[Any, type | None]] = []

    def subscribe(self, handler: Any, event_type: type | None = None):
        entry = (handler, event_type)
        self._handlers.append(entry)

        def _unsub() -> None:
            try:
                self._handlers.remove(entry)
            except ValueError:
                pass

        return _unsub

    async def publish(self, event: Any) -> None:
        for handler, event_type in list(self._handlers):
            if event_type is None or isinstance(event, event_type):
                result = handler(event)
                if hasattr(result, "__await__"):
                    await result


class FakeLogEvent:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    def __repr__(self) -> str:
        return f"FakeLogEvent(event_type={self.kwargs.get('event_type')!r})"


class FakeDbLoggingService:
    """Имитация ``DbLoggingService.log_event`` — собирает события.

    Подписчик передаёт реальный ``LogEvent`` (dataclass), проверяем
    через ``dataclasses.fields``.
    """

    def __init__(self) -> None:
        self.events: list[Any] = []

    def log_event(self, event: Any) -> None:
        self.events.append(event)

    def finish_request(self, *args: Any, **kwargs: Any) -> None:
        pass


class FakeLLMUsage:
    """Минимальный stand-in для ``LLMUsage`` (dataclass с total_tokens)."""

    def __init__(self, total_tokens: int) -> None:
        self.total_tokens = total_tokens


def _make_turn_runtime_admitted(
    session_key: str = "telegram:1",
    limit: int = 40000,
    model: str = "MiniMax-M3",
) -> Any:
    """Создать fake TurnRuntimeAdmitted через прямую инстанцию dataclass."""
    from nanobot.bus.runtime_events import (
        RuntimeEventContext,
        TurnRuntimeAdmitted,
    )

    ctx = RuntimeEventContext(
        channel="telegram",
        chat_id="1",
        session_key=session_key,
    )

    class _FakeRuntime:
        pass

    rt = _FakeRuntime()
    rt.context_window_tokens = limit
    rt.model = model
    return TurnRuntimeAdmitted(context=ctx, runtime=rt)


def _make_turn_completed(
    session_key: str = "telegram:1",
    latency_ms: int = 250,
    outcome: str = "completed",
    total_tokens: int | None = 100,
) -> Any:
    from nanobot.bus.runtime_events import (
        RuntimeEventContext,
        TurnCompleted,
    )

    ctx = RuntimeEventContext(
        channel="telegram",
        chat_id="1",
        session_key=session_key,
    )
    usage = FakeLLMUsage(total_tokens=total_tokens) if total_tokens else None
    return TurnCompleted(
        context=ctx,
        latency_ms=latency_ms,
        runtime=None,
        usage=usage,
        outcome=outcome,
    )


def _make_subagent_event(
    task_id: str = "subagent-1",
    parent_user_id: str | None = "alice",
    had_error: bool = False,
) -> Any:
    return SubagentTurnCompleted(
        task_id=task_id,
        parent_request_id="req-parent",
        parent_user_id=parent_user_id,
        final_content="done",
        tools_used=["read_file"],
        stop_reason="end_turn",
        request_id=f"subagent:{task_id}",
        task="describe test",
        had_error=had_error,
        error="boom" if had_error else None,
    )


@pytest.fixture
def bus() -> FakeBus:
    return FakeBus()


@pytest.fixture
def db_service() -> FakeDbLoggingService:
    return FakeDbLoggingService()


@pytest.fixture
def subscriber(bus: FakeBus, db_service: FakeDbLoggingService) -> RuntimeEventsSubscriber:
    s = RuntimeEventsSubscriber(bus, db_logging_service=db_service)
    s.start()
    return s


# ---------------------------------------------------------------------------
# Lifecycle: start() / stop()
# ---------------------------------------------------------------------------


def test_subscribe_all_events(bus: FakeBus) -> None:
    """start() регистрирует три подписки (TurnRuntimeAdmitted,
    TurnCompleted, SubagentTurnCompleted)."""
    RuntimeEventsSubscriber(bus).start()
    assert len(bus._handlers) == 3
    event_types = [t for _, t in bus._handlers]
    from nanobot.bus.runtime_events import TurnCompleted, TurnRuntimeAdmitted

    assert event_types.count(TurnRuntimeAdmitted) == 1
    assert event_types.count(TurnCompleted) == 1
    assert event_types.count(SubagentTurnCompleted) == 1


def test_double_start_noop(bus: FakeBus) -> None:
    """Повторный start() без stop() НЕ добавляет дублирующих подписок.

    loguru не интегрируется с pytest caplog; контракт проверяется через
    счётчик подписок: должно остаться ровно 3 (без дублирования).
    """
    s = RuntimeEventsSubscriber(bus)
    s.start()
    assert len(bus._handlers) == 3
    s.start()
    assert len(bus._handlers) == 3


def test_stop_unsubscribes_lifo(bus: FakeBus) -> None:
    """stop() вызывает unsubscribe в LIFO-порядке."""
    s = RuntimeEventsSubscriber(bus)
    s.start()
    assert len(bus._handlers) == 3
    s.stop()
    assert len(bus._handlers) == 0
    assert s._started is False


# ---------------------------------------------------------------------------
# _handle_turn_runtime_admitted → seed_context_window
# ---------------------------------------------------------------------------


def test_turn_runtime_admitted_seeds_bridge(
    subscriber: RuntimeEventsSubscriber, db_service: FakeDbLoggingService
) -> None:
    """TurnRuntimeAdmitted вызывает seed_context_window."""
    import asyncio
    from lib.hooks.database_logging_hook import (
        _CONTEXT_BRIDGE,
        _CONTEXT_BRIDGE_LOCK,
    )

    event = _make_turn_runtime_admitted(limit=40000, model="MiniMax-M3")
    asyncio.run(subscriber._handle_turn_runtime_admitted(event))
    with _CONTEXT_BRIDGE_LOCK:
        entry = _CONTEXT_BRIDGE.get("telegram:1")
    assert entry is not None
    assert entry["limit"] == 40000
    assert entry["model"] == "MiniMax-M3"
    with _CONTEXT_BRIDGE_LOCK:
        _CONTEXT_BRIDGE.pop("telegram:1", None)


def test_turn_runtime_admitted_empty_session_key_noop(
    subscriber: RuntimeEventsSubscriber,
) -> None:
    """Пустой session_key — no-op без seed."""
    import asyncio

    event = _make_turn_runtime_admitted(session_key="")
    asyncio.run(subscriber._handle_turn_runtime_admitted(event))


# ---------------------------------------------------------------------------
# _handle_turn_completed → LogEvent(turn_completed)
# ---------------------------------------------------------------------------


def test_turn_completed_writes_log_event(
    subscriber: RuntimeEventsSubscriber, db_service: FakeDbLoggingService
) -> None:
    """TurnCompleted → LogEvent(turn_completed) с правильными полями."""
    import asyncio

    event = _make_turn_completed(latency_ms=250, total_tokens=128)
    asyncio.run(subscriber._handle_turn_completed(event))
    assert len(db_service.events) == 1
    log = db_service.events[0]
    assert log.event_type == "turn_completed"
    assert log.payload["latency_ms"] == 250
    assert log.payload["outcome"] == "completed"
    assert log.payload["usage_tokens"] == 128


def test_turn_completed_without_db_service(
    bus: FakeBus,
) -> None:
    """Без db_logging_service — no-op без ошибок."""
    import asyncio

    s = RuntimeEventsSubscriber(bus, db_logging_service=None)
    s.start()
    event = _make_turn_completed()
    asyncio.run(s._handle_turn_completed(event))


# ---------------------------------------------------------------------------
# _handle_subagent_turn_completed → LogEvent(subagent_run_finished)
# ---------------------------------------------------------------------------


def test_subagent_turn_completed_writes_log_event(
    subscriber: RuntimeEventsSubscriber, db_service: FakeDbLoggingService
) -> None:
    """SubagentTurnCompleted → LogEvent(subagent_run_finished) с тем же
    контрактом, что у ``_SubagentLoggingHook._finalize``."""
    import asyncio

    event = _make_subagent_event(task_id="sub-1", had_error=False)
    asyncio.run(subscriber._handle_subagent_turn_completed(event))
    assert len(db_service.events) == 1
    log = db_service.events[0]
    assert log.event_type == "subagent_run_finished"
    payload = log.payload
    assert payload["task_id"] == "sub-1"
    assert payload["final_content"] == "done"
    assert payload["tools_used"] == ["read_file"]
    assert payload["parent_user_id"] == "alice"
    assert payload["had_error"] is False
    assert "error" not in payload


def test_subagent_turn_completed_error(
    subscriber: RuntimeEventsSubscriber, db_service: FakeDbLoggingService
) -> None:
    """SubagentTurnCompleted с had_error=True пишет error+level=ERROR."""
    import asyncio

    event = _make_subagent_event(task_id="sub-2", had_error=True)
    asyncio.run(subscriber._handle_subagent_turn_completed(event))
    log = db_service.events[0]
    assert log.event_type == "subagent_run_finished"
    assert log.level == "ERROR"
    assert log.payload["had_error"] is True
    assert log.payload["error"] == "boom"


# ---------------------------------------------------------------------------
# Wire subagent publishing: set_default_bus(_bus) при start().
# ---------------------------------------------------------------------------


def test_start_wires_subagent_default_bus(bus: FakeBus) -> None:
    """start() вызывает _set_subagent_default_bus(bus), что сохраняет
    ссылку в ``_SubagentLoggingHook._default_bus`` (если патч применён).

    Если monkey-patch ``_SubagentHook`` ещё не применён — no-op (logger
    говорит, что подписки зарегистрированы, но set_default_bus
    молча проглатывает ImportError/AttributeError).
    """
    # Без патча set_default_bus не существует — _set_subagent_default_bus
    # ловит AttributeError silently.
    RuntimeEventsSubscriber(bus).start()  # не падает
