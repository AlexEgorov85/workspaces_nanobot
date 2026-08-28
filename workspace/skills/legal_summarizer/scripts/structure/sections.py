"""Deterministic Section Detection для legal_summarizer.

Извлекает иерархию разделов документа **детерминированно**, без LLM:

Источники (по убыванию confidence):
  1. DOCX ``style == "Heading N"`` (0.95)
  2. PDF outline (0.95)
  3. Regex для русских юр. headings ("Статья N", "Глава N", "§ N") (0.80-0.85)
  4. Regex для цифровых нумераций ("1.", "1.2") (0.65-0.70)

Threshold = 0.60. Ниже — не считается heading.

Confidence penalty (мягкое правило):
  * если после heading идёт другой heading или пустой block → score *= 0.5
  * это анти-false-positive для заголовков в конце документа или в списке

Дерево строится по ``DocumentBlock.ordinal`` (canonical order, invariant #3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock,
    PhysicalDocument,
)


CONFIDENCE_THRESHOLD = 0.60
ROOT_SECTION_ID = "s_root"

_HEADING_KEYWORDS = ("heading ", "heading_", "заголовок")

_RE_NUMBERED_LEVEL_1 = re.compile(r"^\s*(\d+)\.\s+(.{2,200})$")
_RE_NUMBERED_LEVEL_2 = re.compile(r"^\s*(\d+)\.(\d+)\.?\s+(.{2,200})$")
_RE_NUMBERED_LEVEL_3 = re.compile(r"^\s*(\d+)\.(\d+)\.(\d+)\.?\s+(.{2,200})$")

_RE_STATIYA = re.compile(r"^\s*Статья\s+(\d+(?:\.\d+)?)\s*\.?\s*(.{2,200})$", re.IGNORECASE)
_RE_GLAVA = re.compile(r"^\s*Глава\s+(\d+(?:\.\d+)?)\s*\.?\s*(.{2,200})$", re.IGNORECASE)
_RE_RAZDEL = re.compile(r"^\s*Раздел\s+(\d+(?:\.\d+)?)\s*\.?\s*(.{2,200})$", re.IGNORECASE)
_RE_PARAGRAPH = re.compile(r"^\s*§\s*(\d+(?:\.\d+)?)\s*\.?\s*(.{2,200})$")

_ANY_HEADING_RE = re.compile(
    r"^\s*(?:\d+\.(?:\d+\.?)?(?:\d+\.?)?|Статья\s+\d+|Глава\s+\d+|Раздел\s+\d+|§\s*\d+)\s*[.:]?\s*\S"
)


@dataclass(frozen=True)
class HeadingCandidate:
    """Кандидат на heading с confidence score."""

    block_index: int
    text: str
    score: float
    source: str
    level: int
    raw_number: str | None = None


@dataclass(frozen=True)
class DocumentSection:
    """Узел в дереве разделов документа."""

    section_id: str
    level: int
    heading: str
    section_path: str
    block_indices: tuple[int, ...]
    children: tuple[str, ...] = field(default_factory=tuple)
    parent_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "level": self.level,
            "heading": self.heading,
            "section_path": self.section_path,
            "block_indices": list(self.block_indices),
            "children": list(self.children),
            "parent_id": self.parent_id,
        }


@dataclass(frozen=True)
class SectionTree:
    """Дерево DocumentSection для документа."""

    sections: dict[str, DocumentSection]
    root_id: str
    block_to_section: dict[int, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_id": self.root_id,
            "sections": {sid: s.to_dict() for sid, s in self.sections.items()},
            "block_to_section": dict(self.block_to_section),
        }


def _is_docx_heading_style(style_name: str) -> bool:
    if not style_name:
        return False
    name = style_name.lower()
    return any(name.startswith(prefix) for prefix in _HEADING_KEYWORDS)


def _looks_like_heading(text: str) -> bool:
    """Грубая проверка формата heading в тексте."""
    return bool(_ANY_HEADING_RE.match(text.strip()))


def _classify_regex(text: str) -> tuple[int, float, str, str | None] | None:
    """Классифицировать текст по regex'ам. Вернуть (level, score, source, number)."""
    s = text.strip()
    if not s:
        return None
    m = _RE_NUMBERED_LEVEL_3.match(s)
    if m:
        return (3, 0.70, "regex_numbered_3", f"{m.group(1)}.{m.group(2)}.{m.group(3)}")
    m = _RE_NUMBERED_LEVEL_2.match(s)
    if m:
        return (2, 0.70, "regex_numbered_2", f"{m.group(1)}.{m.group(2)}")
    m = _RE_NUMBERED_LEVEL_1.match(s)
    if m:
        return (1, 0.65, "regex_numbered_1", m.group(1))
    m = _RE_STATIYA.match(s)
    if m:
        return (1, 0.85, "regex_statiya", f"статья_{m.group(1)}")
    m = _RE_GLAVA.match(s)
    if m:
        return (1, 0.80, "regex_glava", f"глава_{m.group(1)}")
    m = _RE_RAZDEL.match(s)
    if m:
        return (1, 0.80, "regex_razdel", f"раздел_{m.group(1)}")
    m = _RE_PARAGRAPH.match(s)
    if m:
        return (2, 0.80, "regex_paragraph", f"§{m.group(1)}")
    return None


