"""Context Packing для legal_summarizer (Phase 2B).

``packing.py`` — facade. Внутренняя структура разделена на:

* ``packing_models.py`` — dataclasses (ContextBatch, PackingConfig).
* ``packing_impl.py`` — packer (``pack_chunks``, ``_build_batches_strict``).

Этот модуль — единственная точка импорта для summarizer.py, llm_calls.py,
pipeline.py, prompts.py и тестов. Public API сохранён — back-compat не
нарушен.

Section-locality greedy first-fit: последовательно добавляем chunk в
текущий ContextBatch, **никогда** не перепрыгивая через section boundary.

Алгоритм:
  1. Идём по chunks в document order.
  2. Открываем новый batch.
  3. Пытаемся добавить следующий chunk, если:
     а) его section_path совпадает с первым chunk'ом текущего batch;
     б) суммарный tokens ≤ budget.available_chunk_tokens.
  4. Если одно из условий нарушено — закрываем batch и открываем новый.

``PackingConfig.allow_adjacent_sections=True`` разрешает «заимствовать»
chunks из соседнего section_path при наличии бюджета. По умолчанию
выключено (back-compat).
"""
from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.packing_impl import (
    _BATCH_OVERHEAD_TOKENS,
    pack_chunks,
)
from workspace.skills.legal_summarizer.scripts.packing_models import (
    ContextBatch,
    PackingConfig,
)
from workspace.skills.legal_summarizer.scripts.token_budget import (
    TokenBudget as _TokenBudget,
)

# Back-compat: исходное имя ``TokenBudget``.
TokenBudget = _TokenBudget

__all__ = [
    "TokenBudget",
    "ContextBatch",
    "PackingConfig",
    "pack_chunks",
    "_BATCH_OVERHEAD_TOKENS",
]