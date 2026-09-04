"""Full-document fallback (PLAN §38, Этап 38).

По плану §38, full-document fallback должен быть **последним** шагом
retrieval cascade — не default'ом. Этот модуль предоставляет явный
helper для controlled fallback, когда confidence низкая.

Confidence levels (PLAN §38):

* high → retrieval answer.
* medium → expanded context (Этап 37).
* low → broader section search.
* very low → controlled full-document fallback (этот модуль).
"""

from __future__ import annotations

from dataclasses import dataclass

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.token_estimator import (
    TokenEstimator, TokenEstimatorConfig,
)


@dataclass(frozen=True)
class FullDocFallbackConfig:
    """Параметры controlled full-document fallback."""

    max_chunks: int = 16
    prefer_first_and_last: bool = True
    head_tail_ratio: float = 0.7


def full_document_fallback(
    chunks: tuple[Chunk, ...],
    *,
    config: FullDocFallbackConfig | None = None,
    estimator: TokenEstimator | None = None,
) -> tuple[Chunk, ...]:
    """Вернуть **controlled** subset документа как последний resort.

    Не отправляет весь документ в LLM. Выбирает:

    * first N chunks (preamble + start of document);
    * last N chunks (conclusion);
    * preserves order.
    """
    cfg = config or FullDocFallbackConfig()
    est = estimator or TokenEstimator(TokenEstimatorConfig())

    if not chunks or cfg.max_chunks <= 0:
        return ()

    sorted_chunks = sorted(chunks, key=lambda c: c.index)
    if len(sorted_chunks) <= cfg.max_chunks:
        return tuple(sorted_chunks)

    n = cfg.max_chunks
    if cfg.prefer_first_and_last:
        head_n = int(n * cfg.head_tail_ratio)
        tail_n = n - head_n
        selected: list[Chunk] = []
        if head_n > 0:
            selected.extend(sorted_chunks[:head_n])
        if tail_n > 0:
            selected.extend(sorted_chunks[-tail_n:])
        seen: set[str] = set()
        unique: list[Chunk] = []
        for c in selected:
            if c.chunk_id in seen:
                continue
            seen.add(c.chunk_id)
            unique.append(c)
        return tuple(unique[:n])

    return tuple(sorted_chunks[:n])


@dataclass(frozen=True)
class RetrievalDecision:
    """Решение retrieval cascade (PLAN §38)."""

    confidence: str
    hits: tuple[Chunk, ...] = ()
    used_full_doc_fallback: bool = False
    reason: str = ""


def decide_retrieval(
    hits: tuple[Chunk, ...],
    *,
    high_threshold: int = 3,
    medium_threshold: int = 2,
) -> RetrievalDecision:
    """Решить confidence level на основе числа hits."""
    if len(hits) >= high_threshold:
        return RetrievalDecision(
            confidence="high", hits=hits,
            reason=f"got {len(hits)} hits >= {high_threshold}",
        )
    if len(hits) >= medium_threshold:
        return RetrievalDecision(
            confidence="medium", hits=hits,
            reason=f"got {len(hits)} hits >= {medium_threshold}",
        )
    if len(hits) >= 1:
        return RetrievalDecision(
            confidence="low", hits=hits,
            reason=f"got only {len(hits)} hit",
        )
    return RetrievalDecision(
        confidence="very_low", hits=(),
        reason="no hits — full-document fallback recommended",
    )


__all__ = [
    "FullDocFallbackConfig",
    "RetrievalDecision",
    "full_document_fallback",
    "decide_retrieval",
]