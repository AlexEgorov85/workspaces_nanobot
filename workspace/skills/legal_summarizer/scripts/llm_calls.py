"""LLM-call wrappers: один вызов на одну задачу (map batch / section reduce /
section trim / document reduce), плюс формирование ``doc_context``.

NOTE: модуль НЕ называется ``llm.py``, чтобы не конфликтовать с
существующим ``scripts/llm.py`` (LLM-клиент).
"""
from __future__ import annotations

import llm

from workspace.skills.legal_summarizer.scripts.prompts import (
    build_batch_user_message,
    parse_batch_response,
)
from workspace.skills.legal_summarizer.scripts.prompts_runtime import (
    load_prompt,
    system_instruction,
)
from workspace.skills.legal_summarizer.scripts.packing import ContextBatch


def doc_context(structure: dict | None, *, with_begin_end: bool = False) -> str:
    """Сформировать doc-context (title + опционально begin/end) для user_body."""
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


def llm_batch(
    batch: ContextBatch,
    *,
    chunks_total: int,
    structure: dict | None,
    length: str,
    question: str | None = None,
) -> dict[str, str]:
    """Вызвать LLM для одного ContextBatch. Возвращает dict[chunk_id, summary]."""
    system = load_prompt("summarize_system").replace(
        "{length_instruction}", system_instruction(length, question)
    )
    user_body = build_batch_user_message(batch, chunks_total=chunks_total)
    dctx = doc_context(structure, with_begin_end=False)
    if dctx:
        user_body = dctx + "\n\n" + user_body
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_body},
    ]
    response = llm.chat(messages, context=None)
    return parse_batch_response(batch, response)


def llm_section_reduce(
    section_path: str,
    section_heading: str,
    joined_text: str,
    *,
    length: str,
    question: str | None = None,
) -> str:
    """Per-section reduce: объединить partials чанков раздела."""
    system = load_prompt("section_reduce_system").replace(
        "{length_instruction}", system_instruction(length, question)
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


def llm_document_reduce(
    section_summaries_text: str,
    *,
    length: str,
    focus: str | None,
    structure: dict | None,
    question: str | None = None,
) -> str:
    """Document-level reduce: объединить section_summaries в финальное саммари."""
    system = load_prompt("reduce_system").replace(
        "{length_instruction}", system_instruction(length, question)
    )
    dctx = doc_context(structure, with_begin_end=True)
    user_body = "Саммари разделов документа:\n\n" + section_summaries_text
    if dctx:
        user_body = dctx + "\n\n" + user_body
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


__all__ = [
    "doc_context",
    "llm_batch",
    "llm_section_reduce",
    "llm_document_reduce",
]
