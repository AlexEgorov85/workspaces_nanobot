"""Bootstrap сервера ``enterprise-mcp``.

В файле **нет ни одного** ``@mcp.tool()``. Операции приходят файлами из
``capabilities/*/tools/``, проходят валидацию и регистрируются из реестра.
Обратный порядок — объявить операции декораторами в этом файле — означал бы
правку `server.py` при каждом добавлении, и через месяц новая операция просто
не появилась бы.

Порядок запуска важен:

1. **Проверка зависимостей до всего остального.** Без ``sqlglot`` guard
   проверяет только первый блокируемый оператор, ``INTO`` и multi-statement,
   и пропускает ``pg_sleep``, ``information_schema`` и ``UPDATE`` после
   ``--``-комментария. Сервер, который поднялся без него, охраняет вывод LLM
   хуже, чем не поднялся бы вовсе.
2. **Регистрация сервисов**, затем **загрузка операций**: операция получает
   сервис из контейнера в момент ``create_tool``.
3. **Старт фоновых писателей** — после того как всё загрузилось, иначе буфер
   начнёт принимать события от операций, которых ещё нет.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.errors import InfrastructureError
from libs.enterprise_common.loader import build_server, load_registry
from libs.enterprise_common.registry import ToolRegistry

#: Корень платформы: ``mcp-platform/``. Нужен и загрузчику (для модульных имён),
#: и проверке зависимостей.
PLATFORM_ROOT = Path(__file__).resolve().parents[2]
CAPABILITIES_DIR = PLATFORM_ROOT / "servers" / "enterprise" / "capabilities"

logger = logging.getLogger(__name__)

#: Обязательные пакеты. ``sqlglot`` обязателен: без него AST-ветка guard'а
#: выключается, а это тихо ослабляет единственную защиту вывода LLM.
REQUIRED_PACKAGES = ("sqlglot",)


def _check_dependencies() -> None:
    """Проверить, что сервер может работать, **до** того как он начал.

    Два условия, оба fail-fast по одной причине: сервер, который поднялся и
    работает вхолостую, хуже сервера, который не поднялся.

    * ``sqlglot`` — без AST-ветки guard проверяет только первый блокируемый
      оператор, ``INTO`` и multi-statement, и пропускает ``pg_sleep``,
      ``information_schema`` и ``UPDATE``/``DELETE`` после ``--``-комментария;
    * DSN — без него пул не инициализируется, и буфер журнала честно, но
      незаметно теряет каждое событие. Поднявшийся сервер без DSN выглядит
      рабочим, пока журнал пуст.
    """
    missing: list[str] = []
    for name in REQUIRED_PACKAGES:
        try:
            __import__(name)
        except ImportError:
            missing.append(name)
    if missing:
        raise InfrastructureError(
            "сервер не поднимается без пакетов: "
            + ", ".join(missing)
            + ". Без sqlglot guard проверяет только первый оператор и пропускает "
            "pg_sleep, information_schema и UPDATE/DELETE после -- комментария"
        )

    from libs.enterprise_data.db import resolve_dsn

    if not resolve_dsn():
        raise InfrastructureError(
            "не задан DSN. Задайте переменную окружения DATABASE_URL (или PG_DSN) "
            "для процесса enterprise-mcp. Без неё пул не поднимется, а буфер "
            "журнала будет терять каждое событие"
        )


def _build_container() -> ToolContainer:
    """Собрать контейнер: сервисы capability и конфигурация из окружения."""
    from servers.enterprise.capabilities.data.service.main import DataService

    expected = tuple(
        name.strip()
        for name in os.environ.get("ENTERPRISE_EXPECTED_TABLES", "").split(",")
        if name.strip()
    )
    statement_timeout_ms = int(os.environ.get("ENTERPRISE_STATEMENT_TIMEOUT_MS", "30000"))
    max_rows = int(os.environ.get("ENTERPRISE_MAX_ROWS", "1000"))
    data = DataService(
        log_table=_log_table_from_env(),
        expected_tables=expected,
        statement_timeout_ms=statement_timeout_ms,
        max_rows=max_rows,
        buffer_maxlen=int(os.environ.get("ENTERPRISE_LOG_BUFFER_MAXLEN", "2048")),
        buffer_flush_interval=float(os.environ.get("ENTERPRISE_LOG_FLUSH_INTERVAL", "5")),
    )
    container = ToolContainer(
        services={"data": data},
        config={"statement_timeout_ms": statement_timeout_ms, "max_rows": max_rows},
    )
    return container


def _log_table_from_env() -> tuple[str, str]:
    raw = os.environ.get("ENTERPRISE_LOG_TABLE", "public.agent_gateway_logs")
    schema, _, table = raw.partition(".")
    return schema or "public", table or "agent_gateway_logs"


def build() -> tuple[Any, ToolRegistry, ToolContainer]:
    """Собрать сервер: проверка, сервисы, реестр, транспорт.

    Возвращает ``(server, registry, container)`` — чтобы тест мог проверить
    реестр, не поднимая транспорт.
    """
    _check_dependencies()
    container = _build_container()
    registry = load_registry(CAPABILITIES_DIR, container, root=PLATFORM_ROOT)
    transport = build_server(
        registry,
        name="enterprise-mcp",
        instructions=(
            "Enterprise-слой проекта. Операции данных, векторов, аудита и LLM. "
            "Операций произвольного SQL здесь нет и не будет."
        ),
    )
    logger.info("операций загружено: %d", len(registry))
    for name in registry.names():
        logger.info("  операция: %s", name)
    return transport, registry, container


def main() -> None:
    """Поднять сервер по stdio. Точка входа для MCP-клиента агента."""
    import anyio
    from mcp.server.stdio import stdio_server

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    transport, _, container = build()
    data = container.get("data")
    data.start()
    try:
        async def _serve() -> None:
            async with stdio_server() as (read_stream, write_stream):
                await transport.run(
                    read_stream,
                    write_stream,
                    transport.create_initialization_options(),
                )

        anyio.run(_serve)
    finally:
        data.stop()


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    main()
