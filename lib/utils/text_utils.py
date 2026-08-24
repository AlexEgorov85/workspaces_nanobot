"""Text-и JSON-safe value helpers shared by tools and skills.

Перенесено из ``workspace/skills/audit_analyzer/scripts/output.py::_sanitize_value``
без изменения контракта. Дублирование заменено единой реализацией.
"""

from __future__ import annotations

import decimal
import math
import uuid
from datetime import date, datetime, time, timedelta
from typing import Any

__all__ = ["sanitize_value", "truncate_middle"]


def sanitize_value(obj: Any) -> Any:
    """Рекурсивно привести объект к JSON-совместимому виду.

    Поддерживает:
        datetime / date / time → .isoformat()
        timedelta              → str()
        Decimal                → float или int
        UUID                   → str
        bytes                  → str (utf-8 decode)
        float (nan/inf)        → None
        list / tuple / dict    → рекурсивно
        всё остальное          → str()

    Args:
        obj: Любой объект.

    Returns:
        JSON-совместимое значение (None, bool, int, float, str, list, dict).
    """
    if obj is None:
        return None
    if isinstance(obj, (datetime, date, time)):
        return obj.isoformat()
    if isinstance(obj, timedelta):
        return str(obj)
    if isinstance(obj, decimal.Decimal):
        if obj == obj.to_integral_value():
            return int(obj)
        return float(obj)
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, (int, str, bool)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [sanitize_value(v) for v in obj]
    if isinstance(obj, dict):
        return {str(k): sanitize_value(v) for k, v in obj.items()}
    if hasattr(obj, "isoformat"):
        try:
            return obj.isoformat()
        except Exception:
            return str(obj)
    try:
        return str(obj)
    except Exception:
        return repr(obj)


def truncate_middle(text: str, max_chars: int) -> str:
    """Обрезать ``text`` до ``max_chars`` символов, сохранив head и tail.

    Если длина ``text`` не превышает ``max_chars``, возвращается как есть.
    Иначе берётся половина ``max_chars`` с начала и столько же с конца,
    между ними вставляется маркер с числом пропущенных символов.

    Args:
        text: Исходная строка.
        max_chars: Жёсткий потолок длины результата (>= 4).

    Returns:
        Усечённая строка с маркером ``... (N chars truncated) ...``.
    """
    if max_chars < 4:
        raise ValueError("max_chars must be >= 4")
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    omitted = len(text) - max_chars
    return (
        text[:half]
        + f"\n\n... ({omitted:,} chars truncated) ...\n\n"
        + text[-half:]
    )
