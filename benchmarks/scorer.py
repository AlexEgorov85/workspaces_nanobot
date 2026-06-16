"""Подсчёт взвешенных оценок для результатов бенчмарков.

Определяет веса для каждого типа проверки и функции расчёта
итоговых баллов для одношаговых, многошаговых заданий и отдельных шагов.
"""

from __future__ import annotations

from benchmarks.models import BenchExpect, BenchItem, BenchResult, CheckResult, EvalResult, StepResult

# Веса по умолчанию для каждого типа проверки.
# Используются при расчёте взвешенной средней оценки.
# Нормализация весов происходит внутри _weighted_score.
CHECK_WEIGHTS: dict[str, float] = {
    # tools — вес проверки инструментов
    "tools": 0.20,
    "keywords_include": 0.15,
    "keywords_exclude": 0.10,
    "iterations": 0.15,
    "skills": 0.10,
    "file_exists": 0.15,
    "file_content": 0.15,
    "llm_judge": 0.20,
}


def score_item(item: BenchItem, eval_result: EvalResult) -> BenchResult:
    """Расчёт результата задания на основе оценки (без дополнительных метаданных).

    Args:
        item: Задание бенчмарка.
        eval_result: Результат оценки ответа агента.

    Returns:
        Результат выполнения задания с взвешенным баллом.
    """
    weighted = _weighted_score(eval_result.checks)
    return BenchResult(
        item_id=item.id,
        item_name=item.name,
        difficulty=item.difficulty,
        passed=eval_result.passed,
        total_score=weighted,
        checks=eval_result.checks,
    )


def score_single(
    item: BenchItem,
    eval_result: EvalResult,
    response: str | None = None,
    tools_used: list[str] | None = None,
    skills_activated: set[str] | None = None,
    iterations: int = 0,
    duration_sec: float = 0.0,
) -> BenchResult:
    """Расчёт полного результата одношагового задания с метаданными выполнения.

    Args:
        item: Задание бенчмарка.
        eval_result: Результат оценки ответа агента.
        response: Текст ответа агента.
        tools_used: Список использованных инструментов.
        skills_activated: Множество активированных навыков.
        iterations: Число итераций.
        duration_sec: Длительность выполнения.

    Returns:
        Результат задания с заполненными метаданными.
    """
    weighted = _weighted_score(eval_result.checks)
    llm_judge = _find_check_score(eval_result.checks, "llm_judge")

    return BenchResult(
        item_id=item.id,
        item_name=item.name,
        difficulty=item.difficulty,
        passed=eval_result.passed,
        total_score=round(weighted, 4),
        response=response,
        tools_used=tools_used or [],
        skills_activated=list(skills_activated) if skills_activated else [],
        total_iterations=iterations,
        duration_sec=duration_sec,
        llm_judge_score=llm_judge,
        checks=eval_result.checks,
    )


def score_step(
    step_index: int,
    weight: float,
    eval_result: EvalResult,
    response: str | None = None,
    tools_used: list[str] | None = None,
    iterations: int = 0,
    duration_sec: float = 0.0,
) -> StepResult:
    """Расчёт результата одного шага многошагового задания.

    Args:
        step_index: Номер шага.
        weight: Вес шага в итоговой оценке.
        eval_result: Результат оценки ответа агента на шаге.
        response: Ответ агента на шаге.
        tools_used: Инструменты, использованные на шаге.
        iterations: Число итераций на шаге.
        duration_sec: Длительность шага.

    Returns:
        Результат шага с метаданными.
    """
    weighted = _weighted_score(eval_result.checks)
    return StepResult(
        step=step_index,
        weight=weight,
        passed=eval_result.passed,
        score=round(weighted, 4),
        response=response or "",
        tools_used=tools_used or [],
        iterations=iterations,
        duration_sec=duration_sec,
        checks=eval_result.checks,
    )


def score_multi_step(item: BenchItem, step_results: list[StepResult]) -> BenchResult:
    """Агрегация результатов всех шагов в итоговый результат многошагового задания.

    Итоговый балл = 80% взвешенная сумма шагов + 20% доля пройденных шагов.

    Args:
        item: Задание бенчмарка.
        step_results: Список результатов по каждому шагу.

    Returns:
        Итоговый результат задания.
    """
    if not step_results:
        return BenchResult(
            item_id=item.id,
            passed=False,
            total_score=0.0,
        )

    total_weight = sum(sr.weight for sr in step_results)
    if total_weight == 0:
        total_weight = 1.0

    weighted_sum = sum(sr.score * sr.weight for sr in step_results)
    final_score = weighted_sum / total_weight

    all_passed = all(sr.passed for sr in step_results)
    all_tools = set()
    total_iterations = 0
    total_duration = 0.0
    all_checks = []

    for sr in step_results:
        all_tools.update(sr.tools_used)
        total_iterations += sr.iterations
        total_duration += sr.duration_sec
        all_checks.extend(sr.checks)

    completeness = len([sr for sr in step_results if sr.passed]) / len(step_results)
    final_score = final_score * 0.8 + completeness * 0.2

    return BenchResult(
        item_id=item.id,
        item_name=item.name,
        difficulty=item.difficulty,
        passed=all_passed and final_score >= 0.5,
        total_score=round(final_score, 4),
        steps=step_results,
        tools_used=sorted(all_tools),
        total_iterations=total_iterations,
        duration_sec=total_duration,
        checks=all_checks,
    )


def _weighted_score(checks: list[CheckResult]) -> float:
    """Расчёт взвешенного среднего балла по всем проверкам.

    Args:
        checks: Список результатов проверок.

    Returns:
        Взвешенный средний балл (0.0–1.0).
    """
    if not checks:
        return 0.0
    total_weight = 0.0
    weighted_sum = 0.0
    for c in checks:
        w = CHECK_WEIGHTS.get(c.check, 0.10)
        weighted_sum += c.score * w
        total_weight += w
    if total_weight == 0:
        return 0.0
    return weighted_sum / total_weight


def _find_check_score(checks: list[CheckResult], name: str) -> float | None:
    """Поиск балла конкретной проверки по её имени.

    Args:
        checks: Список результатов проверок.
        name: Имя проверки (например "llm_judge").

    Returns:
        Балл проверки или None, если проверка не найдена.
    """
    for c in checks:
        if c.check == name:
            return c.score
    return None
