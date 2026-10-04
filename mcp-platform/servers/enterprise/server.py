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

import logging
import sys
from pathlib import Path
from typing import Any

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.errors import InfrastructureError
from libs.enterprise_common.execution.factory import (
    build_execution_layer,
    resolved_journal_min_level,
)
from libs.enterprise_common.loader import build_server, load_registry
from libs.enterprise_common.registry import ToolRegistry
from libs.enterprise_common.settings import (
    AGENT_SETTINGS_FILE_FLAG,
    POOL_SETTING_KEYS,
    SCRIPTS_REGISTRY_LABEL,
    Settings,
    agent_settings_summary,
    job_class_config,
    job_class_setting_names,
    pool_config,
)
from servers.enterprise import http_transport

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


def _source_note(settings: Settings, names: tuple[str, ...]) -> str:
    """Откуда применены значения: ``file:platform.json`` либо смесь.

    Одна подпись на раздел, а не по строке на ключ: источник у ключей обычно
    один, и перечисление одинаковых подписей не читается. Разворачивается он
    поимённо ровно тогда, когда источники разошлись, — и это единственный
    случай, в котором оператор не может объяснить «почему применено не то, что
    объявлено в файле» без лога.
    """
    sources = sorted({settings.source(name) for name in names})
    if len(sources) == 1:
        return sources[0]
    return ", ".join(f"{name}={settings.source(name)}" for name in names)


def _apply_pool_settings(settings: Settings) -> None:
    """Передать размеры, таймауты и пределы классов их владельцу.

    Единственное место платформы, где пул настраивается: значения приходят
    из реестра (окружение > ``platform.json`` > дефолт), а не из словаря в
    коде. До этого вызова пул жил на ``_DEFAULT_POOL``, и изменить его было
    нечем — ни файлом, ни переменной, ничем.

    Применяются обе секции: ``pool`` отвечает за размер и резерв, а
    ``job_classes`` — за то, чему этот размер разрешает ждать. Пока
    ``set_job_class_config`` не вызван, воркеры берут любую работу, и
    объявленный резерв не значит ничего.
    """
    from libs.enterprise_data.db import set_job_class_config, set_pool_config

    config = pool_config(settings)
    set_pool_config(config)
    classes = job_class_config(settings)
    set_job_class_config(classes)
    shown = ("min_conn", "max_conn", "reserved_workers", "queue_maxsize", "pool_timeout")
    logger.info(
        "пул соединений: min=%d max=%d резерв=%d очередь=%d таймаут=%s сек (%s)",
        config["min_conn"],
        config["max_conn"],
        config["reserved_workers"],
        config["queue_maxsize"],
        config["pool_timeout"],
        _source_note(settings, tuple(POOL_SETTING_KEYS[key] for key in shown)),
    )
    for audience, values in classes.items():
        logger.info(
            "класс работы %s: таймаут=%d мс, очередь=%d, ждать=%s сек, аренда=%s (%s)",
            audience,
            values["statement_timeout_ms"],
            values["queue_maxsize"],
            values["wait_sec"],
            "да" if values["leases"] else "нет",
            _source_note(settings, job_class_setting_names(audience)),
        )


def _audit_config(settings: Settings) -> dict[str, Any]:
    """Собрать конфигурацию capability ``audit`` из разрешённых настроек.

    Объявление таблиц приходит одним списком записей из ``platform.json``
    (``audit.tables``) — той же формой, что в ``project.json`` агента.
    Запись с меткой ``scripts_registry`` — реестр предустановленных
    скриптов: возвращается отдельно и в доменные таблицы не попадает,
    потому что аудит не должен читать собственные скрипты в обход проверки
    строк. Раньше это разделение делал агент перед экспортом в окружение;
    теперь оно живёт здесь, рядом с объявлением, а не в коде потребителя.

    Реестр без метки (значение из окружения — строка) остаётся пустым, и
    capability отвечает ``registry_unavailable``: строка не умеет сказать
    «это реестр», а выдать его за доменную таблицу хуже, чем не выдать.
    """
    registry = ""
    tables: list[str] = []
    for name, label in settings.get("ENTERPRISE_AUDIT_TABLES"):
        if label == SCRIPTS_REGISTRY_LABEL:
            registry = name
        else:
            tables.append(name)
    return {
        "scripts_registry": {"table": registry},
        "audit": {
            "tables": tables,
            "row_ceiling": settings.get("ENTERPRISE_AUDIT_ROW_CEILING"),
        },
    }


def _question_runs_table(settings: Settings) -> tuple[str, str]:
    """Разобрать ``data.question_runs_table`` так же, как таблицу журнала."""
    return _split_table(
        str(settings.get("ENTERPRISE_LOG_QUESTION_RUNS_TABLE")),
        "ENTERPRISE_LOG_QUESTION_RUNS_TABLE",
    )


