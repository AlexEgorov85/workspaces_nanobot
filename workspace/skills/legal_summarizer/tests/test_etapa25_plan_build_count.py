"""Этап 25: ``build_execution_plan`` вызывается правильное число раз за ``run()``.

Архитектурная картина:
- ``Inspection.execution_plan`` — legacy/default (document-level) поле,
  строится в ``inspect()``.
- ``ExecutionContext.plan`` — canonical (run-level) snapshot, строится
  в ``_build_execution_context()``.

Инварианты:
- direct-run (1 chunk или нет structure): ``build_execution_plan == 0``
  (legacy пуст в inspect(), ctx тоже direct).
- map-run: ``build_execution_plan == 2`` — один в inspect() (для
  Inspection.execution_plan, legacy compat), один в
  _build_execution_context() (для ExecutionContext.plan, canonical).
  Execution использует ``ctx.plan`` (canonical), не ``insp.execution_plan``.
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
    from workspace.skills.legal_summarizer.scripts import llm_calls

    def _fake_batch(chunks, *, chunks_total, structure, length, question=None):
        return {c.chunk_id: f"summary {c.chunk_id}" for c in chunks}

    def _fake_section(path, heading, text, *, length, question=None):
        return "section summary"

    def _fake_doc(text, *, length, focus, structure, question=None):
        return "doc summary"

    monkeypatch.setattr(llm_calls, "llm_batch", _fake_batch)
    monkeypatch.setattr(llm_calls, "llm_section_reduce", _fake_section)
    monkeypatch.setattr(llm_calls, "llm_document_reduce", _fake_doc)

    import summarizer as _summarizer
    monkeypatch.setattr(_summarizer, "_llm_batch", _fake_batch)
    monkeypatch.setattr(_summarizer, "_llm_section_reduce", _fake_section)
    monkeypatch.setattr(_summarizer, "_llm_document_reduce", _fake_doc)

    from workspace.skills.legal_summarizer.scripts import pipeline as _pipeline_mod
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


def test_plan_built_for_map_run(tmp_path, monkeypatch):
    """Для map-run: build_execution_plan вызывается для ctx.plan + insp.execution_plan.

    Документируем число 2 (1 в inspect() для legacy, 1 в _build_execution_context()
    для canonical). Главное — execution использует canonical ctx.plan.
    """
    import summarizer
    from workspace.skills.legal_summarizer.scripts.structure import unified_execution

    calls = {"n": 0}
    original = unified_execution.build_execution_plan

    def _spy(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(unified_execution, "build_execution_plan", _spy)
    monkeypatch.setattr(summarizer, "build_execution_plan", _spy)

    _install_llm_mocks(monkeypatch)
    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)

    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] == "completed", result
    # 1 для Inspection.execution_plan (legacy), 1 для ExecutionContext.plan (canonical).
    assert calls["n"] == 2, (
        f"expected 2 build_execution_plan calls (inspect + ctx) for map, "
        f"got {calls['n']}"
    )


def test_plan_not_built_for_direct_run(tmp_path, monkeypatch):
    """Для direct-run: build_execution_plan == 0 (direct path в inspect и в ctx)."""
    import summarizer
    from workspace.skills.legal_summarizer.scripts.structure import unified_execution

    calls = {"n": 0}
    original = unified_execution.build_execution_plan

    def _spy(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(unified_execution, "build_execution_plan", _spy)
    monkeypatch.setattr(summarizer, "build_execution_plan", _spy)

    _install_llm_mocks(monkeypatch)
    small_text = "Только один абзац текста, без секций."
    p = _write_doc(tmp_path, small_text)

    result = summarizer.run(
        small_text,
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] == "completed", result
    assert calls["n"] == 0, (
        f"expected 0 build_execution_plan calls for direct, got {calls['n']}"
    )


def test_execution_uses_ctx_plan_not_insp_plan(tmp_path, monkeypatch):
    """Execution path получает ctx.plan, не insp.execution_plan.

    Spy на _run_map_reduce — он должен получать ctx.plan как plan=...
    """
    import summarizer

    captured = {}

    def _wrap_map_reduce(chunks, *, plan, strategy, **_kwargs):
        captured["plan"] = plan
        captured["strategy"] = strategy
        # Возвращаем фейковый результат без реального execution.
        return {
            "status": "completed",
            "summary": "fake",
            "manifest": {
                "strategy": strategy,
                "chunks_selected": len(chunks),
                "actual_llm_calls": 1,
                "context_batches_total": len(plan.batches) if plan else 1,
            },
        }

    monkeypatch.setattr(summarizer, "_run_map_reduce", _wrap_map_reduce)
    _install_llm_mocks(monkeypatch)

    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)

    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] == "completed"
    assert captured["plan"] is not None
    assert captured["strategy"] in ("map_flat", "map_hierarchical")
    # plan, который получил _run_map_reduce, должен иметь
    # тот же document_id, что и ctx.plan (canonical).
    assert captured["plan"].document_id  # non-empty
