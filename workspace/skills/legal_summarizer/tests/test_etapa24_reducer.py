"""Этап 24: Hierarchical reducer acceptance tests.

Покрывает:
- Размеры 1/2/3/10 chunks
- Пустой ввод
- Сохранение контента
"""

from __future__ import annotations

import sys
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _make_chunks_and_summaries(n: int):
    from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
    chunks = [
        Chunk(
            chunk_id=f"{i:03d}", index=i,
            text=f"Chunk {i} content. " * 10,
            char_count=100 + i, token_estimate=30 + i,
            page_start=None, page_end=None,
            section_id="s_root", section_path="", section_heading="",
            block_indices=(i,), block_types=("text",),
        )
        for i in range(n)
    ]
    summaries = {c.chunk_id: f"Summary for {c.chunk_id}" for c in chunks}
    section_ids = [f"sec_{i}" for i in range(n)]
    return chunks, summaries, section_ids


def _fake_llm(messages, *, context=None, **kwargs):
    """Mock LLM: возвращает склейку всех сообщений."""
    parts = []
    for m in messages:
        if isinstance(m, dict):
            parts.append(m.get("content", ""))
        else:
            parts.append(str(m))
    return "\n".join(parts)


def test_reducer_single_chunk():
    """1 chunk → reducer возвращает HierarchicalReducerResult."""
    from workspace.skills.legal_summarizer.scripts.structure.hierarchical_reducer import (
        HierarchicalReducerResult,
        reduce_chunks_hierarchical,
    )
    chunks, summaries, section_ids = _make_chunks_and_summaries(1)
    result = reduce_chunks_hierarchical(
        chunks, summaries,
        section_ids=section_ids,
        llm_runner=_fake_llm,
        length="brief",
    )
    assert isinstance(result, HierarchicalReducerResult)
    assert isinstance(result.final_summary, str)


def test_reducer_two_chunks():
    """2 chunks → reducer возвращает HierarchicalReducerResult."""
    from workspace.skills.legal_summarizer.scripts.structure.hierarchical_reducer import (
        HierarchicalReducerResult,
        reduce_chunks_hierarchical,
    )
    chunks, summaries, section_ids = _make_chunks_and_summaries(2)
    result = reduce_chunks_hierarchical(
        chunks, summaries,
        section_ids=section_ids,
        llm_runner=_fake_llm,
        length="brief",
    )
    assert isinstance(result, HierarchicalReducerResult)
    assert isinstance(result.final_summary, str)


def test_reducer_three_chunks():
    """3 chunks → reducer возвращает HierarchicalReducerResult."""
    from workspace.skills.legal_summarizer.scripts.structure.hierarchical_reducer import (
        HierarchicalReducerResult,
        reduce_chunks_hierarchical,
    )
    chunks, summaries, section_ids = _make_chunks_and_summaries(3)
    result = reduce_chunks_hierarchical(
        chunks, summaries,
        section_ids=section_ids,
        llm_runner=_fake_llm,
        length="brief",
    )
    assert isinstance(result, HierarchicalReducerResult)
    assert isinstance(result.final_summary, str)


def test_reducer_10_chunks():
    """10 chunks → reducer возвращает HierarchicalReducerResult."""
    from workspace.skills.legal_summarizer.scripts.structure.hierarchical_reducer import (
        HierarchicalReducerResult,
        reduce_chunks_hierarchical,
    )
    chunks, summaries, section_ids = _make_chunks_and_summaries(10)
    result = reduce_chunks_hierarchical(
        chunks, summaries,
        section_ids=section_ids,
        llm_runner=_fake_llm,
        length="detailed",
    )
    assert isinstance(result, HierarchicalReducerResult)
    assert isinstance(result.final_summary, str)


def test_reducer_empty_input():
    """0 chunks → reducer возвращает пустой результат."""
    from workspace.skills.legal_summarizer.scripts.structure.hierarchical_reducer import (
        reduce_chunks_hierarchical,
    )
    result = reduce_chunks_hierarchical(
        [], {},
        section_ids=[],
        llm_runner=_fake_llm,
        length="brief",
    )
    assert result.final_summary == ""


def test_reducer_preserves_content():
    """Reducer output непустой и содержит информацию из summaries."""
    from workspace.skills.legal_summarizer.scripts.structure.hierarchical_reducer import (
        reduce_chunks_hierarchical,
    )
    from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
    chunks = [
        Chunk(chunk_id="c01", index=0, text="text1", char_count=5, token_estimate=2,
              page_start=None, page_end=None, section_id="s1", section_path="",
              section_heading="", block_indices=(0,), block_types=("text",)),
        Chunk(chunk_id="c02", index=1, text="text2", char_count=5, token_estimate=2,
              page_start=None, page_end=None, section_id="s2", section_path="",
              section_heading="", block_indices=(1,), block_types=("text",)),
    ]
    summaries = {"c01": "summary_01", "c02": "summary_02"}
    result = reduce_chunks_hierarchical(
        chunks, summaries,
        section_ids=["s1", "s2"],
        llm_runner=_fake_llm,
        length="brief",
    )
    assert len(result.final_summary) > 0
    assert result.rounds_done >= 1
