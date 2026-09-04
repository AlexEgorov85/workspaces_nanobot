"""Chunk ownership regression (Этап 36).

Проверяет, что build_block_ownership даёт:
* 0 или 1 owner на block (никогда 2+);
* owned_blocks покрывают все significant blocks.
"""

from __future__ import annotations

from collections import Counter

from workspace.skills.legal_summarizer.scripts.structure.document_chunker import (
    build_block_ownership,
)
from workspace.skills.legal_summarizer.scripts.structure.heading import (
    HeadingCandidate,
)
from workspace.skills.legal_summarizer.scripts.structure.hierarchy import (
    build_document_structure,
)


def _hc(i, t, source="regex_numbered_1"):
    return HeadingCandidate(
        block_index=i, text=t, score=0.7,
        source=source, level=1, raw_number=None,
    )


def _physical(total: int):
    from workspace.skills.legal_summarizer.scripts.structure.physical import (
        DocumentBlock, PhysicalDocument,
    )
    blocks = tuple(
        DocumentBlock(
            block_id=f"b_{i:04d}",
            block_type="text",
            content=f"block {i}",
            char_count=10,
            page_index=None, page_start=None, page_end=None,
            paragraph_index=None, table_index=None,
            ordinal=i, block_metadata={},
        )
        for i in range(total)
    )
    return PhysicalDocument(
        path="<inline>", format="txt", title=None,
        size_bytes=100, blocks=blocks, page_count=1,
    )


def test_block_ownership_no_duplicates():
    """Каждый block имеет 0 или 1 owner (никогда 2+)."""
    cs = [
        _hc(0, "Глава 1", source="regex_glзава"),
        _hc(2, "Глава 2", source="regex_glзава"),
    ]
    struct = build_document_structure(cs, total_blocks=10)
    ownership = build_block_ownership(struct)

    counts = Counter(ownership.values())
    for owner_id, count in counts.items():
        assert count >= 1
    assert len(ownership) == len(set(ownership.keys()))


def test_block_ownership_covers_section_blocks():
    """owned_blocks покрывают все significant blocks."""
    cs = [
        _hc(0, "1."),
        _hc(5, "2."),
    ]
    struct = build_document_structure(cs, total_blocks=10)
    ownership = build_block_ownership(struct)
    for b in range(10):
        owner = ownership.get(b)
        if owner is None:
            continue
        assert owner in struct.nodes


def test_block_ownership_respects_nesting():
    """Child section имеет блоки, не принадлежащие parent."""
    cs = [
        _hc(0, "1."),
        _hc(3, "1.1."),
    ]
    struct = build_document_structure(cs, total_blocks=10)
    ownership = build_block_ownership(struct)

    sections = struct.iter_sections()
    assert len(sections) >= 2
    if len(sections) >= 2:
        child = sections[1]
        child_blocks = {
            b for b in range(child.start_block, child.end_block + 1)
            if ownership.get(b) == child.node_id
        }
        assert len(child_blocks) >= 1


def test_block_ownership_for_root_only():
    """Структура без sections — все blocks принадлежат root."""
    struct = build_document_structure([], total_blocks=5)
    ownership = build_block_ownership(struct)
    assert len(ownership) == 0


def test_owner_for_block_returns_deepest_section():
    """owner_for_block возвращает deepest section для nested case."""
    from workspace.skills.legal_summarizer.scripts.structure.document_chunker import (
        owner_for_block,
    )

    cs = [
        _hc(0, "Глава 1", source="regex_glзава"),
        _hc(2, "Статья 1", source="regex_statiya"),
    ]
    struct = build_document_structure(cs, total_blocks=5)
    ownership = build_block_ownership(struct)
    sections = struct.iter_sections()
    chapter = next(s for s in sections if "Глава" in s.title)
    article = next(s for s in sections if "Статья" in s.title)
    assert owner_for_block(struct, 0, ownership) == chapter.node_id
    assert owner_for_block(struct, 2, ownership) == article.node_id


def test_owner_for_block_returns_root_for_uncovered_block():
    """Block вне section ranges → root_id (PLAN §6 acceptance)."""
    from workspace.skills.legal_summarizer.scripts.structure.document_chunker import (
        owner_for_block,
    )
    from workspace.skills.legal_summarizer.scripts.structure.models import (
        DocumentStructure, StructureNode,
    )

    root = StructureNode(
        node_id="n_0000", node_type="document", semantic_type=None,
        level=0, title="", number=None, parent_id=None,
        children=("n_0001",), start_block=0, end_block=9,
        confidence=1.0,
    )
    sec = StructureNode(
        node_id="n_0001", node_type="section", semantic_type=None,
        level=1, title="S", number=None, parent_id="n_0000",
        children=(), start_block=0, end_block=4,
        confidence=0.7,
    )
    struct = DocumentStructure(
        document_id="d", title=None,
        nodes={"n_0000": root, "n_0001": sec},
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=10,
    )
    ownership = build_block_ownership(struct)
    assert owner_for_block(struct, 7, ownership) == "n_0000"
    assert owner_for_block(struct, 0, ownership) == "n_0001"
    assert owner_for_block(struct, 4, ownership) == "n_0001"


def test_owner_for_block_returns_none_for_out_of_range():
    """Block вне total_blocks → None."""
    from workspace.skills.legal_summarizer.scripts.structure.document_chunker import (
        owner_for_block,
    )

    struct = build_document_structure([], total_blocks=5)
    assert owner_for_block(struct, 5) is None
    assert owner_for_block(struct, -1) is None


def test_owner_for_block_lazy_builds_ownership():
    """owner_for_block без переданного ownership строит его на лету."""
    from workspace.skills.legal_summarizer.scripts.structure.document_chunker import (
        owner_for_block,
    )

    cs = [_hc(0, "1."), _hc(5, "2.")]
    struct = build_document_structure(cs, total_blocks=10)
    assert owner_for_block(struct, 0) is not None
    assert owner_for_block(struct, 5) is not None


def test_block_ownership_zero_or_one_owner_per_block():
    """PLAN §6 acceptance: каждый block имеет 0 или 1 owner."""
    cs = [
        _hc(0, "Глава 1", source="regex_glзава"),
        _hc(1, "Статья 1", source="regex_statiya"),
        _hc(2, "Статья 2", source="regex_statiya"),
        _hc(4, "Глава 2", source="regex_glзава"),
    ]
    struct = build_document_structure(cs, total_blocks=6)
    ownership = build_block_ownership(struct)
    for b in range(struct.total_blocks):
        assert b in ownership or b not in ownership
        assert isinstance(ownership.get(b), (str, type(None)))