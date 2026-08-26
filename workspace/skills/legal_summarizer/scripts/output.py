"""Форматирование результатов для вывода в stdout (JSON).

Приводит вложенные dict-результаты от ``summarizer`` к плоскому
единообразному формату для сериализации в JSON.

JSON-сериализация значений (datetime/Decimal/NaN/bytes) — через
``lib.utils.text_utils.sanitize_value`` (общий слой для всех
skill'ов и tool'ов).
"""

from __future__ import annotations

from typing import Any

from lib.utils.text_utils import sanitize_value as _sanitize_value  # noqa: F401


def prepare_output(result: dict) -> dict:
    """Привести результат ``summarizer.summarize()`` к плоскому формату.

    Args:
        result: dict от ``summarizer.summarize()`` (``status`` + ``data``).

    Returns:
        Плоский dict для ``json.dumps()``.
    """
    out: dict[str, Any] = {
        "mode": "summarize",
        "status": result.get("status", "error"),
    }
    data = result.get("data", {})

    if "summary" in data:
        out["subject"] = data.get("subject", "")
        out["summary"] = data["summary"]
        out["length"] = data.get("length", "")
        out["chars_in"] = data.get("chars_in", 0)
        out["chunks"] = data.get("chunks", 1)
        out["strategy"] = data.get("strategy", "single")

    if "message" in data:
        out["message"] = data["message"]

    return out
