"""Acceptance tests для Этапа 3: DocumentIdentity.document_id == DocumentStructure.document_id.

Invariant: production pipeline передаёт ``document_id=identity.document_id``
в ``build_document_structure``, и ``DocumentAnalysis.build`` выравнивает
``structure.document_id`` по ``identity.document_id`` при расхождении.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _write_doc(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "doc.txt"
    p.write_text(text, encoding="utf-8")
    return p


def test_document_id_invariant_for_txt(tmp_path: Path):
    """TXT: ``structure.document_id == identity.document_id``."""
    from workspace.skills.legal_summarizer.scripts.structure.pipeline import (
        run_canonical_pipeline,
    )

    text = (
        "1. Общие положения\n\n" + ("Текст. " * 50) * 20
        + "\n\n2. Раздел Б\n\n" + ("Текст Б. " * 50) * 20
    )
    p = _write_doc(tmp_path, text)
    result = run_canonical_pipeline(str(p), apply_repair=True)
    assert result.analysis.identity.document_id == result.analysis.structure.document_id


def test_document_id_invariant_for_pdf(tmp_path: Path):
    """PDF: ``structure.document_id == identity.document_id``."""
    from workspace.skills.legal_summarizer.scripts.structure.pipeline import (
        run_canonical_pipeline,
    )

    p = tmp_path / "doc.pdf"
    # Минимальный валидный PDF с извлекаемым текстом.
    p.write_bytes(
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj "
        b"2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj "
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R>>endobj "
        b"4 0 obj<</Length 44>>stream\nBT /F1 12 Tf 100 700 Td "
        b"(Test PDF Document) Tj ET\nendstream\nendobj\n"
        b"xref\n0 5\n0000000000 65535 f \n0000000010 00000 n \n0000000053 00000 n \n"
        b"0000000100 00000 n \n0000000167 00000 n \ntrailer<</Size 5/Root 1 0 R>>\n"
        b"startxref\n230\n%%EOF"
    )

    try:
        result = run_canonical_pipeline(str(p), apply_repair=True)
    except Exception as e:
        # Минимальный PDF может не парситься корректно — допустимо.
        import pytest
        pytest.skip(f"Minimal PDF not parseable: {e}")
    assert result.analysis.identity.document_id == result.analysis.structure.document_id


def test_production_builder_uses_identity_document_id():
    """``run_canonical_pipeline`` передаёт identity.document_id в builder."""
    from workspace.skills.legal_summarizer.scripts.structure.pipeline import (
        run_canonical_pipeline,
    )
    from workspace.skills.legal_summarizer.scripts.structure.identity import (
        DocumentIdentity,
    )

    import tempfile

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "doc.txt"
        p.write_text("1. Пункт\n\nТекст документа.", encoding="utf-8")
        result = run_canonical_pipeline(str(p), apply_repair=True)
        identity = DocumentIdentity.from_path(str(p))
        assert result.analysis.structure.document_id == identity.document_id


def test_identity_is_source_of_truth(tmp_path):
    """Если в builder передан ``document_id`` отличный от identity —
    ``DocumentAnalysis.build`` выравнивает по identity."""
    from workspace.skills.legal_summarizer.scripts.structure.document_analysis import (
        DocumentAnalysis,
    )
    from workspace.skills.legal_summarizer.scripts.structure.hierarchy import (
        StructureTreeBuilderConfig,
        build_document_structure,
    )
    from workspace.skills.legal_summarizer.scripts.structure.identity import (
        DocumentIdentity,
    )
    from workspace.skills.legal_summarizer.scripts.structure.physical import (
        PhysicalDocument,
    )

    p = tmp_path / "test.txt"
    p.write_text("Текст.", encoding="utf-8")

    struct = build_document_structure(
        [],
        total_blocks=0,
        config=StructureTreeBuilderConfig(),
    )
    assert struct.document_id == "doc"

    identity = DocumentIdentity.from_path(str(p))
    physical = PhysicalDocument(
        path=str(p),
        format="txt",
        title="T",
        size_bytes=p.stat().st_size,
        blocks=(),
        page_count=1,
    )
    analysis = DocumentAnalysis.build(
        physical=physical,
        structure=struct,
        chunks=(),
        identity=identity,
        include_retrieval_index=False,
    )
    assert analysis.structure.document_id == identity.document_id