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
import shutil
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

# Добавление корня проекта в sys.path для импортов utils.db, benchmarks.*
_SCRIPT_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
# workspace/ нужен для ToolAuditHook (workspace/hooks/tool_audit_hook.py)
# и для runtime-хуков, которые импортирует AgentLoop.from_config.
if str(_SCRIPT_DIR / "workspace") not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR / "workspace"))

from loguru import logger

# Принудительно UTF-8 для консоли (cp1251 не умеет символ ✗ и пр.)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Глобальный лог-файл прогресса. По умолчанию прогон пишет в
# ``--output-dir/progress.log`` (или ``benchmarks/results/runs/<run_id>/progress.log``),
# дополнительно к stdout. Используется ``_emit()`` ниже — он работает так
# же как ``print()``, но дополнительно пишет в файл, если задан ``_LOG_FILE``.
# Это лечит проблему ``System.Management.Automation.RemoteException``,
# когда stdout/stderr Python содержит большие/бинарные данные.
_LOG_FILE: "Any | None" = None  # io.TextIOBase или None


def _emit(msg: str = "", *, end: str = "\n") -> None:
    """Напечатать ``msg`` в stdout и (если открыт) в лог-файл.

    Замена ``print()`` в runner: вывод идёт в оба места сразу. Используется
    для прогресса по тестам, чтобы хвост лога был виден независимо от того,
    перехватывает ли вызывающая обёртка (PowerShell/CI) Python-stdout.
    """
    text = (msg + ("" if end == "\n" else end)) if end else msg
    try:
        print(text, end="" if end == "\n" else end)
    except Exception:
        try:
            sys.stdout.write(text)
            sys.stdout.flush()
        except Exception:
            pass
    if _LOG_FILE is not None and not _LOG_FILE.closed:
        try:
            _LOG_FILE.write(text)
            _LOG_FILE.flush()
        except Exception:
            pass

from benchmarks.db import BenchmarkDB
from benchmarks.evaluator import evaluate
from benchmarks.hooks import BenchmarkHook
from benchmarks.loader import load_benchmark
from benchmarks.models import BenchItem, BenchResult, BenchSuite, CheckResult, StepResult, SuiteResult
from benchmarks.reporter import save_json_report, save_markdown_report
from benchmarks.scorer import score_multi_step, score_single, score_step

RESULTS_DIR = Path(__file__).parent / "results" / "runs"
ITEMS_DIR = Path(__file__).parent / "items"

# Поднимаем контекст приложения по аналогии с gateway: nanobot-агент +
# DuckDB-кэш аудита + FAISS-индексы + PGSessionManager + ToolAuditHook.
# Без этого бенчмарк тестирует агента в вакууме — он не видит данные
# audit_analyzer и работает медленнее через прямой psycopg2 вместо DuckDB.
BENCH_SCRIPT_DIR = _SCRIPT_DIR
BENCH_WORKSPACE_DIR = BENCH_SCRIPT_DIR / "workspace"


def _detect_run_id() -> str:
    """Генерация уникального идентификатора прогона на основе текущей метки времени.

    Returns:
        Строка вида "2026-06-16_14-30-00".
    """
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def _generate_run_id() -> str:
    """Генерация единого UUID-идентификатора прогона.

    Один и тот же id используется для каталога файловых отчётов
    (results/runs/{run_id}) и первичного ключа прогона в БД, чтобы
    связать файлы и записи в PostgreSQL.

    Returns:
        Строка UUID4 (hex, без дефисов).
    """
    return uuid.uuid4().hex


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
    p.add_argument("--keep-runs", type=int, default=20,
                   help="Keep only N newest run dirs, delete older (0 = never delete)")
    p.add_argument("--dry-run", action="store_true",
                   help="Only show what would be run, don't execute")
    p.add_argument("--no-audit", action="store_true",
                   help="Disable audit_analyzer services (DuckDB+FAISS) — "
                        "use for CI/short runs without DSN")
    p.add_argument("--log-file", default=None,
                   help="Path to progress log file. If omitted, defaults to "
                        "<output-dir>/progress.log when running.")
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


