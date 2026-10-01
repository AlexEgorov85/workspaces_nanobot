"""Bootstrap сервера ``enterprise-mcp``.

В файле **нет ни одного** ``@mcp.tool()``. Операции приходят файлами из
``capabilities/*/tools/``, проходят валидацию и регистрируются из реестра.
Обратный порядок — объявить операции декораторами в этом файле — означал бы
правку `server.py` при каждом добавлении, и через месяц новая операция просто
не появилась бы.

Порядок запуска важен:

1. **Разбор настроек — один, до всего остального.** ``build()`` строит
   единственный ``Settings`` и передаёт его дальше; ``os.environ`` в этом
   файле не читается ни разу, а ``platform.json`` читает только реестр.
   Пока каждая функция читала окружение сама, файл с настройками был
   прочитан тестами и только ими: значения из ``platform.json`` выглядели
   рабочими и не влияли ни на что.
2. **Пул соединений настраивается до первого касания.** Число воркеров
   поднимается в ``start()``, и поздняя настройка осталась бы вхолостую.
3. **Проверка зависимостей до всего остального.** Без ``sqlglot`` guard
   проверяет только первый блокируемый оператор, ``INTO`` и multi-statement,
   и пропускает ``pg_sleep``, ``information_schema`` и ``UPDATE`` после
   ``--``-комментария. Сервер, который поднялся без него, охраняет вывод LLM
   хуже, чем не поднялся бы вовсе.
4. **Регистрация сервисов**, затем **загрузка операций**: операция получает
   сервис из контейнера в момент ``create_tool``.
5. **Старт фоновых писателей** — после того как всё загрузилось, иначе буфер
   начнёт принимать события от операций, которых ещё нет.

Ни одного чтения окружения в платформе, кроме реестра: ``os.environ`` в
этом файле не встречается ни разу, а DSN приходит из ``Settings`` и
передаётся пулу через ``configure()``. Значение, к которому процесс
подключается, имеет одного хозяина — иначе у секрета два независимых
ответа, и расхождение между ними выглядит как «настроено».
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.errors import InfrastructureError
from libs.enterprise_common.loader import build_server, load_registry
from libs.enterprise_common.registry import ToolRegistry
from libs.enterprise_common.settings import Settings, pool_config

#: Корень платформы: ``mcp-platform/``. Нужен и загрузчику (для модульных имён),
#: и проверке зависимостей.
PLATFORM_ROOT = Path(__file__).resolve().parents[2]
CAPABILITIES_DIR = PLATFORM_ROOT / "servers" / "enterprise" / "capabilities"

logger = logging.getLogger(__name__)

#: Обязательные пакеты. ``sqlglot`` обязателен: без него AST-ветка guard'а
#: выключается, а это тихо ослабляет единственную защиту вывода LLM.
REQUIRED_PACKAGES = ("sqlglot",)


def _check_dependencies(settings: Settings) -> None:
    """Проверить, что сервер может работать, **до** того как он начал.

    Два условия, оба fail-fast по одной причине: сервер, который поднялся и
    работает вхолостую, хуже сервера, который не поднялся.

    * ``sqlglot`` — без AST-ветки guard проверяет только первый блокируемый
      оператор, ``INTO`` и multi-statement, и пропускает ``pg_sleep``,
      ``information_schema`` и ``UPDATE``/``DELETE`` после ``--``-комментария;
    * DSN — без него пул не инициализируется, и буфер журнала честно, но
      незаметно теряет каждое событие. Поднявшийся сервер без DSN выглядит
      рабочим, пока журнал пуст.

    DSN берётся из реестра, а не из процесса: читателей окружения должен
    быть ровно один. Сам секрет в реестр не попадает (владелец — агент, в
    ``platform.json`` ключи нет), но **решение о нём** принимает тот же
    код, что и обо всём остальном.
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

    if not settings.get("DATABASE_URL"):
        raise InfrastructureError(
            "не задан DSN. Задайте его в mcp-platform/platform.json "
            "(секция db, ключ dsn — логин и пароль подстановками "
            "${DB_USER}:${DB_PASSWORD}) или переменной окружения DATABASE_URL "
            "(затем PG_DSN) для процесса enterprise-mcp. Без неё пул не "
            "поднимется, а буфер журнала будет терять каждое событие"
        )


