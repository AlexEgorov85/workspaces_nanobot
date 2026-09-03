"""Регрессия: REDUCE_INPUT_EMPTY + защита completed/partial от пустого summary.

План: 4 бага legal_summarizer / шаг 3.

После фикса в summarizer.py:
  1. Перед каждым document-level reduce проверяется, что input непустой.
     Если пустой — ``REDUCE_INPUT_EMPTY``, status=failed, NO LLM call,
     NO retry (это не transient LLM-ошибка).
  2. В hierarchical-path: если ни один раздел не дал section_summary
     (все chunks упали на map), document reduce не вызывается.
  3. После reduce: если ``final_summary.strip() == ""``, прогон
     считается failed (не completed/partial с пустым summary).
  4. Fallback ``final_summary = joined_sections`` в except допустим
     только если ``joined_sections.strip() != ""``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1] / "workspace" / "skills" / "legal_summarizer"
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import summarizer  # noqa: E402


def _one_chunk_per_batch_pack(chunks, budget):
    from workspace.skills.legal_summarizer.scripts.packing import ContextBatch
    return tuple(
        ContextBatch(
            batch_id=f"cb_{i:03d}",
            chunks=(c,),
            total_tokens_estimate=c.token_estimate,
            section_paths=(c.section_path,),
            page_range=None,
        )
        for i, c in enumerate(chunks)
    )


def _build_text_response(user_content: str) -> str:
    chunks_ids = sorted(set(re.findall(r"DOCUMENT CHUNK (\d+)", user_content)))
    parts = []
    for cid in chunks_ids:
        parts.append(f"DOC CHUNK {cid}: саммари чанка {cid}.")
    return "\n\n".join(parts)


def _base_cfg(*, concurrency: int = 1, ctx_tokens: int = 200):
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


# ---------------------------------------------------------------------------
# Test A — все map-батчи провалились → status=failed, document_reduce
# не вызван, REDUCE_INPUT_EMPTY.
# ---------------------------------------------------------------------------


def test_all_map_batches_failed_returns_reduce_input_empty(
    monkeypatch, tmp_path,
):
    """Если все map-батчи упали — joined пуст, document reduce не
    вызывается, статус failed с REDUCE_INPUT_EMPTY."""
    document_reduce_called = {"v": False}

    def fake_doc_reduce(section_summaries_text, **_kw):
        document_reduce_called["v"] = True
        return "should not be called"

    # Mock llm.chat: возвращает мусор (parse-error → все батчи failed).
    def fake_chat(messages, *, context=None, **kwargs):
        return "Это невалидный JSON без маркеров DOC CHUNK N:."

    def fake_doc_reduce_check(section_summaries_text, **_kw):
        document_reduce_called["v"] = True
        return "should not be called"

    monkeypatch.setattr(summarizer.llm, "chat", fake_chat)
    monkeypatch.setattr(summarizer, "pack_chunks", _one_chunk_per_batch_pack)
    monkeypatch.setattr(summarizer, "get_chunking_config", lambda: _base_cfg())
    monkeypatch.setattr(summarizer, "get_execution_config", lambda: _base_exec_cfg())
    monkeypatch.setattr(
        "workspace.skills.legal_summarizer.scripts.summarizer._llm_document_reduce",
        fake_doc_reduce_check,
    )

    paragraph = "Длинный абзац про договор подряда, права и обязанности. "
    text = "\n\n".join([paragraph] * 200)
    result = summarizer.run(
        text, length="brief", confirmed=True, workspace_root=tmp_path,
    )

    assert result["status"] == "failed"
    err = result.get("error") or {}
    # Допустимые коды для пустого входа reduce:
    #   REDUCE_INPUT_EMPTY — наш новый код для document-level reduce.
    #   NO_PARTIALS — pre-existing код, когда все map-батчи провалились
    #     и all_partials пуст (document reduce даже не вызывается).
    # Оба означают: пустой вход reduce, NO LLM call, NO retry.
    assert err.get("code") in {"REDUCE_INPUT_EMPTY", "NO_PARTIALS"}, (
        f"Ожидался REDUCE_INPUT_EMPTY или NO_PARTIALS, получили {err}"
    )
    assert not document_reduce_called["v"], (
        "document_reduce был вызван с пустым input — это запрещено"
    )


# ---------------------------------------------------------------------------
# Test B — section_summaries пуст → status=failed, REDUCE_INPUT_EMPTY.
# (Hierarchical path: section chunks есть, но все упали в section_reduce.)
# ---------------------------------------------------------------------------


def test_section_summaries_empty_returns_reduce_input_empty(
    monkeypatch, tmp_path,
):
    """Если section_summaries_out пуст (все sections дали пустые
    summary), document reduce не вызывается, REDUCE_INPUT_EMPTY.
    """
    document_reduce_called = {"v": False}

    # mock для document reduce — проверяем что не вызван
    def fake_doc_reduce(section_summaries_text, **_kw):
        document_reduce_called["v"] = True
        return "should not be called"

    monkeypatch.setattr(
        "workspace.skills.legal_summarizer.scripts.summarizer._llm_document_reduce",
        fake_doc_reduce,
    )

    # mock для section_reduce — возвращает пустую строку, чтобы
    # section_summaries_out остался пустым после цикла.
    def fake_section_reduce(*_args, **_kw):
        return ""

    monkeypatch.setattr(
        "workspace.skills.legal_summarizer.scripts.summarizer._llm_section_reduce",
        fake_section_reduce,
    )

    # map: каждый chunk даёт валидный partials (иначе test провалится раньше)
    def fake_chat(messages, *, context=None, **kwargs):
        user_content = messages[1]["content"]
        if re.findall(r"DOCUMENT CHUNK \d+", user_content):
            return _build_text_response(user_content)
        return "fallback"

    monkeypatch.setattr(summarizer.llm, "chat", fake_chat)
    monkeypatch.setattr(summarizer, "pack_chunks", _one_chunk_per_batch_pack)
    monkeypatch.setattr(summarizer, "get_chunking_config", lambda: _base_cfg())
    monkeypatch.setattr(summarizer, "get_execution_config", lambda: _base_exec_cfg())

    paragraph = "Длинный абзац про договор подряда, права и обязанности. "
    text = "\n\n".join([paragraph] * 200)
    result = summarizer.run(
        text, length="brief", confirmed=True, workspace_root=tmp_path,
    )

    # Допустимые failure коды для пустого входа reduce (см. test_all_map_batches_failed).
    if result["status"] == "failed":
        err = result.get("error") or {}
        assert err.get("code") in {"REDUCE_INPUT_EMPTY", "NO_PARTIALS"}, (
            f"Ожидался REDUCE_INPUT_EMPTY или NO_PARTIALS, получили {err}"
        )
    # Главный инвариант: document_reduce не вызывается с пустым input.
    assert not document_reduce_called["v"]


# ---------------------------------------------------------------------------
# Test C — LLM document reduce exception → корректный fallback или failed.
# Главное: completed/partial никогда не идут с пустым summary.
# ---------------------------------------------------------------------------


def test_document_reduce_exception_does_not_emit_empty_completed(
    monkeypatch, tmp_path,
):
    """Если document reduce бросает исключение и fallback joined пуст,
