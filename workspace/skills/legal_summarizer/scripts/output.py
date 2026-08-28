"""Форматирование результатов для вывода в stdout (JSON).

Приводит вложенные dict-результаты от ``summarizer.run()`` к плоскому
единообразному формату для сериализации в JSON.

JSON-сериализация значений (datetime/Decimal/NaN/bytes) — через
``lib.utils.text_utils.sanitize_value`` (общий слой для всех
skill'ов и tool'ов).
"""

from __future__ import annotations

from typing import Any

from lib.utils.text_utils import sanitize_value as _sanitize_value  # noqa: F401


def prepare_output(result: dict) -> dict:
    """Привести результат ``summarizer.run()`` к плоскому формату.

    Args:
        result: dict от ``summarizer.run()`` (``status``, ``operation_id``,
            ``result``, ``stats``, ``summary``, ``estimate``, ``hint``, ``error``).

    Returns:
        Плоский dict для ``json.dumps()``.
    """
    out: dict[str, Any] = {
        "mode": "summarize",
        "status": result.get("status", "failed"),
    }
    status = out["status"]

    op_id = result.get("operation_id")
    if op_id:
        out["operation_id"] = op_id

    if status == "completed":
        inner = result.get("result") or {}
        out["subject"] = inner.get("subject", "")
        out["summary"] = inner.get("summary", "")
        out["length"] = inner.get("length", "")
        out["chars_in"] = inner.get("chars_in", 0)
        out["chunks"] = inner.get("chunks", 1)
        out["context_batches"] = inner.get("context_batches", 0)
        out["sections"] = inner.get("sections", 0)
        out["strategy"] = inner.get("strategy", "single")
        title = inner.get("title")
        if title:
            out["title"] = title
        stats = result.get("stats") or {}
        if stats:
            out["stats"] = stats

    elif status == "confirmation_required":
        out["summary"] = result.get("summary") or {}
        out["estimate"] = result.get("estimate") or {}
        hint = result.get("hint")
        if hint:
            out["hint"] = hint

    elif status == "requires_continuation":
        out["summary"] = result.get("summary") or {}
        hint = result.get("hint")
        if hint:
            out["hint"] = hint

    elif status == "failed":
        err = result.get("error") or {}
        out["error"] = err

    return out


__all__ = ["prepare_output"]