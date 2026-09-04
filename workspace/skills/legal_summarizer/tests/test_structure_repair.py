"""Тесты для repair (Этап 15 из PLAN.md)."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentStructure,
    StructureNode,
)
from workspace.skills.legal_summarizer.scripts.structure.repair import (
    RepairReport,
    repair_structure,
)


def _node(nid: str, *, parent_id: str | None = "n_0000",
          level: int = 1, start_block: int = 0, end_block: int = 5,
          node_type: str = "section") -> StructureNode:
    return StructureNode(
        node_id=nid, node_type=node_type, semantic_type=None,
        level=level, title="", number=None, parent_id=parent_id,
        children=(), start_block=start_block, end_block=end_block,
        confidence=0.7,
    )


def _root(children: tuple[str, ...] = ()) -> StructureNode:
    return StructureNode(
        node_id="n_0000", node_type="document", semantic_type=None,
        level=0, title="", number=None, parent_id=None,
        children=children, start_block=0, end_block=100,
        confidence=1.0,
    )


def _struct(nodes: dict[str, StructureNode]) -> DocumentStructure:
    root = nodes["n_0000"]
    return DocumentStructure(
        document_id="d", title=None, nodes=nodes,
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=100,
    )


def test_repair_orphans_fixed():
    root = _root(children=("n_0001", "n_0002"))
    bad = _node("n_0001", parent_id="n_does_not_exist")
    good = _node("n_0002")
    struct = _struct({"n_0000": root, "n_0001": bad, "n_0002": good})
    fixed, report = repair_structure(struct)
    assert report.orphans_fixed == 1
    assert fixed.nodes["n_0001"].parent_id == "n_0000"


def test_repair_invalid_range_dropped():
    root = _root(children=("n_0001",))
    bad = _node("n_0001", start_block=10, end_block=5)
    struct = _struct({"n_0000": root, "n_0001": bad})
    fixed, report = repair_structure(struct)
    assert report.invalid_ranges_dropped == 1
    assert "n_0001" not in fixed.nodes


def test_repair_empty_node_collapsed():
    root = _root(children=("n_0001",))
    empty = _node("n_0001", start_block=5, end_block=5)
    struct = _struct({"n_0000": root, "n_0001": empty})
    fixed, report = repair_structure(struct)
    assert report.empty_nodes_collapsed == 1
    assert "n_0001" not in fixed.nodes


def test_repair_impossible_parent_fixed():
    """Parent.level >= node.level → parent_id склеивается на root_id."""
    root = StructureNode(
        node_id="n_0000", node_type="document", semantic_type=None,
        level=0, title="", number=None, parent_id=None,
        children=("n_0002", "n_0001"), start_block=0, end_block=100,
        confidence=1.0,
    )
    high_parent = StructureNode(
        node_id="n_0002", node_type="section", semantic_type=None,
        level=5, title="", number=None, parent_id="n_0000",
        children=("n_0001",), start_block=0, end_block=10,
        confidence=0.7,
    )
    child = StructureNode(
        node_id="n_0001", node_type="section", semantic_type=None,
        level=2, title="", number=None, parent_id="n_0002",
        children=(), start_block=2, end_block=5,
        confidence=0.7,
    )
    struct = DocumentStructure(
        document_id="d", title=None,
        nodes={"n_0000": root, "n_0002": high_parent, "n_0001": child},
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=100,
    )
    fixed, report = repair_structure(struct)
    assert report.impossible_parents_fixed == 1
    assert fixed.nodes["n_0001"].parent_id == "n_0000"


def test_repair_no_changes_when_healthy():
    root = _root(children=("n_0001",))
    good = _node("n_0001", start_block=0, end_block=10)
    struct = DocumentStructure(
        document_id="d", title=None,
        nodes={"n_0000": root, "n_0001": good},
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=100,
    )
    fixed, report = repair_structure(struct)
    assert report == RepairReport()


def test_repair_keeps_root():
    root = _root()
    struct = _struct({"n_0000": root})
    fixed, _ = repair_structure(struct)
    assert "n_0000" in fixed.nodes
    assert fixed.nodes["n_0000"].node_id == "n_0000"