"""Hierarchical + Flat reduce для legal_summarizer (Phase 2B).

``reducer.py`` — facade. Внутренняя структура разделена на:

* ``reducer_models.py`` — dataclasses (ReduceStats/ReduceConfig/ReduceResult)
  + enum (ReduceStrategy).
* ``reducer_strategy.py`` — selector'ы (should_use_hierarchical_reduce,
  select_reduce_strategy).
* ``reducer_impl.py`` — реализация reduce_results, _reduce_hierarchical,
  _reduce_flat и helpers.

Этот модуль — единственная точка импорта для summarizer.py и тестов.
Public API сохранён — back-compat не нарушен.

Hierarchical reduce используется при ``meaningful_sections >= 3`` (см.
``count_meaningful_sections`` в ``structure/sections.py``):
  * Level 1: per-section reduce (один LLM call на section)
  * Level 2: document reduce (один LLM call, объединяет section_summaries)

Flat reduce используется при ``meaningful_sections < 3`` или для legacy
operations.

Stats разделены: map_calls / section_reduce_calls / section_trim_calls /
document_reduce_calls / retries. Нет hard-assertion
``total == batches + sections + 1`` (см. ARCHITECTURE.md, invariant #19).

См. ``workspace/skills/legal_summarizer/ARCHITECTURE.md`` invariants #9, #14, #15.

``select_reduce_strategy`` — критерий выбора ``flat`` vs ``hierarchical``
на основе token budget (главный) и числа sections (дополнительный).
``should_use_hierarchical_reduce`` оставлена для back-compat (используется
в legacy ``reduce_results``).
"""
from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.reducer_impl import (
    reduce_results,
)
from workspace.skills.legal_summarizer.scripts.reducer_models import (
    ReduceConfig,
    ReduceResult,
    ReduceStats,
    ReduceStrategy,
)
from workspace.skills.legal_summarizer.scripts.reducer_strategy import (
    select_reduce_strategy,
    should_use_hierarchical_reduce,
)

# Back-compat: исходные имена модулей (private имена перенесены в impl/strategy).
__all__ = [
    "ReduceStats",
    "ReduceConfig",
    "ReduceResult",
    "ReduceStrategy",
    "should_use_hierarchical_reduce",
    "select_reduce_strategy",
    "reduce_results",
]