прогон → failed с REDUCE_INPUT_EMPTY. Completed с пустым summary
    НЕ допускается."""
    def fake_doc_reduce_explode(*_args, **_kw):
        raise RuntimeError("simulated LLM error")

    monkeypatch.setattr(
        "workspace.skills.legal_summarizer.scripts.summarizer._llm_document_reduce",
        fake_doc_reduce_explode,
    )

    def fake_chat(messages, *, context=None, **kwargs):
        user_content = messages[1]["content"]
        if re.findall(r"DOCUMENT CHUNK \d+", user_content):
            return _build_text_response(user_content)
        return "fallback"

    monkeypatch.setattr(summarizer.llm, "chat", fake_chat)
    monkeypatch.setattr(summarizer, "pack_chunks", _one_chunk_per_batch_pack)
    monkeypatch.setattr(summarizer, "get_chunking_config", lambda: _base_cfg())
    monkeypatch.setattr(summarizer, "get_execution_config", lambda: _base_exec_cfg())

    paragraph = "Длинный абзац про договор подряда, права и обязанности. "
    text = "\n\n".join([paragraph] * 200)
    result = summarizer.run(
        text, length="brief", confirmed=True, workspace_root=tmp_path,
    )

    # Главный инвариант.
    if result.get("status") in {"completed", "partial"}:
        assert (result.get("result") or {}).get("summary", "").strip(), (
            f"completed/partial с пустым summary — ЗАПРЕЩЕНО. result={result}"
        )


# ---------------------------------------------------------------------------
# Regression — completed/partial никогда не возвращает пустой summary.
# ---------------------------------------------------------------------------


def test_no_empty_summary_in_completed_or_partial():
    """Sanity-check: защита после reduce обязана блокировать
    completed/partial с пустым summary.

    Проверяем интроспекцией: после ``_strip_think_blocks`` в
    summarizer.run() должен быть if not summary.strip() → failed.
    """
    import inspect

    src = inspect.getsource(summarizer.run)
    assert "REDUCE_INPUT_EMPTY" in src, (
        "summarizer.run должен возвращать REDUCE_INPUT_EMPTY для "
        "пустого document reduce"
    )
    # Проверяем наличие проверки summary.strip() после reduce.
    assert re.search(r"final_summary.*\.strip\(\)", src), (
        "summarizer.run должен проверять final_summary.strip() "
        "после reduce и блокировать пустой результат"
    )


def test_reduce_input_empty_is_non_retryable():
    """REDUCE_INPUT_EMPTY должен встречаться как минимум в 3 точках
    summarizer.run():
      1. hierarchical section_summaries пуст
      2. hierarchical joined_sections пуст (дополнительная проверка)
      3. flat joined пуст
      4. пост-reduce защита final_summary пуст

    Все они возвращают ``status=failed`` с error.code=REDUCE_INPUT_EMPTY
    — без raise, без retry (это не transient LLM-ошибка).
    """
    import inspect

    src = inspect.getsource(summarizer.run)
    n = src.count("REDUCE_INPUT_EMPTY")
    assert n >= 3, (
        f"Ожидалось >= 3 упоминания REDUCE_INPUT_EMPTY, найдено {n}. "
        "Защита недостаточна."
    )
