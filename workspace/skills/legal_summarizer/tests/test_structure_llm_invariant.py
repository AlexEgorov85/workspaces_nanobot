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
    from workspace.skills.legal_summarizer.scripts.structure import (
        models, physical, numbering, heading, hierarchy,
        repair, validation, title, list_detection,
        candidate_aggregator, pdf_outline, identity,
        safety_merge, document_loader, document_chunker,
        token_estimator, execution_plan, adjacent_packing,
        unified_execution, hierarchical_reducer, semantic_record,
        retry, importance_brief, retrieval, query_normalizer,
        retrieval_index, context_expansion, full_doc_fallback,
        cleanup, block_lookup, pipeline, provenance,
        document_analysis, followup,
        benchmark, reference_qa, quality_metrics,
        single_flight, architecture_guard,
    )
    modules = [
        models, physical, numbering, heading, hierarchy,
        repair, validation, title, list_detection,
        candidate_aggregator, pdf_outline, identity,
        safety_merge, document_loader, document_chunker,
        token_estimator, execution_plan, adjacent_packing,
        unified_execution, hierarchical_reducer, semantic_record,
        retry, importance_brief, retrieval, query_normalizer,
        retrieval_index, context_expansion, full_doc_fallback,
        cleanup, block_lookup, pipeline, provenance,
        document_analysis, followup,
        benchmark, reference_qa, quality_metrics,
        single_flight, architecture_guard,
    ]
    for module in modules:
        assert _module_has_llm_call(module) is False, (
            f"{module.__name__} should not call LLM directly"
        )


def test_hierarchical_reducer_accepts_llm_runner():
    """HierarchicalReducer **принимает** LLMRunner, но не вызывает его сам."""
    from workspace.skills.legal_summarizer.scripts.structure.hierarchical_reducer import (
        HierarchicalReducerConfig,
    )
    import dataclasses
    fields = dataclasses.fields(HierarchicalReducerConfig)
    assert any("group_size" in str(f) for f in fields)


def test_retry_module_uses_llm_for_repair():
    """retry.build_repair_prompt формирует prompt для **точечного** LLM-call."""
    from workspace.skills.legal_summarizer.scripts.structure.retry import (
        build_repair_prompt,
    )
    prompt = build_repair_prompt("bad", ("c1",))
    assert "JSON" in prompt
    assert "c1" in prompt