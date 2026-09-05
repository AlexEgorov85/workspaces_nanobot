"""LLM-call wrappers: низкоуровневые обёртки для LLM (map batch / section reduce /
document reduce), плюс утилитарная ``doc_context``.

NOTE: legacy импорт ``ContextBatch`` удалён в PLAN §20. ``llm_batch``
теперь принимает ``list[Chunk]`` (canonical-compatible signature).

Single LLM boundary: ``chat_locked`` — единая обёртка для всех
``llm.chat(...)`` вызовов в этом модуле, сериализующая их через
``threading.Lock``. Это покрывает single-flight invariant для
map / section reduce / document reduce внутри одной ``run()``.
"""
from __future__ import annotations

import threading
from typing import Iterable

import llm

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.prompts import (
    build_batch_user_message,
    parse_batch_response,
)
from workspace.skills.legal_summarizer.scripts.prompts_runtime import (
    load_prompt,
    system_instruction,
)


# ---------------------------------------------------------------------------
# Single-flight LLM boundary (intra-process)
# ---------------------------------------------------------------------------
#
# Все вызовы ``llm.chat`` сериализуются через этот lock. В комбинации
# с ``pipeline._LLM_FLIGHT_LOCK`` (cross-thread guard в pipeline.py)
# это даёт гарантию ``max_active_llm_calls == 1`` для всех LLM-вызовов
# во всех точках входа.
_CHAT_LOCK = threading.Lock()


def chat_locked(messages, *, context=None) -> str:
    """Сериализованный ``llm.chat`` через единый lock."""
    with _CHAT_LOCK:
        return llm.chat(messages, context=context)


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
            parts.append("Начало документа:\n" + begin)
        if end:
            parts.append("Конец документа:\n" + end)
    return "\n\n".join(parts)


def llm_batch(
    chunks: Iterable[Chunk],
    *,
    chunks_total: int,
    structure: dict | None,
    length: str,
    question: str | None = None,
) -> dict[str, str]:
    """Сгруппировать LLM вызов для батча Chunk'ов.

    Возвращает dict[chunk_id, summary].

    Args:
        chunks: список Chunk для одного батча.
        chunks_total: общее число chunks в документе.
    """
    chunks_list = list(chunks)
    system = load_prompt("summarize_system").replace(
        "{length_instruction}", system_instruction(length, question)
    )
    user_body = build_batch_user_message(
        chunks_list, chunks_total=chunks_total,
    )
    dctx = doc_context(structure, with_begin_end=False)
    if dctx:
        user_body = dctx + "\n\n" + user_body
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_body},
    ]
    response = chat_locked(messages, context=None)
    return parse_batch_response(chunks_list, response)


def llm_section_reduce(
    section_path: str,
    section_heading: str,
    joined_text: str,
    *,
    length: str,
    question: str | None = None,
) -> str:
    """Per-section reduce: объединить partials в финальную section_summary."""
    system = load_prompt("section_reduce_system").replace(
        "{length_instruction}", system_instruction(length, question)
    )
    user_body = (
        f"Раздел: {section_path}\n"
        f"Заголовок раздела: {section_heading}\n\n"
        f"Объединённые краткие описания частей раздела:\n\n{joined_text}\n\n"
        "Объедини их в одно итоговое описание раздела."
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_body},
    ]
    return chat_locked(messages, context=None)


def llm_document_reduce(
    section_summaries_text: str,
    *,
    length: str,
    focus: str | None,
    structure: dict | None,
    question: str | None = None,
) -> str:
    """Document-level reduce: объединить section_summaries в финальный документ."""
    system = load_prompt("reduce_system").replace(
        "{length_instruction}", system_instruction(length, question)
    )
    dctx = doc_context(structure, with_begin_end=True)
    user_body = "Краткие описания разделов документа:\n\n" + section_summaries_text
    if dctx:
        user_body = dctx + "\n\n" + user_body
    if focus:
        user_body = (
            user_body
            + "\n\nАкцент (если задан фокус внимания читателя): "
            + focus
        )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_body},
    ]
    return chat_locked(messages, context=None)


__all__ = [
    "doc_context",
    "llm_batch",
    "llm_section_reduce",
    "llm_document_reduce",
    "chat_locked",
]