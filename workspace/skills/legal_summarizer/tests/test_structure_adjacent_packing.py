"""Тесты для adjacent-section packing (Этап 22 из PLAN.md)."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.adjacent_packing import (
    AdjacentPackingConfig,
    pack_chunks_with_adjacent,
)
from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk


def _c(cid: str, section: str, text: str = "x" * 100) -> Chunk:
    return Chunk(
        chunk_id=cid, index=int(cid), text=text, char_count=len(text),
        token_estimate=10, page_start=1, page_end=1,
        section_id=section, section_path="1", section_heading=section,
        block_indices=(0,), block_types=("paragraph",),
    )


def test_packs_same_section():
    chunks = tuple(_c(f"{i:03d}", "s1") for i in range(3))
    batches = pack_chunks_with_adjacent(chunks)
    assert len(batches) == 1
    assert batches[0] == ("000", "001", "002")


def test_separates_by_max_sections():
    chunks = (
        _c("000", "s1"),
        _c("001", "s1"),
        _c("002", "s2"),
        _c("003", "s3"),
    )
    cfg = AdjacentPackingConfig(max_sections_per_batch=2, per_batch_token_budget=10000)
    batches = pack_chunks_with_adjacent(chunks, config=cfg)
    assert len(batches) >= 2


def test_keeps_section_provenance_in_batch():
    chunks = (
        _c("000", "s1"),
        _c("001", "s2"),
    )
    cfg = AdjacentPackingConfig(max_sections_per_batch=2, per_batch_token_budget=10000)
    batches = pack_chunks_with_adjacent(chunks, config=cfg)
    assert len(batches) == 1
    assert batches[0] == ("000", "001")


def test_respects_token_budget():
    chunks = tuple(_c(f"{i:03d}", "s1", text="x" * 1000) for i in range(10))
    cfg = AdjacentPackingConfig(per_batch_token_budget=1000)
    batches = pack_chunks_with_adjacent(chunks, config=cfg)
    assert len(batches) > 1


def test_root_section_starts_new_batch():
    chunks = (
        _c("000", "s1"),
        _c("001", "s_root"),
        _c("002", "s1"),
    )
    batches = pack_chunks_with_adjacent(chunks)
    assert batches == [("000",), ("001",), ("002",)]


def test_empty():
    assert pack_chunks_with_adjacent(()) == []


def _table_c(cid: str, section: str) -> Chunk:
    return Chunk(
        chunk_id=cid, index=int(cid), text="| cell |", char_count=10,
        token_estimate=5, page_start=1, page_end=1,
        section_id=section, section_path="1", section_heading=section,
        block_indices=(0,), block_types=("table",),
        table_id=f"t_{cid}",
    )


def test_table_not_mixed_with_non_table():
    """PLAN §9 Rule 1: table + non-table → отдельные batches."""
    chunks = (
        _c("000", "s1"),
        _table_c("001", "s1"),
        _c("002", "s1"),
    )
    cfg = AdjacentPackingConfig(max_sections_per_batch=2, per_batch_token_budget=10000)
    batches = pack_chunks_with_adjacent(chunks, config=cfg)
    assert batches == [("000",), ("001",), ("002",)]


def test_table_table_separate_by_default():
    """PLAN §9 Rule 2: table + table → отдельные (allow_table_table_batch=False)."""
    chunks = (
        _table_c("000", "s1"),
        _table_c("001", "s1"),
    )
    cfg = AdjacentPackingConfig(allow_table_table_batch=False)
    batches = pack_chunks_with_adjacent(chunks, config=cfg)
    assert batches == [("000",), ("001",)]


def test_table_table_combined_when_allowed():
    """PLAN §9 Rule 2: table + table → один batch если allow_table_table_batch=True."""
    chunks = (
        _table_c("000", "s1"),
        _table_c("001", "s1"),
    )
    cfg = AdjacentPackingConfig(allow_table_table_batch=True)
    batches = pack_chunks_with_adjacent(chunks, config=cfg)
    assert batches == [("000", "001")]


def test_two_sections_in_batch():
    """PLAN §9 Rule 3: 2 sections allowed (max=2)."""
    chunks = (
        _c("000", "s1"),
        _c("001", "s2"),
    )
    cfg = AdjacentPackingConfig(max_sections_per_batch=2, per_batch_token_budget=10000)
    batches = pack_chunks_with_adjacent(chunks, config=cfg)
    assert batches == [("000", "001")]


def test_three_sections_split():
    """PLAN §9 Rule 3: 3 sections → split (max=2)."""
    chunks = (
        _c("000", "s1"),
        _c("001", "s2"),
        _c("002", "s3"),
    )
    cfg = AdjacentPackingConfig(max_sections_per_batch=2, per_batch_token_budget=10000)
    batches = pack_chunks_with_adjacent(chunks, config=cfg)
    assert batches == [("000", "001"), ("002",)]


def test_document_order_preserved():
    """PLAN §9 Rule 4: document order сохраняется."""
    chunks = tuple(_c(f"{i:03d}", "s1" if i < 5 else "s2") for i in range(10))
    cfg = AdjacentPackingConfig(max_sections_per_batch=3, per_batch_token_budget=10000)
    batches = pack_chunks_with_adjacent(chunks, config=cfg)
    flat = [cid for batch in batches for cid in batch]
    assert flat == [f"{i:03d}" for i in range(10)]


def test_budget_exceeded_splits():
    """PLAN §9 Rule 6: budget exceeded → split."""
    chunks = tuple(_c(f"{i:03d}", "s1", text="x" * 3500) for i in range(5))
    cfg = AdjacentPackingConfig(per_batch_token_budget=1000)
    batches = pack_chunks_with_adjacent(chunks, config=cfg)
    assert len(batches) > 1
    flat = [cid for batch in batches for cid in batch]
    assert flat == [f"{i:03d}" for i in range(5)]


def test_deterministic_order_with_set_iteration():
    """PLAN §9: section_ids order не зависит от dict insertion order."""
    chunks = (
        _c("000", "s1"),
        _c("001", "s2"),
        _c("002", "s1"),
        _c("003", "s2"),
    )
    cfg = AdjacentPackingConfig(max_sections_per_batch=2, per_batch_token_budget=10000)
    b1 = pack_chunks_with_adjacent(chunks, config=cfg)
    b2 = pack_chunks_with_adjacent(chunks, config=cfg)
    assert b1 == b2