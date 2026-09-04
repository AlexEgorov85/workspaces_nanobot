"""Legacy map_reduce execution (PLAN §13 — transitional).

Вся map_reduce фаза из старого ``summarizer.run()`` вынесена сюда, чтобы
главный ``summarizer.py`` оставался canonical-only (PLAN §13
acceptance).

Этот модуль — **переходный слой**. Новый ``summarizer.run()`` для
стратегий ``"direct"`` использует canonical pipeline напрямую. Для
``"map_flat"`` / ``"map_hierarchical"`` он делегирует в
``legacy_run_map_reduce()`` здесь.

Полное удаление этого модуля (после перевода map_reduce на canonical
ExecutionPlan + HierarchicalReducer) — §32 (cleanup legacy files).
"""

from __future__ import annotations

import asyncio
import re
import time as _time
import warnings
from dataclasses import dataclass
from typing import Any

import llm

from workspace.skills.legal_summarizer.scripts.manifest import (
    NormalizedManifest,
    save_manifest,
    write_result,
)
from workspace.skills.legal_summarizer.scripts.packing import (
    ContextBatch,
    pack_chunks,
)
from workspace.skills.legal_summarizer.scripts.prompts import (
    ChunkResultParseError,
    build_batch_user_message,
    parse_batch_response,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock,
)
from workspace.skills.legal_summarizer.scripts.structure.sections import (
    ROOT_SECTION_ID,
    SectionTree,
    count_meaningful_sections,
)
from workspace.skills.legal_summarizer.scripts.pipeline import (
    now_iso as _now_iso,
    run_one_batch_async as _run_one_batch_async,
    load_cached_partials as _load_cached_partials,
)
from workspace.skills.legal_summarizer.scripts.sanitize import (
    extract_subject as _extract_subject,
    strip_think_blocks as _strip_think_blocks,
)
from workspace.skills.legal_summarizer.scripts.llm_calls import (
    doc_context as _doc_context,
    llm_section_reduce as _llm_section_reduce,
    llm_document_reduce as _llm_document_reduce,
)
from workspace.skills.legal_summarizer.scripts.document_cache import (
    load_doc_cache as _load_doc_cache,
    save_doc_cache as _save_doc_cache,
    cache_is_fresh as _cache_is_fresh,
)
from workspace.skills.legal_summarizer.scripts.fingerprint import (
    resolve_document_id as _resolve_document_id,
    resolve_session_key as _resolve_session_key,
)
from workspace.skills.legal_summarizer.scripts.token_budget import (
    count_tokens as _count_tokens,
)
from workspace.skills.legal_summarizer.scripts.prompts_runtime import (
    load_prompt as _load_prompt,
)


DOCUMENT_REDUCE_INPUT_BUDGET_CHARS = 60_000
SECTION_REDUCE_INPUT_BUDGET_CHARS = 60_000
MID_REDUCE_GROUP_SIZE = 3
MAX_REDUCE_ROUNDS = 4


def _progress(msg: str) -> None:
    import sys
    line = f"[legal_summarizer] {msg}"
    print(line, file=sys.stderr, flush=True)


