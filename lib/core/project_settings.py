"""ProjectSettings — типизированная валидация проектных настроек (pydantic).

Fail-fast граница конфигурации: неправильный тип или недопустимое значение
ключа ``config.json`` ловится на старте приложения (``ApplicationContext.
create``), а не в рантайме канала/сервиса.

Принципы:
  - все ключи опциональны с дефолтами: отсутствие настройки не ошибка
    (дефолты живут в потребителях через ``get_setting``);
  - неверный ТИП или значение — ошибка: ``ConfigurationError`` со списком
    всех проблем сразу;
  - неизвестные ключи на верхнем уровне разрешены (extra="allow") —
    forward-совместимость для новых подсекций;
  - внутри ``skills.<name>`` неизвестные ключи ЗАПРЕЩЕНЫ (extra="forbid")
    — fail-fast на опечатках (например, ``tablse`` вместо ``tables``);
  - единственный источник правды — SETTINGS после мержа
    config.json → session_manager.json → .secrets.env.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from config import ConfigurationError

__all__ = [
    "ProjectSettings",
    "SkillBriefContextSettings",
    "SkillCliSettings",
    "SkillLlmSettings",
    "SkillSettings",
    "SkillsSettings",
    "StartupSchemaValidationSettings",
    "StartupSettings",
    "TableEntry",
    "VectorIndexConfig",
    "VectorIndexEntry",
    "GatewaySettings",
    "VectorInfrastructureSettings",
    "UsageStoreSettings",
    "SessionColdSyncSettings",
    "validate_project_settings",
]


class _StrictOptional(BaseModel):
    """База для секций: неизвестные ключи разрешены, известные — типизированы."""

    model_config = ConfigDict(extra="allow")


class PostgresChannelSettings(_StrictOptional):
    worker_id: str | None = None
    poll_interval: float | None = Field(default=None, gt=0)
    error_retry_delay: float | None = Field(default=None, ge=0)
    unstick_interval: float | None = Field(default=None, gt=0)
    processing_timeout: int | None = Field(default=None, gt=0)


class CompactSettings(_StrictOptional):
    enabled: bool | None = None
    notify_in_history: bool | None = None
    print_to_terminal: bool | None = None


class StartupSchemaValidationSettings(_StrictOptional):
    """Pre-startup проверка наличия обязательных runtime-таблиц.

    При ``enabled=True`` (по умолчанию) ``ApplicationContext.start()``
    выполняет один ``SELECT`` к ``information_schema.tables`` для 5
    таблиц из ``SETTINGS["channels"]["postgres"]`` и
    ``SETTINGS["logging"]["db"]`` (те же ключи, что проходят
    ``validate_runtime_isolation``). При отсутствии любой из них —
    ``SchemaValidationError`` (наследник ``ConfigurationError``) →
    ``exit 2`` через ``gateway.main()`` / ``cli_agent.main()``.

    Attributes:
        enabled: включить проверку (по умолчанию ``True``).
        timeout_sec: верхняя граница ожидания запроса к БД
            (по умолчанию ``5.0``, диапазон ``0.1 ≤ value ≤ 60.0``).
            Выставляется как ``statement_timeout`` на соединении пула;
            истечение даёт ``SchemaValidationTimeoutError`` — отдельный
            отказ, а не «нет таблиц».

    См. спеку ``openspec/specs/runtime/startup-schema-validation``.
    """

    enabled: bool = True
    timeout_sec: float = Field(default=5.0, ge=0.1, le=60.0)


class StartupSettings(_StrictOptional):
    """Секция ``gateway.startup.*`` — параметры pre-startup валидации."""

    schema_validation: StartupSchemaValidationSettings | None = None


class ErrorMessagesSettings(_StrictOptional):
    """Заготовленные ответы при internal-ошибке ``AgentLoop._process_message``.

    Используется патчем ``RuntimePatcher.patch_turn_delivery_fail`` (см.
    спеку ``openspec/specs/runtime/error-fallback``): при любом
    ``Exception`` в upstream-``AgentLoop`` пользователь получает
    ``gateway.error_messages.internal_error`` вместо захардкоженного
    англоязычного литерала из upstream-``TurnDelivery.fail``. Детали
    исключения (тип + текст) пишутся в ``agent_gateway_logs`` при
    ``log_to_db=true``.

    Attributes:
        internal_error: текст, который видит пользователь вместо upstream
            ``"Sorry, I encountered an error."``. По умолчанию — русская
            формулировка без раскрытия внутренних деталей.
        log_to_db: писать ли ``event_type="agent.failed"`` в
            ``agent_gateway_logs`` через ``DbLoggingService.try_log_event``
            (см. ``lib/services/db_logging_service.py:34``). При
            ``False`` — детали остаются только в ``loguru``. По умолчанию
            ``True`` (оператор видит, что сломалось, через
            ``history_search``).

    Unknown keys разрешены (``_StrictOptional(extra="allow")``) —
    forward-compat по будущим per-channel/per-language формулировкам.
    """

    internal_error: str | None = None
    log_to_db: bool | None = None


# DuckDbQuerySettings / VectorSearchSettings удалены (этап 18):
# Agent-facing tools (duckdb_query_tool.py, vector_search_tool.py) удалены.


class VectorIndexSettings(_StrictOptional):
    """Параметры FAISS-инфраструктуры.

    Хранилище эмбеддингов и сами индексы — общий runtime, не привязанный
    к домену skill'а. Доменные таблицы, нужные в DuckDB-кэше, декларируются
    в ``skills.<name>.tables[]``. Какие индексы строить и из каких
    source-таблиц — описывается в ``indexes`` (см. ``VectorIndexConfig``);
    это единственный источник (раньше был PG-реестр
    ``public.agent_vector_index_config``).

    Путь в ``config.json``: ``gateway.vector.index.*`` (см.
    ``VectorInfrastructureSettings``). Раньше жил в ``gateway.vector_index.*`` —
    устаревший путь удалён, обратной совместимости нет (fail-fast).

    ⚠️ **Секция валидируется, но никем не читается.** С 2026-10-01 сборку и
    владение индексами забрала capability ``vectors``, и она читает
    ``mcp-platform/platform.json → vectors.indexes`` (и ``vectors.storage_table``).
    В дереве агента потребителей ``gateway.vector.index.*`` нет: grep по
    ``lib/``, ``workspace/``, ``tools/``, ``gateway.py`` и ``cli_agent.py`` даёт
    только эту модель. Правка ``config.json`` здесь не изменит ни сборку, ни
    поиск — объявление продублировано в двух файлах, и какое из них отживает
    своё, решает владелец. См. ``docs/MIGRATION.md`` и ``docs/VECTOR_INDEXES.md``.

    Attributes:
        enable: включён ли vector-indexing слой. ``None`` → дефолт ``True``.
        default_root: корневая папка FAISS-индексов. Дефолт
            ``"data_store/vectors"``. Путь к индексу = ``<root>/<name>``.
        backend: runtime-бэкенд (``"faiss"``, ``"pgvector"``, ``"qdrant"``).
        storage_table: единая PG-таблица-хранилище сырых эмбеддингов.
            Чтением и загрузкой владеет capability ``vectors`` платформы;
            реестр ресурсов, который раньше его объявлял, удалён.
        indexes: полный конфиг vector-индексов ``{имя: VectorIndexConfig}``
            (какие индексы строить, из каких source-таблиц, content_cols,
            embedding_cols, chunk-параметры, metric). Единственный источник
            для ``mcp-platform/libs/vectors/config.py`` и сборки индексов
            на платформе.
    """

    enable: bool | None = None
    default_root: str | None = None
    backend: str | None = None
    storage_table: str | None = None
    indexes: dict[str, VectorIndexConfig] | None = None


class VectorInfrastructureSettings(_StrictOptional):
    """Векторная инфраструктура (``gateway.vector.*``): индексы.

    Содержит ``index`` — ``VectorIndexSettings`` (конфиг индексов,
    storage-таблица). Параметры подключения к эмбеддеру больше не
    настраиваются: они принадлежат capability ``vectors`` платформы
    (``mcp-platform/libs/vectors/embedding.py``) и читаются там.
    Каноническое место для **общей** vector-инфраструктуры.
    """

    index: VectorIndexSettings | None = None


class HeartbeatSettings(_StrictOptional):
    enabled: bool | None = None
    intervalS: int | None = Field(default=None, gt=0)


class UsageStoreSettings(_StrictOptional):
    """Параметры upstream ``LLMUsageStore`` (``gateway.usage_store.*``).

    ``sqlite_path`` — путь к SQLite-файлу (по умолчанию —
    ``<get_runtime_subdir("usage")>/usage.db``). ``enabled=False``
    отключает запись LLM-usage (graceful degradation).

    См. спеку ``openspec/specs/storage/usage-store/spec.md``.
    """

    sqlite_path: str | None = None
    enabled: bool | None = True


class SessionColdSyncSettings(_StrictOptional):
    """Параметры зеркала сессий (``gateway.session_cold_sync.*``).

    Класс назван по разделу конфигурации, а не по реализации: раздел
    объявлен настройкой, и переименование класса не должно тащить за собой
    переименование ключа. Реализация — ``lib/gateway/mirror/``.

    Cold-storage mirror upstream JSONL → PG. Все ключи опциональны.
    См. спеку ``openspec/specs/storage/session-hybridization/spec.md``
    и design D23 (stale-detection + reverse-lag detection).
    """

    enabled: bool | None = True
    sync_interval_sec: float | None = Field(default=None, gt=0)
    batch_size: int | None = Field(default=None, gt=0)
    stale_tolerance_seconds: int | None = Field(default=None, ge=0)
    sync_lag_threshold_seconds: int | None = Field(default=None, ge=0)


class GatewaySettings(_StrictOptional):
    print_llm_calls: bool | None = None
    print_worker_activity: bool | None = None
    print_db_activity: bool | None = None
    llm_timeout: int | None = Field(default=None, gt=0)
    exec_timeout: int | None = Field(default=None, ge=0)
    compact: CompactSettings | None = None
    error_messages: ErrorMessagesSettings | None = None
    # duckdb_query / vector_search: Agent-facing tools удалены (этап 18).
    vector: VectorInfrastructureSettings | None = None
    heartbeat: HeartbeatSettings | None = None
    usage_store: UsageStoreSettings | None = None
    session_cold_sync: SessionColdSyncSettings | None = None
    startup: StartupSettings | None = None
    repeat_guard: GatewayRepeatGuardSettings | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_renamed_sections(cls, data: Any) -> Any:
        """Fail-fast на legacy-переименованных секциях ``gateway.*``.

        ``GatewaySettings`` унаследован от ``_StrictOptional(extra="allow")``
        для forward-compat по **новым** flat-ключам (``print_*``, ``storage``
        и т.п.). Но это означает, что **известные legacy-переименования**
        (``vector_index``) тоже прошли бы как extra-поля, и тогда:
          * комментарий «обратной совместимости нет (fail-fast)» врёт;
          * consumer (``register_vector_storage``) молча игнорирует секцию;
          * пользователь получает «всё стартануло, но DuckDB-кеш пустой».

        Этот ``mode="before"`` валидатор делает явный fail-fast:
        legacy-секции сразу падают. Ошибка ловится в
        ``validate_project_settings`` (см. ``_LEGACY_GATEWAY_KEYS``)
        и unwrap'ается в чистую ``ConfigurationError``.
        """
        if not isinstance(data, dict):
            return data
        problems: list[str] = []
        for legacy_key, hint in _LEGACY_GATEWAY_KEYS.items():
            if legacy_key in data:
                problems.append(f"  gateway.{legacy_key}: {hint}")
        if problems:
            raise _LegacyGatewaySectionsError(
                "Некорректная конфигурация config.json (legacy-секции gateway.*):\n"
                + "\n".join(problems)
            )
        return data


class GatewayRepeatGuardSettings(_StrictOptional):
    """Защитник от повторных tool-вызовов (``gateway.repeat_guard.*``).

    Детектирует вырожденные циклы вида «модель зовёт один и тот же
    инструмент с одними и теми же аргументами» внутри одного оборота.
    Существующие throttles nanobot покрывают только web-fetch/web-search и
    workspace-bypass; этот — общий случай.

    Дефолт ``mode = "off"``: деплой без правок ``config.json`` ведёт себя
    ровно как раньше.

    Объявлен ПОСЛЕ ``GatewaySettings``, хотя используется в его поле
    ``repeat_guard``: в Python это не мешает, но держать вложенную модель
    рядом с местом использования опасно — одна правка порядка классов
    незаметно утянула бы за собой валидаторы родителя (так уже случилось
    с ``_reject_legacy_renamed_sections``).

    Attributes:
        mode: ``off`` (no-op) / ``warn`` (событие в журнал) / ``block``
            (вызов подменяется синтетической ошибкой для модели).
        window_size: сколько последних вызовов оборота удерживать.
            Ограничение ``le=1000`` — это потолок памяти на сессию.
        max_repeats_in_window: срабатывание на N-ом **идентичном** вызове
            (текущий считается). Первые ``N-1`` проходят.
        exempt_tools: имена инструментов, которые никогда не проверяются.
            Сопоставление точным равенством, шаблоны запрещены.

    См. ``openspec/specs/runtime/anti-loop/spec.md``.
    """

    mode: Literal["off", "warn", "block"] = "off"
    window_size: int = Field(default=20, ge=1, le=1000)
    max_repeats_in_window: int = Field(default=3, ge=2, le=100)
    exempt_tools: list[str] = Field(default_factory=list)

    @field_validator("exempt_tools")
    @classmethod
    def _reject_patterns(cls, value: list[str]) -> list[str]:
        """Отвергнуть glob/regex-шаблоны: сопоставление только точное.

        Молчаливый шаблон был бы ловушкой: ``exempt_tools=["read_*"]``
        выглядел бы как «исключить read_file», а на деле исключил бы
        инструмент, которого нет. Лучше fail-fast на старте.
        """
        for entry in value:
            if not isinstance(entry, str):
                raise ValueError(
                    f"gateway.repeat_guard.exempt_tools: запись {entry!r} "
                    "не является строкой"
                )
            for meta in ("*", "?", "[", "]", "^", "$", "\\"):
                if meta in entry:
                    raise ValueError(
                        f"gateway.repeat_guard.exempt_tools: запись "
                        f"{entry!r} содержит метасимвол {meta!r}; "
                        "сопоставление только точное, шаблоны не поддерживаются"
                    )
        return value


class CliSettings(_StrictOptional):
    show_context_window: bool | None = None
    max_iterations: int | None = Field(default=None, gt=0)


class ChannelsSettings(_StrictOptional):
    postgres: PostgresChannelSettings | None = None
    document_text_threshold: int | None = Field(default=None, ge=0)


class LoggingDbSettings(_StrictOptional):
    enabled: bool | None = None
    flush_interval_sec: float | None = Field(default=None, ge=0.5, le=60.0)

    @model_validator(mode="after")
    def _default_flush_interval_sec(self) -> LoggingDbSettings:
        """Подменить ``None`` на канонический дефолт ``5.0``.

        Спека change ``improve-history-search-pagination-and-logging``
        требует, чтобы типизированная конфигурация была
        **источником default-value** для ``flush_interval_sec``:
        ``LoggingDbSettings().flush_interval_sec == 5.0``. Pydantic
        ``default=None`` оставляет поле ``None``-able (что нужно для
        семантики «отсутствующий ключ не ошибка»), но downstream-код
        (``ApplicationContext``) получает ``5.0`` без явного fallback
        на константу. Контракт: внутри сконструированной модели
        ``None`` сюда не попадает.
        """
        if self.flush_interval_sec is None:
            object.__setattr__(self, "flush_interval_sec", 5.0)
        return self


class LoggingSettings(_StrictOptional):
    db: LoggingDbSettings | None = None


class EnterpriseMcpSettings(_StrictOptional):
    """Подключение агента к MCP-серверу ``enterprise-mcp``.

    Объявление одно, и читает его клиент агента. ``mcpServers`` в
    ``config.json`` намеренно остаётся пустым: пока операции не отдаются
    модели, вторая копия процесса была бы вторым владельцем пула
    PostgreSQL, а владелец у разделяемого ресурса должен быть один.

    Пути и интерпретатор приходят как ``${VAR}`` и резолвятся в
    ``os.environ`` (см. ``config._export_runtime_env``) — в конфиг не
    зашивается ничего, что принадлежит конкретной машине.
    """

    enabled: bool | None = None
    command: str | None = None
    args: list[str] | None = None
    cwd: str | None = None
    tool_timeout_sec: float | None = Field(default=None, gt=0)
    #: Как часто шлюз спрашивает у процесса «ты жив?» (``lib/gateway/mcp_health.py``).
    #: Отдельный ключ рядом с ``stderr_log``, потому что это протокол наблюдения,
    #: а не транспорт вывода. Без него — 10 секунд.
    health_interval_sec: float | None = Field(default=None, gt=0)
    #: Нижний предел между попытками ПОДНЯТЬ платформу, пока она не отвечает.
    #: Отдельно от предыдущего: часто спрашивать дёшево, часто поднимать
    #: процесс — нет. Без него — 60 секунд.
    reconnect_interval_sec: float | None = Field(default=None, gt=0)


# ---------------------------------------------------------------------------
# skills.<name> — универсальная декларация навыка (см. PHASE «унификация»).
# Каждый skill объявляется в config.json одной JSON-секцией. Реестр ресурсов,
# который их принимал, удалён вместе со своими читателями; состав снимка
# объявляет ``mcp-platform/platform.json``.
# ---------------------------------------------------------------------------


class TableEntry(BaseModel):
    """Один ресурс skill'а в ``tables: [...]``.

    Единый формат для всех PG-таблиц skill'а: обычные таблицы, vector-таблицы,
    реестры метаданных, predefined scripts. Каждый ресурс имеет ``name``
    (обязательно) и опциональные атрибуты, которые runtime-sync либо
    игнорирует (``label``), либо читает (``tracking_column``, ``type``).

    Attributes:
        name: имя таблицы в формате ``schema.table``.
        type: ``"table"`` (по умолчанию) или ``"vector"``. Определяет, за
            таблицей или за векторным индексом стоит объявление.
        label: opaque-метка. Если задана, таблица не попадает в описание
            схемы для LLM. Реестр, который по ней искал
            (``TableRegistry.resources_by_label``), удалён; метка осталась
            только как признак «внутренняя таблица».
        tracking_column: колонка для инкрементального поллинга. Дефолт
            ``updated_at`` для обычных, ``id`` для vector.

    Unknown keys запрещены (``extra="forbid"``) — fail-fast на опечатках
    в ``config.json``.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    type: Literal["table", "vector"] = "table"
    label: str | None = None
    tracking_column: str | None = None


