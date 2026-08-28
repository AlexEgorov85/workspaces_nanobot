"""Тесты для ``packing.py`` (section-locality greedy).

Покрывает:
    * Section-locality (никогда не смешивает разные sections)
    * Document order сохраняется
    * Token budget рассчитан правильно
    * Greedy заполнение в одной section
    * Long section → multiple batches той же section
    * Edge cases: empty, single chunk
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "workspace" / "skills" / "legal_summarizer" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
_PROJ = _REPO
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))

from workspace.skills.legal_summarizer.scripts.packing import (  # noqa: E402
    ContextBatch,
    TokenBudget,
    pack_chunks,
)
from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk  # noqa: E402


def _budget(available: int = 10000) -> TokenBudget:
    return TokenBudget(
        context_window_tokens=65536,
        system_prompt_tokens=1200,
        instruction_tokens=200,
        output_reserve_tokens=8192,
        safety_margin=0.85,
        chars_per_token=3.5,
    )


def _chunk(
    chunk_id: str,
    chars: int,
    *,
    section_id: str = "s_0001",
    section_path: str = "1",
    page: int | None = 1,
    token_estimate: int | None = None,
) -> Chunk:
    est = token_estimate if token_estimate is not None else max(1, chars // 3)
    return Chunk(
        chunk_id=chunk_id,
        index=int(chunk_id),
        text="x" * chars,
        char_count=chars,
        token_estimate=est,
        page_start=page,
        page_end=page,
        section_id=section_id,
        section_path=section_path,
        section_heading="Heading",
        block_indices=(int(chunk_id),),
        block_types=("paragraph",),
    )


def test_empty_chunks_returns_empty_batches():
    budget = _budget()
    assert pack_chunks([], budget) == []


def test_single_chunk_one_batch():
    chunks = [_chunk("000", 1000)]
    budget = _budget()
    batches = pack_chunks(chunks, budget)
    assert len(batches) == 1
    assert len(batches[0].chunks) == 1


def test_two_small_chunks_one_batch():
    chunks = [_chunk("000", 1000), _chunk("001", 1000)]
    budget = _budget()
    batches = pack_chunks(chunks, budget)
    assert len(batches) == 1
    assert len(batches[0].chunks) == 2


def test_two_chunks_exceed_budget_two_batches():
    """Budget не позволяет оба → разные batches."""
    budget = TokenBudget(
        context_window_tokens=1000,
        system_prompt_tokens=0,
        instruction_tokens=0,
        output_reserve_tokens=0,
        safety_margin=1.0,
        chars_per_token=1.0,
    )
    available = budget.available_chunk_tokens
    chunks = [
        _chunk("000", 600, token_estimate=600),
        _chunk("001", 600, token_estimate=600),
    ]
    batches = pack_chunks(chunks, budget)
    assert len(batches) == 2


def test_packing_preserves_document_order():
    chunks = [_chunk(f"{i:03d}", 100, token_estimate=10) for i in range(5)]
    budget = _budget()
    batches = pack_chunks(chunks, budget)
    all_chunk_ids = [c.chunk_id for b in batches for c in b.chunks]
    assert all_chunk_ids == [f"{i:03d}" for i in range(5)]


def test_packing_never_mixes_distant_sections():
    chunks = [
        _chunk("000", 100, section_id="s1", section_path="1", token_estimate=10),
        _chunk("001", 100, section_id="s1", section_path="1", token_estimate=10),
        _chunk("002", 100, section_id="s2", section_path="2", token_estimate=10),
        _chunk("003", 100, section_id="s3", section_path="3", token_estimate=10),
    ]
    budget = TokenBudget(
        context_window_tokens=10000,
        system_prompt_tokens=0,
        instruction_tokens=0,
        output_reserve_tokens=0,
        safety_margin=1.0,
        chars_per_token=1.0,
    )
    batches = pack_chunks(chunks, budget)
    for b in batches:
        section_ids = {c.section_id for c in b.chunks}
        assert len(section_ids) == 1, (
            f"Batch {b.batch_id} mixes sections: {section_ids}"
        )


def test_packing_fills_section_before_crossing_boundary():
    chunks = [
        _chunk("000", 100, section_id="s1", section_path="1", token_estimate=10),
        _chunk("001", 100, section_id="s1", section_path="1", token_estimate=10),
        _chunk("002", 100, section_id="s1", section_path="1", token_estimate=10),
        _chunk("003", 100, section_id="s2", section_path="2", token_estimate=10),
        _chunk("004", 100, section_id="s2", section_path="2", token_estimate=10),
    ]
    budget = _budget(available=10000)
    batches = pack_chunks(chunks, budget)
    assert batches[0].chunks[-1].chunk_id == "002"
    assert batches[1].chunks[0].chunk_id == "003"


def test_packing_long_section_produces_multiple_batches_same_section():
    """Section > budget → несколько batches, все той же section."""
    budget = TokenBudget(
        context_window_tokens=1000,
        system_prompt_tokens=0,
        instruction_tokens=0,
        output_reserve_tokens=0,
        safety_margin=1.0,
        chars_per_token=1.0,
    )
    available = budget.available_chunk_tokens
    chunks = [
        _chunk(f"{i:03d}", 100, section_id="s1", section_path="1", token_estimate=available // 4)
        for i in range(6)
    ]
    batches = pack_chunks(chunks, budget)
    assert len(batches) > 1
    for b in batches:
        for c in b.chunks:
            assert c.section_id == "s1"


def test_chunk_larger_than_budget_raises():
    budget = TokenBudget(
        context_window_tokens=1500,
        system_prompt_tokens=0,
        instruction_tokens=0,
        output_reserve_tokens=0,
        safety_margin=1.0,
        chars_per_token=1.0,
    )
    chunks = [_chunk("000", 2000, token_estimate=budget.available_chunk_tokens + 1)]
    with pytest.raises(ValueError, match="больше budget"):
        pack_chunks(chunks, budget)


def test_token_budget_calculation():
    budget = TokenBudget(
        context_window_tokens=10000,
        system_prompt_tokens=1000,
        instruction_tokens=500,
        output_reserve_tokens=2000,
        safety_margin=0.5,
        chars_per_token=3.5,
    )
    assert budget.available_chunk_tokens == max(int((10000 - 3500) * 0.5), 1000)


def test_token_budget_floor():
    budget = TokenBudget(
        context_window_tokens=100,
        system_prompt_tokens=50,
        instruction_tokens=50,
        output_reserve_tokens=0,
        safety_margin=1.0,
        chars_per_token=3.5,
    )
    assert budget.available_chunk_tokens == 1000


def test_context_batch_id_format():
    chunks = [_chunk("000", 100, token_estimate=10)]
    batches = pack_chunks(chunks, _budget())
    assert batches[0].batch_id == "cb_000"
    chunks2 = [
        _chunk("000", 100, token_estimate=600),
        _chunk("001", 100, token_estimate=600),
        _chunk("002", 100, token_estimate=600),
    ]
    budget = TokenBudget(
        context_window_tokens=1000,
        system_prompt_tokens=0,
        instruction_tokens=0,
        output_reserve_tokens=0,
        safety_margin=1.0,
        chars_per_token=1.0,
    )
    batches = pack_chunks(chunks2, budget)
    assert batches[0].batch_id == "cb_000"
    assert batches[1].batch_id == "cb_001"


def test_section_paths_in_batch():
    chunks = [_chunk("000", 100, section_path="1 > 1.1")]
    batches = pack_chunks(chunks, _budget())
    assert "1 > 1.1" in batches[0].section_paths


def test_page_range_correct():
    chunks = [
        _chunk("000", 100, page=1),
        _chunk("001", 100, page=2),
        _chunk("002", 100, page=3),
    ]
    batches = pack_chunks(chunks, _budget())
    assert batches[0].page_range == (1, 3)


def test_to_dict_roundtrip():
    batches = pack_chunks([_chunk("000", 100)], _budget())
    d = batches[0].to_dict()
    assert d["batch_id"] == "cb_000"
    assert d["chunk_ids"] == ["000"]


def test_utilization_property():
    chunks = [_chunk("000", 300)]
    batches = pack_chunks(chunks, _budget())
    b = batches[0]
    util = b.utilization
    assert 0.0 <= util <= 1.0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))