"""P0: signature индекса покрывает chunk_size / chunk_overlap / metric.

Контракт (docs/TARGET_ARCHITECTURE.md §22.9 по vector-части):
    build  →  конфиг-реестр (agent_vector_index_config)  →  signature
    verify →  тот же конфиг-реестр                        →  signature
Один и тот же canonical config-объект в обоих местах: ``_read_current_index_config``
(verify) и ``_compute_index_signature_from_config`` (build).

Раньше ``_INDEX_SIGNATURE_FIELDS`` объявлял chunk_size/chunk_overlap, но ни один
из config-ридеров их не заполнял — смена chunk-параметров не помечала индекс
как STALE. Эти тесты фиксируют, что поля реально доходят до signature.
"""
from __future__ import annotations

import pytest


def _base_cfg(**overrides) -> dict:
    cfg = {
        "table": "oarb.audits",
        "pk": "id",
        "content_columns": ["title", "description"],
        "embedding_columns": [{"col": "title", "chunk": True}],
        "track_column": "updated_at",
        "chunk_size": 500,
        "chunk_overlap": 80,
        "metric": "cosine",
    }
    cfg.update(overrides)
    return cfg


def _make_provider(monkeypatch, indexes: dict, emb_cfg: dict | None = None):
    """Провайдер с подменёнными конфиг-ридерами (без реальной БД)."""
    import lib.services.cache_provider_impl as impl

    monkeypatch.setattr(impl, "read_vector_index_config", lambda _c: indexes)
    monkeypatch.setattr(
        impl, "read_embedding_config",
        lambda: emb_cfg if emb_cfg is not None
        else {"model": "mxbai-embed-large:latest", "dimension": 1024},
    )
    return impl.PostgresDuckDbProvider()


class TestReadCurrentIndexConfig:
    def test_includes_chunk_and_metric_from_registry(self, monkeypatch):
        impl = _make_provider(monkeypatch, {"audits_index": _base_cfg(
            chunk_size=300, chunk_overlap=40, metric="inner_product",
        )})
        cfg = impl._read_current_index_config("audits_index")
        assert cfg["chunk_size"] == 300
        assert cfg["chunk_overlap"] == 40
        assert cfg["metric"] == "inner_product"
        assert cfg["src_table"] == "oarb.audits"
        assert cfg["embedding_model"] == "mxbai-embed-large:latest"

    def test_falls_back_to_embedding_defaults_on_legacy_registry(self, monkeypatch):
        """Схема до миграции V002 не имеет chunk-колонок — fallback на дефолты."""
        import lib.services.cache_provider_impl as impl

        legacy = _base_cfg()
        for key in ("chunk_size", "chunk_overlap", "metric"):
            legacy.pop(key)
        provider = _make_provider(monkeypatch, {"audits_index": legacy})
        defaults = impl.read_embedding_defaults()
        cfg = provider._read_current_index_config("audits_index")
        assert cfg["chunk_size"] == defaults["chunk_size"]
        assert cfg["chunk_overlap"] == defaults["chunk_overlap"]
        assert cfg["metric"] == "cosine"

    def test_none_when_index_missing(self, monkeypatch):
        provider = _make_provider(monkeypatch, {})
        assert provider._read_current_index_config("audits_index") is None


