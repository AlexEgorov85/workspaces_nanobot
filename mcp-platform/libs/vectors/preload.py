"""Здоровье прогрева векторных индексов (свёртка объявления и снимка).

Портировано из агента: ``lib/services/preload_service.py`` — та часть, которая
относится к прогреву векторных индексов (``compute_index_health``,
``_format_lines``). Удаление агентской копии — фазы 4/5/9.

Что **не** портировано и почему: класс ``PreloadService`` целиком. В агенте он —
асинхронный runtime-сервис с циклом и записью событий в
``agent_gateway_logs``; его вызывающая сторона — ``gateway.py:223``, то есть
агентский процесс. В capability вызова на старте нет и быть не должно:
прогрев ленивый, по первому векторному запросу (миграция, фаза 3, пункт 3.9).
Оставлены чистые функции — весь диагностический смысл модуля в них, и они же
позволяют capability отвечать на вопрос «что объявлено, что в снимке» без
поднятия FAISS.
"""

from __future__ import annotations

from typing import Any


def format_index_health_lines(
    declared_names: list[str],
    loaded_items: list[dict[str, Any]],
    missing: list[str],
    orphan: list[str],
    stale: list[str],
) -> list[str]:
    """Human-readable multi-line сводка для ответа capability.

    Цвет/жирность не навешиваем (нет ANSI-гарантии у MCP-клиентов). Только
    текст.
    """

    def fmt_loaded() -> str:
        if not loaded_items:
            return "—"
        return ", ".join(
            f"{it['index_name']}({it.get('vectors', '?')})"
            for it in sorted(
                loaded_items,
                key=lambda x: x.get("index_name") or "",
            )
        )

    return [
        "[vector] preload health summary:",
        f"  declared ({len(declared_names)}): "
        f"{', '.join(declared_names) or '—'}",
        f"  loaded   ({len(loaded_items)}): {fmt_loaded()}",
        f"  missing  ({len(missing)}): {', '.join(missing) or '—'}",
        f"  orphan   ({len(orphan)}): {', '.join(orphan) or '—'}",
        f"  stale    ({len(stale)}): {', '.join(stale) or '—'}",
    ]


def compute_index_health(
    declared: dict[str, Any],
    loaded: list[dict[str, Any]] | None,
    runtime_rows: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Посчитать declared / loaded / missing / orphan / stale.

    Args:
        declared: результат ``read_vector_index_config()``.
        loaded: то, что вернул ``preload()`` (или ``None`` / пустой list).
            Записи могут содержать ``signature_status``
            (CURRENT/STALE/INVALID).
        runtime_rows: то, что вернул ``list_runtime_vector_indexes()``. Может
            быть ``None`` при недоступности снимка.

    Returns:
        dict с ``declared_names``, ``loaded_items``, ``missing``, ``orphan``,
        ``stale`` (все отсортированы), ``divergence`` — bool, ``level`` —
        ``"INFO"`` или ``"WARN"``.
    """
    declared_names = sorted(declared.keys())

    loaded_items: list[dict[str, Any]] = (
        sorted(loaded, key=lambda x: x.get("index_name") or "")
        if loaded
        else []
    )
    loaded_names = {it.get("index_name") for it in loaded_items if it.get("index_name")}

    missing = sorted(n for n in declared_names if n not in loaded_names)

    if runtime_rows:
        runtime_names = sorted(
            r.get("source") for r in runtime_rows if r.get("source")
        )
        orphan = sorted(n for n in runtime_names if n not in declared)
        stale: list[str] = []
        for it in loaded_items:
            name = it.get("index_name")
            if not name or name not in declared:
                continue
            status = it.get("signature_status")
            if status in ("STALE", "INVALID"):
                stale.append(f"{name}:{status}")
    else:
        orphan = []
        stale = []

    divergence = bool(missing) or bool(orphan) or bool(stale)
    return {
        "declared_names": declared_names,
        "loaded_items": loaded_items,
        "missing": missing,
        "orphan": orphan,
        "stale": stale,
        "divergence": divergence,
        "level": "WARN" if divergence else "INFO",
    }
