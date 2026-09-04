"""Canonical orchestration layer для legal_summarizer (PLAN §13).

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
fingerprint, token_budget, packing, document_cache, document_stats)
**не** импортируются в этой версии. Canonical pipeline — единственный
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
from workspace.skills.legal_summarizer.scripts.structure.unified_execution import (
    build_execution_plan,
)
from workspace.utils.office_files import extract_text


_SKILL_ROOT = Path(__file__).resolve().parent.parent


def _chunk_structure_label(chunk: "Chunk") -> str:
    """Структурная метка чанка: global heading, иначе локальная из текста.

    Когда detect_sections не нашёл разделов (например, заголовки
    «утоплены» внутри постраничных блоков PDF), чанк несёт пустой
    section_heading. Тогда извлекаем метку (Раздел/Глава/Часть/Статья)
    прямо из текста чанка, чтобы подписать его при сборке общего ответа.
    """
    heading = getattr(chunk, "section_heading", "") or ""
    if heading:
        return heading
    return extract_local_structure_label(getattr(chunk, "text", "") or "")


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


# Бюджет входа для document_reduce (до LLM), chars. ~17K токенов для
# большинства моделей с контекстом 32K+ — компактно для провайдера,
# стабильно для ответа. НЕ путать с ReduceConfig.section_summary_max_chars:
# это бюджет ВЫХОДА section_reduce (обрезает одну section_summary после LLM).
DOCUMENT_REDUCE_INPUT_BUDGET_CHARS = 60_000

# Бюджет входа для section_reduce. Большие разделы (например раздел ГК РФ
# «Общие положения» — сотни статей) могут дать joined >> 60K chars из
# склеенных partials; обрезка до входа в LLM спасает от того же перегруза.
SECTION_REDUCE_INPUT_BUDGET_CHARS = 60_000

# Сколько section_summary группировать в один промежуточный reduce-вызов
# при рекурсивной иерархии. 3 — баланс: группы достаточно малы, чтобы
# вход умещался в budget, при этом цепочка не взрывается по числу вызовов.
MID_REDUCE_GROUP_SIZE = 3

# Safety net: оборвать рекурсивный reduce после этого числа раундов даже
# если секций > group_size**max_rounds. Защита от взрывного роста
# LLM-вызовов на очень больших документах (~100+ секций).
MAX_REDUCE_ROUNDS = 4


def _fit_input(text: str, budget: int) -> str:
    """Урезать text до budget символов стратегией head + tail.

    Сохраняет начало (заголовки, контекст, ключевые определения) и конец
    (выводы, заключительные формулировки). Средняя часть заменяется явным
    маркером с числом пропущенных символов — без него модель может
    «додумать» пропущенное и сгенерировать галлюцинации на стыке.
    """
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


from workspace.skills.legal_summarizer.scripts.document_cache import (  # noqa: E402
    doc_cache_dir as _doc_cache_dir,
    load_doc_cache as _load_doc_cache,
    save_doc_cache as _save_doc_cache,
)
from workspace.skills.legal_summarizer.scripts.fingerprint import (  # noqa: E402
    resolve_document_id as _resolve_document_id,
    resolve_session_key as _resolve_session_key,
)
from workspace.skills.legal_summarizer.scripts.token_budget import (  # noqa: E402
    count_tokens as _count_tokens,
)


_SUPPORTED_EXTENSIONS = frozenset({".pdf", ".docx", ".txt"})


def load_text(path, *, mode: str = "full") -> str:
    """Извлечь plain text из файла через office_files.

    ``mode='brief'`` для PDF: первые 100 стр. + до 300К символов через pypdf
    (быстрая экстракция ~5 сек для ГК РФ вместо 70+ сек pdfplumber).
    Для ГК РФ 663 стр. это первые 100 стр. = общая часть + оглавление,
    чего достаточно для краткого саммари.

    ``mode='full'`` (по умолчанию): полная экстракция через pdfplumber/extract_text.

    Args:
        path: путь к .pdf / .docx / .txt.
        mode: ``"full"`` для detailed/question, ``"brief"`` для --length brief.
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
    """Извлечь первые ``max_pages`` страниц PDF через pypdf.

    Быстрая экстракция (5-10× быстрее pdfplumber). Используется в brief mode
    для чтения только начала документа — титульник, общая часть, оглавление.
    """
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


