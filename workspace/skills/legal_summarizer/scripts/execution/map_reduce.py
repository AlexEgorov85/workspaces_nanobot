"""Map-reduce execution strategy: batching + LLM calls + reduce.

Фактическая реализация ``map_reduce`` strategy, извлечённая
из ``application.execution_orchestration``.

Архитектурный контракт:

* ``execution`` НЕ импортирует ``cache`` и ``application``.
* ``cache.manifest.write_chunk_result`` и
  ``cache.manifest.load_cached_partials`` инжектируются
  через callback-параметры (``WriteChunkResultFn``/``LoadCachedPartialsFn``).
* ``execution.pipeline.run_one_batch_async`` инжектируется
  через callback-параметр (``RunOneBatchFn``) — caller
  (``application.execution_orchestration``) резолвит mock-совместимость.
* LLM-вызовы идут **напрямую** через ``llm.calls``
  (``llm_batch``/``llm_section_reduce``/``llm_document_reduce``).
"""

from __future__ import annotations

import asyncio
import time as _time
from pathlib import Path
from typing import Any, Callable

from chunking._text_helpers import (
    fit_input,
    format_chunk_block,
    progress,
)
from chunking.chunks import Chunk
from document.analysis import DocumentAnalysis
from document.section_helpers import (
    count_meaningful_sections_canonical,
    count_sections,
)
from document.structure import DocumentStructure
from execution.config import (
    MAX_REDUCE_ROUNDS,
    MID_REDUCE_GROUP_SIZE,
)
from execution.hierarchical import (
    HierarchicalReducerConfig,
    reduce_chunks_hierarchical,
)
import llm.calls as _llm_calls_mod
from llm.sanitize import (
    extract_subject,
    strip_think_blocks,
)
from planning.plan import ExecutionPlan


DOCUMENT_REDUCE_INPUT_BUDGET_CHARS = 60_000
_SECTION_SUMMARY_MAX_CHARS = 12_000


# Callback-типы для dependency injection из application layer.
WriteChunkResultFn = Callable[..., None]
# ``run_one_batch_async(pending_chunks, *, chunks_total, structure,
#   operation_id, workspace_root, sem, batch_id, length, question)``
RunOneBatchFn = Callable[..., Any]
# ``load_cached_partials(operation_id, expected_chunk_ids, workspace_root)``
LoadCachedPartialsFn = Callable[..., dict[str, str]]


def _assert_invariants(
    plan: ExecutionPlan,
    chunks: list[Chunk],
    final_batches: list[list[Chunk]],
) -> None:
    """Проверить инварианты Phase 2B между plan и chunks."""
    actual_chunk_ids: list[str] = []
    for fb in final_batches:
        actual_chunk_ids.extend(c.chunk_id for c in fb)
    expected_chunk_ids = [c.chunk_id for c in chunks]
    if sorted(actual_chunk_ids) != sorted(expected_chunk_ids):
        raise RuntimeError(
            "Этап 1 invariant violated: "
            f"plan batches do not cover expected chunks. "
            f"missing={sorted(set(expected_chunk_ids) - set(actual_chunk_ids))}, "
            f"extra={sorted(set(actual_chunk_ids) - set(expected_chunk_ids))}",
        )
    if len(set(actual_chunk_ids)) != len(actual_chunk_ids):
        raise RuntimeError(
            "Этап 1 invariant violated: duplicate chunk_id in map batches"
        )


def _queued_batches(
    final_batches: list[list[Chunk]],
    chunk_states: dict[str, dict[str, Any]],
) -> list[tuple[str, list[Chunk], int]]:
    """Список ``(batch_id, pending_chunks, total_chunks_in_batch)``."""
    queued: list[tuple[str, list[Chunk], int]] = []
    total_batches = len(final_batches)
    for batch_idx, batch_chunks in enumerate(final_batches):
        pending = [
            c for c in batch_chunks
            if c.chunk_id not in chunk_states
            or chunk_states[c.chunk_id].get("status") != "completed"
        ]
        if not pending:
            continue
        batch_id = f"cb_{batch_idx:03d}"
        queued.append((batch_id, pending, len(batch_chunks)))
        progress(
            f"batch {batch_id}: {len(pending)}/{len(batch_chunks)} chunks "
            f"queued ({total_batches} batches total)"
        )
    return queued


