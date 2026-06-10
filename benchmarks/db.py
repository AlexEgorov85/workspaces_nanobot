"""PostgreSQL storage for benchmark results.

Uses the project-wide shared db connector from utils.db.

Usage:
    from benchmarks.db import BenchmarkDB

    db = BenchmarkDB(dsn="...")
    db.ensure_tables()
    db.save_run(suite_result)
    history = db.get_history("my_suite", limit=5)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from benchmarks.models import BenchResult, SuiteResult

try:
    from utils.db import db as shared_db
except ImportError:
    shared_db = None


SCHEMA = "public"
RUNS_TABLE = "benchmark_runs"
RESULTS_TABLE = "benchmark_results"


class BenchmarkDB:
    def __init__(self, dsn: str = "", schema: str = SCHEMA) -> None:
        self._dsn = dsn
        self._schema = schema
        self._fq_runs = f'"{schema}"."{RUNS_TABLE}"'
        self._fq_results = f'"{schema}"."{RESULTS_TABLE}"'
        self._available = shared_db is not None
        if self._available and dsn:
            shared_db.configure(dsn)

    def ensure_tables(self) -> None:
        if not self._available:
            logger.warning("PostgreSQL not available, skipping table creation")
            return
        sql_path = Path(__file__).parent / "sql" / "create_benchmark_tables.sql"
        if sql_path.exists():
            sql = sql_path.read_text(encoding="utf-8")
            shared_db.sync_execute(sql)
            logger.info("Benchmark tables ensured")
        else:
            logger.warning("SQL DDL file not found at {}", sql_path)

    def save_run(self, suite_result: SuiteResult) -> str | None:
        if not self._available:
            logger.warning("PostgreSQL not available, skipping save")
            return None
        return shared_db.sync_transaction(lambda conn: self._async_save_run(conn, suite_result))

    async def _async_save_run(self, conn, suite_result: SuiteResult) -> str:
        now = datetime.now()
        run_id = await conn.fetchval(
            f"INSERT INTO {self._fq_runs} "
            f"(suite_name, suite_tags, config, total_items, passed_items, "
            f"total_score, avg_score, duration_sec, started_at, finished_at) "
            f"VALUES ($1, $2::jsonb, $3::jsonb, $4, $5, $6, $7, $8, $9, $10) "
            f"RETURNING id",
            suite_result.suite_name,
            suite_result.config.get("tags", []),
            suite_result.config,
            suite_result.total_items,
            suite_result.passed_items,
            suite_result.total_score,
            suite_result.avg_score,
            suite_result.duration_sec,
            now,
            now,
        )
        run_id_str = str(run_id)

        for r in suite_result.results:
            await conn.execute(
                f"INSERT INTO {self._fq_results} "
                f"(run_id, item_id, item_name, difficulty, category, item_type, "
                f"passed, score, response, tools_used, skills_activated, "
                f"total_iterations, duration_sec, error, llm_judge_score, details) "
                f"VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, "
                f"$11::jsonb, $12, $13, $14, $15, $16::jsonb)",
                run_id_str,
                r.item_id,
                "",
                r.total_score,
                "",
                "single" if not r.steps else "multi_step",
                r.passed,
                r.total_score,
                r.response,
                r.tools_used,
                r.skills_activated,
                r.total_iterations,
                r.duration_sec,
                r.error,
                r.llm_judge_score,
                r.details,
            )

        logger.info("Saved benchmark run {} to PostgreSQL", run_id_str)
        return run_id_str

    def get_history(self, suite_name: str, limit: int = 10) -> list[dict[str, Any]]:
        if not self._available:
            logger.warning("PostgreSQL not available")
            return []
        return shared_db.sync_fetch(
            f"SELECT id, suite_name, total_items, passed_items, "
            f"total_score, avg_score, duration_sec, started_at, finished_at "
            f"FROM {self._fq_runs} "
            f"WHERE suite_name = $1 "
            f"ORDER BY started_at DESC LIMIT $2",
            suite_name,
            limit,
        )

    def compare_runs(self, run_id_1: str, run_id_2: str) -> dict[str, Any] | None:
        if not self._available:
            logger.warning("PostgreSQL not available")
            return None
        return shared_db.sync_transaction(
            lambda conn: self._async_compare_runs(conn, run_id_1, run_id_2)
        )

    async def _async_compare_runs(
        self, conn, run_id_1: str, run_id_2: str
    ) -> dict[str, Any]:
        run1 = await conn.fetchrow(
            f"SELECT * FROM {self._fq_runs} WHERE id = $1", run_id_1
        )
        run2 = await conn.fetchrow(
            f"SELECT * FROM {self._fq_runs} WHERE id = $1", run_id_2
        )
        if not run1 or not run2:
            return None

        results1 = await conn.fetch(
            f"SELECT * FROM {self._fq_results} WHERE run_id = $1", run_id_1
        )
        results2 = await conn.fetch(
            f"SELECT * FROM {self._fq_results} WHERE run_id = $1", run_id_2
        )

        items1 = {r["item_id"]: r["score"] for r in results1}
        items2 = {r["item_id"]: r["score"] for r in results2}
        all_items = set(items1) | set(items2)

        deltas = []
        for item_id in sorted(all_items):
            s1 = items1.get(item_id, 0.0)
            s2 = items2.get(item_id, 0.0)
            deltas.append({
                "item_id": item_id,
                "score_1": s1,
                "score_2": s2,
                "delta": round(s2 - s1, 4),
            })

        return {
            "run_1": {"id": run_id_1, "total_score": float(run1["total_score"]),
                       "avg_score": float(run1["avg_score"]),
                       "passed": run1["passed_items"]},
            "run_2": {"id": run_id_2, "total_score": float(run2["total_score"]),
                       "avg_score": float(run2["avg_score"]),
                       "passed": run2["passed_items"]},
            "deltas": deltas,
            "total_delta": round(float(run2["total_score"]) - float(run1["total_score"]), 4),
        }
