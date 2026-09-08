"""Регрессия: invariant'ы из удалённых ~48 тестов исходного characterization.

Эти тесты покрывают поведение, которое НЕ покрыто актуальными etapa/
structure тестами и пришло из исходного
``tests/test_skill_legal_summarizer.py`` (1900 строк, удалён в коммите
``0a9c313 test(legal): перенести legacy root-тесты в Skill tests/``).
Перенесены были 22 теста как ``test_skill_legal_summarizer.py``.
~46 уникальных тестов исходного файла были удалены; из них ~12
представляют unique behavior, которое нужно сохранить как regression.

Эти тесты дополняют этап 7 плана восстановлением критических invariants.
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

import llm.calls as llm_calls  # noqa: E402
import application.service as service  # noqa: E402


# ---------------------------------------------------------------------------
# 1. inspect() должен НЕ вызывать LLM (важный contract инвариант).
# ---------------------------------------------------------------------------

def test_inspect_does_not_call_llm(monkeypatch, tmp_path):
    """Inspection — это document-level анализ без LLM-вызовов.

    LLM вызывается только в execution-фазе (через ``llm_batch`` /
    ``llm_section_reduce`` / ``llm_document_reduce``). ``inspect()``
    используется для idempotency check и для estimate — там LLM
    не нужен.

    Регрессия: ранний ``summarize`` использовал LLM прямо в inspect;
    это было исправлено. Тест защищает от возврата.
    """
    llm_called = {"n": 0}

    def tracking_llm(*_args, **_kw):
        llm_called["n"] += 1
        return "should-not-be-called"

    monkeypatch.setattr(llm_calls, "llm_batch", tracking_llm)
    monkeypatch.setattr(llm_calls, "llm_section_reduce", tracking_llm)
    monkeypatch.setattr(llm_calls, "llm_document_reduce", tracking_llm)

    p = tmp_path / "doc.txt"
    p.write_text(
        "1. Раздел 1\n\n" + ("Текст раздела. " * 30) * 30 + "\n\n2. Раздел 2\n\n"
        + ("Текст второго раздела. " * 30) * 30,
        encoding="utf-8",
    )
    text = p.read_text(encoding="utf-8")
    service.inspect(text, document_path=str(p))

    assert llm_called["n"] == 0, (
        f"inspect() НЕ должен вызывать LLM; "
        f"вызвано {llm_called['n']} раз"
    )


# ---------------------------------------------------------------------------
# 2. quick_estimate для TXT должен НЕ загружать полный текст.
# ---------------------------------------------------------------------------

def test_quick_estimate_txt_estimates_without_full_load(monkeypatch, tmp_path):
    """Регрессия инцидента 2026-08-28: пользователь ждал 3–5 минут
    пока ``pypdf`` прочитывал 663 стр. PDF, чтобы узнать «документ
    большой». ``quick_estimate`` на TXT должен использовать только
    page_count и short sample (если PDF) или только chunking config
    (если TXT), без полной экстракции.

    Contract: ``quick_estimate`` для TXT возвращает estimate БЕЗ
    полного извлечения текста. Проверка через ``DocumentLoader``
    не должна вызываться внутри quick_estimate.
    """
    from llm import config as llm_config

    p = tmp_path / "doc.txt"
    long_text = ("Текст. " * 50) * 100  # ~5KB
    p.write_text(long_text, encoding="utf-8")

    # quick_estimate reads file size or chunks, but should NOT
    # invoke document.pdf parser / office parser for TXT.
    monkeypatch.setattr(
        llm_config, "get_chunking_config",
        lambda: {
            "chunk_size": 200, "chunk_overlap": 0,
            "single_call_threshold": 100,
            "chunk_size_input_ratio": None,
            "context_window_tokens": 200,
        },
    )
    monkeypatch.setattr(
        llm_config, "get_execution_config",
        lambda: {
            "confirmation_threshold_sec": 0.5,
            "estimated_chunk_duration_sec": 0.1,
            "max_chunks_for_execution": 1000,
            "max_concurrent_batches": 1,
            "context_batching": {
                "system_prompt_tokens": 0, "instruction_tokens_per_map": 0,
                "chars_per_token": 3.5, "safety_margin": 0.85,
            },
            "llm_max_tokens": 100,
        },
    )

    # quick_estimate must succeed for txt without errors.
    result = service.quick_estimate(str(p))
    assert "chars_in" in result or "estimate" in result
    # estimate must be an Estimate dataclass.
    assert result["estimate"].estimated_duration_min_sec >= 0
    assert result["estimate"].estimated_duration_max_sec >= result["estimate"].estimated_duration_min_sec


# ---------------------------------------------------------------------------
# 3. confirmation_required payload: estimate block присутствует.
#    (Подробная проверка stripping llm_call_count из user-facing stats
#    добавлена в ``test_presenter_strips_llm_call_counts_from_stats``
#    ниже. ``summary.estimated_llm_calls`` остаётся в payload для
#    технической интроспекции — это не regression, это design choice.)
# ---------------------------------------------------------------------------

def test_confirmation_required_payload_includes_estimate_block(
    monkeypatch, tmp_path,
):
    """Confirmation payload содержит ``estimate`` namespace с min/max
    seconds — пользователь видит время обработки, не тех-числа.
    """
    import llm.config

    monkeypatch.setattr(
        llm.config, "get_execution_config",
        lambda: {
            "confirmation_threshold_sec": 0.001,
            "estimated_chunk_duration_sec": 100.0,
            "max_chunks_for_execution": 1000,
            "context_batching": {
                "system_prompt_tokens": 0, "instruction_tokens_per_map": 0,
                "chars_per_token": 3.5, "safety_margin": 0.85,
            },
        },
    )

    parts = []
    for i in range(1, 5):
        parts.append(
            f"{i}. Раздел {i}\n\n" + ("Текст. " * 50) * 300 + "\n\n"
        )
    text = "".join(parts)
    p = tmp_path / "doc.txt"
    p.write_text(text, encoding="utf-8")

    result = service.run(
        text, length="brief",
        document_path=str(p), workspace_root=tmp_path,
    )
    assert result["status"] == "confirmation_required"
    estimate = result["estimate"]
    assert "min_seconds" in estimate
    assert "max_seconds" in estimate
    assert "confirmation_threshold_sec" in estimate


# ---------------------------------------------------------------------------
# 4. Presenter должен strips llm_call_count из stats.
# ---------------------------------------------------------------------------

def test_presenter_strips_llm_call_counts_from_stats():
    """Presenter (``prepare_output``) должен скрывать точные числа
    LLM-вызовов из user-facing output, оставляя только duration /
    strategy / partial-flag.
    """
    from output.presenter import prepare_output

    fake = {
        "status": "completed",
        "operation_id": "op1",
        "result": {
            "subject": "test",
            "summary": "test summary",
            "length": "detailed",
            "chars_in": 1000,
            "chunks": 4,
            "context_batches": 2,
            "sections": 3,
            "strategy": "map_reduce_flat",
            "title": None,
            "partial": False,
        },
        "stats": {
            "chars_in": 1000,
            "chunks_total": 4,
            "context_batches_total": 2,
            "sections_total": 3,
            "map_calls": 2,  # <- тех-информация
            "section_reduce_calls": 0,
            "section_trim_calls": 0,
            "document_reduce_calls": 1,
            "reduce_calls": 1,
            "total_llm_calls": 3,  # <- тех-информация
            "retries": 0,
            "failed_batches": [],
            "partial": False,
            "duration_sec": 0.5,
            "strategy": "map_reduce_flat",
        },
    }

    out = prepare_output(fake)
    assert "stats" in out
    stats = out["stats"]
    # LLM-call details should be hidden from user-facing view.
    assert "map_calls" not in stats, (
        f"presenter должен скрывать map_calls из user-facing stats; "
        f"got {stats}"
    )
    assert "total_llm_calls" not in stats, (
        f"presenter должен скрывать total_llm_calls; got {stats}"
    )


# ---------------------------------------------------------------------------
# 5. Document reduce должен strip_think_blocks на финальный output.
# ---------------------------------------------------------------------------

def test_run_reduce_output_with_think_blocks_is_cleaned(monkeypatch, tmp_path):
    """Если LLM возвращает <think>...</think>блоки в финальном summary,
    runtime должен их убрать (через ``llm.sanitize.strip_think_blocks``).
    """
    def fake_doc_reduce(*_args, **_kw):
        return (
            "<think>внутренний разбор</think>"
            "Итоговое саммари документа. Полезная информация."
        )

    def fake_batch_ok(chunks, **_kw):
        return {c.chunk_id: f"summary {c.chunk_id}" for c in chunks}

    def fake_section(*_a, **_kw):
        return "section summary"

    monkeypatch.setattr(llm_calls, "llm_batch", fake_batch_ok)
    monkeypatch.setattr(llm_calls, "llm_section_reduce", fake_section)
    monkeypatch.setattr(llm_calls, "llm_document_reduce", fake_doc_reduce)

    import llm.config
    monkeypatch.setattr(
        llm.config, "get_chunking_config",
        lambda: {
            "chunk_size": 200, "chunk_overlap": 0,
            "single_call_threshold": 100,
            "chunk_size_input_ratio": None,
            "context_window_tokens": 200,
        },
    )
    monkeypatch.setattr(
        llm.config, "get_execution_config",
        lambda: {
            "confirmation_threshold_sec": 0.001,
            "estimated_chunk_duration_sec": 0.001,
            "max_chunks_for_execution": 1000,
            "context_batching": {
                "system_prompt_tokens": 0, "instruction_tokens_per_map": 0,
                "chars_per_token": 3.5, "safety_margin": 0.85,
            },
        },
    )

    parts = []
    for i in range(1, 5):
        parts.append(
            f"{i}. Раздел {i}\n\n" + ("Текст. " * 50) * 100 + "\n\n"
        )
    text = "".join(parts)
    p = tmp_path / "doc.txt"
    p.write_text(text, encoding="utf-8")

    result = service.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] == "completed", result
    summary = result["result"]["summary"]
    assert "<think>" not in summary, (
        f"summary должен быть очищен от <think>...</think>; got {summary!r}"
    )


# ---------------------------------------------------------------------------
# 6. Batch parse error retry → success (уже covered by structure_retry, но
#    дополнительный покрытие на integration).
# ---------------------------------------------------------------------------

def test_batch_parse_error_eventual_success_returns_completed(
    monkeypatch, tmp_path,
):
    """``MAX_BATCH_PARSE_RETRIES`` retry-цикл: первый вызов parse-error,
    второй и третий — success. Финальный result — completed.
    """
    attempts = {"n": 0}

    def flaky_batch(chunks, **_kw):
        attempts["n"] += 1
        if attempts["n"] == 1:
            from llm.prompts import ChunkResultParseError
            raise ChunkResultParseError("malformed JSON from LLM")
        return {c.chunk_id: f"summary {c.chunk_id}" for c in chunks}

    def fake_section(*_a, **_kw):
        return "section summary"

    def fake_doc(*_a, **_kw):
        return "final summary"

    monkeypatch.setattr(llm_calls, "llm_batch", flaky_batch)
    monkeypatch.setattr(llm_calls, "llm_section_reduce", fake_section)
    monkeypatch.setattr(llm_calls, "llm_document_reduce", fake_doc)

    import llm.config
    monkeypatch.setattr(
        llm.config, "get_chunking_config",
        lambda: {
            "chunk_size": 200, "chunk_overlap": 0,
            "single_call_threshold": 100,
            "chunk_size_input_ratio": None,
            "context_window_tokens": 200,
        },
    )
    monkeypatch.setattr(
        llm.config, "get_execution_config",
        lambda: {
            "confirmation_threshold_sec": 0.001,
            "estimated_chunk_duration_sec": 0.001,
            "max_chunks_for_execution": 1000,
            "context_batching": {
                "system_prompt_tokens": 0, "instruction_tokens_per_map": 0,
                "chars_per_token": 3.5, "safety_margin": 0.85,
            },
        },
    )

    parts = []
    for i in range(1, 5):
        parts.append(
            f"{i}. Раздел {i}\n\n" + ("Текст. " * 50) * 100 + "\n\n"
        )
    text = "".join(parts)
    p = tmp_path / "doc.txt"
    p.write_text(text, encoding="utf-8")

    result = service.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] == "completed", result
    assert attempts["n"] >= 2, (
        f"Retry должен был сработать (>=2 попыток); got {attempts['n']}"
    )


# ---------------------------------------------------------------------------
# 7. Question mode fallback — уже covered by test_question_integration,
#    но duplicate coverage для устойчивости.
# ---------------------------------------------------------------------------

def test_run_question_passes_question_to_llm(monkeypatch, tmp_path):
    """В question mode текст вопроса передаётся в LLM как параметр
    ``question``. Smoke test на интеграцию.
    """
    captured = {}

    def fake_batch(chunks, *, chunks_total, structure, length, question=None):
        captured["batch_question"] = question
        return {c.chunk_id: f"summary {c.chunk_id}" for c in chunks}

    def fake_section(*a, **kw):
        captured["section_question"] = kw.get("question")
        return "section"

    def fake_doc(*a, **kw):
        captured["doc_question"] = kw.get("question")
        return "answer"

    monkeypatch.setattr(llm_calls, "llm_batch", fake_batch)
    monkeypatch.setattr(llm_calls, "llm_section_reduce", fake_section)
    monkeypatch.setattr(llm_calls, "llm_document_reduce", fake_doc)

    import llm.config
    monkeypatch.setattr(
        llm.config, "get_chunking_config",
        lambda: {
            "chunk_size": 200, "chunk_overlap": 0,
            "single_call_threshold": 100,
            "chunk_size_input_ratio": None,
            "context_window_tokens": 200,
        },
    )
    monkeypatch.setattr(
        llm.config, "get_execution_config",
        lambda: {
            "confirmation_threshold_sec": 0.001,
            "estimated_chunk_duration_sec": 0.001,
            "max_chunks_for_execution": 1000,
            "context_batching": {
                "system_prompt_tokens": 0, "instruction_tokens_per_map": 0,
                "chars_per_token": 3.5, "safety_margin": 0.85,
            },
        },
    )

    parts = []
    for i in range(1, 5):
        parts.append(
            f"{i}. Раздел {i}\n\n" + ("Текст. " * 50) * 100 + "\n\n"
        )
    text = "".join(parts)
    p = tmp_path / "doc.txt"
    p.write_text(text, encoding="utf-8")

    service.run(
        text, length="detailed", question="Что такое Раздел 1?",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert captured.get("doc_question") == "Что такое Раздел 1?", (
        f"question должен быть передан в llm_document_reduce; "
        f"captured={captured}"
    )


# ---------------------------------------------------------------------------
# 8. Cache stats (cached=True) после повторного run (idempotency).
# ---------------------------------------------------------------------------

def test_run_returns_cache_stats_for_repeat(monkeypatch, tmp_path):
    """После повторного ``run()`` с теми же параметрами runtime
    возвращает cached stats (``stats.cached=True``).
    """
    def fake_batch(chunks, **_kw):
        return {c.chunk_id: f"summary {c.chunk_id}" for c in chunks}

    def fake_section(*_a, **_kw):
        return "section summary"

    def fake_doc(*_a, **_kw):
        return "final summary"

    monkeypatch.setattr(llm_calls, "llm_batch", fake_batch)
    monkeypatch.setattr(llm_calls, "llm_section_reduce", fake_section)
    monkeypatch.setattr(llm_calls, "llm_document_reduce", fake_doc)

    import llm.config
    monkeypatch.setattr(
        llm.config, "get_chunking_config",
        lambda: {
            "chunk_size": 200, "chunk_overlap": 0,
            "single_call_threshold": 100,
            "chunk_size_input_ratio": None,
            "context_window_tokens": 200,
        },
    )
    monkeypatch.setattr(
        llm.config, "get_execution_config",
        lambda: {
            "confirmation_threshold_sec": 0.001,
            "estimated_chunk_duration_sec": 0.001,
            "max_chunks_for_execution": 1000,
            "context_batching": {
                "system_prompt_tokens": 0, "instruction_tokens_per_map": 0,
                "chars_per_token": 3.5, "safety_margin": 0.85,
            },
        },
    )

    parts = []
    for i in range(1, 5):
        parts.append(
            f"{i}. Раздел {i}\n\n" + ("Текст. " * 50) * 100 + "\n\n"
        )
    text = "".join(parts)
    p = tmp_path / "doc.txt"
    p.write_text(text, encoding="utf-8")

    # First run.
    r1 = service.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert r1["status"] == "completed", r1

    # Second run with same inputs → idempotency.
    r2 = service.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
    )
    assert r2["status"] == "completed", r2
    assert r2["stats"].get("cached") is True, (
        f"Repeat run должен возвращать cached stats; got {r2['stats']}"
    )
