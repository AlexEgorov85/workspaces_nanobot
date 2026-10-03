"""Тесты личности событий конца оборота (``agent.completed``/``agent.delivered``).

**Что чинится.** Журнал уходит на платформу операцией ``log_events``, которая
берёт ``session_id``/``user_id``/``request_id`` из контекста ВЫЗОВА. Группировка
батча (``log_transport.group_by_identity``) отправляет группу только если
``_IdentityKey.complete()`` — то есть есть ``session_id`` **и** ``user_id``.
Подставлять выдуманный ``user_id`` нельзя: это запись события в чужую
личность, ровно то, от чего журнал отказывается.

**Почему события оказывались без личности.** Оба события конца оборота
публикуются уже ПОСЛЕ ``DatabaseLoggingHook.after_run``, который дергает
``clear_request`` и опустошает индекс вопросов, и ПОСЛЕ снятия
``RequestContext``. К этому моменту подписывать событие нечем. Отсюда
живые данные: после перехода на MCP-транспорт из пяти оборотов дошли один
``outbound_final`` и один ``turn_completed`` — финальный ответ и завершение
оборота, то есть ровно то, что нужно оператору. (Имена записаны как они были
в боевой таблице ДО переименования; канонические — ``agent.delivered`` и
``agent.completed``.)

**Как чинится.** Личность снимается в середине оборота, на
``TurnRuntimeAdmitted``, где ``RequestContext`` привязан и индекс ещё полон;
значение — тот же ``InboundMessage.sender_id``, который уходит в
``register_request(user_id=...)``. Ничего не выдумывается: если личности нет,
событие уходит без неё, но потеря считается и логируется.

Имя таблицы берётся из конфигурации (``config.runtime_table``), а не
зашивается: страж ``test_no_hardcoded_table_names`` запрещает литералы.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from config import runtime_table
from lib.services.db_logging_service import DbLoggingService, LogEvent
from lib.services.log_transport import group_by_identity
from lib.services.runtime_events_subscriber import RuntimeEventsSubscriber

SESSION = "postgres:42"
CHANNEL = "postgres"
CHAT_ID = "42"
SENDER = "user-77"
REQUEST_ID = "msg-9001"


def _service() -> DbLoggingService:
    """Настоящий сервис с пустым DSN: события копятся в очереди, в БД не
    уходят. Worker не запускается — тест ничего не пишет."""
    return DbLoggingService(
        dsn="",
        table_name=runtime_table("gateway_logs"),
        question_runs_table=runtime_table("question_runs"),
    )


def _register_inbound(svc: DbLoggingService) -> None:
    """Шаг inbound: ``make_inbound_logger`` регистрирует вопрос в индексе."""
    svc.register_request(
        SESSION, REQUEST_ID,
        user_id=SENDER, chat_id=CHAT_ID, channel=CHANNEL,
    )


def _queued_events(svc: DbLoggingService) -> list[LogEvent]:
    return [item for item in svc._queue.queue if isinstance(item, LogEvent)]


def _by_type(svc: DbLoggingService, event_type: str) -> list[LogEvent]:
    return [e for e in _queued_events(svc) if e.event_type == event_type]


def _is_groupable(event: LogEvent) -> bool:
    """Отправит ли транспорт это событие: полная личность вызова."""
    return group_by_identity([event])[0].key.complete()


def _runtime_admitted(session_key: str = SESSION) -> Any:
    from nanobot.bus.runtime_events import RuntimeEventContext, TurnRuntimeAdmitted

    class _Runtime:
        context_window_tokens = 8000
        model = "test-model"

    return TurnRuntimeAdmitted(
        context=RuntimeEventContext(
            channel=CHANNEL, chat_id=CHAT_ID, session_key=session_key
        ),
        runtime=_Runtime(),
    )


def _turn_completed(session_key: str = SESSION) -> Any:
    from nanobot.bus.runtime_events import RuntimeEventContext, TurnCompleted

    return TurnCompleted(
        context=RuntimeEventContext(
            channel=CHANNEL, chat_id=CHAT_ID, session_key=session_key
        ),
        latency_ms=1200,
        outcome="completed",
    )


class _FakeBus:
    """Шина без подписок: подписчику достаточно прямых вызовов handler'ов."""

    def subscribe(self, handler: Any, event_type: type | None = None):
        return lambda: None


