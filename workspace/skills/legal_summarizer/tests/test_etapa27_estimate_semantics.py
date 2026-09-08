"""Этап 27: estimate semantics — estimated != actual.

Главный invariant: ``manifest.estimated_llm_calls`` — это прогноз,
``manifest.actual_llm_calls`` — это фактический runtime counter.

Система НЕ ДОЛЖНА писать ``actual_llm_calls == estimated_llm_calls`` как
гарантию. Это два разных поля с разной семантикой.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

def _write_doc(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "doc.txt"
    p.write_text(text, encoding="utf-8")
    return p

def _install_llm_mocks(monkeypatch):
    import llm.calls as llm_calls

    def _fake_batch(chunks, *, chunks_total, structure, length, question=None):
        return {c.chunk_id: f"summary {c.chunk_id}" for c in chunks}

    def _fake_section(path, heading, text, *, length, question=None):
        return "section summary"

    def _fake_doc(text, *, length, focus, structure, question=None):
        return "doc summary"

    monkeypatch.setattr(llm_calls, "llm_batch", _fake_batch)
    monkeypatch.setattr(llm_calls, "llm_section_reduce", _fake_section)
    monkeypatch.setattr(llm_calls, "llm_document_reduce", _fake_doc)

def _build_doc(sections: int = 6) -> str:
    parts = []
    for i in range(1, sections + 1):
        parts.append(
            f"{i}. Раздел {i}\n\n"
            + ("Текст. " * 50) * 200
            + "\n\n"
        )
    return "".join(parts)

def _insp_ctx_est(summarizer, tmp_path, *, sections=6, length="detailed", text=None):
    """Build (text, doc_path, insp, ctx, est) tuple для text-builder fixtures.

    Использует публичные public API:
        * ``summarizer.inspect``
        * ``application.context_builder.build_execution_context``
        * ``application.estimation.estimate_for_run``
    """
    from application.context_builder import build_execution_context
    from application.estimation import estimate_for_run

    if text is None:
        text = _build_doc(sections=sections)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))
    ctx = build_execution_context(insp, length=length)
    est = estimate_for_run(insp, ctx)
    return text, p, insp, ctx, est

def test_estimate_and_actual_are_separate_fields(tmp_path, monkeypatch):
    """Manifest хранит estimated и actual как разные поля."""
    import application.service as summarizer
    _install_llm_mocks(monkeypatch)
    text, p, _, _, est = _insp_ctx_est(summarizer, tmp_path, sections=6)

    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] == "completed", result
    assert "total_llm_calls" in result["stats"]
    assert result["stats"]["total_llm_calls"] >= 1
    assert est.estimated_llm_calls > 0

def test_estimate_is_a_forecast_not_a_guarantee(tmp_path):
    """Estimate остаётся forecast'ом, не превращается в факт."""
    import application.service as summarizer
    _, _, _, _, est = _insp_ctx_est(summarizer, tmp_path, sections=6)
    assert isinstance(est.estimated_llm_calls, int)
    assert est.estimated_llm_calls >= 1

def test_estimate_for_run_is_upper_bound_for_direct(tmp_path):
    """estimate_for_run: direct → upper bound = 1."""
    import application.service as summarizer
    _, _, _, ctx, est = _insp_ctx_est(summarizer, tmp_path, sections=3)
    if ctx.plan is None or ctx.strategy == "direct":
        assert est.estimated_llm_calls == 1, (
            f"direct strategy → estimate == 1, got {est.estimated_llm_calls}"
        )

def test_estimate_for_run_hierarchical_upper_bound(tmp_path):
    """estimate_for_run: map_hierarchical → batches + S + R (upper bound)."""
    import application.service as summarizer
    from application.estimation import _hierarchical_reduce_calls

    _, _, insp, ctx, est = _insp_ctx_est(summarizer, tmp_path, sections=6)
    if ctx.strategy == "map_hierarchical" and ctx.plan is not None:
        n_batches = len(ctx.plan.batches)
        sections = [n.node_id for n in insp.structure.iter_sections()] if insp.structure else []
        expected = n_batches + len(sections) + _hierarchical_reduce_calls(len(sections))
        assert est.estimated_llm_calls == expected
    assert isinstance(est.estimated_llm_calls, int)
    assert est.estimated_llm_calls >= 1