def _configure_dsn(settings: Settings) -> None:
    """Передать DSN, разрешённый реестром, владельцу пула.

    Пул не читает окружение: ``resolve_dsn()`` возвращает только то, что
    задано ``configure()``. Ответственность за «к какой базе подключается
    процесс» таким образом одна, а не две — у реестра и у пула.
    """
    from libs.enterprise_data.db import configure

    configure(str(settings.get("DATABASE_URL")))


def _apply_pool_settings(settings: Settings) -> None:
    """Передать размеры и таймауты пула его владельцу.

    Единственное место платформы, где пул настраивается: значения приходят
    из реестра (окружение > ``platform.json`` > дефолт), а не из словаря в
    коде. До этого вызова пул жил на ``_DEFAULT_POOL``, и изменить его было
    нечем — ни файлом, ни переменной, ничем.
    """
    from libs.enterprise_data.db import set_pool_config

    config = pool_config(settings)
    set_pool_config(config)
    logger.info(
        "пул соединений: min=%d max=%d очередь=%d таймаут=%s сек (%s)",
        config["min_conn"],
        config["max_conn"],
        config["queue_maxsize"],
        config["pool_timeout"],
        settings.source("ENTERPRISE_POOL_MAX_CONN"),
    )


def _audit_config(settings: Settings) -> dict[str, Any]:
    """Собрать конфигурацию capability ``audit`` из разрешённых настроек.

    Имя таблицы реестра и список разрешённых таблиц — источник истины агента:
    реестр скриптов помечен в ``TableRegistry`` меткой ``scripts_registry``, и
    платформа его не читает. Оба значения приходят аргументом операции, а не
    знанием о проекте.

    Белый список таблиц — одна переменная окружения, а не JSON: список
    короткий, разбирается глазами в логе старта, и ошибка разбора JSON здесь
    стоила бы отдельного кода ошибки ради значения, которое оператор и так
    напишет руками. Разбор по переводу строки и запятой — в реестре
    (``Setting.coerce``), потому что агент экспортирует список по одной
    таблице на строку.
    """
    return {
        "scripts_registry": {
            "table": str(settings.get("ENTERPRISE_SCRIPTS_REGISTRY_TABLE")).strip(),
        },
        "audit": {
            "tables": list(settings.get("ENTERPRISE_AUDIT_TABLES")),
            "row_ceiling": settings.get("ENTERPRISE_AUDIT_ROW_CEILING"),
        },
    }


