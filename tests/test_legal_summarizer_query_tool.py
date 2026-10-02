"""Страж маршрутизации ``legal_summarizer_query`` — фаза 11, п. 11.3.

Tool тонкий: он не разбирает документ, не ходит в кэш и не держит
LLM-клиента. Всё, что в нём есть, — маршрутизация вопроса в операцию
``query_operation`` capability ``legal_summarizer`` и подстановка личности
оборота. Поэтому проверяются ровно четыре вещи:

  * **куда уходит вызов** — одна операция, аргументы соответствуют её
    подписи, а неизвестное поле отвергается до сетевого вызова;
  * **куда едет личность** — ``session_id``/``user_id``/``request_id`` идут
    в ``identity``, а не в аргументы. Если они попадут в аргументы, граница
    изоляции перестанет быть границей: значение от модели не считается
    личностью (см. ``enterprise_mcp_client.CallIdentity``);
  * **как переводится доменный код** — ``EnterpriseOperationError`` должен
    доехать до модели кодом, а не текстом traceback;
  * **что видно при недоступной платформе** — структурная ошибка, а не
    «неизвестный инструмент».

Механизм мока — фейковый клиент: единственный async-метод ``call``, который
записывает имя операции, аргументы и ``identity`` и возвращает заданный
JSON.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from lib.services.enterprise_mcp_client import (
    EnterpriseMcpUnavailable,
    EnterpriseOperationError,
)
from workspace.tools.legal_summarizer_query import (
    _FIELDS,
    _OPERATION,
    LegalSummarizerQueryTool,
    LegalSummarizerQueryToolConfig,
)

_SESSION_CTX = "nanobot.agent.tools.context.current_request_context"
_SESSION_KEY = "nanobot.agent.tools.context.current_request_session_key"


class _FakeClient:
    """Клиент, который только запоминает вызов."""

    def __init__(self, reply: str = '{"status": "ok", "field": "stats"}') -> None:
        self.reply = reply
        self.calls: list[tuple[str, dict[str, Any], Any]] = []

    async def call(
        self,
        operation: str,
        arguments: dict[str, Any] | None = None,
        *,
        identity: Any = None,
    ) -> str:
        self.calls.append((operation, arguments or {}, identity))
        return self.reply

    @property
    def last(self) -> tuple[str, dict[str, Any], Any]:
        assert self.calls, "операция не вызвана"
        return self.calls[-1]


class _FakeLogging:
    """Источник ``request_id``: метод на экземпляре, не функция модуля."""

    def __init__(self, request_id: str | None = "req-42") -> None:
        self.request_id = request_id
        self.asked: list[str] = []

    def get_request_id(self, session_key: str | None) -> str | None:
        self.asked.append(str(session_key))
        return self.request_id


class _Ctx:
    def __init__(self, sender_id: Any = "u-1") -> None:
        self.sender_id = sender_id


def _tool(
    client: Any = None, *, max_chars: int = 20000
) -> LegalSummarizerQueryTool:
    return LegalSummarizerQueryTool(
        config=LegalSummarizerQueryToolConfig(max_result_chars=max_chars),
        client=_FakeClient() if client is None else client,
        request_id_source=_FakeLogging(),
    )


def _tool_without_client() -> LegalSummarizerQueryTool:
    return LegalSummarizerQueryTool(
        config=LegalSummarizerQueryToolConfig(),
        client=None,
        request_id_source=_FakeLogging(),
    )


def _in_turn(sender_id: Any = "u-1", session_key: str = "s-1") -> Any:
    return patch.multiple(
        "nanobot.agent.tools.context",
        current_request_context=lambda: _Ctx(sender_id),
        current_request_session_key=lambda: session_key,
    )


class TestRouting:
    """Вызов уходит в известную операцию с известными аргументами."""

    @pytest.mark.asyncio
    async def test_goes_to_the_single_capability_operation(self) -> None:
        client = _FakeClient()
        with _in_turn():
            await _tool(client).execute(operation_id="op-1")

        operation, arguments, _ = client.last
        assert operation == _OPERATION == "query_operation"
        assert arguments["operation_id"] == "op-1"
        assert arguments["field"] == "stats"
        assert arguments["max_chunk_summary_chars"] == 1500

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", _FIELDS)
    async def test_every_declared_field_is_forwarded(self, field: str) -> None:
        client = _FakeClient()
        with _in_turn():
            await _tool(client).execute(operation_id="op-1", field=field)

        assert client.last[1]["field"] == field

    @pytest.mark.asyncio
    async def test_unknown_field_is_rejected_before_any_call(self) -> None:
        """Плохое поле не должно стоить сетевого вызова."""
        client = _FakeClient()
        with _in_turn():
            out = await _tool(client).execute(operation_id="op-1", field="nope")

        assert not client.calls, "вызов ушёл при заведомо неверном поле"
        assert json.loads(out)["error_type"] == "invalid_params"


class TestIdentity:
    """Личность едет в identity, а не в аргументы."""

    @pytest.mark.asyncio
    async def test_three_values_go_to_identity(self) -> None:
        client = _FakeClient()
        with _in_turn():
            await _tool(client).execute(operation_id="op-1")

        _, arguments, identity = client.last
        assert identity.session_id == "s-1"
        assert identity.user_id == "u-1"
        assert identity.request_id == "req-42"
        # Граница изоляции: в аргументах личности быть не должно.
        for key in ("session_id", "user_id", "request_id"):
            assert key not in arguments

    @pytest.mark.asyncio
    async def test_outside_a_turn_there_is_no_identity(self) -> None:
        client = _FakeClient()
        with patch(_SESSION_CTX, lambda: None):
            await _tool(client).execute(operation_id="op-1")

        assert client.last[2] is None

    @pytest.mark.asyncio
    async def test_without_sender_there_is_no_identity(self) -> None:
        client = _FakeClient()
        with _in_turn(sender_id=None):
            await _tool(client).execute(operation_id="op-1")

        assert client.last[2] is None

    @pytest.mark.asyncio
    async def test_broken_log_does_not_take_the_call_down(self) -> None:
        class _Broken:
            def get_request_id(self, _key: str | None) -> str | None:
                raise RuntimeError("журнал недоступен")

        tool = LegalSummarizerQueryTool(
            config=LegalSummarizerQueryToolConfig(),
            client=_FakeClient(),
            request_id_source=_Broken(),
        )
        with _in_turn():
            await tool.execute(operation_id="op-1")

        assert tool._client.last[2].request_id is None


class TestErrors:
    """Доменный код доезжает до модели кодом, платформа - структурной ошибкой."""

    @pytest.mark.asyncio
    async def test_domain_code_is_forwarded(self) -> None:
        class _Failing:
            async def call(self, *_a: Any, **_k: Any) -> str:
                raise EnterpriseOperationError(
                    "not_found",
                    "manifest.json для operation_id='op-1' не найден",
                )

        with _in_turn():
            out = await _tool(_Failing()).execute(operation_id="op-1")

        payload = json.loads(out)
        assert payload["status"] == "error"
        assert payload["error_type"] == "not_found"

    @pytest.mark.asyncio
    async def test_unavailable_platform_is_reported(self) -> None:
        class _Failing:
            async def call(self, *_a: Any, **_k: Any) -> str:
                raise EnterpriseMcpUnavailable("процесс не отвечает")

        with _in_turn():
            out = await _tool(_Failing()).execute(operation_id="op-1")

        assert json.loads(out)["error_type"] == "mcp_unavailable"

    @pytest.mark.asyncio
    async def test_without_a_client_the_tool_explains_itself(self) -> None:
        with _in_turn():
            out = await _tool_without_client().execute(operation_id="op-1")

        assert json.loads(out)["error_type"] == "mcp_unavailable"

    @pytest.mark.asyncio
    async def test_model_never_sees_a_traceback(self) -> None:
        class _Failing:
            async def call(self, *_a: Any, **_k: Any) -> str:
                raise RuntimeError("соединение оборвано")

        with _in_turn():
            out = await _tool(_Failing()).execute(operation_id="op-1")

        payload = json.loads(out)
        assert payload["error_type"] == "unexpected_error"
        # Ни одного кадра трейсбека: модель получает конверт, а не стектрейс.
        assert "File \"" not in payload["message"]
        assert "Traceback (most recent call last)" not in payload["message"]


class TestAnswerSize:
    """Потолок ответа держится по-настоящему."""

    @pytest.mark.asyncio
    async def test_short_answer_passes_through(self) -> None:
        reply = '{"status": "ok", "field": "stats"}'
        with _in_turn():
            out = await _tool(_FakeClient(reply)).execute(operation_id="op-1")

        assert out == reply

    @pytest.mark.asyncio
    async def test_long_answer_is_truncated(self) -> None:
        client = _FakeClient("x" * 5000)
        with _in_turn():
            out = await _tool(client, max_chars=1000).execute(
                operation_id="op-1", field="all"
            )

        payload = json.loads(out)
        assert payload["truncated"] is True
        # Потолок держится на содержимом ответа. Конверт с пояснением идёт
        # сверху и в потолок не входит: усечённый ответ всё равно должен
        # сказать модели, что он усечён и почему.
        assert len(payload["preview"]) <= 1000
        assert len(out) < 5000
