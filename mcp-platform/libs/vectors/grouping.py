"""Группировка результатов векторного поиска: чанки → документы.

Портировано из агента: ``lib/utils/duckdb_query.py`` (``build_raw_items``,
``group_vector_hits``). Удаление агентской копии — фазы 4/5/9.

Единственное отличие от агентской копии: вместо ``conn`` (DuckDB-соединение)
принимается ``fetch_fn`` — колбэк чтения одного чанка. Так ``libs/vectors``
не знает про DuckDB: соединение открывает владелец снимка и отдаёт готовый
результат. Раньше здесь был прямой ``conn.execute`` по таблице векторного
хранилища, то есть второй путь к данным мимо ``CacheProvider``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

#: Колбэк чтения payload'а чанка: ``(source, pk_value, chunk_index) -> dict``.
#: Возвращает ``{}``, если чанк не найден (легитимный пустой результат).
ChunkFetcher = Callable[[str, Any, int], dict[str, Any]]


def build_raw_items(
    meta_items: dict[str, Any],
    scores: Any,
    ids: Any,
    index_name: str,
    threshold: float | None,
    *,
    fetch_fn: ChunkFetcher | None = None,
) -> list[dict[str, Any]]:
    """Собрать сырые чанки-строки из результатов поиска FAISS.

    ``meta_items`` содержит только координаты (``pk_value``, ``chunk_index``,
    ``chunk_count``, ``table``, ``source``) — тяжёлый payload (``content``,
    ``search_text``, ``row_data``) подтягивается через ``fetch_fn`` из снимка.

    Если ``fetch_fn`` не передан, payload остаётся пустым (тесты и сценарии без
    гидрации).
    """
    raw: list[dict[str, Any]] = []
    for score, doc_id in zip(scores[0], ids[0], strict=False):
        if doc_id < 0:
            continue
        if threshold is not None and score < threshold:
            continue
        item = meta_items.get(str(doc_id), {})
        chunk_idx = item.get("chunk_index", 0)
        chunk_total = item.get("chunk_count", 1)
        pk = item.get("pk_value", int(doc_id))
        tbl = item.get("table", "")
        src = item.get("source", index_name)

        content = ""
        row: dict[str, Any] = {}
        if fetch_fn is not None:
            try:
                payload = fetch_fn(src, pk, chunk_idx) or {}
            except Exception:  # noqa: BLE001 - отсутствие чанка не роняет выдачу
                payload = {}
            content = payload.get("content") or payload.get("search_text") or ""
            row = _coerce_row(payload.get("row"))

        raw.append({
            "content": content,
            "score": float(score),
            "source": src,
            "table": tbl,
            "pk_value": pk,
            "chunk_index": chunk_idx,
            "chunk_total": chunk_total,
            "chunk": f"{chunk_idx + 1}/{chunk_total}" if chunk_total > 1 else "",
            "row": row,
        })
    return raw


def _coerce_row(raw: Any) -> dict[str, Any]:
    """Привести ``row_data`` к dict (``{}`` — мусор, а не падение выдачи)."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except Exception:  # noqa: BLE001 - мусорный JSON → пустая исходная строка
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def group_vector_hits(
    raw: list[dict[str, Any]],
    top_k: int = 5,
    threshold: float | None = None,
) -> list[dict[str, Any]]:
    """Группировка чанков: один документ = одно место в top_k.

    Принимает сырые чанки (содержат ``source``, ``table``, ``pk_value``,
    ``score``, ``chunk_index``, ``chunk_total``) и возвращает документы с
    ``matched_chunks``, отсортированные по ``score`` (срезанные до ``top_k``).
    """
    doc_groups: dict[tuple, dict[str, Any]] = {}
    for r in raw:
        key = (r["source"], r["table"], r["pk_value"])
        if key not in doc_groups or r["score"] > doc_groups[key]["score"]:
            entry = dict(r)
            entry["matched_chunks"] = 1
            doc_groups[key] = entry
        else:
            doc_groups[key]["matched_chunks"] += 1

    results = sorted(doc_groups.values(), key=lambda r: r["score"], reverse=True)
    threshold_active = threshold is not None and threshold > 0
    if top_k and not threshold_active:
        results = results[:top_k]

    for r in results:
        r.pop("chunk_index", None)
        r.pop("chunk_total", None)

    return results
