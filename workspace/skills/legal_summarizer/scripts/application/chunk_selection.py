"""Chunk selection policy для application layer.

Единая точка ``select_chunks_for_mode(insp, length, question)``,
которая выбирает chunks для запуска по режиму (brief/detailed/question):

* ``question`` → retrieval (canonical), затем relaxed lexical fallback,
  затем bounded top-of-document fallback.
* ``brief`` → ``BriefContextBuilder.build_brief_chunk``: ровно один
  структурный ``Chunk`` из ``DocumentStructure`` + ``PhysicalDocument``
  (см. ``application.brief_context``).
* ``detailed``/default → все chunks из ``insp.chunks``.
"""

from __future__ import annotations

import re

from chunking._text_helpers import progress
from llm.config import get_execution_config


def _resolve_max_chunks() -> int:
    """Максимум chunks для brief/question режимов из конфига."""
    cfg = get_execution_config()
    try:
        n = int(cfg.get("max_chunks_per_question", 8))
        return n if n > 0 else 8
    except (TypeError, ValueError):
        return 8


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
    max_chunks = _resolve_max_chunks()
    if question:
        from retrieval.query import (
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
        from application.brief_context import (
            BriefContextConfig,
            build_brief_chunk,
        )
        from llm.config import get_brief_context_config

        if insp.analysis is None:
            return []
        cfg_dict = get_brief_context_config()
        cfg = BriefContextConfig(
            max_chars_fallback=int(cfg_dict.get("max_chars_fallback", 30000)),
            input_ratio=(
                float(cfg_dict["input_ratio"])
                if cfg_dict.get("input_ratio") is not None
                else None
            ),
            chars_per_token=float(cfg_dict.get("chars_per_token", 3.5)),
            structure_max_chars=int(cfg_dict.get("structure_max_chars", 12000)),
        )
        chunk = build_brief_chunk(insp.analysis, config=cfg)
        if chunk is None:
            raise RuntimeError(
                "BriefContextBuilder.build_brief_chunk returned no chunk"
            )
        progress(
            f"brief: 1 structural chunk "
            f"(char_count={chunk.char_count}, blocks={len(chunk.block_indices)})"
        )
        return [chunk]
    return list(insp.chunks)


__all__ = [
    "relaxed_lexical_fallback",
    "select_chunks_for_mode",
]
