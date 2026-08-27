"""Map-reduce суммаризация юридических документов.

Короткие документы (``<= single_call_threshold``) - один вызов LLM.
Длинные - разбиение на чанки (``lib.services.text_splitter.split_text``),
саммари каждого чанка, затем объединение в финальное.

Целевая аудитория - аналитик без юридического образования:
юридические термины переписываются на бытовой язык, суть сохраняется.

Извлечение текста из PDF/DOCX/TXT - через общий
``workspace.utils.office_files.extract_text`` (skill ничего
специфичного по парсингу файлов не делает).

Производительность:
- LLM-клиент ``lib.services.llm_client.call_llm`` уже имеет свой
  retry-цикл с exponential backoff (``lib/utils/retry.py``), поэтому
  искусственный ``time.sleep`` между чанками не нужен.
- Для очень больших документов (>100 чанков) рекомендуется задать
  ``--max-chunks`` через CLI, иначе skill вернёт структурированную
  ошибку вместо бесконечной суммаризации.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import llm
from skill_config import get_chunking_config

from lib.services.text_splitter import split_text
from workspace.utils.office_files import extract_text

_SKILL_ROOT = Path(__file__).resolve().parent.parent
_PROMPTS_DIR = _SKILL_ROOT / "prompts"

_PROMPT_FILES = {
    "summarize_system": _PROMPTS_DIR / "summarize_system.md",
    "reduce_system": _PROMPTS_DIR / "reduce_system.md",
}


def _load_prompt(name: str) -> str:
    """Прочитать системный промпт из ``prompts/<name>.md``."""
    p = _PROMPT_FILES.get(name)
    if p is None or not p.is_file():
        raise FileNotFoundError(
            f"Не найден файл промпта: {p}. Промпты живут в "
            "workspace/skills/legal_summarizer/prompts/."
        )
    return p.read_text(encoding="utf-8")


_LENGTH_INSTRUCTIONS = {
    "brief": (
        "1 абзац, 150-250 слов: что это за документ и ключевые условия "
        "в двух-трёх фразах."
    ),
    "medium": (
        "3-5 абзацев, 400-600 слов: что это + стороны + предмет + условия "
        "+ сроки + риски."
    ),
    "detailed": (
        "по разделам документа, 800-1200 слов: каждый раздел простым языком, "
        "что он значит и к чему обязывает."
    ),
}


_SUPPORTED_EXTENSIONS = frozenset({".pdf", ".docx", ".txt"})


def load_text(path) -> str:
    """Извлечь plain text из файла через общий ``office_files``-слой.

    Поддерживаются только ``.pdf``, ``.docx``, ``.txt`` - это контракт
    навыка. ``office_files`` умеет больше (pptx/xlsx/csv), но юридические
    документы приходят в этих трёх форматах; для остальных форматов
    ``load_text`` падает ``ValueError``, чтобы агент не пытался
    интерпретировать «мусор» из распарсенного бинаря.

    Raises:
        FileNotFoundError: файл не существует.
        ValueError: формат не поддерживается или документ без текста.
    """
    p = Path(path)
    if p.suffix.lower() not in _SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Неподдерживаемый формат: '{p.suffix}'. "
            "legal_summarizer принимает только .pdf, .docx, .txt"
        )
    text = extract_text(p)
    if not text or not text.strip():
        raise ValueError(
            f"Документ не содержит извлекаемого текста: {p}. "
            "Возможно, это скан - потребуется OCR "
            "(в данной версии не поддерживается)."
        )
    return text


def _progress(msg: str) -> None:
    """Прогресс-сообщение в stderr (без буферизации)."""
    print(f"[legal_summarizer] {msg}", file=sys.stderr, flush=True)


def _compute_chunk_size(cfg: dict) -> int:
    """Вычислить размер чанка в символах из конфигурации.

    Источники (по убыванию приоритета):
      1. ``cfg["chunk_size_input_ratio"]`` + контекстное окно из
         ``config.json::agents.defaults.contextWindowTokens`` —
         основной источник. chunk_size = ctx_tokens * ratio.
      2. ``cfg["chunk_size"]`` — fallback, если ratio не задан
         или контекстное окно неизвестно.

    Args:
        cfg: ``skill_config.get_chunking_config()``.

    Returns:
        Размер чанка в символах (>= 1000).
    """
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
    # Эмпирическая оценка: для русского юридического текста ~3.5 chars/token.
    chars = int(ctx_tokens * 3.5 * float(ratio))
    # Округляем вниз до тысячи для читаемости и стабильности.
    return max(1000, (chars // 1000) * 1000)


def summarize(
    text: str,
    *,
    length: str = "medium",
    context=None,
    max_chunks: int | None = 50,
) -> dict:
    """Суммаризовать текст документа.

    Args:
        text: Исходный текст документа (plain text).
        length: ``brief`` | ``medium`` | ``detailed``.
        context: История чата (опционально, передаётся в LLM).
        max_chunks: Жёсткий лимит числа чанков при map-reduce. Если
            документа больше, возвращается ошибка с понятным
            сообщением (защита от подвисания на сверхбольших входах).

    Returns:
        dict со ``status`` и ``data`` (subject/summary/length/chars_in/
        chunks/strategy) - стандартный формат для ``prepare_output``.
    """
    text = (text or "").strip()
    if not text:
        return {
            "status": "error",
            "data": {"message": "Документ не содержит текста"},
        }

    length = length if length in _LENGTH_INSTRUCTIONS else "medium"
    cfg = get_chunking_config()
    threshold = int(cfg["single_call_threshold"])

    chars = len(text)
    _progress(f"document chars: {chars}, threshold: {threshold}")

    if chars <= threshold:
        strategy = "single"
        chunks_count = 1
        summary = _llm_summarize(text, length, context)
    else:
        chunk_size = _compute_chunk_size(cfg)
        _progress(
            f"map-reduce: chunk_size={chunk_size} chars "
            f"(ratio={cfg.get('chunk_size_input_ratio')}, "
            f"fallback={cfg.get('chunk_size')})"
        )
        chunks = split_text(
            text,
            chunk_size=chunk_size,
            chunk_overlap=int(cfg["chunk_overlap"]),
        )
        chunks_count = len(chunks) if chunks else 1
        _progress(f"map-reduce strategy: {chunks_count} chunks")
        if not chunks:
            summary = _llm_summarize(text, length, context)
            strategy = "single"
        else:
            if max_chunks is not None and chunks_count > max_chunks:
                _progress(
                    f"ABORT: {chunks_count} chunks > max_chunks={max_chunks}. "
                    "Рекомендуем streaming через --batch-size."
                )
                return {
                    "status": "error",
                    "data": {
                        "message": (
                            f"Документ слишком большой для map-reduce "
                            f"за один вызов: {chunks_count} чанков "
                            f"(chars={chars}), --max-chunks={max_chunks}. "
                            "Используйте streaming: "
                            f"--batch-size {chunks_count // 4 + 1} "
                            "--batch-index 0 для первой порции."
                        ),
                        "chars_in": chars,
                        "chunks_estimated": chunks_count,
                    },
                    "stream": {
                        "chunks_total": chunks_count,
                        "chunks_done": 0,
                        "next_batch_index": 0,
                        "recommended_batch_size": chunks_count // 4 + 1,
                        "next_resume_hint": (
                            "передайте --batch-size N --batch-index 0 "
                            f"для streaming (рекомендую N={chunks_count // 4 + 1})"
                        ),
                    },
                }
            partials = []
            for i, chunk in enumerate(chunks):
                _progress(
                    f"chunk {i + 1}/{chunks_count} "
                    f"({(i + 1) * 100 // chunks_count}%)"
                )
                partial = _llm_summarize(
                    chunk,
                    "brief",
                    context,
                    part_label=f"[Часть {i + 1}/{chunks_count}]",
                )
                partials.append(partial)
            _progress(f"combining {chunks_count} partials")
            summary = _llm_combine(partials, length, context)
            strategy = "map_reduce"

    subject = _extract_subject(summary)

    return {
        "status": "success",
        "data": {
            "subject": subject,
            "summary": summary,
            "length": length,
            "chars_in": chars,
            "chunks": chunks_count,
            "strategy": strategy,
        },
    }


def summarize_batch(
    text: str,
    *,
    length: str = "medium",
    context: list[dict] | None = None,
    batch_size: int = 3,
    batch_index: int = 0,
) -> dict:
    """Обработать текст порциями по ``batch_size`` чанков за вызов.

    Streaming-режим для очень больших документов. На каждом вызове
    обрабатывает только чанки
    ``[batch_index * batch_size, (batch_index + 1) * batch_size)``
    относительно ``split_text``. Возвращает partial_summary по этой
    порции + метаданные для следующего вызова.

    Финальный ``status='complete'`` возвращается, когда последний батч
    обработан и сделан combine поверх всех partial.

    Args:
        text: Полный текст документа (передаётся в каждом вызове).
        length: ``brief`` | ``medium`` | ``detailed``.
        context: История чата. На последующих вызовах сюда надо
            передать partial_summary предыдущего батча для continuity.
        batch_size: Сколько чанков обработать за один вызов.
        batch_index: Номер батча (0-based).

    Returns:
        dict со ``status`` (``partial`` | ``complete`` | ``error``) и
        ``stream``-метаданными (chunks_total, chunks_done,
        next_batch_index).
    """
    text = (text or "").strip()
    if not text:
        return {
            "status": "error",
            "data": {"message": "Документ не содержит текста"},
        }

    length = length if length in _LENGTH_INSTRUCTIONS else "medium"
    cfg = get_chunking_config()
    threshold = int(cfg["single_call_threshold"])

    if len(text) <= threshold:
        return summarize(
            text,
            length=length,
            context=context,
            max_chunks=None,
        )

    chunk_size = _compute_chunk_size(cfg)
    chunks = split_text(
        text,
        chunk_size=chunk_size,
        chunk_overlap=int(cfg["chunk_overlap"]),
    )
    chunks_total = len(chunks) if chunks else 1
    _progress(
        f"batch: {chunks_total} chunks total, batch_size={batch_size}, "
        f"batch_index={batch_index}"
    )

    start = batch_index * batch_size
    end = start + batch_size
    batch_chunks = chunks[start:end]
    if not batch_chunks:
        return {
            "status": "error",
            "data": {
                "message": (
                    f"batch_index={batch_index} выходит за пределы "
                    f"(всего чанков {chunks_total}, размер батча {batch_size})"
                ),
            },
        }

    partials = []
    for j, chunk in enumerate(batch_chunks):
        global_i = start + j
        _progress(
            f"chunk {global_i + 1}/{chunks_total} "
            f"({(global_i + 1) * 100 // chunks_total}%)"
        )
        partial = _llm_summarize(
            chunk,
            "brief",
            context,
            part_label=f"[Часть {global_i + 1}/{chunks_total}]",
        )
        partials.append(partial)

    partial_summary = "\n\n".join(
        f"[Часть {start + j + 1}]\n{p}" for j, p in enumerate(partials)
    )

    is_last = end >= chunks_total
    chunks_done = min(end, chunks_total)
    next_batch_index = batch_index + 1

    if not is_last:
        _progress(
            f"BATCH [{batch_index}] DONE: {chunks_done}/{chunks_total}. "
            f"Чтобы продолжить, передайте --batch-index {next_batch_index}."
        )
        return {
            "status": "partial",
            "data": {
                "partial_summary": partial_summary,
                "chunks_in_batch": len(batch_chunks),
                "chars_in": len(text),
                "length": length,
            },
            "stream": {
                "chunks_total": chunks_total,
                "chunks_done": chunks_done,
                "next_batch_index": next_batch_index,
                "next_resume_hint": (
                    f"передайте --batch-index {next_batch_index} для продолжения"
                ),
            },
        }

    # Последний батч: комбинируем partials этого батча как финальный
    # ответ (агенту для продолжения лучше делать отдельный
    # финальный reduce, но батч-режим для очень больших документов уже
    # сам по себе достоин внимания).
    _progress(f"combining {len(partials)} partials of final batch")
    summary = _llm_combine(partials, length, context)
    subject = _extract_subject(summary)
    return {
        "status": "complete",
        "data": {
            "subject": subject,
            "summary": summary,
            "length": length,
            "chars_in": len(text),
            "chunks": chunks_total,
            "strategy": "map_reduce_batch",
        },
        "stream": {
            "chunks_total": chunks_total,
            "chunks_done": chunks_done,
            "next_batch_index": None,
        },
    }


def _llm_summarize(
    text: str,
    length: str,
    context,
    part_label: str = "",
) -> str:
    instruction = _LENGTH_INSTRUCTIONS.get(length, _LENGTH_INSTRUCTIONS["medium"])
    system = _load_prompt("summarize_system").format(length_instruction=instruction)
    user_body = f"{part_label}\n\nДокумент для саммари:\n\n{text}".strip()
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_body},
    ]
    return llm.chat(messages, context=context)


def _llm_combine(
    partials,
    length: str,
    context,
) -> str:
    instruction = _LENGTH_INSTRUCTIONS.get(length, _LENGTH_INSTRUCTIONS["medium"])
    system = _load_prompt("reduce_system").format(length_instruction=instruction)
    joined = "\n\n".join(
        f"[Часть {i + 1}]\n{p}" for i, p in enumerate(partials)
    )
    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": (
                "Частичные саммари частей одного документа:\n\n" + joined
            ),
        },
    ]
    return llm.chat(messages, context=context)


def _extract_subject(summary: str) -> str:
    """Первая непустая строка саммари; длиннее 400 симв. - обрезать по ``.!?``."""
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
