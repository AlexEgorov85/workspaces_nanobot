"""Manifest builder: построение NormalizedManifest для cache/state."""

from __future__ import annotations

from typing import Any

from cache.manifest import NormalizedManifest
from document.analysis import DocumentAnalysis
from document.structure import DocumentStructure


def build_manifest(
    *,
    operation_id: str,
    document_path: str | None,
    structure: DocumentStructure | None,
    analysis: DocumentAnalysis | None,
    chars_in: int,
    length: str,
    chunks_total: int,
    context_batches_total: int,
    estimated_llm_calls: int,
    sections_payload: dict[str, dict[str, Any]],
    started_at: str | None = None,
    article_count: int,
    now_iso,
) -> NormalizedManifest:
    """Собрать ``NormalizedManifest`` для запуска (начальное состояние).

    ``structure`` — back-compat параметр публичного API ``run()``.
    Реальный title берётся из ``analysis.structure.title`` если есть,
    иначе fallback на ``structure.title``.
    """
    title = None
    if analysis is not None and analysis.structure.title is not None:
        title = analysis.structure.title.value
    elif structure is not None and structure.title is not None:
        title = structure.title.value
    return NormalizedManifest(
        operation_id=operation_id,
        status="running",
        version=2,
        document_path=document_path,
        structure_title=title,
        chars_in=chars_in,
        length=length,
        chunks_total=chunks_total,
        context_batches_total=context_batches_total,
        estimated_llm_calls=estimated_llm_calls,
        actual_llm_calls=None,
        sections=sections_payload,
        chunk_states={},
        context_batches={},
        section_summaries={},
        batches_done=[],
        batches_failed=[],
        last_error=None,
        started_at=started_at or now_iso(),
        completed_at=None,
        duration_sec=None,
        article_count=article_count,
        raw={},
    )


__all__ = ["build_manifest"]
