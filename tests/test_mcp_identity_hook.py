"""Тесты ``McpIdentityHook`` — подстановки личности в вызовы MCP-операций.

Каждый тест обязан падать при откате своего куска хука. Особенно важна
пара «подставляет» / «перезаписывает чужое значение»: если заменить
присваивание на ``setdefault``, второй тест упадёт, а первый останется
зелёным — и подмена идентичности вернётся незамеченной.

Соответствие имён ключей платформе проверяет
``tests/test_mcp_platform_declaration.py``, а не этот файл: сверять надо с
объявлением в ``mcp-platform``, а не с собственным ожиданием теста.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from lib.hooks.mcp_identity_hook import (
    IDENTITY_KEYS,
    MCP_TOOL_PREFIX,
    McpIdentityHook,
)

SESSION_KEY = "postgres:chat-42"
SENDER_ID = "alice"
REQUEST_ID = "probe-request-1"


def _service(request_id: str | None = REQUEST_ID):
    return SimpleNamespace(get_request_id=lambda key: request_id)


def _context(session_key: str | None = SESSION_KEY):
    return SimpleNamespace(session_key=session_key)


def _tool_call(name: str):
    return SimpleNamespace(name=name)


async def _run(
    hook: McpIdentityHook,
    params,
    *,
    tool_name: str = f"{MCP_TOOL_PREFIX}run_script",
    context=None,
):
    await hook.before_execute_tool(
        _context() if context is None else context,
        _tool_call(tool_name),
        None,
        params,
    )
    return params


def _with_sender(sender_id: str | None):
    """Контекст RequestContext с заданным отправителем (или без него)."""
    return patch(
        "nanobot.agent.tools.context.current_request_context",
        return_value=(
            None if sender_id is None else SimpleNamespace(sender_id=sender_id)
        ),
    )


class TestIdentityInjection:
    def test_injects_full_identity_into_mcp_params(self):
        hook = McpIdentityHook(_service())
        params: dict = {"operation": "run_script"}
        with _with_sender(SENDER_ID):
            asyncio.run(_run(hook, params))
        assert params == {
            "operation": "run_script",
            "session_id": SESSION_KEY,
            "user_id": SENDER_ID,
            "request_id": REQUEST_ID,
        }

    def test_preserves_domain_arguments(self):
        """Аргументы операции хук не трогает: он добавляет, а не переписывает."""
        hook = McpIdentityHook(_service())
        params = {"query": "SELECT 1", "limit": 10}
        with _with_sender(SENDER_ID):
            asyncio.run(_run(hook, params))
        assert params["query"] == "SELECT 1"
        assert params["limit"] == 10

    def test_uses_exactly_the_three_platform_keys(self):
        """Ни лишних, ни переименованных ключей: сервер режет ровно эти."""
        hook = McpIdentityHook(_service())
        params: dict = {}
        with _with_sender(SENDER_ID):
            asyncio.run(_run(hook, params))
        assert set(params) == set(IDENTITY_KEYS)
        assert len(IDENTITY_KEYS) == 3


class TestSpoofing:
    def test_overwrites_model_supplied_identity(self):
        """Значения, присланные моделью, НЕ должны выигрывать.

        В опубликованной схеме операции таких полей нет, поэтому в аргументах
        они могут появиться только снизу. ``setdefault`` здесь превратил бы
        вызов в средство выдать себя за другого пользователя.
        """
        hook = McpIdentityHook(_service())
        params = {
            "session_id": "postgres:чужой-чат",
            "user_id": "mallory",
            "request_id": "чужой-оборот",
        }
        with _with_sender(SENDER_ID):
            asyncio.run(_run(hook, params))
        assert params["session_id"] == SESSION_KEY
        assert params["user_id"] == SENDER_ID
        assert params["request_id"] == REQUEST_ID

    def test_overwrites_partial_spoof(self):
        """Подставленное частично тоже перебивается: смешивать нельзя."""
        hook = McpIdentityHook(_service())
        params = {"user_id": "mallory"}
        with _with_sender(SENDER_ID):
            asyncio.run(_run(hook, params))
        assert params["user_id"] == SENDER_ID
        assert set(params) == set(IDENTITY_KEYS)


class TestScope:
    def test_ignores_non_mcp_tool(self):
        hook = McpIdentityHook(_service())
        params: dict = {"path": "workspace/report.md"}
        with _with_sender(SENDER_ID):
            asyncio.run(_run(hook, params, tool_name="read_file"))
        assert params == {"path": "workspace/report.md"}

    def test_ignores_another_mcp_server(self):
        """Чужой MCP-сервер не обязан знать про LEGACY_IDENTITY_KEYS."""
        hook = McpIdentityHook(_service())
        params: dict = {}
        with _with_sender(SENDER_ID):
            asyncio.run(_run(hook, params, tool_name="mcp_other_run_script"))
        assert params == {}

    def test_accepts_custom_prefix(self):
        hook = McpIdentityHook(_service(), tool_prefix="mcp_audit_")
        params: dict = {}
        with _with_sender(SENDER_ID):
            asyncio.run(_run(hook, params, tool_name="mcp_audit_run_script"))
        assert set(params) == set(IDENTITY_KEYS)

    def test_survives_non_dict_params(self):
        """Хук не имеет права ронять оборот из-за формы аргументов."""
        hook = McpIdentityHook(_service())
        with _with_sender(SENDER_ID):
            asyncio.run(_run(hook, None))
            asyncio.run(_run(hook, ["не", "словарь"]))

    def test_falls_back_to_tool_object_name(self):
        """Раннер может не передать tool_call — имя есть и у самого tool'а."""
        hook = McpIdentityHook(_service())
        params: dict = {}
        with _with_sender(SENDER_ID):
            asyncio.run(
                hook.before_execute_tool(
                    _context(),
                    SimpleNamespace(name=""),
                    SimpleNamespace(name=f"{MCP_TOOL_PREFIX}vector_search"),
                    params,
                )
            )
        assert set(params) == set(IDENTITY_KEYS)


