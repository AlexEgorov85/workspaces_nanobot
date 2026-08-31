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


def build_confirmation_options(
    *,
    chars_in: int,
    min_seconds: float,
    max_seconds: float,
) -> dict[str, Any]:
    """Сформировать payload ``confirmation_required`` для агента.

    Возвращает dict с двумя вариантами (brief / detailed) и подсказкой
    про ``--question``. Технические числа (chunks, batches, llm_calls)
    НЕ включаются — агент их зеркалит в ответ, что раздражает
    (инцидент 2026-08-28).

    brief: ~40-55% от detailed (sample vs full). Защита от деления на ноль.
    """
    detailed_min = max(60, int(min_seconds))
    detailed_max = max(detailed_min + 30, int(max_seconds))
    brief_min = max(60, int(detailed_min * 0.45))
    brief_max = max(brief_min + 30, int(detailed_max * 0.55))
    return {
        "mode": "summarize",
        "status": "confirmation_required",
        "summary": {"chars_in": chars_in},
        "options": [
            {
                "id": "brief",
                "label": "краткая выжимка",
                "min_seconds": brief_min,
                "max_seconds": brief_max,
                "words_estimate": 250,
                "description": (
                    "Прочитаю только ключевые разделы (введение, подписи, "
                    "заголовки всех частей). Детали могу пропустить."
                ),
            },
            {
                "id": "detailed",
                "label": "подробный анализ",
                "min_seconds": detailed_min,
                "max_seconds": detailed_max,
                "words_estimate": 1000,
                "description": (
                    "Прочитаю весь документ, опишу каждый раздел простым языком."
                ),
            },
        ],
        "supports_question": True,
        "hint": (
            "Покажите пользователю меню из двух вариантов (brief / detailed) "
            "и упомяните, что можно задать конкретный вопрос через --question."
        ),
    }


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
        cache_stats = result.get("cache_stats")
        if cache_stats:
            out["cache_stats"] = cache_stats
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
        summary = result.get("summary") or {}
        est = result.get("estimate") or {}
        min_sec = est.get("min_seconds") or est.get("estimated_duration_min_sec")
        max_sec = est.get("max_seconds") or est.get("estimated_duration_max_sec")
        if min_sec is not None and max_sec is not None:
            payload = build_confirmation_options(
                chars_in=summary.get("chars_in") or 0,
                min_seconds=min_sec,
                max_seconds=max_sec,
            )
            out.update(payload)
            return out
        # Если время неизвестно — fallback на старый минимальный payload.
        title = summary.get("title")
        if title:
            out["summary"] = {"title": title}

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