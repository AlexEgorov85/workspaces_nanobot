from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

_project_root = Path(__file__).resolve().parent.parent
_workspace_path = str(_project_root / "workspace")
if _workspace_path not in sys.path:
    sys.path.insert(0, _workspace_path)
# Add project root for config import
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

# conftest.py adds user site-packages for nanobot

from cli_agent import (
    DisplayConfig,
    _patch_agent_tool_audit,
    _parse_args,
    _scan_and_register_hooks,
)


class TestDisplayConfig:
    def test_defaults(self):
        cfg = DisplayConfig()
        assert cfg.show_reasoning is True
        assert cfg.show_tool_calls is True
        assert cfg.show_tool_results is True
        assert cfg.show_tool_params is True
        assert cfg.show_progress is True
        assert cfg.typewriter_speed == 0.01

    def test_custom_values(self):
        cfg = DisplayConfig(show_reasoning=False, typewriter_speed=0.05)
        assert cfg.show_reasoning is False
        assert cfg.typewriter_speed == 0.05


class TestPatchAgentToolAudit:
    def test_wraps_assemble_outbound(self):
        agent = MagicMock()
        hook = MagicMock()
        hook.drain.return_value = [{"name": "test_tool"}]

        # Set the return value before patching so _orig captures it
        original_return = MagicMock()
        original_return.metadata = {}
        agent._assemble_outbound.return_value = original_return

        _patch_agent_tool_audit(agent, hook)

        output = agent._assemble_outbound(MagicMock(), "content", [], "stop", False, None)

        hook.drain.assert_called_once()
        assert output.metadata["_tool_audit"] == [{"name": "test_tool"}]

    def test_no_hook_when_result_none(self):
        agent = MagicMock()
        orig = agent._assemble_outbound
        orig.return_value = None

        hook = MagicMock()
        _patch_agent_tool_audit(agent, hook)

        result = agent._assemble_outbound(None, None, None, None, False, None)
        assert result is None
        hook.drain.assert_not_called()


class TestTypewriter:
    @pytest.mark.asyncio
    async def test_empty_text_noop(self):
        from cli_agent import _typewriter

        await _typewriter("", "dim", 0.01)

    @pytest.mark.asyncio
    async def test_zero_speed_prints_immediately(self):
        from cli_agent import _typewriter

        with patch("cli_agent.console") as mock_console:
            await _typewriter("hello", "bold", 0)
            mock_console.print.assert_called_once()


class TestPrintReasoningBlock:
    @pytest.mark.asyncio
    async def test_blank_text_noop(self):
        from cli_agent import _print_reasoning_block

        await _print_reasoning_block("  ", DisplayConfig())

    @pytest.mark.asyncio
    async def test_hidden_when_disabled(self):
        from cli_agent import _print_reasoning_block

        await _print_reasoning_block("text", DisplayConfig(show_reasoning=False))


class TestPrintToolEvents:
    @pytest.mark.asyncio
    async def test_empty_events_noop(self):
        from cli_agent import _print_tool_events

        await _print_tool_events([], DisplayConfig())

    @pytest.mark.asyncio
    async def test_hidden_when_disabled(self):
        from cli_agent import _print_tool_events

        await _print_tool_events([{"name": "x"}], DisplayConfig(show_tool_calls=False))

    @pytest.mark.asyncio
    async def test_non_dict_skipped(self):
        from cli_agent import _print_tool_events

        await _print_tool_events(["string"], DisplayConfig())

    @pytest.mark.asyncio
    async def test_ok_status(self):
        from cli_agent import _print_tool_events, _typewriter

        with patch("cli_agent._typewriter", new_callable=AsyncMock) as mock_tw:
            await _print_tool_events(
                [{"name": "read", "status": "ok", "result_preview": "content"}],
                DisplayConfig(),
            )
            mock_tw.assert_called_once()

    @pytest.mark.asyncio
    async def test_error_status(self):
        from cli_agent import _print_tool_events

        with patch("cli_agent._typewriter", new_callable=AsyncMock) as mock_tw:
            await _print_tool_events(
                [{"name": "write", "status": "error", "error": "permission denied"}],
                DisplayConfig(),
            )
            mock_tw.assert_called_once()

    @pytest.mark.asyncio
    async def test_params_formatted(self):
        from cli_agent import _print_tool_events

        with patch("cli_agent._typewriter", new_callable=AsyncMock) as mock_tw:
            await _print_tool_events(
                [{"name": "search", "status": "ok", "arguments": {"q": "hello", "limit": 5}}],
                DisplayConfig(show_tool_params=True),
            )
            called_text = mock_tw.call_args[0][0]
            assert "q=hello" in called_text
            assert "limit=5" in called_text


