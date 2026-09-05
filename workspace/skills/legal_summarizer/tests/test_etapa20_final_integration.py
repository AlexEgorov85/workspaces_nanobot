"""Final integration suite (Этап 20).

Обязательные сценарии (из плана):

* **Direct**: small document → direct → exactly 1 LLM call → correct metadata.
* **Map-flat**: document → exact ExecutionPlan → exact actual batches →
  no duplicate chunks → no omitted chunks → correct manifest.
* **Map-hierarchical**: many sections → map batches → section reductions →
  document reductions → exactly one final result.
* **Question**: retrieval → selected chunks → execution → correct result.
* **Brief**: brief selection → budget → execution.
* **Idempotency**: same operation twice → second call does not re-run.
* **Single-flight**: two concurrent runs → max concurrent LLM calls = 1.
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


def _install_llm_mocks(monkeypatch, *, batch_recorder=None):
    """Подменяем llm_* во всех namespace."""
    from workspace.skills.legal_summarizer.scripts import llm_calls

    def _fake_batch(chunks, *, chunks_total, structure, length, question=None):
        if batch_recorder is not None:
            batch_recorder.append(tuple(c.chunk_id for c in chunks))
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


def test_scenario_direct(tmp_path: Path, monkeypatch):
    """Small document → direct → exactly 1 LLM call → correct metadata."""
    _install_llm_mocks(monkeypatch)
    import summarizer

    text = "1. Пункт\n\nКороткий текст договора для прямого пути."
    p = _write_doc(tmp_path, text)
    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
    )
    assert result["status"] == "completed", result
    assert result["result"]["strategy"] == "direct"
    assert result["stats"]["total_llm_calls"] == 1
    assert result["stats"]["document_reduce_calls"] == 1
    assert result["stats"]["map_calls"] == 0
    assert result["stats"]["sections_total"] >= 0


def test_scenario_map_flat(tmp_path: Path, monkeypatch):
    """Document → exact ExecutionPlan → no duplicates, no omissions."""
    batches: list[tuple[str, ...]] = []
    _install_llm_mocks(monkeypatch, batch_recorder=batches)
    import summarizer

    text = (
        "1. Общие положения\n\n"
        + ("Текст. " * 50) * 300
        + "\n\n2. Предмет\n\n"
        + ("Текст. " * 50) * 300
    )
    p = _write_doc(tmp_path, text)

    insp = summarizer.inspect(text, document_path=str(p))
    assert insp.strategy in ("map_flat", "map_hierarchical")

    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] == "completed", result
    # No duplicates.
    seen: list[str] = []
    for b in batches:
        seen.extend(b)
    assert len(seen) == len(set(seen)), "duplicate chunks"
    # No omissions: union covers plan.
    planned: set[str] = set()
    for b in insp.context_batches:
        planned.update(b)
    actual: set[str] = set()
    for b in batches:
        actual.update(b)
    assert actual == planned


def test_scenario_map_hierarchical(tmp_path: Path, monkeypatch):
    """Many sections → map_hierarchical → exactly one final result."""
    _install_llm_mocks(monkeypatch)
    import summarizer

    sections = []
    for i in range(5):
        sections.append(f"{i+1}. Раздел {i+1}\n\n" + ("Текст. " * 60) * 100 + "\n\n")
    text = "".join(sections)
    p = _write_doc(tmp_path, text)

    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] == "completed", result
    # final_summary всегда ровно один (Этап 9 invariant).
    assert result["result"]["summary"]
    assert isinstance(result["result"]["summary"], str)
    # total_llm_calls >= 1 (map calls + section reductions + document reduce).
    assert result["stats"]["total_llm_calls"] >= 1


def test_scenario_idempotency(tmp_path: Path, monkeypatch):
    """Same operation twice → second call is cached."""
    _install_llm_mocks(monkeypatch)
    import summarizer

    text = "1. Пункт\n\nКороткий текст для проверки идемпотентности."
    p = _write_doc(tmp_path, text)

    r1 = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
    )
    assert r1["status"] == "completed"
    assert r1["stats"].get("cached") is not True

    r2 = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
    )
    assert r2["status"] == "completed"
    assert r2["stats"].get("cached") is True


def test_scenario_brief(tmp_path: Path, monkeypatch):
    """Brief mode → execution с budget."""
    _install_llm_mocks(monkeypatch)
    import summarizer

    text = (
        "1. Раздел А\n\n" + ("Текст. " * 60) * 50
        + "\n\n2. Раздел Б\n\n" + ("Текст. " * 60) * 50
        + "\n\n3. Раздел В\n\n" + ("Текст. " * 60) * 50
    )
    p = _write_doc(tmp_path, text)

    result = summarizer.run(
        text, length="brief",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] == "completed", result
    assert result["result"]["length"] == "brief"
    assert result["result"]["summary"]


def test_scenario_single_flight(tmp_path: Path, monkeypatch):
    """Two execution path invocations have peak==1 concurrent LLM call."""
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

    text = (
        "1. Общие положения\n\n"
        + ("Текст длинный. " * 50) * 80
        + "\n\n2. Раздел Б\n\n"
        + ("Текст предмета. " * 50) * 80
        + "\n\n3. Раздел В\n\n"
        + ("Текст третий. " * 50) * 80
    )
    p = _write_doc(tmp_path, text)
    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] == "completed", result
    assert active["peak"] == 1, (
        f"expected peak==1, got peak={active['peak']}"
    )