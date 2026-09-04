"""Тесты для importance score (Этап 66 из PLAN.md)."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.importance_score import (
    ImportanceScore, compute_importance, select_top_chunks_by_importance,
)


def _c(cid: str, text: str, idx: int = 0, section: str = "s1") -> Chunk:
    return Chunk(
        chunk_id=cid, index=idx, text=text, char_count=len(text),
        token_estimate=1, page_start=1, page_end=1,
        section_id=section, section_path="1",
        section_heading="Section", block_indices=(0,),
        block_types=("paragraph",),
    )


def test_short_chunk_high_score():
    s = compute_importance(_c("001", "short"), section_level=1)
    assert s.is_heading >= 1.0


def test_legal_keywords():
    s = compute_importance(_c("001", "Статья 12. Штраф за нарушение"))
    assert s.legal_importance > 0


def test_definition_bonus():
    s = compute_importance(_c("001", "Договор определяется как соглашение сторон"))
    assert s.is_definition > 0


def test_first_chunk_bonus():
    s = compute_importance(_c("001", "x"), section_index=0, section_chunk_count=5)
    assert s.is_first_in_section == 1.0


def test_last_chunk_bonus():
    s = compute_importance(_c("001", "x"), section_index=4, section_chunk_count=5)
    assert s.is_last_in_section == 1.0


def test_total_score():
    s = compute_importance(_c("001", "Статья 12"), section_level=1)
    assert s.total > 0


def test_importance_score_dataclass():
    s = ImportanceScore(is_title=1.0)
    assert s.total >= 1.0


def test_select_top_chunks_by_importance():
    chunks = (
        _c("001", "x" * 500),
        _c("002", "Статья 12. Штраф за нарушение"),
        _c("003", "y"),
    )
    selected = select_top_chunks_by_importance(chunks, top_k=2)
    assert len(selected) == 2


def test_select_top_chunks_empty():
    assert select_top_chunks_by_importance(()) == []