class TestChunkChangeDetectsStale:
    """Ключевой P0-контракт: смена chunk-параметров в реестре → STALE."""

    def test_chunk_size_change_detected(self, monkeypatch):
        import lib.services.cache_provider_impl as impl
        from lib.services.cache_provider_impl import (
            compute_index_signature,
            verify_index_signature,
        )

        provider = _make_provider(
            monkeypatch, {"audits_index": _base_cfg(chunk_size=500)},
        )
        stored_cfg = provider._read_current_index_config("audits_index")
        stored_sig = compute_index_signature(stored_cfg)

        # Конфиг в реестре изменился: chunk_size 500 → 800.
        monkeypatch.setattr(
            impl, "read_vector_index_config",
            lambda _c: {"audits_index": _base_cfg(chunk_size=800)},
        )
        current_cfg = provider._read_current_index_config("audits_index")

        assert stored_sig != compute_index_signature(current_cfg)
        assert verify_index_signature(
            {"signature": stored_sig}, current_cfg,
        ) == "STALE"

    def test_metric_change_detected(self, monkeypatch):
        import lib.services.cache_provider_impl as impl
        from lib.services.cache_provider_impl import (
            compute_index_signature,
            verify_index_signature,
        )

        provider = _make_provider(
            monkeypatch, {"audits_index": _base_cfg(metric="cosine")},
        )
        stored_sig = compute_index_signature(
            provider._read_current_index_config("audits_index"),
        )
        monkeypatch.setattr(
            impl, "read_vector_index_config",
            lambda _c: {"audits_index": _base_cfg(metric="inner_product")},
        )
        current_cfg = provider._read_current_index_config("audits_index")

        assert verify_index_signature(
            {"signature": stored_sig}, current_cfg,
        ) == "STALE"

    def test_embedding_columns_change_detected(self, monkeypatch):
        import lib.services.cache_provider_impl as impl
        from lib.services.cache_provider_impl import (
            compute_index_signature,
            verify_index_signature,
        )

        provider = _make_provider(
            monkeypatch, {"audits_index": _base_cfg()},
        )
        stored_sig = compute_index_signature(
            provider._read_current_index_config("audits_index"),
        )
        monkeypatch.setattr(
            impl, "read_vector_index_config",
            lambda _c: {"audits_index": _base_cfg(
                embedding_columns=[{"col": "description", "chunk": True}],
            )},
        )
        current_cfg = provider._read_current_index_config("audits_index")

        assert verify_index_signature(
            {"signature": stored_sig}, current_cfg,
        ) == "STALE"


class TestComputeIndexSignatureFromConfig:
    def test_includes_chunk_and_metric(self, monkeypatch):
        import utils.db as dbmod

        import lib.services.cache_provider_impl as impl

        row = {
            "src_table": "oarb.audits",
            "pk_column": "id",
            "content_cols": ["title"],
            "embedding_cols": [{"col": "title"}],
            "track_column": "updated_at",
            "chunk_size": 300,
            "chunk_overlap": 40,
            "metric": "inner_product",
        }
        monkeypatch.setattr(dbmod, "fetch", lambda *a, **k: [row])
        monkeypatch.setattr(
            impl, "read_vector_index_config_table", lambda: "public.agent_vector_index_config",
        )
        monkeypatch.setattr(
            impl, "read_embedding_config",
            lambda: {"model": "mxbai-embed-large:latest", "dimension": 1024},
        )
        provider = impl.PostgresDuckDbProvider(
            vector_store_table="public.agent_vector_index_store",
        )
        sig = provider._compute_index_signature_from_config("audits_index")

        expected = impl.compute_index_signature({
            "src_table": "oarb.audits",
            "pk_column": "id",
            "content_cols": ["title"],
            "embedding_cols": [{"col": "title"}],
            "track_column": "updated_at",
            "embedding_model": "mxbai-embed-large:latest",
            "embedding_dimension": 1024,
            "chunk_size": 300,
            "chunk_overlap": 40,
            "metric": "inner_product",
        })
        assert sig == expected

    def test_chunk_change_changes_build_signature(self, monkeypatch):
        import utils.db as dbmod

        import lib.services.cache_provider_impl as impl

        def _sig(chunk_size: int, metric: str) -> str:
            row = {
                "src_table": "oarb.audits", "pk_column": "id",
                "content_cols": ["title"], "embedding_cols": [{"col": "title"}],
                "track_column": "updated_at",
                "chunk_size": chunk_size, "chunk_overlap": 80, "metric": metric,
            }
            monkeypatch.setattr(dbmod, "fetch", lambda *a, **k: [row])
            provider = impl.PostgresDuckDbProvider(
                vector_store_table="public.agent_vector_index_store",
            )
            return provider._compute_index_signature_from_config("audits_index")

        assert _sig(500, "cosine") != _sig(800, "cosine")
        assert _sig(500, "cosine") != _sig(500, "inner_product")

    def test_returns_none_when_no_store_table(self, monkeypatch):
        import lib.services.cache_provider_impl as impl

        provider = impl.PostgresDuckDbProvider(vector_store_table="")
        assert provider._compute_index_signature_from_config("audits_index") is None


class TestGetIndexMetric:
    def test_returns_metric_from_registry(self, monkeypatch):
        provider = _make_provider(monkeypatch, _register_with_metric("cosine"))
        assert provider._get_index_metric("audits_index") == "cosine"

    def test_returns_none_when_no_registry(self, monkeypatch):
        provider = _make_provider(monkeypatch, {})
        assert provider._get_index_metric("audits_index") is None


def _register_with_metric(metric: str) -> dict:
    return {"audits_index": _base_cfg(metric=metric)}