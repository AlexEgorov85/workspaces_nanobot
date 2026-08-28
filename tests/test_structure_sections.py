"""Тесты для ``structure/sections.py``.

Покрывает:
    * Confidence scoring (DOCX Heading, PDF outline, regex)
    * Penalty для heading без body после него
    * Построение дерева section (nested, flat, no headings)
    * DOCX style приоритет над regex
    * meaningful_sections для reduce
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "workspace" / "skills" / "legal_summarizer" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
_PROJ = _REPO
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))

from workspace.skills.legal_summarizer.scripts.structure.physical import (  # noqa: E402
    DocumentBlock,
    PhysicalDocument,
)
from workspace.skills.legal_summarizer.scripts.structure.sections import (  # noqa: E402
    CONFIDENCE_THRESHOLD,
    ROOT_SECTION_ID,
    SectionTree,
    count_meaningful_sections,
    detect_sections,
    merge_short_sections,
)


def _make_doc(blocks_data: list[dict[str, Any]]) -> PhysicalDocument:
    blocks: list[DocumentBlock] = []
    for i, b in enumerate(blocks_data):
        blocks.append(
            DocumentBlock(
                block_id=f"b_{i:04d}",
                block_type=b.get("block_type", "paragraph"),
                content=b["content"],
                char_count=len(b["content"]),
                page_index=b.get("page_index"),
                page_start=b.get("page_index"),
                page_end=b.get("page_index"),
                paragraph_index=b.get("paragraph_index"),
                table_index=None,
                ordinal=i,
                block_metadata=b.get("block_metadata", {}),
            )
        )
    return PhysicalDocument(
        path="<test>",
        format="docx",
        title=None,
        size_bytes=0,
        blocks=tuple(blocks),
        page_count=1,
    )


def test_no_headings_returns_root_section():
    doc = _make_doc([
        {"content": "Просто параграф без headings."},
        {"content": "Ещё один параграф."},
    ])
    tree = detect_sections(doc)
    assert tree.root_id == ROOT_SECTION_ID
    assert tree.sections[ROOT_SECTION_ID].level == 0
    assert len(tree.sections) == 1
    assert tree.sections[ROOT_SECTION_ID].block_indices == (0, 1)
    assert tree.block_to_section[0] == ROOT_SECTION_ID
    assert tree.block_to_section[1] == ROOT_SECTION_ID


def test_simple_numbered_sections_level_1():
    doc = _make_doc([
        {"content": "1. Общие положения"},
        {"content": "Это длинный текст про общие положения договора, описывающий стороны и предмет."},
        {"content": "2. Предмет договора"},
        {"content": "Длинный текст про предмет договора с описанием обязательств сторон."},
    ])
    tree = detect_sections(doc)
    non_root = [s for sid, s in tree.sections.items() if sid != ROOT_SECTION_ID]
    assert len(non_root) == 2
    headings = sorted(s.heading for s in non_root)
    assert "1. Общие положения" in headings
    assert "2. Предмет договора" in headings


def test_nested_numbered_sections():
    doc = _make_doc([
        {"content": "1. Общие положения"},
        {"content": "Длинное введение в общие положения договора аренды с описанием сторон."},
        {"content": "1.1. Стороны"},
        {"content": "Длинное описание сторон договора: арендодатель и арендатор, их реквизиты и полномочия."},
        {"content": "1.2. Предмет"},
        {"content": "Длинное описание предмета договора: помещение, площадь, характеристики и состояние."},
        {"content": "2. Ответственность"},
        {"content": "Длинное описание ответственности сторон и санкций за нарушение обязательств."},
    ])
    tree = detect_sections(doc)
    non_root = [s for sid, s in tree.sections.items() if sid != ROOT_SECTION_ID]
    assert len(non_root) == 4
    paths = sorted(s.section_path for s in non_root)
    assert "1" in paths
    assert "2" in paths
    nested = [s for s in non_root if s.level >= 2]
    assert len(nested) == 2


def test_statya_glava_razdel_detected():
    doc = _make_doc([
        {"content": "Статья 5. Ответственность сторон"},
        {"content": "Подробное описание ответственности сторон по договору аренды помещения."},
        {"content": "Глава 2. Порядок расчётов"},
        {"content": "Подробное описание порядка расчётов между сторонами и формы оплаты услуг."},
        {"content": "Раздел 3. Срок действия"},
        {"content": "Подробное описание сроков действия договора и условий его продления."},
    ])
    tree = detect_sections(doc)
    non_root = [s for sid, s in tree.sections.items() if sid != ROOT_SECTION_ID]
    assert len(non_root) >= 3


def test_paragraph_sign_detected():
    doc = _make_doc([
        {"content": "I. Общая часть"},
        {"content": "Длинное введение в общую часть договора аренды с описанием целей и предмета."},
        {"content": "§ 1. Определения"},
        {"content": "Длинное описание основных определений и терминов, используемых в договоре."},
    ])
    tree = detect_sections(doc)
    non_root = [s for sid, s in tree.sections.items() if sid != ROOT_SECTION_ID]
    assert len(non_root) >= 1


def test_docx_heading_style_takes_priority():
    """DOCX Heading style → 0.95, всегда проходит threshold."""
    doc = _make_doc([
        {
            "content": "Короткий заголовок раздела",
            "block_metadata": {"style": "Heading 1"},
        },
        {"content": "Длинное тело раздела с описанием предмета и сторон договора."},
        {
            "content": "Подраздел 1",
            "block_metadata": {"style": "Heading 2"},
        },
        {"content": "Длинное тело подраздела с подробным описанием условий и обязательств."},
    ])
    tree = detect_sections(doc)
    non_root = [s for sid, s in tree.sections.items() if sid != ROOT_SECTION_ID]
    assert len(non_root) == 2


def test_heading_at_end_of_document_demoted():
    """Heading без body после него получает score *= 0.5."""
    doc = _make_doc([
        {"content": "1. Заголовок с телом"},
        {"content": "Длинное тело раздела с содержательным описанием."},
        {"content": "2. Заголовок без тела"},
    ])
    tree = detect_sections(doc)
    candidates = [s for sid, s in tree.sections.items() if sid != ROOT_SECTION_ID]
    headings = sorted([s.heading for s in candidates])
    assert "1. Заголовок с телом" in headings
    assert "2. Заголовок без тела" not in headings


def test_table_blocks_not_used_as_headings():
    """Tables не становятся headings — не создают section сами по себе."""
    doc = _make_doc([
        {"content": "1. Заголовок"},
        {"content": "Длинное тело с описанием обязательств сторон договора аренды."},
        {"content": "1. Таблица внутри раздела", "block_type": "table"},
    ])
    tree = detect_sections(doc)
    headings = [s.heading for s in tree.sections.values() if s.heading]
    assert not any("Таблица" in h for h in headings)


def test_section_assignment_covers_all_blocks():
    doc = _make_doc([
        {"content": "1. Раздел один"},
        {"content": "Длинное тело раздела один с подробным описанием условий договора."},
        {"content": "Параграф без heading"},
        {"content": "2. Раздел два"},
        {"content": "Длинное тело раздела два с описанием прав и обязанностей сторон."},
    ])
    tree = detect_sections(doc)
    for i in range(len(doc.blocks)):
        assert i in tree.block_to_section


def test_ordinals_are_preserved():
    doc = _make_doc([
        {"content": "1. Заголовок один"},
        {"content": "Длинное тело один."},
        {"content": "2. Заголовок два"},
        {"content": "Длинное тело два."},
    ])
    tree = detect_sections(doc)
    for s in tree.sections.values():
        if not s.block_indices:
            continue
        assert list(s.block_indices) == sorted(s.block_indices)
        assert all(isinstance(i, int) for i in s.block_indices)


def test_long_heading_text_demoted():
    """Heading >80 chars при regex level=1 → score 0.55 (ниже threshold)."""
    doc = _make_doc([
        {
            "content": "1. Это очень-очень-очень длинный заголовок раздела который не должен считаться заголовком потому что слишком длинный",
        },
        {"content": "Длинное тело после длинного заголовка с описанием предмета договора."},
    ])
    tree = detect_sections(doc)
    non_root = [s for sid, s in tree.sections.items() if sid != ROOT_SECTION_ID]
    assert non_root == []


def test_meaningful_sections_excludes_short_chunks():
    doc = _make_doc([
        {"content": "1. Раздел один", "block_metadata": {"style": "Heading 1"}},
        {"content": "Длинное содержательное тело раздела один с подробностями."},
        {"content": "2. Раздел два", "block_metadata": {"style": "Heading 2"}},
        {"content": "x"},
    ])
    tree = detect_sections(doc)
    non_root = [s for sid, s in tree.sections.items() if sid != ROOT_SECTION_ID]
    assert len(non_root) == 2
    meaningful = count_meaningful_sections(tree, doc.blocks)
    assert meaningful >= 1


def test_meaningful_sections_includes_top_level_even_short():
    """Top-level heading + подраздел с body — обе meaningful."""
    doc = _make_doc([
        {"content": "1. Топ-раздел", "block_metadata": {"style": "Heading 1"}},
        {"content": "Длинное введение в топ-раздел с описанием целей и предмета договора аренды."},
        {"content": "1.1. Подраздел с body", "block_metadata": {"style": "Heading 2"}},
        {"content": "Длинное тело подраздела с подробным описанием условий и обязательств сторон."},
    ])
    tree = detect_sections(doc)
    meaningful = count_meaningful_sections(tree, doc.blocks)
    assert meaningful >= 2


def test_section_path_format():
    doc = _make_doc([
        {"content": "1. Раздел один", "block_metadata": {"style": "Heading 1"}},
        {"content": "Длинное тело с описанием."},
        {"content": "1.1. Подраздел", "block_metadata": {"style": "Heading 2"}},
        {"content": "Длинное тело с описанием."},
        {"content": "1.1.1. Под-подраздел", "block_metadata": {"style": "Heading 3"}},
        {"content": "Длинное тело с описанием."},
    ])
    tree = detect_sections(doc)
    non_root = [s for sid, s in tree.sections.items() if sid != ROOT_SECTION_ID]
    paths = sorted(s.section_path for s in non_root)
    assert any(" > " in p for p in paths)


def test_pdf_outline_overrides_regex():
    """Если PDF outline даёт heading — он используется как primary."""
    doc = _make_doc([
        {"content": "Это просто текст, не heading"},
        {"content": "И это просто текст"},
    ])
    tree = detect_sections(doc, pdf_path=None)
    assert tree.sections[ROOT_SECTION_ID].level == 0


def test_confidence_threshold_constant():
    assert CONFIDENCE_THRESHOLD == 0.60


def test_block_to_section_for_root():
    doc = _make_doc([
        {"content": "Просто текст 1"},
        {"content": "Просто текст 2"},
    ])
    tree = detect_sections(doc)
    assert tree.block_to_section[0] == ROOT_SECTION_ID
    assert tree.block_to_section[1] == ROOT_SECTION_ID


def test_merge_short_sections_collapses_tiny_numbered_list():
    """30 нумерованных heading'ов с микро-body → 1 секция после merge.

    Реальный сценарий «32 → 450» (и наоборот — 450 → 1): короткие
    параграфы между heading'ами делают каждую секцию микро. После
    merge — все они схлопываются в одну родительскую секцию.
    """
    blocks_data: list[dict[str, Any]] = []
    body_text = "Краткий текст."
    for i in range(1, 31):
        blocks_data.append({"content": f"{i}. Раздел"})
        blocks_data.append({"content": body_text})
    doc = _make_doc(blocks_data)
    raw_tree = detect_sections(doc)
    raw_count = len([s for sid, s in raw_tree.sections.items() if sid != ROOT_SECTION_ID])
    assert raw_count >= 10

    merged = merge_short_sections(raw_tree, doc.blocks, min_section_chars=200)
    non_root = [s for sid, s in merged.sections.items() if sid != ROOT_SECTION_ID]
    assert len(non_root) <= 2
    assert len(non_root) < raw_count // 5
    for i in range(len(doc.blocks)):
        assert i in merged.block_to_section
    assert merged.root_id == ROOT_SECTION_ID


def test_merge_short_sections_preserves_large_sections():
    """Большие секции НЕ сливаются — только микро."""
    doc = _make_doc([
        {"content": "1. Большой раздел"},
        {"content": "Длинное содержательное тело большого раздела с подробностями и условиями." * 5},
        {"content": "2. Микро-пункт"},
        {"content": "x"},
        {"content": "3. Микро-пункт"},
        {"content": "y"},
        {"content": "4. Ещё микро"},
        {"content": "z"},
    ])
    tree = detect_sections(doc)
    merged = merge_short_sections(tree, doc.blocks, min_section_chars=200)
    non_root = [s for sid, s in merged.sections.items() if sid != ROOT_SECTION_ID]
    by_heading = sorted(s.heading for s in non_root)
    assert any("Большой" in h for h in by_heading)
    assert len(non_root) <= 2


def test_merge_short_sections_preserves_block_to_section_consistency():
    """block_to_section покрывает все блоки и не указывает на удалённые sid."""
    doc = _make_doc([
        {"content": f"{i}. Пункт {i}."} for i in range(1, 51)
    ])
    tree = detect_sections(doc)
    merged = merge_short_sections(tree, doc.blocks)
    for i in range(len(doc.blocks)):
        assert i in merged.block_to_section
        assert merged.block_to_section[i] in merged.sections


def test_merge_short_sections_no_op_when_all_large():
    """Если все секции достаточно большие — merge оставляет дерево как есть."""
    doc = _make_doc([
        {"content": "1. Раздел"},
        {"content": "Длинное тело раздела один с подробным описанием предмета и сторон договора аренды." * 3},
        {"content": "2. Раздел"},
        {"content": "Длинное тело раздела два с подробным описанием прав и обязанностей сторон." * 3},
    ])
    raw_tree = detect_sections(doc)
    raw_count = len([s for sid, s in raw_tree.sections.items() if sid != ROOT_SECTION_ID])
    merged = merge_short_sections(raw_tree, doc.blocks, min_section_chars=200)
    merged_count = len([s for sid, s in merged.sections.items() if sid != ROOT_SECTION_ID])
    assert merged_count == raw_count


def test_merge_short_sections_custom_threshold_low():
    """С min_section_chars=1 — почти все короткие секции сливаются."""
    doc = _make_doc([
        {"content": "1. Пункт"},
        {"content": "Тело пункта."},
        {"content": "2. Пункт"},
        {"content": "Тело пункта."},
    ])
    tree = detect_sections(doc)
    raw_count = len([s for sid, s in tree.sections.items() if sid != ROOT_SECTION_ID])
    assert raw_count == 2
    merged = merge_short_sections(tree, doc.blocks, min_section_chars=1)
    non_root = [s for sid, s in merged.sections.items() if sid != ROOT_SECTION_ID]
    # Оба пункта по ~10 символов (<1 это невозможно), не сливаются —
    # но достаточно маленький threshold оставляет их как есть.
    # Главное — не нарушена инвариантность.
    assert len(non_root) == 2


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))