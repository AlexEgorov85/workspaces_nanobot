"""Execution-level конфигурация для HierarchicalReducer.

Эти параметры описывают **execution policy** (как LLM-вызовы
группируются и объединяются при reduce-фазе), а не **доменную модель
документа**. Поэтому они живут в ``execution/``, а не в ``domain/``.

Единый источник истины для:

* ``HierarchicalReducerConfig`` — параметры reducer'а, пробрасываемые
  в ``reduce_chunks_hierarchical`` и ``reduce_sections_to_document``.
* ``MID_REDUCE_GROUP_SIZE`` / ``MAX_REDUCE_ROUNDS`` — константы,
  используемые и reducer'ом, и estimator'ом (в ``application/``) для
  гарантии, что ``actual_llm_calls <= estimated_llm_calls``.

Если добавить ещё один потребитель этих констант — он обязан
импортировать отсюда, а не дублировать литералы.
"""

from __future__ import annotations

from dataclasses import dataclass


MID_REDUCE_GROUP_SIZE = 3
MAX_REDUCE_ROUNDS = 4


@dataclass(frozen=True)
class HierarchicalReducerConfig:
    """Параметры HierarchicalReducer'а."""

    group_size: int = 3
    max_rounds: int = 4
    input_budget_chars: int = 60_000
    section_summary_max_chars: int = 4_000


__all__ = [
    "HierarchicalReducerConfig",
    "MID_REDUCE_GROUP_SIZE",
    "MAX_REDUCE_ROUNDS",
]
