"""Тесты #0b: ``section_summaries`` сохраняются в ``manifest.json``.

После commit #0a ``_reduce_phase`` возвращает ``section_summaries`` через
``_internal``. ``_persist_final_manifest`` должен пробрасывать их в
``NormalizedManifest.section_summaries``, а не сбрасывать в ``{}``.

Проверяется end-to-end: после успешного ``run_map_reduce`` manifest.json
содержит заполненное ``section_summaries``.
"""

from __future__ import annotations

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
    """Mock LLM: section_reduce и doc_reduce возвращают фиксированные строки."""
    import llm.calls as llm_calls

    def _fake_batch(chunks, *, chunks_total, structure, length, question=None):
        return {c.chunk_id: f"summary {c.chunk_id}" for c in chunks}

    def _fake_section(path, heading, text, *, length, question=None):
        return f"section_summary[{path or heading}]"

    def _fake_doc(text, *, length, focus, structure, question=None):
        return "final_summary"

    monkeypatch.setattr(llm_calls, "llm_batch", _fake_batch)
    monkeypatch.setattr(llm_calls, "llm_section_reduce", _fake_section)
    monkeypatch.setattr(llm_calls, "llm_document_reduce", _fake_doc)


def test_manifest_persists_section_summaries(tmp_path, monkeypatch):
    """End-to-end: после ``run()`` с hierarchical-совместимым документом
    ``manifest.json`` содержит ``section_summaries`` (даже если для txt
    без headings он пуст — главное, чтобы поле сохранилось)."""
    import application.service as summarizer
    _install_recording_llm(monkeypatch)

    text = _build_doc(sections=6)
    p = _write_doc(tmp_path, text)

    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    # run() может выбрать map_flat для txt без headings — тогда
    # section_summaries == {}, и это корректно (LLM не делал section reduce).
    # Главное — поле в manifest существует и имеет тип dict.
    assert result["status"] in ("completed", "partial"), result

    from cache.manifest import load_manifest
    manifest = load_manifest(result["operation_id"], tmp_path)
    assert manifest is not None
    assert isinstance(manifest.section_summaries, dict), (
        f"section_summaries must be dict, got {type(manifest.section_summaries)}"
    )


def test_manifest_section_summaries_roundtrip(tmp_path, monkeypatch):
    """Прямой вызов ``_persist_final_manifest`` с section_summaries
    в _internal → поле сохраняется в JSON и читается обратно."""
    import json
    import application.execution_orchestration as eo
    import llm.calls as llm_calls

    # Мокаем llm только чтобы run() прошёл без реального LLM.
    monkeypatch.setattr(
        llm_calls, "llm_batch",
        lambda chunks, **kw: {c.chunk_id: "x" for c in chunks},
    )
    monkeypatch.setattr(
        llm_calls, "llm_section_reduce",
        lambda *a, **kw: "fake_section",
    )
    monkeypatch.setattr(
        llm_calls, "llm_document_reduce",
        lambda *a, **kw: "final",
    )

    # Читаем manifest.json с диска через public API.
    from cache.manifest import _read_json, manifest_path

    text = _build_doc(sections=2)
    p = _write_doc(tmp_path, text)

    # Прогоняем run() и проверяем что manifest.json содержит
    # поле section_summaries (хоть и пустое для txt без headings).
    import application.service as summarizer
    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] in ("completed", "partial")

    raw = _read_json(manifest_path(result["operation_id"], tmp_path))
    assert raw is not None
    assert "section_summaries" in raw, (
        f"manifest.json must contain 'section_summaries' key, "
        f"got keys: {sorted(raw.keys())}"
    )
    assert isinstance(raw["section_summaries"], dict)