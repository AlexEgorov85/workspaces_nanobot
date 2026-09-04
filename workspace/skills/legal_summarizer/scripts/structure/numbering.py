"""Numbering parser для headings / list items / captions (PLAN §6, §12).

Цель: **один модуль** для всего numbering detection. Сейчас regex'ы
разбросаны между ``heading.py`` (private ``_RE_NUMBERED_LEVEL_*``) и
``list_detection.py`` (тоже private ``_RE_NUMBERED_LEVEL_*``). После
Этапа 6 они будут импортироваться отсюда.

Поддерживаемые схемы (PLAN §6):

* ``decimal``: ``1.``, ``1.1``, ``1.1.1`` — компоненты числовые.
* ``legal_article``: ``Статья 12``, ``Статья 12.1``.
* ``legal_chapter``: ``Глава 3``.
* ``legal_section_roman``: ``Раздел I``, ``Раздел IV``.
* ``legal_clause``: ``Пункт 1``, ``Пункт 12``.
* ``paragraph_mark``: ``§ 5``, ``§ 12.1``.
* ``cyrillic_alpha``: ``а)``, ``б)``, ``в)``.
* ``appendix``: ``Приложение 1``, ``Приложение А``, ``Приложение А.1``.

Каждая функция возвращает ``NumberingInfo`` или ``None``.
"""

from __future__ import annotations

import re
from typing import Any

from workspace.skills.legal_summarizer.scripts.structure.models import NumberingInfo


_DECIMAL_RE = re.compile(r"^\s*(\d+(?:\.\d+)+|\d+)\.?\s+(.{2,200})$")
_LEGAL_ARTICLE_RE = re.compile(
    r"^\s*Статья\s+(\d+(?:\.\d+)+|\d+)\s*\.?\s*(.*)$", re.IGNORECASE
)
_LEGAL_CHAPTER_RE = re.compile(
    r"^\s*Глава\s+(\d+(?:\.\d+)+|\d+)\s*\.?\s*(.*)$", re.IGNORECASE
)
_LEGAL_SECTION_ROMAN_RE = re.compile(
    r"^\s*Раздел\s+([IVX]{1,5})\b\s*(.{2,200})$", re.IGNORECASE
)
_LEGAL_CLAUSE_RE = re.compile(
    r"^\s*Пункт\s+(\d+(?:\.\d+)+|\d+)\s*\.?\s*(.*)$", re.IGNORECASE
)
_PARAGRAPH_MARK_RE = re.compile(
    r"^\s*§\s*(\d+(?:\.\d+)+|\d+)\s*\.?\s*(.*)$"
)
_CYRILLIC_ALPHA_RE = re.compile(r"^\s*([а-яё])\)\s+(.{2,200})$", re.IGNORECASE)
_APPENDIX_RE = re.compile(
    r"^\s*Приложение\s+([A-ZА-Я0-9]+(?:\.\d+)?)\s*(.{2,200})?$", re.IGNORECASE
)


def _roman_to_int(s: str) -> int | None:
    """Преобразовать римскую цифру (I..XXX) в int. None если не парсится."""
    s = s.upper().strip()
    if not s:
        return None
    roman_map = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
    total = 0
    prev = 0
    for ch in reversed(s):
        v = roman_map.get(ch)
        if v is None:
            return None
        if v < prev:
            total -= v
        else:
            total += v
        prev = v
    return total if total > 0 else None


def _parse_decimal_number(raw: str) -> tuple[tuple[int, ...], int]:
    """``"1.2.3"`` → ((1, 2, 3), 3)."""
    parts = tuple(int(p) for p in raw.split("."))
    return parts, len(parts)


