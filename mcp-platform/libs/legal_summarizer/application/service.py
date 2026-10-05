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

import libs.legal_summarizer.application.context_builder as _ctx_builder_mod
import libs.legal_summarizer.application.estimation as _estimation_mod
import libs.legal_summarizer.application.inspection as _inspection_mod
import libs.legal_summarizer.llm.calls as _llm_calls_mod
import libs.legal_summarizer.llm.config as _llm_config_mod
import libs.legal_summarizer.llm.sanitize as _llm_sanitize_mod
from libs.legal_summarizer.application.chunk_selection import (
    relaxed_lexical_fallback,
    select_chunks_for_mode,
)
from libs.legal_summarizer.application.context_builder import (
    ExecutionContext,
    build_execution_context,
)
from libs.legal_summarizer.application.document_io import load_text
from libs.legal_summarizer.application.estimation import (
    Estimate,
    estimate_for_run,
    needs_confirmation,
    quick_estimate,
)
from libs.legal_summarizer.application.execution_orchestration import (
    map_plan_to_chunk_batches,
    run_direct,
    run_map_reduce,
)
from libs.legal_summarizer.application.inspection import Inspection, inspect
from libs.legal_summarizer.application.operation_id import (
    make_operation_id,
)

# Re-export: ``run_canonical_pipeline`` не вызывается этим модулем (идёт
# через ``inspection``), но остаётся атрибутом ``service`` намеренно —
# на нём подменяют функцию тесты инварианта «pipeline вызывается ровно
# один раз» (``monkeypatch.setattr(service, "run_canonical_pipeline", ...)``).
# Redundant-alias форма помечает имя как явный реэкспорт, поэтому линтер
# не считает его неиспользуемым импортом. Убирать атрибут нельзя.
from libs.legal_summarizer.application.pipeline_structure import (
    run_canonical_pipeline as run_canonical_pipeline,
)
from libs.legal_summarizer.application.question_context import build_question_context
from libs.legal_summarizer.cache.document_cache import DocumentCache
from libs.legal_summarizer.cache.manifest import (
    load_manifest,
    read_result,
)
from libs.legal_summarizer.document.analysis import DocumentAnalysis
from libs.legal_summarizer.document.structure import DocumentStructure
from libs.legal_summarizer.llm.prompts_runtime import LENGTH_INSTRUCTIONS
from libs.legal_summarizer.planning.strategy import (
    build_execution_plan,
)


def _remaining_batches(ctx, chunk_states: dict) -> int:
    """Число батчей, в которых осталась неоплаченная работа.

    Работа считается по состоянию операции, а не по всему плану:
    ``chunk_states`` — это то, что уже записано, и повторно это
    считать нельзя. Правило совпадает с ``_queued_batches``
    (``execution/map_reduce.py``): батч выполнен, когда все его
    чанки помечены ``completed``.
    """
    if ctx.plan is None:
        # ``direct`` — неделимый шаг: он либо уже выполнен (тогда
        # состояние непустое), либо впереди.
        return 0 if chunk_states else 1
    return sum(
        1
        for batch in ctx.plan.batches
        if any(
            (chunk_states.get(cid) or {}).get("status") != "completed"
            for cid in batch.chunk_ids
        )
    )


def _estimate_for_remaining(est: Estimate, remaining_batches: int) -> Estimate:
    """Оценка по оставшейся работе, а не по всему документу заново.

    Иначе подтверждение, данное на первом шаге, не помогало бы: на
    втором шаге верхняя оценка снова считалась бы от начала и гейт
    снова потребовал бы подтверждения — при нуле платной работы.

    ``estimated_llm_calls`` остаётся верхней границей и для остатка:
    reduce-фаза (последний вызов) не исчезает, поэтому её часть
    сохраняется, а батчевая уменьшается до остатка.
    """
    if remaining_batches >= est.context_batches:
        return est
    chunk_dur = float(
        _llm_config_mod.get_execution_config()["estimated_chunk_duration_sec"]
    )
    avg = remaining_batches * chunk_dur
    reduce_calls = max(0, est.estimated_llm_calls - est.context_batches)
    return Estimate(
        chunks_count=est.chunks_count,
        context_batches=remaining_batches,
        estimated_llm_calls=remaining_batches + reduce_calls,
        estimated_duration_min_sec=round(avg * 0.8, 1),
        estimated_duration_max_sec=round(avg * 1.2, 1),
        confirmation_threshold_sec=est.confirmation_threshold_sec,
    )


def _progress_report_from_manifest(manifest) -> dict:
    """``progress_report`` по уже готовому состоянию операции.

    ``done`` считается по тому же правилу, что и в остальных ответах:
    число записей состояния со ``status: "completed"``, а не батчей.
    Готовое состояние означает законченный разбор, поэтому остаток
    пуст, а продолжение не обещается.
    """
    chunk_states = getattr(manifest, "chunk_states", None) or {}
    done = sum(
        1 for state in chunk_states.values()
        if (state or {}).get("status") == "completed"
    )
    return {"done": done, "remaining": 0, "continues": False}


