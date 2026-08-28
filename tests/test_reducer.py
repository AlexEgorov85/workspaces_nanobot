"""Тесты для ``reducer.py``.

Покрывает:
    * Flat reduce при meaningful_sections < 3
    * Hierarchical reduce при meaningful_sections >= 3
    * section_summaries: per-section
    * Stats разделены на map/reduce/retries
    * Legacy (tree=None) → flat reduce
    * focus только в document_reduce
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "workspace" / "skills" / "legal_summarizer" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
_PROJ = _REPO
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))

from workspace.skills.legal_summarizer.scripts.reducer import (  # noqa: E402
    ReduceConfig,
    ReduceStats,
    reduce_results,
    should_use_hierarchical_reduce,
)
from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk  # noqa: E402
from workspace.skills.legal_summarizer.scripts.structure.physical import (  # noqa: E402
    DocumentBlock,
    PhysicalDocument,
)
from workspace.skills.legal_summarizer.scripts.structure.sections import (  # noqa: E402
    DocumentSection,
    ROOT_SECTION_ID,
    SectionTree,
    detect_sections,
)


def _chunk(
    chunk_id: str,
    chars: int,
    *,
    section_id: str,
    section_path: str,
    section_heading: str = "Heading",
) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        index=int(chunk_id),
        text="x" * chars,
        char_count=chars,
        token_estimate=max(1, chars // 3),
        page_start=1,
        page_end=1,
        section_id=section_id,
        section_path=section_path,
        section_heading=section_heading,
        block_indices=(int(chunk_id),),
        block_types=("paragraph",),
    )


def test_flat_reduce_when_no_sections():
    chunks = [_chunk("000", 100, section_id="s_root", section_path="")]
    chunk_summaries = {"000": "summary 0"}
    result = reduce_results(
        chunks=chunks,
        chunk_summaries=chunk_summaries,
        tree=None,
        length="brief",
        focus=None,
        config=ReduceConfig(),
        llm_runner=None,
    )
    assert result.strategy == "flat"
    assert result.stats.document_reduce_calls == 1
    assert result.stats.section_reduce_calls == 0


def test_hierarchical_reduce_when_meaningful_sections_ge_3():
    doc = _make_doc([
        {"content": "1. Раздел один", "block_metadata": {"style": "Heading 1"}},
        {"content": "Длинное тело раздела один с описанием предмета и обязательств сторон договора аренды."},
        {"content": "2. Раздел два", "block_metadata": {"style": "Heading 1"}},
        {"content": "Длинное тело раздела два с описанием прав и обязанностей сторон договора аренды."},
        {"content": "3. Раздел три", "block_metadata": {"style": "Heading 1"}},
        {"content": "Длинное тело раздела три с описанием ответственности и санкций за нарушение обязательств."},
    ])
    tree = detect_sections(doc)
    chunks = [
        _chunk("000", 100, section_id=next(iter(tree.sections.keys() & {"s_0001"})), section_path="1", section_heading="Раздел один"),
    ]
    chunk_summaries = {"000": "summary 0"}
    sections_dict = {sid: s for sid, s in tree.sections.items() if sid != ROOT_SECTION_ID}
    chunk_id_to_section = {c.chunk_id: c.section_id for c in chunks}

    def fake_llm(messages, **kwargs):
        if kwargs.get("trim"):
            return messages[:200]
        if "section_path" in kwargs and kwargs["section_path"]:
            return f"section_summary[{kwargs.get('section_path')}]"
        return "final_summary"

    result = reduce_results(
        chunks=chunks,
        chunk_summaries=chunk_summaries,
        tree=tree,
        length="brief",
        focus=None,
        config=ReduceConfig(),
        llm_runner=fake_llm,
    )
    assert result.strategy == "hierarchical"


def test_no_chunks_returns_empty():
    result = reduce_results(
        chunks=[],
        chunk_summaries={},
        tree=None,
        length="brief",
        focus=None,
        config=ReduceConfig(),
        llm_runner=None,
    )
    assert result.strategy == "empty"


def test_no_summaries_returns_no_summaries():
    chunks = [_chunk("000", 100, section_id="s_0001", section_path="1")]
    result = reduce_results(
        chunks=chunks,
        chunk_summaries={},
        tree=None,
        length="brief",
        focus=None,
        config=ReduceConfig(),
        llm_runner=None,
    )
    assert result.strategy == "no_summaries"


def test_should_use_hierarchical_with_tree_none():
    assert not should_use_hierarchical_reduce(None, [], threshold=3)


def test_stats_separate_map_reduce_retries():
    stats = ReduceStats(
        map_calls=10,
        section_reduce_calls=4,
        section_trim_calls=1,
        document_reduce_calls=1,
        retries=2,
    )
    d = stats.to_dict()
    assert d["map_calls"] == 10
    assert d["reduce_calls"] == 6
    assert d["total_llm_calls"] == 16
    assert d["retries"] == 2


def test_focus_only_in_document_reduce():
    captured = {}

    def fake_llm(messages, **kwargs):
        captured.setdefault("calls", []).append(kwargs)
        return "result"

    doc = _make_doc([
        {"content": "1. Раздел один", "block_metadata": {"style": "Heading 1"}},
        {"content": "Длинное тело раздела один с подробным описанием."},
        {"content": "2. Раздел два", "block_metadata": {"style": "Heading 1"}},
        {"content": "Длинное тело раздела два с подробным описанием."},
        {"content": "3. Раздел три", "block_metadata": {"style": "Heading 1"}},
        {"content": "Длинное тело раздела три с подробным описанием."},
    ])
    tree = detect_sections(doc)
    section_ids = [sid for sid in tree.sections if sid != ROOT_SECTION_ID]
    chunks = [
        _chunk(f"{i:03d}", 100, section_id=section_ids[i], section_path=f"{i+1}", section_heading=f"R{i+1}")
        for i in range(3)
    ]
    chunk_summaries = {c.chunk_id: f"summary {c.chunk_id}" for c in chunks}

    result = reduce_results(
        chunks=chunks,
        chunk_summaries=chunk_summaries,
        tree=tree,
        length="brief",
        focus="сроки и штрафы",
        config=ReduceConfig(),
        llm_runner=fake_llm,
    )

    section_calls = [c for c in captured["calls"] if c.get("section_path")]
    document_calls = [c for c in captured["calls"] if not c.get("section_path")]
    for c in section_calls:
        assert c.get("focus") is None
    for c in document_calls:
        assert c.get("focus") == "сроки и штрафы"


def _make_doc(blocks_data: list[dict]) -> PhysicalDocument:
    blocks: list[DocumentBlock] = []
    for i, b in enumerate(blocks_data):
        blocks.append(
            DocumentBlock(
                block_id=f"b_{i:04d}",
                block_type=b.get("block_type", "paragraph"),
                content=b["content"],
                char_count=len(b["content"]),
                page_index=1,
                page_start=1,
                page_end=1,
                paragraph_index=i,
                table_index=None,
                ordinal=i,
                block_metadata=b.get("block_metadata", {}),
            )
        )
    return PhysicalDocument(
        path="<test>",
        format="docx",
        title=None,
        size_bytes=0,
        blocks=tuple(blocks),
        page_count=1,
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))