class VectorIndexEntry(BaseModel):
    """Один vector-storage индекс в ``vector_indexes: [...]``.

    Минимальный generic-контракт: **только имя** индекса.
    Источник эмбеддингов (PG-таблица исходных строк), алгоритм построения
    (FAISS / pgvector / Qdrant / иной бэкенд), параметры чанкинга и формат
    хранения — это runtime-параметры конкретного бэкенда, **общая
    инфраструктура** (см. ``gateway.vector.index.indexes``),
    а не часть декларации ресурса в ``skills.<name>``.

    Attributes:
        name: логическое имя индекса (``"audits_index"``, ``"products_v"``).

    Unknown keys запрещены (``extra="forbid"``). Это сознательно:
    ``source``, ``embedding`` или другие legacy-поля НЕ должны «тихо»
    проходить через pydantic-валидацию. Если кто-то добавит
    legacy-поле — старт gateway упадёт с ``ConfigurationError``,
    а не пройдёт валидацию и обнаружится только в runtime.

    Раньше в этой модели было обязательное поле ``source`` (имя PG-таблицы
    исходных строк). После того как source-таблицу перенесли в общий
    runtime-конфиг (``gateway.vector.index.indexes``), ``source`` удалён
    из декларации skill'а. Если будет добавлен новый backend, где source
    декларируется прямо в skill'е — это будет новая схема, а не возврат
    к старой.
    """

    model_config = ConfigDict(extra="forbid")

    name: str


