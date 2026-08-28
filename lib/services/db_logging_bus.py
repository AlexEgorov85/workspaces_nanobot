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
           → ``is_outbound_noise(msg)`` (stream-delta/stream-end/progress/
             reasoning/retry-wait) — drop (бесполезный шум, раздувает таблицу);
           → ``is_outbound_final(msg)`` (``_final_turn`` в meta / StreamedResponseEvent)
             — ``outbound_final``;
           → иначе (промежуточный ``message(...)`` агента) — ``outbound_intermediate``;
           → service.log_outbound(...)
         → original publish_outbound(msg)

Фильтрация служебных сигналов runner'а выполняется через единый
``lib.utils.outbound_meta`` — добавление новых флагов правится в одном
месте.

``latency_ms`` / ``tokens_used`` берутся из ``msg.metadata._turn`` (если
runner туда положил) — иначе None.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from utils.media import serialize as media_serialize

from lib.utils.outbound_meta import is_outbound_final, is_outbound_noise, msg_session_key


def make_inbound_logger(
    service: Any, agent_id: str | None = None
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
            # request_id: берём message_id, если канал его передаёт, иначе
            # генерируем стабильный UUID. БЕЗ этого websocket-канал
            # (message_id=None) никогда не регистрировал question_runs, и
            # ~97% событий оставались без request_id (не джойнились к
            # agent_question_runs). См. fix request_id-linkage.
            request_id = message_id or str(uuid.uuid4())
            if session_key:
                service.register_request(
                    session_key, request_id,
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
                request_id=request_id,
                media=media or None,
            )
        except Exception:
            pass
    return _log


def make_outbound_logger(
    service: Any, agent_id: str | None = None
) -> Callable[[Any], Awaitable[None]]:
    """Создать async-логгер для ``MessageBus.publish_outbound``.

    Получает ``OutboundMessage`` (см. ``nanobot.bus.events``) и решает,
    писать ли его в БД (единый контракт — ``lib.utils.outbound_meta``):

      1. ``is_outbound_noise(msg)`` — stream-delta (каждый токен стрима) /
         stream-end / progress / reasoning / retry-wait → drop (шум, не
         несёт аналитической ценности, только раздувает таблицу);
      2. ``is_outbound_final(msg)`` (``_final_turn`` в meta ИЛИ
         ``StreamedResponseEvent``) — финальный ответ оборота,
         ``kind="outbound_final"``;
      3. иначе — промежуточный ``message(...)`` агента в течение оборота,
         ``kind="outbound_intermediate"`` (ценен для анализа поведения).

    Контекст вопроса (user_id/agent_id/...) подхватывается из индекса
    сервиса по session_id (зарегистрирован при inbound).

    ``latency_ms`` и ``tokens_used`` достаются из ``msg.metadata._turn``
    (если ``_turn`` — dict; runner туда кладёт метрики). При отсутствии
    остаются ``None``.

    Все ошибки глотаются (см. ``make_inbound_logger``).
    """
    async def _log(msg: Any) -> None:
        try:
            if is_outbound_noise(msg):
                return
            meta = getattr(msg, "metadata", {}) or {}
            kind = "outbound_final" if is_outbound_final(msg) else "outbound_intermediate"
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
