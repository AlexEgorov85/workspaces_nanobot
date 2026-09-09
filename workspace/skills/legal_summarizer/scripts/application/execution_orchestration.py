"""Execution orchestration: координатор ``run_direct`` / ``run_map_reduce``.

Тонкая прослойка application layer:
* выбирает стратегию (``direct`` vs ``map_reduce``);
* делегирует фактическую работу в ``execution/map_reduce``;
* отвечает за cache lifecycle (initial/final manifest, write_result).

Алгоритмы execution (batching, LLM-вызовы, reduce) живут в
``execution.map_reduce`` и ``execution.hierarchical``.
Cache persistence — application-level responsibility (см.
``references/architecture.md``).

LLM boundary: ``llm.calls``/``llm.sanitize`` импортируются напрямую.
Тестовый monkeypatch выполняется через ``llm.calls.llm_*``
(``monkeypatch.setattr(llm_calls, "llm_document_reduce", mock)``)
и ``llm.sanitize.strip_think_blocks`` — без back-compat aliases
на ``application.service``.
"""

from __future__ import annotations

import time as _time
from pathlib import Path
from typing import Any

import llm.calls as _llm_calls_mod
import llm.sanitize as _llm_sanitize_mod
from application.manifest_builder import build_manifest
from cache.manifest import (
    NormalizedManifest,
    save_manifest,
    write_result,
)
from chunking.chunks import Chunk
from chunking._text_helpers import (
    fit_input,
    progress,
)
from document.analysis import DocumentAnalysis
from document.section_helpers import (
    count_meaningful_sections_canonical,
    count_sections,
    section_index,
)
from document.structure import DocumentStructure
from execution.map_reduce import (
    DOCUMENT_REDUCE_INPUT_BUDGET_CHARS,
    run_map_reduce_execution,
)
from execution.pipeline import now_iso
from planning.plan import ExecutionPlan


def map_plan_to_chunk_batches(
    plan: ExecutionPlan,
    chunks: list,
) -> list[list[Chunk]]:
    """Преобразовать ``ExecutionPlan`` в список батчей chunks.

    Единая точка маппинта plan→chunks для ``run_map_reduce``.
    Неизвестные ``cid`` → ``RuntimeError``. Дубликаты → ``RuntimeError``.
    Порядок батчей == порядок ``plan.batches``.
    """
    chunk_by_id = {c.chunk_id: c for c in chunks}
    seen_ids: set[str] = set()
    result: list[list[Chunk]] = []
    for batch in plan.batches:
        batch_chunks: list[Chunk] = []
        for cid in batch.chunk_ids:
            if cid not in chunk_by_id:
                raise RuntimeError(
                    f"plan references unknown chunk_id={cid!r}; "
                    f"available={[c.chunk_id for c in chunks]}"
                )
            if cid in seen_ids:
                raise RuntimeError(
                    f"duplicate chunk_id={cid!r} across batches"
                )
            seen_ids.add(cid)
            batch_chunks.append(chunk_by_id[cid])
        result.append(batch_chunks)
    return result