class TestParseArgs:
    def test_defaults(self):
        with patch("sys.argv", ["cli_agent.py"]):
            args = _parse_args()
            assert args.patched is False
            assert args.storage == "auto"
            assert args.session is None

    def test_patched_flag(self):
        with patch("sys.argv", ["cli_agent.py", "--patched"]):
            args = _parse_args()
            assert args.patched is True

    def test_storage_postgres(self):
        with patch("sys.argv", ["cli_agent.py", "-P", "-S", "postgres"]):
            args = _parse_args()
            assert args.patched is True
            assert args.storage == "postgres"

    def test_session_key(self):
        with patch("sys.argv", ["cli_agent.py", "-s", "my-session"]):
            args = _parse_args()
            assert args.session == "my-session"

    def test_short_patched(self):
        with patch("sys.argv", ["cli_agent.py", "-P"]):
            args = _parse_args()
            assert args.patched is True


class TestApplyTimeouts:
    def test_sets_env_vars(self):
        from cli_agent import _apply_timeouts, LLM_TIMEOUT

        config = MagicMock()
        with patch.dict("os.environ", clear=True):
            _apply_timeouts(config)
            assert os.environ["NANOBOT_LLM_TIMEOUT_S"] == str(LLM_TIMEOUT)


class TestScanAndRegisterHooks:
    def test_no_hooks_dir(self):
        with patch("cli_agent._HOOKS_DIR", MagicMock()) as mock_dir:
            mock_dir.is_dir.return_value = False
            result = _scan_and_register_hooks()
            assert result == []

    def test_empty_hooks_dir(self):
        with patch("cli_agent._HOOKS_DIR", MagicMock()) as mock_dir:
            mock_dir.is_dir.return_value = True
            mock_dir.iterdir.return_value = []
            result = _scan_and_register_hooks()
            assert result == []


class TestMigrateCronStore:
    def test_moves_legacy_to_workspace(self, tmp_path):
        from cli_agent import _migrate_cron_store

        config = MagicMock()
        config.workspace_path = tmp_path / "workspace"

        legacy = tmp_path / "legacy" / "jobs.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text('{"jobs": []}')

        with patch("nanobot.config.paths.get_cron_dir", return_value=legacy.parent):
            _migrate_cron_store(config)

        new_path = tmp_path / "workspace" / "cron" / "jobs.json"
        assert new_path.exists()
        assert new_path.read_text() == '{"jobs": []}'

    def test_noop_when_legacy_missing(self, tmp_path):
        from cli_agent import _migrate_cron_store

        config = MagicMock()
        config.workspace_path = tmp_path / "workspace"

        legacy_dir = tmp_path / "legacy"
        legacy_dir.mkdir(parents=True)

        with patch("nanobot.config.paths.get_cron_dir", return_value=legacy_dir):
            _migrate_cron_store(config)

        new_path = tmp_path / "workspace" / "cron" / "jobs.json"
        assert not new_path.exists()

    def test_noop_when_new_already_exists(self, tmp_path):
        from cli_agent import _migrate_cron_store

        config = MagicMock()
        config.workspace_path = tmp_path / "workspace"

        legacy = tmp_path / "legacy" / "cron" / "jobs.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text('{"jobs": []}')

        new_path = tmp_path / "workspace" / "cron" / "jobs.json"
        new_path.parent.mkdir(parents=True)
        new_path.write_text('{"jobs": ["existing"]}')

        with patch("nanobot.config.paths.get_cron_dir", return_value=legacy.parent.parent):
            _migrate_cron_store(config)

        assert new_path.read_text() == '{"jobs": ["existing"]}'


class TestGetAuditCacheConfig:
    def test_disabled_returns_none(self):
        from cli_agent import _get_audit_cache_config

        config = MagicMock()
        with patch("cli_agent.SETTINGS") as mock_settings:
            mock_settings.skills.audit_analyzer = {"in_memory_enabled": False}
            result = _get_audit_cache_config(config)
            assert result == (None, None)

    def test_no_cache_path_returns_none(self):
        from cli_agent import _get_audit_cache_config

        config = MagicMock()
        with patch("cli_agent.SETTINGS") as mock_settings:
            mock_settings.skills.audit_analyzer = {"in_memory_enabled": True, "in_memory_cache_path": ""}
            result = _get_audit_cache_config(config)
            assert result == (None, None)

    def test_absolute_path_resolved(self):
        from cli_agent import _get_audit_cache_config

        config = MagicMock()
        with patch("cli_agent.SETTINGS") as mock_settings:
            mock_settings.skills.audit_analyzer = {
                "in_memory_enabled": True, "in_memory_cache_path": r"C:\abs\cache.db"
            }
            with patch("skills.audit_analyzer.scripts.skill_config.load_db_config", return_value={"db": "cfg"}):
                cache_path, db_cfg = _get_audit_cache_config(config)
                assert cache_path == r"C:\abs\cache.db"
                assert db_cfg == {"db": "cfg"}

    def test_relative_path_resolved_against_workspace(self):
        from cli_agent import _get_audit_cache_config

        config = MagicMock()
        config.workspace_path = Path(r"C:\ws")
        with patch("cli_agent.SETTINGS") as mock_settings:
            mock_settings.skills.audit_analyzer = {
                "in_memory_enabled": True, "in_memory_cache_path": "rel/cache.db"
            }
            with patch("skills.audit_analyzer.scripts.skill_config.load_db_config", return_value={"db": "cfg"}):
                cache_path, db_cfg = _get_audit_cache_config(config)
                assert "rel\\cache.db" in cache_path or "rel/cache.db" in cache_path
                assert cache_path.startswith(r"C:\ws\skills\audit_analyzer")
                assert db_cfg == {"db": "cfg"}

    def test_exception_returns_none(self):
        from cli_agent import _get_audit_cache_config

        config = MagicMock()
        with patch("cli_agent.SETTINGS") as mock_settings:
            mock_settings.skills.audit_analyzer = MagicMock()
            mock_settings.skills.audit_analyzer.get.side_effect = AttributeError("fail")
            result = _get_audit_cache_config(config)
            assert result == (None, None)


