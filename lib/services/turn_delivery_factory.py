"""Настраиваемый fallback-ответ на internal-ошибку — через публичную точку nanobot.

Заменяет патч ``RuntimePatcher.patch_turn_delivery_fail``. Решение и
обоснование — в ``docs/architecture/decisions/turn-delivery-public-extension.md``:
хук ``finalize_content`` текст ошибки не видит (он рождается в
``TurnDelivery.fail``), зато ``AgentLoop.__init__`` принимает
``turn_delivery_factory``.

Почему это лучше патча:

* **Один ответ конструктивно.** ``fail()`` ниже не вызывает ``super().fail()``,
  а публикует outbound сам, поэтому двойная публикация невозможна. Патчу для
  этого понадобился ``_OutboundSilencer`` — прокси поверх шины, подменявший
  атрибут экземпляра.
* **Нет зависимости от момента вызова.** Патч ловил исключение через
  ``sys.exception()`` внутри чужого ``except``-блока; здесь то же самое, но
  не в monkey-patch'е, а в собственном методе.
* **Поломка при апгрейде громкая и ранняя.** ``AgentLoop.__init__``
  валидирует переданную фабрику (``factory.bus is bus``), то есть
  несовместимость всплывёт на старте, а не у пользователя.

Ограничение, фиксируемое честно: переопределение дублирует ~8 строк
upstream-логики ``fail()`` — публикация и ``agent.completed``. Это осознанный
размен: патч зависел от приватного метода и от порядка вызовов, а эти строки
опираются на публичную сигнатуру.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

try:
    from nanobot.agent.turn_delivery import TurnDelivery, TurnDeliveryFactory

    UPSTREAM_AVAILABLE = True
except Exception:  # noqa: BLE001 - форк/заглушка nanobot без этого модуля
    # Модуль обязан импортироваться везде: голый импорт на верхнем уровне ронял
    # сборку агента там, где пакет nanobot собран не полностью. Старый патч в
    # такой ситуации не применялся и писал причину в отчёт — здесь будет так
    # же: класс-плейсхолдер даёт импортируемый модуль, а фабрика не
    # собирается и пользователь получает upstream-текст.
    TurnDelivery = object  # type: ignore[assignment,misc]
    TurnDeliveryFactory = object  # type: ignore[assignment,misc]

    UPSTREAM_AVAILABLE = False

_log = logging.getLogger("lib.turn_delivery_factory")

#: Текст fallback'а, когда оператор его не задал. Контракт с пользователем:
#: извинение и просьба переформулировать, без технических деталей.
DEFAULT_INTERNAL_ERROR_TEXT: str = (
    "Я не справился с вашим вопросом. "
    "Попробуйте, пожалуйста, переформулировать конкретнее — "
    "например, уточните ключевую часть или приведите пример."
)
DEFAULT_LOG_TO_DB: bool = True


class FallbackTurnDelivery(TurnDelivery):
    """``TurnDelivery``, чей ``fail()`` говорит пользователю настроенный текст.

    Экземпляры создаёт :class:`FallbackTurnDeliveryFactory`: класс подставляется
    подменой ``__class__`` уже собранного upstream-объекта, поэтому
    конструктор не переопределяется — фабрика проставляет поля после
    подмены.
    """

    _fallback_text: str = DEFAULT_INTERNAL_ERROR_TEXT
    _log_to_db: bool = DEFAULT_LOG_TO_DB
    _db_logging_service: Any = None
    _agent_id: str | None = None

    async def fail(self, *, publish_completion: bool) -> None:
        """Ответить пользователю и закрыть оборот.

        Ровно одна публикация outbound. ``super().fail()`` НЕ вызывается:
        он опубликовал бы второй, upstream-текст.
        """
        lifecycle = self.lifecycle_message
        metadata = dict(getattr(lifecycle, "metadata", None) or {})
        # ``_error_kind`` — по нему канал понимает, что это отказ, а не ответ
        # модели; ``_final_turn`` — что оборот закрыт этой публикацией.
        metadata["_error_kind"] = "internal"
        metadata["_final_turn"] = True

        await self._publish_fallback(lifecycle, metadata)
        self._log_turn_failed(lifecycle, publish_completion)

        if publish_completion:
            # Копия upstream-логики: оборот остаётся «failed» в рантайм-событиях.
            await self.runtime_event_publisher.turn_completed(
                channel=lifecycle.channel,
                chat_id=lifecycle.chat_id,
                session_key=self.session_key,
                metadata=lifecycle.metadata,
                outcome="failed",
                failure_kind="internal",
            )

    async def _publish_fallback(self, lifecycle: Any, metadata: dict[str, Any]) -> None:
        """Опубликовать fallback. Сбой публикации не должен ронять оборот."""
        from nanobot.bus.events import OutboundMessage

        try:
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=lifecycle.channel,
                    chat_id=lifecycle.chat_id,
                    content=self._fallback_text,
                    metadata=metadata,
                )
            )
        except Exception as exc:  # noqa: BLE001 - оборот должен закрыться
            _log.warning("fallback-ответ не опубликован: %s", exc)

    def _log_turn_failed(
        self, lifecycle: Any, publish_completion: bool
    ) -> None:
        """Записать ``agent.failed`` в журнал. Fail-open: сбой БД не важен."""
        if not self._log_to_db or self._db_logging_service is None:
            return
        from lib.services.db_logging_service import LogEvent, try_log_event

        # ``fail()`` зовётся изнутри ``except Exception`` в
        # ``AgentLoop._process_message``, поэтому активное исключение доступно
        # именно здесь. Вне ``except`` (юнит-тест) вернётся ``None`` — это
        # отражено флагом ``exception_available``, а не выдуманным текстом.
        exc = sys.exception()

        failure_error_kind = getattr(self, "_failure_error_kind", None)
        channel = lifecycle.channel
        chat_id = lifecycle.chat_id
        # У ``InboundMessage`` нет ``user_id``/``session_key`` (есть
        # ``sender_id``), поэтому идентификатор пользователя берём оттуда, а
        # ``session_key`` — с экземпляра delivery.
        sender_id = getattr(lifecycle, "sender_id", None)
        session_key = self.session_key

        try:
            try_log_event(
                self._db_logging_service,
                LogEvent(
                    event_type="agent.failed",
                    level="ERROR",
                    session_id=(
                        session_key if isinstance(session_key, str) else None
                    ),
                    channel=channel,
                    actor=None,
                    summary=str(failure_error_kind or "agent.failed"),
                    payload={
                        "kind": "internal",
                        "failure_error_kind": failure_error_kind,
                        "agent_id": self._agent_id,
                        "sender_id": sender_id,
                        "chat_id": chat_id,
                        "exception_type": (
                            type(exc).__name__ if exc is not None else None
                        ),
                        "exception_message": str(exc) if exc is not None else None,
                        "exception_available": exc is not None,
                    },
                    metadata={
                        "fallback_text_len": len(self._fallback_text),
                        "publish_completion": bool(publish_completion),
                    },
                    user_id=sender_id if isinstance(sender_id, str) else None,
                ),
                producer="turn_delivery_factory",
                event_type="agent.failed",
            )
        except Exception as exc_log:  # noqa: BLE001 - fail-open
            _log.warning("turn_failed не записан: %s", exc_log)


class FallbackTurnDeliveryFactory(TurnDeliveryFactory):
    """Фабрика, выдающая :class:`FallbackTurnDelivery` вместо upstream-класса.

    Маршрутизация остаётся upstream: ``create``/``unrouted`` вызываются через
    ``super()``, и подменяется только класс уже собранного экземпляра.
    """

    def __init__(
        self,
        bus: Any,
        *,
        internal_error: str = DEFAULT_INTERNAL_ERROR_TEXT,
        log_to_db: bool = DEFAULT_LOG_TO_DB,
        db_logging_service: Any = None,
        agent_id: str | None = None,
    ) -> None:
        # ``AgentLoop`` проверяет ``factory.bus is bus`` — идентичность
        # сохраняется, потому что объект тот же, что пришёл в конструктор.
        super().__init__(bus)
        self._fallback_text = internal_error
        self._log_to_db = log_to_db
        self._db_logging_service = db_logging_service
        self._agent_id = agent_id

    def create(
        self,
        msg: Any,
        session_key: str,
        *,
        enable_stream: bool = False,
    ) -> TurnDelivery:
        return self._adopt(
            super().create(msg, session_key, enable_stream=enable_stream)
        )

    def unrouted(self, msg: Any, session_key: str) -> TurnDelivery:
        return self._adopt(super().unrouted(msg, session_key))

    def _adopt(self, delivery: TurnDelivery) -> TurnDelivery:
        delivery.__class__ = FallbackTurnDelivery
        delivery._fallback_text = self._fallback_text
        delivery._log_to_db = self._log_to_db
        delivery._db_logging_service = self._db_logging_service
        delivery._agent_id = self._agent_id
        return delivery


def build_turn_delivery_factory(
    bus: Any,
    *,
    settings: Any = None,
    db_logging_service: Any = None,
    agent_id: str | None = None,
) -> FallbackTurnDeliveryFactory:
    """Собрать фабрику из конфигурации агента.

    Args:
        bus: шина, **обязательно тот же объект**, что у ``AgentLoop``.
        settings: merged ``SETTINGS``; читается
            ``gateway.error_messages.internal_error`` и ``.log_to_db``.
            ``None`` — дефолтный текст и ``log_to_db=True``.
        db_logging_service: ``DbLoggingService`` или ``None`` (запись пропускается).
        agent_id: идентификатор агента для колонки в журнале.
    Returns:
        Готовая фабрика либо ``None``, если upstream-модуль недоступен:
        тогда ``AgentLoop`` соберёт свою фабрику и пользователь увидит
        upstream-текст ошибки — как и до переноса.
    """
    if not UPSTREAM_AVAILABLE:
        _log.warning(
            "nanobot.agent.turn_delivery недоступен — настраиваемый "
            "fallback-ответ не подключён, пользователь увидит upstream-текст"
        )
        return None

    internal_error = _get(
        settings, "gateway", "error_messages", "internal_error", default=None,
    )
    # Пробельный текст отбрасывается наравне с пустым: иначе пользователь
    # получил бы сообщение из одних пробелов, что читается как «агент молчит».
    if not isinstance(internal_error, str) or not internal_error.strip():
        internal_error = DEFAULT_INTERNAL_ERROR_TEXT

    log_to_db = _get(
        settings, "gateway", "error_messages", "log_to_db", default=None,
    )
    if not isinstance(log_to_db, bool):
        log_to_db = DEFAULT_LOG_TO_DB

    return FallbackTurnDeliveryFactory(
        bus,
        internal_error=internal_error,
        log_to_db=log_to_db,
        db_logging_service=db_logging_service,
        agent_id=agent_id,
    )


def _get(source: Any, *path: str, default: Any = None) -> Any:
    """Чтение вложенного ключа из dict-проекции или объекта с атрибутами."""
    current = source
    for key in path:
        if current is None:
            return default
        if isinstance(current, dict):
            if key not in current:
                return default
            current = current[key]
        else:
            current = getattr(current, key, None)
    return default if current is None else current
