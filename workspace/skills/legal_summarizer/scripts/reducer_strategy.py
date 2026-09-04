"""Compatibility shim — мигрирован на canonical (Этап 8).

Все функции этого модуля перенесены в
``workspace.skills.legal_summarizer.scripts.summarizer_canonical``:

* ``select_reduce_strategy`` → ``reduce_strategy_for_legacy``
* ``should_use_hierarchical_reduce`` → не нужен (canonical
  ``select_strategy`` принимает решение)

Этот модуль оставлен только для обратной совместимости и удалится в
Этапе 9, когда ``reducer.py`` и ``reducer_impl.py`` будут
переведены на canonical ``HierarchicalReducer``.
"""

from workspace.skills.legal_summarizer.scripts.reducer_models import (
    ReduceStrategy,
)

__all__ = [
    "ReduceStrategy",
    "select_reduce_strategy",
    "should_use_hierarchical_reduce",
]


def should_use_hierarchical_reduce(*args, **kwargs) -> bool:
    """Legacy shim — больше не используется в production.

    Мигрирован на canonical ``reduce_strategy_for_legacy`` и
    ``select_strategy``.
    """
    raise NotImplementedError(
        "should_use_hierarchical_reduce deprecated; "
        "use summarizer_canonical.reduce_strategy_for_legacy",
    )


def select_reduce_strategy(*args, **kwargs) -> ReduceStrategy:
    """Legacy shim — больше не используется в production.

    Мигрирован на canonical ``reduce_strategy_for_legacy``.
    """
    raise NotImplementedError(
        "select_reduce_strategy deprecated; "
        "use summarizer_canonical.reduce_strategy_for_legacy",
    )