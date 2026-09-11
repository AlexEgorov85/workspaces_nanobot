"""Integration-тест вертикального среза векторной подсистемы (P0-e2e).

Покрывает полный lifecycle:

    raw vectors (PG) → `rebuild_and_store_index` → FAISS (cosine) →
    `agent_vector_index_store` (blob + signature) → НОВЫЙ провайдер
    (симуляция restart) → `_load_index` (reload из store) → `search_vector`
    → корректный top-hit с score == cosine(query, doc).

Также верифицирует P0-2 (нормализация L2 для cosine): после сборки косинус
запроса к эталонному документу равен ~1.0, а не raw-IP (длина вектора²).

PostgreSQL НЕ требуется: ``utils.db.fetch/execute`` замоканы in-memory
фейком PG-store. ``get_embedding`` — детерминированный вектор.

Пропускается автоматически, если faiss/numpy недоступны.
"""
from __future__ import annotations

import json

import pytest

faiss = pytest.importorskip("faiss")
np = pytest.importorskip("numpy")


_EMBED_DIM = 4
_VECTOR_TABLE = "oarb.audit_vectors"
_STORE_TABLE = "public.agent_vector_index_store"
_CONFIG_TABLE = "public.agent_vector_index_config"
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


class _FakePG:
    """In-memory имитация PostgreSQL: store + конфиг + векторы."""

    def __init__(self) -> None:
        self.store: dict[str, dict] = {}
        self.config_rows: list[dict] = []
        self.config_row_single: dict | None = None
        self.vector_rows: list[dict] = []
        self.executed: list[tuple[str, tuple]] = []

    def fetch(self, sql: str, *args) -> list[dict]:
        s = sql.strip()
        lower = s.lower()
        if lower.startswith("select index_name, source_table"):
            return list(self.config_rows)
        if "where index_name = %s" in lower and "chunk_size" in lower:
            return [dict(self.config_row_single)] if self.config_row_single else []
        if "index_binary" in lower and "agent_vector_index_store" in lower:
            entry = self.store.get(args[0]) if args else None
            if entry:
                return [{"index_binary": entry["index_binary"], "metadata": entry["metadata"]}]
            return []
        if lower.startswith("select 1 from"):
            return [{"?column?": 1}] if self.store.get(args[0]) else []
        if lower.startswith("select id, source, content"):
            return [dict(r) for r in self.vector_rows]
        return []

    def execute(self, sql: str, *args) -> str:
        s = sql.strip()
        self.executed.append((s, args))
        lower = s.lower()
        if "agent_vector_index_store" in lower and (lower.startswith("update") or lower.startswith("insert")):
            blob, meta_json, dim, ntotal, source = self._unpack(lower, args)
            self.store[source] = {
                "index_binary": blob,
                "metadata": meta_json,
                "dimension": dim,
                "vector_count": ntotal,
            }
            return "UPDATE 1" if lower.startswith("update") else "INSERT 1"
        return "OK"

    @staticmethod
    def _unpack(lower: str, args: tuple):
        # INSERT: (source, blob, meta_json, dim, ntotal) | UPDATE: (blob, meta_json, dim, ntotal, source)
        if lower.startswith("insert"):
            source, blob, meta_json, dim, ntotal = args
        else:
            blob, meta_json, dim, ntotal, source = args
        return blob, meta_json, dim, ntotal, source


def _patch_impl(monkeypatch, fake: _FakePG):
    import utils.db as dbmod

    import lib.services.cache_provider_impl as impl

    monkeypatch.setattr(dbmod, "fetch", fake.fetch)
    monkeypatch.setattr(dbmod, "execute", fake.execute)
    monkeypatch.setattr(impl, "read_vector_index_config_table", lambda: _CONFIG_TABLE)
    monkeypatch.setattr(
        impl, "read_embedding_config",
        lambda: {"model": "mxbai-embed-large:latest", "dimension": _EMBED_DIM},
    )
    return impl


