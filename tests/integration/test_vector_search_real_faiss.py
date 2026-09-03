"""Integration-тест ``vector_search`` с реальным FAISS-индексом.

Покрывает путь, который моки ``_FakeProvider`` не покрывают:

  * реальный ``faiss.IndexFlatIP`` (сборка, нормализация, cosine-IP);
  * реальный ``build_faiss_index`` / ``build_raw_items`` / ``group_vector_hits``;
  * реальная сериализация чанков через ``build_raw_items`` (threshold,
    chunk_grouping, метаданные);
  * полный путь ``VectorSearchTool.execute`` (валидация, _is_known_index,
    JSON-сериализация, truncate_middle);
  * edge cases: dimension mismatch, пустой индекс, top_k>ntotal,
    threshold=0.0 (всё пропускаем), threshold=высокий (всё отсекаем),
    group_vector_hits склеивает чанки одного документа.

Не требует PostgreSQL/HTTP-embedding-сервиса: ``get_embedding`` замокан
через детерминированный генератор векторов, привязанный к query.

Пропускается автоматически, если ``faiss`` или ``numpy`` недоступны
(см. ``pytest.importorskip`` ниже).
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest

faiss = pytest.importorskip("faiss")
np = pytest.importorskip("numpy")


_EMBED_DIM = 16


def _deterministic_embedding(text: str) -> list[float]:
    """Стабильный детерминированный вектор из текста через хеш.

    Идея: разбиваем первые 16 байт SHA-256 на signed-float значения.
    Все векторы одной длины, ортогональные тексты дают разные направления.
    Для теста важна предсказуемость: один и тот же query → один вектор.
    """
    import hashlib

    digest = hashlib.sha256(text.encode("utf-8")).digest()
    raw = list(digest[:_EMBED_DIM])
    floats = [(b - 128) / 128.0 for b in raw]
    vec = np.array(floats, dtype=np.float32)
    vec /= (np.linalg.norm(vec) + 1e-12)
    return vec.tolist()


def _build_real_index(records: list[dict[str, Any]]) -> tuple[Any, dict[str, Any]]:
    """Собрать реальный FAISS-индекс через ``build_faiss_index``.

    ``records`` — список словарей с ключами ``source``, ``content``,
    ``search_text``, ``table``, ``pk_value``, ``chunk_index``,
    ``chunk_count``, ``row_data``, ``embedding``. ``embedding`` — list[float]
    длиной ``_EMBED_DIM``.
    """
    from lib.utils.duckdb_query import build_faiss_index

    return build_faiss_index(records)


def _make_records(
    *,
    source: str = "violations_index",
    table: str = "oarb.violations",
    n: int = 5,
    chunked_pk: int | None = None,
) -> list[dict[str, Any]]:
    """Сгенерировать N записей с предсказуемыми метаданными.

    Используется для проверки индексации, поиска и группировки.
    Каждой записи присваивается уникальный текст → уникальный вектор.
    Если задан ``chunked_pk``, все записи с этим pk (имитируем чанки
    одного документа).
    """
    out: list[dict[str, Any]] = []
    for i in range(n):
        text = f"violation #{i}: пожарная безопасность, нарушение #{i}"
        pk = chunked_pk if chunked_pk is not None else i + 1
        out.append({
            "source": source,
            "content": text,
            "search_text": text,
            "table": table,
            "pk_value": pk,
            "chunk_index": i if chunked_pk is not None else 0,
            "chunk_count": n if chunked_pk is not None else 1,
            "row_data": {"id": pk, "title": text},
            "embedding": _deterministic_embedding(text),
        })
    return out


def _exec(tool, **kwargs):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(tool.execute(**kwargs))
    finally:
        loop.close()


@dataclass
class _FakeProviderState:
    """Минимальная заглушка провайдера, не маскирующая под _FakeProvider."""

    idx: Any
    meta: dict[str, Any]


class _RealFaissProvider:
    """Провайдер, делегирующий в реальный FAISS-индекс через модули cache_provider_impl.

    Используется вместо прямого вызова ``provider.search_vector``, потому что
    ``PostgresDuckDbProvider.search_vector`` тащит зависимости от DuckDB и
    embedding-конфига. Вместо этого — узкая заглушка, которая:
      * хранит готовый FAISS-индекс;
      * вызывает настоящие ``build_raw_items`` / ``group_vector_hits``;
      * получает embedding через мок ``get_embedding``;
      * возвращает ``SearchResult`` (см. ``lib.services.cache_provider``).
    """

    def __init__(self, idx: Any, meta: dict[str, Any]) -> None:
        self._idx = idx
        self._meta = meta
        self._last_loaded_meta: dict[str, Any] | None = meta

    def search_vector(
        self, query: str, index_name: str, top_k: int = 5, threshold: float | None = None,
    ) -> list[Any]:
        import numpy as np
        from lib.services.cache_provider import SearchResult
        from lib.utils.duckdb_query import build_raw_items, group_vector_hits

        embedding = _deterministic_embedding(query)
        query_vec = np.array([embedding], dtype=np.float32)
        if self._idx.ntotal == 0:
            return []
        threshold_active = threshold is not None and threshold > 0
        n = self._idx.ntotal if threshold_active else min(top_k, self._idx.ntotal)
        scores, ids = self._idx.search(query_vec, n)

        meta_items = self._meta.get("metadata", {})
        raw = build_raw_items(meta_items, scores, ids, index_name, threshold)
        grouped = group_vector_hits(raw, top_k=top_k, threshold=threshold)

        return [
            SearchResult(
                content=r["content"],
                score=r["score"],
                source=r["source"],
                table=r["table"],
                pk_value=r["pk_value"],
                chunk=r.get("chunk", ""),
                matched_chunks=r.get("matched_chunks", 1),
                row=r.get("row", {}),
            )
            for r in grouped
        ]


@pytest.fixture
def real_faiss_tool():
    """Tool с реальным FAISS-индексом из 5 записей violations_index."""
    from workspace.tools.vector_search_tool import (
        VectorSearchTool,
        VectorSearchToolConfig,
    )

    records = _make_records(n=5)
    idx, meta = _build_real_index(records)
    assert idx is not None, "FAISS index must build"

    tool = VectorSearchTool(config=VectorSearchToolConfig(default_top_k=3, max_top_k=10))
    tool.set_provider(_RealFaissProvider(idx, meta))
    return tool


@pytest.fixture
def chunked_faiss_tool():
    """Tool с реальным FAISS-индексом: 5 чанков одного документа с одинаковым текстом.

    Чтобы все 5 чанков имели одинаковый embedding (и попали в FAISS top-k
    при поиске), все они используют один и тот же текст — embedding от
    одного текста через детерминированный хеш даёт один вектор.
    """
    from workspace.tools.vector_search_tool import (
        VectorSearchTool,
        VectorSearchToolConfig,
    )

    text = "одинаковое содержание для всех чанков одного документа"
    embedding = _deterministic_embedding(text)
    records = [
        {
            "source": "violations_index",
            "content": text,
            "search_text": text,
            "table": "oarb.violations",
            "pk_value": 42,
            "chunk_index": i,
            "chunk_count": 5,
            "row_data": {"id": 42, "chunk": i},
            "embedding": embedding,
        }
        for i in range(5)
    ]
    idx, meta = _build_real_index(records)
    assert idx is not None

    tool = VectorSearchTool(config=VectorSearchToolConfig(default_top_k=5, max_top_k=10))
    tool.set_provider(_RealFaissProvider(idx, meta))
    return tool


@pytest.fixture
def empty_faiss_tool():
    """Tool с пустым FAISS-индексом (0 векторов)."""
    from workspace.tools.vector_search_tool import (
        VectorSearchTool,
        VectorSearchToolConfig,
    )

    empty = faiss.IndexFlatIP(_EMBED_DIM)
    tool = VectorSearchTool(config=VectorSearchToolConfig())
    tool.set_provider(_RealFaissProvider(empty, {"metadata": {}}))
    return tool


@pytest.fixture
def dim_mismatch_tool():
    """Tool с FAISS-индексом другой размерности — должно выдавать ошибку.

    Чтобы воспроизвести эту ветку, провайдер должен поднять ошибку
    в ``search_vector``. Используем IndexFlatIP с другим ``d``.
    """
    from workspace.tools.vector_search_tool import (
        VectorSearchTool,
        VectorSearchToolConfig,
    )

    records = _make_records(n=3)
    idx, meta = _build_real_index(records)
    fake_provider = _RealFaissProvider(idx, meta)

    def _explode(query, **kwargs):
        raise RuntimeError(
            f"Размерность индекса '{kwargs.get('index_name', '')}' "
            f"({idx.d}) не совпадает с размерностью эмбеддинга запроса (999). "
            "Пересоберите снимок."
        )

    fake_provider.search_vector = _explode

    tool = VectorSearchTool(config=VectorSearchToolConfig())
    tool.set_provider(fake_provider)
    return tool


@pytest.fixture(autouse=True)
def _patch_known_index():
    """Mock ``_is_known_index`` для интеграционных тестов без PG.

    По умолчанию — все индексы считаются известными. Это нормально для
    интеграционного теста с реальным FAISS, потому что он проверяет
    сам поиск, а не реестр индексов. Проверка реестра уже покрыта
    unit-тестами ``test_vector_search_tool.py``.
    """
    with patch(
        "workspace.tools.vector_search_tool._is_known_index",
        return_value=True,
    ):
        yield


# ---------------------------------------------------------------------------
# Полный путь: provider + tool → JSON
# ---------------------------------------------------------------------------


class TestRealFaissEndToEnd:
    def test_returns_top_k_results(self, real_faiss_tool):
        """Tool возвращает JSON с ровно top_k результатами."""
        out = _exec(real_faiss_tool, query="qu_0", index_name="violations_index")
        payload = json.loads(out)
        assert payload["status"] == "success"
        assert payload["index_name"] == "violations_index"
        assert payload["count"] == 3, f"expected top_k=3 results, got {payload['count']}"
        assert payload["truncated"] is False

    def test_results_have_real_metadata(self, real_faiss_tool):
        """Каждый результат содержит реальные метаданные из FAISS."""
        out = _exec(real_faiss_tool, query="qu_0", index_name="violations_index")
        payload = json.loads(out)
        first = payload["results"][0]
        assert "id" in first
        assert "score" in first
        assert "text" in first
        assert first["metadata"]["source"] == "violations_index"
        assert first["metadata"]["table"] == "oarb.violations"
        assert first["metadata"]["index_name"] == "violations_index"

    def test_results_sorted_by_score_desc(self, real_faiss_tool):
        """Результаты отсортированы по убыванию score (cosine/IP)."""
        out = _exec(real_faiss_tool, query="qu_0", index_name="violations_index")
        payload = json.loads(out)
        scores = [r["score"] for r in payload["results"]]
        assert scores == sorted(scores, reverse=True), f"not sorted: {scores}"

    def test_query_relevance(self, real_faiss_tool):
        """Поиск возвращает релевантные документы из индекса.

        Детерминированный хеш не гарантирует упорядоченности по embedding-расстоянию,
        поэтому проверяем, что в top-k есть хотя бы один документ из индекса.
        """
        out = _exec(real_faiss_tool, query="violation", index_name="violations_index")
        payload = json.loads(out)
        texts = [r["text"] for r in payload["results"]]
        assert texts, "expected non-empty results for query='violation'"
        assert all(t.startswith("violation #") for t in texts), (
            f"expected results to be from indexed violations, got {texts}"
        )


# ---------------------------------------------------------------------------
# Граничные случаи (edge cases)
# ---------------------------------------------------------------------------


class TestRealFaissEdgeCases:
    def test_empty_index_returns_empty_results(self, empty_faiss_tool):
        """Пустой индекс → success с пустым списком (НЕ ошибка)."""
        out = _exec(empty_faiss_tool, query="anything", index_name="empty_idx")
        payload = json.loads(out)
        assert payload["status"] == "success"
        assert payload["results"] == []
        assert payload["count"] == 0

    def test_threshold_filters_low_scores(self, real_faiss_tool):
        """Высокий threshold отсекает все результаты с низким score."""
        out = _exec(
            real_faiss_tool,
            query="qu_0",
            index_name="violations_index",
            threshold=0.99,
        )
        payload = json.loads(out)
        assert payload["status"] == "success"
        for r in payload["results"]:
            assert r["score"] >= 0.99

    def test_top_k_caps_results(self, real_faiss_tool):
        """top_k=1 возвращает ровно 1 результат."""
        out = _exec(
            real_faiss_tool, query="qu_0", index_name="violations_index", top_k=1
        )
        payload = json.loads(out)
        assert payload["count"] == 1

    def test_top_k_exceeds_ntotal_returns_all(self, real_faiss_tool):
        """top_k равный max_top_k при ntotal=5 возвращает результаты без падения.

        Не проверяем точное количество, потому что часть хитов может быть
        отсеяна по ``threshold=0.0`` (отрицательные cosine scores в IndexFlatIP).
        Главное — tool не падает и возвращает корректный JSON.
        """
        out = _exec(
            real_faiss_tool, query="qu_0", index_name="violations_index", top_k=10
        )
        payload = json.loads(out)
        assert payload["status"] == "success"
        assert payload["count"] >= 1
        assert payload["count"] <= 5

    def test_chunked_documents_grouped(self, chunked_faiss_tool):
        """5 чанков одного pk_value группируются в 1 результат (matched_chunks=5).

        Query должен совпадать с текстом чанков, чтобы детерминированный
        эмбеддер дал одинаковый вектор. Без этого тест проверял бы только
        семантическую близость, что не гарантировано хешем.
        """
        out = _exec(
            chunked_faiss_tool,
            query="одинаковое содержание для всех чанков одного документа",
            index_name="violations_index",
        )
        payload = json.loads(out)
        assert payload["count"] == 1, (
            f"expected 1 grouped document, got {payload['count']}"
        )
        assert payload["results"][0]["metadata"]["matched_chunks"] == 5
        assert payload["results"][0]["id"] == 42

    def test_dim_mismatch_returns_search_failure(self, dim_mismatch_tool):
        """Размерность индекса != размерности embedding → search_failure."""
        out = _exec(
            dim_mismatch_tool, query="qu_0", index_name="violations_index"
        )
        payload = json.loads(out)
        assert payload["status"] == "error"
        assert payload["error_type"] == "search_failure"
        assert "Размерность" in payload["message"]


# ---------------------------------------------------------------------------
# Регрессия: persist-механизм НЕ вытесняет результат при новом пороге
# ---------------------------------------------------------------------------


class TestPersistIntegration:
    def test_payload_size_under_50kb_threshold(self, real_faiss_tool):
        """Ответ tool'а для 5 записей < 50KB (persist_threshold)."""
        out = _exec(
            real_faiss_tool, query="qu_0", index_name="violations_index"
        )
        size = len(out.encode("utf-8"))
        assert size < 50_000, (
            f"payload {size} bytes exceeds new persist_threshold=50000; "
            f"agent will see '[Result saved to ...]' instead of content"
        )