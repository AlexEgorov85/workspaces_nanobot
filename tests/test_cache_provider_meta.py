"""Подпись векторного индекса и её проверка (``cache_provider_impl``).

Раньше здесь же лежали тесты ``_capture_schema_meta`` — функции без единого
production-вызова, удалённой как дубликат ``DuckDbCacheStore._save_schema_meta``.
Живой путь метаданных схемы покрыт в ``tests/test_duckdb_cache_store.py``
(``class TestSchema``).
"""

from __future__ import annotations

from tests.conftest import TEST_TABLE as _TEST_TABLE


class TestIndexSignature:
    """``compute_index_signature`` + ``verify_index_signature`` —
    integrity-метаданные для ``agent_vector_index_store``.
    """

    def test_compute_deterministic_same_input(self):
        from lib.services.cache_provider_impl import compute_index_signature

        cfg = {
            "src_table": _TEST_TABLE,
            "pk_column": "id",
            "content_cols": ["title", "description"],
            "embedding_cols": [{"col": "title", "text_chunk_size": 500}],
            "track_column": "updated_at",
            "embedding_model": "mxbai-embed-large:latest",
            "embedding_dimension": 1024,
            "chunk_size": 500,
            "chunk_overlap": 80,
        }
        sig1 = compute_index_signature(cfg)
        sig2 = compute_index_signature(cfg)
        assert sig1 == sig2
        assert len(sig1) == 64
        assert all(c in "0123456789abcdef" for c in sig1)

    def test_compute_changes_on_model_change(self):
        from lib.services.cache_provider_impl import compute_index_signature

        cfg1 = {"embedding_model": "mxbai", "embedding_dimension": 1024}
        cfg2 = {"embedding_model": "nomic", "embedding_dimension": 1024}
        assert compute_index_signature(cfg1) != compute_index_signature(cfg2)

    def test_compute_changes_on_dimension_change(self):
        from lib.services.cache_provider_impl import compute_index_signature

        cfg1 = {"embedding_dimension": 1024}
        cfg2 = {"embedding_dimension": 768}
        assert compute_index_signature(cfg1) != compute_index_signature(cfg2)

    def test_compute_changes_on_chunk_change(self):
        from lib.services.cache_provider_impl import compute_index_signature

        cfg1 = {"chunk_size": 500, "chunk_overlap": 80}
        cfg2 = {"chunk_size": 600, "chunk_overlap": 80}
        cfg3 = {"chunk_size": 500, "chunk_overlap": 100}
        sig1 = compute_index_signature(cfg1)
        assert compute_index_signature(cfg2) != sig1
        assert compute_index_signature(cfg3) != sig1

    def test_compute_handles_missing_keys_as_empty(self):
        from lib.services.cache_provider_impl import compute_index_signature

        sig_empty = compute_index_signature({})
        sig_same = compute_index_signature({})
        assert sig_empty == sig_same

    def test_verify_current_when_signatures_match(self):
        from lib.services.cache_provider_impl import (
            compute_index_signature,
            verify_index_signature,
        )

        cfg = {"embedding_model": "mxbai", "embedding_dimension": 1024}
        stored_sig = compute_index_signature(cfg)
        assert verify_index_signature({"signature": stored_sig}, cfg) == "CURRENT"

    def test_verify_stale_when_model_changed(self):
        from lib.services.cache_provider_impl import (
            compute_index_signature,
            verify_index_signature,
        )

        stored_cfg = {"embedding_model": "mxbai", "embedding_dimension": 1024}
        current_cfg = {"embedding_model": "nomic", "embedding_dimension": 1024}
        stored_sig = compute_index_signature(stored_cfg)
        assert verify_index_signature({"signature": stored_sig}, current_cfg) == "STALE"

    def test_verify_current_when_no_signature(self):
        """Без stored signature — CURRENT (новое поведение change remove-vector-index-store).

        Раньше возвращался INVALID (нет signature → «legacy blob»). После
        change persisted-signature больше нет; сигнатура вычисляется
        inline при preload и всегда совпадает с текущим конфигом.
        """
        from lib.services.cache_provider_impl import verify_index_signature

        assert verify_index_signature({}, {"embedding_model": "mxbai"}) == "CURRENT"
        assert verify_index_signature(
            {"signature": None}, {"embedding_model": "mxbai"},
        ) == "CURRENT"

    def test_verify_invalid_when_signature_corrupt(self):
        from lib.services.cache_provider_impl import verify_index_signature

        assert verify_index_signature(
            {"signature": "not-hex"}, {},
        ) == "INVALID"
        assert verify_index_signature(
            {"signature": "a" * 32}, {},
        ) == "INVALID"

    def test_verify_current_when_no_metadata(self):
        """Без stored_meta — нет данных для проверки, трактуем как CURRENT
        (индекс ещё не был сохранён / нечего перепроверять).
        """
        from lib.services.cache_provider_impl import verify_index_signature

        assert verify_index_signature(None, {}) == "CURRENT"

