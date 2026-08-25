"""Тесты для ``lib/services/preload_service.py``.

После рефакторинга ``refactor/core-extract-duckdb-faiss`` сервис
содержит только ``preload_vector_indexes`` (legacy CLI-методы
``preload_audit_cache`` / ``background_audit_cache_refresh`` удалены
как неиспользуемые — единственный писатель ``audit_cache.duckdb``
теперь ``DuckDbCacheStore.publish()`` через gateway).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from lib.services.preload_service import PreloadService


class TestPreloadVectorIndexes:
    @pytest.mark.asyncio
    async def test_store_not_ready_returns_none(self):
        store = MagicMock()
        store.is_ready.return_value = False
        svc = PreloadService()
        assert await svc.preload_vector_indexes(store) is None

    @pytest.mark.asyncio
    async def test_ready_preloads(self):
        store = MagicMock()
        store.is_ready.return_value = True
        store.preload_indexes.return_value = [{"index_name": "a", "vectors": 10}]
        svc = PreloadService()
        result = await svc.preload_vector_indexes(store)
        assert result == [{"index_name": "a", "vectors": 10}]

    @pytest.mark.asyncio
    async def test_error_returns_none(self):
        store = MagicMock()
        store.is_ready.return_value = True
        store.preload_indexes.side_effect = RuntimeError("boom")
        svc = PreloadService()
        assert await svc.preload_vector_indexes(store) is None

    @pytest.mark.asyncio
    async def test_none_store_returns_none(self):
        svc = PreloadService()
        assert await svc.preload_vector_indexes(None) is None