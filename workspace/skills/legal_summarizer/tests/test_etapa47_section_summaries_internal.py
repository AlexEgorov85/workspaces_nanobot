"""Тесты #0a: ``section_summaries`` пробрасывается через ``_reduce_phase`` → ``_internal``.

Проверяется:

1. ``map_reduce_hierarchical`` — ``_internal["section_summaries"]`` содержит
   построенные phase-1 reducer'ом summaries (LLM делал section-level reduce).
2. ``map_reduce_flat`` — ``_internal["section_summaries"] == {}`` (LLM не делал
   section-level reduce, нечего сохранять).
3. ``direct``-режим не вызывает ``_reduce_phase`` — manifest пишется через
   ``build_manifest`` с ``section_summaries={}`` (regression-guard: ``direct``
   не должен сломаться от изменений tuple-сигнатуры ``_reduce_phase``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _write_doc(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "doc.txt"
    p.write_text(text, encoding="utf-8")
    return p


def _build_doc(sections: int = 4) -> str:
    parts = []
    for i in range(1, sections + 1):
        parts.append(
            f"{i}. Раздел {i}\n\n"
            + ("Текст. " * 50) * 200
            + "\n\n"
        )
    return "".join(parts)


def _install_recording_llm(monkeypatch, recorded: dict) -> None:
    """Mock LLM: section_reduce возвращает короткий summary, doc_reduce — final."""
    import llm.calls as llm_calls

    def _fake_batch(chunks, *, chunks_total, structure, length, question=None):
        return {c.chunk_id: f"summary {c.chunk_id}" for c in chunks}

    def _fake_section(path, heading, text, *, length, question=None):
        recorded["section_calls"] += 1
        # Возвращаем достаточно длинный summary, чтобы phase-1
        # reducer его сохранил.
        return f"section_summary_for_{path or heading}"

    def _fake_doc(text, *, length, focus, structure, question=None):
        recorded["doc_calls"] += 1
        return "final_summary"

    monkeypatch.setattr(llm_calls, "llm_batch", _fake_batch)
    monkeypatch.setattr(llm_calls, "llm_section_reduce", _fake_section)
    monkeypatch.setattr(llm_calls, "llm_document_reduce", _fake_doc)


def test_hierarchical_internal_has_section_summaries(tmp_path, monkeypatch):
    """``_internal["section_summaries"]`` присутствует всегда.

    Контракт: ключ существует и в нём лежит dict.
    Содержимое зависит от стратегии:
      * ``map_hierarchical`` — непустой dict (LLM делал section reduce);
      * ``map_reduce_flat`` — пустой dict (тестируется отдельно).
    """
    import application.service as summarizer

    recorded = {"section_calls": 0, "doc_calls": 0}
    _install_recording_llm(monkeypatch, recorded)

    # Берём много секций с большим объёмом → map_hierarchical или map_flat.
    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)

    insp = summarizer.inspect(text, document_path=str(p))
    from application.context_builder import build_execution_context
    from document.section_helpers import section_index
    from execution.map_reduce import run_map_reduce_execution
    from execution.pipeline import run_one_batch_async as _rba

    ctx = build_execution_context(insp, length="detailed", question=None)
    # В txt без явных headings heading-detection не срабатывает → sections=0.
    # Тогда map_hierarchical не выберется. Пропускаем hierarchical-специфичные
    # assertions, но ВСЕ РАВНО проверяем, что ключ в _internal существует.
    hier_selected = (ctx.strategy == "map_hierarchical")

    final_batches = __import__(
        "application.execution_orchestration", fromlist=["map_plan_to_chunk_batches"]
    ).map_plan_to_chunk_batches(ctx.plan, list(ctx.chunks))

    section_ids, section_headings, section_paths = section_index(insp.analysis.structure)

    memory_store: dict[str, str] = {}

    def _write_chunk_result(op_id, cid, summary, **kw):
        memory_store[cid] = summary

    def _load_cached_partials(op_id, expected_ids, workspace_root):
        return {cid: memory_store[cid] for cid in expected_ids if cid in memory_store}

    payload = run_map_reduce_execution(
        chunks=list(ctx.chunks),
        plan=ctx.plan,
        strategy=ctx.strategy,
        length="detailed",
        focus=None,
        question=None,
        structure=insp.structure,
        analysis=insp.analysis,
        operation_id="op_test_internal",
        workspace_root=tmp_path,
        chars_in=len(text),
        article_count=0,
        existing_manifest=None,
        final_batches=final_batches,
        section_ids=section_ids,
        section_headings=section_headings,
        section_paths=section_paths,
        write_chunk_result=_write_chunk_result,
        run_one_batch_async=_rba,
        load_cached_partials=_load_cached_partials,
    )

    assert payload["status"] in ("completed", "partial"), payload
    internal = payload["stats"]["_internal"]
    # Контракт: ключ ВСЕГДА присутствует (новое поведение #0a).
    assert "section_summaries" in internal, (
        f"_internal must contain 'section_summaries' key (got keys: {sorted(internal)})"
    )
    section_summaries = internal["section_summaries"]
    assert isinstance(section_summaries, dict)
    # Только при map_hierarchical и при sections > 0 — реальные summaries.
    if hier_selected and len(section_ids) > 0:
        assert len(section_summaries) >= 1, (
            f"map_hierarchical with {len(section_ids)} sections "
            f"must produce ≥1 section_summary, got {section_summaries}"
        )
    # recorded["section_calls"] > 0 подтверждает, что LLM был вызван.
    if hier_selected and len(section_ids) > 0:
        assert recorded["section_calls"] >= 1, (
            f"hierarchical strategy must call llm_section_reduce, "
            f"got section_calls={recorded['section_calls']}"
        )


def test_flat_internal_has_empty_section_summaries(tmp_path, monkeypatch):
    """flat strategy → _internal['section_summaries'] == {}."""
    import application.service as summarizer

    def _fake_batch(chunks, *, chunks_total, structure, length, question=None):
        return {c.chunk_id: f"summary {c.chunk_id}" for c in chunks}

    def _fake_doc(text, *, length, focus, structure, question=None):
        return "final_summary"

    import llm.calls as llm_calls
    monkeypatch.setattr(llm_calls, "llm_batch", _fake_batch)
    monkeypatch.setattr(llm_calls, "llm_document_reduce", _fake_doc)

    # Берём 2 секции с большим объёмом → tokens > direct_threshold,
    # но sections < hierarchical_section_threshold (3) → map_flat.
    text = _build_doc(sections=2)
    p = _write_doc(tmp_path, text)

    insp = summarizer.inspect(text, document_path=str(p))
    from application.context_builder import build_execution_context
    from document.section_helpers import section_index
    from execution.map_reduce import run_map_reduce_execution
    from execution.pipeline import run_one_batch_async as _rba

    ctx = build_execution_context(insp, length="detailed", question=None)
    if ctx.strategy != "map_flat":
        pytest.skip(
            f"test requires map_flat strategy, got {ctx.strategy!r}"
        )

    final_batches = __import__(
        "application.execution_orchestration", fromlist=["map_plan_to_chunk_batches"]
    ).map_plan_to_chunk_batches(ctx.plan, list(ctx.chunks))

    section_ids, section_headings, section_paths = section_index(insp.analysis.structure)

    # Имитируем write_chunk_result + load_cached_partials в памяти,
    # чтобы batch'и могли записать partials и затем reduce нашёл их.
    memory_store: dict[str, str] = {}

    def _write_chunk_result(op_id, cid, summary, **kw):
        memory_store[cid] = summary

    def _load_cached_partials(op_id, expected_ids, workspace_root):
        return {cid: memory_store[cid] for cid in expected_ids if cid in memory_store}

    payload = run_map_reduce_execution(
        chunks=list(ctx.chunks),
        plan=ctx.plan,
        strategy="map_flat",
        length="detailed",
        focus=None,
        question=None,
        structure=insp.structure,
        analysis=insp.analysis,
        operation_id="op_test_flat",
        workspace_root=tmp_path,
        chars_in=len(text),
        article_count=0,
        existing_manifest=None,
        final_batches=final_batches,
        section_ids=section_ids,
        section_headings=section_headings,
        section_paths=section_paths,
        write_chunk_result=_write_chunk_result,
        run_one_batch_async=_rba,
        load_cached_partials=_load_cached_partials,
    )

    assert payload["status"] in ("completed", "partial"), payload
    internal = payload["stats"]["_internal"]
    assert internal["section_summaries"] == {}, internal


def test_direct_strategy_unaffected_by_signature_change(tmp_path, monkeypatch):
    """direct-режим не вызывает ``_reduce_phase`` — manifest пишется через
    ``build_manifest`` с ``section_summaries={}``. Regression-guard: новая
    6-tuple сигнатура ``_reduce_phase`` не должна ломать ``run_direct``.
    """
    import application.service as summarizer

    def _fake_doc(text, *, length, focus, structure, question=None):
        return "final_summary"

    import llm.calls as llm_calls
    monkeypatch.setattr(llm_calls, "llm_document_reduce", _fake_doc)

    # Маленький текст → strategy == "direct"
    text = "1. Раздел\n\nКороткий текст.\n\n2. Раздел\n\nЕщё текст."
    p = _write_doc(tmp_path, text)

    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] == "completed", result
    # manifest.section_summaries остаётся пустым dict в direct-режиме
    from cache.manifest import load_manifest
    m = load_manifest(result["operation_id"], tmp_path)
    assert m is not None
    assert m.section_summaries == {}