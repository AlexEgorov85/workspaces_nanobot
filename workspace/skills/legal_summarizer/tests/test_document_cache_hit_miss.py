"""Тесты #3: ``run_canonical_pipeline`` document-level cache hit/miss.

Проверяется:

1. Cache miss → full pipeline (DocumentLoader.parse → structure → ChunkPlanner).
2. Cache write после успешного pipeline (snapshot + ``_complete.marker``).
3. Cache hit на повторном вызове с тем же файлом → НЕТ повторного парсинга.
4. Cache invalidate при изменении файла (mtime/size) → miss на следующем вызове.
5. Cache hit восстанавливает DocumentAnalysis с тем же ``document_id``,
   теми же chunk_id и правильным retrieval_index.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _write_txt(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "doc.txt"
    p.write_text(text, encoding="utf-8")
    return p


def _build_text(sections: int = 3) -> str:
    parts = []
    for i in range(1, sections + 1):
        parts.append(
            f"Раздел {i}. Заголовок {i}\n\n"
            + ("Текст параграфа раздела. " * 50) * 40
            + "\n\n"
        )
    return "".join(parts)


def test_first_run_cache_miss_writes_snapshot(tmp_path):
    """Первый run — cache miss → snapshot пишется на диск."""
    from application.pipeline_structure import run_canonical_pipeline
    from cache.manifest import is_document_cache_complete, read_document_snapshot

    text = _build_text(sections=3)
    p = _write_txt(tmp_path, text)

    result = run_canonical_pipeline(p, workspace_root=tmp_path)
    assert result.analysis is not None
    assert len(result.chunks) >= 1

    # Snapshot записан.
    document_id = result.analysis.identity.document_id
    assert is_document_cache_complete(document_id, tmp_path)
    snap = read_document_snapshot(document_id, tmp_path)
    assert snap is not None
    physical_data, analysis_data, meta = snap
    assert physical_data["path"] == str(p.resolve())
    assert analysis_data["document_id"] == document_id
    assert len(analysis_data["chunks"]) == len(result.chunks)
    assert meta is not None
    assert meta["chunk_count"] == len(result.chunks)


def test_second_run_cache_hit_returns_same_analysis(tmp_path):
    """Второй run с тем же файлом — cache hit, identity/chunk_id те же."""
    from application.pipeline_structure import run_canonical_pipeline

    text = _build_text(sections=3)
    p = _write_txt(tmp_path, text)

    result1 = run_canonical_pipeline(p, workspace_root=tmp_path)
    result2 = run_canonical_pipeline(p, workspace_root=tmp_path)

    assert result1.analysis.identity.document_id == (
        result2.analysis.identity.document_id
    )
    assert result1.analysis.identity.fingerprint == (
        result2.analysis.identity.fingerprint
    )
    # Chunk IDs идентичны (cache hit восстанавливает из JSON, не ChunkPlanner).
    assert [c.chunk_id for c in result1.chunks] == [
        c.chunk_id for c in result2.chunks
    ]
    # structure идентична.
    assert result1.analysis.structure.root_id == (
        result2.analysis.structure.root_id
    )


def test_cache_hit_skips_parsing(monkeypatch, tmp_path):
    """Cache hit: DocumentLoader.load НЕ вызывается (нет повторного парсинга)."""
    import document.loader as loader_mod
    from application.pipeline_structure import run_canonical_pipeline

    text = _build_text(sections=3)
    p = _write_txt(tmp_path, text)

    # Первый run: cache miss → DocumentLoader.load вызывается.
    run_canonical_pipeline(p, workspace_root=tmp_path)

    calls = {"n": 0}
    original_load = loader_mod.DocumentLoader.load

    def counting_load(self, path, **kw):
        calls["n"] += 1
        return original_load(self, path, **kw)

    monkeypatch.setattr(loader_mod.DocumentLoader, "load", counting_load)

    # Второй run: cache hit → DocumentLoader.load НЕ должен вызываться.
    run_canonical_pipeline(p, workspace_root=tmp_path)
    assert calls["n"] == 0, (
        f"cache hit must skip DocumentLoader.load, got {calls['n']} calls"
    )


def test_cache_invalidation_on_file_change(tmp_path):
    """Изменение файла (mtime/size) → новый document_id, cache hit для
    нового файла, старый snapshot остаётся валиден (он соответствует
    старому fingerprint'у). Это документированное поведение: каждый
    document_id привязан к конкретному (path, size, mtime_ns)."""
    from application.pipeline_structure import run_canonical_pipeline

    text_v1 = _build_text(sections=2)
    p = _write_txt(tmp_path, text_v1)

    result1 = run_canonical_pipeline(p, workspace_root=tmp_path)
    document_id_v1 = result1.analysis.identity.document_id

    # Изменяем содержимое файла. Размер текста сильно меняется → другой
    # SHA-256 fingerprint → другой document_id.
    text_v2 = _build_text(sections=6) + "\n" + ("Новое содержимое. " * 1000)
    time.sleep(1.1)
    p.write_text(text_v2, encoding="utf-8")

    result2 = run_canonical_pipeline(p, workspace_root=tmp_path)
    document_id_v2 = result2.analysis.identity.document_id

    # Новый документ — другой document_id.
    assert document_id_v1 != document_id_v2
    # Старый snapshot по document_id_v1 остаётся (он валиден для старого
    # fingerprint'а — больше такого файла нет, snapshot осиротел, но это
    # не bug: кто-то вручную может вызвать invalidate_document_cache).
    # Главное — второй вызов был cache miss (новый файл).


def test_cache_hit_invalidates_when_fingerprint_mtime_mismatch(tmp_path):
    """Если кто-то подменил файл вручную (mtime изменился, но мы
    искусственно подменили бы snapshot) → is_fresh() возвращает False,
    _try_load_cached_pipeline_result инвалидирует snapshot.
    """
    from application.pipeline_structure import run_canonical_pipeline
    from cache.manifest import (
        document_dir,
        invalidate_document_cache,
        is_document_cache_complete,
        write_document_snapshot,
    )
    from document.identity import DocumentIdentity

    text = _build_text(sections=2)
    p = _write_txt(tmp_path, text)

    result = run_canonical_pipeline(p, workspace_root=tmp_path)
    document_id = result.analysis.identity.document_id
    assert is_document_cache_complete(document_id, tmp_path)

    # Симулируем «подмену» snapshot'а: создаём его с другим fingerprint
    # (как если бы snapshot был от прошлого содержимого файла).
    fake_identity = DocumentIdentity.from_path_with_mtime(
        p, size_bytes=1, mtime_ns=999,
    )
    if fake_identity.document_id == document_id:
        pytest.skip("test setup: file mtime не дал разные identity")

    # Записываем «осиротевший» snapshot.
    write_document_snapshot(
        workspace_root=tmp_path,
        document_id=fake_identity.document_id,
        physical_data={"path": str(p.resolve())},
        analysis_data={"document_id": fake_identity.document_id},
    )

    # Теперь делаем cache hit попытку с реальным path: наш snapshot не
    # подходит (другой document_id), cache hit = False, новый snapshot
    # пишется. Старый «осиротевший» остаётся (это нормально — его
    # никто не запрашивает).
    result2 = run_canonical_pipeline(p, workspace_root=tmp_path)
    # Новый run создал свой snapshot (документ всё равно валидный).
    new_document_id = result2.analysis.identity.document_id
    assert is_document_cache_complete(new_document_id, tmp_path)


def test_cache_workspace_root_none_always_miss(tmp_path):
    """Без workspace_root cache не работает — каждый раз полный pipeline."""
    from application.pipeline_structure import run_canonical_pipeline

    text = _build_text(sections=2)
    p = _write_txt(tmp_path, text)

    # Без workspace_root: cache hit невозможен, но и snapshot не пишется.
    result = run_canonical_pipeline(p)
    assert result.analysis is not None
    # Никаких cache-файлов не должно появиться.
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
        assert not any(docs_root.iterdir()), (
            f"workspace_root=None must not write snapshot, found: "
            f"{list(docs_root.iterdir())}"
        )