from workspace.skills.legal_summarizer.scripts.llm_calls import (  # noqa: E402
    doc_context as _doc_context,
    llm_batch as _llm_batch,
    llm_section_reduce as _llm_section_reduce,
    llm_document_reduce as _llm_document_reduce,
)


def _progress(msg: str) -> None:
    """Прогресс ТОЛЬКО в stderr.

    ВАЖНО: НЕ зеркалим в stdout — иначе progress-строки попадают в финальный
    вывод exec-вызова и LLM видит их как 'шум' / 'побитую кириллицу'
    (инцидент 2026-08-31: агент интерпретировал progress как legacy cp1251).
    Для polling через ``write_stdin`` sentinel ``__LEGAL_SUMMARIZER_DONE__``
    достаточно, agent его ловит и не путается в потоке.
    """
    line = f"[legal_summarizer] {msg}"
    print(line, file=sys.stderr, flush=True)


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


def _build_token_budget(cfg: dict | None = None) -> TokenBudget:
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
    # Test override: context_window_tokens из chunking_config (для unit-тестов,
    # которые хотят принудительно направить ExecutionStrategy в MAP_* —
    # уменьшают window через mock).
    if cfg is None:
        cfg = globals()["get_chunking_config"]()
    override_ctx = cfg.get("context_window_tokens") if isinstance(cfg, dict) else None
    if isinstance(override_ctx, (int, float)) and override_ctx > 0:
        ctx_tokens = int(override_ctx)
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


def _inline_stats(doc, tree, chars_per_token: float) -> dict[str, int]:
    """Inline замена compute_document_stats (PLAN §15).

    Возвращает dict с метриками: chars, estimated_tokens, blocks,
    sections, tables. Раньше это был ``DocumentStats`` dataclass, но
    теперь вычисляется inline без отдельного класса.
    """
    chars = sum(len(b.content) for b in doc.blocks)
    estimated_tokens = max(1, int(chars / chars_per_token + 0.999))
    blocks = len(doc.blocks)
    tables = sum(1 for b in doc.blocks if b.block_type == "table")
    sections = 0
    if tree is not None:
        sections = sum(
            1 for sid in tree.sections
            if sid != tree.root_id
        )
    return {
        "chars": chars,
        "estimated_tokens": estimated_tokens,
        "blocks": blocks,
        "sections": sections,
        "tables": tables,
    }


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


def _relaxed_lexical_fallback(
    question: str,
    chunks: list,
    *,
    max_chunks: int,
) -> list | None:
    """Управляемый fallback для question: расслабленный lexical match.

    Когда строгий keyword match (``select_relevant_chunks``) ничего не нашёл,
    делаем вторую попытку с prefix-match по первым 4 символам каждого слова
    вопроса. Это устойчиво к словоформам русского языка
    («договор» ↔ «договора» ↔ «договору»).

    Возвращает ``None``, если даже расслабленный match не нашёл ничего —
    caller эскалирует на bounded top-of-document fallback.
    """
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


