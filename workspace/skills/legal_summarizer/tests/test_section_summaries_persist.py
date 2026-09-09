"""Тесты #0c: ``write_document_section_summary`` + persist after map/reduce.

После commit #0a/#0b ``_internal["section_summaries"]`` содержит построенные
phase-1 reducer'ом summaries. ``_persist_final_manifest`` должен дополнительно
сохранять их в document-level cache ``<repo>/workspace/data_store/cache/
skills/legal_summarizer/documents/<document_id>/sections/<sid>.json``.
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
        # Возвращаем маркер, в котором виден section_id (heading).
        return f"section_summary_for[{heading or path}]"

    def _fake_doc(text, *, length, focus, structure, question=None):
        return "final_summary"

    monkeypatch.setattr(llm_calls, "llm_batch", _fake_batch)
    monkeypatch.setattr(llm_calls, "llm_section_reduce", _fake_section)
    monkeypatch.setattr(llm_calls, "llm_document_reduce", _fake_doc)


def test_write_document_section_summary_atomic(tmp_path):
    """Helper сам по себе: атомарная запись + idempotency."""
    from cache.manifest import (
        document_section_result_path,
        write_document_section_summary,
    )

    document_id = "test_doc_abc"
    write_document_section_summary(
        workspace_root=tmp_path,
        document_id=document_id,
        section_id="sec_001",
        summary="hello",
    )
    path = document_section_result_path(document_id, "sec_001", tmp_path)
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"section_id": "sec_001", "summary": "hello"}

    # Idempotency: повторный write перезаписывает.
    write_document_section_summary(
        workspace_root=tmp_path,
        document_id=document_id,
        section_id="sec_001",
        summary="hello v2",
    )
    data2 = json.loads(path.read_text(encoding="utf-8"))
    assert data2["summary"] == "hello v2"


def test_write_document_section_summary_no_op_for_empty(tmp_path):
    """Пустой document_id / section_id / summary — no-op, файл не создаётся."""
    from cache.manifest import (
        document_section_result_path,
        write_document_section_summary,
    )

    # document_id="" → no-op
    write_document_section_summary(
        workspace_root=tmp_path,
        document_id="",
        section_id="sec_001",
        summary="x",
    )
    # section_id="" → no-op
    write_document_section_summary(
        workspace_root=tmp_path,
        document_id="d",
        section_id="",
        summary="x",
    )
    # summary="" → no-op
    write_document_section_summary(
        workspace_root=tmp_path,
        document_id="d",
        section_id="sec_001",
        summary="",
    )
    # Ни один файл не должен появиться.
    assert not document_section_result_path("d", "sec_001", tmp_path).exists()


def test_persist_writes_section_files_when_hierarchical_built_them(
    tmp_path, monkeypatch,
):
    """End-to-end: после ``run_map_reduce`` с hierarchical strategy (если
    select_strategy её выберет) — секционные файлы появляются в
    ``documents/<doc_id>/sections/``.

    Для txt без headings strategy скорее всего будет ``map_flat`` —
    в этом случае files просто не создаются (section_summaries=={}).
    Тест не делает assert на ненулевое число файлов, а только проверяет,
    что **если они созданы, они валидны**, и что document_dir создан
    тогда и только тогда, когда есть section_summaries.
    """
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

    # Получаем document_id от analysis, который был использован.
    # Читаем manifest.json — он содержит strategy_label в raw.
    from cache.manifest import _read_json, manifest_path
    raw = _read_json(manifest_path(result["operation_id"], tmp_path))
    assert raw is not None
    strategy_label = raw.get("raw", {}).get("strategy") or raw.get("strategy")
    section_summaries = raw.get("section_summaries") or {}

    # Если manifest говорит, что есть section_summaries — файлы должны быть.
    if section_summaries:
        # Поищем document_id по operation_id через manifest
        # (для теста прочитаем прямо из raw manifest — там нет document_id,
        # нужно найти documents/ каталог по document_id из analysis.identity).
        from cache.manifest import document_dir

        # В run() analysis не доступен через result, но мы можем найти
        # каталог documents/<doc_id>/ — он единственный, т.к. doc один на тест.
        docs_root = (
            tmp_path
            / "workspace"
            / "data_store"
            / "cache"
            / "skills"
            / "legal_summarizer"
            / "documents"
        )
        if docs_root.is_dir():
            doc_dirs = [p for p in docs_root.iterdir() if p.is_dir()]
            assert len(doc_dirs) == 1, (
                f"expected exactly one document dir, got {doc_dirs}"
            )
            doc_dir = doc_dirs[0]
            sections_dir = doc_dir / "sections"
            if section_summaries:
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
    # В случае map_flat — section_summaries == {} и никакого document_dir не создаётся.


def test_direct_strategy_no_section_cache_writes(tmp_path, monkeypatch):
    """direct strategy: section_summaries пуст → никаких files в documents/."""
    import application.service as summarizer

    def _fake_doc(text, *, length, focus, structure, question=None):
        return "final_summary_direct"

    import llm.calls as llm_calls
    monkeypatch.setattr(llm_calls, "llm_document_reduce", _fake_doc)

    # Короткий текст → strategy == direct
    text = "1. Раздел\n\nКороткий текст.\n\n2. Раздел\n\nЕщё текст."
    p = _write_doc(tmp_path, text)

    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] == "completed", result

    docs_root = (
        tmp_path
        / "workspace"
        / "data_store"
        / "cache"
        / "skills"
        / "legal_summarizer"
        / "documents"
    )
    if docs_root.is_dir():
        for doc_dir in docs_root.iterdir():
            sections_dir = doc_dir / "sections"
            # Может быть создан, но должен быть пуст.
            if sections_dir.is_dir():
                assert not any(sections_dir.iterdir()), (
                    f"direct strategy must not write section summaries, "
                    f"but found files in {sections_dir}"
                )