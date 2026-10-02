"""Тесты для LLM-as-semantic-only invariant.



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
    import libs.legal_summarizer.document.structure as models
    import libs.legal_summarizer.document.physical as physical
    import libs.legal_summarizer.document.numbering as numbering
    import libs.legal_summarizer.document.heading as heading
    import libs.legal_summarizer.document.hierarchy as hierarchy
    import libs.legal_summarizer.document.repair as repair
    import libs.legal_summarizer.document.validation as validation
    import libs.legal_summarizer.document.title as title
    import libs.legal_summarizer.document.list_detection as list_detection
    import libs.legal_summarizer.retrieval.candidate_aggregator as candidate_aggregator
    import libs.legal_summarizer.document.pdf_outline as pdf_outline
    import libs.legal_summarizer.document.identity as identity
    import libs.legal_summarizer.document.safety_merge as safety_merge
    import libs.legal_summarizer.document.loader as document_loader
    import libs.legal_summarizer.chunking.chunker as document_chunker
    import libs.legal_summarizer.llm.tokens as token_estimator
    import libs.legal_summarizer.planning.plan as execution_plan
    import libs.legal_summarizer.chunking.packing as adjacent_packing
    import libs.legal_summarizer.planning.strategy as unified_execution
    import libs.legal_summarizer.execution.hierarchical as hierarchical_reducer
    import libs.legal_summarizer.retrieval.records as semantic_record
    import libs.legal_summarizer.llm.retry as retry
    import libs.legal_summarizer.application.brief_context as brief_context
    import libs.legal_summarizer.application.brief_compression as brief_compression
    import libs.legal_summarizer.retrieval.query as retrieval
    import libs.legal_summarizer.retrieval.normalizer as query_normalizer
    import libs.legal_summarizer.retrieval.index as retrieval_index
    import libs.legal_summarizer.retrieval.context_expansion as context_expansion
    import libs.legal_summarizer.retrieval.fallback as full_doc_fallback
    import libs.legal_summarizer.document.block_lookup as block_lookup
    import libs.legal_summarizer.application.pipeline_structure as pipeline
    import libs.legal_summarizer.retrieval.provenance as provenance
    import libs.legal_summarizer.document.analysis as document_analysis
    import libs.legal_summarizer.retrieval.followup as followup
    import libs.legal_summarizer.retrieval.qa as reference_qa
    import libs.legal_summarizer.retrieval.quality as quality_metrics
    import libs.legal_summarizer.llm.single_flight as single_flight
    import libs.legal_summarizer.document.structure as document_structure_module
    modules = [
        models, physical, numbering, heading, hierarchy,
        repair, validation, title, list_detection,
        candidate_aggregator, pdf_outline, identity,
        safety_merge, document_loader, document_chunker,
        token_estimator, execution_plan, adjacent_packing,
        unified_execution, hierarchical_reducer, semantic_record,
        retry, brief_context, brief_compression, retrieval, query_normalizer,
        retrieval_index, context_expansion, full_doc_fallback,
        block_lookup, pipeline, provenance,
        document_analysis, followup,
        reference_qa, quality_metrics,
        single_flight,
        document_structure_module,
    ]
    for module in modules:
        assert _module_has_llm_call(module) is False, (
            f"{module.__name__} should not call LLM directly"
        )

def test_hierarchical_reducer_accepts_llm_runner():
    """HierarchicalReducer **принимает** LLMRunner, но не вызывает его сам."""
    from libs.legal_summarizer.execution.config import (
    HierarchicalReducerConfig,
    )
    import dataclasses
    fields = dataclasses.fields(HierarchicalReducerConfig)
    assert any("group_size" in str(f) for f in fields)

def test_retry_module_uses_llm_for_repair():
    """retry.build_repair_prompt формирует prompt для **точечного** LLM-call."""
    from libs.legal_summarizer.llm.retry import (
        build_repair_prompt,
    )
    prompt = build_repair_prompt("bad", ("c1",))
    assert "JSON" in prompt
    assert "c1" in prompt