class TestIncompleteIdentity:
    def test_no_injection_without_request_id(self):
        """Нет оборота — нет связи с agent_question_runs, и подставлять нечего.

        Вызов уйдёт без личности и будет отвергнут платформой с
        ``identity_missing``: это внятнее выдуманной сессии.
        """
        hook = McpIdentityHook(_service(request_id=None))
        params: dict = {}
        with _with_sender(SENDER_ID):
            asyncio.run(_run(hook, params))
        assert params == {}

    def test_no_injection_without_sender(self):
        hook = McpIdentityHook(_service())
        params: dict = {}
        with _with_sender(None):
            asyncio.run(_run(hook, params))
        assert params == {}

    def test_no_injection_without_session_key(self):
        hook = McpIdentityHook(_service())
        params: dict = {}
        with _with_sender(SENDER_ID):
            asyncio.run(_run(hook, params, context=_context(session_key=None)))
        assert params == {}

    def test_no_injection_without_logging_service(self):
        """Без журнала request_id взять неоткуда — вызов без личности."""
        hook = McpIdentityHook(None)
        params: dict = {}
        with _with_sender(SENDER_ID):
            asyncio.run(_run(hook, params))
        assert params == {}

    def test_broken_journal_does_not_break_turn(self):
        """Индекс журнала — не граница изоляции: его поломка не роняет оборот."""
        service = SimpleNamespace(
            get_request_id=Mock(side_effect=RuntimeError("pool closed"))
        )
        hook = McpIdentityHook(service)
        params: dict = {}
        with _with_sender(SENDER_ID):
            asyncio.run(_run(hook, params))
        assert params == {}

    def test_session_key_from_contextvar_when_context_is_empty(self):
        """Запасной путь: вне итерации у хука контекст без session_key."""
        hook = McpIdentityHook(_service())
        params: dict = {}
        with (
            _with_sender(SENDER_ID),
            patch(
                "nanobot.agent.tools.context.current_request_session_key",
                return_value="postgres:из-контекста",
            ),
        ):
            asyncio.run(_run(hook, params, context=SimpleNamespace()))
        assert params["session_id"] == "postgres:из-контекста"


class TestWarnings:
    def test_warns_once_per_hook(self, caplog):
        """Иначе каждый вызов оборота добавил бы одинаковую строку в журнал."""
        hook = McpIdentityHook(_service(request_id=None))
        params: dict = {}
        with caplog.at_level("WARNING"), _with_sender(SENDER_ID):
            for _ in range(3):
                asyncio.run(_run(hook, params))
        assert sum("McpIdentityHook" in r.message for r in caplog.records) == 1
