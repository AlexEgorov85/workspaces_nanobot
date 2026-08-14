from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from benchmarks.evaluator import (
    _aggregate_score,
    _call_llm_json,
    _check_file_content,
    _check_file_exists,
    _check_iterations,
    _check_keywords_exclude,
    _check_keywords_include,
    _check_llm_judge,
    _check_skills,
    _check_tools,
    _critical_checks,
    _resolve_path,
    evaluate,
)
from benchmarks.hooks import BenchmarkHook
from benchmarks.models import BenchExpect, CheckResult, EvalResult


@pytest.fixture
def hook() -> BenchmarkHook:
    h = BenchmarkHook()
    h.iterations = 5
    h._tool_names = {"exec", "glob"}
    h.skills = {"coding"}
    return h


class TestCheckTools:
    def test_no_expectations(self):
        result = _check_tools([], ["exec"])
        assert result.passed is True
        assert result.score == 1.0
        assert "No tool expectations" in result.detail

    def test_all_used(self):
        result = _check_tools(["exec", "glob"], ["exec", "glob", "read"])
        assert result.passed is True
        assert result.score == 1.0

    def test_some_missing(self):
        result = _check_tools(["exec", "missing_tool"], ["exec"])
        assert result.passed is False
        assert result.score == 0.0
        assert "missing_tool" in result.detail

    def test_empty_actual(self):
        result = _check_tools(["exec"], [])
        assert result.passed is False
        assert result.score == 0.0


class TestCheckSkills:
    def test_no_expectations(self):
        result = _check_skills([], {"coding"})
        assert result.passed is True
        assert result.score == 1.0

    def test_all_activated(self):
        result = _check_skills(["coding", "analysis"], {"coding", "analysis", "writing"})
        assert result.passed is True
        assert result.score == 1.0

    def test_some_missing(self):
        result = _check_skills(["coding", "missing"], {"coding"})
        assert result.passed is False
        assert result.score == 0.0

    def test_empty_actual(self):
        result = _check_skills(["coding"], set())
        assert result.passed is False


class TestCheckIterations:
    def test_zero_iterations(self):
        result = _check_iterations(10, 0)
        assert result.passed is False
        assert result.score == 0.0

    def test_within_limit(self):
        result = _check_iterations(10, 5)
        assert result.passed is True
        assert 0.0 < result.score <= 1.0

    def test_exactly_at_limit(self):
        result = _check_iterations(10, 10)
        assert result.passed is True

    def test_exceeded_limit(self):
        result = _check_iterations(10, 20)
        assert result.passed is False
        assert result.score == 0.5  # 10/20

    def test_score_floor(self):
        result = _check_iterations(10, 1000)
        # ratio = max(10/1000, 0.0) = 0.01
        assert result.score == 0.01

    def test_efficiency_minimum(self):
        # efficiency = 1.0 - (actual - 1) / (max * 2)
        # for actual=1, efficiency=1.0
        result = _check_iterations(10, 1)
        assert result.score == 1.0

    def test_efficiency_floor(self):
        # large actual close to limit gives ~0.1 min
        result = _check_iterations(10, 9)
        assert result.score >= 0.1


class TestCheckKeywordsInclude:
    def test_no_keywords(self):
        result = _check_keywords_include([], "hello world")
        assert result.passed is True
        assert result.score == 1.0

    def test_all_found(self):
        result = _check_keywords_include(["hello", "world"], "hello world")
        assert result.passed is True
        assert result.score == 1.0

    def test_case_insensitive(self):
        result = _check_keywords_include(["HELLO"], "Hello World")
        assert result.passed is True

    def test_some_missing(self):
        result = _check_keywords_include(["hello", "missing"], "hello world")
        assert result.passed is False
        assert result.score == 0.0

    def test_no_response(self):
        result = _check_keywords_include(["hello"], None)
        assert result.passed is False
        assert result.score == 0.0


class TestCheckKeywordsExclude:
    def test_no_keywords(self):
        result = _check_keywords_exclude([], "hello error")
        assert result.passed is True
        assert result.score == 1.0

    def test_none_found(self):
        result = _check_keywords_exclude(["error", "fail"], "hello world")
        assert result.passed is True
        assert result.score == 1.0

    def test_forbidden_found(self):
        result = _check_keywords_exclude(["error"], "hello error world")
        assert result.passed is False
        assert result.score == 0.0

    def test_no_response(self):
        result = _check_keywords_exclude(["error"], None)
        assert result.passed is True  # safe: no response = no forbidden words

    def test_case_insensitive(self):
        result = _check_keywords_exclude(["ERROR"], "hello error")
        assert result.passed is False


