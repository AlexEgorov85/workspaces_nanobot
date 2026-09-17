"""Integration-тест вертикального среза векторной подсистемы (e2e).

После change ``remove-vector-index-store`` покрывает полный lifecycle:

    raw vectors (``<storage_table>``) → DuckDB-снапшот →
    ``provider.preload_indexes`` → in-memory ``IndexFlatIP`` →
    ``search_vector`` → корректный top-hit с score == cosine(query, doc).

Также верифицирует:
  * P0-2 (нормализация L2 для cosine): после сборки косинус запроса
    к эталонному документу равен ~1.0, а не raw-IP (длина вектора²);
  * холодный miss без прогрева → ошибка ``_search_error`` (без ленивой
    сборки FAISS, как требует спека «Preload indexes at startup»);
  * payload (``content``/``row``) подтягивается per-hit через DuckDB SELECT,
    а не из сериализованного индекса.

PostgreSQL НЕ требуется: ``utils.db.fetch/execute`` замоканы
in-memory фейком PG-store. ``get_embedding`` — детерминированный вектор.
DuckDB-коннекшен провайдера подменяется in-memory DuckDB-инстансом.

Пропускается автоматически, если faiss/numpy/duckdb недоступны.
"""
from __future__ import annotations

import json

import pytest

faiss = pytest.importorskip("faiss")
np = pytest.importorskip("numpy")
duckdb = pytest.importorskip("duckdb")


_EMBED_DIM = 4
_VECTOR_TABLE = "audit_vectors_test_e2e"
_INDEX_NAME = "audits_index"


def _vec(*vals: float) -> list[float]:
    out = [0.0] * _EMBED_DIM
    for i, v in enumerate(vals):
        out[i] = v
    return out


def _emb(text: str) -> list[float]:
    """Детерминированный эмбеддинг для query.

    Для слова-маркера возвращаем ТОЧНО вектор документа A, чтобы
    релевантный top-hit имел cosine == 1.0.
    """
    if text == "Документ A":
        return _vec(1.0, 0.0, 0.0, 0.0)
    return _vec(0.0, 1.0, 0.0, 0.0)


def _make_duckdb_with_vectors(records: list[dict]) -> "duckdb.DuckDBPyConnection":
    """In-memory DuckDB с таблицей ``_VECTOR_TABLE`` и данными.

    Колонки совпадают со схемой ``oarb.audit_vectors``.
    """
    conn = duckdb.connect(":memory:")
    conn.execute(
        f'CREATE TABLE {_VECTOR_TABLE} ('
        f'  id INTEGER,'
        f'  source TEXT,'
        f'  content TEXT,'
        f'  search_text TEXT,'
        f'  "table" TEXT,'
        f'  pk_value TEXT,'
        f'  chunk_index INTEGER,'
        f'  chunk_count INTEGER,'
        f'  row_data JSON,'
        f'  embedding REAL[{_EMBED_DIM}]'
        f')'
    )
    for i, r in enumerate(records):
        conn.execute(
            f'INSERT INTO {_VECTOR_TABLE} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            [
                i,
                r["source"],
                r["content"],
                r["search_text"],
                r["table"],
                str(r["pk_value"]),
                r["chunk_index"],
                r["chunk_count"],
                json.dumps(r["row_data"]),
                r["embedding"],
            ],
        )
    return conn


