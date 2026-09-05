"""Регистратор вызовов pipeline для тестов Этапа 2.

Используется для проверки инварианта: ``run_canonical_pipeline``
вызывается ровно один раз на запуск.
"""

from __future__ import annotations

PIPELINE_CALLS: int = 0


def reset() -> None:
    global PIPELINE_CALLS
    PIPELINE_CALLS = 0


def record_pipeline_call() -> None:
    global PIPELINE_CALLS
    PIPELINE_CALLS += 1