def run_direct(
    chunks: list,
    *,
    length: str,
    focus: str | None,
    question: str | None,
    structure: DocumentStructure | None,
    analysis: DocumentAnalysis | None,
    operation_id: str,
    document_path: str | None,
    workspace_root: Path | str | None,
    chars_in: int,
    estimated_llm_calls: int,
    article_count: int,
    existing_manifest: NormalizedManifest | None,
) -> dict:
    """Canonical direct execution: single llm_document_reduce call."""
    total_start = _time.monotonic()
    retries = 0
    ordered = list(chunks)

    joined = "\n\n".join(f"[Chunk {c.chunk_id}]\n{c.text}" for c in ordered)
    joined = fit_input(joined, DOCUMENT_REDUCE_INPUT_BUDGET_CHARS)

    canonical_structure = analysis.structure if analysis is not None else None
    try:
        final_summary = _llm_calls_mod.llm_document_reduce(
            joined, length=length, focus=focus,
            structure=canonical_structure, question=question,
        )
        reduce_calls = 1
    except Exception:
        # REDUCE_INPUT_EMPTY на non-retryable input error. Не используем
        # ``joined`` как fallback — это невалидный summary (сырой текст
        # чанков), и он нарушит контракт ``completed only with non-empty
        # summary``. Runtime классифицирует это как REDUCE_INPUT_EMPTY
        # → ``status='failed'``.
        retries += 1
        reduce_calls = 0
        final_summary = ""

    final_summary = _llm_sanitize_mod.strip_think_blocks(final_summary)

    if not final_summary or not final_summary.strip():
        return {
            "status": "failed",
            "operation_id": operation_id,
            "error": {"code": "REDUCE_INPUT_EMPTY", "message": "Document reduce вернул пустой summary"},
        }

    duration = round(_time.monotonic() - total_start, 1)
    subject = _llm_sanitize_mod.extract_subject(final_summary)

    title = None
    if analysis is not None and analysis.structure.title is not None:
        title = analysis.structure.title.value
    elif structure is not None and structure.title is not None:
        title = structure.title.value

    result = {
        "subject": subject,
        "summary": final_summary,
        "length": length,
        "chars_in": chars_in,
        "chunks": len(ordered),
        "context_batches": 1,
        "sections": count_sections(analysis.structure if analysis else None),
        "strategy": "direct",
        "title": title,
        "partial": False,
    }
    write_result(operation_id, result, workspace_root=workspace_root)

    manifest = build_manifest(
        operation_id=operation_id,
        document_path=document_path,
        structure=structure,
        analysis=analysis,
        chars_in=chars_in,
        length=length,
        chunks_total=len(ordered),
        context_batches_total=1,
        estimated_llm_calls=estimated_llm_calls,
        sections_payload={},
        started_at=existing_manifest.started_at if existing_manifest else now_iso(),
        article_count=article_count,
        now_iso=now_iso,
    )
    manifest.status = "completed"
    manifest.actual_llm_calls = reduce_calls
    manifest.completed_at = now_iso()
    manifest.duration_sec = duration
    manifest.context_batches = {
        "cb_000": {"chunk_ids": [c.chunk_id for c in ordered], "status": "completed"},
    }
    manifest.batches_done = ["cb_000"]
    # ``document_id`` в ``manifest.raw`` для reverse-lookup
    # ``operation_id → document_id`` (вопрос через document cache).
    document_id_direct = (
        analysis.identity.document_id
        if analysis is not None and getattr(analysis, "identity", None) is not None
        else None
    )
    manifest.raw["document_id"] = document_id_direct
    save_manifest(manifest, workspace_root=workspace_root)

    sections_total = count_sections(analysis.structure if analysis else None)
    meaningful_sections = (
        count_meaningful_sections_canonical(analysis.structure)
        if analysis is not None else 0
    )

    return {
        "status": "completed",
        "operation_id": operation_id,
        "result": result,
        "stats": {
            "chars_in": chars_in,
            "chunks_total": len(ordered),
            "context_batches_total": 1,
            "sections_total": sections_total,
            "meaningful_sections": meaningful_sections,
            "article_count": article_count,
            "map_calls": 0,
            "section_reduce_calls": 0,
            "section_trim_calls": 0,
            "document_reduce_calls": reduce_calls,
            "reduce_calls": reduce_calls,
            "total_llm_calls": reduce_calls,
            "retries": retries,
            "failed_batches": [],
            "partial": False,
            "duration_sec": duration,
            "strategy": "direct",
        },
    }