class TestCheckFileExists:
    def test_file_exists(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("content", encoding="utf-8")
        result = _check_file_exists("test.txt", tmp_path)
        assert result.passed is True
        assert result.score == 1.0

    def test_file_not_exists(self, tmp_path):
        result = _check_file_exists("nonexistent.txt", tmp_path)
        assert result.passed is False
        assert result.score == 0.0

    def test_absolute_path(self, tmp_path):
        f = tmp_path / "abs.txt"
        f.write_text("content", encoding="utf-8")
        result = _check_file_exists(str(f), None)
        assert result.passed is True

    def test_no_workspace(self):
        result = _check_file_exists("nonexistent.txt", None)
        assert result.passed is False


class TestCheckFileContent:
    def test_content_found(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello world", encoding="utf-8")
        result = _check_file_content("test.txt", "world", tmp_path)
        assert result.passed is True
        assert result.score == 1.0

    def test_content_not_found(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello world", encoding="utf-8")
        result = _check_file_content("test.txt", "missing", tmp_path)
        assert result.passed is False
        assert result.score == 0.0

    def test_file_not_exists(self, tmp_path):
        result = _check_file_content("nonexistent.txt", "content", tmp_path)
        assert result.passed is False
        assert result.score == 0.0

    def test_case_insensitive(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("Hello World", encoding="utf-8")
        result = _check_file_content("test.txt", "hello", tmp_path)
        assert result.passed is True


class TestCheckLlmJudge:
    def test_returns_stub(self, hook):
        expect = BenchExpect()
        result = _check_llm_judge(expect, "response", hook)
        assert result.check == "llm_judge"
        assert result.passed is False
        assert result.score == 0.0

    def test_empty_response_fails(self, hook):
        expect = BenchExpect(goal="some goal")
        result = _check_llm_judge(expect, "", hook)
        assert result.passed is False
        assert result.score == 0.0

    def test_no_goal_fails(self, hook):
        expect = BenchExpect()
        result = _check_llm_judge(expect, "response", hook)
        assert result.passed is False
        assert result.score == 0.0

    def test_judge_score_mapping(self, hook, monkeypatch):
        from benchmarks.models import BenchExpect
        expect = BenchExpect(goal="reach the goal")

        monkeypatch.setattr("benchmarks.evaluator._call_llm_json",
                            lambda prompt: {"score": 1.0, "reason": "done"})
        r = _check_llm_judge(expect, "good answer", hook)
        assert r.score == 1.0
        assert r.passed is True

        monkeypatch.setattr("benchmarks.evaluator._call_llm_json",
                            lambda prompt: {"score": 0.0, "reason": "nope"})
        r = _check_llm_judge(expect, "bad answer", hook)
        assert r.score == 0.0
        assert r.passed is False

    def test_judge_exception_fails(self, hook, monkeypatch):
        expect = BenchExpect(goal="reach the goal")

        def boom(prompt):
            raise RuntimeError("net down")
        monkeypatch.setattr("benchmarks.evaluator._call_llm_json", boom)
        r = _check_llm_judge(expect, "answer", hook)
        assert r.score == 0.0
        assert r.passed is False

    def test_judge_none_fails(self, hook, monkeypatch):
        expect = BenchExpect(goal="reach the goal")
        monkeypatch.setattr("benchmarks.evaluator._call_llm_json",
                            lambda prompt: None)
        r = _check_llm_judge(expect, "answer", hook)
        assert r.score == 0.0
        assert r.passed is False

    def test_judge_missing_score_field_fails(self, hook, monkeypatch):
        expect = BenchExpect(goal="reach the goal")
        monkeypatch.setattr("benchmarks.evaluator._call_llm_json",
                            lambda prompt: {"reason": "no score"})
        r = _check_llm_judge(expect, "answer", hook)
        assert r.score == 0.0
        assert r.passed is False

    def test_judge_non_numeric_score_fails(self, hook, monkeypatch):
        expect = BenchExpect(goal="reach the goal")
        monkeypatch.setattr("benchmarks.evaluator._call_llm_json",
                            lambda prompt: {"score": "high"})
        r = _check_llm_judge(expect, "answer", hook)
        assert r.score == 0.0
        assert r.passed is False


class TestCallLlmJson:
    def test_parses_markdown_wrapped_json(self, monkeypatch):
        class _FakeResp:
            def raise_for_status(self):
                return None
            def json(self):
                return {"choices": [{"message": {"content": '```json\n{"score": 1.0, "reason": "ok"}\n```'}}]}
        class _FakeClient:
            def __init__(self, *a, **k):
                self.post = lambda *a, **k: _FakeResp()
            def __enter__(self):
                return self
            def __exit__(self, *a, **k):
                return None
        monkeypatch.setattr("httpx.Client", _FakeClient)
        monkeypatch.setattr("lib.services.llm_config.resolve_llm_config",
                            lambda overrides=None: {"api_base": "http://x/v1", "api_key": "k", "model": "m"})
        result = _call_llm_json("prompt")
        assert result == {"score": 1.0, "reason": "ok"}

    def test_returns_none_on_empty_content(self, monkeypatch):
        class _FakeResp:
            def raise_for_status(self):
                return None
            def json(self):
                return {"choices": [{"message": {"content": ""}}]}
        class _FakeClient:
            def __init__(self, *a, **k):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *a, **k):
                return None
        monkeypatch.setattr("httpx.Client", _FakeClient)
        monkeypatch.setattr("lib.services.llm_config.resolve_llm_config",
                            lambda overrides=None: {"api_base": "http://x/v1", "api_key": "k", "model": "m"})
        assert _call_llm_json("prompt") is None


class TestResolvePath:
    def test_absolute(self):
        p = _resolve_path("/absolute/path.txt", "/workspace")
        assert p == Path("/absolute/path.txt")

    def test_relative_with_workspace(self):
        p = _resolve_path("relative/path.txt", "/workspace")
        assert p == Path("/workspace/relative/path.txt")

    def test_relative_without_workspace(self):
        p = _resolve_path("relative/path.txt", None)
        assert p == Path("relative/path.txt")


class TestAggregateScore:
    def test_empty(self):
        assert _aggregate_score([]) == 0.0

    def test_all_perfect(self):
        checks = [CheckResult("tools", True, 1.0, ""), CheckResult("skills", True, 1.0, "")]
        assert _aggregate_score(checks) == 1.0

    def test_mixed(self):
        checks = [CheckResult("tools", True, 1.0, ""), CheckResult("skills", False, 0.0, "")]
        assert _aggregate_score(checks) == 0.5


class TestCriticalChecks:
    def test_filters_critical(self):
        checks = [
            CheckResult("tools", True, 1.0, ""),
            CheckResult("skills", True, 1.0, ""),
            CheckResult("iterations", True, 1.0, ""),
            CheckResult("keywords_include", True, 1.0, ""),
            CheckResult("keywords_exclude", True, 1.0, ""),
        ]
        critical = _critical_checks(checks)
        names = {c.check for c in critical}
        assert names == {"tools", "keywords_include"}

    def test_file_checks_are_critical(self):
        checks = [
            CheckResult("file_exists", True, 1.0, ""),
            CheckResult("file_content", True, 1.0, ""),
            CheckResult("llm_judge", True, 0.5, ""),
        ]
        critical = _critical_checks(checks)
        assert len(critical) == 3


class TestEvaluate:
    def test_all_checks_pass(self, hook):
        expect = BenchExpect(
            tools=["exec", "glob"],
            skills=["coding"],
            keywords_include=["hello"],
            keywords_exclude=["error"],
            max_iterations=10,
        )
        result = evaluate(expect, "hello world", hook)
        assert result.passed is True
        assert result.total_score >= 0.5
        assert len(result.checks) == 5  # tools, skills, iterations, keywords_include, keywords_exclude

    def test_with_file_checks(self, hook, tmp_path):
        f = tmp_path / "output.txt"
        f.write_text("success", encoding="utf-8")
        expect = BenchExpect(
            tools=[],
            keywords_include=[],
            check_file="output.txt",
            check_file_content="success",
        )
        result = evaluate(expect, "done", hook, workspace=str(tmp_path))
        assert result.passed is True
        check_names = {c.check for c in result.checks}
        assert "file_exists" in check_names
        assert "file_content" in check_names

    def test_critical_check_fails(self, hook):
        expect = BenchExpect(
            tools=["exec"],
            keywords_include=["missing_keyword"],
        )
        result = evaluate(expect, "response without keyword", hook)
        assert result.passed is False

    def test_llm_judge_mode(self, hook):
        expect = BenchExpect(
            tools=[],
            keywords_include=[],
            match_type="llm_judge",
        )
        result = evaluate(expect, "response", hook)
        check_names = {c.check for c in result.checks}
        assert "llm_judge" in check_names

    def test_empty_expectations(self, hook):
        expect = BenchExpect()
        result = evaluate(expect, "response", hook)
        assert result.passed is True
        # iterations check gives ~0.93 because 5 iterations out of 30 max
        assert result.total_score > 0.9
