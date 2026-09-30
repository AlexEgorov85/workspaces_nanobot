# Work brief `01-core-entrypoints`

Product files: **12**, LOC: **5680**

## `lib/core/application_context.py` — 1816 LOC (code 1336)
- module: `lib.core.application_context`
- docstring: ApplicationContext — единая точка создания и связывания сервисов. Создаёт все общие сервисы (конфиг, БД-логирование, аудит-сервисы, шина сообщений, хранилище сессий, агент) и публикует их атрибутами. Точки входа (gateway
- static importers (20): `benchmarks/runner.py`, `cli_agent.py`, `gateway.py`, `lib/core/skill_config.py`, `lib/services/cache_provider.py`, `tests/test_application_context.py`, `tests/test_application_context_cache_lifecycle.py`, `tests/test_application_context_logging.py`, `tests/test_application_context_role.py`, `tests/test_application_context_schema_validation.py`, `tests/test_application_context_single_application_point.py`, `tests/test_auto_register_skills.py`, `tests/test_gateway.py`, `tests/test_gateway_live_media_e2e.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 24

### class `ApplicationContext` — lines 125-730 (606 LOC), 4 methods
- bases: object
- decorators: —
- docstring: Контекст приложения: конфиг + все сервисы.
- name referenced in 15 file(s); tests: 11

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `create` | 195-510 | `(cls, script_dir: Path, workspace_dir: Path, *, role: Literal['gateway', 'cli'], storage_o` | 23 | 0 | 15 | Собрать контекст приложения. Args: script_dir: корень проекта (где лежит config.json). workspace_dir: корень w |
| `start` | 516-614 | `(self) -> None` | 15 | 0 | 21 | Запустить фоновые сервисы (БД-логирование, аудит). |
| `stop` | 616-676 | `(self) -> None` | 16 | 0 | 16 | Корректно остановить все фоновые сервисы. |
| `_validate_runtime_schema` | 678-730 | `(self) -> None` | 13 | 1 | 2 | Pre-startup проверка наличия обязательных runtime-таблиц. Вызывается из ``start()`` сразу после ``_start_db_po |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_resolve_enable_kwargs` | 66-122 | `(kwargs: dict[str, Any], *, gateway_settings: dict[str, Any] | None=None) -> dict[str, Any` | 11 | 2 | Извлечь deprecated ``enable_*``/``print_llm_calls`` из ``**kwargs``. Возвращает dict со всеми DEPRECATED_ENABL |
| `_log_connected_hooks` | 738-758 | `(ctx: ApplicationContext) -> None` | 6 | 1 | Однократно вывести полный список подключённых хуков. Единая точка вывода: плагины ``workspace/hooks/`` + фрейм |
| `_emit_hook_inventory_banner` | 761-832 | `(ctx: ApplicationContext) -> None` | 16 | 1 | Промпт-сводка по хукам через ``runtime_inventory.diff_hooks``. Печатает: * (нет вывода) — все required хуки на |
| `_emit_patch_inventory_banner` | 835-900 | `(patch_report: Any) -> None` | 16 | 1 | Промпт-сводка по runtime-патчам через ``runtime_inventory.diff_runtime_patches``. Печатает красный блок, если  |
| `_emit_project_tools_inventory_banner` | 903-995 | `(project_tools_result: Any) -> None` | 23 | 2 | Промпт-сводка по project tools через ``runtime_inventory``. Использует **структурные поля** ``ProjectToolsLoad |
| `_register_readiness_checks` | 998-1102 | `(ctx: ApplicationContext) -> None` | 15 | 1 | Зарегистрировать проверки зависимостей для RuntimeReadiness. Профили: * ``postgres`` — required. Если БД досту |
| `_make_config_service` | 1105-1126 | `(script_dir: Path, workspace_dir: Path, *, settings_override: Any | None=None) -> Any` | 1 | 1 | Создать ``ConfigService``, привязанный к корню проекта. Использует lazy-import, чтобы не зависеть от ``config. |
| `_resolve_agent_id` | 1129-1140 | `(config: Any) -> str` | 7 | 2 | Получить идентификатор агента из конфигурации (или ``"main"``). |
| `_make_db_logging` | 1143-1213 | `(ctx: ApplicationContext) -> Any | None` | 25 | 1 | Собрать ``DbLoggingService`` из секции ``logging.db`` в settings. Возвращает ``None`` если: * ``logging.db.ena |
| `_default_local_cache_dir` | 1216-1232 | `() -> 'Path'` | 1 | 2 | Безопасный default для runtime-кеша: ``~/.cache/nanobot/duckdb``. DuckDB ATTACH берёт эксклюзивный flock, кото |
| `resolve_cache_path` | 1235-1307 | `(workspace_path, cache_cfg: dict | None=None) -> str` | 7 | 5 | **ЕДИНЫЙ** механизм вычисления пути к файлу кэша ``cache.duckdb``. Имя функции исторически было ``resolve_publ |
| `_warn_if_cache_path_on_nfs` | 1310-1369 | `(cache_path: str) -> None` | 10 | 2 | Если ``cache_path`` живёт на NFS — напечатать громкое предупреждение. Используется ``/proc/mounts`` (только Li |
| `_init_cache_runtime` | 1372-1563 | `(ctx: ApplicationContext) -> tuple` | 25 | 2 | Загрузить кэш и открыть его на чтение: ``(CacheProvider, CacheLoadService)``. Список таблиц берётся из ``Table |
| `_record_sync_skipped` | 1566-1595 | `(db_logging_service: Any, event_type: str, reason: str, detail: str) -> None` | 1 | 0 | DEPRECATED: инлайнен в ``_init_cache_runtime``. Оставлен как back-compat shim для возможных внешних callers'ов |
| `_auto_register_skills` | 1600-1612 | `(ctx: ApplicationContext) -> None` | 4 | 2 | Зарегистрировать skills из ``project.json::skills.*`` в ``table_registry``. Делегирует ``lib.core.skill_regist |
| `_register_infra_resources` | 1618-1633 | `(ctx: ApplicationContext) -> None` | 1 | 1 | Зарегистрировать инфраструктурные ресурсы runtime'а. Делегирует ``lib.core.infra_registration`` — общую логику |
| `_make_transcription` | 1636-1645 | `(config: Any) -> Any` | 1 | 1 | Создать ``TranscriptionService`` для настройки Postgres-канала. ``TranscriptionService`` достаёт API-ключ/URL/ |
| `_make_preload` | 1648-1663 | `(settings: Any, db_logging_service: Any | None=None) -> Any` | 1 | 1 | Создать ``PreloadService`` (для gateway — FAISS preload, для CLI — кеш навыка). ``settings`` — полные ``SETTIN |
| `_make_cron_service` | 1666-1675 | `(config: Any) -> Any` | 1 | 1 | Создать ``CronService`` для CLI-режима (только там он нужен). ``CronService`` хранит задачи в ``workspace/cron |
| `_make_session_cold_sync_service` | 1678-1750 | `(ctx: ApplicationContext) -> Any | None` | 12 | 2 | Создать ``SessionColdSyncService`` (cold-storage mirror). Сервис создаётся только если: - есть ``session_manag |
| `_make_usage_store` | 1753-1769 | `(ctx: ApplicationContext) -> Any | None` | 3 | 3 | Создать ``LLMUsageStore`` (upstream nanobot) по конфигу. Конфиг — ``gateway.usage_store.*`` (``sqlite_path``,  |
| `_configure_db_pool` | 1777-1796 | `(pool_cfg: dict, print_activity: bool=False) -> None` | 2 | 1 | Применить ``channels.postgres.pool`` к общему пулу ``utils.db``. ``pool_cfg`` — словарь с ключами ``min_conn/m |
| `_start_db_pool` | 1799-1806 | `() -> None` | 2 | 1 | Запустить общий пул ``utils.db`` (воркеры подключаются лениво). |
| `_stop_db_pool` | 1809-1816 | `() -> None` | 2 | 1 | Остановить общий пул ``utils.db`` и закрыть все соединения. |

## `lib/core/project_settings.py` — 759 LOC (code 571)
- module: `lib.core.project_settings`
- docstring: ProjectSettings — типизированная валидация проектных настроек (pydantic). Fail-fast граница конфигурации: неправильный тип или недопустимое значение ключа ``project.json`` ловится на старте приложения (``ApplicationConte
- static importers (5): `lib/core/application_context.py`, `tests/test_application_context_logging.py`, `tests/test_auto_register_skills.py`, `tests/test_config_keys.py`, `tests/test_project_settings.py`
- string/dynamic refs: 2
- test files touching it: — none —
- classes: 32, module functions: 1

### class `_StrictOptional` — lines 48-51 (4 LOC), 0 methods
- bases: BaseModel
- decorators: —
- docstring: База для секций: неизвестные ключи разрешены, известные — типизированы.
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `PostgresChannelSettings` — lines 54-62 (9 LOC), 0 methods
- bases: _StrictOptional
- decorators: —
- docstring: — NONE —
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `CompactSettings` — lines 65-68 (4 LOC), 0 methods
- bases: _StrictOptional
- decorators: —
- docstring: — NONE —
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `StartupSchemaValidationSettings` — lines 71-91 (21 LOC), 0 methods
- bases: _StrictOptional
- decorators: —
- docstring: Pre-startup проверка наличия обязательных runtime-таблиц. При ``enabled=True`` (по умолчанию) ``ApplicationContext.start()`` выполняет один ``SELECT`` к ``information_schema.tables`` для 6 таблиц из ``SETTINGS["channels"
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `StartupSettings` — lines 94-97 (4 LOC), 0 methods
- bases: _StrictOptional
- decorators: —
- docstring: Секция ``gateway.startup.*`` — параметры pre-startup валидации.
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `ErrorMessagesSettings` — lines 100-127 (28 LOC), 0 methods
- bases: _StrictOptional
- decorators: —
- docstring: Заготовленные ответы при internal-ошибке ``AgentLoop._process_message``. Используется патчем ``RuntimePatcher.patch_turn_delivery_fail`` (см. спеку ``openspec/specs/runtime/error-fallback``): при любом ``Exception`` в up
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `VectorIndexSettings` — lines 134-167 (34 LOC), 0 methods
- bases: _StrictOptional
- decorators: —
- docstring: Параметры FAISS-инфраструктуры. Хранилище эмбеддингов и сами индексы — общий runtime, не привязанный к домену skill'а. Доменные таблицы, нужные в DuckDB-кэше, декларируются в ``skills.<name>.tables[]``. Какие индексы стр
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `VectorInfrastructureSettings` — lines 170-180 (11 LOC), 0 methods
- bases: _StrictOptional
- decorators: —
- docstring: Векторная инфраструктура (``gateway.vector.*``): индексы. Содержит ``index`` — ``VectorIndexSettings`` (конфиг индексов, storage-таблица). Параметры подключения к эмбеддеру больше не настраиваются: они захардкожены в ``c
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `HeartbeatSettings` — lines 183-185 (3 LOC), 0 methods
- bases: _StrictOptional
- decorators: —
- docstring: — NONE —
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `UsageStoreSettings` — lines 188-199 (12 LOC), 0 methods
- bases: _StrictOptional
- decorators: —
- docstring: Параметры upstream ``LLMUsageStore`` (``gateway.usage_store.*``). ``sqlite_path`` — путь к SQLite-файлу (по умолчанию — ``<get_runtime_subdir("usage")>/usage.db``). ``enabled=False`` отключает запись LLM-usage (graceful 
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `SessionColdSyncSettings` — lines 202-214 (13 LOC), 0 methods
- bases: _StrictOptional
- decorators: —
- docstring: Параметры ``SessionColdSyncService`` (``gateway.session_cold_sync.*``). Cold-storage mirror upstream JSONL → PG. Все ключи опциональны. См. спеку ``openspec/specs/storage/session-hybridization/spec.md`` и design D23 (sta
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `GatewaySettings` — lines 217-262 (46 LOC), 1 methods
- bases: _StrictOptional
- decorators: —
- docstring: — NONE —
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `_reject_legacy_renamed_sections` | 235-262 | `(cls, data: Any) -> Any` | 6 | 0 | 0 | Fail-fast на legacy-переименованных секциях ``gateway.*``. ``GatewaySettings`` унаследован от ``_StrictOptiona |

### class `CacheSettings` — lines 265-299 (35 LOC), 0 methods
- bases: _StrictOptional
- decorators: —
- docstring: Параметры runtime-кеша (DuckDB-снапшот). **ЕДИНЫЙ механизм вычисления пути к кешу** — :func:`lib.core.application_context.resolve_cache_path`. Все слои runtime'а (gateway + CLI/skill) обязаны звать её, чтобы путь записи 
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `CliSettings` — lines 302-304 (3 LOC), 0 methods
- bases: _StrictOptional
- decorators: —
- docstring: — NONE —
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `StreamlitSettings` — lines 307-309 (3 LOC), 0 methods
- bases: _StrictOptional
- decorators: —
- docstring: — NONE —
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `ChannelsSettings` — lines 312-314 (3 LOC), 0 methods
- bases: _StrictOptional
- decorators: —
- docstring: — NONE —
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `LoggingDbSettings` — lines 317-337 (21 LOC), 1 methods
- bases: _StrictOptional
- decorators: —
- docstring: — NONE —
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `_default_flush_interval_sec` | 322-337 | `(self) -> 'LoggingDbSettings'` | 2 | 0 | 0 | Подменить ``None`` на канонический дефолт ``5.0``. Спека change ``improve-history-search-pagination-and-loggin |

### class `LoggingSettings` — lines 340-341 (2 LOC), 0 methods
- bases: _StrictOptional
- decorators: —
- docstring: — NONE —
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `TableEntry` — lines 352-386 (35 LOC), 0 methods
- bases: BaseModel
- decorators: —
- docstring: Один ресурс skill'а в ``tables: [...]``. Единый формат для всех PG-таблиц skill'а: обычные таблицы, vector-таблицы, реестры метаданных, predefined scripts. Каждый ресурс имеет ``name`` (обязательно) и опциональные атрибу
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `VectorIndexEntry` — lines 389-418 (30 LOC), 0 methods
- bases: BaseModel
- decorators: —
- docstring: Один vector-storage индекс в ``vector_indexes: [...]``. Минимальный generic-контракт: **только имя** индекса. Источник эмбеддингов (PG-таблица исходных строк), алгоритм построения (FAISS / pgvector / Qdrant / иной бэкенд
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `VectorIndexConfig` — lines 421-463 (43 LOC), 0 methods
- bases: BaseModel
- decorators: —
- docstring: Полный конфиг одного vector-индекса (``gateway.vector.index.indexes``). Единственный источник деталей построения индекса: исходная таблица (``table``), первичный ключ (``pk``), логическое имя источника (``source_table``)
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `SkillCliSettings` — lines 466-476 (11 LOC), 0 methods
- bases: _StrictOptional
- decorators: —
- docstring: Секция ``cli`` — параметры CLI навыка (например, ``audit_analyze``). Это **специфические настройки skill'а**: режимы, форматы вывода, таймауты. У разных skill'ов могут быть разные CLI-флаги.
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `SkillLlmSettings` — lines 479-488 (10 LOC), 0 methods
- bases: _StrictOptional
- decorators: —
- docstring: Секция ``llm`` — execution policy генерации для навыка (необязательно). Это НЕ выбор модели/провайдера — это runtime-параметры вызова (``temperature``, ``max_tokens``). Выбор модели — в ``config.json`` (``agents.defaults
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `SkillChunkingSettings` — lines 491-509 (19 LOC), 0 methods
- bases: _StrictOptional
- decorators: —
- docstring: Секция ``chunking`` — параметры map-reduce чанкинга для длинных текстов (необязательно). Управляет поведением ``lib.services.text_splitter.split_text`` внутри skill'а: для текстов короче ``single_call_threshold`` — один 
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `SkillBriefContextSettings` — lines 512-526 (15 LOC), 0 methods
- bases: _StrictOptional
- decorators: —
- docstring: Секция ``brief_context`` — параметры ``BriefContextBuilder`` (необязательно). Используется навыком ``legal_summarizer``: brief собирает ровно один структурный ``Chunk`` из DocumentStructure + PhysicalDocument. ``max_char
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `SkillExecutionContextBatchingSettings` — lines 529-543 (15 LOC), 0 methods
- bases: _StrictOptional
- decorators: —
- docstring: Параметры context batching для skill'а (опционально). ``chars_per_token`` — оценка токенов для русского текста. ``system_prompt_tokens`` / ``instruction_tokens_per_map`` — резерв под system prompt и user_body. ``safety_m
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `SkillExecutionSettings` — lines 546-558 (13 LOC), 0 methods
- bases: _StrictOptional
- decorators: —
- docstring: Секция ``execution`` — параметры запуска skill'а (необязательно). Управляет подтверждением длинных операций (``confirmation_required``), оценкой длительности (``estimated_chunk_duration_sec``), safety net (``max_chunks_f
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `SkillSettings` — lines 561-604 (44 LOC), 0 methods
- bases: BaseModel
- decorators: —
- docstring: Универсальная декларация навыка в ``project.json::skills.<name>``. Это **единственный источник истины** для регистрации skill'а: ApplicationContext читает эту секцию и создаёт ресурсы в ``table_registry`` без всякого ``r
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `SkillsSettings` — lines 607-663 (57 LOC), 1 methods
- bases: _StrictOptional
- decorators: —
- docstring: Контейнер для всех навыков: ``skills.<name>``. Имя skill'а — произвольное (forward-compat), но **форма** секции строго типизирована через ``SkillSettings`` (``extra="forbid"``). Любой новый skill добавляется простым доба
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `_validate_skill_sections` | 627-663 | `(cls, data: Any) -> Any` | 11 | 0 | 0 | Прогнать каждую вложенную ``skills.<name>`` через ``SkillSettings``. Без этого валидатора pydantic не спускает |

### class `ProjectMetadataSettings` — lines 666-685 (20 LOC), 0 methods
- bases: _StrictOptional
- decorators: —
- docstring: Метаданные проекта (``project.json::project.*``). Канонический источник project metadata: ``project.json`` секция ``project``. Содержит релизные данные, читаемые runtime'ом через ``lib.utils.project_version.project_versi
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `ProjectSettings` — lines 688-699 (12 LOC), 0 methods
- bases: BaseModel
- decorators: —
- docstring: Корневая модель проектных настроек (проекция секций SETTINGS).
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `_LegacyGatewaySectionsError` — lines 702-708 (7 LOC), 0 methods
- bases: Exception
- decorators: —
- docstring: Маркер: внутри pydantic обнаружена legacy gateway-секция. Pydantic оборачивает любое исключение из ``model_validator(mode="before")`` в свой ``ValidationError``, что размывает сообщение. ``validate_project_settings`` лов
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `validate_project_settings` | 722-759 | `(settings: Any) -> ProjectSettings` | 16 | 3 | Валидировать SETTINGS; вернуть типизированную проекцию. Args: settings: merged SETTINGS (AttrDict/dict) из ``c |

## `config.py` — 725 LOC (code 546)
- module: `config`
- docstring: — NONE —
- static importers (54): `benchmarks/db.py`, `cli_agent.py`, `gateway.py`, `lib/cli/console_loop.py`, `lib/core/application_context.py`, `lib/core/infra_registration.py`, `lib/core/project_settings.py`, `lib/core/skill_config.py`, `lib/lifecycle/gateway_runner.py`, `lib/services/cache_provider.py`, `lib/services/cache_provider_impl.py`, `lib/services/config_service.py`, `lib/services/llm_config.py`, `lib/services/runtime_patcher.py`
- string/dynamic refs: 0
- test files touching it: `tests/conftest.py`, `tests/integration/test_worker_pool_concurrency.py`, `tests/test_application_context.py`, `tests/test_benchmarks_runner.py`, `tests/test_cache_provider_open_failure.py`, `tests/test_cli_agent.py`, `tests/test_cli_agent_profile.py`, `tests/test_config_service.py`, `tests/test_gateway.py`, `tests/test_gateway_live_media_e2e.py`
- classes: 3, module functions: 21

### class `AttrDict` — lines 17-26 (10 LOC), 2 methods
- bases: dict
- decorators: —
- docstring: — NONE —
- name referenced in 3 file(s); tests: 2

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__getattr__` | 18-23 | `(self, name)` | 3 | 0 | 0 | — |
| `__setattr__` | 25-26 | `(self, name, val)` | 1 | 0 | 3 | — |

### class `ConfigurationError` — lines 230-241 (12 LOC), 0 methods
- bases: ValueError
- decorators: —
- docstring: Ошибка конфигурации: обязательный ключ отсутствует или некорректен. В отличие от ``get_setting`` (возвращает переданный ``default``), ``require_setting`` выбрасывает эту ошибку, чтобы отсутствие настройки не маскировалос
- name referenced in 26 file(s); tests: 18

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `_LazySettings` — lines 498-587 (90 LOC), 14 methods
- bases: object
- decorators: —
- docstring: Compatibility proxy для ``SETTINGS``. Состояния: UNINITIALIZED (пустой ``_inner_dict``) и INITIALIZED (заполненный ``_inner_dict`` — ``AttrDict``, построенный ``resolve_application_config``). Переключение — только через 
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 515-516 | `(self) -> None` | 1 | 0 | 6 | — |
| `_ensure_initialized` | 518-524 | `(self) -> AttrDict` | 2 | 10 | 1 | — |
| `__getitem__` | 526-527 | `(self, key: str) -> Any` | 1 | 0 | 0 | — |
| `__setitem__` | 529-536 | `(self, key: str, value: Any) -> None` | 1 | 0 | 0 | ``SETTINGS[k] = v`` для legacy-тестов, мутирующих proxy in-place. Допустимо только в INITIALIZED state — UNINI |
| `__delitem__` | 538-540 | `(self, key: str) -> None` | 1 | 0 | 0 | — |
| `__getattr__` | 542-551 | `(self, name: str) -> Any` | 3 | 0 | 0 | — |
| `__contains__` | 553-556 | `(self, key: str) -> bool` | 2 | 0 | 1 | — |
| `__iter__` | 558-559 | `(self)` | 1 | 0 | 0 | — |
| `__len__` | 561-562 | `(self) -> int` | 1 | 0 | 0 | — |
| `__repr__` | 564-567 | `(self) -> str` | 3 | 0 | 0 | — |
| `get` | 569-578 | `(self, key: str, default: Any=None) -> Any` | 2 | 0 | 149 | Mapping-style ``.get`` — используется в некоторых существующих путях (``SETTINGS.get('channels', {})``); при U |
| `items` | 580-581 | `(self)` | 1 | 0 | 49 | — |
| `keys` | 583-584 | `(self)` | 1 | 0 | 17 | — |
| `values` | 586-587 | `(self)` | 1 | 0 | 15 | — |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_parse_value` | 29-54 | `(val: str)` | 12 | 2 | — |
| `_header_to_prefix` | 57-59 | `(header: str) -> list[str]` | 2 | 2 | — |
| `load_env` | 62-90 | `(path: str | Path | None=None) -> AttrDict` | 11 | 2 | — |
| `_strip_jsonc_comments` | 93-138 | `(text: str) -> str` | 18 | 5 | Удалить ``//`` и ``/* */`` комментарии из JSON (JSONC), не трогая строки. Сохраняет содержимое строковых литер |
| `load_config_json` | 141-159 | `(path: str | Path | None=None) -> AttrDict` | 6 | 2 | Загрузить JSON/JSONC-файл в AttrDict; несуществующий/битый файл → пустой AttrDict. Поддерживает комментарии `` |
| `_deep_merge` | 162-167 | `(base: dict, override: dict) -> None` | 5 | 2 | — |
| `_load_session_manager_override` | 244-254 | `() -> dict` | 3 | 2 | Прочитать session_manager.json (если есть) ДО profile overlay. Это сохраняет историческую роль per-deploy over |
| `_load_secrets_override` | 257-274 | `() -> dict` | 4 | 1 | Прочитать ``.secrets.env`` (если есть) — содержит секреты для ``${VAR}`` плейсхолдеров (``DATABASE_URL``, ``EM |
| `_export_secrets_to_env` | 277-285 | `(cfg: dict) -> None` | 3 | 2 | Экспорт «плоских» значений из ``cfg`` в ``os.environ``. Делается ДО ``_resolve_env_refs`` — чтобы ``${VAR}`` н |
| `_merge_profile_overlay` | 288-306 | `(cfg: dict, mode: str) -> None` | 4 | 1 | Применить profiles/<mode>.jsonc как ПОСЛЕДНИЙ шаг перед валидацией. Для prod — no-op (prod это чистый project. |
| `validate_profile_overlay` | 309-347 | `(overlay_cfg: dict, mode: str) -> None` | 5 | 4 | Hard-fail: profiles/<mode>.jsonc симметрично проверяется на: * все 6 profile-owned runtime-ключей ОБЯЗАНЫ прис |
| `validate_runtime_isolation` | 350-382 | `(cfg: dict, mode: str) -> None` | 24 | 3 | Hard-fail: точное соответствие runtime-таблиц профилю. |
| `_resolve_env_refs` | 388-402 | `(value)` | 7 | 1 | Рекурсивно заменить ``${VAR}`` на значение из os.environ. Неизвестная переменная оставляется как есть (ленивый |
| `_flatten_env` | 405-424 | `(d: dict, prefix: str='') -> dict[str, str]` | 4 | 2 | Рекурсивно «расплющить» вложенный dict в плоский ``{KEY_CHILD_...: str(value)}`` для экспорта в ``os.environ`` |
| `_` | 427-429 | `(s: str) -> str` | 1 | 3 | Sanitize-преобразование имени env-переменной. |
| `resolve_application_config` | 432-481 | `(profile: str) -> AttrDict` | 5 | 6 | Единая точка формирования SETTINGS. Используется внутри ``_initialize_settings(profile)`` после проверки white |
| `_initialize_settings` | 593-649 | `(profile: str) -> None` | 17 | 13 | Lifecycle-gate: единственная точка публикации ``SETTINGS``. Args: profile: ``"prod"`` или ``"test"`` (только w |
| `is_settings_initialized` | 652-659 | `() -> bool` | 2 | 7 | ``True`` после успешного ``_initialize_settings(profile)``. Используется в ``tests/test_standalone_failfast.py |
| `get_active_profile` | 662-669 | `() -> str` | 1 | 0 | Вернуть активный профиль (``SETTINGS["profile"]``). Бросает ``ConfigurationError``, если ``_initialize_setting |
| `get_setting` | 672-707 | `(*keys: str, default=None)` | 6 | 6 | Безопасный доступ к вложенным ключам SETTINGS. Принимает путь из имён ключей: ``get_setting("channels", "postg |
| `require_setting` | 710-725 | `(*keys: str)` | 6 | 1 | Строгий доступ к ключам SETTINGS (единственный источник правды — project.json). Возвращает значение по пути `` |

## `streamlit_app.py` — 669 LOC (code 476)
- module: `streamlit_app`
- docstring: Streamlit UI — тонкий клиент gateway через agent_conversation_messages.
- static importers (1): `tests/test_streamlit_app.py`
- string/dynamic refs: 0
- test files touching it: `tests/test_streamlit_app.py`
- classes: 0, module functions: 8

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_resolve_profile_from_argv` | 44-61 | `(argv: list[str] | None=None) -> str` | 7 | 1 | Достать ``--profile=<v>`` или ``--profile <v>`` из ``sys.argv``. Streamlit пробрасывает ``<args>`` после ``--` |
| `_load_chat_history` | 144-195 | `(chat_id: str=_CHAT_ID) -> list[dict]` | 13 | 1 | Загрузить историю чата из БД. Возвращает список сообщений в формате для st.session_state.messages. |
| `_get_extension_from_mime` | 198-207 | `(mime_type: str) -> str` | 2 | 1 | Получить расширение файла по MIME-типу (без принудительного дефолта). Делегирует общей ``utils.session_file_st |
| `_save_file_from_data_url` | 210-225 | `(data_url: str, filename: str) -> str | None` | 5 | 1 | Сохранить файл из data URL через общий ``SessionFileStore``. Дисковая иерархия теперь совпадает с каналом Post |
| `_check_response` | 228-249 | `(msg_id: str) -> tuple[str | None, dict | None]` | 6 | 1 | Проверяет ответ assistant'а. Возвращает кортеж (контент, метаданные) или (None, None). |
| `_get_processing_state` | 252-266 | `(msg_id: str) -> dict | None` | 7 | 1 | Возвращает промежуточное состояние processing-сообщения (контент, размышления). |
| `_render_context_window` | 269-297 | `(block: dict) -> None` | 16 | 2 | Отрисовать прогресс-бар занятости контекстного окна (M1 UI). Блок ``{used, limit, pct, model}`` кладётся в ``m |
| `_buffer_uploads` | 596-616 | `() -> None` | 10 | 1 | on_change для file_uploader: переложить свежевыбранные файлы в устойчивый буфер session_state, чтобы они переж |

## `gateway.py` — 522 LOC (code 378)
- module: `gateway`
- docstring: gateway.py — серверный режим работы агента. Тонкий оркестратор: вся инициализация сервисов — в ``ApplicationContext``, каналы — в ``ChannelFactory``, lifecycle — в ``GatewayRunner``. Файл отвечает ТОЛЬКО за gateway-специ
- static importers (2): `tests/test_gateway.py`, `tests/test_gateway_entrypoint_schema_validation.py`
- string/dynamic refs: 0
- test files touching it: `tests/test_application_context.py`, `tests/test_application_context_single_application_point.py`, `tests/test_cli_agent.py`, `tests/test_context_compaction.py`, `tests/test_gateway.py`, `tests/test_project_settings.py`, `tests/test_runtime_patcher_e2e.py`
- classes: 0, module functions: 13

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_parse_args` | 37-79 | `(argv: list[str] | None=None) -> argparse.Namespace` | 6 | 6 | Парсинг argv без делегирования валидации ``--profile`` в argparse. Ошибки argparse (``--help``, missing flag)  |
| `_entrypoint_main` | 103-173 | `(args: argparse.Namespace, script_dir: Path, workspace_dir: Path) -> None` | 2 | 2 | Startup + application body. Raises ``ConfigurationError`` on startup errors. Никакого ``sys.exit(2)`` изнутри  |
| `_project_version` | 176-186 | `() -> str` | 1 | 1 | Ленивая обёртка над ``lib.utils.project_version.project_version``. Module-level импорт lib.* был отложен до пе |
| `_run` | 189-281 | `(ctx) -> None` | 20 | 7 | Основной рабочий цикл gateway: каналы + Streamlit + агент. |
| `script_dir_for_runtime` | 287-297 | `() -> Path` | 2 | 2 | Абсолютный путь к каталогу gateway.py. Module-level ``Path(__file__).parent`` лениво: чтобы ``import gateway`` |
| `_configure_logging` | 300-310 | `(settings) -> None` | 3 | 3 | Настроить loguru из конфига (gateway.log_level). |
| `_gateway_print_llm_calls` | 313-324 | `() -> bool` | 3 | 0 | Прочитать флаг вывода токенов LLM в терминал из ``gateway.print_llm_calls``. Отключаемая опция: `false` по умо |
| `_gateway_print_worker_activity` | 327-339 | `() -> bool` | 3 | 1 | Прочитать флаг вывода активности пула воркеров в терминал. Читает ``gateway.print_worker_activity`` из `projec |
| `_streamlit_enabled` | 342-355 | `() -> bool` | 3 | 2 | Прочитать флаг включения Streamlit UI. Читает ``streamlit.enabled`` из `project.json` (секция streamlit). ``fa |
| `_report_db_pool_startup` | 358-392 | `() -> None` | 9 | 1 | Прогреть пул соединений БД и вывести отчёт о его воркерах. Воркеры ``utils.db`` подключаются лениво, поэтому п |
| `_check_websocket_port_available` | 395-446 | `(ctx) -> None` | 8 | 1 | Проверить занятость порта WebSocket-канала перед стартом цикла. ``WebSocketChannel.start()`` биндит ``127.0.0. |
| `_find_listener_pid` | 449-479 | `(host: str, port: int) -> int | None` | 4 | 1 | Найти PID процесса, слушающего ``host:port`` (Windows). Использует ``netstat -ano`` через subprocess (PowerShe |
| `main` | 485-518 | `(argv: list[str] | None=None) -> int` | 5 | 32 | Точка входа gateway с единым error-lifecycle boundary. ``parse → validate → _initialize_settings → runtime imp |

## `lib/core/skill_config.py` — 325 LOC (code 247)
- module: `lib.core.skill_config`
- docstring: Runtime API для skill'ов: конфигурация, таблицы, FAISS. Параметризован по ``skill_name``. Каждый skill вызывает функции со своим именем (например, ``get_db_tables("audit_analyzer")``). Это единая точка для всех skill'ов 
- static importers (0): — NONE —
- string/dynamic refs: 2
- test files touching it: — none —
- classes: 0, module functions: 21

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_skills` | 27-31 | `() -> dict[str, Any]` | 3 | 1 | Секция ``skills.*`` из project.json. |
| `_skill_cfg` | 34-38 | `(skill_name: str) -> dict[str, Any]` | 3 | 1 | — |
| `_tables_list` | 41-44 | `(skill_name: str) -> list[dict]` | 5 | 1 | — |
| `_vector_indexes_list` | 47-50 | `(skill_name: str) -> list[dict]` | 4 | 1 | — |
| `get_db_tables` | 53-66 | `(skill_name: str) -> list[str]` | 6 | 4 | Доменные таблицы skill'а для LLM-схемы. Возвращает имена таблиц из ``tables[]`` без ``label`` — это доменные т |
| `get_db_schema` | 69-82 | `(skill_name: str) -> str` | 4 | 4 | Схема skill'а (по первой таблице в ``tables[]``). |
| `get_predefined_scripts_table` | 85-101 | `(skill_name: str) -> str` | 2 | 4 | Имя таблицы реестра предопределённых SQL-скриптов (``label='scripts_registry'``). Lookup идёт через ``TableReg |
| `load_db_config` | 104-105 | `(skill_name: str) -> dict[str, Any]` | 1 | 0 | — |
| `get_llm_config` | 108-111 | `(skill_name: str) -> dict[str, Any]` | 1 | 4 | — |
| `get_tool_config` | 114-115 | `(skill_name: str) -> dict[str, Any]` | 1 | 0 | — |
| `get_cli_config` | 118-126 | `(skill_name: str) -> dict[str, Any]` | 7 | 7 | — |
| `get_max_retries` | 129-132 | `(skill_name: str) -> int` | 4 | 3 | — |
| `get_chunking_config` | 135-167 | `(skill_name: str) -> dict[str, Any]` | 10 | 5 | Параметры map-reduce чанкинга из ``skills.<name>.chunking.*``. Дефолты согласованы с прежней реализацией навык |
| `get_brief_context_config` | 170-202 | `(skill_name: str) -> dict[str, Any]` | 10 | 2 | Параметры BriefContextBuilder (``skills.<name>.brief_context.*``). Новый секционный ключ, введённый в brief-re |
| `get_in_memory_cache_path` | 205-231 | `(skill_root: Path | str) -> str` | 6 | 0 | Путь к файлу runtime-кэша (``cache.duckdb``). Файл общий для всех skill'ов. v2.5.2+ путь вычисляется через :fu |
| `get_vector_index_path` | 234-251 | `(skill_name: str, skill_root: Path | str) -> str` | 12 | 0 | Путь к FAISS-индексу: ``<default_root>/<index_name>``. Берёт первый индекс из ``vector_indexes[]``. Путь относ |
| `get_vector_db_table` | 254-271 | `(skill_name: str) -> str` | 14 | 1 | Имя таблицы-хранилища векторов. Источник — ``gateway.vector.index.storage_table``. Fallback — ``tables[type="v |
| `build_cache_provider` | 274-300 | `(skill_name: str, skill_root: Path | str) -> CacheProvider` | 1 | 2 | Провайдера кэша для skill'а — через ту же точку создания, что и у runtime. Тонкий делегат в :func:`lib.service |
| `get_vector_indexes` | 303-309 | `(skill_name: str) -> dict[str, Any]` | 1 | 0 | Метаданные индексов из ``gateway.vector.index.indexes`` (см. ``VectorIndexSettings.indexes`` и ``cache_provide |
| `get_embedding_config` | 312-321 | `() -> dict[str, Any]` | 1 | 1 | Embedding-конфиг из захардкоженных констант. Источник — ``cache_provider_impl.read_embedding_config()`` (``_EM |
| `get_embedding_model` | 324-325 | `() -> str` | 2 | 0 | — |

## `cli_agent.py` — 294 LOC (code 211)
- module: `cli_agent`
- docstring: cli_agent.py — терминальный режим работы агента (REPL). Тонкий оркестратор: загрузка конфига и сервисов — в ``ApplicationContext`` (включая auto-scan проектных хуков из ``workspace/hooks/``), REPL/typewriter — в ``lib.cl
- static importers (2): `tests/test_cli_agent.py`, `tests/test_cli_agent_profile.py`
- string/dynamic refs: 0
- test files touching it: `tests/test_cli_agent.py`
- classes: 0, module functions: 10

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_parse_args` | 42-87 | `(argv: list[str] | None=None) -> argparse.Namespace` | 8 | 6 | Парсинг argv. ``--profile`` НЕ принимается (CLI = фиксированный profile ``test``, см. design D8). Если передан |
| `_entrypoint_main` | 97-142 | `(args: argparse.Namespace) -> None` | 4 | 2 | Startup + application body, поднимает ``ConfigurationError`` на ошибках. Граница ``ConfigurationError → exit 2 |
| `_run_vanilla` | 145-166 | `(args: argparse.Namespace) -> None` | 1 | 2 | Стандартный CLI-агент (как ``nanobot agent``). Без доработок. |
| `_run_patched` | 169-191 | `(args: argparse.Namespace) -> None` | 1 | 1 | CLI-агент с PGSessionManager и workspace-хуками. |
| `_run_patched_repl` | 194-209 | `(ctx, args: argparse.Namespace) -> None` | 1 | 1 | REPL для patched-режима. |
| `__get_cron` | 212-215 | `(_ctx)` | 1 | 0 | CronService уже создан в ApplicationContext — возвращаем None, потому что AgentFactory уже подключила его из h |
| `_configure_logging` | 218-229 | `(settings) -> None` | 6 | 3 | loguru из cli.log_level. |
| `_migrate_cron_store` | 232-242 | `(config) -> None` | 4 | 2 | Перенос cron-задач из глобальной cron-директории nanobot в workspace. |
| `script_dir_for_runtime` | 248-257 | `() -> Path` | 2 | 2 | Абсолютный путь к каталогу ``cli_agent.py``. Ленивая инициализация, чтобы ``import cli_agent`` оставался чисты |
| `main` | 263-290 | `(argv: list[str] | None=None) -> int` | 5 | 32 | Точка входа cli_agent с единым error-lifecycle boundary. Аналогично ``gateway.main`` — ловит ``ConfigurationEr |

## `lib/core/agent_factory.py` — 287 LOC (code 218)
- module: `lib.core.agent_factory`
- docstring: AgentFactory — создание AgentLoop с хуками. Подключает к ``AgentLoop`` обязательные и опциональные хуки: * ``ToolAuditHook`` (всегда) — собирает вызовы инструментов за один оборот агента. Данные читаются из ``hook.drain(
- static importers (3): `lib/core/application_context.py`, `tests/contract/test_database_logging_get_model.py`, `tests/test_agent_factory.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 0

### class `AgentFactory` — lines 49-287 (239 LOC), 5 methods
- bases: object
- decorators: —
- docstring: Фабрика AgentLoop с консистентно настроенными хуками. Управляет только составом ``hooks=`` и ``hook_factories=`` в ``AgentLoop.from_config``. Дополнительные параметры (``session_manager``, ``cron_service``) пробрасываютс
- name referenced in 3 file(s); tests: 2

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `create` | 66-195 | `(self, config: Any, bus: Any, session_manager: Any | None=None, cron_service: Any | None=N` | 10 | 0 | 15 | Создать AgentLoop с подключёнными хуками. Args: config: runtime-конфиг nanobot (объект с ``.agents.defaults``, |
| `_wrap_provider_snapshot_loader` | 198-213 | `(config: Any, usage_store: Any, bus: Any | None) -> Any` | 2 | 1 | 1 | Build a ``provider_snapshot_loader`` that attaches the LLM observer. Falls back to ``config.build_provider_sna |
| `_import_tool_audit_hook` | 216-226 | `()` | 1 | 1 | 1 | Ленивый импорт ``ToolAuditHook`` из ``lib/hooks/``. ``ToolAuditHook`` — фреймворковый хук (живёт в ``lib/hooks |
| `_import_terminal_tool_print_hook` | 229-245 | `()` | 2 | 1 | 1 | Ленивый импорт ``TerminalToolPrintHook`` из ``lib/hooks/``. Возвращает класс хука или ``None``, если модуль от |
| `_build_database_logging_factory` | 248-287 | `(db_logging_service: Any, agent_id: str | None=None, print_llm_calls: bool=False, get_mode` | 2 | 1 | 1 | Создать фабрику оборота ``DatabaseLoggingHook``. Импорт через try/except, чтобы: * ``AgentFactory`` не зависел |

## `lib/core/bus_factory.py` — 130 LOC (code 101)
- module: `lib.core.bus_factory`
- docstring: BusFactory — создание MessageBus и опциональная обёртка для логирования. ``MessageBus`` (см. ``nanobot.bus.queue``) — асинхронная очередь, через которую каналы публикуют inbound-сообщения пользователя, а AgentLoop публик
- static importers (2): `lib/core/application_context.py`, `tests/test_bus_factory.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 1

### class `BusFactory` — lines 37-101 (65 LOC), 3 methods
- bases: object
- decorators: —
- docstring: Производство MessageBus, при необходимости — с логирующими обёртками. Параметры конструктора — опциональные async-callable, которые оборачивают соответствующий метод шины: * ``inbound_logger(msg)`` — вызывается ПЕРЕД ``p
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 54-60 | `(self, inbound_logger: Callable[[Any], Awaitable[None]] | None=None, outbound_logger: Call` | 1 | 0 | 6 | — |
| `create` | 62-77 | `(self) -> Any` | 3 | 0 | 15 | Создать MessageBus, при необходимости обернув publish_* логгерами. Returns: ``MessageBus`` (или совместимый об |
| `_wrap` | 80-101 | `(bus: Any, method: str, logger: Callable[[Any], Awaitable[None]]) -> None` | 2 | 2 | 1 | Заменить ``bus.<method>`` на async-обёртку ``await logger(); await original()``. Исходный метод сохраняется в  |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `build_logging_bus` | 104-130 | `(bus: Any, log_event: Callable[[str, str], None]) -> Any` | 3 | 0 | Синхронный shim: подменяет ``publish_outbound`` на запись в лог. Legacy-хелпер для сценариев, когда у вызывающ |

## `lib/core/skill_registration.py` — 98 LOC (code 78)
- module: `lib.core.skill_registration`
- docstring: Утилиты для регистрации skill'ов в ``table_registry``. Используется в ``ApplicationContext._auto_register_skills`` (runtime старт gateway) и в standalone-утилитах (``tools/build_vectors.py``). Контракт декларации skill'а
- static importers (4): `lib/core/application_context.py`, `tests/test_auto_register_skills.py`, `workspace/skills/audit_analyzer/scripts/cli.py`, `workspace/skills/legal_summarizer/scripts/cli.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 2

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `build_resources_for_skill` | 31-60 | `(skill_cfg: dict) -> list` | 17 | 1 | Построить список ресурсов для одного skill'а из его секции ``project.json``. Дедупликация: если ``name`` встре |
| `register_skill_from_config` | 63-98 | `(skill_name: str, cfg: dict, registry=None) -> SkillRegistration | None` | 8 | 4 | Зарегистрировать skill в ``table_registry`` из его ``project.json``-секции. ``enabled=False`` → skill пропуска |

## `lib/core/infra_registration.py` — 54 LOC (code 39)
- module: `lib.core.infra_registration`
- docstring: Регистрация инфраструктурных ресурсов в ``TableRegistry``. Единая точка для runtime (``ApplicationContext``) и standalone-утилит (``tools/build_vectors.py``). Читает конфиг из ``gateway.vector.index.*`` и регистрирует ин
- static importers (6): `lib/core/application_context.py`, `tests/test_infra_registration.py`, `tests/test_project_settings.py`, `tools/build_vectors.py`, `workspace/skills/audit_analyzer/scripts/cli.py`, `workspace/skills/legal_summarizer/scripts/cli.py`
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 0, module functions: 2

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_settings` | 21-24 | `() -> dict[str, Any]` | 1 | 12 | — |
| `register_vector_storage` | 27-54 | `() -> bool` | 14 | 6 | Зарегистрировать ``vector.storage`` (PG-таблица-хранилище эмбеддингов). Источник — ``project.json::gateway.vec |

## `lib/core/__init__.py` — 1 LOC (code 1)
- module: `lib.core`
- docstring: Core: единая точка создания и связывания сервисов приложения.
- static importers (4): `tests/test_cli_agent.py`, `tests/test_skill_config_api.py`, `workspace/skills/audit_analyzer/scripts/skill_config.py`, `workspace/skills/legal_summarizer/scripts/llm/config.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 0

