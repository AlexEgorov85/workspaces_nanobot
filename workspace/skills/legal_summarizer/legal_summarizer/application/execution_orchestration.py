"""Execution orchestration: ``_run_direct`` / ``_run_map_reduce``.

Два варианта canonical execution path:

* ``run_direct`` — один ``llm_document_reduce`` вызов для коротких
  документов (strategy='direct').
* ``run_map_reduce`` — batched execution → hierarchical/flat reduce
  для длинных документов.

Оба варианта возвращают dict в shape, ожидаемом ``application.service.run()``.

Note on monkeypatch:
    Тесты делают ``monkeypatch.setattr(service, "_llm_batch", mock)``
    и ожидают, что ``service._run_direct`` / ``service._run_map_reduce``
    вызовут именно mock. Чтобы patch работал, мы читаем все
    ``_llm_*`` / ``_strip_*`` / ``_run_one_batch_async`` / etc.
    функции **через module attribute ``legal_summarizer.application.service``**
    (lazy lookup), а не прямой импорт. Это back-compat shim для
    этапа-тестов; основной код может импортировать напрямую из
    subsystem-модулей.
"""

from __future__ import annotations

import asyncio
import time as _time
from pathlib import Path
from typing import Any

from legal_summarizer.application._text_helpers import (
    fit_input,
    format_chunk_block,
    progress,
)
from legal_summarizer.application.manifest_builder import build_manifest
from legal_summarizer.application.section_index import (
    count_meaningful_sections_canonical,
    count_sections,
    section_index,
)
from legal_summarizer.cache.manifest import (
    NormalizedManifest,
    save_manifest,
    write_chunk_result,
    write_result,
)
from legal_summarizer.chunking.chunks import Chunk
from legal_summarizer.document.analysis import DocumentAnalysis
from legal_summarizer.document.structure import DocumentStructure
from legal_summarizer.execution.config import (
    MAX_REDUCE_ROUNDS,
    MID_REDUCE_GROUP_SIZE,
)
from legal_summarizer.execution.hierarchical import (
    HierarchicalReducerConfig,
    reduce_chunks_hierarchical,
)
from legal_summarizer.planning.plan import ExecutionPlan


def _service_mod():
    """Lazy lookup модуля ``legal_summarizer.application.service``.

    Все ссылки на ``_llm_*``, ``_strip_*``, ``_extract_subject``,
    ``_now_iso``, ``_load_cached_partials``, ``_run_one_batch_async``
    идут через этот lookup, чтобы ``monkeypatch.setattr(service, name, mock)``
    в тестах перехватывал реальный вызов.
    """
    import legal_summarizer.application.service as _svc
    return _svc


DOCUMENT_REDUCE_INPUT_BUDGET_CHARS = 60_000


