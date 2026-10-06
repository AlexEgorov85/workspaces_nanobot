"""LLM-call wrappers: низкоуровневые обёртки для LLM (map batch / section reduce /
document reduce), плюс утилитарная ``doc_context``.

NOTE: legacy импорт ``ContextBatch`` удалён. ``llm_batch``
теперь принимает ``list[Chunk]`` (canonical-compatible signature).

Single LLM boundary: каждый вызов ``llm.chat`` в этом модуле
проходит через ``guarded_chat`` из ``llm.single_flight``. Это **единственный
путь к LLM** в runtime — ``execution.pipeline`` НЕ импортирует
``threading.Lock`` / ``LLM_FLIGHT_LOCK`` напрямую (см. архитектурный
контракт ``execution не знает о llm``).
"""
from __future__ import annotations

from collections.abc import Iterable

from libs.legal_summarizer.chunking.chunks import Chunk
from libs.legal_summarizer.document.structure import DocumentStructure
from libs.legal_summarizer.llm import client as llm
from libs.legal_summarizer.llm.config import get_llm_config
from libs.legal_summarizer.llm.prompts import (
    build_batch_user_message,
    parse_batch_response,
)
from libs.legal_summarizer.llm.prompts_runtime import (
    load_prompt,
    system_instruction,
)
from libs.legal_summarizer.llm.single_flight import (
    guarded_chat,
)


def chat_locked(messages, *, context=None) -> str:
    """Сериализованный ``llm.chat`` через единый ``guarded_chat`` API.

    Back-compat public API: старый код вызывает ``chat_locked(...)``
    для manual LLM-вызовов. Реализация делегирует в ``guarded_chat``.
    """
    return guarded_chat(llm.chat, messages, context=context)


def _reduce_max_tokens(nominal: int) -> int:
    """Потолок ответа reduce-вызова: номинал + резерв под рассуждение.

    Номинал — это потолок на **содержимое** ответа (свод раздела, финальный
    свод). У рассуждающей модели рассуждение расходует тот же бюджет ответа:
    при номинале 2000 токенов модель могла исчерпать его целиком на
    ``<think>`` и не оставить ни одного символа свода. Тогда домен получал
    пустую строку и объявлял ``REDUCE_INPUT_EMPTY`` — «свод действительно
    пуст», то есть неповторяемый отказ для документа, который через минуту
    разбирался без всяких правок.

    Резерв объявлен настройкой домена (``llm.reasoning_reserve_tokens``) и
    ограничен потолком платформы (``llm.max_tokens``): превышать то, что
    провайдер вообще готов отдать, смысла нет.
    """
    declared = get_llm_config()
    reserve = int(declared.get("reasoning_reserve_tokens", 0) or 0)
    ceiling = int(declared.get("max_tokens", 0) or 0)
    value = nominal + max(reserve, 0)
    if ceiling > 0:
        value = min(value, ceiling)
    return value


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

    Single-flight: lock берётся **внешним** кодом —
    ``execution.pipeline.process_context_batch`` (map phase) или
    ``chat_locked()`` для прямого вызова. Этот модуль НЕ берёт lock —
    иначе был бы deadlock при вызове из-под lock'а.
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

    Single-flight: см. ``llm_batch``.

    ``max_tokens``: потолок на **содержимое** ответа (~4000 chars ≈ 1100
    tokens) плюс резерв под рассуждение модели — см. ``_reduce_max_tokens``.
    Раньше стоял голый номинал 2000, и у рассуждающей модели он уходил на
    ``<think>`` целиком: контента не оставалось, свод не состоялся. Замер
    до override: avg 30-60 сек на section_reduce. Латентность выросла, и это
    цена ответа, который вообще существует.

    Безопасно: каждый вызов возвращает ровно одну section_summary,
    ``strip_think_blocks + fit_input`` обрежут в caller'е до
    ``_SECTION_SUMMARY_MAX_CHARS`` (12000 chars).
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
    return llm.chat(messages, context=None, max_tokens=_reduce_max_tokens(2000))


def llm_document_reduce(
    section_summaries_text: str,
    *,
    length: str,
    focus: str | None,
    structure: DocumentStructure | None,
    question: str | None = None,
) -> str:
    """Document-level reduce: объединить section_summaries в финальный документ.

    Single-flight: см. ``llm_batch``.

    ``max_tokens``: потолок на **содержимое** финального саммари (~8000 chars
    ≈ 2300 tokens) плюс резерв под рассуждение модели — см.
    ``_reduce_max_tokens``. Раньше стоял голый номинал 3000: дефолтные 8192
    замедляли LLM в 2-3 раза на длинных output'ах (trace detailed-прогона:
    doc_reduce с input 16-26K → output 7-10K → 30-100 сек на вызов), но под
    рассуждающей моделью 3000 уходило на рассуждение, и свод не состоялся.

    Безопасно: каждый вызов возвращает ровно один финальный summary.
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
    return llm.chat(messages, context=None, max_tokens=_reduce_max_tokens(3000))


__all__ = [
    "doc_context",
    "llm_batch",
    "llm_section_reduce",
    "llm_document_reduce",
    "chat_locked",
]
