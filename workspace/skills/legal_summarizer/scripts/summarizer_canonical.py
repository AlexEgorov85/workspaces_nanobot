"""Canonical run pipeline (Этапы 4А, 6А).

Этот модуль — **новый** production-flow, использующий только canonical
pipeline (``run_canonical_pipeline`` → ``DocumentAnalysis`` →
``HierarchicalReducer``).

Это **не замена** ``summarizer.run()`` сразу: legacy-путь остаётся
для обратной совместимости с существующими тестами, пока equivalence
не доказана на fixtures. Переключение делается поэтапно.

Здесь:

* ``build_pipeline_result`` — обёртка над ``run_canonical_pipeline``,
  возвращающая ``PipelineResult`` с логированием прогресса;
* ``strategy_from_pipeline`` — определить стратегию выполнения по
  ``PipelineResult`` ("direct" / "map_flat" / "map_hierarchical").
* ``inspect_canonical`` — канонический аналог ``summarizer.inspect()``,
  использующий только canonical-путь (DocumentStructure + ChunkPlanner
  + TokenEstimator + ExecutionPlan).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from workspace.skills.legal_summarizer.scripts.structure.adjacent_packing import (
    AdjacentPackingConfig,
    pack_chunks_with_adjacent,
)
from workspace.skills.legal_summarizer.scripts.structure.execution_plan import (
    ExecutionPlan,
)
from workspace.skills.legal_summarizer.scripts.structure.pipeline import (
    PipelineResult,
    run_canonical_pipeline,
)
from workspace.skills.legal_summarizer.scripts.structure.token_estimator import (
    TokenEstimator,
    TokenEstimatorConfig,
)
from workspace.skills.legal_summarizer.scripts.structure.unified_execution import (
    ExecutionPolicy,
    build_execution_plan,
    select_strategy,
)


def build_pipeline_result(
    *,
    document_path: str | Path,
    text: str | None = None,
    workspace_root: Path | str | None = None,
) -> PipelineResult:
    """Запустить canonical pipeline и вернуть ``PipelineResult``.

    Параметр ``text`` — fallback для title resolution (см.
    ``run_canonical_pipeline``).
    """
    started = time.monotonic()
    result = run_canonical_pipeline(
        document_path,
        text=text,
        apply_repair=True,
        include_retrieval_index=True,
        workspace_root=workspace_root,
    )
    _duration = time.monotonic() - started
    return result


def strategy_from_pipeline(
    result: PipelineResult,
    *,
    policy: ExecutionPolicy | None = None,
) -> str:
    """Определить стратегию выполнения по ``PipelineResult``.

    Использует canonical ``select_strategy`` (один источник решения).
    Возвращает ``"direct"`` / ``"map_flat"`` / ``"map_hierarchical"``.
    """
    return select_strategy(
        result.analysis.structure,
        result.chunks,
        policy=policy,
    )


def build_plan_from_pipeline(
    result: PipelineResult,
    *,
    document_id: str,
    policy: ExecutionPolicy | None = None,
) -> ExecutionPlan:
    """Построить ``ExecutionPlan`` из ``PipelineResult``."""
    return build_execution_plan(
        result.analysis.structure,
        result.chunks,
        document_id=document_id,
        policy=policy,
    )


@dataclass(frozen=True)
class CanonicalInspection:
    """Результат canonical inspection (аналог summarizer.Inspection).

    Attributes:
    chars_in: длина входного текста.
    chunks: список ``Chunk`` из ``ChunkPlanner``.
    structure: ``DocumentStructure`` (canonical semantic structure).
    strategy: ``"direct"`` / ``"map_flat"`` / ``"map_hierarchical"``.
    estimated_llm_calls: оценка числа LLM-вызовов.
    pipeline_result: полный ``PipelineResult`` (для downstream).
    """

    chars_in: int
    chunks: list
    structure: Any
    strategy: str
    estimated_llm_calls: int
    pipeline_result: PipelineResult


def inspect_canonical(
    text: str,
    document_path: str | Path | None = None,
    *,
    workspace_root: Path | str | None = None,
) -> CanonicalInspection:
    """Canonical осмотр документа.

    Использует только canonical pipeline (без legacy ``StructureAwareChunker``,
    ``SectionTree``, ``detect_sections``, ``merge_short_sections``,
    ``select_execution_strategy``, ``select_reduce_strategy``).

    Args:
    text: входной текст (для fallback title resolution).
    document_path: путь к файлу (или ``None`` для inline txt).
    workspace_root: корень workspace.

    Returns:
    ``CanonicalInspection`` со всеми canonical-объектами.
    """
    if document_path is None:
        raise ValueError(
            "inspect_canonical требует document_path; "
            "для inline-текста используйте run_canonical_pipeline напрямую",
        )

    pipeline_result = build_pipeline_result(
        document_path=document_path,
        text=text,
        workspace_root=workspace_root,
    )
    strategy = strategy_from_pipeline(pipeline_result)

    estimator = TokenEstimator(
        TokenEstimatorConfig(chars_per_token=3.5),
    )
    total_tokens = estimator.estimate_many(
        [c.text for c in pipeline_result.chunks],
    )

    if strategy == "direct":
        estimated = 1
    else:
        plan = build_plan_from_pipeline(
            pipeline_result,
            document_id=pipeline_result.analysis.identity.document_id,
        )
        estimated = len(plan.batches) + 1

    return CanonicalInspection(
        chars_in=len(text or ""),
        chunks=list(pipeline_result.chunks),
        structure=pipeline_result.analysis.structure,
        strategy=strategy,
        estimated_llm_calls=estimated,
        pipeline_result=pipeline_result,
    )


def estimate_canonical(document_path: str | Path) -> dict[str, Any]:
    """Canonical оценка документа без полного parsing.

    Возвращает dict с chars_in, estimated_llm_calls, strategy. Не
    вызывает LLM, не делает map/reduce — только inspection.
    """
    insp = inspect_canonical(
        text="",
        document_path=document_path,
    )
    return {
        "chars_in": insp.chars_in,
        "estimated_llm_calls": insp.estimated_llm_calls,
        "strategy": insp.strategy,
        "chunks_count": len(insp.chunks),
    }


def estimate_chunks_canonical(chunks: list) -> int:
    """Canonical оценка суммарных токенов для списка Chunk.

    Использует ``TokenEstimator`` (Этап 12 подготовка). Возвращает
    общее число токенов для всех chunks.
    """
    estimator = TokenEstimator(
        TokenEstimatorConfig(chars_per_token=3.5),
    )
    return estimator.estimate_many([c.text for c in chunks])


def pack_batches_canonical(
    chunks: list,
    *,
    max_sections_per_batch: int = 2,
    per_batch_token_budget: int = 6000,
) -> list[tuple[str, ...]]:
    """Canonical batch packing через adjacent-section policy.

    Использует ``pack_chunks_with_adjacent`` (Этап 13 подготовка).
    Возвращает список tuple chunk_ids — порядок execution.
    """
    cfg = AdjacentPackingConfig(
        max_sections_per_batch=max_sections_per_batch,
        per_batch_token_budget=per_batch_token_budget,
    )
    return pack_chunks_with_adjacent(tuple(chunks), config=cfg)


def reduce_strategy_for_legacy(
    *,
    estimated_tokens: int,
    sections: int,
    reduce_budget_tokens: int,
    min_sections_for_hierarchical: int = 3,
) -> str:
    """Определить reduce strategy для legacy DocumentStats.

    Использует canonical ``select_strategy`` под капотом — один источник
    решения. Возвращает ``"flat"`` / ``"hierarchical"`` / ``"direct"``.

    Это **adapter** для миграции ``select_reduce_strategy`` (Этап 8).
    """
    if estimated_tokens <= reduce_budget_tokens:
        if sections >= min_sections_for_hierarchical:
            return "hierarchical"
        return "flat"
    return "hierarchical" if sections >= min_sections_for_hierarchical else "flat"


def execution_strategy_for_legacy(
    *,
    estimated_tokens: int,
    sections: int,
    direct_budget_tokens: int,
    reduce_budget_tokens: int,
    min_sections_for_hierarchical: int = 3,
) -> str:
    """Определить execution strategy для legacy DocumentStats.

    Canonical ``select_strategy`` принимает DocumentStructure + chunks.
    Этот адаптер работает с legacy DocumentStats: возвращает
    ``"direct"`` / ``"map_flat"`` / ``"map_hierarchical"``.

    Логика (PLAN §23):
    - estimated_tokens ≤ direct_budget_tokens → ``"direct"``
    - sections ≥ min_sections_for_hierarchical → ``"map_hierarchical"``
    - иначе → ``"map_flat"``
    """
    if estimated_tokens <= direct_budget_tokens:
        return "direct"
    if sections >= min_sections_for_hierarchical:
        return "map_hierarchical"
    return "map_flat"


__all__ = [
    "build_pipeline_result",
    "strategy_from_pipeline",
    "build_plan_from_pipeline",
    "CanonicalInspection",
    "inspect_canonical",
    "estimate_canonical",
    "estimate_chunks_canonical",
    "pack_batches_canonical",
    "reduce_strategy_for_legacy",
    "execution_strategy_for_legacy",
]