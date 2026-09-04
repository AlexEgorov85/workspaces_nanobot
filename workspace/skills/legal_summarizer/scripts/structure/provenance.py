"""Provenance checks (PLAN §46, Этап 46).

Каждый результат должен уметь показать:

* document;
* section;
* subsection;
* page;
* block;
* chunk.

Этот модуль предоставляет ``ProvenanceChain`` — связку document → section
→ chunk → page/block для **полной** traceability.

PLAN §46: если final answer содержит утверждение "цена = X", система
должна иметь возможность определить источник.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentStructure,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    PhysicalDocument,
)


@dataclass(frozen=True)
class ProvenanceChain:
    """Полная provenance chain для ответа.

    Attributes:
        document_id: ``DocumentIdentity.document_id``.
        document_path: ``PhysicalDocument.path``.
        section_id: ``StructureNode.node_id``.
        section_title: heading.
        section_path: e.g. ``"1 > 1.2"``.
        chunk_id: ``Chunk.chunk_id``.
        page_start / page_end: 1-based.
        block_ordinals: tuple of ``DocumentBlock.ordinal``.
    """

    document_id: str
    document_path: str
    section_id: str
    section_title: str
    section_path: str
    chunk_id: str
    page_start: int | None
    page_end: int | None
    block_ordinals: tuple[int, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "document_path": self.document_path,
            "section_id": self.section_id,
            "section_title": self.section_title,
            "section_path": self.section_path,
            "chunk_id": self.chunk_id,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "block_ordinals": list(self.block_ordinals),
        }

    def is_complete(self) -> bool:
        """``True`` если все критичные поля заполнены (PLAN §58 acceptance)."""
        return all([
            self.document_id,
            self.document_path,
            self.section_id,
            self.chunk_id,
            self.page_start is not None,
            self.page_end is not None,
            bool(self.block_ordinals),
        ])


def build_provenance_chain(
    chunk: Chunk,
    *,
    doc: PhysicalDocument,
    struct: DocumentStructure,
    document_id: str,
) -> ProvenanceChain | None:
    """Построить полную provenance chain для ``chunk``.

    Возвращает ``None`` если chunk не найден в PhysicalDocument.
    """
    by_ord = {b.ordinal: b for b in doc.blocks}
    blocks = [by_ord[o] for o in chunk.block_indices if o in by_ord]
    if not blocks and chunk.block_indices:
        return None

    page_starts = [b.page_index for b in blocks if b.page_index is not None]
    page_end = [b.page_end for b in blocks if b.page_end is not None]

    section = struct.nodes.get(chunk.section_id) if chunk.section_id else None
    section_path_parts: list[str] = []
    if section is not None:
        cur = section
        while cur is not None and cur.node_id != struct.root_id:
            section_path_parts.append(cur.title or "")
            if cur.parent_id is None:
                break
            cur = struct.nodes.get(cur.parent_id)
        section_path_parts.reverse()
        section_path = " > ".join(p for p in section_path_parts if p)
    else:
        section_path = ""

    return ProvenanceChain(
        document_id=document_id,
        document_path=doc.path,
        section_id=chunk.section_id or "",
        section_title=chunk.section_heading or "",
        section_path=section_path,
        chunk_id=chunk.chunk_id,
        page_start=min(page_starts) if page_starts else None,
        page_end=max(page_end) if page_end else None,
        block_ordinals=tuple(b.ordinal for b in blocks),
    )


__all__ = ["ProvenanceChain", "build_provenance_chain"]