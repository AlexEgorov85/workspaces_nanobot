from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


@pytest.fixture(autouse=True)
def mock_nanobot_agent():
    """Mock nanobot.agent before importing terminal_tool_print_hook."""
    with patch.dict("sys.modules"):
        nanobot = types.ModuleType("nanobot")
        nanobot.agent = types.ModuleType("nanobot.agent")
        nanobot.agent.AgentHook = type(
            "AgentHook", (), {"__init__": lambda self: None}
        )
        nanobot.agent.AgentHookContext = MagicMock()
        sys.modules["nanobot"] = nanobot
        sys.modules["nanobot.agent"] = nanobot.agent

        from lib.hooks.terminal_tool_print_hook import TerminalToolPrintHook

        yield {"TerminalToolPrintHook": TerminalToolPrintHook}


def _make_tc(name: str, arguments: dict, call_id: str = "c1") -> object:
    tc = types.SimpleNamespace()
    tc.id = call_id
    tc.name = name
    tc.arguments = arguments
    return tc


def _make_ctx(calls, events, session_key: str = "s1", results=None) -> object:
    return types.SimpleNamespace(
        session_key=session_key,
        tool_calls=calls,
        tool_events=events,
        tool_results=list(results or []),
        iteration=1,
    )


class _LogSink:
    """Простой sink для loguru: собирает записи в список для assert'ов."""

    def __init__(self) -> None:
        self.records: list[dict] = []

    def write(self, msg: str) -> None:
        payload = json.loads(msg)
        rec = payload.get("record", payload)
        self.records.append({
            "level": rec.get("level", {}).get("name", ""),
            "message": rec.get("message", ""),
            "extra": rec.get("extra", {}),
        })


@contextlib.contextmanager
def _patched_loguru(sink: _LogSink):
    from loguru import logger

    handler_id = logger.add(
        sink.write, level="DEBUG", serialize=True,
        format="{level.no}|{extra[channel]}|{message}",
    )
    try:
        yield
    finally:
        logger.remove(handler_id)


