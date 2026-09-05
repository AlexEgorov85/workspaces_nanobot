"""Unified execution planner (PLAN §23, Этапы 7, 19).

Единственный селектор для выбора стратегии:

* ``"direct"`` — один LLM-вызов.
* ``"map_flat"`` — map → flat reduce.
* ``"map_hierarchical"`` — map → hierarchical reduce (multi-round).

Правила (PLAN §23):

* total_tokens ≤ direct_threshold → ``"direct"``.
* total_sections ≥ hierarchical_threshold → ``"map_hierarchical"``.
* иначе → ``"map_flat"``.

Все параметры — через ``ExecutionPolicy`` (Этап 7). Packing —
``pack_chunks_with_adjacent`` с ``AdjacentPackingConfig``,
сконструированным из той же policy.
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
    """Параметры unified execution strategy (PLAN §25, Этап 7).

    Все параметры, влияющие на построение ExecutionPlan:

    * ``direct_threshold_tokens``: ниже → ``"direct"``.
    * ``hierarchical_section_threshold``: число meaningful sections.
    * ``chars_per_token``: для ``TokenEstimator`` и ``AdjacentPackingConfig``.
    * ``max_sections_per_batch``: передаётся в ``AdjacentPackingConfig``.
    * ``per_batch_token_budget``: token budget на batch.
    * ``allow_table_table_batch``: разрешить table + table в один batch.

    Изменение любого поля меняет реальный ExecutionPlan.
    """

    direct_threshold_tokens: int = 12_000
    hierarchical_section_threshold: int = 3
    chars_per_token: float = 3.5
    max_sections_per_batch: int = 2
    per_batch_token_budget: int = 6_000
    allow_table_table_batch: bool = False


def _count_meaningful_sections(struct: DocumentStructure) -> int:
    """Число meaningful sections (Этап 6).

    Критерий (Этап 4 + Этап 6):

    * ``node_type == "section"``;
    * ``title.strip() != ""``;
    * имеет валидный range ``[start_block, end_block]`` (одно-блочные
      секции ``start_block == end_block`` — допустимы и считаются
      meaningful, если у них есть title).

    Не используется ``end_block > start_block`` как критерий.
    """
    if struct.root_id not in struct.nodes:
        return 0
    count = 0
    for n in struct.nodes.values():
        if n.node_type != "section":
            continue
        if not n.title.strip():
            continue
        if n.start_block > n.end_block:
            continue
        if n.start_block < 0:
            continue
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

    Этап 7: ``ExecutionPolicy`` — единый источник параметров для
    strategy selection и adjacent packing. ``AdjacentPackingConfig``
    формируется **из** ``ExecutionPolicy``; никаких скрытых defaults.
    """
    from workspace.skills.legal_summarizer.scripts.structure.execution_plan import (
        build_direct_plan, build_map_plan,
    )
    from workspace.skills.legal_summarizer.scripts.structure.adjacent_packing import (
        AdjacentPackingConfig,
        pack_chunks_with_adjacent,
    )

    cfg = policy or ExecutionPolicy()
    estimator = TokenEstimator(TokenEstimatorConfig(chars_per_token=cfg.chars_per_token))
    strategy = select_strategy(struct, chunks, policy=cfg)

    if strategy == "direct":
        return build_direct_plan(
            chunks, document_id=document_id, token_estimator=estimator,
        )

    packing_cfg = AdjacentPackingConfig(
        max_sections_per_batch=cfg.max_sections_per_batch,
        per_batch_token_budget=cfg.per_batch_token_budget,
        chars_per_token=cfg.chars_per_token,
        allow_table_table_batch=cfg.allow_table_table_batch,
    )
    batches_input = pack_chunks_with_adjacent(chunks, config=packing_cfg)
    return build_map_plan(
        chunks, document_id=document_id, strategy=strategy,
        batches_input=batches_input, token_estimator=estimator,
    )


__all__ = [
    "ExecutionPolicy",
    "select_strategy",
    "build_execution_plan",
]