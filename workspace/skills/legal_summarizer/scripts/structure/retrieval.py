"""Question retrieval cascade (PLAN §33–35, Этапы 33–35).

Целевой cascade:

    user query
        ↓
    query normalization  (Этап 34: query_normalizer)
        ↓
    stopword removal    (минимальный, без LLM)
        ↓
    normalized lexical search (substring match)
        ↓
    sparse ranking      (PLAN §35: score, BM25-lite)
        ↓
    context expansion   (PLAN §37)
        ↓
    final top-K

Сейчас в проекте ``cached_retrieval.select_relevant_chunks`` — substring +
first-match + full-document fallback. Это **слишком просто** (PLAN §33).

Этот модуль предоставляет новый каскад, который постепенно заменит
старый (Этап 45).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk


@dataclass(frozen=True)
class RetrievalHit:
    """Один кандидат из retrieval cascade."""

    chunk_id: str
    score: float
    title_hit: bool
    section_title_hit: bool
    matched_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetrievalConfig:
    """Параметры retrieval cascade."""

    max_results: int = 8
    min_score: float = 0.05
    section_title_weight: float = 2.0
    heading_weight: float = 1.5
    body_weight: float = 1.0


_RUSSIAN_STOPWORDS = frozenset({
    "и", "в", "на", "с", "по", "для", "не", "что", "это", "как",
    "к", "у", "из", "от", "о", "об", "за", "но", "а", "то",
    "все", "она", "он", "мы", "вы", "они", "оно",
    "быть", "есть", "является",
})

_WORD_RE = re.compile(r"\w+", re.UNICODE)  # noqa: W605 — \w is valid Python regex syntax


def tokenize(text: str) -> list[str]:
    """Нормализация + tokenization (PLAN §34).

    Без LLM. Приводит к lowercase, выбрасывает стоп-слова, оставляет
    только word-tokens (regex \\w+).
    """
    if not text:
        return []
    text = text.lower()
    words = _WORD_RE.findall(text)
    return [w for w in words if w and w not in _RUSSIAN_STOPWORDS]


def score_chunk(
    chunk: Chunk,
    terms: list[str],
    *,
    config: RetrievalConfig,
) -> RetrievalHit:
    """Посчитать score для chunk'а по термам (PLAN §35).

    Score = sum(weight * term_frequency_in_section).

    ``section_title_hit``/``title_hit`` — boost'ы, если терм встречается
    в section title или в chunk's own short title (heuristic).
    """
    if not terms:
        return RetrievalHit(chunk_id=chunk.chunk_id, score=0.0,
                          title_hit=False, section_title_hit=False, matched_terms=())

    text_lower = chunk.text.lower()
    section_title_lower = (chunk.section_heading or "").lower()

    score = 0.0
    matched: list[str] = []
    for term in terms:
        if term in section_title_lower:
            score += config.section_title_weight
            matched.append(term)
        if term in text_lower:
            score += config.body_weight
            if term not in matched:
                matched.append(term)

    if len(chunk.text) < 200 and any(t in text_lower for t in terms):
        score += config.heading_weight - config.body_weight

    return RetrievalHit(
        chunk_id=chunk.chunk_id,
        score=score,
        title_hit=len(chunk.text) < 200 and bool(matched),
        section_title_hit=any(t in section_title_lower for t in terms),
        matched_terms=tuple(matched),
    )


def retrieve_chunks(
    chunks: Iterable[Chunk],
    query: str,
    *,
    config: RetrievalConfig | None = None,
) -> list[RetrievalHit]:
    """Cascade retrieval: normalize → score → top-K.

    PLAN §33: retrieval ranking (не first-match).
    """
    cfg = config or RetrievalConfig()
    terms = tokenize(query)
    if not terms:
        return []

    hits: list[RetrievalHit] = []
    for chunk in chunks:
        hit = score_chunk(chunk, terms, config=cfg)
        if hit.score >= cfg.min_score:
            hits.append(hit)

    hits.sort(key=lambda h: (-h.score, h.chunk_id))
    return hits[: cfg.max_results]


__all__ = [
    "RetrievalHit",
    "RetrievalConfig",
    "tokenize",
    "score_chunk",
    "retrieve_chunks",
]