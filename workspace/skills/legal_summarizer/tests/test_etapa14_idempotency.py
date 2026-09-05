"""Acceptance tests для Этапа 14: idempotency check до analysis."""

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


def _patch_run_canonical_pipeline(monkeypatch):
    """Подменяем ``run_canonical_pipeline`` счётчиком вызовов."""
    from workspace.skills.legal_summarizer.scripts.structure import pipeline as _pipeline_mod
    import summarizer as _summarizer

    calls = {"n": 0}
    original = _pipeline_mod.run_canonical_pipeline

    def _counting_run(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(_pipeline_mod, "run_canonical_pipeline", _counting_run)
    monkeypatch.setattr(_summarizer, "run_canonical_pipeline", _counting_run)
    return calls


def test_idempotent_run_does_not_call_pipeline(tmp_path: Path, monkeypatch):
    """Запуск с уже-completed manifest → pipeline НЕ вызывается."""
    calls = _patch_run_canonical_pipeline(monkeypatch)

    import summarizer

    text = "1. Пункт\n\nТекст документа для саммари."
    p = _write_doc(tmp_path, text)

    # Первый запуск → pipeline вызывается.
    r1 = summarizer.run(
        text, length="detailed", document_path=str(p), workspace_root=tmp_path,
    )
    assert r1["status"] == "completed", r1
    pipeline_calls_after_first = calls["n"]

    # Второй запуск с теми же аргументами → pipeline НЕ должен вызываться.
    r2 = summarizer.run(
        text, length="detailed", document_path=str(p), workspace_root=tmp_path,
    )
    assert r2["status"] == "completed", r2
    assert r2["stats"].get("cached") is True, r2["stats"]
    assert calls["n"] == pipeline_calls_after_first, (
        f"pipeline called again on idempotent run: {calls['n']}"
    )


def test_different_inputs_create_different_operation_id(tmp_path: Path, monkeypatch):
    """Разные text/length/path → разные operation_id → нет cache hit."""
    calls = _patch_run_canonical_pipeline(monkeypatch)

    import summarizer

    text1 = "1. Пункт 1\n\nТекст первый."
    text2 = "1. Пункт 2\n\nТекст второй, длиннее и содержательнее."
    p = _write_doc(tmp_path, text1)

    r1 = summarizer.run(
        text1, length="detailed", document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert r1["status"] == "completed", r1
    pipeline_calls_after_first = calls["n"]
    assert pipeline_calls_after_first >= 1, (
        f"first run should call pipeline: {pipeline_calls_after_first}"
    )

    # Другой текст — другой operation_id → pipeline вызывается.
    r2 = summarizer.run(
        text2, length="detailed", document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert r2["status"] == "completed", r2
    assert r2["stats"].get("cached") is not True, r2["stats"]
    assert calls["n"] > pipeline_calls_after_first, (
        f"different inputs should trigger pipeline: {calls['n']}"
    )