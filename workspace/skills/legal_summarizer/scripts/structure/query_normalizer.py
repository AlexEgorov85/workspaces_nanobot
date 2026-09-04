"""Query normalization (PLAN §34, Этап 34).

Отдельный модуль для query normalization (PLAN §34):

* case (lowercase);
* punctuation;
* whitespace;
* stopwords;
* legal aliases (если применимо).

Не использует LLM — детерминированный (PLAN §61).

Сейчас функция ``tokenize`` уже реализована в ``retrieval.py``. Этот
модуль выносит её в собственный файл + добавляет ``normalize_query``
(более широкий API).
"""

from __future__ import annotations

import re
import unicodedata

from workspace.skills.legal_summarizer.scripts.structure.retrieval import (
    _RUSSIAN_STOPWORDS,
    _WORD_RE,
)


_LEGAL_ALIASES = {
    "штраф": ("неустойка", "пени", "penalty"),
    "оплата": ("платёж", "расчёт", "payment"),
    "срок": ("период", "deadline", "term"),
    "цена": ("стоимость", "price", "cost"),
}


def normalize_query(query: str) -> str:
    """Нормализовать query: lowercase, strip punctuation, collapse whitespace.

    PLAN §34. Не выбрасывает стоп-слова (это делает tokenize).
    """
    if not query:
        return ""
    normalized = unicodedata.normalize("NFKC", query).lower()
    normalized = re.sub(r"\s+", " ", normalized).strip()
    normalized = re.sub(r"[^\w\s]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def expand_with_aliases(text: str) -> list[str]:
    """Вернуть оригинал + все legal aliases (PLAN §34).

    Используется, если downstream хочет матчить как по query, так и
    по юридическим синонимам.

    Пример: ``"штраф"`` → ``["штраф", "неустойка", "пени", "penalty"]``.
    """
    if not text:
        return []
    tokens = set(_WORD_RE.findall(text.lower()))
    expanded: list[str] = []
    for token in tokens:
        if token in _LEGAL_ALIASES:
            expanded.extend([token] + list(_LEGAL_ALIASES[token]))
        else:
            expanded.append(token)
    return expanded


def tokenize_normalized(query: str) -> list[str]:
    """Удобный API: normalize → tokenize → drop stopwords."""
    if not query:
        return []
    normalized = normalize_query(query)
    tokens = _WORD_RE.findall(normalized)
    return [t for t in tokens if t and t not in _RUSSIAN_STOPWORDS]


__all__ = [
    "normalize_query",
    "expand_with_aliases",
    "tokenize_normalized",
]