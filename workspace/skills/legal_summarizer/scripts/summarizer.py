"""Map-reduce суммаризация юридических документов (Phase 2B).

Structure-Aware Context Batching:

  file
  ↓
  office_files → load_physical_document (PhysicalDocument)
  ↓
  detect_sections (deterministic + confidence scoring)
  ↓
  StructureAwareChunker → list[Chunk] (с section_path / page_start / page_end)
  ↓
  pack_chunks (section-locality greedy, token budget)
  ↓
  estimate_execution (confirmation_threshold?)
  ↓
  executor (внутри одного run() — без возвратов в AgentLoop):
  ↓
  for batch in context_batches: process_context_batch → → parse_chunk_results
  ↓
  reduce_hierarchical (если meaningful_sections >= 3) | reduce_flat
  ↓
  result.json + manifest.completed

Архитектурные инварианты — см. ``workspace/skills/legal_summarizer/ARCHITECTURE.md``.
"""

from __future__ import annotations

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

from workspace.skills.legal_summarizer.scripts.manifest import (
    NormalizedManifest,
    load_manifest,
    manifest_path,
    read_chunk_result,
    read_result,
    result_path,
    save_manifest,
    write_chunk_result,
    write_result,
)
from workspace.skills.legal_summarizer.scripts.packing import (
    ContextBatch,
    TokenBudget,
    pack_chunks,
)
from workspace.skills.legal_summarizer.scripts.prompts import (
    ChunkResultParseError,
    build_batch_user_message,
    parse_batch_response,
)
from workspace.skills.legal_summarizer.scripts.reducer import (
    ReduceConfig,
    reduce_results,
    should_use_hierarchical_reduce,
)
from workspace.skills.legal_summarizer.scripts.structure.chunks import (
    Chunk,
    ChunkConfig,
    StructureAwareChunker,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock,
    PhysicalDocument,
    load_physical_document,
)
from workspace.skills.legal_summarizer.scripts.structure.sections import (
    ROOT_SECTION_ID,
    DocumentSection,
    SectionTree,
    count_meaningful_sections,
    detect_sections,
    merge_short_sections,
)
from workspace.utils.office_files import extract_text


_SKILL_ROOT = Path(__file__).resolve().parent.parent
_PROMPTS_DIR = _SKILL_ROOT / "prompts"

_PROMPT_FILES = {
    "summarize_system": _PROMPTS_DIR / "summarize_system.md",
    "reduce_system": _PROMPTS_DIR / "reduce_system.md",
    "section_reduce_system": _PROMPTS_DIR / "section_reduce_system.md",
}


# Сколько раз перезапускать LLM-вызов батча при ChunkResultParseError
# (валидный JSON + все chunk_id). LLM-JSON флакает, ретрай обычно помогает.
# При исчерпании — батч помечается failed, обработка документа продолжается.
MAX_BATCH_PARSE_RETRIES = 3
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_THINK_OPEN = "<think>"
_THOUGHT_CLOSE = "</think>"


def _strip_think_blocks(text: str) -> str:
    """Убрать ``<think>...</think>`` блоки из текста (CoT от моделей).

    Некоторые модели (DeepSeek/Qwen) выдают chain-of-thought прямо в
    финальном ответе — иначе ``_extract_subject`` подберёт ``<think>``
    как subject, а summary будет рассуждением вместо саммари. Чистим
    ДО извлечения subject и записи ``result.json``.

    Два варианта мусора:
    1. Закрытый блок ``<think>reasoning</think>answer`` — целиком
       вырезается regex'ом (lazy + DOTALL).
    2. **Незакрытый** ``<think>reasoning\n\nanswer`` — модель забыла
       ``</think>``. Без этого фикса regex не матчит → рассуждение
       утекает в result.json (наблюдалось в проде 2026-08-28 на ГК РФ).
       Решение: отрезать от ``<think>`` до первого абзацного разрыва
       (``\n\n``), сохраняя реальный ответ после. Если разрыва нет
       (весь текст — рассуждение) — отрезать до конца: пустой
       subject/summary лучше сырого ``<think>``.
    """
    if not text:
        return text
    cleaned = _THINK_BLOCK_RE.sub("", text)
    # Дорезаем незакрытые<think>... (модель забыла закрывающий тег).
    while _THINK_OPEN in cleaned:
        idx = cleaned.find(_THINK_OPEN)
        close_idx = cleaned.find(_THOUGHT_CLOSE, idx)
        if close_idx != -1:
            # Повторный/вложенный — отрезаем до ближайшего ``</think>``.
            cleaned = cleaned[:idx] + cleaned[close_idx + len(_THOUGHT_CLOSE):]
            continue
        blank = cleaned.find("\n\n", idx)
        if blank == -1:
            cleaned = cleaned[:idx]
        else:
            cleaned = cleaned[:idx] + cleaned[blank + 2:]
    return cleaned.strip()


