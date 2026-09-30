"""Слой адаптера: MCP ↔ сервис.

Только три обязанности:
1. объявить схему параметров (агент видит её в tool discovery);
2. вызвать метод сервиса;
3. превратить доменную ошибку в ``McpError`` с понятным кодом.

Здесь не должно быть ни ветвлений по бизнес-правилам, ни SQL,
ни обращений к БД, ни знания о том, как устроен агент.
"""

from __future__ import annotations

from libs.enterprise_common.errors import EnterpriseError
from mcp.server.fastmcp import FastMCP

from servers._template.service import EchoService

mcp = FastMCP("enterprise-template")
_service = EchoService()


@mcp.tool()
def echo(text: str) -> str:
    """Пример capabilities. Описание попадает в tool discovery агента."""
    try:
        result = _service.echo(text)
    except EnterpriseError as exc:
        # Доменная ошибка наружу уходит как MCP-ошибка с её кодом.
        # Агент не должен видеть traceback и внутренние исключения.
        raise ValueError(f"[{exc.code}] {exc.message}") from exc
    return f"{result.text} (len={result.length})"


@mcp.tool()
def lookup(key: str) -> str:
    """Пример «не найдено»."""
    try:
        result = _service.find(key)
    except EnterpriseError as exc:
        raise ValueError(f"[{exc.code}] {exc.message}") from exc
    return result.text


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    mcp.run()  # stdio по умолчанию
