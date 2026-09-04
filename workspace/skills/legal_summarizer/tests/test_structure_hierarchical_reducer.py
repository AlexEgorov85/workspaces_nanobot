"""Тесты для HierarchicalReducer (Этап 24 из PLAN.md)."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.hierarchical_reducer import (
    HierarchicalReducerConfig,
    deterministic_truncate,
    reduce_chunks_hierarchical,
    reduce_sections_to_document,
)


def _c(cid: str, section: str) -> Chunk:
    return Chunk(
        chunk_id=cid, index=int(cid), text=f"text-{cid}", char_count=10,
        token_estimate=2, page_start=1, page_end=1,
        section_id=section, section_path="1", section_heading=section,
        block_indices=(0,), block_types=("paragraph",),
    )


def test_deterministic_truncate_no_truncation_needed():
    assert deterministic_truncate("short", 100) == "short"


def test_deterministic_truncate_marks_omission():
    long = "x" * 1000
    out = deterministic_truncate(long, 100)
    assert "пропущено" in out
    assert len(out) < len(long)


def test_reduce_sections_to_document_no_runner():
    """Без llm_runner просто объединяет секции (без LLM вызова)."""
    items = [("s1", "summary 1"), ("s2", "summary 2")]
    result = reduce_sections_to_document(items, config=HierarchicalReducerConfig(
        group_size=2, max_rounds=2,
    ))
    assert result.rounds_done == 1
    assert "[s1]" in result.final_summary
    assert "[s2]" in result.final_summary


def test_reduce_sections_to_document_single_section():
    items = [("s1", "summary only")]
    result = reduce_sections_to_document(items, config=HierarchicalReducerConfig())
    assert result.rounds_done == 0
    assert result.final_summary == "summary only"


def test_reduce_sections_with_llm_runner():
    items = [("s1", "a"), ("s2", "b")]

    def runner(joined: str, **kwargs):
        return f"SUMMARIZED: {joined}"

    result = reduce_sections_to_document(
        items, config=HierarchicalReducerConfig(group_size=2, max_rounds=3),
        llm_runner=runner, length="detailed",
    )
    assert "SUMMARIZED" in result.final_summary


def test_reduce_chunks_hierarchical():
    chunks = [
        _c("001", "s1"),
        _c("002", "s1"),
        _c("003", "s2"),
    ]
    summaries = {"001": "sum 1", "002": "sum 2", "003": "sum 3"}
    result = reduce_chunks_hierarchical(
        chunks, summaries,
        section_ids=["s1", "s2"],
        section_headings={"s1": "Section 1", "s2": "Section 2"},
        config=HierarchicalReducerConfig(group_size=5, max_rounds=2),
    )
    assert "s1" in result.section_summaries
    assert "s2" in result.section_summaries
    assert result.rounds_done >= 1


def test_reduce_chunks_skips_empty_sections():
    chunks = [_c("001", "s1")]
    summaries = {"001": "sum 1"}
    result = reduce_chunks_hierarchical(
        chunks, summaries,
        section_ids=["s1", "s2"],
        config=HierarchicalReducerConfig(),
    )
    assert "s2" not in result.section_summaries


def test_reduce_sections_to_document_truncates_long_input():
    items = [("s1", "x" * 100_000), ("s2", "y" * 100_000)]
    cfg = HierarchicalReducerConfig(input_budget_chars=10_000, group_size=2)
    result = reduce_sections_to_document(items, config=cfg)
    assert result.truncated is True


def test_reduce_sections_to_document_respects_max_rounds():
    items = [(f"s{i}", f"sum {i}") for i in range(10)]
    cfg = HierarchicalReducerConfig(group_size=2, max_rounds=2)
    result = reduce_sections_to_document(items, config=cfg)
    assert result.rounds_done <= 2