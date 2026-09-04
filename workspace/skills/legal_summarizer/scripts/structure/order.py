"""Order-preserving utilities (PLAN §67).

PLAN §67: даже при ranking/retrieval порядок документа не должен
уничтожаться. После ranking `restore_document_order`.

Этот модуль — minimal helpers.
"""

from __future__ import annotations

from typing import Iterable

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk


def restore_document_order(
    chunks: Iterable[Chunk],
    *,
    key=None,
) -> list[Chunk]:
    """Восстановить document order (по ``chunk.index``)."""
    if key is None:
        key = lambda c: c.index
    return sorted(chunks, key=key)


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