class TestTerminalToolPrintHook:
    """Хук живого вывода tool-вызовов: ошибки подробно, успехи кратко."""

    def test_success_prints_result_preview(self, mock_nanobot_agent):
        TerminalToolPrintHook = mock_nanobot_agent["TerminalToolPrintHook"]
        sink = _LogSink()
        hook = TerminalToolPrintHook()
        calls = [_make_tc("read_file", {"path": "foo.txt"})]
        events = [{"status": "ok", "detail": "hello"}]
        results = ["file contents here"]

        async def run():
            await hook.before_execute_tools(_make_ctx(calls, []))
            await hook.after_iteration(
                _make_ctx(calls, events, results=results)
            )

        with _patched_loguru(sink):
            asyncio.run(run())

        assert len(sink.records) == 1
        rec = sink.records[0]
        assert rec["level"] == "INFO"
        assert "✓" in rec["message"]
        assert "read_file" in rec["message"]
        # Результат превьюится (это новая инфа, иначе теряется в терминале)
        assert "file contents here" in rec["message"]
        assert "ms" in rec["message"]
        # Аргументы НЕ дублируем — их уже печатает ProgressHook
        assert "foo.txt" not in rec["message"]

    def test_success_without_result_omits_preview(self, mock_nanobot_agent):
        TerminalToolPrintHook = mock_nanobot_agent["TerminalToolPrintHook"]
        sink = _LogSink()
        hook = TerminalToolPrintHook()
        calls = [_make_tc("ping", {})]
        events = [{"status": "ok", "detail": ""}]
        results = [None]

        async def run():
            await hook.before_execute_tools(_make_ctx(calls, []))
            await hook.after_iteration(
                _make_ctx(calls, events, results=results)
            )

        with _patched_loguru(sink):
            asyncio.run(run())

        rec = sink.records[0]
        assert "✓" in rec["message"]
        assert "ping" in rec["message"]
        assert "ms" in rec["message"]
        # Нет превью — нет и стрелки
        assert "→" not in rec["message"]

    def test_multiline_result_collapsed_to_one_line(self, mock_nanobot_agent):
        TerminalToolPrintHook = mock_nanobot_agent["TerminalToolPrintHook"]
        sink = _LogSink()
        hook = TerminalToolPrintHook()
        calls = [_make_tc("exec", {})]
        events = [{"status": "ok", "detail": ""}]
        results = ["line1\nline2\n  line3   with   spaces"]

        async def run():
            await hook.before_execute_tools(_make_ctx(calls, []))
            await hook.after_iteration(
                _make_ctx(calls, events, results=results)
            )

        with _patched_loguru(sink):
            asyncio.run(run())

        msg = sink.records[0]["message"]
        assert "line1 line2 line3 with spaces" in msg
        # Без переносов строк в самом сообщении лога
        assert "\n" not in msg.split("→", 1)[1].split("(ms)")[0]

    def test_long_result_truncated(self, mock_nanobot_agent):
        from lib.hooks.terminal_tool_print_hook import _MAX_RESULT_CHARS

        TerminalToolPrintHook = mock_nanobot_agent["TerminalToolPrintHook"]
        sink = _LogSink()
        hook = TerminalToolPrintHook()
        calls = [_make_tc("exec", {})]
        events = [{"status": "ok", "detail": ""}]
        results = ["x" * (_MAX_RESULT_CHARS + 200)]

        async def run():
            await hook.before_execute_tools(_make_ctx(calls, []))
            await hook.after_iteration(
                _make_ctx(calls, events, results=results)
            )

        with _patched_loguru(sink):
            asyncio.run(run())

        msg = sink.records[0]["message"]
        # Превью обрезано (иначе длинный вывод ломает строку терминала)
        assert "…" in msg
        assert len(msg) < _MAX_RESULT_CHARS + 100

    def test_error_prints_verbose(self, mock_nanobot_agent):
        TerminalToolPrintHook = mock_nanobot_agent["TerminalToolPrintHook"]
        sink = _LogSink()
        hook = TerminalToolPrintHook()
        calls = [_make_tc("exec", {"cmd": "rm -rf /"})]
        events = [{"status": "error", "detail": "permission denied"}]

        async def run():
            await hook.before_execute_tools(_make_ctx(calls, []))
            await hook.after_iteration(_make_ctx(calls, events))

        with _patched_loguru(sink):
            asyncio.run(run())

        assert len(sink.records) == 1
        rec = sink.records[0]
        assert rec["level"] == "ERROR"
        assert "✗" in rec["message"]
        assert "exec" in rec["message"]
        assert "rm -rf /" in rec["message"]
        assert "permission denied" in rec["message"]

    def test_truncates_long_args_and_errors(self, mock_nanobot_agent):
        from lib.hooks.terminal_tool_print_hook import (
            TerminalToolPrintHook,
            _MAX_ARGS_CHARS,
            _MAX_ERROR_CHARS,
        )
        sink = _LogSink()
        hook = TerminalToolPrintHook()
        long_arg = "x" * (_MAX_ARGS_CHARS + 100)
        long_err = "y" * (_MAX_ERROR_CHARS + 100)
        calls = [_make_tc("write_file", {"path": long_arg})]
        events = [{"status": "error", "detail": long_err}]

        async def run():
            await hook.before_execute_tools(_make_ctx(calls, []))
            await hook.after_iteration(_make_ctx(calls, events))

        with _patched_loguru(sink):
            asyncio.run(run())

        msg = sink.records[0]["message"]
        # Длинные данные усечены — итоговое сообщение короче суммы длин.
        assert len(msg) < len(long_arg) + len(long_err) + 100

    def test_channel_is_tools(self, mock_nanobot_agent):
        """Канал ``tools``, не дефолтный ``app``/имя-модуля."""
        TerminalToolPrintHook = mock_nanobot_agent["TerminalToolPrintHook"]
        sink = _LogSink()
        hook = TerminalToolPrintHook()
        calls = [_make_tc("list_dir", {})]
        events = [{"status": "ok", "detail": ""}]

        async def run():
            await hook.before_execute_tools(_make_ctx(calls, []))
            await hook.after_iteration(_make_ctx(calls, events))

        with _patched_loguru(sink):
            asyncio.run(run())

        assert sink.records[0]["extra"].get("channel") == "tools"

    def test_per_session_isolation(self, mock_nanobot_agent):
        """Старт-тайминги двух сессий изолированы (конкурентные обороты)."""
        TerminalToolPrintHook = mock_nanobot_agent["TerminalToolPrintHook"]
        sink = _LogSink()
        hook = TerminalToolPrintHook()

        ctx_a = _make_ctx(
            [_make_tc("a", {})], [{"status": "ok", "detail": ""}],
            session_key="sA", results=["ok-a"],
        )
        ctx_b = _make_ctx(
            [_make_tc("b", {})], [{"status": "ok", "detail": ""}],
            session_key="sB", results=["ok-b"],
        )

        async def run():
            await hook.before_execute_tools(ctx_a)
            await hook.before_execute_tools(ctx_b)
            await hook.after_iteration(ctx_b)
            await hook.after_iteration(ctx_a)

        with _patched_loguru(sink):
            asyncio.run(run())

        names = [r["message"] for r in sink.records]
        assert any(" a " in n or n.endswith(" a ") for n in names)
        assert any(" b " in n or n.endswith(" b ") for n in names)
        assert len(sink.records) == 2

    def test_no_args_success_still_prints(self, mock_nanobot_agent):
        """Успех без аргументов — печатается ``✓ name → preview (NNms)``."""
        TerminalToolPrintHook = mock_nanobot_agent["TerminalToolPrintHook"]
        sink = _LogSink()
        hook = TerminalToolPrintHook()
        calls = [_make_tc("ping", {})]
        events = [{"status": "ok", "detail": ""}]
        results = ["pong"]

        async def run():
            await hook.before_execute_tools(_make_ctx(calls, []))
            await hook.after_iteration(
                _make_ctx(calls, events, results=results)
            )

        with _patched_loguru(sink):
            asyncio.run(run())

        rec = sink.records[0]
        assert "✓" in rec["message"]
        assert "ping" in rec["message"]
        assert "pong" in rec["message"]
        assert "ms" in rec["message"]
