"""Invariant-тесты для DocumentStructure hierarchy (PLAN §3).

Формальные инварианты дерева:

1. Для каждого section node:
   - parent_id существует,
   - parent.level < node.level (только у root parent_id=None, level=0),
   - node достижим из root через children,
   - node встречается ровно в одном parent's children.

2. Siblings упорядочены в document order (по start_block).

3. Ranges:
   - start_block <= end_block,
   - 0 <= start_block < total_blocks,
   - 0 <= end_block < total_blocks,
   - end_block == next_candidate.block_index - 1 (или total_blocks - 1
     для последнего кандидата) — секция покрывает диапазон от своего
     heading до блока перед следующим кандидатом; это совместимо с
     block_ownership на уровне ChunkPlanner (§6).

4. Детерминизм: повторный запуск build_document_structure
   даёт byte-for-byte одинаковый to_dict().
"""

from __future__ import annotations

import json

from workspace.skills.legal_summarizer.scripts.structure.heading import (
    HeadingCandidate,
)
from workspace.skills.legal_summarizer.scripts.structure.hierarchy import (
    StructureTreeBuilderConfig,
    build_document_structure,
)
from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentStructure,
    StructureNode,
)


def _hc(block_index: int, text: str, source: str = "regex_numbered_1",
        level: int = 1, score: float = 0.7, raw_number: str | None = None):
    return HeadingCandidate(
        block_index=block_index, text=text, score=score, source=source,
        level=level, raw_number=raw_number,
    )


def _nested_three_level_structure() -> DocumentStructure:
    """root / A / A.1, A.2 / B — явный nested case из PLAN §3.

    Blocks:
      0  → A        (level 1)
      1  → A.1      (level 2)
      2  → A.2      (level 2)
      3  → B        (level 1)
    """
    cs = [
        _hc(0, "A", source="regex_glзава", level=1, score=0.85, raw_number="A"),
        _hc(1, "A.1", source="regex_statiya", level=2, score=0.85, raw_number="A.1"),
        _hc(2, "A.2", source="regex_statiya", level=2, score=0.85, raw_number="A.2"),
        _hc(3, "B", source="regex_glзава", level=1, score=0.85, raw_number="B"),
    ]
    return build_document_structure(cs, total_blocks=4)


def test_invariant_root_has_no_parent():
    s = _nested_three_level_structure()
    root = s.nodes[s.root_id]
    assert root.parent_id is None
    assert root.level == 0


def test_invariant_every_non_root_has_existing_parent():
    s = _nested_three_level_structure()
    for nid, node in s.nodes.items():
        if nid == s.root_id:
            continue
        assert node.parent_id is not None, f"{nid} has no parent"
        assert node.parent_id in s.nodes, f"{nid} parent {node.parent_id} not in nodes"


def test_invariant_parent_level_less_than_node_level():
    s = _nested_three_level_structure()
    for nid, node in s.nodes.items():
        if nid == s.root_id:
            continue
        parent = s.nodes[node.parent_id]
        assert parent.level < node.level, (
            f"parent.level={parent.level} not < node.level={node.level} "
            f"for {nid}"
        )


def test_invariant_node_reachable_from_root():
    """Каждый узел достижим из root по цепочке children."""
    s = _nested_three_level_structure()

    def _reachable(start: str, target: str, seen: set[str]) -> bool:
        if start == target:
            return True
        if start in seen:
            return False
        seen = seen | {start}
        node = s.nodes[start]
        for cid in node.children:
            if _reachable(cid, target, seen):
                return True
        return False

    for nid in s.nodes:
        assert _reachable(s.root_id, nid, set()), f"{nid} not reachable from root"


def test_invariant_node_in_exactly_one_parents_children():
    s = _nested_three_level_structure()
    child_owners: dict[str, list[str]] = {}
    for nid, node in s.nodes.items():
        if node.parent_id is not None:
            child_owners.setdefault(nid, []).append(node.parent_id)
    for nid, parents in child_owners.items():
        assert len(parents) == 1, f"{nid} has {len(parents)} parents: {parents}"


def test_invariant_siblings_ordered_by_start_block():
    s = _nested_three_level_structure()
    for node in s.nodes.values():
        child_starts = [s.nodes[cid].start_block for cid in node.children]
        assert child_starts == sorted(child_starts), (
            f"siblings of {node.node_id} not sorted: {child_starts}"
        )


def test_invariant_ranges_inside_document():
    s = _nested_three_level_structure()
    for nid, node in s.nodes.items():
        assert 0 <= node.start_block < s.total_blocks, (
            f"{nid} start_block={node.start_block} out of bounds"
        )
        assert 0 <= node.end_block < s.total_blocks, (
            f"{nid} end_block={node.end_block} out of bounds"
        )
        assert node.start_block <= node.end_block, (
            f"{nid} start={node.start_block} > end={node.end_block}"
        )


