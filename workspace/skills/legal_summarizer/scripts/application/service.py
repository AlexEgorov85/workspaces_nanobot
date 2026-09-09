"""Orchestration facade для legal_summarizer.

Public entry points:

* ``run`` — canonical execution (idempotency → inspect → context →
  confirmation → execute).
* ``inspect`` — document-level snapshot через ``run_canonical_pipeline``.
* ``quick_estimate`` — быстрая оценка размера без полного extraction.
* ``make_operation_id`` — детерминированный id для manifest.
* ``load_text`` — извлечение plain text из файла.
* ``Inspection`` / ``ExecutionContext`` / ``Estimate`` — типы.

Тонкая надстройка над subsystem-модулями в ``application/``. Вся
бизнес-логика живёт в них; здесь только dispatch и idempotency.

Этот модуль **не** содержит back-compat aliases для приватных LLM/
sanitize/pipeline функций. Тестовый monkeypatch работает через
``llm.calls.llm_*`` / ``llm.sanitize.strip_think_blocks`` /
``execution.pipeline.run_one_batch_async`` напрямую.
"""

from __future__ import annotations

import re
from pathlib import Path

import application.chunk_selection as _chunk_selection_mod
import application.context_builder as _ctx_builder_mod
import application.estimation as _estimation_mod
import application.execution_orchestration as _exec_orchestration_mod
import application.inspection as _inspection_mod
import application.operation_id as _operation_id_mod
import application.pipeline_structure as _pipeline_structure_mod
from application.chunk_selection import (
    relaxed_lexical_fallback,
    select_chunks_for_mode,
)
from application.context_builder import (
    ExecutionContext,
    build_execution_context,
)
from application.document_io import load_text
from application.estimation import (
    Estimate,
    estimate_for_run,
    needs_confirmation,
    quick_estimate,
)
from application.execution_orchestration import (
    map_plan_to_chunk_batches,
    run_direct,
    run_map_reduce,
)
from application.inspection import Inspection, inspect
from application.operation_id import (
    make_operation_id,
)
from application.pipeline_structure import (
    run_canonical_pipeline,
)
from application.question_context import build_question_context
from cache.manifest import (
    load_manifest,
    read_result,
)
from document.structure import DocumentStructure
from document.analysis import DocumentAnalysis
import llm.config as _llm_config_mod
import llm.calls as _llm_calls_mod
import llm.sanitize as _llm_sanitize_mod
from llm.prompts_runtime import LENGTH_INSTRUCTIONS
from planning.strategy import (
    build_execution_plan,
)


