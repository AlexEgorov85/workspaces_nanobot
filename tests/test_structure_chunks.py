"""Тесты для ``structure/chunks.py``.

Покрывает:
    * Section-preserving chunking
    * Page range корректен
    * Tables атомарны (split by rows, table_id/row_start/row_end)
    * Section splitting с split_text
    * block_indices монотонны (invariant #3)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "workspace" / "skills" / "legal_summarizer" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
_PROJ = _REPO
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))

from workspace.skills.legal_summarizer.scripts.structure.chunks import (  # noqa: E402
    Chunk,
    ChunkConfig,
    StructureAwareChunker,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (  # noqa: E402
    DocumentBlock,
    PhysicalDocument,
)
from workspace.skills.legal_summarizer.scripts.structure.sections import (  # noqa: E402
    ROOT_SECTION_ID,
    DocumentSection,
    SectionTree,
    detect_sections,
)


def _make_doc(blocks_data: list[dict[str, Any]]) -> PhysicalDocument:
    blocks: list[DocumentBlock] = []
    for i, b in enumerate(blocks_data):
        blocks.append(
            DocumentBlock(
                block_id=f"b_{i:04d}",
                block_type=b.get("block_type", "paragraph"),
                content=b["content"],
                char_count=len(b["content"]),
                page_index=b.get("page_index"),
                page_start=b.get("page_index"),
                page_end=b.get("page_index"),
                paragraph_index=b.get("paragraph_index"),
                table_index=None,
                ordinal=i,
                block_metadata=b.get("block_metadata", {}),
            )
        )
    return PhysicalDocument(
        path="<test>",
        format="docx",
        title=None,
        size_bytes=0,
        blocks=tuple(blocks),
        page_count=1,
    )


def _config(max_chunk_chars: int = 4000) -> ChunkConfig:
    return ChunkConfig(
        max_chunk_chars=max_chunk_chars,
        chunk_overlap_chars=200,
        chars_per_token=3.5,
        table_chunk_threshold_chars=6000,
    )


def test_chunks_preserve_section_path():
    doc = _make_doc([
        {"content": "1. Первый раздел", "block_metadata": {"style": "Heading 1"}},
        {"content": "Длинное тело первого раздела с описанием предмета и сторон договора аренды."},
        {"content": "2. Второй раздел", "block_metadata": {"style": "Heading 1"}},
        {"content": "Длинное тело второго раздела с описанием прав и обязанностей сторон."},
    ])
    tree = detect_sections(doc)
    chunker = StructureAwareChunker()
    chunks = chunker.chunk(doc, tree, _config())
    assert len(chunks) == 2
    assert chunks[0].section_path == "1"
    assert chunks[1].section_path == "2"


def test_chunks_have_zero_padded_ids():
    doc = _make_doc([
        {"content": "1. Р", "block_metadata": {"style": "Heading 1"}},
        {"content": "Длинное тело первого раздела с описанием."},
        {"content": "2. Р", "block_metadata": {"style": "Heading 1"}},
        {"content": "Длинное тело второго раздела с описанием."},
    ])
    tree = detect_sections(doc)
    chunks = StructureAwareChunker().chunk(doc, tree, _config())
    assert chunks[0].chunk_id == "000"
    assert chunks[1].chunk_id == "001"


def test_chunks_page_range_correct():
    doc = _make_doc([
        {"content": "1. Р", "block_metadata": {"style": "Heading 1"}, "page_index": 1},
        {"content": "Тело раздела на странице 1.", "page_index": 1},
        {"content": "Продолжение тела на странице 2.", "page_index": 2},
        {"content": "Конец тела на странице 2.", "page_index": 2},
    ])
    tree = detect_sections(doc)
    chunks = StructureAwareChunker().chunk(doc, tree, _config())
    assert chunks[0].page_start == 1
    assert chunks[0].page_end == 2


def test_section_larger_than_max_splits_within_section():
    big_body = "Слово " * 1500
    doc = _make_doc([
        {"content": "1. Раздел один", "block_metadata": {"style": "Heading 1"}},
        {"content": big_body},
        {"content": big_body},
    ])
    tree = detect_sections(doc)
    chunks = StructureAwareChunker().chunk(doc, tree, _config(max_chunk_chars=4000))
    assert len(chunks) >= 2
    for c in chunks:
        assert c.section_id != ROOT_SECTION_ID
        assert c.section_path == "1"


def test_chunk_block_indices_are_sorted():
    doc = _make_doc([
        {"content": "1. Р", "block_metadata": {"style": "Heading 1"}},
        {"content": "Тело a.", "page_index": 1},
        {"content": "Тело b.", "page_index": 1},
        {"content": "Тело c.", "page_index": 2},
    ])
    tree = detect_sections(doc)
    chunks = StructureAwareChunker().chunk(doc, tree, _config())
    for c in chunks:
        assert list(c.block_indices) == sorted(c.block_indices)


def test_table_block_not_split_across_chunks():
    doc = _make_doc([
        {"content": "1. Р", "block_metadata": {"style": "Heading 1"}},
        {"content": "Длинное тело раздела с описанием обязательств сторон."},
        {"content": "a | b\nc | d\ne | f", "block_type": "table", "block_metadata": {"row_count": 3}},
    ])
    tree = detect_sections(doc)
    chunks = StructureAwareChunker().chunk(doc, tree, _config())
    table_chunks = [c for c in chunks if c.table_id]
    assert len(table_chunks) == 1
    assert table_chunks[0].table_id == "t_0002"
    assert table_chunks[0].table_row_start == 1
    assert table_chunks[0].table_row_end == 3


def test_large_table_split_by_rows_with_table_metadata():
    rows = [f"row{i} | col{i}" for i in range(1, 21)]
    big_table = "\n".join(rows)
    doc = _make_doc([
        {"content": "1. Р", "block_metadata": {"style": "Heading 1"}},
        {"content": big_table, "block_type": "table", "block_metadata": {"row_count": 20}},
    ])
    tree = detect_sections(doc)
    chunks = StructureAwareChunker().chunk(
        doc, tree, ChunkConfig(max_chunk_chars=4000, chunk_overlap_chars=0, table_chunk_threshold_chars=200)
    )
    table_chunks = [c for c in chunks if c.table_id]
    assert len(table_chunks) > 1
    table_id = table_chunks[0].table_id
    for c in table_chunks:
        assert c.table_id == table_id
        assert c.table_row_start is not None
        assert c.table_row_end is not None
        assert c.table_row_start <= c.table_row_end


def test_split_table_preserves_section_path():
    rows = [f"row{i} | col{i}" for i in range(1, 11)]
    big_table = "\n".join(rows)
    doc = _make_doc([
        {"content": "1. Р", "block_metadata": {"style": "Heading 1"}},
        {"content": "Длинное тело раздела с описанием предмета и сторон договора."},
        {"content": big_table, "block_type": "table", "block_metadata": {"row_count": 10}},
    ])
    tree = detect_sections(doc)
    chunks = StructureAwareChunker().chunk(
        doc, tree, ChunkConfig(max_chunk_chars=4000, chunk_overlap_chars=0, table_chunk_threshold_chars=100)
    )
    table_chunks = [c for c in chunks if c.table_id]
    assert len(table_chunks) > 1
    assert all(c.section_path == "1" for c in table_chunks)


def test_no_sections_chunks_all_to_root():
    doc = _make_doc([
        {"content": "Просто параграф один без headings."},
        {"content": "Просто параграф два без headings и с содержанием."},
        {"content": "Просто параграф три без headings и с содержанием."},
    ])
    tree = detect_sections(doc)
    chunks = StructureAwareChunker().chunk(doc, tree, _config())
    assert all(c.section_id == ROOT_SECTION_ID for c in chunks)


def test_chunk_token_estimate():
    """token_estimate = ceil(char_count / chars_per_token)."""
    doc = _make_doc([
        {"content": "1. Р", "block_metadata": {"style": "Heading 1"}},
        {"content": "Длинное тело раздела с подробным описанием предмета и обязательств сторон договора аренды."},
        {"content": "a" * 3500},
    ])
    tree = detect_sections(doc)
    chunks = StructureAwareChunker().chunk(doc, tree, _config())
    body_chunk = max(chunks, key=lambda c: c.char_count)
    import math
    expected = math.ceil(body_chunk.char_count / 3.5)
    assert body_chunk.token_estimate == expected


def test_chunk_text_not_empty():
    doc = _make_doc([
        {"content": "1. Р", "block_metadata": {"style": "Heading 1"}},
        {"content": "Длинное тело раздела с описанием предмета договора аренды."},
    ])
    tree = detect_sections(doc)
    chunks = StructureAwareChunker().chunk(doc, tree, _config())
    for c in chunks:
        assert c.text.strip()


def test_chunk_to_dict_roundtrip():
    c = Chunk(
        chunk_id="000",
        index=0,
        text="hello",
        char_count=5,
        token_estimate=2,
        page_start=1,
        page_end=2,
        section_id="s_0001",
        section_path="1",
        section_heading="Заголовок",
        block_indices=(0, 1),
        block_types=("paragraph", "paragraph"),
        table_id=None,
        table_row_start=None,
        table_row_end=None,
    )
    d = c.to_dict()
    assert d["chunk_id"] == "000"
    assert d["section_path"] == "1"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))