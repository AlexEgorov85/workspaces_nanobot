"""Тесты для safety merge (Этап 17 из PLAN.md)."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentStructure,
    StructureNode,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock,
)
from workspace.skills.legal_summarizer.scripts.structure.safety_merge import (
    SafetyMergeConfig, safety_merge,
)


def _b(ordinal: int, content: str = "x") -> DocumentBlock:
    return DocumentBlock(
        block_id=f"b_{ordinal:04d}", block_type="paragraph", content=content,
        char_count=len(content), page_index=None, page_start=None,
        page_end=None, paragraph_index=None, table_index=None,
        ordinal=ordinal, block_metadata={},
    )


def _root(children: tuple[str, ...] = ()) -> StructureNode:
    return StructureNode(
        node_id="n_0000", node_type="document", semantic_type=None,
        level=0, title="", number=None, parent_id=None,
        children=children, start_block=0, end_block=100,
        confidence=1.0,
    )


def _sec(nid: str, *, level: int, start: int, end: int) -> StructureNode:
    return StructureNode(
        node_id=nid, node_type="section", semantic_type=None,
        level=level, title="", number=None, parent_id="n_0000",
        children=(), start_block=start, end_block=end,
        confidence=0.7,
    )


def test_safety_merge_collapses_micro():
    blocks = (
        _b(0, "tiny"),
        _b(1, "long " * 200),
    )
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={
            "n_0000": _root(("n_0001", "n_0002")),
            "n_0001": _sec("n_0001", level=1, start=0, end=0),
            "n_0002": _sec("n_0002", level=1, start=1, end=1),
        },
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=2,
    )
    out = safety_merge(s, blocks)
    assert out.nodes["n_0001"].confidence == 0.0
    assert out.nodes["n_0002"].end_block == 1


def test_safety_merge_no_action_when_healthy():
    blocks = (
        _b(0, "long " * 200),
        _b(1, "long " * 200),
    )
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={
            "n_0000": _root(("n_0001", "n_0002")),
            "n_0001": _sec("n_0001", level=1, start=0, end=0),
            "n_0002": _sec("n_0002", level=1, start=1, end=1),
        },
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=2,
    )
    out = safety_merge(s, blocks)
    assert out.nodes["n_0001"].confidence == 0.7


def test_safety_merge_does_not_change_total_blocks():
    blocks = (_b(0, "tiny"), _b(1, "x"))
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={
            "n_0000": _root(("n_0001",)),
            "n_0001": _sec("n_0001", level=1, start=0, end=1),
        },
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=2,
    )
    out = safety_merge(s, blocks)
    assert out.total_blocks == 2


def test_safety_merge_level_3_skipped():
    """Safety merge не трогает level > max_level (по умолчанию 2)."""
    blocks = (_b(0, "tiny"), _b(1, "x"))
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={
            "n_0000": _root(("n_0001", "n_0002")),
            "n_0001": _sec("n_0001", level=3, start=0, end=0),
            "n_0002": _sec("n_0002", level=3, start=1, end=1),
        },
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=2,
    )
    out = safety_merge(s, blocks)
    assert out.nodes["n_0001"].confidence == 0.7
    assert out.nodes["n_0002"].confidence == 0.7


def test_safety_merge_custom_threshold():
    cfg = SafetyMergeConfig(min_section_chars=10)
    blocks = (_b(0, "x" * 100), _b(1, "y" * 100))
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={
            "n_0000": _root(("n_0001",)),
            "n_0001": _sec("n_0001", level=1, start=0, end=1),
        },
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=2,
    )
    out = safety_merge(s, blocks, config=cfg)
    assert out == s