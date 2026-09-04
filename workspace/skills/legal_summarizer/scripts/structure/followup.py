"""First-run vs Follow-up split (PLAN §40, Этап 40).

Архитектурное разделение (PLAN §40):

* **First-run**: file → parse → structure → chunk → semantic map → cache.
  Долго (LLM-вызовы). Результат — ``DocumentAnalysis``.
* **Follow-up**: cached analysis → retrieval → context expansion →
  ONE final LLM call. Быстро (один LLM-вызов).

Этот модуль предоставляет явные функции для обоих режимов:

* ``build_first_run_analysis(physical, structure, chunks, records)``
* ``build_followup_response(analysis, query, mode)``

Back-compat: текущий pipeline (``summarizer.py``) использует свой
путь. Этот модуль — новый канонический API для будущих consumers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from workspace.skills.legal_summarizer.scripts.structure.context_expansion import (
    ContextExpansionConfig, expand_context,
)
from workspace.skills.legal_summarizer.scripts.structure.document_analysis import (
    DocumentAnalysis,
)
from workspace.skills.legal_summarizer.scripts.structure.full_doc_fallback import (
    FullDocFallbackConfig, decide_retrieval, full_document_fallback,
)
from workspace.skills.legal_summarizer.scripts.structure.importance_brief import (
    BriefSelectionConfig, select_brief_chunks,
)
from workspace.skills.legal_summarizer.scripts.structure.retrieval import (
    RetrievalConfig,
)


@dataclass(frozen=True)
class FollowupConfig:
    """Параметры follow-up запроса."""

    retrieval_config: RetrievalConfig | None = None
    expansion_config: ContextExpansionConfig | None = None
    fallback_config: FullDocFallbackConfig | None = None
    brief_config: BriefSelectionConfig | None = None


@dataclass(frozen=True)
class FollowupResult:
    """Результат follow-up запроса."""

    target_chunks: tuple
    total_tokens: int
    confidence: str
    used_full_doc_fallback: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_count": len(self.target_chunks),
            "total_tokens": self.total_tokens,
            "confidence": self.confidence,
            "used_full_doc_fallback": self.used_full_doc_fallback,
            "reason": self.reason,
        }


def build_first_run_analysis(
    *,
    analysis: DocumentAnalysis,
) -> DocumentAnalysis:
    """First-run: возвращает готовый ``DocumentAnalysis``.

    Этот wrapper нужен для явной семантики в коде — отличить first-run
    cache build от follow-up reuse.
    """
    return analysis


def build_followup_response(
    analysis: DocumentAnalysis,
    query: str | None = None,
    *,
    mode: str = "question",
    config: FollowupConfig | None = None,
) -> FollowupResult:
    """Follow-up: retrieval → expansion → (low conf) full-doc fallback.

    Args:
        analysis: ``DocumentAnalysis`` из cache (PLAN §40 — не перепарсиваем).
        query: для question mode; для brief mode ``None``.
        mode: ``"question"`` или ``"brief"``.
        config: ``FollowupConfig`` (overrides).
    """
    cfg = config or FollowupConfig()

    if mode == "brief":
        brief_cfg = cfg.brief_config or BriefSelectionConfig()
        selected = select_brief_chunks(
            analysis.chunks, analysis.structure, config=brief_cfg,
        )
        return FollowupResult(
            target_chunks=tuple(selected),
            total_tokens=sum(len(c.text) for c in selected),
            confidence="medium",
            used_full_doc_fallback=False,
            reason="brief mode — importance selection",
        )

    hits = analysis.retrieve(query or "", config=cfg.retrieval_config)
    decision = decide_retrieval(tuple(h.chunk_id for h in hits))

    if decision.confidence in ("high", "medium"):
        target_ids = [h.chunk_id for h in hits[:8]]
        target_chunks = tuple(
            analysis.get_chunk(cid) for cid in target_ids
            if analysis.get_chunk(cid) is not None
        )
        return FollowupResult(
            target_chunks=target_chunks,
            total_tokens=sum(len(c.text) for c in target_chunks),
            confidence=decision.confidence,
            used_full_doc_fallback=False,
            reason=decision.reason,
        )

    if decision.confidence == "low" and hits:
        top_hit = analysis.get_chunk(hits[0].chunk_id)
        if top_hit is not None:
            expansion = expand_context(
                top_hit, analysis.chunks, analysis.structure,
                config=cfg.expansion_config,
            )
            target_chunks = (top_hit,) + expansion.neighbour_chunks
            return FollowupResult(
                target_chunks=target_chunks,
                total_tokens=expansion.total_tokens,
                confidence="low",
                used_full_doc_fallback=False,
                reason=f"low confidence — expanded from {top_hit.chunk_id}",
            )

    fallback_cfg = cfg.fallback_config or FullDocFallbackConfig()
    selected = full_document_fallback(
        analysis.chunks, config=fallback_cfg,
    )
    return FollowupResult(
        target_chunks=selected,
        total_tokens=sum(len(c.text) for c in selected),
        confidence="very_low",
        used_full_doc_fallback=True,
        reason="no/insufficient hits — controlled full-doc fallback",
    )


__all__ = [
    "FollowupConfig",
    "FollowupResult",
    "build_first_run_analysis",
    "build_followup_response",
]