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
    import legal_summarizer.llm.calls as llm_calls

    def _fake_batch(chunks, *, chunks_total, structure, length, question=None):
        return {c.chunk_id: f"summary {c.chunk_id}" for c in chunks}

    def _fake_section(path, heading, text, *, length, question=None):
        return "section summary"

    def _fake_doc(text, *, length, focus, structure, question=None):
        return "doc summary"

    monkeypatch.setattr(llm_calls, "llm_batch", _fake_batch)
    monkeypatch.setattr(llm_calls, "llm_section_reduce", _fake_section)
    monkeypatch.setattr(llm_calls, "llm_document_reduce", _fake_doc)

    import legal_summarizer.application.service as _summarizer
    monkeypatch.setattr(_summarizer, "_llm_batch", _fake_batch)
    monkeypatch.setattr(_summarizer, "_llm_section_reduce", _fake_section)
    monkeypatch.setattr(_summarizer, "_llm_document_reduce", _fake_doc)

    import legal_summarizer.execution.pipeline as _pipeline_mod
    monkeypatch.setattr(_pipeline_mod, "_llm_batch", _fake_batch)


def _build_doc(sections: int = 6) -> str:
    parts = []
    for i in range(1, sections + 1):
        parts.append(
            f"{i}. Раздел {i}\n\n"
            + ("Текст. " * 50) * 200
            + "\n\n"
        )
    return "".join(parts)


def test_estimate_and_actual_are_separate_fields(tmp_path, monkeypatch):
    """Manifest хранит estimated и actual как разные поля."""
    import legal_summarizer.application.service as summarizer
    _install_llm_mocks(monkeypatch)
    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)

    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] == "completed", result
    # run() возвращает stats с актуальным счётчиком.
    # total_llm_calls — это фактическое число LLM-вызовов.
    assert "total_llm_calls" in result["stats"]
    assert result["stats"]["total_llm_calls"] >= 1
    # estimate доступен через ctx/estimate.
    insp = summarizer.inspect(text, document_path=str(p))
    ctx = summarizer._build_execution_context(insp)
    est = summarizer._estimate_for_run(insp, ctx)
    assert est.estimated_llm_calls > 0


def test_estimate_is_a_forecast_not_a_guarantee(tmp_path, monkeypatch):
    """Estimate остаётся forecast'ом, не превращается в факт."""
    import legal_summarizer.application.service as summarizer
    _install_llm_mocks(monkeypatch)
    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)

    insp = summarizer.inspect(text, document_path=str(p))
    ctx = summarizer._build_execution_context(insp)
    est = summarizer._estimate_for_run(insp, ctx)

    # estimate.estimated_llm_calls основан на selected batches.
    # Реальное число LLM-вызовов зависит от hierarchical reducer (min/max bounds).
    # Минимум — это map_calls + 1 (document reduce).
    # Максимум — это map_calls + N (если hierarchical reducer разбивает много раз).
    # Главное: estimate это upper bound для простого случая.
    assert isinstance(est.estimated_llm_calls, int)
    assert est.estimated_llm_calls >= 1


def test_estimate_for_run_is_upper_bound_for_direct(tmp_path):
    """_estimate_for_run: direct → upper bound = 1."""
    import legal_summarizer.application.service as summarizer
    text = _build_doc(sections=3)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))
    ctx = summarizer._build_execution_context(insp)
    est = summarizer._estimate_for_run(insp, ctx)
    if ctx.plan is None or ctx.strategy == "direct":
        assert est.estimated_llm_calls == 1


def test_estimate_for_run_hierarchical_upper_bound(tmp_path):
    """_estimate_for_run: map_hierarchical → batches + S + R (upper bound)."""
    import legal_summarizer.application.service as summarizer
    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))
    ctx = summarizer._build_execution_context(insp)
    est = summarizer._estimate_for_run(insp, ctx)
    if ctx.strategy == "map_hierarchical" and ctx.plan is not None:
        n_batches = len(ctx.plan.batches)
        sections = [n.node_id for n in insp.structure.iter_sections()] if insp.structure else []
        expected = n_batches + len(sections) + summarizer._hierarchical_reduce_calls(len(sections))
        assert est.estimated_llm_calls == expected
    assert isinstance(est.estimated_llm_calls, int)
    assert est.estimated_llm_calls >= 1