def _load_prompt(name: str) -> str:
    """Прочитать системный промпт из ``prompts/<name>.md``."""
    p = _PROMPT_FILES.get(name)
    if p is None or not p.is_file():
        raise FileNotFoundError(
            f"Не найден файл промпта: {p}. Промпты лежат в "
            "workspace/skills/legal_summarizer/prompts/."
        )
    return p.read_text(encoding="utf-8")


_LENGTH_INSTRUCTIONS = {
    "brief": "1 абзац, 150-250 слов: что это за документ и ключевые условия в двух-трёх фразах.",
    "medium": "3-5 абзацев, 400-600 слов: стороны + предмет + условия + сроки + риски.",
    "detailed": "по разделам документа, 800-1200 слов: каждый раздел простым языком.",
}


_SUPPORTED_EXTENSIONS = frozenset({".pdf", ".docx", ".txt"})


def load_text(path) -> str:
    """Извлечь plain text из файла через office_files."""
    p = Path(path)
    if p.suffix.lower() not in _SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Неподдерживаемый формат: '{p.suffix}'. "
            "legal_summarizer принимает только .pdf, .docx, .txt"
        )
    text = extract_text(p)
    if not text or not text.strip():
        raise ValueError(
            f"Документ не содержит извлекаемого текста: {p}."
        )
    return text


def load_structure(path) -> dict:
    """Legacy: title/begin/end/text (Phase 2 совместимость)."""
    p = Path(path)
    if p.suffix.lower() not in _SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Неподдерживаемый формат: '{p.suffix}'."
        )
    fmt = p.suffix.lower().lstrip(".")
    text = extract_text(p)
    if not text or not text.strip():
        raise ValueError(f"Документ не содержит извлекаемого текста: {p}.")

    title: str | None = None
    if fmt == "docx":
        try:
            from docx import Document
            d = Document(str(p))
            if d.core_properties.title:
                title = d.core_properties.title.strip()
        except Exception:
            pass

    return {
        "title": title,
        "begin": text[:800],
        "end": text[-800:],
        "text": text,
        "format": fmt,
        "size_bytes": p.stat().st_size,
    }


def make_operation_id(text: str, length: str) -> str:
    """Стабильный operation_id."""
    sample = text[: 64 * 1024].encode("utf-8", errors="replace")
    h = hashlib.sha256(sample).hexdigest()[:12]
    ts = _time.monotonic_ns()
    return f"op_{ts}_{h}_{length}"


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _doc_context(structure: dict | None, *, with_begin_end: bool = False) -> str:
    if not structure:
        return ""
    parts: list[str] = []
    title = (structure.get("title") or "").strip()
    if title:
        parts.append(f"НАЗВАНИЕ ДОКУМЕНТА: {title}")
    if with_begin_end:
        begin = (structure.get("begin") or "").strip()
        end = (structure.get("end") or "").strip()
        if begin:
            parts.append("НАЧАЛО ДОКУМЕНТА:\n" + begin)
        if end:
            parts.append("КОНЕЦ ДОКУМЕНТА:\n" + end)
    return "\n\n".join(parts)


def _progress(msg: str) -> None:
    print(f"[legal_summarizer] {msg}", file=sys.stderr, flush=True)


