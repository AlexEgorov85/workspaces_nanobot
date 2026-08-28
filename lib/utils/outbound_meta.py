"""Служебные ключи в OutboundMessage.metadata, которые runner ставит
в течение оборота.

Сейчас это сигналы прогресса / рассуждений / потоковых чанков, которые
НЕ интересуют пользователя и логирование. Канал и CLI должны дропать их
или буферизовать (reasoning), а DB-логирование — пропускать как drop.
Эти имена — КОНТРАКТ между фреймворком nanobot и всеми местами
потребления, поэтому хранятся в одном месте.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: Метаданные с любым из этих ключей — служебный сигнал,
#: не предназначенный для конечного пользователя.
OUTBOUND_DROPPED_KEYS: tuple[str, ...] = (
    "_reasoning_delta",   # очередной фрагмент chain-of-thought
    "_reasoning_end",     # маркер конца размышлений
    "_tool_hint",         # визуальный разделитель (UI-подсказка)
    "_progress",          # обновление прогресс-бара
    "_turn_end",          # сигнал окончания оборота
    "_stream_end",        # сигнал конца стрим-чанка
)

#: Маркер финального outbound оборота, который ставит патч
#: ``RuntimePatcher.patch_assemble_outbound``. НЕ входит в
#: ``OUTBOUND_DROPPED_KEYS`` намеренно: каналы должны финализировать оборот
#: на нём (а не бросать), а потоковые каналы (Redis) — передавать ответ
#: пользователю как обычно. Отличает финал от промежуточных публикаций тула
#: ``message(...)``, которые приходят в течение оборота.
FINAL_TURN_KEY: str = "_final_turn"


def is_dropped(metadata: Mapping[str, Any] | None) -> bool:
    """True, если в metadata есть любой из ``OUTBOUND_DROPPED_KEYS``.

    Каналы, CLI-цикл и DB-шина используют эту проверку как единый
    источник истины — добавление нового служебного флага правится
    только здесь.
    """
    if not metadata:
        return False
    return any(k in metadata for k in OUTBOUND_DROPPED_KEYS)


def is_stream_delta(metadata: Mapping[str, Any] | None) -> bool:
    """True, если metadata содержит ``_stream_delta`` (чанк стрима).

    Legacy-проверка: в nanobot 0.3.0 потоковые чанки несут типизированный
    ивент ``StreamDeltaEvent`` в ``msg.event`` (см. ``is_outbound_noise``),
    а не флаг в metadata. Оставлена для обратной совместимости.
    """
    if not metadata:
        return False
    return bool(metadata.get("_stream_delta"))


def _typed_event(msg: Any) -> Any | None:
    """Вернуть типизированный outbound-ивент nanobot из ``msg`` (или None).

    nanobot 0.3.0 несёт события в ``msg.event``; legacy-флаги в ``metadata``
    — только fallback. Возвращает сам объект ивента (для ``isinstance``) либо
    ``None`` при отсутствии nanobot/ивента.
    """
    evt = getattr(msg, "event", None)
    if evt is not None:
        return evt
    try:
        from nanobot.bus.outbound_events import outbound_event_from_message
    except Exception:
        return None
    try:
        return outbound_event_from_message(msg)
    except Exception:
        return None


def is_outbound_final(msg: Any) -> bool:
    """True, если сообщение — финальный ответ оборота (логируем как ``outbound_final``).

    Маркеры (единый контракт ``lib.utils.outbound_meta``):
      * ``FINAL_TURN_KEY`` (``_final_turn``) в ``metadata`` — ставит
        ``RuntimePatcher.patch_assemble_outbound`` на финальном outbound
        любого оборота;
      * ``StreamedResponseEvent`` — финальный СОБРАННЫЙ стриминг-ответ
        (``turn_delivery.background_response``).
    """
    meta = getattr(msg, "metadata", None) or {}
    if FINAL_TURN_KEY in meta:
        return True
    evt = _typed_event(msg)
    try:
        from nanobot.bus.outbound_events import StreamedResponseEvent
    except Exception:
        return False
    return isinstance(evt, StreamedResponseEvent)


def is_outbound_noise(msg: Any) -> bool:
    """True для служебных/потоковых событий БЕЗ аналитической ценности.

    Сюда относятся stream-delta (каждый токен), stream-end, progress/
    reasoning/tool_hint (``OUTBOUND_DROPPED_KEYS``) и retry-wait. Их НЕ нужно
    писать в журнал событий: они дублируют финальный ответ и только раздувают
    таблицу (в проде десятки тысяч строк по 2–5 символов на один ответ).
    """
    meta = getattr(msg, "metadata", None) or {}
    if is_dropped(meta):
        return True
    evt = _typed_event(msg)
    try:
        from nanobot.bus.outbound_events import (
            StreamDeltaEvent,
            StreamEndEvent,
            ProgressEvent,
            RetryWaitEvent,
        )
    except Exception:
        return False
    return isinstance(evt, (StreamDeltaEvent, StreamEndEvent, ProgressEvent, RetryWaitEvent))


def msg_session_key(msg: Any) -> str:
    """``session_key`` с объекта msg/context (или ``""`` если нет/не строка).

    Исторически копировалось дословно в 5+ местах (``getattr(msg, ...,
    '') or ''``). Здесь — единая форма, чтобы опечатки в имени атрибута
    тоже не было.
    """
    key = getattr(msg, "session_key", None)
    return key if isinstance(key, str) else ""
