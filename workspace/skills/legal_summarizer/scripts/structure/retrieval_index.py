"""RetrievalIndex (PLAN §36, Этап 36).

Минимальная реализация многоуровневого индекса для retrieval:

* L0: physical parse cache (PhysicalDocument).
* L1: structure/chunk cache (DocumentStructure + Chunks).
* L2: semantic analysis cache (SemanticRecord'ы).
* L3: retrieval metadata (terms → chunk_ids) для быстрого BM25-lite.

Не создаёт новых persistent caches — наследует существующую архитектуру
(``physical_cache_key``, ``document_cache`` и т.д.). Индекс in-memory,
строится на лету из L0/L1/L2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentStructure,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    PhysicalDocument,
)
from workspace.skills.legal_summarizer.scripts.structure.query_normalizer import (
    tokenize_normalized,
)
from workspace.skills.legal_summarizer.scripts.structure.retrieval import (
    RetrievalHit, RetrievalConfig, score_chunk,
)


@dataclass(frozen=True)
class RetrievalIndex:
    """In-memory retrieval index (L3 metadata over L0/L1/L2).

    Attributes:
        document_id: идентификатор документа.
        chunks: tuple of ``Chunk``.
        structure: ``DocumentStructure``.
        physical: ``PhysicalDocument`` (L0 reference).
        term_to_chunks: dict ``term → set(chunk_id)`` (L3 inverted index).
    """

    document_id: str
    chunks: tuple[Chunk, ...]
    structure: DocumentStructure
    physical: PhysicalDocument | None
    term_to_chunks: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def retrieve(
        self,
        query: str,
        *,
        config: RetrievalConfig | None = None,
    ) -> list[RetrievalHit]:
        """Поиск через inverted index + sparse ranking."""
        terms = tokenize_normalized(query)
        if not terms:
            return []
        candidate_ids: set[str] = set()
        for term in terms:
            for cid in self.term_to_chunks.get(term, ()):
                candidate_ids.add(cid)

        if not candidate_ids:
            return []

        candidate_chunks = [c for c in self.chunks if c.chunk_id in candidate_ids]
        cfg = config or RetrievalConfig()
        hits = [score_chunk(c, terms, config=cfg) for c in candidate_chunks]
        hits = [h for h in hits if h.score >= cfg.min_score]
        hits.sort(key=lambda h: (-h.score, h.chunk_id))
        return hits[: cfg.max_results]

    @classmethod
    def build(
        cls,
        *,
        chunks: Iterable[Chunk],
        structure: DocumentStructure,
        physical: PhysicalDocument | None = None,
        document_id: str = "doc",
    ) -> "RetrievalIndex":
        """Построить inverted index из chunks.

        ``L3`` строится один раз — повторные вызовы ``retrieve``
        не пересобирают index.

        Включает section_heading термы (PLAN §65 — structure-aware):
        chunk индексируется по ``chunk.text`` + ``chunk.section_heading``.
        """
        chunks_tuple = tuple(chunks)
        inverted: dict[str, set[str]] = {}
        for chunk in chunks_tuple:
            seen_for_chunk: set[str] = set()
            for term in tokenize_normalized(chunk.text):
                if term in seen_for_chunk:
                    continue
                seen_for_chunk.add(term)
                inverted.setdefault(term, set()).add(chunk.chunk_id)
            for term in tokenize_normalized(chunk.section_heading or ""):
                if term in seen_for_chunk:
                    continue
                seen_for_chunk.add(term)
                inverted.setdefault(term, set()).add(chunk.chunk_id)
        term_to_chunks = {k: tuple(sorted(v)) for k, v in inverted.items()}
        return cls(
            document_id=document_id,
            chunks=chunks_tuple,
            structure=structure,
            physical=physical,
            term_to_chunks=term_to_chunks,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "chunk_count": len(self.chunks),
            "term_count": len(self.term_to_chunks),
        }


__all__ = ["RetrievalIndex"]