"""SectionTree / DocumentSection + tree construction — ``structure/tree.py``.

Поведение НЕ меняется.

Содержит:
    * ``DocumentSection`` — узел в дереве разделов.
    * ``SectionTree`` — само дерево + ``block_to_section`` mapping.
    * ``ROOT_SECTION_ID`` — корневая секция (``"s_root"``).
    * ``build_section_tree(candidates, blocks)`` — построить дерево из
      принятых heading-кандидатов (бывшая приватная ``_build_sections``).
    * ``section_total_chars(section, chars_by_index)`` — суммарная длина
      блоков секции (бывшая приватная ``_section_total_chars``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from workspace.skills.legal_summarizer.scripts.structure.heading import (
    HeadingCandidate,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock,
)


ROOT_SECTION_ID = "s_root"


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


def section_total_chars(section: DocumentSection, chars_by_index: dict[int, int]) -> int:
    """Суммарная длина блоков секции."""
    return sum(chars_by_index.get(idx, 0) for idx in section.block_indices)


def build_section_tree(
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


__all__ = [
    "DocumentSection",
    "SectionTree",
    "ROOT_SECTION_ID",
    "build_section_tree",
    "section_total_chars",
]
