"""Тесты #1: roundtrip-сериализация для Chunk, StructureNode,
DocumentStructure, ValidationReport, ValidationIssue.

Используется при восстановлении DocumentAnalysis из document-level cache
(``cache.manifest.read_document_snapshot``). Каждый ``to_dict`` → ``from_dict``
должен вернуть эквивалентный объект.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def test_chunk_roundtrip_minimal():
    """Минимальный Chunk без optional полей → roundtrip."""
    from chunking.chunks import Chunk

    chunk = Chunk(
        chunk_id="001",
        index=0,
        text="hello world",
        char_count=11,
        token_estimate=3,
        page_start=1,
        page_end=2,
        section_id="s_0001",
        section_path="1",
        section_heading="Раздел 1",
        block_indices=(0, 1),
        block_types=("paragraph", "paragraph"),
    )
    data = chunk.to_dict()
    restored = Chunk.from_dict(data)
    assert restored == chunk


def test_chunk_roundtrip_full():
    """Chunk со всеми optional полями (table_id, source_spans, target_*) → roundtrip."""
    from chunking.chunks import Chunk

    chunk = Chunk(
        chunk_id="042",
        index=5,
        text="содержимое таблицы",
        char_count=100,
        token_estimate=30,
        page_start=10,
        page_end=11,
        section_id="s_0005",
        section_path="2 > 2.3",
        section_heading="Подраздел",
        block_indices=(7, 8, 9),
        block_types=("table", "table", "table"),
        table_id="t_001",
        table_row_start=1,
        table_row_end=5,
        source_char_start=0,
        source_char_end=100,
        target_block_indices=(7, 8),
        target_source_char_start=0,
        target_source_char_end=50,
        source_spans=(
            (7, 0, 50, 1),
            (8, 0, 50, 0),
        ),
        section_ids=("s_0005", "s_0006"),
    )
    data = chunk.to_dict()
    restored = Chunk.from_dict(data)
    assert restored == chunk
    # Дополнительные проверки типов кортежей.
    assert isinstance(restored.source_spans, tuple)
    assert restored.source_spans[0] == (7, 0, 50, 1)
    assert isinstance(restored.block_indices, tuple)
    assert restored.block_indices == (7, 8, 9)


def test_chunk_roundtrip_legacy_source_spans_as_tuples():
    """Legacy формат (list of tuples) тоже принимается."""
    from chunking.chunks import Chunk

    chunk = Chunk(
        chunk_id="010",
        index=0,
        text="x",
        char_count=1,
        token_estimate=1,
        page_start=None,
        page_end=None,
        section_id="s_root",
        section_path="",
        section_heading="",
        block_indices=(0,),
        block_types=("paragraph",),
    )
    data = chunk.to_dict()
    # Подменяем на legacy формат.
    data["source_spans"] = [[1, 2, 3, 1]]
    restored = Chunk.from_dict(data)
    assert restored.source_spans == ((1, 2, 3, 1),)


def test_chunk_roundtrip_optional_none():
    """Chunk где optional поля = None (источник/provenance не заданы) → roundtrip."""
    from chunking.chunks import Chunk

    chunk = Chunk(
        chunk_id="005",
        index=0,
        text="abc",
        char_count=3,
        token_estimate=1,
        page_start=None,
        page_end=None,
        section_id="",
        section_path="",
        section_heading="",
        block_indices=(),
        block_types=(),
        table_id=None,
        table_row_start=None,
        table_row_end=None,
    )
    data = chunk.to_dict()
    restored = Chunk.from_dict(data)
    assert restored == chunk
    assert restored.table_id is None
    assert restored.page_start is None


def test_structure_node_roundtrip():
    from document.structure import (
        DocumentTitle,
        NumberingInfo,
        StructureEvidence,
        StructureNode,
    )

    node = StructureNode(
        node_id="n_0001",
        node_type="section",
        semantic_type="article",
        level=2,
        title="Статья 12. Обязанности",
        number=NumberingInfo(
            raw="12", scheme="legal_article", components=(12,),
            level=1, ordinal=12,
        ),
        parent_id="n_0000",
        children=("n_0002", "n_0003"),
        start_block=10,
        end_block=20,
        confidence=0.95,
        evidence=(
            StructureEvidence(source="docx_style", weight=0.8, detail="bold"),
            StructureEvidence(source="legal_numbering", weight=0.9),
        ),
        source_refs=("pdf_outline",),
    )
    data = node.to_dict()
    restored = StructureNode.from_dict(data)
    assert restored == node


def test_document_structure_roundtrip():
    from document.structure import (
        DocumentStructure,
        DocumentTitle,
        NumberingInfo,
        StructureNode,
    )

    root = StructureNode(
        node_id="n_0000",
        node_type="root",
        semantic_type=None,
        level=0,
        title="",
        number=None,
        parent_id=None,
        children=("n_0001",),
        start_block=0,
        end_block=2,
        confidence=1.0,
    )
    child = StructureNode(
        node_id="n_0001",
        node_type="section",
        semantic_type="article",
        level=1,
        title="Статья 1",
        number=NumberingInfo(
            raw="1", scheme="legal_article", components=(1,),
            level=1, ordinal=1,
        ),
        parent_id="n_0000",
        children=(),
        start_block=0,
        end_block=2,
        confidence=0.9,
    )
    struct = DocumentStructure(
        document_id="d_test",
        title=DocumentTitle(
            value="Тестовый договор",
            source="metadata",
            confidence=1.0,
            block_ordinal=None,
        ),
        nodes={"n_0000": root, "n_0001": child},
        root_id="n_0000",
        preamble_node_id="n_0000",
        numbering=(child.number,),
        total_blocks=3,
        coverage_ratio=1.0,
    )
    data = struct.to_dict()
    restored = DocumentStructure.from_dict(data)
    assert restored == struct
    # Проверяем что nodes.keys() идентичны.
    assert set(restored.nodes.keys()) == set(struct.nodes.keys())
    # Каждый node идентичен.
    for nid in struct.nodes:
        assert restored.nodes[nid] == struct.nodes[nid]


def test_document_structure_roundtrip_no_title():
    """DocumentStructure без title → roundtrip."""
    from document.structure import (
        DocumentStructure,
        StructureNode,
    )

    root = StructureNode(
        node_id="n_0000",
        node_type="root",
        semantic_type=None,
        level=0,
        title="",
        number=None,
        parent_id=None,
        children=(),
        start_block=0,
        end_block=0,
        confidence=1.0,
    )
    struct = DocumentStructure(
        document_id="d",
        title=None,
        nodes={"n_0000": root},
        root_id="n_0000",
        preamble_node_id="n_0000",
        numbering=(),
        total_blocks=1,
        coverage_ratio=0.0,
    )
    data = struct.to_dict()
    restored = DocumentStructure.from_dict(data)
    assert restored == struct
    assert restored.title is None


def test_validation_report_roundtrip_empty():
    """Пустой ValidationReport (нет issues, is_valid=True) → roundtrip."""
    from document.validation import ValidationReport

    report = ValidationReport()
    data = report.to_dict()
    restored = ValidationReport.from_dict(data)
    assert restored == report
    assert restored.is_valid is True
    assert restored.issues == ()


def test_validation_report_roundtrip_with_issues():
    """ValidationReport с issues → roundtrip."""
    from document.validation import (
        ValidationIssue,
        ValidationReport,
    )

    report = ValidationReport(
        issues=(
            ValidationIssue(
                kind="low_coverage",
                detail="only 30% of blocks covered",
                node_id=None,
            ),
            ValidationIssue(
                kind="cycle",
                detail="parent chain contains cycle starting at n_0005",
                node_id="n_0005",
            ),
        ),
        coverage_ratio=0.3,
        is_valid=False,
    )
    data = report.to_dict()
    restored = ValidationReport.from_dict(data)
    assert restored == report
    assert restored.is_valid is False
    assert len(restored.issues) == 2
    assert restored.issues[1].node_id == "n_0005"