"""Map-reduce суммаризация юридических документов.

Короткие документы (``<= single_call_threshold``) — один вызов LLM.
Длинные — разбиение на чанки (``lib.services.text_splitter.split_text``),
саммари каждого чанка, затем объединение в финальное.

Целевая аудитория — аналитик без юридического образования:
юридические термины переписываются на бытовой язык, суть сохраняется.

Извлечение текста из PDF/DOCX/TXT — через общий
``workspace.utils.office_files.extract_text`` (skill ничего
специфичного по парсингу файлов не делает).
"""

from __future__ import annotations

import re
import time
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
            f"workspace/skills/legal_summarizer/prompts/."
        )
    return p.read_text(encoding="utf-8")


_LENGTH_INSTRUCTIONS = {
    "brief": (
        "1 абзац, 150–250 слов: что это за документ и ключевые условия "
        "в двух-трёх фразах."
    ),
    "medium": (
        "3–5 абзацев, 400–600 слов: что это + стороны + предмет + условия "
        "+ сроки + риски."
    ),
    "detailed": (
        "по разделам документа, 800–1200 слов: каждый раздел простым языком, "
        "что он значит и к чему обязывает."
    ),
}


_SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({".pdf", ".docx", ".txt"})


def load_text(path: str | Path) -> str:
    """Извлечь plain text из файла через общий ``office_files``-слой.

    Поддерживаются только ``.pdf``, ``.docx``, ``.txt`` — это контракт
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
            "Возможно, это скан — потребуется OCR "
            "(в данной версии не поддерживается)."
        )
    return text


def summarize(
    text: str,
    *,
    length: str = "medium",
    context: list[dict] | None = None,
) -> dict:
    """Суммаризовать текст документа.

    Args:
        text: Исходный текст документа (plain text).
        length: ``brief`` | ``medium`` | ``detailed``.
        context: История чата (опционально, передаётся в LLM).

    Returns:
        dict со ``status`` и ``data`` (subject/summary/length/chars_in/
        chunks/strategy) — стандартный формат для ``prepare_output``.
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
        strategy = "single"
        chunks_count = 1
        summary = _llm_summarize(text, length, context)
    else:
        chunks = split_text(
            text,
            chunk_size=int(cfg["chunk_size"]),
            chunk_overlap=int(cfg["chunk_overlap"]),
        )
        chunks_count = len(chunks) if chunks else 1
        if not chunks:
            summary = _llm_summarize(text, length, context)
            strategy = "single"
        else:
            partials: list[str] = []
            for i, chunk in enumerate(chunks):
                time.sleep(2)
                partial = _llm_summarize(
                    chunk,
                    "brief",
                    context,
                    part_label=f"[Часть {i + 1}/{chunks_count}]",
                )
                partials.append(partial)
            summary = _llm_combine(partials, length, context)
            strategy = "map_reduce"

    subject = _extract_subject(summary)

    return {
        "status": "success",
        "data": {
            "subject": subject,
            "summary": summary,
            "length": length,
            "chars_in": len(text),
            "chunks": chunks_count,
            "strategy": strategy,
        },
    }


def _llm_summarize(
    text: str,
    length: str,
    context: list[dict] | None,
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
    partials: list[str],
    length: str,
    context: list[dict] | None,
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
    """Первая непустая строка саммари; длиннее 400 симв. — обрезать по ``.!?``."""
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
