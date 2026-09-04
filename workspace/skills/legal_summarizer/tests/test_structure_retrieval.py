"""Тесты для retrieval cascade (Этапы 33–35 из PLAN.md)."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.retrieval import (
    RetrievalConfig, retrieve_chunks, score_chunk, tokenize,
)


def _c(cid: str, text: str, section_heading: str = "") -> Chunk:
    return Chunk(
        chunk_id=cid, index=int(cid), text=text, char_count=len(text),
        token_estimate=1, page_start=1, page_end=1,
        section_id="s1", section_path="1", section_heading=section_heading,
        block_indices=(0,), block_types=("paragraph",),
    )


def test_tokenize_basic():
    tokens = tokenize("Какой срок оплаты по договору?")
    assert "какой" in tokens
    assert "срок" in tokens
    assert "оплаты" in tokens
    assert "договору" in tokens
    assert "по" not in tokens


def test_tokenize_strips_punctuation():
    tokens = tokenize("оплата, штраф; неустойка.")
    assert "оплата" in tokens
    assert "штраф" in tokens
    assert "неустойка" in tokens


def test_tokenize_empty():
    assert tokenize("") == []


def test_tokenize_only_stopwords():
    assert tokenize("и в на с") == []


def test_score_section_title_boost():
    chunk = _c("001", "some body", section_heading="Срок оплаты")
    terms = tokenize("срок оплаты")
    cfg = RetrievalConfig()
    hit = score_chunk(chunk, terms, config=cfg)
    assert hit.section_title_hit is True
    assert hit.score >= 2.0


def test_score_body_only():
    chunk = _c("001", "срок оплаты 30 дней")
    terms = tokenize("срок")
    hit = score_chunk(chunk, terms, config=RetrievalConfig())
    assert hit.score > 0


def test_retrieve_ranked():
    chunks = (
        _c("001", "срок оплаты — 30 дней", section_heading="Общее"),
        _c("002", "срок хранения — 5 лет", section_heading="Срок хранения"),
        _c("003", "просто текст", section_heading="Другое"),
    )
    hits = retrieve_chunks(chunks, "срок оплаты")
    assert hits[0].chunk_id in ("001", "002")


def test_retrieve_max_results():
    chunks = tuple(_c(f"{i:03d}", f"срок {i}") for i in range(20))
    cfg = RetrievalConfig(max_results=3)
    hits = retrieve_chunks(chunks, "срок", config=cfg)
    assert len(hits) == 3


def test_retrieve_no_match_returns_empty():
    chunks = (_c("001", "hello"),)
    assert retrieve_chunks(chunks, "xyz123") == []


def test_retrieve_returns_score_and_matched_terms():
    chunks = (
        _c("001", "оплата по факту", section_heading="Оплата"),
    )
    hits = retrieve_chunks(chunks, "оплата")
    assert len(hits) == 1
    assert hits[0].score > 0
    assert "оплата" in hits[0].matched_terms


def test_retrieve_min_score_filter():
    chunks = (_c("001", "minor text"),)
    cfg = RetrievalConfig(min_score=100.0)
    assert retrieve_chunks(chunks, "minor", config=cfg) == []


def test_retrieve_returns_deterministic_ordered_by_score():
    chunks = (
        _c("001", "срок оплаты срок оплаты срок оплаты"),
        _c("002", "срок"),
    )
    hits = retrieve_chunks(chunks, "срок оплаты")
    assert hits[0].score > hits[1].score