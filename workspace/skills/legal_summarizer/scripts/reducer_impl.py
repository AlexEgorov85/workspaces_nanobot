"""Reducer implementation — выделено из ``reducer.py`` в этапе 21.

Содержит:

* ``reduce_results`` — public entry point.
* ``_reduce_flat`` — flat reduce (один LLM call).
* ``_reduce_hierarchical`` — hierarchical reduce (per-section + document).
* ``_section_text``, ``_chunk_id_by_section``, ``_section_order_key`` — helpers.

Модели и селектор — в ``reducer_models.py`` и ``reducer_strategy.py``.
"""
from __future__ import annotations

from typing import Any

from workspace.skills.legal_summarizer.scripts.reducer_models import (
    LLMRunner,
    ReduceConfig,
    ReduceResult,
    ReduceStats,
)
from workspace.skills.legal_summarizer.scripts.reducer_strategy import (
    should_use_hierarchical_reduce,
)
from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.sections import (
    SectionTree,
    extract_local_structure_label,
)


def _section_text(items: list[tuple[str, str]], labels: dict[str, str] | None = None) -> str:
    """Склеить (chunk_id, summary) пары в читаемый текст для LLM.

    Если для chunk_id задана структурная метка (``labels``), она
    добавляется в подпись блока: ``[Chunk 012 | Раздел III. Наследственное
    право]`` — чтобы модель сохранила принадлежность фактов к разделам при
    объединении в общий ответ.
    """
    parts: list[str] = []
    for cid, summary in items:
        label = (labels or {}).get(cid)
        if label:
            parts.append(f"[Chunk {cid} | {label}]\n{summary}")
        else:
            parts.append(f"[Chunk {cid}]\n{summary}")
    return "\n\n".join(parts)


def _chunk_id_by_section(chunks: list[Chunk]) -> dict[str, list[str]]:
    """Группировать chunk_id по section_id."""
    out: dict[str, list[str]] = {}
    for c in chunks:
        out.setdefault(c.section_id, []).append(c.chunk_id)
    return out


def _section_order_key(tree: SectionTree, sid: str) -> tuple:
    s = tree.sections.get(sid)
    if s is None:
        return (999,)
    parts: list[int] = []
    for p in s.section_path.split(" > "):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(999)
    return tuple(parts)


def reduce_results(
    chunks: list[Chunk],
    chunk_summaries: dict[str, str],
    tree: SectionTree | None,
    *,
    length: str,
    focus: str | None = None,
    config: ReduceConfig | None,
    llm_runner: LLMRunner | None = None,
) -> ReduceResult:
    """Hierarchical или flat reduce.

    Args:
        chunks: список Chunk (для group by section_id).
        chunk_summaries: dict[chunk_id, summary].
        tree: SectionTree (None для legacy).
        length: brief | medium | detailed.
        focus: пользовательский focus (только в document_reduce).
        config: ReduceConfig.
        llm_runner: callable(messages, system_prompt) -> str для LLM-вызовов.
            Если None — fallback на strings (для тестов без LLM).

    Returns:
        ReduceResult.
    """
    stats = ReduceStats()
    cfg = config or ReduceConfig()

    if not chunks:
        return ReduceResult(
            final_summary="",
            section_summaries={},
            stats=stats,
            strategy="empty",
        )

    if not chunk_summaries:
        return ReduceResult(
            final_summary="",
            section_summaries={},
            stats=stats,
            strategy="no_summaries",
        )

    hierarchical = should_use_hierarchical_reduce(tree, chunks)

    if hierarchical and tree is not None:
        return _reduce_hierarchical(
            chunks=chunks,
            chunk_summaries=chunk_summaries,
            tree=tree,
            length=length,
            focus=focus,
            config=cfg,
            llm_runner=llm_runner,
            stats=stats,
        )

    return _reduce_flat(
        chunks=chunks,
        chunk_summaries=chunk_summaries,
        length=length,
        focus=focus,
        config=cfg,
        llm_runner=llm_runner,
        stats=stats,
    )


