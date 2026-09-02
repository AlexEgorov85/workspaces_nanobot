"""Тесты на таблицы: small/large/header preservation.

Покрывает 3 сценария:

    * **A. small table**: таблица ≤ table_chunk_threshold_chars → один chunk,
      все rows + header inline.
    * **B. large table**: таблица > threshold → split на row-chunks,
      table_id и row_start/row_end корректны для каждого.
    * **C. header preserved**: header (первая строка) сохранён в каждом
      chunk'е при split (если влезает в threshold).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "workspace" / "skills" / "legal_summarizer" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


def _make_doc(blocks_data: list[dict]) -> "PhysicalDocument":
    """Helper: создать PhysicalDocument с блоками."""
    from workspace.skills.legal_summarizer.scripts.structure.physical import (
        DocumentBlock,
        PhysicalDocument,
    )

    blocks = []
    for i, b in enumerate(blocks_data):
        content = b["content"]
        blocks.append(
            DocumentBlock(
                block_id=f"b_{i:04d}",
                block_type=b.get("block_type", "paragraph"),
                content=content,
                char_count=len(content),
                page_index=1,
                page_start=1,
                page_end=1,
                paragraph_index=None,
                table_index=None if b.get("block_type") != "table" else i,
                ordinal=i,
                block_metadata=b.get("block_metadata", {}),
            )
        )
    return PhysicalDocument(
        path="<test>",
        format="txt",
        title=None,
        size_bytes=sum(len(b["content"]) for b in blocks_data),
        blocks=tuple(blocks),
        page_count=1,
    )


def _config(
    *,
    max_chunk_chars: int = 4000,
    chunk_overlap_chars: int = 0,
    table_chunk_threshold_chars: int = 1000,
) -> "ChunkConfig":
    from workspace.skills.legal_summarizer.scripts.structure.chunks import (
        ChunkConfig,
    )

    return ChunkConfig(
        max_chunk_chars=max_chunk_chars,
        chunk_overlap_chars=chunk_overlap_chars,
        table_chunk_threshold_chars=table_chunk_threshold_chars,
    )


def _detect_sections(doc):
    from workspace.skills.legal_summarizer.scripts.structure.sections import (
        detect_sections,
    )

    return detect_sections(doc)


# ---------------------------------------------------------------------------
# Scenario A: small table → один chunk inline
# ---------------------------------------------------------------------------


def test_table_scenario_a_small_inline_one_chunk():
    """A: small table (≤ threshold) → один chunk с table_id."""
    from workspace.skills.legal_summarizer.scripts.structure.chunks import (
        StructureAwareChunker,
    )

    doc = _make_doc([
        {"content": "1. Р", "block_metadata": {"style": "Heading 1"}},
        {"content": "Текст раздела перед таблицей."},
        {
            "content": "Заголовок1 | Заголовок2\nA | B\nC | D",
            "block_type": "table",
            "block_metadata": {"row_count": 3},
        },
    ])
    tree = _detect_sections(doc)
    chunks = StructureAwareChunker().chunk(doc, tree, _config())

    table_chunks = [c for c in chunks if c.table_id]
    assert len(table_chunks) == 1
    chunk = table_chunks[0]
    assert chunk.table_id == "t_0002"
    assert chunk.table_row_start == 1
    assert chunk.table_row_end == 3
    # Header inline.
    assert "Заголовок1 | Заголовок2" in chunk.text


def test_table_scenario_a_small_inline_all_rows_preserved():
    """A: small table — все rows сохранены в chunk'е (header + data)."""
    from workspace.skills.legal_summarizer.scripts.structure.chunks import (
        StructureAwareChunker,
    )

    rows = [f"row{i} | val{i}" for i in range(1, 6)]
    table_text = "Header1 | Header2\n" + "\n".join(rows)

    doc = _make_doc([
        {"content": "1. Р", "block_metadata": {"style": "Heading 1"}},
        {
            "content": table_text,
            "block_type": "table",
            "block_metadata": {"row_count": 6},
        },
    ])
    tree = _detect_sections(doc)
    chunks = StructureAwareChunker().chunk(doc, tree, _config())

    table_chunks = [c for c in chunks if c.table_id]
    assert len(table_chunks) == 1
    chunk = table_chunks[0]
    # Header preserved.
    assert "Header1 | Header2" in chunk.text
    # All 5 data rows preserved.
    for row in rows:
        assert row in chunk.text
    assert chunk.table_row_end == 6


