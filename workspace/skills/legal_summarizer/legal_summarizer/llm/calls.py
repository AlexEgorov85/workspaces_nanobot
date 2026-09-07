"""LLM-call wrappers: низкоуровневые обёртки для LLM (map batch / section reduce /
document reduce), плюс утилитарная ``doc_context``.

NOTE: legacy импорт ``ContextBatch`` удалён. ``llm_batch``
теперь принимает ``list[Chunk]`` (canonical-compatible signature).

Single LLM boundary: ``chat_locked`` — единая обёртка для всех
``llm.chat(...)`` вызовов в этом модуле, сериализующая их через
``LLM_FLIGHT_LOCK`` из ``llm.single_flight``. Это покрывает
single-flight invariant для map / section reduce / document reduce.
"""
from __future__ import annotations

from typing import Iterable

from legal_summarizer.llm import client as llm
from legal_summarizer.chunking.chunks import Chunk
from legal_summarizer.document.structure import DocumentStructure
from legal_summarizer.llm.prompts import (
    build_batch_user_message,
    parse_batch_response,
)
from legal_summarizer.llm.prompts_runtime import (
    load_prompt,
    system_instruction,
)
from legal_summarizer.llm.single_flight import LLM_FLIGHT_LOCK


# Back-compat: старые импорты ``from legal_summarizer.llm.calls import _CHAT_LOCK``
# продолжают работать (используется в ``test_lock_finally_releases``).
_CHAT_LOCK = LLM_FLIGHT_LOCK


def chat_locked(messages, *, context=None) -> str:
    """Сериализованный ``llm.chat`` через единый lock."""
    with LLM_FLIGHT_LOCK:
        return llm.chat(messages, context=context)


def doc_context(
    structure: DocumentStructure | None,
    *,
    with_begin_end: bool = False,
) -> str:
    """Сформировать doc-context (title + опционально begin/end) для user_body.

    Принимает canonical ``DocumentStructure`` (а не legacy dict). Параметр
    ``with_begin_end`` оставлен для back-compat с вызывающими, но
    сейчас всегда возвращает пустой ``begin``/``end`` — эти поля
    удалены вместе с legacy ``extract_structure()`` (см.
    ``document/physical.py``).
    """
    if structure is None:
        return ""
    parts: list[str] = []
    if structure.title is not None:
        title = (structure.title.value or "").strip()
        if title:
            parts.append(f"НАЗВАНИЕ ДОКУМЕНТА: {title}")
    if with_begin_end:
        pass
    return "\n\n".join(parts)


def llm_batch(
    chunks: Iterable[Chunk],
    *,
    chunks_total: int,
    structure: DocumentStructure | None,
    length: str,
    question: str | None = None,
) -> dict[str, str]:
    """Сгруппировать LLM вызов для батча Chunk'ов.

    Возвращает dict[chunk_id, summary].

    Note on single-flight: lock берётся **на уровне выше** —
    ``execution.pipeline.process_context_batch`` для map-phase
    (внутри ``asyncio.to_thread``) или через ``chat_locked()`` для
    прямого вызова. Этот модуль не берёт lock повторно — иначе
    был бы deadlock при вызове из-под lock'а.
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
    response = llm.chat(messages, context=None)
    return parse_batch_response(chunks_list, response)


def llm_section_reduce(
    section_path: str,
    section_heading: str,
    joined_text: str,
    *,
    length: str,
    question: str | None = None,
) -> str:
    """Per-section reduce: объединить partials в финальную section_summary.

    Single-flight: lock берётся в ``application.execution_orchestration``
    или в ``execution.direct`` — не здесь.
    """
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
    return llm.chat(messages, context=None)


def llm_document_reduce(
    section_summaries_text: str,
    *,
    length: str,
    focus: str | None,
    structure: DocumentStructure | None,
    question: str | None = None,
) -> str:
    """Document-level reduce: объединить section_summaries в финальный документ.

    Single-flight: lock берётся в ``application.execution_orchestration``
    или в ``execution.direct`` — не здесь.
    """
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
    return llm.chat(messages, context=None)


__all__ = [
    "doc_context",
    "llm_batch",
    "llm_section_reduce",
    "llm_document_reduce",
    "chat_locked",
]
