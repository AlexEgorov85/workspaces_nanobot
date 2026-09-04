"""Canonical retrieval wrapper (Этап 16А).

Использует только ``DocumentAnalysis.retrieve`` и canonical
``build_followup_response`` (через ``structure.followup``).
"""

from __future__ import annotations

from typing import Any

from workspace.skills.legal_summarizer.scripts.structure.document_analysis import (
    DocumentAnalysis,
)
from workspace.skills.legal_summarizer.scripts.structure.followup import (
    FollowupConfig,
    FollowupResult,
    build_followup_response,
)


def answer_followup(
    analysis: DocumentAnalysis,
    query: str,
    *,
    config: FollowupConfig | None = None,
) -> FollowupResult:
    """Ответить на follow-up question через canonical analysis.

    Не делает повторного parsing/structure/chunking — работает
    только по ``DocumentAnalysis`` snapshot.
    """
    return build_followup_response(
        analysis,
        query,
        mode="question",
        config=config,
    )


def select_brief_from_analysis(
    analysis: DocumentAnalysis,
    *,
    config: FollowupConfig | None = None,
) -> FollowupResult:
    """Выбрать chunks для brief через canonical analysis."""
    return build_followup_response(
        analysis,
        None,
        mode="brief",
        config=config,
    )


__all__ = [
    "answer_followup",
    "select_brief_from_analysis",
]