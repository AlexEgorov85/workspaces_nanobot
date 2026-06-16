"""Модуль оценки выполнения заданий бенчмарка.

Содержит функции проверки инструментов, навыков, ключевых слов,
файлов и LLM-судьи, а также агрегации итоговых оценок.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from loguru import logger

from benchmarks.hooks import BenchmarkHook
from benchmarks.models import BenchExpect, CheckResult, EvalResult


def evaluate(
    expect: BenchExpect,
    response: str | None,
    hook: BenchmarkHook,
    workspace: str | Path | None = None,
) -> EvalResult:
    """Полная оценка ответа агента по указанным ожиданиям.

    Args:
        expect: Ожидаемые критерии оценки.
        response: Текстовый ответ агента.
        hook: Хук с метриками выполнения.
        workspace: Рабочая директория для проверки файлов.

    Returns:
        Результат оценки (пройден/не пройден, итоговый балл, проверки).
    """
    checks: list[CheckResult] = []

    checks.append(_check_tools(expect.tools, hook.tools_used))
    checks.append(_check_skills(expect.skills, hook.skills))
    checks.append(_check_iterations(expect.max_iterations, hook.iterations))
    checks.append(_check_keywords_include(expect.keywords_include, response))
    checks.append(_check_keywords_exclude(expect.keywords_exclude, response))

    if expect.check_file:
        checks.append(_check_file_exists(expect.check_file, workspace))
    if expect.check_file_content:
        checks.append(_check_file_content(expect.check_file, expect.check_file_content, workspace))

    if expect.match_type == "llm_judge":
        checks.append(_check_llm_judge(expect, response, hook))

    total_score = _aggregate_score(checks)
    passed = total_score >= 0.5 and all(c.passed for c in _critical_checks(checks))

    return EvalResult(passed=passed, total_score=total_score, checks=checks)


def _check_tools(expected: list[str], actual: list[str]) -> CheckResult:
    """Проверка, что агент использовал все ожидаемые инструменты.

    Args:
        expected: Список ожидаемых инструментов.
        actual: Список реально использованных инструментов.

    Returns:
        Результат проверки с баллом 1.0 или 0.0.
    """
    if not expected:
        return CheckResult("tools", True, 1.0, "No tool expectations")
    missing = [t for t in expected if t not in actual]
    if missing:
        return CheckResult("tools", False, 0.0, f"Missing tools: {missing}")
    return CheckResult("tools", True, 1.0, f"All expected tools used: {expected}")


def _check_skills(expected: list[str], actual: set[str]) -> CheckResult:
    """Проверка, что агент активировал все ожидаемые навыки.

    Args:
        expected: Список ожидаемых навыков.
        actual: Множество реально активированных навыков.

    Returns:
        Результат проверки с баллом 1.0 или 0.0.
    """
    if not expected:
        return CheckResult("skills", True, 1.0, "No skill expectations")
    missing = [s for s in expected if s not in actual]
    if missing:
        return CheckResult("skills", False, 0.0, f"Missing skills: {missing}")
    return CheckResult("skills", True, 1.0, f"All expected skills activated: {expected}")


def _check_iterations(max_iterations: int, actual: int) -> CheckResult:
    """Проверка, что агент уложился в лимит итераций.

    Args:
        max_iterations: Максимально допустимое число итераций.
        actual: Фактическое число итераций.

    Returns:
        Результат проверки: 0.0 при превышении, от 0.1 до 1.0 при соблюдении.
    """
    if actual == 0:
        return CheckResult("iterations", False, 0.0, "No iterations recorded")
    if actual > max_iterations:
        ratio = max(max_iterations / actual, 0.0)
        return CheckResult("iterations", False, ratio, f"{actual} iterations > {max_iterations} max")
    efficiency = 1.0 - (actual - 1) / (max_iterations * 2)
    efficiency = max(0.1, min(1.0, efficiency))
    return CheckResult("iterations", True, efficiency, f"{actual} iterations within limit of {max_iterations}")


def _check_keywords_include(keywords: list[str], response: str | None) -> CheckResult:
    """Проверка наличия обязательных ключевых слов в ответе.

    Args:
        keywords: Список обязательных ключевых слов.
        response: Текстовый ответ агента.

    Returns:
        Результат проверки: 1.0, если все слова найдены, иначе 0.0.
    """
    if not keywords:
        return CheckResult("keywords_include", True, 1.0, "No keyword requirements")
    if not response:
        return CheckResult("keywords_include", False, 0.0, "No response to check")
    missing = [kw for kw in keywords if kw.lower() not in response.lower()]
    if missing:
        return CheckResult("keywords_include", False, 0.0, f"Missing keywords: {missing}")
    return CheckResult("keywords_include", True, 1.0, f"All keywords found: {keywords}")


def _check_keywords_exclude(keywords: list[str], response: str | None) -> CheckResult:
    """Проверка отсутствия запрещённых ключевых слов в ответе.

    Args:
        keywords: Список запрещённых ключевых слов.
        response: Текстовый ответ агента.

    Returns:
        Результат проверки: 1.0, если запрещённых слов нет, иначе 0.0.
    """
    if not keywords:
        return CheckResult("keywords_exclude", True, 1.0, "No forbidden keywords")
    if not response:
        return CheckResult("keywords_exclude", True, 1.0, "No response to check")
    found = [kw for kw in keywords if kw.lower() in response.lower()]
    if found:
        return CheckResult("keywords_exclude", False, 0.0, f"Forbidden keywords found: {found}")
    return CheckResult("keywords_exclude", True, 1.0, f"No forbidden keywords")


def _check_file_exists(file_path: str, workspace: str | Path | None) -> CheckResult:
    """Проверка существования файла в рабочей области.

    Args:
        file_path: Путь к файлу (относительный или абсолютный).
        workspace: Базовая рабочая директория.

    Returns:
        Результат проверки: 1.0, если файл существует, иначе 0.0.
    """
    full_path = _resolve_path(file_path, workspace)
    if full_path.exists():
        return CheckResult("file_exists", True, 1.0, f"File exists: {full_path}")
    return CheckResult("file_exists", False, 0.0, f"File not found: {full_path}")


def _check_file_content(file_path: str, expected_content: str, workspace: str | Path | None) -> CheckResult:
    """Проверка наличия ожидаемого содержимого в файле.

    Args:
        file_path: Путь к файлу.
        expected_content: Ожидаемое содержимое (поиск подстроки).
        workspace: Базовая рабочая директория.

    Returns:
        Результат проверки: 1.0, если содержимое найдено, иначе 0.0.
    """
    full_path = _resolve_path(file_path, workspace)
    if not full_path.exists():
        return CheckResult("file_content", False, 0.0, f"File not found: {full_path}")
    content = full_path.read_text(encoding="utf-8", errors="replace")
    if expected_content.lower() in content.lower():
        return CheckResult("file_content", True, 1.0, f"Expected content found in {full_path}")
    return CheckResult("file_content", False, 0.0, f"Expected content not found in {full_path}")


def _check_llm_judge(expect: BenchExpect, response: str | None, hook: BenchmarkHook) -> CheckResult:
    """Заглушка проверки LLM-судьёй.

    Args:
        expect: Ожидаемые критерии.
        response: Ответ агента.
        hook: Хук с метриками.

    Returns:
        Результат с фиксированным баллом 0.5 (LLM-судья пока не реализован).
    """
    return CheckResult("llm_judge", True, 0.5, "LLM judge not available, skipped")


def _resolve_path(file_path: str, workspace: str | Path | None) -> Path:
    """Преобразование пути в абсолютный с учётом рабочей директории.

    Args:
        file_path: Исходный путь к файлу.
        workspace: Базовая рабочая директория (может быть None).

    Returns:
        Абсолютный путь Path.
    """
    p = Path(file_path)
    if p.is_absolute():
        return p
    if workspace:
        return Path(workspace) / p
    return p


def _aggregate_score(checks: list[CheckResult]) -> float:
    """Усреднение баллов по всем проверкам.

    Args:
        checks: Список результатов проверок.

    Returns:
        Средний балл (0.0, если проверок нет).
    """
    if not checks:
        return 0.0
    return sum(c.score for c in checks) / len(checks)


def _critical_checks(checks: list[CheckResult]) -> list[CheckResult]:
    """Фильтрация критических проверок (инструменты, ключевые слова, файлы, LLM-судья).

    Args:
        checks: Полный список проверок.

    Returns:
        Список только критических проверок.
    """
    critical_names = {"tools", "keywords_include", "file_exists", "file_content", "llm_judge"}
    return [c for c in checks if c.check in critical_names]