def _build_container(settings: Settings) -> ToolContainer:
    """Собрать контейнер: сервисы capability и конфигурация из настроек."""
    from servers.enterprise.capabilities.audit.service.main import AuditService
    from servers.enterprise.capabilities.data.service.main import DataService
    from servers.enterprise.capabilities.llm.service.main import LlmService
    from servers.enterprise.capabilities.vectors.service.main import VectorsService

    statement_timeout_ms = int(settings.get("ENTERPRISE_STATEMENT_TIMEOUT_MS"))
    max_rows = int(settings.get("ENTERPRISE_MAX_ROWS"))
    data = DataService(
        log_table=_log_table(settings),
        expected_tables=tuple(settings.get("ENTERPRISE_EXPECTED_TABLES")),
        statement_timeout_ms=statement_timeout_ms,
        max_rows=max_rows,
        buffer_maxlen=int(settings.get("ENTERPRISE_LOG_BUFFER_MAXLEN")),
        buffer_flush_interval=float(settings.get("ENTERPRISE_LOG_FLUSH_INTERVAL")),
        snapshot=_snapshot(settings),
    )
    # Конфигурация LLM разбирается реестром и передаётся сервису, но
    # используется лениво, внутри него. Сборка сервера обязана пережить её
    # отсутствие: процесс обслуживает несколько capability, и забытая
    # переменная провайдера не должна снимать из работы ``data``.
    # Незаданный провайдер отдаёт ``infrastructure_error`` на своей операции
    # и виден как ``configured: false`` в health-отчёте.
    #
    # ``as_env()`` — а не ``os.environ``: иначе сервис разрешал бы значения
    # сам, и файл с настройками влиял бы на один сервис, а не на все.
    llm = LlmService(env=settings.as_env())
    config = {
        "statement_timeout_ms": statement_timeout_ms,
        "max_rows": max_rows,
        **_vectors_config(settings),
        **_audit_config(settings),
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


def _snapshot(settings: Settings) -> Any:
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
    path = str(settings.get("ENTERPRISE_SNAPSHOT_PATH")).strip()
    if not path:
        return None
    from libs.enterprise_data.snapshot import CacheAccessMode, open_snapshot_store

    return open_snapshot_store(
        path,
        CacheAccessMode.READ_ONLY,
        vector_db_table=str(settings.get("ENTERPRISE_VECTOR_DB_TABLE")),
    )


def _vectors_config(settings: Settings) -> dict[str, Any]:
    """Собрать конфигурацию capability ``vectors`` из разрешённых настроек.

    Форма повторяет ``project.json`` агента (``gateway.vector.*``), чтобы
    перенос не переписывал объявления индексов руками: агент экспортирует их
    в окружение процесса, платформа читает — и индекса, объявленного у
    агента, не может случайно не оказаться здесь.

    Пустые значения **не заменяются дефолтами**: подпись индекса обязана
    отражать конфигурацию процесса, и выдуманный дефолт сделал бы её
    одинаковой для настроенного и ненастроенного провайдера.

    Типы (``ENTERPRISE_EMBED_DIMENSION``, ``ENTERPRISE_EMBED_TIMEOUT``)
    приводит реестр: значение не того типа поднимается там, с именем
    настройки, а не здесь, где неизвестно, о какой речь.
    """
    raw_indexes = str(settings.get("ENTERPRISE_VECTOR_INDEXES")).strip()
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

    embedding = {
        "model": str(settings.get("ENTERPRISE_EMBED_MODEL")).strip() or None,
        "dimension": settings.get("ENTERPRISE_EMBED_DIMENSION"),
        "timeout_sec": settings.get("ENTERPRISE_EMBED_TIMEOUT"),
    }
    index = {
        "enable": bool(settings.get("ENTERPRISE_VECTOR_ENABLE")),
        "storage_table": str(settings.get("ENTERPRISE_VECTOR_STORAGE_TABLE")).strip(),
        "indexes": indexes,
    }
    return {"gateway": {"vector": {"embedding": embedding, "index": index}}}


def _log_table(settings: Settings) -> tuple[str, str]:
    raw = str(settings.get("ENTERPRISE_LOG_TABLE"))
    schema, _, table = raw.partition(".")
    return schema or "public", table or "agent_gateway_logs"


def build() -> tuple[Any, ToolRegistry, ToolContainer]:
    """Собрать сервер: настройки, пул, сервисы, реестр, транспорт.

    Возвращает ``(server, registry, container)`` — чтобы тест мог проверить
    реестр, не поднимая транспорт.
    """
    settings = Settings()
    _check_dependencies(settings)
    _configure_dsn(settings)
    _apply_pool_settings(settings)
    container = _build_container(settings)
    registry = load_registry(CAPABILITIES_DIR, container, root=PLATFORM_ROOT)
    transport = build_server(
        registry,
        name="enterprise-mcp",
        instructions=(
            "Enterprise-слой проекта. Операции данных, векторов, аудита и LLM. "
            "Операций произвольного SQL здесь нет и не будет."
        ),
    )
    from_file = settings.file_backed()
    if from_file:
        logger.info("настройки из platform.json: %s", ", ".join(from_file))
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
