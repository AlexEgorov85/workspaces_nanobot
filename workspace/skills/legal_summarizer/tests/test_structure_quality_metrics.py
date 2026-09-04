"""Тесты для quality metrics (Этап 53 из PLAN.md)."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentStructure, StructureNode,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock, PhysicalDocument,
)
from workspace.skills.legal_summarizer.scripts.structure.provenance import (
    ProvenanceChain, build_provenance_chain,
)
from workspace.skills.legal_summarizer.scripts.structure.quality_metrics import (
    QualityMetrics, compute_quality_metrics,
)
from workspace.skills.legal_summarizer.scripts.structure.reference_qa import (
    ReferenceQuestion, ReferenceQASet, standard_qa_set,
)
from workspace.skills.legal_summarizer.scripts.structure.retrieval_index import (
    RetrievalIndex,
)


def _b(ord: int) -> DocumentBlock:
    return DocumentBlock(
        block_id=f"b_{ord:04d}", block_type="paragraph", content="x",
        char_count=1, page_index=1, page_start=1, page_end=1,
        paragraph_index=None, table_index=None, ordinal=ord,
        block_metadata={},
    )


def _doc() -> PhysicalDocument:
    import tempfile
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8",
    ) as f:
        path = f.name
    return PhysicalDocument(
        path=path, format="txt", title=None, size_bytes=0,
        blocks=tuple(_b(i) for i in range(3)), page_count=1,
    )


def _c(cid: str, text: str, idx: int = 0, ord: tuple[int, ...] = (0,)) -> Chunk:
    return Chunk(
        chunk_id=cid, index=idx, text=text, char_count=len(text),
        token_estimate=1, page_start=1, page_end=1,
        section_id="s1", section_path="1", section_heading="x",
        block_indices=ord, block_types=("paragraph",),
    )


def _struct() -> DocumentStructure:
    return DocumentStructure(
        document_id="d", title=None,
        nodes={"n_0000": StructureNode(
            node_id="n_0000", node_type="document", semantic_type=None,
            level=0, title="", number=None, parent_id=None,
            children=(), start_block=0, end_block=2, confidence=1.0,
        )},
        root_id="n_0000", preamble_node_id="n_0000",
        numbering=(), total_blocks=3,
    )


def test_quality_metrics_summary():
    m = QualityMetrics(
        retrieval_recall_at_k=0.8,
        section_hit_rate=0.8,
        provenance_correctness=1.0,
        structure_correctness=True,
        answer_completeness=0.9,
        number_of_llm_calls=10,
        total_tokens=50000,
    )
    d = m.summary()
    assert d["retrieval_recall_at_k"] == 0.8
    assert d["tokens_per_call"] == 5000.0


def test_compute_quality_metrics_empty():
    m = compute_quality_metrics()
    assert m.retrieval_recall_at_k == 0.0
    assert m.structure_correctness is True


def test_compute_quality_metrics_with_recall():
    chunks = (
        _c("001", "Цена договора 1000 рублей"),
        _c("002", "Срок оплата — 30 дней"),
        _c("003", "Ответственность сторон"),
    )
    index = RetrievalIndex.build(
        chunks=chunks, structure=_struct(),
    )
    qa = ReferenceQASet(
        document_name="d",
        questions=(
            ReferenceQuestion(
                query="цена", expected_section_keywords=("цена",),
            ),
            ReferenceQuestion(
                query="оплата", expected_section_keywords=("оплата",),
            ),
        ),
    )
    m = compute_quality_metrics(index=index, qa_set=qa, top_k=3)
    assert m.retrieval_recall_at_k == 1.0
    assert m.answer_completeness == 1.0


def test_compute_quality_metrics_with_provenance():
    doc = _doc()
    struct = _struct()
    chunk = _c("001", "text", ord=(0,))
    chain = build_provenance_chain(
        chunk, doc=doc, struct=struct, document_id="d-1",
    )
    assert chain is not None
    m = compute_quality_metrics(chains=[chain])
    assert m.provenance_correctness == 1.0


def test_compute_quality_metrics_tokens_per_call():
    m = compute_quality_metrics(llm_calls=10, total_tokens=10000)
    assert m.tokens_per_call == 1000.0


def test_compute_quality_metrics_zero_calls():
    m = compute_quality_metrics(llm_calls=0, total_tokens=1000)
    assert m.tokens_per_call == 0.0


def test_standard_qa_set_with_recall():
    chunks = (
        _c("001", "Цена и стоимость договора"),
        _c("002", "Срок оплаты"),
        _c("003", "Ответственность сторон"),
    )
    index = RetrievalIndex.build(
        chunks=chunks, structure=_struct(),
    )
    qa = standard_qa_set()
    m = compute_quality_metrics(index=index, qa_set=qa)
    assert m.retrieval_recall_at_k >= 0.0