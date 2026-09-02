"""Выбор chunks для brief и question режимов legal_summarizer.

Используется только при strategy='map_reduce'. Для коротких документов
(стратегия 'single') все chunks и так уходят в один LLM-вызов.
"""
from __future__ import annotations

import math
import re

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

_MAX_CHUNKS_DEFAULT = 8

# Жёсткий лимит: brief не должен покрывать больше половины документа.
_HALF_COVERAGE_RATIO = 0.5


def _keywords(text: str) -> set[str]:
    """Слова длиной >=3 из текста, нижний регистр."""
    return {w.lower() for w in _TOKEN_RE.findall(text) if len(w) >= 3}


def select_brief_chunks(chunks: list, *, max_chunks: int = _MAX_CHUNKS_DEFAULT) -> list:
    """Brief: первые N chunks в document order.

    Устаревший fallback — оставлен для обратной совместимости и unit-тестов.
    Предпочтительный путь: ``select_brief_chunks_structured`` с SectionTree.
    """
    if max_chunks <= 0 or len(chunks) <= max_chunks:
        return list(chunks)
    return list(chunks[:max_chunks])


def _effective_brief_limit(total: int, max_chunks: int) -> int:
    """Эффективный лимит: min(max_chunks, 50% от total)."""
    if total <= 0:
        return 0
    half_limit = max(1, math.ceil(total * _HALF_COVERAGE_RATIO))
    return max(0, min(max_chunks, half_limit))


def _is_root_only_tree(tree) -> bool:
    """True если в дереве только root секция (нет реальной структуры)."""
    try:
        sections = getattr(tree, "sections", None)
    except Exception:
        return True
    if not sections:
        return True
    root_id = getattr(tree, "root_id", None)
    real = [sid for sid in sections if sid != root_id]
    return len(real) == 0


def select_brief_chunks_structured(
    chunks: list,
    tree,
    *,
    max_chunks: int = _MAX_CHUNKS_DEFAULT,
    coverage_ratio: float = 0.5,
) -> list:
    """Brief: round-robin по секциям для максимального покрытия документа.

    Берём первые N chunks по кругу из chunks каждой секции,
    чтобы краткое саммари покрывало все секции, а не начало одной.

    Лимиты:
      - эффективный лимит = ``min(max_chunks, ceil(total * coverage_ratio))``.
        Brief не обрабатывает больше половины секций (default).
      - если ``tree`` is None или секций только root → fallback на
        ``select_brief_chunks`` (первые N chunks).
      - иначе round-robin по секциям в порядке первой секции в документе,
        потом вторично в document order.

    Args:
        chunks: список chunks (с атрибутом ``.index`` и ``.section_id``).
        tree: ``SectionTree`` или None (если без секций).
        max_chunks: жёсткий лимит (например, 10).
        coverage_ratio: доля chunks для brief (0 < ratio ≤ 1).
            Default 0.5 = ~50% (round-robin).
            Для этапа 20 «structural sampling» можно задать 0.33.

    Returns:
        список chunks в document order, не более effective_limit.
    """
    if not chunks:
        return []
    total = len(chunks)
    # coverage_ratio вынесен в параметр (default 0.5 = старое поведение).
    if not (0.0 < coverage_ratio <= 1.0):
        coverage_ratio = 0.5
    effective_limit = min(max_chunks, math.ceil(total * coverage_ratio))
    if effective_limit == 0:
        return []

    if tree is None or _is_root_only_tree(tree):
        return select_brief_chunks(chunks, max_chunks=effective_limit)

    root_id = getattr(tree, "root_id", None)
    sections_to_chunks: dict[str, list] = {}
    first_seen: dict[str, int] = {}
    for c in chunks:
        sid = getattr(c, "section_id", "") or root_id or "s_root"
        sections_to_chunks.setdefault(sid, []).append(c)
        if sid not in first_seen:
            first_seen[sid] = c.index

    ordered_sids = sorted(
        sections_to_chunks.keys(),
        key=lambda s: first_seen.get(s, 0),
    )

    chosen: list = []
    cursors = {s: 0 for s in ordered_sids}
    while len(chosen) < effective_limit:
        progressed = False
        for sid in ordered_sids:
            if len(chosen) >= effective_limit:
                break
            i = cursors[sid]
            if i < len(sections_to_chunks[sid]):
                chosen.append(sections_to_chunks[sid][i])
                cursors[sid] = i + 1
                progressed = True
        if not progressed:
            break

    return sorted(chosen, key=lambda c: c.index)


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
