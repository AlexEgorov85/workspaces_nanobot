#!/usr/bin/env python3
"""Benchmark runner for nanobot agent.

Usage:
    python benchmarks/runner.py
    python benchmarks/runner.py --tags simple
    python benchmarks/runner.py --items benchmarks/items/simple.yaml
    python benchmarks/runner.py --db postgresql://user@host/dbname
    python benchmarks/runner.py --compare runs/2026-06-08/ runs/2026-06-09/
    python benchmarks/runner.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# Ensure project root is on sys.path for imports like utils.db, benchmarks.*
_SCRIPT_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from loguru import logger

from benchmarks.db import BenchmarkDB
from benchmarks.evaluator import evaluate
from benchmarks.hooks import BenchmarkHook
from benchmarks.loader import load_benchmark
from benchmarks.models import BenchItem, BenchResult, BenchSuite, StepResult, SuiteResult
from benchmarks.reporter import save_json_report, save_markdown_report
from benchmarks.scorer import score_multi_step, score_single, score_step

RESULTS_DIR = Path(__file__).parent / "results" / "runs"
ITEMS_DIR = Path(__file__).parent / "items"


def _detect_run_id() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="nanobot benchmark runner")
    p.add_argument("--items", default=str(ITEMS_DIR),
                   help="Path to YAML file or directory with benchmark items")
    p.add_argument("--tags", nargs="*", default=None,
                   help="Filter by tags (e.g. simple, medium, hard)")
    p.add_argument("--category", nargs="*", default=None,
                   help="Filter by category")
    p.add_argument("--difficulty", default=None,
                   help="Difficulty range, e.g. 1-3, 4-7, 8-10")
    p.add_argument("--mode", choices=["all", "single", "multi_step"], default="all",
                   help="Filter by item type")
    p.add_argument("--output", default=None,
                   help="Output directory for reports (default: results/runs/<timestamp>)")
    p.add_argument("--db", default=None,
                   help="PostgreSQL DSN to save results")
    p.add_argument("--verbose", action="store_true",
                   help="Detailed output")
    p.add_argument("--compare", nargs=2, default=None, metavar=("RUN1", "RUN2"),
                   help="Compare two previous runs (directories or DB run IDs)")
    p.add_argument("--model", default=None,
                   help="Override model name (e.g. phi4:latest, qwen3:4b)")
    p.add_argument("--config", default=None,
                   help="Path to config.json (default: auto-detect)")
    p.add_argument("--dry-run", action="store_true",
                   help="Only show what would be run, don't execute")
    return p.parse_args(argv)


def _filter_items(
    suite: BenchSuite,
    tags: list[str] | None,
    category: list[str] | None,
    difficulty: str | None,
    mode: str,
) -> BenchSuite:
    items = suite.items

    if mode == "single":
        items = [i for i in items if i.type == "single"]
    elif mode == "multi_step":
        items = [i for i in items if i.type == "multi_step"]

    if tags:
        seen_ids: set[str] = set()
        filtered: list[BenchItem] = []
        for t in tags:
            t_lower = t.lower()
            if t_lower == "simple":
                candidates = [i for i in items if i.difficulty <= 3]
            elif t_lower == "medium":
                candidates = [i for i in items if 4 <= i.difficulty <= 7]
            elif t_lower == "hard":
                candidates = [i for i in items if i.difficulty >= 8]
            else:
                candidates = []
            for i in candidates:
                if i.id not in seen_ids:
                    seen_ids.add(i.id)
                    filtered.append(i)
        items = filtered

    if category:
        items = [i for i in items if i.category in category]

    if difficulty:
        parts = difficulty.split("-")
        if len(parts) == 2:
            lo, hi = int(parts[0]), int(parts[1])
            items = [i for i in items if lo <= i.difficulty <= hi]

    return BenchSuite(name=suite.name, items=items, tags=suite.tags)


def _print_summary(suite_result: SuiteResult) -> None:
    print()
    print("=" * 60)
    print(f"  BENCHMARK COMPLETE: {suite_result.suite_name}")
    print("=" * 60)
    print(f"  Items:    {suite_result.total_items}")
    print(f"  Passed:   {suite_result.passed_items} / {suite_result.total_items} "
          f"({suite_result.passed_items / suite_result.total_items * 100:.1f}%"
          f")" if suite_result.total_items else "  Passed:   0 / 0")
    print(f"  Avg Score: {suite_result.avg_score:.2%}")
    print(f"  Duration:  {suite_result.duration_sec:.1f}s")
    print("-" * 60)
    for r in suite_result.results:
        status = "PASS" if r.passed else "FAIL"
        diff = "S" if r.total_score <= 3 else ("M" if r.total_score <= 7 else "H")
        print(f"  [{status}] [{diff}] {r.item_id:40s} score={r.total_score:.2%} "
              f"iter={r.total_iterations} dur={r.duration_sec:.1f}s")
        if r.error:
            print(f"         ERROR: {r.error}")
    print("=" * 60)


async def _run_item(
    item: BenchItem,
    run_id: str,
    bot: Any,
    verbose: bool,
) -> BenchResult:
    if item.type == "single":
        return await _run_single(item, run_id, bot, verbose)
    else:
        return await _run_multi_step(item, run_id, bot, verbose)


async def _run_single(
    item: BenchItem,
    run_id: str,
    bot: Any,
    verbose: bool,
) -> BenchResult:
    hook = BenchmarkHook()
    session_key = f"bench:single:{item.id}:{run_id}"

    if verbose:
        print(f"  Running: {item.id} ({item.name})")

    try:
        result = await bot.run(
            message=item.question,
            session_key=session_key,
            hooks=[hook],
        )
    except Exception as e:
        logger.error("Error running item {}: {}", item.id, e)
        if verbose:
            print(f"  ERROR: {e}")
        try:
            await bot._loop.sessions.delete_session(session_key)
        except Exception:
            pass
        return BenchResult(
            item_id=item.id,
            passed=False,
            total_score=0.0,
            error=str(e),
            total_iterations=hook.iterations,
            duration_sec=hook.duration_sec,
        )

    response = result.content if result else ""
    tools_used = result.tools_used if result else []
    eval_result = evaluate(
        item.expect, response, hook,
        workspace=bot._loop.workspace if hasattr(bot._loop, "workspace") else None,
    )
    bench_result = score_single(
        item, eval_result,
        response=response,
        tools_used=tools_used,
        skills_activated=hook.skills,
        iterations=hook.iterations,
        duration_sec=hook.duration_sec,
    )

    # Clean up session
    try:
        await bot._loop.sessions.delete_session(session_key)
    except Exception:
        pass

    return bench_result


async def _run_multi_step(
    item: BenchItem,
    run_id: str,
    bot: Any,
    verbose: bool,
) -> BenchResult:
    session_key = f"bench:multi:{item.id}:{run_id}"
    step_results: list[StepResult] = []

    for step in item.steps:
        hook = BenchmarkHook()

        if verbose:
            total = len(item.steps)
            print(f"  Step {step.step}/{total}: {step.question[:60]}")

        try:
            result = await bot.run(
                message=step.question,
                session_key=session_key,
                hooks=[hook],
            )
        except Exception as e:
            logger.error("Error in step {} of item {}: {}", step.step, item.id, e)
            step_results.append(StepResult(
                step=step.step,
                weight=step.weight,
                passed=False,
                score=0.0,
                response="",
                tools_used=[],
                iterations=hook.iterations,
                duration_sec=hook.duration_sec,
                details={"error": str(e)},
            ))
            continue

        response = result.content if result else ""
        tools_used = result.tools_used if result else []
        eval_result = evaluate(
            step.expect, response, hook,
            workspace=bot._loop.workspace if hasattr(bot._loop, "workspace") else None,
        )
        sr = score_step(
            step.step, step.weight, eval_result,
            response=response,
            tools_used=tools_used,
            iterations=hook.iterations,
            duration_sec=hook.duration_sec,
        )
        step_results.append(sr)

    # Clean up session
    try:
        await bot._loop.sessions.delete_session(session_key)
    except Exception:
        pass

    return score_multi_step(item, step_results)


async def _run_suite(
    suite: BenchSuite,
    run_id: str,
    args: argparse.Namespace,
) -> SuiteResult:
    from nanobot import Nanobot
    from nanobot.config.loader import load_config, resolve_config_env_vars
    from nanobot.agent.loop import AgentLoop
    from nanobot.providers.image_generation import image_gen_provider_configs

    config_path = args.config
    config = resolve_config_env_vars(load_config(config_path))
    if args.model:
        config.agents.defaults.model = args.model
        if args.model == args.model:  # also set provider for local models
            config.agents.defaults.provider = "ollama"
    loop = AgentLoop.from_config(
        config,
        image_generation_provider_configs=image_gen_provider_configs(config),
    )
    bot = Nanobot(loop)

    start_time = time.time()
    results: list[BenchResult] = []

    print(f"Running suite: {suite.name} ({len(suite.items)} items)")
    print()

    for idx, item in enumerate(suite.items, 1):
        print(f"[{idx}/{len(suite.items)}] {item.id} (difficulty={item.difficulty})")
        bench_result = await _run_item(item, run_id, bot, args.verbose)
        results.append(bench_result)

        status = "PASS" if bench_result.passed else "FAIL"
        print(f"  -> {status} score={bench_result.total_score:.2%} "
              f"iter={bench_result.total_iterations} "
              f"dur={bench_result.duration_sec:.1f}s")
        if bench_result.error:
            print(f"     ERROR: {bench_result.error}")
        print()

    duration_sec = time.time() - start_time
    total_items = len(results)
    passed_items = sum(1 for r in results if r.passed)
    total_score = sum(r.total_score for r in results)
    avg_score = total_score / total_items if total_items else 0.0

    suite_result = SuiteResult(
        suite_name=suite.name,
        timestamp=datetime.now().isoformat(),
        total_items=total_items,
        passed_items=passed_items,
        total_score=total_score,
        avg_score=avg_score,
        duration_sec=duration_sec,
        results=results,
        config={"tags": suite.tags, "mode": args.mode},
    )

    return suite_result


def _do_compare(args: argparse.Namespace) -> None:
    run1_path, run2_path = args.compare

    def load_summary(path: str) -> dict[str, Any] | None:
        p = Path(path)
        if p.is_dir():
            p = p / "summary.json"
        if p.exists():
            with open(p) as f:
                return json.load(f)
        return None

    run1 = load_summary(run1_path)
    run2 = load_summary(run2_path)

    if not run1 or not run2:
        print("Could not load run summaries from specified paths")
        return

    print()
    print("=" * 70)
    print("  COMPARISON REPORT")
    print("=" * 70)

    r1_results = {r["item_id"]: r for r in run1["results"]}
    r2_results = {r["item_id"]: r for r in run2["results"]}
    all_items = sorted(set(r1_results) | set(r2_results))

    print(f"{'Item':40s} {'Run 1':>8s} {'Run 2':>8s} {'Δ':>8s}")
    print("-" * 70)
    for item_id in all_items:
        s1 = r1_results.get(item_id, {}).get("total_score", 0)
        s2 = r2_results.get(item_id, {}).get("total_score", 0)
        delta = s2 - s1
        delta_str = f"{delta:+.2%}"
        print(f"{item_id:40s} {s1:7.1%}  {s2:7.1%}  {delta_str:>8s}")

    print("-" * 70)
    total_delta = run2["total_score"] - run1["total_score"]
    print(f"{'TOTAL':40s} {run1['total_score']:7.1%}  {run2['total_score']:7.1%}  "
          f"{total_delta:+.1%}")
    print()


async def main_async(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.compare:
        _do_compare(args)
        return 0

    log_level = "DEBUG" if args.verbose else "INFO"
    logger.remove()
    logger.add(sys.stderr, level=log_level)

    suite = load_benchmark(args.items)
    suite = _filter_items(suite, args.tags, args.category, args.difficulty, args.mode)

    if not suite.items:
        print("No items match the filters. Nothing to run.")
        return 0

    if args.dry_run:
        print(f"DRY RUN: {suite.name}")
        print(f"Total items: {len(suite.items)}")
        print()
        for item in suite.items:
            item_type = item.type
            diff = item.difficulty
            name = f"{item.id} ({item.name})"
            if item_type == "single":
                print(f"  [SINGLE] d={diff} {name}")
                print(f"           Q: {item.question}")
            else:
                print(f"  [MULTI]  d={diff} {name}")
                for step in item.steps:
                    print(f"           Step {step.step}: {step.question}")
            print()
        return 0

    run_id = _detect_run_id()
    suite_result = await _run_suite(suite, run_id, args)

    output_dir = args.output or str(RESULTS_DIR / run_id)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    json_path = save_json_report(suite_result, output_path)
    md_path = save_markdown_report(suite_result, output_path)

    print(f"JSON report:  {json_path}")
    print(f"Markdown report: {md_path}")

    if args.db:
        try:
            bdb = BenchmarkDB(dsn=args.db)
            bdb.ensure_tables()
            db_run_id = bdb.save_run(suite_result)
            if db_run_id:
                print(f"Saved to PostgreSQL: run_id={db_run_id}")
        except Exception as e:
            logger.error("Failed to save to PostgreSQL: {}", e)

    _print_summary(suite_result)

    return 0 if suite_result.passed_items == suite_result.total_items else 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(main_async(argv))


if __name__ == "__main__":
    sys.exit(main())
