"""Тесты для order (Этап 67 из PLAN.md)."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.order import (
    ensure_order_preserved, restore_document_order,
)


def _c(cid: str, idx: int) -> Chunk:
    return Chunk(
        chunk_id=cid, index=idx, text="x", char_count=1, token_estimate=1,
        page_start=1, page_end=1, section_id="s1", section_path="1",
        section_heading="x", block_indices=(0,), block_types=("paragraph",),
    )


def test_restore_document_order():
    chunks = [_c("001", 1), _c("002", 0), _c("003", 2)]
    out = restore_document_order(chunks)
    assert [c.chunk_id for c in out] == ["002", "001", "003"]


def test_ensure_order_already_sorted():
    chunks = [_c("001", 0), _c("002", 1)]
    out = ensure_order_preserved(chunks, ["001", "002"])
    assert [c.chunk_id for c in out] == ["001", "002"]


def test_ensure_order_dedups():
    chunks = [_c("001", 1), _c("002", 0), _c("003", 2)]
    out = ensure_order_preserved(chunks, ["001", "002"])
    assert [c.chunk_id for c in out] == ["001", "002", "003"]


def test_restore_empty():
    assert restore_document_order(()) == []


def test_ensure_empty():
    assert ensure_order_preserved((), ["a"]) == []