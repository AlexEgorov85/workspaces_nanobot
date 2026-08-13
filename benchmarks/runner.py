#!/usr/bin/env python3
"""Запуск бенчмарков для агента nanobot.

Примеры:
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

# Добавление корня проекта в sys.path для импортов utils.db, benchmarks.*
_SCRIPT_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from loguru import logger

# Принудительно UTF-8 для консоли (cp1251 не умеет символ ✗ и пр.)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from benchmarks.db import BenchmarkDB
from benchmarks.evaluator import evaluate
from benchmarks.hooks import BenchmarkHook
from benchmarks.loader import load_benchmark
from benchmarks.models import BenchItem, BenchResult, BenchSuite, CheckResult, StepResult, SuiteResult
from benchmarks.reporter import save_json_report, save_markdown_report
from benchmarks.scorer import score_multi_step, score_single, score_step

RESULTS_DIR = Path(__file__).parent / "results" / "runs"
ITEMS_DIR = Path(__file__).parent / "items"


def _detect_run_id() -> str:
    """Генерация уникального идентификатора прогона на основе текущей метки времени.

    Returns:
        Строка вида "2026-06-16_14-30-00".
    """
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Парсинг аргументов командной строки.

    Args:
        argv: Список аргументов (по умолчанию sys.argv).

    Returns:
        Пространство имён с распарсенными аргументами.
    """
    p = argparse.ArgumentParser(description="Запуск бенчмарков nanobot")
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
    """Фильтрация заданий по тегам, категории, сложности и типу.

    Args:
        suite: Исходный набор тестов.
        tags: Список тегов для фильтрации (simple, medium, hard).
        category: Список категорий.
        difficulty: Диапазон сложности (например "1-3").
        mode: Режим ("all", "single", "multi_step").

    Returns:
        Новый набор тестов только с отфильтрованными заданиями.
    """
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


def _format_checks_failures(checks: list[CheckResult]) -> str:
    """Форматирование списка заваленных проверок в читаемую строку.

    Args:
        checks: Список результатов проверок.

    Returns:
        Строка вида 'tools✗ keywords_include✗' или пустая строка.
    """
    failed = [c for c in checks if not c.passed]
    if not failed:
        return ""
    return " ".join(f"{c.check}✗" for c in failed)


def _print_summary(suite_result: SuiteResult) -> None:
    """Вывод итоговой сводки по прогону в консоль.

    Args:
        suite_result: Результаты прогона набора.
    """
    print()
    print("=" * 70)
    print(f"  BENCHMARK COMPLETE: {suite_result.suite_name}")
    print("=" * 70)
    print(f"  Items:    {suite_result.total_items}")
    print(f"  Passed:   {suite_result.passed_items} / {suite_result.total_items} "
          f"({suite_result.passed_items / suite_result.total_items * 100:.1f}%)")
    print(f"  Avg Score: {suite_result.avg_score:.2%}")
    print(f"  Duration:  {suite_result.duration_sec:.1f}s")
    print("-" * 70)
    for r in suite_result.results:
        status = "PASS" if r.passed else "FAIL"
        diff = "S" if r.difficulty <= 3 else ("M" if r.difficulty <= 7 else "H")
        line = f"  [{status}] [{diff}] {r.item_id:35s} score={r.total_score:.2%}  iter={r.total_iterations}  dur={r.duration_sec:.1f}s"
        if not r.passed:
            fails = _format_checks_failures(r.checks)
            if fails:
                line += f"  [{fails}]"
        print(line)
        if r.error:
            print(f"  {'':38s}ERROR: {r.error}")
    print("=" * 70)
    if suite_result.passed_items < suite_result.total_items:
        print(f"  {suite_result.total_items - suite_result.passed_items} item(s) FAILED.")
        print("  See detail/<id>.json for per-check breakdown.")
        print("  Hint: run with --verbose to see full agent responses.")


def _cleanup_item(item: BenchItem, bot: Any) -> None:
    """Удаление файлов, созданных агентом во время выполнения задания.

    Args:
        item: Задание бенчмарка.
        bot: Экземпляр агента Nanobot.
    """
    workspace = Path(bot._loop.workspace) if hasattr(bot._loop, "workspace") else None
    if not workspace:
        return
    files_to_check: list[str] = []
    if item.type == "single" and item.expect:
        if item.expect.check_file:
            files_to_check.append(item.expect.check_file)
        if item.expect.check_file_content:
            files_to_check.append(item.expect.check_file)
    elif item.type == "multi_step" and item.steps:
        for s in item.steps:
            if s.expect.check_file:
                files_to_check.append(s.expect.check_file)
            if s.expect.check_file_content:
                files_to_check.append(s.expect.check_file)
    for fname in set(files_to_check):
        fpath = workspace / fname
        if fpath.exists():
            try:
                fpath.unlink()
            except Exception:
                pass


