"""Importance score для chunk (PLAN §66).

Deterministic score (PLAN §61) на основе:

* is_title (короткий chunk с section_heading).
* is_heading (короткий, body < 200).
* section_level (низкий level = важнее).
* legal importance (есть legal keywords).
* first_section_chunk (первый в section).
* last_section_chunk (последний в section).
* definition block (содержит "определяется как" и т.п.).

Используется в brief, retrieval tie-break, packing, fallback selection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk


_LEGAL_KEYWORDS = (
    "статья", "глава", "раздел", "пункт", "часть", "§",
    "определяется", "определение", "обязан", "ответственность",
    "штраф", "неустойка", "расторжение", "прекращение",
)


@dataclass(frozen=True)
class ImportanceScore:
    """Score компоненты для chunk."""

    is_title: float = 0.0
    is_heading: float = 0.0
    section_level: float = 0.0
    legal_importance: float = 0.0
    is_first_in_section: float = 0.0
    is_last_in_section: float = 0.0
    is_definition: float = 0.0

    @property
    def total(self) -> float:
        return (
            self.is_title
            + self.is_heading
            + self.section_level
            + self.legal_importance
            + self.is_first_in_section
            + self.is_last_in_section
            + self.is_definition
        )


def compute_importance(
    chunk: Chunk,
    *,
    section_chunk_count: int = 0,
    section_index: int = 0,
    section_level: int = 1,
) -> ImportanceScore:
    """Вычислить importance score для chunk."""
    text_lower = chunk.text.lower()

    is_title = 1.0 if (len(chunk.text) < 100 and chunk.section_heading) else 0.0

    is_heading = 1.0 if len(chunk.text) < 200 else 0.0

    level_weight = max(0.0, 2.0 - section_level)

    legal = sum(0.5 for kw in _LEGAL_KEYWORDS if kw in text_lower)

    first = 1.0 if section_index == 0 else 0.0
    last = 1.0 if section_index == max(0, section_chunk_count - 1) else 0.0

    definition = 0.5 if "определяется" in text_lower or "определение" in text_lower else 0.0

    return ImportanceScore(
        is_title=is_title,
        is_heading=is_heading,
        section_level=level_weight,
        legal_importance=legal,
        is_first_in_section=first,
        is_last_in_section=last,
        is_definition=definition,
    )


def select_top_chunks_by_importance(
    chunks: Iterable[Chunk],
    *,
    top_k: int = 8,
) -> list[Chunk]:
    """Выбрать top-K chunks по importance score."""
    scored: list[tuple[float, Chunk]] = []
    chunk_list = list(chunks)
    chunk_count = len(chunk_list)
    for i, chunk in enumerate(chunk_list):
        score = compute_importance(
            chunk, section_chunk_count=chunk_count, section_index=i,
        )
        scored.append((score.total, chunk))
    scored.sort(key=lambda x: -x[0])
    return [c for _, c in scored[:top_k]]


__all__ = ["ImportanceScore", "compute_importance", "select_top_chunks_by_importance"]