def run_map_reduce(
    chunks: list,
    *,
    plan: ExecutionPlan | None,
    strategy: str,
    length: str,
    focus: str | None,
    question: str | None,
    structure: DocumentStructure | None,
    analysis: DocumentAnalysis | None,
    document_path: str | None,
    operation_id: str,
    workspace_root: Path | str | None,
    chars_in: int,
    estimated_llm_calls: int,
    article_count: int,
    existing_manifest: NormalizedManifest | None,
) -> dict:
    """Canonical map_reduce: coordinator.

    Вычисляет ``section_index`` / ``sections_payload`` (document-level
    helpers), делегирует execution в ``execution.map_reduce``,
    затем отвечает за cache persistence (``save_manifest`` /
    ``write_result``) и финальный manifest.

    Cache boundary: ``execution.map_reduce`` НЕ пишет в cache —
    ``write_chunk_result`` и ``load_cached_partials`` инжектируются
    callback'ами сюда.
    """
    if plan is None:
        raise RuntimeError(
            "run_map_reduce requires a non-None ExecutionPlan; "
            "direct mode should use run_direct instead"
        )

    struct = analysis.structure if analysis is not None else None

    final_batches = map_plan_to_chunk_batches(plan, chunks)

    section_ids: list[str] = []
    section_headings: dict[str, str] = {}
    section_paths: dict[str, str] = {}
    sections_payload: dict[str, dict[str, Any]] = {}
    if struct is not None:
        section_ids, section_headings, section_paths = section_index(struct)
        for node in struct.iter_sections():
            sections_payload[node.node_id] = node.to_dict()

    initial_manifest = build_manifest(
        operation_id=operation_id,
        document_path=document_path,
        structure=structure,
        analysis=analysis,
        chars_in=chars_in,
        length=length,
        chunks_total=len(chunks),
        context_batches_total=len(final_batches),
        estimated_llm_calls=estimated_llm_calls,
        sections_payload=sections_payload,
        started_at=existing_manifest.started_at if existing_manifest else now_iso(),
        article_count=article_count,
        now_iso=now_iso,
    )
    if existing_manifest is None:
        save_manifest(initial_manifest, workspace_root=workspace_root)

    from cache.manifest import (
        load_cached_partials as _load_cached_partials,
        write_chunk_result as _write_chunk_result,
        write_document_chunk_summary as _write_document_chunk_summary,
    )
    from execution.pipeline import run_one_batch_async as _run_one_batch_async

    # ─ опциональная параллельная запись chunk summaries в
    # document-level cache (cross-operation identity). Если ``analysis``
    # или ``workspace_root`` отсутствуют — callback не инжектируется,
    # execution НЕ пишет в document-level cache.
    #
    # document-level cache хранит только question-independent
    # (baseline) summaries. При ``question is not None`` chunk summary
    # пишется только в operation-level cache (``operations/<op_id>/chunks/``).
    # Через callback пробрасываем ``question`` для guard внутри
    # ``write_document_chunk_summary``.
    document_id = (
        analysis.identity.document_id
        if analysis is not None and getattr(analysis, "identity", None) is not None
        else None
    )

    def _write_doc_chunk_summary(
        *, workspace_root, chunk_id, summary, section_id,
        section_path, page_start, page_end,
    ):
        if document_id is not None and workspace_root is not None:
            _write_document_chunk_summary(
                workspace_root=workspace_root,
                document_id=document_id,
                chunk_id=chunk_id,
                summary=summary,
                section_id=section_id,
                section_path=section_path,
                page_start=page_start,
                page_end=page_end,
                question=question,
            )

    payload = run_map_reduce_execution(
        chunks=chunks,
        plan=plan,
        strategy=strategy,
        length=length,
        focus=focus,
        question=question,
        structure=structure,
        analysis=analysis,
        operation_id=operation_id,
        workspace_root=workspace_root,
        chars_in=chars_in,
        article_count=article_count,
        existing_manifest=existing_manifest,
        final_batches=final_batches,
        section_ids=section_ids,
        section_headings=section_headings,
        section_paths=section_paths,
        write_chunk_result=_write_chunk_result,
        write_document_chunk_summary=_write_doc_chunk_summary,
        run_one_batch_async=_run_one_batch_async,
        load_cached_partials=_load_cached_partials,
    )

    # Cache persistence — application-level responsibility.
    # ``execution.map_reduce`` returns ``_internal`` artifacts for us to
    # persist (chunk_states, ctx_batches, batch failure tracking).
    if payload.get("status") in ("completed", "partial") and "_internal" in payload.get("stats", {}):
        _persist_final_manifest(
            payload=payload,
            document_path=document_path,
            structure=structure,
            analysis=analysis,
            chars_in=chars_in,
            length=length,
            chunks=chunks,
            estimated_llm_calls=estimated_llm_calls,
            sections_payload=sections_payload,
            existing_manifest=existing_manifest,
            article_count=article_count,
            workspace_root=workspace_root,
            now_iso=now_iso,
            question=question,
        )

    # Удалить ``_internal`` из payload перед возвратом.
    if payload.get("stats", {}).get("_internal") is not None:
        del payload["stats"]["_internal"]

    return payload