def _reduce_flat(
    *,
    chunks: list[Chunk],
    chunk_summaries: dict[str, str],
    length: str,
    focus: str | None,
    config: ReduceConfig,
    llm_runner: LLMRunner | None,
    stats: ReduceStats,
) -> ReduceResult:
    """Flat reduce — один LLM call с всеми partials."""
    items = []
    for c in chunks:
        if c.chunk_id in chunk_summaries:
            items.append((c.chunk_id, chunk_summaries[c.chunk_id]))

    if not items:
        return ReduceResult(
            final_summary="",
            section_summaries={},
            stats=stats,
            strategy="flat_empty",
        )

    labels = {
        c.chunk_id: (c.section_heading or extract_local_structure_label(c.text))
        for c in chunks
        if c.chunk_id in chunk_summaries
    }
    joined = _section_text(items, labels)

    if llm_runner is None:
        joined_summary = joined
    else:
        joined_summary = llm_runner(joined, length=length, focus=focus)

    stats.document_reduce_calls += 1

    return ReduceResult(
        final_summary=joined_summary,
        section_summaries={},
        stats=stats,
        strategy="flat",
    )


def _reduce_hierarchical(
    *,
    chunks: list[Chunk],
    chunk_summaries: dict[str, str],
    tree: SectionTree,
    length: str,
    focus: str | None,
    config: ReduceConfig,
    llm_runner: LLMRunner | None,
    stats: ReduceStats,
) -> ReduceResult:
    """Hierarchical reduce: section-level + document-level."""
    from workspace.skills.legal_summarizer.scripts.structure.sections import (
        ROOT_SECTION_ID,
    )

    section_summaries: dict[str, str] = {}
    chunk_by_id = {c.chunk_id: c for c in chunks}

    sections_in_order: list[tuple[str, Any]] = []
    for sid, section in tree.sections.items():
        if sid == ROOT_SECTION_ID:
            continue
        if not section.heading:
            continue
        chunk_ids = [cid for cid in section.block_indices if cid in chunk_by_id]
        chunk_ids = [cid for cid in [c.chunk_id for c in chunks if c.section_id == sid] if cid in chunk_summaries]
        if not chunk_ids:
            continue
        sections_in_order.append((sid, section, chunk_ids))

    for sid, section, chunk_ids in sections_in_order:
        items = [(cid, chunk_summaries[cid]) for cid in chunk_ids]
        labels = {
            cid: (section.heading or extract_local_structure_label(chunk_by_id[cid].text))
            for cid in chunk_ids
        }
        joined = _section_text(items, labels)
        if llm_runner is None:
            section_summary = joined
        else:
            section_summary = llm_runner(
                joined,
                length=length,
                focus=None,
                section_path=section.section_path,
                section_heading=section.heading,
            )
        section_summaries[sid] = section_summary
        stats.section_reduce_calls += 1

    if len(section_summaries) > 1:
        for sid, section in tree.sections.items():
            if sid not in section_summaries:
                continue
            if len(section_summaries[sid]) > config.section_summary_max_chars:
                if llm_runner is None:
                    section_summaries[sid] = section_summaries[sid][:config.section_summary_max_chars]
                else:
                    section_summaries[sid] = llm_runner(
                        section_summaries[sid],
                        length="brief",
                        focus=None,
                        trim=True,
                        section_path=section.section_path,
                    )
                stats.section_trim_calls += 1

    if llm_runner is None:
        ordered = sorted(section_summaries.items(), key=lambda kv: _section_order_key(tree, kv[0]))
        joined = "\n\n".join(
            f"[Раздел {tree.sections[sid].section_path}: {tree.sections[sid].heading}]\n{summary}"
            for sid, summary in ordered
        )
        final_summary = joined
    else:
        ordered = sorted(section_summaries.items(), key=lambda kv: _section_order_key(tree, kv[0]))
        joined = "\n\n".join(
            f"[Раздел {tree.sections[sid].section_path}: {tree.sections[sid].heading}]\n{summary}"
            for sid, summary in ordered
        )
        final_summary = llm_runner(
            joined,
            length=length,
            focus=focus,
            section_path=None,
        )

    stats.document_reduce_calls += 1

    return ReduceResult(
        final_summary=final_summary,
        section_summaries=section_summaries,
        stats=stats,
        strategy="hierarchical",
    )


__all__ = ["reduce_results"]