"""Тесты для repeated cleanup (Этап 42, Этап 43 из PLAN.md)."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.cleanup import (
    CleanupConfig, cleanup_repeated_blocks, detect_repeated_regions,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock,
)


def _b(ord: int, content: str) -> DocumentBlock:
    return DocumentBlock(
        block_id=f"b_{ord:04d}", block_type="page", content=content,
        char_count=len(content), page_index=ord + 1, page_start=ord + 1,
        page_end=ord + 1, paragraph_index=None, table_index=None,
        ordinal=ord, block_metadata={},
    )


def test_detect_repeated_simple():
    blocks = (
        _b(0, "Уникальный текст"),
        _b(1, "footer"),
        _b(2, "footer"),
        _b(3, "footer"),
        _b(4, "Ещё текст"),
    )
    regions = detect_repeated_regions(blocks)
    assert len(regions) == 1
    assert regions[0].text == "footer"


def test_detect_repeated_min_threshold():
    blocks = (
        _b(0, "header"),
        _b(1, "text"),
        _b(2, "header"),
        _b(3, "text"),
    )
    cfg = CleanupConfig(min_repetitions=3)
    regions = detect_repeated_regions(blocks, config=cfg)
    assert len(regions) == 0


def test_cleanup_keeps_first_occurrence():
    blocks = (
        _b(0, "footer"),
        _b(1, "footer"),
        _b(2, "footer"),
        _b(3, "unique"),
    )
    removed = cleanup_repeated_blocks(blocks)
    assert 0 not in removed
    assert 1 in removed
    assert 2 in removed


def test_cleanup_skips_short_text():
    blocks = (
        _b(0, "ab"),
        _b(1, "ab"),
        _b(2, "ab"),
    )
    removed = cleanup_repeated_blocks(blocks)
    assert removed == []


def test_cleanup_skips_long_text():
    blocks = (
        _b(0, "x" * 300),
        _b(1, "x" * 300),
        _b(2, "x" * 300),
    )
    removed = cleanup_repeated_blocks(blocks)
    assert removed == []


def test_cleanup_multiple_regions():
    blocks = (
        _b(0, "footer1"),
        _b(1, "footer1"),
        _b(2, "footer1"),
        _b(3, "unique1"),
        _b(4, "footer2"),
        _b(5, "footer2"),
        _b(6, "footer2"),
        _b(7, "unique2"),
    )
    removed = cleanup_repeated_blocks(blocks)
    assert set(removed) >= {1, 2, 5, 6}


def test_role_guess():
    from workspace.skills.legal_summarizer.scripts.structure.cleanup import (
        _guess_role,
    )
    assert _guess_role("© 2024 company") == "footer_copyright"
    assert _guess_role("стр. 5") == "footer_page_number"


def test_cleanup_no_blocks():
    assert cleanup_repeated_blocks(()) == []