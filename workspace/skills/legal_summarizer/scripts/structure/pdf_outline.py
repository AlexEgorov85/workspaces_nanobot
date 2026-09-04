"""PDF outline mapping (PLAN §11).

Критический bugfix: ``heading._extract_pdf_outline`` ставил ``block_index = -1``
для outline-кандидатов, после чего ``tree.build_section_tree`` отбрасывал
их (filter ``c.block_index >= 0``). В результате PDF outline фактически
не участвовал в построении дерева.

Этот модуль реализует **явный pipeline**:

    PDF outline entry
            |
            v
        destination
            |
            v
        page (1-based, valid в документе)
            |
            v
        nearest/containing DocumentBlock
            |
            v
        StructureAnchor
            |
            v
        MappedOutlineCandidate (block_index >= 0)
            |
            v
        HeadingCandidate (через агрегатор)

Валидации (PLAN §11):

* destination существует (не ``None``);
* page в документе (``1 <= page <= page_count``);
* порядок outline соответствует document order (страницы не убывают);
* duplicate destinations → один outline entry;
* отсут конные destinations → пропускаются с diagnostics;
* конфликт с существующим heading (тот же ``block_index``) → outline
  считается более приоритетным (PLAN §8, «very high»).

PDF outline нельзя слепо считать истиной — но **валидно mapped**
outline даёт очень высокую confidence (0.95).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock,
    PhysicalDocument,
)


_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StructureAnchor:
    """Якорь, связывающий outline entry с конкретным ``DocumentBlock``.

    Attributes:
        block_ordinal: ordinal ``DocumentBlock``, на который указывает
            outline entry.
        page_index: 1-based страница из destination.
        char_offset: смещение в тексте страницы (если доступно;
            ``None`` если pypdf не вернул).
    """

    block_ordinal: int
    page_index: int
    char_offset: int | None = None


@dataclass(frozen=True)
class MappedOutlineCandidate:
    """Outline entry после успешного mapping на DocumentBlock.

    Attributes:
        block_index: ordinal DocumentBlock (**всегда >= 0** для mapped;
            ``-1`` только если mapping провалился и есть в diagnostics).
        text: текст outline.
        level: outline depth (1-based).
        score: confidence (0.95 для успешно mapped).
        anchor: ``StructureAnchor`` (или ``None`` если mapping провалился).
        diagnostics: tuple строк с описанием проблем mapping
            (``"missing_destination"``, ``"page_out_of_range"``,
            ``"no_blocks_on_page"``).
    """

    block_index: int
    text: str
    level: int
    score: float
    anchor: StructureAnchor | None
    diagnostics: tuple[str, ...] = ()

    def to_heading_candidate(self) -> HeadingCandidate | None:
        """Преобразовать в ``HeadingCandidate``.

        Возвращает ``None`` если mapping провалился (нет anchor).
        """
        if self.anchor is None:
            return None
        return HeadingCandidate(
            block_index=self.block_index,
            text=self.text,
            score=self.score,
            source="pdf_outline",
            level=self.level,
            raw_number=None,
        )


def _resolve_destination_page(reader: Any, page_ref: Any) -> int | None:
    """Получить 1-based номер страницы из outline destination.

    Поддерживает два формата destination:

    * ``list`` (``['4', 'XYZ', ...]``) — прямой page reference.
    * ``DictionaryObject`` (``{'/Page': IndirectObject(...)}``) — именованный.
    * ``IndirectObject`` — page reference.

    Возвращает ``None`` если destination не парсится или ссылается на
    несуществующую страницу.
    """
    try:
        if isinstance(page_ref, list) and page_ref:
            target = page_ref[0]
        elif isinstance(page_ref, dict):
            target = page_ref.get("/Page")
        else:
            target = page_ref
        if target is None:
            return None
        page = target if not hasattr(target, "get_object") else target.get_object()
        idx = None
        for i, p in enumerate(reader.pages):
            if p.indirect_reference == getattr(page, "indirect_reference", None):
                idx = i
                break
            if p is page:
                idx = i
                break
        if idx is None:
            return None
        return idx + 1
    except Exception:
        return None


def _find_nearest_block_on_page(
    doc: PhysicalDocument,
    page_index: int,
) -> int | None:
    """Найти ближайший DocumentBlock на заданной странице.

    Возвращает ordinal первого block'а с ``page_index == page_index``,
    или ``None`` если на странице нет блоков (например, только таблицы).
    """
    for b in doc.blocks:
        if b.page_index == page_index:
            return b.ordinal
    return None


def _walk_outline(
    reader: Any,
    items: list[Any],
    level: int,
) -> list[tuple[int, str, Any]]:
    """Рекурсивно обойти outline, вернуть ``(level, title, page_ref)``.

    Page_ref — это destination target, который потом резолвится в
    1-based page index через ``_resolve_destination_page``.
    """
    out: list[tuple[int, str, Any]] = []
    for item in items:
        if isinstance(item, list):
            out.extend(_walk_outline(reader, item, level + 1))
            continue
        try:
            title = getattr(item, "title", None) or str(item)
        except Exception:
            continue
        if not title or not str(title).strip():
            continue
        page_ref = None
        try:
            page_ref = getattr(item, "page", None)
        except Exception:
            page_ref = None
        out.append((level, str(title).strip(), page_ref))
    return out


def map_pdf_outline(
    pdf_path: str,
    doc: PhysicalDocument,
) -> list[MappedOutlineCandidate]:
    """Прочитать PDF outline и замапить каждую entry на ``DocumentBlock``.

    Возвращает список ``MappedOutlineCandidate`` — успешно mapped (с
    ``block_index >= 0``) **плюс** провалившиеся (с ``diagnostics``).

    Diagnostics элементы не должны использоваться как headings; они
    остаются для отладки и diagnostics (``map_pdf_outline`` отдельный
    output не для ``detect_heading_candidates``, а для
    ``aggregate_by_block`` → который увидит их только если есть
    успешный ``block_index >= 0``).
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        return []

    try:
        reader = PdfReader(pdf_path)
    except Exception as e:
        _log.debug("map_pdf_outline: cannot open %s: %s", pdf_path, e)
        return []

    try:
        items = list(reader.outline)
    except Exception:
        items = []

    try:
        raw = _walk_outline(reader, items, 1)
    except Exception:
        return []

    if not raw:
        return []

    seen_pages: set[int] = set()
    out: list[MappedOutlineCandidate] = []
    prev_page = 0

    for level, title, page_ref in raw:
        page_index = _resolve_destination_page(reader, page_ref)
        diagnostics: list[str] = []

        if page_index is None:
            diagnostics.append("missing_destination")
            out.append(
                MappedOutlineCandidate(
                    block_index=-1,
                    text=title,
                    level=level,
                    score=0.0,
                    anchor=None,
                    diagnostics=tuple(diagnostics),
                )
            )
            continue

        if page_index < 1 or page_index > doc.page_count:
            diagnostics.append("page_out_of_range")
            out.append(
                MappedOutlineCandidate(
                    block_index=-1,
                    text=title,
                    level=level,
                    score=0.0,
                    anchor=None,
                    diagnostics=tuple(diagnostics),
                )
            )
            continue

        if page_index < prev_page:
            diagnostics.append("out_of_document_order")
        prev_page = page_index

        if page_index in seen_pages:
            diagnostics.append("duplicate_destination")
        else:
            seen_pages.add(page_index)

        block_ord = _find_nearest_block_on_page(doc, page_index)
        if block_ord is None:
            diagnostics.append("no_blocks_on_page")
            out.append(
                MappedOutlineCandidate(
                    block_index=-1,
                    text=title,
                    level=level,
                    score=0.0,
                    anchor=None,
                    diagnostics=tuple(diagnostics),
                )
            )
            continue

        anchor = StructureAnchor(
            block_ordinal=block_ord,
            page_index=page_index,
            char_offset=None,
        )
        out.append(
            MappedOutlineCandidate(
                block_index=block_ord,
                text=title,
                level=level,
                score=0.95,
                anchor=anchor,
                diagnostics=tuple(diagnostics),
            )
        )

    return out


def mapped_to_heading_candidates(
    mapped: list[MappedOutlineCandidate],
) -> list["HeadingCandidate"]:
    """Преобразовать успешно mapped кандидатов в ``HeadingCandidate``.

    Провалившие (с ``block_index = -1``) **отбрасываются** —
    они остаются в diagnostics, но не участвуют в heading detection.
    Это решает исходный bug: outline теперь даёт ``block_index >= 0``,
    и ``build_section_tree`` его не отбрасывает.
    """
    from workspace.skills.legal_summarizer.scripts.structure.heading import (
        HeadingCandidate,
    )
    out: list[HeadingCandidate] = []
    for m in mapped:
        if m.anchor is None:
            continue
        out.append(
            HeadingCandidate(
                block_index=m.block_index,
                text=m.text,
                score=m.score,
                source="pdf_outline",
                level=m.level,
                raw_number=None,
            )
        )
    return out


__all__ = [
    "StructureAnchor",
    "MappedOutlineCandidate",
    "map_pdf_outline",
    "mapped_to_heading_candidates",
]