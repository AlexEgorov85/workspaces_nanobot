"""Pipeline execution: один LLM batch + retry + cached partials.

Содержит:
    * ``process_context_batch`` — sync: один LLM call → parse → write chunks
    * ``run_one_batch_async`` — async: retry-цикл (parse-error) под семафором
    * ``load_cached_partials`` — загрузить per-chunk summary из disk-манифеста
    * ``now_iso`` — текущее время в ISO 8601 (UTC)

NOTE: legacy импорт ``ContextBatch`` удалён в PLAN §20. Сигнатуры
``process_context_batch(chunks, ...)`` и ``run_one_batch_async(chunks, ...)``
принимают ``list[Chunk]`` (canonical-compatible).
"""
from __future__ import annotations

import asyncio
import time as _time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from workspace.skills.legal_summarizer.scripts.llm_calls import llm_batch as _llm_batch
from workspace.skills.legal_summarizer.scripts.manifest import (
    read_chunk_result,
    write_chunk_result,
)
from workspace.skills.legal_summarizer.scripts.prompts import ChunkResultParseError
from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk


MAX_BATCH_PARSE_RETRIES = 3


def now_iso() -> str:
    """Текущее время в ISO 8601 (UTC)."""
    return datetime.now(timezone.utc).isoformat()


def process_context_batch(
    chunks: Sequence[Chunk],
    *,
    chunks_total: int,
    structure: dict | None,
    length: str,
    operation_id: str,
    workspace_root: Path | str | None,
    question: str | None = None,
    progress: Any = None,
    batch_id: str = "",
) -> dict[str, Any]:
    """Один LLM call → parse → write per-chunk files."""
    chunks_list = list(chunks)
    started_at = now_iso()
    start = _time.monotonic()
    result = _llm_batch(
        chunks_list,
        chunks_total=chunks_total,
        structure=structure,
        length=length,
        question=question,
    )
    duration = round(_time.monotonic() - start, 3)
    completed_at = now_iso()

    for c in chunks_list:
        if c.chunk_id in result:
            write_chunk_result(
                operation_id,
                c.chunk_id,
                result[c.chunk_id],
                context_batch_id=batch_id,
                section_id=c.section_id,
                section_path=c.section_path,
                page_start=c.page_start,
                page_end=c.page_end,
                duration_sec=duration,
                workspace_root=workspace_root,
            )
    return {
        "batch_id": batch_id,
        "chunk_ids": [c.chunk_id for c in chunks_list],
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_sec": duration,
    }


async def run_one_batch_async(
    chunks: Sequence[Chunk],
    *,
    chunks_total: int,
    structure: dict | None,
    operation_id: str,
    workspace_root: Path | str | None,
    sem: asyncio.Semaphore,
    batch_id: str = "",
    length: str = "brief",
    question: str | None = None,
    progress: Any = None,
) -> tuple[str, dict | None, tuple[str, Exception] | None]:
    """Один батч с retry-циклом (parse-error), под семафором concurrency."""
    async with sem:
        last_error: tuple[str, Exception] | None = None
        for attempt in range(1, MAX_BATCH_PARSE_RETRIES + 1):
            try:
                batch_meta = await asyncio.to_thread(
                    process_context_batch,
                    chunks,
                    chunks_total=chunks_total,
                    structure=structure,
                    length=length,
                    operation_id=operation_id,
                    workspace_root=workspace_root,
                    question=question,
                    progress=progress,
                    batch_id=batch_id,
                )
                return ("ok", batch_meta, None)
            except ChunkResultParseError as exc:
                last_error = ("LLM_PARSE_ERROR", exc)
                if progress is not None:
                    progress(
                        f"batch {batch_id}: parse error "
                        f"attempt {attempt}/{MAX_BATCH_PARSE_RETRIES}, retrying"
                    )
                continue
            except Exception as exc:
                last_error = ("LLM_ERROR", exc)
                break
        return ("failed", None, last_error)


def load_cached_partials(
    operation_id: str,
    expected_chunk_ids: list[str],
    workspace_root: Path | str | None,
) -> dict[str, str]:
    """Загрузить per-chunk summary из disk-манифеста (operation/chunks/*.json)."""
    out: dict[str, str] = {}
    for cid in expected_chunk_ids:
        rec = read_chunk_result(operation_id, cid, workspace_root)
        if rec and isinstance(rec.get("summary"), str):
            out[cid] = rec["summary"]
    return out


__all__ = [
    "MAX_BATCH_PARSE_RETRIES",
    "now_iso",
    "process_context_batch",
    "run_one_batch_async",
    "load_cached_partials",
]