def test_hierarchical_reduce_calls_upper_bound_simulation():
    """_hierarchical_reduce_calls — детерминированная симуляция reduce-фазы.

    Верхняя граница = sum(ceil(current/group_size)) по раундам
    (каждый group в раунде → 1 LLM-вызов) + финальный call,
    если после max_rounds осталось >1 группы.
    """
    from application.estimation import _hierarchical_reduce_calls
    from execution.config import (
        MAX_REDUCE_ROUNDS,
        MID_REDUCE_GROUP_SIZE,
    )

    gs = MID_REDUCE_GROUP_SIZE
    max_rounds = MAX_REDUCE_ROUNDS

    def simulate(sections: int) -> int:
        if sections <= 1:
            return 0
        current, calls, rounds = sections, 0, 0
        while current > 1 and rounds < max_rounds:
            groups = -(-current // gs)
            calls += groups
            current = groups
            rounds += 1
        if current > 1:
            calls += 1
        return calls

    for sections in range(0, 60):
        assert _hierarchical_reduce_calls(sections) == simulate(sections), (
            f"section count {sections}"
        )

    prev = 0
    for sections in range(1, 200, 7):
        cur = _hierarchical_reduce_calls(sections)
        assert cur >= prev, (
            f"non-monotonic: sections {sections} → {cur} < prev {prev}"
        )
        prev = cur

def test_actual_llm_calls_bounded_by_estimate(tmp_path, monkeypatch):
    """Invariant: actual_llm_calls <= estimated_llm_calls.

    estimate строится на selected chunks того же run-context'а, поэтому
    верхняя граница должна гарантированно перекрывать фактический
    runtime-счётчик для любой стратегии (direct / map_flat /
    map_hierarchical).

    После рефакторинга (STRUCTURAL_PACKING_PLAN) chunker объединяет
    соседние sections. Чтобы получить несколько chunks и
    map_hierarchical стратегию, используем 30 секций с большим телом.
    """
    import docx
    import application.service as summarizer
    from application.context_builder import build_execution_context
    from application.estimation import (
        _hierarchical_reduce_calls,
        estimate_for_run,
    )

    _install_llm_mocks(monkeypatch)

    doc = docx.Document()
    for i in range(1, 31):
        doc.add_paragraph(f"{i}. Раздел {i}")
        doc.add_paragraph(("Общие положения и порядок действий раздела. ") * 220)
    p = tmp_path / "big.docx"
    doc.save(str(p))
    text = summarizer.load_text(p)

    insp = summarizer.inspect(text, document_path=str(p))
    ctx = build_execution_context(insp, length="detailed")
    assert ctx.strategy == "map_hierarchical", f"setup: {ctx.strategy}"
    est = estimate_for_run(insp, ctx)

    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] == "completed", result
    actual = result["stats"]["total_llm_calls"]
    assert actual >= 1
    # После рефакторинга (STRUCTURAL_PACKING_PLAN) chunker объединяет
    # соседние sections в более крупные chunks. Это значит, что
    # фактическое число LLM-вызовов может быть МЕНЬШЕ старого estimate
    # (который строился по 1 chunk/section). Это ожидаемо и желательно —
    # estimate остаётся как верхняя граница для всех стратегий.
    assert actual <= est.estimated_llm_calls, (
        f"strategy={result['stats']['strategy']}: "
        f"actual={actual} > estimate={est.estimated_llm_calls}"
    )

    n_batches = len(ctx.plan.batches)
    s = len(list(insp.structure.iter_sections()))
    expected = n_batches + s + _hierarchical_reduce_calls(s)
    assert est.estimated_llm_calls == expected
    # После рефакторинга (STRUCTURAL_PACKING_PLAN) фактическое число
    # LLM-вызовов может быть МЕНЬШЕ симулированной верхней границы
    # (которая строилась по 1 chunk/section). Это ожидаемо: structural
    # packing объединяет соседние sections в более крупные chunks.
    assert actual <= expected, (
        f"actual={actual} > simulated upper bound={expected}"
    )

def test_actual_llm_calls_bounded_by_estimate_map_flat(tmp_path, monkeypatch):
    """map_flat (txt → 0 sections, > direct threshold): actual <= estimate.

    TXT загружается одним physical block → structure без meaningful
    sections → strategy ``map_flat``. Upper bound = batches + 1.
    """
    import application.service as summarizer
    from application.context_builder import build_execution_context
    from application.estimation import estimate_for_run

    _install_llm_mocks(monkeypatch)
    text, p, _, ctx, est = _insp_ctx_est(
        summarizer, tmp_path, sections=6, length="detailed",
    )
    assert ctx.strategy == "map_flat", f"setup: {ctx.strategy}"

    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] == "completed", result
    actual = result["stats"]["total_llm_calls"]
    assert actual <= est.estimated_llm_calls, (
        f"map_flat: actual={actual} > estimate={est.estimated_llm_calls}"
    )
    expected = len(ctx.plan.batches) + 1
    assert est.estimated_llm_calls == expected
    assert actual == expected
    build_execution_context  # silence unused-import lint
    estimate_for_run  # silence unused-import lint

def test_actual_llm_calls_bounded_by_estimate_direct(tmp_path, monkeypatch):
    """direct: estimate == actual == 1."""
    import application.service as summarizer
    from application.context_builder import build_execution_context
    from application.estimation import estimate_for_run

    _install_llm_mocks(monkeypatch)
    p = _write_doc(tmp_path, "Только один абзац текста, без секций.")
    text = p.read_text(encoding="utf-8")

    insp = summarizer.inspect(text, document_path=str(p))
    ctx = build_execution_context(insp)
    assert ctx.strategy == "direct", f"setup: {ctx.strategy}"
    est = estimate_for_run(insp, ctx)
    assert est.estimated_llm_calls == 1

    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] == "completed", result
    assert result["stats"]["total_llm_calls"] == 1

def test_estimate_for_run_returns_estimate_dataclass(tmp_path):
    """estimate_for_run возвращает dataclass Estimate с min/max."""
    import application.service as summarizer
    from application.context_builder import build_execution_context
    from application.estimation import estimate_for_run

    text, p, _, ctx = _insp_ctx_est(summarizer, tmp_path, sections=6)[:4]
    del ctx  # explicit: only build context for setup

    insp = summarizer.inspect(text, document_path=str(p))
    ctx2 = build_execution_context(insp)
    est = estimate_for_run(insp, ctx2)

    assert hasattr(est, "estimated_llm_calls")
    assert hasattr(est, "estimated_duration_min_sec")
    assert hasattr(est, "estimated_duration_max_sec")
    assert est.estimated_duration_min_sec <= est.estimated_duration_max_sec