def _extract_pdf_outline(path: str) -> list[HeadingCandidate]:
    """Прочитать PDF outline → list[HeadingCandidate].

    Не возвращаем уровень/номер — outline сам даёт структуру,
    а нам нужно только текст заголовка и порядок.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        return []

    try:
        reader = PdfReader(path)
    except Exception:
        return []

    candidates: list[HeadingCandidate] = []
    ordinal_counter = 0
    try:
        items = list(reader.outline)
    except Exception:
        items = []

    def _walk(items: list[Any], level: int) -> None:
        nonlocal ordinal_counter
        for item in items:
            if isinstance(item, list):
                _walk(item, level + 1)
                continue
            try:
                title = getattr(item, "title", None) or str(item)
            except Exception:
                continue
            if not title or not str(title).strip():
                continue
            candidates.append(
                HeadingCandidate(
                    block_index=-1,
                    text=str(title).strip(),
                    score=0.95,
                    source="pdf_outline",
                    level=level,
                    raw_number=None,
                )
            )
            ordinal_counter += 1

    try:
        _walk(items, 1)
    except Exception:
        pass
    return candidates


def _detect_candidates(blocks: tuple[DocumentBlock, ...], pdf_path: str | None) -> list[HeadingCandidate]:
    """Найти всех кандидатов в heading'и (DOCX style + regex + PDF outline)."""
    candidates: list[HeadingCandidate] = []

    for block in blocks:
        if block.block_type == "table":
            continue
        text = block.content.strip()

        style_name = block.block_metadata.get("style", "")
        if _is_docx_heading_style(style_name):
            level = 1
            m = re.search(r"(\d+)", style_name)
            if m:
                try:
                    level = max(1, min(6, int(m.group(1))))
                except ValueError:
                    pass
            candidates.append(
                HeadingCandidate(
                    block_index=block.ordinal,
                    text=text,
                    score=0.95,
                    source="docx_style",
                    level=level,
                    raw_number=None,
                )
            )
            continue

        classified = _classify_regex(text)
        if classified is None:
            continue
        level, score, source, raw_number = classified
        if level == 1 and len(text) > 80:
            score = min(score, 0.55)
        candidates.append(
            HeadingCandidate(
                block_index=block.ordinal,
                text=text,
                score=score,
                source=source,
                level=level,
                raw_number=raw_number,
            )
        )

    if pdf_path:
        outline_candidates = _extract_pdf_outline(pdf_path)
        for c in outline_candidates:
            candidates.append(c)

    return candidates