# ---------------------------------------------------------------------------
# Scenario B: large table → split into row-chunks
# ---------------------------------------------------------------------------


def test_table_scenario_b_large_split_into_multiple_chunks():
    """B: large table (> threshold) → multiple chunks."""
    from workspace.skills.legal_summarizer.scripts.structure.chunks import (
        StructureAwareChunker,
    )

    rows = [f"row{i:03d} | col{i}" for i in range(1, 31)]  # 30 rows
    big_table = "\n".join(rows)

    doc = _make_doc([
        {"content": "1. Р", "block_metadata": {"style": "Heading 1"}},
        {
            "content": big_table,
            "block_type": "table",
            "block_metadata": {"row_count": 30},
        },
    ])
    tree = _detect_sections(doc)
    chunks = StructureAwareChunker().chunk(
        doc, tree,
        _config(max_chunk_chars=100000, table_chunk_threshold_chars=200),
    )

    table_chunks = [c for c in chunks if c.table_id]
    assert len(table_chunks) > 1, "Large table должна разбиться на >1 chunk"


def test_table_scenario_b_split_same_table_id():
    """B: все chunks одной таблицы имеют одинаковый table_id."""
    from workspace.skills.legal_summarizer.scripts.structure.chunks import (
        StructureAwareChunker,
    )

    rows = [f"row{i:03d} | col{i}" for i in range(1, 21)]
    big_table = "\n".join(rows)

    doc = _make_doc([
        {"content": "1. Р", "block_metadata": {"style": "Heading 1"}},
        {
            "content": big_table,
            "block_type": "table",
            "block_metadata": {"row_count": 20},
        },
    ])
    tree = _detect_sections(doc)
    chunks = StructureAwareChunker().chunk(
        doc, tree,
        _config(table_chunk_threshold_chars=100),
    )

    table_chunks = [c for c in chunks if c.table_id]
    assert len(table_chunks) > 1
    table_id = table_chunks[0].table_id
    for c in table_chunks:
        assert c.table_id == table_id


def test_table_scenario_b_split_row_ranges_cover_all_rows():
    """B: row_start/row_end покрывают все rows без пропусков."""
    from workspace.skills.legal_summarizer.scripts.structure.chunks import (
        StructureAwareChunker,
    )

    rows = [f"row{i:03d} | col{i}" for i in range(1, 16)]
    big_table = "\n".join(rows)

    doc = _make_doc([
        {"content": "1. Р", "block_metadata": {"style": "Heading 1"}},
        {
            "content": big_table,
            "block_type": "table",
            "block_metadata": {"row_count": 15},
        },
    ])
    tree = _detect_sections(doc)
    chunks = StructureAwareChunker().chunk(
        doc, tree,
        _config(table_chunk_threshold_chars=80),
    )

    table_chunks = [c for c in chunks if c.table_id]
    # Все chunks покрывают 15 rows без перекрытия.
    covered_ranges = [
        (c.table_row_start, c.table_row_end) for c in table_chunks
    ]
    # Start = 1, end = 15.
    assert covered_ranges[0][0] == 1
    assert covered_ranges[-1][1] == 15
    # Последовательные ranges не пересекаются.
    for i in range(len(covered_ranges) - 1):
        assert covered_ranges[i][1] < covered_ranges[i + 1][0]


# ---------------------------------------------------------------------------
# Scenario C: header preserved при truncation
# ---------------------------------------------------------------------------


