"""Эталон bootstrap'а: реестр плюс базовый API ``mcp``.

Копируется в новый сервер как есть. Три обязанности:

1. собрать сервисы в контейнер;
2. загрузить операции из ``capabilities/*/tools/*.py``;
3. собрать транспорт из реестра.

Здесь **нет** ``@mcp.tool()`` и **нет** ``FastMCP``: добавление операции — это
новый файл, а не правка этого модуля, и на провод уходит ровно та схема,
которая провалидирована при загрузке.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.loader import build_server, load_registry
from libs.enterprise_common.registry import ToolRegistry

#: Корень сервера: ``servers/_template``.
SERVER_ROOT = Path(__file__).resolve().parent
CAPABILITIES_DIR = SERVER_ROOT / "capabilities"

logger = logging.getLogger(__name__)


def build_container() -> ToolContainer:
    """Контейнер сервисов. В реальном сервере здесь создаются доменные сервисы.

    Регистрация — единственное место, где сервис появляется. Операции его
    достают, но не создают: иначе одна и та же доменная логика жила бы в двух
    экземплярах с разным состоянием.
    """
    from servers._template.capabilities.template.service.main import EchoService

    return ToolContainer(services={"template": EchoService()})


def build() -> tuple[Any, ToolRegistry, ToolContainer]:
    """Собрать сервер и вернуть ``(server, registry, container)``."""
    container = build_container()
    registry = load_registry(CAPABILITIES_DIR, container, root=SERVER_ROOT)
    transport = build_server(
        registry,
        name="enterprise-template",
        instructions="Эталонный сервер. Копируется при создании нового.",
    )
    return transport, registry, container


def main() -> None:
    """Поднять сервер по stdio."""
    import anyio
    from mcp.server.stdio import stdio_server

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    transport, _, _ = build()

    async def _serve() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await transport.run(
                read_stream,
                write_stream,
                transport.create_initialization_options(),
            )

    anyio.run(_serve)


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    main()