def _task_table(settings: Settings) -> tuple[str, str] | None:
    """Таблица очереди задач — из ``data.task_table``, без дефолта.

    Имя объявляет платформа, а не вызывающая сторона: операции очереди
    возвращены в capability ``data`` (change 2026-10-02-task-queue-into-mcp)
    именно с этим условием. Раньше ``claim_task`` и ``update_task_status``
    принимали имя таблицы в теле вызова — то есть вход в данные агента шёл
    мимо его конфигурации, и это было причиной их удаления (пункт 2.18).

    Пустое значение — не «искать таблицу по умолчанию», а «операции очереди
    не настроены»: ровно как с таблицей журнала.
    """
    raw = settings.get("ENTERPRISE_TASK_TABLE")
    if not raw or not str(raw).strip():
        return None
    return _split_table(str(raw), "ENTERPRISE_TASK_TABLE")


def _session_tables(settings: Settings) -> tuple[tuple[str, str], tuple[str, str]] | None:
    """Таблицы холодного зеркала сессий — из ``data.session_*_table``.

    Возвращает пару ``(meta, messages)`` либо ``None``, если она не настроена.
    Пара, а не две независимые настройки, потому что перезапись сессии всегда
    трогает обе таблицы: настроенная одна без второй не даёт отказа при старте,
    а разорванную запись — на первом же цикле синхронизации, то есть позже и
    в худшем месте.

    Профилем эти имена НЕ разделяются (``PROFILE_OWNED_KEYS`` их не содержит):
    зеркало хранит разговор одного пользователя, разделённый между репликами
    техническим ``replica_id``, и разведение его по контурам развело бы один и
    тот же диалог. Поэтому и в баннере профиля они не печатаются — в отличие от
    журнала, прогоны и очереди, у которых оверлей реально применяется.
    """
    meta_raw = str(settings.get("ENTERPRISE_SESSION_META_TABLE") or "").strip()
    messages_raw = str(settings.get("ENTERPRISE_SESSION_MESSAGES_TABLE") or "").strip()
    if not meta_raw or not messages_raw:
        return None
    return (
        _split_table(meta_raw, "ENTERPRISE_SESSION_META_TABLE"),
        _split_table(messages_raw, "ENTERPRISE_SESSION_MESSAGES_TABLE"),
    )


def _declared_tables(settings: Settings) -> tuple[str, ...]:
    """Таблицы, которые платформа сама объявила в ``platform.json``.

    Раньше сюда приходил список runtime-таблиц агента переменной окружения.
    Проверять чужие таблицы — значит зависеть от того, кто их назвал: список
    приезжал из чужой конфигурации, и когда у платформы появлялось своё
    объявление, старая переменная молча побеждала его. Теперь ``schema_check``
    отвечает на вопрос «на месте ли то, чем пользуется платформа», а
    runtime-таблицы агента он проверяет сам, на своём старте.
    """
    log = ".".join(_log_table(settings))
    tables = {log, ".".join(_question_runs_table(settings))}
    audit = _audit_config(settings)
    if audit["scripts_registry"]["table"]:
        tables.add(audit["scripts_registry"]["table"])
    tables.update(audit["audit"]["tables"])
    # Очередь задач — тоже объявлена платформой и ею же используется, но в
    # проверку раньше не попадала. Из-за этого профиль, перекрывший task_table,
    # проверял бы наличие боевой таблицы: оверлей применился бы, а
    # schema_check отвечал бы «таблицы на месте» про ту, в которую платформа
    # не пишет. Агент сверяет по этому списку свои имена с именами
    # платформы, поэтому отсутствующая здесь строка сделала бы сверку неполной.
    task = _task_table(settings)
    if task:
        tables.add(".".join(task))
    # Зеркало сессий — тоже объявлено платформой и ею же используется, поэтому
    # проверке подлежит. В отличие от очереди его имена профилем не
    # перекрываются, так что сверять их не с чем: проверка нужна не для
    # согласования контуров, а чтобы отсутствие таблиц обнаружилось на старте,
    # а не на первом же цикле синхронизации.
    session = _session_tables(settings)
    if session:
        tables.add(".".join(session[0]))
        tables.add(".".join(session[1]))
    return tuple(sorted(tables))