class VectorIndexConfig(BaseModel):
    """Полный конфиг одного vector-индекса (``gateway.vector.index.indexes``).

    Единственный источник деталей построения индекса: исходная таблица (``table``),
    первичный ключ (``pk``), логическое имя источника (``source_table``),
    колонки контента/эмбеддинга, track-колонка, chunk-параметры и metric.

    Раньше это жило в PG-реестре ``public.agent_vector_index_config``
    (``sql/vectors/create_vector_index_config.sql`` + seed) и читалось
    ``cache_provider_impl.read_vector_index_config``. Теперь — это
    настройка в ``config.json``, а читает её capability ``vectors``
    платформы: ``mcp-platform/libs/vectors/config.py``.

    Attributes:
        table: исходная таблица для эмбеддинга (``schema.table``).
        pk: колонка первичного ключа в ``table``.
        source_table: логическое имя источника (значение ``source`` в
            vector-хранилище ``oarb.audit_vectors``).
        content_columns: колонки, попадающие в ``content`` вектора.
        embedding_columns: колонки для эмбеддинга; элемент — строка
            (имя колонки) или объект ``{"column": ..., "chunk": true,
            "chunk_size": ..., "chunk_overlap": ...}``.
        track_column: колонка инкрементального отслеживания (дефолт ``updated_at``).
        chunk_size: размер чанка для длинных текстов (дефолт 500).
        chunk_overlap: перекрытие чанков (дефолт 80).
        metric: метрика FAISS (``cosine`` / ``inner_product``; дефолт ``cosine``).
        enabled: включён ли индекс (дефолт ``True``).

    Unknown keys запрещены (``extra="forbid"``) — fail-fast на опечатках
    в ``config.json``.
    """

    model_config = ConfigDict(extra="forbid")

    table: str
    pk: str
    source_table: str | None = None
    content_columns: list[str] = Field(default_factory=list)
    embedding_columns: list[str | dict[str, Any]] | None = None
    track_column: str | None = None
    chunk_size: int | None = Field(default=None, gt=0)
    chunk_overlap: int | None = Field(default=None, ge=0)
    metric: Literal["cosine", "inner_product"] | None = None
    enabled: bool = True


