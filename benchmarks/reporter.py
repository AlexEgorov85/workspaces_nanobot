"""Формирование отчётов о результатах бенчмарков.

Поддерживает сохранение в JSON и Markdown форматах, группировку
по сложности и форматирование оценок.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from benchmarks.models import BenchResult, SuiteResult


def _score_label(score: float) -> str:
    """Текстовое обозначение уровня оценки.

    Args:
        score: Числовой балл (0.0–1.0).

    Returns:
        "EXCELLENT" при score >= 0.9, "GOOD" при >= 0.7,
        "SATISFACTORY" при >= 0.5, иначе "FAIL".
    """
    if score >= 0.9:
        return "EXCELLENT"
    if score >= 0.7:
        return "GOOD"
    if score >= 0.5:
        return "SATISFACTORY"
    return "FAIL"


def _difficulty_label(d: int) -> str:
    """Текстовое обозначение уровня сложности.

    Args:
        d: Числовой уровень сложности (1–10).

    Returns:
        "simple" при d <= 3, "medium" при d <= 7, иначе "hard".
    """
    if d <= 3:
        return "simple"
    if d <= 7:
        return "medium"
    return "hard"


def save_json_report(suite_result: SuiteResult, output_dir: str | Path) -> Path:
    """Сохранение результатов в JSON-формате.

    Создаёт summary.json и отдельные файлы detail/<item_id>.json
    для каждого задания.

    Args:
        suite_result: Результаты прогона набора.
        output_dir: Директория для сохранения отчётов.

    Returns:
        Путь к созданному файлу summary.json.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    summary_path = output_dir / "summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(_suite_to_dict(suite_result), f, indent=2, ensure_ascii=False)

    details_dir = output_dir / "detail"
    details_dir.mkdir(parents=True, exist_ok=True)

    for r in suite_result.results:
        item_path = details_dir / f"{r.item_id}.json"
        with open(item_path, "w", encoding="utf-8") as f:
            json.dump(_result_to_dict(r), f, indent=2, ensure_ascii=False)

    return summary_path


