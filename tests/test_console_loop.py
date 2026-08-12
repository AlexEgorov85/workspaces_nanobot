"""Tests for lib/cli/console_loop helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from lib.cli.display_config import DisplayConfig


class TestPrintReasoningBlock:
    @pytest.mark.asyncio
    async def test_empty_noop(self):
        from lib.cli.console_loop import _print_reasoning_block

        await _print_reasoning_block("  ", DisplayConfig())

    @pytest.mark.asyncio
    async def test_disabled_noop(self):
        from lib.cli.console_loop import _print_reasoning_block

        with patch("lib.cli.console_loop._typewriter", new_callable=AsyncMock) as tw:
            await _print_reasoning_block("text", DisplayConfig(show_reasoning=False))
            tw.assert_not_called()

    @pytest.mark.asyncio
    async def test_enabled_calls_typewriter(self):
        from lib.cli.console_loop import _print_reasoning_block

        with patch("lib.cli.console_loop._typewriter", new_callable=AsyncMock) as tw:
            await _print_reasoning_block("thinking", DisplayConfig())
            tw.assert_called_once()


class TestPrintToolEvents:
    @pytest.mark.asyncio
    async def test_disabled_noop(self):
        from lib.cli.console_loop import _print_tool_events

        with patch("lib.cli.console_loop._typewriter", new_callable=AsyncMock) as tw:
            await _print_tool_events([{"name": "read"}], DisplayConfig(show_tool_calls=False))
            tw.assert_not_called()

    @pytest.mark.asyncio
    async def test_skip_non_dict(self):
        from lib.cli.console_loop import _print_tool_events

        with patch("lib.cli.console_loop._typewriter", new_callable=AsyncMock) as tw:
            await _print_tool_events(["string"], DisplayConfig())
            tw.assert_not_called()

    @pytest.mark.asyncio
    async def test_ok_status(self):
        from lib.cli.console_loop import _print_tool_events

        with patch("lib.cli.console_loop._typewriter", new_callable=AsyncMock) as tw:
            await _print_tool_events(
                [{"name": "read", "status": "ok", "result_preview": "..."}],
                DisplayConfig(),
            )
            tw.assert_called_once()

    @pytest.mark.asyncio
    async def test_error_status(self):
        from lib.cli.console_loop import _print_tool_events

        with patch("lib.cli.console_loop._typewriter", new_callable=AsyncMock) as tw:
            await _print_tool_events(
                [{"name": "write", "status": "error", "error": "denied"}],
                DisplayConfig(),
            )
            tw.assert_called_once()
            assert "denied" in tw.call_args[0][0]

    @pytest.mark.asyncio
    async def test_end_status(self):
        from lib.cli.console_loop import _print_tool_events

        with patch("lib.cli.console_loop._typewriter", new_callable=AsyncMock) as tw:
            await _print_tool_events(
                [{"name": "exec", "status": "end", "result": "ok"}],
                DisplayConfig(),
            )
            tw.assert_called_once()

    @pytest.mark.asyncio
    async def test_args_formatted(self):
        from lib.cli.console_loop import _print_tool_events

        with patch("lib.cli.console_loop._typewriter", new_callable=AsyncMock) as tw:
            await _print_tool_events(
                [{"name": "search", "status": "ok", "arguments": {"q": "hi", "n": 3}}],
                DisplayConfig(show_tool_params=True),
            )
            text = tw.call_args[0][0]
            assert "q=hi" in text
            assert "n=3" in text

    @pytest.mark.asyncio
    async def test_args_hidden_when_disabled(self):
        from lib.cli.console_loop import _print_tool_events

        with patch("lib.cli.console_loop._typewriter", new_callable=AsyncMock) as tw:
            await _print_tool_events(
                [{"name": "search", "status": "ok", "arguments": {"q": "hi"}}],
                DisplayConfig(show_tool_params=False, show_tool_results=False),
            )
            text = tw.call_args[0][0]
            assert "q=" not in text
            assert "→" not in text
