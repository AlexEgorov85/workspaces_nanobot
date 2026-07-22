from __future__ import annotations

from benchmarks.models import BenchExpect, BenchItem, CheckResult, EvalResult, StepResult
from benchmarks.scorer import (
    _find_check_score,
    _weighted_score,
    score_item,
    score_multi_step,
    score_single,
    score_step,
)


class TestWeightedScore:
    def test_empty_list(self):
        assert _weighted_score([]) == 0.0

    def test_single_check(self):
        checks = [CheckResult("tools", True, 1.0, "")]
        assert _weighted_score(checks) == 1.0

    def test_multiple_checks(self):
        checks = [
            CheckResult("tools", True, 1.0, ""),
            CheckResult("keywords_include", True, 1.0, ""),
        ]
        total_w = 0.20 + 0.15
        expected = (1.0 * 0.20 + 1.0 * 0.15) / total_w
        assert _weighted_score(checks) == expected

    def test_mixed_scores(self):
        checks = [
            CheckResult("tools", True, 1.0, ""),
            CheckResult("keywords_include", False, 0.0, ""),
        ]
        total_w = 0.20 + 0.15
        expected = (1.0 * 0.20 + 0.0 * 0.15) / total_w
        assert _weighted_score(checks) == expected

    def test_unknown_check_type(self):
        checks = [CheckResult("unknown_check", True, 0.5, "")]
        assert _weighted_score(checks) == 0.5  # default weight 0.10

    def test_partial_score(self):
        checks = [CheckResult("iterations", False, 0.6, "")]
        assert _weighted_score(checks) == 0.6


class TestFindCheckScore:
    def test_found(self):
        checks = [CheckResult("llm_judge", True, 0.5, "")]
        assert _find_check_score(checks, "llm_judge") == 0.5

    def test_not_found(self):
        checks = [CheckResult("tools", True, 1.0, "")]
        assert _find_check_score(checks, "llm_judge") is None

    def test_first_match_returned(self):
        checks = [
            CheckResult("llm_judge", True, 0.3, ""),
            CheckResult("llm_judge", True, 0.9, ""),
        ]
        assert _find_check_score(checks, "llm_judge") == 0.3


class TestScoreItem:
    def test_simple_item(self, sample_item_single, sample_eval_result):
        result = score_item(sample_item_single, sample_eval_result)
        assert result.item_id == "test-1"
        assert result.item_name == "Test item"
        assert result.difficulty == 3
        assert result.passed is True
        assert result.total_score > 0


class TestScoreSingle:
    def test_basic(self, sample_item_single, sample_eval_result):
        result = score_single(
            sample_item_single,
            sample_eval_result,
            response="42",
            tools_used=["exec"],
            skills_activated={"coding"},
            iterations=5,
            duration_sec=2.0,
        )
        assert result.item_id == "test-1"
        assert result.passed is True
        assert result.response == "42"
        assert result.tools_used == ["exec"]
        assert result.skills_activated == ["coding"]
        assert result.total_iterations == 5
        assert result.duration_sec == 2.0
        assert result.llm_judge_score is None

    def test_with_skills_none(self, sample_item_single, sample_eval_result):
        result = score_single(sample_item_single, sample_eval_result)
        assert result.skills_activated == []

    def test_score_rounded(self, sample_item_single):
        checks = [CheckResult("tools", True, 0.33333, "")]
        eval_result = EvalResult(passed=True, total_score=0.33333, checks=checks)
        result = score_single(sample_item_single, eval_result)
        assert result.total_score == 0.3333  # rounded to 4 decimal


class TestScoreStep:
    def test_basic(self):
        checks = [CheckResult("tools", True, 1.0, "")]
        eval_result = EvalResult(passed=True, total_score=1.0, checks=checks)
        result = score_step(1, 0.5, eval_result, response="ok", tools_used=["exec"], iterations=3, duration_sec=1.0)
        assert result.step == 1
        assert result.weight == 0.5
        assert result.passed is True
        assert result.response == "ok"
        assert result.tools_used == ["exec"]
        assert result.iterations == 3
        assert result.duration_sec == 1.0


class TestScoreMultiStep:
    def test_empty_steps(self):
        item = BenchItem(id="m", name="M", difficulty=5, category="c", type="multi_step")
        result = score_multi_step(item, [])
        assert result.passed is False
        assert result.total_score == 0.0

    def test_all_passed(self, sample_item_multi_step, sample_step_results):
        result = score_multi_step(sample_item_multi_step, sample_step_results)
        assert result.passed is True
        assert result.total_score > 0
        assert result.item_id == "multi-1"
        assert result.total_iterations == 5
        assert result.duration_sec == 8.0
        assert "exec" in result.tools_used
        assert "glob" in result.tools_used

    def test_some_failed(self, sample_item_multi_step):
        results = [
            StepResult(step=1, weight=0.5, passed=True, score=0.9, response="ok", tools_used=["exec"], iterations=2, duration_sec=1.0),
            StepResult(step=2, weight=0.5, passed=False, score=0.0, response="fail", tools_used=[], iterations=5, duration_sec=2.0),
        ]
        result = score_multi_step(sample_item_multi_step, results)
        assert result.passed is False
        assert result.total_score < 0.5

    def test_score_formula(self, sample_item_multi_step):
        """final = weighted_sum * 0.8 + completeness * 0.2"""
        results = [
            StepResult(step=1, weight=1.0, passed=True, score=1.0, response="ok", tools_used=["exec"], iterations=1, duration_sec=1.0),
        ]
        result = score_multi_step(sample_item_multi_step, results)
        expected = 1.0 * 0.8 + 1.0 * 0.2
        assert result.total_score == expected

    def test_zero_total_weight_normalized(self):
        item = BenchItem(id="m", name="M", difficulty=5, category="c", type="multi_step")
        results = [
            StepResult(step=1, weight=0.0, passed=True, score=0.5, response="ok", tools_used=[], iterations=1, duration_sec=1.0),
        ]
        result = score_multi_step(item, results)
        # total_weight = 0 -> normalized to 1.0
        # weighted_sum = 0.5 * 0.0 = 0.0
        # final = 0.0 * 0.8 + 1.0 * 0.2 = 0.2
        assert result.total_score == 0.2
