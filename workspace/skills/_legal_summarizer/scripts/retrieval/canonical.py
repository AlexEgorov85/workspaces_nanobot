"""Canonical retrieval wrapper.

Использует только ``DocumentAnalysis.retrieve`` и canonical
``build_followup_response`` (через ``structure.followup``) для
``mode="question"``.

Архитектурное замечание (brief-refactor): ``select_brief_from_analysis``
УДАЛЁН из этого модуля — он нарушал архитектурное правило
``retrieval → application`` (запрещено). Для brief используйте
``application.chunk_selection.select_chunks_for_mode`` напрямую.
"""

from __future__ import annotations

from document.analysis import (
    DocumentAnalysis,
)
from retrieval.followup import (
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


__all__ = [
    "answer_followup",
]

