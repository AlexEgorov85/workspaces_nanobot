"""DocumentStructure как SoT для всех downstream'ов.

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

from chunking.chunks import Chunk
from document.analysis import (
    DocumentAnalysis,
)
from chunking.chunker import (
    ChunkPlanner,
)
from document.loader import (
    DocumentLoader,
)
from document.heading import (
    detect_heading_candidates,
)
from document.hierarchy import (
    StructureTreeBuilderConfig,
    build_document_structure,
)
from document.identity import (
    DocumentIdentity,
)
from document.structure import (
    DocumentStructure,
)
from document.physical import (
    PhysicalDocument,
)
from document.repair import (
    repair_structure,
)
from document.title import (
    resolve_title,
)
from document.validation import (
    ValidationReport, validate_structure,
)


def _read_context_window_tokens() -> int | None:
    """Прочитать ``contextWindowTokens`` из SETTINGS.

    Источник: ``config.json::agents.defaults.contextWindowTokens`` (или
    override в project.json через ``agents.defaults.contextWindowTokens``).

    Returns:
        int или None если ключ отсутствует / не парсится.
    """
    try:
        from config import SETTINGS
    except Exception:
        return None
    try:
        raw = (
            SETTINGS.get("agents", {})
            .get("defaults", {})
            .get("contextWindowTokens")
        )
    except Exception:
        return None
    if raw is None or raw == "":
        return None
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


@dataclass(frozen=True)
class PipelineResult:
    """Полный результат canonical pipeline."""

    analysis: DocumentAnalysis
    validation: ValidationReport
    chunks: tuple[Chunk, ...]


def _try_load_cached_pipeline_result(
    *,
    path: str | Path,
    workspace_root: Path | str | None,
    session_key: str = "default",
) -> PipelineResult | None:
    """Попробовать загрузить cached ``PipelineResult`` из document-level cache.

    Условия cache hit:
      * ``workspace_root`` не None (для path resolution);
      * файл существует и ``DocumentIdentity.is_fresh(path) == True``
        (дешёвая проверка: stat + сравнение mtime_ns/size);
      * snapshot complete (есть ``_complete.marker``).

    При hit восстанавливает ``PhysicalDocument``, ``DocumentStructure``,
    ``Chunk[]``, ``ValidationReport`` из их ``to_dict``. ``RetrievalIndex``
    пересобирается заново (детерминированно из chunks+structure).

    Returns:
        ``PipelineResult`` или ``None`` при miss.
    """
    if workspace_root is None:
        return None

    try:
        identity = DocumentIdentity.from_path(path)
    except (FileNotFoundError, OSError):
        return None

    if not identity.is_fresh(path):
        # mtime/size изменились — инвалидируем старый snapshot.
        from cache.manifest import (
            invalidate_document_cache,
            is_document_cache_complete,
            read_document_snapshot,
        )
        if is_document_cache_complete(
            identity.document_id, workspace_root, session_key,
        ):
            invalidate_document_cache(
                identity.document_id, workspace_root, session_key,
            )
        return None

    from cache.manifest import (
        is_document_cache_complete,
        read_document_snapshot,
    )

    if not is_document_cache_complete(identity.document_id, workspace_root, session_key):
        return None

    snap = read_document_snapshot(identity.document_id, workspace_root, session_key)
    if snap is None:
        return None
    physical_data, analysis_data, _meta = snap
    if physical_data is None or analysis_data is None:
        return None

    try:
        physical = PhysicalDocument.from_dict(physical_data)
        structure = DocumentStructure.from_dict(analysis_data["structure"])
        validation = ValidationReport.from_dict(
            analysis_data.get("validation") or {},
        )
        chunks = tuple(Chunk.from_dict(c) for c in analysis_data["chunks"])
    except (KeyError, TypeError, ValueError):
        # Битый snapshot — инвалидируем и cache miss.
        from cache.manifest import invalidate_document_cache
        invalidate_document_cache(
            identity.document_id, workspace_root, session_key,
        )
        return None

    analysis = DocumentAnalysis.build(
        physical=physical,
        structure=structure,
        chunks=chunks,
        identity=identity,
        include_retrieval_index=True,
        semantic_records={},
    )

    return PipelineResult(
        analysis=analysis,
        validation=validation,
        chunks=chunks,
    )


def _write_document_snapshot_after_pipeline(
    *,
    path: str | Path,
    workspace_root: Path | str | None,
    physical: PhysicalDocument,
    identity: DocumentIdentity,
    structure: DocumentStructure,
    validation: ValidationReport,
    chunks: tuple[Chunk, ...],
    analysis: DocumentAnalysis,
    session_key: str = "default",
) -> None:
    """Сохранить document-level snapshot после успешного canonical pipeline.

    Используется только при cache miss. При cache hit snapshot
    уже существует и write_document_snapshot выбросит ``RuntimeError`` —
    мы это явно НЕ вызываем в hit-ветке.
    """
    if workspace_root is None:
        return
    from cache.manifest import write_document_snapshot

    analysis_payload = {
        "version": 1,
        "document_id": identity.document_id,
        "structure": structure.to_dict(),
        "chunks": [c.to_dict() for c in chunks],
        "validation": validation.to_dict(),
    }
    retrieval_meta: dict[str, Any] | None = None
    if analysis.retrieval_index is not None:
        retrieval_meta = {
            "chunk_count": len(analysis.retrieval_index.chunks),
            "term_count": len(analysis.retrieval_index.term_to_chunks),
        }

    try:
        write_document_snapshot(
            workspace_root=workspace_root,
            document_id=identity.document_id,
            physical_data=physical.to_dict(),
            analysis_data=analysis_payload,
            retrieval_index_meta=retrieval_meta,
            session_key=session_key,
        )
    except RuntimeError:
        # Уже complete (конкурентная запись или race) — это OK, ничего не делаем.
        pass


def run_canonical_pipeline(
    path: str | Path,
    *,
    text: str | None = None,
    apply_repair: bool = True,
    include_retrieval_index: bool = True,
    workspace_root: Path | str | None = None,
    session_key: str = "default",
) -> PipelineResult:
    """Запустить canonical pipeline.

    При наличии document-level cache — попытка cache hit:
    если файл не менялся (mtime/size) и snapshot complete — возвращаем
    восстановленный ``PipelineResult`` без повторного парсинга PDF/DOCX,
    heading detection, structure build, ChunkPlanner.

    При cache miss — полный pipeline + запись snapshot в конце.

    Args:
        path: путь к документу.
        text: полный текст (для fallback title resolution).
        apply_repair: применить repair pass.
        include_retrieval_index: построить inverted index.
        workspace_root: корень workspace.
        session_key: ключ сессии для session-scoped cache-пути.

    Returns:
        ``PipelineResult`` с ``DocumentAnalysis``, ``ValidationReport``,
        и ``chunks``.
    """
    # cache hit branch.
    cached = _try_load_cached_pipeline_result(
        path=path, workspace_root=workspace_root, session_key=session_key,
    )
    if cached is not None:
        return cached

    # Cache miss — existing pipeline.
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

    from chunking.chunker import (
        ChunkPlanner,
        DocumentStructureChunkerConfig,
        build_chunk_config_from_runtime,
    )
    context_window_tokens = _read_context_window_tokens()
    chunker_config = DocumentStructureChunkerConfig(
        chunk_config=build_chunk_config_from_runtime(
            context_window_tokens=context_window_tokens,
        ),
    )
    planner = ChunkPlanner(config=chunker_config)
    chunks = tuple(planner.plan(physical, struct))

    analysis = DocumentAnalysis.build(
        physical=physical,
        structure=struct,
        chunks=chunks,
        identity=identity,
        include_retrieval_index=include_retrieval_index,
    )

    # write snapshot после успешного pipeline (cache miss).
    _write_document_snapshot_after_pipeline(
        path=path,
        workspace_root=workspace_root,
        physical=physical,
        identity=identity,
        structure=struct,
        validation=validation,
        chunks=chunks,
        analysis=analysis,
        session_key=session_key,
    )

    return PipelineResult(
        analysis=analysis,
        validation=validation,
        chunks=chunks,
    )


__all__ = ["PipelineResult", "run_canonical_pipeline"]
