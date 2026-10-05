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
import logging
import uuid
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
    def test_injects_when_turn_has_no_request_id(self):
        """Неочередной канал: вопроса в обороте нет, а вызов отправлять надо.

        Очередь — единственный канал, который кладёт идентификатор вопроса в
        метаданные входящего сообщения; на webUI и в CLI его просто не
        существует, и прежний отказ здесь делал MCP неработающим на этих
        каналах целиком (платформа отвечала ``identity_missing``).

        Досылка не имеет права выглядеть как оборот: значение из журнала не
        берётся, оно и было пустым.
        """
        hook = McpIdentityHook(_service(request_id=None))
        params: dict = {}
        with _with_sender(SENDER_ID):
            asyncio.run(_run(hook, params))
        assert set(params) == set(IDENTITY_KEYS)
        assert params["session_id"] == SESSION_KEY
        assert params["user_id"] == SENDER_ID
        assert params["request_id"] != REQUEST_ID
        assert uuid.UUID(params["request_id"]).version == 4

    def test_generated_request_id_is_unique_per_call(self):
        """Повторное значение связало бы разные вызовы одного оборота.

        Один инстанс хука обслуживает все обороты, поэтому «последний
        досыланный» в его состоянии был бы общим для всего процесса.
        """
        hook = McpIdentityHook(_service(request_id=None))
        first: dict = {}
        second: dict = {}
        with _with_sender(SENDER_ID):
            asyncio.run(_run(hook, first))
            asyncio.run(_run(hook, second))
        assert first["request_id"] and second["request_id"]
        assert first["request_id"] != second["request_id"]

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

    def test_injects_without_logging_service(self):
        """MCP обязан работать и без журнала — ``request_id`` досылается.

        Носителя оборота может не быть вовсе (журнал не подключён, фоновая
        служба вне оборота), и отказ по этой причине снова отправил бы все
        такие вызовы в ``identity_missing``.
        """
        hook = McpIdentityHook(None)
        params: dict = {}
        with _with_sender(SENDER_ID):
            asyncio.run(_run(hook, params))
        assert set(params) == set(IDENTITY_KEYS)
        assert params["session_id"] == SESSION_KEY
        assert params["user_id"] == SENDER_ID
        assert params["request_id"]

    def test_broken_journal_does_not_break_turn(self):
        """Индекс журнала — не граница изоляции: его поломка не роняет оборот.

        Отказ носителя — это отсутствие вопроса, то есть ровно тот случай, в
        котором вопрос по контракту и не бывает: досылка даёт вызову полную
        личность.
        """
        service = SimpleNamespace(
            get_request_id=Mock(side_effect=RuntimeError("pool closed"))
        )
        hook = McpIdentityHook(service)
        params: dict = {}
        with _with_sender(SENDER_ID):
            asyncio.run(_run(hook, params))
        assert set(params) == set(IDENTITY_KEYS)
        assert params["session_id"] == SESSION_KEY
        assert params["user_id"] == SENDER_ID
        assert params["request_id"]

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


class TestGeneratedRequestId:
    """Досылка обязана быть видна и не имеет права попасть в колонку журнала."""

    def test_journal_service_is_only_ever_read(self):
        """Журнал у хука — только читатель, и только через ``get_request_id``.

        Два поля с одним именем — два разных контракта. Любая запись в журнал
        означала бы, что выдуманный идентификатор попал в
        ``agent_gateway_logs.request_id``, где заполняется он исключительно
        существующим оборотом (change 2026-10-04-queue-as-anchor-identity,
        Ф0). То есть «досылаемо в конверт вызова» и «выдумываемо в журнале» —
        разные вещи, и страж держит первую.
        """
        service = Mock()
        service.get_request_id.return_value = None
        hook = McpIdentityHook(service)
        params: dict = {}
        with _with_sender(SENDER_ID):
            asyncio.run(_run(hook, params))
        assert params["request_id"]
        assert [entry[0] for entry in service.method_calls] == ["get_request_id"]

    def test_first_generation_announced_once_per_process(self, caplog):
        """Одна строка ``info`` за процесс, дальше — счётчиком на ``debug``.

        «Вызов уехал с личностью» и «вызов уехал без неё» по журналу
        неразличимы, а различать пришлось бы читателю постфактум, по
        отсутствующим строкам итогов.
        """
        hook = McpIdentityHook(_service(request_id=None))
        with caplog.at_level("INFO"), _with_sender(SENDER_ID):
            asyncio.run(_run(hook, {}))
            after_first = [r for r in caplog.records if r.levelno == logging.INFO]
            asyncio.run(_run(hook, {}))
            after_second = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(after_first) == 1
        assert "request_id" in after_first[0].message
        assert len(after_second) == len(after_first)


class TestWarnings:
    def test_warns_once_per_hook(self, caplog):
        """Иначе каждый вызов оборота добавил бы одинаковую строку в журнал.

        Отказ остался только для вызова ВНЕ оборота: нет ни отправителя, ни
        сессии — подставлять нечего, и внятный отказ лучше выдуманной личности.
        """
        hook = McpIdentityHook(_service())
        params: dict = {}
        with caplog.at_level("WARNING"), _with_sender(None):
            for _ in range(3):
                asyncio.run(_run(hook, params))
        assert sum("McpIdentityHook" in r.message for r in caplog.records) == 1

    def test_generated_path_does_not_warn(self, caplog):
        """Досылка — не авария: предупреждений она не даёт.

        Иначе канал без вопроса (webUI, CLI) писал бы в журнал то же
        предупреждение на каждом вызове, и «вызов отвергнут» нельзя было бы
        отличить от «вызов дослан».
        """
        hook = McpIdentityHook(_service(request_id=None))
        with caplog.at_level("DEBUG"), _with_sender(SENDER_ID):
            for _ in range(3):
                asyncio.run(_run(hook, {}))
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert [r for r in caplog.records if r.levelno == logging.INFO]
