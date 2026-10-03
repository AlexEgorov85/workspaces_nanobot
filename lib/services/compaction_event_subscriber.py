"""CompactionEventSubscriber — единый наблюдатель upstream-событий компакции.

Подписывается на ``OutboundMessage.event`` типа
``nanobot.events.ContextCompactionEvent`` (через фильтрацию
``bus.outbound`` при чтении каналом — см. ``lib/channels/postgres_channel.py``)
и вызывает публичный API
``ContextCompactionService.notify_session_compacted(...)`` для записи факта
в ``agent_gateway_logs`` (``event_type="agent.compacted"``) и/или
``agent_conversation_messages`` (history-notice).

Контракт:

  * handler-функции канала (``postgres_channel.send``)
    **должны** вызвать ``CompactionEventSubscriber.feed(outbound)``
    перед обработкой события: фильтр-логика общая, SRP не нарушается
    (канал по-прежнему не знает о бизнес-логике компакции).

  * handler-функции CLI-gateway (``lib/cli/console_loop.py::_run_cli_compact``)
    **также** зовут ``notify_session_compacted`` напрямую — минуя
    ``bus.outbound`` (CLI не публикует events через шину).

Файл отделён от ``ContextCompactionService``, чтобы последний не
зависел от asyncio-инфраструктуры шины (``MessageBus.consume_outbound``)
и мог использоваться в синхронных unit-тестах.
"""
from __future__ import annotations

from typing import Any

from loguru import logger


class CompactionEventSubscriber:
    """Подписчик на ``OutboundMessage.event`` типа ``ContextCompactionEvent``.

    Канал (postgres/redis) вызывает ``feed(outbound)`` при
    каждом ``OutboundMessage``. Subscriber фильтрует события по
    ``isinstance(msg.event, ContextCompactionEvent)`` и зовёт
    публичный API ``ContextCompactionService``.

    Для всех фаз (``started``/``succeeded``/``failed``/``cancelled``)
    пишется ``event_type="agent.compacted"`` в долговечный
    ``agent_gateway_logs``. History-notice в ``agent_conversation_messages``
    пишется **только** для ``succeeded``.
    """

    def __init__(
        self,
        *,
        compaction_service: Any = None,
    ) -> None:
        self._service = compaction_service

    def set_service(self, compaction_service: Any) -> None:
        """Установить ``ContextCompactionService`` после конструирования.

        Подходит для случая, когда ``RuntimePatcher.apply_all`` создаёт
        сервис позже, чем инициализируется subscriber (типовая схема
        lifecycle).
        """
        self._service = compaction_service

    async def feed(self, outbound: Any) -> None:
        """Обработать один ``OutboundMessage``.

        Если ``outbound.event`` — ``ContextCompactionEvent``, зовёт
        ``ContextCompactionService.notify_session_compacted(session_key,
        phase, compaction_id)``. Безопасна: при отсутствии сервиса или
        исключении пишется только warning, обработка других фаз/сообщений
        не затрагивается.
        """
        event = getattr(outbound, "event", None)
        if event is None:
            return
        try:
            from nanobot.events import ContextCompactionEvent
        except Exception:
            return
        if not isinstance(event, ContextCompactionEvent):
            return
        session_key = getattr(outbound, "session_key", None) or ""
        compaction_id = getattr(event, "compaction_id", "") or ""
        phase = getattr(event, "phase", "") or ""
        if not session_key or not phase:
            return
        svc = self._service
        if svc is None:
            return
        try:
            await svc.notify_session_compacted(
                session_key=session_key,
                phase=phase,
                compaction_id=compaction_id,
            )
        except AttributeError:
            logger.opt(exception=True).warning(
                "CompactionEventSubscriber: service has no "
                "notify_session_compacted ({})",
                type(svc).__name__,
            )
        except Exception:
            logger.opt(exception=True).warning(
                "CompactionEventSubscriber failed for {} phase={}",
                session_key, phase,
            )