async def _run_all_batches(
    queued: list[tuple[str, list[Chunk], int]],
    *,
    chunks_total: int,
    struct: DocumentStructure | None,
    operation_id: str,
    workspace_root: Path | str | None,
    length: str,
    question: str | None,
    run_one_batch_async: RunOneBatchFn,
) -> list[tuple[str, dict | None, dict | None, tuple[str, Exception] | None]]:
    """Запустить все queued батчи через ``run_one_batch_async`` (concurrency=1)."""
    sem = asyncio.Semaphore(1)

    async def _gather_all():
        return await asyncio.gather(*[
            run_one_batch_async(
                pending_chunks,
                chunks_total=chunks_total,
                structure=struct,
                operation_id=operation_id,
                workspace_root=workspace_root,
                sem=sem,
                batch_id=batch_id,
                length=length,
                question=question,
            )
            for batch_id, pending_chunks, _ in queued
        ])

    return await _gather_all()


def _persist_batch_results(
    queued: list[tuple[str, list[Chunk], int]],
    gather_results: list[tuple[str, dict | None, dict | None, tuple[str, Exception] | None]],
    *,
    operation_id: str,
    write_chunk_result: WriteChunkResultFn,
    workspace_root: Path | str | None = None,
) -> tuple[
    int, list[str], dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict | None,
]:
    """Cache writes per-chunk + собрать stats.

    ``write_chunk_result`` инжектируется из application — это
    соблюдение ``cache boundary``: execution НЕ импортирует cache.

    Возвращает ``(map_calls, failed_batch_ids, chunk_states, ctx_batches,
    first_batch_error)``.
    """
    map_calls = 0
    failed_batch_ids: list[str] = []
    first_batch_error: dict[str, Any] | None = None
    chunk_states: dict[str, dict[str, Any]] = {}
    ctx_batches: dict[str, dict[str, Any]] = {}

    for (batch_id, batch_chunks, _pending_count), (
        status, batch_meta, chunk_results, last_error,
    ) in zip(queued, gather_results):
        if status == "ok":
            assert batch_meta is not None
            assert chunk_results is not None
            map_calls += 1
            ctx_batches[batch_id] = {
                "chunk_ids": batch_meta["chunk_ids"],
                "status": "completed",
                "started_at": batch_meta["started_at"],
                "completed_at": batch_meta["completed_at"],
                "duration_sec": batch_meta["duration_sec"],
                "section_paths": list({c.section_path for c in batch_chunks}),
            }
            duration = batch_meta["duration_sec"]
            for c in batch_chunks:
                if c.chunk_id in chunk_results:
                    write_chunk_result(
                        operation_id,
                        c.chunk_id,
                        chunk_results[c.chunk_id],
                        context_batch_id=batch_id,
                        section_id=c.section_id,
                        section_path=c.section_path,
                        page_start=c.page_start,
                        page_end=c.page_end,
                        duration_sec=duration,
                        workspace_root=workspace_root,
                    )
                chunk_states[c.chunk_id] = {
                    "status": "completed",
                    "context_batch_id": batch_id,
                    "section_id": c.section_id,
                    "section_path": c.section_path,
                    "page_start": c.page_start,
                    "page_end": c.page_end,
                    "result_path": f"chunks/{c.chunk_id}.json",
                    "duration_sec": batch_meta["duration_sec"],
                }
        else:
            assert last_error is not None
            error_code, error_exc = last_error
            failed_batch_ids.append(batch_id)
            if first_batch_error is None:
                first_batch_error = {
                    "code": error_code,
                    "batch_id": batch_id,
                    "message": str(error_exc),
                }
            ctx_batches[batch_id] = {
                "chunk_ids": [c.chunk_id for c in batch_chunks],
                "status": "failed",
                "error": {"code": error_code, "message": str(error_exc)},
            }
            for c in batch_chunks:
                chunk_states[c.chunk_id] = {
                    "status": "failed",
                    "context_batch_id": batch_id,
                    "section_id": c.section_id,
                    "section_path": c.section_path,
                    "page_start": c.page_start,
                    "page_end": c.page_end,
                    "error_code": error_code,
                }

    return map_calls, failed_batch_ids, chunk_states, ctx_batches, first_batch_error


