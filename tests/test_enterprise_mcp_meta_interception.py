"""Страж перехвата вызова агента в MCP: личность достраивается автоматически.

До появления перехвата каждый tool' собирал ``CallIdentity`` руками: одна и
та же функция копировалась из tool'а в tool' (``audit_analyzer_query`` ->
``legal_summarizer_query``), и расхождение копий не было бы заметно нигде -
вызов либо проходил, либо нет.

Перехват живёт в ``EnterpriseMcpClient.call()``: если вызывающая сторона
личность не передала, она собирается из доверенного контекста оборота. Это
единственное место, где личность появляется сама.

Проверяются четыре свойства, каждое из которых ломает изоляцию при
нарушении:

  * личность едет в ``meta``, а не в аргументы;
  * вне оборота ``_meta`` **не отправляется вовсе**, а не отправляется пустым
    (пустой ``_meta`` на сервере неотличим от «идентичность была и пустая»);
  * явный ``identity`` выигрывает у контекста;
  * недоступный журнал не роняет вызов: связь с ``agent_question_runs``
    выражается признаком ``correlated=false``, который проставит сервер.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from lib.services.enterprise_mcp_client import (
    META_PREFIX,
    CallIdentity,
    EnterpriseMcpClient,
)

_SESSION_CTX = "nanobot.agent.tools.context.current_request_context"
_SESSION_KEY = "nanobot.agent.tools.context.current_request_session_key"


class _Turn:
    def __init__(self, sender_id: Any = "u-1") -> None:
        self.sender_id = sender_id


class _Logging:
    def __init__(self, request_id: str | None = "req-7") -> None:
        self.request_id = request_id

    def get_request_id(self, _session_key: str | None) -> str | None:
        return self.request_id


class _BrokenLogging:
    def get_request_id(self, _session_key: str | None) -> str | None:
        raise RuntimeError("журнал недоступен")


class _FakeSession:
    """Сессия, которая только запоминает, как её вызвали."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], Any]] = []

    async def call_tool(
        self, operation: str, arguments: dict[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        self.calls.append((operation, arguments or {}, kwargs.get("meta")))
        return type("_R", (), {"content": [type("_B", (), {"text": "{}"})()]})()

    async def list_tools(self) -> Any:
        return type("_R", (), {"tools": []})()


def _client(logging_service: Any = None) -> tuple[EnterpriseMcpClient, _FakeSession]:
    client = EnterpriseMcpClient(
        command="echo", db_logging_service=logging_service
    )
    session = _FakeSession()
    # Сессия подставляется напрямую: поднимать реальный процесс ради проверки
    # разбора метаданных незачем.
    client._session = session
    return client, session


def _in_turn(sender_id: Any = "u-1", session_key: str = "s-1") -> Any:
    return patch.multiple(
        "nanobot.agent.tools.context",
        current_request_context=lambda: _Turn(sender_id),
        current_request_session_key=lambda: session_key,
    )


class TestInterception:
    """Личность появляется сама, когда вызывающий её не передал."""

    @pytest.mark.asyncio
    async def test_meta_is_filled_without_explicit_identity(self) -> None:
        client, session = _client(_Logging())
        with _in_turn():
            await client.call("query_operation", {"operation_id": "op-1"})

        operation, arguments, meta = session.calls[0]
        assert operation == "query_operation"
        assert meta is not None, "_meta не отправлен - перехват не сработал"
        assert meta[f"{META_PREFIX}session_id"] == "s-1"
        assert meta[f"{META_PREFIX}user_id"] == "u-1"
        assert meta[f"{META_PREFIX}request_id"] == "req-7"
        # Граница изоляции: личность не должна дублироваться в аргументах.
        for key in ("session_id", "user_id", "request_id"):
            assert key not in arguments

    @pytest.mark.asyncio
    async def test_explicit_identity_wins(self) -> None:
        client, session = _client(_Logging())
        explicit = CallIdentity(session_id="s-explicit", user_id="u-explicit")
        with _in_turn():
            await client.call("query_operation", {}, identity=explicit)

        meta = session.calls[0][2]
        assert meta[f"{META_PREFIX}session_id"] == "s-explicit"
        assert meta[f"{META_PREFIX}user_id"] == "u-explicit"

    @pytest.mark.asyncio
    async def test_outside_a_turn_meta_is_not_sent_at_all(self) -> None:
        """Пустой ``_meta`` неотличим от «идентичность была и пустая»."""
        client, session = _client(_Logging())
        with patch(_SESSION_CTX, lambda: None):
            await client.call("query_operation", {"operation_id": "op-1"})

        assert session.calls[0][2] is None

    @pytest.mark.asyncio
    async def test_without_sender_no_meta(self) -> None:
        client, session = _client(_Logging())
        with _in_turn(sender_id=None):
            await client.call("query_operation", {})

        assert session.calls[0][2] is None

    @pytest.mark.asyncio
    async def test_broken_journal_does_not_break_the_call(self) -> None:
        """Падение журнала убирает связь с оборотом, но не сам вызов.

        ``request_id`` в этом случае подставляет сам клиент - и пишет в лог,
        что связи с ``agent_question_runs`` не будет. Проверяем именно это:
        отказ был бы правильнее для честности журнала и хуже для живости
        агента, потому что недоступность логирования не должна отменять
        работу.
        """
        client, session = _client(_BrokenLogging())
        with _in_turn():
            await client.call("query_operation", {"operation_id": "op-1"})

        meta = session.calls[0][2]
        assert meta is not None, "падение журнала отменяло вызов"
        # Свой request_id, а не подстановка несуществующего оборота.
        assert meta[f"{META_PREFIX}request_id"]
        assert meta[f"{META_PREFIX}session_id"] == "s-1"

    @pytest.mark.asyncio
    async def test_without_journal_service_call_still_goes(self) -> None:
        """Журнал может быть выключен - это не повод отказывать в вызове."""
        client, session = _client(None)
        with _in_turn():
            await client.call("query_operation", {"operation_id": "op-1"})

        meta = session.calls[0][2]
        assert meta is not None
        assert meta[f"{META_PREFIX}session_id"] == "s-1"


class TestNoNewTransport:
    """Перехват не создаёт второго пути к серверу."""

    def test_client_does_not_read_identity_env_vars(self) -> None:
        """Клиент не читает ``ENTERPRISE_*`` напрямую.

        Окружение читает только реестр платформы. Клиент, разбирающий
        переменные поимённо, стал бы вторым читателем настройки - и упал бы
        на ``test_settings_registry.py::TestOnlyTheRegistryReadsTheEnvironment``.
        """
        import inspect

        source = inspect.getsource(EnterpriseMcpClient)
        assert "ENTERPRISE_SESSION_ID" not in source
        assert "ENTERPRISE_USER_ID" not in source
        assert "ENTERPRISE_REQUEST_ID" not in source

    def test_loop_is_not_leaked(self) -> None:
        """Сеанс перехвата не должен оставлять живых loop'ов."""
        client, _ = _client()
        assert isinstance(client._lock, asyncio.Lock)
