"""Algorithm tests.

11 unit-тестов, которые проверяют поведение нового алгоритма packing.
Большинство из них FAIL на текущем owner-boundary chunker'е — это
ожидаемо до рефакторинга.

После этого все тесты должны проходить.
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


def _make_section(
    nid: str,
    *,
    start: int,
    end: int,
    title: str = "Section",
    semantic_type: str | None = None,
    level: int = 1,
    parent: str = "n_0000",
    children: tuple[str, ...] = (),
) -> StructureNode:
    return StructureNode(
        node_id=nid,
        node_type="section",
        semantic_type=semantic_type,
        level=level,
        title=title,
        number=None,
        parent_id=parent,
        children=children,
        start_block=start,
        end_block=end,
        confidence=0.7,
    )


def _cfg(
    *,
    target: int = 20000,
    max_chars: int = 30000,
) -> DocumentStructureChunkerConfig:
    return DocumentStructureChunkerConfig(
        chunk_config=ChunkConfig(
            max_chunk_chars=max_chars,
            chunk_overlap_chars=0,
            target_chunk_chars=target,
            preferred_min_before_strong_boundary=0.7,
        ),
    )


def _build_flat_doc(
    sections: list[tuple[str, str, int]],
) -> tuple[PhysicalDocument, DocumentStructure]:
    """Build doc+struct с плоским списком sibling sections.

    sections: list of (semantic_type, title, char_count_per_section).
    Каждая section — 1 block размера char_count.
    Все sections — дети root (node_type='document').
    """
    blocks: list[DocumentBlock] = []
    section_ids: list[str] = []
    ordinal = 0
    for _i, _sc in enumerate(sections, start=1):
        _char = _sc[2]
        section_ids.append(f"n_{_i:04d}")
        blocks.append(_b(ordinal, "x" * _char))
        ordinal += 1
    nodes: dict[str, StructureNode] = {
        "n_0000": _root(tuple(section_ids)),
    }
    for i, (sem_type, title, _char) in enumerate(sections, start=1):
        nid = f"n_{i:04d}"
        nodes[nid] = _make_section(
            nid,
            start=i - 1,
            end=i - 1,
            title=title,
            semantic_type=sem_type,
            level=1,
            parent="n_0000",
        )
    doc = _make_doc(tuple(blocks))
    struct = DocumentStructure(
        document_id="d",
        title=None,
        nodes=nodes,
        root_id="n_0000",
        preamble_node_id="n_0000",
        numbering=(),
        total_blocks=len(blocks),
    )
    return doc, struct


# --- Test 1: 3K + 3K + 3K = 1 chunk (3 small siblings) --------------------


def test_01_three_small_siblings_one_chunk():
    """3 articles × 3K chars → 1 chunk (target=10K, max=20K)."""
    doc, struct = _build_flat_doc([
        ("article", "Article 1", 3000),
        ("article", "Article 2", 3000),
        ("article", "Article 3", 3000),
    ])
    chunks = chunk_from_structure(
        doc, struct, config=_cfg(target=10000, max_chars=20000),
    )
    assert len(chunks) == 1
    assert chunks[0].block_indices == (0, 1, 2)
    assert chunks[0].section_ids == ("n_0001", "n_0002", "n_0003")


# --- Test 2: target exceeded but not max — один chunk --------------------


def test_02_target_soft_one_chunk():
    """9K + 9K + 5K = 23K → 1 chunk (target=10K soft, max=30K)."""
    doc, struct = _build_flat_doc([
        ("article", "Article 1", 9000),
        ("article", "Article 2", 9000),
        ("article", "Article 3", 5000),
    ])
    chunks = chunk_from_structure(
        doc, struct, config=_cfg(target=10000, max_chars=30000),
    )
    assert len(chunks) == 1
    assert chunks[0].block_indices == (0, 1, 2)


# --- Test 3: max split ----------------------------------------------------


def test_03_max_split_two_chunks():
    """10K + 10K + 15K → 2 chunks (max=30K)."""
    doc, struct = _build_flat_doc([
        ("article", "Article 1", 10000),
        ("article", "Article 2", 10000),
        ("article", "Article 3", 15000),
    ])
    chunks = chunk_from_structure(
        doc, struct, config=_cfg(target=20000, max_chars=30000),
    )
    assert len(chunks) == 2
    assert chunks[0].block_indices == (0, 1)
    assert chunks[1].block_indices == (2,)


# --- Test 4: chapter→chapter strong boundary -----------------------------


def test_04_chapter_chapter_strong_boundary():
    """Chapter 1 + Chapter 2 — strong boundary, не объединяем.

    Blocks принадлежат непосредственно chapters (no Article layer).
    Block 0,1 → Chapter 1 (semantic_type='chapter').
    Block 2,3 → Chapter 2 (semantic_type='chapter').
    Chapter 1 total = 12K, preferred_min * target = 7K.
    При переходе к Chapter 2 — strong boundary, current >= 7K → close.
    """
    chapter1 = _make_section(
        "n_0001",
        start=0,
        end=1,
        title="Chapter 1",
        semantic_type="chapter",
        level=1,
    )
    chapter2 = _make_section(
        "n_0003",
        start=2,
        end=3,
        title="Chapter 2",
        semantic_type="chapter",
        level=1,
    )
    blocks = (
        _b(0, "x" * 6000), _b(1, "x" * 6000),
        _b(2, "x" * 3000), _b(3, "x" * 3000),
    )
    doc = _make_doc(blocks)
    struct = DocumentStructure(
        document_id="d",
        title=None,
        nodes={
            "n_0000": _root(("n_0001", "n_0003")),
            "n_0001": chapter1,
            "n_0003": chapter2,
        },
        root_id="n_0000",
        preamble_node_id="n_0000",
        numbering=(),
        total_blocks=4,
    )
    chunks = chunk_from_structure(
        doc, struct, config=_cfg(target=10000, max_chars=30000),
    )
    assert len(chunks) == 2


# --- Test 5: 3×10K = 2 chunks (target=20K, max=25K) ----------------------


def test_05_three_articles_two_chunks():
    """3 articles × 10K, target=20K, max=25K → 2 chunks (20K + 10K)."""
    doc, struct = _build_flat_doc([
        ("article", "Article 1", 10000),
        ("article", "Article 2", 10000),
        ("article", "Article 3", 10000),
    ])
    chunks = chunk_from_structure(
        doc, struct, config=_cfg(target=20000, max_chars=25000),
    )
    assert len(chunks) == 2
    assert chunks[0].block_indices == (0, 1)
    assert chunks[1].block_indices == (2,)


# --- Test 6: small article intact ----------------------------------------


def test_06_small_article_intact():
    """Article 8K (target=20K) → 1 unit, не раскрывается."""
    doc, struct = _build_flat_doc([
        ("article", "Article 1", 8000),
    ])
    chunks = chunk_from_structure(
        doc, struct, config=_cfg(target=20000, max_chars=30000),
    )
    assert len(chunks) == 1
    assert chunks[0].char_count == 8000


# --- Test 7: oversized article split -------------------------------------


def test_07_oversized_article_split():
    """Article 40K при max=30K → split на 2+ chunks через _split_block_with_offsets."""
    blocks = (_b(0, "x" * 40000),)
    doc = _make_doc(blocks)
    article = _make_section(
        "n_0001", start=0, end=0, title="Big Article",
        semantic_type="article", level=1,
    )
    struct = DocumentStructure(
        document_id="d",
        title=None,
        nodes={
            "n_0000": _root(("n_0001",)),
            "n_0001": article,
        },
        root_id="n_0000",
        preamble_node_id="n_0000",
        numbering=(),
        total_blocks=1,
    )
    chunks = chunk_from_structure(
        doc, struct, config=_cfg(target=20000, max_chars=30000),
    )
    assert len(chunks) >= 2
    for c in chunks:
        assert c.char_count <= 30000


# --- Test 8: oversized block splitter -----------------------------------


def test_08_oversized_block_splitter():
    """Block 40K без children → splitter используется."""
    blocks = (_b(0, "x" * 40000),)
    doc = _make_doc(blocks)
    art = _make_section(
        "n_0001", start=0, end=0, title="A",
        semantic_type="article", level=1,
    )
    struct = DocumentStructure(
        document_id="d",
        title=None,
        nodes={
            "n_0000": _root(("n_0001",)),
            "n_0001": art,
        },
        root_id="n_0000",
        preamble_node_id="n_0000",
        numbering=(),
        total_blocks=1,
    )
    chunks = chunk_from_structure(
        doc, struct, config=_cfg(target=20000, max_chars=30000),
    )
    assert len(chunks) >= 2
    assert all(c.block_indices == (0,) for c in chunks)
    offsets = [(c.source_char_start, c.source_char_end) for c in chunks]
    assert len(set(offsets)) == len(offsets)


# --- Test 9: table inline в normal chunk ---------------------------------


def test_09_table_inline():
    """Table block — обычный atomic блок, inline'нутый в normal chunk.

    С архитектурой "таблица = обычный блок" packing объединяет таблицу
    с соседним текстом в один chunk, если секция целиком влезает в
    target. ``table_id`` проставляется только когда chunk — pure table
    (без текстовых блоков); здесь chunk смешанный, поэтому ``table_id
    is None``. Наличие таблицы в chunk'е определяется через
    ``block_types``.
    """
    blocks = (
        _b(0, "before"),
        _b(1, "row | cell", block_type="table"),
        _b(2, "after"),
    )
    doc = _make_doc(blocks)
    sec = _make_section(
        "n_0001", start=0, end=2, title="Sec",
        semantic_type="article", level=1,
    )
    struct = DocumentStructure(
        document_id="d",
        title=None,
        nodes={
            "n_0000": _root(("n_0001",)),
            "n_0001": sec,
        },
        root_id="n_0000",
        preamble_node_id="n_0000",
        numbering=(),
        total_blocks=3,
    )
    chunks = chunk_from_structure(doc, struct, config=_cfg())
    assert len(chunks) == 1
    assert chunks[0].block_indices == (0, 1, 2)
    assert "table" in chunks[0].block_types
    assert chunks[0].table_id is None


# --- Test 10: multi-section reconstruction -------------------------------


def test_10_multisection_chunk_reconstruction():
    """Для multi-section chunk: chunk.text == reconstruct_source_fragment(chunk)."""
    doc, struct = _build_flat_doc([
        ("article", "Article 1", 3000),
        ("article", "Article 2", 3000),
        ("article", "Article 3", 3000),
    ])
    chunks = chunk_from_structure(
        doc, struct, config=_cfg(target=10000, max_chars=30000),
    )
    assert len(chunks) == 1
    reconstructed = reconstruct_source_fragment(chunks[0], doc=doc)
    assert reconstructed == chunks[0].text


# --- Test 11: physical order preserved -----------------------------------


def test_11_physical_order_preserved():
    """chunks[i].block_indices[-1] < chunks[i+1].block_indices[0].

    Используем 3 chapters × 3 articles × 3000 chars для получения
    множественных chunks с strong boundaries между chapters.
    """
    doc, struct = _build_flat_doc([
        ("chapter", f"Chapter {i}", 9000) for i in range(1, 4)
    ])
    chunks = chunk_from_structure(
        doc, struct, config=_cfg(target=10000, max_chars=30000),
    )
    assert len(chunks) >= 2
    for c1, c2 in zip(chunks, chunks[1:]):
        assert c1.block_indices[-1] < c2.block_indices[0]
