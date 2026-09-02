"""Pipeline execution: один LLM batch + retry + cached partials.

Содержит:
    * ``process_context_batch`` — sync: один LLM call → parse → write chunks
    * ``run_one_batch_async`` — async: retry-цикл (parse-error) под семафором
    * ``load_cached_partials`` — загрузить per-chunk summary из disk-манифеста
    * ``now_iso`` — текущее время в ISO 8601 (UTC)

NOTE: модуль назван ``pipeline.py``, не ``pipeline/...`` — оставлено
top-level для минимизации структурных изменений на этом этапе.
Целевая структура (``pipeline/execute.py``) будет достигнута после
переименования конфликтующего ``llm.py`` → ``llm_client.py``.
"""
from __future__ import annotations

import asyncio
import time as _time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from workspace.skills.legal_summarizer.scripts.llm_calls import llm_batch as _llm_batch
from workspace.skills.legal_summarizer.scripts.manifest import (
    read_chunk_result,
    write_chunk_result,
)
from workspace.skills.legal_summarizer.scripts.packing import ContextBatch
from workspace.skills.legal_summarizer.scripts.prompts import ChunkResultParseError


# Сколько раз перезапускать LLM-вызов батча при ChunkResultParseError
# (валидный JSON + все chunk_id). LLM-JSON флакает, ретрай обычно помогает.
# При исчерпании — батч помечается failed, обработка документа продолжается.
MAX_BATCH_PARSE_RETRIES = 3


def now_iso() -> str:
    """Текущее время в ISO 8601 (UTC)."""
    return datetime.now(timezone.utc).isoformat()


def process_context_batch(
    batch: ContextBatch,
    *,
    chunks_total: int,
    structure: dict | None,
    length: str,
    operation_id: str,
    workspace_root: Path | str | None,
    question: str | None = None,
    progress: Any = None,
) -> dict[str, Any]:
    """Один LLM call → parse → write per-chunk files.

    Args:
        progress: callable(str) для вывода прогресса (например, о parse-retry).
            Если ``None`` — пропускается (тестам обычно не нужен).
    """
    started_at = now_iso()
    start = _time.monotonic()
    result = _llm_batch(
        batch,
        chunks_total=chunks_total,
        structure=structure,
        length=length,
        question=question,
    )
    duration = round(_time.monotonic() - start, 3)
    completed_at = now_iso()

    for c in batch.chunks:
        if c.chunk_id in result:
            write_chunk_result(
                operation_id,
                c.chunk_id,
                result[c.chunk_id],
                context_batch_id=batch.batch_id,
                section_id=c.section_id,
                section_path=c.section_path,
                page_start=c.page_start,
                page_end=c.page_end,
                duration_sec=duration,
                workspace_root=workspace_root,
            )
    return {
        "batch_id": batch.batch_id,
        "chunk_ids": [c.chunk_id for c in batch.chunks],
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_sec": duration,
    }


async def run_one_batch_async(
    batch_to_process: ContextBatch,
    *,
    chunks_total: int,
    structure: dict | None,
    operation_id: str,
    workspace_root: Path | str | None,
    sem: asyncio.Semaphore,
    length: str = "brief",
    question: str | None = None,
    progress: Any = None,
) -> tuple[str, dict | None, tuple[str, Exception] | None]:
    """Один батч с retry-циклом (parse-error), под семафором concurrency.

    Sync-функция ``process_context_batch`` (внутри LLM HTTP + JSON parse +
    запись chunks/*.json) обёрнута в ``asyncio.to_thread`` — пока один батч
    висит на HTTP, event loop крутит остальные. Семафор ограничивает
    число одновременных HTTP-запросов к LLM-провайдеру (rate-limit).

    Возвращает:
      ("ok", batch_meta, None) — успех (с метаданными батча)
      ("failed", None, (code, exc)) — все retry исчерпаны или не-parse ошибка
    """
    async with sem:
        last_error: tuple[str, Exception] | None = None
        for attempt in range(1, MAX_BATCH_PARSE_RETRIES + 1):
            try:
                batch_meta = await asyncio.to_thread(
                    process_context_batch,
                    batch_to_process,
                    chunks_total=chunks_total,
                    structure=structure,
                    length=length,
                    operation_id=operation_id,
                    workspace_root=workspace_root,
                    question=question,
                    progress=progress,
                )
                return ("ok", batch_meta, None)
            except ChunkResultParseError as exc:
                # LLM-JSON флакает — ретраим с тем же промптом.
                last_error = ("LLM_PARSE_ERROR", exc)
                if progress is not None:
                    progress(
                        f"batch {batch_to_process.batch_id}: parse error "
                        f"attempt {attempt}/{MAX_BATCH_PARSE_RETRIES}, retrying"
                    )
                continue
            except Exception as exc:
                # Не-parse ошибка (сеть, OOM, структурно): не ретраим.
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
