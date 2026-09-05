"""Acceptance tests для Этапа 17: single-flight через execution path.

Главный invariant: пиковое количество одновременных LLM-вызовов
не превышает 1 во все фазы выполнения (map → section reduce → document
reduce → retry).
"""

from __future__ import annotations

import sys
import threading
import time as _time
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _write_doc(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "doc.txt"
    p.write_text(text, encoding="utf-8")
    return p


def test_concurrent_llm_calls_counter_at_most_one(monkeypatch):
    """Подсчёт одновременных вызовов ``llm_batch`` — максимум 1."""
    from workspace.skills.legal_summarizer.scripts import llm_calls

    active = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def _fake_batch(chunks, *, chunks_total, structure, length, question=None):
        with lock:
            active["now"] += 1
            if active["now"] > active["peak"]:
                active["peak"] = active["now"]
        _time.sleep(0.05)
        with lock:
            active["now"] -= 1
        return {c.chunk_id: f"summary {c.chunk_id}" for c in chunks}

    def _fake_section(path, heading, text, *, length, question=None):
        return "section summary"

    def _fake_doc(text, *, length, focus, structure, question=None):
        return "doc summary"

    monkeypatch.setattr(llm_calls, "llm_batch", _fake_batch)
    monkeypatch.setattr(llm_calls, "llm_section_reduce", _fake_section)
    monkeypatch.setattr(llm_calls, "llm_document_reduce", _fake_doc)

    import summarizer
    monkeypatch.setattr(summarizer, "_llm_batch", _fake_batch)
    monkeypatch.setattr(summarizer, "_llm_section_reduce", _fake_section)
    monkeypatch.setattr(summarizer, "_llm_document_reduce", _fake_doc)

    from workspace.skills.legal_summarizer.scripts import pipeline as _pipeline_mod
    monkeypatch.setattr(_pipeline_mod, "_llm_batch", _fake_batch)

    text = (
        "1. Общие положения\n\n"
        + ("Текст длинный. " * 50) * 100
        + "\n\n2. Раздел Б\n\n"
        + ("Текст предмета. " * 50) * 100
        + "\n\n3. Раздел В\n\n"
        + ("Текст третий. " * 50) * 100
    )
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = _write_doc(Path(td), text)

        result = summarizer.run(
            text, length="detailed",
            document_path=str(p), workspace_root=Path(td),
            confirmed=True,
        )
    assert result["status"] == "completed", result
    assert active["peak"] == 1, (
        f"expected max 1 concurrent LLM call, got peak={active['peak']}"
    )