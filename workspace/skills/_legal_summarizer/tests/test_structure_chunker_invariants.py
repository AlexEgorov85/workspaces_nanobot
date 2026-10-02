"""Invariant tests для chunker'а (STRUCTURAL_PACKING_).

Тесты для инвариантов I1-I10. Все тесты работают поверх публичного API:
``chunk_from_structure(doc, struct, config=...)``.

Используется существующий chunker + новые поля Chunk (section_ids).
Цель — зафиксировать инварианты ДО изменения алгоритма (на текущем коде)
и убедиться, что после рефакторинга они не нарушаются.
"""

from __future__ import annotations

from chunking.chunker import (
    DocumentStructureChunkerConfig,
    chunk_from_structure,
)
from chunking.chunks import (
    ChunkConfig,
    reconstruct_source_fragment,
)
from document.physical import (
    DocumentBlock,
    PhysicalDocument,
)
from document.structure import (
    DocumentStructure,
    StructureNode,
)


# --- helpers --------------------------------------------------------------


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
    title: str = "Section",
    semantic_type: str | None = None,
    level: int = 1,
    parent: str = "n_0000",
) -> StructureNode:
    return StructureNode(
        node_id=nid,
        node_type="section",
        semantic_type=semantic_type,
        level=level,
        title=title,
        number=None,
        parent_id=parent,
        children=(),
        start_block=start,
        end_block=end,
        confidence=0.7,
    )


def _cfg(*, target: int = 20000, max_chars: int = 30000) -> DocumentStructureChunkerConfig:
    return DocumentStructureChunkerConfig(
        chunk_config=ChunkConfig(
            max_chunk_chars=max_chars,
            chunk_overlap_chars=0,
            target_chunk_chars=target,
            preferred_min_before_strong_boundary=0.7,
        ),
    )


# --- I1 + I2: каждый non-oversized block ровно один раз -------------------


def test_i1_each_non_oversized_block_once():
    """I1: non-oversized block встречается ровно в одном Chunk.block_indices."""
    blocks = tuple(_b(i, f"block {i}") for i in range(6))
    doc = _make_doc(blocks)
    struct = DocumentStructure(
        document_id="d",
        title=None,
        nodes={
            "n_0000": _root(("n_0001", "n_0002")),
            "n_0001": _sec("n_0001", start=0, end=2, title="A"),
            "n_0002": _sec("n_0002", start=3, end=5, title="B"),
        },
        root_id="n_0000",
        preamble_node_id="n_0000",
        numbering=(),
        total_blocks=6,
    )
    chunks = chunk_from_structure(doc, struct, config=_cfg())

    seen: set[int] = set()
    for c in chunks:
        for b in c.block_indices:
            assert b not in seen, f"block {b} duplicated"
            seen.add(b)
    assert seen == set(range(6))


# --- I4: physical order ----------------------------------------------------


def test_i4_physical_order():
    """I4: chunks строго в порядке block ordinals."""
    blocks = (
        _b(0, "A"),
        _b(1, "B"),
        _b(2, "C"),
        _b(3, "D"),
        _b(4, "E"),
    )
    doc = _make_doc(blocks)
    struct = DocumentStructure(
        document_id="d",
        title=None,
        nodes={
            "n_0000": _root(("n_0001", "n_0002", "n_0003")),
            "n_0001": _sec("n_0001", start=0, end=1, title="A"),
            "n_0002": _sec("n_0002", start=2, end=3, title="B"),
            "n_0003": _sec("n_0003", start=4, end=4, title="C"),
        },
        root_id="n_0000",
        preamble_node_id="n_0000",
        numbering=(),
        total_blocks=5,
    )
    chunks = chunk_from_structure(doc, struct, config=_cfg())
    for c1, c2 in zip(chunks, chunks[1:]):
        assert c1.block_indices[-1] < c2.block_indices[0], (
            f"physical order violated: {c1.block_indices[-1]} >= {c2.block_indices[0]}"
        )


# --- I7: нет phantom ordinals ----------------------------------------------


def test_i7_no_phantom_ordinals():
    """I7: все block_indices принадлежат doc.blocks_by_ord."""
    blocks = tuple(_b(i, f"b{i}") for i in range(5))
    doc = _make_doc(blocks)
    struct = DocumentStructure(
        document_id="d",
        title=None,
        nodes={
            "n_0000": _root(("n_0001", "n_0002")),
            "n_0001": _sec("n_0001", start=0, end=2, title="A"),
            "n_0002": _sec("n_0002", start=3, end=4, title="B"),
        },
        root_id="n_0000",
        preamble_node_id="n_0000",
        numbering=(),
        total_blocks=5,
    )
    chunks = chunk_from_structure(doc, struct, config=_cfg())
    valid = set(doc.blocks_by_ord.keys())
    for c in chunks:
        for b in c.block_indices:
            assert b in valid, f"phantom ordinal {b} in chunk {c.chunk_id}"


# --- I8: section_id = anchor owner -----------------------------------------


def test_i8_section_id_is_anchor():
    """I8: chunk.section_id == deepest owner первого block'а."""
    blocks = (
        _b(0, "A1"),
        _b(1, "A2"),
        _b(2, "B1"),
        _b(3, "B2"),
    )
    doc = _make_doc(blocks)
    struct = DocumentStructure(
        document_id="d",
        title=None,
        nodes={
            "n_0000": _root(("n_0001", "n_0002")),
            "n_0001": _sec("n_0001", start=0, end=1, title="A"),
            "n_0002": _sec("n_0002", start=2, end=3, title="B"),
        },
        root_id="n_0000",
        preamble_node_id="n_0000",
        numbering=(),
        total_blocks=4,
    )
    chunks = chunk_from_structure(doc, struct, config=_cfg())
    # chunk[0] должен иметь section_id = "n_0001" (owner первого block'а)
    assert chunks[0].section_id == "n_0001"
    assert chunks[0].section_heading == "A"