def _fit_input(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    head = budget * 2 // 3
    tail = budget - head - 200
    if tail < 0:
        tail = 0
    skipped = len(text) - head - tail
    if tail:
        return (
            text[:head]
            + f"\n\n[...пропущено {skipped} символов...]\n\n"
            + text[-tail:]
        )
    return text[:head] + f"\n\n[...пропущено {skipped} символов...]"


def _format_chunk_block(chunk, summary: str) -> str:
    heading = getattr(chunk, "section_heading", "") or ""
    if heading:
        return f"[Chunk {chunk.chunk_id} | {heading}]\n{summary}"
    return f"[Chunk {chunk.chunk_id}]\n{summary}"


def legacy_run_map_reduce(
    *,
    text: str,
    length: str,
    focus: str | None,
    question: str | None,
    confirmed: bool,
    operation_id: str,
    structure: dict | None,
    document_path: str | None,
    workspace_root: Any,
    chunks: list,
    tree: SectionTree | None,
    batches: list,
    insp_chars_in: int,
    insp_strategy: str,
    insp_estimated_llm_calls: int,
    insp_context_batches: list,
    make_operation_id_fn: Any,
    get_chunking_config_fn: Any,
    get_execution_config_fn: Any,
    build_token_budget_fn: Any,
    session_key: str | None,
    existing_manifest: NormalizedManifest | None,
) -> dict:
    """Legacy map_reduce path.

    Возвращает dict в формате summarizer.run().
    """
    document_id = _resolve_document_id(document_path, text)

    est_dict: dict[str, Any] = {
        "chunks_count": len(chunks),
        "context_batches": len(batches),
        "estimated_llm_calls": insp_estimated_llm_calls,
    }

    exec_cfg = get_execution_config_fn()
    max_chunks_for_execution = int(exec_cfg["max_chunks_for_execution"])

    if not operation_id:
        operation_id = make_operation_id_fn(text, length)

    if (
        existing_manifest is not None
        and existing_manifest.status == "completed"
        and not existing_manifest.is_legacy
    ):
        from workspace.skills.legal_summarizer.scripts.manifest import read_result
        cached_result = read_result(operation_id, workspace_root)
        if cached_result is not None:
            _progress(f"idempotent: operation {operation_id} уже completed")
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

    if len(chunks) > max_chunks_for_execution:
        return {
            "status": "requires_continuation",
            "operation_id": operation_id,
            "summary": {
                "chars_in": insp_chars_in,
                "chunks_total": len(chunks),
                "chunks_selected": len(chunks),
                "estimated_llm_calls": insp_estimated_llm_calls,
                "title": (structure or {}).get("title"),
            },
            "hint": (
                f"Выбранная выборка ({len(chunks)} chunks) превышает "
                f"max_chunks_for_execution={max_chunks_for_execution}. "
                f"Уменьшите max_chunks_per_question / question_fallback_max_chunks "
                f"или передайте --confirm для принудительного продолжения."
            ),
        }

    if chunks is insp_context_batches:
        final_batches = insp_context_batches
    else:
        if insp_strategy == "single":
            final_batches = insp_context_batches
        else:
            budget_pack = build_token_budget_fn(get_chunking_config_fn())
            final_batches = pack_chunks(chunks, budget_pack)
    batches = final_batches

    doc_cache_chunks: dict[str, dict] = (
        _load_doc_cache(document_id, session_key, workspace_root)
        if session_key else {}
    )

    if doc_cache_chunks and document_path:
        if not _cache_is_fresh(document_id, session_key, workspace_root, document_path):
            _progress("document-cache stale → пропускаем reuse как partials")
            doc_cache_chunks = {}

    article_count = len(re.findall(r"Статья\s+\d+(?:\.\d+)?", text))

    sections_dict = {sid: s for sid, s in tree.sections.items()} if tree else {}
    section_payload: dict[str, dict[str, Any]] = {}
    for sid, s in sections_dict.items():
        if sid == ROOT_SECTION_ID:
            continue
        section_payload[sid] = s.to_dict()

    initial_manifest = NormalizedManifest(
        operation_id=operation_id,
        status="running",
        version=2,
        document_path=document_path,
        structure_title=(structure or {}).get("title"),
        chars_in=insp_chars_in,
        length=length,
        chunks_total=len(chunks),
        context_batches_total=len(batches),
        estimated_llm_calls=insp_estimated_llm_calls,
        actual_llm_calls=None,
        sections=section_payload,
        chunk_states={},
        context_batches={},
        section_summaries={},
        batches_done=[],
        batches_failed=[],
        last_error=None,
        started_at=_now_iso(),
        completed_at=None,
        duration_sec=None,
        article_count=article_count,
        is_legacy=False,
        raw={},
    )

    if existing_manifest is None:
        save_manifest(initial_manifest, workspace_root=workspace_root)

    expected_chunk_ids = [c.chunk_id for c in chunks]
    cached_partials = _load_cached_partials(operation_id, expected_chunk_ids, workspace_root)
    chunk_states: dict[str, dict[str, Any]] = dict(existing_manifest.chunk_states) if existing_manifest else {}

    if doc_cache_chunks:
        for cid, cdata in doc_cache_chunks.items():
            if cid not in chunk_states and cid in expected_chunk_ids:
                chunk_states[cid] = {
                    "status": "completed",
                    "context_batch_id": None,
                    "section_id": cdata.get("section_id"),
                    "section_path": cdata.get("section_path"),
                    "page_start": cdata.get("page_start"),
                    "page_end": cdata.get("page_end"),
                    "result_path": f"chunks/{cid}.json",
                    "duration_sec": cdata.get("duration_sec"),
                    "from_doc_cache": True,
                }

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

    exec_cfg_for_map = get_execution_config_fn()
    configured_concurrency = int(exec_cfg_for_map.get("max_concurrent_batches", 1) or 1)
    if configured_concurrency > 1:
        warnings.warn(
            "skills.legal_summarizer.execution.max_concurrent_batches > 1 "
            f"({configured_concurrency}) DEPRECATED и игнорируется: "
            "текущий runtime строго single-flight (max_active_llm_calls == 1). "
            "Удалите ключ из project.json — см. CHANGELOG.md (Deprecation).",
            DeprecationWarning,
            stacklevel=2,
        )
    concurrency = 1

    queued: list[tuple[ContextBatch, int]] = []
    total_batches = len(batches)
    for batch in batches:
        pending = [
            c for c in batch.chunks
            if c.chunk_id not in chunk_states
            or chunk_states[c.chunk_id].get("status") != "completed"
        ]
        if not pending:
            continue
        batch_to_process = ContextBatch(
            batch_id=batch.batch_id,
            chunks=tuple(pending),
            total_tokens_estimate=sum(c.token_estimate for c in pending),
            section_paths=tuple({c.section_path for c in pending}),
            page_range=batch.page_range,
        )
        queued.append((batch_to_process, len(pending)))
        _progress(
            f"batch {batch_to_process.batch_id}: {len(pending)}/{len(batch.chunks)} chunks "
            f"queued ({total_batches} batches total, concurrency={concurrency})"
        )

    if queued:
        sem = asyncio.Semaphore(concurrency)

        async def _gather_all():
            return await asyncio.gather(*[
                _run_one_batch_async(
                    btp,
                    chunks_total=len(chunks),
                    structure=structure,
                    operation_id=operation_id,
                    workspace_root=workspace_root,
                    sem=sem,
                    length=length,
                    question=question,
                )
                for btp, _ in queued
            ])

        gather_results = asyncio.run(_gather_all())

        for (batch_to_process, pending_count), (status, batch_meta, last_error) in zip(
            queued, gather_results
        ):
            if status == "ok":
                assert batch_meta is not None
                map_calls += 1
                ctx_batches[batch_to_process.batch_id] = {
                    "chunk_ids": batch_meta["chunk_ids"],
                    "status": "completed",
                    "started_at": batch_meta["started_at"],
                    "completed_at": batch_meta["completed_at"],
                    "duration_sec": batch_meta["duration_sec"],
                    "section_paths": list(batch_to_process.section_paths),
                }
                for c in batch_to_process.chunks:
                    chunk_states[c.chunk_id] = {
                        "status": "completed",
                        "context_batch_id": batch_to_process.batch_id,
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
                failed_batch_ids.append(batch_to_process.batch_id)
                if first_batch_error is None:
                    first_batch_error = {
                        "code": error_code,
                        "batch_id": batch_to_process.batch_id,
                        "message": str(error_exc),
                    }
                ctx_batches[batch_to_process.batch_id] = {
                    "chunk_ids": [c.chunk_id for c in batch_to_process.chunks],
                    "status": "failed",
                    "error": {"code": error_code, "message": str(error_exc)},
                }
                for c in batch_to_process.chunks:
                    chunk_states[c.chunk_id] = {
                        "status": "failed",
                        "context_batch_id": batch_to_process.batch_id,
                        "section_id": c.section_id,
                        "section_path": c.section_path,
                        "page_start": c.page_start,
                        "page_end": c.page_end,
                        "error_code": error_code,
                    }

    all_partials = _load_cached_partials(operation_id, expected_chunk_ids, workspace_root)

    if doc_cache_chunks:
        for cid, cdata in doc_cache_chunks.items():
            if cid in expected_chunk_ids and cid not in all_partials:
                all_partials[cid] = cdata.get("summary", "")

    if not all_partials:
        return {
            "status": "failed",
            "operation_id": operation_id,
            "error": {"code": "NO_PARTIALS", "message": "Нет per-chunk partials"},
        }

    if session_key and all_partials:
        chunks_by_id: dict[str, Any] = {c.chunk_id: c for c in chunks}
        to_save: dict[str, dict] = {}
        for cid, summary in all_partials.items():
            if cid in doc_cache_chunks:
                continue
            cs = chunk_states.get(cid, {})
            chunk = chunks_by_id.get(cid)
            payload: dict[str, Any] = {
                "chunk_id": cid,
                "summary": summary,
                "section_id": cs.get("section_id"),
                "section_path": cs.get("section_path"),
                "page_start": cs.get("page_start"),
                "page_end": cs.get("page_end"),
                "duration_sec": cs.get("duration_sec"),
                "saved_at": _now_iso(),
            }
            if chunk is not None:
                payload["block_indices"] = list(chunk.block_indices)
                payload["block_types"] = list(chunk.block_types)
                payload["source_char_start"] = chunk.source_char_start
                payload["source_char_end"] = chunk.source_char_end
                payload["table_id"] = chunk.table_id
                payload["table_row_start"] = chunk.table_row_start
                payload["table_row_end"] = chunk.table_row_end
                _preview_src = chunk.text
                _PREVIEW_MAX = 500
                if len(_preview_src) > _PREVIEW_MAX:
                    payload["chunk_text_preview"] = _preview_src[:_PREVIEW_MAX]
                else:
                    payload["chunk_text_preview"] = _preview_src
            to_save[cid] = payload
        if to_save:
            _save_doc_cache(
                document_id, session_key, workspace_root, to_save,
                progress=_progress, document_path=document_path,
            )

    _section_summary_max_chars = 12000
    _chars_per_token = 3.5

    _chars_in_reduce = sum(c.char_count for c in chunks)
    _sections_in_reduce = (
        sum(1 for sid in tree.sections if sid != ROOT_SECTION_ID)
        if tree is not None else 0
    )
    _r_est = max(1, int(_chars_in_reduce / _chars_per_token + 0.999))
    _r_sections = _sections_in_reduce
    _budget_for_reduce = build_token_budget_fn(get_chunking_config_fn())
    _r_budget = int(_budget_for_reduce.available_chunk_tokens)
    hierarchical = _r_est > _r_budget and _r_sections >= 2
    section_summaries_out: dict[str, str] = {}
    section_reduce_calls = 0
    section_trim_calls = 0
    document_reduce_calls = 0

    if hierarchical and tree is not None:
        for sid, section in tree.sections.items():
            if sid == ROOT_SECTION_ID:
                continue
            if not section.heading:
                continue
            section_chunk_ids = [
                c.chunk_id for c in chunks if c.section_id == sid and c.chunk_id in all_partials
            ]
            if not section_chunk_ids:
                continue
            joined = "\n\n".join(
                _format_chunk_block(c, all_partials[c.chunk_id])
                for c in chunks if c.chunk_id in section_chunk_ids
            )
            joined = _fit_input(joined, SECTION_REDUCE_INPUT_BUDGET_CHARS)
            try:
                section_summary = _llm_section_reduce(
                    section.section_path,
                    section.heading,
                    joined,
                    length=length,
                    question=question,
                )
            except Exception:
                section_summary = joined
                retries += 1
            if len(section_summary) > _section_summary_max_chars:
                section_summary = section_summary[: _section_summary_max_chars]
            section_summaries_out[sid] = section_summary
            section_reduce_calls += 1

        if not section_summaries_out:
            return {
                "status": "failed",
                "operation_id": operation_id,
                "error": {
                    "code": "REDUCE_INPUT_EMPTY",
                    "message": "Нет валидных section_summaries для финального reduce",
                },
                "stats": {
                    "chars_in": insp_chars_in,
                    "chunks_total": len(chunks),
                    "context_batches_total": len(batches),
                    "map_calls": map_calls,
                    "section_reduce_calls": section_reduce_calls,
                    "section_trim_calls": section_trim_calls,
                    "document_reduce_calls": 0,
                    "reduce_calls": section_reduce_calls + section_trim_calls,
                    "total_llm_calls": map_calls + section_reduce_calls + section_trim_calls,
                    "retries": retries,
                    "failed_batches": list(failed_batch_ids),
                    "duration_sec": round(_time.monotonic() - total_start, 1),
                    "strategy": "map_reduce_hierarchical",
                },
            }

        ordered = sorted(
            section_summaries_out.items(),
            key=lambda kv: tuple(int(p) if p.isdigit() else 999 for p in tree.sections[kv[0]].section_path.split(" > ")),
        )
        joined_sections = "\n\n".join(
            f"[Раздел {tree.sections[sid].section_path}: {tree.sections[sid].heading}]\n{summary}"
            for sid, summary in ordered
        )
        joined_sections = _fit_input(joined_sections, DOCUMENT_REDUCE_INPUT_BUDGET_CHARS)
        if not joined_sections.strip():
            return {
                "status": "failed",
                "operation_id": operation_id,
                "error": {
                    "code": "REDUCE_INPUT_EMPTY",
                    "message": "Joined section summaries пусты для финального reduce",
                },
                "stats": {
                    "chars_in": insp_chars_in,
                    "chunks_total": len(chunks),
                    "context_batches_total": len(batches),
                    "map_calls": map_calls,
                    "section_reduce_calls": section_reduce_calls,
                    "section_trim_calls": section_trim_calls,
                    "document_reduce_calls": 0,
                    "reduce_calls": section_reduce_calls + section_trim_calls,
                    "total_llm_calls": map_calls + section_reduce_calls + section_trim_calls,
                    "retries": retries,
                    "failed_batches": list(failed_batch_ids),
                    "duration_sec": round(_time.monotonic() - total_start, 1),
                    "strategy": "map_reduce_hierarchical",
                },
            }
        try:
            ordered_pairs = [(sid, summary) for sid, summary in ordered]
            from workspace.skills.legal_summarizer.scripts.structure.hierarchical_reducer import (
                HierarchicalReducerConfig,
                reduce_sections_to_document,
            )
            reducer_config = HierarchicalReducerConfig(
                group_size=MID_REDUCE_GROUP_SIZE,
                max_rounds=MAX_REDUCE_ROUNDS,
                input_budget_chars=DOCUMENT_REDUCE_INPUT_BUDGET_CHARS,
            )
            reducer_result = reduce_sections_to_document(
                ordered_pairs,
                config=reducer_config,
                llm_runner=lambda joined, **_kw: (
                    _strip_think_blocks(
                        _llm_document_reduce(
                            joined,
                            length=length,
                            focus=focus,
                            structure=structure,
                            question=question,
                        ),
                    )
                ),
                length=length,
                focus=focus,
                structure=structure,
                question=question,
            )
            final_summary = reducer_result.final_summary
            extra_rounds = reducer_result.rounds_done
            document_reduce_calls += 1 + extra_rounds
        except Exception:
            retries += 1
            final_summary = joined_sections if joined_sections.strip() else ""
        strategy_label = "map_reduce_hierarchical"
    else:
        ordered_chunks = [c for c in chunks if c.chunk_id in all_partials]
        joined = "\n\n".join(
            _format_chunk_block(c, all_partials[c.chunk_id]) for c in ordered_chunks
        )
        if not joined.strip():
            return {
                "status": "failed",
                "operation_id": operation_id,
                "error": {
                    "code": "REDUCE_INPUT_EMPTY",
                    "message": "Нет валидных partial summaries для финального reduce",
                },
                "stats": {
                    "chars_in": insp_chars_in,
                    "chunks_total": len(chunks),
                    "context_batches_total": len(batches),
                    "map_calls": map_calls,
                    "section_reduce_calls": 0,
                    "section_trim_calls": 0,
                    "document_reduce_calls": 0,
                    "reduce_calls": 0,
                    "total_llm_calls": map_calls,
                    "retries": retries,
                    "failed_batches": list(failed_batch_ids),
                    "duration_sec": round(_time.monotonic() - total_start, 1),
                    "strategy": "map_reduce_flat",
                },
            }
        try:
            final_summary = _llm_document_reduce(
                joined,
                length=length,
                focus=focus,
                structure=structure,
                question=question,
            )
            document_reduce_calls += 1
        except Exception:
            retries += 1
            final_summary = joined if joined.strip() else ""
        strategy_label = "map_reduce_flat"

    final_summary = _strip_think_blocks(final_summary)

    if not final_summary or not final_summary.strip():
        return {
            "status": "failed",
            "operation_id": operation_id,
            "error": {
                "code": "REDUCE_INPUT_EMPTY",
                "message": "Document reduce вернул пустой summary",
            },
            "stats": {
                "chars_in": insp_chars_in,
                "chunks_total": len(chunks),
                "context_batches_total": len(batches),
                "map_calls": map_calls,
                "section_reduce_calls": section_reduce_calls,
                "section_trim_calls": section_trim_calls,
                "document_reduce_calls": document_reduce_calls,
                "reduce_calls": section_reduce_calls + section_trim_calls + document_reduce_calls,
                "total_llm_calls": (
                    map_calls + section_reduce_calls + section_trim_calls + document_reduce_calls
                ),
                "retries": retries,
                "failed_batches": list(failed_batch_ids),
                "duration_sec": round(_time.monotonic() - total_start, 1),
                "strategy": strategy_label,
            },
        }

    total_duration = round(_time.monotonic() - total_start, 1)
    subject = _extract_subject(final_summary)

    is_partial = bool(failed_batch_ids)

    result = {
        "subject": subject,
        "summary": final_summary,
        "length": length,
        "chars_in": insp_chars_in,
        "chunks": len(chunks),
        "context_batches": len(batches),
        "sections": sum(1 for sid in tree.sections if sid != ROOT_SECTION_ID) if tree else 0,
        "strategy": strategy_label,
        "title": (structure or {}).get("title"),
        "partial": is_partial,
    }
    write_result(operation_id, result, workspace_root=workspace_root)

    meaningful = 0
    if tree:
        fake_blocks = tuple(
            DocumentBlock(
                block_id=f"b_{i:04d}",
                block_type="paragraph",
                content="x" * c.char_count,
                char_count=c.char_count,
                page_index=c.page_start,
                page_start=c.page_start,
                page_end=c.page_end,
                paragraph_index=None,
                table_index=None,
                ordinal=c.index,
                block_metadata={},
            )
            for i, c in enumerate(chunks)
        )
        meaningful = count_meaningful_sections(tree, fake_blocks)

    total_llm_calls = map_calls + section_reduce_calls + section_trim_calls + document_reduce_calls

    final_manifest = NormalizedManifest(
        operation_id=operation_id,
        status="partial" if is_partial else "completed",
        version=2,
        document_path=document_path,
        structure_title=(structure or {}).get("title"),
        chars_in=insp_chars_in,
        length=length,
        chunks_total=len(chunks),
        context_batches_total=len(batches),
        estimated_llm_calls=insp_estimated_llm_calls,
        actual_llm_calls=total_llm_calls,
        sections=section_payload,
        chunk_states=chunk_states,
        context_batches=ctx_batches,
        section_summaries=section_summaries_out,
        batches_done=[f"cb_{i:03d}" for i in range(len(batches))],
        batches_failed=list(failed_batch_ids),
        last_error=first_batch_error,
        started_at=existing_manifest.started_at if existing_manifest else _now_iso(),
        completed_at=_now_iso(),
        duration_sec=total_duration,
        article_count=article_count,
        is_legacy=False,
        raw={},
    )
    save_manifest(final_manifest, workspace_root=workspace_root)

    return {
        "status": "partial" if is_partial else "completed",
        "operation_id": operation_id,
        "result": result,
        "cache_stats": {
            "document_id": document_id,
            "chunks_from_cache": sum(1 for s in chunk_states.values() if s.get("from_doc_cache")),
            "chunks_processed": map_calls,
            "cache_enabled": bool(session_key),
        },
        "stats": {
            "chars_in": insp_chars_in,
            "chunks_total": len(chunks),
            "context_batches_total": len(batches),
            "sections_total": result["sections"],
            "meaningful_sections": meaningful,
            "article_count": article_count,
            "map_calls": map_calls,
            "section_reduce_calls": section_reduce_calls,
            "section_trim_calls": section_trim_calls,
            "document_reduce_calls": document_reduce_calls,
            "reduce_calls": section_reduce_calls + section_trim_calls + document_reduce_calls,
            "total_llm_calls": total_llm_calls,
            "retries": retries,
            "failed_batches": list(failed_batch_ids),
            "partial": is_partial,
            "duration_sec": total_duration,
            "strategy": strategy_label,
        },
    }


__all__ = ["legacy_run_map_reduce"]