def _compute_chunk_size_chars(cfg: dict) -> int:
    fallback = int(cfg.get("chunk_size") or 100000)
    ratio = cfg.get("chunk_size_input_ratio")
    if ratio is None or not (0 < float(ratio) <= 1):
        return fallback
    try:
        from config import SETTINGS as _SET
        ctx_tokens = int(
            _SET.get("agents", {}).get("defaults", {}).get("contextWindowTokens")
            or 0
        )
    except Exception:
        return fallback
    if ctx_tokens <= 0:
        return fallback
    chars = int(ctx_tokens * 3.5 * float(ratio))
    return max(1000, (chars // 1000) * 1000)


def _build_token_budget(cfg: dict) -> TokenBudget:
    try:
        from config import SETTINGS as _SET
        ctx_tokens = int(
            _SET.get("agents", {}).get("defaults", {}).get("contextWindowTokens")
            or 65536
        )
    except Exception:
        ctx_tokens = 65536

    exec_cfg = globals()["get_execution_config"]()
    context_batching_cfg = exec_cfg.get("context_batching") or {}
    return TokenBudget(
        context_window_tokens=ctx_tokens,
        system_prompt_tokens=int(context_batching_cfg.get("system_prompt_tokens", 1200)),
        instruction_tokens=int(context_batching_cfg.get("instruction_tokens_per_map", 200)),
        output_reserve_tokens=int(exec_cfg.get("llm_max_tokens", 8192)),
        safety_margin=float(context_batching_cfg.get("safety_margin", 0.85)),
        chars_per_token=float(context_batching_cfg.get("chars_per_token", 3.5)),
    )


def _iter_text_blocks(text: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return parts or [text]


def _make_text_block(content: str, *, ordinal: int) -> DocumentBlock:
    return DocumentBlock(
        block_id=f"b_{ordinal:04d}",
        block_type="text",
        content=content,
        char_count=len(content),
        page_index=None,
        page_start=None,
        page_end=None,
        paragraph_index=None,
        table_index=None,
        ordinal=ordinal,
        block_metadata={},
    )


@dataclass(frozen=True)
class Inspection:
    chars_in: int
    chunks: list
    context_batches: list
    tree: SectionTree | None
    strategy: str
    estimated_llm_calls: int


def _make_chunk_config(cfg: dict, budget: TokenBudget) -> ChunkConfig:
    chunk_size_chars = _compute_chunk_size_chars(cfg)
    return ChunkConfig(
        max_chunk_chars=min(chunk_size_chars, int(budget.available_chunk_tokens * budget.chars_per_token)),
        chunk_overlap_chars=int(cfg["chunk_overlap"]),
        chars_per_token=budget.chars_per_token,
        table_chunk_threshold_chars=6000,
        min_section_chars=int(cfg.get("min_section_chars", 200)),
    )


def inspect(
    text: str,
    document_path: str | None = None,
) -> Inspection:
    """Осмотреть документ: структура, секции, chunks, context_batches."""
    text = (text or "").strip()
    if not text:
        return Inspection(
            chars_in=0,
            chunks=[],
            context_batches=[],
            tree=None,
            strategy="empty",
            estimated_llm_calls=0,
        )

    cfg = globals()["get_chunking_config"]()
    threshold = int(cfg["single_call_threshold"])
    if len(text) <= threshold:
        sb = _make_text_block(text, ordinal=0)
        dummy_doc = PhysicalDocument(
            path="<inline>",
            format="txt",
            title=None,
            size_bytes=len(text.encode("utf-8")),
            blocks=(sb,),
            page_count=1,
        )
        tree = SectionTree(
            sections={ROOT_SECTION_ID: DocumentSection(
                section_id=ROOT_SECTION_ID,
                level=0,
                heading="",
                section_path="",
                block_indices=(0,),
                children=(),
                parent_id=None,
            )},
            root_id=ROOT_SECTION_ID,
            block_to_section={0: ROOT_SECTION_ID},
        )
        from workspace.skills.legal_summarizer.scripts.structure.chunks import (
            Chunk as _Chunk,
        )
        chunk = _Chunk(
            chunk_id="000",
            index=0,
            text=text,
            char_count=len(text),
            token_estimate=max(1, len(text) // 4),
            page_start=None,
            page_end=None,
            section_id=ROOT_SECTION_ID,
            section_path="",
            section_heading="",
            block_indices=(0,),
            block_types=("text",),
        )
        return Inspection(
            chars_in=len(text),
            chunks=[chunk],
            context_batches=[],
            tree=tree,
            strategy="single",
            estimated_llm_calls=1,
        )

    budget = _build_token_budget(cfg)
    chunk_cfg = _make_chunk_config(cfg, budget)

    try:
        if document_path:
            doc = load_physical_document(document_path)
        else:
            blocks = tuple(
                _make_text_block(p, ordinal=i)
                for i, p in enumerate(_iter_text_blocks(text))
            )
            doc = PhysicalDocument(
                path="<inline>",
                format="txt",
                title=None,
                size_bytes=len(text.encode("utf-8")),
                blocks=blocks,
                page_count=1,
            )
    except Exception:
        blocks = tuple(
            _make_text_block(p, ordinal=i)
            for i, p in enumerate(_iter_text_blocks(text))
        )
        doc = PhysicalDocument(
            path="<inline>",
            format="txt",
            title=None,
            size_bytes=len(text.encode("utf-8")),
            blocks=blocks,
            page_count=1,
        )

    tree = detect_sections(
        doc,
        pdf_path=document_path if (document_path and Path(document_path).suffix.lower() == ".pdf") else None,
    )

    tree = merge_short_sections(
        tree,
        doc.blocks,
        min_section_chars=chunk_cfg.min_section_chars,
    )

    chunker = StructureAwareChunker()
    chunks = chunker.chunk(doc, tree, chunk_cfg)

    batches = pack_chunks(chunks, budget)

    map_calls = len(batches)
    meaningful = count_meaningful_sections(tree, doc.blocks)
    estimated_reduce_calls = 1 + max(0, meaningful - 1)
    estimated_total = map_calls + estimated_reduce_calls

    return Inspection(
        chars_in=len(text),
        chunks=chunks,
        context_batches=batches,
        tree=tree,
        strategy="map_reduce",
        estimated_llm_calls=estimated_total,
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


def _llm_batch(
    batch: ContextBatch,
    *,
    chunks_total: int,
    structure: dict | None,
    length: str,
) -> dict[str, str]:
    """Вызвать LLM для одного ContextBatch. Возвращает dict[chunk_id, summary]."""
    system = _load_prompt("summarize_system").replace(
        "{length_instruction}", _LENGTH_INSTRUCTIONS.get(length, _LENGTH_INSTRUCTIONS["medium"])
    )
    user_body = build_batch_user_message(batch, chunks_total=chunks_total)
    doc_ctx = _doc_context(structure, with_begin_end=False)
    if doc_ctx:
        user_body = doc_ctx + "\n\n" + user_body
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_body},
    ]
    response = llm.chat(messages, context=None)
    return parse_batch_response(batch, response)


def _llm_section_reduce(
    section_path: str,
    section_heading: str,
    joined_text: str,
    *,
    length: str,
) -> str:
    system = _load_prompt("section_reduce_system").replace(
        "{length_instruction}", _LENGTH_INSTRUCTIONS.get(length, _LENGTH_INSTRUCTIONS["medium"])
    )
    user_body = (
        f"Раздел: {section_path}\n"
        f"Заголовок: {section_heading}\n\n"
        f"Частичные саммари чанков этого раздела:\n\n{joined_text}\n\n"
        "Объедини в одно связное саммари этого раздела."
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_body},
    ]
    return llm.chat(messages, context=None)


def _llm_section_trim(
    section_path: str,
    summary: str,
    *,
    max_chars: int,
) -> str:
    """Сократить section summary до max_chars."""
    system = (
        "Ты — редактор. Сократи саммари юридического раздела до краткой формы, "
        "сохранив ключевые факты."
    )
    user_body = (
        f"Раздел: {section_path}\n"
        f"Саммари раздела (превышает допустимый размер):\n\n{summary}\n\n"
        f"Сократи до ≤{max_chars} символов. Сохрани: стороны, предмет, "
        "обязательства, сроки, штрафы."
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_body},
    ]
    return llm.chat(messages, context=None)


def _llm_document_reduce(
    section_summaries_text: str,
    *,
    length: str,
    focus: str | None,
    structure: dict | None,
) -> str:
    system = _load_prompt("reduce_system").replace(
        "{length_instruction}", _LENGTH_INSTRUCTIONS.get(length, _LENGTH_INSTRUCTIONS["medium"])
    )
    doc_ctx = _doc_context(structure, with_begin_end=True)
    user_body = "Саммари разделов документа:\n\n" + section_summaries_text
    if doc_ctx:
        user_body = doc_ctx + "\n\n" + user_body
    if focus:
        user_body = (
            user_body
            + "\n\nФокус пользователя (что особенно важно подсветить): "
            + focus
        )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_body},
    ]
    return llm.chat(messages, context=None)


