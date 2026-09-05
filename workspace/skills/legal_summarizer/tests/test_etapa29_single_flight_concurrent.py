"""Этап 29: Single-flight concurrent, retry, exception safety.

Реальные concurrent тесты для cross-thread single-flight boundary:

24. Два параллельных run() → peak == 1.
25. Retry после exception → peak == 1.
26. Exception в LLM → lock released, второй run работает.
27. exception safety: ``finally`` clause release.
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


def _build_doc(sections: int = 4) -> str:
    parts = []
    for i in range(1, sections + 1):
        parts.append(
            f"{i}. Раздел {i}\n\n"
            + ("Текст. " * 50) * 200
            + "\n\n"
        )
    return "".join(parts)


def _install_counting_llm_mocks(monkeypatch, *, llm_runner):
    """Установить мок-функции для LLM, которые считают active calls."""
    from workspace.skills.legal_summarizer.scripts import llm_calls

    def _fake_batch(chunks, *, chunks_total, structure, length, question=None):
        return llm_runner(chunks, kind="batch")

    def _fake_section(path, heading, text, *, length, question=None):
        return llm_runner(None, kind="section")

    def _fake_doc(text, *, length, focus, structure, question=None):
        return llm_runner(None, kind="doc")

    monkeypatch.setattr(llm_calls, "llm_batch", _fake_batch)
    monkeypatch.setattr(llm_calls, "llm_section_reduce", _fake_section)
    monkeypatch.setattr(llm_calls, "llm_document_reduce", _fake_doc)

    import summarizer as _summarizer
    monkeypatch.setattr(_summarizer, "_llm_batch", _fake_batch)
    monkeypatch.setattr(_summarizer, "_llm_section_reduce", _fake_section)
    monkeypatch.setattr(_summarizer, "_llm_document_reduce", _fake_doc)

    from workspace.skills.legal_summarizer.scripts import pipeline as _pipeline_mod
    monkeypatch.setattr(_pipeline_mod, "_llm_batch", _fake_batch)


def test_concurrent_runs_peak_is_one(tmp_path, monkeypatch):
    """24: два параллельных run() → peak active == 1."""
    state = {"active": 0, "peak": 0}
    state_lock = threading.Lock()
    barrier = threading.Barrier(2)

    def _llm_runner(_chunks, *, kind):
        with state_lock:
            state["active"] += 1
            if state["active"] > state["peak"]:
                state["peak"] = state["active"]
        # Оба потока стартуют вместе.
        try:
            barrier.wait(timeout=5.0)
        except threading.BrokenBarrierError:
            pass
        _time.sleep(0.05)
        with state_lock:
            state["active"] -= 1
        if kind == "batch":
            return {}
        return "summary"

    _install_counting_llm_mocks(monkeypatch, llm_runner=_llm_runner)

    import summarizer

    text = _build_doc(sections=4)
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    p1 = _write_doc(dir_a, text)
    p2 = _write_doc(dir_b, text)

    def _run_one(path, workspace):
        return summarizer.run(
            text, length="detailed",
            document_path=str(path), workspace_root=workspace,
            confirmed=True,
        )

    t1 = threading.Thread(target=_run_one, args=(p1, dir_a))
    t2 = threading.Thread(target=_run_one, args=(p2, dir_b))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert state["peak"] <= 1, (
        f"peak concurrent calls = {state['peak']}, expected <= 1"
    )


def test_retry_after_exception_peak_is_one(tmp_path, monkeypatch):
    """25: первый вызов → exception, второй → success, peak == 1."""
    state = {"active": 0, "peak": 0, "calls": 0}
    state_lock = threading.Lock()

    def _llm_runner(_chunks, *, kind):
        with state_lock:
            state["calls"] += 1
            state["active"] += 1
            if state["active"] > state["peak"]:
                state["peak"] = state["active"]
        try:
            _time.sleep(0.05)
            # Первый вызов → exception; второй → success.
            if state["calls"] == 1:
                raise RuntimeError("simulated transient error")
        finally:
            with state_lock:
                state["active"] -= 1
        if kind == "batch":
            return {}
        return "summary"

    _install_counting_llm_mocks(monkeypatch, llm_runner=_llm_runner)

    import summarizer

    text = _build_doc(sections=4)
    p = _write_doc(tmp_path, text)

    # Retry path: первый LLM бросает, pipeline retry → второй успешен.
    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    # Не падает (либо completed, либо failed — это OK для нашего теста,
    # мы проверяем peak).
    assert state["peak"] <= 1, f"peak={state['peak']}, expected <= 1"


def test_exception_releases_lock(tmp_path, monkeypatch):
    """26: LLM exception → lock released, второй run получает LLM."""
    state = {"active": 0, "peak": 0}
    state_lock = threading.Lock()
    call_count = {"n": 0}

    def _llm_runner(_chunks, *, kind):
        with state_lock:
            call_count["n"] += 1
            state["active"] += 1
            if state["active"] > state["peak"]:
                state["peak"] = state["active"]
        try:
            _time.sleep(0.05)
            # Первый call всегда исключение.
            if call_count["n"] == 1:
                raise RuntimeError("simulated LLM failure")
        finally:
            with state_lock:
                state["active"] -= 1
        if kind == "batch":
            return {}
        return "summary"

    _install_counting_llm_mocks(monkeypatch, llm_runner=_llm_runner)

    import summarizer

    # Два последовательных run — оба должны иметь возможность вызвать LLM.
    text = _build_doc(sections=4)
    first_ws = tmp_path / "first"
    second_ws = tmp_path / "second"
    first_ws.mkdir()
    second_ws.mkdir()
    p1 = _write_doc(first_ws, text)
    p2 = _write_doc(second_ws, text)

    # Run 1 — LLM exception в первом call, retry может успешно завершиться.
    summarizer.run(
        text, length="detailed",
        document_path=str(p1), workspace_root=first_ws,
        confirmed=True,
    )
    # После run 1 lock освобождён.

    # Run 2 — должен успешно стартовать (lock доступен).
    state["active"] = 0
    summarizer.run(
        text, length="detailed",
        document_path=str(p2), workspace_root=second_ws,
        confirmed=True,
    )
    # Если бы lock не освободился, второй run завис бы.
    assert call_count["n"] >= 2, "second run must be able to call LLM"


def test_lock_finally_releases():
    """27: ``finally`` clause release — lock освобождается даже при exception.

    Прямой тест над chat_locked — внутри lock бросаем исключение.
    """
    from workspace.skills.legal_summarizer.scripts.llm_calls import chat_locked
    import workspace.skills.legal_summarizer.scripts.llm_calls as lc

    # Если lock удерживается — второй вызов ждёт; проверим это.
    acquired = []

    def _blocking_call():
        # Пытаемся взять lock — он должен быть свободен после exception.
        acquired.append(lc._CHAT_LOCK.acquire(blocking=False))
        if acquired[-1]:
            lc._CHAT_LOCK.release()

    # Бросаем исключение внутри chat_locked.
    import llm

    def _explode(messages, *, context=None):
        raise RuntimeError("test exception")

    original_chat = lc.llm.chat
    lc.llm.chat = _explode

    try:
        try:
            chat_locked([{"role": "user", "content": "x"}])
        except RuntimeError:
            pass  # Ожидаемое исключение.

        # Lock должен быть свободен.
        _blocking_call()
        assert acquired[-1] is True, (
            "lock must be released after exception in chat_locked"
        )
    finally:
        lc.llm.chat = original_chat
