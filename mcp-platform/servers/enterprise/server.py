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

import json
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


def _audit_config_from_env() -> dict[str, Any]:
    """Собрать конфигурацию capability ``audit`` из окружения.

    Имя таблицы реестра и список разрешённых таблиц — источник истины агента:
    реестр скриптов помечен в ``TableRegistry`` меткой ``scripts_registry``, и
    платформа его не читает. Оба значения приходят аргументом операции, а не
    знанием о проекте.

    Белый список таблиц разбирается с ``\\n`` и ``,`` — одна переменная
    окружения, а не JSON: список короткий, разбирается глазами в логе старта, и
    ошибка разбора JSON здесь стоила бы отдельного кода ошибки ради значения,
    которое оператор и так напишет руками.
    """
    raw_tables = os.environ.get("ENTERPRISE_AUDIT_TABLES", "")
    tables = [
        item.strip()
        for line in raw_tables.splitlines()
        for item in line.split(",")
        if item.strip()
    ]
    return {
        "scripts_registry": {
            "table": os.environ.get("ENTERPRISE_SCRIPTS_REGISTRY_TABLE", "").strip(),
        },
        "audit": {
            "tables": tables,
            "row_ceiling": os.environ.get("ENTERPRISE_AUDIT_ROW_CEILING", "").strip(),
        },
    }


def _build_container() -> ToolContainer:
    """Собрать контейнер: сервисы capability и конфигурация из окружения."""
    from servers.enterprise.capabilities.audit.service.main import AuditService
    from servers.enterprise.capabilities.data.service.main import DataService
    from servers.enterprise.capabilities.llm.service.main import LlmService
    from servers.enterprise.capabilities.vectors.service.main import VectorsService

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
        snapshot=_snapshot_from_env(),
    )
    # Конфигурация LLM резолвится лениво, внутри сервиса. Сборка сервера
    # обязана пережить её отсутствие: процесс обслуживает несколько
    # capability, и забытая переменная провайдера не должна снимать из
    # работы ``data``. Незаданный провайдер отдаёт ``infrastructure_error``
    # на своей операции и виден как ``configured: false`` в health-отчёте.
    llm = LlmService()
    config = {
        "statement_timeout_ms": statement_timeout_ms,
        "max_rows": max_rows,
        **_audit_config_from_env(),
    }
    container = ToolContainer(services={"data": data, "llm": llm}, config=config)
    # Регистрация ПОСЛЕ сборки контейнера: конструктор VectorsService берёт
    # сервисы ``data`` и ``llm`` из контейнера сразу, а не на первом запросе.
    # Причина — диагностика: отсутствие эмбеддера должно падать на сборке,
    # а не отдавать агенту «индексов нет» там, где на самом деле нет провайдера.
    container.register(
        "vectors", VectorsService(container=container, config=config)
    )
    # Конвейер аудита берёт снимок у ``data``, модель у ``llm`` — те же
    # колбэки, а не собственные подключения. Регистрация тоже после сборки
    # контейнера: сервис обращается к нему в момент первого вызова, и
    # отсутствие владельца должно быть видно на старте, а не в рантайме хода.
    container.register("audit", AuditService(container=container, config=config))
    return container


def _snapshot_from_env() -> Any:
    """Открыть снимок DuckDB, если оператор его задал. Иначе — ``None``.

    Снимок **не обязателен**. ``open_snapshot_store`` падает громко и
    правильно (занятый файл, неподдерживаемая ФС), и требование снимка при
    старте означало бы, что сервер перестаёт подниматься там, где capability
    ``vectors`` не развёрнут, — вместе с работающим ``history_search``.

    Без переменной снимок остаётся неподключённым, и чтение даёт
    ``InfrastructureError`` («снимок недоступен»), а не «индексов нет»:
    разница между «нечего искать» и «нечем искать» обязана быть видна.

    Принимается именно **путь к файлу**, а не каталог: у агента путь
    считается единственным механизмом ``resolve_cache_path()``, и требовать
    от него ещё и разбирать свой путь обратно на каталог значило бы завести
    второе место, где решается, где лежит снимок. Форма «каталог» остаётся в
    платформе для standalone-утилит (``resolve_snapshot_path``), но не как
    вторая переменная окружения.
    """
    path = os.environ.get("ENTERPRISE_SNAPSHOT_PATH", "").strip()
    if not path:
        return None
    from libs.enterprise_data.snapshot import CacheAccessMode, open_snapshot_store

    return open_snapshot_store(
        path,
        CacheAccessMode.READ_ONLY,
        vector_db_table=os.environ.get("ENTERPRISE_VECTOR_DB_TABLE", ""),
    )


def _vectors_config_from_env() -> dict[str, Any]:
    """Собрать конфигурацию capability ``vectors`` из окружения.

    Форма повторяет ``project.json`` агента (``gateway.vector.*``), чтобы
    перенос не переписывал объявления индексов руками: агент экспортирует их
    в окружение процесса, платформа читает — и индекса, объявленного у
    агента, не может случайно не оказаться здесь.

    Пустые значения **не заменяются дефолтами**: подпись индекса обязана
    отражать конфигурацию процесса, и выдуманный дефолт сделал бы её
    одинаковой для настроенного и ненастроенного провайдера.
    """
    raw_indexes = os.environ.get("ENTERPRISE_VECTOR_INDEXES", "").strip()
    indexes: dict[str, Any] = {}
    if raw_indexes:
        try:
            parsed = json.loads(raw_indexes)
        except json.JSONDecodeError as exc:
            raise InfrastructureError(
                f"ENTERPRISE_VECTOR_INDEXES не разбирается как JSON: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise InfrastructureError(
                "ENTERPRISE_VECTOR_INDEXES должен быть JSON-объектом вида "
                "{имя индекса: описание}, получено "
                f"{type(parsed).__name__}"
            )
        indexes = parsed

    def _optional_int(name: str) -> int | None:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError as exc:
            raise InfrastructureError(
                f"{name} должен быть целым числом, получено {raw!r}"
            ) from exc

    def _optional_float(name: str) -> float | None:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError as exc:
            raise InfrastructureError(
                f"{name} должен быть числом, получено {raw!r}"
            ) from exc

    embedding = {
        "model": os.environ.get("ENTERPRISE_EMBED_MODEL", "").strip() or None,
        "dimension": _optional_int("ENTERPRISE_EMBED_DIMENSION"),
        "timeout_sec": _optional_float("ENTERPRISE_EMBED_TIMEOUT"),
    }
    index = {
        "enable": os.environ.get("ENTERPRISE_VECTOR_ENABLE", "1") != "0",
        "storage_table": os.environ.get("ENTERPRISE_VECTOR_STORAGE_TABLE", "").strip(),
        "indexes": indexes,
    }
    return {"gateway": {"vector": {"embedding": embedding, "index": index}}}


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