def _build_container(
    settings: Settings,
    capabilities: frozenset[str] | None = None,
) -> ToolContainer:
    """Собрать контейнер: сервисы capability и конфигурация из настроек.

    Args:
        capabilities: какие capability поднимать. ``None`` — все.
        settings: разобранные настройки, включая принятый блок агента.

    Сборка не тяжелеет сама себя: сервисы и настройки читаются только для
    отобранных capability. Это не оптимизация, а условие корректности —
    см. ``_needs_data()``.
    """
    from libs.llm.gateway import LlmGateway, set_gateway
    from servers.enterprise.capabilities.llm.service.main import LlmService

    wanted = _selected(capabilities)

    # Сервис общения с LLM — один на процесс, и он получает тот самый
    # реестр, что и всё остальное. Передача ``Settings``, а не
    # ``settings.as_env()``, преследует одну цель: у настройки должен быть
    # ровно один источник. Второй разбор того же файла рядом с реестром
    # означал бы, что «откуда взялось значение» у настройки два ответа, и
    # одна из копий рано или поздно разошлась бы с другой.
    #
    # Конфигурация LLM используется лениво, внутри сервиса. Сборка сервера
    # обязана пережить её отсутствие: процесс обслуживает несколько
    # capability, и забытый провайдер не должен снимать из работы ``data``.
    # Незаданный провайдер отдаёт ``infrastructure_error`` на своей операции
    # и виден как ``configured: false`` в баннере ниже.
    set_gateway(LlmGateway(settings=settings))
    services: dict[str, Any] = {"llm": LlmService()}
    # ``legal_summarizer`` не читает снимок и не ходит в БД: состояние
    # операции лежит на диске и объявлено самим доменом. Поэтому сервис
    # собирается здесь, до раннего возврата для capability без данных, -
    # иначе ``--capabilities legal_summarizer`` поднимал бы пустой сервер.
    from servers.enterprise.capabilities.legal_summarizer.service.main import (
        LegalSummarizerService,
    )

    services["legal_summarizer"] = LegalSummarizerService(settings=settings)
    # Объявления собираются здесь и передаются сервисам в конструктор.
    # Контейнер их не хранит: у значения должен быть один владелец, и им
    # является тот, кто это значение использует.
    config: dict[str, Any] = {}

    if not _needs_data(wanted):
        # Единственная capability, которой не нужен доступ к данным.
        # Ни пула, ни снимка, ни настроек журнала: см. ``_needs_data``.
        return ToolContainer(services=services)

    from servers.enterprise.capabilities.audit.service.main import AuditService
    from servers.enterprise.capabilities.data.service.main import DataService
    from servers.enterprise.capabilities.vectors.service.main import VectorsService

    statement_timeout_ms = int(settings.get("ENTERPRISE_STATEMENT_TIMEOUT_MS"))
    max_rows = int(settings.get("ENTERPRISE_MAX_ROWS"))
    session_tables = _session_tables(settings)
    data = DataService(
        log_table=_log_table(settings),
        question_runs_table=_question_runs_table(settings),
        task_table=_task_table(settings),
        session_meta_table=session_tables[0] if session_tables else None,
        session_messages_table=session_tables[1] if session_tables else None,
        expected_tables=_declared_tables(settings),
        statement_timeout_ms=statement_timeout_ms,
        max_rows=max_rows,
        buffer_maxlen=int(settings.get("ENTERPRISE_LOG_BUFFER_MAXLEN")),
        buffer_flush_interval=float(settings.get("ENTERPRISE_LOG_FLUSH_INTERVAL")),
        # Правило очистки журнала — платформенное: агент свою копию журнала
        # больше не ведёт, и решение «сколько живёт запись» принимает сервер.
        log_retention_days=int(settings.get("ENTERPRISE_LOG_RETENTION_DAYS")),
        purge_empty_outbound=bool(settings.get("ENTERPRISE_LOG_PURGE_EMPTY_OUTBOUND")),
        # Порог журнала платформы — от агента, из того же ключа его
        # конфигурации, что и у его писателя. Настройкой платформы он не
        # является: объявлять его ещё и в ``platform.json`` значило бы
        # завести второе место, и вопрос «каким уровнем пишется журнал» получил
        # бы два ответа, разъехавшихся молча.
        min_level=resolved_journal_min_level(settings),
        snapshot=_snapshot(settings),
    )
    services["data"] = data
    config = {
        "statement_timeout_ms": statement_timeout_ms,
        "max_rows": max_rows,
        **_vectors_config(settings),
        **_audit_config(settings),
    }
    container = ToolContainer(services=services)
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


#: Capability, которой не нужен доступ к данным. Полный набор — всё, что
#: описано в ``servers/enterprise/capabilities``.
_ALL_CAPABILITIES = frozenset(
    {"audit", "data", "legal_summarizer", "llm", "vectors"}
)
#: Capability, читающие снимок или очередь. Их нельзя поднимать без ``data``.
_DATA_CAPABILITIES = frozenset({"audit", "data", "vectors"})


def _selected(capabilities: frozenset[str] | None) -> frozenset[str]:
    """Нормализовать отбор capability и отсеять опечатки."""
    if capabilities is None:
        return _ALL_CAPABILITIES
    unknown = set(capabilities) - _ALL_CAPABILITIES
    if unknown:
        raise InfrastructureError(
            f"неизвестные capability: {', '.join(sorted(unknown))}. "
            f"Доступны: {', '.join(sorted(_ALL_CAPABILITIES))}"
        )
    return frozenset(capabilities)


def _needs_data(wanted: frozenset[str]) -> bool:
    """Нужен ли в этом процессе доступ к данным.

    Считается по запрошенному набору, а не по факту регистрации: пока
    ``data`` не в списке, процесс не трогает ни DSN, ни пул, ни файл
    снимка. Это ровно то, что делает второй экземпляр сервера
    безопасным — он не становится вторым владельцем PostgreSQL и вторым
    держателем блокировки на файле DuckDB.
    """
    return bool(wanted & _DATA_CAPABILITIES)


