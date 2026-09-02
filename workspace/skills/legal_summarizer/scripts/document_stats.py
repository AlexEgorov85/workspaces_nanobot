"""Cheap document statistics — вход для adaptive execution strategy.

``DocumentStats`` dataclass с дешёвыми метриками, которые считаются
**без LLM-вызовов** (только из ``PhysicalDocument`` + ``SectionTree`` +
chunks).

Эти метрики являются входом для ``ExecutionStrategy`` selector:
    * ``DIRECT`` — документ помещается в один LLM call.
    * ``MAP_FLAT`` — chunks помещаются в один reduce.
    * ``MAP_HIERARCHICAL`` — chunks требуют section reduce.

Считается всё за один проход — O(N) по blocks.

NOTE: top-level, не ``pipeline/strategy.py`` — пакет ``pipeline``
планируется после переименования ``llm.py``. Когда это произойдёт,
мигрируем на целевую структуру.
"""
from __future__ import annotations

from dataclasses import dataclass

from workspace.skills.legal_summarizer.scripts.structure.chunks import (
    Chunk,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock,
    PhysicalDocument,
)
from workspace.skills.legal_summarizer.scripts.structure.sections import (
    SectionTree,
)


@dataclass(frozen=True)
class DocumentStats:
    """Дешёвые метрики документа для стратегии execution.

    Все поля считаются из ``PhysicalDocument`` + ``SectionTree`` + chunks,
    без обращения к LLM.

    Attributes:
        chars: суммарная длина текста (в символах).
        estimated_tokens: оценка токенов через ``ceil(chars / 4)``.
        pages: число страниц (``PhysicalDocument.page_count``); для DOCX/TXT
            может быть приблизительным.
        blocks: число DocumentBlock в документе.
        sections: число секций (без root).
        tables: число table-блоков.
        chunks: число chunks (после chunker'а).
        repeated_blocks: число блоков, помеченных ``is_repeated=True`` в
            cleanup (header/footer/duplicate candidates). Default 0 если
            cleanup не выполнялся.
    """

    chars: int
    estimated_tokens: int
    pages: int
    blocks: int
    sections: int
    tables: int
    chunks: int
    repeated_blocks: int = 0

    @property
    def blocks_per_section(self) -> float:
        """Среднее число блоков на секцию (для adaptive strategy)."""
        if self.sections <= 0:
            return float(self.blocks)
        return self.blocks / self.sections

    @property
    def chars_per_block(self) -> float:
        """Средняя длина блока (для adaptive strategy)."""
        if self.blocks <= 0:
            return 0.0
        return self.chars / self.blocks


def compute_document_stats(
    doc: PhysicalDocument,
    tree: SectionTree | None = None,
    chunks: list[Chunk] | None = None,
    *,
    chars_per_token: float = 4.0,
    repeated_blocks: int = 0,
) -> DocumentStats:
    """Подсчитать ``DocumentStats`` из PhysicalDocument.

    Args:
        doc: PhysicalDocument.
        tree: SectionTree (optional). Если задан — считаем число секций.
        chunks: list[Chunk] (optional). Если задан — считаем число chunks.
        chars_per_token: оценка перевода chars → tokens (default 4.0 —
            это нижняя граница; для русского ~3.5).
        repeated_blocks: число repeated-блоков из cleanup (если выполнялся).

    Returns:
        DocumentStats.
    """
    chars = sum(len(b.content) for b in doc.blocks)
    estimated_tokens = max(1, int(chars / chars_per_token + 0.999))

    blocks = len(doc.blocks)
    tables = sum(1 for b in doc.blocks if b.block_type == "table")

    sections = 0
    if tree is not None:
        sections = sum(
            1 for sid in tree.sections
            if sid != tree.root_id
        )

    chunks_count = len(chunks) if chunks is not None else 0

    return DocumentStats(
        chars=chars,
        estimated_tokens=estimated_tokens,
        pages=doc.page_count,
        blocks=blocks,
        sections=sections,
        tables=tables,
        chunks=chunks_count,
        repeated_blocks=repeated_blocks,
    )


__all__ = ["DocumentStats", "compute_document_stats"]
