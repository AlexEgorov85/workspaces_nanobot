"""Регрессия: REDUCE_INPUT_EMPTY + защита completed/partial от пустого summary.

Перенесено из tests/test_legal_summarizer_empty_reduce.py в рамках
миграции Skill к целевой структуре (runtime в scripts/).

Использует canonical execution path: документ со структурой (разделы
1, 2, 3...) выбирает strategy=map_reduce_flat, ``llm_batch`` mock
возвращает ``{}`` (все батчи failed), ``_load_cached_partials`` видит
пустой cache → runtime возвращает ``NO_PARTIALS`` (REDUCE_INPUT_EMPTY).
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
import execution.pipeline as pipeline_mod  # noqa: E402


def _make_doc(tmp_path, text: str) -> Path:
    p = tmp_path / "doc.txt"
    p.write_text(text, encoding="utf-8")
    return p


def _structured_text() -> str:
    """Документ с распознаваемой структурой (разделы 1, 2, 3...),
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


def _base_cfg(*, ctx_tokens: int = 200):
    return {
        "chunk_size": 200, "chunk_overlap": 0, "single_call_threshold": 100,
        "chunk_size_input_ratio": None,
        "context_window_tokens": ctx_tokens,
    }


def _base_exec_cfg(*, concurrency: int = 1, ctx_tokens: int = 200):
    return {
        "confirmation_threshold_sec": 0.001, "estimated_chunk_duration_sec": 0.001,
        "max_chunks_for_execution": 100,
        "max_concurrent_batches": concurrency,
        "context_batching": {
            "system_prompt_tokens": 100, "instruction_tokens_per_map": 50,
            "chars_per_token": 3.5, "safety_margin": 0.85,
        },
        "llm_max_tokens": 100,
    }


def _install_llm_mocks(monkeypatch, *, doc_fn=None, section_fn=None):
    """Установить LLM-mocks на canonical surfaces."""
    def fake_batch(chunks, **_kw):
        return {}

    if section_fn is None:
        section_fn = lambda *_a, **_kw: "section summary"
    if doc_fn is None:
        doc_fn = lambda *_a, **_kw: "doc summary"

    monkeypatch.setattr(llm_calls, "llm_batch", fake_batch)
    monkeypatch.setattr(llm_calls, "llm_section_reduce", section_fn)
    monkeypatch.setattr(llm_calls, "llm_document_reduce", doc_fn)
    monkeypatch.setattr(service, "_llm_batch", fake_batch)
    monkeypatch.setattr(service, "_llm_section_reduce", section_fn)
    monkeypatch.setattr(service, "_llm_document_reduce", doc_fn)
    monkeypatch.setattr(pipeline_mod, "_llm_batch", fake_batch)
    monkeypatch.setattr(
        llm_config, "get_chunking_config", lambda: _base_cfg(),
    )
    monkeypatch.setattr(
        llm_config, "get_execution_config", lambda: _base_exec_cfg(),
    )


def test_all_map_batches_failed_returns_reduce_input_empty(
    monkeypatch, tmp_path,
):
    """Test 1: ``llm_batch`` возвращает пустой dict → все map-батчи
    failed → ``_load_cached_partials`` пуст → runtime → ``NO_PARTIALS``
    → status=failed. ``llm_document_reduce`` не должен вызываться.
    """
    document_reduce_called = {"v": False}

    def fake_doc_reduce(*_args, **_kw):
        document_reduce_called["v"] = True
        return "should not be called"

    _install_llm_mocks(monkeypatch, doc_fn=fake_doc_reduce)

    text = _structured_text()
    doc = _make_doc(tmp_path, text)
    result = service.run(
        text, length="detailed", confirmed=True, workspace_root=tmp_path,
        document_path=str(doc),
    )

    assert result["status"] == "failed"
    err = result.get("error") or {}
    assert err.get("code") in {"REDUCE_INPUT_EMPTY", "NO_PARTIALS"}, (
        f"Ожидался REDUCE_INPUT_EMPTY или NO_PARTIALS, получили {err}"
    )
    assert not document_reduce_called["v"], (
        "document_reduce был вызван с пустым input — это запрещено"
    )


