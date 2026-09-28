"""Форматирование результатов и ошибок для CLI/stdout.

Формат успеха: ``{"mode": <mode>, "status": "success", "data": {...}}``.
Формат ошибки: ``{"mode": <mode>, "status": "error", "data": {"message": ..., "error_type": ...}}``.

Совместимо с ``audit_analyzer.scripts.output.prepare_output`` —
одинаковая верхнеуровневая структура.
"""

from __future__ import annotations

from typing import Any

from lib.utils.text_utils import sanitize_value


__all__ = ["prepare_output", "make_error", "sanitize_output"]


def prepare_output(result: dict[str, Any], mode: str) -> dict[str, Any]:
    """Привести результат режима к плоскому формату ``{mode, status, data}``.

    Args:
        result: dict с результатом работы режима.
        mode: имя режима (``"analyze" | "search" | "synthesize" | "all"``).

    Returns:
        dict верхнего уровня с ключами ``mode``, ``status`` и ``data``.
    """
    if not result:
        return {
            "mode": mode,
            "status": "error",
            "data": {"message": "пустой результат"},
        }

    sanitized = sanitize_value(result)
    return {
        "mode": mode,
        "status": sanitized.get("status", "success"),
        "data": sanitized.get("data", {}),
    }


def make_error(message: str, *, error_type: str | None = None) -> dict[str, Any]:
    """Сформировать JSON-ответ с ошибкой.

    Args:
        message: человекочитаемое сообщение.
        error_type: машинно-читаемая категория (например, ``vnd_not_found``,
            ``json_parse_failed``, ``empty_violation``).

    Returns:
        ``{"status": "error", "data": {"message": ..., "error_type": ...}}``.
    """
    data: dict[str, Any] = {"message": message}
    if error_type:
        data["error_type"] = error_type
    return {"status": "error", "data": data}


def sanitize_output(out: dict[str, Any]) -> dict[str, Any]:
    """Привести dict к JSON-safe виду (через ``lib.utils.text_utils.sanitize_value``)."""
    return sanitize_value(out)
