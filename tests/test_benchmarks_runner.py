from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _build_suite(items):
    """Create a BenchSuite-like object with given items."""
    from benchmarks.models import BenchSuite
    return BenchSuite(name="test-suite", items=items, tags=[])


def _make_item(
    item_id: str = "test-1",
    difficulty: int = 5,
    category: str = "general",
    type_: str = "single",
    question: str = "Hello?",
    max_iterations: int = 30,
    timeout: int = 60,
    check_file: str | None = None,
    steps=None,
):
    from benchmarks.models import BenchExpect, BenchItem
    return BenchItem(
        id=item_id,
        name=item_id,
        difficulty=difficulty,
        category=category,
        type=type_,
        question=question,
        expect=BenchExpect(check_file=check_file),
        max_iterations=max_iterations,
        timeout=timeout,
        steps=steps or [],
    )


# ===================================================================
# _detect_run_id
# ===================================================================

class TestDetectRunId:
    def test_returns_formatted_timestamp(self):
        from benchmarks.runner import _detect_run_id
        import re
        result = _detect_run_id()
        assert re.match(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}", result)


# ===================================================================
# _generate_run_id
# ===================================================================

class TestGenerateRunId:
    def test_returns_uuid_hex(self):
        from benchmarks.runner import _generate_run_id
        result = _generate_run_id()
        assert len(result) == 32
        assert all(c in "0123456789abcdef" for c in result)

    def test_is_unique(self):
        from benchmarks.runner import _generate_run_id
        assert _generate_run_id() != _generate_run_id()


# ===================================================================
# _parse_args
# ===================================================================

class TestParseArgs:
    def test_defaults(self):
        from benchmarks.runner import _parse_args
        ns = _parse_args([])
        assert ns.mode == "all"
        assert ns.verbose is False
        assert ns.dry_run is False
        assert ns.tags is None
        assert ns.category is None
        assert ns.difficulty is None
        assert ns.compare is None

    def test_verbose_flag(self):
        from benchmarks.runner import _parse_args
        ns = _parse_args(["--verbose"])
        assert ns.verbose is True

    def test_dry_run_flag(self):
        from benchmarks.runner import _parse_args
        ns = _parse_args(["--dry-run"])
        assert ns.dry_run is True

    def test_tags(self):
        from benchmarks.runner import _parse_args
        ns = _parse_args(["--tags", "simple", "hard"])
        assert ns.tags == ["simple", "hard"]

    def test_category(self):
        from benchmarks.runner import _parse_args
        ns = _parse_args(["--category", "code", "math"])
        assert ns.category == ["code", "math"]

    def test_difficulty(self):
        from benchmarks.runner import _parse_args
        ns = _parse_args(["--difficulty", "1-3"])
        assert ns.difficulty == "1-3"

    def test_mode(self):
        from benchmarks.runner import _parse_args
        ns = _parse_args(["--mode", "single"])
        assert ns.mode == "single"

    def test_mode_multi_step(self):
        from benchmarks.runner import _parse_args
        ns = _parse_args(["--mode", "multi_step"])
        assert ns.mode == "multi_step"

    def test_items(self):
        from benchmarks.runner import _parse_args
        ns = _parse_args(["--items", "/custom/path.yaml"])
        assert ns.items == "/custom/path.yaml"

    def test_db(self):
        from benchmarks.runner import _parse_args
        ns = _parse_args(["--db", "postgresql://user@host/db"])
        assert ns.db == "postgresql://user@host/db"

    def test_compare(self):
        from benchmarks.runner import _parse_args
        ns = _parse_args(["--compare", "run1", "run2"])
        assert ns.compare == ["run1", "run2"]

    def test_model(self):
        from benchmarks.runner import _parse_args
        ns = _parse_args(["--model", "phi4:latest"])
        assert ns.model == "phi4:latest"

    def test_config(self):
        from benchmarks.runner import _parse_args
        ns = _parse_args(["--config", "/path/to/config.json"])
        assert ns.config == "/path/to/config.json"

    def test_output(self):
        from benchmarks.runner import _parse_args
        ns = _parse_args(["--output", "/custom/output"])
        assert ns.output == "/custom/output"


# ===================================================================
# _filter_items
# ===================================================================