def test_hierarchical_reduce_calls_upper_bound_simulation():
    """_hierarchical_reduce_calls — детерминированная симуляция reduce-фазы.

    Верхняя граница = sum(ceil(current/group_size)) по раундам
    (каждый group в раунде → 1 LLM-вызов) + финальный call,
    если после max_rounds осталось >1 группы.
    """
    import legal_summarizer.application.service as summarizer
    gs = summarizer.MID_REDUCE_GROUP_SIZE
    max_rounds = summarizer.MAX_REDUCE_ROUNDS

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
        assert summarizer._hierarchical_reduce_calls(sections) == simulate(sections), (
            f"section count {sections}"
        )

    # Монотонность: больше sections → не меньше reduce-вызовов.
    prev = 0
    for sections in range(1, 200, 7):
        cur = summarizer._hierarchical_reduce_calls(sections)
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

    DOCX с 8 секциями детерминированно даёт ``map_hierarchical``
    (sections >= hierarchical_section_threshold, каждый section — ≥1
    chunk), поэтому тест доказывает верхнюю границу на самом
    нетривиальном пути: ``batches + S + reduce_calls(S)``.
    """
    import docx
    import legal_summarizer.application.service as summarizer
    _install_llm_mocks(monkeypatch)

    doc = docx.Document()
    for i in range(1, 9):
        doc.add_paragraph(f"{i}. Раздел {i}")
        doc.add_paragraph(("Общие положения и порядок действий раздела. ") * 220)
    p = tmp_path / "big.docx"
    doc.save(str(p))
    text = summarizer.load_text(p)

    insp = summarizer.inspect(text, document_path=str(p))
    ctx = summarizer._build_execution_context(insp, length="detailed")
    assert ctx.strategy == "map_hierarchical", f"setup: {ctx.strategy}"
    est = summarizer._estimate_for_run(insp, ctx)

    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] == "completed", result
    actual = result["stats"]["total_llm_calls"]
    assert actual >= 1
    assert actual <= est.estimated_llm_calls, (
        f"strategy={result['stats']['strategy']}: "
        f"actual={actual} > estimate={est.estimated_llm_calls}"
    )

    # Для map_hierarchical верхняя граница детерминированна:
    # batches (map) + S (section reduce) + reduce-симуляция(S).
    n_batches = len(ctx.plan.batches)
    s = len(list(insp.structure.iter_sections()))
    expected = n_batches + s + summarizer._hierarchical_reduce_calls(s)
    assert est.estimated_llm_calls == expected
    assert actual == expected, (
        f"mocked LLM: каждый non-empty section и каждый group дают ровно "
        f"по вызову; actual={actual} != simulated upper bound={expected}"
    )


def test_actual_llm_calls_bounded_by_estimate_map_flat(tmp_path, monkeypatch):
    """map_flat (txt → 0 sections, > direct threshold): actual <= estimate.

    TXT загружается одним physical block → structure без meaningful
    sections → strategy ``map_flat``. Upper bound = batches + 1.
    """
    import legal_summarizer.application.service as summarizer
    _install_llm_mocks(monkeypatch)
    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)

    insp = summarizer.inspect(text, document_path=str(p))
    ctx = summarizer._build_execution_context(insp, length="detailed")
    assert ctx.strategy == "map_flat", f"setup: {ctx.strategy}"
    est = summarizer._estimate_for_run(insp, ctx)

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


def test_actual_llm_calls_bounded_by_estimate_direct(tmp_path, monkeypatch):
    """direct: estimate == actual == 1."""
    import legal_summarizer.application.service as summarizer
    _install_llm_mocks(monkeypatch)
    p = _write_doc(tmp_path, "Только один абзац текста, без секций.")
    text = p.read_text(encoding="utf-8")

    insp = summarizer.inspect(text, document_path=str(p))
    ctx = summarizer._build_execution_context(insp)
    assert ctx.strategy == "direct", f"setup: {ctx.strategy}"
    est = summarizer._estimate_for_run(insp, ctx)
    assert est.estimated_llm_calls == 1

    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] == "completed", result
    assert result["stats"]["total_llm_calls"] == 1


def test_estimate_for_run_returns_estimate_dataclass(tmp_path):
    """_estimate_for_run возвращает dataclass Estimate с min/max."""
    import legal_summarizer.application.service as summarizer
    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)
    insp = summarizer.inspect(text, document_path=str(p))
    ctx = summarizer._build_execution_context(insp)

    est = summarizer._estimate_for_run(insp, ctx)
    assert hasattr(est, "estimated_llm_calls")
    assert hasattr(est, "estimated_duration_min_sec")
    assert hasattr(est, "estimated_duration_max_sec")
    assert est.estimated_duration_min_sec <= est.estimated_duration_max_sec