def _setup_fake(fake: _FakePG) -> None:
    fake.config_rows = [{
        "index_name": _INDEX_NAME,
        "source_table": "audits",
        "src_table": "oarb.audits",
        "pk_column": "id",
        "content_cols": ["title"],
        "embedding_cols": [{"col": "title", "chunk": True}],
        "track_column": "updated_at",
        "chunk_size": 500,
        "chunk_overlap": 80,
        "metric": "cosine",
        "enabled": True,
    }]
    fake.config_row_single = {
        "src_table": "oarb.audits",
        "pk_column": "id",
        "content_cols": ["title"],
        "embedding_cols": [{"col": "title", "chunk": True}],
        "track_column": "updated_at",
        "chunk_size": 500,
        "chunk_overlap": 80,
        "metric": "cosine",
    }
    fake.vector_rows = [
        {
            "source": _INDEX_NAME, "content": "Документ A", "search_text": "Документ A",
            "table": "oarb.audits", "pk_value": 1, "chunk_index": 0, "chunk_count": 1,
            "row_data": json.dumps({"id": 1, "title": "Документ A"}),
            "embedding": _vec(1.0, 0.0, 0.0, 0.0),
        },
        {
            "source": _INDEX_NAME, "content": "Документ B", "search_text": "Документ B",
            "table": "oarb.audits", "pk_value": 2, "chunk_index": 0, "chunk_count": 1,
            "row_data": json.dumps({"id": 2, "title": "Документ B"}),
            "embedding": _vec(0.0, 1.0, 0.0, 0.0),
        },
    ]


class TestVectorBuildE2E:
    def test_build_faiss_cosine_normalizes_vectors(self):
        """P0-2: ``build_faiss_index(metric="cosine")`` нормализует векторы."""
        from lib.utils.duckdb_query import build_faiss_index

        idx, meta = build_faiss_index(
            [{"embedding": _vec(3.0, 4.0, 0.0, 0.0),
              "source": "s", "table": "t", "pk_value": 1, "content": "x"}],
            metric="cosine",
        )
        assert meta["metric"] == "cosine"
        vec = idx.reconstruct(0)
        np.testing.assert_allclose(np.linalg.norm(vec), 1.0, atol=1e-6)

    def test_rebuild_store_reload_search(self, monkeypatch):
        """Полный vertical slice: build → store → reload (новый провайдер) → search."""
        fake = _FakePG()
        _setup_fake(fake)
        impl = _patch_impl(monkeypatch, fake)
        monkeypatch.setattr(impl, "get_embedding", lambda text: _emb(text))

        provider = impl.PostgresDuckDbProvider(
            vector_db_table=_VECTOR_TABLE,
            vector_store_table=_STORE_TABLE,
        )

        # 1. Build: vectors → FAISS → persist в store.
        count = provider.rebuild_and_store_index(_INDEX_NAME, _VECTOR_TABLE)
        assert count == 2
        assert _INDEX_NAME in fake.store
        meta_saved = json.loads(fake.store[_INDEX_NAME]["metadata"])
        assert meta_saved.get("metric") == "cosine"
        assert meta_saved.get("signature"), "signature должна писаться в store"
        sig_saved = meta_saved["signature"]

        # 2. "Restart": новый провайдер с чистым кэшем → reload из store.
        provider2 = impl.PostgresDuckDbProvider(
            vector_db_table=_VECTOR_TABLE,
            vector_store_table=_STORE_TABLE,
        )
        idx, meta = provider2._load_index("", _INDEX_NAME, _VECTOR_TABLE)
        assert idx is not None and idx.ntotal == 2
        assert meta.get("signature") == sig_saved, "blob из store несёт ту же signature"
        assert meta.get("_signature_status", "CURRENT") == "CURRENT"

        # 3. Search: документ A должен быть top-1, score ≈ cosine.
        results = provider2.search_vector(
            query="Документ A", index_name=_INDEX_NAME, top_k=1,
        )
        assert len(results) == 1
        assert results[0].pk_value == 1
        assert results[0].score == pytest.approx(1.0, abs=1e-5)

    def test_config_change_detects_stale_on_reload(self, monkeypatch):
        """Смена chunk_size в реестре → STALE при reload (но загрузка работает)."""
        fake = _FakePG()
        _setup_fake(fake)
        impl = _patch_impl(monkeypatch, fake)
        monkeypatch.setattr(impl, "get_embedding", lambda text: _emb(text))

        provider = impl.PostgresDuckDbProvider(
            vector_db_table=_VECTOR_TABLE,
            vector_store_table=_STORE_TABLE,
        )
        provider.rebuild_and_store_index(_INDEX_NAME, _VECTOR_TABLE)

        # Конфиг изменился: chunk_size 500 → 900 (пересборка обязательна).
        fake.config_rows[0]["chunk_size"] = 900
        fake.config_row_single["chunk_size"] = 900

        provider2 = impl.PostgresDuckDbProvider(
            vector_db_table=_VECTOR_TABLE,
            vector_store_table=_STORE_TABLE,
        )
        idx, meta = provider2._load_index("", _INDEX_NAME, _VECTOR_TABLE)
        assert idx is not None  # STALE не блокирует загрузку
        assert meta.get("_signature_status") == "STALE"