class SkillCliSettings(_StrictOptional):
    """Секция ``cli`` — параметры CLI навыка (например, ``audit_analyze``).

    Это **специфические настройки skill'а**: режимы, форматы вывода, таймауты.
    У разных skill'ов могут быть разные CLI-флаги.
    """

    default_mode: str | None = None
    default_format: str | None = None
    max_retries: int | None = Field(default=None, ge=0)
    timeout_sec: float | None = Field(default=None, gt=0)


class SkillLlmSettings(_StrictOptional):
    """Секция ``llm`` — execution policy генерации для навыка (необязательно).

    Это НЕ выбор модели/провайдера — это runtime-параметры вызова
    (``temperature``, ``max_tokens``). Выбор модели и провайдера агент
    не знает вовсе: он принадлежит capability ``llm`` платформы
    (``mcp-platform/libs/llm/config.py::resolve_llm_config``), читается из
    ``mcp-platform/platform.json`` и попадает в процесс скилла оттуда.
    Агентская копия этого выбора (``lib/services/llm_config.py``) снесена
    2026-10-02 — именно она была второй копией, которая разъезжалась с
    платформенной при первой же смене модели.
    """

    max_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = Field(default=None, ge=0, le=2)


class SkillChunkingSettings(_StrictOptional):
    """Секция ``chunking`` — параметры map-reduce чанкинга (необязательно).

    Исторически управляла ``lib.services.text_splitter.split_text`` внутри
    скилла ``legal_summarizer``: для текстов короче
    ``single_call_threshold`` — один вызов модели, для длиннее — разбиение с
    перекрытием. **Оба потребителя сняты** (чанкинг уехал на платформу
    вместе со скиллом), модель ``text_splitter`` в дереве агента отсутствует,
    а чанкинг для эмбеддингов живёт на платформе отдельным модулем. Секция
    сохранена как приём конфигурации, но **PRODUCTION-ЧТЕНИЙ НЕ ИМЕЕТ**.

    Размер чанка управлялся через ``chunk_size_input_ratio`` (доля от
    ``agents.defaults.contextWindowTokens``), ``chunk_size`` — fallback.
    """

    chunk_size: int | None = Field(default=None, gt=0)
    chunk_overlap: int | None = Field(default=None, ge=0)
    single_call_threshold: int | None = Field(default=None, gt=0)
    chunk_size_input_ratio: float | None = Field(default=None, gt=0, le=1)


