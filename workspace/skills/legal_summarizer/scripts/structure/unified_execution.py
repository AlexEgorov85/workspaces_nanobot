"""Unified execution planner (PLAN §23, Этап 23).

Один селектор для выбора стратегии:

* ``"direct"`` — один LLM-вызов.
* ``"map_flat"`` — map → flat reduce.
* ``"map_hierarchical"`` — map → hierarchical reduce (multi-round).

Сейчас в проекте есть:

* ``execution_strategy.select_execution_strategy`` — legacy (DIRECT/MAP_FLAT/MAP_HIERARCHICAL).
* ``reducer_strategy.should_use_hierarchical_reduce`` — другой criterion.
* ``reducer_strategy.select_reduce_strategy`` — token-budget first, sections second.
* ``summarizer._hierarchical_reduce_rounds`` — специфичная rounds logic.

PLAN §23 — один ``ExecutionPlanner``. Сейчас legacy API не трогаем
(back-compat); добавляем новый ``ExecutionPlanner`` с явной policy.

Правила (PLAN §23, объединены из существующих):

* total_tokens ≤ direct_threshold → ``"direct"``.
* total_sections ≥ hierarchical_threshold → ``"map_hierarchical"``.
* иначе → ``"map_flat"``.
"""

from __future__ import annotations

from dataclasses import dataclass

from workspace.skills.legal_summarizer.scripts.structure.execution_plan import (
    ExecutionPlan,
)
from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentStructure,
)
from workspace.skills.legal_summarizer.scripts.structure.token_estimator import (
    TokenEstimator, TokenEstimatorConfig,
)


@dataclass(frozen=True)
class ExecutionPolicy:
    """Параметры unified execution strategy (PLAN §25)."""

    direct_threshold_tokens: int = 12_000
    hierarchical_section_threshold: int = 3
    chars_per_token: float = 3.5


def _count_meaningful_sections(struct: DocumentStructure) -> int:
    """Число meaningful sections (с непустым title или дочерними)."""
    if struct.root_id not in struct.nodes:
        return 0
    count = 0
    root = struct.nodes[struct.root_id]
    for cid in root.children:
        n = struct.nodes.get(cid)
        if n is None:
            continue
        if n.node_type != "section":
            continue
        if not n.title.strip():
            continue
        if n.end_block > n.start_block:
            count += 1
    return count


def select_strategy(
    struct: DocumentStructure,
    chunks: tuple,
    *,
    policy: ExecutionPolicy | None = None,
) -> str:
    """Выбрать стратегию (``"direct"``/``"map_flat"``/``"map_hierarchical"``).

    PLAN §23: один ExecutionPlanner, одно решение.
    """
    cfg = policy or ExecutionPolicy()
    estimator = TokenEstimator(TokenEstimatorConfig(chars_per_token=cfg.chars_per_token))

    total_tokens = estimator.estimate_many([c.text for c in chunks])

    if total_tokens <= cfg.direct_threshold_tokens:
        return "direct"

    section_count = _count_meaningful_sections(struct)
    if section_count >= cfg.hierarchical_section_threshold:
        return "map_hierarchical"

    return "map_flat"


def build_execution_plan(
    struct: DocumentStructure,
    chunks: tuple,
    *,
    document_id: str,
    policy: ExecutionPolicy | None = None,
) -> ExecutionPlan:
    """Построить ``ExecutionPlan`` для выбранной стратегии.

    Это unified API — заменяет разрозненные legacy селекторы
    для **новых** consumers (Этап 45). Старые consumers продолжают
    использовать свои пути.
    """
    from workspace.skills.legal_summarizer.scripts.structure.execution_plan import (
        build_direct_plan, build_map_plan,
    )
    from workspace.skills.legal_summarizer.scripts.structure.adjacent_packing import (
        pack_chunks_with_adjacent,
    )

    cfg = policy or ExecutionPolicy()
    estimator = TokenEstimator(TokenEstimatorConfig(chars_per_token=cfg.chars_per_token))
    strategy = select_strategy(struct, chunks, policy=cfg)

    if strategy == "direct":
        return build_direct_plan(
            chunks, document_id=document_id, token_estimator=estimator,
        )

    batches_input = pack_chunks_with_adjacent(chunks)
    return build_map_plan(
        chunks, document_id=document_id, strategy=strategy,
        batches_input=batches_input, token_estimator=estimator,
    )


__all__ = [
    "ExecutionPolicy",
    "select_strategy",
    "build_execution_plan",
]