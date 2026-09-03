"""Cached candidate selection для follow-up вопросов.

Контракт:

* Выбор (selection) и реконструкция (reconstruction) — разные обязанности.
  Этот модуль **не читает исходный документ** — только ``doc_cache``.

* Алгоритм: lexical match по meaningful terms (≥4 chars, lower).
  Matched text = ``chunk_text_preview`` + ``summary``. Поддержка
  prefix-match уже есть в :func:`workspace.skills.legal_summarizer.scripts.summarizer._relaxed_lexical_fallback`
  — здесь применяем тот же подход для устойчивости к словоформам.

* Score: сумма matched terms (без двойного счёта одинаковых слов в
  preview и summary).

* Confidence threshold:
    - ``min_score`` — минимальный общий score (default 2).
    - ``min_top_score`` — минимальный score лучшего кандидата (default 3).
    - Если ничего не набрало нужное — возвращаем ``None``, caller
      fallback на existing retrieval.

* Сортировка: ``score DESC, document_order ASC`` (тот же порядок, что
  в вопрос-режиме сегодня — back-compat).

* Возвращает список ``CachedCandidate`` (dict-подобные dataclass'ы с
  provenance и метаданными) — downstream превращает их в chunks с
  :func:`reconstruct_source_fragment`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from typing import Any


# Слова ≥4 букв (latin/cyrillic) — те же слова не учитываем дважды.
_WORD_RE = re.compile(r"\w{4,}", re.UNICODE)


def _meaningful_terms(text: str) -> list[str]:
    """Список meaningful terms (≥4 chars, lower)."""
    return [w.lower() for w in _WORD_RE.findall(text or "")]


@dataclass(frozen=True)
class CachedCandidate:
    """Один кандидат из document-cache для follow-up retrieval.

    Attributes:
        chunk_id: ``"001"``/``"002"``/... (zero-padded width=3).
        score: число матчей meaningful terms (по preview+summary).
        summary: оригинальный LLM summary chunk'а.
        section_id, section_path, page_start, page_end: navigation meta.
        block_indices, block_types: provenance.
        source_char_start, source_char_end: offsets.
        table_id, table_row_start, table_row_end: table provenance.
        chunk_text_preview: ограниченный preview (≤500 chars).
    """

    chunk_id: str
    score: int
    summary: str
    section_id: str | None
    section_path: str | None
    page_start: int | None
    page_end: int | None
    block_indices: tuple[int, ...]
    block_types: tuple[str, ...]
    source_char_start: int | None
    source_char_end: int | None
    table_id: str | None
    table_row_start: int | None
    table_row_end: int | None
    chunk_text_preview: str
    matched_terms: tuple[str, ...] = field(default_factory=tuple)


def _score_record(question_terms: list[str], record: dict[str, Any]) -> tuple[int, set[str]]:
    """Сколько meaningful terms нашлось в record (preview + summary).

    Используем **prefix-match по 4 символам** — устойчиво к словоформам
    русского языка «договор» ↔ «договора» ↔ «договору», но не требует
    embeddings или морфологического словаря. Совпадает с
    :func:`summarizer._relaxed_lexical_fallback`.

    Возвращает ``(score, matched_set)`` для дедупликации.
    """
    if not question_terms:
        return 0, set()
    haystack_parts: list[str] = []
    preview = record.get("chunk_text_preview") or ""
    summary = record.get("summary") or ""
    if preview:
        haystack_parts.append(preview.lower())
    if summary:
        haystack_parts.append(summary.lower())
    if not haystack_parts:
        return 0, set()
    haystack = "\n".join(haystack_parts)
    matched: set[str] = set()
    seen_prefixes: set[str] = set()
    for t in question_terms:
        prefix = t[:4]
        if prefix in seen_prefixes:
            continue
        if prefix in haystack:
            matched.add(t)
            seen_prefixes.add(prefix)
    return len(matched), matched


def select_cached_candidates(
    question: str,
    cache_records: dict[str, dict],
    *,
    max_candidates: int = 5,
    min_score: int = 2,
    min_top_score: int = 3,
) -> list[CachedCandidate] | None:
    """Найти кандидатов в document-cache, подходящих под вопрос.

    Args:
        question: пользовательский follow-up вопрос.
        cache_records: ``{chunk_id: record}`` из :func:`load_doc_cache`.
        max_candidates: верхняя граница выборки (default 5).
        min_score: минимальный score для любого кандидата (default 2).
        min_top_score: минимальный score лучшего кандидата (default 3).

    Returns:
        Список ``CachedCandidate``, отсортированный по
        ``(-score, chunk_id)`` (document order через ``chunk_id``);
        или ``None``, если ничего не набрало нужный score (caller →
        existing lexical/relaxed fallback).

    Notes:
        * Не читает исходный документ (canonical source) — только cache.
        * Backward-compatible: cache без ``chunk_text_preview`` /
          ``block_indices`` получит score на основе ``summary``.
        * Без ``chunk_text_preview``/``summary`` → score=0
          (record не участвует в выборке).
    """
    if not question or not cache_records:
        return None
    q_terms = _meaningful_terms(question)
    if not q_terms:
        return None

    scored: list[tuple[int, str, set[str]]] = []
    for cid, record in cache_records.items():
        score, matched = _score_record(q_terms, record)
        if score >= min_score:
            scored.append((score, cid, matched))

    if not scored:
        return None

    top_score = max(s for s, _, _ in scored)
    if top_score < min_top_score:
        return None

    scored.sort(key=lambda x: (-x[0], x[1]))
    scored = scored[:max_candidates]

    out: list[CachedCandidate] = []
    for score, cid, matched in scored:
        r = cache_records[cid]
        bi_raw = r.get("block_indices") or []
        bt_raw = r.get("block_types") or []
        out.append(CachedCandidate(
            chunk_id=cid,
            score=score,
            summary=str(r.get("summary") or ""),
            section_id=r.get("section_id"),
            section_path=r.get("section_path"),
            page_start=r.get("page_start"),
            page_end=r.get("page_end"),
            block_indices=tuple(int(x) for x in bi_raw),
            block_types=tuple(str(x) for x in bt_raw),
            source_char_start=r.get("source_char_start"),
            source_char_end=r.get("source_char_end"),
            table_id=r.get("table_id"),
            table_row_start=r.get("table_row_start"),
            table_row_end=r.get("table_row_end"),
            chunk_text_preview=str(r.get("chunk_text_preview") or ""),
            matched_terms=tuple(sorted(matched)),
        ))
    return out


def is_confident(candidates: list[CachedCandidate] | None, *, min_score: int = 3) -> bool:
    """Является ли выборка «confident» для использования вместо LLM-getter.

    Если лучший кандидат имеет score < min_score → вызывающий код
    должен fallback на existing retrieval (не делать answering
    по cache preview).
    """
    if not candidates:
        return False
    return candidates[0].score >= min_score


__all__ = [
    "CachedCandidate",
    "select_cached_candidates",
    "is_confident",
]