def _apply_confidence_penalties(
    candidates: list[HeadingCandidate],
    blocks: tuple[DocumentBlock, ...],
) -> list[HeadingCandidate]:
    """Снизить score для heading'ов, после которых идёт другой heading
    или пустой block (anti-false-positive)."""
    if not candidates:
        return candidates
    by_index = {c.block_index: c for c in candidates if c.block_index >= 0}
    if not by_index:
        return candidates
    max_ord = max(b.ordinal for b in blocks)
    out: list[HeadingCandidate] = []
    for c in candidates:
        if c.block_index < 0 or c.source == "pdf_outline":
            out.append(c)
            continue
        next_idx = c.block_index + 1
        if next_idx > max_ord:
            out.append(HeadingCandidate(**{**c.__dict__, "score": c.score * 0.5}))
            continue
        next_block = blocks[next_idx] if next_idx < len(blocks) else None
        if next_block is None or not next_block.content.strip():
            out.append(HeadingCandidate(**{**c.__dict__, "score": c.score * 0.5}))
            continue
        if next_block.ordinal in by_index:
            next_score = by_index[next_block.ordinal].score
            if next_score >= CONFIDENCE_THRESHOLD:
                out.append(HeadingCandidate(**{**c.__dict__, "score": c.score * 0.5}))
                continue
        out.append(c)
    return out


def _filter_above_threshold(candidates: list[HeadingCandidate]) -> list[HeadingCandidate]:
    return [c for c in candidates if c.score >= CONFIDENCE_THRESHOLD]


def _build_sections(
    candidates: list[HeadingCandidate],
    blocks: tuple[DocumentBlock, ...],
) -> SectionTree:
    """Построить дерево DocumentSection из принятых кандидатов."""
    sections: dict[str, DocumentSection] = {
        ROOT_SECTION_ID: DocumentSection(
            section_id=ROOT_SECTION_ID,
            level=0,
            heading="",
            section_path="",
            block_indices=tuple(b.ordinal for b in blocks),
            children=(),
            parent_id=None,
        )
    }
    block_to_section: dict[int, str] = {b.ordinal: ROOT_SECTION_ID for b in blocks}

    accepted = sorted(
        [c for c in candidates if c.block_index >= 0],
        key=lambda c: c.block_index,
    )
    if not accepted:
        return SectionTree(sections=sections, root_id=ROOT_SECTION_ID, block_to_section=block_to_section)

    section_counter = 0
    path_counter_by_level: dict[int, int] = {}

    def _next_section_id() -> str:
        nonlocal section_counter
        sid = f"s_{section_counter:04d}"
        section_counter += 1
        return sid

    def _make_with_children(section: DocumentSection, children: tuple[str, ...]) -> DocumentSection:
        return DocumentSection(
            section_id=section.section_id,
            level=section.level,
            heading=section.heading,
            section_path=section.section_path,
            block_indices=section.block_indices,
            children=children,
            parent_id=section.parent_id,
        )

    stack: list[DocumentSection] = [sections[ROOT_SECTION_ID]]

    heading_index_to_section_id: dict[int, str] = {}

    for c in accepted:
        level = c.level
        while len(stack) > 1 and stack[-1].level >= level:
            stack.pop()

        parent = stack[-1]
        parent_level_int = parent.level

        path_counter_by_level[level] = path_counter_by_level.get(level, 0) + 1
        own_path = str(path_counter_by_level[level])

        ancestors_paths: list[str] = []
        for s in stack[1:]:
            if s.section_path:
                ancestors_paths.extend(s.section_path.split(" > "))
        section_path = " > ".join(ancestors_paths + [own_path])

        sid = _next_section_id()
        new_section = DocumentSection(
            section_id=sid,
            level=level,
            heading=c.text,
            section_path=section_path,
            block_indices=(c.block_index,),
            children=(),
            parent_id=parent.section_id,
        )
        sections[sid] = new_section
        block_to_section[c.block_index] = sid
        heading_index_to_section_id[c.block_index] = sid

        sections[parent.section_id] = _make_with_children(parent, parent.children + (sid,))
        if parent.section_id == ROOT_SECTION_ID:
            stack[0] = sections[ROOT_SECTION_ID]
        else:
            for i in range(len(stack) - 1, -1, -1):
                if stack[i].section_id == parent.section_id:
                    stack[i] = sections[parent.section_id]
                    break

        stack.append(new_section)

    sorted_accepted = sorted(accepted, key=lambda c: c.block_index)
    ranges: list[tuple[int, int, str]] = []
    for i, c in enumerate(sorted_accepted):
        sid = heading_index_to_section_id[c.block_index]
        start = c.block_index
        end = (
            sorted_accepted[i + 1].block_index - 1
            if i + 1 < len(sorted_accepted)
            else max((b.ordinal for b in blocks), default=start)
        )
        ranges.append((start, end, sid))

    sections_by_id: dict[str, DocumentSection] = {}
    for start, end, sid in ranges:
        existing = sections[sid]
        new_indices = tuple(range(start, end + 1))
        updated = DocumentSection(
            section_id=existing.section_id,
            level=existing.level,
            heading=existing.heading,
            section_path=existing.section_path,
            block_indices=new_indices,
            children=existing.children,
            parent_id=existing.parent_id,
        )
        sections_by_id[sid] = updated
        for idx in new_indices:
            if block_to_section.get(idx) == sid:
                continue
            if idx in heading_index_to_section_id:
                continue
            block_to_section[idx] = sid
    sections.update(sections_by_id)

    root_blocks: list[int] = []
    covered = set()
    for s in sections.values():
        if s.section_id == ROOT_SECTION_ID:
            continue
        covered.update(s.block_indices)
    for b in blocks:
        if b.ordinal not in covered:
            root_blocks.append(b.ordinal)
            block_to_section[b.ordinal] = ROOT_SECTION_ID

    root = sections[ROOT_SECTION_ID]
    sections[ROOT_SECTION_ID] = DocumentSection(
        section_id=root.section_id,
        level=root.level,
        heading=root.heading,
        section_path=root.section_path,
        block_indices=tuple(root_blocks),
        children=root.children,
        parent_id=root.parent_id,
    )

    return SectionTree(sections=sections, root_id=ROOT_SECTION_ID, block_to_section=block_to_section)


