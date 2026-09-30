"""Эталон bootstrap'а: реестр вместо декораторов.

Копируется в новый сервер как есть. Три обязанности:

1. собрать сервисы в контейнер;
2. загрузить операции из ``capabilities/*/tools/*.py``;
3. зарегистрировать их в MCP.

Здесь **нет** ``@mcp.tool()``: добавление операции — это новый файл, а не
правка этого модуля.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.loader import load_registry, register_with_server
from mcp.server.fastmcp import FastMCP

#: Корень сервера: ``servers/_template``.
SERVER_ROOT = Path(__file__).resolve().parent
CAPABILITIES_DIR = SERVER_ROOT / "capabilities"

logger = logging.getLogger(__name__)


def build_container() -> ToolContainer:
    """Контейнер сервисов. В реальном сервере здесь создаются доменные сервисы."""
    return ToolContainer()


def build() -> tuple[Any, Any, ToolContainer]:
    """Собрать сервер и вернуть ``(mcp, registry, container)``."""
    container = build_container()
    registry = load_registry(CAPABILITIES_DIR, container, root=SERVER_ROOT)
    register_with_server(registry, mcp)
    return mcp, registry, container


#: Экземпляр экспортируется под именем ``mcp`` — это обязательный контракт,
#: на который опирается архитектурный тест «сервер поднимается без агента».
mcp = FastMCP("enterprise-template")


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    build()
    mcp.run()
