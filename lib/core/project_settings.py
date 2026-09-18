"""ProjectSettings — типизированная валидация проектных настроек (pydantic).

Fail-fast граница конфигурации: неправильный тип или недопустимое значение
ключа ``project.json`` ловится на старте приложения (``ApplicationContext.
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
    project.json → config.json → .secrets.env.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from config import ConfigurationError

__all__ = [
    "ProjectSettings",
    "SkillBriefContextSettings",
    "SkillCliSettings",
    "SkillLlmSettings",
    "SkillSettings",
    "SkillsSettings",
    "SyncSettings",
    "TableEntry",
    "VectorIndexConfig",
    "VectorIndexEntry",
    "GatewaySettings",
    "VectorInfrastructureSettings",
    "validate_project_settings",
]


class _StrictOptional(BaseModel):
    """База для секций: неизвестные ключи разрешены, известные — типизированы."""

    model_config = ConfigDict(extra="allow")


class PostgresChannelSettings(_StrictOptional):
    worker_id: str | None = None
    claims_table: str | None = None
    claim_strategy: Literal["single", "worker_pool"] | None = None
    poll_interval: float | None = Field(default=None, gt=0)
    lease_interval: float | None = Field(default=None, gt=0)
    error_retry_delay: float | None = Field(default=None, ge=0)
    unstick_interval: float | None = Field(default=None, gt=0)
    processing_timeout: int | None = Field(default=None, gt=0)


class CompactSettings(_StrictOptional):
    enabled: bool | None = None
    notify_in_history: bool | None = None
    print_to_terminal: bool | None = None


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

    Путь в ``project.json``: ``gateway.vector.index.*`` (см.
    ``VectorInfrastructureSettings``). Раньше жил в ``gateway.vector_index.*`` —
    устаревший путь удалён, обратной совместимости нет (fail-fast).

    Attributes:
        enable: включён ли vector-indexing слой. ``None`` → дефолт ``True``.
        default_root: корневая папка FAISS-индексов. Дефолт
            ``"data_store/vectors"``. Путь к индексу = ``<root>/<name>``.
        backend: runtime-бэкенд (``"faiss"``, ``"pgvector"``, ``"qdrant"``).
        storage_table: единая PG-таблица-хранилище сырых эмбеддингов.
            Регистрируется в ``TableRegistry`` через ``register_infra``
            и попадает в DuckDB-кэш через ``PgDuckDbSyncService``.
        indexes: полный конфиг vector-индексов ``{имя: VectorIndexConfig}``
            (какие индексы строить, из каких source-таблиц, content_cols,
            embedding_cols, chunk-параметры, metric). Единственный источник
            для ``cache_provider_impl.read_vector_index_config`` и
            ``tools/build_vectors.py``.
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
    настраиваются: они захардкожены в ``cache_provider_impl.get_embedding()``
    (модульные константы + ``EMBED_TOKEN`` из окружения OS).
    Каноническое место для **общей** vector-инфраструктуры.
    """

    index: VectorIndexSettings | None = None


class HeartbeatSettings(_StrictOptional):
    enabled: bool | None = None
    intervalS: int | None = Field(default=None, gt=0)


class GatewaySettings(_StrictOptional):
    print_llm_calls: bool | None = None
    print_worker_activity: bool | None = None
    print_db_activity: bool | None = None
    llm_timeout: int | None = Field(default=None, gt=0)
    exec_timeout: int | None = Field(default=None, ge=0)
    compact: CompactSettings | None = None
    # duckdb_query / vector_search: Agent-facing tools удалены (этап 18).
    vector: VectorInfrastructureSettings | None = None
    heartbeat: HeartbeatSettings | None = None
    sync: SyncSettings | None = None
    cache: CacheSettings | None = None

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
                "Некорректная конфигурация project.json (legacy-секции gateway.*):\n"
                + "\n".join(problems)
            )
        return data


