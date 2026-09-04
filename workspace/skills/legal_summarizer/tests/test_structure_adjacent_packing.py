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