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
from libs.enterprise_common.execution.factory import build_execution_layer
from libs.enterprise_common.loader import build_server, load_registry
from libs.enterprise_common.registry import ToolRegistry
from libs.enterprise_common.settings import Settings

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


def build(
    session_root: str | Path | None = None,
) -> tuple[Any, ToolRegistry, ToolContainer]:
    """Собрать сервер и вернуть ``(server, registry, container)``.

    Args:
        session_root: куда писать файлы сессии. ``None`` — путь из настроек,
            развёрнутый против каталога сервера.
    """
    container = build_container()
    registry = load_registry(CAPABILITIES_DIR, container, root=SERVER_ROOT)
    # Слой исполнения обязателен и здесь: ``build_server`` без конвейера
    # означал бы вызов операции без идентичности, без журнала и без предела
    # времени.
    #
    # Настройки берутся из реестра, а не из пустого словаря — так же, как в
    # настоящем сервере. Заготовка, собранная с пустыми настройками,
    # поднималась бы с другими порогами и другим отношением к идентичности,
    # чем прод, и расхождение обнаружилось бы уже после копирования: сначала
    # в готовом сервере, у которого уже есть вызывающие. Реестр остаётся
    # единственным, кто читает ``platform.json`` и окружение, — сервер
    # получает уже разрешённые значения и не знает, откуда они.
    settings = Settings()
    execution = build_execution_layer(
        settings,
        session_root=session_root
        or SERVER_ROOT / (settings.get("ENTERPRISE_EXEC_SESSION_ROOT") or ".sessions"),
    )
    transport = build_server(
        registry,
        pipeline=execution.pipeline,
        name="enterprise-template",
        instructions="Эталонный сервер. Копируется при создании нового.",
    )
    return transport, registry, container


def main(argv: list[str] | None = None) -> None:
    """Поднять сервер по stdio.

    Args:
        argv: аргументы командной строки; ``None`` — ``sys.argv[1:]``.

    ``--session-root`` перенаправляет файлы сессии: заготовку поднимают и в
    стенде, и в каталоге рядом с исходниками, и второй раз писать туда же
    нельзя.
    """
    import argparse

    import anyio
    from mcp.server.stdio import stdio_server

    parser = argparse.ArgumentParser(description="Эталонный MCP-сервер")
    parser.add_argument(
        "--session-root",
        default=None,
        help="каталог файлов сессии; по умолчанию — из настроек",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    transport, _, _ = build(session_root=args.session_root)

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
