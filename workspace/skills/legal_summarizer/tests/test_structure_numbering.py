"""Тесты для numbering parser (Этап 6 из PLAN.md)."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.numbering import (
    assign_sibling_ordinals,
    parse_numbering,
)
from workspace.skills.legal_summarizer.scripts.structure.models import NumberingInfo


def _ni(**kw):
    base = dict(raw="1", scheme="decimal", components=(1,), level=1, ordinal=None)
    base.update(kw)
    return NumberingInfo(**base)


def test_decimal_simple():
    n = parse_numbering("1. Общие положения")
    assert n is not None
    assert n.scheme == "decimal"
    assert n.components == (1,)
    assert n.level == 1


def test_decimal_nested():
    n = parse_numbering("1.2.3 Пункт подпункта")
    assert n is not None
    assert n.scheme == "decimal"
    assert n.components == (1, 2, 3)
    assert n.level == 3


def test_legal_article():
    n = parse_numbering("Статья 12. Права сторон")
    assert n is not None
    assert n.scheme == "legal_article"
    assert n.components == (12,)
    assert n.level == 1


def test_legal_article_sub():
    n = parse_numbering("Статья 12.1")
    assert n is not None
    assert n.scheme == "legal_article"
    assert n.components == (12, 1)
    assert n.level == 2


def test_legal_chapter():
    n = parse_numbering("Глава 3. Ответственность")
    assert n is not None
    assert n.scheme == "legal_chapter"
    assert n.components == (3,)


def test_legal_section_roman():
    n = parse_numbering("Раздел IV. Заключительные положения")
    assert n is not None
    assert n.scheme == "legal_section_roman"
    assert n.components == (4,)


def test_paragraph_mark():
    n = parse_numbering("§ 5. Конфиденциальность")
    assert n is not None
    assert n.scheme == "paragraph_mark"
    assert n.components == (5,)


def test_legal_clause():
    n = parse_numbering("Пункт 1. Обязанности заказчика")
    assert n is not None
    assert n.scheme == "legal_clause"
    assert n.components == (1,)


def test_cyrillic_alpha():
    n = parse_numbering("а) первое условие")
    assert n is not None
    assert n.scheme == "cyrillic_alpha"
    assert n.components == ("а",)


def test_appendix_digit():
    n = parse_numbering("Приложение 1")
    assert n is not None
    assert n.scheme == "appendix"
    assert n.components == (1,)


def test_appendix_letter():
    n = parse_numbering("Приложение А")
    assert n is not None
    assert n.scheme == "appendix"
    assert n.components == ("А",)


def test_appendix_letter_subnumber():
    n = parse_numbering("Приложение А.1")
    assert n is not None
    assert n.scheme == "appendix"
    assert n.components == ("А", 1)
    assert n.level == 2


def test_no_numbering():
    assert parse_numbering("Просто текст без номера") is None
    assert parse_numbering("") is None
    assert parse_numbering("   ") is None


def test_sibling_ordinals_decimal_flat():
    items = [
        _ni(raw="1", components=(1,)),
        _ni(raw="2", components=(2,)),
        _ni(raw="3", components=(3,)),
    ]
    ordinals = assign_sibling_ordinals(items)
    assert ordinals == [1, 2, 3]


def test_sibling_ordinals_decimal_nested():
    items = [
        _ni(raw="1", components=(1,)),
        _ni(raw="1.1", components=(1, 1), level=2),
        _ni(raw="1.2", components=(1, 2), level=2),
        _ni(raw="2", components=(2,)),
        _ni(raw="2.1", components=(2, 1), level=2),
    ]
    ordinals = assign_sibling_ordinals(items)
    assert ordinals == [1, 1, 2, 1, 1]


def test_sibling_ordinals_resets_per_parent():
    items = [
        _ni(raw="1", components=(1,)),
        _ni(raw="1.1", components=(1, 1), level=2),
        _ni(raw="1.2", components=(1, 2), level=2),
        _ni(raw="2", components=(2,)),
        _ni(raw="2.3", components=(2, 3), level=2),
        _ni(raw="2.4", components=(2, 4), level=2),
    ]
    ordinals = assign_sibling_ordinals(items)
    assert ordinals == [1, 1, 2, 1, 1, 2]


def test_sibling_ordinals_with_none():
    items = [
        _ni(raw="1", components=(1,)),
        None,
        _ni(raw="2", components=(2,)),
    ]
    ordinals = assign_sibling_ordinals(items)
    assert ordinals == [1, None, 1]


def test_sibling_ordinals_empty():
    assert assign_sibling_ordinals([]) == []