async def _run_item(
    item: BenchItem,
    run_id: str,
    bot: Any,
    verbose: bool,
) -> BenchResult:
    """Запуск одного задания бенчмарка (single или multi_step).

    Args:
        item: Задание бенчмарка.
        run_id: Идентификатор текущего прогона.
        bot: Экземпляр агента Nanobot.
        verbose: Флаг подробного вывода.

    Returns:
        Результат выполнения задания.
    """
    try:
        if item.type == "single":
            return await _run_single(item, run_id, bot, verbose)
        else:
            return await _run_multi_step(item, run_id, bot, verbose)
    finally:
        _cleanup_item(item, bot)


async def _run_single(
    item: BenchItem,
    run_id: str,
    bot: Any,
    verbose: bool,
) -> BenchResult:
    """Запуск одношагового задания.

    Выполняет задание через агента, оценивает ответ и возвращает результат.

    Args:
        item: Задание бенчмарка (тип "single").
        run_id: Идентификатор прогона.
        bot: Экземпляр агента Nanobot.
        verbose: Флаг подробного вывода.

    Returns:
        Результат выполнения задания.
    """
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

    # Очистка сессии агента
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
    """Запуск многошагового задания.

    Последовательно выполняет каждый шаг в рамках одной сессии,
    оценивает каждый шаг и агрегирует итоговый результат.

    Args:
        item: Задание бенчмарка (тип "multi_step").
        run_id: Идентификатор прогона.
        bot: Экземпляр агента Nanobot.
        verbose: Флаг подробного вывода.

    Returns:
        Результат выполнения задания.
    """
    session_key = f"bench:multi:{item.id}:{run_id}"
    step_results: list[StepResult] = []

    total_steps = len(item.steps)

    for step in item.steps:
        hook = BenchmarkHook()
        step_index = step.step
        print(f"    Step {step_index}/{total_steps}: {step.question[:70]}")

        try:
            result = await bot.run(
                message=step.question,
                session_key=session_key,
                hooks=[hook],
            )
        except Exception as e:
            logger.error("Error in step {} of item {}: {}", step.step, item.id, e)
            print(f"      -> ERROR: {e}")
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

        s_status = "PASS" if sr.passed else "FAIL"
        fails = _format_checks_failures(sr.checks)
        s_ext = f" [{fails}]" if fails else ""
        print(f"      -> {s_status} score={sr.score:.2%} iter={sr.iterations} dur={sr.duration_sec:.1f}s{s_ext}")
        step_results.append(sr)

    # Очистка сессии агента
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
    """Запуск всех заданий набора бенчмарков.

    Args:
        suite: Набор тестов для запуска.
        run_id: Идентификатор прогона.
        args: Аргументы командной строки.

    Returns:
        Результаты прогона всего набора.
    """
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
    """Сравнение двух предыдущих прогонов по JSON-отчётам.

    Args:
        args: Аргументы командной строки (содержит compare — два пути).
    """
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


def _validate_items(suite: BenchSuite) -> list[str]:
    """Проверка всех заданий на очевидные проблемы перед запуском.

    Args:
        suite: Набор тестов для проверки.

    Returns:
        Список предупреждений (пустой, если всё в порядке).
    """
    warnings: list[str] = []
    seen_ids: set[str] = set()
    for item in suite.items:
        if item.id in seen_ids:
            warnings.append(f"DUPLICATE ID '{item.id}' — will be overwritten in reports/DB")
        seen_ids.add(item.id)
        if item.type == "single" and not item.question:
            warnings.append(f"Item '{item.id}' is single but has no question")
        if item.type == "multi_step" and not item.steps:
            warnings.append(f"Item '{item.id}' is multi_step but has no steps")
        if item.difficulty < 1 or item.difficulty > 10:
            warnings.append(f"Item '{item.id}' has difficulty={item.difficulty}, expected 1-10")
        if item.max_iterations < 1:
            warnings.append(f"Item '{item.id}' has max_iterations={item.max_iterations}, must be >= 1")
        if item.timeout < 1:
            warnings.append(f"Item '{item.id}' has timeout={item.timeout}, must be >= 1")
        if item.type == "multi_step":
            total_weight = sum(s.weight for s in item.steps)
            if total_weight == 0:
                warnings.append(f"Item '{item.id}': sum of step weights is 0 (all steps will be ignored)")
    return warnings