def _persist_final_manifest(
    *,
    payload: dict,
    document_path: str | None,
    structure: DocumentStructure | None,
    analysis: DocumentAnalysis | None,
    chars_in: int,
    length: str,
    chunks: list,
    estimated_llm_calls: int,
    sections_payload: dict[str, dict[str, Any]],
    existing_manifest: NormalizedManifest | None,
    article_count: int,
    workspace_root: Path | str | None,
    now_iso,
    question: str | None = None,
) -> None:
    """Сохранить финальный manifest на диск.

    Извлекает ``_internal`` из ``payload.stats`` и сохраняет
    ``NormalizedManifest`` через ``cache.manifest.save_manifest``.
    """
    from cache.manifest import (
        NormalizedManifest,
        save_manifest,
        write_document_section_summary,
        write_result,
    )

    internal = payload["stats"]["_internal"]
    chunk_states = internal["chunk_states"]
    ctx_batches = internal["ctx_batches"]
    failed_batch_ids = internal["failed_batch_ids"]
    first_batch_error = internal["first_batch_error"]
    total_llm_calls = internal["total_llm_calls"]
    total_duration = internal["total_duration"]
    strategy_label = internal["strategy_label"]
    section_summaries = internal.get("section_summaries", {})

    title = None
    if analysis is not None and analysis.structure.title is not None:
        title = analysis.structure.title.value
    elif structure is not None and structure.title is not None:
        title = structure.title.value

    write_result(payload["operation_id"], payload["result"], workspace_root=workspace_root)

    final_manifest = NormalizedManifest(
        operation_id=payload["operation_id"],
        status=payload["status"],
        version=2,
        document_path=document_path,
        structure_title=title,
        chars_in=chars_in,
        length=length,
        chunks_total=len(chunks),
        context_batches_total=len(ctx_batches),
        estimated_llm_calls=estimated_llm_calls,
        actual_llm_calls=total_llm_calls,
        sections=sections_payload,
        chunk_states=chunk_states,
        context_batches=ctx_batches,
        section_summaries=section_summaries,
        batches_done=[f"cb_{i:03d}" for i in range(len(ctx_batches))],
        batches_failed=failed_batch_ids,
        last_error=first_batch_error,
        started_at=existing_manifest.started_at if existing_manifest else now_iso(),
        completed_at=now_iso(),
        duration_sec=total_duration,
        article_count=article_count,
        raw={
            "strategy": strategy_label,
            "document_id": (
                analysis.identity.document_id
                if analysis is not None and getattr(analysis, "identity", None) is not None
                else None
            ),
        },
    )
    save_manifest(final_manifest, workspace_root=workspace_root)

    # persist section summaries в document-level cache.
    # Только после save_manifest — отдельная стадия жизненного цикла.
    # ``document_id`` берётся из ``analysis.identity`` (DocumentIdentity,
    # построенный в run_canonical_pipeline от path+size+mtime).
    if (
        section_summaries
        and workspace_root is not None
        and analysis is not None
        and getattr(analysis, "identity", None) is not None
    ):
        document_id = analysis.identity.document_id
        for sid, summary in section_summaries.items():
            write_document_section_summary(
                workspace_root=workspace_root,
                document_id=document_id,
                section_id=sid,
                summary=summary,
                question=question,
            )


__all__ = [
    "map_plan_to_chunk_batches",
    "run_direct",
    "run_map_reduce",
]