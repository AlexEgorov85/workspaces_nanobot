"""Inspection: document-level снимок анализа документа.

Один canonical pipeline на запуск (см. PLAN §13): ``inspect()``
возвращает ``Inspection`` со structure + analysis + chunks. Выбор
strategy / batch'ей / plan строится на уровне запуска через
``build_execution_context`` (см. ``application.context_builder``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import application.pipeline_structure as _pipeline_struct_mod
from document.analysis import DocumentAnalysis
from document.structure import DocumentStructure


@dataclass(frozen=True)
class Inspection:
    """Снимок анализа документа (document-level).

    ``Inspection`` описывает **документ**: structure + analysis + chunks.
    Это НЕ план конкретного запуска. Для конкретного запуска строить
    ``ExecutionContext`` через ``build_execution_context``.
    """

    chars_in: int
    chunks: list
    structure: DocumentStructure | None
    analysis: DocumentAnalysis | None


def inspect(
    text: str,
    document_path: str | None = None,
    *,
    workspace_root: Path | str | None = None,
) -> Inspection:
    """Canonical inspection (document-level).

    Возвращает ``Inspection`` со structure + analysis + chunks. Выбор
    strategy / batch'ей / plan строится на уровне запуска через
    ``build_execution_context``.
    """
    text = (text or "").strip()
    if not text:
        return Inspection(chars_in=0, chunks=[], structure=None, analysis=None)
    if document_path is None:
        raise ValueError(
            "inspect() требует document_path для canonical pipeline; "
            "для inline-текста используйте run_canonical_pipeline напрямую"
        )

    pipeline_result = _pipeline_struct_mod.run_canonical_pipeline(
        document_path,
        text=text,
        apply_repair=True,
        include_retrieval_index=True,
        workspace_root=workspace_root,
    )
    analysis = pipeline_result.analysis
    return Inspection(
        chars_in=len(text),
        chunks=list(pipeline_result.chunks),
        structure=analysis.structure,
        analysis=analysis,
    )


__all__ = ["Inspection", "inspect"]
