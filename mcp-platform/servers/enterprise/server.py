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
from libs.enterprise_common.loader import load_registry, register_with_server
from mcp.server.fastmcp import FastMCP

#: Корень платформы: ``mcp-platform/``. Нужен и загрузчику (для модульных имён),
#: и проверке зависимостей.
PLATFORM_ROOT = Path(__file__).resolve().parents[2]
CAPABILITIES_DIR = PLATFORM_ROOT / "servers" / "enterprise" / "capabilities"

logger = logging.getLogger(__name__)

#: Обязательные пакеты. ``sqlglot`` обязателен: без него AST-ветка guard'а
#: выключается, а это тихо ослабляет единственную защиту вывода LLM.
REQUIRED_PACKAGES = ("sqlglot",)


def _check_dependencies() -> None:
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


def build() -> tuple[Any, Any, ToolContainer]:
    """Собрать сервер: проверка, сервисы, реестр, регистрация.

    Возвращает ``(mcp, registry, container)`` — чтобы тест мог проверить
    реестр, не поднимая транспорт.
    """
    _check_dependencies()
    container = _build_container()
    registry = load_registry(CAPABILITIES_DIR, container, root=PLATFORM_ROOT)
    registered = register_with_server(registry, mcp)

    logger.info("операций загружено: %d", len(registered))
    for name in registry.names():
        logger.info("  операция: %s", name)
    return mcp, registry, container


#: Экземпляр создаётся один и экспортируется под именем ``mcp``: это
#: обязательный контракт — архитектурный тест поднимает сервер в подпроцессе
#: с заблокированным ``nanobot`` и требует наличия этого атрибута.
mcp = FastMCP("enterprise-mcp")


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    build()
    mcp.run()  # stdio по умолчанию