def save_markdown_report(suite_result: SuiteResult, output_dir: str | Path) -> Path:
    """Сохранение результатов в Markdown-формате.

    Создаёт summary.md с таблицами сводки, группировки по сложности
    и детальным отчётом по каждому заданию.

    Args:
        suite_result: Результаты прогона набора.
        output_dir: Директория для сохранения отчёта.

    Returns:
        Путь к созданному файлу summary.md.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    lines.append(f"# Benchmark Report: {suite_result.suite_name}")
    lines.append(f"**Date:** {suite_result.timestamp}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"| Metric | Value |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Total Items | {suite_result.total_items} |")
    lines.append(f"| Passed | {suite_result.passed_items} / {suite_result.total_items} |")
    lines.append(f"| Pass Rate | {_pct(suite_result.passed_items / suite_result.total_items) if suite_result.total_items else '0%'} |")
    lines.append(f"| Average Score | {_pct(suite_result.avg_score)} |")
    lines.append(f"| Total Score | {_pct(suite_result.total_score)} |")
    lines.append(f"| Duration | {suite_result.duration_sec:.1f}s |")
    lines.append("")

    by_difficulty = _group_by_difficulty(suite_result.results)
    if by_difficulty:
        lines.append("## By Difficulty")
        lines.append("")
        lines.append("| Level | Items | Passed | Pass Rate | Avg Score |")
        lines.append("|-------|-------|--------|-----------|-----------|")
        for label, items in sorted(by_difficulty.items()):
            passed = sum(1 for r in items if r.passed)
            avg = sum(r.total_score for r in items) / len(items) if items else 0
            lines.append(f"| {label} | {len(items)} | {passed} | {_pct(passed / len(items))} | {_pct(avg)} |")
        lines.append("")

    lines.append("## Results")
    lines.append("")
    lines.append("| ID | Name | Difficulty | Score | Passed | Iterations | Duration |")
    lines.append("|----|------|------------|-------|--------|------------|----------|")
    for r in suite_result.results:
        diff = _difficulty_label(r.difficulty)
        passed_mark = "PASS" if r.passed else "FAIL"
        name_short = r.item_name[:30] if r.item_name else r.item_id
        lines.append(f"| {r.item_id} | {name_short} | {diff} | {_pct(r.total_score)} | {passed_mark} | {r.total_iterations} | {r.duration_sec:.1f}s |")
    lines.append("")

    for r in suite_result.results:
        lines.append(f"### {r.item_id}")
        lines.append("")
        lines.append(f"- **Score:** {_pct(r.total_score)} ({_score_label(r.total_score)})")
        lines.append(f"- **Passed:** {r.passed}")
        lines.append(f"- **Tools Used:** {', '.join(r.tools_used) if r.tools_used else 'none'}")
        lines.append(f"- **Iterations:** {r.total_iterations}")
        lines.append(f"- **Duration:** {r.duration_sec:.1f}s")
        if r.error:
            lines.append(f"- **Error:** {r.error}")
        if r.checks:
            lines.append("- **Checks:**")
            for c in r.checks:
                status = "✓" if c.passed else "✗"
                lines.append(f"  - {status} **{c.check}**: {c.detail} (score: {c.score:.2f})")
        if r.steps:
            lines.append("- **Steps:**")
            for s in r.steps:
                status = "✓" if s.passed else "✗"
                lines.append(f"  - {status} Step {s.step}: score={s.score:.2f}, weight={s.weight}, iterations={s.iterations}")
        lines.append("")

    report_path = output_dir / "summary.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return report_path


def _suite_to_dict(suite_result: SuiteResult) -> dict[str, Any]:
    """Преобразование SuiteResult в словарь для JSON-сериализации.

    Args:
        suite_result: Результаты прогона набора.

    Returns:
        Словарь с данными набора.
    """
    return {
        "suite_name": suite_result.suite_name,
        "timestamp": suite_result.timestamp,
        "total_items": suite_result.total_items,
        "passed_items": suite_result.passed_items,
        "total_score": suite_result.total_score,
        "avg_score": suite_result.avg_score,
        "duration_sec": suite_result.duration_sec,
        "config": suite_result.config,
        "results": [_result_to_dict(r) for r in suite_result.results],
    }


def _result_to_dict(r: BenchResult) -> dict[str, Any]:
    """Преобразование BenchResult в словарь для JSON-сериализации.

    Args:
        r: Результат выполнения задания.

    Returns:
        Словарь с данными результата.
    """
    return {
        "item_id": r.item_id,
        "item_name": r.item_name,
        "difficulty": r.difficulty,
        "passed": r.passed,
        "total_score": r.total_score,
        "response": r.response,
        "error": r.error,
        "tools_used": r.tools_used,
        "skills_activated": r.skills_activated,
        "total_iterations": r.total_iterations,
        "duration_sec": r.duration_sec,
        "llm_judge_score": r.llm_judge_score,
        "checks": [{"check": c.check, "passed": c.passed, "score": c.score, "detail": c.detail} for c in r.checks],
        "steps": [
            {
                "step": s.step,
                "weight": s.weight,
                "passed": s.passed,
                "score": s.score,
                "response": s.response,
                "tools_used": s.tools_used,
                "iterations": s.iterations,
                "duration_sec": s.duration_sec,
                "checks": [{"check": c.check, "passed": c.passed, "score": c.score, "detail": c.detail} for c in s.checks],
            }
            for s in r.steps
        ],
    }


def _group_by_difficulty(results: list[BenchResult]) -> dict[str, list[BenchResult]]:
    """Группировка результатов по уровню сложности.

    Args:
        results: Список результатов заданий.

    Returns:
        Словарь {метка_сложности: список_результатов}.
    """
    groups: dict[str, list[BenchResult]] = {}
    for r in results:
        d = _difficulty_label(r.difficulty)
        groups.setdefault(d, []).append(r)
    return groups


def _pct(value: float) -> str:
    """Форматирование числа как процента с одним знаком после запятой.

    Args:
        value: Дробное число (0.0–1.0).

    Returns:
        Отформатированная строка, например "75.3%".

    Пример:
        >>> _pct(0.753)
        '75.3%'
    """
    return f"{value * 100:.1f}%"
