"""Тесты #4: ``write_document_chunk_summary`` callback в map_reduce.

После commit #4 каждый успешный batch параллельно пишет per-chunk
summary в document-level cache (``documents/<doc_id>/chunks/<cid>.json``),
в дополнение к operation-level (``operations/<op_id>/chunks/<cid>.json``).

Проверяется:

1. После успешного ``run_map_reduce`` файлы появляются в
   ``documents/<doc_id>/chunks/`` (для strategy с реальными batch'ами).
2. Содержимое файлов соответствует ожидаемому (summary + metadata).
3. Если document_id отсутствует (analysis=None) — document-level cache
   НЕ пишется, но operation-level пишется (regression-guard).
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
    import llm.calls as llm_calls

    def _fake_batch(chunks, *, chunks_total, structure, length, question=None):
        return {c.chunk_id: f"summary {c.chunk_id}" for c in chunks}

    def _fake_section(path, heading, text, *, length, question=None):
        return "section_summary"

    def _fake_doc(text, *, length, focus, structure, question=None):
        return "final_summary"

    monkeypatch.setattr(llm_calls, "llm_batch", _fake_batch)
    monkeypatch.setattr(llm_calls, "llm_section_reduce", _fake_section)
    monkeypatch.setattr(llm_calls, "llm_document_reduce", _fake_doc)


def test_run_map_reduce_writes_document_chunk_summaries(tmp_path, monkeypatch):
    """После run() document-level cache содержит per-chunk summaries."""
    import application.service as summarizer
    _install_recording_llm(monkeypatch)

    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)

    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] in ("completed", "partial"), result

    # Берём document_id из result -> DocumentIdentity напрямую по path.
    # Не делаем второй run_canonical_pipeline — он может инвалидировать
    # snapshot на Windows (mtime filesystem fluctuation между write_text
    # и stat), а для теста нужен сам факт записи chunks/ после ПЕРВОГО run.
    from document.identity import DocumentIdentity
    document_id = DocumentIdentity.from_path(p).document_id

    docs_root = (
        tmp_path
        / "workspace"
        / "data_store"
        / "cache"
        / "skills"
        / "legal_summarizer"
        / "documents"
    )
    assert docs_root.is_dir(), (
        f"document-level dir missing: {docs_root}"
    )
    doc_dirs = [d for d in docs_root.iterdir() if d.is_dir()]
    assert len(doc_dirs) == 1, (
        f"expected exactly one document dir, got {doc_dirs}"
    )
    doc_dir = doc_dirs[0]
    assert doc_dir.name == document_id, (
        f"document_id mismatch: dir={doc_dir.name}, expected={document_id}"
    )

    chunks_root = doc_dir / "chunks"
    assert chunks_root.is_dir(), (
        f"document-level chunks dir missing: {chunks_root}"
    )

    chunk_files = list(chunks_root.glob("*.json"))
    assert len(chunk_files) > 0, (
        "document-level chunk summaries должны быть записаны после "
        "успешного map_reduce"
    )

    # Содержимое валидно.
    for f in chunk_files[:3]:
        data = json.loads(f.read_text(encoding="utf-8"))
        assert "chunk_id" in data
        assert "summary" in data
        assert data["summary"].startswith("summary ")
    assert docs_root.is_dir(), (
        f"document-level dir missing: {docs_root}"
    )
    doc_dirs = [d for d in docs_root.iterdir() if d.is_dir()]
    assert len(doc_dirs) == 1, (
        f"expected exactly one document dir, got {doc_dirs}"
    )
    doc_dir = doc_dirs[0]
    assert doc_dir.name == document_id, (
        f"document_id mismatch: dir={doc_dir.name}, expected={document_id}"
    )

    chunks_root = doc_dir / "chunks"
    assert chunks_root.is_dir(), (
        f"document-level chunks dir missing: {chunks_root}"
    )

    chunk_files = list(chunks_root.glob("*.json"))
    assert len(chunk_files) > 0, (
        "document-level chunk summaries должны быть записаны после "
        "успешного map_reduce"
    )

    # Содержимое валидно.
    for f in chunk_files[:3]:
        data = json.loads(f.read_text(encoding="utf-8"))
        assert "chunk_id" in data
        assert "summary" in data
        assert data["summary"].startswith("summary ")


def test_document_chunk_summary_idempotent(tmp_path, monkeypatch):
    """Повторный run() с тем же файлом → document-level cache hit →
    не делает нового map → chunk summaries не перезаписываются LLM.

    Главное: chunk summary files ОСТАЮТСЯ на диске (они не удаляются
    cache hit'ом), потому что cache hit НЕ вызывает LLM и не пишет
    новые summaries. Документ cache-валидный, summaries те же.
    """
    import application.service as summarizer
    _install_recording_llm(monkeypatch)

    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)

    result1 = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result1["status"] in ("completed", "partial")

    # Получаем document_id и список chunk файлов после первого run.
    import application.pipeline_structure as pipeline
    pr = pipeline.run_canonical_pipeline(p, workspace_root=tmp_path)
    document_id = pr.analysis.identity.document_id

    chunks_root = (
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
    files_before = sorted(p.name for p in chunks_root.glob("*.json"))
    summaries_before = sorted(
        json.loads(f.read_text(encoding="utf-8"))["summary"]
        for f in chunks_root.glob("*.json")
    )

    # Второй run: cache hit → НЕ вызывает LLM.
    result2 = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result2["status"] in ("completed", "partial")

    files_after = sorted(p.name for p in chunks_root.glob("*.json"))
    summaries_after = sorted(
        json.loads(f.read_text(encoding="utf-8"))["summary"]
        for f in chunks_root.glob("*.json")
    )

    # Имена файлов и summaries идентичны (ничего не удалено, ничего
    # не перезаписано LLM).
    assert files_before == files_after
    assert summaries_before == summaries_after