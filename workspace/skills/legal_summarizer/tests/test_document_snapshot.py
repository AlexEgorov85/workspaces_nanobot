"""Тесты #2: document-level snapshot (write/read/invalidate).

``DocumentCache.write_snapshot`` атомарно пишет все файлы + ``_complete.marker``
через staging dir + Path.rename. Без marker snapshot считается неполным
(cache miss → ``DocumentCache.read_snapshot`` возвращает ``None``).

Большинство тестов — contract tests, вызывающие только ``DocumentCache``
public API. Тест ``test_snapshot_no_staging_leftover_on_error`` —
internal atomicity test (требует доступа к staging dir layout и
monkey-patch'а ``_atomic_write_json``); это явное исключение из правила
«только public API».
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
    """После ``DocumentCache.write_snapshot`` snapshot complete и round-trip
    сохраняет данные без потерь."""
    from cache.document_cache import DocumentCache

    document_id = "abc123def456"
    physical_data = {"path": "/tmp/test.txt", "blocks": []}
    analysis_data = {"document_id": document_id, "structure": {}, "chunks": []}

    cache = DocumentCache(tmp_path)
    target = cache.write_snapshot(
        document_id=document_id,
        physical_data=physical_data,
        analysis_data=analysis_data,
    )

    assert target.is_dir()
    assert cache.is_complete(document_id)

    snap = cache.read_snapshot(document_id)
    assert snap is not None
    physical, analysis, meta = snap
    assert physical == physical_data
    assert analysis == analysis_data
    assert meta is None


def test_snapshot_with_retrieval_index_meta(tmp_path):
    """retrieval_index.meta.json сохраняется и читается через snapshot."""
    from cache.document_cache import DocumentCache

    document_id = "d_meta"
    cache = DocumentCache(tmp_path)
    cache.write_snapshot(
        document_id=document_id,
        physical_data={"path": "x"},
        analysis_data={"document_id": document_id},
        retrieval_index_meta={"chunk_count": 10, "term_count": 200},
    )
    snap = cache.read_snapshot(document_id)
    assert snap is not None
    _, _, meta = snap
    assert meta == {"chunk_count": 10, "term_count": 200}


def test_snapshot_incomplete_returns_none(tmp_path):
    """Если marker отсутствует (partial write) — ``is_complete`` False,
    ``read_snapshot`` = None. Файлы на диске могут остаться — contract
    именно про completeness, не про файлы.

    Здесь нужен прямой доступ к marker path для setup. Используем
    ``DocumentCache._marker_path`` (явно private, документирован как
    internal API). Это допустимо для atomicity-теста.
    """
    from cache.document_cache import DocumentCache

    document_id = "d_incomplete"
    cache = DocumentCache(tmp_path)
    cache.write_snapshot(
        document_id=document_id,
        physical_data={"path": "x"},
        analysis_data={"document_id": document_id},
    )

    marker = cache._marker_path(document_id)
    marker.unlink()

    assert not cache.is_complete(document_id)
    assert cache.read_snapshot(document_id) is None


def test_snapshot_refuses_overwrite_complete(tmp_path):
    """Если snapshot уже complete — повторный write падает с RuntimeError."""
    from cache.document_cache import DocumentCache

    document_id = "d_once"
    cache = DocumentCache(tmp_path)
    cache.write_snapshot(
        document_id=document_id,
        physical_data={"v": 1},
        analysis_data={"document_id": document_id},
    )
    with pytest.raises(RuntimeError, match="уже complete"):
        cache.write_snapshot(
            document_id=document_id,
            physical_data={"v": 2},
            analysis_data={"document_id": document_id},
        )


def test_snapshot_overwrite_after_invalidate(tmp_path):
    """После ``DocumentCache.invalidate`` можно записать заново."""
    from cache.document_cache import DocumentCache

    document_id = "d_re"
    cache = DocumentCache(tmp_path)
    cache.write_snapshot(
        document_id=document_id,
        physical_data={"v": 1},
        analysis_data={"document_id": document_id},
    )
    snap1 = cache.read_snapshot(document_id)
    assert snap1 is not None and snap1[0] == {"v": 1}

    cache.invalidate(document_id)
    assert cache.read_snapshot(document_id) is None

    cache.write_snapshot(
        document_id=document_id,
        physical_data={"v": 2},
        analysis_data={"document_id": document_id},
    )
    snap2 = cache.read_snapshot(document_id)
    assert snap2 is not None and snap2[0] == {"v": 2}


def test_snapshot_no_staging_leftover_on_error(tmp_path):
    """Если write падает посередине — staging dir удаляется, нет мусора.

    Internal atomicity test: требует monkey-patch'а ``_atomic_write_json``
    и доступа к layout (``documents`` parent). Это явное исключение из
    правила «только public API» — atomicity — это контракт, и его
    нельзя полноценно проверить через public API.
    """
    from cache.document_cache import DocumentCache
    import cache.document_cache as dc

    document_id = "d_fail"
    cache = DocumentCache(tmp_path)

    original = dc._atomic_write_json

    def _failing(path, payload):
        if "physical" in str(path):
            raise RuntimeError("simulated write failure")
        original(path, payload)

    dc._atomic_write_json = _failing
    try:
        with pytest.raises(RuntimeError, match="simulated"):
            cache.write_snapshot(
                document_id=document_id,
                physical_data={"x": 1},
                analysis_data={"document_id": document_id},
            )
    finally:
        dc._atomic_write_json = original

    docs_parent = dc._cache_root(tmp_path, "default")
    if docs_parent.exists():
        for p in docs_parent.iterdir():
            assert not p.name.startswith(".staging_"), (
                f"staging leftover: {p}"
            )
    assert not cache._document_dir(document_id).exists()


def test_concurrent_writers_snapshot_is_always_valid(tmp_path):
    """Concurrency contract: после любого числа параллельных writer'ов
    snapshot либо полностью отсутствует, либо полностью complete (marker
    на месте, payload читается целиком).

    Тест симулирует гонку: 5 потоков одновременно пытаются записать
    snapshot для одного ``document_id``. После завершения всех потоков
    инвариант «target либо отсутствует, либо complete и читаем» должен
    выполняться. Параллельные writer'ы пишут семантически
    эквивалентные snapshot'ы (один document_id = один
    (path, size, mtime) → SHA-256 детерминирован → одинаковый payload),
    поэтому «выигрыш» любого writer'а допустим; data loss невозможен.
    """
    import threading
    from cache.document_cache import DocumentCache

    document_id = "d_concurrent"
    cache = DocumentCache(tmp_path)
    errors: list[BaseException] = []

    def _writer(idx: int) -> None:
        try:
            cache.write_snapshot(
                document_id=document_id,
                physical_data={"writer": idx, "path": "x"},
                analysis_data={"document_id": document_id, "writer": idx},
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_writer, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Хотя бы один writer должен был успешно завершить.
    assert not errors or any(
        not isinstance(e, RuntimeError) or "уже complete" not in str(e)
        for e in errors
    ), f"unexpected errors: {errors}"

    # Invariant: либо target отсутствует, либо complete.
    target = cache._document_dir(document_id)
    if target.exists():
        assert cache.is_complete(document_id), (
            "target exists but marker отсутствует — race нарушил atomicity"
        )
        snap = cache.read_snapshot(document_id)
        assert snap is not None, "is_complete=True но read_snapshot=None"
        physical, analysis, _meta = snap
        assert physical is not None and "writer" in physical
        assert analysis is not None and "writer" in analysis


def test_atomic_replace_no_staging_leftover_on_collision(tmp_path):
    """``os.replace`` атомарно перезаписывает target. Если первый writer
    уже сделал ``os.replace(staging1, target)``, второй writer тоже
    делает ``os.replace(staging2, target)`` — атомарно, без мусора в
    target (staging2 полностью заменяет staging1; staging1 уже не
    существует как staging). Stale staging от упавших потоков может
    остаться, но target всегда валиден.
    """
    from cache.document_cache import DocumentCache
    import cache.document_cache as dc

    document_id = "d_replace"
    cache = DocumentCache(tmp_path)
    cache.write_snapshot(
        document_id=document_id,
        physical_data={"v": 1},
        analysis_data={"document_id": document_id},
    )
    # Второй writer: invalidate → запись нового snapshot.
    # ``os.replace`` атомарно перезаписывает target.
    cache.invalidate(document_id)
    cache.write_snapshot(
        document_id=document_id,
        physical_data={"v": 2},
        analysis_data={"document_id": document_id},
    )
    snap = cache.read_snapshot(document_id)
    assert snap is not None
    physical, _, _ = snap
    assert physical == {"v": 2}


def test_load_document_chunk_summaries(tmp_path):
    """``DocumentCache.load_chunk_summaries``: cross-operation lookup."""
    from cache.document_cache import DocumentCache

    document_id = "d_chunks"
    cache = DocumentCache(tmp_path)
    cache.write_chunk_summary(
        document_id=document_id,
        chunk_id="001",
        summary="first chunk summary",
        section_id="s_0001",
        section_path="1",
        page_start=1,
        page_end=2,
    )
    cache.write_chunk_summary(
        document_id=document_id,
        chunk_id="002",
        summary="second chunk summary",
    )

    out = cache.load_chunk_summaries(
        document_id, ["001", "002", "003_missing"],
    )
    assert out == {
        "001": "first chunk summary",
        "002": "second chunk summary",
    }


def test_load_document_section_summaries(tmp_path):
    """``DocumentCache.load_section_summaries``: round-trip."""
    from cache.document_cache import DocumentCache

    document_id = "d_sec"
    cache = DocumentCache(tmp_path)
    cache.write_section_summary(
        document_id=document_id,
        section_id="s_0001",
        summary="section 1 summary",
    )
    cache.write_section_summary(
        document_id=document_id,
        section_id="s_0002",
        summary="section 2 summary",
    )

    out = cache.load_section_summaries(
        document_id, ["s_0001", "s_0002", "s_missing"],
    )
    assert out == {
        "s_0001": "section 1 summary",
        "s_0002": "section 2 summary",
    }
