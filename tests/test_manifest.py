"""Тесты для ``manifest.py``.

Покрывает:
    * v2 manifest read/write
    * chunk_result read/write
    * result.json read/write

Поддерживается только формат v2: ``load_manifest`` возвращает ``None``
для несовместимых (legacy v1) манифестов — v1 normalizer удалён.
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

from legal_summarizer.cache.manifest import (  # noqa: E402
    MANIFEST_VERSION_V2,
    NormalizedManifest,
    chunk_result_path,
    chunks_dir,
    load_manifest,
    manifest_path,
    manifest_root,
    read_chunk_result,
    read_result,
    save_manifest,
    write_chunk_result,
    write_result,
)


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "workspace").mkdir(parents=True)
    return ws


def test_manifest_v2_roundtrip(tmp_path):
    ws = _workspace(tmp_path)
    op_id = "op_test_v2_001"
    m = NormalizedManifest(
        operation_id=op_id,
        status="running",
        version=MANIFEST_VERSION_V2,
        document_path="/tmp/contract.pdf",
        structure_title="Договор аренды",
        chars_in=1000,
        length="medium",
        chunks_total=5,
        context_batches_total=3,
        estimated_llm_calls=4,
        actual_llm_calls=None,
        sections={"s_0001": {"level": 1, "heading": "1. Р", "section_path": "1", "chunk_ids": ["000", "001"]}},
        chunk_states={"000": {"status": "completed", "context_batch_id": "cb_000"}},
        context_batches={"cb_000": {"chunk_ids": ["000", "001"], "status": "completed"}},
        section_summaries={},
        batches_done=["cb_000"],
        batches_failed=[],
        last_error=None,
        started_at="2026-01-01T00:00:00",
        completed_at=None,
        duration_sec=None,
        article_count=42,
        raw={},
    )
    save_manifest(m, workspace_root=ws)
    loaded = load_manifest(op_id, workspace_root=ws)
    assert loaded is not None
    assert loaded.version == MANIFEST_VERSION_V2
    assert loaded.chunks_total == 5
    assert loaded.chunk_states["000"]["status"] == "completed"
    assert loaded.sections["s_0001"]["heading"] == "1. Р"
    assert loaded.article_count == 42


def test_legacy_v1_manifest_returns_none(tmp_path):
    """Legacy v1 manifest (без version/chunk_states) игнорируется."""
    ws = _workspace(tmp_path)
    op_id = "op_legacy_001"
    legacy = {
        "operation_id": op_id,
        "status": "completed",
        "chunks_total": 5,
        "batches_done": [0, 1, 2, 3, 4],
        "batches_failed": [],
        "actual_llm_calls": 6,
        "started_at": "2026-01-01T00:00:00",
        "completed_at": "2026-01-01T00:05:00",
        "duration_sec": 300.0,
        "chars_in": 1000,
        "length": "medium",
    }
    manifest_path(op_id, ws).parent.mkdir(parents=True, exist_ok=True)
    manifest_path(op_id, ws).write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    assert load_manifest(op_id, workspace_root=ws) is None


def test_legacy_manifest_not_overwritten_on_disk(tmp_path):
    """Legacy v1 manifest не читается и не перезаписывается."""
    ws = _workspace(tmp_path)
    op_id = "op_legacy_no_overwrite"
    legacy = {
        "operation_id": op_id,
        "status": "running",
        "chunks_total": 3,
        "batches_done": [0, 1],
    }
    path = manifest_path(op_id, ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    loaded = load_manifest(op_id, workspace_root=ws)
    assert loaded is None


def test_chunk_result_roundtrip(tmp_path):
    ws = _workspace(tmp_path)
    op_id = "op_chunk_001"
    write_chunk_result(
        op_id, "001", "summary here",
        context_batch_id="cb_001",
        section_id="s_0001",
        section_path="1",
        page_start=1,
        page_end=2,
        duration_sec=12.3,
        workspace_root=ws,
    )
    rec = read_chunk_result(op_id, "001", workspace_root=ws)
    assert rec is not None
    assert rec["summary"] == "summary here"
    assert rec["context_batch_id"] == "cb_001"
    assert rec["section_path"] == "1"


def test_result_roundtrip(tmp_path):
    ws = _workspace(tmp_path)
    op_id = "op_result_001"
    payload = {"subject": "S", "summary": "T", "chunks": 5}
    write_result(op_id, payload, workspace_root=ws)
    loaded = read_result(op_id, workspace_root=ws)
    assert loaded == payload


def test_missing_manifest_returns_none(tmp_path):
    ws = _workspace(tmp_path)
    assert load_manifest("op_none", workspace_root=ws) is None


def test_manifest_root_default():
    p = manifest_root(None)
    assert "data_store/cache/skills/legal_summarizer" in str(p).replace("\\", "/")


def test_chunks_dir_under_operation(tmp_path):
    ws = _workspace(tmp_path)
    p = chunks_dir("op_001", ws)
    assert p.name == "chunks"
    assert "op_001" in str(p)


def test_chunk_result_path_format():
    p = chunk_result_path("op_001", "007", "/workspace")
    assert p.name == "007.json"


def test_load_corrupted_json_returns_none(tmp_path):
    ws = _workspace(tmp_path)
    op_id = "op_corrupt"
    path = manifest_path(op_id, ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken json", encoding="utf-8")
    assert load_manifest(op_id, workspace_root=ws) is None


def test_legacy_chunk_states_have_none_section_metadata(tmp_path):
    """Legacy v1 manifest (без chunk_states) → load_manifest вернёт None."""
    ws = _workspace(tmp_path)
    op_id = "op_legacy_none"
    legacy = {
        "operation_id": op_id,
        "status": "running",
        "chunks_total": 3,
        "batches_done": [0, 1, 2],
    }
    path = manifest_path(op_id, ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    assert load_manifest(op_id, workspace_root=ws) is None


def test_v2_manifest_detected_via_version_field(tmp_path):
    ws = _workspace(tmp_path)
    op_id = "op_v2_explicit"
    raw = {
        "version": 2,
        "operation_id": op_id,
        "status": "running",
        "chunks_total": 0,
        "chunk_states": {},
    }
    path = manifest_path(op_id, ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    loaded = load_manifest(op_id, workspace_root=ws)
    assert loaded.version == MANIFEST_VERSION_V2


def test_v2_manifest_detected_via_field_absence(tmp_path):
    """Нет version field, но есть chunk_states → считаем v2."""
    ws = _workspace(tmp_path)
    op_id = "op_v2_implicit"
    raw = {
        "operation_id": op_id,
        "status": "running",
        "chunk_states": {"000": {"status": "completed"}},
    }
    path = manifest_path(op_id, ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    loaded = load_manifest(op_id, workspace_root=ws)
    assert loaded.version == MANIFEST_VERSION_V2


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))