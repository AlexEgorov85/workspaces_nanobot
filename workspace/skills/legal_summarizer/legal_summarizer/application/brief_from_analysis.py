"""Brief reuse analysis для ``application`` layer.

``brief`` mode не должен заново парсить документ, detect_sections, chunk,
и т.п. Должен работать через ``DocumentAnalysis`` + importance-aware
selection.

Этот модуль — convenience helper для ``application.chunk_selection``:
``select_brief_chunks_from_analysis``.
"""

from __future__ import annotations

from legal_summarizer.chunking.chunks import Chunk
from legal_summarizer.chunking.importance_brief import (
    BriefSelectionConfig,
    select_brief_chunks,
)
from legal_summarizer.document.analysis import DocumentAnalysis


def select_brief_chunks_from_analysis(
    analysis: DocumentAnalysis,
    *,
    config: BriefSelectionConfig | None = None,
) -> tuple[Chunk, ...]:
    """Выбрать chunks для brief из cached analysis.

    Использует ``DocumentAnalysis.chunks`` и
    ``DocumentAnalysis.structure`` — **без** повторного parsing.
    """
    return tuple(select_brief_chunks(
        analysis.chunks, analysis.structure, config=config,
    ))


__all__ = ["select_brief_chunks_from_analysis"]
