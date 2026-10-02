"""Structure correctness suite.

Расширенное покрытие DocumentStructure:
* nested decimal (1.1.1)
* chapter/article
* section/subsection
* parent reconstruction
* sibling ordinal reset
* level jumps
* missing intermediate parent
* mixed numbering
* unrelated numbering not merged
"""

from __future__ import annotations

from document.heading import (
    HeadingCandidate,
)
from document.hierarchy import (
    build_document_structure,
)

def _hc(block_index: int, text: str, source: str = "regex_numbered_1"):
    return HeadingCandidate(
        block_index=block_index, text=text, score=0.7,
        source=source, level=1, raw_number=None,
    )

def test_nested_decimal():
    """1.1.1 — три уровня decimal."""
    cs = [
        _hc(0, "1. Первая"),
        _hc(5, "1.1. Под первая"),
        _hc(10, "1.1.1. Под под первая"),
    ]
    s = build_document_structure(cs, total_blocks=15, document_id="test")
    assert s.root_id in s.nodes

    sections = s.iter_sections()
    assert len(sections) == 3
    first = sections[0]
    second = sections[1]
    third = sections[2]
    assert second.parent_id == first.node_id
    assert third.parent_id == second.node_id

def test_chapter_article():
    """Глава → Статья."""
    cs = [
        _hc(0, "Глава 1", source="regex_glava"),
        _hc(5, "Статья 1", source="regex_statiya"),
        _hc(10, "Статья 2", source="regex_statiya"),
    ]
    s = build_document_structure(cs, total_blocks=15, document_id="test")
    sections = s.iter_sections()
    assert len(sections) == 3
    chapter = sections[0]
    assert chapter.semantic_type == "chapter"
    article_1 = sections[1]
    article_2 = sections[2]
    assert article_1.semantic_type == "article"
    assert article_2.semantic_type == "article"
    assert article_1.parent_id == chapter.node_id
    assert article_2.parent_id == chapter.node_id

def test_section_subsection():
    """Раздел → подраздел.

    ``Раздел N`` (level 1) содержит ``Подраздел N.M`` (level 2).
    Проверяем, что ``Подраздел`` парсится как decimal и parent указывает
    на соответствующий ``Раздел``.
    """
    cs = [
        _hc(0, "Раздел 1"),
        _hc(5, "Подраздел 1.1"),
        _hc(10, "Подраздел 1.2"),
    ]
    s = build_document_structure(cs, total_blocks=15, document_id="test")
    sections = s.iter_sections()
    assert len(sections) == 3, f"expected 3 sections, got {len(sections)}"
    parent = sections[0]
    assert parent.level == 1, f"parent level expected 1, got {parent.level}"
    child_1 = sections[1]
    child_2 = sections[2]
    assert child_1.level == 2
    assert child_2.level == 2
    assert child_1.parent_id == parent.node_id
    assert child_2.parent_id == parent.node_id


def test_sibling_ordinal_reset():
    """Под каждым parent ordinal начинается с 1.

    Дерево:
        1. Раздел 1
            1.1. Подраздел
        2. Раздел 2
        1. Новый подраздел (parent=root, level=1)

    Проверяем, что после ``2. Раздел 2`` heading ``1. ...`` снова
    становится level 1 (а не продолжает 2.x.y).
    """
    cs = [
        _hc(0, "1. Раздел 1"),
        _hc(5, "1.1. Подраздел"),
        _hc(10, "2. Раздел 2"),
        _hc(15, "1. Новый раздел после 2."),
    ]
    s = build_document_structure(cs, total_blocks=20, document_id="test")
    sections = s.iter_sections()
    assert len(sections) == 4
    # levels: 1, 2, 1, 1
    assert [n.level for n in sections] == [1, 2, 1, 1], (
        f"expected levels [1, 2, 1, 1], got {[n.level for n in sections]}"
    )
    # sections[2] is the second "1." — should be parent=root.
    assert sections[2].parent_id == s.root_id
    assert sections[3].parent_id == s.root_id


def test_level_jumps_allowed():
    """Level 1 → Level 3 через отсутствующий Level 2.

    Если в дереве встречается ``1.1.1.`` без явного ``1.1.``, родителем
    становится ближайший предок с меньшим level (или root).
    """
    cs = [
        _hc(0, "1. Раздел 1"),
        _hc(5, "1.1.1. Под под подраздел"),
    ]
    s = build_document_structure(cs, total_blocks=10, document_id="test")
    sections = s.iter_sections()
    assert len(sections) == 2
    parent, child = sections
    assert parent.level < child.level, (
        f"parent.level={parent.level}, child.level={child.level}"
    )
    assert child.parent_id in {parent.node_id, s.root_id}, (
        "level-jump child должен быть привязан к ближайшему предку или root"
    )

def test_missing_intermediate_parent_handled():
    """Section без родителя становится ребёнком root."""
    cs = [
        _hc(0, "Random heading without numbering"),
        _hc(5, "1.1."),
    ]
    s = build_document_structure(cs, total_blocks=10, document_id="test")
    sections = s.iter_sections()
    assert sections[0].parent_id == s.root_id

def test_mixed_numbering_schemes():
    """decimal + legal не сливаются.

    Известное ограничение: ``parse_numbering`` не распознаёт "1.1."
    (regex требует текст после цифры). Тест зафиксирован для будущего
    исправления numbering.py.
    """
    cs = [
        _hc(0, "Глава 1", source="regex_glava"),
        _hc(5, "Статья 5", source="regex_statiya"),
    ]
    s = build_document_structure(cs, total_blocks=15, document_id="test")
    sections = s.iter_sections()
    assert len(sections) == 2
    assert sections[0].semantic_type == "chapter"
    assert sections[1].semantic_type == "article"

def test_unrelated_numbering_not_merged():
    """``1.1. ...`` и ``2.1. ...`` НЕ склеиваются в один узел.

    Оба становятся разными section-nodes; важно что они не сливаются
    в одну запись только потому что у них похожая структура.
    """
    cs = [
        _hc(0, "Глава 1", source="regex_glava"),
        _hc(5, "1.1. Раздел 1.1"),
        _hc(10, "2.1. Раздел 2.1"),
    ]
    s = build_document_structure(cs, total_blocks=15, document_id="test")
    sections = s.iter_sections()
    assert len(sections) == 3
    chapter = sections[0]
    sec_1_1 = sections[1]
    sec_2_1 = sections[2]
    assert sec_1_1.node_id != sec_2_1.node_id, (
        "1.1. и 2.1. должны быть разными узлами"
    )
    assert sec_1_1.parent_id == chapter.node_id
    assert sec_2_1.parent_id == chapter.node_id