def _extract_subject(summary: str) -> str:
    lines = [ln.strip() for ln in (summary or "").splitlines()]
    subject = ""
    for ln in lines:
        if ln:
            subject = ln
            break
    if len(subject) > 400:
        m = re.match(r"(.+?[.!?])\s", subject)
        if m:
            subject = m.group(1)
    return subject


def _process_context_batch(
    batch: ContextBatch,
    *,
    chunks_total: int,
    structure: dict | None,
    length: str,
    operation_id: str,
    workspace_root: Path | str | None,
) -> dict[str, str]:
    """Один LLM call → parse → write per-chunk files."""
    started_at = _now_iso()
    start = _time.monotonic()
    result = _llm_batch(
        batch,
        chunks_total=chunks_total,
        structure=structure,
        length=length,
    )
    duration = round(_time.monotonic() - start, 3)
    completed_at = _now_iso()

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


def _load_cached_partials(
    operation_id: str,
    expected_chunk_ids: list[str],
    workspace_root: Path | str | None,
) -> dict[str, str]:
    out: dict[str, str] = {}
    for cid in expected_chunk_ids:
        rec = read_chunk_result(operation_id, cid, workspace_root)
        if rec and isinstance(rec.get("summary"), str):
            out[cid] = rec["summary"]
    return out


