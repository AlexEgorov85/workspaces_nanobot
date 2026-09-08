"""Smoke test для STRUCTURAL_PACKING_PLAN §9.

Создаёт синтетический документ с разнообразными секциями, прогоняет
новый chunker и печатает diagnostics.

Использование:
    python -m workspace.skills.legal_summarizer.tests.smoke_chunking_diagnostics
"""

from __future__ import annotations

import sys
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
_WORKSPACE_ROOT = _SKILL_ROOT.parent.parent.parent

for p in [str(_WORKSPACE_ROOT), str(_WORKSPACE_ROOT / "workspace"), str(_SCRIPTS_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


from chunking.chunker import (
    DocumentStructureChunkerConfig,
    chunk_from_structure_with_diagnostics,
)
from chunking.chunks import ChunkConfig
from document.physical import DocumentBlock, PhysicalDocument
from document.structure import DocumentStructure, StructureNode


def _b(ordinal: int, content: str) -> DocumentBlock:
    return DocumentBlock(
        block_id=f"b_{ordinal:04d}",
        block_type="paragraph",
        content=content,
        char_count=len(content),
        page_index=ordinal + 1,
        page_start=ordinal + 1,
        page_end=ordinal + 1,
        paragraph_index=None,
        table_index=None,
        ordinal=ordinal,
        block_metadata={},
    )


def _build_synthetic_doc() -> tuple[PhysicalDocument, DocumentStructure]:
    """Синтетический документ: 30 sections разных размеров, плюс tables."""
    blocks: list[DocumentBlock] = []
    nodes: dict[str, StructureNode] = {}
    ordinal = 0

    nodes["n_0000"] = StructureNode(
        node_id="n_0000",
        node_type="document",
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

    section_specs: list[tuple[str, str, int]] = [
        ("chapter", "Chapter 1", 5000),
        ("article", "Article 1", 3000),
        ("article", "Article 2", 4000),
        ("article", "Article 3", 3500),
        ("article", "Article 4", 2500),
        ("article", "Article 5", 4500),
        ("chapter", "Chapter 2", 6000),
        ("article", "Article 6", 3000),
        ("article", "Article 7", 3500),
        ("article", "Article 8", 4000),
        ("chapter", "Chapter 3", 7000),
        ("article", "Article 9", 3000),
        ("article", "Article 10", 3500),
        ("article", "Article 11", 4000),
        ("article", "Article 12", 3000),
        ("article", "Article 13", 4500),
        ("chapter", "Chapter 4", 5000),
        ("article", "Article 14", 3000),
        ("article", "Article 15", 3500),
        ("article", "Article 16", 4000),
        ("chapter", "Chapter 5", 5500),
        ("article", "Article 17", 3000),
        ("article", "Article 18", 3500),
        ("article", "Article 19", 4000),
        ("article", "Article 20", 3000),
        ("chapter", "Chapter 6", 6000),
        ("article", "Article 21", 3500),
        ("article", "Article 22", 3000),
        ("article", "Article 23", 4000),
        ("article", "Article 24", 3500),
    ]
    section_ids: list[str] = []
    for i, (sem_type, title, size) in enumerate(section_specs, start=1):
        nid = f"n_{i:04d}"
        section_ids.append(nid)
        blocks.append(_b(ordinal, "x" * size))
        nodes[nid] = StructureNode(
            node_id=nid,
            node_type="section",
            semantic_type=sem_type,
            level=1,
            title=title,
            number=None,
            parent_id="n_0000",
            children=(),
            start_block=ordinal,
            end_block=ordinal,
            confidence=0.9,
        )
        ordinal += 1

    blocks.append(
        DocumentBlock(
            block_id=f"b_{ordinal:04d}",
            block_type="table",
            content="col1 | col2\nval1 | val2\nval3 | val4",
            char_count=len("col1 | col2\nval1 | val2\nval3 | val4"),
            page_index=ordinal + 1,
            page_start=ordinal + 1,
            page_end=ordinal + 1,
            paragraph_index=None,
            table_index=0,
            ordinal=ordinal,
            block_metadata={"row_count": 3},
        )
    )
    ordinal += 1

    nodes["n_0000"] = StructureNode(
        node_id="n_0000",
        node_type="document",
        semantic_type=None,
        level=0,
        title="",
        number=None,
        parent_id=None,
        children=tuple(section_ids),
        start_block=0,
        end_block=ordinal - 1,
        confidence=1.0,
    )

    doc = PhysicalDocument(
        path="<synthetic>",
        format="txt",
        title="Synthetic Legal Doc",
        size_bytes=0,
        blocks=tuple(blocks),
        page_count=len(blocks),
    )
    struct = DocumentStructure(
        document_id="synth_nk",
        title=None,
        nodes=nodes,
        root_id="n_0000",
        preamble_node_id="n_0000",
        numbering=(),
        total_blocks=len(blocks),
    )
    return doc, struct


def main() -> None:
    doc, struct = _build_synthetic_doc()
    cfg = DocumentStructureChunkerConfig(
        chunk_config=ChunkConfig(
            max_chunk_chars=30000,
            chunk_overlap_chars=0,
            target_chunk_chars=20000,
            preferred_min_before_strong_boundary=0.7,
        ),
    )
    chunks, diag = chunk_from_structure_with_diagnostics(doc, struct, config=cfg)

    print("=" * 70)
    print("SYNTHETIC DOCUMENT CHUNKING — STATISTICS")
    print("=" * 70)
    print(f"Physical blocks:       {len(doc.blocks)}")
    print(f"Sections:              {diag.sections}")
    print(f"Special blocks:        {diag.special_blocks} (tables + oversized)")
    print(f"Unassigned blocks:     {diag.unassigned_blocks}")
    print(f"Structural units:      {diag.structural_chunks}")
    print(f"Chunks total:          {diag.chunks}")
    print("-" * 70)
    print(f"Avg chunk chars:       {diag.avg_chunk_chars:.0f}")
    print(f"Median chunk chars:    {diag.median_chunk_chars}")
    print(f"Min chunk chars:       {diag.min_chunk_chars}")
    print(f"Max chunk chars:       {diag.max_chunk_chars}")
    print("-" * 70)
    print(f"Small chunks (<5K):    {diag.small_chunks_count}")
    print(f"  total chars:         {diag.small_chunks_total_chars}")
    print(f"Multi-section chunks:  {diag.multi_section_chunks}")
    print(f"Table chunks:          {diag.table_chunks}")
    print(f"Oversized chunks:      {diag.oversized_chunks}")
    print("=" * 70)
    print("\nChunk breakdown:")
    for c in chunks:
        sec_ids = c.section_ids if c.section_ids else ()
        kind = "T" if c.table_id else ("O" if c.source_char_start is not None else "N")
        print(
            f"  [{kind}] {c.chunk_id}: {c.char_count:6d} chars, "
            f"blocks={c.block_indices}, "
            f"section_ids={sec_ids}"
        )


if __name__ == "__main__":
    main()