def _reduce_phase(
    *,
    strategy: str,
    struct: DocumentStructure | None,
    section_ids: list[str],
    section_headings: dict[str, str],
    section_paths: dict[str, str],
    chunks: list[Chunk],
    all_partials: dict[str, str],
    length: str,
    focus: str | None,
    question: str | None,
) -> tuple[str, int, int, bool, str]:
    """Reduce phase: hierarchical или flat.

    Возвращает ``(final_summary, section_reduce_calls,
    document_reduce_calls, retries_incremented, strategy_label)``.
    """
    import os
    import sys

    _MR_TRACE_ENABLED = (
        "--mr-trace" in sys.argv
        or os.environ.get("LEGAL_SUMMARIZER_MR_TRACE") == "1"
    )

    def _mr_trace(stage: str, **fields) -> None:
        if not _MR_TRACE_ENABLED:
            return
        parts = [f"{k}={v}" for k, v in fields.items()]
        sys.stderr.write(
            f"[mr-trace {_time.monotonic():.2f}s] {stage} "
            + " ".join(parts) + "\n"
        )
        sys.stderr.flush()

    section_reduce_calls = 0
    document_reduce_calls = 0

    _mr_trace(
        "reduce_dispatch",
        strategy=strategy,
        has_struct=struct is not None,
        n_section_ids=len(section_ids),
    )

    if strategy == "map_hierarchical" and struct is not None and section_ids:
        reducer_config = HierarchicalReducerConfig(
            group_size=MID_REDUCE_GROUP_SIZE,
            max_rounds=MAX_REDUCE_ROUNDS,
            input_budget_chars=DOCUMENT_REDUCE_INPUT_BUDGET_CHARS,
            section_summary_max_chars=_SECTION_SUMMARY_MAX_CHARS,
        )

        def _llm_section_runner(joined, *, section_path="", section_heading="", **_kw):
            result = _llm_calls_mod.llm_section_reduce(
                section_path, section_heading, joined,
                length=length, question=question,
            )
            result = strip_think_blocks(result)
            if len(result) > _SECTION_SUMMARY_MAX_CHARS:
                result = fit_input(result, _SECTION_SUMMARY_MAX_CHARS)
            _mr_trace(
                "section_reduce",
                path=section_path[:50],
                heading=section_heading[:50],
                joined_chars=len(joined),
                result_chars=len(result),
                truncated=len(result) >= _SECTION_SUMMARY_MAX_CHARS,
            )
            return result

        def _llm_doc_runner(joined, *, length=length, focus=focus, structure=struct, question=question, **_kw):
            result = strip_think_blocks(
                _llm_calls_mod.llm_document_reduce(
                    joined, length=length, focus=focus, structure=structure, question=question,
                )
            )
            _mr_trace(
                "doc_reduce",
                joined_chars=len(joined),
                result_chars=len(result),
            )
            return result

        def _llm_hybrid_runner(joined, *, section_path=None, section_heading=None, **kw):
            if section_path is not None or section_heading is not None:
                nonlocal section_reduce_calls
                section_reduce_calls += 1
                return _llm_section_runner(
                    joined, section_path=section_path or "", section_heading=section_heading or "",
                )
            nonlocal document_reduce_calls
            document_reduce_calls += 1
            return _llm_doc_runner(joined, **kw)

        reducer_result = reduce_chunks_hierarchical(
            list(chunks),
            all_partials,
            section_ids=section_ids,
            section_headings=section_headings,
            section_paths=section_paths,
            config=reducer_config,
            llm_runner=_llm_hybrid_runner,
            length=length,
            focus=focus,
        )
        return (
            reducer_result.final_summary,
            section_reduce_calls,
            document_reduce_calls,
            False,
            "map_reduce_hierarchical",
        )

    ordered_chunks = [c for c in chunks if c.chunk_id in all_partials]
    joined = "\n\n".join(
        format_chunk_block(c, all_partials[c.chunk_id]) for c in ordered_chunks
    )
    if not joined.strip():
        _mr_trace(
            "flat_reduce_empty",
            ordered_chunks=len(ordered_chunks),
            total_partials=len(all_partials),
        )
        return "", section_reduce_calls, document_reduce_calls, False, "map_reduce_flat"
    original_joined_chars = len(joined)
    joined = fit_input(joined, DOCUMENT_REDUCE_INPUT_BUDGET_CHARS)
    _mr_trace(
        "flat_reduce_input",
        ordered_chunks=len(ordered_chunks),
        original_chars=original_joined_chars,
        after_fit_chars=len(joined),
        truncated=len(joined) < original_joined_chars,
    )
    try:
        final_summary = _llm_calls_mod.llm_document_reduce(
            joined, length=length, focus=focus,
            structure=struct, question=question,
        )
        document_reduce_calls += 1
    except Exception as exc:
        # REDUCE_INPUT_EMPTY на non-retryable input error: возвращаем
        # пустую строку, чтобы runtime классифицировал это как
        # ``REDUCE_INPUT_EMPTY`` → ``status='failed'``.
        # Никакого fallback на ``joined`` (это невалидное поведение —
        # сырой текст не является summary). И никакого retry — input
        # сам по себе non-retryable.
        _mr_trace(
            "flat_reduce_error",
            err=type(exc).__name__,
            msg=str(exc)[:200],
        )
        return "", section_reduce_calls, document_reduce_calls, True, "map_reduce_flat"
    _mr_trace(
        "flat_reduce_done",
        result_chars=len(final_summary),
    )
    return (
        final_summary,
        section_reduce_calls,
        document_reduce_calls,
        False,
        "map_reduce_flat",
    )


