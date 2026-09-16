"""Форматирование результатов для вывода.

Для аудитора-человека (основной режим ``--mode synthesize``) — текстовый
отчёт в формате Markdown (см. ``scripts/report/markdown_renderer.py``)
записывается в файл или печатается в stdout.

Для промежуточных режимов (``analyze``, ``search``) и для CI/тестов —
единый плоский JSON ``{mode, status, data}``. Формат совместим с
``audit_analyzer.scripts.output.prepare_output`` (одинаковая верхнеуровневая
структура, но без специфики SQL/vector).
"""

from __future__ import annotations

from typing import Any

from lib.utils.text_utils import sanitize_value


__all__ = ["prepare_output", "make_error"]


def prepare_output(result: dict[str, Any], mode: str) -> dict[str, Any]:
    """Привести результат режима к плоскому формату для вывода в JSON.

    Args:
        result: dict с результатом работы режима (зависит от mode).
        mode: ``"analyze"`` | ``"search"`` | ``"synthesize"``.

    Returns:
        dict верхнего уровня с ключами ``mode``, ``status`` и
        ``data`` (плоский dict с результатом; datetime/Decimal/NaN/bytes
        сериализуются через ``sanitize_value``).
    """
    sanitized = sanitize_value(result) if result else {}
    return {
        "mode": mode,
        "status": sanitized.get("status", "success"),
        "data": sanitized.get("data", {}),
    }


def make_error(message: str, *, error_type: str | None = None) -> dict[str, Any]:
    """Сформировать стандартный JSON-ответ с ошибкой.

    Args:
        message: человекочитаемое сообщение об ошибке.
        error_type: машинно-читаемая категория (например,
            ``"vnd_unreadable"``, ``"json_parse_failed"``,
            ``"vnd_not_found"``).

    Returns:
        ``{"status": "error", "data": {"message": ..., "error_type": ...?}}``.
    """
    data: dict[str, Any] = {"message": message}
    if error_type:
        data["error_type"] = error_type
    return {"status": "error", "data": data}
