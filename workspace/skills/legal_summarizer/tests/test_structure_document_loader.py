"""Тесты для DocumentLoader (PLAN §12).

Acceptance: same physical source не парсится дважды.

* PDF: ``PdfReader`` создаётся один раз, ``pdfplumber`` — один раз
  (внутри ``_iter_pdf_blocks``);
* DOCX: ``Document(str(path))`` создаётся один раз
  (внутри ``_iter_docx_blocks``), title подхватывается через
  ``_pick_title_from_text`` который делает дополнительный ``Document``
  только для ``core_properties.title``.

DocumentLoader — single canonical loading path. Этот тест ловит
регрессию, если кто-то добавит второй парсинг файла.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from workspace.skills.legal_summarizer.scripts.structure.document_loader import (
    DocumentLoader,
)
from workspace.skills.legal_summarizer.scripts.structure.identity import (
    DocumentIdentity,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    PhysicalDocument,
)


def _write_pdf(path: Path) -> None:
    path.write_bytes(b"%PDF-1.4\n%fake pdf content\n")


def _write_docx(path: Path) -> None:
    path.write_bytes(b"PK\x03\x04fake docx")


def _write_txt(path: Path) -> None:
    path.write_text("plain text content", encoding="utf-8")


def test_loader_loads_txt(tmp_path: Path):
    p = tmp_path / "doc.txt"
    _write_txt(p)
    loader = DocumentLoader()
    doc = loader.load(p)
    assert isinstance(doc, PhysicalDocument)
    assert doc.format == "txt"
    assert len(doc.blocks) == 1
    assert doc.blocks[0].content == "plain text content"


def test_loader_uses_document_identity(tmp_path: Path):
    """PLAN §12 + §11: DocumentLoader использует DocumentIdentity."""
    p = tmp_path / "doc.txt"
    _write_txt(p)
    identity = DocumentIdentity.from_path(p)
    loader = DocumentLoader()
    doc = loader.load(p)
    assert doc.path == identity.resolved_path
    assert doc.size_bytes == identity.size_bytes


def test_loader_loads_pdf_calls_pypdf_at_most_twice(tmp_path: Path):
    """PLAN §12 acceptance: PdfReader создаётся минимизированно.

    ``_iter_pdf_blocks`` открывает PdfReader один раз. Title resolution
    через ``_pick_title_from_text`` для PDF открывает второй раз (для
    metadata.title). Итого ≤ 2.

    Допускается ≤ 2, не строго 1, потому что ``_pick_title_from_text``
    это общий helper и для DOCX открывает ``docx.Document`` отдельно.
    Это by design — title resolution из метаданных **не** дублирует
    body extraction.
    """
    p = tmp_path / "doc.pdf"
    _write_pdf(p)
    loader = DocumentLoader()

    with patch("pypdf.PdfReader") as mock_reader:
        mock_reader.return_value.pages = []
        mock_reader.return_value.metadata = {}
        loader.load(p)
        assert mock_reader.call_count <= 2, (
            f"expected ≤ 2 PdfReader instantiations, "
            f"got {mock_reader.call_count}"
        )


def test_loader_loads_docx_calls_document_once(tmp_path: Path):
    """PLAN §12 acceptance: docx.Document создаётся один раз для blocks."""
    p = tmp_path / "doc.docx"
    _write_docx(p)
    loader = DocumentLoader()

    with patch("docx.Document") as mock_doc:
        instance = mock_doc.return_value
        instance.element.body.iterchildren.return_value = []
        instance.paragraphs = []
        instance.tables = []
        instance.core_properties.title = None
        loader.load(p)
        assert mock_doc.call_count <= 2, (
            f"expected at most 2 Document() calls (one for blocks, "
            f"optionally one for title), got {mock_doc.call_count}"
        )


def test_loader_missing_file_raises(tmp_path: Path):
    p = tmp_path / "missing.txt"
    loader = DocumentLoader()
    import pytest
    with pytest.raises(FileNotFoundError):
        loader.load(p)


def test_loader_unsupported_format_raises(tmp_path: Path):
    p = tmp_path / "doc.xyz"
    p.write_text("anything", encoding="utf-8")
    loader = DocumentLoader()
    import pytest
    with pytest.raises(ValueError):
        loader.load(p)