def _build_initial_partials_from_cache(
    chunk_states: dict[str, dict[str, Any]],
    cached_partials: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Build chunk_states entries для уже-cached chunks."""
    new_states: dict[str, dict[str, Any]] = {}
    for cid in cached_partials:
        new_states[cid] = {
            "status": "completed",
            "context_batch_id": chunk_states.get(cid, {}).get("context_batch_id"),
            "section_id": chunk_states.get(cid, {}).get("section_id"),
            "section_path": chunk_states.get(cid, {}).get("section_path"),
            "page_start": chunk_states.get(cid, {}).get("page_start"),
            "page_end": chunk_states.get(cid, {}).get("page_end"),
            "result_path": f"chunks/{cid}.json",
            "duration_sec": chunk_states.get(cid, {}).get("duration_sec"),
        }
    return new_states


def run_map_reduce_execution(
    chunks: list,
    *,
    plan: ExecutionPlan | None,
    strategy: str,
    length: str,
    focus: str | None,
    question: str | None,
    structure: DocumentStructure | None,
    analysis: DocumentAnalysis | None,
    operation_id: str,
    workspace_root: Path | str | None,
    chars_in: int,
    article_count: int,
    existing_manifest: Any,
    final_batches: list[list[Chunk]],
    section_ids: list[str],
    section_headings: dict[str, str],
    section_paths: dict[str, str],
    write_chunk_result: WriteChunkResultFn,
    run_one_batch_async: RunOneBatchFn,
    load_cached_partials: LoadCachedPartialsFn,
) -> dict:
    """Фактическая реализация map-reduce execution.

    Pure execution: возвращает dict в shape ``application.service.run()``.
    НЕ делает cache writes напрямую — ``write_chunk_result`` инжектируется
    из application. ``run_one_batch_async`` инжектируется из application
    (mock-совместимость с этапа-тестами).

    Фазы:
    1. ``_assert_invariants`` — plan vs chunks.
    2. Загрузить cached partials.
    3. ``_queued_batches`` — pending chunks.
    4. ``_run_all_batches`` — execute all queued batches.
    5. ``_persist_batch_results`` — write per-chunk results (через callback).
    6. ``_reduce_phase`` — section/document reduce.

    Финальный manifest + write_result делает ``application.execution_orchestration``.

    Диагностика (``LEGAL_SUMMARIZER_MR_TRACE=1`` или ``--mr-trace``):
    печатает в stderr структурный trace каждой фазы (chunks, partials,
    reduce inputs/outputs) — нужно для отладки "потерянных" данных
    в map-reduce.
    """
    import os
    import sys

    _MR_TRACE_ENABLED = (
        "--mr-trace" in sys.argv
        or os.environ.get("LEGAL_SUMMARIZER_MR_TRACE") == "1"
    )

    def _mr_trace(stage: str, **fields) -> None:
        if not _MR_TRACE_ENABLED:
            return
        parts = [f"{k}={v}" for k, v in fields.items()]
        sys.stderr.write(
            f"[mr-trace {_time.monotonic():.2f}s] {stage} "
            + " ".join(parts) + "\n"
        )
        sys.stderr.flush()

    _mr_trace(
        "enter",
        strategy=strategy,
        chunks=len(chunks),
        batches=len(final_batches),
        section_ids=len(section_ids),
        operation_id=operation_id,
    )
    """Фактическая реализация map-reduce execution.

    Pure execution: возвращает dict в shape ``application.service.run()``.
    НЕ делает cache writes напрямую — ``write_chunk_result`` инжектируется
    из application. ``run_one_batch_async`` инжектируется из application
    (mock-совместимость с этапа-тестами).

    Фазы:
    1. ``_assert_invariants`` — plan vs chunks.
    2. Загрузить cached partials.
    3. ``_queued_batches`` — pending chunks.
    4. ``_run_all_batches`` — execute all queued batches.
    5. ``_persist_batch_results`` — write per-chunk results (через callback).
    6. ``_reduce_phase`` — section/document reduce.

    Финальный manifest + write_result делает ``application.execution_orchestration``.
    """
    struct = analysis.structure if analysis is not None else None

    _assert_invariants(plan, chunks, final_batches)

    expected_chunk_ids = [c.chunk_id for c in chunks]
    chunk_states: dict[str, dict[str, Any]] = (
        dict(existing_manifest.chunk_states) if existing_manifest else {}
    )

    cached_partials = load_cached_partials(
        operation_id, expected_chunk_ids, workspace_root,
    )
    chunk_states.update(_build_initial_partials_from_cache(
        chunk_states, cached_partials,
    ))
    _mr_trace(
        "cached_partials",
        loaded=len(cached_partials),
        expected=len(expected_chunk_ids),
        coverage_pct=f"{100.0 * len(cached_partials) / max(1, len(expected_chunk_ids)):.1f}",
    )

    ctx_batches: dict[str, dict[str, Any]] = (
        dict(existing_manifest.context_batches) if existing_manifest else {}
    )

    total_start = _time.monotonic()
    queued = _queued_batches(final_batches, chunk_states)

    _mr_trace(
        "map_queued",
        batches=len(queued),
        pending_chunks=sum(len(pending) for _, pending, _ in queued),
        cached_already_done=(
            len(expected_chunk_ids) - sum(len(pending) for _, pending, _ in queued)
        ),
    )

    map_calls = 0
    retries = 0
    failed_batch_ids: list[str] = []
    first_batch_error: dict[str, Any] | None = None

    if queued:
        gather_results = asyncio.run(_run_all_batches(
            queued,
            chunks_total=len(chunks),
            struct=struct,
            operation_id=operation_id,
            workspace_root=workspace_root,
            length=length,
            question=question,
            run_one_batch_async=run_one_batch_async,
        ))
        map_calls, failed_batch_ids, batch_chunk_states, ctx_batches, first_batch_error = (
            _persist_batch_results(
                queued, gather_results,
                operation_id=operation_id,
                write_chunk_result=write_chunk_result,
                workspace_root=workspace_root,
            )
        )
        chunk_states.update(batch_chunk_states)
        _mr_trace(
            "map_done",
            map_calls=map_calls,
            failed_batches=len(failed_batch_ids),
            failed_ids=failed_batch_ids[:5],
            first_error=str(first_batch_error)[:200] if first_batch_error else None,
        )

    all_partials = load_cached_partials(
        operation_id, expected_chunk_ids, workspace_root,
    )

    if not all_partials:
        return {
            "status": "failed",
            "operation_id": operation_id,
            "error": {"code": "NO_PARTIALS", "message": "Нет per-chunk partials"},
        }

    # Диагностика partials: детектим пустые/короткие summaries
    empty_partials = [cid for cid, p in all_partials.items() if not p or not p.strip()]
    short_partials = [
        cid for cid, p in all_partials.items()
        if p and p.strip() and len(p.strip()) < 50
    ]
    avg_chars = (
        sum(len(p) for p in all_partials.values()) / max(1, len(all_partials))
    )
    _mr_trace(
        "reduce_input",
        total_partials=len(all_partials),
        expected=len(expected_chunk_ids),
        empty=len(empty_partials),
        empty_ids=empty_partials[:5],
        short=len(short_partials),
        short_ids=short_partials[:5],
        avg_chars=f"{avg_chars:.0f}",
    )

    final_summary, section_reduce_calls, document_reduce_calls, retries_incremented, strategy_label = (
        _reduce_phase(
            strategy=strategy,
            struct=struct,
            section_ids=section_ids,
            section_headings=section_headings,
            section_paths=section_paths,
            chunks=chunks,
            all_partials=all_partials,
            length=length,
            focus=focus,
            question=question,
        )
    )
    if retries_incremented:
        retries += 1

    final_summary = strip_think_blocks(final_summary)

    if not final_summary or not final_summary.strip():
        return {
            "status": "failed",
            "operation_id": operation_id,
            "error": {"code": "REDUCE_INPUT_EMPTY", "message": "Document reduce вернул пустой summary"},
        }

    total_duration = round(_time.monotonic() - total_start, 1)
    subject = extract_subject(final_summary)
    is_partial = bool(failed_batch_ids)
    total_llm_calls = map_calls + section_reduce_calls + document_reduce_calls
    meaningful = count_meaningful_sections_canonical(struct) if struct else 0

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
        "chunks": len(chunks),
        "context_batches": len(final_batches),
        "sections": count_sections(struct),
        "strategy": strategy_label,
        "title": title,
        "partial": is_partial,
    }

    return {
        "status": "partial" if is_partial else "completed",
        "operation_id": operation_id,
        "result": result,
        "stats": {
            "chars_in": chars_in,
            "chunks_total": len(chunks),
            "context_batches_total": len(final_batches),
            "sections_total": count_sections(struct),
            "meaningful_sections": meaningful,
            "article_count": article_count,
            "map_calls": map_calls,
            "section_reduce_calls": section_reduce_calls,
            "section_trim_calls": 0,
            "document_reduce_calls": document_reduce_calls,
            "reduce_calls": section_reduce_calls + document_reduce_calls,
            "total_llm_calls": total_llm_calls,
            "retries": retries,
            "failed_batches": list(failed_batch_ids),
            "partial": is_partial,
            "duration_sec": total_duration,
            "strategy": strategy_label,
            "_internal": {
                "chunk_states": chunk_states,
                "ctx_batches": ctx_batches,
                "failed_batch_ids": list(failed_batch_ids),
                "first_batch_error": first_batch_error,
                "total_llm_calls": total_llm_calls,
                "total_duration": total_duration,
                "strategy_label": strategy_label,
            },
        },
    }


__all__ = [
    "DOCUMENT_REDUCE_INPUT_BUDGET_CHARS",
    "run_map_reduce_execution",
]
