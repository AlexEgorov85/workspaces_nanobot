"""Order-preserving utilities.

Даже при ranking/retrieval порядок документа не должен
уничтожаться. После ranking `restore_document_order`.

Этот модуль — minimal helpers.
"""

from __future__ import annotations

from collections.abc import Iterable

from libs.legal_summarizer.chunking.chunks import Chunk


def _chunk_index(c: Chunk) -> int:
    """Ключ сортировки по умолчанию — ``chunk.index``.

    Отдельная функция вместо ``lambda`` внутри тела: она определена до
    первого вызова ``restore_document_order``, поэтому момент связывания
    тот же, а у рефакторинга нет побочного эффекта.
    """
    return c.index


def restore_document_order(
    chunks: Iterable[Chunk],
    *,
    key=None,
) -> list[Chunk]:
    """Восстановить document order (по ``chunk.index``)."""
    return sorted(chunks, key=_chunk_index if key is None else key)


def ensure_order_preserved(
    chunks: Iterable[Chunk],
    original_order_ids: list[str],
) -> list[Chunk]:
    """Если chunks уже в document order, вернуть как есть.

    Иначе — отсортировать по позиции в ``original_order_ids``.
    """
    by_id = {c.chunk_id: c for c in chunks}
    ordered: list[Chunk] = []
    for cid in original_order_ids:
        if cid in by_id:
            ordered.append(by_id[cid])
    extras = [c for c in chunks if c.chunk_id not in original_order_ids]
    return ordered + extras


__all__ = ["restore_document_order", "ensure_order_preserved"]
