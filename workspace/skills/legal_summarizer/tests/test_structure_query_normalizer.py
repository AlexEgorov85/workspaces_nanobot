"""Тесты для query normalizer (Этап 34 из PLAN.md).

Кириллические строки читаются из файла ``cyrillic_literals.py`` —
для обхода проблем cp1251/cp866 в Windows PowerShell.
"""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.query_normalizer import (
    expand_with_aliases, normalize_query, tokenize_normalized,
)
from workspace.skills.legal_summarizer.tests import cyrillic_literals as L


def test_normalize_basic():
    assert normalize_query("  Hello  WORLD!  ") == "hello world"


def test_normalize_strips_punctuation():
    assert normalize_query("Что? Где? Когда?") == "что где когда"


def test_normalize_empty():
    assert normalize_query("") == ""
    assert normalize_query("   ") == ""


def test_normalize_unicode_nfkc():
    assert normalize_query("Café") == "café"


def test_normalize_collapse_whitespace():
    assert normalize_query("a\n\n\tb") == "a b"


def test_expand_with_aliases_legal_terms():
    expanded = expand_with_aliases(L.QUERY_LEGAL)
    assert "штраф" in expanded
    assert "неустойка" in expanded
    assert any("оплат" in t for t in expanded)


def test_expand_with_aliases_no_legal_terms():
    expanded = expand_with_aliases(L.QUERY_PLAIN)
    assert "обычный" in expanded
    assert "вопрос" in expanded


def test_tokenize_normalized_drops_stopwords():
    tokens = tokenize_normalized(L.QUERY_WITH_STOPWORDS)
    assert "штраф" in tokens
    assert any("оплат" in t for t in tokens)
    assert "по" not in tokens
    assert "за" not in tokens


def test_tokenize_normalized_empty():
    assert tokenize_normalized("") == []


def test_tokenize_normalized_punctuation_handling():
    tokens = tokenize_normalized("оплата, штраф!")
    assert "оплата" in tokens
    assert "штраф" in tokens