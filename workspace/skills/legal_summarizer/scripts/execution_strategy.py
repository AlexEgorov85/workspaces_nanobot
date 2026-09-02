"""Adaptive execution strategy.

Три стратегии:

* ``DIRECT`` — документ помещается в один LLM call.
* ``MAP_FLAT`` — chunks помещаются в один reduce context (1 LLM call).
* ``MAP_HIERARCHICAL`` — нужен section reduce (N section calls + 1 doc call).

Правила выбора:

* ``DIRECT`` — если ``estimated_tokens <= direct_budget_tokens``.
* ``MAP_FLAT`` — если ``estimated_tokens <= reduce_budget_tokens`` (всё
  помещается в один reduce context).
* ``MAP_HIERARCHICAL`` — иначе (нужна иерархия).

Выбор **детерминированный** (без LLM-вызовов) на основе ``DocumentStats``.

NOTE: top-level, не ``pipeline/strategy.py`` — пакет ``pipeline``
планируется после переименования ``llm.py``. Когда это произойдёт,
мигрируем на целевую структуру.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from workspace.skills.legal_summarizer.scripts.document_stats import (
    DocumentStats,
)


class ExecutionStrategy(Enum):
    """Стратегия execution для документа."""

    DIRECT = "direct"
    MAP_FLAT = "map_flat"
    MAP_HIERARCHICAL = "map_hierarchical"


@dataclass(frozen=True)
class StrategyConfig:
    """Параметры для ``select_execution_strategy``."""

    direct_budget_tokens: int
    """Бюджет для одного DIRECT LLM call (≈ context_window - system - output_reserve)."""

    reduce_budget_tokens: int
    """Бюджет для reduce context (≈ context_window - system - instruction - output_reserve)."""


def select_execution_strategy(
    stats: DocumentStats,
    config: StrategyConfig,
) -> ExecutionStrategy:
    """Детерминированный выбор стратегии.

    Args:
        stats: ``DocumentStats`` (без LLM, дешёвые метрики).
        config: ``StrategyConfig`` с прямым и reduce бюджетами.

    Returns:
        ExecutionStrategy.

    Examples:
        >>> stats = DocumentStats(chars=10000, estimated_tokens=2500, ...)
        >>> config = StrategyConfig(direct_budget_tokens=30000, reduce_budget_tokens=20000)
        >>> select_execution_strategy(stats, config).value
        'direct'
    """
    if stats.estimated_tokens <= config.direct_budget_tokens:
        return ExecutionStrategy.DIRECT
    if stats.estimated_tokens <= config.reduce_budget_tokens:
        return ExecutionStrategy.MAP_FLAT
    return ExecutionStrategy.MAP_HIERARCHICAL


__all__ = [
    "ExecutionStrategy",
    "StrategyConfig",
    "select_execution_strategy",
]
