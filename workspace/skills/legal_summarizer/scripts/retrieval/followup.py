"""First-run vs Follow-up split.

Архитектурное разделение:

* **First-run**: file → parse → structure → chunk → semantic map → cache.
  Долго (LLM-вызовы). Результат — ``DocumentAnalysis``.
* **Follow-up**: cached analysis → retrieval → context expansion →
  ONE final LLM call. Быстро (один LLM-вызов).

Этот модуль предоставляет явные функции для обоих режимов:

* ``build_first_run_analysis(physical, structure, chunks, records)``
* ``build_followup_response(analysis, query, mode)`` — только
  ``mode="question"``.

Архитектурное замечание (brief-refactor): brief-режим НЕ
маршрутизируется через ``build_followup_response``. Brief — это
chunk-selection concern (см.
``application.chunk_selection.select_chunks_for_mode``),
а ``build_followup_response`` обслуживает только ``mode="question"``.
Это сохраняет архитектурное правило ``retrieval → application``
(запрещено); ``followup.py`` остаётся на слое ``retrieval`` без
обратной зависимости.

Back-compat: текущий pipeline (``summarizer.py``) использует свой
путь. Этот модуль — новый канонический API для будущих consumers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from retrieval.context_expansion import (
    ContextExpansionConfig, expand_context,
)
from document.analysis import (
    DocumentAnalysis,
)
from retrieval.fallback import (
    FullDocFallbackConfig, decide_retrieval, full_document_fallback,
)
from retrieval.query import (
    RetrievalConfig,
)


@dataclass(frozen=True)
class FollowupConfig:
    """Параметры follow-up запроса (режим ``question``)."""

    retrieval_config: RetrievalConfig | None = None
    expansion_config: ContextExpansionConfig | None = None
    fallback_config: FullDocFallbackConfig | None = None


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
        analysis: ``DocumentAnalysis`` из cache (не перепарсиваем).
        query: для question mode.
        mode: ``"question"`` (единственный поддерживаемый режим после
            brief-refactor — brief маршрутизируется через
            ``application.chunk_selection.select_chunks_for_mode``).
        config: ``FollowupConfig`` (overrides).

    Raises:
        NotImplementedError: если ``mode != "question"``.
    """
    cfg = config or FollowupConfig()

    if mode != "question":
        raise NotImplementedError(
            f"build_followup_response: mode={mode!r} не поддерживается; "
            "используйте application.chunk_selection.select_chunks_for_mode "
            "для brief-режима"
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
