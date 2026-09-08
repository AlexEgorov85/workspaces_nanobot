"""Этап 28: idempotency no re-execution.

Главный invariant: при повторном вызове ``summarizer.run()`` с тем же
``operation_id`` и завершённым manifest'ом:

- ``inspect()`` НЕ вызывается
- ``_build_execution_context()`` НЕ вызывается
- ``build_execution_plan()`` НЕ вызывается
- LLM-вызовы НЕ происходят
- возвращается cached result со статусом ``completed`` и ``cached=True``
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

    import application.service as _summarizer

    import execution.pipeline as _pipeline_mod

def _build_doc(sections: int = 4) -> str:
    parts = []
    for i in range(1, sections + 1):
        parts.append(
            f"{i}. Раздел {i}\n\n"
            + ("Текст. " * 50) * 100
            + "\n\n"
        )
    return "".join(parts)

import application.context_builder  # noqa: E402


def test_second_run_uses_cached_result(tmp_path, monkeypatch):
    """Повторный run() не вызывает pipeline, plan и LLM."""
    import application.service as summarizer
    _install_llm_mocks(monkeypatch)

    text = _build_doc(sections=4)
    p = _write_doc(tmp_path, text)

    # Первый run.
    result1 = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result1["status"] == "completed", result1
    # Не cached.
    cached1 = result1.get("stats", {}).get("cached")
    # Поле cached может отсутствовать у первого run.

    # Теперь устанавливаем spies.
    import application.service as _sm
    import planning.strategy as unified_execution

    counters = {
        "inspect": 0, "build_ctx": 0, "plan_build": 0,
        "llm_batch": 0, "llm_doc": 0,
    }
    original_inspect = _sm.inspect

    def _spy_inspect(text, document_path=None):
        counters["inspect"] += 1
        return original_inspect(text, document_path=document_path)

    monkeypatch.setattr(_sm, "inspect", _spy_inspect)

    original_ctx = _sm.build_execution_context

    def _spy_ctx(insp, **kwargs):
        counters["build_ctx"] += 1
        return original_ctx(insp, **kwargs)

    monkeypatch.setattr(application.context_builder, "build_execution_context", _spy_ctx)

    original_plan = unified_execution.build_execution_plan

    def _spy_plan(*args, **kwargs):
        counters["plan_build"] += 1
        return original_plan(*args, **kwargs)

    monkeypatch.setattr(unified_execution, "build_execution_plan", _spy_plan)
    monkeypatch.setattr(_sm, "build_execution_plan", _spy_plan)

    import llm.calls as llm_calls

    original_batch = llm_calls.llm_batch

    def _spy_batch(*args, **kwargs):
        counters["llm_batch"] += 1
        return original_batch(*args, **kwargs)

    monkeypatch.setattr(llm_calls, "llm_batch", _spy_batch)

    original_doc = llm_calls.llm_document_reduce

    def _spy_doc(*args, **kwargs):
        counters["llm_doc"] += 1
        return original_doc(*args, **kwargs)

    monkeypatch.setattr(llm_calls, "llm_document_reduce", _spy_doc)

    # Второй run — должен вернуть cached result без вызовов.
    result2 = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result2["status"] == "completed", result2
    # Cached.
    assert result2.get("stats", {}).get("cached") is True, (
        f"second run must return cached result; got stats={result2.get('stats')}"
    )

    # Никаких вызовов pipeline.
    assert counters["inspect"] == 0, (
        f"second run must not call inspect; got {counters['inspect']}"
    )
    assert counters["build_ctx"] == 0, (
        f"second run must not call _build_execution_context; "
        f"got {counters['build_ctx']}"
    )
    assert counters["plan_build"] == 0, (
        f"second run must not call build_execution_plan; "
        f"got {counters['plan_build']}"
    )
    assert counters["llm_batch"] == 0, (
        f"second run must not call llm_batch; got {counters['llm_batch']}"
    )
    assert counters["llm_doc"] == 0, (
        f"second run must not call llm_document_reduce; got {counters['llm_doc']}"
    )

def test_idempotency_counts_first_run_only(tmp_path, monkeypatch):
    """Подсчёт вызовов на первом run — все ненулевые."""
    import application.service as summarizer
    import planning.strategy as unified_execution
    import llm.calls as llm_calls

    counters = {
        "plan_build": 0, "llm_batch": 0, "llm_doc": 0,
    }
    _install_llm_mocks(monkeypatch)

    def _fake_batch(chunks, **kwargs):
        counters["llm_batch"] += 1
        return {c.chunk_id: f"summary {c.chunk_id}" for c in chunks}

    def _fake_doc(text, **kwargs):
        counters["llm_doc"] += 1
        return "doc summary"

    monkeypatch.setattr(llm_calls, "llm_batch", _fake_batch)
    monkeypatch.setattr(llm_calls, "llm_document_reduce", _fake_doc)

    import execution.pipeline as _pipeline_mod

    # Изолируем workspace в tmp.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    text = _build_doc(sections=4)
    p = _write_doc(workspace, text)

    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=workspace,
        confirmed=True,
    )
    assert result["status"] == "completed", result
    # Первый run должен иметь > 0 LLM вызовов.
    assert counters["llm_batch"] >= 1, (
        f"first run must call llm_batch; got {counters['llm_batch']}"
    )
    assert counters["llm_doc"] >= 1, (
        f"first run must call llm_document_reduce; got {counters['llm_doc']}"
    )