class SkillBriefContextSettings(_StrictOptional):
    """Секция ``brief_context`` — параметры сборки brief-контекста (необязательно).

    Собирал ровно один структурный чанк из DocumentStructure +
    PhysicalDocument для скилла ``legal_summarizer``. **Скилл уехал на
    платформу** вместе с ``lib.core.skill_config.get_brief_context_config``
    и модулем по этому пути, поэтому секция **PRODUCTION-ЧТЕНИЙ НЕ ИМЕЕТ** и
    осталась формой совместимости конфигурации.

    ``max_chars`` рассчитывался динамически из contextWindowTokens и
    ``chunking.brief_input_ratio``; эти поля — резервные параметры.
    """

    max_chars_fallback: int | None = Field(default=None, gt=0)
    chars_per_token: float | None = Field(default=None, gt=0)
    structure_max_chars: int | None = Field(default=None, gt=0)


class SkillExecutionContextBatchingSettings(_StrictOptional):
    """Параметры context batching для skill'а (опционально).

    ``chars_per_token`` — оценка токенов для русского текста.
    ``system_prompt_tokens`` / ``instruction_tokens_per_map`` — резерв
    под system prompt и user_body.
    ``safety_margin`` — запас поверх budget (нелинейность оценки).
    ``llm_max_tokens`` — резерв под output LLM.
    """

    chars_per_token: float | None = Field(default=None, gt=0)
    system_prompt_tokens: int | None = Field(default=None, gt=0)
    instruction_tokens_per_map: int | None = Field(default=None, gt=0)
    safety_margin: float | None = Field(default=None, gt=0, le=1)
    llm_max_tokens: int | None = Field(default=None, gt=0)


