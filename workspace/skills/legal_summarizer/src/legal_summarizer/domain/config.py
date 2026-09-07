"""Backward-compat re-export.

Исторически ``HierarchicalReducerConfig`` / ``MID_REDUCE_GROUP_SIZE`` /
``MAX_REDUCE_ROUNDS`` жили в ``domain/``. Реальная реализация переехала
в ``legal_summarizer.execution.config`` (это execution policy, а не
доменная модель). Этот модуль сохранён ради обратной совместимости
тестов и CLI.

Новых потребителей писать сюда **не нужно** — импортируйте напрямую из
``legal_summarizer.execution.config``.
"""

from __future__ import annotations

from legal_summarizer.execution.config import (
    MAX_REDUCE_ROUNDS,
    MID_REDUCE_GROUP_SIZE,
    HierarchicalReducerConfig,
)


__all__ = [
    "HierarchicalReducerConfig",
    "MID_REDUCE_GROUP_SIZE",
    "MAX_REDUCE_ROUNDS",
]
