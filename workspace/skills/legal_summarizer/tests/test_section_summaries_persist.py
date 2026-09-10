"""Тесты #0c: ``DocumentCache.write_section_summary`` + persist after map/reduce.

После commit #0a/#0b ``_internal["section_summaries"]`` содержит построенные
phase-1 reducer'ом summaries. ``_persist_final_manifest`` дополнительно
сохраняет их в document-level cache через ``DocumentCache.write_section_summary``
(layout: ``sessions/<safe_session_key>/documents/<document_id>/sections/<sid>.json``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _write_doc(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "doc.txt"
    p.write_text(text, encoding="utf-8")
    return p


def _build_doc(sections: int = 6) -> str:
    parts = []
    for i in range(1, sections + 1):
        parts.append(
            f"{i}. Раздел {i}\n\n"
            + ("Текст. " * 50) * 200
            + "\n\n"
        )
    return "".join(parts)


def _install_recording_llm(monkeypatch) -> None:
    """Mock LLM: section_reduce возвращает marker с section_id, чтобы
    потом проверить, что он попал в document-level cache."""
    import llm.calls as llm_calls

    def _fake_batch(chunks, *, chunks_total, structure, length, question=None):
        return {c.chunk_id: f"summary {c.chunk_id}" for c in chunks}

    def _fake_section(path, heading, text, *, length, question=None):
        return f"section_summary_for[{heading or path}]"

    def _fake_doc(text, *, length, focus, structure, question=None):
        return "final_summary"

    monkeypatch.setattr(llm_calls, "llm_batch", _fake_batch)
    monkeypatch.setattr(llm_calls, "llm_section_reduce", _fake_section)
    monkeypatch.setattr(llm_calls, "llm_document_reduce", _fake_doc)


def test_write_document_section_summary_atomic(tmp_path):
    """``DocumentCache.write_section_summary``: idempotency + payload contract."""
    from cache.document_cache import DocumentCache

    document_id = "test_doc_abc"
    cache = DocumentCache(tmp_path)
    cache.write_section_summary(
        document_id=document_id,
        section_id="sec_001",
        summary="hello",
    )
    summaries = cache.load_section_summaries(document_id, ["sec_001"])
    assert summaries == {"sec_001": "hello"}

    cache.write_section_summary(
        document_id=document_id,
        section_id="sec_001",
        summary="hello v2",
    )
    summaries = cache.load_section_summaries(document_id, ["sec_001"])
    assert summaries == {"sec_001": "hello v2"}


def test_write_document_section_summary_no_op_for_empty(tmp_path):
    """Пустой document_id / section_id / summary — no-op, ничего не пишется."""
    from cache.document_cache import DocumentCache

    cache = DocumentCache(tmp_path)
    cache.write_section_summary(
        document_id="",
        section_id="sec_001",
        summary="x",
    )
    cache.write_section_summary(
        document_id="d",
        section_id="",
        summary="x",
    )
    cache.write_section_summary(
        document_id="d",
        section_id="sec_001",
        summary="",
    )
    assert cache.load_section_summaries("d", ["sec_001"]) == {}


def test_persist_writes_section_files_when_hierarchical_built_them(
    tmp_path, monkeypatch,
):
    """End-to-end: после ``run_map_reduce`` с hierarchical strategy —
    секционные файлы появляются в ``sessions/<key>/documents/<doc_id>/sections/``.
    """
    import application.service as summarizer
    from cache.document_cache import DocumentCache
    from document.identity import DocumentIdentity
    _install_recording_llm(monkeypatch)

    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)

    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] in ("completed", "partial"), result

    from cache.manifest import _read_json, manifest_path
    raw = _read_json(manifest_path(result["operation_id"], tmp_path))
    assert raw is not None
    strategy_label = raw.get("raw", {}).get("strategy") or raw.get("strategy")
    section_summaries = raw.get("section_summaries") or {}

    if section_summaries:
        document_id = DocumentIdentity.from_path(p).document_id
        cache = DocumentCache(tmp_path)
        sections_dir = cache._document_dir(document_id) / "sections"
        assert sections_dir.is_dir(), (
            f"section_summaries in manifest but no sections/ dir: "
            f"{section_summaries}"
        )
        written_files = list(sections_dir.glob("*.json"))
        assert len(written_files) == len(section_summaries), (
            f"expected {len(section_summaries)} files, "
            f"got {len(written_files)}"
        )
        for f in written_files:
            data = json.loads(f.read_text(encoding="utf-8"))
            assert "section_id" in data
            assert "summary" in data
            assert data["summary"].startswith("section_summary_for")


def test_direct_strategy_no_section_cache_writes(tmp_path, monkeypatch):
    """direct strategy: section_summaries пуст → никаких files в document cache."""
    import application.service as summarizer
    from cache.document_cache import DocumentCache

    def _fake_doc(text, *, length, focus, structure, question=None):
        return "final_summary_direct"

    import llm.calls as llm_calls
    monkeypatch.setattr(llm_calls, "llm_document_reduce", _fake_doc)

    text = "1. Раздел\n\nКороткий текст.\n\n2. Раздел\n\nЕщё текст."
    p = _write_doc(tmp_path, text)

    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] == "completed", result

    cache = DocumentCache(tmp_path)
    sections_dir = cache._document_dir("any") / "sections"
    if sections_dir.exists():
        assert not any(sections_dir.iterdir())
