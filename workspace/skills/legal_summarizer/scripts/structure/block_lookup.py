"""Lookup helpers — замена linear ``index()`` (PLAN §44).

В legacy коде (``context_expansion.py``, ``cached_retrieval.py``)
использовался ``doc.blocks.index(target)`` — O(N) на каждый chunk.

Этот модуль предоставляет ``build_block_lookup`` для O(1) lookup
по ``block_id`` или ``ordinal``.

``PhysicalDocument.blocks_by_ord`` уже есть (Этап 4) — он
даёт lookup по ordinal. Этот модуль добавляет lookup по ``block_id``
(``b_NNNN``) для удобства downstream'ов, которые ищут по id.
"""

from __future__ import annotations

from dataclasses import dataclass

from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock, PhysicalDocument,
)


@dataclass(frozen=True)
class BlockLookup:
    """O(1) lookup для ``DocumentBlock``."""

    by_ord: dict[int, DocumentBlock]
    by_id: dict[str, DocumentBlock]

    def get_by_ord(self, ordinal: int) -> DocumentBlock | None:
        return self.by_ord.get(ordinal)

    def get_by_id(self, block_id: str) -> DocumentBlock | None:
        return self.by_id.get(block_id)


def build_block_lookup(doc: PhysicalDocument) -> BlockLookup:
    """Построить O(1) lookup для всех блоков документа.

    Для больших документов (10k+ blocks) это критично — позволяет
    избежать O(N) linear ``index()`` в ``context_expansion.py``,
    ``cached_retrieval.py`` и других местах.
    """
    by_ord: dict[int, DocumentBlock] = {}
    by_id: dict[str, DocumentBlock] = {}
    for b in doc.blocks:
        by_ord[b.ordinal] = b
        by_id[b.block_id] = b
    return BlockLookup(by_ord=by_ord, by_id=by_id)


__all__ = ["BlockLookup", "build_block_lookup"]