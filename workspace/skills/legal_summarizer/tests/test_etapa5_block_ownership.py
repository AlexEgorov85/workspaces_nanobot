"""Acceptance tests для Этапа 5: один механизм block ownership."""

from __future__ import annotations

import sys
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _make_structure_with_nested():
    """Структура: root, chapter 0..4, два articles (0..1, 2..2), article2 (3..3)."""
    from workspace.skills.legal_summarizer.scripts.structure.models import (
        DocumentStructure,
        NumberingInfo,
        StructureNode,
    )

    root = StructureNode(
        node_id="n_0000",
        node_type="document",
        semantic_type=None,
        level=0,
        title="",
        number=None,
        parent_id=None,
        children=("n_0001",),
        start_block=0,
        end_block=9,
        confidence=1.0,
    )
    chapter = StructureNode(
        node_id="n_0001",
        node_type="section",
        semantic_type="chapter",
        level=1,
        title="Глава 1",
        number=NumberingInfo(
            raw="1", scheme="decimal", components=(1,), level=1,
        ),
        parent_id="n_0000",
        children=("n_0002", "n_0003"),
        start_block=0,
        end_block=4,
        confidence=0.95,
    )
    article1 = StructureNode(
        node_id="n_0002",
        node_type="section",
        semantic_type="article",
        level=2,
        title="Статья 1",
        number=NumberingInfo(
            raw="1", scheme="legal_article", components=(1,), level=1,
        ),
        parent_id="n_0001",
        children=(),
        start_block=0,
        end_block=1,
        confidence=0.95,
    )
    article2 = StructureNode(
        node_id="n_0003",
        node_type="section",
        semantic_type="article",
        level=2,
        title="Статья 2",
        number=NumberingInfo(
            raw="2", scheme="legal_article", components=(2,), level=1,
        ),
        parent_id="n_0001",
        children=(),
        start_block=2,
        end_block=2,
        confidence=0.95,
    )
    nodes = {
        "n_0000": root,
        "n_0001": chapter,
        "n_0002": article1,
        "n_0003": article2,
    }
    return DocumentStructure(
        document_id="test",
        title=None,
        nodes=nodes,
        root_id="n_0000",
        preamble_node_id="n_0000",
        numbering=(),
        total_blocks=10,
        coverage_ratio=1.0,
    )


def test_block_to_node_delegates_to_canonical():
    """``DocumentStructure.block_to_node`` даёт тот же результат,
    что ``block_ownership.block_to_node``."""
    struct = _make_structure_with_nested()

    from workspace.skills.legal_summarizer.scripts.structure.block_ownership import (
        block_to_node as canonical_b2n,
    )

    via_struct = struct.block_to_node()
    via_canonical = canonical_b2n(struct)
    assert via_struct == via_canonical


def test_block_to_node_returns_root_for_uncovered():
    """Blocks вне section ranges → root_id."""
    struct = _make_structure_with_nested()

    from workspace.skills.legal_summarizer.scripts.structure.block_ownership import (
        block_to_node,
    )

    mapping = block_to_node(struct)
    # Block 5..9 — вне article/chapter ranges.
    for b in (5, 6, 7, 8, 9):
        assert mapping[b] == struct.root_id


def test_block_to_node_assigns_deepest_section():
    """Block в диапазоне article → article (deepest)."""
    struct = _make_structure_with_nested()

    from workspace.skills.legal_summarizer.scripts.structure.block_ownership import (
        block_to_node,
    )

    mapping = block_to_node(struct)
    assert mapping[0] == "n_0002"  # article 1
    assert mapping[1] == "n_0002"  # article 1
    assert mapping[2] == "n_0003"  # article 2


def test_only_one_owner_per_block():
    """``build_block_ownership`` даёт ровно одного owner на block."""
    struct = _make_structure_with_nested()

    from workspace.skills.legal_summarizer.scripts.structure.block_ownership import (
        build_block_ownership,
    )

    ownership = build_block_ownership(struct)
    # Каждый ordinal → ровно один owner.
    for ordinal in range(struct.total_blocks):
        if ordinal in ownership:
            assert isinstance(ownership[ordinal], str)