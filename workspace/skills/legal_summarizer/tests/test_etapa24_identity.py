"""Этап 24: Document identity / document_id invariants.

Инварианты:
- identity.document_id == structure.document_id (через DocumentAnalysis.build).
- structure.document_id передаётся из pipeline через config.
- make_operation_id детерминирован для одного документа.
- make_operation_id стабилен при повторных вызовах.
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


def test_identity_matches_structure(tmp_path):
    """identity.document_id == structure.document_id после DocumentAnalysis.build."""
    from workspace.skills.legal_summarizer.scripts.structure.document_analysis import (
        DocumentAnalysis,
    )
    from workspace.skills.legal_summarizer.scripts.structure.identity import (
        DocumentIdentity,
    )
    from workspace.skills.legal_summarizer.scripts.structure.physical import (
        PhysicalDocument,
    )
    from workspace.skills.legal_summarizer.scripts.structure.hierarchy import (
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
    import summarizer

    text = "Тестовый документ."
    op1 = summarizer.make_operation_id(text, "brief")
    op2 = summarizer.make_operation_id(text, "brief")
    assert op1 == op2


def test_make_operation_id_stable(tmp_path):
    """make_operation_id стабилен при повторных вызовах."""
    import summarizer

    text = "Договор аренды помещения."
    ops = [summarizer.make_operation_id(text, "detailed") for _ in range(10)]
    assert len(set(ops)) == 1


def test_make_operation_id_differs_by_length(tmp_path):
    """make_operation_id различается для разных length."""
    import summarizer

    text = "Договор аренды."
    op_brief = summarizer.make_operation_id(text, "brief")
    op_detailed = summarizer.make_operation_id(text, "detailed")
    assert op_brief != op_detailed
