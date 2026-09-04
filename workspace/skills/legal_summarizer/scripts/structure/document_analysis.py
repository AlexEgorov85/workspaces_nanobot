"""DocumentAnalysis cache architecture (PLAN §39, Этап 39).

Единый ``DocumentAnalysis`` — это immutable snapshot всех результатов
анализа документа, который переиспользуется для follow-up запросов:

* identity (DocumentIdentity);
* physical (PhysicalDocument);
* semantic structure (DocumentStructure);
* chunks (tuple of Chunk);
* semantic records (per-chunk structured LLM-output);
* retrieval index (RetrievalIndex — для fast lookup).

**Не делает**:
* не хранит LLM summaries в свободном тексте — только structured records;
* не дублирует text chunks — ссылается на PhysicalDocument.

Plan §40 + §63: ``brief`` и ``question`` используют один и тот же
DocumentAnalysis — не перепарсивают документ.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.identity import (
    DocumentIdentity,
)
from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentStructure,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    PhysicalDocument,
)
from workspace.skills.legal_summarizer.scripts.structure.retrieval_index import (
    RetrievalIndex,
)
from workspace.skills.legal_summarizer.scripts.structure.semantic_record import (
    SemanticRecord,
)


@dataclass(frozen=True)
class DocumentAnalysis:
    """Единый cache для анализа документа (PLAN §39).

    Attributes:
        identity: ``DocumentIdentity``.
        physical: ``PhysicalDocument``.
        structure: ``DocumentStructure`` (canonical semantic structure).
        chunks: tuple of ``Chunk`` (в document order).
        semantic_records: dict ``chunk_id → SemanticRecord``.
        retrieval_index: ``RetrievalIndex`` (L3 metadata).
        created_at: ISO timestamp (для diagnostics).
        version: версия схемы cache (для migrations).
    """

    identity: DocumentIdentity
    physical: PhysicalDocument
    structure: DocumentStructure
    chunks: tuple[Chunk, ...]
    semantic_records: dict[str, SemanticRecord] = field(default_factory=dict)
    retrieval_index: RetrievalIndex | None = None
    created_at: str = ""
    version: int = 1

    def get_chunk(self, chunk_id: str) -> Chunk | None:
        for c in self.chunks:
            if c.chunk_id == chunk_id:
                return c
        return None

    def get_record(self, chunk_id: str) -> SemanticRecord | None:
        return self.semantic_records.get(chunk_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity.to_dict(),
            "physical_path": self.physical.path,
            "structure_root": self.structure.root_id,
            "chunk_count": len(self.chunks),
            "record_count": len(self.semantic_records),
            "has_retrieval_index": self.retrieval_index is not None,
            "created_at": self.created_at,
            "version": self.version,
        }

    @classmethod
    def build(
        cls,
        *,
        physical: PhysicalDocument,
        structure: DocumentStructure,
        chunks: tuple[Chunk, ...],
        identity: DocumentIdentity | None = None,
        semantic_records: dict[str, SemanticRecord] | None = None,
        include_retrieval_index: bool = True,
        created_at: str = "",
    ) -> "DocumentAnalysis":
        """Построить DocumentAnalysis из ингредиентов.

        Это **новая** canonical сборка (PLAN §39). Старые caches
        (physical_cache_key, document_cache) продолжают работать
        (back-compat). DocumentAnalysis — это слой **выше** них,
        объединяющий их результаты.
        """
        if identity is None:
            identity = DocumentIdentity.from_path(physical.path)
        if include_retrieval_index:
            retrieval = RetrievalIndex.build(
                chunks=chunks,
                structure=structure,
                physical=physical,
                document_id=identity.document_id,
            )
        else:
            retrieval = None

        return cls(
            identity=identity,
            physical=physical,
            structure=structure,
            chunks=chunks,
            semantic_records=semantic_records or {},
            retrieval_index=retrieval,
            created_at=created_at,
        )

    def retrieve(
        self,
        query: str,
        *,
        config=None,
    ) -> list:
        """Удобный API: retrieval через ``RetrievalIndex``.

        Возвращает список ``RetrievalHit``.
        """
        if self.retrieval_index is None:
            from workspace.skills.legal_summarizer.scripts.structure.retrieval import (
                retrieve_chunks,
            )
            return retrieve_chunks(self.chunks, query, config=config)
        return self.retrieval_index.retrieve(query, config=config)


__all__ = ["DocumentAnalysis"]