"""Страж маршрутизации ``audit_analyzer_query`` — фаза 9.

Tool тонкий: он не строит SQL, не открывает снимок и не держит LLM-клиента.
Всё, что в нём есть, — маршрутизация выбора модели в операцию capability
``audit``/``vectors`` и подстановка личности оборота. Поэтому проверяются ровно
две вещи:

  * **куда уходит вызов** — имя операции совпадает с ``operation`` модели, а
    аргументы соответствуют подписи операции на платформе;
  * **куда НЕ уходит личность** — ``session_id``/``user_id``/``request_id`` едут
    в ``identity`` (то есть в ``params._meta``), а не в аргументы. Если они
    попадут в аргументы, граница изоляции перестанет быть границей: значение
    от модели не считается личностью (см. ``enterprise_mcp_client.CallIdentity``).

Механизм мока — фейковый клиент: единственный async-метод ``call``, который
записывает имя операции, аргументы и ``identity`` и возвращает заданный JSON.
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
from workspace.tools.audit_analyzer_query import (
    _ROUTED_OPERATIONS,
    AuditAnalyzerQueryTool,
    AuditAnalyzerQueryToolConfig,
)

_SESSION_CTX = "nanobot.agent.tools.context.current_request_context"
_SESSION_KEY = "nanobot.agent.tools.context.current_request_session_key"


class _FakeClient:
    """Клиент, который только запоминает вызов."""

    def __init__(self, reply: str = '{"status": "ok", "row_count": 0, "rows": []}') -> None:
        self.reply = reply
        self.calls: list[tuple[str, dict[str, Any], Any]] = []

    async def call(self, operation: str, arguments: dict[str, Any] | None = None,
                   *, identity: Any = None) -> str:
        self.calls.append((operation, arguments or {}, identity))
        return self.reply

    @property
    def last(self) -> tuple[str, dict[str, Any], Any]:
        assert self.calls, "операция не вызвана"
        return self.calls[-1]


def _tool(client: Any = None, *, max_chars: int = 20000) -> AuditAnalyzerQueryTool:
    return AuditAnalyzerQueryTool(
        config=AuditAnalyzerQueryToolConfig(max_result_chars=max_chars),
        client=client if client is not None else _FakeClient(),
        request_id_source=_FakeLogging(),
    )


class _FakeLogging:
    """Источник ``request_id``: метод на экземпляре, не функция модуля."""

    def __init__(self, request_id: str | None = "req-42") -> None:
        self.request_id = request_id
        self.asked: list[str] = []

    def get_request_id(self, session_key: str | None) -> str | None:
        self.asked.append(str(session_key))
        return self.request_id


class _RequestCtx:
    sender_id = "user-7"


def _with_identity(fn):
    """Подставить RequestContext: личность обязана собираться, а не выдумываться."""
    return (
        patch(_SESSION_CTX, return_value=_RequestCtx()),
        patch(_SESSION_KEY, return_value="sess-9"),
    )


class TestRouting:
    """Имя операции модели едет на сервер как есть."""

    @pytest.mark.asyncio
    async def test_operation_name_is_passed_through(self) -> None:
        for operation in _ROUTED_OPERATIONS:
            client = _FakeClient()
            tool = _tool(client)
            ctx_patch, key_patch = _with_identity(None)
            with ctx_patch, key_patch:
                await tool.execute(operation=operation, query="проверка", script="s")
            assert client.last[0] == operation, (
                f"{operation} ушёл как {client.last[0]!r} — модель не может "
                "выбрать чужую операцию"
            )

    @pytest.mark.asyncio
    async def test_run_script_passes_script_and_params(self) -> None:
        client = _FakeClient()
        tool = _tool(client)
        ctx_patch, key_patch = _with_identity(None)
        with ctx_patch, key_patch:
            await tool.execute(
                operation="run_script",
                script="analytics_by_year_month",
                params={"year": 2024},
            )
        _, arguments, _ = client.last
        assert arguments == {"script": "analytics_by_year_month", "params": {"year": 2024}}

    @pytest.mark.asyncio
    async def test_generate_sql_passes_only_query(self) -> None:
        client = _FakeClient()
        tool = _tool(client)
        ctx_patch, key_patch = _with_identity(None)
        with ctx_patch, key_patch:
            await tool.execute(operation="generate_sql", query="сколько аудитов?",
                               script="лишний")
        _, arguments, _ = client.last
        # ``script`` от модели в generate_sql не едет: операция его не знает,
        # а молчаливый лишний ключ выглядел бы как «всё прошло».
        assert arguments == {"query": "сколько аудитов?"}

    @pytest.mark.asyncio
    async def test_list_scripts_has_no_arguments(self) -> None:
        client = _FakeClient()
        tool = _tool(client)
        ctx_patch, key_patch = _with_identity(None)
        with ctx_patch, key_patch:
            await tool.execute(operation="list_scripts", query="лишний")
        assert client.last[1] == {}

    @pytest.mark.asyncio
    async def test_vector_search_forwards_k_and_threshold(self) -> None:
        client = _FakeClient()
        tool = _tool(client)
        ctx_patch, key_patch = _with_identity(None)
        with ctx_patch, key_patch:
            await tool.execute(operation="vector_search", query="пожарная безопасность",
                               index_name="audits_index", top_k=3, threshold=0.5)
        _, arguments, _ = client.last
        assert arguments == {
            "query": "пожарная безопасность",
            "index_name": "audits_index",
            "top_k": 3,
            "threshold": 0.5,
        }

    @pytest.mark.asyncio
    async def test_vector_search_without_threshold_omits_key(self) -> None:
        client = _FakeClient()
        tool = _tool(client)
        ctx_patch, key_patch = _with_identity(None)
        with ctx_patch, key_patch:
            await tool.execute(operation="vector_search", query="q")
        _, arguments, _ = client.last
        assert "threshold" not in arguments, (
            "порог не задан — ключ не должен ехать как null"
        )


class TestIdentityBoundary:
    """Личность уходит в ``identity``, а не в аргументы операции."""

    @pytest.mark.asyncio
    async def test_identity_is_passed_outside_arguments(self) -> None:
        client = _FakeClient()
        logging = _FakeLogging("req-42")
        tool = AuditAnalyzerQueryTool(
            config=AuditAnalyzerQueryToolConfig(), client=client,
            request_id_source=logging,
        )
        ctx_patch, key_patch = _with_identity(None)
        with ctx_patch, key_patch:
            await tool.execute(operation="list_scripts")
        _, arguments, identity = client.last
        assert identity is not None, "личность обязана собираться из RequestContext"
        assert identity.session_id == "sess-9"
        assert identity.user_id == "user-7"
        assert identity.request_id == "req-42"
        assert logging.asked == ["sess-9"]
        for key in ("session_id", "user_id", "request_id"):
            assert key not in arguments, (
                f"{key} попал в аргументы операции — значение от модели не "
                "является личностью, изоляция сломана"
            )

    @pytest.mark.asyncio
    async def test_missing_request_context_yields_no_identity(self) -> None:
        """Вне оборота identity=None: сервер ответит identity_missing сам."""
        client = _FakeClient()
        tool = _tool(client)
        with patch(_SESSION_CTX, return_value=None):
            await tool.execute(operation="list_scripts")
        _, _, identity = client.last
        assert identity is None

    @pytest.mark.asyncio
    async def test_missing_sender_id_refuses_to_invent_user(self) -> None:
        """Нет ``sender_id`` — выдумывать ``user_id`` нельзя."""
        client = _FakeClient()
        tool = _tool(client)

        class _NoSender:
            sender_id = None

        with patch(_SESSION_CTX, return_value=_NoSender()), \
                patch(_SESSION_KEY, return_value="sess-9"):
            await tool.execute(operation="list_scripts")
        _, _, identity = client.last
        assert identity is None

    @pytest.mark.asyncio
    async def test_logging_failure_does_not_break_the_call(self) -> None:
        """``request_id`` — удобство корреляции, а не предпосылка вызова."""

        class _Broken:
            def get_request_id(self, _key: str) -> str | None:
                raise RuntimeError("журнал недоступен")

        client = _FakeClient()
        tool = AuditAnalyzerQueryTool(
            config=AuditAnalyzerQueryToolConfig(), client=client,
            request_id_source=_Broken(),
        )
        ctx_patch, key_patch = _with_identity(None)
        with ctx_patch, key_patch:
            await tool.execute(operation="list_scripts")
        _, _, identity = client.last
        assert identity is not None and identity.request_id is None


class TestRejections:
    """Отказ до сетевого вызова вместо догадки."""

    @pytest.mark.asyncio
    async def test_unknown_operation_never_reaches_server(self) -> None:
        client = _FakeClient()
        tool = _tool(client)
        out = json.loads(await tool.execute(operation="drop_table"))
        assert out["error_type"] == "invalid_operation"
        assert not client.calls, "неизвестная операция не должна доходить до сервера"

    @pytest.mark.asyncio
    async def test_run_script_requires_script_name(self) -> None:
        client = _FakeClient()
        tool = _tool(client)
        out = json.loads(await tool.execute(operation="run_script"))
        assert out["error_type"] == "invalid_params"
        assert not client.calls

    @pytest.mark.asyncio
    async def test_generate_sql_requires_query(self) -> None:
        client = _FakeClient()
        tool = _tool(client)
        out = json.loads(await tool.execute(operation="generate_sql"))
        assert out["error_type"] == "invalid_params"
        assert not client.calls

    @pytest.mark.asyncio
    async def test_vector_search_requires_query(self) -> None:
        client = _FakeClient()
        tool = _tool(client)
        out = json.loads(await tool.execute(operation="vector_search"))
        assert out["error_type"] == "invalid_params"
        assert not client.calls

    @pytest.mark.asyncio
    async def test_missing_client_reports_cause(self) -> None:
        tool = AuditAnalyzerQueryTool(config=AuditAnalyzerQueryToolConfig(), client=None)
        out = json.loads(await tool.execute(operation="list_scripts"))
        assert out["error_type"] == "mcp_unavailable"
        assert "enterprise_mcp" in out["message"]


class TestErrorEnvelope:
    """Доменная ошибка операции доезжает до модели кодом, а не traceback'ом."""

    @pytest.mark.asyncio
    async def test_operation_error_code_is_preserved(self) -> None:
        class _Failing:
            async def call(self, *_a: Any, **_k: Any) -> str:
                raise EnterpriseOperationError("not_found", "Скрипт не найден")

        tool = _tool(_Failing())
        out = json.loads(await tool.execute(operation="run_script", script="нет"))
        assert out["error_type"] == "not_found"
        assert "не найден" in out["message"]

    @pytest.mark.asyncio
    async def test_unavailable_server_is_reported_as_such(self) -> None:
        class _Down:
            async def call(self, *_a: Any, **_k: Any) -> str:
                raise EnterpriseMcpUnavailable("процесс не отвечает")

        tool = _tool(_Down())
        out = json.loads(await tool.execute(operation="list_scripts"))
        assert out["error_type"] == "mcp_unavailable"

    @pytest.mark.asyncio
    async def test_unexpected_exception_never_leaks_traceback(self) -> None:
        class _Broken:
            async def call(self, *_a: Any, **_k: Any) -> str:
                raise RuntimeError("внутренняя поломка")

        tool = _tool(_Broken())
        out = json.loads(await tool.execute(operation="list_scripts"))
        assert out["error_type"] == "unexpected_error"
        assert "Traceback" not in json.dumps(out)


class TestTruncation:
    """Слишком длинный ответ ужат и помечен, а не молча обрезан."""

    @pytest.mark.asyncio
    async def test_oversized_answer_is_truncated_with_flag(self) -> None:
        client = _FakeClient(reply="x" * 5000)
        tool = _tool(client, max_chars=500)
        out = json.loads(await tool.execute(operation="list_scripts"))
        assert out["truncated"] is True
        assert out["operation"] == "list_scripts"
        assert len(out["preview"]) <= 500

    @pytest.mark.asyncio
    async def test_answer_within_limit_is_passed_through_untouched(self) -> None:
        reply = '{"status": "ok", "row_count": 1}'
        tool = _tool(_FakeClient(reply=reply))
        assert await tool.execute(operation="list_scripts") == reply
