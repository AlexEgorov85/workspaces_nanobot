"""Canonical orchestration layer для legal_summarizer (PLAN §13, §13c, §32).

Производственный pipeline:

  file / text
  ↓
  DocumentLoader (single-pass loading)
  ↓
  DocumentIdentity
  ↓
  run_canonical_pipeline (DocumentStructure + repair + validate + chunks)
  ↓
  DocumentAnalysis (immutable snapshot, см. §28)
  ↓
  ExecutionPlan (build_execution_plan, через canonical select_strategy)
  ↓
  HierarchicalReducer / final synthesis (canonical §24)
  ↓
  manifest + result.json

Все legacy модули (StructureAwareChunker, SectionTree, document_cleanup,
fingerprint, token_budget, packing, document_cache, document_stats,
_legacy_run_map_reduce) удалены. Canonical pipeline — единственный
production path.

Архитектурные инварианты — см. ``workspace/skills/legal_summarizer/ARCHITECTURE.md``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
import time as _time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import llm
import skill_config
from skill_config import get_chunking_config, get_execution_config

from workspace.utils.session_key import (
    extract_session_key_from_path,
    safe_session_key,
)

from workspace.skills.legal_summarizer.scripts.manifest import (
    NormalizedManifest,
    load_manifest,
    manifest_path,
    read_result,
    result_path,
    save_manifest,
    write_result,
)
from workspace.skills.legal_summarizer.scripts.structure.token_estimator import (
    TokenEstimator,
    TokenEstimatorConfig,
)
from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock,
    PhysicalDocument,
)
from workspace.skills.legal_summarizer.scripts.structure.pipeline import (
    PipelineResult,
    run_canonical_pipeline,
)
from workspace.skills.legal_summarizer.scripts.structure.document_analysis import (
    DocumentAnalysis,
)
from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentStructure,
)
from workspace.skills.legal_summarizer.scripts.structure.execution_plan import (
    ExecutionPlan,
)
from workspace.skills.legal_summarizer.scripts.structure.unified_execution import (
    build_execution_plan,
    select_strategy,
)
from workspace.utils.office_files import extract_text


_SKILL_ROOT = Path(__file__).resolve().parent.parent

_LOCAL_HEADING_RE = re.compile(
    r"^\s*(?:Раздел|Подраздел|Глава|Статья|Часть|§)\b[^\n]{0,120}",
    re.IGNORECASE | re.MULTILINE,
)


def _local_structure_label(text: str) -> str:
    """Extract first structural heading from text chunk (inline replacement).

    Finds the first line starting with a legal section prefix
    (Раздел/Подраздел/Глава/Статья/Часть/§), truncated to 120 chars.
    Returns ``""`` if not found.
    """
    if not text:
        return ""
    m = _LOCAL_HEADING_RE.search(text)
    return m.group(0).strip()[:120] if m else ""


def _chunk_structure_label(chunk: "Chunk") -> str:
    """Структурная метка чанка: global heading, иначе локальная из текста."""
    heading = getattr(chunk, "section_heading", "") or ""
    if heading:
        return heading
    return _local_structure_label(getattr(chunk, "text", "") or "")


def _format_chunk_block(chunk: "Chunk", summary: str) -> str:
    """Подписать блок чанка его структурной меткой при сборке ответа."""
    label = _chunk_structure_label(chunk)
    if label:
        return f"[Chunk {chunk.chunk_id} | {label}]\n{summary}"
    return f"[Chunk {chunk.chunk_id}]\n{summary}"


from workspace.skills.legal_summarizer.scripts.pipeline import (  # noqa: E402
    MAX_BATCH_PARSE_RETRIES,
    now_iso as _now_iso,
    process_context_batch as _process_context_batch,
    run_one_batch_async as _run_one_batch_async,
    load_cached_partials as _load_cached_partials,
)
from workspace.skills.legal_summarizer.scripts.sanitize import (  # noqa: E402
    extract_subject as _extract_subject,
    _THINK_BLOCK_RE,
    _THINK_OPEN,
    _THOUGHT_CLOSE,
    strip_think_blocks as _strip_think_blocks,
)
from workspace.skills.legal_summarizer.scripts.prompts_runtime import (  # noqa: E402
    load_prompt as _load_prompt,
    LENGTH_INSTRUCTIONS as _LENGTH_INSTRUCTIONS,
    QUESTION_INSTRUCTION_TEMPLATE as _QUESTION_INSTRUCTION_TEMPLATE,
    system_instruction as _system_instruction,
)


DOCUMENT_REDUCE_INPUT_BUDGET_CHARS = 60_000
SECTION_REDUCE_INPUT_BUDGET_CHARS = 60_000
MID_REDUCE_GROUP_SIZE = 3
MAX_REDUCE_ROUNDS = 4


def _fit_input(text: str, budget: int) -> str:
    """Урезать text до budget символов стратегией head + tail."""
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


def _resolve_max_chunks() -> int:
    """Максимум chunks для brief/question режимов из конфига."""
    cfg = globals()["get_execution_config"]()
    try:
        n = int(cfg.get("max_chunks_per_question", 8))
        return n if n > 0 else 8
    except (TypeError, ValueError):
        return 8


def _session_key_for(document_path: str | None) -> str | None:
    """Извлечь safe_session_key из пути документа (canonical, без fingerprint)."""
    if not document_path:
        return None
    raw = extract_session_key_from_path(document_path)
    if not raw:
        return None
    return safe_session_key(raw)


_SUPPORTED_EXTENSIONS = frozenset({".pdf", ".docx", ".txt"})


def load_text(path, *, mode: str = "full") -> str:
    """Извлечь plain text из файла через office_files.

    ``mode='brief'`` для PDF: первые 100 стр. + до 300К символов через pypdf.
    ``mode='full'`` (по умолчанию): полная экстракция через pdfplumber/extract_text.
    """
    p = Path(path)
    if p.suffix.lower() not in _SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Неподдерживаемый формат: '{p.suffix}'. "
            "legal_summarizer принимает только .pdf, .docx, .txt"
        )
    if mode == "brief" and p.suffix.lower() == ".pdf":
        text = _extract_pdf_head(p, max_pages=100, max_chars=300_000)
    else:
        text = extract_text(p)
    if not text or not text.strip():
        raise ValueError(
            f"Документ не содержит извлекаемого текста: {p}."
        )
    return text


def _extract_pdf_head(path: Path, *, max_pages: int, max_chars: int) -> str:
    """Извлечь первые ``max_pages`` страниц PDF через pypdf."""
    from pypdf import PdfReader

    reader = PdfReader(str(path), strict=False)
    parts: list[str] = []
    total = 0
    for i in range(min(max_pages, len(reader.pages))):
        try:
            text = reader.pages[i].extract_text() or ""
        except Exception:
            text = ""
        if not text.strip():
            continue
        parts.append(text)
        total += len(text)
        if total >= max_chars:
            break
    return "\n\n".join(parts)


def make_operation_id(
    text: str,
    length: str,
    *,
    document_path: str | None = None,
    question: str | None = None,
) -> str:
    """Стабильный operation_id (Этап 13).

    Детерминированно зависит от:

    * первых 64 КБ текста (sha256 hex);
    * ``length`` (brief / detailed);
    * ``document_path`` (если передан);
    * ``question`` (если передан).

    Не использует wall-clock или ``monotonic_ns`` — два прогона с
    одинаковыми аргументами дают одинаковый ``operation_id``, что
    необходимо для idempotency manifest.
    """
    sample = text[: 64 * 1024].encode("utf-8", errors="replace")
    h = hashlib.sha256(sample).hexdigest()[:12]
    extras = (
        f"\npath:{document_path or ''}"
        f"\nlen:{length}"
        f"\nq:{question or ''}"
    )
    extras_hash = hashlib.sha256(extras.encode("utf-8")).hexdigest()[:8]
    return f"op_{h}_{extras_hash}_{length}"


from workspace.skills.legal_summarizer.scripts.llm_calls import (  # noqa: E402
    doc_context as _doc_context,
    llm_batch as _llm_batch,
    llm_section_reduce as _llm_section_reduce,
    llm_document_reduce as _llm_document_reduce,
)


def _progress(msg: str) -> None:
    """Прогресс ТОЛЬКО в stderr."""
    line = f"[legal_summarizer] {msg}"
    print(line, file=sys.stderr, flush=True)


def _relaxed_lexical_fallback(
    question: str,
    chunks: list,
    *,
    max_chunks: int,
) -> list | None:
    """Управляемый fallback для question: расслабленный lexical match."""
    if not question or not chunks or max_chunks <= 0:
        return None
    raw_words = re.findall(r"\w{4,}", question.lower())
    if not raw_words:
        return None
    prefixes = [w[:4] for w in raw_words]
    matched: list = []
    for c in chunks:
        text_lower = getattr(c, "text", "").lower()
        if any(p in text_lower for p in prefixes):
            matched.append(c)
            if len(matched) >= max_chunks:
                break
    return matched if matched else None


# ---------------------------------------------------------------------------
# Canonical chunk selection by mode
# ---------------------------------------------------------------------------


def _select_chunks_for_mode(
    insp: "Inspection",
    *,
    question: str | None,
    length: str,
) -> list:
    """Select chunks for the given run mode (brief/detailed/question)."""
    max_chunks = _resolve_max_chunks()
    if question:
        from workspace.skills.legal_summarizer.scripts.structure.retrieval import (
            RetrievalConfig,
        )
        hits = insp.analysis.retrieve(
            question, config=RetrievalConfig(max_results=max_chunks),
        ) if insp.analysis is not None else []
        if hits:
            by_id = {c.chunk_id: c for c in insp.chunks}
            chosen = [by_id[h.chunk_id] for h in hits if h.chunk_id in by_id]
            if chosen:
                _progress(f"question: retrieval → {len(chosen)} chunks")
                return chosen
        _progress("question: retrieval пустой → relaxed lexical fallback")
        _exec_cfg = globals()["get_execution_config"]()
        _fallback_max = int(_exec_cfg.get("question_fallback_max_chunks", 16))
        chosen = _relaxed_lexical_fallback(
            question, insp.chunks, max_chunks=_fallback_max,
        )
        if chosen is not None:
            return chosen
        _progress("question: keyword miss → bounded top-of-document fallback")
        return insp.chunks[:_fallback_max]
    if length == "brief":
        from workspace.skills.legal_summarizer.scripts.structure.brief_from_analysis import (
            select_brief_chunks_from_analysis,
        )
        chunk_cfg = globals()["get_chunking_config"]()
        chosen = list(select_brief_chunks_from_analysis(insp.analysis, config=None))
        brief_coverage = chunk_cfg.get("brief_coverage_ratio")
        if brief_coverage is None:
            brief_coverage = 0.5
        if brief_coverage < 1.0 and chosen:
            target = max(1, int(len(chosen) * brief_coverage))
            chosen = chosen[:target]
        brief_total_budget = chunk_cfg.get("brief_max_input_chars")
        if brief_total_budget:
            from workspace.skills.legal_summarizer.scripts.structure.brief_budget import (
                allocate_brief_budget,
            )
            chosen = list(allocate_brief_budget(
                chosen, total_budget_chars=int(brief_total_budget),
            ))
        return chosen
    return list(insp.chunks)


# ---------------------------------------------------------------------------
# Canonical section helpers
# ---------------------------------------------------------------------------


def _section_index(
    struct: DocumentStructure,
) -> tuple[list[str], dict[str, str], dict[str, str]]:
    """Build canonical section index: (ids, headings, paths) from DocumentStructure."""
    section_ids: list[str] = []
    section_headings: dict[str, str] = {}
    section_paths: dict[str, str] = {}
    for node in struct.iter_sections():
        section_ids.append(node.node_id)
        section_headings[node.node_id] = node.title
        parts: list[str] = []
        cur = node
        while cur is not None and cur.node_id != struct.root_id:
            if cur.number is not None and cur.number.ordinal is not None:
                parts.append(str(cur.number.ordinal))
            else:
                parts.append(str(cur.level))
            if cur.parent_id is None:
                break
            cur = struct.nodes.get(cur.parent_id)
        section_paths[node.node_id] = " > ".join(reversed(parts))
    return section_ids, section_headings, section_paths


def _count_meaningful_sections_canonical(struct: DocumentStructure) -> int:
    """Meaningful sections: section nodes with non-empty title or spanning >1 block."""
    return sum(
        1 for n in struct.iter_sections()
        if n.title.strip() or n.end_block > n.start_block
    )


# ---------------------------------------------------------------------------
# Inspection + estimate (unchanged canonical path)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Inspection:
    """Результат inspection документа (PLAN §13).

    Canonical variant: ``structure: DocumentStructure`` + ``analysis: DocumentAnalysis``.
    ``execution_plan`` — единый план map/direct (для инварианта Этапа 2:
    один canonical pipeline на запуск).
    """

    chars_in: int
    chunks: list
    context_batches: list
    structure: DocumentStructure | None
    analysis: DocumentAnalysis | None
    strategy: str
    estimated_llm_calls: int
    execution_plan: ExecutionPlan | None = None


def inspect(
    text: str,
    document_path: str | None = None,
) -> Inspection:
    """Canonical inspection (PLAN §13)."""
    text = (text or "").strip()
    if not text:
        return Inspection(
            chars_in=0, chunks=[], context_batches=[], structure=None,
            analysis=None, strategy="empty", estimated_llm_calls=0,
        )
    if document_path is None:
        raise ValueError(
            "inspect() требует document_path для canonical pipeline; "
            "для inline-текста используйте run_canonical_pipeline напрямую"
        )

    pipeline_result = run_canonical_pipeline(
        document_path,
        text=text,
        apply_repair=True,
        include_retrieval_index=True,
    )

    analysis = pipeline_result.analysis
    chunks = list(pipeline_result.chunks)
    structure = analysis.structure
    strategy = select_strategy(structure, tuple(chunks))

    execution_plan: ExecutionPlan | None = None
    if strategy == "direct":
        estimated = 1
        batches: list[tuple[str, ...]] = []
    else:
        plan = build_execution_plan(
            structure, tuple(chunks),
            document_id=analysis.identity.document_id,
        )
        execution_plan = plan
        batches = [tuple(b.chunk_ids) for b in plan.batches]
        estimated = len(batches) + 1

    return Inspection(
        chars_in=len(text),
        chunks=chunks,
        context_batches=batches,
        structure=structure,
        analysis=analysis,
        strategy=strategy,
        estimated_llm_calls=estimated,
        execution_plan=execution_plan,
    )


@dataclass(frozen=True)
class Estimate:
    chunks_count: int
    context_batches: int
    estimated_llm_calls: int
    estimated_duration_min_sec: float
    estimated_duration_max_sec: float
    confirmation_threshold_sec: float


def estimate(insp: Inspection) -> Estimate:
    cfg = globals()["get_execution_config"]()
    chunk_dur = float(cfg["estimated_chunk_duration_sec"])
    threshold = float(cfg["confirmation_threshold_sec"])
    avg = insp.context_batches.__len__() * chunk_dur
    return Estimate(
        chunks_count=len(insp.chunks),
        context_batches=len(insp.context_batches),
        estimated_llm_calls=insp.estimated_llm_calls,
        estimated_duration_min_sec=round(avg * 0.8, 1),
        estimated_duration_max_sec=round(avg * 1.2, 1),
        confirmation_threshold_sec=threshold,
    )


def needs_confirmation(est: Estimate) -> bool:
    return est.estimated_duration_max_sec > est.confirmation_threshold_sec


_QUICK_SAMPLE_PAGES = 10
_CHARS_OVERESTIMATE = 1.3
_BATCH_OVERESTIMATE_RATIO = 1.0


def quick_estimate(path: Path | str) -> dict[str, Any]:
    """Быстрая оценка размера документа БЕЗ полного извлечения текста."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(str(p))
    ext = p.suffix.lower()
    cfg = globals()["get_execution_config"]()
    chunk_dur = float(cfg["estimated_chunk_duration_sec"])
    threshold = float(cfg["confirmation_threshold_sec"])
    chunk_size = int(globals()["get_chunking_config"]().get("chunk_size", 100000))

    chars_in_est = 0
    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(p), strict=False)
        page_count = len(reader.pages)
        sample_n = min(_QUICK_SAMPLE_PAGES, page_count)
        sample_chars = 0
        for i in range(sample_n):
            try:
                sample_chars += len(reader.pages[i].extract_text() or "")
            except Exception:
                pass
        if sample_n > 0 and sample_chars > 0:
            avg_per_page = sample_chars / sample_n
            chars_in_est = int(avg_per_page * page_count * _CHARS_OVERESTIMATE)
        else:
            chars_in_est = int(p.stat().st_size * 0.5)
    elif ext == ".txt":
        chars_in_est = int(p.stat().st_size * 0.95)
    elif ext == ".docx":
        from docx import Document as _Docx
        doc = _Docx(str(p))
        n_par = len(doc.paragraphs)
        sample_n = min(50, n_par)
        sample_chars = sum(len(par.text) for par in doc.paragraphs[:sample_n])
        if sample_n > 0:
            avg = sample_chars / sample_n
            chars_in_est = int(avg * n_par * _CHARS_OVERESTIMATE)
        else:
            chars_in_est = int(p.stat().st_size * 0.3)
    else:
        chars_in_est = int(p.stat().st_size)

    chunks_count_est = max(1, -(-chars_in_est // max(1, chunk_size)))
    context_batches_est = max(1, int(chunks_count_est * _BATCH_OVERESTIMATE_RATIO))
    estimated_llm_calls_est = context_batches_est + 1
    avg_sec = context_batches_est * chunk_dur

    return {
        "chars_in": chars_in_est,
        "estimate": Estimate(
            chunks_count=chunks_count_est,
            context_batches=context_batches_est,
            estimated_llm_calls=estimated_llm_calls_est,
            estimated_duration_min_sec=round(avg_sec * 0.8, 1),
            estimated_duration_max_sec=round(avg_sec * 1.2, 1),
            confirmation_threshold_sec=threshold,
        ),
    }


# ---------------------------------------------------------------------------
# Canonical execution: _run_direct / _run_map_reduce
# ---------------------------------------------------------------------------


def _count_sections(struct: DocumentStructure | None) -> int:
    if struct is None:
        return 0
    return len(struct.iter_sections())


def _build_manifest(
    *,
    operation_id: str,
    document_path: str | None,
    structure: dict | None,
    analysis: DocumentAnalysis | None,
    chars_in: int,
    length: str,
    chunks_total: int,
    context_batches_total: int,
    estimated_llm_calls: int,
    sections_payload: dict[str, dict[str, Any]],
    started_at: str | None = None,
    article_count: int,
) -> NormalizedManifest:
    title = None
    if analysis is not None and analysis.structure.title is not None:
        title = analysis.structure.title.value
    elif structure:
        title = structure.get("title")
    return NormalizedManifest(
        operation_id=operation_id,
        status="running",
        version=2,
        document_path=document_path,
        structure_title=title,
        chars_in=chars_in,
        length=length,
        chunks_total=chunks_total,
        context_batches_total=context_batches_total,
        estimated_llm_calls=estimated_llm_calls,
        actual_llm_calls=None,
        sections=sections_payload,
        chunk_states={},
        context_batches={},
        section_summaries={},
        batches_done=[],
        batches_failed=[],
        last_error=None,
        started_at=started_at or _now_iso(),
        completed_at=None,
        duration_sec=None,
        article_count=article_count,
        is_legacy=False,
        raw={},
    )


def _run_direct(
    chunks: list,
    *,
    length: str,
    focus: str | None,
    question: str | None,
    structure: dict | None,
    analysis: DocumentAnalysis | None,
    execution_plan: ExecutionPlan | None,
    operation_id: str,
    document_path: str | None,
    workspace_root: Path | str | None,
    chars_in: int,
    insp_estimated_llm_calls: int,
    article_count: int,
    existing_manifest: NormalizedManifest | None,
) -> dict:
    """Canonical direct execution: single llm_document_reduce call.

    Использует ``analysis`` из ``Inspection`` (один canonical pipeline
    на запуск; см. Этап 2).
    """
    total_start = _time.monotonic()
    retries = 0
    ordered = list(chunks)

    joined = "\n\n".join(f"[Chunk {c.chunk_id}]\n{c.text}" for c in ordered)
    joined = _fit_input(joined, DOCUMENT_REDUCE_INPUT_BUDGET_CHARS)

    try:
        final_summary = _llm_document_reduce(
            joined, length=length, focus=focus, structure=structure, question=question,
        )
        reduce_calls = 1
    except Exception:
        retries += 1
        reduce_calls = 0
        final_summary = joined if joined.strip() else ""

    final_summary = _strip_think_blocks(final_summary)

    if not final_summary or not final_summary.strip():
        return {
            "status": "failed",
            "operation_id": operation_id,
            "error": {"code": "REDUCE_INPUT_EMPTY", "message": "Document reduce вернул пустой summary"},
        }

    duration = round(_time.monotonic() - total_start, 1)
    subject = _extract_subject(final_summary)

    title = None
    if analysis is not None and analysis.structure.title is not None:
        title = analysis.structure.title.value
    elif structure:
        title = structure.get("title")

    result = {
        "subject": subject,
        "summary": final_summary,
        "length": length,
        "chars_in": chars_in,
        "chunks": len(ordered),
        "context_batches": 1,
        "sections": _count_sections(analysis.structure if analysis else None),
        "strategy": "direct",
        "title": title,
        "partial": False,
    }
    write_result(operation_id, result, workspace_root=workspace_root)

    manifest = _build_manifest(
        operation_id=operation_id,
        document_path=document_path,
        structure=structure,
        analysis=analysis,
        chars_in=chars_in,
        length=length,
        chunks_total=len(ordered),
        context_batches_total=1,
        estimated_llm_calls=insp_estimated_llm_calls,
        sections_payload={},
        started_at=existing_manifest.started_at if existing_manifest else _now_iso(),
        article_count=article_count,
    )
    manifest.status = "completed"
    manifest.actual_llm_calls = reduce_calls
    manifest.completed_at = _now_iso()
    manifest.duration_sec = duration
    manifest.context_batches = {
        "cb_000": {"chunk_ids": [c.chunk_id for c in ordered], "status": "completed"},
    }
    manifest.batches_done = ["cb_000"]
    save_manifest(manifest, workspace_root=workspace_root)

    sections_total = _count_sections(analysis.structure if analysis else None)
    meaningful_sections = (
        _count_meaningful_sections_canonical(analysis.structure)
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


def _run_map_reduce(
    chunks: list,
    *,
    strategy: str,
    length: str,
    focus: str | None,
    question: str | None,
    structure: dict | None,
    analysis: DocumentAnalysis | None,
    execution_plan: ExecutionPlan | None,
    document_path: str | None,
    operation_id: str,
    workspace_root: Path | str | None,
    chars_in: int,
    insp_estimated_llm_calls: int,
    article_count: int,
    existing_manifest: NormalizedManifest | None,
) -> dict:
    """Canonical map_reduce: batch execution → hierarchical/flat reduce.

    Invariant Этапа 2: ``run_canonical_pipeline`` вызывается ровно один раз
    в ``inspect()``. Эта функция использует уже готовые ``analysis`` и
    ``execution_plan`` из ``Inspection``.
    """
    from workspace.skills.legal_summarizer.scripts.structure.hierarchical_reducer import (
        HierarchicalReducerConfig,
        reduce_chunks_hierarchical,
        reduce_sections_to_document,
    )

    struct = analysis.structure if analysis is not None else None
    plan = execution_plan
    if plan is None and struct is not None:
        plan = build_execution_plan(
            struct,
            tuple(chunks),
            document_id=analysis.identity.document_id if analysis else "",
        )
    # Map actual batches strictly to plan.batches[i].chunk_ids (one-to-one,
    # no duplication, no omission). See Этап 1 acceptance: each chunk is
    # processed exactly once.
    chunk_by_id = {c.chunk_id: c for c in chunks}
    final_batches = [
        [chunk_by_id[cid] for cid in batch.chunk_ids if cid in chunk_by_id]
        for batch in plan.batches
    ]
    # Invariant: union of final_batches chunk_ids == expected chunk_ids.
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
            "Этап 1 invariant violated: duplicate chunk_id in map batches",
        )

    # Section metadata for manifest + hierarchical reduce
    section_ids: list[str] = []
    section_headings: dict[str, str] = {}
    section_paths: dict[str, str] = {}
    sections_payload: dict[str, dict[str, Any]] = {}
    if struct is not None:
        section_ids, section_headings, section_paths = _section_index(struct)
        for node in struct.iter_sections():
            sections_payload[node.node_id] = node.to_dict()

    # Manifest initialization
    article_count_for_manifest = article_count
    initial_manifest = _build_manifest(
        operation_id=operation_id,
        document_path=document_path,
        structure=structure,
        analysis=analysis,
        chars_in=chars_in,
        length=length,
        chunks_total=len(chunks),
        context_batches_total=len(final_batches),
        estimated_llm_calls=insp_estimated_llm_calls,
        sections_payload=sections_payload,
        started_at=existing_manifest.started_at if existing_manifest else _now_iso(),
        article_count=article_count_for_manifest,
    )
    if existing_manifest is None:
        save_manifest(initial_manifest, workspace_root=workspace_root)

    expected_chunk_ids = [c.chunk_id for c in chunks]
    cached_partials = _load_cached_partials(operation_id, expected_chunk_ids, workspace_root)
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
        _progress(
            f"batch {batch_id}: {len(pending)}/{len(batch_chunks)} chunks "
            f"queued ({total_batches} batches total, concurrency={concurrency})"
        )

    if queued:
        sem = asyncio.Semaphore(concurrency)

        async def _gather_all():
            return await asyncio.gather(*[
                _run_one_batch_async(
                    pending_chunks,
                    chunks_total=len(chunks),
                    structure=structure,
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

        for (batch_id, batch_chunks, _pending_count), (status, batch_meta, last_error) in zip(
            queued, gather_results
        ):
            if status == "ok":
                assert batch_meta is not None
                map_calls += 1
                ctx_batches[batch_id] = {
                    "chunk_ids": batch_meta["chunk_ids"],
                    "status": "completed",
                    "started_at": batch_meta["started_at"],
                    "completed_at": batch_meta["completed_at"],
                    "duration_sec": batch_meta["duration_sec"],
                    "section_paths": list({c.section_path for c in batch_chunks}),
                }
                for c in batch_chunks:
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

    all_partials = _load_cached_partials(operation_id, expected_chunk_ids, workspace_root)

    if not all_partials:
        return {
            "status": "failed",
            "operation_id": operation_id,
            "error": {"code": "NO_PARTIALS", "message": "Нет per-chunk partials"},
        }

    _section_summary_max_chars = 12000
    section_reduce_calls = 0
    document_reduce_calls = 0

    # Hierarchical reduce via canonical reducer
    if strategy == "map_hierarchical" and struct is not None and section_ids:
        reducer_config = HierarchicalReducerConfig(
            group_size=MID_REDUCE_GROUP_SIZE,
            max_rounds=MAX_REDUCE_ROUNDS,
            input_budget_chars=DOCUMENT_REDUCE_INPUT_BUDGET_CHARS,
            section_summary_max_chars=_section_summary_max_chars,
        )

        def _llm_section_runner(joined, *, section_path="", section_heading="", **_kw):
            result = _llm_section_reduce(
                section_path, section_heading, joined,
                length=length, question=question,
            )
            result = _strip_think_blocks(result)
            if len(result) > _section_summary_max_chars:
                result = _fit_input(result, _section_summary_max_chars)
            return result

        def _llm_doc_runner(joined, *, length=length, focus=focus, structure=structure, question=question, **_kw):
            return _strip_think_blocks(
                _llm_document_reduce(
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
        # Flat reduce: join all chunk partials
        ordered_chunks = [c for c in chunks if c.chunk_id in all_partials]
        joined = "\n\n".join(
            _format_chunk_block(c, all_partials[c.chunk_id]) for c in ordered_chunks
        )
        if not joined.strip():
            return {
                "status": "failed",
                "operation_id": operation_id,
                "error": {"code": "REDUCE_INPUT_EMPTY", "message": "Нет валидных partial summaries для финального reduce"},
            }
        joined = _fit_input(joined, DOCUMENT_REDUCE_INPUT_BUDGET_CHARS)
        try:
            final_summary = _llm_document_reduce(
                joined, length=length, focus=focus, structure=structure, question=question,
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
            "error": {"code": "REDUCE_INPUT_EMPTY", "message": "Document reduce вернул пустой summary"},
        }

    total_duration = round(_time.monotonic() - total_start, 1)
    subject = _extract_subject(final_summary)
    is_partial = bool(failed_batch_ids)
    total_llm_calls = map_calls + section_reduce_calls + document_reduce_calls
    meaningful = _count_meaningful_sections_canonical(struct) if struct else 0

    title = None
    if analysis is not None and analysis.structure.title is not None:
        title = analysis.structure.title.value
    elif structure:
        title = structure.get("title")

    result = {
        "subject": subject,
        "summary": final_summary,
        "length": length,
        "chars_in": chars_in,
        "chunks": len(chunks),
        "context_batches": len(final_batches),
        "sections": _count_sections(struct),
        "strategy": strategy_label,
        "title": title,
        "partial": is_partial,
    }
    write_result(operation_id, result, workspace_root=workspace_root)

    final_manifest = _build_manifest(
        operation_id=operation_id,
        document_path=document_path,
        structure=structure,
        analysis=analysis,
        chars_in=chars_in,
        length=length,
        chunks_total=len(chunks),
        context_batches_total=len(final_batches),
        estimated_llm_calls=insp_estimated_llm_calls,
        sections_payload=sections_payload,
        started_at=existing_manifest.started_at if existing_manifest else _now_iso(),
        article_count=article_count_for_manifest,
    )
    final_manifest.status = "partial" if is_partial else "completed"
    final_manifest.actual_llm_calls = total_llm_calls
    final_manifest.chunk_states = chunk_states
    final_manifest.context_batches = ctx_batches
    final_manifest.section_summaries = {}
    final_manifest.batches_done = [f"cb_{i:03d}" for i in range(len(final_batches))]
    final_manifest.batches_failed = list(failed_batch_ids)
    final_manifest.last_error = first_batch_error
    final_manifest.completed_at = _now_iso()
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
            "sections_total": _count_sections(struct),
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


# ---------------------------------------------------------------------------
# Public API: run()
# ---------------------------------------------------------------------------


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
    """Canonical execution path (Этап 14: idempotency до analysis).

    Порядок:

    1. ``resolve operation_id`` (детерминированно из text+length+path+question);
    2. ``check completed manifest`` — если completed и не legacy → return cached;
    3. ``inspect()`` — только если нужен;
    4. select chunks, estimate, execute.
    """
    text = (text or "").strip()
    if not text:
        return {
            "status": "failed",
            "error": {"code": "EMPTY_DOCUMENT", "message": "Документ не содержит текста"},
            "operation_id": operation_id,
        }

    length = length if length in _LENGTH_INSTRUCTIONS else "brief"

    # Этап 14: resolve operation_id ДО analysis.
    operation_id = operation_id or make_operation_id(
        text, length, document_path=document_path, question=question,
    )

    # Этап 14: idempotency check до inspect/analysis.
    existing_manifest = load_manifest(operation_id, workspace_root)
    if (
        existing_manifest is not None
        and existing_manifest.status == "completed"
        and not existing_manifest.is_legacy
    ):
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

    insp = inspect(text, document_path=document_path)
    if insp.strategy == "empty":
        return {
            "status": "failed",
            "error": {"code": "EMPTY_DOCUMENT", "message": "Документ не содержит текста"},
            "operation_id": operation_id,
        }

    chosen_chunks = _select_chunks_for_mode(insp, question=question, length=length)

    est = estimate(insp)
    exec_cfg = globals()["get_execution_config"]()
    max_chunks_for_execution = int(exec_cfg["max_chunks_for_execution"])

    # Этап 14: idempotency уже обработан выше (до inspect).
    # existing_manifest нужен для resume/sections_payload в execution,
    # поэтому он загружается здесь повторно (cache hit на filesystem).
    existing_manifest = load_manifest(operation_id, workspace_root)

    if needs_confirmation(est) and not confirmed:
        _progress(
            f"confirmation_required: chunks={len(insp.chunks)}, "
            f"batches={len(insp.context_batches)}, "
            f"est_duration={est.estimated_duration_min_sec:.0f}-"
            f"{est.estimated_duration_max_sec:.0f}s"
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
                "context_batches_total": len(insp.context_batches),
                "estimated_llm_calls": insp.estimated_llm_calls,
                "strategy": insp.strategy,
                "title": title,
            },
            "estimate": {
                "min_seconds": est.estimated_duration_min_sec,
                "max_seconds": est.estimated_duration_max_sec,
                "confirmation_threshold_sec": est.confirmation_threshold_sec,
            },
            "hint": "Передайте --confirm для запуска полной обработки.",
        }

    if len(chosen_chunks) > max_chunks_for_execution:
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
                "chunks_selected": len(chosen_chunks),
                "estimated_llm_calls": insp.estimated_llm_calls,
                "title": title,
            },
            "hint": (
                f"Выбранная выборка ({len(chosen_chunks)} chunks) превышает "
                f"max_chunks_for_execution={max_chunks_for_execution}. "
                f"Уменьшите max_chunks_per_question / question_fallback_max_chunks "
                f"или передайте --confirm для принудительного продолжения."
            ),
        }

    article_count = len(re.findall(r"Статья\s+\d+(?:\.\d+)?", text))
    strategy = select_strategy(insp.structure, tuple(chosen_chunks))

    _common = dict(
        length=length,
        focus=focus,
        question=question,
        structure=structure,
        analysis=insp.analysis,
        execution_plan=insp.execution_plan,
        document_path=document_path,
        operation_id=operation_id,
        workspace_root=workspace_root,
        chars_in=insp.chars_in,
        insp_estimated_llm_calls=insp.estimated_llm_calls,
        article_count=article_count,
        existing_manifest=existing_manifest,
    )

    if strategy == "direct":
        return _run_direct(chosen_chunks, **_common)

    return _run_map_reduce(chosen_chunks, strategy=strategy, **_common)


# ---------------------------------------------------------------------------
# Public API (PLAN §13)
# ---------------------------------------------------------------------------

__all__ = [
    "run",
    "inspect",
    "Inspection",
    "Estimate",
    "estimate",
    "needs_confirmation",
    "quick_estimate",
    "make_operation_id",
    "load_text",
]
