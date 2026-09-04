"""Brief reuse analysis (PLAN §63).

PLAN §63: ``brief`` mode не должен заново парсить документ,
detect_sections, chunk, и т.п. Должен работать через ``DocumentAnalysis``
+ importance-aware selection.

Этот модуль — convenience helper: ``select_brief_chunks_from_analysis``.
"""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.document_analysis import (
    DocumentAnalysis,
)
from workspace.skills.legal_summarizer.scripts.structure.importance_brief import (
    BriefSelectionConfig, select_brief_chunks,
)


def select_brief_chunks_from_analysis(
    analysis: DocumentAnalysis,
    *,
    config: BriefSelectionConfig | None = None,
) -> tuple[Chunk, ...]:
    """Выбрать chunks для brief из cached analysis (PLAN §63).

    Использует ``DocumentAnalysis.chunks`` и
    ``DocumentAnalysis.structure`` — **без** повторного parsing.
    """
    return tuple(select_brief_chunks(
        analysis.chunks, analysis.structure, config=config,
    ))


__all__ = ["select_brief_chunks_from_analysis"]