def _bind_turn_context(sender_id: str | None, session_key: str = SESSION):
    """Привязать ``RequestContext`` оборота — как это делает
    ``nanobot/agent/loop.py`` на время ``_process_message``."""
    from nanobot.agent.tools.context import RequestContext, request_context

    return request_context(
        RequestContext(
            channel=CHANNEL,
            chat_id=CHAT_ID,
            session_key=session_key,
            sender_id=sender_id,
        )
    )


@pytest.fixture
def subscriber() -> RuntimeEventsSubscriber:
    return RuntimeEventsSubscriber(_FakeBus(), db_logging_service=_service())


@pytest.fixture(autouse=True)
def _clean_context_bridge():
    """Мост контекстного окна — глобальный; чистим за собой."""
    from lib.hooks.database_logging_hook import _CONTEXT_BRIDGE

    _CONTEXT_BRIDGE.pop(SESSION, None)
    yield
    _CONTEXT_BRIDGE.pop(SESSION, None)


# ---------------------------------------------------------------------------
# Критерий 3: agent.completed несёт полную личность
# ---------------------------------------------------------------------------


class TestTurnCompletedCarriesIdentity:
    def test_turn_completed_is_groupable_after_clear_request(
        self, subscriber: RuntimeEventsSubscriber
    ) -> None:
        """Критерий 3: полный сценарий оборота.

        Порядок повторяет рантайм: inbound → допуск оборота (``sender_id``
        в контексте, индекс полон) → ``after_run`` чистит индекс →
        ``TurnCompleted``. Событие обязано остаться отправляемым.
        """
        svc = subscriber._db_logging_service
        _register_inbound(svc)

        with _bind_turn_context(SENDER):
            asyncio.run(subscriber._handle_turn_runtime_admitted(_runtime_admitted()))

        # То, что делает DatabaseLoggingHook.after_run в конце оборота.
        svc.clear_request(SESSION)

        asyncio.run(subscriber._handle_turn_completed(_turn_completed()))

        events = _by_type(svc, "agent.completed")
        assert len(events) == 1
        event = events[0]
        assert event.user_id == SENDER
        assert event.request_id == REQUEST_ID
        assert _is_groupable(event), (
            "событие без полной личности транспорт не отправит: группа "
            "неполная, вызов подписать нечем"
        )

    def test_inbound_would_still_be_groupable(self, subscriber) -> None:
        """Контроль: тест ловит именно потерю конца оборота, а не поломку
        механизма в целом — inbound с тем же request_id остаётся отправляемым.

        Заодно видно, почему ``agent.completed`` не подписывался сам:
        ``_resolve_event_user_id`` требует совпадения ``request_id``, а у
        события конца оборота его не было вовсе.
        """
        svc = subscriber._db_logging_service
        _register_inbound(svc)
        svc.log_inbound(
            session_id=SESSION,
            channel=CHANNEL,
            content="вопрос",
            sender_id=SENDER,
            request_id=REQUEST_ID,
        )
        assert _is_groupable(_by_type(svc, "agent.received")[0])

    def test_turn_completed_without_identity_is_not_faked(
        self, subscriber: RuntimeEventsSubscriber
    ) -> None:
        """Нет ``sender_id`` — ``user_id`` НЕ подставляется. Плейсхолдер вроде
        ``"unknown"`` записал бы событие в чужую личность."""
        svc = subscriber._db_logging_service
        _register_inbound(svc)
        with _bind_turn_context(None):
            asyncio.run(subscriber._handle_turn_runtime_admitted(_runtime_admitted()))
        svc.clear_request(SESSION)
        asyncio.run(subscriber._handle_turn_completed(_turn_completed()))

        event = _by_type(svc, "agent.completed")[0]
        assert event.user_id is None

    def test_loss_without_identity_is_visible(
        self, subscriber: RuntimeEventsSubscriber
    ) -> None:
        """Документированный выбор: потеря не молчаливая — WARNING и счётчик.

        Именно этого не хватало: событие уходило в очередь и исчезало в
        транспорте, а оператор узнавал об этом только по пустому журналу.
        """
        svc = subscriber._db_logging_service
        _register_inbound(svc)
        with _bind_turn_context(None):
            asyncio.run(subscriber._handle_turn_runtime_admitted(_runtime_admitted()))
        svc.clear_request(SESSION)

        assert subscriber.unidentified_turn_events() == 0
        asyncio.run(subscriber._handle_turn_completed(_turn_completed()))
        assert subscriber.unidentified_turn_events() == 1

    def test_identity_is_consumed_once_per_turn(
        self, subscriber: RuntimeEventsSubscriber
    ) -> None:
        """Снимок одноразовый: второй ``TurnCompleted`` той же сессии уже не
        получит личность первого оборота (иначе события разных оборотов
        одного чата подписались бы чужим ``user_id``)."""
        svc = subscriber._db_logging_service
        _register_inbound(svc)
        with _bind_turn_context(SENDER):
            asyncio.run(subscriber._handle_turn_runtime_admitted(_runtime_admitted()))
        svc.clear_request(SESSION)
        asyncio.run(subscriber._handle_turn_completed(_turn_completed()))
        asyncio.run(subscriber._handle_turn_completed(_turn_completed()))

        events = _by_type(svc, "agent.completed")
        assert [e.user_id for e in events] == [SENDER, None]
        assert subscriber.unidentified_turn_events() == 1

    def test_request_id_is_optional(self, subscriber: RuntimeEventsSubscriber) -> None:
        """``user_id`` обязателен для группировки, ``request_id`` — нет:
        событие оборота без request_id остаётся отправляемым."""
        svc = subscriber._db_logging_service
        with _bind_turn_context(SENDER):
            asyncio.run(subscriber._handle_turn_runtime_admitted(_runtime_admitted()))
        svc.clear_request(SESSION)
        asyncio.run(subscriber._handle_turn_completed(_turn_completed()))

        event = _by_type(svc, "agent.completed")[0]
        assert event.user_id == SENDER
        assert event.request_id is None
        assert _is_groupable(event)

    def test_stop_clears_pending_identities(
        self, subscriber: RuntimeEventsSubscriber
    ) -> None:
        """Снимки не переживают остановку подписчика: следующий ``start()``
        обслуживает уже другие обороты."""
        svc = subscriber._db_logging_service
        _register_inbound(svc)
        with _bind_turn_context(SENDER):
            asyncio.run(subscriber._handle_turn_runtime_admitted(_runtime_admitted()))
        subscriber.stop()
        svc.clear_request(SESSION)
        asyncio.run(subscriber._handle_turn_completed(_turn_completed()))
        assert _by_type(svc, "agent.completed")[0].user_id is None