def test_invariant_end_block_equals_next_candidate_minus_one():
    """end_block секции = block_index следующего кандидата - 1.

    Семантика: каждая секция покрывает диапазон от своего heading
    до блока перед следующим кандидатом (или до конца документа для
    последнего кандидата). Это совместимо с block_ownership на уровне
    ChunkPlanner.
    """
    cs = [
        _hc(0, "A", source="regex_glзава", level=1, score=0.85, raw_number="A"),
        _hc(1, "A.1", source="regex_statiya", level=2, score=0.85, raw_number="A.1"),
        _hc(2, "A.2", source="regex_statiya", level=2, score=0.85, raw_number="A.2"),
        _hc(3, "B", source="regex_glзава", level=1, score=0.85, raw_number="B"),
    ]
    s = build_document_structure(cs, total_blocks=4)
    section_nodes = s.iter_sections()
    expected_ends = {
        "A": 0,
        "A.1": 1,
        "A.2": 2,
        "B": 3,
    }
    for sec in section_nodes:
        title = sec.title
        assert title in expected_ends, f"unexpected section {title}"
        assert sec.end_block == expected_ends[title], (
            f"{title}: end_block={sec.end_block}, expected={expected_ends[title]}"
        )


def test_invariant_last_candidate_end_is_total_blocks_minus_one():
    """end_block последнего кандидата = total_blocks - 1."""
    cs = [
        _hc(0, "1.", score=0.7, raw_number="1"),
        _hc(5, "2.", score=0.7, raw_number="2"),
    ]
    s = build_document_structure(cs, total_blocks=8)
    section_nodes = s.iter_sections()
    last = section_nodes[-1]
    assert last.title == "2."
    assert last.end_block == 7


def test_invariant_siblings_start_blocks_monotonic():
    """Стартовые блоки siblings строго возрастают."""
    s = _nested_three_level_structure()
    for node in s.nodes.values():
        child_starts = [s.nodes[cid].start_block for cid in node.children]
        for a, b in zip(child_starts, child_starts[1:]):
            assert a < b, f"siblings not monotonic: {child_starts}"


def test_invariant_determinism_byte_for_byte():
    """Повторный build_document_structure → byte-for-byte тот же to_dict()."""
    cs = [
        _hc(0, "A", source="regex_glзава", level=1, score=0.85, raw_number="A"),
        _hc(1, "A.1", source="regex_statiya", level=2, score=0.85, raw_number="A.1"),
        _hc(2, "A.2", source="regex_statiya", level=2, score=0.85, raw_number="A.2"),
        _hc(3, "B", source="regex_glзава", level=1, score=0.85, raw_number="B"),
        _hc(4, "B.1", source="regex_statiya", level=2, score=0.85, raw_number="B.1"),
    ]
    s1 = build_document_structure(cs, total_blocks=8)
    s2 = build_document_structure(cs, total_blocks=8)
    d1 = s1.to_dict()
    d2 = s2.to_dict()
    j1 = json.dumps(d1, sort_keys=True, ensure_ascii=False)
    j2 = json.dumps(d2, sort_keys=True, ensure_ascii=False)
    assert j1 == j2, "build_document_structure not deterministic"


def test_invariant_determinism_repeated_runs_three():
    """3 прогона → идентичный JSON."""
    cs = [
        _hc(0, "Глава 1", source="regex_glзава", level=1, score=0.85),
        _hc(5, "Статья 1", source="regex_statiya", level=2, score=0.85),
        _hc(10, "Статья 2", source="regex_statiya", level=2, score=0.85),
        _hc(15, "Глава 2", source="regex_glзава", level=1, score=0.85),
    ]
    cfg = StructureTreeBuilderConfig(document_id="d")
    runs = [
        build_document_structure(cs, total_blocks=20, config=cfg).to_dict()
        for _ in range(3)
    ]
    j = json.dumps(runs[0], sort_keys=True, ensure_ascii=False)
    for r in runs[1:]:
        assert json.dumps(r, sort_keys=True, ensure_ascii=False) == j


def test_invariant_three_level_expected_shape():
    """Проверить что nested hierarchy имеет правильный parent/children.

    root
    ├── A (children: A.1, A.2)
    └── B
    """
    s = _nested_three_level_structure()
    root = s.nodes[s.root_id]
    assert len(root.children) == 2, f"root has {len(root.children)} children"
    a_id, b_id = root.children
    a = s.nodes[a_id]
    b = s.nodes[b_id]
    assert set(a.children) == set(
        cid for cid in s.nodes
        if cid != s.root_id and s.nodes[cid].parent_id == a_id
    )
    assert len(a.children) == 2, f"A has {len(a.children)} children"


def test_invariant_empty_structure_has_only_root():
    s = build_document_structure([], total_blocks=5)
    assert len(s.nodes) == 1
    assert s.root_id in s.nodes
    root = s.nodes[s.root_id]
    assert root.start_block == 0
    assert root.end_block == 4
    assert root.children == ()


def test_invariant_root_covers_full_document():
    s = _nested_three_level_structure()
    root = s.nodes[s.root_id]
    assert root.start_block == 0
    assert root.end_block == s.total_blocks - 1