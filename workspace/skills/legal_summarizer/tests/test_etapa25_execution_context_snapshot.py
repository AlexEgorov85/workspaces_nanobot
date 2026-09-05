"""Этап 25: ExecutionContext строится один раз за один ``run()``.

Главный invariant: ``_build_execution_context`` вызывается ровно один раз
за время выполнения ``summarizer.run()``. Раньше ``_estimate_execution``
мог пересоздавать context — теперь этот путь изолирован от run-пути.

Эти тесты фиксируют текущее (правильное) поведение. Если в будущем кто-то
добавит второй вызов ``_build_execution_context`` в run(), тест упадёт.
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


def test_build_execution_context_called_once_for_map_run(tmp_path, monkeypatch):
    """За один ``run()`` _build_execution_context вызывается ровно 1 раз."""
    import summarizer

    calls = {"n": 0}
    original = summarizer._build_execution_context

    def _spy(insp, **kwargs):
        calls["n"] += 1
        return original(insp, **kwargs)

    monkeypatch.setattr(summarizer, "_build_execution_context", _spy)

    _install_llm_mocks(monkeypatch)
    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)

    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] == "completed", result
    assert calls["n"] == 1, (
        f"expected exactly 1 _build_execution_context call, got {calls['n']}"
    )


def test_build_execution_context_called_once_for_direct_run(tmp_path, monkeypatch):
    """Даже для direct-run (1 chunk) context строится 1 раз."""
    import summarizer

    # Малый документ → 1 chunk → direct strategy.
    small_text = "Только один абзац текста, без секций."

    calls = {"n": 0}
    original = summarizer._build_execution_context

    def _spy(insp, **kwargs):
        calls["n"] += 1
        return original(insp, **kwargs)

    monkeypatch.setattr(summarizer, "_build_execution_context", _spy)
    _install_llm_mocks(monkeypatch)

    p = _write_doc(tmp_path, small_text)

    result = summarizer.run(
        small_text,
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] == "completed", result
    assert calls["n"] == 1, (
        f"expected exactly 1 _build_execution_context call, got {calls['n']}"
    )


def test_build_execution_context_called_once_for_confirmation_path(tmp_path, monkeypatch):
    """Confirmation path тоже должен построить context один раз."""
    import summarizer

    monkeypatch.setattr(
        summarizer, "get_execution_config",
        lambda: {
            "confirmation_threshold_sec": 0.001,
            "estimated_chunk_duration_sec": 100.0,
            "max_chunks_for_execution": 100,
            "context_batching": {
                "system_prompt_tokens": 0,
                "instruction_tokens_per_map": 0,
                "chars_per_token": 3.5,
                "safety_margin": 0.85,
            },
        },
    )

    calls = {"n": 0}
    original = summarizer._build_execution_context

    def _spy(insp, **kwargs):
        calls["n"] += 1
        return original(insp, **kwargs)

    monkeypatch.setattr(summarizer, "_build_execution_context", _spy)

    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)

    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=False,
    )
    assert result["status"] == "confirmation_required"
    assert calls["n"] == 1, (
        f"expected exactly 1 _build_execution_context call, got {calls['n']}"
    )
