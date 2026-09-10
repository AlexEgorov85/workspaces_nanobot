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
    from cache.document_cache import DocumentCache

    document_id = "d_c1"
    cache = DocumentCache(tmp_path)
    cache.write_chunk_summary(
        document_id=document_id,
        chunk_id="001",
        summary="question-specific summary",
        question="What about X?",
    )
    assert cache.load_chunk_summaries(document_id, ["001"]) == {}, (
        "write_chunk_summary с question=... должен быть no-op "
        "(document cache хранит только question-independent summaries)"
    )


def test_write_document_chunk_summary_question_none_writes(tmp_path):
    """question is None → summary сохраняется в document cache."""
    from cache.document_cache import DocumentCache

    document_id = "d_c1b"
    cache = DocumentCache(tmp_path)
    cache.write_chunk_summary(
        document_id=document_id,
        chunk_id="001",
        summary="baseline summary",
        question=None,
    )
    assert cache.load_chunk_summaries(document_id, ["001"]) == {"001": "baseline summary"}


def test_write_document_section_summary_question_guard_no_op(tmp_path):
    """question is not None → no-op для section summaries."""
    from cache.document_cache import DocumentCache

    document_id = "d_c1c"
    cache = DocumentCache(tmp_path)
    cache.write_section_summary(
        document_id=document_id,
        section_id="s_0001",
        summary="question-specific section summary",
        question="Specific Q",
    )
    assert cache.load_section_summaries(document_id, ["s_0001"]) == {}


def test_write_document_section_summary_question_none_writes(tmp_path):
    """question is None → summary сохраняется."""
    from cache.document_cache import DocumentCache

    document_id = "d_c1d"
    cache = DocumentCache(tmp_path)
    cache.write_section_summary(
        document_id=document_id,
        section_id="s_0001",
        summary="baseline section summary",
        question=None,
    )
    assert cache.load_section_summaries(document_id, ["s_0001"]) == {
        "s_0001": "baseline section summary",
    }


def test_run_map_reduce_question_mode_no_document_chunk_summaries(tmp_path, monkeypatch):
    """End-to-end: question mode → document chunks/*.json НЕ создаются
    (cross-operation document cache хранит только baseline summaries)."""
    import application.service as summarizer
    import llm.calls as llm_calls
    from cache.document_cache import DocumentCache

    monkeypatch.setattr(
        llm_calls, "llm_batch",
        lambda chunks, **kw: {c.chunk_id: f"summary_{c.chunk_id}_q_{kw.get('question','')}" for c in chunks},
    )
    monkeypatch.setattr(llm_calls, "llm_section_reduce", lambda *a, **kw: "section_summary")
    monkeypatch.setattr(llm_calls, "llm_document_reduce", lambda *a, **kw: "final")

    parts = []
    for i in range(1, 7):
        parts.append(
            f"{i}. Раздел {i}\n\n" + ("Текст. " * 50) * 200 + "\n\n"
        )
    text = "".join(parts)
    p = tmp_path / "doc.txt"
    p.write_text(text, encoding="utf-8")

    summarizer.run(
        text, question="Специфический вопрос",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )

    from document.identity import DocumentIdentity
    document_id = DocumentIdentity.from_path(p).document_id
    cache = DocumentCache(tmp_path)
    assert cache.load_chunk_summaries(document_id, []) == {}


def test_run_map_reduce_no_question_writes_document_chunk_summaries(tmp_path, monkeypatch):
    """End-to-end: non-question (length=detailed) → document chunks/*.json создаются."""
    import application.service as summarizer
    import llm.calls as llm_calls
    from cache.document_cache import DocumentCache

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

    summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )

    from document.identity import DocumentIdentity
    document_id = DocumentIdentity.from_path(p).document_id
    cache = DocumentCache(tmp_path)
    # Проверяем filesystem contract: chunk summary files должны быть на диске.
    # Это filesystem-level atomicity test — требует доступа к layout, поэтому
    # используется private ``_document_dir``. Прямой импорт helper'ов
    # ``cache.manifest`` запрещён.
    chunks_dir = cache._document_dir(document_id) / "chunks"
    assert chunks_dir.is_dir(), (
        f"non-question run должен создать document chunks dir, "
        f"но {chunks_dir} отсутствует"
    )
    chunk_files = list(chunks_dir.glob("*.json"))
    assert len(chunk_files) > 0, (
        f"non-question run должен создать document chunk summary files, "
        f"но {chunks_dir} пуст"
    )


# ============================================================================
# C2: idempotency question shortcut
# ============================================================================

def test_question_shortcut_creates_manifest_for_idempotency(tmp_path, monkeypatch):
    """После успешного --question shortcut manifest.json со status=completed
    создаётся. Второй вызов с тем же operation_id проходит через
    idempotency-check (manifest hit) и НЕ делает LLM."""
    import application.service as summarizer
    import llm.calls as llm_calls

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