def parse_numbering(text: str) -> NumberingInfo | None:
    """Определить numbering scheme для ``text``.

    Возвращает ``NumberingInfo`` или ``None``, если текст не является
    нумерованным heading/list-item.

    Алгоритм: проверяем схемы в порядке убывания специфичности —
    сначала legal markers (Статья / Глава / Раздел / § / Пункт),
    потом appendix, потом цирillic_alpha, потом decimal.

    ``ordinal`` пока ставим в ``None`` — он требует знания siblings
    и будет вычислен в ``StructureTreeBuilder`` (PLAN §13).
    """
    if not text:
        return None
    s = text.strip()
    if not s:
        return None

    m = _LEGAL_ARTICLE_RE.match(s)
    if m:
        comp, level = _parse_decimal_number(m.group(1))
        return NumberingInfo(
            raw=m.group(1), scheme="legal_article",
            components=comp, level=level, ordinal=None,
        )

    m = _LEGAL_CHAPTER_RE.match(s)
    if m:
        comp, level = _parse_decimal_number(m.group(1))
        return NumberingInfo(
            raw=m.group(1), scheme="legal_chapter",
            components=comp, level=level, ordinal=None,
        )

    m = _LEGAL_SECTION_ROMAN_RE.match(s)
    if m:
        roman = m.group(1)
        val = _roman_to_int(roman)
        if val is None:
            return None
        return NumberingInfo(
            raw=roman, scheme="legal_section_roman",
            components=(val,), level=1, ordinal=None,
        )

    m = _LEGAL_CLAUSE_RE.match(s)
    if m:
        comp, level = _parse_decimal_number(m.group(1))
        return NumberingInfo(
            raw=m.group(1), scheme="legal_clause",
            components=comp, level=level, ordinal=None,
        )

    m = _PARAGRAPH_MARK_RE.match(s)
    if m:
        comp, level = _parse_decimal_number(m.group(1))
        return NumberingInfo(
            raw=m.group(1), scheme="paragraph_mark",
            components=comp, level=level, ordinal=None,
        )

    m = _APPENDIX_RE.match(s)
    if m:
        raw = m.group(1)
        comp: tuple[Any, ...]
        if "." in raw:
            letter, num = raw.split(".", 1)
            comp = (letter, int(num))
            level = 2
        elif raw.isdigit():
            comp = (int(raw),)
            level = 1
        else:
            comp = (raw,)
            level = 1
        return NumberingInfo(
            raw=raw, scheme="appendix", components=comp, level=level, ordinal=None,
        )

    m = _CYRILLIC_ALPHA_RE.match(s)
    if m:
        letter = m.group(1).lower()
        return NumberingInfo(
            raw=letter, scheme="cyrillic_alpha",
            components=(letter,), level=1, ordinal=None,
        )

    m = _DECIMAL_RE.match(s)
    if m:
        raw_num = m.group(1)
        comp, level = _parse_decimal_number(raw_num)
        return NumberingInfo(
            raw=raw_num, scheme="decimal",
            components=comp, level=level, ordinal=None,
        )

    return None


def assign_sibling_ordinals(
    items: list[NumberingInfo | None],
) -> list[int | None]:
    """Вычислить ``ordinal`` среди siblings одного ``scheme``/``level``.

    На вход — список ``NumberingInfo`` (или ``None`` для не-нумерованных),
    отсортированный по document order. Возвращает параллельный список
    с вычисленными ``ordinal`` (или ``None`` если нельзя вычислить).

    Правило: для каждой группы подряд идущих items с одним ``scheme``
    и одним ``parent`` (``components[:-1]``) — ordinal — позиция в группе
    (1-based). Это решает проблему PLAN §13: глобальный counter даёт
    неправильную нумерацию для nested структур.
    """
    if not items:
        return []

    out: list[int | None] = [None] * len(items)
    group_indices: list[int] = []
    current_parent: tuple[Any, ...] | None = None
    current_scheme: str | None = None
    current_level: int | None = None

    def _flush_group() -> None:
        nonlocal current_scheme, current_parent, current_level
        for k, idx in enumerate(group_indices, start=1):
            out[idx] = k
        group_indices.clear()
        current_scheme = None
        current_parent = None
        current_level = None

    for i, ni in enumerate(items):
        if ni is None:
            _flush_group()
            continue
        parent = ni.components[:-1] if len(ni.components) > 1 else ()
        if (
            ni.scheme == current_scheme
            and ni.level == current_level
            and parent == current_parent
        ):
            group_indices.append(i)
            continue
        _flush_group()
        current_scheme = ni.scheme
        current_level = ni.level
        current_parent = parent
        group_indices.append(i)

    if group_indices:
        _flush_group()

    return out


__all__ = [
    "parse_numbering",
    "assign_sibling_ordinals",
]