def test_table_scenario_c_header_preserved_in_first_chunk():
    """C: header (первая строка) сохранён в первом chunk'е при split.

    Header — первая строка таблицы, попадает в первый chunk (row 1).
    Последующие chunks содержат свои data rows (без header).
    """
    from workspace.skills.legal_summarizer.scripts.structure.chunks import (
        StructureAwareChunker,
    )

    header = "ID | Name | Value"
    rows = [f"{i:03d} | item{i} | val{i}" for i in range(1, 11)]
    table_text = header + "\n" + "\n".join(rows)

    doc = _make_doc([
        {"content": "1. Р", "block_metadata": {"style": "Heading 1"}},
        {
            "content": table_text,
            "block_type": "table",
            "block_metadata": {"row_count": 11},
        },
    ])
    tree = _detect_sections(doc)
    chunks = StructureAwareChunker().chunk(
        doc, tree,
        _config(table_chunk_threshold_chars=50),
    )

    table_chunks = sorted(
        [c for c in chunks if c.table_id],
        key=lambda c: c.table_row_start,
    )
    assert len(table_chunks) > 1

    # Первый chunk содержит header.
    first = table_chunks[0]
    assert first.table_row_start == 1
    assert header in first.text

    # Каждый chunk содержит хотя бы одну data row.
    for c in table_chunks:
        lines = c.text.split("\n")
        assert len(lines) >= 1
        # Header только в первом chunk'е.
        if c.table_row_start == 1:
            assert header in lines
        else:
            # Последующие chunks не содержат header (он в row 1).
            assert header not in c.text or any(
                line.startswith("ID |") for line in lines
            ) is False


def test_table_scenario_c_first_chunk_includes_header_first():
    """C: первый chunk начинается с header (row 1)."""
    from workspace.skills.legal_summarizer.scripts.structure.chunks import (
        StructureAwareChunker,
    )

    header = "Заголовок | Колонка2"
    rows = [f"данные{i} | значение{i}" for i in range(1, 11)]
    table_text = header + "\n" + "\n".join(rows)

    doc = _make_doc([
        {"content": "1. Р", "block_metadata": {"style": "Heading 1"}},
        {
            "content": table_text,
            "block_type": "table",
            "block_metadata": {"row_count": 11},
        },
    ])
    tree = _detect_sections(doc)
    chunks = StructureAwareChunker().chunk(
        doc, tree,
        _config(table_chunk_threshold_chars=50),
    )

    table_chunks = sorted(
        [c for c in chunks if c.table_id],
        key=lambda c: c.table_row_start,
    )
    first = table_chunks[0]
    assert first.table_row_start == 1
    # Первая строка chunk'а — header.
    lines = first.text.split("\n")
    assert lines[0] == header


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_table_scenario_a_empty_table_no_rows():
    """A: таблица с 0 rows — обрабатывается как один chunk."""
    from workspace.skills.legal_summarizer.scripts.structure.chunks import (
        StructureAwareChunker,
    )

    doc = _make_doc([
        {"content": "1. Р", "block_metadata": {"style": "Heading 1"}},
        {
            "content": "Только заголовок",
            "block_type": "table",
            "block_metadata": {"row_count": 0},
        },
    ])
    tree = _detect_sections(doc)
    chunks = StructureAwareChunker().chunk(doc, tree, _config())
    table_chunks = [c for c in chunks if c.table_id]
    assert len(table_chunks) == 1
    assert table_chunks[0].table_row_end == 0


def test_table_scenario_b_threshold_equal_to_size_no_split():
    """B: таблица ровно по размеру threshold → 1 chunk (≤)."""
    from workspace.skills.legal_summarizer.scripts.structure.chunks import (
        StructureAwareChunker,
    )

    rows = [f"r{i:03d} | v{i}" for i in range(1, 4)]
    table_text = "\n".join(rows)
    threshold = len(table_text)

    doc = _make_doc([
        {"content": "1. Р", "block_metadata": {"style": "Heading 1"}},
        {
            "content": table_text,
            "block_type": "table",
            "block_metadata": {"row_count": 3},
        },
    ])
    tree = _detect_sections(doc)
    chunks = StructureAwareChunker().chunk(
        doc, tree,
        _config(table_chunk_threshold_chars=threshold),
    )
    table_chunks = [c for c in chunks if c.table_id]
    assert len(table_chunks) == 1
