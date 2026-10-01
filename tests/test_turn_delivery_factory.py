"""Контракт fallback'а на internal-ошибку через публичную точку nanobot.

Тестируется не заглушка, а настоящий ``TurnDeliveryFactory`` из установленного
nanobot: патчем подменялся метод класса, здесь же подменяется класс целиком, и
ошибка могла бы прятаться в деталях конструктора (``session_key``, ``route``,
``runtime_event_publisher``), которые фейковый класс не воспроизводит.

Стражи построены на правиле проекта «проверяется на заведомо плохих данных»:

* ``test_exactly_one_publication`` — если реализация начнёт звать
  ``super().fail()`` или публиковать дважды, тест упадёт на количестве.
* ``test_both_creation_paths_are_adopted`` — если фабрика перестанет подменять
  класс в ``unrouted`` (или в ``create``), упадёт на типе.
* ``test_turn_completed_still_published`` — если потеряется закрытие оборота
  в рантайм-событиях, упадёт на отсутствии вызова.
* ``test_logging_failure_is_swallowed`` — если запись в журнал начнёт бросать,
  упадёт на поднятом исключении.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from nanobot.agent.turn_delivery import TurnDelivery
from nanobot.bus.events import InboundMessage

from lib.services.turn_delivery_factory import (
    DEFAULT_INTERNAL_ERROR_TEXT,
    FallbackTurnDelivery,
    FallbackTurnDeliveryFactory,
    build_turn_delivery_factory,
)


class _Bus:
    """Шина, которая помнит всё опубликованное."""

    def __init__(self) -> None:
        self.outbound: list[Any] = []
        self.sessions: list[Any] = []

    async def publish_outbound(self, msg: Any) -> None:
        self.outbound.append(msg)


class _PublisherSpy:
    """Вместо рантайм-публикатора — счётчик вызовов ``turn_completed``."""

    def __init__(self) -> None:
        self.completed: list[dict[str, Any]] = []

    async def turn_completed(self, **kwargs: Any) -> None:
        self.completed.append(kwargs)


def _msg() -> InboundMessage:
    return InboundMessage(
        channel="cli",
        sender_id="u1",
        chat_id="c1",
        content="вопрос",
        timestamp=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        metadata={"thread": "t1"},
    )


def _factory(bus: _Bus, **kwargs: Any) -> FallbackTurnDeliveryFactory:
    factory = FallbackTurnDeliveryFactory(bus, **kwargs)
    # Подмена класса на живом экземпляре должна сохранить publisher фабрики —
    # иначе ``turn_completed`` нечем публиковать.
    delivery = factory.create(_msg(), "sess1")
    delivery.runtime_event_publisher = _PublisherSpy()
    return factory


class TestSingleFallbackAnswer:
    @pytest.mark.asyncio
    async def test_exactly_one_publication(self):
        """Пользователь получает ровно один ответ, а не два.

        Страж на заведомо плохие данные: возврат ``super().fail()`` даст вторую
        публикацию с upstream-литералом — этот тест на ней и падает.
        """
        bus = _Bus()
        factory = _factory(bus)
        delivery = factory.create(_msg(), "sess1")
        delivery.runtime_event_publisher = _PublisherSpy()

        await delivery.fail(publish_completion=False)

        assert len(bus.outbound) == 1, (
            f"ожидалась одна публикация, их {len(bus.outbound)}: "
            f"{[m.content for m in bus.outbound]}"
        )
        assert bus.outbound[0].content == DEFAULT_INTERNAL_ERROR_TEXT
        assert "Sorry, I encountered an error" not in bus.outbound[0].content

    @pytest.mark.asyncio
    async def test_metadata_marks_internal_refusal(self):
        bus = _Bus()
        delivery = _factory(bus).create(_msg(), "sess1")
        delivery.runtime_event_publisher = _PublisherSpy()

        await delivery.fail(publish_completion=True)

        metadata = bus.outbound[0].metadata
        assert metadata["_error_kind"] == "internal"
        assert metadata["_final_turn"] is True
        # Исходные метаданные lifecycle-сообщения сохраняются.
        assert metadata["thread"] == "t1"

    @pytest.mark.asyncio
    async def test_configured_text_is_used(self):
        bus = _Bus()
        factory = _factory(bus, internal_error="Служба недоступна, попробуйте позже.")
        delivery = factory.create(_msg(), "sess1")
        delivery.runtime_event_publisher = _PublisherSpy()

        await delivery.fail(publish_completion=False)

        assert bus.outbound[0].content == "Служба недоступна, попробуйте позже."

    @pytest.mark.asyncio
    async def test_publish_failure_does_not_break_the_turn(self):
        """Сбой публикации не должен ронять оборот — он уже в обработчике."""

        class _BrokenBus(_Bus):
            async def publish_outbound(self, msg: Any) -> None:
                raise RuntimeError("шина мертва")

        delivery = _factory(_BrokenBus()).create(_msg(), "sess1")
        delivery.runtime_event_publisher = _PublisherSpy()

        await delivery.fail(publish_completion=True)  # не должно бросить


class TestBothCreationPathsAdopted:
    @pytest.mark.asyncio
    async def test_both_creation_paths_are_adopted(self):
        """Подмена класса нужна и в ``create``, и в ``unrouted``.

        ``AgentLoop`` создаёт delivery обоими путями (loop.py:1219 и 1427),
        поэтому подмена только одного оставила бы пользователя с
        upstream-литералом в одном из сценариев.
        """
        bus = _Bus()
        factory = _factory(bus)

        routed = factory.create(_msg(), "sess1")
        unrouted = factory.unrouted(_msg(), "sess2")

        for label, delivery in (("create", routed), ("unrouted", unrouted)):
            assert isinstance(delivery, FallbackTurnDelivery), (
                f"{label}: фабрика не подменила класс — вернулся "
                f"{type(delivery).__name__}, и пользователь увидит "
                f"upstream-текст"
            )
            delivery.runtime_event_publisher = _PublisherSpy()
            before = len(bus.outbound)
            await delivery.fail(publish_completion=False)
            assert len(bus.outbound) - before == 1, (
                f"{label}: за этот оборот опубликовано "
                f"{len(bus.outbound) - before} ответов вместо одного"
            )

    def test_route_and_session_key_survive_adoption(self):
        """Подмена класса не теряет то, что собрал upstream."""
        factory = _factory(_Bus())
        delivery = factory.create(_msg(), "sess-42")

        assert delivery.session_key == "sess-42"
        assert delivery.route.channel == "cli"
        assert delivery.route.chat_id == "c1"
        assert delivery.input_message.content == "вопрос"

    def test_agent_loop_accepts_the_factory(self):
        """Ровно то условие, которое проверяет ``AgentLoop.__init__``.

        Если начнёт проверяться что-то ещё (например, тип фабрики), страж
        упадёт здесь, а не у пользователя на старте.
        """
        bus = _Bus()
        assert FallbackTurnDeliveryFactory(bus).bus is bus


class TestTurnCompletedStillPublished:
    @pytest.mark.asyncio
    async def test_turn_completed_still_published(self):
        bus = _Bus()
        delivery = _factory(bus).create(_msg(), "sess1")
        spy = _PublisherSpy()
        delivery.runtime_event_publisher = spy

        await delivery.fail(publish_completion=True)

        assert len(spy.completed) == 1, "оборот не закрыт в рантайм-событиях"
        call = spy.completed[0]
        assert call["outcome"] == "failed"
        assert call["failure_kind"] == "internal"
        assert call["session_key"] == "sess1"

    @pytest.mark.asyncio
    async def test_no_completion_event_when_not_requested(self):
        delivery = _factory(_Bus()).create(_msg(), "sess1")
        spy = _PublisherSpy()
        delivery.runtime_event_publisher = spy

        await delivery.fail(publish_completion=False)

        assert spy.completed == []


class TestTurnFailedIsLogged:
    @pytest.mark.asyncio
    async def test_logs_turn_failed_with_exception_context(self):
        from lib.services.db_logging_service import LogEvent

        logged: list[LogEvent] = []
        bus = _Bus()

        class _Svc:
            def log_event(self, event: LogEvent) -> bool:
                logged.append(event)
                return True

            def is_running(self) -> bool:
                return True

        delivery = _factory(
            bus, db_logging_service=_Svc(), agent_id="agent-7",
        ).create(_msg(), "sess-9")
        delivery.runtime_event_publisher = _PublisherSpy()
        delivery._failure_error_kind = "RuntimeError"

        try:
            raise ValueError("сломалось")
        except ValueError as exc:
            await delivery.fail(publish_completion=False)
            assert exc is not None

        assert len(logged) == 1, "turn_failed не записан"
        event = logged[0]
        assert event.event_type == "turn_failed"
        assert event.level == "ERROR"
        assert event.session_id == "sess-9"
        assert event.user_id == "u1"
        assert event.payload["kind"] == "internal"
        assert event.payload["failure_error_kind"] == "RuntimeError"
        assert event.payload["agent_id"] == "agent-7"
        # Детали исключения уходят в журнал, а не пользователю.
        assert event.payload["exception_type"] == "ValueError"
        assert event.payload["exception_message"] == "сломалось"
        assert event.payload["exception_available"] is True
        assert "сломалось" not in bus.outbound[0].content

    @pytest.mark.asyncio
    async def test_logging_failure_is_swallowed(self):
        """Сбой записи в журнал не должен обрывать оборот."""

        class _BrokenSvc:
            def log_event(self, event: Any) -> bool:
                raise RuntimeError("БД недоступна")

            def is_running(self) -> bool:
                return True

        bus = _Bus()
        delivery = _factory(bus, db_logging_service=_BrokenSvc()).create(
            _msg(), "sess1"
        )
        spy = _PublisherSpy()
        delivery.runtime_event_publisher = spy

        await delivery.fail(publish_completion=True)  # не должно бросить

        assert len(bus.outbound) == 1
        assert len(spy.completed) == 1

    @pytest.mark.asyncio
    async def test_no_logging_when_disabled(self):
        class _Svc:
            def log_event(self, event: Any) -> bool:
                raise AssertionError("логирование выключено, звать нельзя")

            def is_running(self) -> bool:
                return True

        delivery = _factory(
            _Bus(), db_logging_service=_Svc(), log_to_db=False,
        ).create(_msg(), "sess1")
        delivery.runtime_event_publisher = _PublisherSpy()

        await delivery.fail(publish_completion=False)


class TestBuildFromSettings:
    def test_reads_text_and_flag_from_dict_projection(self):
        settings = {
            "gateway": {
                "error_messages": {
                    "internal_error": "Извините, не получилось.",
                    "log_to_db": False,
                }
            }
        }
        factory = build_turn_delivery_factory(_Bus(), settings=settings)

        assert factory._fallback_text == "Извините, не получилось."
        assert factory._log_to_db is False

    def test_reads_from_attribute_projection(self):
        class _Settings:
            class gateway:  # noqa: N801 - повторяет форму конфигурации
                class error_messages:  # noqa: N801
                    internal_error = "Из атрибутов."
                    log_to_db = True

        factory = build_turn_delivery_factory(_Bus(), settings=_Settings())

        assert factory._fallback_text == "Из атрибутов."
        assert factory._log_to_db is True

    def test_defaults_when_settings_missing(self):
        factory = build_turn_delivery_factory(_Bus())

        assert factory._fallback_text == DEFAULT_INTERNAL_ERROR_TEXT
        assert factory._log_to_db is True

    def test_empty_text_falls_back_to_default(self):
        """Пустой текст — это «оператор стёр настройку», а не «молчи»."""
        settings = {"gateway": {"error_messages": {"internal_error": "   "}}}
        factory = build_turn_delivery_factory(_Bus(), settings=settings)

        assert factory._fallback_text == DEFAULT_INTERNAL_ERROR_TEXT

    def test_non_bool_flag_falls_back_to_default(self):
        settings = {"gateway": {"error_messages": {"log_to_db": "да"}}}
        factory = build_turn_delivery_factory(_Bus(), settings=settings)

        assert factory._log_to_db is True
