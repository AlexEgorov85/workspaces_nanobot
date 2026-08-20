"""Tests for lib/cli/console_loop helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from lib.cli.display_config import DisplayConfig


class TestPrintReasoningBlock:
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


class TestPrintContextWindow:
    """``_print_context_window`` — метрика M1 в CLI."""

    @pytest.mark.asyncio
    async def test_disabled_noop(self):
        from lib.cli.console_loop import _print_context_window

        with patch("lib.cli.console_loop.console") as console:
            await _print_context_window(
                {"used": 1, "limit": 10, "pct": 0.1, "model": "x"},
                DisplayConfig(show_context_window=False),
            )
            console.print.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_block_noop(self):
        from lib.cli.console_loop import _print_context_window

        with patch("lib.cli.console_loop.console") as console:
            await _print_context_window(None, DisplayConfig())
            await _print_context_window({}, DisplayConfig())
            await _print_context_window(
                {"used": 1, "limit": 0, "pct": 0.1, "model": "x"}, DisplayConfig(),
            )
            await _print_context_window(
                {"used": -1, "limit": 10, "pct": 0.1, "model": "x"}, DisplayConfig(),
            )
            console.print.assert_not_called()

    @pytest.mark.asyncio
    async def test_renders_label_with_used_limit_pct_model(self):
        from lib.cli.console_loop import _print_context_window

        with patch("lib.cli.console_loop.console") as console:
            await _print_context_window(
                {"used": 12345, "limit": 65536, "pct": 0.1883, "model": "MiniMax-M3"},
                DisplayConfig(),
            )
            console.print.assert_called_once()
            text = console.print.call_args.args[0]
            assert "12345" in text
            assert "65536" in text
            assert "19%" in text
            assert "MiniMax-M3" in text

    @pytest.mark.asyncio
    async def test_clamps_pct(self):
        from lib.cli.console_loop import _print_context_window

        with patch("lib.cli.console_loop.console") as console:
            await _print_context_window(
                {"used": 999, "limit": 10, "pct": 1.5, "model": "x"},
                DisplayConfig(),
            )
            text = console.print.call_args.args[0]
            assert "100%" in text

    @pytest.mark.asyncio
    async def test_no_model_omits_model_suffix(self):
        from lib.cli.console_loop import _print_context_window

        with patch("lib.cli.console_loop.console") as console:
            await _print_context_window(
                {"used": 1, "limit": 10, "pct": 0.1, "model": ""},
                DisplayConfig(),
            )
            text = console.print.call_args.args[0]
            assert "10%" in text
            # Без модели — только один разделитель `·` (limit→pct).
            assert text.count("·") == 1
