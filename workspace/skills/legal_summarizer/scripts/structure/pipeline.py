"""DocumentStructure как SoT для всех downstream'ов (PLAN §45, Этап 19).

Этот модуль — **точка сборки** canonical pipeline:

    file → DocumentLoader → DocumentIdentity → DocumentStructure
        → repair → validate → ChunkPlanner → DocumentAnalysis
        → ExecutionPlan → batch execution → ...

Все компоненты принимают ``DocumentStructure`` как вход; не делают
повторных определений heading/numbering/etc.

Canonical pipeline — единственный production path. Legacy API
(``SectionTree``, ``DocumentSection``, ``build_section_tree``) удалены.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.document_analysis import (
    DocumentAnalysis,
)
from workspace.skills.legal_summarizer.scripts.structure.document_chunker import (
    ChunkPlanner,
)
from workspace.skills.legal_summarizer.scripts.structure.document_loader import (
    DocumentLoader,
)
from workspace.skills.legal_summarizer.scripts.structure.heading import (
    detect_heading_candidates,
)
from workspace.skills.legal_summarizer.scripts.structure.hierarchy import (
    StructureTreeBuilderConfig,
    build_document_structure,
)
from workspace.skills.legal_summarizer.scripts.structure.identity import (
    DocumentIdentity,
)
from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentStructure,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    PhysicalDocument,
)
from workspace.skills.legal_summarizer.scripts.structure.repair import (
    repair_structure,
)
from workspace.skills.legal_summarizer.scripts.structure.title import (
    resolve_title,
)
from workspace.skills.legal_summarizer.scripts.structure.validation import (
    ValidationReport, validate_structure,
)


@dataclass(frozen=True)
class PipelineResult:
    """Полный результат canonical pipeline."""

    analysis: DocumentAnalysis
    validation: ValidationReport
    chunks: tuple[Chunk, ...]


def run_canonical_pipeline(
    path: str | Path,
    *,
    text: str | None = None,
    apply_repair: bool = True,
    include_retrieval_index: bool = True,
    workspace_root: Path | str | None = None,
) -> PipelineResult:
    """Запустить canonical pipeline.

    Args:
        path: путь к документу.
        text: полный текст (для fallback title resolution).
        apply_repair: применить repair pass (PLAN §15).
        include_retrieval_index: построить inverted index (PLAN §36).
        workspace_root: корень workspace.

    Returns:
        ``PipelineResult`` с ``DocumentAnalysis``, ``ValidationReport``,
        и ``chunks``.
    """
    loader = DocumentLoader()
    physical = loader.load(path, workspace_root=workspace_root)
    identity = DocumentIdentity.from_path(physical.path)

    candidates = detect_heading_candidates(
        physical.blocks, pdf_path=str(path) if str(path).endswith(".pdf") else None,
        physical_doc=physical,
    )
    struct = build_document_structure(
        candidates,
        total_blocks=len(physical.blocks),
        config=StructureTreeBuilderConfig(document_id=identity.document_id),
    )
    title = resolve_title(physical, text=text)
    if title is not None:
        struct = DocumentStructure(
            document_id=struct.document_id,
            title=title,
            nodes=struct.nodes,
            root_id=struct.root_id,
            preamble_node_id=struct.preamble_node_id,
            numbering=struct.numbering,
            total_blocks=struct.total_blocks,
            coverage_ratio=struct.coverage_ratio,
        )

    if apply_repair:
        struct, _ = repair_structure(struct)

    validation = validate_structure(struct, physical)

    planner = ChunkPlanner()
    chunks = tuple(planner.plan(physical, struct))

    analysis = DocumentAnalysis.build(
        physical=physical,
        structure=struct,
        chunks=chunks,
        identity=identity,
        include_retrieval_index=include_retrieval_index,
    )

    return PipelineResult(
        analysis=analysis,
        validation=validation,
        chunks=chunks,
    )


__all__ = ["PipelineResult", "run_canonical_pipeline"]