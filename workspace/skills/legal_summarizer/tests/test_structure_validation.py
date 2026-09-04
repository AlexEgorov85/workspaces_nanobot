"""Тесты для structure validation (Этап 16 из PLAN.md)."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentStructure,
    StructureNode,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock,
    PhysicalDocument,
)
from workspace.skills.legal_summarizer.scripts.structure.validation import (
    validate_structure,
)


def _b(ordinal: int) -> DocumentBlock:
    return DocumentBlock(
        block_id=f"b_{ordinal:04d}", block_type="page", content="x",
        char_count=1, page_index=ordinal + 1, page_start=ordinal + 1,
        page_end=ordinal + 1, paragraph_index=None, table_index=None,
        ordinal=ordinal, block_metadata={},
    )


def _root(children: tuple[str, ...] = (), end_block: int = 10) -> StructureNode:
    return StructureNode(
        node_id="n_0000", node_type="document", semantic_type=None,
        level=0, title="", number=None, parent_id=None,
        children=children, start_block=0, end_block=end_block,
        confidence=1.0,
    )


def _sec(nid: str, *, start: int, end: int,
         parent_id: str = "n_0000") -> StructureNode:
    return StructureNode(
        node_id=nid, node_type="section", semantic_type=None,
        level=1, title="", number=None, parent_id=parent_id,
        children=(), start_block=start, end_block=end,
        confidence=0.7,
    )


def test_validate_healthy():
    doc = PhysicalDocument(
        path="/tmp/x", format="pdf", title=None, size_bytes=0,
        blocks=tuple(_b(i) for i in range(5)),
        page_count=5,
    )
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={
            "n_0000": _root(("n_0001",), end_block=4),
            "n_0001": _sec("n_0001", start=0, end=4),
        },
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=5,
    )
    r = validate_structure(s, doc)
    assert r.is_valid
    assert r.coverage_ratio == 1.0


def test_validate_invalid_range():
    doc = PhysicalDocument(
        path="/tmp/x", format="pdf", title=None, size_bytes=0,
        blocks=tuple(_b(i) for i in range(5)), page_count=5,
    )
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={
            "n_0000": _root(("n_0001",), end_block=4),
            "n_0001": _sec("n_0001", start=5, end=2),
        },
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=5,
    )
    r = validate_structure(s, doc)
    assert not r.is_valid
    kinds = {i.kind for i in r.issues}
    assert "invalid_range" in kinds


def test_validate_orphan_parent():
    doc = PhysicalDocument(
        path="/tmp/x", format="pdf", title=None, size_bytes=0,
        blocks=tuple(_b(i) for i in range(5)), page_count=5,
    )
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={
            "n_0000": _root(("n_0001",), end_block=4),
            "n_0001": _sec("n_0001", start=0, end=4, parent_id="n_missing"),
        },
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=5,
    )
    r = validate_structure(s, doc)
    kinds = {i.kind for i in r.issues}
    assert "orphan" in kinds


def test_validate_section_overlap():
    doc = PhysicalDocument(
        path="/tmp/x", format="pdf", title=None, size_bytes=0,
        blocks=tuple(_b(i) for i in range(5)), page_count=5,
    )
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={
            "n_0000": _root(("n_0001", "n_0002"), end_block=4),
            "n_0001": _sec("n_0001", start=0, end=4),
            "n_0002": _sec("n_0002", start=2, end=4),
        },
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=5,
    )
    r = validate_structure(s, doc)
    kinds = {i.kind for i in r.issues}
    assert "sibling_overlap" in kinds


def test_validate_low_coverage():
    doc = PhysicalDocument(
        path="/tmp/x", format="pdf", title=None, size_bytes=0,
        blocks=tuple(_b(i) for i in range(10)), page_count=10,
    )
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={
            "n_0000": _root(("n_0001",)),
            "n_0001": _sec("n_0001", start=0, end=1),
        },
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=10,
    )
    r = validate_structure(s, doc)
    kinds = {i.kind for i in r.issues}
    assert "low_coverage" in kinds
    assert r.coverage_ratio < 0.5


def test_validate_total_blocks_mismatch():
    doc = PhysicalDocument(
        path="/tmp/x", format="pdf", title=None, size_bytes=0,
        blocks=tuple(_b(i) for i in range(5)), page_count=5,
    )
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={"n_0000": _root()},
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=99,
    )
    r = validate_structure(s, doc)
    kinds = {i.kind for i in r.issues}
    assert "total_blocks_mismatch" in kinds