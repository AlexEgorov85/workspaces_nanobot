"""Тесты для candidate aggregator (Этап 9 из PLAN.md)."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.candidate_aggregator import (
    AggregatedCandidate,
    aggregate_by_block,
)
from workspace.skills.legal_summarizer.scripts.structure.heading import (
    HeadingCandidate,
)


def _hc(block_index: int, *, score: float = 0.7, source: str = "regex_numbered_1",
        level: int = 1, raw_number: str | None = None, text: str = "x"):
    return HeadingCandidate(
        block_index=block_index, text=text, score=score, source=source,
        level=level, raw_number=raw_number,
    )


def test_aggregate_single_per_block():
    cs = [
        _hc(0, score=0.7, source="regex_numbered_1", level=1, raw_number="1"),
        _hc(1, score=0.8, source="regex_statiya", level=1, raw_number="статья_1"),
    ]
    out = aggregate_by_block(cs)
    assert len(out) == 2
    assert all(isinstance(a, AggregatedCandidate) for a in out)


def test_aggregate_combines_multiple_sources_same_block():
    cs = [
        _hc(0, score=0.7, source="regex_numbered_1", level=1, raw_number="1"),
        _hc(0, score=0.95, source="docx_style", level=2),
    ]
    out = aggregate_by_block(cs)
    assert len(out) == 1
    a = out[0]
    assert a.block_index == 0
    assert a.confidence == 0.95
    assert a.level == 2
    assert set(a.sources) == {"regex_numbered_1", "docx_style"}


def test_aggregate_outline_separate():
    cs = [
        _hc(0, score=0.7, source="regex_numbered_1"),
        _hc(-1, score=0.95, source="pdf_outline", level=1),
    ]
    out = aggregate_by_block(cs)
    assert len(out) == 2
    assert out[0].block_index == 0
    assert out[1].block_index == -1
    assert out[1].sources == ("pdf_outline",)


def test_aggregate_raw_numbers_collected():
    cs = [
        _hc(0, score=0.5, source="regex_numbered_1", raw_number="1"),
        _hc(0, score=0.95, source="docx_style"),
    ]
    out = aggregate_by_block(cs)
    assert out[0].raw_numbers == ("1",)


def test_aggregate_empty():
    assert aggregate_by_block([]) == []


def test_aggregate_sorted_by_block():
    cs = [
        _hc(5, score=0.5),
        _hc(1, score=0.5),
        _hc(3, score=0.5),
    ]
    out = aggregate_by_block(cs)
    assert [a.block_index for a in out if a.block_index >= 0] == [1, 3, 5]