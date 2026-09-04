"""Тесты для benchmark (Этап 51 из PLAN.md)."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.benchmark import (
    BenchmarkMetrics, BenchmarkScenario,
    large_scenario, medium_scenario, run_benchmark,
    small_scenario, very_large_scenario,
)


def test_benchmark_metrics_dataclass():
    m = BenchmarkMetrics(
        name="test",
        parse_count=1, structure_pass_count=1,
        chunk_count=10, batch_count=3,
        map_calls=3, reduce_calls=1, final_calls=1,
        input_tokens=5000, output_tokens=600,
        total_tokens=5600,
    )
    assert m.name == "test"
    assert m.total_tokens == 5600


def test_benchmark_scenario_dataclass():
    s = BenchmarkScenario(name="t", text="x" * 1000, n_sections=2)
    assert s.n_sections == 2


def test_small_scenario():
    s = small_scenario()
    assert s.name == "small"
    assert "Общие положения" in s.text


def test_medium_scenario():
    s = medium_scenario()
    assert s.name == "medium"
    assert s.n_sections == 3


def test_large_scenario():
    s = large_scenario()
    assert s.name == "large"
    assert s.n_sections == 10


def test_very_large_scenario():
    s = very_large_scenario()
    assert s.name == "very_large"
    assert s.n_sections >= 20


def test_run_benchmark_small():
    metrics = run_benchmark(small_scenario())
    assert metrics.name == "small"
    assert metrics.parse_count == 1
    assert metrics.structure_pass_count == 1
    assert metrics.final_calls == 1
    assert metrics.total_tokens > 0


def test_run_benchmark_large():
    metrics = run_benchmark(large_scenario())
    assert metrics.chunk_count > 0
    assert metrics.batch_count > 0


def test_run_benchmark_very_large_no_parse_repetition():
    metrics = run_benchmark(very_large_scenario())
    assert metrics.parse_count == 1
    assert metrics.structure_pass_count == 1


def test_benchmark_no_duplicate_parsing():
    metrics_a = run_benchmark(medium_scenario())
    metrics_b = run_benchmark(medium_scenario())
    assert metrics_a.parse_count == metrics_b.parse_count == 1
    assert metrics_a.structure_pass_count == 1


def test_benchmark_total_tokens_consistent():
    metrics = run_benchmark(small_scenario())
    assert metrics.total_tokens == metrics.input_tokens + metrics.output_tokens