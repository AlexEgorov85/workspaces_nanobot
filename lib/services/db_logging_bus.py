"""Утилиты: связать DbLoggingService с MessageBus через обёртки.

Проблема: ``nanobot.bus.queue.MessageBus`` — простая асинхронная очередь,
у неё нет встроенных хуков на ``publish_inbound``/``publish_outbound``.
Чтобы логировать содержимое сообщений без monkey-patch'ей, мы передаём
async-callable-логгеры в ``BusFactory`` — и они оборачивают оригинальные
методы шины (см. ``BusFactory._wrap``).

Архитектура:

  Inbound (сообщение от пользователя в агенте)
    nanobot bus.publish_inbound(msg)
      → wrapper (BusFactory)
        → make_inbound_logger(service)(msg)
          → service.log_inbound(session_key, channel, content, message_id)
        → original publish_inbound(msg)

  Outbound (ответ агента пользователю)
    nanobot bus.publish_outbound(msg)
      → wrapper (BusFactory)
        → make_outbound_logger(service)(msg)
          → ``is_dropped(meta)`` (reasoning/tool_hint/progress/...) — drop;
          → ``is_stream_delta(meta)`` — логируем как ``outbound_delta``;
          → иначе — ``outbound_final``;
          → service.log_outbound(...)
        → original publish_outbound(msg)

Фильтрация служебных сигналов runner'а выполняется через единый
``lib.utils.outbound_meta`` — добавление новых флагов правится в одном
месте.

``latency_ms`` / ``tokens_used`` берутся из ``msg.metadata._turn`` (если
runner туда положил) — иначе None.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from lib.utils.outbound_meta import is_dropped, is_stream_delta, msg_session_key
from utils.media import serialize as media_serialize


def make_inbound_logger(
    service: Any, agent_id: Optional[str] = None
) -> Callable[[Any], Awaitable[None]]:
    """Создать async-логгер для ``MessageBus.publish_inbound``.

    Получает ``InboundMessage`` (см. ``nanobot.bus.events``) и достаёт:
      * ``session_key`` — формируется в ``InboundMessage.session_key``
        как ``f"{channel}:{chat_id}"`` (используется как PK сессии);
      * ``channel`` — telegram / cli / postgres / redis / ...;
      * ``content`` — текст сообщения;
      * ``message_id`` — из ``metadata`` (если канал его туда положил).

    Регистрирует контекст вопроса в сервисе (user_id/chat_id/agent_id),
    чтобы все последующие события вопроса (tool/run/outbound) несли
    эти поля. ``agent_id`` — id агента, обрабатывающего шину (опционален).

    Все ошибки глотаются — логгер не должен ломать публикацию сообщения,
    иначе агент зависнет. Если нужна диагностика — смотрите ``service.get_stats()``.

    Returns:
        Async-callable ``async def(msg) -> None`` для ``BusFactory``.
    """
    async def _log(msg: Any) -> None:
        try:
            session_key = msg_session_key(msg)
            channel = getattr(msg, "channel", "") or ""
            message_id = (getattr(msg, "metadata", {}) or {}).get("message_id")
            sender_id = getattr(msg, "sender_id", None) or None
            chat_id = getattr(msg, "chat_id", None) or None
            content = getattr(msg, "content", "") or ""
            media = [p for p in (getattr(msg, "media", None) or []) if isinstance(p, str) and p]
            media = media_serialize(media) if media else None
            if session_key and message_id:
                service.register_request(
                    session_key, message_id,
                    user_id=sender_id, chat_id=chat_id, channel=channel,
                    agent_id=agent_id,
                    question=content,
                    media=media or None,
                )
            service.log_inbound(
                session_id=session_key,
                channel=channel,
                content=content,
                message_id=message_id,
                sender_id=sender_id,
                chat_id=chat_id,
                request_id=message_id,
                media=media or None,
            )
        except Exception:
            pass
    return _log


def make_outbound_logger(
    service: Any, agent_id: Optional[str] = None
) -> Callable[[Any], Awaitable[None]]:
    """Создать async-логгер для ``MessageBus.publish_outbound``.

    Получает ``OutboundMessage`` (см. ``nanobot.bus.events``) и решает,
    писать ли его в БД:

      1. Если ``is_dropped(msg.metadata)`` — drop
         (reasoning/tool_hint/progress/turn_end/stream_end);
      2. Если ``is_stream_delta(msg.metadata)`` — это очередной чанк
         стриминг-ответа, логируем с ``kind="outbound_delta"``;
      3. Иначе — это финальный ответ оборота, логируем с
         ``kind="outbound_final"``.

    Контекст вопроса (user_id/agent_id/...) подхватывается из индекса
    сервиса по session_id (зарегистрирован при inbound).

    ``latency_ms`` и ``tokens_used`` достаются из ``msg.metadata._turn``
    (если ``_turn`` — dict; runner туда кладёт метрики). При отсутствии
    остаются ``None``.

    Все ошибки глотаются (см. ``make_inbound_logger``).
    """
    async def _log(msg: Any) -> None:
        try:
            meta = getattr(msg, "metadata", {}) or {}
            if is_dropped(meta):
                return
            kind = "outbound_delta" if is_stream_delta(meta) else "outbound_final"
            latency = None
            tokens = None
            turn_meta = meta.get("_turn") or {}
            if isinstance(turn_meta, dict):
                latency = turn_meta.get("latency_ms")
                tokens = turn_meta.get("tokens_used")
            ch = getattr(msg, "channel", "") or ""
            cid = getattr(msg, "chat_id", "") or ""
            session_id = f"{ch}:{cid}" if (ch or cid) else ""
            request_id = meta.get("message_id") or service.get_request_id(session_id)
            media = [p for p in (getattr(msg, "media", None) or []) if isinstance(p, str) and p]
            media = media_serialize(media) if media else None
            service.log_outbound(
                session_id=session_id,
                channel=ch,
                request_id=request_id,
                content=getattr(msg, "content", "") or "",
                latency_ms=latency,
                tokens_used=tokens,
                kind=kind,
                media=media or None,
            )
        except Exception:
            pass
    return _log