class SyncSettings(_StrictOptional):
    """Параметры фоновой синхронизации PG → DuckDB (PgDuckDbSyncService).

    Глобальные runtime-параметры, общие для всех skills. До рефакторинга
    жили в ``skills.audit_analyzer.sync.*``; вынесены в ``gateway.sync.*``,
    поскольку sync — это свойство runtime infrastructure, а не skill-домена.
    """

    poll_interval_sec: float | None = Field(default=None, gt=0)
    full_resync_every: int | None = Field(default=None, ge=0)
    max_queue_size: int | None = Field(default=None, gt=0)
    reconnect_backoff_sec: float | None = Field(default=None, gt=0)
    reconnect_backoff_max_sec: float | None = Field(default=None, gt=0)


class CacheSettings(_StrictOptional):
    """Параметры runtime-кеша (DuckDB-снапшот).

    **ЕДИНЫЙ механизм вычисления пути к кешу** —
    :func:`lib.core.application_context.resolve_publish_path`. Все
    слои runtime'а (gateway + CLI/skill) обязаны звать её, чтобы
    путь записи и путь чтения **совпадали**.

    Без настройки default — ``~/.cache/nanobot/duckdb/cache.duckdb``
    (POSIX ``fcntl`` работает там штатно; на NFS ATTACH падает с
    ``"Conflicting lock is held in PID 0"`` даже на свежем файле после
    ``rm`` — проверено эмпирически). Legacy
    ``<workspace>/data_store/duckdb/cache.duckdb`` **не поддерживается**
    (на NFS гарантированно ломается, и именно это расхождение между
    gateway и CLI было исходным багом v2.5.1 и ниже).

    Единственный knob:

      * ``local_path`` (str, опц.) — абсолютный/относительный (от workspace)
        путь к каталогу на **локальной** ФС, где будет лежать
        ``cache.duckdb``. Полезно, когда у ``~/.cache`` нет места или
        нужна отдельная ФС.

    Пример для jupyter-инсталляции, где ``~/.cache`` не подходит:

    .. code-block:: jsonc

        "gateway": {
          "cache": {
            "local_path": "/var/lib/nanobot/duckdb"
          }
        }
    """

    local_path: str | None = None


class CliSettings(_StrictOptional):
    show_context_window: bool | None = None
    max_iterations: int | None = Field(default=None, gt=0)


class StreamlitSettings(_StrictOptional):
    enabled: bool | None = None
    error_window_sec: float | None = Field(default=None, gt=0)


class ChannelsSettings(_StrictOptional):
    postgres: PostgresChannelSettings | None = None
    document_text_threshold: int | None = Field(default=None, ge=0)


class LoggingDbSettings(_StrictOptional):
    enabled: bool | None = None
    flush_interval_sec: float | None = Field(default=None, ge=0.5, le=60.0)

    @model_validator(mode="after")
    def _default_flush_interval_sec(self) -> "LoggingDbSettings":
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


# ---------------------------------------------------------------------------
# skills.<name> — универсальная декларация навыка (см. PHASE «унификация»).
# Каждый skill объявляется в project.json одной JSON-секцией; ApplicationContext
# авто-регистрирует ресурсы в table_registry при старте gateway. Никакого
# register.py не требуется.
# ---------------------------------------------------------------------------


class TableEntry(BaseModel):
    """Один ресурс skill'а в ``tables: [...]``.

    Единый формат для всех PG-таблиц skill'а: обычные таблицы, vector-таблицы,
    реестры метаданных, predefined scripts. Каждый ресурс имеет ``name``
    (обязательно) и опциональные атрибуты, которые runtime-sync либо
    игнорирует (``label``), либо читает (``tracking_column``, ``type``).

    Attributes:
        name: имя таблицы в формате ``schema.table`` (контракт
            ``TableResource.__post_init__``).
        type: ``"table"`` (по умолчанию) или ``"vector"``. Определяет,
            какой ``Resource`` создаёт ``_auto_register_skills``: обычный
            ``TableResource`` или ``VectorResource``. Не влияет на то,
            попадает ли таблица в DuckDB — это определяется автоматически
            по ``VectorResource``.
        label: opaque-метка. Если задана, таблица НЕ попадает в описание
            схемы для LLM (см. ``skill_config.get_db_tables()``) и доступна
            только через ``TableRegistry.resources_by_label(label)``.
            Типичный кейс: реестр метаданных
            (``public.agent_predefined_scripts`` с
            ``label="scripts_registry"``). Runtime-sync игнорирует.
        tracking_column: колонка для инкрементального поллинга. Дефолт
            ``updated_at`` для обычных, ``id`` для vector.

    Unknown keys запрещены (``extra="forbid"``) — fail-fast на опечатках
    в ``project.json``.
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
    настройка, переносится в ``project.json``.

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
    в ``project.json``.
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
    (``temperature``, ``max_tokens``). Выбор модели — в ``config.json``
    (``agents.defaults.*``). См. ``llm_config.resolve_llm_config()``.
    """

    max_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = Field(default=None, ge=0, le=2)


