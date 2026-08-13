from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add workspace to sys.path
_workspace_path = str(Path(__file__).resolve().parent.parent / "workspace")
if _workspace_path not in sys.path:
    sys.path.insert(0, _workspace_path)


@pytest.fixture(autouse=True)
def mock_utils_db():
    """Mock utils.db before importing benchmarks.db."""
    with (
        patch.dict("sys.modules"),
        patch("psycopg2.extras.Json", lambda x: x),
    ):
        import types

        utils_db = types.ModuleType("utils.db")
        utils_db.configure = MagicMock()
        utils_db.execute = MagicMock()
        utils_db.transaction = MagicMock()
        utils_db.fetchval = MagicMock()
        utils_db.fetch = MagicMock()
        sys.modules["utils"] = types.ModuleType("utils")
        sys.modules["utils.db"] = utils_db

        from benchmarks.db import BenchmarkDB, _is_greenplum, _db_ok

        yield {
            "utils_db": utils_db,
            "BenchmarkDB": BenchmarkDB,
            "_is_greenplum": _is_greenplum,
            "_db_ok": _db_ok,
        }


class TestDBModule:
    def test_db_ok_true(self, mock_utils_db):
        assert mock_utils_db["_db_ok"] is True


class TestIsGreenplum:
    def test_detects_greenplum(self, mock_utils_db):
        mock_utils_db["utils_db"].fetchval.return_value = "Greenplum 6.25"
        assert mock_utils_db["_is_greenplum"]() is True

    def test_detects_postgres(self, mock_utils_db):
        mock_utils_db["utils_db"].fetchval.return_value = "PostgreSQL 14.10"
        assert mock_utils_db["_is_greenplum"]() is False

    def test_handles_error(self, mock_utils_db):
        mock_utils_db["utils_db"].fetchval.side_effect = Exception("no db")
        assert mock_utils_db["_is_greenplum"]() is False


class TestBenchmarkDBInit:
    def test_defaults(self, mock_utils_db):
        db = mock_utils_db["BenchmarkDB"]()
        assert db._available is True
        assert db._schema == "public"

    def test_with_dsn_configured(self, mock_utils_db):
        db = mock_utils_db["BenchmarkDB"](dsn="postgresql://u:p@h/db")
        mock_utils_db["utils_db"].configure.assert_called_with("postgresql://u:p@h/db")


class TestEnsureTables:
    def test_noop_if_not_available(self, mock_utils_db):
        db = mock_utils_db["BenchmarkDB"]()
        db._available = False
        db.ensure_tables()
        mock_utils_db["utils_db"].execute.assert_not_called()


class TestSaveRun:
    def test_noop_if_not_available(self, mock_utils_db):
        db = mock_utils_db["BenchmarkDB"]()
        db._available = False
        result = db.save_run(MagicMock())
        assert result is None

    def test_saves_suite_and_returns_run_id(self, mock_utils_db):
        from datetime import datetime
        from benchmarks.models import BenchResult, SuiteResult

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.__enter__.return_value = mock_cur
        mock_cur.fetchone.side_effect = [
            (42,),
            (99,),
        ]
        mock_conn.cursor.return_value = mock_cur
        mock_utils_db["utils_db"].transaction.return_value.__enter__.return_value = mock_conn

        db = mock_utils_db["BenchmarkDB"]()
        suite = SuiteResult(
            suite_name="test_suite",
            config={"tags": ["fast"]},
            timestamp=datetime.now(),
            total_items=1,
            passed_items=1,
            total_score=0.95,
            avg_score=0.95,
            duration_sec=1.0,
            results=[
                BenchResult(
                    item_id="item-1",
                    passed=True,
                    total_score=0.95,
                    response="ok",
                    error="",
                    duration_sec=1.0,
                    total_iterations=2,
                    tools_used=["read"],
                    skills_activated=["python"],
                    llm_judge_score=0.9,
                    details={},
                ),
            ],
        )

        run_id = db.save_run(suite)
        assert run_id == "42"
        assert mock_utils_db["utils_db"].transaction.called

    def test_saves_with_explicit_run_id_and_artifacts_dir(self, mock_utils_db):
        from datetime import datetime
        from benchmarks.models import BenchResult, CheckResult, SuiteResult

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.__enter__.return_value = mock_cur
        mock_cur.fetchone.return_value = ("abc123",)
        mock_conn.cursor.return_value = mock_cur
        mock_utils_db["utils_db"].transaction.return_value.__enter__.return_value = mock_conn

        db = mock_utils_db["BenchmarkDB"]()
        suite = SuiteResult(
            run_id="run-uuid-1",
            artifacts_dir="benchmarks/results/runs/run-uuid-1",
            suite_name="test_suite",
            config={"tags": ["fast"]},
            timestamp=datetime.now(),
            total_items=1,
            passed_items=1,
            total_score=0.95,
            avg_score=0.95,
            duration_sec=1.0,
            results=[
                BenchResult(
                    item_id="item-1",
                    passed=True,
                    total_score=0.95,
                    response="ok",
                    total_iterations=2,
                    tools_used=["read"],
                    skills_activated=["python"],
                    checks=[CheckResult(check="tools", passed=True, score=1.0)],
                ),
            ],
        )

        run_id = db.save_run(suite)
        assert run_id == "abc123"

        # первый INSERT (runs) должен содержать явный id и artifacts_dir
        runs_args = mock_cur.execute.call_args_list[0][0][1]
        assert "run-uuid-1" in runs_args
        assert "benchmarks/results/runs/run-uuid-1" in runs_args

        # второй INSERT (results) должен содержать details с checks
        results_args = mock_cur.execute.call_args_list[1][0][1]
        details = results_args[-1]
        assert details["checks"][0]["check"] == "tools"

    def test_details_include_multi_step(self, mock_utils_db):
        from datetime import datetime
        from benchmarks.models import BenchResult, CheckResult, StepResult, SuiteResult

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.__enter__.return_value = mock_cur
        mock_cur.fetchone.return_value = ("xyz",)
        mock_conn.cursor.return_value = mock_cur
        mock_utils_db["utils_db"].transaction.return_value.__enter__.return_value = mock_conn

        db = mock_utils_db["BenchmarkDB"]()
        suite = SuiteResult(
            suite_name="s",
            timestamp=datetime.now(),
            total_items=1,
            passed_items=1,
            total_score=0.5,
            avg_score=0.5,
            duration_sec=1.0,
            results=[
                BenchResult(
                    item_id="m",
                    passed=True,
                    total_score=0.5,
                    tools_used=["exec"],
                    total_iterations=3,
                    steps=[
                        StepResult(step=1, weight=1.0, passed=True, score=0.5,
                                   response="step resp", tools_used=["exec"],
                                   iterations=1, duration_sec=1.0,
                                   checks=[CheckResult(check="tools", passed=True, score=1.0)]),
                    ],
                ),
            ],
        )
        db.save_run(suite)
        results_args = mock_cur.execute.call_args_list[1][0][1]
        details = results_args[-1]
        assert details["steps"][0]["step"] == 1
        assert details["steps"][0]["response"] == "step resp"
        assert details["steps"][0]["checks"][0]["check"] == "tools"


