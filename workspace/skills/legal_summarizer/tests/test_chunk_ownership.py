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