"""Scenario tests на resume / manifest state recovery.

Покрывает 4 сценария:

    * **A. completed**: manifest с ``status=completed`` → reload → status preserved.
    * **B. failed → retry**: manifest с ``status=failed`` → retry → status=completed.
    * **C. partial**: manifest с ``status=partial`` (есть failed_batch_ids) → reload →
      status preserved + failed batches видны.
    * **D. cache reuse**: тот же ``document_id`` в той же ``session_key`` →
      пропуск map-фазы (chunks загружены из document-cache).

Эти тесты используют уже существующие ``manifest.py``/``document_cache.py``
и ``summarizer.run()`` для проверки фактического поведения.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "workspace" / "skills" / "legal_summarizer" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
_PROJ = _REPO
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "workspace").mkdir(parents=True)
    return ws


def _make_manifest_dict(
    op_id: str,
    *,
    status: str = "running",
    chunks_total: int = 0,
    batches_done: list | None = None,
    batches_failed: list | None = None,
    chunk_states: dict | None = None,
    sections: dict | None = None,
    context_batches: dict | None = None,
    last_error: dict | None = None,
) -> dict:
    """Helper: создаёт минимальный v2 manifest dict для тестов."""
    if batches_done is None:
        batches_done = []
    if batches_failed is None:
        batches_failed = []
    if chunk_states is None:
        chunk_states = {}
    if sections is None:
        sections = {}
    if context_batches is None:
        context_batches = {}
    return {
        "version": 2,
        "operation_id": op_id,
        "status": status,
        "document_path": "/tmp/contract.pdf",
        "structure_title": "Документ",
        "chars_in": 1000,
        "length": "medium",
        "chunks_total": chunks_total,
        "context_batches_total": len(context_batches),
        "estimated_llm_calls": chunks_total + 1,
        "actual_llm_calls": chunks_total + 1,
        "sections": sections,
        "chunk_states": chunk_states,
        "context_batches": context_batches,
        "section_summaries": {},
        "batches_done": batches_done,
        "batches_failed": batches_failed,
        "last_error": last_error,
        "started_at": "2026-01-01T00:00:00",
        "completed_at": (
            "2026-01-01T00:05:00" if status in ("completed", "partial") else None
        ),
        "duration_sec": 300.0 if status in ("completed", "partial") else None,
        "article_count": 42,
    }


# ---------------------------------------------------------------------------
# Scenario A: completed manifest → reload → status preserved
# ---------------------------------------------------------------------------


def test_resume_scenario_a_completed_manifest_roundtrip(tmp_path):
    """Scenario A: manifest со status=completed → load → status preserved.

    Проверяет, что после reload manifest содержит корректные chunk_states
    и status='completed'.
    """
    from workspace.skills.legal_summarizer.scripts.manifest import (
        load_manifest,
        manifest_path,
    )

    ws = _workspace(tmp_path)
    op_id = "op_scenario_a"

    raw = _make_manifest_dict(
        op_id,
        status="completed",
        chunks_total=3,
        batches_done=["cb_000", "cb_001"],
        chunk_states={
            "000": {"status": "completed", "context_batch_id": "cb_000",
                     "section_id": "s_1", "section_path": "1"},
            "001": {"status": "completed", "context_batch_id": "cb_000",
                     "section_id": "s_1", "section_path": "1"},
            "002": {"status": "completed", "context_batch_id": "cb_001",
                     "section_id": "s_2", "section_path": "2"},
        },
    )
    path = manifest_path(op_id, ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    loaded = load_manifest(op_id, workspace_root=ws)
    assert loaded is not None
    assert loaded.status == "completed"
    assert loaded.version == 2
    assert len(loaded.chunk_states) == 3
    assert loaded.batches_failed == []
    assert len(loaded.batches_done) == 2


def test_resume_scenario_a_completed_manifest_path_exists(tmp_path):
    """Scenario A: manifest_path существует на диске после save."""
    from workspace.skills.legal_summarizer.scripts.manifest import (
        manifest_path,
    )

    ws = _workspace(tmp_path)
    op_id = "op_scenario_a_path"

    raw = _make_manifest_dict(op_id, status="completed")
    path = manifest_path(op_id, ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    assert path.exists()


# ---------------------------------------------------------------------------
# Scenario B: failed → retry → completed
# ---------------------------------------------------------------------------


def test_resume_scenario_b_failed_manifest_reload_preserves_failure(tmp_path):
    """Scenario B: manifest с status=failed → reload → status='failed'.

    Retry должен сохранить старый manifest до тех пор, пока новый run
    не создаст новый manifest (или обновит тот же).
    """
    from workspace.skills.legal_summarizer.scripts.manifest import (
        load_manifest,
        manifest_path,
    )

    ws = _workspace(tmp_path)
    op_id = "op_scenario_b"

    raw = _make_manifest_dict(
        op_id,
        status="failed",
        chunks_total=2,
        batches_done=["cb_000"],
        batches_failed=["cb_001"],
        chunk_states={
            "000": {"status": "completed", "context_batch_id": "cb_000",
                     "section_id": "s_1", "section_path": "1"},
            "001": {"status": "failed", "context_batch_id": "cb_001",
                     "section_id": "s_1", "section_path": "1"},
        },
        last_error={"code": "LLM_PARSE_ERROR", "message": "Invalid JSON"},
    )
    path = manifest_path(op_id, ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    # Reload после сбоя — статус 'failed' сохранён.
    loaded = load_manifest(op_id, workspace_root=ws)
    assert loaded is not None
    assert loaded.status == "failed"
    assert "cb_001" in loaded.batches_failed
    assert loaded.chunk_states["001"]["status"] == "failed"
    assert loaded.last_error is not None
    assert loaded.last_error["code"] == "LLM_PARSE_ERROR"


def test_resume_scenario_b_retry_updates_status_to_completed(tmp_path):
    """Scenario B: retry после failure → status='completed'.

    Симулируем retry: overwrite manifest с новым status=completed.
    """
    from workspace.skills.legal_summarizer.scripts.manifest import (
        load_manifest,
        manifest_path,
    )

    ws = _workspace(tmp_path)
    op_id = "op_scenario_b_retry"

    # Initial state — failed.
    raw_failed = _make_manifest_dict(
        op_id,
        status="failed",
        chunks_total=2,
        batches_failed=["cb_001"],
    )
    path = manifest_path(op_id, ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw_failed, ensure_ascii=False), encoding="utf-8")

    # Retry — overwrite с completed.
    raw_completed = _make_manifest_dict(
        op_id,
        status="completed",
        chunks_total=2,
        batches_done=["cb_000", "cb_001"],
    )
    path.write_text(json.dumps(raw_completed, ensure_ascii=False), encoding="utf-8")

    loaded = load_manifest(op_id, workspace_root=ws)
    assert loaded.status == "completed"
    assert loaded.batches_failed == []


# ---------------------------------------------------------------------------
# Scenario C: partial → reload → status preserved + failed batches
# ---------------------------------------------------------------------------


def test_resume_scenario_c_partial_manifest_roundtrip(tmp_path):
    """Scenario C: status='partial' → reload → status preserved.

    Partial = есть успешные batches + batches_failed.
    """
    from workspace.skills.legal_summarizer.scripts.manifest import (
        load_manifest,
        manifest_path,
    )

    ws = _workspace(tmp_path)
    op_id = "op_scenario_c"

    raw = _make_manifest_dict(
        op_id,
        status="partial",
        chunks_total=3,
        batches_done=["cb_000"],
        batches_failed=["cb_001"],
        chunk_states={
            "000": {"status": "completed", "context_batch_id": "cb_000",
                     "section_id": "s_1", "section_path": "1"},
            "001": {"status": "completed", "context_batch_id": "cb_000",
                     "section_id": "s_1", "section_path": "1"},
            "002": {"status": "failed", "context_batch_id": "cb_001",
                     "section_id": "s_2", "section_path": "2"},
        },
    )
    path = manifest_path(op_id, ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    loaded = load_manifest(op_id, workspace_root=ws)
    assert loaded is not None
    assert loaded.status == "partial"
    assert "cb_001" in loaded.batches_failed
    completed = [
        cid for cid, st in loaded.chunk_states.items() if st["status"] == "completed"
    ]
    failed = [
        cid for cid, st in loaded.chunk_states.items() if st["status"] == "failed"
    ]
    assert len(completed) == 2
    assert len(failed) == 1


def test_resume_scenario_c_partial_chunk_results_persist(tmp_path):
    """Scenario C: chunk_results для completed chunks записаны, для failed — нет."""
    from workspace.skills.legal_summarizer.scripts.manifest import (
        chunk_result_path,
        read_chunk_result,
        write_chunk_result,
    )

    ws = _workspace(tmp_path)
    op_id = "op_scenario_c_results"

    # Записать result для completed chunk.
    write_chunk_result(
        op_id, "000",
        "Саммари чанка 0",
        context_batch_id="cb_000", section_id="s_1", section_path="1",
        page_start=1, page_end=1, duration_sec=1.5,
        workspace_root=ws,
    )
    # Failed chunk — result НЕ пишется (предполагаем).
    path_0 = chunk_result_path(op_id, "000", ws)
    assert path_0.exists()
    data = read_chunk_result(op_id, "000", workspace_root=ws)
    assert data["summary"] == "Саммари чанка 0"
    assert data["context_batch_id"] == "cb_000"

    # 001 — нет файла.
    path_1 = chunk_result_path(op_id, "001", ws)
    assert not path_1.exists()


# ---------------------------------------------------------------------------
# Scenario D: cache reuse — пропуск map-фазы
# ---------------------------------------------------------------------------


def test_resume_scenario_d_cache_reuse_loads_chunks(tmp_path):
    """Scenario D: document-cache → load_doc_cache → chunks восстановлены."""
    from workspace.skills.legal_summarizer.scripts.document_cache import (
        load_doc_cache,
        save_doc_cache,
    )

    ws = _workspace(tmp_path)
    session_key = "test_session_d"
    document_id = "doc_abc123"

    # Simulate cache write.
    chunks = {
        "000": {"chunk_id": "000", "summary": "Саммари 1"},
        "001": {"chunk_id": "001", "summary": "Саммари 2"},
    }
    save_doc_cache(document_id, session_key, ws, chunks)

    # Reload.
    loaded = load_doc_cache(document_id, session_key, ws)
    assert len(loaded) == 2
    assert loaded["000"]["summary"] == "Саммари 1"
    assert loaded["001"]["summary"] == "Саммари 2"


def test_resume_scenario_d_empty_cache_returns_empty_dict(tmp_path):
    """Scenario D: cache не существует → empty dict (не raise)."""
    from workspace.skills.legal_summarizer.scripts.document_cache import (
        load_doc_cache,
    )

    ws = _workspace(tmp_path)
    loaded = load_doc_cache("nonexistent_doc", "nonexistent_session", ws)
    assert loaded == {}


def test_resume_scenario_d_cache_isolated_by_session(tmp_path):
    """Scenario D: разные session_key — разные cache."""
    from workspace.skills.legal_summarizer.scripts.document_cache import (
        load_doc_cache,
        save_doc_cache,
    )

    ws = _workspace(tmp_path)
    document_id = "doc_iso"

    save_doc_cache(
        document_id, "session_1", ws,
        {"000": {"chunk_id": "000", "summary": "S1"}},
    )
    save_doc_cache(
        document_id, "session_2", ws,
        {"000": {"chunk_id": "000", "summary": "S2"}},
    )

    assert load_doc_cache(document_id, "session_1", ws)["000"]["summary"] == "S1"
    assert load_doc_cache(document_id, "session_2", ws)["000"]["summary"] == "S2"


# ---------------------------------------------------------------------------
# Integration: run → manifest persists → reload
# ---------------------------------------------------------------------------


def test_resume_integration_run_writes_manifest(tmp_path, monkeypatch):
    """Integration: summarizer.run (map_reduce) → manifest записан на диск.

    Single-path НЕ пишет manifest (только result.json). Используем
    map_reduce: chunk_size=200 → много chunks → manifest пишется.
    """
    import summarizer

    monkeypatch.setattr(summarizer, "get_chunking_config", lambda: {
        "chunk_size": 200, "chunk_overlap": 0, "single_call_threshold": 100,
        "chunk_size_input_ratio": None,
    })
    monkeypatch.setattr(summarizer, "get_execution_config", lambda: {
        "confirmation_threshold_sec": 0.001, "estimated_chunk_duration_sec": 0.001,
        "max_chunks_for_execution": 100,
        "context_batching": {
            "system_prompt_tokens": 100, "instruction_tokens_per_map": 50,
            "chars_per_token": 3.5, "safety_margin": 0.85,
        },
        "llm_max_tokens": 100,
    })

    def fake_chat(messages, *, context=None, **kwargs):
        # Map-вызовы — текстовый формат с DOC CHUNK N.
        user_content = messages[1]["content"]
        import re as _re
        if _re.findall(r"DOCUMENT CHUNK \d+", user_content):
            n = len(_re.findall(r"DOCUMENT CHUNK \d+", user_content))
            return "\n\n".join(
                f"DOC CHUNK {i + 1}: саммари чанка {i + 1}" for i in range(n)
            ) + "\n"
        # Reduce-вызов.
        return "Тест. Саммари для integration теста."

    monkeypatch.setattr(summarizer.llm, "chat", fake_chat)

    paragraph = "Тестовый абзац документа для проверки записи manifest. "
    text = "\n\n".join([paragraph] * 50)
    result = summarizer.run(
        text, length="brief", confirmed=True, workspace_root=tmp_path,
    )
    assert result["status"] == "completed"
    op_id = result["operation_id"]

    # Manifest должен быть на диске (manifest_root = tmp_path/workspace/...).
    from workspace.skills.legal_summarizer.scripts.manifest import (
        load_manifest,
        manifest_path,
    )

    loaded = load_manifest(op_id, workspace_root=tmp_path)
    assert loaded is not None, (
        f"manifest не найден по пути {manifest_path(op_id, tmp_path)}"
    )
    assert loaded.status == "completed"
