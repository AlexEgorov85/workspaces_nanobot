"""Facade для sections detection — ``structure/sections.py``.

``sections.py`` — тонкая обёртка над двумя специализированными модулями:

    * ``structure/heading.py`` — кандидаты + scoring (DOCX style, regex,
      PDF outline), confidence penalties.
    * ``structure/tree.py`` — DocumentSection/SectionTree и построение
      дерева из принятых кандидатов.

Facade сохраняет старый публичный API (все symbols re-exported), чтобы
``test_structure_sections.py`` / ``summarizer.py`` не ломались.

См. ``workspace/skills/legal_summarizer/ARCHITECTURE.md`` invariants #4, #5.
"""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.heading import (
    CONFIDENCE_THRESHOLD,
    HeadingCandidate,
    _classify_regex,
    _extract_pdf_outline,
    _is_docx_heading_style,
    _looks_like_heading,
    apply_confidence_penalties as _apply_confidence_penalties,
    apply_evidence_scoring as _apply_evidence_scoring,
    detect_heading_candidates as _detect_candidates,
    filter_above_threshold as _filter_above_threshold,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock,
    PhysicalDocument,
)
from workspace.skills.legal_summarizer.scripts.structure.tree import (
    ROOT_SECTION_ID,
    DocumentSection,
    SectionTree,
    build_section_tree as _build_sections,
    section_total_chars as _section_total_chars,
)


def extract_local_structure_label(text: str) -> str:
    """Извлечь первую структурную метку из текста чанка.

    Возвращает первую строку, начинающуюся с юр. заголовка
    (Раздел/Подраздел/Глава/Статья/Часть/§), обрезанную до 120 символов.
    Если не найдено — пустая строка.

    Используется как fallback для подписи чанка при формировании общего
    ответа, когда глобальный detect_sections не нашёл разделов.
    """
    import re

    _LOCAL_HEADING_RE = re.compile(
        r"^\s*(?:Раздел|Подраздел|Глава|Статья|Часть|§)\b[^\n]{0,120}",
        re.IGNORECASE | re.MULTILINE,
    )

    if not text:
        return ""
    m = _LOCAL_HEADING_RE.search(text)
    if not m:
        return ""
    return m.group(0).strip()[:120]


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
    candidates = _apply_evidence_scoring(candidates, doc.blocks)
    candidates = _filter_above_threshold(candidates)
    return _build_sections(candidates, doc.blocks)


def merge_short_sections(
    tree: SectionTree,
    blocks: tuple[DocumentBlock, ...],
    *,
    min_section_chars: int = 200,
) -> SectionTree:
    """Схлопнуть микро-секции, порождённые детектором heading'ов.

    См. ARCHITECTURE.md § merge_short_sections для правил и инвариантов.
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
    "extract_local_structure_label",
    "ROOT_SECTION_ID",
    "CONFIDENCE_THRESHOLD",
]