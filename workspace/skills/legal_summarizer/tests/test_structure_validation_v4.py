"""Тесты для §4 валидации (PLAN §4).

Разделение overlap-проверок:

* parent-child overlap — **разрешён** (parent range содержит child range);
* sibling overlap — **запрещён**;
* cross-branch overlap — **запрещён**.

Дополнительные проверки:
* no cycles в parent-chain;
* no duplicate child (один node в children нескольких parents);
* root covers full document;
* ranges inside document;
* no orphan parent.
"""

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


def _root(children: tuple[str, ...] = (), end_block: int = 9) -> StructureNode:
    return StructureNode(
        node_id="n_0000", node_type="document", semantic_type=None,
        level=0, title="", number=None, parent_id=None,
        children=children, start_block=0, end_block=end_block,
        confidence=1.0,
    )


def _sec(nid: str, *, start: int, end: int,
          parent_id: str = "n_0000", level: int = 1,
          children: tuple[str, ...] = ()) -> StructureNode:
    return StructureNode(
        node_id=nid, node_type="section", semantic_type=None,
        level=level, title="", number=None, parent_id=parent_id,
        children=children, start_block=start, end_block=end,
        confidence=0.7,
    )


def _doc(total_blocks: int = 10) -> PhysicalDocument:
    return PhysicalDocument(
        path="/tmp/x", format="pdf", title=None, size_bytes=0,
        blocks=tuple(_b(i) for i in range(total_blocks)),
        page_count=total_blocks,
    )


def test_parent_child_overlap_is_valid():
    """Parent range покрывает child range — валидно (§4)."""
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={
            "n_0000": _root(("n_0001",), end_block=9),
            "n_0001": _sec("n_0001", start=0, end=9, children=("n_0002",)),
            "n_0002": _sec("n_0002", start=3, end=7, parent_id="n_0001", level=2),
        },
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=10,
    )
    r = validate_structure(s, _doc(10))
    kinds = {i.kind for i in r.issues}
    assert "sibling_overlap" not in kinds
    assert "cross_branch_overlap" not in kinds
    assert r.is_valid, f"unexpected issues: {r.issues}"


def test_sibling_overlap_is_invalid():
    """Siblings под одним parent с перекрывающимися ranges — невалидно."""
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={
            "n_0000": _root(("n_0001", "n_0002"), end_block=9),
            "n_0001": _sec("n_0001", start=0, end=5),
            "n_0002": _sec("n_0002", start=4, end=8),
        },
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=10,
    )
    r = validate_structure(s, _doc(10))
    kinds = {i.kind for i in r.issues}
    assert "sibling_overlap" in kinds


def test_cross_branch_overlap_is_invalid():
    """Sections из разных ветвей с перекрывающимися ranges — невалидно."""
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={
            "n_0000": _root(("n_0001", "n_0003"), end_block=9),
            "n_0001": _sec("n_0001", start=0, end=5, children=("n_0002",)),
            "n_0002": _sec("n_0002", start=1, end=3, parent_id="n_0001", level=2),
            "n_0003": _sec("n_0003", start=3, end=7),
        },
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=10,
    )
    r = validate_structure(s, _doc(10))
    kinds = {i.kind for i in r.issues}
    assert "cross_branch_overlap" in kinds


def test_no_cycle_detected():
    """A.parent_id=B, B.parent_id=A → cycle issue."""
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={
            "n_0000": _root(("n_0001", "n_0002"), end_block=9),
            "n_0001": _sec("n_0001", start=0, end=4, parent_id="n_0002"),
            "n_0002": _sec("n_0002", start=5, end=9, parent_id="n_0001"),
        },
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=10,
    )
    r = validate_structure(s, _doc(10))
    kinds = {i.kind for i in r.issues}
    assert "cycle" in kinds


def test_duplicate_child_detected():
    """Один node в children двух parents → duplicate_child issue."""
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={
            "n_0000": _root(("n_0001", "n_0002", "n_0003"), end_block=9),
            "n_0001": _sec("n_0001", start=0, end=4, children=("n_0003",)),
            "n_0002": _sec("n_0002", start=5, end=9, children=("n_0003",)),
            "n_0003": _sec("n_0003", start=2, end=3, parent_id="n_0001"),
        },
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=10,
    )
    r = validate_structure(s, _doc(10))
    kinds = {i.kind for i in r.issues}
    assert "duplicate_child" in kinds


def test_root_must_cover_full_document():
    """Root.end_block != total_blocks-1 → issue."""
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={
            "n_0000": _root(end_block=5),
        },
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=10,
    )
    r = validate_structure(s, _doc(10))
    kinds = {i.kind for i in r.issues}
    assert "root_does_not_cover_document" in kinds


def test_root_must_start_at_zero():
    """Root.start_block != 0 → issue."""
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={
            "n_0000": StructureNode(
                node_id="n_0000", node_type="document", semantic_type=None,
                level=0, title="", number=None, parent_id=None,
                children=(), start_block=3, end_block=9, confidence=1.0,
            ),
        },
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=10,
    )
    r = validate_structure(s, _doc(10))
    kinds = {i.kind for i in r.issues}
    assert "root_not_at_start" in kinds


def test_range_out_of_bounds_detected():
    """end_block >= total_blocks → range_out_of_bounds issue."""
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={
            "n_0000": _root(("n_0001",), end_block=9),
            "n_0001": _sec("n_0001", start=0, end=15),
        },
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=10,
    )
    r = validate_structure(s, _doc(10))
    kinds = {i.kind for i in r.issues}
    assert "range_out_of_bounds" in kinds


def test_nested_hierarchy_no_false_positive_overlap():
    """Полная nested иерархия (root → A → A.1, A.2 → B → B.1) валидна."""
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={
            "n_0000": _root(("n_0001", "n_0003"), end_block=9),
            "n_0001": _sec("n_0001", start=0, end=4, children=("n_0002",)),
            "n_0002": _sec("n_0002", start=1, end=3, parent_id="n_0001", level=2),
            "n_0003": _sec("n_0003", start=5, end=9, children=("n_0004",)),
            "n_0004": _sec("n_0004", start=6, end=8, parent_id="n_0003", level=2),
        },
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=10,
    )
    r = validate_structure(s, _doc(10))
    overlap_kinds = {
        "sibling_overlap", "cross_branch_overlap",
        "cycle", "duplicate_child",
    }
    bad = [i for i in r.issues if i.kind in overlap_kinds]
    assert bad == [], f"unexpected overlap/cycle/duplicate issues: {bad}"