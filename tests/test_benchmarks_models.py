from __future__ import annotations

from benchmarks.models import (
    BenchExpect,
    BenchItem,
    BenchResult,
    BenchStep,
    BenchSuite,
    CheckResult,
    EvalResult,
    StepResult,
    SuiteResult,
)


class TestBenchExpect:
    def test_defaults(self):
        e = BenchExpect()
        assert e.tools == []
        assert e.skills == []
        assert e.keywords_include == []
        assert e.keywords_exclude == []
        assert e.max_iterations == 30
        assert e.match_type == "keyword"
        assert e.check_file is None
        assert e.check_file_content is None

    def test_custom_values(self):
        e = BenchExpect(
            tools=["exec"],
            skills=["coding"],
            keywords_include=["hello"],
            keywords_exclude=["error"],
            max_iterations=5,
            match_type="llm_judge",
            check_file="out.txt",
            check_file_content="done",
        )
        assert e.tools == ["exec"]
        assert e.skills == ["coding"]
        assert e.keywords_include == ["hello"]
        assert e.max_iterations == 5
        assert e.match_type == "llm_judge"
        assert e.check_file == "out.txt"
        assert e.check_file_content == "done"


class TestBenchStep:
    def test_defaults(self):
        s = BenchStep(step=1, question="Q?")
        assert s.step == 1
        assert s.question == "Q?"
        assert s.weight == 1.0
        assert isinstance(s.expect, BenchExpect)


class TestBenchItem:
    def test_defaults_single(self):
        item = BenchItem(id="i1", name="Test", difficulty=5, category="gen", type="single")
        assert item.question is None
        assert item.steps == []
        assert isinstance(item.expect, BenchExpect)
        assert item.context_files == []
        assert item.max_iterations == 30
        assert item.timeout == 60
        assert item.new_session is True

    def test_hash(self):
        item1 = BenchItem(id="i1", name="A", difficulty=1, category="c", type="single")
        item2 = BenchItem(id="i1", name="B", difficulty=2, category="c", type="single")
        item3 = BenchItem(id="i3", name="C", difficulty=3, category="c", type="single")
        assert hash(item1) == hash(item2)
        assert hash(item1) != hash(item3)
        assert len({item1, item2, item3}) == 2


class TestBenchSuite:
    def test_defaults(self):
        suite = BenchSuite(name="suite1", items=[])
        assert suite.name == "suite1"
        assert suite.items == []
        assert suite.tags == []

    def test_with_items(self):
        items = [BenchItem(id="a", name="A", difficulty=1, category="c", type="single")]
        suite = BenchSuite(name="s", items=items, tags=["simple"])
        assert len(suite.items) == 1
        assert suite.tags == ["simple"]


class TestCheckResult:
    def test_creation(self):
        c = CheckResult("tools", True, 1.0, "All good")
        assert c.check == "tools"
        assert c.passed is True
        assert c.score == 1.0
        assert c.detail == "All good"


class TestEvalResult:
    def test_defaults(self):
        r = EvalResult(passed=True, total_score=0.9)
        assert r.passed is True
        assert r.total_score == 0.9
        assert r.checks == []


class TestStepResult:
    def test_defaults(self):
        r = StepResult(step=1, weight=0.5, passed=True, score=0.8, response="ok", tools_used=[], iterations=3, duration_sec=1.0)
        assert r.step == 1
        assert r.weight == 0.5
        assert r.passed is True
        assert r.score == 0.8
        assert r.response == "ok"
        assert r.tools_used == []
        assert r.iterations == 3
        assert r.duration_sec == 1.0
        assert r.details == {}
        assert r.checks == []


class TestBenchResult:
    def test_defaults(self):
        r = BenchResult(item_id="r1")
        assert r.item_id == "r1"
        assert r.item_name == ""
        assert r.difficulty == 5
        assert r.passed is False
        assert r.total_score == 0.0
        assert r.response is None
        assert r.error is None
        assert r.tools_used == []
        assert r.skills_activated == []
        assert r.total_iterations == 0
        assert r.duration_sec == 0.0
        assert r.steps == []
        assert r.checks == []
        assert r.details == {}
        assert r.llm_judge_score is None


class TestSuiteResult:
    def test_creation(self):
        results = [BenchResult(item_id="r1")]
        r = SuiteResult(
            suite_name="test",
            timestamp="2024-01-01T00:00:00",
            total_items=1,
            passed_items=1,
            total_score=0.9,
            avg_score=0.9,
            duration_sec=5.0,
            results=results,
            config={"mode": "test"},
        )
        assert r.suite_name == "test"
        assert r.total_items == 1
        assert r.passed_items == 1
        assert r.total_score == 0.9
        assert r.avg_score == 0.9
        assert r.duration_sec == 5.0
        assert len(r.results) == 1
        assert r.config == {"mode": "test"}
