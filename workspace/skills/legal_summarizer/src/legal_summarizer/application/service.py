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

Note on monkeypatch compatibility:
    Тесты патчат атрибуты этого модуля
    (``monkeypatch.setattr(summarizer, "_llm_batch", mock)`` и т.д.).
    Чтобы patch работал, все runtime-функции внутри ``run`` читаются
    через module-attribute lookup (``globals()["..."]``), а не прямые
    локальные имена. Это back-compat shim для этапа-тестов.
"""

from __future__ import annotations

import re
from pathlib import Path

from legal_summarizer.application._text_helpers import progress
from legal_summarizer.application.chunk_selection import (
    relaxed_lexical_fallback as _relaxed_lexical_fallback,
    select_chunks_for_mode as _select_chunks_for_mode,
)
from legal_summarizer.application.context_builder import (
    ExecutionContext,
    build_execution_context as _build_execution_context,
)
from legal_summarizer.application.document_io import load_text as _load_text
from legal_summarizer.application.estimation import (
    Estimate,
    estimate_for_run as _estimate_for_run,
    needs_confirmation,
    quick_estimate,
)
from legal_summarizer.application.estimation import (
    _hierarchical_reduce_calls,
)
from legal_summarizer.application.execution_orchestration import (
    map_plan_to_chunk_batches as _map_plan_to_chunk_batches,
    run_direct as _run_direct,
    run_map_reduce as _run_map_reduce,
)
from legal_summarizer.application.inspection import Inspection, inspect
from legal_summarizer.application.operation_id import (
    make_operation_id as _make_operation_id,
)
from legal_summarizer.application.pipeline_structure import (
    run_canonical_pipeline as _run_canonical_pipeline,
)
from legal_summarizer.planning.strategy import (
    build_execution_plan as _build_execution_plan,
)
from legal_summarizer.cache.manifest import (
    load_manifest,
    read_result,
)
from legal_summarizer.llm.config import get_execution_config
from legal_summarizer.llm.prompts_runtime import LENGTH_INSTRUCTIONS
from legal_summarizer.execution.config import (
    MAX_REDUCE_ROUNDS as _MAX_REDUCE_ROUNDS,
    MID_REDUCE_GROUP_SIZE as _MID_REDUCE_GROUP_SIZE,
)


# Back-compat aliases для тестов, которые ссылаются на приватные
# функции старого ``summarizer.py``. Реальные реализации живут в
# subsystem-модулях ниже; эти имена — тонкие re-exports для monkeypatch.
_map_plan_to_chunk_batches = _map_plan_to_chunk_batches
_run_map_reduce = _run_map_reduce
_run_direct = _run_direct
_build_execution_context = _build_execution_context
_estimate_for_run = _estimate_for_run
_select_chunks_for_mode = _select_chunks_for_mode
_relaxed_lexical_fallback = _relaxed_lexical_fallback
_hierarchical_reduce_calls = _hierarchical_reduce_calls
_make_operation_id = _make_operation_id
_load_text = _load_text
run_canonical_pipeline = _run_canonical_pipeline
build_execution_plan = _build_execution_plan
MID_REDUCE_GROUP_SIZE = _MID_REDUCE_GROUP_SIZE
MAX_REDUCE_ROUNDS = _MAX_REDUCE_ROUNDS


from legal_summarizer.llm.calls import (  # noqa: E402
    doc_context as _doc_context,
    llm_batch as _llm_batch,
    llm_section_reduce as _llm_section_reduce,
    llm_document_reduce as _llm_document_reduce,
)
from legal_summarizer.llm.sanitize import (  # noqa: E402
    extract_subject as _extract_subject,
    strip_think_blocks as _strip_think_blocks,
    _THINK_BLOCK_RE,
    _THINK_OPEN,
    _THOUGHT_CLOSE,
)
from legal_summarizer.llm.prompts_runtime import (  # noqa: E402
    load_prompt as _load_prompt,
    LENGTH_INSTRUCTIONS as _LENGTH_INSTRUCTIONS,
    QUESTION_INSTRUCTION_TEMPLATE as _QUESTION_INSTRUCTION_TEMPLATE,
    system_instruction as _system_instruction,
)
from legal_summarizer.execution.pipeline import (  # noqa: E402
    MAX_BATCH_PARSE_RETRIES as _MAX_BATCH_PARSE_RETRIES,
    now_iso as _now_iso,
    process_context_batch as _process_context_batch,
    load_cached_partials as _load_cached_partials,
    run_one_batch_async as _run_one_batch_async,
)
from legal_summarizer.application._text_helpers import (  # noqa: E402
    format_chunk_block as _format_chunk_block,
    fit_input as _fit_input,
    local_structure_label as _local_structure_label,
    chunk_structure_label as _chunk_structure_label,
    progress as _progress,
    _LOCAL_HEADING_RE as _local_heading_re,
)
from legal_summarizer.application.document_io import (  # noqa: E402
    _extract_pdf_head,
    _SUPPORTED_EXTENSIONS as _supported_extensions,
)
from legal_summarizer.application.estimation import (  # noqa: E402
    _QUICK_SAMPLE_PAGES as _quick_sample_pages,
    _CHARS_OVERESTIMATE as _chars_overestimate,
    _BATCH_OVERESTIMATE_RATIO as _batch_overestimate_ratio,
    count_execution_calls as _count_execution_calls,
)
from legal_summarizer.application.chunk_selection import (  # noqa: E402
    _resolve_max_chunks,
)
from legal_summarizer.application.manifest_builder import (  # noqa: E402
    build_manifest as _build_manifest,
)
from legal_summarizer.application.section_index import (  # noqa: E402
    section_index as _section_index,
    count_meaningful_sections_canonical as _count_meaningful_sections_canonical,
    count_sections as _count_sections,
)


def _session_key_for(document_path: str | None) -> str | None:
    """Извлечь safe_session_key из пути документа (canonical, без fingerprint)."""
    if not document_path:
        return None
    from workspace.utils.session_key import (
        extract_session_key_from_path,
        safe_session_key,
    )
    raw = extract_session_key_from_path(document_path)
    if not raw:
        return None
    return safe_session_key(raw)


_session_key_for = _session_key_for


def load_text(path, *, mode: str = "full") -> str:
    """Извлечь plain text из файла через office_files.

    Re-export из ``application.document_io`` для back-compat.
    """
    return _load_text(path, mode=mode)


def make_operation_id(
    text: str,
    length: str,
    *,
    document_path: str | None = None,
    question: str | None = None,
) -> str:
    """Стабильный operation_id (re-export из ``application.operation_id``)."""
    return _make_operation_id(
        text, length,
        document_path=document_path, question=question,
    )


def run(
    text: str,
    *,
    length: str = "brief",
    focus: str | None = None,
    question: str | None = None,
    confirmed: bool = False,
    operation_id: str | None = None,
    structure: dict | None = None,
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

    length = length if length in globals()["LENGTH_INSTRUCTIONS"] else "brief"

    operation_id = operation_id or globals()["_make_operation_id"](
        text, length, document_path=document_path, question=question,
    )

    existing_manifest = load_manifest(operation_id, workspace_root)
    if (
        existing_manifest is not None
        and existing_manifest.status == "completed"
    ):
        cached_result = read_result(operation_id, workspace_root)
        if cached_result is not None:
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

    insp = globals()["inspect"](text, document_path=document_path)

    if not insp.chunks:
        return {
            "status": "failed",
            "error": {"code": "EMPTY_DOCUMENT", "message": "Документ не содержит текста"},
            "operation_id": operation_id,
        }

    ctx = globals()["_build_execution_context"](insp, length=length, question=question)

    run_estimate = globals()["_estimate_for_run"](insp, ctx)

    exec_cfg = globals()["get_execution_config"]()
    max_chunks_for_execution = int(exec_cfg["max_chunks_for_execution"])

    existing_manifest = load_manifest(operation_id, workspace_root)

    if needs_confirmation(run_estimate) and not confirmed:
        progress(
            f"confirmation_required: chunks={len(ctx.chunks)}, "
            f"batches={run_estimate.context_batches}, "
            f"est_duration={run_estimate.estimated_duration_min_sec:.0f}-"
            f"{run_estimate.estimated_duration_max_sec:.0f}s"
        )
        title = (
            insp.analysis.structure.title.value
            if insp.analysis is not None and insp.analysis.structure.title is not None
            else (structure or {}).get("title")
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
            else (structure or {}).get("title")
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
        return globals()["_run_direct"](list(ctx.chunks), **_common)

    return globals()["_run_map_reduce"](
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
]