class TestFilterItems:
    def test_mode_single_filters_multi(self):
        from benchmarks.runner import _filter_items
        suite = _build_suite([
            _make_item("s1", type_="single"),
            _make_item("m1", type_="multi_step"),
        ])
        result = _filter_items(suite, tags=None, category=None, difficulty=None, mode="single")
        assert len(result.items) == 1
        assert result.items[0].id == "s1"

    def test_mode_multi_step_filters_single(self):
        from benchmarks.runner import _filter_items
        suite = _build_suite([
            _make_item("s1", type_="single"),
            _make_item("m1", type_="multi_step"),
        ])
        result = _filter_items(suite, tags=None, category=None, difficulty=None, mode="multi_step")
        assert len(result.items) == 1
        assert result.items[0].id == "m1"

    def test_mode_all_keeps_all(self):
        from benchmarks.runner import _filter_items
        suite = _build_suite([
            _make_item("s1", type_="single"),
            _make_item("m1", type_="multi_step"),
        ])
        result = _filter_items(suite, tags=None, category=None, difficulty=None, mode="all")
        assert len(result.items) == 2

    def test_tag_simple(self):
        from benchmarks.runner import _filter_items
        suite = _build_suite([
            _make_item("easy", difficulty=1),
            _make_item("med", difficulty=5),
            _make_item("hard", difficulty=10),
        ])
        result = _filter_items(suite, tags=["simple"], category=None, difficulty=None, mode="all")
        assert [i.id for i in result.items] == ["easy"]

    def test_tag_medium(self):
        from benchmarks.runner import _filter_items
        suite = _build_suite([
            _make_item("easy", difficulty=1),
            _make_item("med", difficulty=5),
            _make_item("hard", difficulty=10),
        ])
        result = _filter_items(suite, tags=["medium"], category=None, difficulty=None, mode="all")
        assert [i.id for i in result.items] == ["med"]

    def test_tag_hard(self):
        from benchmarks.runner import _filter_items
        suite = _build_suite([
            _make_item("easy", difficulty=1),
            _make_item("med", difficulty=5),
            _make_item("hard", difficulty=10),
        ])
        result = _filter_items(suite, tags=["hard"], category=None, difficulty=None, mode="all")
        assert [i.id for i in result.items] == ["hard"]

    def test_multiple_tags_dedup(self):
        from benchmarks.runner import _filter_items
        suite = _build_suite([
            _make_item("easy", difficulty=1),
            _make_item("both", difficulty=5),
        ])
        result = _filter_items(suite, tags=["simple", "medium"], category=None, difficulty=None, mode="all")
        assert [i.id for i in result.items] == ["easy", "both"]

    def test_unknown_tag_returns_empty(self):
        from benchmarks.runner import _filter_items
        suite = _build_suite([_make_item("a", difficulty=1)])
        result = _filter_items(suite, tags=["unknown"], category=None, difficulty=None, mode="all")
        assert len(result.items) == 0

    def test_category_filter(self):
        from benchmarks.runner import _filter_items
        suite = _build_suite([
            _make_item("code", category="code"),
            _make_item("math", category="math"),
        ])
        result = _filter_items(suite, tags=None, category=["code"], difficulty=None, mode="all")
        assert [i.id for i in result.items] == ["code"]

    def test_difficulty_range(self):
        from benchmarks.runner import _filter_items
        suite = _build_suite([
            _make_item("a", difficulty=1),
            _make_item("b", difficulty=5),
            _make_item("c", difficulty=10),
        ])
        result = _filter_items(suite, tags=None, category=None, difficulty="4-6", mode="all")
        assert [i.id for i in result.items] == ["b"]

    def test_preserves_suite_name_and_tags(self):
        from benchmarks.runner import _filter_items
        suite = _build_suite([_make_item("a")])
        result = _filter_items(suite, tags=None, category=None, difficulty=None, mode="all")
        assert result.name == "test-suite"


# ===================================================================
# _format_checks_failures
# ===================================================================

