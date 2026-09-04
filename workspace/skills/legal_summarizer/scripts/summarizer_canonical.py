"""Canonical run pipeline (Этап 4А).

Этот модуль — **новый** production-flow, использующий только canonical
pipeline (``run_canonical_pipeline`` → ``DocumentAnalysis`` →
``HierarchicalReducer``).

Это **не замена** ``summarizer.run()`` сразу: legacy-путь остаётся
для обратной совместимости с существующими тестами, пока equivalence
не доказана на fixtures. Переключение делается поэтапно.

Здесь только:

* ``build_pipeline_result`` — обёртка над ``run_canonical_pipeline``,
  возвращающая ``PipelineResult`` с логированием прогресса;
* ``strategy_from_pipeline`` — определить ExecutionPolicy по
  ``PipelineResult`` (DIRECT / MAP_FLAT / MAP_HIERARCHICAL).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from workspace.skills.legal_summarizer.scripts.structure.pipeline import (
    PipelineResult,
    run_canonical_pipeline,
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


def strategy_from_pipeline(result: PipelineResult) -> ExecutionPolicy:
    """Определить стратегию выполнения по ``PipelineResult``.

    Использует canonical ``select_strategy`` (один источник решения).
    """
    return select_strategy(result.analysis.structure)


def build_plan_from_pipeline(
    result: PipelineResult,
    *,
    estimator=None,
) -> Any:
    """Построить ``ExecutionPlan`` из ``PipelineResult``.

    Возвращает план, готовый к выполнению через ``unified_execution``.
    """
    return build_execution_plan(
        result.analysis.structure,
        estimator=estimator,
    )


__all__ = [
    "build_pipeline_result",
    "strategy_from_pipeline",
    "build_plan_from_pipeline",
]