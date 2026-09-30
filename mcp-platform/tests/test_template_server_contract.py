"""Контракт эталона: форма сервиса и адаптера.

Проверяет ровно то, что потом копируется в каждый домен:
* сервис работает и тестируется БЕЗ MCP;
* адаптер отдаёт discoverable-инструменты;
* доменная ошибка не превращается в мусор на стороне агента.
"""

from __future__ import annotations

import asyncio

import pytest

from libs.enterprise_common.errors import (
    EnterpriseError,
    InfrastructureError,
    InvalidRequestError,
    NotFoundError,
)
from servers._template.server import mcp
from servers._template.service import EchoService


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

    def test_tools_are_discoverable(self) -> None:
        tools = asyncio.run(mcp.list_tools())
        names = {t.name for t in tools}
        assert {"echo", "lookup"} <= names, f"не найдены инструменты: {names}"

    def test_every_tool_is_documented(self) -> None:
        """Описание попадает агенту в tool discovery — пустое описание
        заставляет модель гадать."""
        for tool in asyncio.run(mcp.list_tools()):
            assert tool.description, f"у инструмента {tool.name} нет описания"
