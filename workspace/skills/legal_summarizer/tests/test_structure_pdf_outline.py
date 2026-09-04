"""Тесты для PDF outline mapping (Этап 11 из PLAN.md).

Тесты используют in-memory mock PdfReader / outline, чтобы не зависеть
от реальных PDF-файлов и platform-specific pypdf behaviour.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from workspace.skills.legal_summarizer.scripts.structure.pdf_outline import (
    StructureAnchor,
    map_pdf_outline,
    mapped_to_heading_candidates,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock,
    PhysicalDocument,
)


def _b(ordinal: int, page_index: int, content: str = "x") -> DocumentBlock:
    return DocumentBlock(
        block_id=f"b_{ordinal:04d}", block_type="page", content=content,
        char_count=len(content), page_index=page_index,
        page_start=page_index, page_end=page_index,
        paragraph_index=None, table_index=None, ordinal=ordinal, block_metadata={},
    )


def _make_doc(n_pages: int) -> PhysicalDocument:
    blocks = tuple(_b(i, i + 1) for i in range(n_pages))
    return PhysicalDocument(
        path="/tmp/fake.pdf", format="pdf", title="x",
        size_bytes=100, blocks=blocks, page_count=n_pages,
    )


def _outline_item(title: str, page_index_1based: int):
    """Создать mock outline item с ``page=IndirectObject(page_index)``."""
    item = MagicMock()
    item.title = title
    page_ref = MagicMock()
    page_ref.get_object.return_value = page_ref
    item.page = [page_ref]
    item.page.__getitem__.side_effect = lambda i: page_ref if i == 0 else (_ for _ in ()).throw(
        IndexError
    )
    return item, page_ref


def _make_reader(outline_items: list, n_pages: int):
    """Создать mock PdfReader с outline и pages."""
    reader = MagicMock()
    reader.pages = []
    refs = []
    for i in range(n_pages):
        page = MagicMock()
        page.indirect_reference = MagicMock(id=i)
        refs.append(page.indirect_reference)
        reader.pages.append(page)

    def _resolve(target):
        page_ref = target if not hasattr(target, "get_object") else target.get_object()
        for i, p in enumerate(reader.pages):
            if p.indirect_reference == getattr(page_ref, "indirect_reference", None):
                return i
        return None

    for item, page_ref in outline_items:
        page_ref.get_object.return_value = page_ref

    reader.outline = outline_items
    return reader


def test_mapped_to_heading_candidates_skips_unmapped(monkeypatch):
    """Кандидаты с anchor=None отбрасываются."""
    from workspace.skills.legal_summarizer.scripts.structure.pdf_outline import (
        MappedOutlineCandidate,
    )

    mapped = [
        MappedOutlineCandidate(
            block_index=-1, text="bad", level=1, score=0.0,
            anchor=None, diagnostics=("missing_destination",),
        ),
        MappedOutlineCandidate(
            block_index=5, text="good", level=1, score=0.95,
            anchor=StructureAnchor(block_ordinal=5, page_index=3),
        ),
    ]
    out = mapped_to_heading_candidates(mapped)
    assert len(out) == 1
    assert out[0].block_index == 5
    assert out[0].source == "pdf_outline"


def test_map_pdf_outline_no_pypdf(monkeypatch):
    """Если pypdf недоступен — возвращает пустой список."""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pypdf":
            raise ImportError("no pypdf")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    doc = _make_doc(3)
    result = map_pdf_outline("/tmp/x.pdf", doc)
    assert result == []


def test_map_pdf_outline_no_outline(monkeypatch):
    """Outline пуст → возвращает пустой список."""
    import builtins
    real_import = builtins.__import__

    class FakePdfReader:
        def __init__(self, path):
            self.outline = []
            self.pages = []

    def fake_import(name, *args, **kwargs):
        if name == "pypdf":
            return type("FakePypdf", (), {"PdfReader": FakePdfReader})
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    doc = _make_doc(3)
    result = map_pdf_outline("/tmp/x.pdf", doc)
    assert result == []


def test_map_pdf_outline_missing_destination(monkeypatch):
    """Item без destination → diagnostics=missing_destination, anchor=None."""
    import builtins
    real_import = builtins.__import__

    class _Item:
        title = "no-destination"

        @property
        def page(self):
            return None

    class FakePdfReader:
        def __init__(self, path):
            self.outline = [_Item()]
            self.pages = [MagicMock()]

    def fake_import(name, *args, **kwargs):
        if name == "pypdf":
            return type("FakePypdf", (), {"PdfReader": FakePdfReader})
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    doc = _make_doc(1)
    result = map_pdf_outline("/tmp/x.pdf", doc)
    assert len(result) == 1
    assert result[0].anchor is None
    assert "missing_destination" in result[0].diagnostics


def test_structure_anchor_roundtrip():
    a = StructureAnchor(block_ordinal=5, page_index=3, char_offset=42)
    assert a.block_ordinal == 5
    assert a.page_index == 3
    assert a.char_offset == 42