class SkillChunkingSettings(_StrictOptional):
    """Секция ``chunking`` — параметры map-reduce чанкинга для длинных текстов (необязательно).

    Управляет поведением ``lib.services.text_splitter.split_text``
    внутри skill'а: для текстов короче ``single_call_threshold`` —
    один вызов LLM; для длиннее — разбиение с перекрытием.
    Используется навыком ``legal_summarizer`` (map-reduce LLM).
    ``text_splitter`` для эмбеддингов живёт отдельно в ``vector.*``.

    Размер чанка управляется через ``chunk_size_input_ratio``
    (доля от ``agents.defaults.contextWindowTokens``) — это основной
    источник. ``chunk_size`` — fallback (если ratio не задано или
    контекстное окно неизвестно).
    """

    chunk_size: int | None = Field(default=None, gt=0)
    chunk_overlap: int | None = Field(default=None, ge=0)
    single_call_threshold: int | None = Field(default=None, gt=0)
    chunk_size_input_ratio: float | None = Field(default=None, gt=0, le=1)


class SkillBriefContextSettings(_StrictOptional):
    """Секция ``brief_context`` — параметры ``BriefContextBuilder`` (необязательно).

    Используется навыком ``legal_summarizer``: brief собирает ровно один
    структурный ``Chunk`` из DocumentStructure + PhysicalDocument.
    ``max_chars`` рассчитывается динамически из contextWindowTokens и
    ``chunking.brief_input_ratio``; эти поля — резервные параметры.
    Дефолты согласованы с ``BriefContextConfig``
    в ``workspace/skills/legal_summarizer/scripts/application/brief_context.py``
    (см. ``lib.core.skill_config.get_brief_context_config``).
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
    context batching (Phase 2B для ``legal_summarizer``).
    """

    confirmation_threshold_sec: float | None = Field(default=None, gt=0)
    estimated_chunk_duration_sec: float | None = Field(default=None, gt=0)
    max_chunks_for_execution: int | None = Field(default=None, gt=0)
    context_batching: SkillExecutionContextBatchingSettings | None = None


class SkillSettings(BaseModel):
    """Универсальная декларация навыка в ``project.json::skills.<name>``.

    Это **единственный источник истины** для регистрации skill'а:
    ApplicationContext читает эту секцию и создаёт ресурсы в
    ``table_registry`` без всякого ``register.py``.

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
    на опечатках в ``project.json`` (например, ``tablse`` вместо
    ``tables`` сразу поднимет ``ConfigurationError`` на старте gateway,
    а не тихо пройдёт валидацию). Имя skill'а остаётся динамическим —
    добавляется простым добавлением секции в ``project.json``; форма
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
    ``project.json``; опечатки внутри секции (``tablse``, ``embedding``,
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
                    "Некорректная конфигурация project.json (skills."
                    f"{name}):\n" + "\n".join(problems)
                ) from exc
            normalized[name] = validated.model_dump(exclude_none=True)
        return normalized


class ProjectMetadataSettings(_StrictOptional):
    """Метаданные проекта (``project.json::project.*``).

    Канонический источник project metadata: ``project.json`` секция
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
    streamlit: StreamlitSettings | None = None
    logging: LoggingSettings | None = None
    skills: SkillsSettings | None = None


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
            "Некорректная конфигурация project.json:\n" + "\n".join(problems)
        ) from exc
