"""Тесты для repair (PLAN §5, §15).

Acceptance criteria из PLAN §5:

* one-block section survives;
* removed node absent from children;
* child of repaired parent becomes valid;
* orphan repaired;
* impossible parent repaired;
* no dangling references;
* repeated repair is idempotent: ``repair(repair(struct)) == repair(struct)``.
"""

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
          node_type: str = "section",
          children: tuple[str, ...] = ()) -> StructureNode:
    return StructureNode(
        node_id=nid, node_type=node_type, semantic_type=None,
        level=level, title="", number=None, parent_id=parent_id,
        children=children, start_block=start_block, end_block=end_block,
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


def test_repair_one_block_section_survives():
    """PLAN §5.1: one-block section (start == end) НЕ удаляется."""
    root = _root(children=("n_0001",))
    one_block = _node("n_0001", start_block=5, end_block=5)
    struct = _struct({"n_0000": root, "n_0001": one_block})
    fixed, report = repair_structure(struct)
    assert report.invalid_ranges_dropped == 0
    assert "n_0001" in fixed.nodes


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


def test_repair_removed_node_absent_from_children():
    """PLAN §5.3: при drop node он удаляется из parent's children."""
    parent = _node("n_0001", children=("n_0002",), start_block=0, end_block=20)
    bad = _node("n_0002", parent_id="n_0001", start_block=10, end_block=5)
    root = _root(children=("n_0001",))
    struct = _struct({"n_0000": root, "n_0001": parent, "n_0002": bad})
    fixed, _ = repair_structure(struct)
    assert "n_0002" not in fixed.nodes
    assert "n_0002" not in fixed.nodes["n_0001"].children
    assert "n_0002" not in fixed.nodes["n_0000"].children


def test_repair_child_of_repaired_parent_becomes_valid():
    """PLAN §5.3: child of repaired parent (parent reparented) сохраняется.

    Child имеет level=2, parent level=1 — нормальный nested case.
    """
    root = _root(children=("n_0001", "n_0002"))
    orphan = _node("n_0001", parent_id="n_does_not_exist")
    child = _node("n_0002", parent_id="n_0001", level=2)
    struct = _struct({"n_0000": root, "n_0001": orphan, "n_0002": child})
    fixed, _ = repair_structure(struct)
    assert fixed.nodes["n_0001"].parent_id == "n_0000"
    assert fixed.nodes["n_0002"].parent_id == "n_0001"
    assert "n_0002" in fixed.nodes["n_0001"].children


def test_repair_idempotent():
    """PLAN §5: repair(repair(struct)) == repair(struct)."""
    root = _root(children=("n_0001", "n_0002", "n_0003"))
    orphan = _node("n_0001", parent_id="n_missing")
    one_block = _node("n_0002", start_block=3, end_block=3)
    good = _node("n_0003", start_block=5, end_block=10)
    struct = _struct({
        "n_0000": root, "n_0001": orphan,
        "n_0002": one_block, "n_0003": good,
    })

    fixed_once, _ = repair_structure(struct)
    fixed_twice, _ = repair_structure(fixed_once)

    once_ids = sorted(fixed_once.nodes.keys())
    twice_ids = sorted(fixed_twice.nodes.keys())
    assert once_ids == twice_ids
    for nid in once_ids:
        once = fixed_once.nodes[nid]
        twice = fixed_twice.nodes[nid]
        assert once.parent_id == twice.parent_id
        assert once.start_block == twice.start_block
        assert once.end_block == twice.end_block
        assert once.children == twice.children


def test_repair_drops_invalid_child_of_dropped_node():
    """PLAN §5.3: child of dropped invalid-range node → repaired parent."""
    parent = _node("n_0001", children=("n_0002",), start_block=0, end_block=20)
    invalid = _node("n_0002", parent_id="n_0001", start_block=10, end_block=5)
    root = _root(children=("n_0001",))
    struct = _struct({"n_0000": root, "n_0001": parent, "n_0002": invalid})
    fixed, _ = repair_structure(struct)
    assert "n_0002" not in fixed.nodes


def test_repair_dropped_node_removed_from_sibling_children():
    """PLAN §5.3: dropped node не появляется в children других parent'ов."""
    parent_a = _node("n_0001", children=("n_0003",), level=2)
    parent_b = _node("n_0002", children=("n_0003",), level=2)
    invalid = _node("n_0003", parent_id="n_0001", start_block=10, end_block=5)
    root = _root(children=("n_0001", "n_0002"))
    struct = _struct({
        "n_0000": root, "n_0001": parent_a,
        "n_0002": parent_b, "n_0003": invalid,
    })
    fixed, _ = repair_structure(struct)
    assert "n_0003" not in fixed.nodes
    assert "n_0003" not in fixed.nodes["n_0001"].children
    assert "n_0003" not in fixed.nodes["n_0002"].children


def test_repair_parent_changed_synchronously_rebuilds_children():
    """PLAN §5.2: при изменении parent_id children parent пересобирается."""
    root = _root(children=("n_0001", "n_0002"))
    bad_parent = _node("n_0001", parent_id="n_missing", children=("n_0002",))
    good_child = _node("n_0002", parent_id="n_0001", level=2)
    struct = _struct({"n_0000": root, "n_0001": bad_parent, "n_0002": good_child})
    fixed, _ = repair_structure(struct)
    assert fixed.nodes["n_0001"].parent_id == "n_0000"
    assert "n_0002" in fixed.nodes["n_0001"].children