def map_plan_to_chunk_batches(
    plan: ExecutionPlan,
    chunks: list,
) -> list[list[Chunk]]:
    """Преобразовать ``ExecutionPlan`` в список батчей chunks.

    Единая точка маппинга plan→chunks для ``run_map_reduce``.
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
    _svc = _service_mod()
    try:
        final_summary = _svc._llm_document_reduce(
            joined, length=length, focus=focus,
            structure=canonical_structure, question=question,
        )
        reduce_calls = 1
    except Exception:
        retries += 1
        reduce_calls = 0
        final_summary = joined if joined.strip() else ""

    final_summary = _svc._strip_think_blocks(final_summary)

    if not final_summary or not final_summary.strip():
        return {
            "status": "failed",
            "operation_id": operation_id,
            "error": {"code": "REDUCE_INPUT_EMPTY", "message": "Document reduce вернул пустой summary"},
        }

    duration = round(_time.monotonic() - total_start, 1)
    subject = _svc._extract_subject(final_summary)

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
        started_at=existing_manifest.started_at if existing_manifest else _svc._now_iso(),
        article_count=article_count,
        now_iso=_svc._now_iso,
    )
    manifest.status = "completed"
    manifest.actual_llm_calls = reduce_calls
    manifest.completed_at = _svc._now_iso()
    manifest.duration_sec = duration
    manifest.context_batches = {
        "cb_000": {"chunk_ids": [c.chunk_id for c in ordered], "status": "completed"},
    }
    manifest.batches_done = ["cb_000"]
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
    """Canonical map_reduce: batch execution → hierarchical/flat reduce.

    Invariant: ``run_canonical_pipeline`` вызывается ровно один раз
    в ``inspect()``. Эта функция использует уже готовые ``analysis`` из
    ``Inspection`` и ``plan`` из ``ExecutionContext``.
    """
    if plan is None:
        raise RuntimeError(
            "run_map_reduce requires a non-None ExecutionPlan; "
            "direct mode should use run_direct instead"
        )

    struct = analysis.structure if analysis is not None else None

    final_batches = map_plan_to_chunk_batches(plan, chunks)

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

    section_ids: list[str] = []
    section_headings: dict[str, str] = {}
    section_paths: dict[str, str] = {}
    sections_payload: dict[str, dict[str, Any]] = {}
    if struct is not None:
        section_ids, section_headings, section_paths = section_index(struct)
        for node in struct.iter_sections():
            sections_payload[node.node_id] = node.to_dict()

    article_count_for_manifest = article_count
    _svc = _service_mod()
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
        started_at=existing_manifest.started_at if existing_manifest else _svc._now_iso(),
        article_count=article_count_for_manifest,
        now_iso=_svc._now_iso,
    )
    if existing_manifest is None:
        save_manifest(initial_manifest, workspace_root=workspace_root)

    expected_chunk_ids = [c.chunk_id for c in chunks]
    cached_partials = _svc._load_cached_partials(
        operation_id, expected_chunk_ids, workspace_root,
    )
    chunk_states: dict[str, dict[str, Any]] = (
        dict(existing_manifest.chunk_states) if existing_manifest else {}
    )

    for cid in cached_partials:
        chunk_states[cid] = {
            "status": "completed",
            "context_batch_id": chunk_states.get(cid, {}).get("context_batch_id"),
            "section_id": chunk_states.get(cid, {}).get("section_id"),
            "section_path": chunk_states.get(cid, {}).get("section_path"),
            "page_start": chunk_states.get(cid, {}).get("page_start"),
            "page_end": chunk_states.get(cid, {}).get("page_end"),
            "result_path": f"chunks/{cid}.json",
            "duration_sec": chunk_states.get(cid, {}).get("duration_sec"),
        }

    ctx_batches: dict[str, dict[str, Any]] = (
        dict(existing_manifest.context_batches) if existing_manifest else {}
    )

    total_start = _time.monotonic()
    map_calls = 0
    retries = 0
    failed_batch_ids: list[str] = []
    first_batch_error: dict[str, Any] | None = None
    concurrency = 1

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
            f"queued ({total_batches} batches total, concurrency={concurrency})"
        )

    expected_ids = [f"cb_{i:03d}" for i in range(len(final_batches))]
    actual_ids = [bid for bid, _, _ in queued]
    if actual_ids != [eid for eid in expected_ids if eid in set(actual_ids)]:
        raise RuntimeError(
            f"batch order mismatch: queued={actual_ids} != expected={expected_ids}"
        )

    if queued:
        sem = asyncio.Semaphore(concurrency)

        async def _gather_all():
            return await asyncio.gather(*[
                _svc._run_one_batch_async(
                    pending_chunks,
                    chunks_total=len(chunks),
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

        gather_results = asyncio.run(_gather_all())

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
                # Cache persistence — application-level responsibility:
                # ``execution.pipeline`` возвращает ``chunk_results``, но
                # решение о записи в disk-манифест принимает application.
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
                retries += 1
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

    all_partials = _svc._load_cached_partials(
        operation_id, expected_chunk_ids, workspace_root,
    )

    if not all_partials:
        return {
            "status": "failed",
            "operation_id": operation_id,
            "error": {"code": "NO_PARTIALS", "message": "Нет per-chunk partials"},
        }

    _section_summary_max_chars = 12000
    section_reduce_calls = 0
    document_reduce_calls = 0

    if strategy == "map_hierarchical" and struct is not None and section_ids:
        reducer_config = HierarchicalReducerConfig(
            group_size=MID_REDUCE_GROUP_SIZE,
            max_rounds=MAX_REDUCE_ROUNDS,
            input_budget_chars=DOCUMENT_REDUCE_INPUT_BUDGET_CHARS,
            section_summary_max_chars=_section_summary_max_chars,
        )

        def _llm_section_runner(joined, *, section_path="", section_heading="", **_kw):
            result = _svc._llm_section_reduce(
                section_path, section_heading, joined,
                length=length, question=question,
            )
            result = _svc._strip_think_blocks(result)
            if len(result) > _section_summary_max_chars:
                result = fit_input(result, _section_summary_max_chars)
            return result

        def _llm_doc_runner(joined, *, length=length, focus=focus, structure=struct, question=question, **_kw):
            return _svc._strip_think_blocks(
                _svc._llm_document_reduce(
                    joined, length=length, focus=focus, structure=structure, question=question,
                )
            )

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
        final_summary = reducer_result.final_summary
        strategy_label = "map_reduce_hierarchical"
    else:
        ordered_chunks = [c for c in chunks if c.chunk_id in all_partials]
        joined = "\n\n".join(
            format_chunk_block(c, all_partials[c.chunk_id]) for c in ordered_chunks
        )
        if not joined.strip():
            return {
                "status": "failed",
                "operation_id": operation_id,
                "error": {"code": "REDUCE_INPUT_EMPTY", "message": "Нет валидных partial summaries для финального reduce"},
            }
        joined = fit_input(joined, DOCUMENT_REDUCE_INPUT_BUDGET_CHARS)
        try:
            final_summary = _svc._llm_document_reduce(
                joined, length=length, focus=focus,
                structure=struct, question=question,
            )
            document_reduce_calls += 1
        except Exception:
            retries += 1
            final_summary = joined if joined.strip() else ""
        strategy_label = "map_reduce_flat"

    final_summary = _svc._strip_think_blocks(final_summary)

    if not final_summary or not final_summary.strip():
        return {
            "status": "failed",
            "operation_id": operation_id,
            "error": {"code": "REDUCE_INPUT_EMPTY", "message": "Document reduce вернул пустой summary"},
        }

    total_duration = round(_time.monotonic() - total_start, 1)
    subject = _svc._extract_subject(final_summary)
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
    write_result(operation_id, result, workspace_root=workspace_root)

    final_manifest = build_manifest(
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
        started_at=existing_manifest.started_at if existing_manifest else _svc._now_iso(),
        article_count=article_count_for_manifest,
        now_iso=_svc._now_iso,
    )
    final_manifest.status = "partial" if is_partial else "completed"
    final_manifest.actual_llm_calls = total_llm_calls
    final_manifest.chunk_states = chunk_states
    final_manifest.context_batches = ctx_batches
    final_manifest.section_summaries = {}
    final_manifest.batches_done = [f"cb_{i:03d}" for i in range(len(final_batches))]
    final_manifest.batches_failed = list(failed_batch_ids)
    final_manifest.last_error = first_batch_error
    final_manifest.completed_at = _svc._now_iso()
    final_manifest.duration_sec = total_duration
    save_manifest(final_manifest, workspace_root=workspace_root)

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
            "article_count": article_count_for_manifest,
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
        },
    }


__all__ = [
    "map_plan_to_chunk_batches",
    "run_direct",
    "run_map_reduce",
]
