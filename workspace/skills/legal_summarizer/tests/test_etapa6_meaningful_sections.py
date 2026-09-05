"""Acceptance tests для Этапа 6: meaningful sections."""

from __future__ import annotations

import sys
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _make_node(*, node_id, title="", start_block=0, end_block=0, parent_id="n_0000"):
    from workspace.skills.legal_summarizer.scripts.structure.models import StructureNode
    return StructureNode(
        node_id=node_id,
        node_type="section",
        semantic_type=None,
        level=1,
        title=title,
        number=None,
        parent_id=parent_id,
        children=(),
        start_block=start_block,
        end_block=end_block,
        confidence=1.0,
    )


def _wrap(nodes):
    from workspace.skills.legal_summarizer.scripts.structure.models import (
        DocumentStructure,
    )
    root = _make_node(node_id="n_0000", title="", start_block=0, end_block=9, parent_id=None)
    root = root.__class__(
        node_id="n_0000",
        node_type="document",
        semantic_type=None,
        level=0,
        title="",
        number=None,
        parent_id=None,
        children=tuple(n.node_id for n in nodes),
        start_block=0,
        end_block=9,
        confidence=1.0,
    )
    nodes_dict = {"n_0000": root, **{n.node_id: n for n in nodes}}
    return DocumentStructure(
        document_id="t",
        title=None,
        nodes=nodes_dict,
        root_id="n_0000",
        preamble_node_id="n_0000",
        numbering=(),
        total_blocks=10,
        coverage_ratio=1.0,
    )


def test_one_block_section_with_title_counts():
    """Одна секция ``start_block == end_block`` с title → meaningful."""
    from workspace.skills.legal_summarizer.scripts.structure.unified_execution import (
        _count_meaningful_sections,
    )

    s = _wrap([
        _make_node(node_id="n_0001", title="Глава", start_block=0, end_block=0),
    ])
    assert _count_meaningful_sections(s) == 1


def test_three_one_block_sections_above_threshold():
    """3 one-block sections → ``map_hierarchical``."""
    from workspace.skills.legal_summarizer.scripts.structure.unified_execution import (
        ExecutionPolicy,
        _count_meaningful_sections,
        select_strategy,
    )

    s = _wrap([
        _make_node(node_id="n_0001", title="Гл.1", start_block=0, end_block=0),
        _make_node(node_id="n_0002", title="Гл.2", start_block=1, end_block=1),
        _make_node(node_id="n_0003", title="Гл.3", start_block=2, end_block=2),
    ])
    assert _count_meaningful_sections(s) == 3

    policy = ExecutionPolicy(
        direct_threshold_tokens=0,
        hierarchical_section_threshold=3,
    )
    # Каждый chunk ~10к символов → > direct_threshold (0).
    class _FakeChunk:
        text = "x" * 35_000
    chunks = tuple(_FakeChunk() for _ in range(10))
    strategy = select_strategy(s, chunks, policy=policy)
    assert strategy == "map_hierarchical", (
        f"expected map_hierarchical, got {strategy}"
    )


def test_title_less_section_excluded():
    """Section без title → не считается meaningful."""
    from workspace.skills.legal_summarizer.scripts.structure.unified_execution import (
        _count_meaningful_sections,
    )

    s = _wrap([
        _make_node(node_id="n_0001", title="", start_block=0, end_block=5),
    ])
    assert _count_meaningful_sections(s) == 0


def test_invalid_range_excluded():
    """Section с ``start_block > end_block`` → не считается."""
    from workspace.skills.legal_summarizer.scripts.structure.unified_execution import (
        _count_meaningful_sections,
    )

    s = _wrap([
        _make_node(node_id="n_0001", title="X", start_block=5, end_block=3),
    ])
    assert _count_meaningful_sections(s) == 0