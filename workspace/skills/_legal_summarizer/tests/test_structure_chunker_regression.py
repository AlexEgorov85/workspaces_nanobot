"""Regression test для STRUCTURAL_PACKING_PLAN.

Фиксирует текущее поведение chunker'а: 3 articles × 3K → 3 chunks.
После рефакторинга тест изменяется: ожидаемое число chunks должно быть
1 (см. updated expected в комментарии).
"""

from __future__ import annotations

from chunking.chunker import (
    DocumentStructureChunkerConfig,
    chunk_from_structure,
)
from chunking.chunks import (
    ChunkConfig,
)
from document.physical import (
    DocumentBlock,
    PhysicalDocument,
)
from document.structure import (
    DocumentStructure,
    StructureNode,
)


def _b(ordinal: int, content: str, block_type: str = "paragraph") -> DocumentBlock:
    return DocumentBlock(
        block_id=f"b_{ordinal:04d}",
        block_type=block_type,
        content=content,
        char_count=len(content),
        page_index=ordinal + 1,
        page_start=ordinal + 1,
        page_end=ordinal + 1,
        paragraph_index=None,
        table_index=None,
        ordinal=ordinal,
        block_metadata={},
    )


def _make_doc(blocks: tuple[DocumentBlock, ...]) -> PhysicalDocument:
    return PhysicalDocument(
        path="/tmp/x.pdf",
        format="pdf",
        title=None,
        size_bytes=0,
        blocks=blocks,
        page_count=len(blocks),
    )


def _root(children: tuple[str, ...] = ()) -> StructureNode:
    return StructureNode(
        node_id="n_0000",
        node_type="document",
        semantic_type=None,
        level=0,
        title="",
        number=None,
        parent_id=None,
        children=children,
        start_block=0,
        end_block=10,
        confidence=1.0,
    )


def _sec(
    nid: str,
    *,
    start: int,
    end: int,
    title: str,
    parent: str = "n_0000",
) -> StructureNode:
    return StructureNode(
        node_id=nid,
        node_type="section",
        semantic_type="article",
        level=1,
        title=title,
        number=None,
        parent_id=parent,
        children=(),
        start_block=start,
        end_block=end,
        confidence=0.7,
    )


def test_three_articles_one_chunk():
    """Bug fix regression: 3 articles × 3K → 1 chunk (после рефакторинга).

    Setup:
        Chapter
            Article 1 (3 blocks × 1000 = 3000 chars)
            Article 2 (3 blocks × 1000 = 3000 chars)
            Article 3 (3 blocks × 1000 = 3000 chars)
        target=10000, max=20000.
        Total content ≈ 9000 chars (всего < target).

    До рефакторинга (owner-boundary):
        chunks = 3 (каждый article отдельно, потому что owner меняется).

    После рефакторинга (structural packing):
        chunks = 1 (9000 < target=10000, объединяем).
    """
    chapter = StructureNode(
        node_id="n_0001",
        node_type="section",
        semantic_type="chapter",
        level=1,
        title="Chapter",
        number=None,
        parent_id="n_0000",
        children=("n_0002", "n_0003", "n_0004"),
        start_block=0,
        end_block=8,
        confidence=0.9,
    )
    art1 = _sec("n_0002", start=0, end=2, title="Article 1", parent="n_0001")
    art2 = _sec("n_0003", start=3, end=5, title="Article 2", parent="n_0001")
    art3 = _sec("n_0004", start=6, end=8, title="Article 3", parent="n_0001")

    blocks = tuple(_b(i, "x" * 1000) for i in range(9))
    doc = _make_doc(blocks)
    struct = DocumentStructure(
        document_id="d",
        title=None,
        nodes={
            "n_0000": _root(("n_0001",)),
            "n_0001": chapter,
            "n_0002": art1,
            "n_0003": art2,
            "n_0004": art3,
        },
        root_id="n_0000",
        preamble_node_id="n_0000",
        numbering=(),
        total_blocks=9,
    )
    cfg = DocumentStructureChunkerConfig(
        chunk_config=ChunkConfig(
            max_chunk_chars=20000,
            chunk_overlap_chars=0,
            target_chunk_chars=10000,
            preferred_min_before_strong_boundary=0.7,
        ),
    )
    chunks = chunk_from_structure(doc, struct, config=cfg)

    assert len(chunks) == 1, (
        f"expected 1 chunk (structural packing), got {len(chunks)}"
    )
    assert chunks[0].block_indices == (0, 1, 2, 3, 4, 5, 6, 7, 8)
    assert chunks[0].section_ids == ("n_0002", "n_0003", "n_0004")
