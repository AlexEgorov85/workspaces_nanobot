"""Регрессия: REDUCE_INPUT_EMPTY + NO_PARTIALS + защита completed/partial
от пустого summary.

Перенесено из tests/test_legal_summarizer_empty_reduce.py в рамках
миграции Skill к целевой структуре (runtime в scripts/).

Использует canonical execution path: документ со структурой (разделы
1, 2, 3...) выбирает strategy=map_reduce_flat, ``llm_batch`` mock
возвращает ``{}`` (все батчи failed), ``load_cached_partials`` видит
пустой cache → runtime возвращает ``NO_PARTIALS``.

Контракт (точный):

* ``llm_batch={}`` + ``load_cached_partials возвращает {}`` →
  ``status='failed'``, ``error.code='NO_PARTIALS'``,
  ``llm_document_reduce`` НЕ вызывается.

* ``llm_batch рабочий`` + ``llm_document_reduce возвращает пустую строку``
  → ``status='failed'``, ``error.code='REDUCE_INPUT_EMPTY'``,
  ``llm_document_reduce`` ВЫЗЫВАЕТСЯ (joined непуст).

* ``llm_document_reduce бросает exception`` →
  ``status='failed'``, NO retry, ``llm_document_reduce`` ВЫЗЫВАЕТСЯ
  ровно один раз (один attempt).

* ``llm_batch возвращает dict с одним chunk, ``llm_document_reduce``
  возвращает whitespace-only строку → ``status='failed'``
  with ``REDUCE_INPUT_EMPTY``.

* Нормальный путь: непустой joined, непустой result → ``completed``
  with non-whitespace ``result.summary``.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_PROJECT_ROOT = _SKILL_ROOT.parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import application.service as service  # noqa: E402
import llm.calls as llm_calls  # noqa: E402
import llm.config as llm_config  # noqa: E402


def _make_doc(tmp_path, text: str) -> Path:
    p = tmp_path / "doc.txt"
    p.write_text(text, encoding="utf-8")
    return p


def _structured_text() -> str:
    """Документ с распознаваемой структурой (разделы 1, 2, 3, 4...),
    чтобы context_builder выбрал map_reduce strategy.
    """
    parts = []
    for i in range(1, 5):
        parts.append(
            f"{i}. Раздел {i}\n\n"
            + ("Текст. " * 50) * 80
            + "\n\n"
        )
    return "".join(parts)


def _base_cfg() -> dict:
    """Chunker config с реалистичными параметрами (не mock 200).

    Раньше был chunk_size=200 (mock), что приводило к 716 chunks на
    синтетике 4 Раздел X — а test падал на ``requires_continuation``
    ещё до достижения REDUCE_INPUT_EMPTY логики. Используем
    реалистичные 100000 + короткий текст, чтобы тест проверял то,
    для чего предназначен: REDUCE_INPUT_EMPTY behavior.
    """
    return {
        "chunk_size": 100000, "chunk_overlap": 0, "single_call_threshold": 100,
        "chunk_size_input_ratio": None,
        "context_window_tokens": 200,
    }


def _base_exec_cfg() -> dict:
    return {
        "confirmation_threshold_sec": 0.001, "estimated_chunk_duration_sec": 0.001,
        "max_chunks_for_execution": 1000,
        "max_concurrent_batches": 1,
        "context_batching": {
            "system_prompt_tokens": 100, "instruction_tokens_per_map": 50,
            "chars_per_token": 3.5, "safety_margin": 0.85,
        },
        "llm_max_tokens": 100,
    }


def _patch_cfg(monkeypatch) -> None:
    monkeypatch.setattr(
        llm_config, "get_chunking_config", lambda: _base_cfg(),
    )
    monkeypatch.setattr(
        llm_config, "get_execution_config", lambda: _base_exec_cfg(),
    )


def _install_empty_batch(
    monkeypatch,
    *,
    doc_fn=None,
    section_fn=None,
) -> None:
    """Установить ``llm_batch → {}`` + LLM mocks."""
    def fake_batch(chunks, **_kw):
        return {}

    if section_fn is None:
        section_fn = lambda *_a, **_kw: "section summary"
    if doc_fn is None:
        doc_fn = lambda *_a, **_kw: "doc summary"

    monkeypatch.setattr(llm_calls, "llm_batch", fake_batch)
    monkeypatch.setattr(llm_calls, "llm_section_reduce", section_fn)
    monkeypatch.setattr(llm_calls, "llm_document_reduce", doc_fn)
    _patch_cfg(monkeypatch)


def _install_working_batch(
    monkeypatch,
    *,
    doc_fn=None,
    section_fn=None,
) -> None:
    """Установить ``llm_batch`` → непустой partials."""
    def fake_batch_ok(chunks, **_kw):
        return {c.chunk_id: f"summary {c.chunk_id}" for c in chunks}

    if section_fn is None:
        section_fn = lambda *_a, **_kw: "section summary"
    if doc_fn is None:
        doc_fn = lambda *_a, **_kw: "doc summary"

    monkeypatch.setattr(llm_calls, "llm_batch", fake_batch_ok)
    monkeypatch.setattr(llm_calls, "llm_section_reduce", section_fn)
    monkeypatch.setattr(llm_calls, "llm_document_reduce", doc_fn)
    _patch_cfg(monkeypatch)


def _run(monkeypatch, tmp_path, *, length="detailed", confirmed=True):
    """Run Skill на структурированном документе."""
    text = _structured_text()
    doc = _make_doc(tmp_path, text)
    return service.run(
        text, length=length, confirmed=confirmed,
        workspace_root=tmp_path, document_path=str(doc),
    )


def test_all_map_batches_failed_returns_no_partials(monkeypatch, tmp_path):
    """Сценарий 1: ``llm_batch`` возвращает пустой dict.

    Контракт: ``status='failed'``, ``error.code='NO_PARTIALS'``,
    ``llm_document_reduce`` НЕ вызывается.
    """
    document_reduce_called = {"v": False}

    def fake_doc_reduce(*_args, **_kw):
        document_reduce_called["v"] = True
        return "should not be called"

    _install_empty_batch(monkeypatch, doc_fn=fake_doc_reduce)
    result = _run(monkeypatch, tmp_path)

    # Точный контракт.
    assert result["status"] == "failed", result
    err = result.get("error") or {}
    assert err.get("code") == "NO_PARTIALS", (
        f"Точно ожидался NO_PARTIALS (cache пуст); получили {err}"
    )
    assert document_reduce_called["v"] is False, (
        "document_reduce НЕ должен вызываться, когда cache partials пуст"
    )


def test_empty_document_reduce_returns_reduce_input_empty(monkeypatch, tmp_path):
    """Сценарий 2: ``llm_document_reduce`` возвращает пустую строку.

    Контракт: ``status='failed'``, ``error.code='REDUCE_INPUT_EMPTY'``,
    ``llm_document_reduce`` ВЫЗЫВАЕТСЯ (joined непуст).
    """
    document_reduce_calls = {"n": 0}

    def fake_doc_reduce_empty(*_args, **_kw):
        document_reduce_calls["n"] += 1
        return ""

    _install_working_batch(monkeypatch, doc_fn=fake_doc_reduce_empty)
    result = _run(monkeypatch, tmp_path)

    assert result["status"] == "failed", result
    err = result.get("error") or {}
    assert err.get("code") == "REDUCE_INPUT_EMPTY", (
        f"Точно ожидался REDUCE_INPUT_EMPTY (пустой результат reduce); "
        f"получили {err}"
    )
    assert document_reduce_calls["n"] == 1, (
        f"document_reduce должен быть вызван ровно 1 раз "
        f"(joined непуст, single attempt); "
        f"получили n={document_reduce_calls['n']}"
    )


def test_whitespace_only_reduce_returns_reduce_input_empty(monkeypatch, tmp_path):
    """Сценарий 3: ``llm_document_reduce`` возвращает только пробелы.

    Контракт: ``status='failed'``, ``error.code='REDUCE_INPUT_EMPTY'`` —
    whitespace-only summary не считается валидным.
    """
    def fake_doc_reduce_whitespace(*_args, **_kw):
        return "   \n\n\t  \n"

    _install_working_batch(monkeypatch, doc_fn=fake_doc_reduce_whitespace)
    result = _run(monkeypatch, tmp_path)

    assert result["status"] == "failed", result
    err = result.get("error") or {}
    assert err.get("code") == "REDUCE_INPUT_EMPTY", (
        f"whitespace-only summary → REDUCE_INPUT_EMPTY; получили {err}"
    )


def test_document_reduce_exception_is_non_retryable_single_attempt(
    monkeypatch, tmp_path,
):
    """Сценарий 4: ``llm_document_reduce`` бросает исключение.

    Контракт: ``status='failed'``, ``llm_document_reduce`` вызывается
    РОВНО ОДИН раз (NO retry на REDUCE_INPUT_EMPTY — non-retryable
    error, обусловленный input).
    """
    document_reduce_calls = {"n": 0}

    def fake_doc_reduce_explode(*_args, **_kw):
        document_reduce_calls["n"] += 1
        raise RuntimeError("simulated LLM error")

    _install_working_batch(monkeypatch, doc_fn=fake_doc_reduce_explode)
    result = _run(monkeypatch, tmp_path)

    # Точный контракт.
    assert result["status"] == "failed", (
        f"REDUCE_INPUT_EMPTY не должен выходить как completed/partial; "
        f"получили status={result.get('status')!r}"
    )
    assert document_reduce_calls["n"] == 1, (
        f"document_reduce вызван {document_reduce_calls['n']} раз "
        f"при исключении — ожидался ровно один attempt"
    )
    # summary не должен быть пустым или whitespace-only в completed/partial.
    assert "result" not in result or not (
        result.get("result", {}).get("summary", "").strip()
    ), f"completed/partial с непустым summary недопустим: {result}"


def test_normal_run_returns_completed_with_nonempty_summary(monkeypatch, tmp_path):
    """Сценарий 5: нормальный путь (positive control).

    Контракт: ``status='completed'``, ``summary`` непустой.
    Это baseline, против которого проверяются edge-case'ы сценариев 1-4.
    Без этого теста edge-case-тесты могут проходить по неправильной
    причине (например, всегда возвращать failed).
    """
    def fake_doc(*_args, **_kw):
        return "Итоговое саммари документа с полезной информацией."

    _install_working_batch(monkeypatch, doc_fn=fake_doc)
    result = _run(monkeypatch, tmp_path)

    assert result["status"] == "completed", result
    assert result["result"]["summary"].strip(), (
        f"completed требует непустой summary; result={result}"
    )


def test_reduce_input_empty_never_emits_completed_or_partial(monkeypatch, tmp_path):
    """Защитный regression: REDUCE_INPUT_EMPTY / NO_PARTIALS НИКОГДА не должны
    сопровождаться ``status in {completed, partial}`` даже если в кэше
    есть ровно один chunk, joined непуст, но result — пустая строка.
    """
    document_reduce_calls = {"n": 0}

    def fake_doc_reduce_empty(*_args, **_kw):
        document_reduce_calls["n"] += 1
        return ""

    _install_working_batch(monkeypatch, doc_fn=fake_doc_reduce_empty)
    result = _run(monkeypatch, tmp_path)

    assert result.get("status") not in {"completed", "partial"}, (
        f"REDUCE_INPUT_EMPTY с пустым summary → ЗАПРЕЩЕНО completed/partial; "
        f"result={result}"
    )
    assert document_reduce_calls["n"] == 1
