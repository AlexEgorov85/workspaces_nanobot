"""Follow Up 2.0 — Conversation utilities.

Помогает агентам учитывать прошлые ходы диалога:
- format_history()   — кратко сжимает последние сообщения для подачи в промпт
- resolve_references() — резолвит «эта проверка», «расскажи про неё» из истории
- extract_followups()  — вытаскивает блок <followups>[...]</followups> из ответа LLM
                         и возвращает (ответ_без_сентинеля, [подсказки])
"""
from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Tuple

from backend.core import identity


# Сколько последних сообщений (user+assistant) подмешивать в контекст.
# 4 хода ≈ 8 сообщений ≈ ~2-3к токенов — нормально.
_HISTORY_MAX_TURNS = 4
_HISTORY_MAX_CHARS_PER_MSG = 600

# Сентинел для follow-up подсказок, который LLM добавляет в конец ответа.
_FOLLOWUPS_RE = re.compile(
    r"<followups>\s*(\[.*?\])\s*</followups>",
    re.DOTALL | re.IGNORECASE,
)

# Метка фокуса, которую оставляет свёртка карточки (execution_control.
# card_to_markdown). Нужна потому, что в свёртке есть блок «Смежные кейсы» с
# ЧУЖИМИ номерами: без метки «последний упомянутый КМ» мог бы оказаться
# соседней проверкой. Каждая КМ — отдельная проверка.
FOCUS_MARKER = "fu:focus"
_FOCUS_RE = re.compile(rf"<!--\s*{FOCUS_MARKER}\s+(\S+)\s*-->")


def format_history(messages: List[Dict], max_turns: int = _HISTORY_MAX_TURNS) -> str:
    """Превращает список сообщений сессии в компактный текст для промпта.

    `messages` — это список словарей вида {"role": "user|assistant", "content": "..."}.
    Берём последние max_turns пар и обрезаем длинные сообщения.
    """
    if not messages:
        return "(история пуста — это первое сообщение в сессии)"

    # Берём последние 2*max_turns сообщений
    tail = messages[-(2 * max_turns):]
    lines = []
    for m in tail:
        role = "Аудитор" if m.get("role") == "user" else "Помощник"
        text = (m.get("content") or "").strip().replace("\n", " ")
        if len(text) > _HISTORY_MAX_CHARS_PER_MSG:
            text = text[:_HISTORY_MAX_CHARS_PER_MSG] + "…"
        lines.append(f"{role}: {text}")
    return "\n".join(lines)


def last_mentioned_km(messages: List[Dict]) -> Optional[str]:
    """Последняя проверка, о которой шёл разговор.

    Порядок важен и защищает доменное правило:
    1. метка фокуса от карточки — это КМ, о которой карточка, а не смежная;
    2. номер, названный САМИМ аудитором, — он про свою проверку и говорит;
    3. только потом — любой номер из текста ответа.

    Без первых двух шагов строка «Смежные кейсы: КМ-99-12350» в свёртке
    перебила бы КМ, о которой аудитор спрашивал.
    """
    msgs = list(messages or [])

    for m in reversed(msgs):
        found = _FOCUS_RE.search(m.get("content") or "")
        if found:
            km = identity.normalize(found.group(1))
            if km:
                return km

    for m in reversed(msgs):
        if m.get("role") != "user":
            continue
        kms = identity.parse(m.get("content") or "")
        if kms:
            return kms[0]

    for m in reversed(msgs):
        kms = identity.parse(m.get("content") or "")
        if kms:
            return kms[0]
    return None


# Местоимения / общие отсылки, которые «требуют» резолвинга
_REFERENCE_HINTS = re.compile(
    r"\b(эт(?:а|у|ой|ом|их)|её|нее|этим|этом|этой проверк|"
    r"в этой|в данной|по этой|по ней|по нему|про неё|про него|"
    r"подробнее|расскажи подробнее|в конкретн)\b",
    re.IGNORECASE,
)


def needs_reference_resolution(query: str, has_explicit_km: bool) -> bool:
    """True, если запрос вероятно отсылается к чему-то из истории."""
    if has_explicit_km:
        return False
    return bool(_REFERENCE_HINTS.search(query))


def extract_followups(text: str) -> Tuple[str, List[str]]:
    """Вырезает блок <followups>[...]</followups> из ответа LLM.

    Возвращает (clean_text, followups). Если блок не найден или невалидный —
    возвращает (text, []).
    """
    if not text:
        return text, []
    match = _FOLLOWUPS_RE.search(text)
    if not match:
        return text.strip(), []

    raw = match.group(1)
    followups: List[str] = []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            followups = [str(x).strip() for x in parsed if str(x).strip()][:3]
    except json.JSONDecodeError:
        pass

    clean = (text[: match.start()] + text[match.end():]).strip()
    return clean, followups
