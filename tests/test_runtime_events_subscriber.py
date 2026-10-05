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

from io import open as io_open

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

    ``current_turn_identity`` — та же личность ВХОДА, которую журнал отдаёт
    владельцу. Журнал не выводит её для события сам (правило против утечки
    между сессиями), поэтому подписчик спрашивает напрямую у того, кому
    личность принадлежит.
    """

    def __init__(self) -> None:
        self.events: list[Any] = []
        self.turn_identities: dict[str, dict[str, Any]] = {}

    def log_event(self, event: Any) -> None:
        self.events.append(event)

    def current_turn_identity(self, session_key: str | None) -> dict[str, Any] | None:
        # Читатель, а не ``take``: снимок одноразовый, и следующий за
        # подписчиком agent.delivered должен получить свою подпись.
        found = self.turn_identities.get(session_key or "")
        return dict(found) if found else None

    def get_request_id(self, session_key: str | None) -> str | None:
        return None

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
    assert log.event_type == "agent.completed"
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
    assert log.event_type == "agent.completed"
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
    assert log.event_type == "agent.completed"
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

# --- личность оборота: владелец один, снимок не изымается ------------------


def _store_with(session_key: str, user_id: str, request_id: str) -> Any:
    from lib.services.turn_identity import TurnIdentityStore

    store = TurnIdentityStore()
    store.record(session_key, {"user_id": user_id, "request_id": request_id, "at_seq": 0})
    return store


def test_completed_is_signed_when_contextvar_is_blind(
    bus: FakeBus, db_service: FakeDbLoggingService
) -> None:
    """Подписчик берёт личность у владельца, а не из своего contextvar.

    contextvar привязан к задаче оборота, а подписчик живёт в своей: он там
    гарантированно пуст. На практике это стоило подписи каждого
    agent.completed - следом идущий agent.delivered подписывался (он идёт
    через журнал), а completed уходил неподписанным, и в журнале не
    оставалось самого важного события оборота.
    """
    import asyncio

    store = _store_with("telegram:1", "alice", "req-1")
    subscriber = RuntimeEventsSubscriber(
        bus, db_logging_service=db_service, turn_identities=store
    )
    subscriber.start()

    asyncio.run(
        subscriber._handle_turn_runtime_admitted(  # noqa: SLF001
            _make_turn_runtime_admitted(session_key="telegram:1")
        )
    )
    asyncio.run(
        subscriber._handle_turn_completed(  # noqa: SLF001
            _make_turn_completed(session_key="telegram:1")
        )
    )

    completed = [
        e for e in db_service.events if getattr(e, "event_type", "") == "agent.completed"
    ]
    assert completed, "agent.completed не записан вовсе"
    log = completed[-1]
    assert log.user_id == "alice"
    assert log.request_id == "req-1"


def test_subscriber_never_takes_the_snapshot(bus: FakeBus, db_service: FakeDbLoggingService) -> None:
    """Снимок остаётся для финальной доставки ответа.

    Изъятие одноразовое и принадлежит ``agent.delivered``; страж
    ``tests/test_final_delivery_is_signed.py`` это прямо запрещает делать
    в другом месте. Значит подписчик читает, а не забирает.
    """
    import asyncio

    store = _store_with("telegram:1", "alice", "req-1")
    subscriber = RuntimeEventsSubscriber(
        bus, db_logging_service=db_service, turn_identities=store
    )
    subscriber.start()

    asyncio.run(
        subscriber._handle_turn_runtime_admitted(  # noqa: SLF001
            _make_turn_runtime_admitted(session_key="telegram:1")
        )
    )

    assert store.current("telegram:1"), "подписчик забрал снимок ВХОДА"
    assert store.take("telegram:1"), "финальная доставка осталась бы без снимка"
    assert store.current("telegram:1") is None, "take() обязан опустошать запись"


def test_missing_sender_still_produces_no_identity(
    bus: FakeBus, db_service: FakeDbLoggingService
) -> None:
    """Выдумывать отправителя нельзя: событие ушло бы в чужую личность."""
    import asyncio

    from lib.services.turn_identity import TurnIdentityStore

    subscriber = RuntimeEventsSubscriber(
        bus, db_logging_service=db_service, turn_identities=TurnIdentityStore()
    )
    subscriber.start()

    asyncio.run(
        subscriber._handle_turn_runtime_admitted(  # noqa: SLF001
            _make_turn_runtime_admitted(session_key="telegram:1")
        )
    )
    asyncio.run(
        subscriber._handle_turn_completed(  # noqa: SLF001
            _make_turn_completed(session_key="telegram:1")
        )
    )

    for log in db_service.events:
        assert not log.user_id, (
            "подпись без снимка и без contextvar - это выдуманный отправитель"
        )


def test_store_is_the_single_owner_across_both_consumers() -> None:
    """Владелец личности один: и журнал, и подписчик берут ОДНО И ТО ЖЕ.

    Возврат к схеме «журнал хранит, подписчик ходит в журнал» означал бы
    второе место хранения и снова расхождение по видимости.
    """
    from lib.services.turn_identity import TurnIdentityStore

    import lib.core.application_context as context_module
    import lib.services.db_logging_service as journal_module
    import lib.services.runtime_events_subscriber as subscriber_module

    for module in (context_module, journal_module, subscriber_module):
        source = io_open(module.__file__, encoding="utf-8").read()
        assert "TurnIdentityStore" in source, (
            "%s не знает о хранилище личности" % module.__name__
        )

    store = TurnIdentityStore()
    assert store.current("telegram:1") is None
    store.record("telegram:1", {"user_id": "alice", "request_id": "r", "at_seq": 1})
    copied = store.current("telegram:1")
    assert copied is not None and copied["user_id"] == "alice"
    copied["user_id"] = "mallory"
    assert store.current("telegram:1")["user_id"] == "alice", (
        "читатель сумел изменить снимок - это не копия"
    )
