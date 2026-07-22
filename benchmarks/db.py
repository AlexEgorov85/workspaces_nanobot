"""Хранилище результатов бенчмарков в PostgreSQL.

Использует общепроектный коннектор к БД из utils.db.

Пример:
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
    from utils.db import configure, execute, transaction, fetchval, fetch as _bench_fetch
    from psycopg2.extras import Json
    _db_ok = True
except ImportError:
    configure = execute = transaction = _bench_fetch = fetchval = Json = None
    _db_ok = False

try:
    from config import SETTINGS as _S
    _SCHEMA = _S.get("benchmark", {}).get("db_schema", "public") if isinstance(_S.get("benchmark", {}), dict) else "public"
    _RUNS_TABLE = _S.get("benchmark", {}).get("runs_table", "benchmark_runs") if isinstance(_S.get("benchmark", {}), dict) else "benchmark_runs"
    _RESULTS_TABLE = _S.get("benchmark", {}).get("results_table", "benchmark_results") if isinstance(_S.get("benchmark", {}), dict) else "benchmark_results"
except Exception:
    _SCHEMA = "public"
    _RUNS_TABLE = "benchmark_runs"
    _RESULTS_TABLE = "benchmark_results"

SCHEMA = _SCHEMA
RUNS_TABLE = _RUNS_TABLE
RESULTS_TABLE = _RESULTS_TABLE


def _is_greenplum() -> bool:
    """Определить, работаем ли мы с Greenplum."""
    if not _db_ok:
        return False
    try:
        ver = fetchval("SELECT version()")
        return ver and "Greenplum" in ver
    except Exception:
        return False


class BenchmarkDB:
    """Управление сохранением и загрузкой результатов бенчмарков в PostgreSQL."""

    def __init__(self, dsn: str = "", schema: str = SCHEMA) -> None:
        """Инициализация подключения к БД.

        Args:
            dsn: Строка подключения к PostgreSQL.
            schema: Схема базы данных (по умолчанию public).
        """
        self._dsn = dsn
        self._schema = schema
        self._fq_runs = f'"{schema}"."{RUNS_TABLE}"'
        self._fq_results = f'"{schema}"."{RESULTS_TABLE}"'
        self._available = _db_ok
        if self._available and dsn:
            configure(dsn)

    def ensure_tables(self) -> None:
        """Создание таблиц для хранения прогонов, если они ещё не существуют.

        Автоматически выбирает между PG 9.4 и GP 6.25 DDL.
        """
        if not self._available:
            logger.warning("PostgreSQL not available, skipping table creation")
            return
        base = Path(__file__).parent / "sql"
        is_gp = _is_greenplum()
        sql_path = base / "create_benchmark_tables_gp.sql" if is_gp else base / "create_benchmark_tables.sql"
        if sql_path.exists():
            sql = sql_path.read_text(encoding="utf-8")
            execute(sql)
            logger.info("Benchmark tables ensured (gp={})", is_gp)
        else:
            logger.warning("SQL DDL file not found at {}", sql_path)

    def save_run(self, suite_result: SuiteResult) -> str | None:
        """Сохранение результатов прогона набора тестов.

        Args:
            suite_result: Результаты прогона набора.

        Returns:
            Идентификатор сохранённого прогона или None, если БД недоступна.
        """
        if not self._available:
            logger.warning("PostgreSQL not available, skipping save")
            return None
        with transaction() as conn:
            return self._save_run_inner(conn, suite_result)

    def _save_run_inner(self, conn, suite_result: SuiteResult) -> str:
        """Вставка записи прогона и связанных результатов в БД.

        Args:
            conn: Активное соединение с БД (внутри транзакции).
            suite_result: Результаты прогона набора.

        Returns:
            Идентификатор созданной записи прогона.
        """
        now = datetime.now()

        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {self._fq_runs} "
                f"(suite_name, suite_tags, config, total_items, passed_items, "
                f"total_score, avg_score, duration_sec, started_at, finished_at) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                f"RETURNING id",
                (
                    suite_result.suite_name,
                    Json(suite_result.config.get("tags", [])),
                    suite_result.config,
                    suite_result.total_items,
                    suite_result.passed_items,
                    suite_result.total_score,
                    suite_result.avg_score,
                    suite_result.duration_sec,
                    now,
                    now,
                ),
            )
            run_id = str(cur.fetchone()[0])

        for r in suite_result.results:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {self._fq_results} "
                    f"(run_id, item_id, item_name, difficulty, category, item_type, "
                    f"passed, score, response, tools_used, skills_activated, "
                    f"total_iterations, duration_sec, error, llm_judge_score, details) "
                    f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    f"%s, %s, %s, %s, %s, %s)",
                    (
                        run_id,
                        r.item_id,
                        "",
                        r.total_score,
                        "",
                        "single" if not r.steps else "multi_step",
                        r.passed,
                        r.total_score,
                        r.response,
                        Json(r.tools_used),
                        Json(r.skills_activated),
                        r.total_iterations,
                        r.duration_sec,
                        r.error,
                        r.llm_judge_score,
                        r.details,
                    ),
                )

        logger.info("Saved benchmark run {} to PostgreSQL", run_id)
        return run_id

    def get_history(self, suite_name: str, limit: int = 10) -> list[dict[str, Any]]:
        """Получение истории прогонов для указанного набора тестов.

        Args:
            suite_name: Имя набора тестов.
            limit: Максимальное количество записей (по умолчанию 10).

        Returns:
            Список словарей с данными прогонов.
        """
        if not self._available:
            logger.warning("PostgreSQL not available")
            return []
        return _bench_fetch(
            f"SELECT id, suite_name, total_items, passed_items, "
            f"total_score, avg_score, duration_sec, started_at, finished_at "
            f"FROM {self._fq_runs} "
            f"WHERE suite_name = %s "
            f"ORDER BY started_at DESC LIMIT %s",
            suite_name,
            limit,
        )

    def compare_runs(self, run_id_1: str, run_id_2: str) -> dict[str, Any] | None:
        """Сравнение двух прогонов по идентификаторам.

        Args:
            run_id_1: Идентификатор первого прогона.
            run_id_2: Идентификатор второго прогона.

        Returns:
            Словарь со сравнением или None, если БД недоступна.
        """
        if not self._available:
            logger.warning("PostgreSQL not available")
            return None
        with transaction() as conn:
            return self._compare_runs_inner(conn, run_id_1, run_id_2)

    def _compare_runs_inner(
        self, conn, run_id_1: str, run_id_2: str
    ) -> dict[str, Any] | None:
        """Внутренняя логика сравнения двух прогонов.

        Args:
            conn: Активное соединение с БД (внутри транзакции).
            run_id_1: Идентификатор первого прогона.
            run_id_2: Идентификатор второго прогона.

        Returns:
            Словарь с данными обоих прогонов и дельтами.
        """
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {self._fq_runs} WHERE id = %s", (run_id_1,))
            col_names = [desc[0] for desc in cur.description]
            row = cur.fetchone()
            run1 = dict(zip(col_names, row)) if row else None

            cur.execute(f"SELECT * FROM {self._fq_runs} WHERE id = %s", (run_id_2,))
            row = cur.fetchone()
            run2 = dict(zip(col_names, row)) if row else None

        if not run1 or not run2:
            return None

        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {self._fq_results} WHERE run_id = %s", (run_id_1,))
            col_names = [desc[0] for desc in cur.description]
            results1 = [dict(zip(col_names, r)) for r in cur.fetchall()]

            cur.execute(f"SELECT * FROM {self._fq_results} WHERE run_id = %s", (run_id_2,))
            results2 = [dict(zip(col_names, r)) for r in cur.fetchall()]

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
