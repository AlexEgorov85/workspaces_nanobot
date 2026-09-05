"""Этап 24: Single-flight concurrent test (реальные две одновременные операции).

Текущий test_etapa17_single_flight проверяет только одну операцию.
Этот тест проверяет peak==1 при реальных двух параллельных вызовах.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time as _time
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


import pytest


def _write_doc(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "doc.txt"
    p.write_text(text, encoding="utf-8")
    return p


def test_concurrent_runs_peak_is_one(tmp_path, monkeypatch):
    """Две параллельные run() → max concurrent LLM calls == 1."""
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

    import summarizer as _summarizer
    monkeypatch.setattr(_summarizer, "_llm_batch", _fake_batch)
    monkeypatch.setattr(_summarizer, "_llm_section_reduce", _fake_section)
    monkeypatch.setattr(_summarizer, "_llm_document_reduce", _fake_doc)

    from workspace.skills.legal_summarizer.scripts import pipeline as _pipeline_mod
    monkeypatch.setattr(_pipeline_mod, "_llm_batch", _fake_batch)

    import summarizer

    text_a = (
        "1. Раздел прочее\n\n"
        + ("Текст. " * 50) * 300
        + "\n\n2. Второй\n\n"
        + ("Текст. " * 50) * 300
    )
    text_b = (
        "1. Другой раздел\n\n"
        + ("Слово. " * 50) * 300
        + "\n\n2. Иной\n\n"
        + ("Слово. " * 50) * 300
    )
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    p1 = _write_doc(dir_a, text_a)
    p2 = _write_doc(dir_b, text_b)

    def run_one(txt, path):
        return summarizer.run(
            txt, length="detailed",
            document_path=str(path), workspace_root=tmp_path,
            confirmed=True,
        )

    t1 = threading.Thread(target=run_one, args=(text_a, p1))
    t2 = threading.Thread(target=run_one, args=(text_b, p2))
    t1.start()
    _time.sleep(0.01)
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert active["peak"] <= 1, f"peak concurrent calls = {active['peak']}, expected <= 1"