# ---------------------------------------------------------------------------
# Критерий 4: agent.delivered
# ---------------------------------------------------------------------------


def _final_outbound() -> Any:
    """Финальный outbound оборота: маркер ``_final_turn`` ставит
    ``RuntimePatcher.patch_assemble_outbound``."""
    class _Msg:
        channel = CHANNEL
        chat_id = CHAT_ID
        content = "готовый ответ"
        media: list = []
        metadata = {"_final_turn": True}
        event = None

    return _Msg()


def test_outbound_final_is_groupable_after_clear_request() -> None:
    """Критерий 4: финальный ответ оборота должен быть отправляемым.

    Тот же сценарий, что и для ``agent.completed``: финальный outbound
    публикуется после ``clear_request``, поэтому подписать его нечем.

    Маркер ``xfail`` снят: он срабатывал на ИМЕНИ события
    (``outbound_final`` больше не существует, фильтр находил ноль строк, и
    тест падал не по той причине, ради которой был помечен). Теперь фильтр
    ловит настоящее событие, и тест проверяет свойство — а падение на нём
    означает реальную потерю финального ответа из журнала.
    """
    from lib.services.db_logging_bus import make_outbound_logger

    svc = _service()
    _register_inbound(svc)
    # То, что делает DatabaseLoggingHook.after_run в конце оборота.
    svc.clear_request(SESSION)

    asyncio.run(make_outbound_logger(svc)(_final_outbound()))

    events = _by_type(svc, "agent.delivered")
    assert len(events) == 1
    assert events[0].user_id == SENDER
    assert _is_groupable(events[0]), (
        "финальный ответ оборота — самое важное событие журнала; без полной "
        "личности транспорт отбросит группу как неподписанную"
    )
