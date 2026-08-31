"""Выбор chunks для brief и question режимов legal_summarizer.

Используется только при strategy='map_reduce'. Для коротких документов
(стратегия 'single') все chunks и так уходят в один LLM-вызов.
"""
from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

_MAX_CHUNKS_DEFAULT = 8


def _keywords(text: str) -> set[str]:
    """Слова длиной >=3 из текста, нижний регистр."""
    return {w.lower() for w in _TOKEN_RE.findall(text) if len(w) >= 3}


def select_brief_chunks(chunks: list, *, max_chunks: int = _MAX_CHUNKS_DEFAULT) -> list:
    """Brief: первые N chunks в document order."""
    if max_chunks <= 0 or len(chunks) <= max_chunks:
        return list(chunks)
    return list(chunks[:max_chunks])


def select_relevant_chunks(
    question: str,
    chunks: list,
    *,
    max_chunks: int = _MAX_CHUNKS_DEFAULT,
) -> list | None:
    """Question: chunks, содержащие хотя бы одно слово из вопроса.

    Args:
        question: текст вопроса от пользователя.
        chunks: список chunks (с полем ``.text``).
        max_chunks: верхняя граница выборки.

    Returns:
        Список chunks (порядок document order, не более ``max_chunks``)
        или ``None``, если ничего не нашли — caller должен fallback на
        detailed-режим (читать всё).
    """
    kws = _keywords(question)
    if not kws:
        return None
    matched: list = []
    for c in chunks:
        text_lower = c.text.lower()
        if any(kw in text_lower for kw in kws):
            matched.append(c)
            if len(matched) >= max_chunks:
                break
    return matched if matched else None