@dataclass(frozen=True)
class Inspection:
    """Результат inspection документа (PLAN §13).

    Canonical variant: вместо legacy ``tree: SectionTree`` теперь
    ``structure: DocumentStructure`` + ``analysis: DocumentAnalysis``.

    Attributes:
        chars_in: длина входного текста.
        chunks: список ``Chunk`` из canonical ChunkPlanner.
        context_batches: список batch'ей из canonical ExecutionPlan.
        structure: ``DocumentStructure`` (canonical semantic structure).
        analysis: ``DocumentAnalysis`` (immutable snapshot, для follow-up).
        strategy: ``"direct"`` / ``"map_flat"`` / ``"map_hierarchical"`` /
            ``"empty"`` (пустой документ).
        estimated_llm_calls: оценка числа LLM-вызовов.
    """

    chars_in: int
    chunks: list
    context_batches: list
    structure: DocumentStructure | None
    analysis: DocumentAnalysis | None
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
    """Canonical inspection (PLAN §13).

    Pipeline:
        text / document_path
          ↓ DocumentLoader (single-pass loading)
          ↓ run_canonical_pipeline (DocumentStructure + repair + validate)
          ↓ ChunkPlanner → DocumentAnalysis
          ↓ build_execution_plan → ExecutionPlan.batches

    Canonical single source of truth — ``DocumentAnalysis``.
    """
    text = (text or "").strip()
    if not text:
        return Inspection(
            chars_in=0,
            chunks=[],
            context_batches=[],
            structure=None,
            analysis=None,
            strategy="empty",
            estimated_llm_calls=0,
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

    from workspace.skills.legal_summarizer.scripts.structure.unified_execution import (
        select_strategy,
    )
    strategy = select_strategy(structure, tuple(chunks))

    if strategy == "direct":
        estimated = 1
        batches: list[tuple[str, ...]] = []
    else:
        plan = build_execution_plan(
            structure,
            tuple(chunks),
            document_id=analysis.identity.document_id,
        )
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


# Сэмпл страниц для оценки плотности chars/page в quick_estimate.
# 10 страниц достаточно для грубой оценки (< сек на pypdf для 600+ стр.).
_QUICK_SAMPLE_PAGES = 10
# Консервативные множители (лучше переспросить confirm, чем стартовать
# длинный ран без предупреждения — пользователь не любит, когда «обещали
# 5 минут, а вышло 12»).
_CHARS_OVERESTIMATE = 1.3
_BATCH_OVERESTIMATE_RATIO = 1.0  # batches ~= chunks (worst case 1 chunk/batch)


def quick_estimate(path: Path | str) -> dict[str, Any]:
    """Быстрая оценка размера документа БЕЗ полного извлечения текста.

    Используется как pre-confirm gate в ``cli.py``: для PDF (ГК РФ на 663
    стр.) полная экстракция через pdfplumber занимает ~3–5 минут —
    пользователь 3.5 минуты ждал только чтобы узнать «документ большой»
    (см. инцидент 2026-08-28). Здесь — pypdf page_count + сэмпл 10
    страниц (~секунды), для txt/docx — дешёвое чтение длины.

    Возвращает ``{"chars_in": int, "estimate": Estimate}``. Оценки
    завышены (×1.3 chars, batches ~= chunks) чтобы fast-path скорее
    требовал confirm, чем рисковал стартовать длинный ран.
    """
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
            # пустой/битый PDF — грубый fallback по размеру файла
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
        # для неподдерживаемых расширений — fallback на размер; пусть
        # load_text() ниже сам бросит ValueError с понятным сообщением
        chars_in_est = int(p.stat().st_size)

    chunks_count_est = max(1, -(-chars_in_est // max(1, chunk_size)))  # ceil
    # batches ~= chunks (worst case 1 chunk/batch) → консервативная оценка
    context_batches_est = max(1, int(chunks_count_est * _BATCH_OVERESTIMATE_RATIO))
    estimated_llm_calls_est = context_batches_est + 1  # +1 doc reduce
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
    """Главная execution path для legal_summarizer (Phase 2B + brief/detailed/question)."""
    text = (text or "").strip()
    if not text:
        return {
            "status": "failed",
            "error": {"code": "EMPTY_DOCUMENT", "message": "Документ не содержит текста"},
            "operation_id": operation_id,
        }

    length = length if length in _LENGTH_INSTRUCTIONS else "brief"

    insp = inspect(text, document_path=document_path)
    if insp.strategy == "empty":
        return {
            "status": "failed",
            "error": {"code": "EMPTY_DOCUMENT", "message": "Документ не содержит текста"},
            "operation_id": operation_id,
        }

    # === Выбор chunks по режиму ===
    # brief / detailed / question используют разные стратегии:
    #   brief    — первые max_chunks
    #   detailed — все chunks
    #   question — chunks, содержащие слова вопроса (≤max_chunks);
    #              если ничего не нашли — fallback на detailed (все)
    document_id = _resolve_document_id(document_path, text)
    session_key = _resolve_session_key(document_path)
    max_chunks = _resolve_max_chunks()

    if insp.strategy == "map_reduce":
        if question:
            # Cache-assisted follow-up retrieval: если для документа есть
            # свежий doc_cache, пытаемся вытащить точные source fragments
            # через PhysicalDocument. При stale/no match/weak — existing
            # keyword match + relaxed fallback.
            cache_chunks = None
            if session_key:
                try:
                    from cache_followup import (
                        retrieve_followup_context_via_cache,
                    )
                    phys_doc = None
                    if document_path:
                        try:
                            phys_doc = load_physical_document(
                                document_path, workspace_root=workspace_root,
                            )
                        except Exception:
                            phys_doc = None
                    if phys_doc is not None:
                        cache_chunks = retrieve_followup_context_via_cache(
                            question=question,
                            document_id=document_id,
                            session_key=session_key,
                            document_path=document_path,
                            workspace_root=workspace_root,
                            doc=phys_doc,
                            max_candidates=max_chunks,
                            min_top_score=3,
                        )
                except Exception:
                    cache_chunks = None
            if cache_chunks:
                _progress(
                    f"question: cache-assisted retrieval → {len(cache_chunks)} chunks"
                )
                chosen_chunks = cache_chunks
            else:
                from brief_strategy import select_relevant_chunks as _sel_relevant
                chosen_chunks = _sel_relevant(question, insp.chunks, max_chunks=max_chunks)
                if chosen_chunks is None:
                    _progress("question: keyword match пустой → controlled fallback")
                    _exec_cfg = globals()["get_execution_config"]()
                    _question_fallback_max = int(
                        _exec_cfg.get("question_fallback_max_chunks", 16)
                    )
                    chosen_chunks = _relaxed_lexical_fallback(
                        question, insp.chunks, max_chunks=_question_fallback_max,
                    )
                    if chosen_chunks is None:
                        _progress(
                            "question: keyword miss → bounded top-of-document fallback"
                        )
                        chosen_chunks = insp.chunks[:_question_fallback_max]
        elif length == "brief":
            from workspace.skills.legal_summarizer.scripts.structure.brief_from_analysis import (
                select_brief_chunks_from_analysis,
            )
            chunk_cfg_brief = globals()["get_chunking_config"]()
            brief_coverage = chunk_cfg_brief.get("brief_coverage_ratio")
            if brief_coverage is None:
                brief_coverage = 0.5
            chosen_chunks = select_brief_chunks_from_analysis(
                insp.analysis,
                config=None,
            )
            if brief_coverage < 1.0 and chosen_chunks:
                target_count = max(1, int(len(chosen_chunks) * brief_coverage))
                chosen_chunks = chosen_chunks[:target_count]
            # Двухуровневая модель (coverage + budget):
            #
            #   1. Coverage: select_brief_chunks_structured уже выбрал
            #      N chunks (round-robin по sections, max N=10).
            #   2. Budget: общий лимит chars для LLM-input через
            #      brief_max_input_chars (предпочтительно, новый путь).
            #      Распределяется пропорционально между chunks
            #      через brief_representation.allocate_brief_budget.
            #
            # Если brief_max_input_chars не задан (None/0) — fallback
            # на legacy per-chunk budget (brief_max_chars_per_chunk),
            # чтобы старые конфиги продолжали работать.
            brief_total_budget = chunk_cfg_brief.get("brief_max_input_chars")
            if brief_total_budget:
                from workspace.skills.legal_summarizer.scripts.structure.brief_budget import (
                    allocate_brief_budget,
                )
                chosen_chunks = allocate_brief_budget(
                    chosen_chunks, total_budget_chars=int(brief_total_budget),
                )
            else:
                # No-op: без brief_max_input_chars возвращаем как есть.
                pass
        else:
            chosen_chunks = insp.chunks
    else:
        # single-call: chunks — это dummy Chunk, не трогаем.
        chosen_chunks = insp.chunks

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

    # === Map-reduce СЃС‚СЂР°С‚РµРіРёСЏ: РґРµР»РµРіР°С†РёСЏ РІ legacy transitional layer ===
    # (СЃРј. _legacy_run_map_reduce.py вЂ” Р±СѓРґРµС‚ СѓРґР°Р»С‘РЅ РІ В§32 РїРѕСЃР»Рµ РїРѕР»РЅРѕРіРѕ
    # РїРµСЂРµРІРѕРґР° map-reduce РЅР° canonical ExecutionPlan + HierarchicalReducer)
    if insp.strategy != "single":
        from workspace.skills.legal_summarizer.scripts._legacy_run_map_reduce import (
            legacy_run_map_reduce,
        )
        session_key = _resolve_session_key(document_path)
        return legacy_run_map_reduce(
            text=text,
            length=length,
            focus=focus,
            question=question,
            confirmed=confirmed,
            operation_id=operation_id,
            structure=structure,
            document_path=document_path,
            workspace_root=workspace_root,
            chunks=chunks,
            tree=insp.structure,
            batches=batches,
            insp_chars_in=insp.chars_in,
            insp_strategy=insp.strategy,
            insp_estimated_llm_calls=insp.estimated_llm_calls,
            insp_context_batches=insp.context_batches,
            make_operation_id_fn=make_operation_id,
            get_chunking_config_fn=globals()["get_chunking_config"],
            get_execution_config_fn=globals()["get_execution_config"],
            build_token_budget_fn=_build_token_budget,
            session_key=session_key,
            existing_manifest=existing_manifest,
        )



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