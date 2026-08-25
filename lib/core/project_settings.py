"""ProjectSettings — типизированная валидация проектных настроек (pydantic).

Fail-fast граница конфигурации: неправильный тип или недопустимое значение
ключа ``project.json`` ловится на старте приложения (``ApplicationContext.
create``), а не в рантайме канала/сервиса.

Принципы:
  - все ключи опциональны с дефолтами: отсутствие настройки не ошибка
    (дефолты живут в потребителях через ``get_setting``);
  - неверный ТИП или значение — ошибка: ``ConfigurationError`` со списком
    всех проблем сразу;
  - неизвестные ключи разрешены (extra="allow") — forward-совместимость;
  - единственный источник правды — SETTINGS после мержа
    project.json → config.json → .secrets.env.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from config import ConfigurationError

__all__ = [
    "ProjectSettings",
    "SkillCacheSettings",
    "SkillCliSettings",
    "SkillEmbeddingSettings",
    "SkillLlmSettings",
    "SkillSettings",
    "SkillsSettings",
    "SyncSettings",
    "TableEntry",
    "VectorIndexEntry",
    "GatewaySettings",
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


class DuckDbQuerySettings(_StrictOptional):
    enable: bool | None = None
    max_rows: int | None = Field(default=None, gt=0)
    max_result_chars: int | None = Field(default=None, gt=0)
    query_timeout_sec: float | None = Field(default=None, gt=0)


class VectorSearchSettings(_StrictOptional):
    enable: bool | None = None
    default_top_k: int | None = Field(default=None, gt=0)
    max_top_k: int | None = Field(default=None, gt=0)
    default_threshold: float | None = Field(default=None, ge=0, le=1)
    max_query_chars: int | None = Field(default=None, gt=0)
    max_result_chars: int | None = Field(default=None, gt=0)
    timeout_sec: float | None = Field(default=None, gt=0)


class VectorIndexSettings(_StrictOptional):
    """Параметры FAISS-хранилища индексов (общая инфраструктура).

    До рефакторинга backend-specific поля (default_path, storage-таблица
    ``oarb.audit_vectors``) жили прямо в skill'е через ``tables[]`` с
    ``type="vector"``. Это смешивало инфраструктуру и домен навыка: storage
    общий на все skill'ы, не относится к конкретному навыку. Теперь:

      * ``storage_tables`` — список PG-таблиц-хранилищ сырых эмбеддингов
        (одна общая или несколько; имя в формате ``schema.table``).
      * ``default_root`` — корневая папка FAISS-индексов; путь к индексу =
        ``<default_root>/<index_name>``.

    Attributes:
        enable: включён ли vector-indexing слой. ``None`` → дефолт ``True``.
        default_root: корневая папка FAISS-индексов. Дефолт
            ``"data_store/vectors"``. Путь к индексу = ``<root>/<name>``.
        backend: runtime-бэкенд построения индексов. ``"faiss"`` (по
            умолчанию), ``"pgvector"``, ``"qdrant"`` и т.п.
        storage_tables: список PG-таблиц-хранилищ сырых эмбеддингов
            (по схеме-имени, например ``["oarb.audit_vectors"]``). Эти
            таблицы регистрируются как ``VectorResource`` в table_registry
            и попадают в DuckDB-кэш.
    """

    enable: bool | None = None
    default_root: str | None = None
    backend: str | None = None
    storage_tables: list[str] | None = None


class HeartbeatSettings(_StrictOptional):
    enabled: bool | None = None
    intervalS: int | None = Field(default=None, gt=0)


class GatewaySettings(_StrictOptional):
    print_llm_calls: bool | None = None
    print_worker_activity: bool | None = None
    print_db_activity: bool | None = None
    llm_timeout: int | None = Field(default=None, gt=0)
    exec_timeout: int | None = Field(default=None, gt=0)
    compact: CompactSettings | None = None
    duckdb_query: DuckDbQuerySettings | None = None
    vector_search: VectorSearchSettings | None = None
    vector_index: VectorIndexSettings | None = None
    heartbeat: HeartbeatSettings | None = None
    sync: SyncSettings | None = None


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


class CliSettings(_StrictOptional):
    show_context_window: bool | None = None
    max_iterations: int | None = Field(default=None, gt=0)


class StreamlitSettings(_StrictOptional):
    enabled: bool | None = None
    error_window_sec: float | None = Field(default=None, gt=0)


class ChannelsSettings(_StrictOptional):
    postgres: PostgresChannelSettings | None = None


class LoggingDbSettings(_StrictOptional):
    enabled: bool | None = None


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
    type: str = "table"
    label: str | None = None
    tracking_column: str | None = None


class VectorIndexEntry(BaseModel):
    """Один vector-storage индекс в ``vector_indexes: [...]``.

    Минимальный generic-контракт: имя индекса + источник данных.
    Алгоритм построения (FAISS / pgvector / Qdrant / иной бэкенд),
    параметры чанкинга и формат хранения — это runtime-параметры конкретного
    бэкенда, **общая инфраструктура** (см. ``gateway.vector_index.*``),
    а не часть декларации ресурса.

    Attributes:
        name: логическое имя индекса (``"audits_index"``, ``"products_v"``).
        source: имя PG-таблицы-источника сырых эмбеддингов.

    Backend-specific параметры (для FAISS: ``text_chunk_size``,
    ``text_chunk_overlap``, ``build_batch_pause_sec``; для Qdrant:
    ``collection_name``; для pgvector: ``vector_column``) — это OPTIONAL
    ключи с ``extra="allow"``. Они читаются конкретным runtime-бэкендом,
    не валидируются здесь.
    """

    model_config = ConfigDict(extra="allow")

    name: str
    source: str


class SkillEmbeddingSettings(_StrictOptional):
    """Секция ``embedding`` — параметры эмбеддингов (Ollama /api/embed).

    Пишется в ``table_registry.set_embedding_config(...)`` при регистрации
    skill'а и далее читается ``lib/services/cache_provider_impl.get_embedding``.
    """

    base_url: str | None = None
    model: str | None = None
    dimension: int | None = Field(default=None, gt=0)
    http_timeout_sec: float | None = Field(default=None, gt=0)


class SkillCacheSettings(_StrictOptional):
    """Секция ``cache`` — параметры in-memory кэша (DuckDB).

    Снимок общий для всех skill'ов (``workspace/data_store/duckdb/cache.duckdb``,
    см. ``TableRegistry.snapshot_path()``); путь НЕ per-skill.
    """

    enabled: bool | None = None
    engine: str | None = None
    max_age_sec: float | None = Field(default=None, gt=0)
    refresh_interval_sec: float | None = Field(default=None, gt=0)


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
    """Секция ``llm`` — переопределение LLM для навыка (необязательно)."""

    max_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = Field(default=None, ge=0, le=2)


class SkillSettings(_StrictOptional):
    """Универсальная декларация навыка в ``project.json::skills.<name>``.

    Это **единственный источник истины** для регистрации skill'а:
    ApplicationContext читает эту секцию и создаёт ресурсы в
    ``table_registry`` без всякого ``register.py``.

    Секции:

      * ``tables`` — единый список ресурсов (PG-таблицы + vector-источники);
      * ``vector_indexes`` — какие vector-индексы нужны skill'у
        (min-контракт: имя + источник; runtime определяет бэкенд);
      * ``embedding`` — параметры эмбеддингов (если есть vector);
      * ``cache`` — параметры in-memory кэша;
      * ``cli`` — параметры CLI навыка;
      * ``llm`` — переопределение LLM для навыка (опционально).

    Глобальные параметры синхронизации PG → DuckDB теперь лежат в
    ``gateway.sync.*`` (не per-skill), см. ``GatewaySettings.sync``.

    Корневой ``enabled`` отключает skill без удаления секции.
    """

    enabled: bool | None = None
    tables: list[str | TableEntry] | None = None
    vector_indexes: list[VectorIndexEntry] | None = None
    embedding: SkillEmbeddingSettings | None = None
    cache: SkillCacheSettings | None = None
    cli: SkillCliSettings | None = None
    llm: SkillLlmSettings | None = None


class SkillsSettings(_StrictOptional):
    """Контейнер для всех навыков: ``skills.<name>``.

    Универсальная схема: каждый skill — это ``SkillSettings``. Любой новый
    skill добавляется простым добавлением секции в ``project.json``.
    """

    # Skill-секции не типизируем жёстко по имени (forward-compat): используем
    # ``dict[str, Any]``. ИЗВЕСТНЫЕ ключи (например, ``audit_analyzer``) можно
    # добавить сюда как alias, если потребуется; сейчас — единая модель для всех.
    model_config = ConfigDict(extra="allow")


class ProjectSettings(BaseModel):
    """Корневая модель проектных настроек (проекция секций SETTINGS)."""

    model_config = ConfigDict(extra="allow")

    version: str | None = None
    channels: ChannelsSettings | None = None
    gateway: GatewaySettings | None = None
    cli: CliSettings | None = None
    streamlit: StreamlitSettings | None = None
    logging: LoggingSettings | None = None
    skills: SkillsSettings | None = None


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
    except ValidationError as exc:
        problems: list[str] = []
        for err in exc.errors():
            path = ".".join(str(p) for p in err.get("loc", ()))
            msg = err.get("msg", "invalid")
            input_val = repr(err.get("input"))[:80]
            problems.append(f"  {path}: {msg} (получено: {input_val})")
        raise ConfigurationError(
            "Некорректная конфигурация project.json:\n" + "\n".join(problems)
        ) from exc