def run(
    text: str,
    *,
    length: str = "medium",
    focus: str | None = None,
    confirmed: bool = False,
    operation_id: str | None = None,
    structure: dict | None = None,
    document_path: str | None = None,
    workspace_root: Path | str | None = None,
) -> dict:
    """Главная execution path для legal_summarizer (Phase 2B)."""
    text = (text or "").strip()
    if not text:
        return {
            "status": "failed",
            "error": {"code": "EMPTY_DOCUMENT", "message": "Документ не содержит текста"},
            "operation_id": operation_id,
        }

    length = length if length in _LENGTH_INSTRUCTIONS else "medium"

    insp = inspect(text, document_path=document_path)
    if insp.strategy == "empty":
        return {
            "status": "failed",
            "error": {"code": "EMPTY_DOCUMENT", "message": "Документ не содержит текста"},
            "operation_id": operation_id,
        }

    est = estimate(insp)
    exec_cfg = globals()["get_execution_config"]()
    max_chunks_for_execution = int(exec_cfg["max_chunks_for_execution"])

    operation_id = operation_id or make_operation_id(text, length)

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

    if needs_confirmation(est) and not confirmed:
        _progress(
            f"confirmation_required: chunks={len(insp.chunks)}, "
            f"batches={len(insp.context_batches)}, "
            f"est_duration={est.estimated_duration_min_sec:.0f}-"
            f"{est.estimated_duration_max_sec:.0f}s"
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
                "title": (structure or {}).get("title"),
            },
            "estimate": {
                "min_seconds": est.estimated_duration_min_sec,
                "max_seconds": est.estimated_duration_max_sec,
                "confirmation_threshold_sec": est.confirmation_threshold_sec,
            },
            "hint": "Передайте --confirm для запуска полной обработки.",
        }

    if len(insp.chunks) > max_chunks_for_execution:
        return {
            "status": "requires_continuation",
            "operation_id": operation_id,
            "summary": {
                "chars_in": insp.chars_in,
                "chunks_total": len(insp.chunks),
                "estimated_llm_calls": insp.estimated_llm_calls,
                "title": (structure or {}).get("title"),
            },
            "hint": (
                f"Документ превышает max_chunks_for_execution={max_chunks_for_execution}."
            ),
        }

    chunks = insp.chunks
    batches = insp.context_batches
    tree = insp.tree

    if insp.strategy == "single":
        _progress(f"single-call: chars={insp.chars_in}")
        system = _load_prompt("summarize_system").replace(
            "{length_instruction}", _LENGTH_INSTRUCTIONS.get(length, _LENGTH_INSTRUCTIONS["medium"])
        )
        user_body = (
            "Документ для саммари:\n\n" + text
        )
        doc_ctx = _doc_context(structure, with_begin_end=False)
        if doc_ctx:
            user_body = doc_ctx + "\n\n" + user_body
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_body},
        ]
        summary = llm.chat(messages, context=None)
        # Убираем<think>... от моделей с CoT, чтобы subject/summary
        # в result.json были чистыми (single-strategy path).
        summary = _strip_think_blocks(summary)
        subject = _extract_subject(summary)
        result = {
            "subject": subject,
            "summary": summary,
            "length": length,
            "chars_in": insp.chars_in,
            "chunks": 1,
            "context_batches": 0,
            "sections": 0,
            "strategy": "single",
            "title": (structure or {}).get("title"),
        }
        write_result(operation_id, result, workspace_root=workspace_root)
        return {
            "status": "completed",
            "operation_id": operation_id,
            "result": result,
            "stats": {
                "chars_in": insp.chars_in,
                "chunks": 1,
                "context_batches_total": 0,
                "sections_total": 0,
                "meaningful_sections": 0,
                "map_calls": 1,
                "section_reduce_calls": 0,
                "section_trim_calls": 0,
                "document_reduce_calls": 0,
                "reduce_calls": 0,
                "total_llm_calls": 1,
                "retries": 0,
                "duration_sec": 0.0,
                "strategy": "single",
            },
        }

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
        chars_in=insp.chars_in,
        length=length,
        chunks_total=len(chunks),
        context_batches_total=len(batches),
        estimated_llm_calls=insp.estimated_llm_calls,
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
        is_legacy=False,
        raw={},
    )

    if existing_manifest is None:
        save_manifest(initial_manifest, workspace_root=workspace_root)

    expected_chunk_ids = [c.chunk_id for c in chunks]
    cached_partials = _load_cached_partials(operation_id, expected_chunk_ids, workspace_root)
    chunk_states: dict[str, dict[str, Any]] = dict(existing_manifest.chunk_states) if existing_manifest else {}

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

    for batch in batches:
        pending = [c for c in batch.chunks if c.chunk_id not in chunk_states or chunk_states[c.chunk_id].get("status") != "completed"]
        if not pending:
            continue
        _progress(
            f"batch {batch.batch_id}: {len(pending)}/{len(batch.chunks)} chunks "
            f"({(len(batches) - batches.index(batch)) * 100 // len(batches)}% remaining)"
        )
        batch_to_process = ContextBatch(
            batch_id=batch.batch_id,
            chunks=tuple(pending),
            total_tokens_estimate=sum(c.token_estimate for c in pending),
            section_paths=tuple({c.section_path for c in pending}),
            page_range=batch.page_range,
        )
        batch_meta: dict[str, Any] | None = None
        last_error: tuple[str, Exception] | None = None
        for attempt in range(1, MAX_BATCH_PARSE_RETRIES + 1):
            try:
                batch_meta = _process_context_batch(
                    batch_to_process,
                    chunks_total=len(chunks),
                    structure=structure,
                    length="brief",
                    operation_id=operation_id,
                    workspace_root=workspace_root,
                )
                last_error = None
                break
            except ChunkResultParseError as exc:
                # LLM-JSON флакает — ретраим с тем же промптом.
                last_error = ("LLM_PARSE_ERROR", exc)
                _progress(
                    f"batch {batch.batch_id}: parse error "
                    f"attempt {attempt}/{MAX_BATCH_PARSE_RETRIES}, retrying"
                )
                continue
            except Exception as exc:
                # Не-parse ошибка (сеть, OOM, структурно): не ретраим —
                # помечаем батч failed и идём дальше.
                last_error = ("LLM_ERROR", exc)
                break

        if batch_meta is not None:
            map_calls += 1
            ctx_batches[batch.batch_id] = {
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
                    "context_batch_id": batch.batch_id,
                    "section_id": c.section_id,
                    "section_path": c.section_path,
                    "page_start": c.page_start,
                    "page_end": c.page_end,
                    "result_path": f"chunks/{c.chunk_id}.json",
                    "duration_sec": batch_meta["duration_sec"],
                }
        else:
            # Все retry исчерпаны (parse) или не-parse ошибка:
            # батч помечен failed, обработка документа продолжается.
            assert last_error is not None
            error_code, error_exc = last_error
            retries += 1
            failed_batch_ids.append(batch.batch_id)
            if first_batch_error is None:
                first_batch_error = {
                    "code": error_code,
                    "batch_id": batch.batch_id,
                    "message": str(error_exc),
                }
            ctx_batches[batch.batch_id] = {
                "chunk_ids": [c.chunk_id for c in batch_to_process.chunks],
                "status": "failed",
                "error": {"code": error_code, "message": str(error_exc)},
            }
            for c in batch_to_process.chunks:
                chunk_states[c.chunk_id] = {
                    "status": "failed",
                    "context_batch_id": batch.batch_id,
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

    reduce_cfg = ReduceConfig(
        instruction_tokens_per_section_reduce=200,
        instruction_tokens_per_document_reduce=200,
        chars_per_token=3.5,
        section_summary_max_chars=12000,
    )

    hierarchical = should_use_hierarchical_reduce(tree, chunks, threshold=3)
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
                f"[Chunk {cid}]\n{all_partials[cid]}" for cid in section_chunk_ids
            )
            try:
                section_summary = _llm_section_reduce(
                    section.section_path,
                    section.heading,
                    joined,
                    length=length,
                )
            except Exception:
                section_summary = joined
                retries += 1
            if len(section_summary) > reduce_cfg.section_summary_max_chars:
                try:
                    section_summary = _llm_section_trim(
                        section.section_path,
                        section_summary,
                        max_chars=reduce_cfg.section_summary_max_chars,
                    )
                    section_trim_calls += 1
                except Exception:
                    section_summary = section_summary[: reduce_cfg.section_summary_max_chars]
            section_summaries_out[sid] = section_summary
            section_reduce_calls += 1

        ordered = sorted(
            section_summaries_out.items(),
            key=lambda kv: tuple(int(p) if p.isdigit() else 999 for p in tree.sections[kv[0]].section_path.split(" > ")),
        )
        joined_sections = "\n\n".join(
            f"[Раздел {tree.sections[sid].section_path}: {tree.sections[sid].heading}]\n{summary}"
            for sid, summary in ordered
        )
        try:
            final_summary = _llm_document_reduce(
                joined_sections,
                length=length,
                focus=focus,
                structure=structure,
            )
            document_reduce_calls += 1
        except Exception:
            retries += 1
            final_summary = joined_sections
        strategy_label = "map_reduce_hierarchical"
    else:
        ordered_chunks = [c for c in chunks if c.chunk_id in all_partials]
        joined = "\n\n".join(
            f"[Chunk {c.chunk_id}]\n{all_partials[c.chunk_id]}" for c in ordered_chunks
        )
        try:
            final_summary = _llm_document_reduce(
                joined,
                length=length,
                focus=focus,
                structure=structure,
            )
            document_reduce_calls += 1
        except Exception:
            retries += 1
            final_summary = joined
        strategy_label = "map_reduce_flat"

    # Убираем <think>...</think> от моделей с CoT, чтобы subject/summary
    # в result.json были чистыми для агента.
    final_summary = _strip_think_blocks(final_summary)

    total_duration = round(_time.monotonic() - total_start, 1)
    subject = _extract_subject(final_summary)

    is_partial = bool(failed_batch_ids)

    result = {
        "subject": subject,
        "summary": final_summary,
        "length": length,
        "chars_in": insp.chars_in,
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
        chars_in=insp.chars_in,
        length=length,
        chunks_total=len(chunks),
        context_batches_total=len(batches),
        estimated_llm_calls=insp.estimated_llm_calls,
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
        is_legacy=False,
        raw={},
    )
    save_manifest(final_manifest, workspace_root=workspace_root)

    return {
        "status": "partial" if is_partial else "completed",
        "operation_id": operation_id,
        "result": result,
        "stats": {
            "chars_in": insp.chars_in,
            "chunks_total": len(chunks),
            "context_batches_total": len(batches),
            "sections_total": result["sections"],
            "meaningful_sections": meaningful,
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


# ---------------------------------------------------------------------------
# Legacy shims (Phase 2 backward compat)
# ---------------------------------------------------------------------------


def summarize(
    text: str,
    *,
    length: str = "medium",
    structure: dict | None = None,
) -> dict:
    """Legacy entry point (Phase 2). Используйте ``run()`` напрямую."""
    warnings.warn(
        "summarizer.summarize is deprecated; use summarizer.run() instead",
        DeprecationWarning,
        stacklevel=2,
    )
    rd = run(text, length=length, structure=structure)
    status = rd.get("status")
    if status == "completed":
        return {"status": "success", "data": rd.get("result") or {}}
    if status == "failed":
        err = rd.get("error") or {}
        return {
            "status": "error",
            "data": {"message": err.get("message", "Unknown"), "code": err.get("code")},
        }
    if status == "confirmation_required":
        return {
            "status": "partial",
            "data": {
                "partial_summary": "confirmation_required",
                "chunks_in_batch": 0,
            },
        }
    return {"status": "error", "data": {"message": f"Unknown status: {status}"}}