class _FakePG:
    """In-memory имитация PostgreSQL для legacy-путей (должны быть no-op)."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []

    def fetch(self, sql: str, *args) -> list[dict]:
        return []

    def execute(self, sql: str, *args) -> str:
        self.executed.append((sql.strip(), args))
        return "OK"


def _index_cfg(**overrides) -> dict:
    """Pythonic-конфиг индекса (формат ``read_vector_index_config``)."""
    cfg = {
        "table": "oarb.audits",
        "pk": "id",
        "source_table": "audits",
        "content_columns": ["title"],
        "embedding_columns": [{"col": "title", "chunk": True}],
        "track_column": "updated_at",
        "chunk_size": 500,
        "chunk_overlap": 80,
        "metric": "cosine",
        "enabled": True,
    }
    cfg.update(overrides)
    return cfg


def _patch_impl(monkeypatch, fake_pg: _FakePG, duck_conn, index_cfg: dict):
    import utils.db as dbmod

    import lib.services.cache_provider_impl as impl

    monkeypatch.setattr(dbmod, "fetch", fake_pg.fetch)
    monkeypatch.setattr(dbmod, "execute", fake_pg.execute)

    cfg_container = {"indexes": {_INDEX_NAME: index_cfg}}
    monkeypatch.setattr(
        impl, "read_vector_index_config",
        lambda _cfg: cfg_container["indexes"],
    )
    monkeypatch.setattr(
        impl, "read_embedding_config",
        lambda: {"model": "mxbai-embed-large:latest", "dimension": _EMBED_DIM},
    )
    return impl, cfg_container


def _make_provider(monkeypatch, fake_pg: _FakePG, duck_conn, index_cfg: dict):
    """Создать провайдер с подменёнными зависимостями и DuckDB-коннекшеном."""
    import lib.services.cache_provider_impl as impl

    impl_, cfg_container = _patch_impl(monkeypatch, fake_pg, duck_conn, index_cfg)
    provider = impl_.PostgresDuckDbProvider(
        vector_db_table=_VECTOR_TABLE,
        vector_indexes={_INDEX_NAME: index_cfg},
    )
    provider._conn = duck_conn
    return provider, cfg_container


class TestVectorBuildE2E:
    def test_build_faiss_cosine_normalizes_vectors(self):
        """P0-2: ``build_faiss_index(metric="cosine")`` нормализует векторы."""
        from lib.utils.duckdb_query import build_faiss_index

        idx, meta = build_faiss_index(
            [{
                "embedding": _vec(3.0, 4.0, 0.0, 0.0),
                "source": "s", "table": "t", "pk_value": 1,
            }],
            metric="cosine",
        )
        assert meta["metric"] == "cosine"
        vec = idx.reconstruct(0)
        np.testing.assert_allclose(np.linalg.norm(vec), 1.0, atol=1e-6)

    def test_preload_then_search_returns_correct_top_hit(self, monkeypatch):
        """Полный vertical slice: DuckDB → preload_indexes → search_vector."""
        fake = _FakePG()
        records = [
            {
                "source": _INDEX_NAME, "content": "Документ A",
                "search_text": "Документ A", "table": "oarb.audits",
                "pk_value": 1, "chunk_index": 0, "chunk_count": 1,
                "row_data": {"id": 1, "title": "Документ A"},
                "embedding": _vec(1.0, 0.0, 0.0, 0.0),
            },
            {
                "source": _INDEX_NAME, "content": "Документ B",
                "search_text": "Документ B", "table": "oarb.audits",
                "pk_value": 2, "chunk_index": 0, "chunk_count": 1,
                "row_data": {"id": 2, "title": "Документ B"},
                "embedding": _vec(0.0, 1.0, 0.0, 0.0),
            },
        ]
        conn = _make_duckdb_with_vectors(records)
        import lib.services.cache_provider_impl as impl
        impl_, _ = _patch_impl(monkeypatch, fake, conn, _index_cfg())
        monkeypatch.setattr(impl_, "get_embedding", lambda text: _emb(text))
        provider, _ = _make_provider(monkeypatch, fake, conn, _index_cfg())

        # 1. preload_indexes: DuckDB → in-memory FAISS.
        loaded = provider.preload_indexes(_VECTOR_TABLE)
        assert len(loaded) == 1
        assert loaded[0]["index_name"] == _INDEX_NAME
        assert loaded[0]["vectors"] == 2
        assert loaded[0].get("signature_status") == "CURRENT"

        # 2. search_vector: документ A должен быть top-1, score ≈ cosine.
        results = provider.search_vector(
            query="Документ A", index_name=_INDEX_NAME, top_k=1,
        )
        assert len(results) == 1
        assert results[0].pk_value in (1, "1")  # TEXT в DuckDB-схеме, int в старых тестах
        assert results[0].score == pytest.approx(1.0, abs=1e-5)
        assert results[0].content == "Документ A"
        assert results[0].row == {"id": 1, "title": "Документ A"}

    def test_cold_miss_returns_error_no_lazy_build(self, monkeypatch):
        """Cold miss для непрогретого индекса → ошибка, без ленивой сборки."""
        fake = _FakePG()
        conn = _make_duckdb_with_vectors([])
        import lib.services.cache_provider_impl as impl
        impl_, _ = _patch_impl(monkeypatch, fake, conn, _index_cfg())
        monkeypatch.setattr(impl_, "get_embedding", lambda text: _emb(text))
        provider, _ = _make_provider(monkeypatch, fake, conn, _index_cfg())

        # preload_indexes не вызывался → _index_cache пуст.
        results = provider.search_vector(
            query="anything", index_name=_INDEX_NAME, top_k=1,
        )
        assert results == []
        assert provider._search_error is not None