def _snapshot(settings: Settings) -> Any:
    """Открыть снимок DuckDB, если оператор его задал. Иначе — ``None``.

    Снимок **не обязателен**. Требование снимка при старте означало бы, что
    сервер перестаёт подниматься там, где capability ``vectors`` не
    развёрнут, — вместе с работающим ``history_search``. Поэтому ошибка
    открытия (занятый файл, битый файл, неподдерживаемая ФС) не
    поднимается наружу: она логируется, снимок остаётся неподключённым.

    Занятый файл — не экзотика: ``READ_WRITE``-сессия загрузчика исключает
    читателей во всех процессах, а agent и ``enterprise-mcp`` — два разных
    процесса. Без перехвата падал бы весь сервер из-за снимка, к которому
    половина его операций отношения не имеет.

    Без переменной, как и при ошибке открытия, снимок отдаётся не как
    ``None``, а как :class:`UnavailableSnapshot` — с той же причиной и её
    кодом на каждой операции. «Снимок недоступен» без причины не диагноз:
    читателю нужен конкретный ответ — не задан путь, файл держит другой
    процесс или файл не открывается, — а не «индексов нет».

    Принимается именно **путь к файлу**, а не каталог: у агента путь
    считается единственным механизмом ``resolve_cache_path()``, и требовать
    от него ещё и разбирать свой путь обратно на каталог значило бы завести
    второе место, где решается, где лежит снимок. Форма «каталог» остаётся в
    платформе для standalone-утилит (``resolve_snapshot_path``), но не как
    вторая переменная окружения.
    """
    from libs.enterprise_data.snapshot import (
        CacheAccessMode,
        open_snapshot_store,
        resolve_snapshot_setting,
    )
    from libs.enterprise_data.snapshot.unavailable import UnavailableSnapshot

    # ``~`` разворачивается здесь, а не в реестре: правило касается только
    # пути снимка, и общее «разворачивать ``~`` во всех настройках» задело бы
    # секреты, где ведущая ``~`` — обычный символ пароля.
    path = resolve_snapshot_setting(
        settings.get("ENTERPRISE_SNAPSHOT_PATH"), "ENTERPRISE_SNAPSHOT_PATH"
    )

    if not path:
        return UnavailableSnapshot(
            "снимок не настроен: ENTERPRISE_SNAPSHOT_PATH пуст — путь к файлу "
            "кэша не задан оператором"
        )

    try:
        return open_snapshot_store(
            path,
            CacheAccessMode.READ_ONLY,
            vector_db_table=str(settings.get("ENTERPRISE_VECTOR_STORAGE_TABLE")),
        )
    except InfrastructureError as exc:
        # Причина и её код (``cache_busy``, ``cache_open_error``) доезжают до
        # клиента без искажений — см. ``UnavailableSnapshot``. Процесс при
        # этом поднимается: ``vectors`` и ``audit`` от снимка не зависят.
        logger.warning("снимок недоступен, сервер поднимается без него: %s", exc)
        return UnavailableSnapshot(
            str(exc),
            code=getattr(exc, "code", "infrastructure_error"),
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
    # Реестр уже привёл объявления к объекту (тип ``json``): из файла они
    # приходят как есть, из окружения разбираются с именем настройки в
    # ошибке. Разбирать JSON здесь второй раз нечего.
    indexes = dict(settings.get("ENTERPRISE_VECTOR_INDEXES"))

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


def _split_table(raw: str, setting_name: str) -> tuple[str, str]:
    """Разобрать ``'<schema>.<table>'``.

    Без fallback-литерала: настройка обязательна (``FROM_FILE``), пустое или
    неоднозначное значение — ошибка конфигурации, а не молчаливое имя.
    """
    text = raw.strip()
    schema, _, table = text.partition(".")
    if not schema or not table:
        raise InfrastructureError(
            f"{setting_name} должен быть в виде '<schema>.<table>', "
            f"получено {text!r}"
        )
    return schema, table


def _log_table(settings: Settings) -> tuple[str, str]:
    """Таблица долговечного журнала."""
    return _split_table(
        str(settings.get("ENTERPRISE_LOG_TABLE")), "ENTERPRISE_LOG_TABLE"
    )


def build(
    capabilities: frozenset[str] | None = None,
    profile: str | None = None,
    agent_settings_path: Path | None = None,
    *,
    settings: Settings | None = None,
) -> tuple[Any, ToolRegistry, ToolContainer]:
    """Собрать сервер: настройки, пул, сервисы, реестр, транспорт.

    Args:
        capabilities: поднять только эти capability. ``None`` — все.
        profile: имя профиля из ``platform.json → profiles`` (``None`` — база,
            то есть prod). Имя присылает агент: он знает, в каком контуре
            запущен. Значения имён таблиц при этом не приходят — перекрывает
            объявление платформы (:func:`read_profile_overlay`).
        agent_settings_path: файл блока настроек агента, путь приходит
            аргументом запуска (:data:`AGENT_SETTINGS_FILE_FLAG`). ``None`` —
            блок не передан, и настройки агента не применяются.
        settings: уже разобранные настройки. Нужны ``main``: блок читает он
            же (по нему выбирается транспорт), и второй :class:`Settings`
            означал бы двух независимых читателей одного файла. ``None`` —
            разобрать самому из ``profile`` и ``agent_settings_path``, как это
            делают все прежние вызовы.

    Возвращает ``(server, registry, container)`` — чтобы тест мог проверить
    реестр, не поднимая транспорт.

    Зачем отбор: потребителю LLM вне агента (skill-конвейер в отдельном
    процессе) нужен только сервис общения с моделью. Второй экземпляр со
    всеми capability стал бы вторым владельцем пула PostgreSQL и вторым
    держателем блокировки на файле снимка DuckDB, поэтому он поднимает
    ``llm`` и ничего больше — см. :func:`_needs_data`.
    """
    wanted = _selected(capabilities)
    # Блок читается и проверяется здесь, один раз, до сборки сервисов: отказ
    # на неизвестном ключе должен остановить подъём до того, как что-либо
    # схватит настройки. ``main`` приносит сюда свой разобранный экземпляр —
    # блок один, и читать его дважды означало бы два независимых ответа на
    # вопрос, что агент объявил.
    settings = settings or Settings(profile=profile, agent_settings_path=agent_settings_path)
    if _needs_data(wanted):
        _check_dependencies(settings)
        _configure_dsn(settings)
        _apply_pool_settings(settings)
    container = _build_container(settings, wanted)
    registry = load_registry(
        CAPABILITIES_DIR, container, root=PLATFORM_ROOT, capabilities=wanted
    )
    # Слой исполнения собирается после реестра и получает приёмником журнала
    # сервис ``data``: второй писатель событий означал бы, что журнал читают
    # двое, а порядок записей определяет тот, кто быстрее.
    #
    # Порог журнала — тот же, что и у ``DataService`` выше, и берётся из того
    # же слоя разрешённых настроек: два писателя процесса режут по одному
    # правилу, а не по двум. Значение приходит блоком агента
    # (``--agent-settings-file``), и ни одна из половин не имеет своего.
    execution = build_execution_layer(
        settings,
        sink=_event_sink(container),
        session_root=PLATFORM_ROOT / _session_root(settings),
    )
    # Платформенная операция чтения сохранённого результата. Здесь, а не через
    # загрузчик каталогов: файлами сессии владеет платформа, и страж
    # ``tests/test_tool_execution_boundaries.py`` не пускает к ним capability —
    # у операции из каталога появился бы второй каталог файлов одной сессии.
    # Хранилище берётся у слоя исполнения и в контейнер не кладётся, поэтому
    # второго пути к файлам сессии не появляется ни у кого.
    #
    # Условие — тот же ``_needs_data``, что и у сервиса ``data``: сервер для
    # скиллов поднимается коротким подпроцессом ради одной операции LLM, у
    # него нет ни оборота, ни сессии, и читать в них нечего.
    if _needs_data(wanted) and execution.artifacts is not None:
        from servers.enterprise.tools.read_result import create_tool as _read_result

        registry.register(
            _read_result(execution.artifacts, page_chars=execution.policy.preview_bytes)
        )
    # Платформенная операция ``session_files``: отдаёт агенту каталог его
    # сессии и раскладку подкаталогов. Здесь по той же причине, что и
    # ``read_result`` выше: файлами сессии владеет платформа, и путь к ним
    # достаётся слою исполнения, а не контейнеру capability.
    #
    # Условие то же: сервер для скиллов (``--capabilities llm``) поднимается
    # ради одной операции LLM, у него нет сессии, и каталога ей не выдавать.
    if _needs_data(wanted) and execution.workspace is not None:
        from servers.enterprise.tools.session_files import create_tool as _session_files

        registry.register(_session_files(execution.workspace))
    transport = build_server(
        registry,
        # Имя несёт контур: по адресу может отвечать чужой процесс, и
        # доказать, что сервер наш, можно только его собственным
        # ``serverInfo`` из рукопожатия. На stdio это не проверяется, там
        # протокол на stdout и поднятый процесс известен по построению.
        name=_server_identity_name(profile),
        pipeline=execution.pipeline,
        instructions=(
            "Enterprise-слой проекта. Операции данных, векторов, аудита и LLM. "
            "Операций произвольного SQL здесь нет и не будет."
        ),
    )
    from_file = settings.file_backed()
    if from_file:
        logger.info("настройки из platform.json: %s", ", ".join(from_file))
    if profile:
        # Без этой строки не видно, в каком контуре пишет платформа: имена
        # таблиц одинаково выглядят в platform.json, а различаются только
        # оверлеем профиля.
        logger.info(
            "профиль=%s: журнал=%s прогоны=%s очередь=%s",
            profile,
            settings.get("ENTERPRISE_LOG_TABLE"),
            settings.get("ENTERPRISE_LOG_QUESTION_RUNS_TABLE"),
            settings.get("ENTERPRISE_TASK_TABLE"),
        )
    _log_journal_min_level(container)
    # Что пришло из блока агента и какие ключи применены. Отдельная строка от
    # «порог журнала» ниже: та показывает значение, применяемое писателем,
    # эта — источник и состав блока, включая блок, которого не было.
    logger.info("настройки агента: %s", agent_settings_summary(settings))
    if wanted != _ALL_CAPABILITIES:
        logger.info("подняты только capability: %s", ", ".join(sorted(wanted)))
    _log_llm_settings(container.get("llm"))
    _log_execution_settings(execution)
    logger.info("операций загружено: %d", len(registry))
    for name in registry.names():
        logger.info("  операция: %s", name)
    return transport, registry, container


def _event_sink(container: Any) -> Any | None:
    """Приёмник событий журнала — неблокирующий вход сервиса ``data``.

    Доступ именно к ``container.services``, а не через ``container.get()``:
    ``get()`` по отсутствующему ключу бросает ``InfrastructureError`` (сервис не
    зарегистрирован — это сбой сборки), а сервер **без** capability ``data``
    (режим «только LLM» для скиллов) — не сбой. Там приёмника просто нет,
    писатель считает отказы и не пишет никуда.
    """
    data = container.services.get("data")
    accept = getattr(data, "accept", None)
    return accept if callable(accept) else None


def _session_root(settings: Settings) -> str:
    """Каталог файлов сессий из ``platform.json``.

    Объявлено ``${NANOBOT_WORKSPACE}/data_store/sessions``, и подстановка
    разворачивается в абсолютный путь заранее — склейка с корнем платформы
    абсолютный путь не меняет.

    Объявление обязательно, и запасного пути здесь нет намеренно: значение по
    умолчанию означало бы, что каталог сессий оказался не тем, чем объявлен, и о
    потере файлов узнали бы по отсутствию артефактов — уже после инцидента.
    """
    value = str(settings.get("ENTERPRISE_EXEC_SESSION_ROOT") or "").strip()
    if not value:
        raise InfrastructureError(
            "не объявлен корень файлов сессий. Задайте его в "
            "mcp-platform/platform.json (секция execution, ключ session_root, "
            "например ${NANOBOT_WORKSPACE}/data_store/sessions) — переменная "
            "экспортируется агентом при старте. Без него каталоги сессий, "
            "крупные результаты и артефакты писать некуда"
        )
    return value


def _log_journal_min_level(container: Any) -> None:
    """Показать, каким порогом платформа пишет журнал.

    Отдельная строка, а не часть баннера профиля: порог присылает агент и он
    один на обе половины журнала, поэтому «применён ли он» — вопрос к
    стартовому логу, а не к конфигурации платформы. Значение берётся у
    писателя, а не из флага: строка должна показывать то, что действительно
    применяется, иначе неизвестный уровень, тихо упавший в дефолт, был бы
    виден как заданный.

    Отсутствие порога — не ошибка и не «дефолт INFO»: флага может не быть
    (сервер поднят без агента, тест), и тогда платформа пишет всё, как
    сейчас. Отдельная формулировка нужна, чтобы это не читалось как
    «применён INFO».
    """
    data = container.services.get("data")
    stats = getattr(data, "stats", None)
    min_level = stats()["min_level"] if callable(stats) else None
    logger.info(
        "порог журнала: %s",
        min_level if min_level else "не задан — пишем всё",
    )


def _log_execution_settings(execution: Any) -> None:
    """Показать пороги и флаги слоя исполнения.

    Те же соображения, что у баннера LLM: «применяется 64 КиБ» и «в коде 64
    КиБ» — разные утверждения, и проверить можно только первое.
    """
    stats = execution.stats()
    policy = stats.get("execution_policy", {})
    logger.info(
        "исполнение: порог=%s байт, превью=%s байт, таймаут=%s с, крупный=%s, "
        "качество=%s, журнал=%s, файлы событий=%s, требуется _meta=%s",
        policy.get("max_inline_result_bytes"),
        policy.get("preview_bytes"),
        policy.get("execution_timeout_sec"),
        "артефакт" if policy.get("persist_large_results") else "целиком",
        "вкл" if policy.get("quality_check_enabled") else "выкл",
        "вкл" if policy.get("logging_enabled") else "выкл",
        "вкл" if policy.get("persist_session_events") else "выкл",
        "строго" if policy.get("require_call_meta") else "совместимо",
    )
    if not policy.get("require_call_meta"):
        logger.warning(
            "execution.require_call_meta = false: вызов без params._meta принимается, "
            "идентичность берётся из arguments. Это переходное окно — переключите "
            "флаг в platform.json, когда вызывающая сторона перейдёт на meta="
        )


def _log_llm_settings(llm: Any) -> None:
    """Показать, чем сервис общения с моделью настроен. Без ключа.

    Молчание здесь стоило бы дороже строки лога: «модель не задана» и
    «модель задана, но перекрыта переменной окружения» выглядят снаружи
    одинаково — процесс поднялся, ``data`` работает, а ``generate_sql``
    отвечает ``infrastructure_error``. Строка баннера отвечает на вопрос
    «настроен ли сервис» до первого вызова, а не после.
    """
    if llm is None:
        logger.info("LLM: сервис не зарегистрирован — capability llm недоступна")
        return
    info = llm.describe()
    if not info.get("configured"):
        logger.warning(
            "LLM: не настроен (в platform.json нет llm.model / llm.api_base) — "
            "операции complete/embed отдадут infrastructure_error",
        )
        return
    logger.info(
        "LLM: провайдер=%s модель=%s адрес=%s ключ=%s max_tokens=%s temperature=%s",
        info.get("provider"),
        info.get("model"),
        info.get("api_base"),
        "задан" if info.get("key_configured") else "НЕ ЗАДАН",
        info.get("max_tokens"),
        info.get("temperature"),
    )
    embed = info.get("embed") or {}
    logger.info(
        "LLM: эмбеддер — модель=%s адрес=%s ключ=%s",
        embed.get("model"),
        embed.get("api_base"),
        "задан" if embed.get("key_configured") else "не задан",
    )


def _warm_heavy_imports() -> None:
    """Прогреть ``numpy``/``pandas`` до старта event loop.

    DuckDB тянет их лениво, на первом же ``execute` **с параметрами**:
    замерено — ``execute`` без параметров не добавляет в ``sys.modules``
    ничего, а с параметрами добавляет оба пакета (0,6 с) и ещё 3,4 с
    уходит на ``fetchall``. Обработчики операций выполняются в воркерах
    AnyIO, а импорт numpy из не-main-потока на Windows **зависает
    намертво**: операция не падает, а перестаёт отвечать навсегда (проверено
    на живом процессе — больше 180 с вместо 0,2 с, стек стоял в
    ``numpy/_core/multiarray.py``).

    Поэтому тяжёлое поднимается на старте, а не посреди оборота: так же,
    как сам процесс сервера. Суммарно меньше секунды один раз за процесс.

    Отказ импорта не роняет сервер: DuckDB умеет работать без pandas, и
    отсутствие пакета — не причина отказывать в подъёме. Предупреждение в
    лог означает лишь, что первый такой запрос снова заплатит за импорт.
    """
    import time

    for name in ("numpy", "pandas"):
        started = time.monotonic()
        try:
            __import__(name)
        except Exception as exc:  # noqa: BLE001 - прогрев не должен ронять старт
            logger.warning("прогрев %s не удался: %s", name, exc)
            continue
        logger.info("прогрет %s за %.1fс", name, time.monotonic() - started)


def _prepare_capabilities(container: Any) -> None:
    """Довести capability до готового состояния **до** старта event loop.

    Инвариант: на старте сервера все операции доступны быстро, ленивых
    загрузок на горячем пути нет. Иначе первая же операция платит за чужую
    подготовку, и это выглядит как «индексы не ищутся» — дефект там, где
    его нет.

    Что именно готовится и почему:

    * **реестр скриптов** (``audit.list_scripts``) — единственная проверка
      того, что снимок не просто открывается, а отдаёт данные. Пока она не
      прошла, про снимок сказать нечего: он может быть доступен и пуст.
    * **векторные индексы** (``vectors``) — ``ensure_index`` для каждого
      объявленного индекса. Сборка FAISS ленивая по построению: без этого
      шага она случалась на первом же поиске, и на большом снимке это
      секунды молчания на горячем пути.

    Отказ не роняет сервер: снимок по контракту необязателен (``OPTIONAL``),
    и подниматься без него — законное состояние. Но отказ **называется** в
    логе и виден в ответе операций (``list_indexes``, ``index_stats``,
    ``list_scripts``), поэтому «не работает» и «не настроено» не путаются.

    ``None``-сервис пропускается: процесс ``--capabilities llm`` поднимает
    один сервис, и отсутствие остальных — его норма, а не дефект.
    """
    import time

    audit = container.services.get("audit")
    if audit is not None:
        started = time.monotonic()
        try:
            catalog = audit.list_scripts()
        except Exception as exc:  # noqa: BLE001 - причина уходит в лог целиком
            logger.error("реестр скриптов недоступен на старте: %s", exc)
        else:
            logger.info(
                "аудит: %s скриптов в реестре, снимок читается (%.1fс)",
                (catalog or {}).get("count", "?"),
                time.monotonic() - started,
            )

    vectors = container.services.get("vectors")
    if vectors is None:
        return
    try:
        declared = [
            item["index_name"]
            for item in vectors.list_indexes()
            if item.get("declared") and item.get("index_name")
        ]
    except Exception as exc:  # noqa: BLE001 - состояние и так проверяется ниже
        logger.error("не удалось перечислить объявленные индексы: %s", exc)
        return

    for name in declared:
        started = time.monotonic()
        try:
            vectors.owner.ensure_index(name)
        except Exception as exc:  # noqa: BLE001 - состояние индекса уже записано владельцем
            logger.error("индекс %r не собран на старте: %s", name, exc)
            continue
        state = vectors.state(name)
        logger.info(
            "индекс %r на старте: %s (%.1fс)",
            name,
            state,
            time.monotonic() - started,
        )


def _server_identity_name(profile: str | None) -> str:
    """Имя сервера в MCP-рукопожатии — вместе с контуром.

    ``enterprise-mcp`` для базы (prod) и ``enterprise-mcp:<контур>`` для
    профиля. Контур приходит флагом ``--profile`` от агента: он знает, в
    каком контуре запущен, и без этого ответа платформа писала бы в боевые
    таблицы под тестовым профилем.

    Имя — единственное место, где сервер называет себя, и агент сверяет
    его со своим объявлением. Поэтому здесь не проходит ни одна настройка
    из блока: подставленное по шаблону имя проверило бы шаблон.
    """
    return f"enterprise-mcp:{profile}" if profile else "enterprise-mcp"


def main(argv: list[str] | None = None) -> None:
    """Поднять сервер по stdio или streamable-http. Точка входа агента.

    Args:
        argv: аргументы командной строки; ``None`` — ``sys.argv[1:]``.

    ``--capabilities llm`` поднимает только сервис общения с моделью — для
    потребителей вне агента (skill-конвейер в отдельном процессе). Такой
    процесс не трогает ни PostgreSQL, ни файл снимка, поэтому он не может
    стать их вторым владельцем.

    Транспорт выбирает **блок настроек агента**, а не аргумент запуска: порт
    и адрес — передаваемые настройки, и доставка у них одна. Разбор argv
    повторяется здесь трижды и это дёшево: он чистый, без ввода-вывода.
    Читается при этом один раз сам блок — ``build`` получает тот же
    :class:`Settings`, что и разбор транспорта.
    """
    import anyio
    from mcp.server.stdio import stdio_server

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    settings = Settings(
        profile=_profile_from_argv(argv),
        agent_settings_path=_agent_settings_path_from_argv(argv),
    )
    request = http_transport.requested_transport(settings)
    transport, _, container = build(
        _capabilities_from_argv(argv),
        profile=_profile_from_argv(argv),
        agent_settings_path=_agent_settings_path_from_argv(argv),
        settings=settings,
    )
    # ``services``, а не ``get``: у процесса без ``data`` такого сервиса
    # нет, и строгий ``get`` уронил бы старт с «сервис не зарегистрирован»
    # вместо того, чтобы он просто обслуживал capability ``llm``.
    data = container.services.get("data")
    if data is not None:
        data.start()
    # До ``anyio.run``: воркеры AnyIO не должны платить за первый импорт
    # numpy/pandas — из них он зависает (см. _warm_heavy_imports), а сборка
    # индексов и чтение реестра обязаны случиться до первого запроса.
    _warm_heavy_imports()
    _prepare_capabilities(container)
    try:
        async def _serve() -> None:
            if request.mode == http_transport.MODE_HTTP:
                await http_transport.serve_http(transport, request)
                return
            async with stdio_server() as (read_stream, write_stream):
                await transport.run(
                    read_stream,
                    write_stream,
                    transport.create_initialization_options(),
                )

        anyio.run(_serve)
    finally:
        if data is not None:
            data.stop()


def _capabilities_from_argv(argv: list[str] | None) -> frozenset[str] | None:
    """Разобрать ``--capabilities a,b``; без флага — все capability.

    Ручной разбор вместо ``argparse``: входная точка одна и живёт в
    потоке, где ``sys.argv`` принадлежит не серверу, и лишняя зависимость
    ради трёх строк разбора тут не окупается.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    wanted: list[str] = []
    for index, arg in enumerate(args):
        if arg == "--capabilities" and index + 1 < len(args):
            wanted.extend(part.strip() for part in args[index + 1].split(",") if part.strip())
        elif arg.startswith("--capabilities="):
            wanted.extend(
                part.strip() for part in arg.split("=", 1)[1].split(",") if part.strip()
            )
    return frozenset(wanted) if wanted else None


def _profile_from_argv(argv: list[str] | None) -> str | None:
    """Разобрать ``--profile <имя>``; без флага — база (prod).

    Имя присылает агент: он единственный знает, в каком контуре запущен, и
    без него платформа писала бы в боевые таблицы под тестовым профилем.
    Значения имён таблиц при этом не приходят — они объявлены в
    ``platform.json → profiles``.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    for index, arg in enumerate(args):
        if arg == "--profile" and index + 1 < len(args):
            return args[index + 1].strip() or None
        if arg.startswith("--profile="):
            return arg.split("=", 1)[1].strip() or None
    return None


def _agent_settings_path_from_argv(argv: list[str] | None) -> Path | None:
    """Разобрать ``--agent-settings-file <путь>``; без флага — блока нет.

    Ручной разбор по той же причине и в том же виде, что у ``--profile`` и
    ``--capabilities``: входная точка одна и живёт в потоке, где ``sys.argv``
    принадлежит не серверу, а лишняя зависимость ради трёх строк разбора не
    окупается.

    Флаг объявляет **путь**, а не значение: значение лежит в файле, и
    командная строка видна всем, кто может прочитать список процессов.

    Returns:
        Путь к файлу блока либо ``None`` — флага не было.

    Raises:
        InfrastructureError: флаг объявлен, а значения при нём нет. Пустой
            путь развернулся бы в текущий каталог, и платформа прочитала бы
            чужой файл вместо блока.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    for index, arg in enumerate(args):
        if arg == AGENT_SETTINGS_FILE_FLAG and index + 1 < len(args):
            value = args[index + 1].strip()
        elif arg.startswith(f"{AGENT_SETTINGS_FILE_FLAG}="):
            value = arg.split("=", 1)[1].strip()
        else:
            continue
        if not value:
            raise InfrastructureError(
                f"{AGENT_SETTINGS_FILE_FLAG}: не задано значение. Флаг "
                "объявляет путь к файлу блока настроек агента; пустой путь "
                "развернулся бы в текущий каталог."
            )
        return Path(value)
    return None


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    main()
