"""Тесты для list-detection (Этап 10 из PLAN.md)."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.list_detection import (
    classify_ambiguous_run,
    detect_list_runs,
    list_penalty_for_candidate,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock,
)


def _b(ordinal: int, content: str, block_type: str = "paragraph") -> DocumentBlock:
    return DocumentBlock(
        block_id=f"b_{ordinal:04d}", block_type=block_type, content=content,
        char_count=len(content), page_index=None, page_start=None, page_end=None,
        paragraph_index=None, table_index=None, ordinal=ordinal, block_metadata={},
    )


def test_detect_list_runs_simple():
    blocks = (
        _b(0, "1. первый пункт списка"),
        _b(1, "2. второй пункт списка"),
        _b(2, "3. третий пункт списка"),
    )
    runs = detect_list_runs(blocks)
    assert len(runs) == 1
    assert runs[0].is_list is True


def test_detect_sections_not_list():
    blocks = (
        _b(0, "1. Общие положения"),
        _b(1, "Это длинный текст раздела, который точно не короткий список."),
        _b(2, "2. Обязанности сторон"),
        _b(3, "Ещё один длинный текст раздела."),
    )
    runs = detect_list_runs(blocks)
    assert len(runs) == 2
    for r in runs:
        assert r.is_list is False


def test_list_penalty_inside_long_run():
    runs = [
        type("R", (), {"is_list": True, "block_ordinals": (0, 1, 2, 3, 4), "numbers": (1, 2, 3, 4, 5)})()
    ]
    assert list_penalty_for_candidate(2, runs) == 0.15


def test_list_penalty_inside_short_run():
    runs = [
        type("R", (), {"is_list": True, "block_ordinals": (0, 1, 2), "numbers": (1, 2, 3)})()
    ]
    assert list_penalty_for_candidate(1, runs) == 0.08


def test_list_penalty_outside_run():
    runs = [
        type("R", (), {"is_list": True, "block_ordinals": (0, 1, 2), "numbers": (1, 2, 3)})()
    ]
    assert list_penalty_for_candidate(99, runs) == 0.0


def test_classify_ambiguous_all_short():
    blocks = (_b(0, "1. a"), _b(1, "2. b"), _b(2, "3. c"))
    assert classify_ambiguous_run((0, 1, 2), blocks) == "list"


def test_classify_ambiguous_all_long():
    blocks = (
        _b(0, "1. " + "x" * 250),
        _b(1, "2. " + "x" * 250),
    )
    assert classify_ambiguous_run((0, 1), blocks) == "section"


def test_classify_ambiguous_mixed():
    blocks = (_b(0, "1. short"), _b(1, "2. " + "x" * 250))
    assert classify_ambiguous_run((0, 1), blocks) == "ambiguous"