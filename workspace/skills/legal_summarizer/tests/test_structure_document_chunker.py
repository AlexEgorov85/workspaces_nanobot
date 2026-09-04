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
    assert len(chunks) == 2
    assert chunks[0].section_id == "n_0001"
    assert chunks[0].section_heading == "Sec"
    assert chunks[1].text == "second body"


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
    assert section_titles == ["A", "A", "B", "B"]


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