def detect_sections(doc: PhysicalDocument, *, pdf_path: str | None = None) -> SectionTree:
    """Детектировать иерархию разделов документа.

    Args:
        doc: PhysicalDocument.
        pdf_path: путь к исходному PDF (для outline extraction). Если
            None и формат PDF — outline пропускается.

    Returns:
        SectionTree.
    """
    candidates = _detect_candidates(doc.blocks, pdf_path)
    candidates = _apply_confidence_penalties(candidates, doc.blocks)
    candidates = _filter_above_threshold(candidates)
    return _build_sections(candidates, doc.blocks)


def _section_total_chars(section: DocumentSection, chars_by_index: dict[int, int]) -> int:
    """Суммарная длина блоков секции."""
    return sum(chars_by_index.get(idx, 0) for idx in section.block_indices)


def merge_short_sections(
    tree: SectionTree,
    blocks: tuple[DocumentBlock, ...],
    *,
    min_section_chars: int = 200,
) -> SectionTree:
    """Схлопнуть микро-секции, порождённые детектором heading'ов.

    Regex-детектор ``_RE_NUMBERED_LEVEL_1`` (score0.65) срабатывает на
    списках вида «1. Пункт», «2. Пункт», … в длинных нормативных
    текстах, и каждая строка оказывается отдельной секцией. Без
    пост-обработки это приводит к взрыву числа чанков и LLM-вызовов
    (Phase 2B: chunks ~ len(sections)).

    Правило: если у секции ``level ∈ {1, 2}`` суммарный объём body
    (``_section_total_chars``) строго меньше ``min_section_chars`` —
    объединить её с **соседней секцией того же parent_id** (предпочтительно
    предыдущей; если нет — следующей). Heading приклеенной микро-секции
    переносится в heading целевой (через «; »), чтобы не терять
    семантическую разметку.

    Сохранение семантической иерархии важнее, чем идеальная чистота
    ``section_path``: см. ARCHITECTURE.md § merge_short_sections.

    Args:
        tree: SectionTree от ``detect_sections``.
        blocks: блоки документа (нужны для подсчёта char_count).
        min_section_chars: минимальная длина body (символов) для
            секции level 1–2; короче — кандидат на merge.

    Returns:
        Новый SectionTree (старый не мутируется).
    """
    chars_by_index: dict[int, int] = {b.ordinal: b.char_count for b in blocks}

    sections: dict[str, DocumentSection] = dict(tree.sections)
    block_to_section: dict[int, str] = dict(tree.block_to_section)

    def _children_sorted(parent_id: str) -> list[str]:
        kids = [
            s for s in sections.values()
            if s.parent_id == parent_id and s.section_id != ROOT_SECTION_ID
        ]
        ids = sorted(
            [s.section_id for s in kids],
            key=lambda sid: min(sections[sid].block_indices) if sections[sid].block_indices else 0,
        )
        return ids

    def _absorb(src_id: str, dst_id: str) -> None:
        """Слить секцию src_id в dst_id."""
        src = sections[src_id]
        dst = sections[dst_id]
        merged_indices = tuple(sorted(set(dst.block_indices) | set(src.block_indices)))
        merged_heading = (
            f"{dst.heading}; {src.heading}"
            if dst.heading and src.heading
            else (dst.heading or src.heading)
        )
        sections[dst_id] = DocumentSection(
            section_id=dst.section_id,
            level=dst.level,
            heading=merged_heading,
            section_path=dst.section_path,
            block_indices=merged_indices,
            children=dst.children,
            parent_id=dst.parent_id,
        )
        for idx in src.block_indices:
            if block_to_section.get(idx) == src_id:
                block_to_section[idx] = dst_id
        sections.pop(src_id, None)

    changed = True
    while changed:
        changed = False
        for parent_id in list(sections.keys()):
            kid_ids = _children_sorted(parent_id)
            if len(kid_ids) < 2:
                continue
            i = 0
            while i < len(kid_ids):
                cur_id = kid_ids[i]
                if cur_id not in sections:
                    i += 1
                    continue
                cur = sections[cur_id]
                total = _section_total_chars(cur, chars_by_index)
                if cur.level in (1, 2) and total < min_section_chars:
                    target_id: str | None = None
                    if i > 0 and kid_ids[i - 1] in sections and sections[kid_ids[i - 1]].level == cur.level:
                        target_id = kid_ids[i - 1]
                    elif i + 1 < len(kid_ids) and kid_ids[i + 1] in sections and sections[kid_ids[i + 1]].level == cur.level:
                        target_id = kid_ids[i + 1]
                    if target_id is not None:
                        _absorb(cur_id, target_id)
                        kid_ids = _children_sorted(parent_id)
                        changed = True
                        continue
                i += 1

    covered = set()
    for s in sections.values():
        if s.section_id == ROOT_SECTION_ID:
            continue
        covered.update(s.block_indices)
    root_blocks: list[int] = []
    for b in blocks:
        if b.ordinal not in covered:
            root_blocks.append(b.ordinal)
            block_to_section[b.ordinal] = ROOT_SECTION_ID

    root = sections[ROOT_SECTION_ID]
    sections[ROOT_SECTION_ID] = DocumentSection(
        section_id=root.section_id,
        level=root.level,
        heading=root.heading,
        section_path=root.section_path,
        block_indices=tuple(root_blocks),
        children=root.children,
        parent_id=root.parent_id,
    )

    return SectionTree(
        sections=sections,
        root_id=tree.root_id,
        block_to_section=block_to_section,
    )


def count_meaningful_sections(tree: SectionTree, blocks: tuple[DocumentBlock, ...]) -> int:
    """Число meaningful sections для решения о hierarchical reduce.

    Правило: section meaningful если
      (а) у него есть chunk с char_count >= 100, ИЛИ
      (б) level <= 2 И heading непустое (это structural anchor даже без body).
    """
    meaningful = 0
    chars_by_index = {b.ordinal: b.char_count for b in blocks}
    for sid, section in tree.sections.items():
        if sid == ROOT_SECTION_ID:
            continue
        max_chunk = max((chars_by_index.get(i, 0) for i in section.block_indices), default=0)
        is_top_level = section.level <= 2 and section.heading.strip()
        if max_chunk >= 100 or is_top_level:
            meaningful += 1
    return meaningful


__all__ = [
    "DocumentSection",
    "SectionTree",
    "HeadingCandidate",
    "detect_sections",
    "merge_short_sections",
    "count_meaningful_sections",
    "ROOT_SECTION_ID",
    "CONFIDENCE_THRESHOLD",
]