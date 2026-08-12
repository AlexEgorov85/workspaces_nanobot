"""BusFactory — создание MessageBus и опциональная обёртка для логирования.

``MessageBus`` (см. ``nanobot.bus.queue``) — асинхронная очередь, через
которую каналы публикуют inbound-сообщения пользователя, а AgentLoop
публикует outbound-ответы.

У ``MessageBus`` нет встроенных хуков на ``publish_inbound``/``publish_outbound``.
Чтобы логировать содержимое сообщений БЕЗ monkey-patch'ей, ``BusFactory``
подменяет эти методы на async-обёртки, которые перед оригинальным
вызовом зовут пользовательский ``inbound_logger``/``outbound_logger``
(обычно это ``make_inbound_logger``/``make_outbound_logger`` из
``lib.services.db_logging_bus``).

Почему так, а не monkey-patch:
  * безопасно для тестов (можно не подменять, передав ``None``-логгеры);
  * логгеры изолированы в фабрике — легко добавить/убрать;
  * ошибка в логгере НЕ ломает публикацию сообщения (try/except в обёртке);
  * логгеры можно подменять динамически (hot-swap).

Использование::

    from lib.core.bus_factory import BusFactory
    from lib.services.db_logging_bus import make_inbound_logger, make_outbound_logger

    bus = BusFactory(
        inbound_logger=make_inbound_logger(db_logging_service),
        outbound_logger=make_outbound_logger(db_logging_service),
    ).create()
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional


class BusFactory:
    """Производство MessageBus, при необходимости — с логирующими обёртками.

    Параметры конструктора — опциональные async-callable, которые
    оборачивают соответствующий метод шины:

      * ``inbound_logger(msg)`` — вызывается ПЕРЕД ``publish_inbound``;
      * ``outbound_logger(msg)`` — вызывается ПЕРЕД ``publish_outbound``.

    Ошибки в логгерах глотаются (``except Exception: pass`` в обёртке) —
    логгер НИКОГДА не должен ломать поток сообщений.

    Attributes:
        _inbound_logger: callable или None.
        _outbound_logger: callable или None.
    """

    def __init__(
        self,
        inbound_logger: Optional[Callable[[Any], Awaitable[None]]] = None,
        outbound_logger: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> None:
        self._inbound_logger = inbound_logger
        self._outbound_logger = outbound_logger

    def create(self) -> Any:
        """Создать MessageBus, при необходимости обернув publish_* логгерами.

        Returns:
            ``MessageBus`` (или совместимый объект), у которого
            ``publish_inbound``/``publish_outbound`` при наличии логгеров
            становятся async-обёртками.
        """
        from nanobot.bus.queue import MessageBus

        bus = MessageBus()
        if self._inbound_logger is not None:
            self._wrap(bus, "publish_inbound", self._inbound_logger)
        if self._outbound_logger is not None:
            self._wrap(bus, "publish_outbound", self._outbound_logger)
        return bus

    @staticmethod
    def _wrap(
        bus: Any,
        method: str,
        logger: Callable[[Any], Awaitable[None]],
    ) -> None:
        """Заменить ``bus.<method>`` на async-обёртку ``await logger(); await original()``.

        Исходный метод сохраняется в замыкании. Если ``logger`` бросит
        исключение — оно глотается, оригинальный метод всё равно зовётся.
        Это критично: иначе один сломанный хук мог бы положить шину
        сообщений.
        """
        original = getattr(bus, method)

        async def wrapper(msg: Any) -> None:
            try:
                await logger(msg)
            except Exception:
                pass
            await original(msg)

        setattr(bus, method, wrapper)


def build_logging_bus(bus: Any, log_event: Callable[[str, str], None]) -> Any:
    """Синхронный shim: подменяет ``publish_outbound`` на запись в лог.

    Legacy-хелпер для сценариев, когда у вызывающего потока есть
    синхронный sink (``log_event(event, content)`` — обычная функция,
    не coroutine). В современном коде предпочтительнее использовать
    ``BusFactory`` с async-логгерами — они безопаснее и тестируются
    проще. Эта функция оставлена для обратной совместимости.

    Args:
        bus: уже созданный ``MessageBus`` (будет мутирован in-place).
        log_event: ``def(event_name: str, content: str) -> None``.

    Returns:
        Тот же объект ``bus`` (для удобства chaining-а).
    """
    original = bus.publish_outbound

    async def _wrapper(msg: Any) -> None:
        try:
            log_event("outbound", getattr(msg, "content", "") or "")
        except Exception:
            pass
        await original(msg)

    bus.publish_outbound = _wrapper
    return bus
