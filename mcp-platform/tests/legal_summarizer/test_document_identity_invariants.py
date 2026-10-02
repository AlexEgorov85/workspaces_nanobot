"""Document identity / document_id invariants.

Инварианты:
- identity.document_id == structure.document_id (через DocumentAnalysis.build).
- structure.document_id передаётся из pipeline через config.
- make_operation_id детерминирован для одного документа.
- make_operation_id стабилен при повторных вызовах.
"""

from __future__ import annotations

from pathlib import Path


def _write_doc(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "doc.txt"
    p.write_text(text, encoding="utf-8")
    return p

def test_identity_matches_structure(tmp_path):
    """identity.document_id == structure.document_id после DocumentAnalysis.build."""
    from libs.legal_summarizer.document.analysis import (
        DocumentAnalysis,
    )
    from libs.legal_summarizer.document.identity import (
        DocumentIdentity,
    )
    from libs.legal_summarizer.document.physical import (
        PhysicalDocument,
    )
    from libs.legal_summarizer.document.hierarchy import (
        build_document_structure,
    )

    text = "Тестовый документ."
    p = _write_doc(tmp_path, text)
    identity = DocumentIdentity.from_path(str(p))
    physical = PhysicalDocument(
        path=str(p), format="txt", title="T",
        size_bytes=p.stat().st_size, blocks=(), page_count=1,
    )
    structure = build_document_structure([], total_blocks=0, document_id=identity.document_id)
    analysis = DocumentAnalysis.build(
        physical=physical,
        structure=structure,
        chunks=(),
        identity=identity,
    )
    assert analysis.identity.document_id == analysis.structure.document_id

def test_make_operation_id_deterministic(tmp_path):
    """make_operation_id детерминирован для одного входа."""
    import libs.legal_summarizer.application.service as summarizer
    text = "Тестовый документ."
    op1 = summarizer.make_operation_id(text, "brief")
    op2 = summarizer.make_operation_id(text, "brief")
    assert op1 == op2

def test_make_operation_id_stable(tmp_path):
    """make_operation_id стабилен при повторных вызовах."""
    import libs.legal_summarizer.application.service as summarizer
    text = "Договор аренды помещения."
    ops = [summarizer.make_operation_id(text, "detailed") for _ in range(10)]
    assert len(set(ops)) == 1

def test_make_operation_id_differs_by_length(tmp_path):
    """make_operation_id различается для разных length."""
    import libs.legal_summarizer.application.service as summarizer
    text = "Договор аренды."
    op_brief = summarizer.make_operation_id(text, "brief")
    op_detailed = summarizer.make_operation_id(text, "detailed")
    assert op_brief != op_detailed