class SkillExecutionSettings(_StrictOptional):
    """Секция ``execution`` — параметры запуска skill'а (необязательно).

    Управляет подтверждением длинных операций (``confirmation_required``),
    оценкой длительности (``estimated_chunk_duration_sec``),
    safety net (``max_chunks_for_execution``) и параметрами
    context batching (используются ``legal_summarizer``).
    """

    confirmation_threshold_sec: float | None = Field(default=None, gt=0)
    estimated_chunk_duration_sec: float | None = Field(default=None, gt=0)
    max_chunks_for_execution: int | None = Field(default=None, gt=0)
    context_batching: SkillExecutionContextBatchingSettings | None = None


class SkillSettings(BaseModel):
    """Универсальная декларация навыка в ``skills.<name>`` (config.json).

    Это **единственный источник истины** для объявления skill'а: секцию
    читает capability платформы, сопоставляющая объявление с составом
    снимка. Реестр ресурсов на стороне агента, который эти секции принимал,
    удалён вместе со своими читателями.

    Секции:

      * ``enabled`` — флаг включения skill'а (default ``True``);
      * ``tables`` — единый список ресурсов (PG-таблицы + vector-источники);
      * ``vector_indexes`` — какие vector-индексы нужны skill'у
        (min-контракт: имя + источник; runtime определяет бэкенд);
      * ``cli`` — параметры CLI навыка;
      * ``llm`` — execution policy для навыка (опционально);
      * ``chunking`` — параметры map-reduce чанкинга;
      * ``brief_context`` — параметры BriefContextBuilder (опционально);
      * ``execution`` — параметры запуска (confirmation, safety net,
        context batching).

    Это **только domain binding** skill'а. Shared infrastructure
    (DuckDB-кеш, FAISS root, sync) лежит вне ``skills.*`` —
    см. ``gateway.duckdb``, ``gateway.vector.index.*``, ``gateway.sync``.

    Граница: ``model_config = ConfigDict(extra="forbid")`` — fail-fast
    на опечатках в ``config.json`` (например, ``tablse`` вместо
    ``tables`` сразу поднимет ``ConfigurationError`` на старте gateway,
    а не тихо пройдёт валидацию). Имя skill'а остаётся динамическим —
    добавляется простым добавлением секции в ``config.json``; форма
    самой секции строго типизирована.

    Корневой ``enabled`` отключает skill без удаления секции.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    tables: list[str | TableEntry] | None = None
    vector_indexes: list[VectorIndexEntry] | None = None
    cli: SkillCliSettings | None = None
    llm: SkillLlmSettings | None = None
    chunking: SkillChunkingSettings | None = None
    brief_context: SkillBriefContextSettings | None = None
    execution: SkillExecutionSettings | None = None


class SkillsSettings(_StrictOptional):
    """Контейнер для всех навыков: ``skills.<name>``.

    Имя skill'а — произвольное (forward-compat), но **форма** секции
    строго типизирована через ``SkillSettings`` (``extra="forbid"``).
    Любой новый skill добавляется простым добавлением секции в
    ``config.json``; опечатки внутри секции (``tablse``, ``embedding``,
    ``cache`` и т.п.) ловятся на старте через ``_validate_skill_sections``.

    Универсальное правило (TARGET_ARCHITECTURE §skills.* boundary):

      * Меняется при смене домена skill'а → в ``skills.<name>``.
      * Меняется при смене инфраструктуры, но не домена → в ``gateway.*``.
      * Меняется при смене deployment'а → в ``channels.*`` или env.
    """

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _validate_skill_sections(cls, data: Any) -> Any:
        """Прогнать каждую вложенную ``skills.<name>`` через ``SkillSettings``.

        Без этого валидатора pydantic не спускается в типизированные
        секции: ``SkillsSettings`` имеет ``extra="allow"`` (forward-compat
        для новых skill'ов по имени) и не описывает вложенные секции
        как типизированный ``dict[str, SkillSettings]``. В результате
        ``SkillSettings(extra="forbid")`` не срабатывал бы, и опечатки
        вроде ``tablse`` / забытый legacy ``embedding`` / ``cache``
        проходили бы валидацию.

        Этот ``@model_validator(mode="before")`` нормализует каждую
        вложенную секцию: если это dict — пропускает через
        ``SkillSettings.model_validate`` (что поднимет
        ``ConfigurationError`` при ``extra="forbid"`` нарушении).
        """
        if not isinstance(data, dict):
            return data

        normalized: dict[str, Any] = {}
        for name, cfg in data.items():
            if not isinstance(cfg, dict):
                normalized[name] = cfg
                continue
            try:
                validated = SkillSettings.model_validate(cfg)
            except ValidationError as exc:
                problems: list[str] = []
                for err in exc.errors():
                    p = ".".join(str(x) for x in err.get("loc", ()))
                    problems.append(f"  skills.{name}.{p}: {err.get('msg', 'invalid')}")
                raise ConfigurationError(
                    "Некорректная конфигурация config.json (skills."
                    f"{name}):\n" + "\n".join(problems)
                ) from exc
            normalized[name] = validated.model_dump(exclude_none=True)
        return normalized


class ProjectMetadataSettings(_StrictOptional):
    """Метаданные проекта (``project.*``; в файле — ``gateway.agent.project``).

    Канонический источник project metadata: ``config.json`` секция
    ``project``. Содержит релизные данные, читаемые runtime'ом через
    ``lib.utils.project_version.project_version()`` (для баннера
    ``gateway.py``) и как fallback-источник версии.

    Сейчас включает только ``version`` (SemVer-строка, без префикса
    ``v``; см. Release Process в ``AGENTS.md``). Дополнительные
    project-level metadata (``name``, ``description`` и т.п.)
    добавляются сюда по мере надобности.

    **Не** путать с ``ProjectSettings.version`` (которого больше нет)
    или с ``__version__`` библиотеки nanobot.
    """

    model_config = ConfigDict(extra="forbid")

    version: str | None = None


class ProjectSettings(BaseModel):
    """Корневая модель проектных настроек (проекция секций SETTINGS)."""

    model_config = ConfigDict(extra="allow")

    project: ProjectMetadataSettings | None = None
    channels: ChannelsSettings | None = None
    gateway: GatewaySettings | None = None
    cli: CliSettings | None = None
    logging: LoggingSettings | None = None
    skills: SkillsSettings | None = None
    enterprise_mcp: EnterpriseMcpSettings | None = None


class _LegacyGatewaySectionsError(Exception):
    """Маркер: внутри pydantic обнаружена legacy gateway-секция.

    Pydantic оборачивает любое исключение из ``model_validator(mode="before")``
    в свой ``ValidationError``, что размывает сообщение. ``validate_project_settings``
    ловит этот маркер, unwrap'ает, и поднимает чистую ``ConfigurationError``.
    """


# Известные legacy-переименования секций ``gateway.*``. Добавлять сюда при
# следующих rename'ах. Сообщение должно указывать на новый путь и на
# соответствующий блок CHANGELOG.md.
_LEGACY_GATEWAY_KEYS: dict[str, str] = {
    "vector_index": (
        "gateway.vector_index.* → gateway.vector.index.* "
        "(см. Migration notes в CHANGELOG.md :: skill-configuration-boundary)"
    ),
}


def validate_project_settings(settings: Any) -> ProjectSettings:
    """Валидировать SETTINGS; вернуть типизированную проекцию.

    Args:
        settings: merged SETTINGS (AttrDict/dict) из ``config.py``.

    Returns:
        ``ProjectSettings`` с распарсенными секциями.

    Raises:
        ConfigurationError: если хотя бы один известный ключ имеет неверный
            тип или недопустимое значение; сообщение содержит ВСЕ проблемы.
    """
    try:
        return ProjectSettings.model_validate(dict(settings or {}))
    except _LegacyGatewaySectionsError as exc:
        # pydantic не оборачивает произвольные Exception из mode="before"
        # (он оборачивает только ValueError/AssertionError). Маркер
        # _LegacyGatewaySectionsError пробрасывается как есть; поднимаем
        # чистую ConfigurationError.
        raise ConfigurationError(str(exc)) from exc
    except ValidationError as exc:
        # Unwrap _LegacyGatewaySectionsError, если pydantic всё-таки
        # обернул его (например, при изменении версии pydantic).
        for err in exc.errors():
            ctx = err.get("ctx") or {}
            inner = ctx.get("error")
            if isinstance(inner, _LegacyGatewaySectionsError):
                raise ConfigurationError(str(inner)) from exc
        problems: list[str] = []
        for err in exc.errors():
            path = ".".join(str(p) for p in err.get("loc", ()))
            msg = err.get("msg", "invalid")
            input_val = repr(err.get("input"))[:80]
            problems.append(f"  {path}: {msg} (получено: {input_val})")
        raise ConfigurationError(
            "Некорректная конфигурация config.json:\n" + "\n".join(problems)
        ) from exc