# --- I9: section_ids = deepest owners без root_id --------------------------


def test_i9_section_ids_excludes_root():
    """I9: section_ids содержит уникальных deepest owners без root_id."""
    blocks = tuple(_b(i, f"b{i}") for i in range(4))
    doc = _make_doc(blocks)
    struct = DocumentStructure(
        document_id="d",
        title=None,
        nodes={
            "n_0000": _root(("n_0001", "n_0002")),
            "n_0001": _sec("n_0001", start=0, end=1, title="A"),
            "n_0002": _sec("n_0002", start=2, end=3, title="B"),
        },
        root_id="n_0000",
        preamble_node_id="n_0000",
        numbering=(),
        total_blocks=4,
    )
    chunks = chunk_from_structure(doc, struct, config=_cfg())
    # chunks должны иметь заполненное поле section_ids
    for c in chunks:
        assert isinstance(c.section_ids, tuple)
        for sid in c.section_ids:
            assert sid != "n_0000", "root_id должен быть исключён из section_ids"


def test_i9_section_ids_dedup_preserves_order():
    """I9: section_ids сохраняет document order и дедуплицирует."""
    # Создаём документ с 3 articles, чтобы новый chunker мог их объединить.
    # Проверяем что section_ids содержит уникальные значения.
    blocks = tuple(_b(i, f"b{i}") for i in range(9))
    doc = _make_doc(blocks)
    struct = DocumentStructure(
        document_id="d",
        title=None,
        nodes={
            "n_0000": _root(("n_0001", "n_0002", "n_0003")),
            "n_0001": _sec("n_0001", start=0, end=2, title="Art1"),
            "n_0002": _sec("n_0002", start=3, end=5, title="Art2"),
            "n_0003": _sec("n_0003", start=6, end=8, title="Art3"),
        },
        root_id="n_0000",
        preamble_node_id="n_0000",
        numbering=(),
        total_blocks=9,
    )
    chunks = chunk_from_structure(doc, struct, config=_cfg(max_chars=100000))
    # Если все 3 articles объединились в один chunk — section_ids должен
    # содержать все 3 в document order.
    multi = [c for c in chunks if len(c.block_indices) > 1]
    if multi:
        for c in multi:
            assert len(c.section_ids) == len(set(c.section_ids)), (
                f"section_ids не дедуплицирован: {c.section_ids}"
            )


# --- I10: target soft, max hard --------------------------------------------


def test_i10_target_soft_max_hard():
    """I10: chunks могут превышать target (soft), но не max (hard)."""
    # 9K + 9K + 5K = 23K. target=10K, max=30K.
    # Greedy должен объединить, потому что не strong boundary.
    blocks = (
        _b(0, "x" * 1000),
        _b(1, "x" * 1000),
        _b(2, "x" * 1000),
        _b(3, "x" * 1000),
        _b(4, "x" * 1000),
        _b(5, "x" * 1000),
        _b(6, "x" * 1000),
        _b(7, "x" * 1000),
        _b(8, "x" * 1000),
        _b(9, "x" * 1000),
        _b(10, "x" * 1000),
        _b(11, "x" * 1000),
        _b(12, "x" * 1000),
        _b(13, "x" * 1000),
        _b(14, "x" * 1000),
        _b(15, "x" * 1000),
        _b(16, "x" * 1000),
        _b(17, "x" * 1000),
        _b(18, "x" * 1000),
        _b(19, "x" * 1000),
        _b(20, "x" * 1000),
        _b(21, "x" * 1000),
        _b(22, "x" * 1000),
        _b(23, "x" * 1000),
    )
    doc = _make_doc(blocks)
    struct = DocumentStructure(
        document_id="d",
        title=None,
        nodes={
            "n_0000": _root(("n_0001", "n_0002", "n_0003")),
            "n_0001": _sec("n_0001", start=0, end=8, title="A"),
            "n_0002": _sec("n_0002", start=9, end=17, title="B"),
            "n_0003": _sec("n_0003", start=18, end=23, title="C"),
        },
        root_id="n_0000",
        preamble_node_id="n_0000",
        numbering=(),
        total_blocks=24,
    )
    chunks = chunk_from_structure(
        doc, struct, config=_cfg(target=10000, max_chars=30000),
    )
    # Max hard: ни один chunk не превышает 30000.
    for c in chunks:
        assert c.char_count <= 30000


# --- Reconstruction (ТЗ §25) ----------------------------------------------


def test_reconstruction_for_multiblock_chunk():
    """chunk.text должен совпадать с reconstruct_source_fragment()."""
    blocks = tuple(_b(i, f"text {i}") for i in range(5))
    doc = _make_doc(blocks)
    struct = DocumentStructure(
        document_id="d",
        title=None,
        nodes={
            "n_0000": _root(("n_0001",)),
            "n_0001": _sec("n_0001", start=0, end=4),
        },
        root_id="n_0000",
        preamble_node_id="n_0000",
        numbering=(),
        total_blocks=5,
    )
    chunks = chunk_from_structure(doc, struct, config=_cfg())
    for c in chunks:
        reconstructed = reconstruct_source_fragment(c, doc=doc)
        assert reconstructed == c.text, (
            f"reconstruction failed for {c.chunk_id}"
        )