class TestFormatChecksFailures:
    def test_all_passed_returns_empty(self):
        from benchmarks.runner import _format_checks_failures
        from benchmarks.models import CheckResult
        checks = [CheckResult(check="tools", passed=True, score=1.0)]
        assert _format_checks_failures(checks) == ""

    def test_some_failed(self):
        from benchmarks.runner import _format_checks_failures
        from benchmarks.models import CheckResult
        checks = [
            CheckResult(check="tools", passed=True, score=1.0),
            CheckResult(check="keywords_include", passed=False, score=0.0),
        ]
        result = _format_checks_failures(checks)
        assert "keywords_include" in result
        assert "✗" in result

    def test_all_failed(self):
        from benchmarks.runner import _format_checks_failures
        from benchmarks.models import CheckResult
        checks = [
            CheckResult(check="tools", passed=False, score=0.0),
            CheckResult(check="file_exists", passed=False, score=0.0),
        ]
        result = _format_checks_failures(checks)
        assert "tools" in result
        assert "file_exists" in result

    def test_empty_list(self):
        from benchmarks.runner import _format_checks_failures
        assert _format_checks_failures([]) == ""


# ===================================================================
# _validate_items
# ===================================================================

class TestValidateItems:
    def test_duplicate_ids(self):
        from benchmarks.runner import _validate_items
        suite = _build_suite([
            _make_item("dup"),
            _make_item("dup"),
        ])
        warnings = _validate_items(suite)
        assert any("DUPLICATE ID" in w for w in warnings)

    def test_single_item_no_question(self):
        from benchmarks.runner import _validate_items
        from benchmarks.models import BenchItem, BenchExpect
        item = BenchItem(id="no-q", name="x", difficulty=5, category="g", type="single")
        suite = _build_suite([item])
        warnings = _validate_items(suite)
        assert any("question" in w.lower() or "no question" in w.lower() for w in warnings)

    def test_multi_step_no_steps(self):
        from benchmarks.runner import _validate_items
        item = _make_item("multi", type_="multi_step", steps=[])
        suite = _build_suite([item])
        warnings = _validate_items(suite)
        assert any("steps" in w.lower() for w in warnings)

    def test_difficulty_out_of_range(self):
        from benchmarks.runner import _validate_items
        suite = _build_suite([_make_item("bad", difficulty=15)])
        warnings = _validate_items(suite)
        assert any("difficulty" in w.lower() for w in warnings)

    def test_max_iterations_zero(self):
        from benchmarks.runner import _validate_items
        suite = _build_suite([_make_item("bad", max_iterations=0)])
        warnings = _validate_items(suite)
        assert any("max_iterations" in w.lower() for w in warnings)

    def test_timeout_zero(self):
        from benchmarks.runner import _validate_items
        suite = _build_suite([_make_item("bad", timeout=0)])
        warnings = _validate_items(suite)
        assert any("timeout" in w.lower() for w in warnings)

    def test_multi_step_zero_weight(self):
        from benchmarks.runner import _validate_items
        from benchmarks.models import BenchStep, BenchExpect
        step = BenchStep(step=1, question="q", weight=0.0)
        suite = _build_suite([_make_item("multi", type_="multi_step", steps=[step])])
        warnings = _validate_items(suite)
        assert any("weight" in w.lower() for w in warnings)

    def test_valid_item_no_warnings(self):
        from benchmarks.runner import _validate_items
        suite = _build_suite([_make_item("ok")])
        warnings = _validate_items(suite)
        assert len(warnings) == 0


# ===================================================================
# _print_summary
# ===================================================================

class TestPrintSummary:
    def test_output_format(self, capsys):
        from benchmarks.runner import _print_summary
        from benchmarks.models import BenchResult, CheckResult, SuiteResult
        result = SuiteResult(
            suite_name="test",
            timestamp="2026-01-01",
            total_items=2,
            passed_items=2,
            total_score=1.8,
            avg_score=0.9,
            duration_sec=10.5,
            results=[
                BenchResult(item_id="a", passed=True, total_score=1.0,
                            total_iterations=3, duration_sec=5.0,
                            difficulty=5,
                            checks=[CheckResult(check="tools", passed=True, score=1.0)]),
                BenchResult(item_id="b", passed=True, total_score=0.8,
                            total_iterations=5, duration_sec=5.5,
                            difficulty=2,
                            checks=[CheckResult(check="tools", passed=True, score=1.0)]),
            ],
        )
        _print_summary(result)
        captured = capsys.readouterr().out
        assert "BENCHMARK COMPLETE" in captured
        assert "PASS" in captured
        assert "2 / 2" in captured
        assert "90.00%" in captured

    def test_shows_failed_items(self, capsys):
        from benchmarks.runner import _print_summary
        from benchmarks.models import BenchResult, CheckResult, SuiteResult
        result = SuiteResult(
            suite_name="test",
            timestamp="2026-01-01",
            total_items=1,
            passed_items=0,
            total_score=0.0,
            avg_score=0.0,
            duration_sec=1.0,
            results=[
                BenchResult(item_id="fail", passed=False, total_score=0.0,
                            total_iterations=1, duration_sec=1.0,
                            difficulty=5,
                            checks=[CheckResult(check="tools", passed=False, score=0.0)]),
            ],
        )
        _print_summary(result)
        captured = capsys.readouterr().out
        assert "FAIL" in captured
        assert "1 item(s) FAILED" in captured


