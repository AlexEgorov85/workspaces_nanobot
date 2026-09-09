"""Тесты #C1/C2/C3 — corrective pass.

C1: document-level cache хранит только question-independent summaries.
    При question is not None → write_document_chunk_summary/section_summary
    становится no-op.
C2: question shortcut создаёт полноценный NormalizedManifest (status=completed)
    для idempotency — второй вызов с тем же operation_id не делает LLM.
C3: document-level summaries (chunk + section) НЕ сохраняются в question mode.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


# ============================================================================
# C1: question guard
# ============================================================================

def test_write_document_chunk_summary_question_guard_no_op(tmp_path):
    """question is not None → no-op (не сохраняется в document cache)."""
    from cache.manifest import (
        document_chunk_result_path,
        write_document_chunk_summary,
    )

    document_id = "d_c1"
    write_document_chunk_summary(
        workspace_root=tmp_path,
        document_id=document_id,
        chunk_id="001",
        summary="question-specific summary",
        question="What about X?",
    )
    # Файл НЕ должен появиться.
    path = document_chunk_result_path(document_id, "001", tmp_path)
    assert not path.exists(), (
        "write_document_chunk_summary с question=... должен быть no-op "
        "(document cache хранит только question-independent summaries)"
    )


def test_write_document_chunk_summary_question_none_writes(tmp_path):
    """question is None → summary сохраняется в document cache."""
    from cache.manifest import (
        document_chunk_result_path,
        write_document_chunk_summary,
    )

    document_id = "d_c1b"
    write_document_chunk_summary(
        workspace_root=tmp_path,
        document_id=document_id,
        chunk_id="001",
        summary="baseline summary",
        question=None,
    )
    path = document_chunk_result_path(document_id, "001", tmp_path)
    assert path.is_file()


def test_write_document_section_summary_question_guard_no_op(tmp_path):
    """question is not None → no-op для section summaries."""
    from cache.manifest import (
        document_section_result_path,
        write_document_section_summary,
    )

    document_id = "d_c1c"
    write_document_section_summary(
        workspace_root=tmp_path,
        document_id=document_id,
        section_id="s_0001",
        summary="question-specific section summary",
        question="Specific Q",
    )
    path = document_section_result_path(document_id, "s_0001", tmp_path)
    assert not path.exists()


def test_write_document_section_summary_question_none_writes(tmp_path):
    """question is None → summary сохраняется."""
    from cache.manifest import (
        document_section_result_path,
        write_document_section_summary,
    )

    document_id = "d_c1d"
    write_document_section_summary(
        workspace_root=tmp_path,
        document_id=document_id,
        section_id="s_0001",
        summary="baseline section summary",
        question=None,
    )
    assert document_section_result_path(document_id, "s_0001", tmp_path).is_file()


def test_run_map_reduce_question_mode_no_document_chunk_summaries(tmp_path, monkeypatch):
    """End-to-end: question mode → documents/<doc_id>/chunks/*.json НЕ создаются
    (только operations/<op_id>/chunks/)."""
    import application.service as summarizer
    import llm.calls as llm_calls

    def _fake_batch(chunks, *, chunks_total, structure, length, question=None):
        return {c.chunk_id: f"summary {c.chunk_id}" for c in chunks}

    monkeypatch.setattr(
        llm_calls, "llm_batch",
        lambda chunks, **kw: {c.chunk_id: f"summary_{c.chunk_id}_q_{kw.get('question','')}" for c in chunks},
    )
    monkeypatch.setattr(llm_calls, "llm_section_reduce", lambda *a, **kw: "section_summary")
    monkeypatch.setattr(llm_calls, "llm_document_reduce", lambda *a, **kw: "final")

    # Большой текст для map-стратегии.
    parts = []
    for i in range(1, 7):
        parts.append(
            f"{i}. Раздел {i}\n\n" + ("Текст. " * 50) * 200 + "\n\n"
        )
    text = "".join(parts)
    p = tmp_path / "doc.txt"
    p.write_text(text, encoding="utf-8")

    # 1. Run с question → map_reduce (question-specific LLM).
    summarizer.run(
        text, question="Специфический вопрос",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )

    # 2. Проверяем: documents/<doc_id>/chunks/*.json НЕ созданы.
    docs_root = (
        tmp_path
        / "workspace"
        / "data_store"
        / "cache"
        / "skills"
        / "legal_summarizer"
        / "documents"
    )
    from document.identity import DocumentIdentity
    document_id = DocumentIdentity.from_path(p).document_id
    chunks_dir = docs_root / document_id / "chunks"
    if chunks_dir.exists():
        # Если chunks_dir создан — он должен быть пустым (нет question-specific
        # summaries в document cache).
        files = list(chunks_dir.iterdir())
        assert len(files) == 0, (
            f"question mode не должен писать chunk summaries в document "
            f"cache, но найдено {len(files)} файлов: {files}"
        )


def test_run_map_reduce_no_question_writes_document_chunk_summaries(tmp_path, monkeypatch):
    """End-to-end: non-question (length=detailed) → document chunks/*.json создаются."""
    import application.service as summarizer
    import llm.calls as llm_calls

    monkeypatch.setattr(
        llm_calls, "llm_batch",
        lambda chunks, **kw: {c.chunk_id: f"summary_{c.chunk_id}" for c in chunks},
    )
    monkeypatch.setattr(llm_calls, "llm_section_reduce", lambda *a, **kw: "section")
    monkeypatch.setattr(llm_calls, "llm_document_reduce", lambda *a, **kw: "final")

    parts = []
    for i in range(1, 7):
        parts.append(
            f"{i}. Раздел {i}\n\n" + ("Текст. " * 50) * 200 + "\n\n"
        )
    text = "".join(parts)
    p = tmp_path / "doc.txt"
    p.write_text(text, encoding="utf-8")

    # Без question → length=detailed → map → chunk summaries пишутся.
    summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )

    from document.identity import DocumentIdentity
    document_id = DocumentIdentity.from_path(p).document_id
    chunks_dir = (
        tmp_path
        / "workspace"
        / "data_store"
        / "cache"
        / "skills"
        / "legal_summarizer"
        / "documents"
        / document_id
        / "chunks"
    )
    assert chunks_dir.is_dir(), (
        f"non-question run должен создать document chunk summaries, "
        f"но chunks_dir отсутствует"
    )
    assert len(list(chunks_dir.iterdir())) > 0


# ============================================================================
# C2: idempotency question shortcut
# ============================================================================

def test_question_shortcut_creates_manifest_for_idempotency(tmp_path, monkeypatch):
    """После успешного --question shortcut manifest.json со status=completed
    создаётся. Второй вызов с тем же operation_id проходит через
    idempotency-check (manifest hit) и НЕ делает LLM."""
    import application.service as summarizer
    import llm.calls as llm_calls
    import cache.manifest as cm

    # Сначала создаём document cache через length=brief.
    monkeypatch.setattr(
        llm_calls, "llm_document_reduce", lambda *a, **kw: "brief_summary",
    )

    p = tmp_path / "doc.txt"
    text = "1. Раздел\n\n" + ("Тек. " * 50) * 200 + "\n\n"
    p.write_text(text, encoding="utf-8")

    summarizer.run(
        text, length="brief",
        document_path=str(p), workspace_root=tmp_path,
    )

    # Теперь --question → document_cache_question shortcut.
    recorded = {"doc_reduce_calls": 0}

    def _counting_doc_reduce(*a, **kw):
        recorded["doc_reduce_calls"] += 1
        return "question_answer"

    monkeypatch.setattr(llm_calls, "llm_document_reduce", _counting_doc_reduce)

    # Первый вызов.
    result1 = summarizer.run(
        text, question="Вопрос",
        document_path=str(p), workspace_root=tmp_path,
    )
    assert result1["status"] == "completed"
    assert recorded["doc_reduce_calls"] == 1

    # Второй вызов с тем же вопросом → idempotency, 0 LLM calls.
    result2 = summarizer.run(
        text, question="Вопрос",
        document_path=str(p), workspace_root=tmp_path,
    )
    assert result2["status"] == "completed"
    assert result2["stats"].get("cached") is True, (
        "второй вызов должен вернуть cached=True без LLM call, "
        f"получили {result2['stats']}"
    )
    assert recorded["doc_reduce_calls"] == 1, (
        f"второй вызов НЕ должен вызывать llm_document_reduce, "
        f"но было {recorded['doc_reduce_calls']} вызовов"
    )


def test_question_shortcut_manifest_status_completed(tmp_path, monkeypatch):
    """manifest.json после question shortcut имеет status=completed."""
    import application.service as summarizer
    import llm.calls as llm_calls

    monkeypatch.setattr(
        llm_calls, "llm_document_reduce", lambda *a, **kw: "answer",
    )

    p = tmp_path / "doc.txt"
    text = "1. Раздел\n\n" + ("Тек. " * 50) * 200 + "\n\n"
    p.write_text(text, encoding="utf-8")

    summarizer.run(
        text, length="brief",
        document_path=str(p), workspace_root=tmp_path,
    )

    operation_id = None
    result = summarizer.run(
        text, question="?",
        document_path=str(p), workspace_root=tmp_path,
    )
    operation_id = result["operation_id"]

    from cache.manifest import load_manifest
    manifest = load_manifest(operation_id, tmp_path)
    assert manifest is not None, "manifest должен быть создан"
    assert manifest.status == "completed", (
        f"manifest.status должен быть 'completed', got {manifest.status!r}"
    )
    assert manifest.raw.get("strategy") == "document_cache_question"
    assert manifest.raw.get("document_id") is not None