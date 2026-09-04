"""Тесты для premature abstraction guard (Этап 60 из PLAN.md)."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.architecture_guard import (
    count_abstract_classes, has_oversized_class, is_factory_pattern,
)


def test_factory_pattern_detection():
    assert is_factory_pattern("MyFactory") is True
    assert is_factory_pattern("SectionBuilder") is True
    assert is_factory_pattern("HeadingStrategyFactory") is True
    assert is_factory_pattern("DocumentStructure") is False
    assert is_factory_pattern("ChunkPlanner") is False
    assert is_factory_pattern("") is False


def test_count_abstract_classes():
    from workspace.skills.legal_summarizer.scripts.structure import (
        models, repair, validation,
    )
    for module in (models, repair, validation):
        count = count_abstract_classes(module)
        assert count == 0, f"{module.__name__} has {count} abstract classes"


def test_no_oversized_classes_in_new_modules():
    from workspace.skills.legal_summarizer.scripts.structure import (
        hierarchy, retrieval, numbering,
    )
    for module in (hierarchy, retrieval, numbering):
        assert has_oversized_class(module, max_lines=500) is False


def test_factory_check_specific_names():
    """Проверяем имена из PLAN §60."""
    forbidden = [
        "BaseStructureFactory",
        "AbstractHeadingStrategyFactory",
        "GenericNodeResolverFactory",
    ]
    for name in forbidden:
        assert is_factory_pattern(name) is True


def test_clean_module_under_threshold():
    from workspace.skills.legal_summarizer.scripts.structure import numbering
    assert has_oversized_class(numbering, max_lines=300) is False