def _try_question_via_document_cache(
    *,
    question: str,
    text: str,
    document_path: str,
    workspace_root: Path | str,
    operation_id: str,
    length: str,
    focus: str | None,
    session_key: str = "default",
) -> dict | None:
    """Shortcut для ``--question`` через document-level cache.

    При успехе возвращает ``dict`` в shape ``service.run()`` result.
    При любой ошибке (cache miss, broken snapshot, no selected chunks,
    LLM failure) возвращает ``None`` — caller fallthrough на обычный
    pipeline.

    Storage boundary (atomic read, completeness, layout) — ответственность
    ``DocumentCache``. Service не знает про cache paths / marker / format.
    """
    from libs.legal_summarizer.chunking._text_helpers import progress as _progress
    from libs.legal_summarizer.chunking.chunks import Chunk
    from libs.legal_summarizer.document.identity import DocumentIdentity

    try:
        identity = DocumentIdentity.from_path(document_path)
    except (FileNotFoundError, OSError) as exc:
        _progress(f"#6 skip: {exc!r}")
        return None

    cache = DocumentCache(workspace_root, session_key)
    if not cache.is_complete(identity.document_id):
        _progress(
            f"#6 skip: no document cache for document_id={identity.document_id!r}"
        )
        return None

    snap = cache.read_snapshot(identity.document_id)
    if snap is None:
        _progress("#6 skip: snapshot incomplete")
        return None
    physical_data, analysis_data, _meta = snap
    if physical_data is None or analysis_data is None:
        _progress("#6 skip: snapshot data missing")
        return None

    # Восстанавливаем DocumentAnalysis in-memory из snapshot.
    try:
        from libs.legal_summarizer.document.physical import PhysicalDocument
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
    from libs.legal_summarizer.application.chunk_selection import (
        select_chunks_for_mode as _select,
    )
    from libs.legal_summarizer.application.inspection import Inspection

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

    # Синтез-вход из document-level cache.
    from libs.legal_summarizer.execution.map_reduce import DOCUMENT_REDUCE_INPUT_BUDGET_CHARS
    context_text = build_question_context(
        selected,
        document_id=identity.document_id,
        workspace_root=workspace_root,
        budget_chars=DOCUMENT_REDUCE_INPUT_BUDGET_CHARS,
        session_key=session_key,
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

    # записываем полноценный NormalizedManifest + result.json
    # для idempotency. Раньше сохраняли только result.json — следующий
    # вызов с тем же operation_id не находил manifest и снова делал
    # LLM call. Теперь manifest со status=completed ловит idempotency-check
    # в начале следующего вызова service.run().
    from libs.legal_summarizer.cache.manifest import NormalizedManifest, save_manifest, write_result
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
    session_key: str = "default",
    batch_limit: int | None = None,
) -> dict:
    """Canonical execution path.

    Порядок:

    1. ``resolve operation_id`` (детерминированно из text+length+path+focus+question);
    2. ``check completed manifest`` — если completed и не legacy → return cached;
    3. ``inspect()`` — один canonical pipeline (document-level);
    4. ``build_execution_context()`` — selected chunks + strategy + plan (run-level);
    5. confirmation / requires_continuation / execute.

    ``batch_limit`` ограничивает объём работы на один вызов (число
    батчей). ``None`` (по умолчанию) сохраняет прежнее поведение —
    полный разбор; этим пользуется ручной запуск из CLI. Заданное
    ограничение применяется к обеим веткам:

    * ``map_reduce`` — выполняется срез очереди батчей, остаток
      возвращается как ``requires_continuation`` с ``progress_report``
      и БЕЗ reduce-фазы, ``result.json`` и финального manifest;
    * ``direct`` — цикла батчей нет, поэтому ограничение не режет
      работу: выполняется единственный неделимый шаг и возвращается
      ``completed`` без обещания продолжения. Отказать по
      неделимости (``legal_budget_unreachable``) — дело вызывающей
      стороны, которая знает потолок вызова; домен этот потолок не
      читает.

    ``focus`` входит в ``operation_id`` (``make_operation_id``): это
    инструкция LLM, меняющая текст сводки, и без него повторный разбор
    того же документа с другим фокусом молча вернул бы прежнюю
    сводку из idempotency-кэша.
    """
    text = (text or "").strip()
    if not text:
        return {
            "status": "failed",
            "error": {"code": "EMPTY_DOCUMENT", "message": "Документ не содержит текста"},
            "operation_id": operation_id,
        }

    # ``length`` нормализовать молча нельзя: опечатка давала бы успешный
    # ответ на 150-250 слов вместо запрошенного формата. Отказ — до
    # платной работы, допустимые значения названы прямо.
    if length not in LENGTH_INSTRUCTIONS:
        return {
            "status": "failed",
            "operation_id": operation_id,
            "error": {
                "code": "INVALID_LENGTH",
                "message": (
                    f"Неизвестный length={length!r}; допустимые значения: "
                    f"{', '.join(sorted(LENGTH_INSTRUCTIONS))}"
                ),
            },
        }

    operation_id = operation_id or make_operation_id(
        text, length,
        document_path=document_path,
        question=question,
        focus=focus,
    )

    existing_manifest = load_manifest(operation_id, workspace_root)
    if (
        existing_manifest is not None
        and existing_manifest.status == "completed"
    ):
        cached_result = read_result(operation_id, workspace_root)
        if cached_result is not None:
            from libs.legal_summarizer.chunking._text_helpers import progress
            progress(f"idempotent: operation {operation_id} уже completed")
            return {
                "status": "completed",
                "operation_id": operation_id,
                "result": cached_result,
                # Явный признак попадания в готовое состояние: работа
                # в этом вызове не начиналась, платы за неё не было, а
                # результат готов. Без этого поля модель не отличила бы
                # короткозамыкание от шага, который отработал и закончил.
                "from_ready_state": True,
                "progress_report": _progress_report_from_manifest(
                    existing_manifest,
                ),
                "stats": {
                    "chars_in": cached_result.get("chars_in"),
                    "chunks": cached_result.get("chunks"),
                    "actual_llm_calls": existing_manifest.actual_llm_calls,
                    "strategy": cached_result.get("strategy"),
                    "duration_sec": existing_manifest.duration_sec,
                    "cached": True,
                },
            }

    # ──────────────────────────────────────────────────────────────────
    # ``--question`` через document-level cache.
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
            session_key=session_key,
        )
        if cached_question_result is not None:
            return cached_question_result

    insp = _inspection_mod.inspect(
        text,
        document_path=document_path,
        workspace_root=workspace_root,
        session_key=session_key,
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

    # Факт подтверждения живёт в состоянии операции (``raw["confirmed"]``).
    # Без этого продолжение без ``confirmed`` снова получило бы
    # ``confirmation_required`` — с нулём сделанных LLM-вызовов.
    confirmed_in_state = bool(
        existing_manifest is not None
        and (existing_manifest.raw or {}).get("confirmed")
    )
    if not confirmed and confirmed_in_state:
        confirmed = True

    # Оценка идёт по остатку работы: уже оплаченные батчи не входят.
    done_chunk_states = (
        dict(existing_manifest.chunk_states) if existing_manifest else {}
    )
    gate_estimate = _estimate_for_remaining(
        run_estimate, _remaining_batches(ctx, done_chunk_states),
    )

    if needs_confirmation(gate_estimate) and not confirmed:
        from libs.legal_summarizer.chunking._text_helpers import progress
        progress(
            f"confirmation_required: chunks={len(ctx.chunks)}, "
            f"batches={gate_estimate.context_batches}, "
            f"est_duration={gate_estimate.estimated_duration_min_sec:.0f}-"
            f"{gate_estimate.estimated_duration_max_sec:.0f}s"
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
                "context_batches_total": gate_estimate.context_batches,
                "estimated_llm_calls": gate_estimate.estimated_llm_calls,
                "strategy": ctx.strategy,
                "title": title,
            },
            "estimate": {
                "min_seconds": gate_estimate.estimated_duration_min_sec,
                "max_seconds": gate_estimate.estimated_duration_max_sec,
                "confirmation_threshold_sec": gate_estimate.confirmation_threshold_sec,
            },
            "progress_report": {
                "done": 0,
                "remaining": gate_estimate.context_batches,
                "continues": True,
            },
            "hint": (
                "Подтвердите запуск разбора явно (параметр confirmed "
                "операции запуска). Суммаризация не начата."
            ),
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
            "progress_report": {
                "done": 0,
                "remaining": run_estimate.context_batches,
                # Работы с этим operation_id не существует: домен
                # отказывает, а не урезает выборку. Отличать этот отказ
                # от усечённого шага (continues: true) — по этому полю.
                "continues": False,
            },
            "hint": (
                f"Выбранная выборка ({len(ctx.chunks)} chunks) превышает "
                f"max_chunks_for_execution={max_chunks_for_execution}. "
                f"Уменьшите max_chunks_per_question / question_fallback_max_chunks "
                f"либо подтвердите запуск явно (параметр confirmed операции "
                f"запуска)."
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
        session_key=session_key,
        batch_limit=batch_limit,
        confirmed=confirmed,
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
