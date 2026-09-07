"""Execution context: run-level snapshot (selected chunks + strategy + plan)."""

from __future__ import annotations

from dataclasses import dataclass

from application.chunk_selection import select_chunks_for_mode
from application.inspection import Inspection
from chunking.chunks import Chunk
from planning.plan import ExecutionPlan
from planning.strategy import select_strategy


def _service_mod():
    """Lazy lookup для ``build_execution_plan`` (для monkeypatch в тестах)."""
    import application.service as _svc
    return _svc


@dataclass(frozen=True)
class ExecutionContext:
    """Контекст конкретного запуска (run-level).

    Собирается через ``build_execution_context`` из ``Inspection``
    (document-level). ``chunks`` — выбранная для запуска выборка;
    ``strategy`` и ``plan`` построены именно для неё.
    """

    chunks: tuple[Chunk, ...]
    strategy: str
    plan: ExecutionPlan | None


def build_execution_context(
    insp: Inspection,
    length: str | None = None,
    question: str | None = None,
    selected_chunks: list | None = None,
) -> ExecutionContext:
    """Собрать контекст конкретного запуска из Inspection (document-level).

    Единая точка, которая выбирает chunks, вычисляет ``strategy`` и
    строит ``ExecutionPlan``. Вынесена отдельно для тестирования и для
    разделения document-level / run-level.

    Parameters
    ----------
    insp:
        Результат ``inspect()`` (document-level снимок).
    length:
        Режим суммаризации ('brief' / 'detailed' / None = full).
    question:
        Вопрос пользователя (для ``question``-режима).
    selected_chunks:
        Предвыбранная выборка (если передана — используется напрямую,
        без выбора через ``select_chunks_for_mode``).
    """
    if selected_chunks is not None:
        chunks = tuple(selected_chunks)
    else:
        chunks = tuple(select_chunks_for_mode(
            insp, length=length, question=question,
        ))

    if len(chunks) <= 1:
        plan = None
        strategy = "direct"
    elif insp.structure is not None:
        strategy = select_strategy(insp.structure, list(chunks))
        plan = _service_mod().build_execution_plan(
            insp.structure,
            tuple(chunks),
            document_id=insp.analysis.identity.document_id if insp.analysis else "",
        )
    else:
        plan = None
        strategy = "direct"

    return ExecutionContext(chunks=chunks, strategy=strategy, plan=plan)


__all__ = ["ExecutionContext", "build_execution_context"]
