"""Контракт эталона: форма сервиса и сборка транспорта из реестра.

Проверяет ровно то, что потом копируется в каждый домен:
* сервис работает и тестируется БЕЗ протокола;
* операции попадают в discovery с описаниями;
* доменная ошибка не превращается в мусор на стороне агента.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from libs.enterprise_common.errors import (  # noqa: E402
    EnterpriseError,
    InfrastructureError,
    InvalidRequestError,
    NotFoundError,
)
from servers._template.capabilities.template.service.main import EchoService  # noqa: E402
from servers._template.server import build  # noqa: E402


async def _discover(transport: Any) -> list[Any]:
    """Операции так, как их увидит настоящий клиент, — по протоколу."""
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    async with connect(transport) as session:
        return (await session.list_tools()).tools


class TestServiceWithoutMcp:
    """Сервис обязан быть тестируемым без протокола."""

    def test_echo_returns_domain_type(self) -> None:
        result = EchoService().echo("привет")
        assert result.text == "привет"
        assert result.length == 6

    def test_empty_text_is_invalid_request(self) -> None:
        with pytest.raises(InvalidRequestError) as exc:
            EchoService().echo("   ")
        assert exc.value.code == "invalid_request"

    def test_missing_entry_is_not_found(self) -> None:
        with pytest.raises(NotFoundError) as exc:
            EchoService().find("нет-такого")
        assert exc.value.code == "not_found"

    def test_error_hierarchy_is_transport_agnostic(self) -> None:
        """Инфраструктурная ошибка должна отличаться от валидационной:
        retry осмыслен только для первой."""
        for cls in (NotFoundError, InvalidRequestError, InfrastructureError):
            assert issubclass(cls, EnterpriseError)
        assert InfrastructureError.code != InvalidRequestError.code


class TestAdapterDiscovery:
    """Адаптер обязан отдавать инструменты, видимые агенту."""

    def test_operations_come_from_registry_not_decorators(self) -> None:
        """Добавление операции — новый файл, а не правка bootstrap'а."""
        _, registry, _ = build()
        assert set(registry.names()) == {"template.echo", "template.lookup"}
        assert set(registry.by_capability()) == {"template"}

    def test_tools_are_discoverable(self) -> None:
        import anyio

        transport, registry, _ = build()
        tools = anyio.run(_discover, transport)
        names = {t.name for t in tools}
        assert set(registry.names()) <= names, f"не найдены операции: {names}"

    def test_every_tool_is_documented(self) -> None:
        """Описание попадает агенту в discovery — пустое описание заставляет
        модель гадать."""
        import anyio

        transport, _, _ = build()
        for tool in anyio.run(_discover, transport):
            assert tool.description, f"у операции {tool.name} нет описания"

    def test_domain_error_reaches_caller_as_code(self) -> None:
        import anyio

        from mcp.shared.memory import create_connected_server_and_client_session as connect

        from libs.enterprise_common.execution.context import (
            KEY_REQUEST_ID,
            KEY_SESSION_ID,
            KEY_USER_ID,
        )

        transport, _, _ = build()

        async def call() -> Any:
            async with connect(transport) as session:
                return await session.call_tool(
                    "template.lookup",
                    arguments={"key": "нет-такого"},
                    meta={
                        KEY_REQUEST_ID: "req-template",
                        KEY_SESSION_ID: "sess-template",
                        KEY_USER_ID: "user-template",
                    },
                )

        result = anyio.run(call)
        text = result.content[0].text
        assert result.isError is True
        assert "not_found" in text
        assert "Traceback" not in text

    def test_call_without_meta_is_refused(self) -> None:
        """Заготовка поднимается с конвейером, а не с прямым вызовом обработчика.

        Без метаданных оборота операция не выполняется: идентичность на сервере
        не достраивается, и «работающая заготовка без ``_meta``» была бы
        приглашением собрать новый сервер не по контракту.
        """
        import anyio

        from mcp.shared.memory import create_connected_server_and_client_session as connect

        transport, _, _ = build()

        async def call() -> Any:
            async with connect(transport) as session:
                return await session.call_tool("template.echo", arguments={"text": "привет"})

        result = anyio.run(call)
        assert result.isError is True
        assert "identity_missing" in result.content[0].text
