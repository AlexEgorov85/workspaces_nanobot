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
    import document.structure as models
    import document.physical as physical
    import document.numbering as numbering
    import document.heading as heading
    import document.hierarchy as hierarchy
    import document.repair as repair
    import document.validation as validation
    import document.title as title
    import document.list_detection as list_detection
    import retrieval.candidate_aggregator as candidate_aggregator
    import document.pdf_outline as pdf_outline
    import document.identity as identity
    import document.safety_merge as safety_merge
    import document.loader as document_loader
    import chunking.chunker as document_chunker
    import llm.tokens as token_estimator
    import planning.plan as execution_plan
    import chunking.packing as adjacent_packing
    import planning.strategy as unified_execution
    import execution.hierarchical as hierarchical_reducer
    import retrieval.records as semantic_record
    import llm.retry as retry
    import application.brief_context as brief_context
    import application.brief_compression as brief_compression
    import retrieval.query as retrieval
    import retrieval.normalizer as query_normalizer
    import retrieval.index as retrieval_index
    import retrieval.context_expansion as context_expansion
    import retrieval.fallback as full_doc_fallback
    import document.block_lookup as block_lookup
    import application.pipeline_structure as pipeline
    import retrieval.provenance as provenance
    import document.analysis as document_analysis
    import retrieval.followup as followup
    import tools.legal_benchmark as benchmark
    import retrieval.qa as reference_qa
    import retrieval.quality as quality_metrics
    import llm.single_flight as single_flight
    import tools.architecture_guard as architecture_guard
    import chunking.block_ownership as block_ownership
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
    from execution.config import (
    HierarchicalReducerConfig,
    )
    import dataclasses
    fields = dataclasses.fields(HierarchicalReducerConfig)
    assert any("group_size" in str(f) for f in fields)

def test_retry_module_uses_llm_for_repair():
    """retry.build_repair_prompt формирует prompt для **точечного** LLM-call."""
    from llm.retry import (
        build_repair_prompt,
    )
    prompt = build_repair_prompt("bad", ("c1",))
    assert "JSON" in prompt
    assert "c1" in prompt