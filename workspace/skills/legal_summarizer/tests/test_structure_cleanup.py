"""Тесты для repeated cleanup (PLAN §18, §42)."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.cleanup import (
    CleanupConfig, cleanup_repeated_blocks, detect_repeated_regions,
    is_repeated,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock,
)


_PAGE_INDEX_UNSET = object()


def _b(ord: int, content: str, page_index=_PAGE_INDEX_UNSET) -> DocumentBlock:
    """Block helper.

    По умолчанию ``page_index = ord + 1``. Если нужно явно
    ``page_index = None`` (нет page geometry) — передавайте ``None``
    через sentinel: ``_b(0, "x", page_index=None)``.
    """
    if page_index is _PAGE_INDEX_UNSET:
        pi = ord + 1
    else:
        pi = page_index
    return DocumentBlock(
        block_id=f"b_{ord:04d}", block_type="page", content=content,
        char_count=len(content),
        page_index=pi, page_start=pi, page_end=pi,
        paragraph_index=None, table_index=None,
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


def test_page_aware_evidence_when_page_index_known():
    """PLAN §18: page-aware evidence добавляется если page_index есть."""
    blocks = (
        _b(0, "Header", page_index=1),
        _b(1, "Body1", page_index=1),
        _b(2, "Header", page_index=2),
        _b(3, "Body2", page_index=2),
        _b(4, "Header", page_index=3),
        _b(5, "Body3", page_index=3),
    )
    regions = detect_repeated_regions(blocks)
    assert len(regions) == 1
    assert regions[0].text == "Header"
    assert regions[0].has_page_evidence is True


def test_page_aware_evidence_false_without_page_index():
    """PLAN §18: без page_index нет page-aware evidence."""
    blocks = (
        _b(0, "Header", page_index=None),
        _b(1, "Body1", page_index=None),
        _b(2, "Header", page_index=None),
        _b(3, "Body2", page_index=None),
        _b(4, "Header", page_index=None),
    )
    regions = detect_repeated_regions(blocks)
    assert regions[0].has_page_evidence is False


def test_require_page_evidence_filters_legal_content():
    """PLAN §18: require_page_evidence=True защищает от false-positives
    на legal content (где повторяющийся текст — не header/footer)."""
    cfg = CleanupConfig(require_page_evidence=True)
    blocks = (
        _b(0, "Статья 1", page_index=None),
        _b(1, "Body1", page_index=None),
        _b(2, "Статья 1", page_index=None),
        _b(3, "Body2", page_index=None),
        _b(4, "Статья 1", page_index=None),
    )
    regions = detect_repeated_regions(blocks, config=cfg)
    assert regions == []


def test_is_repeated_helper():
    """PLAN §18: is_repeated helper для downstream."""
    blocks = (
        _b(0, "Header", page_index=1),
        _b(1, "Body", page_index=1),
        _b(2, "Header", page_index=2),
        _b(3, "Body", page_index=2),
        _b(4, "Header", page_index=3),
    )
    assert is_repeated(blocks, 0) is False
    assert is_repeated(blocks, 1) is False
    assert is_repeated(blocks, 2) is True
    assert is_repeated(blocks, 4) is True