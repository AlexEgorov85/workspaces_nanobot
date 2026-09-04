"""Тесты для block lookup (Этап 44 из PLAN.md)."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.block_lookup import (
    build_block_lookup,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock, PhysicalDocument,
)


def _b(ord: int) -> DocumentBlock:
    return DocumentBlock(
        block_id=f"b_{ord:04d}", block_type="paragraph", content="x",
        char_count=1, page_index=1, page_start=1, page_end=1,
        paragraph_index=None, table_index=None, ordinal=ord,
        block_metadata={},
    )


def _doc() -> PhysicalDocument:
    import tempfile
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8",
    ) as f:
        path = f.name
    return PhysicalDocument(
        path=path, format="txt", title=None, size_bytes=0,
        blocks=tuple(_b(i) for i in range(5)), page_count=1,
    )


def test_build_block_lookup_by_ord():
    doc = _doc()
    lookup = build_block_lookup(doc)
    assert lookup.get_by_ord(3).ordinal == 3
    assert lookup.get_by_ord(999) is None


def test_build_block_lookup_by_id():
    doc = _doc()
    lookup = build_block_lookup(doc)
    assert lookup.get_by_id("b_0002").ordinal == 2
    assert lookup.get_by_id("b_9999") is None


def test_build_block_lookup_empty():
    import tempfile
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8",
    ) as f:
        path = f.name
    doc = PhysicalDocument(
        path=path, format="txt", title=None, size_bytes=0,
        blocks=(), page_count=0,
    )
    lookup = build_block_lookup(doc)
    assert lookup.get_by_ord(0) is None


def test_block_lookup_o1_speed():
    import time

    import tempfile
    blocks = tuple(_b(i) for i in range(10_000))
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8",
    ) as f:
        path = f.name
    doc = PhysicalDocument(
        path=path, format="txt", title=None, size_bytes=0,
        blocks=blocks, page_count=1,
    )
    lookup = build_block_lookup(doc)

    start = time.perf_counter()
    for _ in range(10_000):
        lookup.get_by_ord(5_000)
    elapsed_lookup = time.perf_counter() - start

    blocks_list = list(doc.blocks)
    start = time.perf_counter()
    for _ in range(100):
        blocks_list.index(_b(5_000))
    elapsed_linear = (time.perf_counter() - start) * 100

    assert elapsed_lookup < elapsed_linear