def test_section_summaries_empty_returns_reduce_input_empty(
    monkeypatch, tmp_path,
):
    """Test 2: ``llm_batch`` возвращает валидные partials, но
    ``llm_document_reduce`` возвращает пустую строку →
    final_summary.strip() == "" → REDUCE_INPUT_EMPTY.
    """
    document_reduce_called = {"v": False}

    def fake_doc_reduce_empty(*_args, **_kw):
        document_reduce_called["v"] = True
        return ""

    def fake_batch_ok(chunks, **_kw):
        return {c.chunk_id: f"summary {c.chunk_id}" for c in chunks}

    def fake_section_ok(*_args, **_kw):
        return "section summary"

    monkeypatch.setattr(llm_calls, "llm_batch", fake_batch_ok)
    monkeypatch.setattr(llm_calls, "llm_section_reduce", fake_section_ok)
    monkeypatch.setattr(llm_calls, "llm_document_reduce", fake_doc_reduce_empty)
    monkeypatch.setattr(service, "_llm_batch", fake_batch_ok)
    monkeypatch.setattr(service, "_llm_section_reduce", fake_section_ok)
    monkeypatch.setattr(service, "_llm_document_reduce", fake_doc_reduce_empty)
    monkeypatch.setattr(pipeline_mod, "_llm_batch", fake_batch_ok)
    monkeypatch.setattr(
        llm_config, "get_chunking_config", lambda: _base_cfg(),
    )
    monkeypatch.setattr(
        llm_config, "get_execution_config", lambda: _base_exec_cfg(),
    )

    text = _structured_text()
    doc = _make_doc(tmp_path, text)
    result = service.run(
        text, length="detailed", confirmed=True, workspace_root=tmp_path,
        document_path=str(doc),
    )

    assert result["status"] == "failed"
    err = result.get("error") or {}
    assert err.get("code") in {"REDUCE_INPUT_EMPTY", "NO_PARTIALS"}, (
        f"Ожидался REDUCE_INPUT_EMPTY или NO_PARTIALS, получили {err}"
    )
    assert document_reduce_called["v"], (
        "document_reduce должен быть вызван (joined непуст); "
        "REDUCE_INPUT_EMPTY возникает из-за пустого результата reduce."
    )


def test_document_reduce_exception_does_not_emit_empty_completed(
    monkeypatch, tmp_path,
):
    """Test 3: ``llm_document_reduce`` бросает исключение →
    completed/partial НЕ допускается с пустым summary.
    """
    def fake_doc_reduce_explode(*_args, **_kw):
        raise RuntimeError("simulated LLM error")

    def fake_batch_ok(chunks, **_kw):
        return {c.chunk_id: f"summary {c.chunk_id}" for c in chunks}

    def fake_section_ok(*_args, **_kw):
        return "section summary"

    monkeypatch.setattr(llm_calls, "llm_batch", fake_batch_ok)
    monkeypatch.setattr(llm_calls, "llm_section_reduce", fake_section_ok)
    monkeypatch.setattr(llm_calls, "llm_document_reduce", fake_doc_reduce_explode)
    monkeypatch.setattr(service, "_llm_batch", fake_batch_ok)
    monkeypatch.setattr(service, "_llm_section_reduce", fake_section_ok)
    monkeypatch.setattr(service, "_llm_document_reduce", fake_doc_reduce_explode)
    monkeypatch.setattr(pipeline_mod, "_llm_batch", fake_batch_ok)
    monkeypatch.setattr(
        llm_config, "get_chunking_config", lambda: _base_cfg(),
    )
    monkeypatch.setattr(
        llm_config, "get_execution_config", lambda: _base_exec_cfg(),
    )

    text = _structured_text()
    doc = _make_doc(tmp_path, text)
    result = service.run(
        text, length="detailed", confirmed=True, workspace_root=tmp_path,
        document_path=str(doc),
    )

    if result.get("status") in {"completed", "partial"}:
        assert (result.get("result") or {}).get("summary", "").strip(), (
            f"completed/partial с пустым summary — ЗАПРЕЩЕНО. result={result}"
        )


def test_reduce_input_empty_is_non_retryable(monkeypatch, tmp_path):
    """Test 4: REDUCE_INPUT_EMPTY / NO_PARTIALS → status=failed сразу,
    document_reduce НЕ вызывается, NO retry.
    """
    document_reduce_calls = {"count": 0}

    def fake_doc_reduce_raises(*_args, **_kw):
        document_reduce_calls["count"] += 1
        raise RuntimeError("should not be called on empty input")

    _install_llm_mocks(monkeypatch, doc_fn=fake_doc_reduce_raises)

    text = _structured_text()
    doc = _make_doc(tmp_path, text)
    result = service.run(
        text, length="detailed", confirmed=True, workspace_root=tmp_path,
        document_path=str(doc),
    )

    assert result["status"] == "failed", (
        f"REDUCE_INPUT_EMPTY не должен быть completed/partial. "
        f"Получили status={result['status']!r}"
    )

    err = result.get("error") or {}
    assert err.get("code") in {"REDUCE_INPUT_EMPTY", "NO_PARTIALS"}, (
        f"Ожидался REDUCE_INPUT_EMPTY/NO_PARTIALS для пустого reduce input, "
        f"получили code={err.get('code')!r}"
    )

    assert document_reduce_calls["count"] == 0, (
        f"document_reduce вызван {document_reduce_calls['count']} раз "
        "при пустом reduce input — ЗАПРЕЩЕНО"
    )
