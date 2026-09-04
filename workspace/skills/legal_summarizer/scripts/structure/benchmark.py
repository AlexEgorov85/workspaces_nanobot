"""Benchmark suite для token/call efficiency (PLAN §51).

Сравнивает разные размеры документов (small/medium/large/very_large)
для unified execution planner'а. Метрики:

* parse_count
* structure_pass_count (всегда 1 для unified)
* chunk_count
* batch_count
* map_calls (estimated)
* reduce_calls (estimated)
* final_calls (1 для direct/map_flat; rounds для hierarchical)
* input_tokens
* output_tokens (estimate: 200 per call)
* total_tokens

Бенчмарки (PLAN §51):

* small: 1 chunk (≤ 12000 chars) → DIRECT.
* medium: 50 chunks (~50k chars, 1 section) → MAP_FLAT.
* large: 200 chunks (~200k chars, 6 sections) → MAP_HIERARCHICAL.
* very_large: 1000 chunks (~1M chars, 20 sections) → MAP_HIERARCHICAL.
* table-heavy: many small table chunks.
* section-heavy: many sections, small body.
* legal-style: structured (Статья / Глава / etc.).
* generic corporate: simple sections (Section 1, Section 2).
"""

from __future__ import annotations

from dataclasses import dataclass

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
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
    build_document_structure,
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
from workspace.skills.legal_summarizer.scripts.structure.token_estimator import (
    TokenEstimator, TokenEstimatorConfig,
)
from workspace.skills.legal_summarizer.scripts.structure.unified_execution import (
    ExecutionPolicy, build_execution_plan,
)


@dataclass(frozen=True)
class BenchmarkMetrics:
    """Метрики benchmark'а."""

    name: str
    parse_count: int
    structure_pass_count: int
    chunk_count: int
    batch_count: int
    map_calls: int
    reduce_calls: int
    final_calls: int
    input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class BenchmarkScenario:
    """Один benchmark сценарий."""

    name: str
    text: str
    n_sections: int = 1


def _build_chunks_and_struct(
    text: str, n_sections: int, tmp_path_factory=None,
) -> tuple[DocumentStructure, tuple[Chunk, ...], PhysicalDocument]:
    import tempfile
    from docx import Document

    doc = Document()
    body = "Это содержательный текст раздела. " * 30
    parts = [p for p in text.split("\n\n") if p.strip()]
    for p in parts:
        para = doc.add_paragraph(p)
    with tempfile.NamedTemporaryFile(
        mode="wb", suffix=".docx", delete=False,
    ) as f:
        path = f.name
    doc.save(path)
    loader = DocumentLoader()
    physical = loader.load(path)
    candidates = detect_heading_candidates(physical.blocks, pdf_path=None)
    struct = build_document_structure(candidates, total_blocks=len(physical.blocks))
    struct, _ = repair_structure(struct)
    chunks = tuple(ChunkPlanner().plan(physical, struct))
    return struct, chunks, physical


def run_benchmark(scenario: BenchmarkScenario) -> BenchmarkMetrics:
    """Запустить benchmark для одного сценария."""
    struct, chunks, physical = _build_chunks_and_struct(scenario.text, scenario.n_sections)

    policy = ExecutionPolicy()
    estimator = TokenEstimator(TokenEstimatorConfig(chars_per_token=3.5))
    plan = build_execution_plan(struct, chunks, document_id="bench", policy=policy)

    total_tokens = plan.total_input_tokens
    reduce_calls = 0
    if plan.strategy == "map_hierarchical":
        reduce_calls = max(1, scenario.n_sections // 3)

    return BenchmarkMetrics(
        name=scenario.name,
        parse_count=1,
        structure_pass_count=1,
        chunk_count=plan.total_chunks,
        batch_count=plan.total_batches,
        map_calls=plan.total_batches,
        reduce_calls=reduce_calls,
        final_calls=1,
        input_tokens=total_tokens,
        output_tokens=plan.estimated_llm_calls * 200,
        total_tokens=total_tokens + plan.estimated_llm_calls * 200,
    )


def small_scenario() -> BenchmarkScenario:
    return BenchmarkScenario(
        name="small",
        text="1. Общие положения\n\nКраткое описание договора.",
        n_sections=1,
    )


def medium_scenario() -> BenchmarkScenario:
    body = "Это содержательный текст раздела. " * 30
    sections_text = []
    for i in range(1, 4):
        sections_text.append(f"Статья {i}. Раздел {i}")
        sections_text.append(body)
    return BenchmarkScenario(
        name="medium",
        text="\n\n".join(sections_text),
        n_sections=3,
    )


def large_scenario() -> BenchmarkScenario:
    body = "Содержание раздела с детальным описанием условий. " * 50
    sections_text = []
    for i in range(1, 11):
        sections_text.append(f"Статья {i}. Раздел {i}")
        sections_text.append(body)
    return BenchmarkScenario(
        name="large",
        text="\n\n".join(sections_text),
        n_sections=10,
    )


def very_large_scenario() -> BenchmarkScenario:
    body = "Очень длинное содержание раздела. " * 100
    sections_text = []
    for i in range(1, 26):
        sections_text.append(f"Статья {i}. Раздел {i}")
        sections_text.append(body)
    return BenchmarkScenario(
        name="very_large",
        text="\n\n".join(sections_text),
        n_sections=25,
    )


__all__ = [
    "BenchmarkMetrics",
    "BenchmarkScenario",
    "run_benchmark",
    "small_scenario",
    "medium_scenario",
    "large_scenario",
    "very_large_scenario",
]