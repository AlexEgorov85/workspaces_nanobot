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

import time
from pathlib import Path

import pytest


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
    from libs.legal_summarizer.application.pipeline_structure import run_canonical_pipeline
    from libs.legal_summarizer import cache  # noqa: F401
    from libs.legal_summarizer.cache.document_cache import DocumentCache

    text = _build_text(sections=3)
    p = _write_txt(tmp_path, text)

    result = run_canonical_pipeline(p, workspace_root=tmp_path)
    assert result.analysis is not None
    assert len(result.chunks) >= 1

    document_id = result.analysis.identity.document_id
    cache = DocumentCache(tmp_path)
    assert cache.is_complete(document_id)
    snap = cache.read_snapshot(document_id)
    assert snap is not None
    physical_data, analysis_data, meta = snap
    assert physical_data["path"] == str(p.resolve())
    assert analysis_data["document_id"] == document_id
    assert len(analysis_data["chunks"]) == len(result.chunks)
    assert meta is not None
    assert meta["chunk_count"] == len(result.chunks)


def test_second_run_cache_hit_returns_same_analysis(tmp_path):
    """Второй run с тем же файлом — cache hit, identity/chunk_id те же."""
    from libs.legal_summarizer.application.pipeline_structure import run_canonical_pipeline

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
    assert [c.chunk_id for c in result1.chunks] == [
        c.chunk_id for c in result2.chunks
    ]
    assert result1.analysis.structure.root_id == (
        result2.analysis.structure.root_id
    )


def test_cache_hit_skips_parsing(monkeypatch, tmp_path):
    """Cache hit: DocumentLoader.load НЕ вызывается (нет повторного парсинга)."""
    import libs.legal_summarizer.document.loader as loader_mod
    from libs.legal_summarizer.application.pipeline_structure import run_canonical_pipeline

    text = _build_text(sections=3)
    p = _write_txt(tmp_path, text)

    run_canonical_pipeline(p, workspace_root=tmp_path)

    calls = {"n": 0}
    original_load = loader_mod.DocumentLoader.load

    def counting_load(self, path, **kw):
        calls["n"] += 1
        return original_load(self, path, **kw)

    monkeypatch.setattr(loader_mod.DocumentLoader, "load", counting_load)

    run_canonical_pipeline(p, workspace_root=tmp_path)
    assert calls["n"] == 0, (
        f"cache hit must skip DocumentLoader.load, got {calls['n']} calls"
    )


def test_cache_invalidation_on_file_change(tmp_path):
    """Изменение файла (mtime/size) → новый document_id, cache hit для
    нового файла, старый snapshot остаётся валиден (он соответствует
    старому fingerprint'у). Это документированное поведение: каждый
    document_id привязан к конкретному (path, size, mtime_ns)."""
    from libs.legal_summarizer.application.pipeline_structure import run_canonical_pipeline

    text_v1 = _build_text(sections=2)
    p = _write_txt(tmp_path, text_v1)

    result1 = run_canonical_pipeline(p, workspace_root=tmp_path)
    document_id_v1 = result1.analysis.identity.document_id

    text_v2 = _build_text(sections=6) + "\n" + ("Новое содержимое. " * 1000)
    time.sleep(1.1)
    p.write_text(text_v2, encoding="utf-8")

    result2 = run_canonical_pipeline(p, workspace_root=tmp_path)
    document_id_v2 = result2.analysis.identity.document_id

    assert document_id_v1 != document_id_v2


def test_same_content_different_path_shares_document_id(tmp_path):
    """Ключевой инвариант контент-хеша: одинаковое содержимое под разными
    путями — это **один** document_id, а значит один кэш-разбор.

    До перехода на контент-хеш id выводился из resolved_path, и такой
    документ платил за парсинг дважды.
    """
    from libs.legal_summarizer.application.pipeline_structure import (
        run_canonical_pipeline,
    )
    from libs.legal_summarizer.cache.document_cache import DocumentCache

    text = _build_text(sections=2)
    a = tmp_path / "a.txt"
    a.write_text(text, encoding="utf-8")
    b = tmp_path / "nested" / "b.txt"
    b.parent.mkdir(parents=True, exist_ok=True)
    b.write_text(text, encoding="utf-8")

    result_a = run_canonical_pipeline(a, workspace_root=tmp_path)
    document_id = result_a.analysis.identity.document_id

    cache = DocumentCache(tmp_path)
    assert cache.is_complete(document_id)

    result_b = run_canonical_pipeline(b, workspace_root=tmp_path)

    assert result_b.analysis.identity.document_id == document_id
    # Тот же разбор, а не второй: те же chunk_id.
    assert [c.chunk_id for c in result_b.chunks] == [
        c.chunk_id for c in result_a.chunks
    ]


def test_orphan_snapshot_from_other_content_does_not_serve_current_file(tmp_path):
    """«Осиротевший» snapshot от другого содержимого не подставляется
    текущему файлу: у него свой document_id, и он остаётся лежать.

    Раньше «другой» id здесь выдумывался подменой stat (size/mtime).
    Теперь различие обязано приходить из содержимого — иначе оно
    недостижимо, что и делало тест фиктивным.
    """
    from libs.legal_summarizer.application.pipeline_structure import (
        run_canonical_pipeline,
    )
    from libs.legal_summarizer.cache.document_cache import DocumentCache
    from libs.legal_summarizer.document.identity import DocumentIdentity

    p = _write_txt(tmp_path, _build_text(sections=2))

    result = run_canonical_pipeline(p, workspace_root=tmp_path)
    document_id = result.analysis.identity.document_id

    other = tmp_path / "other.txt"
    other.write_text(_build_text(sections=5) + " иное", encoding="utf-8")
    orphan_id = DocumentIdentity.from_path(other).document_id
    assert orphan_id != document_id

    cache = DocumentCache(tmp_path)
    cache.write_snapshot(
        document_id=orphan_id,
        physical_data={"path": str(other.resolve())},
        analysis_data={"document_id": orphan_id},
    )

    result2 = run_canonical_pipeline(p, workspace_root=tmp_path)

    assert result2.analysis.identity.document_id == document_id
    assert cache.is_complete(orphan_id), "чужой snapshot никто не запрашивал"


def test_cache_workspace_root_none_always_miss(tmp_path):
    """Без workspace_root cache не работает — каждый раз полный pipeline."""
    from libs.legal_summarizer.application.pipeline_structure import run_canonical_pipeline
    from libs.legal_summarizer.cache.document_cache import DocumentCache

    text = _build_text(sections=2)
    p = _write_txt(tmp_path, text)

    result = run_canonical_pipeline(p)
    assert result.analysis is not None

    cache = DocumentCache(tmp_path)
    document_id = result.analysis.identity.document_id
    assert not cache.is_complete(document_id)



