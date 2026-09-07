"""Chunk selection policy для application layer.

Единая точка ``select_chunks_for_mode(insp, length, question)``,
которая выбирает chunks для запуска по режиму (brief/detailed/question):

* ``question`` → retrieval (canonical), затем relaxed lexical fallback,
  затем bounded top-of-document fallback.
* ``brief`` → ``select_brief_chunks_from_analysis`` (chunking policy) +
  ``allocate_brief_budget`` (chunking policy) для cap по chars.
* ``detailed``/default → все chunks из ``insp.chunks``.
"""

from __future__ import annotations

import re

from legal_summarizer.chunking._text_helpers import progress
from legal_summarizer.llm.config import get_chunking_config, get_execution_config


def _resolve_max_chunks() -> int:
    """Максимум chunks для brief/question режимов из конфига."""
    cfg = get_execution_config()
    try:
        n = int(cfg.get("max_chunks_per_question", 8))
        return n if n > 0 else 8
    except (TypeError, ValueError):
        return 8


def _service_mod():
    """Lazy lookup для ``_resolve_max_chunks`` (для monkeypatch в тестах)."""
    import legal_summarizer.application.service as _svc
    return _svc


def relaxed_lexical_fallback(
    question: str,
    chunks: list,
    *,
    max_chunks: int,
) -> list | None:
    """Управляемый fallback для question: расслабленный lexical match."""
    if not question or not chunks or max_chunks <= 0:
        return None
    raw_words = re.findall(r"\w{4,}", question.lower())
    if not raw_words:
        return None
    prefixes = [w[:4] for w in raw_words]
    matched: list = []
    for c in chunks:
        text_lower = getattr(c, "text", "").lower()
        if any(p in text_lower for p in prefixes):
            matched.append(c)
            if len(matched) >= max_chunks:
                break
    return matched if matched else None


def select_chunks_for_mode(
    insp,
    *,
    question: str | None,
    length: str,
) -> list:
    """Select chunks for the given run mode (brief/detailed/question)."""
    max_chunks = _service_mod()._resolve_max_chunks()
    if question:
        from legal_summarizer.retrieval.query import (
            RetrievalConfig,
        )
        hits = insp.analysis.retrieve(
            question, config=RetrievalConfig(max_results=max_chunks),
        ) if insp.analysis is not None else []
        if hits:
            by_id = {c.chunk_id: c for c in insp.chunks}
            chosen = [by_id[h.chunk_id] for h in hits if h.chunk_id in by_id]
            if chosen:
                progress(f"question: retrieval → {len(chosen)} chunks")
                return chosen
        progress("question: retrieval пустой → relaxed lexical fallback")
        _exec_cfg = get_execution_config()
        _fallback_max = int(_exec_cfg.get("question_fallback_max_chunks", 16))
        chosen = relaxed_lexical_fallback(
            question, insp.chunks, max_chunks=_fallback_max,
        )
        if chosen is not None:
            return chosen
        progress("question: keyword miss → bounded top-of-document fallback")
        return insp.chunks[:_fallback_max]
    if length == "brief":
        from legal_summarizer.application.brief_from_analysis import (
            select_brief_chunks_from_analysis,
        )
        chunk_cfg = get_chunking_config()
        chosen = list(select_brief_chunks_from_analysis(insp.analysis, config=None))
        brief_coverage = chunk_cfg.get("brief_coverage_ratio")
        if brief_coverage is None:
            brief_coverage = 0.5
        if brief_coverage < 1.0 and chosen:
            target = max(1, int(len(chosen) * brief_coverage))
            chosen = chosen[:target]
        brief_total_budget = chunk_cfg.get("brief_max_input_chars")
        if brief_total_budget:
            from legal_summarizer.chunking.brief_budget import (
                allocate_brief_budget,
            )
            chosen = list(allocate_brief_budget(
                chosen, total_budget_chars=int(brief_total_budget),
            ))
        return chosen
    return list(insp.chunks)


__all__ = [
    "relaxed_lexical_fallback",
    "select_chunks_for_mode",
]
