"""Reducer strategy — выделено из ``reducer.py``.

Strategy selector: ``should_use_hierarchical_reduce`` (legacy criterion)
и ``select_reduce_strategy`` (token-budget first criterion).

Реализация flat/hierarchical reduce — в ``reducer_impl.py``.
"""
from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.document_stats import (
    DocumentStats,
)
from workspace.skills.legal_summarizer.scripts.reducer_models import (
    ReduceStrategy,
)
from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.sections import (
    SectionTree,
    count_meaningful_sections,
)


def should_use_hierarchical_reduce(
    tree: SectionTree | None,
    chunks: list[Chunk],
    *,
    threshold: int = 3,
) -> bool:
    """Решает, нужен ли hierarchical reduce (LEGACY criterion).

    Использует ``count_meaningful_sections`` (top-level heading'и или body ≥100 chars).
    Если tree=None (legacy manifest) — возвращает False (flat reduce).

    NOTE: эта функция оставлена для back-compat. Новый критерий —
    ``select_reduce_strategy``, который учитывает token budget первым
    критерием, sections — дополнительным.
    """
    if tree is None:
        return False
    chars_by_ord = {c.index: c.char_count for c in chunks}
    meaningful = count_meaningful_sections(tree, _build_fake_blocks(chars_by_ord))
    return meaningful >= threshold


def select_reduce_strategy(
    stats: DocumentStats,
    reduce_budget_tokens: int,
    *,
    min_sections_for_hierarchical: int = 2,
) -> ReduceStrategy:
    """Strategy selector для reducer'а.

    Принцип:
        * Главный вопрос — «поместятся ли все map summaries в reduce context?».
        * Если ``estimated_reduce_tokens ≤ reduce_budget_tokens`` → FLAT.
        * Иначе — HIERARCHICAL, но **только если** ``sections ≥ min_sections_for_hierarchical``.
        * Иначе (sections < min) → FLAT всё равно (иначе section_reduce неэффективен).

    Args:
        stats: DocumentStats (без LLM-вызовов, дешёвая статистика).
        reduce_budget_tokens: бюджет токенов для reduce-context (≈
            ``TokenBudget.context_window - system - output_reserve``).
        min_sections_for_hierarchical: минимальное число meaningful_sections
            (default 2). Меньше — FLAT (section_reduce не окупается).

    Returns:
        ReduceStrategy.FLAT или ReduceStrategy.HIERARCHICAL.
    """
    if stats.estimated_tokens <= reduce_budget_tokens:
        return ReduceStrategy.FLAT
    if stats.sections < min_sections_for_hierarchical:
        return ReduceStrategy.FLAT
    return ReduceStrategy.HIERARCHICAL


def _build_fake_blocks(chars_by_ord: dict[int, int]) -> tuple:
    """Создать tuple DocumentBlock-likes для count_meaningful_sections."""
    from workspace.skills.legal_summarizer.scripts.structure.physical import (
        DocumentBlock,
    )

    return tuple(
        DocumentBlock(
            block_id=f"b_{i:04d}",
            block_type="paragraph",
            content="x" * n,
            char_count=n,
            page_index=None,
            page_start=None,
            page_end=None,
            paragraph_index=None,
            table_index=None,
            ordinal=i,
            block_metadata={},
        )
        for i, n in chars_by_ord.items()
    )


__all__ = [
    "should_use_hierarchical_reduce",
    "select_reduce_strategy",
]