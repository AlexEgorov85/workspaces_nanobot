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


# Поля со счётчиками LLM-вызовов, которые НЕ отдаём агенту: пользователю
# важно только время (длительность/ETA), а агенты склонны зеркалить числа
# вроде «20 вызовов LLM» в ответ — это раздражает (инцидент 2026-08-28).
# Оставляем duration_sec / retries / chars / chunks / strategy / partial /
# failed_batches — это либо время, либо структурное, либо операционное.
_HIDDEN_LLM_CALL_COUNTERS: frozenset[str] = frozenset({
    "map_calls",
    "section_reduce_calls",
    "section_trim_calls",
    "document_reduce_calls",
    "reduce_calls",
    "total_llm_calls",
})


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

    if status == "completed" or status == "partial":
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
            out["stats"] = {
                k: v for k, v in stats.items() if k not in _HIDDEN_LLM_CALL_COUNTERS
            }
        # partial: саммари есть, но часть батчей не распарсилась после retry.
        # Агент должен сообщить пользователю и предложить resume.
        if status == "partial":
            failed = stats.get("failed_batches") or []
            total_batches = stats.get("context_batches_total") or 0
            out["partial"] = True
            out["failed_batches"] = list(failed)
            out["hint"] = (
                f"Саммари собрано из {total_batches - len(failed)}/{total_batches} батчей; "
                f"не удалось: {', '.join(failed) if failed else '—'}. "
                "Перезапустите с тем же --operation-id --confirm для retry упавших батчей."
            )

    elif status == "confirmation_required":
        # summary может содержать estimated_llm_calls (устаревший путь) —
        # пробрасываем, но вычищаем счётчик: пользователю важно только время.
        out["summary"] = {
            k: v
            for k, v in (result.get("summary") or {}).items()
            if k != "estimated_llm_calls"
        }
        out["estimate"] = {
            k: v
            for k, v in (result.get("estimate") or {}).items()
            if k != "estimated_llm_calls"
        }
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