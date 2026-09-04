"""Question via retrieval index (PLAN §64).

PLAN §64: follow-up ``question`` должен использовать
``DocumentAnalysis.retrieve`` (через inverted index), не substring
first-match.

Этот модуль — convenience: ``answer_question_from_analysis``.
"""

from __future__ import annotations

from dataclasses import dataclass

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.document_analysis import (
    DocumentAnalysis,
)
from workspace.skills.legal_summarizer.scripts.structure.followup import (
    FollowupConfig, FollowupResult, build_followup_response,
)


@dataclass(frozen=True)
class QuestionResponse:
    """Результат question через retrieval index."""

    chunks: tuple[Chunk, ...]
    confidence: str
    used_full_doc_fallback: bool
    reason: str


def answer_question_from_analysis(
    analysis: DocumentAnalysis,
    query: str,
    *,
    config: FollowupConfig | None = None,
) -> QuestionResponse:
    """Ответить на question через cached analysis (PLAN §64).

    Использует ``DocumentAnalysis.retrieve`` (inverted index +
    sparse ranking) — не перепарсивает документ.
    """
    result: FollowupResult = build_followup_response(
        analysis, query=query, mode="question", config=config,
    )
    return QuestionResponse(
        chunks=result.target_chunks,
        confidence=result.confidence,
        used_full_doc_fallback=result.used_full_doc_fallback,
        reason=result.reason,
    )


__all__ = ["QuestionResponse", "answer_question_from_analysis"]