def _try_question_via_document_cache(
    *,
    question: str,
    text: str,
    document_path: str,
    workspace_root: Path | str,
    operation_id: str,
    length: str,
    focus: str | None,
) -> dict | None:
    """Shortcut для ``--question`` через document-level cache (#6).

    При успехе возвращает ``dict`` в shape ``service.run()`` result.
    При любой ошибке (cache miss, broken snapshot, no selected chunks,
    LLM failure) возвращает ``None`` — caller fallthrough на обычный
    pipeline.
    """
    from cache.manifest import (
        is_document_cache_complete,
        read_document_snapshot,
    )
    from chunking._text_helpers import progress as _progress
    from chunking.chunks import Chunk
    from document.identity import DocumentIdentity

    try:
        identity = DocumentIdentity.from_path(document_path)
    except (FileNotFoundError, OSError) as exc:
        _progress(f"#6 skip: {exc!r}")
        return None

    if not is_document_cache_complete(identity.document_id, workspace_root):
        _progress(
            f"#6 skip: no document cache for document_id={identity.document_id!r}"
        )
        return None

    snap = read_document_snapshot(identity.document_id, workspace_root)
    if snap is None:
        _progress("#6 skip: snapshot incomplete")
        return None
    physical_data, analysis_data, _meta = snap
    if physical_data is None or analysis_data is None:
        _progress("#6 skip: snapshot data missing")
        return None

    # Восстанавливаем DocumentAnalysis in-memory из snapshot.
    try:
        from document.physical import PhysicalDocument
        physical = PhysicalDocument.from_dict(physical_data)
        structure = DocumentStructure.from_dict(analysis_data["structure"])
        chunks = tuple(
            Chunk.from_dict(c) for c in analysis_data["chunks"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        _progress(f"#6 skip: broken snapshot: {exc!r}")
        return None

    analysis = DocumentAnalysis.build(
        physical=physical,
        structure=structure,
        chunks=chunks,
        identity=identity,
        include_retrieval_index=True,
        semantic_records={},
    )

    # Selection через lexical retrieval (используем DocumentAnalysis.retrieve).
    from application.chunk_selection import (
        select_chunks_for_mode as _select,
    )
    from application.inspection import Inspection

    insp = Inspection(
        chars_in=len(text or ""),
        chunks=list(chunks),
        structure=structure,
        analysis=analysis,
    )
    selected = _select(insp, question=question, length=length)
    if not selected:
        _progress("#6 skip: no chunks selected by retrieval")
        return None

    # Синтез-вход из document-level cache (commit #7).
    from execution.map_reduce import DOCUMENT_REDUCE_INPUT_BUDGET_CHARS
    context_text = build_question_context(
        selected,
        document_id=identity.document_id,
        workspace_root=workspace_root,
        budget_chars=DOCUMENT_REDUCE_INPUT_BUDGET_CHARS,
    )
    if not context_text:
        _progress("#6 skip: build_question_context returned empty")
        return None

    # Один финальный LLM call (synthesize).
    try:
        final_summary = _llm_calls_mod.llm_document_reduce(
            context_text,
            length=length,
            focus=focus,
            structure=structure,
            question=question,
        )
    except Exception as exc:
        _progress(f"#6 skip: llm_document_reduce failed: {exc!r}")
        return None

    final_summary = _llm_sanitize_mod.strip_think_blocks(final_summary)
    if not final_summary or not final_summary.strip():
        _progress("#6 skip: empty final_summary")
        return None

    subject = _llm_sanitize_mod.extract_subject(final_summary)
    title = (
        structure.title.value
        if structure.title is not None else None
    )

    result = {
        "subject": subject,
        "summary": final_summary,
        "length": length,
        "chars_in": len(text or ""),
        "chunks": len(selected),
        "context_batches": 1,
        "sections": (
            len(structure.iter_sections())
            if structure is not None else 0
        ),
        "strategy": "document_cache_question",
        "title": title,
        "partial": False,
    }

    # Commit #C2: записываем полноценный NormalizedManifest + result.json
    # для idempotency. Раньше сохраняли только result.json — следующий
    # вызов с тем же operation_id не находил manifest и снова делал
    # LLM call. Теперь manifest со status=completed ловит idempotency-check
    # в начале следующего вызова service.run().
    from cache.manifest import NormalizedManifest, save_manifest, write_result
    write_result(operation_id, result, workspace_root=workspace_root)

    manifest = NormalizedManifest(
        operation_id=operation_id,
        status="completed",
        version=2,
        document_path=str(document_path),
        structure_title=title,
        chars_in=len(text or ""),
        length=length,
        chunks_total=len(chunks),
        context_batches_total=1,
        estimated_llm_calls=None,
        actual_llm_calls=1,  # один синтезирующий llm_document_reduce
        sections=(
            {sid: {"section_id": sid} for sid in (
                {c.section_id for c in selected if c.section_id}
            )}
            if structure is not None else {}
        ),
        chunk_states={
            c.chunk_id: {
                "status": "completed",
                "section_id": c.section_id,
                "section_path": c.section_path,
                "page_start": c.page_start,
                "page_end": c.page_end,
                "result_path": f"chunks/{c.chunk_id}.json",
            }
            for c in selected
        },
        context_batches={
            "cb_000": {
                "chunk_ids": [c.chunk_id for c in selected],
                "status": "completed",
            }
        },
        section_summaries={},  # question mode — пусто
        batches_done=["cb_000"],
        batches_failed=[],
        last_error=None,
        started_at=None,
        completed_at=None,
        duration_sec=0.0,
        article_count=0,
        raw={
            "strategy": "document_cache_question",
            "document_id": identity.document_id,
        },
    )
    save_manifest(manifest, workspace_root=workspace_root)

    _progress(
        f"#6 ok: synthesized answer from {len(selected)} chunks "
        f"(document_id={identity.document_id})"
    )

    return {
        "status": "completed",
        "operation_id": operation_id,
        "result": result,
        "stats": {
            "chars_in": len(text or ""),
            "chunks_total": len(chunks),
            "chunks_selected": len(selected),
            "context_batches_total": 1,
            "strategy": "document_cache_question",
            "document_cache_hit": True,
        },
    }


def run(
    text: str,
    *,
    length: str = "brief",
    focus: str | None = None,
    question: str | None = None,
    confirmed: bool = False,
    operation_id: str | None = None,
    structure: DocumentStructure | None = None,
    document_path: str | None = None,
    workspace_root: Path | str | None = None,
) -> dict:
    """Canonical execution path.

    Порядок:

    1. ``resolve operation_id`` (детерминированно из text+length+path+question);
    2. ``check completed manifest`` — если completed и не legacy → return cached;
    3. ``inspect()`` — один canonical pipeline (document-level);
    4. ``build_execution_context()`` — selected chunks + strategy + plan (run-level);
    5. confirmation / requires_continuation / execute.
    """
    text = (text or "").strip()
    if not text:
        return {
            "status": "failed",
            "error": {"code": "EMPTY_DOCUMENT", "message": "Документ не содержит текста"},
            "operation_id": operation_id,
        }

    length = length if length in LENGTH_INSTRUCTIONS else "brief"

    operation_id = operation_id or make_operation_id(
        text, length, document_path=document_path, question=question,
    )

    existing_manifest = load_manifest(operation_id, workspace_root)
    if (
        existing_manifest is not None
        and existing_manifest.status == "completed"
    ):
        cached_result = read_result(operation_id, workspace_root)
        if cached_result is not None:
            from chunking._text_helpers import progress
            progress(f"idempotent: operation {operation_id} уже completed")
            return {
                "status": "completed",
                "operation_id": operation_id,
                "result": cached_result,
                "stats": {
                    "chars_in": cached_result.get("chars_in"),
                    "chunks": cached_result.get("chunks"),
                    "actual_llm_calls": existing_manifest.actual_llm_calls,
                    "strategy": cached_result.get("strategy"),
                    "duration_sec": existing_manifest.duration_sec,
                    "cached": True,
                },
            }

    # ── Commit #6: ``--question`` через document-level cache. ──
    # Условия входа в shortcut-ветку (все 4 обязательны):
    #   1. question is not None (режим question).
    #   2. document_path is not None (для DocumentIdentity).
    #   3. workspace_root is not None (для cache path resolution).
    #   4. document-cache complete для данного document_id.
    # Если хотя бы одно не выполнено — fallthrough на обычный pipeline.
    if (
        question is not None
        and document_path is not None
        and workspace_root is not None
    ):
        cached_question_result = _try_question_via_document_cache(
            question=question,
            text=text,
            document_path=document_path,
            workspace_root=workspace_root,
            operation_id=operation_id,
            length=length,
            focus=focus,
        )
        if cached_question_result is not None:
            return cached_question_result

    insp = _inspection_mod.inspect(
        text,
        document_path=document_path,
        workspace_root=workspace_root,
    )

    if not insp.chunks:
        return {
            "status": "failed",
            "error": {"code": "EMPTY_DOCUMENT", "message": "Документ не содержит текста"},
            "operation_id": operation_id,
        }

    ctx = _ctx_builder_mod.build_execution_context(insp, length=length, question=question)

    run_estimate = _estimation_mod.estimate_for_run(insp, ctx)

    exec_cfg = _llm_config_mod.get_execution_config()
    max_chunks_for_execution = int(exec_cfg["max_chunks_for_execution"])

    existing_manifest = load_manifest(operation_id, workspace_root)

    if needs_confirmation(run_estimate) and not confirmed:
        from chunking._text_helpers import progress
        progress(
            f"confirmation_required: chunks={len(ctx.chunks)}, "
            f"batches={run_estimate.context_batches}, "
            f"est_duration={run_estimate.estimated_duration_min_sec:.0f}-"
            f"{run_estimate.estimated_duration_max_sec:.0f}s"
        )
        title = (
            insp.analysis.structure.title.value
            if insp.analysis is not None and insp.analysis.structure.title is not None
            else (
                structure.title.value
                if structure is not None and structure.title is not None
                else None
            )
        )
        return {
            "status": "confirmation_required",
            "operation_id": operation_id,
            "summary": {
                "chars_in": insp.chars_in,
                "chunks_total": len(insp.chunks),
                "chunks_selected": len(ctx.chunks),
                "context_batches_total": run_estimate.context_batches,
                "estimated_llm_calls": run_estimate.estimated_llm_calls,
                "strategy": ctx.strategy,
                "title": title,
            },
            "estimate": {
                "min_seconds": run_estimate.estimated_duration_min_sec,
                "max_seconds": run_estimate.estimated_duration_max_sec,
                "confirmation_threshold_sec": run_estimate.confirmation_threshold_sec,
            },
            "hint": "Передайте --confirm для запуска полной обработки.",
        }

    if len(ctx.chunks) > max_chunks_for_execution:
        title = (
            insp.analysis.structure.title.value
            if insp.analysis is not None and insp.analysis.structure.title is not None
            else (
                structure.title.value
                if structure is not None and structure.title is not None
                else None
            )
        )
        return {
            "status": "requires_continuation",
            "operation_id": operation_id,
            "summary": {
                "chars_in": insp.chars_in,
                "chunks_total": len(insp.chunks),
                "chunks_selected": len(ctx.chunks),
                "estimated_llm_calls": run_estimate.estimated_llm_calls,
                "title": title,
            },
            "hint": (
                f"Выбранная выборка ({len(ctx.chunks)} chunks) превышает "
                f"max_chunks_for_execution={max_chunks_for_execution}. "
                f"Уменьшите max_chunks_per_question / question_fallback_max_chunks "
                f"или передайте --confirm для принудительного продолжения."
            ),
        }

    article_count = len(re.findall(r"Статья\s+\d+(?:\.\d+)?", text))

    _common = dict(
        length=length,
        focus=focus,
        question=question,
        structure=structure,
        analysis=insp.analysis,
        document_path=document_path,
        operation_id=operation_id,
        workspace_root=workspace_root,
        chars_in=insp.chars_in,
        estimated_llm_calls=run_estimate.estimated_llm_calls,
        article_count=article_count,
        existing_manifest=existing_manifest,
    )

    if ctx.strategy == "direct":
        return run_direct(list(ctx.chunks), **_common)

    return run_map_reduce(
        list(ctx.chunks), plan=ctx.plan, strategy=ctx.strategy, **_common,
    )


__all__ = [
    "run",
    "inspect",
    "Inspection",
    "ExecutionContext",
    "Estimate",
    "needs_confirmation",
    "quick_estimate",
    "make_operation_id",
    "load_text",
    # Public re-exports — общие helpers из application/execution_orchestration
    # и application/chunk_selection, нужные для тестов и CLI.
    "map_plan_to_chunk_batches",
    "run_direct",
    "run_map_reduce",
    "build_execution_context",
    "estimate_for_run",
    "select_chunks_for_mode",
    "relaxed_lexical_fallback",
    "build_execution_plan",
]
