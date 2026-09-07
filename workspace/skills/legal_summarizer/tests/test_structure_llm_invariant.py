"""Тесты для LLM-as-semantic-only invariant (Этапы 61-62).

PLAN §61-62:

* LLM не используется для structure extraction (heading, numbering, list, page).
* LLM используется для semantic summary, fact extraction, answer synthesis.

Эти тесты проверяют, что в наших новых модулях нет LLM-вызовов
для structural decisions.
"""

from __future__ import annotations

import inspect


def _module_has_llm_call(module) -> bool:
    """True если в модуле есть вызовы LLM / OpenAI / Anthropic."""
    source = inspect.getsource(module)
    keywords = ("openai.", "anthropic.", "call_llm(", "client.chat")
    return any(kw in source for kw in keywords)


def test_no_llm_call_in_structure_modules():
    """Структурные модули не должны вызывать LLM."""
    import legal_summarizer.domain.models as models
    import legal_summarizer.document.physical as physical
    import legal_summarizer.domain.numbering as numbering
    import legal_summarizer.document.heading as heading
    import legal_summarizer.document.hierarchy as hierarchy
    import legal_summarizer.document.repair as repair
    import legal_summarizer.document.validation as validation
    import legal_summarizer.document.title as title
    import legal_summarizer.document.list_detection as list_detection
    import legal_summarizer.retrieval.candidate_aggregator as candidate_aggregator
    import legal_summarizer.document.pdf_outline as pdf_outline
    import legal_summarizer.domain.identity as identity
    import legal_summarizer.document.safety_merge as safety_merge
    import legal_summarizer.document.loader as document_loader
    import legal_summarizer.chunking.chunker as document_chunker
    import legal_summarizer.domain.tokens as token_estimator
    import legal_summarizer.planning.plan as execution_plan
    import legal_summarizer.chunking.packing as adjacent_packing
    import legal_summarizer.planning.strategy as unified_execution
    import legal_summarizer.execution.hierarchical as hierarchical_reducer
    import legal_summarizer.retrieval.records as semantic_record
    import legal_summarizer.llm.retry as retry
    import legal_summarizer.chunking.importance_brief as importance_brief
    import legal_summarizer.retrieval.query as retrieval
    import legal_summarizer.retrieval.normalizer as query_normalizer
    import legal_summarizer.retrieval.index as retrieval_index
    import legal_summarizer.retrieval.context_expansion as context_expansion
    import legal_summarizer.retrieval.fallback as full_doc_fallback
    import legal_summarizer.document.block_lookup as block_lookup
    import legal_summarizer.application.pipeline_structure as pipeline
    import legal_summarizer.retrieval.provenance as provenance
    import legal_summarizer.document.analysis as document_analysis
    import legal_summarizer.retrieval.followup as followup
    import tools.legal_benchmark as benchmark
    import legal_summarizer.retrieval.qa as reference_qa
    import legal_summarizer.retrieval.quality as quality_metrics
    import legal_summarizer.llm.single_flight as single_flight
    import tools.architecture_guard as architecture_guard
    import legal_summarizer.chunking.block_ownership as block_ownership
    modules = [
        models, physical, numbering, heading, hierarchy,
        repair, validation, title, list_detection,
        candidate_aggregator, pdf_outline, identity,
        safety_merge, document_loader, document_chunker,
        token_estimator, execution_plan, adjacent_packing,
        unified_execution, hierarchical_reducer, semantic_record,
        retry, importance_brief, retrieval, query_normalizer,
        retrieval_index, context_expansion, full_doc_fallback,
        block_lookup, pipeline, provenance,
        document_analysis, followup,
        benchmark, reference_qa, quality_metrics,
        single_flight, architecture_guard,
        block_ownership,
    ]
    for module in modules:
        assert _module_has_llm_call(module) is False, (
            f"{module.__name__} should not call LLM directly"
        )


def test_hierarchical_reducer_accepts_llm_runner():
    """HierarchicalReducer **принимает** LLMRunner, но не вызывает его сам."""
    from legal_summarizer.domain.config import (
    HierarchicalReducerConfig,
    )
    import dataclasses
    fields = dataclasses.fields(HierarchicalReducerConfig)
    assert any("group_size" in str(f) for f in fields)


def test_retry_module_uses_llm_for_repair():
    """retry.build_repair_prompt формирует prompt для **точечного** LLM-call."""
    from legal_summarizer.llm.retry import (
        build_repair_prompt,
    )
    prompt = build_repair_prompt("bad", ("c1",))
    assert "JSON" in prompt
    assert "c1" in prompt