def cleanup_old_runs(keep_last: int = 20, runs_dir: str | Path | None = None) -> int:
    """Удаление старейших каталогов прогонов, кроме последних N.

    Args:
        keep_last: Сколько последних прогонов сохранять (по времени изменения).
        runs_dir: Каталог прогонов (по умолчанию benchmarks/results/runs).

    Returns:
        Число удалённых каталогов.
    """
    base = Path(runs_dir) if runs_dir else RESULTS_DIR
    if keep_last <= 0 or not base.is_dir():
        return 0
    dirs = sorted(
        (d for d in base.iterdir() if d.is_dir()),
        key=lambda d: d.stat().st_mtime,
    )
    removed = 0
    for old_dir in dirs[:-keep_last]:
        try:
            shutil.rmtree(old_dir)
            removed += 1
            print(f"  Cleanup: removed old run dir {old_dir.name}")
        except Exception as e:
            logger.warning("Failed to remove {}: {}", old_dir, e)
    if removed:
        print(f"  Cleanup: removed {removed} old run dir(s), keeping last {keep_last}")
    return removed


def _cleanup_item(item: BenchItem, bot: Any) -> None:
    """Удаление файлов, созданных агентом во время выполнения задания.

    Удаляет файлы, заявленные в ``expect.check_file`` (задания/шаги), а также
    все пути/глобы из ``item.cleanup`` (например ``bench_test_report.txt`` или
    ``logs/bench_*.txt``), чтобы избежать утечки состояния между тестами.
    Пути резолвятся относительно workspace агента; ошибки игнорируются.

    Args:
        item: Задание бенчмарка.
        bot: Экземпляр агента Nanobot.
    """
    workspace = None
    try:
        if hasattr(bot, "_loop") and hasattr(bot._loop, "workspace"):
            workspace = Path(bot._loop.workspace)
    except Exception:
        workspace = None
    if not workspace:
        workspace = BENCH_WORKSPACE_DIR
    patterns: list[str] = list(item.cleanup or [])
    if item.type == "single" and item.expect:
        if item.expect.check_file:
            patterns.append(item.expect.check_file)
    elif item.type == "multi_step" and item.steps:
        for s in item.steps:
            if s.expect.check_file:
                patterns.append(s.expect.check_file)
    for pattern in set(patterns):
        candidates: list[Path] = []
        p = Path(pattern)
        if p.is_absolute():
            # Абсолютный путь/глоб
            base = p.parent if any(ch in pattern for ch in "*?[") else p.parent
            candidates = list(p.parent.glob(p.name)) if any(ch in pattern for ch in "*?[") else [p]
        else:
            # Глоб или относительный путь внутри workspace
            if any(ch in pattern for ch in "*?["):
                candidates = list(workspace.glob(pattern))
            else:
                candidates = [workspace / pattern]
        for fpath in candidates:
            try:
                if fpath.is_dir():
                    # Удаляем только пустые/созданные агентом деревья сверху вниз
                    for leaf in sorted(fpath.rglob("*"), reverse=True):
                        if leaf.is_file():
                            leaf.unlink()
                        elif leaf.is_dir():
                            leaf.rmdir()
                    fpath.rmdir()
                elif fpath.exists():
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
        t_step_start = time.time()
        hook = BenchmarkHook()
        step_index = step.step
        _emit(f"    Шаг {step_index}/{total_steps} ({datetime.now().strftime('%H:%M:%S')}): {step.question[:70]}")

        try:
            result = await bot.run(
                message=step.question,
                session_key=session_key,
                hooks=[hook],
            )
        except Exception as e:
            logger.error("Error in step {} of item {}: {}", step.step, item.id, e)
            _emit(f"      -> ОШИБКА ({time.time() - t_step_start:.1f}с): {e}")
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
    """Запустить все задания набора бенчмарков через ApplicationContext.

    По аналогии с gateway: поднимаем единый контекст приложения (агент +
    bus + session_manager + db_logging + audit_analyzer), дожидаемся
    initial_load из PG в DuckDB и прогрева FAISS-индексов — затем гоняем
    задания через ``ctx.agent`` (а не собранный вручную ``Nanobot``).

    Args:
        suite: Набор тестов для запуска.
        run_id: Идентификатор прогона.
        args: Аргументы командной строки.

    Returns:
        Результаты прогона всего набора.
    """
    from lib.core.application_context import ApplicationContext
    from lib.services.llm_config import ensure_llm_env, resolve_llm_config

    # Переопределение модели из CLI (если задано). ``ensure_llm_env()``
    # поднимает LLM_API_KEY в env, чтобы ConfigService смог резолвить
    # ${LLM_API_KEY}-плейсхолдеры в config.json.
    ensure_llm_env()
    llm_override = {"llm_model": args.model} if args.model else None
    llm = resolve_llm_config(llm_override)

    # ApplicationContext внутри читает config.json и собирает AgentLoop
    # на модели, указанной в agents.defaults.model. Поэтому override модели
    # через --model делаем ДО create(): временно модифицируем config.json,
    # после прогона восстанавливаем оригинал.
    config_json_path = BENCH_SCRIPT_DIR / "config.json"
    original_config: str | None = None
    if llm_override and config_json_path.is_file():
        try:
            original_config = config_json_path.read_text(encoding="utf-8")
            data = json.loads(original_config)
            agents = data.setdefault("agents", {}).setdefault("defaults", {})
            agents["model"] = llm["model"]
            agents["provider"] = llm["provider"]
            if llm["provider"]:
                providers = data.setdefault("providers", {})
                prov_cfg = providers.setdefault(llm["provider"], {})
                if llm["api_key"]:
                    prov_cfg["apiKey"] = llm["api_key"]
                    prov_cfg["api_key"] = llm["api_key"]
                if llm["api_base"]:
                    prov_cfg["apiBase"] = llm["api_base"]
                    prov_cfg["api_base"] = llm["api_base"]
            config_json_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as exc:
            logger.warning("Не удалось применить --model override: {}", exc)
            original_config = None

    # Контекст приложения: поднимает Bus + SessionStorage + DbLogging +
    # AuditSyncService + AuditMemoryStore + PreloadService + AgentLoop.
    # enable_audit=False при --no-audit (только локальные прогоны без DSN).
    enable_audit = not getattr(args, "no_audit", False)
    try:
        ctx = ApplicationContext.create(
            script_dir=BENCH_SCRIPT_DIR,
            workspace_dir=BENCH_WORKSPACE_DIR,
            enable_db_logging=True,
            enable_audit=enable_audit,
        )
    finally:
        # Восстанавливаем config.json даже при ошибке инициализации
        # (иначе следующие запуски будут гонять на чужой модели).
        if original_config is not None:
            try:
                config_json_path.write_text(original_config, encoding="utf-8")
            except Exception as exc:  # pragma: no cover
                logger.warning("Не удалось восстановить config.json: {}", exc)
            original_config = None

    # Коллбэки синка ставим ДО ctx.start() — иначе worker-тред успеет
    # сделать initial_load раньше и данные не попадут в DuckDB-кэш
    # (см. комментарий в gateway.py:50-77).
    first_sync_event: "asyncio.Event | None" = None
    audit_ready = (
        ctx.audit_sync_service is not None and ctx.audit_memory_store is not None
    )
    if audit_ready:
        ctx.audit_memory_store.open()
        ctx.audit_sync_service.set_on_new_records_callback(
            ctx.audit_memory_store.upsert_records
        )
        prev_cb = getattr(ctx.audit_sync_service, "_on_sync_callback", None)
        first_sync_event = asyncio.Event()

        def _on_first_sync() -> None:
            if first_sync_event is not None:
                first_sync_event.set()

        def _wrapped() -> None:
            _on_first_sync()
            if prev_cb is not None:
                try:
                    prev_cb()
                except Exception:
                    pass

        ctx.audit_sync_service.set_on_sync_callback(_wrapped)

    ctx.start()

    try:
        if audit_ready:
            try:
                await asyncio.wait_for(first_sync_event.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                print(
                    "[yellow]⚠[/yellow] audit_analyzer initial load timeout "
                    "(>30s), прогон на текущем состоянии DuckDB-кэша"
                )
            else:
                print("[green]✓[/green] audit_analyzer initial load received")

            loaded = await ctx.preload_service.preload_vector_indexes(
                ctx.audit_memory_store
            )
            if not loaded:
                print(
                    "[dim]audit_analyzer vector indexes: нет данных в кэше[/dim]"
                )
            else:
                for item in loaded:
                    print(
                        f"[green]✓[/green] vector index '{item['index_name']}' "
                        f"built in memory: {item['vectors']} vectors"
                    )
        else:
            if enable_audit:
                print(
                    "[yellow]⚠[/yellow] audit_analyzer services недоступны "
                    "(нет DSN) — задания с SQL/vector могут работать медленнее"
                )

        # ctx.agent — это AgentLoop, а _run_item/_run_single/_run_multi_step
        # ожидают интерфейс Nanobot.run(message=..., session_key=..., hooks=...).
        # Оборачиваем в Nanobot, чтобы не дублировать сигнатуру.
        from nanobot import Nanobot
        bot = Nanobot(ctx.agent, config=ctx.config)

        start_time = time.time()
        results: list[BenchResult] = []

        suite_start = time.time()
        _emit(f"=== Прогон набора: {suite.name} ({len(suite.items)} заданий) ===")
        _emit(f"Начало: {datetime.now().isoformat(timespec='seconds')}")
        _emit("")

        for idx, item in enumerate(suite.items, 1):
            t_item_start = time.time()
            _emit(f"[{idx}/{len(suite.items)}] {item.id} (сложность={item.difficulty}) — старт {datetime.now().strftime('%H:%M:%S')}")
            bench_result = await _run_item(item, run_id, bot, args.verbose)
            results.append(bench_result)

            status = "ПРОЙДЕН" if bench_result.passed else "ПРОВАЛЕН"
            elapsed = time.time() - t_item_start
            _emit(f"  -> {status} | балл={bench_result.total_score:.2%} "
                  f"| итераций={bench_result.total_iterations} "
                  f"| агент={bench_result.duration_sec:.1f}с "
                  f"| общее={elapsed:.1f}с")
            if bench_result.error:
                _emit(f"     ОШИБКА: {bench_result.error}")
            _emit("")

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
            run_id=run_id,
            artifacts_dir=str(RESULTS_DIR / run_id),
        )
        return suite_result
    finally:
        # Корректный shutdown в стиле gateway.py:158-172
        try:
            await ctx.agent.close_mcp()
        except Exception:
            pass
        try:
            ctx.agent.stop()
        except Exception:
            pass
        try:
            flushed = ctx.agent.sessions.flush_all()
            if flushed:
                logger.info("Flushed {} session(s) to disk", flushed)
        except Exception:
            pass
        ctx.stop()


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
    from lib.utils.logging_utils import configure_loguru

    configure_loguru(log_level)

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

    run_id = _generate_run_id()
    output_dir = args.output or str(RESULTS_DIR / run_id)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # Открываем прогресс-лог (если запрошен) ДО _run_suite, чтобы
    # ловить ход выполнения с самого начала. По умолчанию — пишем в
    # <output-dir>/progress.log.
    global _LOG_FILE
    log_path = Path(args.log_file) if args.log_file else (output_path / "progress.log")
    _LOG_FILE = open(log_path, "w", encoding="utf-8", errors="replace")
    _emit(f"=== Лог прогона бенчмарка ===")
    _emit(f"ID прогона: {run_id}")
    _emit(f"Набор:      {suite.name} ({len(suite.items)} заданий)")
    _emit(f"Старт:      {datetime.now().isoformat(timespec='seconds')}")
    _emit(f"Отчёты:     {output_path}")
    _emit(f"DSN:        {bool(args.db)} | audit_analyzer: {not getattr(args, 'no_audit', False)}")
    _emit("")

    try:
        suite_result = await _run_suite(suite, run_id, args)
    finally:
        # Даже при падении записываем хвост лога перед закрытием.
        try:
            _emit(f"=== Прогон завершён в {datetime.now().isoformat(timespec='seconds')} ===")
        except Exception:
            pass
        if _LOG_FILE is not None and not _LOG_FILE.closed:
            _LOG_FILE.close()
        _LOG_FILE = None

    suite_result.artifacts_dir = str(output_path)

    json_path = save_json_report(suite_result, output_path)
    md_path = save_markdown_report(suite_result, output_path)

    _emit(f"JSON-отчёт:      {json_path}")
    _emit(f"Markdown-отчёт:  {md_path}")
    _emit(f"Лог прогресса:   {log_path}")

    if args.db:
        try:
            bdb = BenchmarkDB(dsn=args.db)
            bdb.ensure_tables()
            db_run_id = bdb.save_run(suite_result)
            if db_run_id:
                _emit(f"Saved to PostgreSQL: run_id={db_run_id}")
        except Exception as e:
            logger.error("Failed to save to PostgreSQL: {}", e)

    cleanup_old_runs(keep_last=args.keep_runs)

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
