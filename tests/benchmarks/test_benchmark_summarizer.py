"""Performance benchmark для детерминированной части pipeline.

Замеряет на representative документе (``C:\\Users\\Алексей\\Downloads\\
gkodeksrf.pdf``, ~5.7 MB ГК РФ):

* ``load_physical_document()`` — парсинг PDF в blocks.
* ``detect_sections()`` — определение section tree.
* ``StructureAwareChunker.chunk()`` — разбиение на chunks.
* ``pack_chunks()`` — упаковка в ContextBatch'и.
* ``compute_document_stats()`` — дешёвая статистика.
* ``compute_evidence_scoring()`` (heading detection) — эвристика.

**Без LLM** — только deterministic части. Реальный LLM-bench нестабилен
(зависит от network/latency/model); это уже покрывается e2e тестами.

Запуск::

    python -m pytest tests/benchmarks/test_benchmark_summarizer.py \\
        --benchmark-only  # если есть pytest-benchmark
    # или просто:
    python -m pytest tests/benchmarks/test_benchmark_summarizer.py -v

Если PDF не найден — тесты skip (graceful degradation).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "workspace" / "skills" / "legal_summarizer" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


_GK_PDF = Path(r"C:\Users\Алексей\Downloads\gkodeksrf.pdf")


@pytest.fixture(scope="module")
def gk_pdf_path() -> Path:
    """Путь к representative PDF. Skip тесты если не найден."""
    if not _GK_PDF.exists():
        pytest.skip(f"Representative PDF не найден: {_GK_PDF}")
    return _GK_PDF


@pytest.fixture(scope="module")
def gk_physical_document(gk_pdf_path):
    """Загруженный PhysicalDocument для benchmark'ов."""
    from workspace.skills.legal_summarizer.scripts.structure.physical import (
        load_physical_document,
    )
    return load_physical_document(gk_pdf_path)


# ---------------------------------------------------------------------------
# Benchmarks: deterministic pipeline
# ---------------------------------------------------------------------------


def test_benchmark_load_physical_document(gk_pdf_path):
    """Замер ``load_physical_document`` на ГК РФ."""
    from workspace.skills.legal_summarizer.scripts.structure.physical import (
        load_physical_document,
    )

    start = time.perf_counter()
    doc = load_physical_document(gk_pdf_path)
    elapsed = time.perf_counter() - start

    # Acceptance: парсинг 5.7 MB PDF должен занимать <30 секунд.
    assert elapsed < 30.0, (
        f"load_physical_document слишком медленный: {elapsed:.2f}s"
    )
    # Sanity: документ не пустой.
    assert doc.blocks


def test_benchmark_detect_sections(gk_physical_document):
    """Замер ``detect_sections`` на ГК РФ."""
    from workspace.skills.legal_summarizer.scripts.structure.sections import (
        detect_sections,
    )

    start = time.perf_counter()
    tree = detect_sections(gk_physical_document)
    elapsed = time.perf_counter() - start

    # Acceptance: detect_sections на большом документе — <60 секунд.
    assert elapsed < 60.0, (
        f"detect_sections слишком медленный: {elapsed:.2f}s"
    )
    assert tree.root_id


def test_benchmark_structure_chunking(gk_physical_document):
    """Замер ``StructureAwareChunker.chunk()``."""
    from workspace.skills.legal_summarizer.scripts.structure.chunks import (
        ChunkConfig,
        StructureAwareChunker,
    )
    from workspace.skills.legal_summarizer.scripts.structure.sections import (
        detect_sections,
    )

    tree = detect_sections(gk_physical_document)
    chunker = StructureAwareChunker()
    config = ChunkConfig(
        max_chunk_chars=5000, chunk_overlap_chars=0,
        table_chunk_threshold_chars=2000,
    )

    start = time.perf_counter()
    chunks = chunker.chunk(gk_physical_document, tree, config)
    elapsed = time.perf_counter() - start

    # Acceptance: chunking — <30 секунд.
    assert elapsed < 30.0, (
        f"StructureAwareChunker.chunk слишком медленный: {elapsed:.2f}s"
    )
    assert len(chunks) > 0


def test_benchmark_pack_chunks(gk_physical_document):
    """Замер ``pack_chunks`` на chunks ГК РФ."""
    from workspace.skills.legal_summarizer.scripts.packing import (
        pack_chunks,
    )
    from workspace.skills.legal_summarizer.scripts.structure.chunks import (
        ChunkConfig,
        StructureAwareChunker,
    )
    from workspace.skills.legal_summarizer.scripts.structure.sections import (
        detect_sections,
    )
    from workspace.skills.legal_summarizer.scripts.token_budget import (
        TokenBudget,
    )

    tree = detect_sections(gk_physical_document)
    chunker = StructureAwareChunker()
    config = ChunkConfig(
        max_chunk_chars=5000, chunk_overlap_chars=0,
        table_chunk_threshold_chars=2000,
    )
    chunks = chunker.chunk(gk_physical_document, tree, config)

    budget = TokenBudget(
        context_window_tokens=65536,
        system_prompt_tokens=1200,
        instruction_tokens=200,
        output_reserve_tokens=8192,
        safety_margin=0.85,
        chars_per_token=3.5,
    )

    start = time.perf_counter()
    batches = pack_chunks(chunks, budget)
    elapsed = time.perf_counter() - start

    # Acceptance: packing — <10 секунд для ~100 chunks.
    assert elapsed < 10.0, f"pack_chunks слишком медленный: {elapsed:.2f}s"
    assert len(batches) > 0