async def main_async(argv: list[str] | None = None) -> int:
    """Асинхронный входной точка запуска бенчмарков.

    Args:
        argv: Список аргументов командной строки.

    Returns:
        Код возврата: 0 — все тесты пройдены, 1 — есть упавшие.
    """
    args = _parse_args(argv)

    if args.compare:
        _do_compare(args)
        return 0

    log_level = "DEBUG" if args.verbose else "INFO"
    logger.remove()
    logger.add(sys.stderr, level=log_level)

    # Загрузка YAML с дружественной диагностикой ошибок
    try:
        suite = load_benchmark(args.items)
    except FileNotFoundError as e:
        print()
        print("=" * 70)
        print("  ERROR: Benchmark file(s) not found")
        print("=" * 70)
        print(f"  {e}")
        print()
        print("  Check that --items points to a valid .yaml file or directory.")
        print(f"  Default items dir: {ITEMS_DIR}")
        print(f"  Try: python benchmarks/runner.py --items {ITEMS_DIR}")
        print()
        return 1
    except ValueError as e:
        print()
        print("=" * 70)
        print("  ERROR: Invalid benchmark definition")
        print("=" * 70)
        print(f"  {e}")
        print()
        print("  Hint: for multi_step items, you must define at least one step.")
        print("  Hint: run with --dry-run to preview items before running.")
        print()
        return 1
    except KeyError as e:
        print()
        print("=" * 70)
        print("  ERROR: Missing required field in benchmark YAML")
        print("=" * 70)
        print(f"  Missing field: {e}")
        print()
        print("  Every item must have at least: id, name, difficulty, category, type")
        print("  For single items: question is required")
        print("  For multi_step items: steps is required")
        print()
        print("  Check your YAML file and add the missing field.")
        print("  See benchmarks/items/_template.yaml for reference.")
        print()
        return 1
    except Exception as e:
        print()
        print("=" * 70)
        print("  ERROR: Failed to load benchmark")
        print("=" * 70)
        print(f"  {type(e).__name__}: {e}")
        print()
        print("  Check your YAML file for syntax errors.")
        print("  Run this to validate:")
        print(f"    python -c \"import yaml; yaml.safe_load(open('{args.items}'))\"")
        print()
        return 1

    suite = _filter_items(suite, args.tags, args.category, args.difficulty, args.mode)

    if not suite.items:
        print()
        print("No items match the filters. Nothing to run.")
        print(f"  Available: {len(suite.items)} items in suite '{suite.name}'")
        print(f"  Filters applied: tags={args.tags}, category={args.category}, "
              f"difficulty={args.difficulty}, mode={args.mode}")
        print()
        return 0

    # Валидация перед запуском
    warnings = _validate_items(suite)
    if warnings:
        print()
        print("Warnings:")
        for w in warnings:
            print(f"  ! {w}")
        print()

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
                if item.expect:
                    parts = []
                    if item.expect.tools:
                        parts.append(f"tools={item.expect.tools}")
                    if item.expect.keywords_include:
                        parts.append(f"kw_in={item.expect.keywords_include}")
                    if item.expect.keywords_exclude:
                        parts.append(f"kw_ex={item.expect.keywords_exclude}")
                    if item.expect.check_file:
                        parts.append(f"file={item.expect.check_file}")
                    if item.expect.match_type != "keyword":
                        parts.append(f"match={item.expect.match_type}")
                    if parts:
                        print(f"           expect: {', '.join(parts)}")
            else:
                print(f"  [MULTI]  d={diff} {name}")
                for step in item.steps:
                    s_parts = []
                    if step.expect.tools:
                        s_parts.append(f"tools={step.expect.tools}")
                    if step.expect.keywords_include:
                        s_parts.append(f"kw_in={step.expect.keywords_include}")
                    if step.expect.check_file:
                        s_parts.append(f"file={step.expect.check_file}")
                    s_str = f" [{', '.join(s_parts)}]" if s_parts else ""
                    print(f"           Step {step.step} (w={step.weight}): {step.question}{s_str}")
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
    """Синхронная точка входа для запуска бенчмарков.

    Args:
        argv: Список аргументов командной строки.

    Returns:
        Код возврата (0 — успех, 1 — ошибка).
    """
    return asyncio.run(main_async(argv))


if __name__ == "__main__":
    sys.exit(main())
