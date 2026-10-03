"""Утилиты: связать DbLoggingService с MessageBus через обёртки.

Проблема: ``nanobot.bus.queue.MessageBus`` — простая асинхронная очередь,
у неё нет встроенных хуков на ``publish_inbound``/``publish_outbound``.
Чтобы логировать содержимое сообщений без monkey-patch'ей, мы передаём
async-callable-логгеры в ``ApplicationContext._create_bus`` — и те оборачивают
оригинальные методы шины (см. ``_wrap_bus_publish``).

Архитектура:

  Inbound (сообщение от пользователя в агенте)
    nanobot bus.publish_inbound(msg)
      → wrapper (_create_bus)
        → make_inbound_logger(service)(msg)
          → service.log_inbound(session_key, channel, content, message_id)
        → original publish_inbound(msg)

   Outbound (ответ агента пользователю)
     nanobot bus.publish_outbound(msg)
       → wrapper (_create_bus)
         → make_outbound_logger(service)(msg)
           → ``is_outbound_noise(msg)`` (stream-delta/stream-end/progress/
             reasoning/retry-wait) — drop (бесполезный шум, раздувает таблицу);
           → ``is_outbound_final(msg)`` (``_final_turn`` в meta / StreamedResponseEvent)
             — пишется ``agent.delivered``;
           → иначе (промежуточный ``message(...)`` агента) — НЕ пишется:
             промежуточные ответы — не-событие (этап 10 непокрытых этапов,
             канонического имени для них в словаре платформы нет);
           → service.log_outbound(...)
         → original publish_outbound(msg)

Фильтрация служебных сигналов runner'а выполняется через единый
``lib.utils.outbound_meta`` — добавление новых флагов правится в одном
месте.

``latency_ms`` / ``tokens_used`` берутся из ``msg.metadata._turn`` (если
runner туда положил) — иначе None.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from utils.media import serialize as media_serialize

from lib.utils.outbound_meta import is_outbound_final, is_outbound_noise, msg_session_key

logger = logging.getLogger(__name__)


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

    Логгер не имеет права уронить публикацию сообщения (иначе агент
    зависнет), поэтому отказ перехватывается — но не проглатывается: он
    уходит в лог с трассировкой и в счётчик ``registration_failures``.
    Диагностика: ``service.get_stats()``. Кто инициировал входящее (``actor``),
    решает сам сервис: у сообщения из очереди на том конце producer, а не
    человек (см. ``DbLoggingService.log_inbound``).

    Returns:
        Async-callable ``async def(msg) -> None`` для ``_create_bus``.
    """
    async def _log(msg: Any) -> None:
        session_key = msg_session_key(msg)
        try:
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
                # Отдельно от записи входящего: сбой регистрации убивает и
                # request_id индекса, то есть ВСЕ события оборота остаются без
                # связи с прогоном. Раньше это уходило в общий ``except`` и
                # терялось целиком — теперь видно и в логе, и в счётчике.
                try:
                    service.register_request(
                        session_key, request_id,
                        user_id=sender_id, chat_id=chat_id, channel=channel,
                        agent_id=agent_id,
                        question=content,
                        media=media or None,
                    )
                except Exception as exc:  # noqa: BLE001 - публикацию не роняем
                    logger.warning(
                        "входящее: контекст вопроса не зарегистрирован "
                        "(session=%s request_id=%s): %s",
                        session_key, request_id, exc, exc_info=True,
                    )
                    _note_registration_failure(service, exc)
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
        except Exception as exc:  # noqa: BLE001 - публикацию не роняем
            # Логгер не имеет права уронить ход агента, но и терять след
            # нельзя: раньше здесь стоял ``pass`` и падение записи входящего
            # было неотличимо от «сообщения не было».
            logger.warning(
                "входящее не залогировано (session=%s): %s",
                session_key, exc, exc_info=True,
            )
    return _log


def _note_registration_failure(service: Any, exc: Exception) -> None:
    """Отметить потерю регистрации в статистике сервиса (если он её умеет).

    Сервис-контракт расширяется свободно: чужой/заглушечный сервис без метода
    просто ничего не получит, и это не повод ронять публикацию.
    """
    note = getattr(service, "record_registration_failure", None)
    if callable(note):
        try:
            note(str(exc))
        except Exception:  # noqa: BLE001 - счётчик не должен ломать публикацию
            logger.warning("счётчик регистраций недоступен", exc_info=True)


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
         ``StreamedResponseEvent``) — финальный ответ оборота, пишется
         ``event_type="agent.delivered"``;
      3. иначе — промежуточный ``message(...)`` агента в течение оборота:
         **не пишется**. Это не потеря наблюдаемости, а решение заказчика:
         промежуточные ответы внесены в список непокрытых этапов, которые
         сознательно остаются не-событиями, и канонического имени для них в
         словаре платформы нет. Подставлять сюда чужое имя (например
         ``agent.delivered``) было бы ложью в журнале, а оставлять
         ``outbound_intermediate`` — отказом батча при ``strict``.

    Контекст вопроса (user_id/agent_id/...) подхватывается из индекса
    сервиса по session_id (зарегистрирован при inbound).

    ``latency_ms`` и ``tokens_used`` достаются из ``msg.metadata._turn``
    (если ``_turn`` — dict; runner туда кладёт метрики). При отсутствии
    остаются ``None``.

    Отказ перехватывается, чтобы не уронить публикацию, но и не теряется
    молча (см. ``make_inbound_logger``).
    """
    async def _log(msg: Any) -> None:
        try:
            if is_outbound_noise(msg):
                return
            if not is_outbound_final(msg):
                return
            meta = getattr(msg, "metadata", {}) or {}
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
                media=media or None,
            )
        except Exception as exc:  # noqa: BLE001 - публикацию не роняем
            logger.warning("исходящее не залогировано: %s", exc, exc_info=True)
    return _log