# ===================================================================
# _cleanup_item
# ===================================================================

class TestCleanupItem:
    def test_deletes_check_file(self, tmp_path):
        from benchmarks.runner import _cleanup_item
        f = tmp_path / "out.txt"
        f.write_text("data")
        item = _make_item("a", check_file="out.txt")
        bot = MagicMock()
        bot._loop.workspace = str(tmp_path)
        _cleanup_item(item, bot)
        assert not f.exists()

    def test_multi_step_items_cleaned(self, tmp_path):
        from benchmarks.runner import _cleanup_item
        from benchmarks.models import BenchStep, BenchExpect
        step = BenchStep(step=1, question="q", expect=BenchExpect(check_file="out.txt"))
        item = _make_item("multi", type_="multi_step", steps=[step])
        f = tmp_path / "out.txt"
        f.write_text("data")
        bot = MagicMock()
        bot._loop.workspace = str(tmp_path)
        _cleanup_item(item, bot)
        assert not f.exists()

    def test_dedup_files(self, tmp_path):
        from benchmarks.runner import _cleanup_item
        from benchmarks.models import BenchExpect
        item = _make_item("a", check_file="out.txt")
        item.expect.check_file_content = "expected"
        f = tmp_path / "out.txt"
        f.write_text("data")
        bot = MagicMock()
        bot._loop.workspace = str(tmp_path)
        _cleanup_item(item, bot)
        assert not f.exists()


# ===================================================================
# cleanup_old_runs
# ===================================================================

class TestCleanupOldRuns:
    def test_removes_oldest_beyond_keep(self, tmp_path):
        from benchmarks.runner import cleanup_old_runs
        for name in ["run-1", "run-2", "run-3"]:
            (tmp_path / name).mkdir()
            (tmp_path / name / "summary.json").write_text("{}")
        # run-1 oldest, run-3 newest (mtimes may be equal on fast FS)
        import os
        base = tmp_path
        for i, name in enumerate(["run-1", "run-2", "run-3"]):
            p = base / name
            os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))
        removed = cleanup_old_runs(keep_last=2, runs_dir=base)
        assert removed == 1
        assert not (base / "run-1").exists()
        assert (base / "run-2").exists()
        assert (base / "run-3").exists()

    def test_keep_all_when_keep_last_gt_count(self, tmp_path):
        from benchmarks.runner import cleanup_old_runs
        (tmp_path / "run-1").mkdir()
        removed = cleanup_old_runs(keep_last=10, runs_dir=tmp_path)
        assert removed == 0
        assert (tmp_path / "run-1").exists()

    def test_zero_keep_disables(self, tmp_path):
        from benchmarks.runner import cleanup_old_runs
        (tmp_path / "run-1").mkdir()
        removed = cleanup_old_runs(keep_last=0, runs_dir=tmp_path)
        assert removed == 0
        assert (tmp_path / "run-1").exists()

    def test_missing_dir_noop(self, tmp_path):
        from benchmarks.runner import cleanup_old_runs
        assert cleanup_old_runs(keep_last=5, runs_dir=tmp_path / "nope") == 0


# ===================================================================
# _do_compare
# ===================================================================

class TestDoCompare:
    def test_loads_and_compares(self, tmp_path, capsys):
        from benchmarks.runner import _do_compare
        run1 = tmp_path / "run1"
        run1.mkdir()
        (run1 / "summary.json").write_text(
            '{"total_score": 0.7, "results": [{"item_id": "a", "total_score": 0.7}]}'
        )
        run2 = tmp_path / "run2"
        run2.mkdir()
        (run2 / "summary.json").write_text(
            '{"total_score": 0.9, "results": [{"item_id": "a", "total_score": 0.9}]}'
        )
        args = argparse.Namespace(compare=[str(run1), str(run2)])
        _do_compare(args)
        captured = capsys.readouterr().out
        assert "COMPARISON REPORT" in captured
        assert "a" in captured

    def test_path_not_found(self, capsys):
        from benchmarks.runner import _do_compare
        args = argparse.Namespace(compare=["/nonexistent/run1", "/nonexistent/run2"])
        _do_compare(args)
        captured = capsys.readouterr().out
        assert "Could not load" in captured


