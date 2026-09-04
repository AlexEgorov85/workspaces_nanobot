"""Тесты для importance-aware brief selection (Этап 31 из PLAN.md)."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.importance_brief import (
    BriefSelectionConfig, select_brief_chunks,
)
from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentStructure, StructureNode,
)


def _b(ord: int) -> StructureNode:
    return StructureNode(
        node_id="n_0000", node_type="document", semantic_type=None,
        level=0, title="", number=None, parent_id=None,
        children=(), start_block=0, end_block=100, confidence=1.0,
    )


def _sec(nid: str, *, start: int = 0, end: int = 5, level: int = 1,
         title: str = "Section") -> StructureNode:
    return StructureNode(
        node_id=nid, node_type="section", semantic_type=None,
        level=level, title=title, number=None, parent_id="n_0000",
        children=(), start_block=start, end_block=end,
        confidence=0.7,
    )


def _struct_with_sections(num_sections: int) -> DocumentStructure:
    sec_ids = [f"n_{i:04d}" for i in range(1, num_sections + 1)]
    root = StructureNode(
        node_id="n_0000", node_type="document", semantic_type=None,
        level=0, title="", number=None, parent_id=None,
        children=tuple(sec_ids), start_block=0, end_block=num_sections * 10,
        confidence=1.0,
    )
    nodes = {"n_0000": root}
    for i, sid in enumerate(sec_ids):
        nodes[sid] = _sec(
            sid, start=i * 10, end=(i + 1) * 10 - 1, title=f"Section {i + 1}",
        )
    return DocumentStructure(
        document_id="d", title=None, nodes=nodes,
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=num_sections * 10,
    )


def _chunk(cid: str, section: str, text: str = "x") -> Chunk:
    return Chunk(
        chunk_id=cid, index=int(cid), text=text, char_count=len(text),
        token_estimate=1, page_start=1, page_end=1,
        section_id=section, section_path="1", section_heading=section,
        block_indices=(0,), block_types=("paragraph",),
    )


def test_select_first_per_top_level():
    s = _struct_with_sections(3)
    chunks = (
        _chunk("001", "n_0001"),
        _chunk("002", "n_0001"),
        _chunk("003", "n_0002"),
        _chunk("004", "n_0003"),
    )
    selected = select_brief_chunks(chunks, s, config=BriefSelectionConfig(target_chunk_count=10))
    assert "001" in [c.chunk_id for c in selected]
    assert "003" in [c.chunk_id for c in selected]
    assert "004" in [c.chunk_id for c in selected]


def test_select_legal_important():
    s = _struct_with_sections(3)
    chunks = (
        _chunk("001", "n_0001", text="обычный текст"),
        _chunk("002", "n_0002", text="Срок оплаты — 30 дней"),
    )
    selected = select_brief_chunks(chunks, s, config=BriefSelectionConfig(
        target_chunk_count=5, coverage_ratio=1.0,
    ))
    selected_ids = [c.chunk_id for c in selected]
    assert "002" in selected_ids


def test_select_respects_target():
    s = _struct_with_sections(2)
    chunks = tuple(_chunk(f"{i:03d}", f"n_{((i - 1) // 5 + 1):04d}") for i in range(1, 11))
    selected = select_brief_chunks(chunks, s, config=BriefSelectionConfig(
        target_chunk_count=3, coverage_ratio=1.0,
    ))
    assert len(selected) <= 3


def test_select_preserves_document_order():
    s = _struct_with_sections(2)
    chunks = tuple(_chunk(f"{i:03d}", "n_0001" if i <= 5 else "n_0002") for i in range(1, 11))
    selected = select_brief_chunks(chunks, s, config=BriefSelectionConfig(target_chunk_count=4))
    ids = [c.chunk_id for c in selected]
    assert ids == sorted(ids)


def test_select_empty():
    s = _struct_with_sections(0)
    assert select_brief_chunks((), s) == []


def test_select_conclusion_section():
    s = _struct_with_sections(3)
    chunks = (
        _chunk("001", "n_0001"),
        _chunk("002", "n_0002"),
        _chunk("003", "n_0003", text="заключительные положения"),
    )
    selected = select_brief_chunks(chunks, s, config=BriefSelectionConfig(
        target_chunk_count=5, coverage_ratio=1.0,
    ))
    selected_ids = [c.chunk_id for c in selected]
    assert "003" in selected_ids


def test_select_coverage_ratio_respected():
    s = _struct_with_sections(1)
    chunks = tuple(_chunk(f"{i:03d}", "n_0001") for i in range(1, 21))
    selected = select_brief_chunks(chunks, s, config=BriefSelectionConfig(
        target_chunk_count=20, coverage_ratio=0.5,
    ))
    assert 5 <= len(selected) <= 20