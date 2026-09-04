"""Тесты для full-document fallback (Этап 38 из PLAN.md)."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.full_doc_fallback import (
    FullDocFallbackConfig, decide_retrieval, full_document_fallback,
)


def _c(cid: str, idx: int, text: str = "x") -> Chunk:
    return Chunk(
        chunk_id=cid, index=idx, text=text, char_count=len(text),
        token_estimate=1, page_start=1, page_end=1,
        section_id="s1", section_path="1", section_heading="x",
        block_indices=(0,), block_types=("paragraph",),
    )


def test_full_doc_fallback_empty():
    assert full_document_fallback(()) == ()


def test_full_doc_fallback_under_limit():
    chunks = tuple(_c(f"{i:03d}", i) for i in range(5))
    out = full_document_fallback(chunks)
    assert out == chunks


def test_full_doc_fallback_over_limit_keeps_first_and_last():
    chunks = tuple(_c(f"{i:03d}", i) for i in range(100))
    cfg = FullDocFallbackConfig(max_chunks=10, head_tail_ratio=0.5)
    out = full_document_fallback(chunks, config=cfg)
    assert len(out) == 10
    indices = [c.index for c in out]
    assert min(indices) < 10
    assert max(indices) >= 90


def test_full_doc_fallback_just_head():
    chunks = tuple(_c(f"{i:03d}", i) for i in range(100))
    cfg = FullDocFallbackConfig(max_chunks=10, prefer_first_and_last=False)
    out = full_document_fallback(chunks, config=cfg)
    assert all(c.index < 10 for c in out)


def test_decide_high_confidence():
    hits = tuple(_c(f"{i:03d}", i) for i in range(3))
    d = decide_retrieval(hits)
    assert d.confidence == "high"


def test_decide_medium_confidence():
    hits = (_c("001", 0),)
    d = decide_retrieval(hits)
    assert d.confidence == "low"


def test_decide_very_low_confidence():
    d = decide_retrieval(())
    assert d.confidence == "very_low"


def test_decide_thresholds():
    hits = (_c("001", 0), _c("002", 1))
    d = decide_retrieval(hits, high_threshold=3, medium_threshold=2)
    assert d.confidence == "medium"


def test_decide_returns_reason():
    d = decide_retrieval(())
    assert d.reason != ""