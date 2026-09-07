"""Pipeline execution: один LLM batch + retry (без side-effects на cache).

Содержит:
    * ``process_context_batch`` — sync: один LLM call → parse → return batch_meta + chunk_results
    * ``run_one_batch_async`` — async: retry-цикл (parse-error) под семафором
    * ``now_iso`` — текущее время в ISO 8601 (UTC)

Pure execution boundary: этот модуль **не** импортирует
``cache.manifest``. Запись per-chunk файлов и lookup partials —
ответственность application-уровня (``application.execution_orchestration``).

Single-flight: ``LLM_FLIGHT_LOCK`` из ``llm.single_flight`` сериализует
все LLM-вызовы между разными ``summarizer.run()`` в разных потоках.
Тот же lock используется в ``llm/calls.py`` через ``guarded_chat`` —
единая single-flight boundary.

Архитектурный контракт: ``execution → llm.single_flight`` — это
**единственное** исключение из общего правила ``execution не знает о llm``.
Single-flight — cross-cutting технический gate (как ``threading.Lock``),
а не domain-зависимость. ``llm.calls`` / ``llm.prompts`` / ``llm.client``
по-прежнему запрещены для ``execution``. См.
``tests/architecture/test_layer_boundaries.py``.
"""
from __future__ import annotations

import asyncio
import time as _time
from datetime import datetime, timezone
from typing import Any, Sequence

from llm.calls import llm_batch as _llm_batch
from llm.prompts import ChunkResultParseError
from chunking.chunks import Chunk
from document.structure import DocumentStructure
from llm.single_flight import LLM_FLIGHT_LOCK


MAX_BATCH_PARSE_RETRIES = 3


def now_iso() -> str:
    """Текущее время в ISO 8601 (UTC)."""
    return datetime.now(timezone.utc).isoformat()


def process_context_batch(
    chunks: Sequence[Chunk],
    *,
    chunks_total: int,
    structure: DocumentStructure | None,
    length: str,
    question: str | None = None,
    progress: Any = None,
    batch_id: str = "",
) -> tuple[dict[str, Any], dict[str, str]]:
    """Один LLM call → parse → return (batch_meta, chunk_results).

    Pure execution: возвращает ``dict[chunk_id, summary]`` и ``batch_meta``
    без side-effects на cache. Application-уровень (``execution_orchestration``)
    сам решает — сохранять ли partials в disk-манифест для resume.

    Single-flight: lock берётся здесь (а не внутри ``llm_batch``) —
    это позволяет mock-тестам подменять ``llm_batch`` напрямую
    (минуя внутреннюю обёртку ``guarded_chat``) и всё равно
    соблюдать ``max_active_llm_calls == 1``. Реальный путь к LLM
    (``guarded_chat``) внутри ``llm_batch`` берёт **тот же** lock —
    блокировка реентрантна семантически (``LLM_FLIGHT_LOCK`` один),
    deadlock'а нет, потому что между вызовами lock отпускается.
    """
    chunks_list = list(chunks)
    started_at = now_iso()
    start = _time.monotonic()
    with LLM_FLIGHT_LOCK:
        chunk_results = _llm_batch(
            chunks_list,
            chunks_total=chunks_total,
            structure=structure,
            length=length,
            question=question,
        )
    duration = round(_time.monotonic() - start, 3)
    completed_at = now_iso()
    batch_meta = {
        "batch_id": batch_id,
        "chunk_ids": [c.chunk_id for c in chunks_list],
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_sec": duration,
    }
    return batch_meta, chunk_results


async def run_one_batch_async(
    chunks: Sequence[Chunk],
    *,
    chunks_total: int,
    structure: DocumentStructure | None,
    operation_id: str | None = None,
    workspace_root: Any = None,
    sem: asyncio.Semaphore,
    batch_id: str = "",
    length: str = "brief",
    question: str | None = None,
    progress: Any = None,
) -> tuple[str, dict[str, Any] | None, dict[str, str] | None, tuple[str, Exception] | None]:
    """Один батч с retry-циклом (parse-error), под семафором concurrency.

    ``operation_id``/``workspace_root`` приняты для back-compat с
    monkeypatch-тестами (``monkeypatch.setattr(service, "_run_one_batch_async", mock)``);
    в самой реализации они не используются (cache writes — на стороне
    application).

    Returns:
        ``(status, batch_meta, chunk_results, last_error)``.

        - ``status == "ok"``: ``batch_meta`` и ``chunk_results`` заполнены,
          ``last_error is None``.
        - ``status == "failed"``: ``batch_meta``/``chunk_results`` равны
          ``None``, ``last_error`` = ``(error_code, exc)``.
    """
    async with sem:
        last_error: tuple[str, Exception] | None = None
        for attempt in range(1, MAX_BATCH_PARSE_RETRIES + 1):
            try:
                batch_meta, chunk_results = await asyncio.to_thread(
                    process_context_batch,
                    chunks,
                    chunks_total=chunks_total,
                    structure=structure,
                    length=length,
                    question=question,
                    progress=progress,
                    batch_id=batch_id,
                )
                return ("ok", batch_meta, chunk_results, None)
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
        return ("failed", None, None, last_error)


__all__ = [
    "MAX_BATCH_PARSE_RETRIES",
    "now_iso",
    "process_context_batch",
    "run_one_batch_async",
]
