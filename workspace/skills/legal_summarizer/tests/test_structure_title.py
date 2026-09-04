"""Тесты для title resolution (Этап 14 из PLAN.md)."""

from __future__ import annotations

from pathlib import Path

from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock,
    PhysicalDocument,
)
from workspace.skills.legal_summarizer.scripts.structure.title import (
    resolve_title,
)


def _b(ordinal: int, content: str, block_type: str = "paragraph",
       style: str = "") -> DocumentBlock:
    return DocumentBlock(
        block_id=f"b_{ordinal:04d}", block_type=block_type, content=content,
        char_count=len(content), page_index=None, page_start=None, page_end=None,
        paragraph_index=None, table_index=None, ordinal=ordinal,
        block_metadata={"style": style} if style else {},
    )


def _make_doc(path: str = "/tmp/no.docx", blocks: tuple[DocumentBlock, ...] = (),
              title: str | None = None) -> PhysicalDocument:
    return PhysicalDocument(
        path=path, format="docx", title=title,
        size_bytes=0, blocks=blocks, page_count=1,
    )


def test_resolve_title_docx_metadata(monkeypatch):
    """DOCX core_properties.title → source='metadata'."""

    class _Props:
        title = "My Doc Title"

    class _Doc:
        core_properties = _Props()

    monkeypatch.setattr("docx.Document", lambda *a, **kw: _Doc())

    doc = _make_doc(path="/tmp/test.docx")
    t = resolve_title(doc)
    assert t is not None
    assert t.value == "My Doc Title"
    assert t.source == "metadata"
    assert t.confidence == 1.0


def test_resolve_title_docx_title_style():
    """DOCX Title style → source='visual'."""
    blocks = (
        _b(0, "Normal para"),
        _b(1, "My Visual Title", style="Title"),
        _b(2, "Body"),
    )
    doc = _make_doc(blocks=blocks)
    t = resolve_title(doc)
    assert t is not None
    assert t.value == "My Visual Title"
    assert t.source == "visual"
    assert t.block_ordinal == 1


def test_resolve_title_docx_subtitle_style():
    """DOCX Subtitle style → source='visual'."""
    blocks = (_b(0, "My Subtitle", style="Subtitle"),)
    doc = _make_doc(blocks=blocks)
    t = resolve_title(doc)
    assert t is not None
    assert t.source == "visual"


def test_resolve_title_first_heading():
    """DOCX Heading 1 → source='inferred'."""
    blocks = (
        _b(0, "First Heading", style="Heading 1"),
        _b(1, "Body"),
    )
    doc = _make_doc(blocks=blocks)
    t = resolve_title(doc)
    assert t is not None
    assert t.value == "First Heading"
    assert t.source == "inferred"


def test_resolve_title_fallback():
    """Ничего не нашли → fallback по первой непустой строке текста."""
    blocks = ()
    doc = _make_doc(blocks=blocks)
    t = resolve_title(doc, text="Some line\nMore content")
    assert t is not None
    assert t.value == "Some line"
    assert t.source == "inferred"


def test_resolve_title_no_fallback():
    """Если fallback не дан → None."""
    blocks = ()
    doc = _make_doc(blocks=blocks)
    assert resolve_title(doc) is None
    assert resolve_title(doc, text="") is None


def test_resolve_title_priority_metadata_over_style(monkeypatch):
    """Metadata важнее visual."""

    class _Props:
        title = "Meta Title"

    class _Doc:
        core_properties = _Props()

    monkeypatch.setattr("docx.Document", lambda *a, **kw: _Doc())

    blocks = (_b(0, "Visual Title", style="Title"),)
    doc = _make_doc(blocks=blocks)
    t = resolve_title(doc)
    assert t.source == "metadata"
    assert t.value == "Meta Title"