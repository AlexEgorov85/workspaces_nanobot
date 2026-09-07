"""Hierarchical config + constants (SPECIAL 1, canonical copy)."""

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

__all__ = ['HierarchicalReducerConfig', 'MID_REDUCE_GROUP_SIZE', 'MAX_REDUCE_ROUNDS']
