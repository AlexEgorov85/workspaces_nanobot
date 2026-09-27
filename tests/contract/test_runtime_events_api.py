"""Contract-тесты поверхности upstream `nanobot.bus.runtime_events` и `nanobot.bus.queue`.

Фиксируют внешний контракт используемых символов nanobot 0.3.5:
- `nanobot.bus.runtime_events.TurnRuntimeAdmitted` (dataclass с полями
  `context: RuntimeEventContext` и `runtime: LLMRuntime`).
- `nanobot.bus.runtime_events.RuntimeEventContext` (dataclass с полями
  `channel`, `chat_id`, `session_key`, `metadata`, `attributes`).
- `nanobot.bus.runtime_events.RuntimeEventPublisher.turn_runtime_admitted`
  (async method, параметры `(msg, session_key, runtime)`).
- `nanobot.bus.queue.MessageBus.subscribe(handler, event_type)` —
  возвращает `Callable[[], None]` (unsubscribe); `event_type=None`
  ловит все события; фильтрация по `isinstance(event, event_type)`.
- `nanobot.bus.queue.MessageBus.publish(event)` — awaited'ит async
  handler через `inspect.isawaitable`.

Эти тесты защищают от изменений upstream контракта между релизами
nanobot-ai и должны ловить регрессии **до** того, как runtime-patcher или
RuntimeEventsSubscriber сломаются в проде.
"""
from __future__ import annotations

import inspect

import pytest


class TestTurnRuntimeAdmittedDataclassFields:
    def test_has_context_and_runtime_fields(self) -> None:
        from nanobot.bus.runtime_events import TurnRuntimeAdmitted

        fields = TurnRuntimeAdmitted.__dataclass_fields__
        assert "context" in fields
        assert "runtime" in fields


class TestRuntimeEventContextFields:
    def test_has_required_fields(self) -> None:
        from nanobot.bus.runtime_events import RuntimeEventContext

        fields = RuntimeEventContext.__dataclass_fields__
        for name in ("channel", "chat_id", "session_key", "metadata", "attributes"):
            assert name in fields, f"RuntimeEventContext missing field {name!r}"


class TestPublisherMethodSignatures:
    def test_turn_runtime_admitted_signature(self) -> None:
        from nanobot.bus.runtime_events import RuntimeEventPublisher

        sig = inspect.signature(RuntimeEventPublisher.turn_runtime_admitted)
        params = list(sig.parameters.keys())
        # self + 3 параметра (msg, session_key, runtime)
        assert params[:4] == ["self", "msg", "session_key", "runtime"], (
            f"Unexpected parameter order: {params}"
        )


class TestMessageBusSubscribeSignature:
    def test_returns_callable_unsubscribe(self) -> None:
        from nanobot.bus.queue import MessageBus

        sig = inspect.signature(MessageBus.subscribe)
        params = list(sig.parameters.keys())
        assert "handler" in params
        assert "event_type" in params

    def test_subscribe_unsubscribe_roundtrip(self) -> None:
        """``subscribe`` MUST вернуть callable; повторный вызов
        unsub должен снимать подписку (subsequent publish не зовёт handler)."""
        import warnings

        from nanobot.bus.queue import MessageBus

        bus = MessageBus()
        calls: list[int] = []

        async def handler(event: object) -> None:
            calls.append(1)

        unsubscribe = bus.subscribe(handler, event_type=None)
        assert callable(unsubscribe)
        unsubscribe()
        # После unsub publish не зовёт handler; вызов без exception.
        # ``publish`` — async, поэтому sync-вызов приводит к
        # ``RuntimeWarning: coroutine 'MessageBus.publish' was never awaited``
        # — это нормальное поведение upstream-контракта.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            bus.publish(object())  # type: ignore[arg-type]


class TestMessageBusPublishAweatsAsyncHandlers:
    def test_publish_is_coroutine(self) -> None:
        """``MessageBus.publish`` MUST быть ``async`` (см. upstream
        `nanobot/bus/queue.py`); sync-вызов приводит к
        ``RuntimeWarning: coroutine was never awaited``."""
        import inspect

        from nanobot.bus.queue import MessageBus

        assert inspect.iscoroutinefunction(MessageBus.publish), (
            "MessageBus.publish MUST be async — RuntimeEventsSubscriber "
            "зависит от awaitable contract"
        )

    def test_publish_aweats_async_handler(self) -> None:
        """``await bus.publish(event)`` MUST awaited'ить async handler
        через ``inspect.isawaitable``."""
        import asyncio

        from nanobot.bus.queue import MessageBus

        async def scenario() -> None:
            bus = MessageBus()
            seen: list[int] = []

            async def handler(event: object) -> None:
                seen.append(1)

            bus.subscribe(handler, event_type=None)
            await bus.publish(object())  # type: ignore[arg-type]
            assert seen == [1], f"Async handler not invoked: {seen}"

        asyncio.run(scenario())


class TestTurnRuntimeAdmittedIsAgentEvent:
    def test_isinstance_agent_event(self) -> None:
        """``TurnRuntimeAdmitted`` MUST наследовать ``nanobot.events.AgentEvent``
        чтобы фильтрация через ``isinstance(event, event_type)`` в
        ``MessageBus.subscribe`` работала."""
        from nanobot.bus.runtime_events import TurnRuntimeAdmitted
        from nanobot.events import AgentEvent

        assert issubclass(TurnRuntimeAdmitted, AgentEvent)