from __future__ import annotations

import json
from pathlib import Path

from benchmarks.models import BenchResult, CheckResult, SuiteResult
from benchmarks.reporter import (
    _difficulty_label,
    _group_by_difficulty,
    _pct,
    _result_to_dict,
    _score_label,
    _suite_to_dict,
    save_json_report,
    save_markdown_report,
)


class TestPct:
    def test_perfect(self):
        assert _pct(1.0) == "100.0%"

    def test_half(self):
        assert _pct(0.5) == "50.0%"

    def test_zero(self):
        assert _pct(0.0) == "0.0%"

    def test_rounding(self):
        assert _pct(0.753) == "75.3%"

    def test_small_number(self):
        assert _pct(0.001) == "0.1%"


class TestScoreLabel:
    def test_excellent(self):
        assert _score_label(0.95) == "EXCELLENT"
        assert _score_label(0.9) == "EXCELLENT"

    def test_good(self):
        assert _score_label(0.8) == "GOOD"
        assert _score_label(0.7) == "GOOD"

    def test_satisfactory(self):
        assert _score_label(0.6) == "SATISFACTORY"
        assert _score_label(0.5) == "SATISFACTORY"

    def test_fail(self):
        assert _score_label(0.4) == "FAIL"
        assert _score_label(0.0) == "FAIL"


class TestDifficultyLabel:
    def test_simple(self):
        assert _difficulty_label(1) == "simple"
        assert _difficulty_label(3) == "simple"

    def test_medium(self):
        assert _difficulty_label(4) == "medium"
        assert _difficulty_label(7) == "medium"

    def test_hard(self):
        assert _difficulty_label(8) == "hard"
        assert _difficulty_label(10) == "hard"


class TestResultToDict:
    def test_basic(self, sample_bench_result):
        d = _result_to_dict(sample_bench_result)
        assert d["item_id"] == "result-1"
        assert d["passed"] is True
        assert d["total_score"] == 0.85
        assert d["tools_used"] == ["exec"]
        assert d["steps"] == []
        assert d["checks"] == []

    def test_with_checks(self):
        r = BenchResult(
            item_id="r1",
            checks=[CheckResult("tools", True, 1.0, "All good")],
        )
        d = _result_to_dict(r)
        assert len(d["checks"]) == 1
        assert d["checks"][0]["check"] == "tools"

    def test_with_skills_and_llm(self):
        r = BenchResult(
            item_id="r1",
            skills_activated=["coding"],
            llm_judge_score=0.5,
        )
        d = _result_to_dict(r)
        assert d["skills_activated"] == ["coding"]
        assert d["llm_judge_score"] == 0.5


class TestSuiteToDict:
    def test_basic(self):
        results = [BenchResult(item_id="r1", passed=True, total_score=0.9)]
        sr = SuiteResult(
            suite_name="test",
            timestamp="now",
            total_items=1,
            passed_items=1,
            total_score=0.9,
            avg_score=0.9,
            duration_sec=5.0,
            results=results,
            config={"mode": "full"},
        )
        d = _suite_to_dict(sr)
        assert d["suite_name"] == "test"
        assert d["total_items"] == 1
        assert d["passed_items"] == 1
        assert d["total_score"] == 0.9
        assert d["avg_score"] == 0.9
        assert d["duration_sec"] == 5.0
        assert d["config"] == {"mode": "full"}
        assert len(d["results"]) == 1


class TestGroupByDifficulty:
    def test_groups_by_difficulty(self):
        results = [
            BenchResult(item_id="a", difficulty=1),   # simple
            BenchResult(item_id="b", difficulty=2),   # simple
            BenchResult(item_id="c", difficulty=5),   # medium
            BenchResult(item_id="d", difficulty=10),  # hard
        ]
        groups = _group_by_difficulty(results)
        assert "simple" in groups
        assert "medium" in groups
        assert "hard" in groups
        assert len(groups["simple"]) == 2
        assert len(groups["medium"]) == 1
        assert len(groups["hard"]) == 1

    def test_empty(self):
        assert _group_by_difficulty([]) == {}


class TestSaveJsonReport:
    def test_saves_summary_json(self, tmp_path, sample_bench_result):
        sr = SuiteResult(
            suite_name="test",
            timestamp="now",
            total_items=1,
            passed_items=1,
            total_score=0.85,
            avg_score=0.85,
            duration_sec=5.0,
            results=[sample_bench_result],
        )
        summary_path = save_json_report(sr, tmp_path)
        assert summary_path == tmp_path / "summary.json"
        assert summary_path.exists()
        data = json.loads(summary_path.read_text(encoding="utf-8"))
        assert data["suite_name"] == "test"
        assert data["total_items"] == 1

    def test_saves_detail_files(self, tmp_path, sample_bench_result):
        sr = SuiteResult(
            suite_name="test",
            timestamp="now",
            total_items=1,
            passed_items=1,
            total_score=0.85,
            avg_score=0.85,
            duration_sec=5.0,
            results=[sample_bench_result],
        )
        save_json_report(sr, tmp_path)
        detail_file = tmp_path / "detail" / "result-1.json"
        assert detail_file.exists()
        data = json.loads(detail_file.read_text(encoding="utf-8"))
        assert data["item_id"] == "result-1"


class TestSaveMarkdownReport:
    def test_creates_summary_md(self, tmp_path, sample_bench_result):
        sr = SuiteResult(
            suite_name="test",
            timestamp="2024-01-01",
            total_items=1,
            passed_items=1,
            total_score=0.85,
            avg_score=0.85,
            duration_sec=5.0,
            results=[sample_bench_result],
        )
        summary_path = save_markdown_report(sr, tmp_path)
        assert summary_path == tmp_path / "summary.md"
        content = summary_path.read_text(encoding="utf-8")
        assert "# Benchmark Report: test" in content
        assert "result-1" in content
        assert "PASS" in content

    def test_shows_fail_items(self, tmp_path):
        r = BenchResult(item_id="fail-1", passed=False, total_score=0.2, error="Timeout")
        sr = SuiteResult(
            suite_name="fail-test",
            timestamp="now",
            total_items=1,
            passed_items=0,
            total_score=0.2,
            avg_score=0.2,
            duration_sec=10.0,
            results=[r],
        )
        path = save_markdown_report(sr, tmp_path)
        content = path.read_text(encoding="utf-8")
        assert "FAIL" in content
        assert "Timeout" in content