def test_benchmark_compute_document_stats(gk_physical_document):
    """Замер ``compute_document_stats``."""
    from workspace.skills.legal_summarizer.scripts.document_stats import (
        compute_document_stats,
    )
    from workspace.skills.legal_summarizer.scripts.structure.chunks import (
        ChunkConfig,
        StructureAwareChunker,
    )
    from workspace.skills.legal_summarizer.scripts.structure.sections import (
        detect_sections,
    )

    tree = detect_sections(gk_physical_document)
    chunker = StructureAwareChunker()
    config = ChunkConfig(
        max_chunk_chars=5000, chunk_overlap_chars=0,
        table_chunk_threshold_chars=2000,
    )
    chunks = chunker.chunk(gk_physical_document, tree, config)

    start = time.perf_counter()
    stats = compute_document_stats(gk_physical_document, tree, chunks)
    elapsed = time.perf_counter() - start

    # Acceptance: stats — <5 секунд (дешёвые метрики).
    assert elapsed < 5.0, (
        f"compute_document_stats слишком медленный: {elapsed:.2f}s"
    )
    assert stats.chars > 0


# ---------------------------------------------------------------------------
# Synthetic benchmarks — без зависимости от PDF
# ---------------------------------------------------------------------------


def test_benchmark_pipeline_synthetic_small_doc():
    """Synthetic benchmark на маленьком документе (100k chars).

    Замеряет полный deterministic pipeline (без LLM):
    blocks → sections → chunks → batches → stats.
    """
    from workspace.skills.legal_summarizer.scripts.document_stats import (
        compute_document_stats,
    )
    from workspace.skills.legal_summarizer.scripts.packing import (
        pack_chunks,
    )
    from workspace.skills.legal_summarizer.scripts.structure.chunks import (
        ChunkConfig,
        StructureAwareChunker,
    )
    from workspace.skills.legal_summarizer.scripts.structure.physical import (
        DocumentBlock,
        PhysicalDocument,
    )
    from workspace.skills.legal_summarizer.scripts.structure.sections import (
        detect_sections,
    )
    from workspace.skills.legal_summarizer.scripts.token_budget import (
        TokenBudget,
    )

    # Synthetic: 100 параграфов по 1k chars.
    paragraphs = [
        (
            f"{i+1}. Раздел {i+1}\n"
            + "Это содержание раздела, описывающее важные аспекты договора "
              "и обязательства сторон. " * 20
        )
        for i in range(100)
    ]
    blocks = tuple(
        DocumentBlock(
            block_id=f"b_{i:04d}",
            block_type="paragraph",
            content=p,
            char_count=len(p),
            page_index=1,
            page_start=1,
            page_end=1,
            paragraph_index=i,
            table_index=None,
            ordinal=i,
            block_metadata={},
        )
        for i, p in enumerate(paragraphs)
    )
    doc = PhysicalDocument(
        path="<synthetic>",
        format="txt",
        title=None,
        size_bytes=sum(len(p) for p in paragraphs),
        blocks=blocks,
        page_count=1,
    )

    start_total = time.perf_counter()
    tree = detect_sections(doc)
    chunker = StructureAwareChunker()
    config = ChunkConfig(
        max_chunk_chars=5000, chunk_overlap_chars=0,
        table_chunk_threshold_chars=2000,
    )
    chunks = chunker.chunk(doc, tree, config)
    budget = TokenBudget(
        context_window_tokens=65536,
        system_prompt_tokens=1200,
        instruction_tokens=200,
        output_reserve_tokens=8192,
        safety_margin=0.85,
        chars_per_token=3.5,
    )
    batches = pack_chunks(chunks, budget)
    stats = compute_document_stats(doc, tree, chunks)
    elapsed = time.perf_counter() - start_total

    # Acceptance: полный deterministic pipeline <5 секунд на синтетике.
    assert elapsed < 5.0, f"Synthetic pipeline слишком медленный: {elapsed:.2f}s"
    assert stats.chunks > 0
    assert len(batches) > 0


def test_benchmark_summary_metrics_reported(gk_physical_document):
    """Замер + метрики записываются (для ручного анализа)."""
    from workspace.skills.legal_summarizer.scripts.document_stats import (
        compute_document_stats,
    )
    from workspace.skills.legal_summarizer.scripts.structure.chunks import (
        ChunkConfig,
        StructureAwareChunker,
    )
    from workspace.skills.legal_summarizer.scripts.structure.sections import (
        detect_sections,
    )

    tree = detect_sections(gk_physical_document)
    chunker = StructureAwareChunker()
    config = ChunkConfig(
        max_chunk_chars=5000, chunk_overlap_chars=0,
        table_chunk_threshold_chars=2000,
    )
    chunks = chunker.chunk(gk_physical_document, tree, config)
    stats = compute_document_stats(gk_physical_document, tree, chunks)

    # Print метрик (pytest покажет в captured output при -v -s).
    print(f"\n[benchmark] ГК РФ representative document:")
    print(f"  chars={stats.chars}")
    print(f"  estimated_tokens={stats.estimated_tokens}")
    print(f"  pages={stats.pages}")
    print(f"  blocks={stats.blocks}")
    print(f"  sections={stats.sections}")
    print(f"  tables={stats.tables}")
    print(f"  chunks={stats.chunks}")
    print(f"  repeated_blocks={stats.repeated_blocks}")
    print(f"  blocks_per_section={stats.blocks_per_section:.2f}")
    print(f"  chars_per_block={stats.chars_per_block:.2f}")
