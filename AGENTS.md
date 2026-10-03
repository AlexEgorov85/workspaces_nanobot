# Agent Instructions

Этот файл — инструкции для opencode-ассистента, работающего с кодовой базой проекта.
Для инструкций **нано-агенту** (запускаемому через `nanobot gateway` / `cli_agent.py`) см. `workspace/AGENTS.md`.

## Project Layout

- `lib/` — кастомные сервисы поверх библиотеки `nanobot 0.3.0` (core/cli/lifecycle/services/session/channels/utils + hooks).
  - `lib/hooks/` — фреймворковые хуки: `tool_audit_hook.py` (`ToolAuditHook` — аудит вызовов tool'ов, всегда в `ctx.hooks`), `terminal_tool_print_hook.py` (`TerminalToolPrintHook` — живой вывод результатов, подключается **после** `ToolAuditHook`), `repeat_guard_hook.py` (`RepeatGuardHook` — защитник от вырожденных циклов одинаковых вызовов, `gateway.repeat_guard.*`, дефолт `off`; подключается **после** `TerminalToolPrintHook` и **всегда**, включая `mode="off"`, чтобы канон `runtime_inventory` совпадал с фактом), `database_logging_hook.py` (`DatabaseLoggingHook` — per-turn инстансы через `hook_factories`, только при наличии `DbLoggingService`), `mcp_identity_hook.py` (`McpIdentityHook` — подставляет `session_id`/`user_id`/`request_id` в аргументы вызовов `mcp_enterprise_*`; общий инстанс, личность читается из контекста вызова; подключается **последним** из фреймворковых, чтобы `ToolAuditHook` видел аргументы модели, а не инфраструктурные ключи). Плагины из `workspace/hooks/` идут **перед** `ToolAuditHook`. Провязываются явно через `AgentFactory`/`ApplicationContext`.
  - `lib/core/agent_factory.py` — фабрика AgentLoop с хуками. Шина (`MessageBus`) собирается в `lib/core/application_context.py::_create_bus` (обёртки `publish_inbound`/`publish_outbound` — там же, `_wrap_bus_publish`); отдельного `bus_factory.py` больше нет.
  - `lib/core/application_context.py` — точка сборки сервисов; `ctx.start()` / `ctx.stop()` — lifecycle.
  - `lib/cli/console_loop.py` — REPL/typewriter для CLI-агента.
  - `lib/services/channel_factory.py` — фабрика каналов (Postgres/Redis).
  - ~~`lib/services/duckdb_cache_store.py`~~ — **удалён** (фаза 5, 2026-10-01). Снимком владеет capability `data` платформы; заглушки в дереве агента **не осталось** — файл отсутствует и на диске, и в git-индексе. Прежняя запись: **единственная** concrete-реализация кэша (`class DuckDbCacheStore(CacheStore)` — наследует обе роли: чтение `CacheProvider` + запись `CacheIngestion`): DuckDB-файл кэша + FAISS в памяти. Путь к файлу — единый `resolve_cache_path()`, который живёт теперь на платформе (`mcp-platform/libs/enterprise_data/snapshot/store.py`) (`gateway.cache.local_path` — это **каталог**, имя `cache.duckdb` добавляется внутри) либо дефолт `~/.cache/nanobot/duckdb/cache.duckdb` (legacy `<workspace>/data_store/duckdb/` на NFS не поддерживается). Модель: **один файл кэша, одна точка входа — интерфейс, одна реализация.** Метода `publish()` НЕТ: снимка для читателей не существует, загрузчик пишет в файл напрямую. **Файл не удерживается между операциями:** каждый read-метод открывает соединение на время вызова (`_read_conn()`) и закрывает сразу после; writer'ом является только загрузка. `query_sql()` валидируется: DDL (`CREATE/ALTER/DROP/TRUNCATE`) → `UnsupportedSqlError` в любом mode; `INSERT/UPDATE/DELETE` в `READ_ONLY` → `ReadOnlyAssertionError`. Читать может только проверенное хранилище (`_is_ready`). Неудачное открытие файла — всегда исключение (`CacheOpenError` / `CacheBusyError` / `UnsupportedFilesystemError`); «полуготовый» провайдер с `is_ready() == False` наружу не отдаётся.
  - ~~`lib/services/cache_load_service.py`~~ — **удалён** 2026-10-01 вместе с кластером снимка (фаза 5): загрузку снимка делает capability `data`; заглушки в дереве не осталось (файл отсутствует и на диске, и в git-индексе), записи реестра и команды `git rm` отработали — см. `PENDING-DELETIONS.md`. Прежняя запись: `CacheLoadService`: **разовая синхронная** загрузка кэша навыков из PostgreSQL. Единственный writer и единственный, кто обращается к PG в этой подсистеме. `load()` блокирует до конца, фоновых потоков не порождает и обращений к PG после возврата не оставляет. Колбэков НЕТ: держит `CacheStore` напрямую, потому что он и есть единственный писатель. Ошибка соединения → `CacheLoadError` (fail loudly); отсутствующая в PG таблица → `missing_tables` без исключения. `max_workers` ограничен `channels.postgres.pool.max_conn` (каждый поток берёт слот общего пула). `CacheLoadResult`/`get_stats()` и событие `cache_load_done` несут `loaded_at` — время, на которое актуален снимок.
  - ~~`lib/services/cache_ownership.py`~~, ~~`lib/services/pg_duckdb_sync_service.py`~~ — **удалены** (change `drop-local-cache-read-from-pg`). Слой владения (`CacheOwnershipCoordinator`, fencing, `agent_cache_ownership`) снят: heartbeat каждые 30 сек = 2 запроса в минуту бессрочно, то есть расходовал тот ресурс, ради экономии которого кэш существует. Гонок за файл не обрабатывается: в системе один gateway, writer один и известен заранее.
  - ~~`lib/services/table_registry.py`~~ — **удалён** вместе с кластером снимка (фаза 5, 2026-10-01): после снятия локального кэша реестр ресурсов не остался с владельцем. Состав таблиц теперь объявляет capability `data` платформы (`mcp-platform/platform.json → audit.tables`), а загрузку снимка делает `mcp-platform/libs/enterprise_data/loader.py`. Прежняя запись: pluggable-реестр ресурсов с двумя namespace'ами: skill-ресурсы (`register(SkillRegistration)` для `TableResource`/`VectorResource`) и инфра-ресурсы (`register_infra(key, resources)` для runtime-storage общего назначения, например `oarb.audit_vectors`). Агрегаторы (`table_names`, `vector_names`, `resources`, `tracking_column_for`) объединяли оба namespace'а; `resources_by_label` смотрел только skills. Per-resource `label` — opaque marker для Skill-логики; загрузка кэша его игнорировал. Track-колонки: per-resource `TableResource.tracking_column`, дефолты `updated_at`/`id`.
  - ~~`lib/core/skill_registration.py`~~ — **удалён** вместе с `TableRegistry`: декларативная регистрация skill'ов из `config.json → gateway.agent.skills.<name>` шла в реестр, которого больше нет; вызывавший её `ApplicationContext._auto_register_skills` и standalone-утилита сборки векторов сняты вместе с кластером снимка. Настройки навыков по-прежнему читаются из `config.json → gateway.agent.skills.<name>`. Раньше источником объявлений был `project.json` — файла больше нет.
  - ~~`lib/core/skill_config.py`~~ — **удалён** (волна 1, кластер B). Параметризованный runtime API для skill'ов не имел продуктовых потребителей: `audit_analyzer` ходит в данные операциями capability `audit`, `legal_summarizer` уехал на платформу (фаза 11, п. 11.6). Заглушки в дереве не осталось. Настройки навыков читаются из `config.json → gateway.agent.skills.<name>`.
  - ~~`lib/core/infra_registration.py`~~ — **удалён** вместе с `TableRegistry`: `register_vector_storage` регистрировал `gateway.vector.index.storage_table` через `register_infra("vector.storage", ...)`; после сноса кластера потребителя регистрации не осталось (его забирал `CacheLoadService`). Состав индексов теперь объявляет capability `vectors` платформы (`mcp-platform/platform.json → vectors.indexes`).
  - `lib/services/context_compaction.py` — `ContextCompactionService`: единая точка записи факта сжатия контекста. Входы: настоящая slash-команда `/compact` (upstream `nanobot/command/builtin.py::cmd_compact`), CLI `/compact` (`console_loop`), tool `compact_context` (`workspace/tools/`), авто-сжатие nanobot → событие `ContextCompactionEvent` на `OutboundMessage` → `lib/services/compaction_event_subscriber.py::CompactionEventSubscriber.feed()` → публичный `ContextCompactionService.notify_session_compacted()` → один путь `_notify` → (1) `_write_history_notice` в `agent_conversation_messages` (видно в UI, но не в контексте промпта) и (2) событие `context_compacted` в долговечный `agent_gateway_logs` (через `DbLoggingService.try_log_event`; модуль-fallback ~~`workspace/utils/event_log.py`~~ удалён), доступное агенту после сжатия через tool `history_search`. Отдельного патча `cmd_compact` нет: `compact_tracking`/`compact_command`/`idle_guard` — DEPRECATED-остатки от `nanobot-035-upgrade`, которые никогда не применялись (см. `docs/architecture/runtime-patcher-inventory.md` § «Удалённые патчи»), функционал живёт в subscriber и штатных точках upstream. Ручные пути = `force=True` (жёстко, игнор порога токенов); при падении `estimate_session_prompt_tokens` — `_estimate_fallback` по символам. Подробности в `docs/ARCHITECTURE.md` § «Управление сжатием контекста».
  - `lib/services/consolidator_locale.py` — переопределение системных шаблонов nanobot из `workspace/overrides/` (monkeypatch Jinja2-loader'а `prompt_templates._environment`: `ChoiceLoader` с приоритетом override-каталога; применяется в `ApplicationContext.start()`, идемпотентно).
  - `lib/services/enterprise_mcp_client.py` — единственный клиент агента к процессу `enterprise-mcp`: долгоживущая stdio-сессия (`call()` / `list_operations()` / `aclose()` / `close()`), `client_from_settings()` из `EnterpriseMcpSettings`. Доменная ошибка разбирается из префикса `[code]` в `EnterpriseOperationError`, недоступность процесса — `EnterpriseMcpUnavailable`. Создаётся в `ApplicationContext._make_enterprise_mcp()`, закрывается в `gateway.py` внутри живого loop (fallback — `ctx.stop()`). **Сессия поднимается рукопожатием на старте** — `gateway._connect_enterprise_mcp(ctx)` в `_run`, до старта каналов и работы агента: сервер всегда нужен, поэтому «платформа не отвечает» обязано обнаруживаться на старте, а не посреди оборота; отказ уходит в `GatewayRunner` (перезапуск с backoff и причина в логе), а не проглатывается. Ленивый путь в `_ensure_session` остался как восстановление оборвавшейся сессии, а не как норма: поднять сессию синхронно нельзя — она привязана к loop, и `asyncio.run` в `ApplicationContext.start()` закрыл бы его сразу после создания. **Сервер объявлен в `config.json → gateway.agent.enterprise_mcp`; второе объявление — `tools.mcpServers.enterprise`, его читает штатный `MCPProvider`:** клиент агента обслуживает фоновые службы, ходящие в платформу вне оборота, а провайдер отдаёт операции модели как `mcp_enterprise_*` с их настоящими `inputSchema`. Поэтому процессов платформы два, и это объявлено, а не вышло случайно. Прежняя запись объявляла `tools.mcpServers` намеренно пустым по двум причинам, и обе были ложны: `ENTERPRISE_EXEC_REQUIRE_CALL_META` — это флаг с дефолтом `False`, а не требование, и `list_operations()` внутри — обычный `session.list_tools()`. Пути снимка и объявления индексов клиент **не вычисляет**: они живут в `mcp-platform/platform.json`. **Имя таблицы журнала клиент тоже не вычисляет** — но профиль обязан доходить до платформы, иначе журнал тестового контура окажется в боевых таблицах: `client_from_settings` дописывает `--profile <имя>` в args (только когда профиль не `prod`), а платформа применяет `platform.json → profiles.<имя>` через `read_profile_overlay`. Перекрывать можно ровно `PROFILE_OWNED_KEYS` (журнал, прогоны вопросов, очередь задач); неизвестный профиль — падение, а не откат к базовым (боевым) именам. Значения имён таблиц **не передаются**: только имя контура, как и `${NANOBOT_PYTHON}`. **На старте печатается вердикт и сводка по capability** — `gateway._report_enterprise_mcp_health()`: по одной дешёвой локальной операции на capability (`vectors` → `list_indexes`, `data` → `schema_check`, `audit` → `list_scripts`). Процесс может подняться и быть частично нерабочим (индексы не собраны, снимок недоступен, реестр скриптов пуст), и раньше это не было видно нигде. Отказ пробы **не роняет старт** — платформа отвечает, неполнота одного capability разбирается отдельно. Отказ рукопожатия печатается как `✗ enterprise-mcp: НЕ ПОДНЯЛСЯ — <причина>` **до** `raise`: `GatewayRunner` сообщает лишь «Gateway exited unexpectedly, restarting in 1.0s», и без этой строки причина подъёма в логе не ищется. Сводка дополнена **сверкой профиля** (`gateway._verify_platform_table_alignment`): агент берёт у платформы список реально проверяемых таблиц (`schema_check` → `tables`) и сверяет со своими; расхождение — `ConfigurationError`, потому что оверлей объявлен в двух файлах и заметить его можно только по содержимому боевого журнала. Выключенный раздел печатается явно, а не молча: «не объявлен». Путь `cli_agent.py` рукопожатие **делает** — `cli_agent.py:238` вызывает `_connect_enterprise_mcp(ctx)` первым шагом в живом loop, до `attach_log_transport()` и до REPL. Это требование спецификации (`openspec/specs/runtime/entrypoints/spec.md:302-306,632-639`), а не частный случай: тот же порядок обязан держать и ветвь `--patched`, и его проверяет `tests/test_gateway_enterprise_mcp_startup.py::TestCliOrdering::test_handshake_runs_before_repl_and_transport` (не `test_gateway.py` — класс живёт в отдельном файле про старт рукопожатия). **Прежняя запись в этом файле утверждала обратное** («рукопожатия не делает — там сессия ленивая») и вводила в заблуждение: из-за неё рукопожатие в CLI выглядело как «сделано по ошибке» и могло быть снесено при чистке. Сессия в CLI ленивая только до входа в REPL — поднять её синхронно нельзя, она привязана к живому loop. Отдельный процесс CLI держит **собственный** клиент, и это осознанно: запрет «второго владельца пула PostgreSQL» касается второго `MCPProvider` внутри агента (`tools.mcpServers` пуст именно поэтому), а не отдельного интерактивного процесса CLI.
  - `mcp-platform/libs/enterprise_client/llm.py` — клиент платформы для процессов **вне** агента (скиллы запускаются как подпроцессы и своего MCP-клиента не имеют). Поднимает лёгкий экземпляр сервера `python -m servers.enterprise.server --capabilities llm`: без `data` он не трогает ни PostgreSQL, ни файл снимка, поэтому не становится их вторым владельцем. Одна живая сессия на процесс, потокобезопасно (сотни вызовов из пула чанков). `complete()` / `complete_json()` / `embed()`; ошибки — `LlmOperationError` (доменная) и `LlmUnavailable` (процесс/сессия).
  - Поверхность для модели: операции платформы объявлены в `config.json → tools.mcpServers.enterprise` (белый список `enabled_tools`, 7 операций) и приходят как `mcp_enterprise_*` с настоящими `inputSchema`. Навыки `workspace/skills/enterprise_mcp/SKILL.md` (общий контракт: личность, конверт `_execution`, крупный результат, коды отказа) и `workspace/skills/audit_analyzer/SKILL.md` (выбор операции, индексы, отказы) описывают работу с ними. Гард: `tests/test_mcp_platform_declaration.py` (объявление, флаг `require_call_meta`, равенство имён ключей `LEGACY_IDENTITY_KEYS` файлу платформы) и `tests/test_audit_analyzer_skill_doc.py` (навык не обещает несуществующих операций, индексов и кодов).
  - Платформенная операция `read_result` (`mcp-platform/servers/enterprise/tools/read_result.py`) — чтение результата, сохранённого по порогу: конвеййер отдаёт крупный результат ссылкой `session://results/...`, и это единственный способ её прочитать. Операция платформенная, а не capability: файлами сессии владеет платформа, а страж `mcp-platform/tests/test_tool_execution_boundaries.py` запрещает capability касаться `SessionWorkspace`/`ArtifactStore`.
  - `_child_env()` клиента **ничего платформе не передаёт**, кроме `PYTHONIOENCODING` (и наследует `os.environ` целиком — DSN и секреты процесса). Раньше отсюда уходили `ENTERPRISE_LLM_*`, `ENTERPRISE_VECTOR_*`, `ENTERPRISE_EMBED_*` и `ENTERPRISE_SNAPSHOT_PATH`; окружение приоритетнее `platform.json`, поэтому такой «экспорт» не просто дублировал значение, а **молча затирал объявление платформы**: источник уезжал в `env:*`, и файл выглядел настроенным, не применяясь. Все значения живут в `mcp-platform/platform.json` (секции `llm`, `vectors`, `data`). Имён `ENTERPRISE_*` от агента в реестре платформы не осталось; страж границы — `tests/test_enterprise_mcp_settings_contract.py`.
  - `lib/services/runtime_patcher.py` — каталог monkey-patch'ей upstream `nanobot.agent.loop.AgentLoop` (**4 patches**: `exec_timeout_cap`, `assemble_outbound`, `subagent_logging`, `repeat_guard_block`; последний — единственный, кто патчит не `AgentLoop`, а `nanobot.agent.tools.execution._execute_tool_call`, чтобы отказ защитника от повторов стал синтетическим tool-результатом вместо обрыва оборота). `exec_limits` и `tool_limits` сняты 2026-10-03: потолки вывода инструментов вернулись к дефолтам nanobot, нативной замены нет by design, секция `gateway.tool_result_limits` удалена из `config.json` как мёртвая; `context_governor` снят в пользу штатного `nanobot/agent/context_governance.py:709-759`, `save_turn` — в пользу `nanobot/utils/helpers.py:580` `maybe_persist_tool_result()`; применяется через `apply_all()` из `ApplicationContext.create()`. **НЕ** занимается регистрацией project tools (вынесено в `lib/services/project_tool_loader.py`) — это же запрещено политикой инвентаря, п. 5. Полный каталог — `docs/architecture/runtime-patcher-inventory.md`; у каждого патча обязательное «Условие удаления» живёт в сводной таблице этого документа.
  - `lib/services/project_tool_loader.py` — stateless loader для регистрации кастомных tool'ов из `workspace/tools/*.py`: единственный публичный контракт `register_project_tools(agent, workspace_dir, *, settings=None, db_logging_service=None, enterprise_mcp=None) -> ProjectToolsLoadResult`. Вызывается из `ApplicationContext.create()` сразу после `RuntimePatcher.apply_all()` как независимый stage composition root'а. **`cache_store` в сигнатуре нет и не было** после сноса локального кэша; прежняя запись этого файла перечисляла его, из-за чего искался несуществующий параметр. Службы кладутся на контекст приватно: `ctx._settings_ref`, `ctx._db_logging_service`, `ctx._enterprise_mcp`, `ctx._agent_ref` — контракт проверяет `tests/test_project_tool_ctx_contract.py`.
  - `lib/services/runtime_events_subscriber.py` — `RuntimeEventsSubscriber`: подписка на runtime-события nanobot (`TurnCompleted`, `TurnRuntimeAdmitted`) → turn-метрики в `agent_gateway_logs`; решение — `docs/architecture/decisions/runtime-events-subscriber.md`.
  - `lib/services/runtime_inventory.py` — single source of truth для startup-инвентаря: канонические списки (`canonical_framework_hooks()`, `canonical_plugin_hooks()`, `canonical_project_tools()`, `canonical_runtime_patches()`) + diff-функции (`diff_hooks`, `diff_project_tools`, `diff_runtime_patches`). Используется в `ApplicationContext` для prominent-баннеров при missing/unexpected в startup-логе (после `_log_connected_hooks`, `apply_all()` и `register_project_tools()`) и в `tools/diagnose_startup.py` для парсинга логов. Тесты: `tests/test_runtime_inventory.py`.
  - `lib/services/session_cold_sync_service.py` — `SessionColdSyncService`: cold-storage mirror upstream JSONL → PG (`storage-hybridization`). Daemon-поток с per-transaction advisory lock, батчами по `session_key`, leader-election для multi-instance. Метрики через `get_stats()`.
  - ~~`lib/services/llm_usage_store_factory.py`~~, ~~`lib/services/llm_observer.py`~~ — **удалены** (волна 1, кластер A). Хранилище создаёт библиотека (`nanobot.llm_usage.get_llm_usage_store()`), а observer'ы провайдера свёрнуты в `AgentFactory._wrap_provider_snapshot_loader`. Модулей в дереве нет.
  - `lib/services/runtime_health.py` — RuntimeHealth/RuntimeReadiness (READY/DEGRADED/NOT_READY по компонентам). **Не HTTP-эндпойнт:** состояние вычисляется по запросу, логируется в `ApplicationContext.start()` и читается вызывающим кодом через `ctx.runtime_health` / `ctx.runtime_readiness`. HTTP-обёртки нет. Проверка `postgres` определяет здоровье **реальным ping'ом по пулу**, а required-ness выводится из конфига (`channels.postgres.enabled` или `storage_mode == "postgres"`) — имя класса менеджера сессий признаком не является.
  - `lib/services/schema_validation.py` — `SchemaValidationService` (pre-startup проверка наличия **5 runtime-таблиц** в БД через один SELECT к `information_schema.tables`; ключи — `channels.postgres.{table_name, messages_table, meta_table}` и `logging.db.{table_name, question_runs_table}`, ровно те, что помечены `PROFILE_OWNED_RUNTIME_KEYS` в `config.py`); `SchemaValidationError` (наследник `ConfigurationError`); `MissingTable`. Имена таблиц резолвятся из merged SETTINGS, **не зашиты в коде**. Вызывается из `ApplicationContext.start()` сразу после `_start_db_pool()`; см. `openspec/specs/runtime/startup-schema-validation`.
  - `lib/services/db_logging_service.py` + `lib/services/db_logging_bus.py` — `DbLoggingService`: пул-воркер записи событий в `agent_gateway_logs` / `agent_question_runs`, purge по `logging.db.retention_days`.
  - `lib/services/log_transport.py` — транспорт записи журнала (фаза 7): либо PostgreSQL напрямую, либо батчами через операцию `log_events` платформы. Батч режется `group_by_identity()` по `(session_id, user_id, request_id)`, потому что `log_events` берёт идентичность из контекста вызова, а не из тела батча; события без подписи уходят в локальный fallback и в счётчик `dropped`.
  - `lib/services/turn_delivery_factory.py` — fallback-ответ на internal-ошибку через публичную точку nanobot (`AgentLoop(turn_delivery_factory=...)`), заменил патч `patch_turn_delivery_fail` (решение — `docs/architecture/decisions/turn-delivery-public-extension.md`).
  - ~~`lib/services/llm_client.py`~~, ~~`lib/services/llm_config.py`~~ — **удалены** 2026-10-02 (change `enterprise-mcp-platform`, п. 3.14 и снос кластера). Общение с моделью принадлежит платформе (`mcp-platform/libs/llm`, операция `complete`). Второй HTTP-клиент в агенте был второй копией выбора модели и разъезжался с платформенной при первой же смене. Скиллы ходят к модели через `libs/enterprise_client/llm.py` (клиент платформы), budget-параметры прогона остались в `skills.<name>.cli.*`. Агент не знает ни адреса, ни модели, ни ключа провайдера и ничего о них не экспортирует в процесс сервера. Стражи: `tests/test_llm_goes_through_mcp.py::test_agent_has_no_llm_client_module` (файл не существует) и `::test_skill_llm_module_calls_the_platform_client` (навык ходит только в `libs.enterprise_client`).
  - ~~`lib/services/vector_index_service.py`~~ — **удалён** 2026-10-01 вместе с кластером снимка (фаза 5): сборку индексов делает capability `vectors`; заглушки в дереве не осталось (файл отсутствует и на диске, и в git-индексе), записи реестра и команды `git rm` отработали — см. `PENDING-DELETIONS.md`. Прежняя запись: build-слой: `VectorIndexBuildService` (провайдера берёт из `open_cache_provider`; FAISS собирается в памяти из файла кэша; персиста нет) + re-export `get_embedding`.
  - ~~`lib/services/cache_provider.py`~~ — **удалён** 2026-10-01 вместе с кластером снимка (фаза 5): интерфейс и `CacheAccessMode` живут в capability `data` платформы; заглушки в дереве не осталось (файл отсутствует и на диске, и в git-индексе), записи реестра и команды `git rm` отработали — см. `PENDING-DELETIONS.md`. Прежняя запись: **слой интерфейса.** `CacheProvider` (ABC) + `CacheBusyError` + `open_cache_provider(*, mode, db_logging_service=None)` — **единственная точка создания провайдера во всём рантайме**. Вызывающий код (runtime, skills, tools, standalone-утилиты) получает `CacheProvider` отсюда и MUST NOT называть конкретный класс хранилища. Состав ABC: чтение (`query_sql`, `explain`, `get_schema`, `search_vector`, `preload_indexes`), ресурс (`is_ready`, `close`). Запись вынесена в `CacheIngestion` (`upsert_records`, `replace_records`, `ensure_schema`); из рантайм-пути загрузчик вызывает **`replace_records`** (таблица берётся целиком, `upsert_records` — только примитив хранилища для фикстур). Здесь же живёт `CacheAccessMode` (`READ_WRITE` / `READ_ONLY`) — **определение MUST быть ровно одно**: дубликат молча ломает READ_ONLY-защиту. Репликация PG→кэш (`refresh`/`check_stale`) в контракт НЕ входит — ею владеет `CacheLoadService`; lifecycle-методов (`open`/`open_cache`/`try_claim`/heartbeat/fencing) на ABC нет.
  - ~~`lib/services/cache_provider_impl.py`~~ — **удалён** 2026-10-01 вместе с кластером снимка (фаза 5): эмбеддинги, сигнатуры индексов и чтение их конфигурации — в capability `vectors`/`llm` платформы; заглушки в дереве не осталось (файл отсутствует и на диске, и в git-индексе), записи реестра и команды `git rm` отработали — см. `PENDING-DELETIONS.md`. Прежняя запись: общие помощники (не реализация): `read_embedding_config`, `read_vector_index_config`, `get_embedding`, `compute_index_signature`/`verify_index_signature`, `list_runtime_vector_indexes`. Последний **требует `fetch_fn`** и НЕ открывает файл кэша сам.
  - ~~`lib/services/transcription_service.py`~~ — **удалён**: голос разбирает базовый класс библиотеки (`nanobot/channels/base.py:48` `BaseChannel.transcribe_audio()`, `audio/transcription.py` + `audio/transcription_registry.py`); канал пробрасывал ему четыре атрибута, которых в `PostgresChannel` не существовало. `channels.transcription_*` в `config.json` помечены upstream как deprecated — канонические настройки живут в верхнеуровневой секции `transcription`. ~~`lib/services/text_splitter.py`~~ — **удалён** как дубликат платформенного (`mcp-platform/libs/vectors/text_splitter.py`), прод-импортёров не осталось. `lib/services/session_storage.py` — хранение сессий; `lib/services/config_service.py` — резолв `${VAR}` в конфиге.
  - `lib/lifecycle/gateway_runner.py` + `lib/lifecycle/shutdown_coordinator.py` — запуск gateway и graceful shutdown.
  - `lib/session/pg_session_manager.py` — **класса `PGSessionManager` в проекте нет** (имя файла осталось от прежней реализации; в самом `lib/session/README.md` это отмечено, а здесь до сих пор утверждалось обратное). Модуль экспортирует три вещи: `SanitizingSessionStore` (upstream `SessionStore` + вычистка NUL на границе `save()`), `build_session_manager(workspace)` (собирает **штатный** `SessionManager` библиотеки, не подкласс) и `clean_session_content`. Зеркалирование сессий в PostgreSQL — не его работа, а отдельный `lib/services/session_cold_sync_service.py` (см. `storage-hybridization`). Гард на отсутствие прямых SQL в `agent_session_meta` / `agent_session_messages` — `tests/test_storage_hybridization.py::TestNoDirectSQLToSessionTables`.
  - `lib/cli/display_config.py` — настройки вывода CLI; `lib/cli/hook_loader.py` — авто-сканирование `workspace/hooks/`.
  - ~~`lib/channels/redis_channel.py`~~ — **удалён**: каналов один, PostgreSQL. Второй транспорт тянул за собой второй цикл поллинга, второй backoff и второе место, где правила очереди могут разойтись с боевыми; заглушки в дереве не осталось (файл отсутствует и на диске, и в git-индексе). `lib/channels/message_exchange.py` — общий формат сообщений каналов.
  - ~~`lib/tools/compact_context_tool.py`~~ — удалён вместе с каталогом `lib/tools/` (после переноса в `workspace/tools/`). Теперь tool живёт в `workspace/tools/compact_context.py` и регистрируется через `lib/services/project_tool_loader.py::register_project_tools` (стандартный путь).
  - Slash-команда `/compact` — **upstream** `nanobot/command/builtin.py::cmd_compact`; локальный ~~`lib/commands/compact_command.py`~~ удалён (см. CHANGELOG). Патча-обёртки нет и не было: `compact_command` остался DEPRECATED-остатком от `nanobot-035-upgrade` (drift между `_PATCH_SPECS` и `apply_all()` — фактически не вызывался, см. `docs/architecture/runtime-patcher-inventory.md` § «Удалённые патчи»); наблюдение ведёт `lib/services/compaction_event_subscriber.py` по событию `ContextCompactionEvent` и вызывает публичный `ContextCompactionService.notify_session_compacted()`. Инвариант сохранён: shortcut-команды минуют `_assemble_outbound`, поэтому обработчик обязан ставить `FINAL_TURN_KEY="_final_turn"` в outbound — иначе постгресс-канал не финализирует оборот и задача зависает в `processing`.
- `lib/channels/postgres_channel.py` — канал PostgreSQL: захват задачи одним `UPDATE ... RETURNING` в `_claim_one` (эксклюзивность — внешний `AND status = 'pending'`, состояние захвата в самой строке задачи), возврат зависших `processing` фоновой `_unstick_loop` (интервал `unstick_interval`), статусы `error`/`failed`. Протокол аренды (таблица `agent_worker_claims`, lease/heartbeat, reclaim+heal) удалён в фазе 1 миграции `enterprise-mcp-platform` — см. `docs/ARCHITECTURE.md` § «Захват задач и статусы».
- `lib/core/project_settings.py` — pydantic-валидация merged SETTINGS (`ProjectSettings`); вызывается в `ApplicationContext.create()` (fail-fast на типы/значения, `ConfigurationError` со списком всех проблем; неизвестные ключи разрешены).
- ~~`lib/utils/sql_safety.py`~~ — **удалён** (фаза 9): единственный продакшн-потребитель был `generated_sql_mode.py` навыка `audit_analyzer`, который уехал на платформу. Граница не потеряна, а переехала и разделилась: белый список таблиц + потолок строк проверяет `mcp-platform/libs/audit/guard.py` (разбором AST, без деградации), режим доступа к снимку — `libs/enterprise_data/snapshot/sql_guard.py`, общая политика — `libs/enterprise_data/sql_safety.py`. Старый `validate_sql` знал только вид оператора и не знал имён таблиц, поэтому `sqlglot` убран и из `requirements.txt` агента. Подробности: docs/DATABASE.md § «SQL Security Guard».
  - `lib/utils/text_utils.py` — общие утилиты для подготовки текста (sanitize_value, truncate_middle); единственный источник для tool'ов и skill'ов (бывший `_sanitize_value` из `audit_analyzer/scripts/output.py` удалён, оставлен back-compat re-export).
  - ~~`lib/utils/table_utils.py`~~ — удалён: `normalize_table_names` канонизировал форму `[[schema, table]]`, которой в `project.json` больше нет (там плоские `{"name": "schema.table"}`). Продуктовых импортёров не осталось.
  - `lib/utils/project_version.py` — единое чтение версии из `config.json` (`project.version`, актуальный релизный тег без `v`); используется баннером `gateway.py` при старте. Git-теги отстают из-за release-веток (см. Release Process).
  - `lib/utils/outbound_meta.py` — фильтрация служебных outbound (`OUTBOUND_DROPPED_KEYS`, `FINAL_TURN_KEY`, `is_dropped`/`is_outbound_noise`/`is_outbound_final`); используется каналами (`postgres_channel.py`, `db_logging_bus.py`) и `runtime_patcher.py`. `is_stream_delta` **удалена** 2026-10-03: 0 вызывающих во всём репозитории, ключа `_stream_delta` нет ни в установленном nanobot, ни в `OUTBOUND_DROPPED_KEYS` — на фильтрацию она не влияла, а `AGENTS.md` до сих пор объявлял её используемой каналами.
  - `lib/utils/windows_terminal.py` — включение VT и починка вывода в legacy-консоли Windows: `prompt_toolkit` управляет VT только для STDIN, поэтому без `ENABLE_VIRTUAL_TERMINAL_PROCESSING` ANSI-последовательности печатаются как `?[1m`.
  - ~~`lib/utils/duckdb_query.py`~~ — **удалён** 2026-10-01 с кластером снимка, заглушки не осталось; `lib/utils/node_access.py` — доступ к именованным нодам `__nanobot_meta`; `lib/utils/logging_utils.py` — утилиты логирования. ~~`lib/utils/retry.py`~~ — **удалён** 2026-10-02 вместе с единственным оставшимся импортёром (`llm_client.py`); блокировавший его `cache_provider_impl.py` уехал с кластером снимка ещё 2026-10-01. Платформа держит свою копию: `mcp-platform/libs/enterprise_common/retry.py` (единственное определение `retry_on_exception`, страж `mcp-platform/tests/test_retry_shared.py`). **BREAKING для внешних импортёров:** `from lib.utils.retry import retry_on_exception` больше не работает.
- `workspace/` — кастомное окружение нано-агента: `hooks/`, `tools/`, `utils/`, `skills/` (**два навыка — `enterprise_mcp` (общий контракт вызовов операций платформы) и `audit_analyzer`** (домен аудита); `legal_summarizer` и его обезличенный каталог `_legal_summarizer/` удалены из репозитория целиком, заглушки не осталось: домен живёт в `mcp-platform/libs/legal_summarizer/`, модель ходит в него операцией `mcp_enterprise_query_operation` — она объявлена в белом списке `config.json → tools.mcpServers.enterprise.enabled_tools`), `memory/`, `cron/`, `prompts/`, `overrides/` (переопределения системных шаблонов nanobot; сейчас — `agent/consolidator_archive.md`, подкладывается через `lib/services/consolidator_locale.py`), `*.md` (`AGENTS.md`, `HEARTBEAT.md`, `SOUL.md`, `USER.md`). `workspace/hooks/` — самодостаточные плагины-хуки (контракт `cls(workspace_dir=...)`), подхватываются auto-scan'ом: `session_file_redirect_hook.py` (`SessionFileRedirectHook` — перенаправление файлов сессии, см. File Storage Policy), `recent_files_hook.py` (`RecentFilesHook` — авто-прикрепление созданных файлов к `OutboundMessage.media`), `debug_stream_diag.py` (`StreamDiagnosisHook` — диагностика стриминга). `active_files_hook.py` удалён (см. `docs/architecture/decisions/active-files-hook-removal.md`); фреймворковые хуки живут в `lib/hooks/`. `workspace/utils/` — утилиты workspace: `db.py` (пул соединений, `resolve_dsn`, `get_stats`), `media.py` (сериализация media), `jsonb.py` (JSONB-декодер), `session_file_store.py`, `session_key.py` (`safe_session_key`), `clean_text.py`. Извлечение текста из документов уехало на платформу (`mcp-platform/libs/office/` + tool `document_read`), агентской `office_files.py` больше нет. ~~`structure_cache.py`~~ удалён (импортировал несуществующий `extract_structure`). Долговечный журнал `agent_gateway_logs` ведётся **только** через `DbLoggingService` (см. `lib/services/db_logging_service.py`); модуль `event_log.py` удалён. `workspace/tools/` — кастомные tool'ы (auto-discover через `lib/services/project_tool_loader.py::register_project_tools`; каждый tool — наследник `nanobot.agent.tools.base.Tool` с `config_key`/`config_cls`/`enabled`/`create`). **Важно:** `ctx.config` — это pydantic `ToolsConfig` из nanobot, которая знает только встроенные подсекции (`web`/`exec`/`file`/...) и отбрасывает неизвестные. Свои настройки читать через `ctx._settings_ref.tools.<config_key>` (или `gateway.<config_key>` для исторических секций). Содержит: `compact_context.py` (ручное сжатие, читает `gateway.compact.*`) и `document_read.py` (извлечение текста из docx/xlsx/xls/pdf/pptx/csv/txt парсером платформы `mcp-platform/libs/office/`). ~~`history_search_tool.py`~~, ~~`legal_summarizer_query.py`~~, ~~`audit_analyzer_query.py`~~ и `example.py` **удалены**: те же операции модель получает как `mcp_enterprise_*` штатным MCP-клиентом нанобота (change `2026-10-03-mcp-native-tools`, п. D6), а личность вызова подставляет хук `lib/hooks/mcp_identity_hook.py` — не tool-обёртка. Прежняя причина держать тонкую обёртку поиска по журналу («`MCPToolWrapper` передаёт ровно аргументы модели, и при прямом вызове операции `session_id` пришёл бы от модели, то есть security-граница перестала бы быть границей») снята сама собой: область поиска задаёт платформа — `history_search` берёт `session_id`/`user_id` из контекста вызова, а объявления модели её не касаются. Tools `duckdb_query`/`vector_search` в агенте отсутствуют — доступ к данным аудита только через операции capability `audit` (`mcp_enterprise_list_scripts` / `run_script` / `generate_sql` / `vector_search`), CLI навыка удалён вместе с Python-слоем навыка (фаза 9). Документация по кастомным tool'ам (инструкция агента: когда вызывать, какие `event_type`/`параметры`) — в `workspace/TOOLS.md`. Секцию «Vector-инфраструктура» ниже.
~~`benchmarks/`~~ удалён в фазе 1 миграции `enterprise-mcp-platform` (runner, evaluator, scorer, reporter, db, hooks, models, `items/*.yaml`). НЕ путать с `tests/benchmarks/` — golden-dataset тесты quality-бенчмарков, они остаются (`test_quality_benchmark.py` самодостаточен, домен не импортирует). Матрица присутствия модулей навыка и `conftest.py` с `sys.path` навыка **обезличены** 2026-10-02 (`_test_acceptance_matrix.py`, `_conftest.py`).
- `tools/` — утилиты. **Состав (6 + `__init__.py`):** `migrate.py` — runner миграций схемы, `apply_test_profile_tables.py` — применение 5 DDL test-таблиц (`public.agent_*_test`) для профиля `test`, `architecture_guard.py` — проверка архитектурных invariant'ов, `validate_component_specs.py` — валидация component-spec'ов, `diagnose_startup.py` — парсер startup-лога gateway/CLI + сверка с `lib.services.runtime_inventory` (печатает OK / DRIFT / CRITICAL по хукам/project tools/runtime patches; `--strict` / `--json`; exit 1 при critical, 2 при drift, 0 при совпадении; принимает `--log PATH` или stdin), `legacy_audit.py` — zero-reference аудит legacy-символов (regression guard: `audit()` / `assert_no_legacy()` / `main()`). Сняты: ~~`build_vectors.py`~~, ~~`check_indexes.py`~~ — **удалены** 2026-10-01 вместе с кластером снимка (сборка индексов — `mcp-platform/servers/enterprise/build_index.py`, диагностика — операция `index_stats` capability `vectors`); `generate_comments_sql.py`, `scan_nanobot_inventory.py`, `smoke_post_cleanup.py`, `release_v25*.py`, `extract_office_structure.py` — удалены (черновики и служебные скрипты релизов), `generate_predefined_scripts_sql.py` удалён (predefined-скрипты — DB-first).
- `sql/` — DDL (каналы, сессии, логи, бенчмарки, audit_analyzer, векторы, воркеры); `sql/migrations/` — версионные миграции схемы (`schema_migrations` tracking-таблица, применяется через `python tools/migrate.py --apply`; см. `sql/README.md`).
- `tests/` — pytest (включая `tests/integration/` и `tests/contract/` — контрактные тесты поверхности nanobot 0.3.0 для upgrade-readiness, CI job `upgrade-readiness` в `.github/workflows/ci.yml`; `asyncio_mode = "auto"`, `pythonpath = ["."]`).
- `docs/` — каталог дополнительной документации (README.md — навигационный хаб, на который ссылается корневой `README.md`):
  - `docs/architecture/` — инвентаризация зависимостей (`nanobot-inventory.md`/`nanobot-inventory.json`) и monkey-patch'ей (`runtime-patcher-inventory.md`).
  - `docs/skill-tool-architecture.md` — контракт Skill ↔ Tool (что разрешено/запрещено, decision procedure в `SKILL.md`).
  - `docs/architecture/decisions/audit-analyzer-runtime-boundary.md` — ADR: `audit_analyzer` эталонной является CLI-слой (skill-side `scripts/cli.py --mode <predefined|generated_sql|vector>` над существующими `lib/services`), Agent-tools для этих режимов НЕ возвращаются; baseline-таблица границы + хронология A–G + два открытых process-boundary дефекта.
  - `docs/skill-tool-inventory.md` — текущее состояние всех skill/tool и история удалённых.
  - `docs/table-registry.md` — реестр таблиц PG → DuckDB, sync-контроль, track-колонки.
  - `docs/architecture/runtime-patcher-inventory.md` — каталог monkey-patch'ей с target/risk/тестами.
  - `docs/TROUBLESHOOTING.md` — диагностический runbook (типовые ошибки и решения).
  - `docs/MIGRATION.md` — сводка изменений между релизами + breaking changes.
  - ~~`docs/_archive/`~~ — исторические процессные артефакты (baseline'ы, инвентаризации, миграционные инструкции) **больше не существуют**: каталога нет ни на диске, ни в индексе git.
- `config.py` + `config.json` + `.secrets.env` — иерархия конфига
  (порядок мержа: `config.json` → `.secrets.env`; секреты через `${VAR}`).
  ~~`project.json`~~ **выпилен** (волна 2): его секции переехали в `config.json`,
  а `gateway.agent.<name>` поднимается в `SETTINGS` функцией `config._lift_agent_sections`.
  Причина не в удобстве: файл был JSONC с комментариями и читался отдельно от
  схемы библиотеки, то есть объявлял настройки, о которых схема не знает.
- Точки входа: `cli_agent.py` (REPL), `gateway.py` (HTTP-сервер). ~~`streamlit_app.py`~~ удалён в фазе 1 миграции `enterprise-mcp-platform` вместе с ~~`lib/services/subprocess_manager.py`~~ и секцией `streamlit.*` — все три отсутствуют и на диске, и в индексе git.

## File Storage Policy

- Новые файлы, создаваемые в рамках сессии, сохраняй под `workspace/data_store/cache/sessions/<session_key>/`
  (политика `workspace/AGENTS.md`). Не пиши напрямую в корень проекта.
- ~~Кэш документов legal_summarizer: `workspace/data_store/cache/sessions/<safe_session_key>/documents/<document_id>/`~~ — **больше не агентский.** С 2026-10-02 домен живёт в `mcp-platform/libs/legal_summarizer/`, и корень его кэша приходит из реестра (`ENTERPRISE_LEGAL_CACHE_ROOT`, объявление — `mcp-platform/platform.json → legal_summarizer`), а не выводится из `Path(__file__).parents[N]`. Ключ сессии берётся из `session_id` контракта операции, не из `SESSION_KEY` в окружении. `document_id` — SHA-256 **содержимого** документа, поэтому одинаковое содержимое под разными путями делит один кэш-разбор. Конвенция имён сессий в агенте (`safe_session_key`) прежняя: `workspace.utils.session_key.safe_session_key`.
- Для редактирования существующих файлов (`AGENTS.md`, `lib/`, `*.py`) — обычные `edit_file` / `apply_patch`.
- **Не используй `>`, `>>` в `exec` для создания файлов** — `session_file_redirect_hook` их не перехватывает.

## Configuration

- Настройки нано-агента (`channels.*`, `cli`, `gateway.*`, `skills.*`, `logging.db`) — в `config.json`. Секции, которых нет в схеме nanobot, живут под `gateway.agent.<name>` и поднимаются в `SETTINGS` через `config._lift_agent_sections`.
- Очистка журнала событий `agent_gateway_logs` / `agent_question_runs`: подсекция `logging.db` (`logging.db.enabled` — вкл/выкл записи):
  - `logging.db.retention_days` (целое, дефолт `90`) — возраст в днях, старше которого события и question_runs удаляются фоновым пулом `DbLoggingService` (через `NOW() - (N || ' days')::interval`, совместимо с Greenplum 6.5). `0` или отсутствие — авто-удаление по возрасту выключено (события хранятся вечно).
  - `logging.db.purge_interval_sec` (дефолт `3600.0`) — интервал периодической очистки в worker-цикле `DbLoggingService`.
  - Любые пустые `outbound_final`/`outbound_delta` (пустой `content` и нет `media` — stream-чанки/синтетические финалы) удаляются ВСЕГДА при каждой итерации очистки, независимо от `retention_days`. Реализация: `DbLoggingService.purge_empty_outbound` / `purge_old` (`lib/services/db_logging_service.py`).
  - `logging.db.flush_interval_sec` (float, дефолт `5.0`, диапазон `0.5 ≤ value ≤ 60.0`) — интервал flush'а батча worker-потоком `DbLoggingService` (секунды). Уменьшение ускоряет видимость событий в БД (полезно для отладки/диагностики), увеличение снижает нагрузку на БД при burst-трафике. Тип и диапазон валидируются через `LoggingDbSettings.flush_interval_sec` в `lib/core/project_settings.py`; вне диапазона — `pydantic.ValidationError` на старте `ApplicationContext.create`.
- Настройки nanobot (агенты, провайдеры, API) — в `config.json`.
- Секреты (API-ключи, `DATABASE_URL`) — в `.secrets.env` через `${VAR}`.
- Читай в коде через `get_setting(*keys, default=...)` или `SETTINGS.*` из `config.py`.
- При добавлении новой обязательной настройки — добавь запись в `REQUIRED_KEYS` в `tests/test_config_keys.py`.
- Версия проекта (баннер gateway): `project.version`, объявленный в `config.json` (секция `gateway.agent.project.version`, поднимается в `SETTINGS` через `config._lift_agent_sections`; актуальный релизный тег без префикса `v`; git-теги и первый релизный блок CHANGELOG на `master` отстают от актуального тега из-за release-веток — см. Release Process). Читается через `lib/utils/project_version.py`.
- Канал PostgreSQL: `channels.postgres.{worker_id, poll_interval, error_retry_delay, unstick_interval, processing_timeout, table_name, messages_table, meta_table}`. Протокол аренды задач (таблица `agent_worker_claims`, `claims_table`/`lease_interval`/`claim_strategy`) удалён — захват через `UPDATE ... RETURNING`, возврат в пул через `_unstick_loop`; `worker_id` участвует только в логах. `table_name`/`messages_table`/`meta_table` — настраиваемые имена таблиц канала/сессий; они же входят в `PROFILE_OWNED_RUNTIME_KEYS` (`config.py`), вместе с `logging.db.{table_name, question_runs_table}` — всего 5 profile-owned ключей, они же проверяются `SchemaValidationService` перед стартом.
- Захват задачи (бывший «режим аренды»): единственный путь — `_claim_one`, один `UPDATE ... RETURNING`. `claim_strategy`/`claims_table`/`lease_interval` удалены, настройки выбора режима нет. `_unstick_processing` для защиты от зависших задач выполняется фоновой задачей с интервалом `channels.postgres.unstick_interval` (дефолт `max(60, processing_timeout/5)` = 120 сек). Известный дефект: ветка повтора `status='error'` в `_claim_one` недостижима (внешний `AND status = 'pending'` её отсекает) — задачи после повторяемой ошибки остаются в `error` навсегда; настройка `error_retry_delay` сохранена как контракт, механизма за ней нет.
- Порог извлечения текста документа в user-промпт: `channels.document_text_threshold` (общий для всех каналов — Postgres/Redis/websocket/streamlit, дефолт `20000` символов извлечённого текста). **Единый механизм**: каналы передают агенту только пути к файлам, текстовое представление документа формирует нативный `nanobot.utils.document.extract_documents` — патч `document_text_threshold` удалён в фазе 6 (п. 6.7), порог живёт в нашем коде, а не в обёртке фреймворка (см. `docs/architecture/runtime-patcher-inventory.md` § «Удалённые патчи»). Унифицированный формат каждого файлового блока: `[File: <basename> (saved at <path>)]\n<text>` (маленький) или `[File: <basename> (saved at <path>)]\n[text omitted (len=… > threshold=…)]` (большой). Путь к файлу присутствует **всегда** — агент в любом случае знает, куда передать файл (skill/`read_file`/`exec`); каналы НЕ дописывают собственных хинтов `[Attachment: … (saved at …)]`, чтобы не дублировать. Действует для всех каналов и subagent-сообщений единообразно. `0` или отсутствие ключа в pydantic — порог не применяется (NO-OP).
- Вывод токенов LLM-итераций в терминал gateway: `gateway.print_llm_calls` (опционально, `false` по умолчанию; CLI включает всегда через `cli_agent.py`).
- Активность пула воркеров в терминал gateway (взял задачу / закончил / размер очереди): `gateway.print_worker_activity` (опционально, `false` по умолчанию).
- Активность db-worker пула соединений в терминал gateway (взял/закончил job с тегом вызывающего): `gateway.print_db_activity` (опционально, `false` по умолчанию).
- Прогрев/проверка пула соединений при старте gateway: `probe_connections` в `workspace/utils/db.py` (вызывается `gateway.py` на старте; метки db-job'ов через `_caller_tag`/`Job.tag`).
- Потолки вывода инструментов: **не настраиваются** (2026-10-03 сняты `patch_exec_limits` и `patch_tool_limits`, нативной замены в nanobot 0.3.5 нет). Действуют дефолты библиотеки: exec — 10 000 символов по умолчанию и 50 000 потолком, `read_file` 128 000, `list_dir` 200, grep 250/200 и **2 МБ на файл** (крупные файлы пропускаются целиком — grep возвращает «No matches found», а уведомление о пропуске идёт в хвосте). Секция `gateway.tool_result_limits` из `config.json` удалена как мёртвая. Последствия и числа — в `docs/architecture/runtime-patcher-inventory.md` § «Снятые патчи», страж — `tests/test_runtime_patcher.py::TestToolLimitPatchesAreGone`. Патча `save_turn` в каталоге нет, персист заменён штатным `nanobot/utils/helpers.py::maybe_persist_tool_result()`.
- Режим защитника циклов `gateway.repeat_guard.*` (`off` по умолчанию; `mode`, `window_size`, `max_repeats_in_window`, `exempt_tools`; см. `openspec/specs/runtime/anti-loop/spec.md`): `lib/hooks/repeat_guard_hook.py` держит скользящее окно последних вызовов оборота и ловит пары `(tool_name, canonical_args)`, повторяющиеся `max_repeats_in_window` раз. Сравнение — **точное равенство** канонического JSON (`sort_keys=True` на всех уровнях); `blake2b`-digest в `payload.fingerprint_hash` участвует только в observability, в детекции не участвует. `exempt_tools` — только точные имена, glob/regex отвергаются на старте (`field_validator` → `ConfigurationError`). Режим `block` поднимает `RepeatGuardBlocked`, которую перехватывает патч `repeat_guard_block` (`RuntimePatcher.patch_repeat_guard_block`, 6-й в `docs/architecture/runtime-patcher-inventory.md`) и превращает в синтетический tool-результат — без патча `block` непригоден: hook-API не умеет «мягко» отклонить вызов, а `before_execute_tool` в `_execute_tool_call` стоит вне `try`. Сброс state — в `before_iteration` при `iteration == 0` (у `AgentRunHookContext` в 0.3.5 **нет** `session_key`, сброс в `before_run` адресно невозможен). События: `tool_repeat_warned` / `tool_repeat_blocked` в `agent_gateway_logs` через `try_log_event`, ровно одно на пересечение порога.
- Ручное и автоматическое сжатие контекста: `gateway.compact.*` (`enabled`, `notify_in_history`, `print_to_terminal`; все опциональны, дефолт `true`/`true`/`false`). Один и тот же сервис `ContextCompactionService` используется: (1) tool'ом `compact_context` (gateway) — вызывается агентом или пользователем; (2) CLI-командой `/compact` (CLI) — перехват в `console_loop.py`; (3) авто-сжатием nanobot — по событию `nanobot.events.ContextCompactionEvent` на `OutboundMessage`: `lib/services/compaction_event_subscriber.py::CompactionEventSubscriber.feed()` зовёт `notify_session_compacted()`. Патчей `compact_tracking`/`compact_command`/`idle_guard` в `runtime_patcher` **нет** — это DEPRECATED-остатки от `nanobot-035-upgrade`, которые никогда не применялись (drift между `_PATCH_SPECS` и `apply_all()`); их функционал живёт в `lib/services/compaction_event_subscriber.py` (`ContextCompactionEvent` на `OutboundMessage`), в upstream `cmd_compact` и в upstream `AutoCompact._is_expired` при `_ttl <= 0`. См. `docs/architecture/runtime-patcher-inventory.md` § «Удалённые патчи». Все три пути пишут служебную заметку в `agent_conversation_messages` (`metadata.kind="context_compact"`, `role='assistant'`, `status='completed'`) одним и тем же методом `_notify`. Заметка видна в истории диалога, но НЕ попадает в контекст промпта (он строится из `PGSessionManager`). Поведение самого сжатия (порог токенов, idle-таймаут) управляется ключами nanobot `consolidationRatio` (дефолт `0.5`) и `idleCompactAfterMinutes` в `config.json` (см. `nanobot/config/schema.py:151-163`); в этом проекте `idleCompactAfterMinutes: 0` — auto-compact idle выключен, активен только token-budget.
- Заготовленные ответы при internal-ошибке `AgentLoop._process_message`: `gateway.error_messages.*` (`internal_error: str`, `log_to_db: bool`; все опциональны, дефолт `"Я не справился с вашим вопросом. Попробуйте, пожалуйста, переформулировать конкретнее — например, уточните ключевую часть или приведите пример."` / `true`). Отдельного патча **нет**: `patch_turn_delivery_fail` из каталога `runtime_patcher` убран, fallback-ответ собирается публичной точкой nanobot — `lib/services/turn_delivery_factory.py` через `AgentLoop(turn_delivery_factory=...)` (решение — `docs/architecture/decisions/turn-delivery-public-extension.md`): `TurnDelivery.fail` не зовётся, outbound публикуется сам, поэтому двойной публикации не бывает. Тип/сообщение исключения пишутся только в `agent_gateway_logs` через `DbLoggingService.try_log_event(..., event_type="turn_failed")`. Маркер `metadata._error_kind="internal"` отличает fallback-ответ от обычного. Контракт описан в `openspec/specs/runtime/error-fallback/spec.md`.
- Pre-startup проверка наличия обязательных runtime-таблиц: `gateway.startup.schema_validation.*` (`enabled: bool = true`, `timeout_sec: float = 5.0` с диапазоном `0.1 ≤ value ≤ 60.0`; оба опциональны). При `enabled=true` (по умолчанию) `ApplicationContext.start()` после `_start_db_pool()` делает один SELECT к `information_schema.tables` для 5 таблиц из `SETTINGS["channels"]["postgres"]` + `SETTINGS["logging"]["db"]` (те же ключи, что проходят `validate_runtime_isolation`); при отсутствии любой из них — `SchemaValidationError` (наследник `ConfigurationError`) → `exit 2` + `stderr` через `gateway.main()` / `cli_agent.main()`. Имена таблиц **не зашиты** в коде проверки — резолвятся из SETTINGS. `timeout_sec` действует как `statement_timeout`, выставляемый на соединении пула через `utils.db.run` (сбрасывается в `finally`; соединение возвращается в пул общим, и незакрытый предел уехал бы в чужие запросы); истечение даёт **`SchemaValidationTimeoutError`** — отдельный отказ, намеренно **не** подкласс `SchemaValidationError`: таймаут не означает «нет таблиц», и отправлять оператора применять миграции там, где нужен DBA — враньё. Клиентский предел ожидания вместо серверного не годится: прерванное ожидание оставило бы запрос работать в базе. Спека: `openspec/specs/runtime/startup-schema-validation`.
- Vector-инфраструктура: `gateway.vector.*` — общий runtime (эмбеддинги + FAISS-индексы), **не привязана к домену skill'а**. Секции:
  - ~~Эмбеддинг-параметры захардкожены в `lib/services/cache_provider_impl.py`~~ — **агент больше их не читает**: `_EMBED_*` и `EMBED_TOKEN` жили в снятом кластере, а объявление провайдера, модели и размерности эмбеддинга теперь принадлежит capability `llm`/`vectors` платформы (`mcp-platform/platform.json`, секции `llm` и `vectors`). Секции `gateway.vector.embedding` в `project.json` **больше нет** (удалена, а не legacy), и переносить её некуда: настройка провайдера — не зона агента.
  - `index.*` (`VectorIndexSettings`): `enable` (гейт), `storage_table` (PG-таблица-хранилище эмбеддингов, формат `schema.table`, напр. `oarb.audit_vectors`), `default_root` (корневая папка FAISS-индексов; путь к индексу = `<default_root>/<index_name>`), `backend` (runtime-бэкенд, `"faiss"` по умолчанию), **`indexes.*`** — декларативный реестр индексов (см. ниже). `storage_table` регистрируется через `lib.core.infra_registration.register_vector_storage` → `TableRegistry.register_infra("vector.storage", ...)`; потребителя регистрации после удаления кластера не осталось (его забирал `CacheLoadService`), чистка — территория фазы 11.
  - `gateway.vector.index.indexes.<name>` (`VectorIndexConfig`, `extra="forbid"`): `table` (source-таблица из PG), `pk`, `source_table` (PG-таблица, чьи данные кэшируются в `table`), `content_columns` (строки для plaintext-поиска), `embedding_columns` (строки `col` или объекты `{column, chunk, chunk_size, chunk_overlap}`), `track_column`, `chunk_size` / `chunk_overlap`, `metric`, `enabled`. **Это единственный источник конфигурации индексов** — PG-реестр `public.agent_vector_index_config` больше НЕ читается кодом (оставлен как legacy-артефакт SQL, см. `docs/VECTOR_INDEXES.md`). Раньше читалось через `lib.services.cache_provider_impl.read_vector_index_config({})`, а сборка шла `tools/build_vectors.py` — **оба ушли вместе с кластером 2026-10-01**; состав индексов теперь объявляет `mcp-platform/platform.json::vectors.indexes`, сборку делает `mcp-platform/servers/enterprise/build_index.py`.
  - Все ключи опциональны; дефолты в `VectorIndexSettings` (для `embedding`/chunk-параметров — константы `_EMBED_*`/`_DEFAULT_CHUNK_*`) жили в снятом `cache_provider_impl`, то есть **эта секция `project.json` больше ничего не настраивает в агенте** и остаётся только декларацией.
  - **Legacy `gateway.vector_index.*` УДАЛЁН.** Обратной совместимости нет: `register_vector_storage` не читает `gateway.vector_index.*`, оставление legacy-секции (её нет и в `config.json`) runtime-mute (fail-fast через runtime-проверку, не через Pydantic). Мигрируйте на `gateway.vector.index.*`.
- Метрика занятости контекстного окна: `metadata.context_window` (`{used, limit, pct, model}`) кладётся в финальный outbound патчем `RuntimePatcher.patch_assemble_outbound` (S1) и обновляется в processing-строке в фоне через `_flush_live_context` (T2). UI: CLI — однострочную метку (`lib.cli.console_loop._print_context_window`); Streamlit-поверхность удалена в фазе 1. Гейт: `cli.show_context_window` (`config.json`, дефолт `true`).
- Запуск команд `tools.exec` (окружение субпроцесса, PATH, `pathPrepend`/`allowedEnvKeys`, allow/deny-паттерны) — подробно в `docs/INTERNAL_API.md` (§ «Конфигурация `tools.exec`»).
- Generic infrastructure tools (`workspace/tools/`):
  - `tools.history_search.*` (`enable`, `max_rows`, `max_result_chars`) — tool `history_search`: generic-поиск по долговечному журналу `agent_gateway_logs` (переживает context compaction). Параметры: `query` (ILIKE по `summary`/`payload`), `event_type`, `since`/`until`, `session_scope` (`current`|`all`), `limit`. Ищет в т.ч. `context_compacted`, `tool_call`/`tool_result`, `run_finished`, `llm_call` — чтобы агент мог вернуть выпавшие из контекста детали. Только `%s`-параметры (без интерполяции). Конфиг читается из `ctx._settings_ref.tools.history_search`.
- Наблюдаемость: tool'ы покрываются штатным `lib/hooks/tool_audit_hook.py` (tool_call_id, session_id, duration_ms, status, error_type — docs/TARGET_ARCHITECTURE.md §26). Дополнительное логирование внутри tool'а не требуется.
- **Профили конфигурации (prod / test)**: см. [docs/PROFILES.md](docs/PROFILES.md). Ключевые факты:
  - **Единственный источник профиля — CLI-флаг `--profile`** в argv `application entrypoint` **только для gateway** (см. change `unify-cli-gateway-architecture` design D8). `gateway.py` БЕЗ `--profile` падает с `exit 2` (`ConfigurationError("--profile is required")`).
  - **CLI = фиксированный профиль `test`** (`cli_agent.py`). ``--profile`` MUST NOT приниматься; передача → `ConfigurationError` + `exit 2`. CLI — локальный test/dev entrypoint, не production-deploy interface; для prod-deploy — `gateway.py`.
  - **Whitelist закрытый: `{"prod", "test"}`.** Любое другое значение (`dev`, `staging`, `foo`) — `ConfigurationError` + exit 2. Введение третьего профиля — отдельный OpenSpec change.
  - **Environment НЕ участвует в выборе профиля.** Ни одна переменная окружения — ни под историческим именем, ни под любым другим — не читается runtime-кодом для выбора активного профиля. Это запрет на источник профиля, а не whitelist имён: guard в `tests/test_profile_lifecycle.py` ловит env-fallback семантически (через AST), поэтому переименование переменной его не обойдёт. Environment остаётся легитимным каналом для **секретов**, `${VAR}`-подстановки и внешних URL. Деплои `docker-compose` / k8s / systemd / GitHub Actions должны передавать профиль через `command: python gateway.py --profile=prod`.
  - Lifecycle-gate: `SETTINGS` публикуется ТОЛЬКО через `config._initialize_settings(profile)`, вызванный из application entrypoint. `import config` — чистый import без side-effects. До явной инициализации доступ к `SETTINGS` поднимает `ConfigurationError`.
  - Режим существует **только во время разрешения конфигурации** — после получения `SETTINGS` исчезает из runtime-модели.
  - В runtime-коде **нет** `if profile == "test"` / `if profile == "prod"` — это баг, не фича.
  - При добавлении новой обязательной настройки — учтите, что она может пересечься с profile overlay; предпочтительно наследовать через `config.json` или вынести в `profiles/<mode>.jsonc`.
  - 5 profile-owned runtime-ключей (immutable после применения профиля): `channels.postgres.{table_name,messages_table,meta_table}` + `logging.db.{table_name,question_runs_table}`. Любые другие ключи в `profiles/test.jsonc` → fail-fast `ConfigurationError`. ``gateway.cache.local_path`` MUST NOT быть в `profiles/<mode>.jsonc` (Stage 7 / design D11) — это shared runtime resource.

- **`ApplicationContext.create(role=...)`** — единая typed signature для CLI и Gateway. ``role: Literal["gateway", "cli"]`` — обязательный KEYWORD_ONLY параметр. ``enable_db_logging/enable_audit/enable_cron/print_llm_calls`` принимаются только через ``**kwargs`` (deprecated boundary, с `DeprecationWarning`; будут удалены в change ``remove-deprecated-enable-kwargs``). ``profile`` MUST NOT быть named параметром (резолвится до ``create()`` через ``_initialize_settings``). ``role`` определяет composition инфраструктуры (``PostgresChannel``/``CronService`` только для ``role="gateway"``). Слоя владения кэшем больше нет: ``role`` НЕ определяет cache owner/reader.

## Scheduled Reminders

- Перед планированием напоминаний проверь доступные skills и следуй их инструкциям.
- Используй встроенный `cron` tool opencode (не вызывай `nanobot cron` через `exec`).
- `USER_ID` и `CHANNEL` бери из текущей сессии.
- Cron-задачи выполняются как scheduled turns в origin-чате и обычно возвращают результат в этот канал.
  Для фоновых проверок, которые молчат, если нечего сообщить, — используй `HEARTBEAT.md`.

**Не пиши напоминания только в `MEMORY.md`** — это не вызывает уведомлений.

## Heartbeat Tasks

`HEARTBEAT.md` (в `workspace/`) периодически проверяется встроенным cron-job'ом `nanobot gateway`,
когда `gateway.heartbeat.enabled=true` (в этом проекте — `true`, `intervalS: 1800`).
Не создавай дублирующий heartbeat-cron, если встроенный не отключён в `config.json`.

- `apply_patch` — для обычных обновлений списка задач (добавление/удаление/изменение многих строк).
- `edit_file` — для точечных замен, скопированных из текущего `HEARTBEAT.md`.
- `write_file` — для первого создания или намеренной полной перезаписи.

Если пользователь просит recurring/periodic задачу — обнови `HEARTBEAT.md`, а не создавай одноразовый cron.
Используй `cron` tool opencode для явных напоминаний, scheduled-задач с отчётом каждый запуск
или custom-расписаний, не входящих в heartbeat-список.

## Evidence-Based Claims

- **Никаких утверждений о существовании классов, методов, полей или модулей nanobot/проекта без проверки кодом.** Каждое такое утверждение в ответе, спеке или дизайне MUST сопровождаться ссылкой `file:line` (или `path:line`-диапазоном).
- **Перед утверждением** — `read`/`grep` целевого файла. Если не проверено — пиши явно «НЕ НАЙДЕНО / НЕ ПРОВЕРЕНО», не выдумывай.
- **Ссылки на upstream-репозиторий `HKUDS/nanobot` или `AlexEgorov85/workspaces_nanobot`** — только если есть инструментально подтверждённое содержимое (через `webfetch` или локально установленный пакет `nanobot`). Текст, приписанный «проверяющему», но не подтверждённый инструментом — не цитировать как факт.
- **Спека/дизайн с непроверенными цитатами** — браковать до verify-прохода. `git status` после verify должен показать, каждая `file:line` ссылка в спеке проходит через `read`/`grep`.

## Working Conventions

- Python ≥ 3.14, в коде `from __future__ import annotations` обязателен.
- Стиль: только stdlib + уже подключённые зависимости (см. `requirements.txt`).
- Логирование: `from loguru import logger` (не stdlib `logging` в новом коде).
- Импорты: `lib.*` для внутренних модулей, `workspace.utils.*` для утилит workspace.
- Не добавляй комментарии в коде без явной просьбы.
- Не коммить и не пушь без явной просьбы пользователя.
- Перед завершением задачи, если есть lint/typecheck/test команды — запусти их.

## OpenSpec

Спецификации изменений ведутся через [OpenSpec](https://github.com/Fission-AI/OpenSpec):
`openspec/specs/<capability>/spec.md` — канонические спеки; `openspec/changes/<name>/{proposal,design,tasks}.md` + `specs/<cap>/spec.md` (дельты)
— черновики изменений. Любая нетривиальная правка архитектуры (новый модуль
`lib/`, изменение таблицы БД, изменение Skill/Tool-контракта) начинается с
`openspec.cmd new change "<name>"`. Каждый шаг флоу (`proposal` → `specs` →
`design` → `tasks`) делается по инструкции, возвращаемой `openspec.cmd instructions <artifact> --change "<name>" --json`. Перед каждым write'ом —
`openspec.cmd status --change "<name>" --json` для подтверждения зависимостей.
`openspec.cmd validate <name>` должен проходить зелёным до коммита change.

**Язык OpenSpec-артефактов:** гибридный. Тело артефактов (proposal, design,
tasks, отдельные абзацы спеки) — на русском, нормативные ключевые слова,
которые парсит грaмматика OpenSpec (`SHALL`, `SHOULD`, `MAY`, `WHEN`,
`THEN`, `AND`, `OR`, `NOT`, `SHALL NOT`) — на английском. Структурные
заголовки OpenSpec-спек (`## Purpose`, `## Requirements`,
`### Requirement:`, `#### Scenario:`, `## ADDED Requirements`,
`## MODIFIED Requirements`, `## REMOVED Requirements`, `## RENAMED Requirements`,
`FROM:` / `TO:` в `RENAMED Requirements`) — на английском. Имена
собственные (имена таблиц, колонок, файлов, секций конфига, классы,
скрипты) — всегда латиницей, без перевода. Это дополняет правила для
commit-сообщений и позволяет grep'абельность по объектам репо.

## Component Specification System

Каталог компонентных спецификаций (`openspec/specs/`) описывает **архитектурный
контракт** каждого значимого компонента, а не его реализацию. Правила ведения —
`openspec/specs/architecture/component-model/spec.md` (модель + шаблон),
`openspec/specs/documentation/component-registry/spec.md` (правила реестра),
`openspec/specs/validation/component-spec-validation/spec.md` (автоматическая проверка).
Реестр — `openspec/specs/COMPONENTS.md`.

**Разделение ответственности** (чтобы не дублировать):

- `docs/TARGET_ARCHITECTURE.md` — глобальные архитектурные правила и принципы
  (цель, не «as-is»).
- `openspec/specs/<domain>/<component>/spec.md` — нормативный контракт
  конкретного компонента: назначение, граница, требования, запрещённое поведение,
  зависимости, реализация, проверка.
- `docs/*.md` (включая `ARCHITECTURE.md`, `DATABASE.md`, `INTERNAL_API.md`,
  `skill-tool-architecture.md`) — описание **текущей реализации** компонента,
  operational/reference details.
- Код — фактическая реализация.

**Когда создавать / обновлять component spec:**

- Добавил новый архитектурный компонент в `lib/`, `workspace/`, или существенный
  подкомпонент с публичным контрактом / lifecycle / конфигурацией →
  сначала запись в `openspec/specs/COMPONENTS.md` со статусом `missing`,
  затем — отдельная spec в `openspec/specs/<domain>/<component>/spec.md`.
- Изменил публичный контракт, границу или зависимости существующего компонента →
  обнови соответствующую spec **в том же изменении**.
- Изменил **нормативное правило** (принцип зависимости, граница Skill↔Tool,
  антипаттерн, инвариант) → правь `docs/TARGET_ARCHITECTURE.md`, не дублируй в spec.
- Изменил детали реализации существующего компонента (имена полей, внутренние
  helper-функции, локальная рефакторинг-оптимизация) → правь код и/или
  `docs/<соответствующий файл>`, **не** трогай spec (контракт не менялся).

**Структура spec:** обязательные разделы по шаблону `architecture/component-model`:
Назначение, Ответственность, Граница (Owns/Does not own/May depend on/Must not
depend on), Публичный контракт, Требования (с минимум одним сценарием
КОГДА/ТОГДА), Запрещённое поведение, Зависимости, Реализация (ссылки на код),
Проверка. Опциональные — Конфигурация, Жизненный цикл, Состояние, Инварианты,
Поведение при ошибке, Потребители.

**Язык:** component specs пишутся на русском (заголовки разделов, тело).
Имена классов (`ApplicationContext`), методов (`can_handle()`), файлов
(`config.json`), ключей конфига (`gateway.cache.local_path`), API
(`search_vector`) **никогда не переводятся** — это имена собственные для
grep'абельности.

**Что НЕ делать:**

- Не выдавать предположения за контракт (spec основывается на анализе кода
  или явно согласованных решениях).
- Не создавать spec для каждого `.py`-файла — только для архитектурных
  компонентов (см. определение в `architecture/component-model`).
- Не устанавливать статус `complete` без реальной проверки соответствия
  коду (статусы и их критерии — `documentation/component-registry`).
- Не дублировать один контракт в двух spec (single source of truth).

**Статусы:** `missing` → `draft` → `partial` → `complete` (или `deprecated`).
Критерии перехода — в `openspec/specs/documentation/component-registry/spec.md`.

**Валидация:** `openspec.cmd validate <name>` должен проходить зелёным до
коммита change; проверка структуры spec — см.
`openspec/specs/validation/component-spec-validation/spec.md`.

## Commit Messages

Используется [Conventional Commits](https://www.conventionalcommits.org/) с русскими описаниями.
Формат: `<type>(<scope>): <краткое описание>`.

**Основные типы (используются в проекте):**

| Тип | Когда |
|---|---|
| `feat` | Новая функциональность |
| `fix` | Баг-фикс |
| `refactor` | Внутреннее изменение без новой функциональности |
| `docs` | Только документация |
| `test` | Только тесты |
| `chore` | Служебные изменения (зависимости, конфиги, .gitignore) |

**Scope** — короткое имя подсистемы в скобках, нижний регистр: `backfill`, `media`,
`hooks`, `cli`, `config`, `audit`, `sql`, `lib`, `appctx`, `embedding`, `benchmark`, и т.д.

**Примеры из истории:**

```
feat(backfill): AW-миграция legacy-медиа ({data} -> file_id) в agent_conversation_messages
refactor(media): единый кодек media + общий MessageExchange для postgres/redis/streamlit
fix(db): не передавать () вместо None в _CursorProxy.execute
docs(development): описать SessionFileRedirectHook
chore(bench): удалить устаревшие отчёты прогонов бенчмарков
```

Время от времени для релизных коммитов используется составной тип через `+`,
например `docs+fix(2.2.0): docs/SQL-пути/.gitignore, регрессии и CHANGELOG для v2.2.0`
— это «документация + фикс регрессий в одном релизе».

**Правила:**

- Описание — на русском, в нижнем регистре, без заглавной первой буквы.
- В конце — точка не ставится.
- Тело сообщения (если есть) — через пустую строку, отделяется от заголовка.

**Язык описания (type/scope vs description):**

- `type` и `scope` — всегда латиницей (Conventional Commits).
- `description` (часть после `type(scope):`) — на русском, **но** допустимы
  английские объектные имена без перевода: имена таблиц (`audit_vectors`,
  `agent_vector_index_config`), колонок (`auditee_entity`), полей конфига
  (`storage_table`, `default_root`), инструментов (`FAISS`, `DuckDB`,
  `EXPLAIN`, `EXTRACT`), типов релизов (`baseline`), классов
  (`IndexIntegrityError`). Это имена собственные — перевод не нужен и
  только ухудшит grep'абельность.
- **Недопустимо** в description: чисто английские описания действий
  («remove X», «pin Y to Z», «enable W by default») — их нужно переводить
  на русский: «убрать X», «закрепить Y на Z», «включить W по умолчанию».

**Ретроспективное исправление старых англоязычных description:**

Не переписывать через `git rebase` поверх уже опубликованных тегов —
это force-push и ломает совместимость по SHA. Вместо этого при выпуске
очередного MINOR/MAJOR релиза:

1. Создать ветку `release/vX.Y` от текущего `master` (см. Release Process).
2. Переписать description старых коммитов через `git commit --amend` после
   cherry-pick (или `git rebase -i` в release-ветке — её история своя,
   теги ниже по истории не ломаются).
3. Закоммитить CHANGELOG и поставить тег `vX.Y.0` уже в release-ветке.
4. После тега release-ветка остаётся в репо как «чистая» история версии.

Англоязычные коммиты в `master` остаются как есть (это исторический
артефакт) — переписываются только их копии в `release/vX.Y`.

## Documentation Maintenance

Документация должна быть **всегда актуальна**. Любое изменение поведения, API или конфигурации
должно сопровождаться правкой соответствующей документации в одном коммите/изменении.

**Где какая документация:**

| Документ | Что описывает |
|---|---|
| `AGENTS.md` (этот файл) | Инструкции для opencode-ассистента: структура, конвенции, политики |
| `workspace/AGENTS.md` | Инструкции для нано-агента: storage policy, cron, heartbeat |
| `README.md` | Общий обзор проекта для пользователя |
| `docs/README.md` | Навигационный индекс каталога `docs/` (разделы, нормативная архитектура, конвенции). Содержимое подсистем — в `ARCHITECTURE.md`, `DATABASE.md`, `VECTOR_INDEXES.md`, `INTERNAL_API.md`, `TESTING.md` |
| `docs/TARGET_ARCHITECTURE.md` | **Нормативный контракт**: принципы, invariant'ы, anti-patterns, decision-чеклист. Не описывает текущую реализацию (это `docs/ARCHITECTURE.md` и др.) |
| `CHANGELOG.md` | История изменений (по разделам) |
| `workspace/HEARTBEAT.md` | Текущие активные периодические задачи |
| `workspace/skills/*/SKILL.md` | Контракт конкретного навыка (входы/выходы/инструменты) |
| `sql/README.md` | DDL-секции и порядок миграций |
| `tools/*.py` — docstring | Назначение и CLI-аргументы утилиты |
| `lib/*/README.md` | Контракт подсистемы (если есть) |

**Когда что править:**

- Изменил публичный API (`lib/...`, `workspace/utils/...`) → обнови `docs/ARCHITECTURE.md` (или соответствующий `docs/*` файл) и docstring.
- Добавил/удалил/переименовал модуль в `lib/` или `workspace/` → обнови секцию «Project Layout» в `AGENTS.md` (этом файле).
- Изменил ключ конфига или порядок мержа → обнови секцию «Configuration» в этом `AGENTS.md` + проверь `tests/test_config_keys.py`.
- Добавил/удалил endpoint канала, sql-таблицу, сессию → обнови `docs/ARCHITECTURE.md` / `docs/DATABASE.md` и при необходимости `sql/README.md`.
- Изменил поведение heartbeat/cron/MEMORY → обнови `workspace/AGENTS.md`.
- Добавил новый skill → опиши в `workspace/skills/<name>/SKILL.md` (входы/выходы/примеры запросов).
- Изменил CLI-аргументы утилиты `tools/*.py` → синхронизируй docstring и `docs/INTERNAL_API.md` (если утилита там упомянута).
- Готовишь релиз → обнови версию в `config.json` → `gateway.agent.project.version` (баннер `gateway.py`)
  на актуальный `X.Y.Z` и открой/закрой блок в `CHANGELOG.md` под ближайший раздел.
- Меняешь **архитектурные правила/invariant'ы/anti-patterns** (принципы зависимостей,
  границы Skill↔Tool, контракты) → правь `docs/TARGET_ARCHITECTURE.md` (это норма, не «as-is»).
  Детали **текущей реализации** — только в `docs/*`, правила — только в `docs/TARGET_ARCHITECTURE.md`
  (чтобы не дублировать). Каждое существенное изменение сверяй с `docs/TARGET_ARCHITECTURE.md §30–§31`.

**Принципы:**

- Документация без кода — мусор; код без документации — неподдерживаем.
- Если правка большая (новый модуль, новый канал, новый skill) — сначала опиши в `docs/` (и `docs/README.md` при необходимости),
  затем реализуй. Обратный порядок тоже допустим, но описание появится **в том же изменении**.
- Перекрёстные ссылки (`see also` / таблицы выше) должны оставаться рабочими — при переименовании
  файла пройдись по всем `.md` и поправь относительные пути.
- Если встречаешь устаревшее место в документации по ходу работы (не связанное с задачей) —
  пометь в финальном ответе, не правь без согласования.

## Release Process

Релизная политика проекта зафиксирована в `CHANGELOG.md` (формат Keep a Changelog + SemVer).
Эта секция — практический чек-лист для проведения релиза в репо
`github.com/AlexEgorov85/workspaces_nanobot`.

### Версионирование (SemVer)

- **MAJOR** (`vX.0.0`) — несовместимые изменения API, удаление публичных модулей `lib/`,
  изменение порядка мержа конфига, миграции БД с ручными действиями.
- **MINOR** (`vX.Y.0`) — новая функциональность с обратной совместимостью: новый канал,
  новый skill, новый сервис, новые ключи конфига (с дефолтами).
- **PATCH** (`vX.Y.Z`) — баг-фиксы, мелкие улучшения, регрессии после релиза.
  Допускается несколько тегов в одной ветке `release/vX.Y` (пример: `v2.0.0` → `v2.0.1`).

### Ветвление и теги

- **Разработка ведётся в `master`.** Все PR и feature-коммиты — сюда.
- **Перед MINOR/MAJOR-релизом** делается копия `master` в ветку `release/vX.Y`,
  после чего локально сразу переключаются обратно на `master`.
- **PATCH-релизы** делаются в той же ветке `release/vX.Y` (cherry-pick фиксов из `master`).
- Тег `vX.Y.Z` ставится аннотированным прямо в релизной ветке
  (см. существующий `tag: v2.2.0` на `release/v2.2`).
- Ветка `release/vX.Y` остаётся в репо после релиза (это история версии).
  Обратного мержа в `master` обычно не требуется, если CHANGELOG — единственный артефакт,
  который меняется только в релизной ветке.

### Чек-лист релиза (MAJOR/MINOR)

1. **Подготовка в `master`**
   - Все PR смержены, рабочая копия чистая.
   - Текущая секция `[Unreleased]` в `CHANGELOG.md` полностью заполнена (категории
     Keep a Changelog: `Added` / `Changed` / `Deprecated` / `Removed` / `Fixed` / `Security`).
   - Документация актуальна: `README.md`, `docs/README.md`, `AGENTS.md` (этот файл),
     `workspace/AGENTS.md`, `workspace/skills/*/SKILL.md`, `sql/README.md`.

2. **Тесты и проверки**
   - `pytest` зелёный. Ориентир по объёму: **3603 теста собираются**
     (`python -m pytest tests/ -q --collect-only`). Зелёный прогон требует
     доступного PostgreSQL (часть тестов работает с test-профилем и падает с
     `could not translate host name "test"` без БД) и терминала с UTF-8
     (иначе падают assert'ы на русских строках в логах).
      - Если менялся SQL — миграция применена локально, create-скрипты подтверждены.
   - Если менялся конфиг — `tests/test_config_keys.py` обновлён, `REQUIRED_KEYS` синхронизирован.
   - Если менялся публичный API `lib/` — пройден smoke через `cli_agent.py` или `gateway.py`.

3. **Создание релизной ветки**
   ```bash
   git checkout master && git pull
   git checkout -b release/vX.Y
   ```
   После создания ветки сразу вернуться на `master`:
   ```bash
   git checkout master
   ```

4. **Версионирование в `CHANGELOG.md` (в ветке `release/vX.Y`)**
   - Переименовать `## [Unreleased]` → `## [X.Y.0] — YYYY-MM-DD`.
   - Добавить свежий пустой блок `## [Unreleased]` сверху (для следующих изменений).
   - В эпиграфе блока указать тип: `**MAJOR-релиз:** …` / **MINOR-релиз:** …`.
   - Обновить `config.json` → `gateway.agent.project.version` на `X.Y.Z` (канонический
     источник версии для баннера `gateway.py`; git-теги и первый релизный блок
     CHANGELOG на `master` отстают из-за release-веток).

5. **Коммит и тег**
   ```bash
   git add CHANGELOG.md
   git commit -m "docs+fix(X.Y.0): <краткое описание> и CHANGELOG для vX.Y.0"
   git tag -a vX.Y.0 -m "vX.Y.0 — <краткое описание>"
   git push origin release/vX.Y --follow-tags
   ```

6. **GitHub Release (опционально)**
   - На странице `github.com/AlexEgorov85/workspaces_nanobot/releases/new` выбрать тег `vX.Y.0`.
   - Описание скопировать из блока в CHANGELOG.

### Чек-лист PATCH-релиза

1. Cherry-pick фикса(ов) из `master` в `release/vX.Y`:
   ```bash
   git checkout release/vX.Y
   git cherry-pick <sha1> [<sha2> ...]
   ```
2. Добавить блок в `CHANGELOG.md` (категория `Fixed`, эпиграф `**PATCH-релиз:** …`).
3. Закоммитить, поставить тег `vX.Y.Z`, запушить ветку с тегами.

### Что НЕ делать при релизе

- **Не редактировать CHANGELOG задним числом** для уже опубликованных версий —
  только добавить новый PATCH-релиз сверху.
- **Не забывать про свежий `## [Unreleased]`** — без него следующий релиз не имеет точки привязки.
- **Не пушить тег без коммита с CHANGELOG** — тег обязан указывать на релизный коммит.
- **Не ставить тег в `master` напрямую** — только через ветку `release/vX.Y`.
- **Не продолжать разработку в `release/vX.Y`** — фиксы только через cherry-pick,
  вся новая функциональность — в `master`.
- **Не создавать PATCH-ветку** (`release/vX.Y.Z`) — патчи идут в существующую `release/vX.Y`.