class TestGetHistory:
    def test_returns_list(self, mock_utils_db):
        mock_utils_db["utils_db"].fetch.return_value = [
            {"id": "1", "suite_name": "s1", "total_items": 5},
        ]
        db = mock_utils_db["BenchmarkDB"]()
        result = db.get_history("s1", limit=5)
        assert len(result) == 1
        assert result[0]["id"] == "1"

    def test_empty_if_not_available(self, mock_utils_db):
        db = mock_utils_db["BenchmarkDB"]()
        db._available = False
        result = db.get_history("s1")
        assert result == []


class TestCompareRuns:
    def test_returns_comparison(self, mock_utils_db):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.__enter__.return_value = mock_cur

        # First cursor context: runs queries
        mock_cur.description = [("id",), ("total_score",), ("avg_score",), ("passed_items",)]
        mock_cur.fetchone.side_effect = [
            (1, 80.0, 0.8, 4),
            (2, 90.0, 0.9, 5),
        ]
        mock_cur.fetchall.return_value = []
        def make_results_cur(rows):
            c = MagicMock()
            c.__enter__.return_value = c
            c.description = [("item_id",), ("score",)]
            c.fetchone.return_value = None
            c.fetchall.return_value = rows
            return c

        mock_conn.cursor.side_effect = [
            mock_cur,
            mock_cur,
            make_results_cur([("a", 0.5)]),
            make_results_cur([("a", 0.9)]),
        ]

        mock_utils_db["utils_db"].transaction.return_value.__enter__.return_value = mock_conn

        db = mock_utils_db["BenchmarkDB"]()
        result = db.compare_runs("1", "2")
        assert result is not None
        assert result["run_1"]["id"] == "1"
        assert result["run_2"]["id"] == "2"

    def test_returns_none_if_not_available(self, mock_utils_db):
        db = mock_utils_db["BenchmarkDB"]()
        db._available = False
        result = db.compare_runs("1", "2")
        assert result is None

    def test_returns_none_if_missing_run(self, mock_utils_db):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.__enter__.return_value = mock_cur
        mock_cur.description = [("id",)]
        mock_cur.fetchone.side_effect = [None, None]
        mock_conn.cursor.return_value = mock_cur
        mock_utils_db["utils_db"].transaction.return_value.__enter__.return_value = mock_conn

        db = mock_utils_db["BenchmarkDB"]()
        result = db.compare_runs("1", "2")
        assert result is None


class TestCompareRunsInner:
    def test_deltas_calculated(self, mock_utils_db):
        mock_conn = MagicMock()

        # First cursor context: runs queries (fetchone called twice)
        mock_cur1 = MagicMock()
        mock_cur1.__enter__.return_value = mock_cur1
        mock_cur1.description = [("id",), ("total_score",), ("avg_score",), ("passed_items",)]
        mock_cur1.fetchone.side_effect = [
            (1, 60.0, 0.6, 3),
            (2, 80.0, 0.8, 4),
        ]

        # Second cursor context: results queries (fetchall called twice)
        mock_cur2 = MagicMock()
        mock_cur2.__enter__.return_value = mock_cur2
        mock_cur2.description = [("item_id",), ("score",)]
        mock_cur2.fetchone.return_value = None
        mock_cur2.fetchall.side_effect = [
            [("a", 0.5), ("b", 0.7)],   # first fetchall (run_id_1)
            [("a", 0.9), ("b", 0.6)],   # second fetchall (run_id_2)
        ]

        mock_conn.cursor.side_effect = [mock_cur1, mock_cur2]
        mock_utils_db["utils_db"].transaction.return_value.__enter__.return_value = mock_conn

        db = mock_utils_db["BenchmarkDB"]()
        result = db.compare_runs("1", "2")
        assert result["total_delta"] == 20.0
        assert len(result["deltas"]) == 2
        assert result["deltas"][0]["item_id"] == "a"
        assert result["deltas"][0]["delta"] == 0.4
        assert result["deltas"][1]["item_id"] == "b"
        assert result["deltas"][1]["delta"] == -0.1