# ===================================================================
# _run_item
# ===================================================================

class TestRunItem:
    @pytest.mark.asyncio
    async def test_dispatches_single(self):
        from benchmarks.runner import _run_item
        item = _make_item("a", type_="single")
        bot = MagicMock()
        with patch("benchmarks.runner._run_single", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = "single_result"
            result = await _run_item(item, "run-1", bot, False)
            assert result == "single_result"
            mock_run.assert_called_once_with(item, "run-1", bot, False)

    @pytest.mark.asyncio
    async def test_dispatches_multi_step(self):
        from benchmarks.runner import _run_item
        item = _make_item("a", type_="multi_step", steps=[])
        bot = MagicMock()
        with patch("benchmarks.runner._run_multi_step", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = "multi_result"
            result = await _run_item(item, "run-1", bot, False)
            assert result == "multi_result"
            mock_run.assert_called_once_with(item, "run-1", bot, False)

    @pytest.mark.asyncio
    async def test_cleanup_called_on_error(self):
        from benchmarks.runner import _run_item
        item = _make_item("a", type_="single")
        bot = MagicMock()
        bot._loop.workspace = "/tmp"
        with patch("benchmarks.runner._run_single", side_effect=ValueError("fail")):
            with pytest.raises(ValueError):
                await _run_item(item, "run-1", bot, False)


# ===================================================================
# _run_single  (lightweight — mocks bot.run, evaluate, score)
# ===================================================================

class TestRunSingle:
    @pytest.mark.asyncio
    async def test_success(self):
        from benchmarks.runner import _run_single
        item = _make_item("a", type_="single")
        bot = MagicMock()
        bot.run = AsyncMock(return_value=MagicMock(content="ok", tools_used=["read_file"]))
        bot._loop.sessions.delete_session = AsyncMock()
        bot._loop.workspace = "/ws"

        with patch("benchmarks.runner.evaluate") as mock_eval:
            mock_eval.return_value = MagicMock(passed=True, total_score=1.0, checks=[])
            with patch("benchmarks.runner.score_single") as mock_score:
                mock_score.return_value = MagicMock(item_id="a", passed=True, total_score=1.0)
                result = await _run_single(item, "run-1", bot, False)

        assert result.item_id == "a"
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_bot_error(self):
        from benchmarks.runner import _run_single
        item = _make_item("a", type_="single")
        bot = MagicMock()
        bot.run.side_effect = RuntimeError("agent crash")
        bot._loop.sessions.delete_session = MagicMock()

        result = await _run_single(item, "run-1", bot, False)
        assert result.passed is False
        assert result.total_score == 0.0
        assert "agent crash" in (result.error or "")

    @pytest.mark.asyncio
    async def test_none_result(self):
        from benchmarks.runner import _run_single
        item = _make_item("a", type_="single")
        bot = MagicMock()
        bot.run.return_value = None

        with patch("benchmarks.runner.evaluate") as mock_eval:
            mock_eval.return_value = MagicMock(passed=True, total_score=1.0, checks=[])
            with patch("benchmarks.runner.score_single") as mock_score:
                mock_score.return_value = MagicMock(item_id="a", passed=True, total_score=1.0)
                result = await _run_single(item, "run-1", bot, False)
                assert result.item_id == "a"


# ===================================================================
# _run_multi_step  (lightweight — mocks bot.run, evaluate, score)
# ===================================================================

class TestRunMultiStep:
    @pytest.mark.asyncio
    async def test_success(self):
        from benchmarks.runner import _run_multi_step
        from benchmarks.models import BenchStep, BenchExpect
        steps = [
            BenchStep(step=1, question="q1", weight=1.0),
            BenchStep(step=2, question="q2", weight=1.0),
        ]
        item = _make_item("multi", type_="multi_step", steps=steps)
        bot = MagicMock()
        bot.run.return_value = MagicMock(content="ok", tools_used=[])
        bot._loop.sessions.delete_session = MagicMock()
        bot._loop.workspace = "/ws"

        with patch("benchmarks.runner.evaluate") as mock_eval:
            mock_eval.return_value = MagicMock(passed=True, total_score=1.0, checks=[])
            with patch("benchmarks.runner.score_step") as mock_ss:
                mock_ss.return_value = MagicMock(step=1, passed=True, score=1.0, checks=[])
                with patch("benchmarks.runner.score_multi_step") as mock_ms:
                    mock_ms.return_value = MagicMock(item_id="multi", passed=True, total_score=1.0)
                    result = await _run_multi_step(item, "run-1", bot, False)

        assert result.passed is True
        assert bot.run.call_count == 2

    @pytest.mark.asyncio
    async def test_step_error_continues(self):
        from benchmarks.runner import _run_multi_step
        from benchmarks.models import BenchStep
        steps = [BenchStep(step=1, question="q1", weight=1.0)]
        item = _make_item("multi", type_="multi_step", steps=steps)
        bot = MagicMock()
        bot.run.side_effect = RuntimeError("step fail")
        bot._loop.sessions.delete_session = MagicMock()

        with patch("benchmarks.runner.evaluate") as mock_eval:
            mock_eval.return_value = MagicMock(passed=True, total_score=1.0, checks=[])
            with patch("benchmarks.runner.score_multi_step") as mock_ms:
                mock_ms.return_value = MagicMock(item_id="multi", passed=False, total_score=0.0)
                result = await _run_multi_step(item, "run-1", bot, False)

        assert result.passed is False


# ===================================================================
# main  /  main_async
# ===================================================================

class TestMainAsync:
    @pytest.mark.asyncio
    async def test_compare_mode_returns_zero(self):
        from benchmarks.runner import main_async
        with patch("benchmarks.runner._do_compare") as mock_cmp:
            rc = await main_async(["--compare", "a", "b"])
            assert rc == 0
            mock_cmp.assert_called_once()

    @pytest.mark.asyncio
    async def test_dry_run_returns_zero(self):
        from benchmarks.runner import main_async
        with patch("benchmarks.runner.load_benchmark") as mock_load:
            mock_load.return_value = _build_suite([_make_item("a")])
            rc = await main_async(["--dry-run"])
            assert rc == 0

    @pytest.mark.asyncio
    async def test_no_items_after_filter_returns_zero(self):
        from benchmarks.runner import main_async
        with patch("benchmarks.runner.load_benchmark") as mock_load:
            mock_load.return_value = _build_suite([])
            rc = await main_async([])
            assert rc == 0

    @pytest.mark.asyncio
    async def test_load_error_returns_one(self):
        from benchmarks.runner import main_async
        with patch("benchmarks.runner.load_benchmark", side_effect=FileNotFoundError("no file")):
            rc = await main_async([])
            assert rc == 1

    @pytest.mark.asyncio
    async def test_all_pass_returns_zero(self):
        from benchmarks.runner import main_async
        suite = _build_suite([_make_item("a")])
        suite_result = MagicMock(
            total_items=1, passed_items=1, avg_score=1.0,
            duration_sec=1.0, results=[], config={},
        )

        with patch("benchmarks.runner.load_benchmark", return_value=suite):
            with patch("benchmarks.runner._run_suite", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = suite_result
                with patch("benchmarks.runner.save_json_report"):
                    with patch("benchmarks.runner.save_markdown_report"):
                        rc = await main_async(["--items", "/fake/path"])
                        assert rc == 0

    @pytest.mark.asyncio
    async def test_some_fail_returns_one(self):
        from benchmarks.runner import main_async
        suite = _build_suite([_make_item("a")])
        suite_result = MagicMock(
            total_items=1, passed_items=0, avg_score=0.0,
            duration_sec=1.0, results=[], config={},
        )
        with patch("benchmarks.runner.load_benchmark", return_value=suite):
            with patch("benchmarks.runner._run_suite", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = suite_result
                with patch("benchmarks.runner.save_json_report"):
                    with patch("benchmarks.runner.save_markdown_report"):
                        rc = await main_async(["--items", "/fake/path"])
                        assert rc == 1


class TestMain:
    def test_calls_main_async(self):
        from benchmarks.runner import main
        async def fake_main_async(argv):
            return 42
        with patch("benchmarks.runner.main_async", side_effect=fake_main_async):
            rc = main(["test"])
            assert rc == 42
