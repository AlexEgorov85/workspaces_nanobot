"""Тесты #2: document-level snapshot (write/read/invalidate).

``write_document_snapshot`` атомарно пишет все файлы + ``_complete.marker``
через staging dir + Path.rename. Без marker snapshot считается неполным
(cache miss → ``read_document_snapshot`` возвращает ``None``).
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


def test_snapshot_atomic_complete_marker(tmp_path):
    """После write_document_snapshot все файлы + marker существуют."""
    from cache.manifest import (
        document_analysis_path,
        document_physical_path,
        is_document_cache_complete,
        read_document_snapshot,
        write_document_snapshot,
    )

    document_id = "abc123def456"
    physical_data = {"path": "/tmp/test.txt", "blocks": []}
    analysis_data = {"document_id": document_id, "structure": {}, "chunks": []}

    result = write_document_snapshot(
        workspace_root=tmp_path,
        document_id=document_id,
        physical_data=physical_data,
        analysis_data=analysis_data,
    )

    assert result.is_dir()
    assert is_document_cache_complete(document_id, tmp_path)
    assert document_physical_path(document_id, tmp_path).is_file()
    assert document_analysis_path(document_id, tmp_path).is_file()

    snap = read_document_snapshot(document_id, tmp_path)
    assert snap is not None
    physical, analysis, meta = snap
    assert physical == physical_data
    assert analysis == analysis_data
    assert meta is None  # retrieval_index.meta не передан


def test_snapshot_with_retrieval_index_meta(tmp_path):
    """retrieval_index.meta.json сохраняется отдельно, читается через snapshot."""
    from cache.manifest import (
        read_document_snapshot,
        write_document_snapshot,
    )

    document_id = "d_meta"
    write_document_snapshot(
        workspace_root=tmp_path,
        document_id=document_id,
        physical_data={"path": "x"},
        analysis_data={"document_id": document_id},
        retrieval_index_meta={"chunk_count": 10, "term_count": 200},
    )
    snap = read_document_snapshot(document_id, tmp_path)
    assert snap is not None
    _, _, meta = snap
    assert meta == {"chunk_count": 10, "term_count": 200}


def test_snapshot_incomplete_returns_none(tmp_path):
    """Если marker отсутствует (стартовали write но упали) — read = None."""
    from cache.manifest import (
        document_analysis_path,
        document_dir,
        document_physical_path,
        is_document_cache_complete,
        read_document_snapshot,
        write_document_snapshot,
    )

    document_id = "d_incomplete"
    write_document_snapshot(
        workspace_root=tmp_path,
        document_id=document_id,
        physical_data={"path": "x"},
        analysis_data={"document_id": document_id},
    )

    # Симулируем partial write: удаляем marker, оставляя файлы.
    marker = document_dir(document_id, tmp_path) / "_complete.marker"
    marker.unlink()

    assert not is_document_cache_complete(document_id, tmp_path)
    # Файлы остались, но read_document_snapshot видит неполный snapshot.
    assert document_physical_path(document_id, tmp_path).is_file()
    assert document_analysis_path(document_id, tmp_path).is_file()
    assert read_document_snapshot(document_id, tmp_path) is None


def test_snapshot_refuses_overwrite_complete(tmp_path):
    """Если snapshot уже complete — повторный write падает с RuntimeError."""
    from cache.manifest import write_document_snapshot

    document_id = "d_once"
    write_document_snapshot(
        workspace_root=tmp_path,
        document_id=document_id,
        physical_data={"v": 1},
        analysis_data={"document_id": document_id},
    )
    with pytest.raises(RuntimeError, match="уже complete"):
        write_document_snapshot(
            workspace_root=tmp_path,
            document_id=document_id,
            physical_data={"v": 2},
            analysis_data={"document_id": document_id},
        )


def test_snapshot_overwrite_after_invalidate(tmp_path):
    """После invalidate_document_cache можно записать заново."""
    from cache.manifest import (
        invalidate_document_cache,
        read_document_snapshot,
        write_document_snapshot,
    )

    document_id = "d_re"
    write_document_snapshot(
        workspace_root=tmp_path,
        document_id=document_id,
        physical_data={"v": 1},
        analysis_data={"document_id": document_id},
    )
    snap1 = read_document_snapshot(document_id, tmp_path)
    assert snap1[0] == {"v": 1}

    invalidate_document_cache(document_id, tmp_path)
    assert read_document_snapshot(document_id, tmp_path) is None

    write_document_snapshot(
        workspace_root=tmp_path,
        document_id=document_id,
        physical_data={"v": 2},
        analysis_data={"document_id": document_id},
    )
    snap2 = read_document_snapshot(document_id, tmp_path)
    assert snap2[0] == {"v": 2}


def test_snapshot_no_staging_leftover_on_error(tmp_path):
    """Если write падает посередине — staging dir удаляется, нет мусора."""
    from cache.manifest import document_dir, manifest_root

    document_id = "d_fail"

    # Подменяем _atomic_write_json на функцию, которая падает.
    import cache.manifest as cm

    original = cm._atomic_write_json

    def _failing(path, payload):
        if "physical" in str(path):
            raise RuntimeError("simulated write failure")
        original(path, payload)

    cm._atomic_write_json = _failing
    try:
        with pytest.raises(RuntimeError, match="simulated"):
            cm.write_document_snapshot(
                workspace_root=tmp_path,
                document_id=document_id,
                physical_data={"x": 1},
                analysis_data={"document_id": document_id},
            )
    finally:
        cm._atomic_write_json = original

    # Никаких staging.* каталогов не осталось.
    docs_parent = manifest_root(tmp_path) / "documents"
    if docs_parent.exists():
        for p in docs_parent.iterdir():
            assert not p.name.startswith(".staging_"), (
                f"staging leftover: {p}"
            )
    # target не создан.
    assert not document_dir(document_id, tmp_path).exists()


def test_load_document_chunk_summaries(tmp_path):
    """load_document_chunk_summaries: cross-operation lookup."""
    from cache.manifest import (
        load_document_chunk_summaries,
        write_document_chunk_summary,
    )

    document_id = "d_chunks"
    write_document_chunk_summary(
        workspace_root=tmp_path,
        document_id=document_id,
        chunk_id="001",
        summary="first chunk summary",
        section_id="s_0001",
        section_path="1",
        page_start=1,
        page_end=2,
    )
    write_document_chunk_summary(
        workspace_root=tmp_path,
        document_id=document_id,
        chunk_id="002",
        summary="second chunk summary",
    )

    out = load_document_chunk_summaries(
        document_id,
        ["001", "002", "003_missing"],
        tmp_path,
    )
    assert out == {
        "001": "first chunk summary",
        "002": "second chunk summary",
    }


def test_load_document_section_summaries(tmp_path):
    from cache.manifest import (
        load_document_section_summaries,
        write_document_section_summary,
    )

    document_id = "d_sec"
    write_document_section_summary(
        workspace_root=tmp_path,
        document_id=document_id,
        section_id="s_0001",
        summary="section 1 summary",
    )
    write_document_section_summary(
        workspace_root=tmp_path,
        document_id=document_id,
        section_id="s_0002",
        summary="section 2 summary",
    )

    out = load_document_section_summaries(
        document_id,
        ["s_0001", "s_0002", "s_missing"],
        tmp_path,
    )
    assert out == {
        "s_0001": "section 1 summary",
        "s_0002": "section 2 summary",
    }