from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lib.services.preload_service import PreloadService


@pytest.fixture
def fake_skill_config(monkeypatch):
    """Регистрирует фейковый модуль skills.audit_analyzer.scripts.skill_config."""
    fake_mod = types.ModuleType("skills.audit_analyzer.scripts.skill_config")
    fake_mod.load_db_config = MagicMock(return_value={"db": "cfg"})
    monkeypatch.setitem(sys.modules, "skills", types.ModuleType("skills"))
    monkeypatch.setitem(sys.modules, "skills.audit_analyzer", types.ModuleType("skills.audit_analyzer"))
    monkeypatch.setitem(sys.modules, "skills.audit_analyzer.scripts", types.ModuleType("skills.audit_analyzer.scripts"))
    monkeypatch.setitem(sys.modules, "skills.audit_analyzer.scripts.skill_config", fake_mod)
    return fake_mod.load_db_config


def _settings(acfg):
    class _Skills:
        audit_analyzer = acfg

    class _Settings:
        skills = _Skills()

    return _Settings()


def _config(workspace="C:/ws"):
    cfg = MagicMock()
    cfg.workspace_path = Path(workspace)
    return cfg


class TestGetAuditCacheConfig:
    def test_disabled_returns_none(self):
        svc = PreloadService(settings=_settings({"in_memory_enabled": False}))
        assert svc.get_audit_cache_config(_config()) == (None, None)

    def test_no_cache_path_returns_none(self):
        svc = PreloadService(settings=_settings(
            {"in_memory_enabled": True, "in_memory_cache_path": ""}))
        assert svc.get_audit_cache_config(_config()) == (None, None)

    def test_absolute_path_resolved(self, fake_skill_config):
        svc = PreloadService(settings=_settings(
            {"in_memory_enabled": True,
             "in_memory_cache_path": r"C:\abs\cache.db"}))
        cache_path, db_cfg = svc.get_audit_cache_config(_config())
        assert cache_path == r"C:\abs\cache.db"
        assert db_cfg == {"db": "cfg"}

    def test_relative_path_against_workspace(self, fake_skill_config):
        svc = PreloadService(settings=_settings(
            {"in_memory_enabled": True, "in_memory_cache_path": "rel/cache.db"}))
        cache_path, _ = svc.get_audit_cache_config(_config("C:/ws"))
        assert "cache.db" in cache_path
        assert cache_path.startswith(r"C:\ws\skills\audit_analyzer")

    def test_exception_returns_none(self):
        class _Boom:
            def __getattr__(self, name):
                raise AttributeError("fail")

        acfg = _Boom()
        svc = PreloadService(settings=_settings(acfg))
        assert svc.get_audit_cache_config(_config()) == (None, None)


class TestPreloadAuditCache:
    def test_no_config_returns_early(self):
        svc = PreloadService(settings=_settings({"in_memory_enabled": False}))
        with patch("lib.services.cache_provider_impl.load_cache_from_postgres") as mock_load:
            svc.preload_audit_cache(_config())
            mock_load.assert_not_called()

    def test_fresh_cache_skips_load(self, tmp_path):
        cache_file = tmp_path / "cache.db"
        cache_file.write_text("data", encoding="utf-8")
        svc = PreloadService(settings=_settings(
            {"in_memory_enabled": True, "in_memory_cache_path": str(cache_file)}))
        with patch("lib.services.cache_provider_impl.load_cache_from_postgres") as mock_load:
            svc.preload_audit_cache(_config())
            mock_load.assert_not_called()

    def test_stale_cache_loads(self, tmp_path, fake_skill_config):
        cache_file = tmp_path / "cache.db"
        cache_file.write_text("data", encoding="utf-8")
        svc = PreloadService(settings=_settings(
            {"in_memory_enabled": True, "in_memory_cache_path": str(cache_file)}))
        with patch.object(Path, "stat") as mock_stat, \
             patch("lib.services.cache_provider_impl.load_cache_from_postgres") as mock_load:
            stat_result = MagicMock()
            stat_result.st_mtime = 1000
            mock_stat.return_value = stat_result
            svc.preload_audit_cache(_config())
            mock_load.assert_called_once_with(str(cache_file), {"db": "cfg"})

    def test_missing_cache_loads(self, fake_skill_config):
        svc = PreloadService(settings=_settings(
            {"in_memory_enabled": True, "in_memory_cache_path": "nonexistent.db"}))
        with patch("lib.services.cache_provider_impl.load_cache_from_postgres") as mock_load:
            svc.preload_audit_cache(_config())
            mock_load.assert_called_once()


class TestBackgroundAuditCacheRefresh:
    @pytest.mark.asyncio
    async def test_no_config_returns_early(self):
        svc = PreloadService(settings=_settings({"in_memory_enabled": False}))
        assert await svc.background_audit_cache_refresh(_config()) is None

    @pytest.mark.asyncio
    async def test_stale_triggers_reload(self, fake_skill_config):
        svc = PreloadService(settings=_settings(
            {"in_memory_enabled": True, "in_memory_cache_path": "cache.db"}))
        with patch("lib.services.preload_service.asyncio.sleep",
                   new_callable=AsyncMock) as mock_sleep, \
             patch("lib.services.cache_provider_impl.check_cache_stale",
                   return_value={"stale_tables": ["audit_log"]}) as mock_stale, \
             patch("lib.services.cache_provider_impl.load_cache_from_postgres") as mock_load:
            mock_sleep.side_effect = [None, asyncio.CancelledError()]
            await svc.background_audit_cache_refresh(_config())
            mock_stale.assert_called_once()
            mock_load.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_stale_skips_reload(self, fake_skill_config):
        svc = PreloadService(settings=_settings(
            {"in_memory_enabled": True, "in_memory_cache_path": "cache.db"}))
        with patch("lib.services.preload_service.asyncio.sleep",
                   new_callable=AsyncMock) as mock_sleep, \
             patch("lib.services.cache_provider_impl.check_cache_stale",
                   return_value={"stale_tables": []}), \
             patch("lib.services.cache_provider_impl.load_cache_from_postgres") as mock_load:
            mock_sleep.side_effect = [None, asyncio.CancelledError()]
            await svc.background_audit_cache_refresh(_config())
            mock_load.assert_not_called()


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
    async def test_start_stop_tasks(self):
        svc = PreloadService(settings=_settings({"in_memory_enabled": False}))
        tasks = await svc.start_audit_cache_tasks(_config())
        await svc.stop_tasks(tasks)
