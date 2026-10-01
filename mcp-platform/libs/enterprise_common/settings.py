"""Реестр настроек платформы: что вообще можно настроить и кто это читает.

Зачем файл реестра, если настройки и так приходят через окружение
--------------------------------------------------------------------

Настройки платформы приходили через 28 имён переменных, прочитанных тремя
разными способами: литерально в ``servers/enterprise/server.py`` (по пяти
функциям, пять из них — инлайном в ``_build_container``), ещё десять
описаны декларативной таблицей ``_ENV_NAMES`` в ``libs/llm/config.py``,
DSN — через ``resolve_dsn()``. Перечислить настройки, не открыв три
файла, было нельзя.

Сейчас разбор **один**: ``Settings`` — единственный, кто читает окружение
и ``platform.json``, и единственный, кто владеет дефолтами. Остальной код
получает уже разрешённое значение, поэтому ``server.py`` не обращается к
``os.environ`` вовсе. Пока это было не так, файл с настройками читался
только в тестах, а на процессе действовали дефолты из кода: значения в
``platform.json`` выглядели рабочими и не влияли ни на что.

Два свойства, из-за которых реестр и нужен:

1. **Владелец.** По части значений у платформы есть мнение, по части
   его быть не должно: выбор модели и ключи — дело агента, а размер
   буфера журнала, размер пула и таймаут запроса — дело платформы. Это
   разные вещи, и в одном списке они неразличимы.
2. **Место, где живут значения без агента.** Свои ручки платформы
   существуют только как дефолты в коде. Задать их без агента можно
   только файлом, и перечислить их — тоже негде.

Структура реестра повторяет код
-------------------------------

Настройки сгруппированы по capability, и у каждой capability названы её
``service`` и её операции — ровно как они устроены в
``servers/enterprise/capabilities/``. Причина практическая: вопрос «кто читает
эту настройку» должен отвечать на вопрос про потребителя, а не про
``_build_container``, который только собирает сервисы. Первая версия реестра
была плоским списком и отвечала именно на второй вопрос.

Страж сверяет дерево с диском, поэтому capability без сервиса, операция,
которой нет в ``tools/``, и настройка без владельца падают в тестах, а не
при разборе инцидента.

Приоритет значений
------------------

``окружение > файл > дефолт``
--------------------------------

Окружение остаётся старшим намеренно. Пока агент экспортирует значения в
дочерний процесс, файл не может ничего сломать: всё, что работает
сегодня, продолжит работать. Второй, нижний слой вводится первым, и
только потом агент перестаёт экспортировать платформенные значения — по
одному, с проверкой на каждом шаге. Обратный порядок (сначала убрать
экспорт, потом добавить файл) оставил бы развёртывание без значения
после неудачного деплоя.

Владелец настройки
------------------

``owner="platform"`` — у платформы есть мнение, и оно живёт в
``platform.json``. ``owner="agent"`` — решение агента (модель, ключ,
адрес, путь снимка, пока снимок грузит агент), значение приходит через
окружение и в файл не попадает никогда.

Настройки вне capability
-----------------------

Пул соединений нужен всем capability сразу и ни одной из них не
принадлежит, поэтому он объявлен в ``SHARED_SETTINGS`` и лежит в файле в
собственной секции ``pool``, а не в секции какой-нибудь capability. Секции
перечислены в ``SHARED_SECTIONS``, и страж сверяет файл именно с этим
списком: секция, которой нет ни в capability, ни в ``SHARED_SECTIONS``, —
это опечатка, которая выглядела бы как «настройка прочитана».

Дефолты пула
------------

``_DEFAULT_POOL`` в ``libs/enterprise_data/db.py`` **выводится** из этого
реестра (``pool_defaults()``), а не объявлен рядом с ним. Два списка
дефолтов разъезжаются при первом же изменении: пул работал бы на одних
значениях, а реестр обещал бы оператору другие.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

#: Плохой файл настройки — это проблема развёртывания, а не плохой запрос
#: вызывающей стороны, поэтому InfrastructureError, а не InvalidRequestError.
#: Тот же класс, что и у «не задан DSN».
from libs.enterprise_common.errors import InfrastructureError

#: Корень платформы — родитель ``libs``.
PLATFORM_ROOT = Path(__file__).resolve().parent.parent.parent

#: Файл настроек платформенной части. Лежит рядом с ``pyproject.toml`` и
#: намеренно НЕ в каталоге ``config/``: такое имя перебило бы модуль
#: ``config`` агента, который платформе импортировать запрещено.
PLATFORM_CONFIG_PATH = PLATFORM_ROOT / "platform.json"

OWNER_PLATFORM = "platform"
OWNER_AGENT = "agent"


@dataclass(frozen=True)
class Setting:
    """Одна настройка: как её зовут, чем она является и кто её читает.

    Attributes:
        name: основное имя переменной окружения.
        aliases: дополнительные имена, если основное не задано.
        kind: ``int`` | ``float`` | ``bool`` | ``str`` | ``list`` | ``secret``.
        default: значение при отсутствии в окружении и файле. ``None``
            означает «без значения», а не «пустая строка».
        owner: ``platform`` — настройка живёт в ``platform.json``;
            ``agent`` — приходит из окружения, в файле ей не место.
        reader: кто читает; приводится в тексте настройки, чтобы при
            поиске «кто это трогает» не пришлось открывать все файлы.
        purpose: зачем настройка существует, в одну строку.
        required: без значения и без дефолта — операция обязана падать с
            внятной ошибкой, а не работать вслепую.
    """

    name: str
    kind: str
    default: Any
    owner: str
    reader: str
    purpose: str
    aliases: tuple[str, ...] = ()
    required: bool = False
    file_key: str = ""
    """Ключ вложенной секции ``platform.json`` (например ``data.log_table``).

    Человек пишет ``{"data": {"log_table": ...}}``, а окружение получает
    ``ENTERPRISE_LOG_TABLE``. Связь между ними живёт здесь, а не в читателе
    и не в файле: иначе пришлось бы угадывать имя переменной по имени
    секции, и опечатка стала бы молчаливой потерей настройки.
    """

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    @property
    def key(self) -> str:
        return self.file_key or self.name

    @property
    def is_secret(self) -> bool:
        return self.kind == "secret"

    def coerce(self, raw: str) -> Any:
        """Привести строку из окружения или файла к типу настройки.

        Raises:
            InfrastructureError: значение не приводится к объявленному
                типу. Молчаливый откат к дефолту выглядел бы как «настройка
                не применилась», и оператор искал бы не там.
        """
        text = raw.strip()
        if self.kind in ("int", "float", "bool", "list"):
            if not text:
                return self.default
        try:
            if self.kind == "int":
                return int(text)
            if self.kind == "float":
                return float(text)
            if self.kind == "bool":
                return text.lower() in ("1", "true", "yes", "on")
            if self.kind == "list":
                # Перевод строки — разделитель наравне с запятой: агент
                # экспортирует списки таблиц по одной на строку, и раньше
                # их читал разбор, который понимал оба. Забытая здесь
                # запятая склеила бы семь таблиц в одно имя — молча.
                return [
                    item.strip()
                    for item in text.replace("\n", ",").split(",")
                    if item.strip()
                ]
            if self.kind == "secret":
                return text
            return text
        except ValueError as exc:
            raise InfrastructureError(
                f"{self.name}: {raw!r} не приводится к типу {self.kind!r}"
            ) from exc


def _s(
    name: str,
    kind: str,
    default: Any,
    owner: str,
    reader: str,
    purpose: str,
    *,
    aliases: tuple[str, ...] = (),
    required: bool = False,
    file_key: str = "",
) -> Setting:
    return Setting(
        name=name,
        kind=kind,
        default=default,
        owner=owner,
        reader=reader,
        purpose=purpose,
        aliases=aliases,
        required=required,
        file_key=file_key,
    )


#: Полный перечень настроек платформы. Единственный список: и документация,
#: и страж сверяются с ним, а не с копией в тесте.
SETTINGS: tuple[Setting, ...] = (
    # -- подключение к базе -------------------------------------------------
    _s("DATABASE_URL", "secret", None, OWNER_AGENT,
       "libs/enterprise_data/db.py:resolve_dsn",
       "DSN рабочей базы; пусто — воркер падает внятно, а не подключается не туда",
       aliases=("PG_DSN",), required=True),
    # -- capability audit ----------------------------------------------------
    _s("ENTERPRISE_SCRIPTS_REGISTRY_TABLE", "str", "", OWNER_AGENT,
       "servers/enterprise/server.py:_audit_config",
       "таблица реестра предустановленных скриптов; пусто — registry_unavailable",
       required=True),
    _s("ENTERPRISE_AUDIT_TABLES", "list", (), OWNER_AGENT,
       "servers/enterprise/server.py:_audit_config",
       "белый список таблиц для аудита; реестр скриптов сюда не входит"),
    _s("ENTERPRISE_AUDIT_ROW_CEILING", "int", 0, OWNER_AGENT,
       "servers/enterprise/server.py:_audit_config",
       "потолок строк для generate_sql; 0 — не применять"),
    # -- capability vectors --------------------------------------------------
    _s("ENTERPRISE_SNAPSHOT_PATH", "str", "", OWNER_AGENT,
       "servers/enterprise/server.py:_snapshot",
       "путь к файлу снимка DuckDB; пусто — снимок не обязателен"),
    _s("ENTERPRISE_VECTOR_DB_TABLE", "str", "", OWNER_AGENT,
       "servers/enterprise/server.py:_snapshot",
       "таблица эмбеддингов в снимке, из которой собираются индексы"),
    _s("ENTERPRISE_VECTOR_INDEXES", "str", "", OWNER_AGENT,
       "servers/enterprise/server.py:_vectors_config",
       "индексы вида имя=таблица, через запятую"),
    _s("ENTERPRISE_VECTOR_STORAGE_TABLE", "str", "", OWNER_AGENT,
       "servers/enterprise/server.py:_vectors_config",
       "инфраструктурная таблица хранения векторов"),
    _s("ENTERPRISE_VECTOR_ENABLE", "bool", True, OWNER_PLATFORM,
       "servers/enterprise/server.py:_vectors_config",
       "выключатель capability vectors", file_key="vectors.enable"),
    _s("ENTERPRISE_EMBED_MODEL", "str", "", OWNER_AGENT,
       "servers/enterprise/server.py:_vectors_config",
       "модель эмбеддера; пусто — capability llm остаётся ненастроенной"),
    _s("ENTERPRISE_EMBED_DIMENSION", "int", None, OWNER_AGENT,
       "servers/enterprise/server.py:_vectors_config",
       "размерность вектора эмбеддера; входит в подпись индекса"),
    _s("ENTERPRISE_EMBED_TIMEOUT", "float", None, OWNER_AGENT,
       "servers/enterprise/server.py:_vectors_config",
       "таймаут HTTP-запроса к эмбеддеру, сек"),
    # -- capability llm ------------------------------------------------------
    _s("ENTERPRISE_LLM_PROVIDER", "str", "", OWNER_AGENT,
       "libs/llm/config.py:_ENV_NAMES",
       "провайдер чата", aliases=("LLM_PROVIDER",), required=True),
    _s("ENTERPRISE_LLM_MODEL", "str", "", OWNER_AGENT,
       "libs/llm/config.py:_ENV_NAMES",
       "модель чата", aliases=("LLM_MODEL",), required=True),
    _s("ENTERPRISE_LLM_API_BASE", "str", "", OWNER_AGENT,
       "libs/llm/config.py:_ENV_NAMES",
       "адрес эндпойнта чата", aliases=("LLM_API_BASE",)),
    _s("ENTERPRISE_LLM_API_KEY", "secret", "", OWNER_AGENT,
       "libs/llm/config.py:_ENV_NAMES",
       "ключ чата", aliases=("LLM_API_KEY",)),
    _s("ENTERPRISE_LLM_MAX_TOKENS", "int", None, OWNER_AGENT,
       "libs/llm/config.py:_ENV_NAMES",
       "потолок токенов ответа", aliases=("LLM_MAX_TOKENS",)),
    _s("ENTERPRISE_LLM_TEMPERATURE", "float", None, OWNER_AGENT,
       "libs/llm/config.py:_ENV_NAMES",
       "температура чата", aliases=("LLM_TEMPERATURE",)),
    _s("ENTERPRISE_EMBED_API_BASE", "str", "", OWNER_AGENT,
       "libs/llm/config.py:_ENV_NAMES",
       "адрес эндпойнта эмбеддингов; эмбеддер и чат — разные провайдеры"),
    _s("ENTERPRISE_EMBED_API_KEY", "secret", "", OWNER_AGENT,
       "libs/llm/config.py:_ENV_NAMES",
       "ключ эмбеддингов"),
    _s("ENTERPRISE_EMBED_PATH", "str", "", OWNER_AGENT,
       "libs/llm/config.py:_ENV_NAMES",
       "путь эндпойнта эмбеддингов, если адрес задан полным URL"),
    # -- журнал и бюджеты запросов: собственные ручки платформы ---------------
    _s("ENTERPRISE_LOG_TABLE", "str", "public.agent_gateway_logs", OWNER_PLATFORM,
       "servers/enterprise/server.py:_log_table",
       "таблица долговечного журнала gateway", file_key="data.log_table"),
    _s("ENTERPRISE_LOG_BUFFER_MAXLEN", "int", 2048, OWNER_PLATFORM,
       "servers/enterprise/server.py:_build_container",
       "потолок буфера журнала; переполнение теряет события, а не растёт",
       file_key="data.log_buffer_maxlen"),
    _s("ENTERPRISE_LOG_FLUSH_INTERVAL", "float", 5.0, OWNER_PLATFORM,
       "servers/enterprise/server.py:_build_container",
       "период сброса буфера журнала в базу, сек",
       file_key="data.log_flush_interval"),
    _s("ENTERPRISE_STATEMENT_TIMEOUT_MS", "int", 30000, OWNER_PLATFORM,
       "servers/enterprise/server.py:_build_container",
       "SET statement_timeout для запросов capability data, мс",
       file_key="data.statement_timeout_ms"),
    _s("ENTERPRISE_MAX_ROWS", "int", 1000, OWNER_PLATFORM,
       "servers/enterprise/server.py:_build_container",
       "потолок строк в ответах операций", file_key="data.max_rows"),
    _s("ENTERPRISE_EXPECTED_TABLES", "list", (), OWNER_PLATFORM,
       "servers/enterprise/server.py:_build_container",
       "обязательные runtime-таблицы для schema_check",
       file_key="data.expected_tables"),
    # -- пул соединений -----------------------------------------------------
    # Размеры и таймауты пула — ручки платформы: агент о них не знает и
    # знать не должен. До этого они жили только в ``_DEFAULT_POOL``, то
    # есть задать их было нечем, а реестр молчал. Тот же отказ, что был
    # с ``ENTERPRISE_SCRIPTS_REGISTRY_TABLE``: значение не имеет пути из
    # конфигурации.
    _s("ENTERPRISE_POOL_MIN_CONN", "int", 1, OWNER_PLATFORM,
       "servers/enterprise/server.py:_apply_pool_settings",
       "воркеров пула, поднимаемых на старте", file_key="pool.min_conn"),
    _s("ENTERPRISE_POOL_MAX_CONN", "int", 4, OWNER_PLATFORM,
       "servers/enterprise/server.py:_apply_pool_settings",
       "потолок пула: больше соединений не открывается никогда",
       file_key="pool.max_conn"),
    _s("ENTERPRISE_POOL_TIMEOUT", "float", 5.0, OWNER_PLATFORM,
       "servers/enterprise/server.py:_apply_pool_settings",
       "ожидание места в очереди и порог warning'а при ожидании аренды, сек",
       file_key="pool.pool_timeout"),
    _s("ENTERPRISE_POOL_QUEUE_MAXSIZE", "int", 10000, OWNER_PLATFORM,
       "servers/enterprise/server.py:_apply_pool_settings",
       "предел длины очереди; переполнение — PoolTimeoutError, а не вечный блок",
       file_key="pool.queue_maxsize"),
    _s("ENTERPRISE_POOL_RECONNECT_BACKOFF_SEC", "float", 1.0, OWNER_PLATFORM,
       "servers/enterprise/server.py:_apply_pool_settings",
       "первая пауза перед повтором подключения, сек",
       file_key="pool.reconnect_backoff_sec"),
    _s("ENTERPRISE_POOL_RECONNECT_BACKOFF_MAX_SEC", "float", 60.0, OWNER_PLATFORM,
       "servers/enterprise/server.py:_apply_pool_settings",
       "потолок паузы между попытками подключения, сек",
       file_key="pool.reconnect_backoff_max_sec"),
    _s("ENTERPRISE_POOL_CONNECT_MAX_RETRIES", "int", 5, OWNER_PLATFORM,
       "servers/enterprise/server.py:_apply_pool_settings",
       "попыток подключения воркера до отказа",
       file_key="pool.connect_max_retries"),
    _s("ENTERPRISE_POOL_IDLE_TIMEOUT_SEC", "float", 60.0, OWNER_PLATFORM,
       "servers/enterprise/server.py:_apply_pool_settings",
       "простой воркера, после которого он снимается до min_conn, сек",
       file_key="pool.idle_timeout_sec"),
    _s("ENTERPRISE_POOL_JOB_MAX_RETRIES", "int", 3, OWNER_PLATFORM,
       "servers/enterprise/server.py:_apply_pool_settings",
       "повторов задания при повторяемой ошибке",
       file_key="pool.job_max_retries"),
    _s("ENTERPRISE_POOL_PRINT_ACTIVITY", "bool", False, OWNER_PLATFORM,
       "servers/enterprise/server.py:_apply_pool_settings",
       "вывод активности db-worker'ов в stderr процесса",
       file_key="pool.print_activity"),
)

#: Имя -> настройка. Построен один раз; единственный источник правды.
BY_NAME: dict[str, Setting] = {s.name: s for s in SETTINGS}

#: Все имена, включая синонимы: страж сверяет по ним, иначе синоним
#: ``PG_DSN`` проскочил бы мимо проверки как «неизвестная переменная».
BY_ANY_NAME: dict[str, Setting] = {n: s for s in SETTINGS for n in s.names}


@dataclass(frozen=True)
class CapabilitySettings:
    """Настройки одной capability — в той же форме, в какой устроен код.

    Реестр повторяет структуру платформы, а не выдумывает свою: сначала
    capability, потом её ``service`` и её операции. Вопрос «кто читает эту
    настройку» тогда отвечает на вопрос про потребителя, а не про
    ``_build_container``, который только собирает сервисы.

    Attributes:
        name: имя capability — оно же секция в ``platform.json`` и каталог
            в ``servers/enterprise/capabilities/``.
        service: путь к ``service/main.py``. Проверяется стражем: если
            сервиса нет, настройки этой capability читать некому.
        tools: операции capability. Тоже проверяются — настройка, которая
            никому из них не нужна, вероятно, объявлена зря.
        settings: имена настроек из ``SETTINGS``.
    """

    name: str
    service: str
    tools: tuple[str, ...]
    settings: tuple[str, ...]
    summary: str = ""


#: Capability платформы. Порядок и состав — как на диске, в
#: ``servers/enterprise/capabilities/``; страж сверяет оба.
CAPABILITIES: tuple[CapabilitySettings, ...] = (
    CapabilitySettings(
        name="data",
        service="servers/enterprise/capabilities/data/service/main.py",
        tools=(
            "claim_task",
            "history_search",
            "log_event",
            "log_events",
            "purge_logs",
            "schema_check",
            "update_task_status",
            "upsert_question_run",
        ),
        settings=(
            "ENTERPRISE_LOG_TABLE",
            "ENTERPRISE_LOG_BUFFER_MAXLEN",
            "ENTERPRISE_LOG_FLUSH_INTERVAL",
            "ENTERPRISE_STATEMENT_TIMEOUT_MS",
            "ENTERPRISE_MAX_ROWS",
            "ENTERPRISE_EXPECTED_TABLES",
        ),
        summary="PostgreSQL: очередь задач, долговечный журнал, чтение журнала",
    ),
    CapabilitySettings(
        name="audit",
        service="servers/enterprise/capabilities/audit/service/main.py",
        tools=("generate_sql", "list_scripts", "run_script"),
        settings=(
            "ENTERPRISE_SCRIPTS_REGISTRY_TABLE",
            "ENTERPRISE_AUDIT_TABLES",
            "ENTERPRISE_AUDIT_ROW_CEILING",
        ),
        summary="запрос к данным агента: реестр скриптов, их выполнение, генерация SQL",
    ),
    CapabilitySettings(
        name="vectors",
        service="servers/enterprise/capabilities/vectors/service/main.py",
        tools=("index_stats", "list_indexes", "vector_search"),
        settings=(
            "ENTERPRISE_SNAPSHOT_PATH",
            "ENTERPRISE_VECTOR_DB_TABLE",
            "ENTERPRISE_VECTOR_INDEXES",
            "ENTERPRISE_VECTOR_STORAGE_TABLE",
            "ENTERPRISE_VECTOR_ENABLE",
            "ENTERPRISE_EMBED_MODEL",
            "ENTERPRISE_EMBED_DIMENSION",
            "ENTERPRISE_EMBED_TIMEOUT",
        ),
        summary="снимок DuckDB, FAISS-индексы и параметры эмбеддера",
    ),
    CapabilitySettings(
        name="llm",
        service="servers/enterprise/capabilities/llm/service/main.py",
        tools=("complete", "embed"),
        settings=(
            "ENTERPRISE_LLM_PROVIDER",
            "ENTERPRISE_LLM_MODEL",
            "ENTERPRISE_LLM_API_BASE",
            "ENTERPRISE_LLM_API_KEY",
            "ENTERPRISE_LLM_MAX_TOKENS",
            "ENTERPRISE_LLM_TEMPERATURE",
            "ENTERPRISE_EMBED_API_BASE",
            "ENTERPRISE_EMBED_API_KEY",
            "ENTERPRISE_EMBED_PATH",
            "ENTERPRISE_EMBED_MODEL",
        ),
        summary="вызовы чата и эмбеддингов к внешнему провайдеру",
    ),
)

#: Настройки вне capability: ими владеет общий код платформы.
#:
#: Пул соединений нужен всем capability сразу, и ни одной из них не
#: принадлежит: ``data`` ходит в базу напрямую, ``audit`` — через снимок,
#: загрузчик снимка — тоже через пул. Объявлять пул в секции ``data``
#: означало бы, что размер пула — дело capability PostgreSQL-очереди.
SHARED_SETTINGS: tuple[str, ...] = (
    "DATABASE_URL",
    "ENTERPRISE_POOL_MIN_CONN",
    "ENTERPRISE_POOL_MAX_CONN",
    "ENTERPRISE_POOL_TIMEOUT",
    "ENTERPRISE_POOL_QUEUE_MAXSIZE",
    "ENTERPRISE_POOL_RECONNECT_BACKOFF_SEC",
    "ENTERPRISE_POOL_RECONNECT_BACKOFF_MAX_SEC",
    "ENTERPRISE_POOL_CONNECT_MAX_RETRIES",
    "ENTERPRISE_POOL_IDLE_TIMEOUT_SEC",
    "ENTERPRISE_POOL_JOB_MAX_RETRIES",
    "ENTERPRISE_POOL_PRINT_ACTIVITY",
)

#: Секции ``platform.json`` для настроек вне capability. Список объявлен
#: явно, потому что страж сверяет с ним файл: секция, не принадлежащая ни
#: capability, ни этому списку, выглядела бы как «настройка прочитана».
SHARED_SECTIONS: tuple[str, ...] = ("pool",)

#: Ключ пула -> имя настройки. Связь названа один раз здесь, и ею пользуются
#: и :func:`pool_defaults`, и :func:`pool_config`: иначе второй список
#: ключей разошёлся бы с первым, и неизвестный ключ уехал бы в
#: ``set_pool_config`` молча.
POOL_SETTING_KEYS: dict[str, str] = {
    "min_conn": "ENTERPRISE_POOL_MIN_CONN",
    "max_conn": "ENTERPRISE_POOL_MAX_CONN",
    "pool_timeout": "ENTERPRISE_POOL_TIMEOUT",
    "queue_maxsize": "ENTERPRISE_POOL_QUEUE_MAXSIZE",
    "reconnect_backoff_sec": "ENTERPRISE_POOL_RECONNECT_BACKOFF_SEC",
    "reconnect_backoff_max_sec": "ENTERPRISE_POOL_RECONNECT_BACKOFF_MAX_SEC",
    "connect_max_retries": "ENTERPRISE_POOL_CONNECT_MAX_RETRIES",
    "idle_timeout_sec": "ENTERPRISE_POOL_IDLE_TIMEOUT_SEC",
    "job_max_retries": "ENTERPRISE_POOL_JOB_MAX_RETRIES",
    "print_activity": "ENTERPRISE_POOL_PRINT_ACTIVITY",
}


def pool_defaults() -> dict[str, Any]:
    """Дефолты пула в форме ``set_pool_config`` — из реестра, а не из кода."""
    return {key: BY_NAME[name].default for key, name in POOL_SETTING_KEYS.items()}


def pool_config(settings: "Settings") -> dict[str, Any]:
    """Разрешённые значения пула в форме ``set_pool_config``."""
    return {key: settings.get(name) for key, name in POOL_SETTING_KEYS.items()}


#: capability -> её настройки, для файла и документации.
SETTINGS_BY_CAPABILITY: dict[str, tuple[str, ...]] = {
    c.name: c.settings for c in CAPABILITIES
}


def settings_owned_by(owner: str) -> tuple[Setting, ...]:
    return tuple(s for s in SETTINGS if s.owner == owner)


def capabilities_of(setting_name: str) -> tuple[str, ...]:
    """Какие capability заинтересованы в настройке (может быть несколько).

    Один и тот же параметр нужен двум capability — например, модель эмбеддера
    нужна и ``vectors`` (подпись индекса), и ``llm`` (вызов эндпойнта).
    Объявлять её дважды нельзя, поэтому настройка одна, а список
    capability — рядом.
    """
    return tuple(c.name for c in CAPABILITIES if setting_name in c.settings)


#: Ключ файла -> настройка. Строится из ``file_key``, а не из имени
#: переменной: человек в файле пишет ``data.log_table``.
BY_FILE_KEY: dict[str, Setting] = {
    s.key: s for s in SETTINGS if s.owner == OWNER_PLATFORM
}


def _flatten(raw: dict[str, Any]) -> dict[str, Any]:
    """Развернуть вложенные секции в точечные ключи, один уровень глубины.

    Ключи, начинающиеся с подчёркивания (``_about``, ``_owner``), —
    комментарии в данных, а не настройки; они пропускаются молча, иначе
    пришлось бы держать в файле синтаксис комментариев ради двух строк.
    """
    flat: dict[str, Any] = {}
    for key, value in raw.items():
        if str(key).startswith("_"):
            continue
        if isinstance(value, dict):
            for sub, sub_value in value.items():
                if str(sub).startswith("_"):
                    continue
                flat[f"{key}.{sub}"] = sub_value
        else:
            flat[str(key)] = value
    return flat


def read_platform_file(path: Path | None = None) -> dict[str, str]:
    """Прочитать ``platform.json`` в вид ``имя переменной -> значение``.

    Только ``owner="platform"``. Значение настройки агента в этом файле —
    это дублирование, и оно поднимается как ошибка, а не игнорируется:
    иначе файл тихо разошёлся бы с окружением, и виноват был бы тот, кто
    чинит последствия.

    Raises:
        InfrastructureError: файл не читается, не объект, содержит неизвестный
            ключ или ключ настройки агента.
    """
    target = path or PLATFORM_CONFIG_PATH
    if not target.exists():
        return {}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InfrastructureError(f"{target.name}: не читается ({exc})") from exc
    if not isinstance(raw, dict):
        raise InfrastructureError(f"{target.name}: ожидался объект")

    allowed = {s.key for s in settings_owned_by(OWNER_PLATFORM)}
    flat: dict[str, str] = {}
    for key, value in _flatten(raw).items():
        if key not in allowed:
            raise InfrastructureError(
                f"{target.name}: {key!r} — не настройка платформы "
                f"(владелец — агент, значение приходит через окружение)"
            )
        if isinstance(value, bool):
            flat[key] = "1" if value else "0"
        elif isinstance(value, (int, float)):
            flat[key] = str(value)
        elif isinstance(value, (list, tuple)):
            flat[key] = ",".join(str(v) for v in value)
        else:
            flat[key] = str(value)
    return {BY_FILE_KEY[k].name: v for k, v in flat.items()}


class Settings:
    """Разрешённые значения настроек: окружение > файл > дефолт."""

    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        file_path: Path | None = None,
    ) -> None:
        self._env: Mapping[str, str] = os.environ if env is None else env
        self._file: dict[str, str] = read_platform_file(file_path)
        self._values: dict[str, Any] = {}
        self._sources: dict[str, str] = {}
        for setting in SETTINGS:
            self._resolve(setting)

    def _resolve(self, setting: Setting) -> None:
        for name in setting.names:
            raw = self._env.get(name)
            if raw is not None and raw.strip():
                self._values[setting.name] = setting.coerce(raw)
                self._sources[setting.name] = f"env:{name}"
                return
        raw = self._file.get(setting.name)
        if raw is not None and str(raw).strip():
            self._values[setting.name] = setting.coerce(str(raw))
            self._sources[setting.name] = "file:platform.json"
            return
        self._values[setting.name] = setting.default
        self._sources[setting.name] = "default"

    def get(self, name: str) -> Any:
        """Значение настройки. Неизвестное имя — исключение, не ``None``."""
        if name not in self._values:
            raise InfrastructureError(f"настройка {name!r} не объявлена в реестре")
        return self._values[name]

    def source(self, name: str) -> str:
        """Откуда взято значение: ``env:*``, ``file:platform.json``, ``default``."""
        if name not in self._sources:
            raise InfrastructureError(f"настройка {name!r} не объявлена в реестре")
        return self._sources[name]

    def as_dict(self, *, reveal_secrets: bool = False) -> dict[str, Any]:
        """Снимок значений — для баннера запуска и диагностики.

        Args:
            reveal_secrets: если False (по умолчанию), секреты заменены на
                ``***``. Баннер печатается в лог, а лог читают люди и
                системы сбора — значение ключа там быть не должно.
        """
        out: dict[str, Any] = {}
        for setting in SETTINGS:
            value = self._values[setting.name]
            if setting.is_secret and value and not reveal_secrets:
                out[setting.name] = "***"
            else:
                out[setting.name] = value
        return out

    def file_backed(self) -> tuple[str, ...]:
        """Настройки, значение которых пришло из ``platform.json``.

        Для баннера запуска: значение из файла — единственное, о котором
        оператор не знает наверняка, потому что он не передавал его через
        окружение.
        """
        return tuple(
            name
            for name, source in self._sources.items()
            if source == "file:platform.json"
        )

    def as_env(self) -> dict[str, str]:
        """Разрешённые значения в виде ``имя переменной -> значение``.

        Нужен потребителям, которые принимают источник окружения параметром
        (``libs/llm``): они получают ровно то, что разрешил реестр, и не
        читают ``os.environ`` сами. Второй разбор — это и есть «настройка
        прописана в двух местах»: файл меняет значение для одного, а для
        второго остаётся окружение.

        Секреты здесь живут так же, как в окружении, и уходят дальше по
        процессу, — но не в лог: печатать их нельзя, поэтому для диагностики
        есть :meth:`as_dict`, который маскирует.
        """
        out: dict[str, str] = {}
        for setting in SETTINGS:
            value = self._values[setting.name]
            if value is None:
                continue
            if isinstance(value, bool):
                out[setting.name] = "1" if value else "0"
            elif isinstance(value, (list, tuple)):
                out[setting.name] = ",".join(str(item) for item in value)
            else:
                out[setting.name] = str(value)
        return out

    def missing_required(self) -> tuple[str, ...]:
        """Обязательные настройки без значения."""
        return tuple(
            s.name
            for s in SETTINGS
            if s.required and not self._values.get(s.name)
        )
