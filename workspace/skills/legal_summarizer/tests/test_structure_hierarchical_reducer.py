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


def test_reduce_chunks_1_section():
    """PLAN §25: 1 section — single round."""
    from workspace.skills.legal_summarizer.scripts.structure.hierarchical_reducer import (
        HierarchicalReducerConfig,
        reduce_chunks_hierarchical,
    )
    chunks = [
        _make_chunk("c0", section_id="s1", text="body 1"),
        _make_chunk("c1", section_id="s1", text="body 2"),
    ]
    partials = {"c0": "summary of c0", "c1": "summary of c1"}
    cfg = HierarchicalReducerConfig(group_size=3, max_rounds=4)
    result = reduce_chunks_hierarchical(
        chunks, partials,
        section_ids=["s1"],
        config=cfg,
    )
    assert len(result.section_summaries) == 1
    assert result.final_summary != ""
    assert result.rounds_done == 0


def test_reduce_chunks_2_sections():
    """PLAN §25: 2 sections — single round."""
    from workspace.skills.legal_summarizer.scripts.structure.hierarchical_reducer import (
        HierarchicalReducerConfig,
        reduce_chunks_hierarchical,
    )
    chunks = [
        _make_chunk("c0", section_id="s1", text="body 1"),
        _make_chunk("c1", section_id="s2", text="body 2"),
    ]
    partials = {"c0": "summary of c0", "c1": "summary of c1"}
    cfg = HierarchicalReducerConfig(group_size=3, max_rounds=4)
    result = reduce_chunks_hierarchical(
        chunks, partials,
        section_ids=["s1", "s2"],
        config=cfg,
    )
    assert len(result.section_summaries) == 2


def test_reduce_chunks_3_sections():
    """PLAN §25: 3 sections — single round (group_size=3)."""
    from workspace.skills.legal_summarizer.scripts.structure.hierarchical_reducer import (
        HierarchicalReducerConfig,
        reduce_chunks_hierarchical,
    )
    chunks = [
        _make_chunk("c0", section_id=f"s{i}", text=f"body {i}")
        for i in range(3)
    ]
    partials = {f"c{i}": f"summary {i}" for i in range(3)}
    cfg = HierarchicalReducerConfig(group_size=3, max_rounds=4)
    result = reduce_chunks_hierarchical(
        chunks, partials,
        section_ids=["s0", "s1", "s2"],
        config=cfg,
    )
    assert len(result.section_summaries) == 3
    assert result.rounds_done == 1


def test_reduce_chunks_10_sections():
    """PLAN §25: 10 sections — multiple rounds."""
    from workspace.skills.legal_summarizer.scripts.structure.hierarchical_reducer import (
        HierarchicalReducerConfig,
        reduce_chunks_hierarchical,
    )
    chunks = [
        _make_chunk(f"c{i}", section_id=f"s{i}", text=f"body {i}")
        for i in range(10)
    ]
    partials = {f"c{i}": f"summary {i}" for i in range(10)}
    cfg = HierarchicalReducerConfig(group_size=3, max_rounds=4)
    result = reduce_chunks_hierarchical(
        chunks, partials,
        section_ids=[f"s{i}" for i in range(10)],
        config=cfg,
    )
    assert len(result.section_summaries) == 10
    assert result.rounds_done >= 2


def test_reduce_chunks_100_sections_no_data_loss():
    """PLAN §25: 100 sections — rounds bounded, no section disappears."""
    from workspace.skills.legal_summarizer.scripts.structure.hierarchical_reducer import (
        HierarchicalReducerConfig,
        reduce_chunks_hierarchical,
    )
    n = 100
    chunks = [
        _make_chunk(f"c{i}", section_id=f"s{i}", text=f"body {i}")
        for i in range(n)
    ]
    partials = {f"c{i}": f"summary {i}" for i in range(n)}
    cfg = HierarchicalReducerConfig(group_size=3, max_rounds=4)
    result = reduce_chunks_hierarchical(
        chunks, partials,
        section_ids=[f"s{i}" for i in range(n)],
        config=cfg,
    )
    assert len(result.section_summaries) == n
    assert result.final_summary != ""
    assert result.rounds_done <= cfg.max_rounds


def test_reduce_sections_continues_until_one_item():
    """PLAN §25: max_rounds РѕРіСЂР°РЅРёС‡РёРІР°РµС‚, РЅРѕ РЅРµ С‚РµСЂСЏРµС‚ РґР°РЅРЅС‹Рµ.

    Если после max_rounds остались > 1 элементов,
    reducer должает итерации до одного элемента.
    """
    from workspace.skills.legal_summarizer.scripts.structure.hierarchical_reducer import (
        reduce_sections_to_document,
    )
    pairs = [(f"s{i}", f"section {i} text") for i in range(20)]
    result = reduce_sections_to_document(
        pairs,
        config=None,
        llm_runner=None,
    )
    assert len(result.final_summary) > 0
    assert result.rounds_done >= 3


def _make_chunk(chunk_id: str, *, section_id: str, text: str = "x"):
    """Helper РґР»СЏ СЃРѕР·РґР°РЅРёСЏ Chunk СЃ section_id."""
    from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
    return Chunk(
        chunk_id=chunk_id, index=int(chunk_id.lstrip("c")) if chunk_id.startswith("c") else 0,
        text=text, char_count=len(text), token_estimate=1,
        page_start=1, page_end=1,
        section_id=section_id, section_path=section_id,
        section_heading=section_id,
        block_indices=(int(chunk_id.lstrip("c")) if chunk_id.startswith("c") else 0,),
        block_types=("text",),
    )