class TestPreloadAuditCache:
    def test_no_cache_config_returns_early(self):
        from cli_agent import _preload_audit_cache

        config = MagicMock()
        with patch("cli_agent._get_audit_cache_config", return_value=(None, None)):
            with patch("lib.services.cache_provider_impl.load_cache_from_postgres") as mock_load:
                _preload_audit_cache(config)
                mock_load.assert_not_called()

    def test_cache_file_fresh_skips_load(self, tmp_path):
        from cli_agent import _preload_audit_cache

        config = MagicMock()
        cache_file = tmp_path / "cache.db"
        cache_file.write_text("data")

        with patch("cli_agent._get_audit_cache_config", return_value=(str(cache_file), {})):
            with patch("lib.services.cache_provider_impl.load_cache_from_postgres") as mock_load:
                _preload_audit_cache(config)
                mock_load.assert_not_called()

    def test_cache_file_stale_loads_from_postgres(self, tmp_path):
        from cli_agent import _preload_audit_cache
        import time

        config = MagicMock()
        cache_file = tmp_path / "cache.db"
        cache_file.write_text("data")

        with patch("cli_agent._get_audit_cache_config", return_value=(str(cache_file), {"host": "local"})):
            with patch.object(Path, "stat") as mock_stat:
                stat_result = MagicMock()
                stat_result.st_mtime = 1000
                mock_stat.return_value = stat_result
                with patch("lib.services.cache_provider_impl.load_cache_from_postgres") as mock_load:
                    _preload_audit_cache(config)
                    mock_load.assert_called_once_with(str(cache_file), {"host": "local"})

    def test_cache_file_missing_loads_from_postgres(self, tmp_path):
        from cli_agent import _preload_audit_cache

        config = MagicMock()
        cache_file = tmp_path / "nonexistent.db"

        with patch("cli_agent._get_audit_cache_config", return_value=(str(cache_file), {"host": "local"})):
            with patch("lib.services.cache_provider_impl.load_cache_from_postgres") as mock_load:
                _preload_audit_cache(config)
                mock_load.assert_called_once_with(str(cache_file), {"host": "local"})


class TestBackgroundAuditCacheRefresh:
    @pytest.mark.asyncio
    async def test_no_cache_config_returns_early(self):
        from cli_agent import _background_audit_cache_refresh

        config = MagicMock()
        with patch("cli_agent._get_audit_cache_config", return_value=(None, None)):
            result = await _background_audit_cache_refresh(config)
            assert result is None

    @pytest.mark.asyncio
    async def test_stale_check_triggers_reload(self):
        from cli_agent import _background_audit_cache_refresh

        config = MagicMock()
        with patch("cli_agent._get_audit_cache_config", return_value=("/cache.db", {"host": "local"})):
            with patch("cli_agent.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                mock_sleep.side_effect = [None, asyncio.CancelledError()]
                with patch("lib.services.cache_provider_impl.check_cache_stale",
                           return_value={"stale_tables": ["audit_log"]}) as mock_stale:
                    with patch("lib.services.cache_provider_impl.load_cache_from_postgres") as mock_load:
                        await _background_audit_cache_refresh(config)

                        mock_stale.assert_called_once_with("/cache.db", {"host": "local"})
                        mock_load.assert_called_once_with("/cache.db", {"host": "local"})

    @pytest.mark.asyncio
    async def test_no_stale_skips_reload(self):
        from cli_agent import _background_audit_cache_refresh

        config = MagicMock()
        with patch("cli_agent._get_audit_cache_config", return_value=("/cache.db", {"host": "local"})):
            with patch("cli_agent.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                mock_sleep.side_effect = [None, asyncio.CancelledError()]
                with patch("lib.services.cache_provider_impl.check_cache_stale",
                           return_value={"stale_tables": []}):
                    with patch("lib.services.cache_provider_impl.load_cache_from_postgres") as mock_load:
                        await _background_audit_cache_refresh(config)

                        mock_load.assert_not_called()
