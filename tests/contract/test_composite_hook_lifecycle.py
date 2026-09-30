"""Интеграционный smoke: ``CompositeHook`` со всеми кастомными хуками
проходит полный lifecycle с РЕАЛЬНЫМИ типами nanobot.

Что покрываем:
  1. ``before_run`` / ``after_run`` / ``on_error`` / ``on_finally`` —
     методы, которые nanobot 0.3.5+ раскручивает на custom-subclasses,
     но bare-классы падают с ``AttributeError``.
  2. ``before_iteration`` / ``after_iteration`` с ``AgentHookContext``,
     у которого ``usage`` — реальный ``LLMUsage`` (frozen dataclass из
     nanobot 0.3.5), а не dict.
  3. ``finalize_content`` — pipeline (нет изоляции, ошибка всплывает).
  4. Tool-события с реальным ``ToolCallRequest``.
  5. ``wants_streaming`` — корректно наследуется как ``False``.

Этот файл СПЕЦИАЛЬНО использует настоящие типы nanobot без ``MagicMock``,
потому что mock-тесты не ловят изменения типа во внешних зависимостях
(вроде ``LLMUsage`` dict→dataclass в 0.3.5).
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.contract

from nanobot.agent import (
    AgentHookContext,
    AgentRunHookContext,
)
from nanobot.agent.hook import CompositeHook
from nanobot.providers.base import LLMUsage, ToolCallRequest


def _build_composite(service=None) -> tuple[CompositeHook, dict[str, object]]:
    """CompositeHook из всех трёх фреймворковых хуков."""
    from lib.hooks.database_logging_hook import DatabaseLoggingHook
    from lib.hooks.terminal_tool_print_hook import TerminalToolPrintHook
    from lib.hooks.tool_audit_hook import ToolAuditHook

    if service is None:
        service = MagicMock()
        service.get_request_id.return_value = "req-1"
    db = DatabaseLoggingHook(service, session_key="s1", request_id="req-1")
    audit = ToolAuditHook()
    term = TerminalToolPrintHook()
    comp = CompositeHook([db, audit, term])
    return comp, {"db": db, "audit": audit, "term": term}


def _make_run_ctx(
    *,
    messages: list[dict] | None = None,
    usage: object = None,
    final_content: str = "ok",
    error: str | None = None,
) -> AgentRunHookContext:
    return AgentRunHookContext(
        messages=messages or [],
        final_content=final_content,
        tools_used=["read_file"],
        usage=usage,
        stop_reason="stop",
        error=error,
        tool_events=[],
        had_injections=False,
    )


def _make_iteration_ctx(
    *,
    messages: list[dict] | None = None,
    usage: object = None,
    iteration: int = 0,
    response: object = None,
) -> AgentHookContext:
    if response is None:
        from nanobot.providers.base import LLMResponse

        response = LLMResponse(
            content="ok",
            tool_calls=[],
            finish_reason="stop",
            usage=None,
        )
    return AgentHookContext(
        iteration=iteration,
        messages=messages or [{"role": "user", "content": "hi"}],
        usage=usage,
        response=response,
    )


class TestCompositeLifecycleWithRealNanobot:
    """Каждый public-lifecycle метод CompositeHook должен работать
    с нашими тремя хуками без AttributeError."""

    def test_before_run(self):
        comp, _ = _build_composite()
        ctx = _make_run_ctx()
        asyncio.run(comp.before_run(ctx))

    def test_after_run_no_error(self):
        service = MagicMock()
        service.get_request_id.return_value = "r"
        comp, _ = _build_composite(service)
        ctx = _make_run_ctx(final_content="ответ")
        asyncio.run(comp.after_run(ctx))
        # log_event был вызван ровно для db-hook
        assert service.log_event.called

    def test_after_run_with_error(self):
        comp, _ = _build_composite()
        ctx = _make_run_ctx(error="boom")
        asyncio.run(comp.after_run(ctx))

    def test_on_error_and_on_finally(self):
        comp, _ = _build_composite()
        ctx = _make_run_ctx(error="boom")
        asyncio.run(comp.on_error(ctx))
        asyncio.run(comp.on_finally(ctx))

    def test_before_iteration_with_real_llm_usage(self):
        comp, _ = _build_composite()
        usage = LLMUsage.reported(
            input_tokens=200,
            output_tokens=10,
            total_tokens=210,
            cache_read_tokens=180,
        )
        ctx = _make_iteration_ctx(usage=usage)
        asyncio.run(comp.before_iteration(ctx))

    def test_after_iteration_with_real_llm_usage(self):
        service = MagicMock()
        comp, _ = _build_composite(service)
        usage = LLMUsage.reported(
            input_tokens=200,
            output_tokens=10,
            total_tokens=210,
        )
        ctx = _make_iteration_ctx(usage=usage)
        asyncio.run(comp.after_iteration(ctx))
        # db-hook пишет llm_call через log_llm_call
        assert service.log_llm_call.called, (
            "log_llm_call не вызван — после fix'а _usage_to_dict должно "
            "корректно нормализовать LLMUsage в dict"
        )
        kwargs = service.log_llm_call.call_args.kwargs
        assert isinstance(kwargs["usage"], dict)
        assert kwargs["usage"]["prompt_tokens"] == 200
        assert kwargs["usage"]["completion_tokens"] == 10

    def test_finalize_content_passes_through(self):
        """finalize_content — pipeline, ошибка НЕ изолируется."""
        comp, _ = _build_composite()
        ctx = _make_iteration_ctx()
        assert comp.finalize_content(ctx, "hello") == "hello"

    def test_tool_events_with_real_tool_call_request(self):
        from lib.hooks.database_logging_hook import DatabaseLoggingHook
        from lib.hooks.tool_audit_hook import ToolAuditHook
        from lib.hooks.terminal_tool_print_hook import TerminalToolPrintHook

        service = MagicMock()
        db = DatabaseLoggingHook(service, session_key="s1", request_id="r")
        audit = ToolAuditHook()
        term = TerminalToolPrintHook()
        comp = CompositeHook([db, audit, term])

        tc = ToolCallRequest(id="tc-1", name="read_file", arguments={"path": "/tmp/x"})
        ctx = AgentHookContext(
            iteration=0,
            messages=[{"role": "user", "content": "read /tmp/x"}],
            tool_calls=[tc],
        )
        tool = MagicMock()
        params = {"path": "/tmp/x"}
        asyncio.run(comp.before_execute_tool(ctx, tc, tool, params))
        asyncio.run(comp.after_execute_tool(ctx, tc, tool, params, "ok"))
        assert service.log_tool_call.called
        assert service.log_tool_result.called

    def test_wants_streaming_inherited_false(self):
        """Все наши хуки не должны менять поведение streaming."""
        comp, hooks = _build_composite()
        assert comp.wants_streaming() is False
        assert hooks["db"].wants_streaming() is False
        assert hooks["audit"].wants_streaming() is False
        assert hooks["term"].wants_streaming() is False
