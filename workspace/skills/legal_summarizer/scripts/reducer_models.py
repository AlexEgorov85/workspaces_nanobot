"""Reducer models — выделено из ``reducer.py`` в этапе 21.

Только dataclasses + enum. Никакой логики.

Используется через:
* ``reducer.py`` (facade с re-exports)
* ``reducer_strategy.py`` (selector)
* ``reducer_impl.py`` (flat/hierarchical impl)
* ``summarizer.py``
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ReduceStrategy(Enum):
    """Strategy selector для reducer'а."""

    FLAT = "flat"
    HIERARCHICAL = "hierarchical"


@dataclass
class ReduceStats:
    """Метрики LLM-вызовов в reducer'е.

    Разделены по фазам (invariant #19): нет единого hard-assertion.
    """

    map_calls: int = 0
    section_reduce_calls: int = 0
    section_trim_calls: int = 0
    document_reduce_calls: int = 0
    retries: int = 0

    def total_llm_calls(self) -> int:
        return (
            self.map_calls
            + self.section_reduce_calls
            + self.section_trim_calls
            + self.document_reduce_calls
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "map_calls": self.map_calls,
            "section_reduce_calls": self.section_reduce_calls,
            "section_trim_calls": self.section_trim_calls,
            "document_reduce_calls": self.document_reduce_calls,
            "reduce_calls": self.section_reduce_calls
            + self.section_trim_calls
            + self.document_reduce_calls,
            "total_llm_calls": self.total_llm_calls(),
            "retries": self.retries,
        }


@dataclass
class ReduceConfig:
    """Параметры reducer'а."""

    instruction_tokens_per_section_reduce: int = 200
    instruction_tokens_per_document_reduce: int = 200
    chars_per_token: float = 3.5
    section_summary_max_chars: int = 12000
    # Для select_reduce_strategy.
    reduce_strategy_min_sections: int = 2
    """Минимальное число meaningful_sections для hierarchical. Ниже —
    flat (даже если token budget не помещается)."""


@dataclass
class ReduceResult:
    """Результат reduce."""

    final_summary: str
    section_summaries: dict[str, str]
    stats: ReduceStats
    strategy: str


# Принимаем любой callable для LLM-вызовов (для тестов мок).
LLMRunner = Any


__all__ = [
    "ReduceStrategy",
    "ReduceStats",
    "ReduceConfig",
    "ReduceResult",
    "LLMRunner",
]