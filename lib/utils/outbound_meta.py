"""Служебные ключи в OutboundMessage.metadata, которые runner ставит
в течение оборота.

Сейчас это сигналы прогресса / рассуждений / потоковых чанков, которые
НЕ интересуют пользователя и логирование. Канал и CLI должны дропать их
или буферизовать (reasoning), а DB-логирование — пропускать как drop.
Эти имена — КОНТРАКТ между фреймворком nanobot и всеми местами
потребления, поэтому хранятся в одном месте.
"""

from __future__ import annotations

from typing import Any, Mapping


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
    """True, если metadata содержит ``_stream_delta`` (чанк стрима)."""
    if not metadata:
        return False
    return bool(metadata.get("_stream_delta"))


def msg_session_key(msg: Any) -> str:
    """``session_key`` с объекта msg/context (или ``""`` если нет/не строка).

    Исторически копировалось дословно в 5+ местах (``getattr(msg, ...,
    '') or ''``). Здесь — единая форма, чтобы опечатки в имени атрибута
    тоже не было.
    """
    key = getattr(msg, "session_key", None)
    return key if isinstance(key, str) else ""
