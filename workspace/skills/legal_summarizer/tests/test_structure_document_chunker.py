"""Тесты для document_structure chunker (Этап 18 из PLAN.md)."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.document_chunker import (
    ChunkPlanner,
    DocumentStructureChunkerConfig,
    chunk_from_structure,
)
from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentStructure,
    StructureNode,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock,
    PhysicalDocument,
)


def _b(ordinal: int, content: str, block_type: str = "paragraph") -> DocumentBlock:
    return DocumentBlock(
        block_id=f"b_{ordinal:04d}", block_type=block_type, content=content,
        char_count=len(content), page_index=ordinal + 1, page_start=ordinal + 1,
        page_end=ordinal + 1, paragraph_index=None, table_index=None,
        ordinal=ordinal, block_metadata={},
    )


def _make_doc(blocks: tuple[DocumentBlock, ...]) -> PhysicalDocument:
    return PhysicalDocument(
        path="/tmp/x.pdf", format="pdf", title=None, size_bytes=0,
        blocks=blocks, page_count=len(blocks),
    )


def _root(children: tuple[str, ...] = ()) -> StructureNode:
    return StructureNode(
        node_id="n_0000", node_type="document", semantic_type=None,
        level=0, title="", number=None, parent_id=None,
        children=children, start_block=0, end_block=10,
        confidence=1.0,
    )


def _sec(nid: str, *, start: int, end: int, title: str = "Section") -> StructureNode:
    return StructureNode(
        node_id=nid, node_type="section", semantic_type=None,
        level=1, title=title, number=None, parent_id="n_0000",
        children=(), start_block=start, end_block=end,
        confidence=0.7,
    )


def test_chunk_from_structure_empty():
    doc = _make_doc(())
    s = DocumentStructure(
        document_id="d", title=None, nodes={"n_0000": _root()},
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=0,
    )
    chunks = chunk_from_structure(doc, s)
    assert chunks == []


def test_chunk_from_structure_single_section():
    """PLAN §7: последовательные blocks с одним owner группируются в chunk.

    blocks (0, "first body"), (1, "second body") оба принадлежат n_0001
    и оба < max_chunk_chars → один chunk с block_indices=(0, 1).
    """
    blocks = (
        _b(0, "first body"),
        _b(1, "second body"),
    )
    doc = _make_doc(blocks)
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={
            "n_0000": _root(("n_0001",)),
            "n_0001": _sec("n_0001", start=0, end=1, title="Sec"),
        },
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=2,
    )
    chunks = chunk_from_structure(doc, s)
    assert len(chunks) == 1
    assert chunks[0].section_id == "n_0001"
    assert chunks[0].section_heading == "Sec"
    assert chunks[0].block_indices == (0, 1)
    assert "first body" in chunks[0].text
    assert "second body" in chunks[0].text


def test_chunk_from_structure_table_atomic():
    blocks = (
        _b(0, "before table"),
        _b(1, "row1 | row2", block_type="table"),
        _b(2, "after table"),
    )
    doc = _make_doc(blocks)
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={
            "n_0000": _root(("n_0001",)),
            "n_0001": _sec("n_0001", start=0, end=2),
        },
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=3,
    )
    chunks = chunk_from_structure(doc, s)
    table_chunks = [c for c in chunks if c.table_id]
    assert len(table_chunks) == 1
    assert table_chunks[0].text == "row1 | row2"


def test_chunk_from_structure_split_oversize_block():
    from workspace.skills.legal_summarizer.scripts.structure.chunks import (
        ChunkConfig,
    )

    long_text = "x" * 200000
    blocks = (_b(0, long_text),)
    doc = _make_doc(blocks)
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={
            "n_0000": _root(("n_0001",)),
            "n_0001": _sec("n_0001", start=0, end=0),
        },
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=1,
    )
    cfg = DocumentStructureChunkerConfig(
        chunk_config=ChunkConfig(max_chunk_chars=50000, chunk_overlap_chars=0),
    )
    chunks = chunk_from_structure(doc, s, config=cfg)
    assert len(chunks) > 1
    assert all(c.section_id == "n_0001" for c in chunks)


def test_chunk_from_structure_section_order():
    """PLAN §7: chunks в physical document order, последовательные blocks
    с одним owner группируются в один chunk.

    blocks 0,1 (A) → один chunk; blocks 2,3 (B) → один chunk.
    """
    blocks = (
        _b(0, "A1"), _b(1, "A2"),
        _b(2, "B1"), _b(3, "B2"),
    )
    doc = _make_doc(blocks)
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={
            "n_0000": _root(("n_0001", "n_0002")),
            "n_0001": _sec("n_0001", start=0, end=1, title="A"),
            "n_0002": _sec("n_0002", start=2, end=3, title="B"),
        },
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=4,
    )
    chunks = chunk_from_structure(doc, s)
    section_titles = [c.section_heading for c in chunks]
    assert section_titles == ["A", "B"]
    assert [c.block_indices for c in chunks] == [(0, 1), (2, 3)]


def test_chunk_planner_class():
    blocks = (_b(0, "hello"),)
    doc = _make_doc(blocks)
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={
            "n_0000": _root(("n_0001",)),
            "n_0001": _sec("n_0001", start=0, end=0),
        },
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=1,
    )
    planner = ChunkPlanner()
    chunks = planner.plan(doc, s)
    assert len(chunks) == 1
    assert chunks[0].text == "hello"


def test_chunks_in_physical_document_order():
    """PLAN §7: chunks строго в document order по block.ordinal.

    blocks:
      0 → Chapter
      1 → Article 1
      2 → Article 1
      3 → Chapter
      4 → Chapter

    Owner: blocks 1,2 принадлежат Article (deepest), 0,3,4 — Chapter.

    Chunks должны выходить в порядке block ordinals, не section-list:
      Chunk(chapter=0,3,4)? Нет — blocks 1,2 между 0 и 3, значит
      chunk с blocks 1,2 появится ПОСЛЕ chunk с block 0.
    """
    blocks = (
        _b(0, "Chapter heading"),
        _b(1, "Article 1 body 1"),
        _b(2, "Article 1 body 2"),
        _b(3, "Chapter body 1"),
        _b(4, "Chapter body 2"),
    )
    doc = _make_doc(blocks)
    chapter = StructureNode(
        node_id="n_0001", node_type="section", semantic_type=None,
        level=1, title="Chapter", number=None, parent_id="n_0000",
        children=("n_0002",), start_block=0, end_block=4,
        confidence=0.7,
    )
    article = StructureNode(
        node_id="n_0002", node_type="section", semantic_type=None,
        level=2, title="Article 1", number=None, parent_id="n_0001",
        children=(), start_block=1, end_block=2,
        confidence=0.7,
    )
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={"n_0000": _root(("n_0001",)), "n_0001": chapter, "n_0002": article},
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=5,
    )
    chunks = chunk_from_structure(doc, s)
    indices = [c.block_indices for c in chunks]
    flat = [b for ci in indices for b in ci]
    assert flat == [0, 1, 2, 3, 4], f"expected physical order, got {flat}"
    assert all(chunks[i].index < chunks[i + 1].index for i in range(len(chunks) - 1))


def test_chunks_have_strictly_increasing_index():
    """chunks[i].index < chunks[i+1].index для всех i."""
    blocks = tuple(_b(i, f"block {i}") for i in range(5))
    doc = _make_doc(blocks)
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={"n_0000": _root()},
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=5,
    )
    chunks = chunk_from_structure(doc, s)
    for i in range(len(chunks) - 1):
        assert chunks[i].index < chunks[i + 1].index


def test_chunks_deterministic_across_runs():
    """Два прогона → identical chunks."""
    blocks = tuple(_b(i, f"block {i}") for i in range(10))
    doc = _make_doc(blocks)
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={
            "n_0000": _root(("n_0001", "n_0002")),
            "n_0001": _sec("n_0001", start=0, end=4, title="A"),
            "n_0002": _sec("n_0002", start=5, end=9, title="B"),
        },
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=10,
    )
    chunks1 = chunk_from_structure(doc, s)
    chunks2 = chunk_from_structure(doc, s)
    assert len(chunks1) == len(chunks2)
    for c1, c2 in zip(chunks1, chunks2):
        assert c1.chunk_id == c2.chunk_id
        assert c1.index == c2.index
        assert c1.block_indices == c2.block_indices


def test_table_ids_unique_across_sections():
    """PLAN §8 acceptance: 10 sections, 20 tables → unique table_ids.

    Document-level counter даёт уникальные table_id для всех таблиц.
    """
    blocks: list[DocumentBlock] = []
    for sec_idx in range(10):
        blocks.append(_b(sec_idx * 3, f"sec {sec_idx} heading"))
        blocks.append(
            _b(sec_idx * 3 + 1, f"row | cell", block_type="table"),
        )
        blocks.append(
            _b(sec_idx * 3 + 2, f"row | cell", block_type="table"),
        )
    doc = _make_doc(tuple(blocks))
    sec_nodes: dict[str, StructureNode] = {}
    sec_nodes["n_0000"] = StructureNode(
        node_id="n_0000", node_type="document", semantic_type=None,
        level=0, title="", number=None, parent_id=None,
        children=tuple(f"n_{i + 1:04d}" for i in range(10)),
        start_block=0, end_block=29, confidence=1.0,
    )
    children_ids: list[str] = []
    for sec_idx in range(10):
        nid = f"n_{sec_idx + 1:04d}"
        sec_nodes[nid] = StructureNode(
            node_id=nid, node_type="section", semantic_type=None,
            level=1, title=f"Sec {sec_idx}", number=None,
            parent_id="n_0000", children=(),
            start_block=sec_idx * 3, end_block=sec_idx * 3 + 2,
            confidence=0.7,
        )
        children_ids.append(nid)
    s = DocumentStructure(
        document_id="d", title=None, nodes=sec_nodes,
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=30,
    )
    chunks = chunk_from_structure(doc, s)
    table_ids = [c.table_id for c in chunks if c.table_id]
    assert len(table_ids) == 20, f"expected 20 tables, got {len(table_ids)}"
    assert len(table_ids) == len(set(table_ids)), (
        f"table_ids must be unique, duplicates: "
        f"{[t for t in table_ids if table_ids.count(t) > 1]}"
    )


def test_table_ids_deterministic():
    """Два прогона → identical table_ids."""
    blocks = (
        _b(0, "h1"),
        _b(1, "row", block_type="table"),
        _b(2, "h2"),
        _b(3, "row", block_type="table"),
    )
    doc = _make_doc(blocks)
    s = DocumentStructure(
        document_id="d", title=None,
        nodes={
            "n_0000": _root(("n_0001", "n_0002")),
            "n_0001": _sec("n_0001", start=0, end=1),
            "n_0002": _sec("n_0002", start=2, end=3),
        },
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=4,
    )
    chunks1 = chunk_from_structure(doc, s)
    chunks2 = chunk_from_structure(doc, s)
    tids1 = [c.table_id for c in chunks1 if c.table_id]
    tids2 = [c.table_id for c in chunks2 if c.table_id]
    assert tids1 == tids2