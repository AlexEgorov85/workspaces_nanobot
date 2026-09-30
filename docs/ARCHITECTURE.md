# 🏗 Архитектура и сервисный слой

> Навигационный индекс каталога `docs/` — в [`README.md`](README.md). Этот документ —
> самодостаточное описание подсистемы.
>
> **Это описание текущей реализации («as-is»).** Нормативная целевая архитектура
> (принципы, invariant'ы, anti-patterns, decision-чеклист) — отдельный контракт
> [TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md). Правила и «как должно быть»
> ищите там; здесь — только то, как оно работает сейчас.

## 🏗 Архитектура

Инфраструктура (DuckDB-кеш, векторные индексы, эмбеддинги) вынесена из навыка
в **универсальный слой** `lib/services` — он не завязан на предметную область
«аудит» и может переиспользоваться любым навыком. Навык `audit_analyzer` остался
тонким CLI: он конфигурирует провайдера из своих настроек и работает с ним
напрямую (без промежуточных обёрток-шимов).

> **Об именах таблиц и индексов.** Все имена таблиц/индексов, упомянутые ниже, —
> **не зашитые константы**, а значения текущей инсталляции, настраиваемые в
> `project.json`. Они могут отличаться в других развёртываниях. Ключи конфигурации:
> `channels.postgres.table_name` / `messages_table` / `meta_table`,
> `skills.audit_analyzer.tables[*].name` / `vector_indexes[*].name`,
> `gateway.vector.index.storage_table`,
> `logging.db.table_name` / `question_runs_table`,
> `gateway.vector.index.storage_table`. Точный список и дефолты — в
> [TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md) и [AGENTS.md](../AGENTS.md).
> Таблицы бенчмарков (`agent_benchmark_runs` / `agent_benchmark_results`) и
> настройка `benchmark.*` удалены в фазе 1 миграции `enterprise-mcp-platform`.

```mermaid
flowchart LR
    SRC["Данные аудита<br/>(в БД, имена в project.json)"] --> SYNC["Фоновая синхронизация<br/>изменения в кеш"]
    VEC["Эмбеддинги строк"] --> SYNC
    SYNC --> CACHE["Локальный кеш<br/>DuckDB + FAISS"]
    CACHE -->|публикация| FILE[("Файл кеша<br/>cache.duckdb")]
    AGENT["Агент / Навык"] --> CACHE
    AGENT --> EMB["Эмбеддинг запроса<br/>(Ollama)"]
    classDef core fill:#fff3cd,stroke:#d39e00,stroke-width:2px
    classDef infra fill:#d4edda,stroke:#1b7a3d,stroke-width:2px
    class SYNC,CACHE,AGENT,EMB core
    class SRC,VEC,FILE infra

```

**Потоки данных**

- `gateway.py` — единственный владелец файла кеша навыка. `CacheLoadService`
  (единственное подключение к PG, синхронная разовая загрузка, без фонового
  потока) наполняет `DuckDbCacheStore`, закрывает файл и отпускает его; провайдер
  на всё время работы процесса открывает файл только на время операции.
- Навык CLI (`predefined` / `generated_sql`) — запросы выполняются по локальному
  снимку кеша, открывая его на время операции в режиме `READ_ONLY`. Создание и
  обновление кеша его не касается. Единый интерфейс бэкенда:
  `get_schema / query_sql / explain`.
- `--mode vector` — семантический поиск: FAISS-индекс собирается **в памяти**
  из локального снимка `gateway.vector.index.storage_table` при старте gateway
  (`PreloadService.preload_vector_indexes`), эмбеддинг запроса получает через Ollama.

---

## Сервисный слой (ApplicationContext + lib/)

После выделения сервисного слоя gateway и cli_agent сократились и вся инициализация вынесена в `ApplicationContext`
(см. подробности в `CHANGELOG.md`). Этот раздел — про
**внутреннее устройство** нового слоя, нужно при добавлении новых
сервисов или изменении lifecycle.

### Точки входа → общий bootstrap

```mermaid
flowchart LR
    GW["gateway.py"] --> CTX["ApplicationContext<br/>create / start / stop"]
    CLI["cli_agent.py"] --> CTX
    CTX --> CFG["ConfigService"]
    CTX --> SESS["SessionStorage"]
    CTX --> DBL["DbLoggingService"]
    CTX --> SYNC["PgDuckDbSync + CacheStore"]
    CTX --> BUS["MessageBus"]
    CTX --> AGENT["AgentFactory (AgentLoop)"]
    CTX --> PATCH["RuntimePatcher"]
    CTX --> PRE["PreloadService"]
    CTX --> TRANS["TranscriptionService"]
    classDef entry fill:#d1ecf1,stroke:#0c5460,stroke-width:2px
    classDef core fill:#fff3cd,stroke:#d39e00,stroke-width:2px
    class GW,CLI entry
    class CTX,CFG,SESS,DBL,SYNC,BUS,AGENT,PATCH,PRE,TRANS core
```

### `lib/core/` (ApplicationContext + фабрики)

- **`application_context.py:ApplicationContext`** — единственный класс,
  собирающий все общие сервисы. Поля (помимо путей и конфига):
  `bus`, `agent`, `tool_audit_hook`, `hooks`, `session_manager`,
  `storage_mode`, `db_logging_service`, `sync_service`,
  `cache_store`, `config_service`, `runtime_patcher`,
  `transcription_service`, `subprocess_manager`, `preload_service`,
  `runtime_health`, `runtime_readiness`, `session_storage_service`,
  `hook_factories`, `project_settings`.
  Метод `start()` использует `ShutdownCoordinator` для регистрации
  сервисов; `stop()` — LIFO graceful shutdown.
  **Graceful degradation:** если БД недоступна, сервис остаётся `None`,
  gateway/cli работают без него (с предупреждением в логах).
- **`agent_factory.py:AgentFactory`** — `create(config, bus, session_manager=...,
  cron_service=..., db_logging_service=..., project_hooks=...)` → `(agent, hooks,
  hook_factories)`. `project_hooks` (плагины `workspace/hooks/`) ставятся
  ПЕРЕД `ToolAuditHook` в `hooks=`; агент создаётся ровно один раз.
  `DatabaseLoggingHook` подключается НЕ как общий инстанс, а как фабрика
  оборота (`make_db_logging_hook_factory` в `hook_factories=`) — фреймворк
  создаёт свежий инстанс на каждый оборот, изолируя состояние вопроса между
  конкурентными сессиями (см. `database_logging_hook.py`).
  Lazy-import `lib.hooks.database_logging_hook` через try/except
  (если модуль не подключён — фабрика просто не создаётся).
  Флаг `print_llm_calls` пробрасывается в фабрику оборота: при `True`
  хук печатает в терминал токены каждой LLM-итерации (включается всегда
  в CLI-режиме через `cli_agent.py`; в gateway — опцией
  `gateway.print_llm_calls`).
- **`bus_factory.py:BusFactory`** — `create()` возвращает `MessageBus`,
  опционально обернув `publish_inbound`/`publish_outbound` async-логгерами
  из `db_logging_bus.py`. **Без monkey-patch'ей**: оригинальные методы
  шины сохраняются в замыкании.

### `lib/services/`

Полный список модулей — см. раздел [«Структура проекта»](#структура-проекта) ниже. Здесь — только
**Новые**, с краткой мотивацией:

| Сервис | Мотивация (почему выделен) |
|--------|---------------------------|
| `config_service.py` | Дубликат `_load_runtime_config` + `SETTINGS`-аксессора между gateway и cli. Pre-resolve `${PROVIDER_API_KEY}` от .secrets.env (см. ниже). |
| `session_storage.py` | Выбор `PGSessionManager` / `SessionManager` (auto / postgres / file) с поддержкой `session_manager.json` override. |
| `runtime_patcher.py` | Все 12 monkey-patch'ей upstream `nanobot.agent.loop.AgentLoop` в одном классе с fallback при изменении API nanobot. Применяется через `apply_all()` из `ApplicationContext.create()`. **НЕ** занимается регистрацией project tools (вынесено в `project_tool_loader.py`). Полный каталог — `docs/architecture/runtime-patcher-inventory.md`. |
| `project_tool_loader.py` | Stateless helper для регистрации кастомных tool'ов из `workspace/tools/*.py`. Единственный публичный контракт: `register_project_tools(...) -> ProjectToolsLoadResult`. Вызывается из `ApplicationContext.create()` сразу после `apply_all()` как независимый stage composition root'а. **НЕ** компонент (нет lifecycle/state/config — критерии `openspec/specs/architecture/component-model/spec.md`). |
| `channel_factory.py` | `ChannelManager` + Redis + Postgres каналы + транскрипция (вынесено из gateway). Конструктор принимает `print_worker_activity` (пробрасывается в `PostgresChannel` из `gateway.print_worker_activity`). |
| `transcription_service.py` | openai/groq key/URL/language (вынесено из gateway). |
| `preload_service.py` | Только FAISS preload (`preload_vector_indexes`) для gateway. Legacy CLI-методы `preload_audit_cache` / `background_audit_cache_refresh` / `start_audit_cache_tasks` / `stop_tasks` удалены в `refactor/core-extract-duckdb-faiss`: единственный писатель DuckDB-снимка — `DuckDbCacheStore.publish()` через gateway; путь снимка вычисляется через единый `resolve_cache_path()` (`lib/core/application_context.py`) — default `~/.cache/nanobot/duckdb/cache.duckdb` или override `gateway.cache.local_path`. CLI/skill/vector_index_service вызывают ту же функцию, так что расхождение невозможно. |
| `db_logging_service.py` | **Новый** — структурированный журнал агента в `agent_gateway_logs` (имя настраивается через `logging.db.table_name`). |
| `db_logging_bus.py` | **Новый** — обёртки `publish_inbound`/`publish_outbound` для `DbLoggingService`. |
| `schema_formatter.py` | **Удалён** — internal service для формирования описания схемы БД. Использовался только `NlSqlRunner`'ом, который тоже удалён. Замена: skill `audit_analyzer` сам читает схему из `SKILL.md` (секция «Схема домена», см. `workspace/skills/audit_analyzer/SKILL.md`). |
| `nl_sql_runner.py` | **Удалён** — общая логика NL→SELECT pipeline. Заменена: CLI skill'а `audit_analyzer` — режим `--mode generated_sql` (`workspace/skills/audit_analyzer/scripts/generated_sql_mode.py`, прямой вызов `lib.services.llm_client.call_llm`), либо Agent формирует SQL сам (см. `SKILL.md` секция «SQL guidance»). |

### Pre-resolve `${VAR}` от `.secrets.env`

`nanobot._load_runtime_config` резолвит `${LLM_API_KEY}` только из
`os.environ` и при отсутствии падает `ValueError`. Между тем `config.py`
для провайдерских ключей использует провайдер-скоупинг формат
`.secrets.env`:

```
# providers: llm
api_key=XavGPsHjtNt3uOtFGUhabUuad5PRm2D0W
```

— в `os.environ` это не попадает как `LLM_API_KEY`. Решение
(`ConfigService._pre_resolve_env_refs`): прочитать `config.json`,
найти `${VAR}` плейсхолдеры, для каждого `*_API_KEY` без env — достать
ключ из `SETTINGS.providers.<любой>.api_key` (туда `config.py` уже
подставил значение) и положить в `os.environ` ДО `_load_runtime_config`.
**Gateway больше НЕ требует `export LLM_API_KEY=...` в shell.**

> **Миграция с исторического имени:** в старых конфигах `.secrets.env` мог
> быть `# providers: mistral`. Логика остаётся обратно совместимой: ключ
> из любой непустой секции `providers.*` подставляется как `LLM_API_KEY`.
> Достаточно переименовать секцию в `# providers: llm` для ясности.

### Единый резолв LLM-конфигурации: `lib/services/llm_config.py`

Резолв провайдера/модели/ключа вынесен в общий модуль
`lib/services/llm_config.py::resolve_llm_config()`: дефолт берётся из
`agents.defaults` (модель/провайдер) и `providers.<provider>`
(`apiBase`/`apiKey`) уже-резолвнутых `SETTINGS`, переопределения
(например, `skills.audit_analyzer.llm_*`) передаются через `overrides`.

Используется единообразно:
* навыком `audit_analyzer` — `scripts/skill_config.py::get_llm_config()`.

Потребитель в лице бенчмарк-раннера (`benchmarks/runner.py::_run_suite()`)
удалён вместе с подсистемой бенчмарков.

Так смена модели/провайдера/ключа агента автоматически меняет LLM и в
навыке, и в бенчмарке — без дублирования секретов в трёх местах.

### Гонка за загрузкой устранена структурно

Раньше `PgDuckDbSyncService` был worker-потоком, который делал `initial_load`
сразу после `start()`, а привязка колбэков шла отдельным шагом. Если
`set_on_new_records_callback` ещё не был вызван, `_dispatch` скипал записи →
DuckDB оставался пустым → `preload_vector_indexes` видел «нет данных» несмотря
на данные в `oarb.audit_vectors`. Обходной путь был один: в `gateway.py:main()`
колбэки ставились **ДО** `ctx.start()`, и гонка была лишь «не проявляется».

Сейчас гонки нет вовсе, а не «не проявляется»: `CacheLoadService.load()`
выполняется **синхронно внутри `ApplicationContext.create()`** и завершается до
того, как начнётся `preload_indexes()`. Колбэков у загрузчика нет — он держит
`CacheStore` напрямую, потому что он и есть единственный writer. Порядок
«загрузка → close → открытие на чтение» закреплён тестом
`tests/test_application_context_cache_lifecycle.py`.

### Конкурентно-безопасное БД-логирование: per-turn инстанс `DatabaseLoggingHook`

Обороты разных сессий (вопросов) обрабатываются конкурентно
(`AgentLoop._dispatch` → `_concurrency_gate`, дефолт
`NANOBOT_MAX_CONCURRENT_REQUESTS=3`). Проблема: фреймворковый
`AgentRunHookContext` (для `after_run`) **не содержит `session_key`** —
контекст вопроса приходится хранить в самом хуке.

**Исторический баг:** один общий инстанс `DatabaseLoggingHook` на все
сессии, контекст в плоских полях (`_run_session_key`/`_request_id`).
При конкурентном переплетении оборотов чужой вопрос перезаписывал
поля между `before_`/`after_execute_tool`, и:
  * `log_tool_result` / `run_finished` получали request_id чужого вопроса;
  * `after_run` мог `finish_request`/`clear_request` чужую сессию.

**Решение — фабрика оборота.** `DatabaseLoggingHook` создаётся на
КАЖДЫЙ оборот через `make_db_logging_hook_factory(db_logging_service,
agent_id)` (`lib/hooks/database_logging_hook.py`). Фабрика получает
`AgentTurnHookContext` (там есть `session_key`), резолвит `request_id`
через `service.get_request_id(session_key)` и запекает оба значения в
конструкторе — состояние вопроса изолировано между сессиями, гонки нет.

`AgentFactory.create` передаёт фабрику в `AgentLoop` через
`hook_factories=[...]` (а не общий инстанс в `hooks=...`). `ToolAuditHook`
остаётся в `hooks=` — он bucket-безопасен по `session_key`.

`RuntimePatcher.patch_subagent_logging` использует тот же паттерн:
каждый `_SubagentLoggingHook` создаёт СВОЙ `_db_hook` на запуск
подагента (class-level shared `_db_hook` давал ту же гонку между
конкурентными субагентами).

Регрессионный тест: `tests/test_hooks_database_logging.py →
TestDatabaseLoggingHookFactory.test_concurrent_sessions_do_not_mix_request_id`
(переплетение двух сессий → `log_tool_result`/`after_run` несут свой
`request_id`).

### Единый конвейер structured-логирования (`DbLoggingService`)

PG→DuckDB sync-путь пишет события (`sync_service_started`,
`sync_initial_load_done`, `sync_table_loaded`, `sync_publish_ok`/
`sync_publish_empty`/`sync_publish_failed`, …) и compaction-события
(`context_compacted`) в `agent_gateway_logs` **единым способом** —
через `DbLoggingService` (см. `lib/services/db_logging_service.py`).

**`DbLoggingService` — единственный runtime writer
`agent_gateway_logs` и `agent_question_runs`.** Любой structured event
передаётся через:

- `db_logging_service.log_event(LogEvent(...))` — основной путь
  (асинхронный, через пул-воркер; `timestamp` проставляется на flush,
  `flush_interval_sec=5`);
- `DbLoggingService.try_log_event(svc, log_event, *, producer, event_type)`
  — defensive helper для producer'ов (контракт WARNING при
  недоступности сервиса, no-op for business). Это контрактно
  единый уровень для всех producer'ов — никаких per-producer уровней
  или fallback-INSERT'ов.

**Прямой SQL INSERT в журнал запрещён** — это invariant архитектуры,
защищён `tests/test_unified_event_logging_pipeline.py::TestNoProductionDirectWriters`
(AST + ownership guard).

Producer'ы (с обязательным keyword-only DI через `db_logging_service=`):

| Producer | События | DI |
|---|---|---|
| `ContextCompactionService` | `context_compacted` | через `RuntimePatcher.patch_compact_command(partial(...))` или `run_repl(...)` параметр |
| `CacheLoadService` | `cache_load_started`, `cache_load_done` | kwarg `db_logging_service` |
| `DuckDbCacheStore` | `sync_publish_ok`/`_failed`/`_empty`, `vector_preload_error`, `vector_index_build_failed` | kwarg `db_logging_service` |
| `PreloadService` | `vector_index_preload_health` | kwarg `db_logging_service` |
| `ApplicationContext._make_sync_services` | `sync_skipped_*` | inline `try_log_event` |
| `DatabaseLoggingHook` (AgentLoop) | `tool_call`/`tool_result`/`llm_call`/`run_finished`/`turn_failed` | kwarg `db_logging_service` |

DI поднимается через `functools.partial` (`RuntimePatcher.patch_compact_command`)
и параметры composition root'ов (`run_repl(...)` в `lib/cli/console_loop.py`).
**Никаких DI-полей на `agent`** (ни `_db_logging_service`, ни
`db_logging_service`) — это историческая ошибка, исправленная в коммите
`1893b17`.

**Skill invocation is out of scope.** Skills не имеют dedicated
runtime `event_type`; загрузка `SKILL.md` в context не порождает event;
вызов Skill-скриптов через `tools.exec` логируется как штатная пара
`tool_call`/`tool_result`; `DbLoggingService.log_skill_call` НЕ
вводится; `event_type="skill_call"` НЕ эмитится. См.
`openspec/specs/logging-db/spec.md` requirement «Skill invocation
is out of scope».

**Зачем единый конвейер (историческая проблема).** Раньше события
писались двумя путями с разной семантикой времени: `DbLoggingService`
буферизовал батчи и проставлял `timestamp` на flush
(`flush_interval_sec=5`), а `workspace.utils.event_log.record_sync_event`
делал прямой INSERT с мгновенным `NOW()`. Из-за этого
`sync_publish_ok` мог получить время РАНЬШЕ `sync_service_started` —
ложная хронология в журнале. После унификации (change
`unify-agent-event-logging-pipeline`) все события идут через
`DbLoggingService`/`try_log_event`, хронология — одним потоком.

**Удалённый модуль.** `workspace/utils/event_log.py` (197 строк,
`record_event`/`record_sync_event`/`emit_sync_event`) удалён в коммите
`1893b17`. Прямой INSERT bypass ликвидирован. Тесты
`tests/test_event_log.py` (83 строки) тоже удалены.

### Видимость «тихих» ошибок: preload векторов и канал

Общий принцип: проблемы, которые раньше молча проглатывались, должны
попадать в `agent_gateway_logs` (post-factum, `history_search`) и в
терминал gateway (мгновенно). Два источника «тихих» сбоев подняты на
этот уровень:

**1. FAISS-preload при старте** (`DuckDbCacheStore.preload_indexes`).
Раньше ошибка чтения списка source была беззвучной (`return []`), а
провал построения одного индекса — тихим `continue`: gateway печатал
dim-«нет данных в кэше», неотличимо от реального отсутствия данных.
Теперь `preload_indexes`:

* собирает ошибки в `self._preload_errors` (аксессор
  `preload_errors()`; сбрасывается при каждом вызове);
* пишет события `vector_preload_error` (ошибка чтения `source` из
  таблицы хранения, `index_name=None`) и `vector_index_build_failed`
  (ошибка построения конкретного индекса) через
  `DbLoggingService.try_log_event(...)` — в журнал; при недоступности
  сервиса — no-op + WARNING внутри `try_log_event`; дополнительно
  дублирует warning в терминал (`logger.warning`, stdlib-logging).
* `PreloadService.preload_vector_indexes` логирует
  `logger.warning` (loguru) при собственном исключении вместо тихого
  `None`;
* `gateway._preload_and_report` по `cache_store.preload_errors()`
  печатает красный список ошибок построения вместо/вместе
  dim-строки «нет данных».

**1.1. Health-summary declared vs runtime (vector index discovery)**.
До этого — даже при полностью diverged состоянии (объявил
новый индекс в `gateway.vector.index.indexes.*`, но не собрал blob
через `tools/build_vectors.py`) gateway **молча** показывал зелёный
«vector index … loaded» через fallback-цепочку
(`store → vdb → cache → files`), без какого-либо указания, что на
самом деле расхождение есть. Теперь `PreloadService.preload_vector_indexes`
после прогона считает явное расхождение между **declared** (JSON,
`project.json::gateway.vector.index.indexes.*`) и **runtime** (DuckDB-снапшот
таблицы-хранилища `gateway.vector.index.storage_table`,
`cache_provider_impl.list_runtime_vector_indexes()`), классифицируя каждое
имя индекса в одну из категорий:

  * `missing` — объявлен в JSON, но не найден в снапшоте-хранилище;
  * `orphan` — есть строки в снапшоте-хранилище, но индекс не объявлен в JSON;
  * `stale` — строки есть, но сигнатура не совпадает с текущим cfg
    (помечается как `STALE` или `INVALID`).

Сводка печатается в **stderr** (multi-line, без ANSI) и пишется в
`agent_gateway_logs` через `DbLoggingService.try_log_event`
(event_type `vector_index_preload_health`, level=`WARN` если есть
divergence, иначе `INFO`). Ошибки любого этапа (PG недоступна, config
parse failed) глотаются — summary **никогда** не валит startup gateway.

Чистая логика вычисления — в pure-функции `compute_index_health()`
в `preload_service.py`, отделена от I/O и эмита; тестируема без mock'ов
PG/JOBS.

**2. PostgresChannel — циклы опроса БД.** Ошибки
`poll_inbound`/`_poll_once`, `_lease_loop`, `_unstick_loop` раньше шли
только в `self.logger.error` (loguru, терминал). Теперь каждая
дублируется в `agent_gateway_logs` через метод `_journal_event`
(`PostgresChannel`), событие-типы `channel_poll_error` /
`channel_lease_error` / `channel_unstick_error` (payload:
`component`/`error_type`/`error`). `_journal_event` — no-op без
запущенного `DbLoggingService` (тесты/standalone пишут без сервиса);
внутренние ошибки журналирования глотаются.

`DbLoggingService` инжектится в `PostgresChannel` по цепочке:
`gateway._run` → `ChannelFactory(db_logging_service=ctx.db_logging_service)`
→ `create_all` → `PostgresChannel(config, bus, db_logging_service=...)`.

### Метрика занятости контекстного окна (`metadata.context_window`)

**Задача.** Видеть в UI, сколько процентов контекстного окна модели
занято финальным запросом — и обновлять это пока агент ещё думает
(живое обновление processing-строки), не дожидаясь конца оборота.

**Решение — три компонента + мост:**

1. **Мост per-iteration usage** (`lib/hooks/database_logging_hook.py`).
   Потокобезопасный словарь `_CONTEXT_BRIDGE: dict[str, dict]` под
   `threading.Lock`. Ключ = `session_key` (`postgres:<chat_id>`), чтобы
   конкурентные сессии не «перепутались». Публичные функции:
   * `seed_context_window(session_key, limit=, model=)` — патч на
     старте оборота кладёт лимит окна и модель (знает только агент).
   * `DatabaseLoggingHook.after_iteration` → `_store_iteration_usage`
     пишет СВЕЖИЙ по-итерационный `context.usage` (именно последняя
     итерация — то, что модель реально видела в финальном запросе).
   * `_store_context_window` — финальный готовый блок, его кладёт
     `_attach_context_window` (патч 2a).
   * `get_context_window(session_key)` — канал читает для live-update;
     предпочитает готовый блок, иначе собирает на лету из
     usage+limit.
   * `pop_context_bridge(session_key)` — анти-stale, чистится при
     `_finalize_turn` и `_mark_failed`.

2. **Патчи `RuntimePatcher`** (`lib/services/runtime_patcher.py`):
   * `patch_context_bridge_seed` (патч 2b) — оборачивает
     `agent._state_build`: на старте оборота сеет лимит/модель в мост
     (best-effort, ошибки не мешают обороте).
   * `_attach_context_window` в `_wrap` `_assemble_outbound` (патч 2a)
     собирает блок `{used: int, limit: int, pct: float (4 знака, clamp 0..1),
     model: str}` из usage последней итерации ÷ лимит окна и кладёт в
     `result.metadata["context_window"]`. Готовый блок дополнительно
     кладётся в мост для канала. Если мост пуст (DB-логирование
     выключено), фолбэк на `agent._last_usage` (сумма по итерациям —
     завышает, но лучше чем ничего).

3. **Живое обновление в канале** (`lib/channels/postgres_channel.py`):
   `_flush_live_context` в `_flush_reasoning_loop` (каждые
   `_flush_interval` секунд) читает `get_context_window(session_key)` и
   пишет блок в `metadata.context_window` processing-ассистент строки
   в БД. Внешний UI видит его через свой поллинг
   `metadata.context_window` и рисует прогресс-бар, который
   заполняется «вживую» по мере роста промпта. После финализации
   оборота `_drop_context_bridge(chat_id)` снимает мост.

**Место хранения:** `metadata.context_window` в JSONB
`agent_conversation_messages` (S1). Без миграций: канал уже сливает
`metadata` целиком в `_finalize_turn`.

**UI:**
* **Streamlit** (`streamlit_app.py`): `_render_context_window(block)` —
  `st.progress(pct, text="Контекст: used / limit · NN% · model")`.
  Рисуется один раз для финальной строки (после загрузки истории)
  и live для processing-строки (каждый poll). Метка `metadata.kind ==
  "context_compact"` (ContextCompactionService) даёт отдельный стиль
  `.compact-notice`.
* **CLI** (`lib/cli/console_loop.py`): `_print_context_window(block)` —
  одна строка `[dim]📊 Контекст: used / limit · NN% · model[/dim]`
  после `_typewriter(content)`. Гейт `cfg.show_context_window`
  (по умолчанию `true`).

**Конфигурация** (`project.json`):
* `cli.show_context_window` (bool, дефолт `true`) — печатать в CLI.
  В `REQUIRED_KEYS` (`tests/test_config_keys.py`).

**Что не делается (явные «нет»):**
* Не суммируется usage по итерациям — `agent._last_usage` слишком
  завышает занятость на многоитеративных оборотах (токены
  накапливаются, но окно модели — снапшот последней итерации).
* Не рисуется в UI из `agent._last_usage` (только из моста) — иначе
  будет рассинхрон с финальным блоком.
* Нет auto-tighten окна (сжатие) — это задача
  `ContextCompactionService` (`lib/services/context_compaction.py`),
  отдельный поток.

**Тесты:** `tests/test_database_logging_bridge.py` (мост),
`tests/test_runtime_patcher.py::TestPatchContextBridgeSeed` (патч 2b),
`tests/test_postgres_channel.py::TestPostgresChannelContextWindow`
(live-update + drop), `tests/test_streamlit_app.py::TestRenderContextWindow`
(UI), `tests/test_console_loop.py::TestPrintContextWindow` (CLI).

### Управление сжатием контекста: `ContextCompactionService`

**Задача.** Дать пользователю и оператору видимый след любого сжатия
контекста диалога — и ручного (``/compact``, ``compact_context``),
и автоматического (idle, token-budget). Один и тот же формат,
один и тот же путь записи.

**Архитектура (один путь — три входа):**

```mermaid
flowchart LR
    TOOL["compact_context (tool)"] --> SVC["Сжатие контекста"]
    SLASH["/compact (slash + CLI)"] --> SVC
    AUTO["Авто: idle / token-budget"] --> SVC
    SVC["Сжатие контекста"] -->|notify| OUT["Заметка в истории<br/>+ лог + UI"]
    classDef entry fill:#d1ecf1,stroke:#0c5460,stroke-width:2px
    classDef core fill:#fff3cd,stroke:#d39e00,stroke-width:2px
    class TOOL,SLASH,AUTO entry
    class SVC,OUT core
```

**Точки входа:**

1. **Настоящая slash-команда ``/compact``** —
   upstream `nanobot/command/builtin.py::cmd_compact`, расширяется
   `RuntimePatcher.patch_compact_command` (fail-soft обёртка) в `agent.commands`
   (`CommandRouter`), где это единственный путь, общий для всех каналов
   (postgres, redis, telegram). В `run()` зарегистрированные команды
   перехватываются **до** LLM (``_dispatch_command_inline`` /
   ``_state_command``), поэтому сжатие срабатывает детерминированно и
   безоговорочно, а не «по усмотрению» модели. Handler ставит
   ``FINAL_TURN_KEY="_final_turn"`` в outbound (см. «Воркеры не берут
   задачи»), зовёт ``svc.compact(session_key=ctx.key, idle=..., force=True)``.

2. **CLI-команда ``/compact``** (`lib/cli/console_loop.py::_run_cli_compact`).
   Приватный путь REPL: в `run_repl` после проверки `_is_exit_command`
   перехват ``if command == "/compact" or command.startswith("/compact ")``.
   Создаёт локальный `ContextCompactionService(agent, settings=None)`
   (в CLI нет `SETTINGS`, но `_write_history_notice` сам отключается —
   нет `channels.postgres.dsn`), зовёт
   ``svc.compact(session_key="cli:<chat_id>", force=True)``, печатает
   отчёт Rich-цветом (cyan/yellow). Флаги `idle` / `--idle` / `-i`
   допустимы для совместимости, поведение не меняют (``force=True``
   уже подразумевает жёсткое idle-сжатие).

3. **Tool ``compact_context``** (`workspace/tools/compact_context.py`),
   регистрируется `RuntimePatcher.patch_project_tools` в `apply_all`
   (см. `lib/services/runtime_patcher.py`). Параметры:
   `session_key: str | None` (по умолчанию — текущая из
   `current_request_session_key()`), ``idle: bool=False``, ``force: bool=True``.
   Пустой вызов ``compact_context({})`` (= ручная просьба пользователя)
   трактуется как ``force=True`` — жёсткое сжатие независимо от порога
   токенов (JSON-schema-дефолт nanobot не применяется, значение подставляет
   Python-сигнатура). Явный ``force=False`` возвращает в token-budget режим.

4. **Авто-сжатие** — обёртки `patch_compaction_tracking` в
   `runtime_patcher`:
   * `_wrap_auto_compact_archive` (`agent.auto_compact._archive`) —
     перед/после вызова замеряет `last_consolidated` и `tokens`,
     если курсор сдвинулся и `result` непустой — зовёт
     `svc.record_external_compaction(...)`.
   * `_wrap_maybe_consolidate_by_tokens`
     (`agent.consolidator.maybe_consolidate_by_tokens`) — то же:
     diff `last_consolidated` до/после; если сдвинулся — пишет
     заметку через `record_external_compaction`.

Ручной вход ``/compact`` имеет два обработчика одного слова: slash-команда
в ``CommandRouter`` (сетевые каналы) и перехват в REPL. Оба ставят
``force=True``.

**Семантика ``force``.** ``ContextCompactionService.compact(session_key, *,
idle=False, force=False, max_suffix=8)``: если ``idle or force`` — идёт
жёсткое усечение ``consolidator.compact_idle_session``, иначе — token-budget
``maybe_consolidate_by_tokens`` (пропустит сессию ниже ``consolidationRatio``).
``force=True`` выражает явное пользовательское действие (``/compact`` или
ручной вызов tool'а) и игнорирует порог токенов. Ручные пути (slash-команда,
CLI, пустой вызов tool'а) всегда ``force=True``; только явный
``force=False`` возвращает в token-budget режим.

**Оценка размера сессии.** ``_estimate`` зовёт нативный
``consolidator.estimate_session_prompt_tokens`` (может быть sync или async).
Если он бросил исключение — ``_estimate_fallback`` даёт грубую оценку по
``~4 символа = 1 токен`` из ``session.messages`` (chain-строка помечается
``[fallback]``), чтобы отчёт/логи не писали «0 токенов» при реальном размере
в десятки тысяч. Fallback-значение идёт и в ``tokens_before``/``tokens_after``,
и в заметку ``metadata.compact``.

**Единый путь записи.** Ручной запуск зовёт `compact()`, авто —
`record_external_compaction(...)`. Оба заканчиваются вызовом
`ContextCompactionService._notify(report)`:

```python
async def _notify(self, session_key, report):
    text = self.format_report(report)
    logger.info("Context compaction [{}] {}: archived={}, tokens {}→{}", ...)
    if self.print_to_terminal:
        Console().print(f"[dim]🗜️ {text}[/dim]")
    if self.notify_in_history:
        await self._write_history_notice(session_key, report)
```

`format_report`, loguru-строка и `_write_history_notice` — **общие**
для всех путей. Ручное и автоматическое сжатие неразличимы по
формату и по содержимому в БД/логах/консоли.

**Формат `format_report`** (`ContextCompactionService.format_report`):

Для ``archived > 0``:

```
<текст LLM-сводки (если есть)>

Итог: заархивировано N сообщений (осталось K), BEFORE → AFTER токенов (экономия ≈P%).
```

Для ``archived == 0`` (сжатие не потребовалось):

```
Сжатие сессии «<key>» не потребовалось: контекст уже в пределах бюджета (N токенов).
```

Для ошибки:

```
Сжатие не выполнено: <причина>
```

**Что пишется в `agent_conversation_messages`:**

| Поле | Значение |
|---|---|
| `chat_id` | из `session_key` (`postgres:<chat>`) |
| `role` | `assistant` |
| `status` | `completed` |
| `content` | результат `format_report(report)` (полный текст) |
| `metadata.kind` | `context_compact` (метка для UI/аналитики) |
| `metadata.compact` | весь `report` (archived_msgs, kept_msgs, tokens, summary, mode) |

Поддерживается **только** префикс `postgres:` — единственный канал с
таблицей обмена (Streamlit-UI и его префикс `streamlit:` удалены в фазе 1
миграции `enterprise-mcp-platform`). Для CLI-сессий (`cli:`)
запись пропускается: REPL сам показывает отчёт в терминале, в БД
идти нечему.

**Почему `agent_conversation_messages`, а не `agent_session_messages`:**
контекст промпта строится из `PGSessionManager` (таблица
`agent_session_messages`). Если бы заметка попадала туда — она бы
съедала токены, которые сжатие только что освободило. Заметка
видна в чате, но не загружается в LLM-промпт.

**Конфигурация** (`project.json` → `gateway.compact.*`, все ключи
опциональны, дефолты прямо в коде):

| Ключ | Дефолт | Эффект |
|---|---|---|
| `gateway.compact.enabled` | `true` | Регистрировать tool `compact_context`, slash-команду `/compact` и обрабатывать `/compact` в CLI. При `false` патчи пропускаются, все пути возвращают «отключено». |
| `gateway.compact.notify_in_history` | `true` | Писать заметки в `agent_conversation_messages` (ручные и авто). |
| `gateway.compact.print_to_terminal` | `false` | Дублировать отчёт в Rich-вывод gateway (по образцу `print_worker_activity`). |

На уровне nanobot (`config.json`) поведение сжатия управляется
стандартными ключами `consolidationRatio` (дефолт `0.5`) и
`idleCompactAfterMinutes` (в этом проекте `0` — auto-compact idle
выключен; см. `nanobot/config/schema.py:151-163`). См. также
**[Конфигурация навыка](DATABASE.md#конфигурация-навыка)** и
**[Структура проекта](#структура-проекта)**.

**Защита от `list_sessions`-шторма при выключенном idle-компакте**
(`runtime_patcher.patch_auto_compact_idle_guard`): `AgentLoop.run` при
отсутствии входящих раз в секунду зовёт `AutoCompact.check_expired()`
(`nanobot/agent/loop.py:1034`), а тот даже при `idleCompactAfterMinutes=0`
делает `sessions.list_sessions()` — дорогой N+1 (перечисление всех сессий +
отдельный запрос превью каждой). При сотне сессий это ~150 запросов/сек
вхолостую. Патч при `auto_compact._ttl <= 0` заменяет `check_expired` на
no-op — сбрасывая load практически до нуля (остаётся только легитимный
поллинг каналов). При `ttl > 0` патч пропускается.

**UI:**

* **Streamlit** (`streamlit_app.py`):
  * `_load_chat_history` поднимает флаг `compact_notice=True`, если
    `metadata.kind == "context_compact"`.
  * В рендере (строка ~290): ``<div class="compact-notice">🗜️ {content}</div>``
    через CSS-стиль — жёлтый фон, левая полоска `#f0c040`,
    мелкий шрифт, отступы. Не путается с обычными
    assistant-сообщениями.
* **CLI** (`lib/cli/console_loop.py`): `_run_cli_compact` печатает
  через Rich: `[cyan]🗜️ {text}[/cyan]` при успехе, `[yellow]🗜️ ...`
  при ошибке.
* **Терминал gateway** (`lib/services/context_compaction.py`):
  если `print_to_terminal=true`, Rich-вывод `[dim]🗜️ {text}[/dim]`
  (по образцу `print_worker_activity`).
* **loguru**: всегда пишется INFO-строка вида
  `Context compaction [token] postgres:streamlit: archived=12, tokens 34500→12300`
  (и аналогично для авто — `Auto context compaction [idle] ...`).

**Что не делается (явные «нет»):**

* Не добавляются служебные сообщения в `session.messages` при
  ручном `/compact` — это намеренно: иначе освобождённые токены
  сразу бы съела новая запись в истории. Состояние сессии
  (`last_consolidated`, `_last_summary`) правит штатный
  `Consolidator` nanobot (`memory.py::_persist_last_summary`),
  а наш сервис лишь пишет UI-заметку.
* Не отправляется пользователю уведомление через cron/heartbeat —
  сжатие не требует реакции.
* Не суммируется экономия по сессиям в отдельную таблицу — для
  аудита достаточно `metadata.compact` в `agent_conversation_messages`
  и loguru-логов.

**Безопасность:** вся блокировка уже внутри консолидатора
(`Consolidator.get_lock(session_key)`) — параллельные сжатия одной
сессии serialized. Обёртки `runtime_patcher` не добавляют
синхронизации и не делают двойных вызовов.

**Тесты:** `tests/test_context_compaction.py` (41 тест):

* `TestFormatReport` — формат отчёта: failure, idle-no-archive,
  with-archive+summary, summary-truncation, summary-`nothing`-marker,
  archive-no-summary.
* `TestEstimateFallback` — `_estimate_fallback` по символам, пустые
  сообщения, использование fallback, когда нативный метод падает.
* `TestCompact` — ручной путь: disabled-failure, missing-session-key,
  token-compaction-archives-and-reports, idle-mode, idle-no-archive,
  compactor-failure, session-state-relies-on-nanobot-consolidator,
  no-extra-message-in-session.
* `TestCmdCompact` — `cmd_compact` (slash-команда): возвращает
  `OutboundMessage` с `_final_turn`, всегда `force=True`, парсит `idle`,
  почитает `enabled=false`, прокидывает `settings` через `partial`.
* `TestCompactContextTool` — `CompactContextTool.enabled`/`create`/`execute`
  (стандартный nanobot-паттерн, читает `gateway.compact.*` через
  `ctx._settings_ref`).
* `TestCompactContextToolRegistered` — `patch_project_tools` реально
  регистрирует `compact_context` в `agent.tools`.
* `TestRecordExternalCompaction` — единый путь записи:
  `_write_history_notice` зовётся с правильным report,
  skip при `archived=0`, skip при `notify_in_history=false`.
* `TestPatchCompactionTracking` — `patch_compaction_tracking`:
  skip при `enabled=false` / `notify_in_history=false`,
  archive-wrapper зовёт `record_external_compaction`,
  skip когда авто не архивирует,
  maybe-consolidate-wrapper зовёт `record_external_compaction`.

#### Переопределение шаблонов nanobot: `workspace/overrides/`

`lib/services/consolidator_locale.py` подкладывает каталог `workspace/overrides/`
в Jinja2-loader шаблонов nanobot на старте приложения (`ApplicationContext.start()`),
поэтому любой системный промпт можно переопределить без правки пакета.

**Механизм.** `nanobot.utils.prompt_templates._environment()` кэшируется
`@lru_cache` и возвращает один и тот же `Environment`; `apply_template_overrides()`
меняет у него `loader` на `ChoiceLoader`, который сначала ищет файл в
`workspace/overrides/`, затем в штатных `templates/`. Мутация того же объекта
видна всем `render_template(...)`, патчить функцию не нужно. Идемпотентен;
при отсутствии каталога — no-op (используются штатные шаблоны).

**Правило размещения.** Файлы кладутся под
`workspace/overrides/<имя шаблона, как оно передаётся в render_template>`,
например `workspace/overrides/agent/consolidator_archive.md` переопределяет
`agent/consolidator_archive.md`.

**Сейчас в `workspace/overrides/`:**

* `agent/consolidator_archive.md` — русскоязычная инструкция Consolidator
  (базовая часть шаблона + правило «пиши факты на языке диалога»). Без него
  Consolidator извлекал бы факты на английском даже из русских диалогов.
  Это делает его единственным источником инструкции для
  `Consolidator.compact_idle_session` / `maybe_consolidate_by_tokens`
  (render `nanobot/agent/memory.py` при каждом сжатии).

**Тесты:** `tests/test_consolidator_locale.py` — приоритет override-файла,
fallback на штатный шаблон при отсутствии файла, идемпотентность, no-op при
отсутствии каталога, корректный путь по умолчанию.

#### `metadata` JSONB в `agent_conversation_messages`: полный справочник

`metadata` — JSONB-колонка таблицы обмена. Это **основной канал
передачи сервисных данных от бэкенда к UI** (помимо `content`,
`media`, `buttons`, `reply_to`). Любой UI, читающий
`agent_conversation_messages`, может отличить служебные записи
от обычных диалоговых по `metadata.kind`.

DDL: `metadata JSONB DEFAULT '{}'::jsonb` (см.
`sql/channels/create_public_agent_conversation_messages.sql`).
Чтение — `_decode_jsonb(metadata)` (из `utils.jsonb`).

##### 1. Жизненный цикл `metadata` одной строки

Одна строка проходит несколько фаз, в каждой из которых
`metadata` дополняется:

```mermaid
flowchart TD
    A["INSERT — pending<br/>metadata = {}"] --> B["processing<br/>+ message_id, reasoning, context_window"]
    B --> C["completed<br/>+ _tool_audit"]
    C --> D["error / failed<br/>+ error, retry_count++"]
    classDef core fill:#fff3cd,stroke:#d39e00,stroke-width:2px
    class A,B,C,D core
```

То есть в `metadata` **накапливаются** ключи от разных подсистем;
UI читает то, что есть, и не обязан понимать каждый ключ.

##### 2. Все ключи `metadata` — таблица

| Ключ | Тип значения | Где создаётся | Где обновляется | Удаляется? | Назначение |
|---|---|---|---|---|---|
| `kind` | `str` | `ContextCompactionService._write_history_notice` | — | никогда | Маркер «служебная заметка». Сейчас единственное значение — `"context_compact"`. Используется UI для условного рендера. |
| `compact` | `dict` | `ContextCompactionService._write_history_notice` | — | никогда | Словарь со статистикой сжатия. Только при `kind == "context_compact"`. См. подсекцию ниже. |
| `message_id` | `str` (UUID) | `PostgresChannel._poll_once` (в `meta` для assistant-placeholder) | — | никогда | ID user-сообщения, на которое это assistant-сообщение — ответ. Пара `message_id ↔ answer_id` (взаимные ссылки в соседних строках). |
| `answer_id` | `str` (UUID) | `PostgresChannel._poll_once` (в `meta` для user-строки) | — | никогда | ID assistant-placeholder, созданного сразу при клейме. Позволяет каналу находить строку для обновления. |
| `session_key` | `str` | Передаётся в `raw_meta` (UI/внешний клиент) | — | никогда | Полный ключ сессии nanobot, формат `<channel>:<chat_id>`. Если не передан в raw_meta, канал подставляет `f"postgres:{chat_id}"` (см. `postgres_channel.py:718`). |
| `retry_count` | `int` | `PostgresChannel._reclaim_and_heal` / `_mark_failed` | инкрементируется при каждом `error`/`stuck` | никогда | Сколько раз задача была в `error`. При `>= max_stuck_retries` → `failed`. |
| `error` | `str` | `PostgresChannel._mark_failed` | — | никогда | Только в строках со статусом `error` или `failed`. Краткое описание причины: `"dispatch_error"`, `"write_error"`. |
| `reasoning` | `str` | `PostgresChannel._flush_reasoning` (live) + `_finalize_turn` (atomic append) | дописывается через `_reasoning_io_lock` | никогда | Полный текст рассуждений модели (chain-of-thought). Может быть очень длинным. |
| `context_window` | `dict` | `PostgresChannel._flush_live_context` (live) | перезаписывается каждые `_flush_interval` сек | никогда | Метрика занятости контекстного окна: `{used: int, limit: int, pct: float (0..1, 4 знака), model: str}`. См. подсекцию «Метрика занятости контекстного окна» выше. |
| `_tool_audit` | `list[dict]` | `RuntimePatcher.patch_assemble_outbound` (финальный outbound) | — | никогда | Массив записей вызовов инструментов за оборот. Рендерится в UI (Streamlit) и CLI. |

**Не пишется в `metadata`:** `_final_turn` (флаг протокола канала,
в БД не пишется как значимое поле), `latency_ms` (для логов).

##### 3. `raw_meta` от UI (что кладёт источник)

Когда внешний клиент (REST, Telegram-бот) пишет
**новое** user-сообщение, он может положить любые поля в `metadata`
INSERT-а. Канал их читает и мерджит с собственными ключами:

```python
# postgres_channel.py:711-716
raw_meta = _decode_jsonb(row["metadata"])
# ...
meta: dict[str, Any] = {
    "message_id": user_msg_id,
    "answer_id": assistant_msg_id,
    **raw_meta,
}
```

**Конвенция** для `raw_meta` от UI: только поля, описывающие
маршрутизацию. Сейчас в проекте используется `session_key`
(потенциально; INSERT от UI не пишет `metadata` — DEFAULT `'{}'`).
Любые `kind`/`compact`/`reasoning`/`context_window` от UI
**игнорируются** (будут перезаписаны каналом/патчами на следующих
стадиях жизненного цикла).

Пример валидного `raw_meta` для внешнего клиента:
```json
{"session_key": "telegram:123456", "client_meta": {"thread": "..."}}
```

##### 4. Кто пишет и когда

| Источник | Файл | Когда | Что пишет в `metadata` |
|---|---|---|---|
| Внешний UI (INSERT) | web-клиент при отправке user-сообщения | при INSERT | `raw_meta` (опционально, `session_key` и прочее) |
| `PostgresChannel._poll_once` | `lib/channels/postgres_channel.py:758` | при клейме задачи | `message_id` (в assistant-строке), `answer_id` (в user-строке) |
| `PostgresChannel._flush_reasoning` | `postgres_channel.py:530` | live, каждые `_flush_interval` сек | `reasoning` (дописывается) |
| `PostgresChannel._finalize_turn` | `postgres_channel.py:1156` | на `_turn_end` | `reasoning` (atomic append остатков) |
| `PostgresChannel._flush_live_context` | `postgres_channel.py:560-570` | live, каждые `_flush_interval` сек | `context_window` (перезаписывается) |
| `PostgresChannel._reclaim_and_heal` | `postgres_channel.py:347-348` | lease-loop, истёк lease | `retry_count++` (затем `status='error'` или `'failed'`) |
| `PostgresChannel._mark_failed` | `postgres_channel.py:835-836` | ошибка диспетчера/записи | `retry_count++`, `error=<reason>` |
| `RuntimePatcher.patch_assemble_outbound` | `lib/services/runtime_patcher.py:730, 722, 96` | на финальном outbound | `_tool_audit` (если есть), `_final_turn: true` (внутренний протокол), `context_window` (если есть) |
| `ContextCompactionService._write_history_notice` | `lib/services/context_compaction.py:332` | после успешного сжатия (ручного или авто) | `kind: "context_compact"`, `compact: {…}` |

##### 5. Маркер `metadata.kind` — версионирование служебных заметок

`metadata.kind` — **точка расширения**: если в будущем добавятся
другие типы служебных заметок (например, `metrics_summary`,
`compaction_failure`, `tool_error_highlight`), они идут по тому же
контракту:

```json
{"kind": "<type>", "<type>": {<payload>}, "...": "..."}
```

**Конвенция:**

* `kind` — короткая строка-идентификатор, `lower_snake_case`.
* `<type>` (то же имя в `payload` ключе) — основной машино-читаемый
  блок.
* `content` строки — уже отформатированный человеком текст для
  показа. UI может рендерить **только** `content` без чтения payload.
* Для UI-клиента: `if metadata.get("kind") == "X": render_special()`
  иначе обычное сообщение.

**Версионирование:** при изменении формата payload — менять `kind`
на `<type>_v2`. Старые записи остаются с `kind = "<type>"` для
обратной совместимости. UI читает обе версии.

**Сейчас единственное значение `kind` — `"context_compact"`.**

##### 6. `metadata.compact` — payload для `kind == "context_compact"`

Только при `kind == "context_compact"`. Записывается
`ContextCompactionService._write_history_notice` после успешного
ручного или автоматического сжатия.

| Поле | Тип | Описание |
|---|---|---|
| `session_key` | `str` | Полный ключ сессии (например, `postgres:chat-42`) |
| `mode` | `"token"` \| `"idle"` | Режим: token-budget (`maybe_consolidate_by_tokens`) или idle (`compact_idle_session`) |
| `ok` | `bool` | `true` если сжатие успешно |
| `archived_msgs` | `int` | Сколько сообщений заархивировано (≥ 1 для записи) |
| `kept_msgs` | `int` | Сколько сообщений осталось в сессии |
| `tokens_before` | `int` | Размер промпта до сжатия (estimated) |
| `tokens_after` | `int` | Размер промпта после сжатия (estimated) |
| `summary` | `str \| null` | Текст LLM-сводки (если был; при `raw_dump=true` — отсутствует) |
| `raw_dump` | `bool` | `true` если LLM-саммарайзер упал, сделана raw-выгрузка без сводки |

**Правила:**

* `archived_msgs > 0` — иначе заметка **не пишется** (нет смысла).
* `metadata.compact.session_key` есть, даже если в `content` не упомянут.
* Для UI-клиента: `pct = (1 - compact.tokens_after / compact.tokens_before) * 100`
  — процент экономии.

##### 7. Примеры UI-логики

Внешний web-клиент — три места с условным рендером
(в агент UI-кода нет; показываю как контракт `metadata`):

```python
# _load_chat_history (строки 89, 96, 100)
if metadata.get("kind") == "context_compact":
    msg_entry["compact_notice"] = True
if isinstance(metadata.get("context_window"), dict):
    msg_entry["context_window"] = metadata["context_window"]
if role == "assistant" and metadata.get("reasoning"):
    msg_entry["reasoning"] = metadata["reasoning"]

# _check_response (строка 157-159) — для одного ответа
metadata = _decode_jsonb(row["metadata"])
result = {"content": row["content"] or "", "metadata": metadata, "media": media}

# _get_processing_state (строки 175-177) — для live-обновления
meta = _decode_jsonb(row["metadata"])
return {"content": ..., "reasoning": meta.get("reasoning", "")}
```

UI читает **только** то, что знает (`reasoning`, `context_window`,
`kind`/`compact_notice`). Не знакомые ключи просто игнорируются.

**React/Telegram-бот — минимальный рендер `kind`:**

```tsx
{msg.metadata?.kind === "context_compact" ? (
  <div className="compact-notice">
    <span>🗜️</span>
    <span>{msg.content}</span>
    {msg.metadata.compact.archived_msgs > 0 && (
      <span className="badge">
        -{Math.round(
          (1 - msg.metadata.compact.tokens_after /
               msg.metadata.compact.tokens_before) * 100
        )}%
      </span>
    )}
  </div>
) : (
  <div className="message">{msg.content}</div>
)}
```

```python
# Telegram-бот
if row["metadata"].get("kind") == "context_compact":
    compact = row["metadata"].get("compact", {})
    pct = (1 - compact.get("tokens_after", 0) /
                max(compact.get("tokens_before", 1), 1)) * 100
    await bot.send_message(
        chat_id,
        f"🗜️ {row['content']}\n\n_экономия ≈{pct:.0f}%_",
        parse_mode="Markdown",
    )
else:
    await bot.send_message(chat_id, row["content"])
```

**Стиль UI** для `compact_notice` (реализация была в Streamlit, удалён
вместе с ним; контракт `metadata` сохранён):

* CSS-класс `.compact-notice` — жёлтый фон `#fff8e1`,
  левая полоска `#f0c040`, мелкий шрифт, скругления. Определён в
  `<style>` блоке.
* В `_load_chat_history` поднимается флаг `compact_notice=True`.
* В цикле рендера — отдельный HTML-блок
  `<div class="compact-notice">🗜️ {content}</div>` через
  `st.markdown(..., unsafe_allow_html=True)`. Без акцента текст
  заметки читаем, но визуально сливается с обычными ответами.

##### 8. Что НЕ делается (явные «нет»)

* **Нет глобальной схемы** — `metadata` это JSONB, схема **описывается
  в коде и в этой документации**, а не в DDL. Дрейф возможен —
  при появлении нового ключа обновляйте эту секцию.
* **Нет валидации** на стороне записи — UI может положить любые
  поля в `raw_meta` (см. §3), но канал/патчи перезапишут конфликтующие
  ключи своими значениями.
* **`_final_turn` в БД не сохраняется** — это флаг протокола
  (`OutboundMessage.metadata["_final_turn"] = True`), используется
  каналом для решения «merge vs finalize». При записи в БД он
  не несёт полезной нагрузки и не документируется как часть
  контракта.
* **Заметка `kind == "context_compact"` не сортируется отдельно** от
  обычных сообщений — она появляется в `created_at` хронологии,
  как любой `assistant`-ответ. Если UI хочет группировать
  «последний сжатый ответ отдельно» — это на стороне UI.
* **Заметка `kind == "context_compact"` не фильтруется по `role`**
  — она `assistant`, как обычные ответы. UI, которые ограничивают
  `role IN ('user', 'assistant')`, покажут её автоматически.

##### 9. Совместимость и обратная совместимость

* Если `metadata` не содержит ключа, который UI ожидает — UI должен
  обрабатывать отсутствие (`metadata.get("X")` / `?.` / `metadata?.X`).
* `metadata.reasoning` может быть очень длинным — UI может рендерить
  свёрнутым `<details>` (так делал Streamlit UI).
* `metadata.context_window.pct` уже clamp 0..1, 4 знака — можно
  умножать на 100 сразу, не нормализуя.
* `metadata.compact.tokens_before/after` — `0` допустимо (если
  замер не удался, `_estimate` вернул `(0, "")`); UI должен делить
  осторожно (`max(compact.tokens_before, 1)` для pct).
* Старые строки в `agent_conversation_messages` без `metadata.kind`
  — это нормально, читается как `null` → рендер как обычное сообщение.

##### 10. Тесты

* `tests/test_postgres_channel.py::TestPostgresChannelReasoning` —
  `reasoning` live + atomic append в `_finalize_turn`.
* `tests/test_postgres_channel.py::TestPostgresChannelContextWindow` —
  `context_window` live-update + drop.
* `tests/test_console_loop.py::TestPrintContextWindow` —
  CLI-рендер `context_window`.
* `tests/test_context_compaction.py` — запись `kind == "context_compact"`,
  `compact`-payload, skip-правила.
* `tests/test_runtime_patcher.py::TestPatchAssembleOutbound` —
  внедрение `_tool_audit` в `metadata`.

### Ликвидация потери данных при усечении больших результатов инструментов

**Проблема.** Результаты больших инструментов терялись на нескольких уровнях:

1. **exec/shell**: nanobot режет вывод команды до
   `MAX_OUTPUT_CHARS = 50K` символов и вставляет маркер
   `... (19,761 chars truncated) ...` (`nanobot/agent/tools/shell.py`,
   `exec_session.py`), отбрасывая середину. Persist потом сохраняет
   «голову+хвост» — данные теряются безвозвратно.
2. **История сессии**: `AgentLoop._save_turn` усекает строковый результат
   инструмента до `max_tool_result_chars = 16K` символов, если результат не
   ушёл в persist раньше (в первую очередь это `read_file`).
3. **Вторичные инструменты** (`read_file`/`grep`/`list_dir`) имеют свои
   потолки с маркерами `truncated`.

**Решение (все уровни закрыты патчами `RuntimePatcher`):**

| Патч | Что делает |
|------|-----------|
| `patch_exec_limits` | Поднимает `MAX_OUTPUT_CHARS` (дефолт 500K), `DEFAULT_MAX_OUTPUT_CHARS` (100K), `ExecTool._MAX_OUTPUT`, а также подменяет `maximum` в JSON-Schema параметра `max_output_chars`/`max_output_tokens` (модель может запросить вывод >50K). Безопасно для контекста: вывод exec не exempt в persist → >`persist_threshold` уходит полным файлом в `data_store`, в контекст — ссылка. |
| `patch_tool_limits` | Поднимает `ReadFileTool._MAX_CHARS` (512K), `search._DEFAULT_HEAD_LIMIT` / `_DEFAULT_FILE_HEAD_LIMIT` (500/400), `GrepTool._MAX_FILE_BYTES` (20MB), `ListDirTool._DEFAULT_MAX` (500). |
| `patch_save_turn` | Оборачивает `AgentLoop._save_turn`: любой большой результат `role == "tool"` (строка или JSON-сериализуемый список) пишется **полным** файлом в `data_store` через `SessionFileStore` (суффикс `__<hash>` — dedupe), в историю кладётся ссылка `[Result saved to data_store/<path> (<size> KB)]` — тот же формат, что кастомный persist. Оригинальный `_save_turn` вызывается с копией сообщений, логика nanobot не дублируется. |
| `save(..., dedupe=True)` | Новый параметр `SessionFileStore.save`: повторное сохранение того же содержимого (sha1, первые 12 hex) возвращает уже существующий файл (`deduped=True`), чтобы повторные/конкурентные обороты не плодили копии. |

**Конфигурация** — `gateway.tool_result_limits` в `project.json`
(все ключи опциональны, дефолты в коде):

```jsonc
"tool_result_limits": {
  "exec_max_output_chars": 500000,
  "exec_default_output_chars": 100000,
  "read_file_max_chars": 512000,
  "grep_head_limit": 500,
  "grep_file_head_limit": 400,
  "grep_max_file_bytes": 20000000,
  "list_dir_max_entries": 500
}
```

**Fallback-поведение**: каждый патч в try/except; при изменении API nanobot —
`(False, <причина>)` в `PatchReport`, процесс не падает. `_save_turn`-обёртка
при `OSError` в persist грейсит — вызывает оригинал (поведение «как раньше»).
`read_file` остаётся exempt в `normalize_tool_result` (избегаем
read→persist→read петли).

**Реальные проверки** — `tests/test_runtime_patcher_e2e.py` (9 тестов):
настоящий `ExecTool` исполняет команду с выводом >лимита и проверяет
отсутствие маркера `chars truncated` после патча; настоящий `ReadFileTool`
читает файл >дефолтного потолка целиком; `_save_turn`-обёртка и
`ContextGovernor.normalize_tool_result` пишут **полные** файлы на диск в
`data_store/` и подменяют историю ссылкой. Каждый тест сам восстанавливает
состояние фреймворка в `finally`.

---

## Сервисный слой (MessageExchange + LLM-клиент + утилиты)

Этот раздел добавляет поверх сервисного слоя набор общих модулей, чтобы
устранить дрейф поведения между каналами, навыками и CLI-инструментами.

### `lib/channels/message_exchange.py` — общий движок каналов

`MessageExchange` — единая точка кодирования/декодирования `InboundMessage` /
`OutboundMessage`, поллинга и публикации outbound, фильтрации служебных
сообщений. `PostgresChannel` и `RedisChannel` — тонкие обёртки над ним. Запрещено
дублировать логику polling/encoding в новых каналах — только через
`MessageExchange`.

Зависимости модуля:
- `workspace/utils/media.py` — кодек media (AW-формат `{filename, file_id, mime_type,
  file_size}` + обратная совместимость со старым `{filename, data}` и
  data-URL).
- `workspace/utils/jsonb.py` — JSONB-декодер media для PG.
- `lib/utils/outbound_meta.py` — единый фильтр служебных outbound
  (`system`, `audit`, `tool_audit`, `_assemble_outbound`-артефакты).
- `SessionFileStore` (`workspace/utils/session_file_store.py`) — общий стор
  вложений под `data_store/cache/sessions/<key>/attachments/`.

При добавлении нового канала: наследовать `nanobot.channels.base.BaseChannel`
и делегировать `start/stop/send/send_delta/poll_once` в `MessageExchange`.
Подробнее — [`../lib/channels/README.md`](../lib/channels/README.md).

### Захват задач и статусы (`agent_conversation_messages`)

Захват задачи (сообщения веб-чата) делает **один** `UPDATE ... RETURNING`
в `_claim_one` (`lib/channels/postgres_channel.py`):

```sql
UPDATE agent_conversation_messages
   SET status = 'processing', updated_at = NOW()
 WHERE id = (SELECT id FROM agent_conversation_messages
              WHERE role = 'user' AND (status = 'pending' OR (status = 'error' AND ...))
                AND status != 'cancelled'
                AND NOT EXISTS (SELECT 1 ... m2.status = 'processing' в том же chat_id)
              ORDER BY created_at ASC LIMIT 1)
   AND status = 'pending' AND status != 'cancelled'
RETURNING id, chat_id, user_id, content, media, metadata, created_at
```

**Инвариант:** захват эксклюзивен, потому что внешний `AND status = 'pending'`
делает повторный UPDATE нерабочим. Если задачу уже взял другой захват, её
статус не `pending`, UPDATE не срабатывает, вторая обработка невозможна.
Это MVCC-перепроверка UPDATE, а не UNIQUE-индекс.

Таблицы аренды `public.agent_worker_claims` **нет** — она удалена миграцией
`sql/migrations/V006__drop_agent_worker_claims.sql`, вместе с настройками
`claims_table`, `claim_strategy`, `lease_interval`. Протокол lease/heartbeat
(`_lease_loop`, `_reclaim_needed`, `_reclaim_and_heal`, `_delete_claim`) удалён
из канала. Состояние захвата хранится в самой строке задачи.

**Почему не `FOR UPDATE SKIP LOCKED`.** План перехода предполагал заменить опрос
на `SELECT ... FOR UPDATE SKIP LOCKED`. Это сознательно не сделано: проект
разворачивается на Greenplum 6.5 (ядро PostgreSQL 9.4 — см. `sql/README.md`),
где `SKIP LOCKED` (появился в PostgreSQL 9.5) недоступен, а Greenplum при
`SELECT ... FOR UPDATE` берёт блокировку уровня **таблицы** — такой захват
заблокировал бы всех читателей и писателей `agent_conversation_messages`.
Корректности `SKIP LOCKED` здесь и не нужен: он даёт только снижение задержки
при конкурентных захватах, а эксклюзивность обеспечивает `AND status='pending'`.

**Статусы задач:**

| Статус | Значение | Повторяется? |
|---|---|---|
| `pending` | готова к обработке | да, сразу |
| `processing` | в работе (задачу взял инстанс) | нет |
| `error` | повторяемая ошибка | заявлен повтор после `error_retry_delay` |
| `failed` | терминальный (не меняется) | нет — окончательно |
| `completed` | успешно завершена | — |

`error` и `failed` разведены: `error` (retry-каунтер в `metadata.retry_count`
не исчерпан) должен вернуться в пул после паузы; `failed` — immutable-терминал.

> **Известный дефект.** Ветка повтора `error` сейчас недостижима: внешний
> `AND status = 'pending'` отсекает строку, выбранную подзапросом по
> `status = 'error'`. `_mark_failed` переводит задачу в `error` с обещанием
> вернуть её в пул, но повторного захвата не происходит — задача остаётся в
> `error` навсегда. Настройка `error_retry_delay` сохранена как контракт,
> но механизма за ней сейчас нет. Требует отдельного решения (см. CHANGELOG).

**Возврат зависших задач в пул.** Единственный механизм — фоновая
`_unstick_loop` с интервалом `unstick_interval` (по умолчанию
`max(60, processing_timeout/5)` = 120 сек). `_unstick_processing` возвращает
`processing`-строки с `updated_at` старше `processing_timeout` в `pending`
(или в `failed`, если исчерпан `max_stuck_retries`) и чистит assistant-placeholder.
Отдельный таймер вместо вызова на каждом poll-тике — чтобы не делать
`SELECT`+`UPDATE` каждые `poll_interval` на пустом столе. При `stop()`
незавершённые задачи возвращает `_return_claimed_to_pool`.

**Что это значит по мульти-машинности.** Несколько инстансов gateway на общей
таблице по-прежнему не схлопываются в двойную обработку (это гарантирует
MVCC-перепроверка UPDATE), но HA-механизма больше нет: упавший инстанс не
отдаёт задачу, пока её не вернёт `_unstick_loop` соседнего. Отказоустойчивость
уровня HA сознательно потеряна.

**Промежуточные публикации тула `message(...)` vs финал оборота.**
`MessageTool` в nanobot публикует свой outbound через шину **в момент
исполнения тула**, т.е. до завершения оборота. `send()` финализирует оборот
(слот + `_msg_ctx`) **только** на маркере `metadata["_final_turn"]`
(или legacy `_turn_end` / `latency_ms`), который ставит патч
`RuntimePatcher.patch_assemble_outbound` (при подавленном финале — синтетическим
outbound). Все остальные сообщения `send()` merge'ит в assistant-строку
(`_merge_tool_delivery`): накопление `content` + media без дублей, status
остаётся `processing`, слот не трогается. Это исключает ситуацию, когда оборот
«завершался» на промежуточной публикации. Маркер объявлен в
`lib/utils/outbound_meta.py` как `FINAL_TURN_KEY` (не входит в
`OUTBOUND_DROPPED_KEYS`, чтобы потоковые каналы вроде Redis передавали
финальный ответ как обычно). `_release_slot` в финализации вызывается после
успешной записи, а не до неё.

**Конфиг (`channels.postgres`):** `table_name` (таблица канала, дефолт
`agent_conversation_messages`), `messages_table` / `meta_table` (таблицы
сессий), `poll_interval`, `flush_interval`, `max_concurrent`,
`processing_timeout`, `unstick_interval`, `max_stuck_retries`,
`error_retry_delay`, `worker_id` (пусто → авто `{hostname}:{pid}:{rand8}`;
участвует только в логах и выводе активности).

**Поток данных:**

1. `PostgresChannel._poll_once()` → `_claim_one()` — один
   `UPDATE ... RETURNING` через `fetchone`.
2. После обработки `_finalize_turn()` → `UPDATE SET status='completed'`,
   снятие слота и `_claimed_ids`.
3. Раз в `unstick_interval` сек `_unstick_loop` → `_unstick_processing()` —
   откат зависших `processing` (retry/failed счётчик в `metadata`).

### Priority polling path (для priority-команд nanobot)

**Задача.** Slash-команды, зарегистрированные как priority в
`nanobot.command.router.CommandRouter` (`/stop`, `/restart`, `/status`),
должны доходить до AgentLoop даже когда все обычные слоты заняты
активной задачей той же сессии — иначе пользователь не может прервать
долгий turn.

**Решение.** `MessageExchange._poll_loop` сначала вызывает опциональный
хук канала `poll_priority_inbound`, и только если тот вернул `False`
(нет priority-кандидатов) — переходит к обычному `poll_inbound` с
проверкой `is_slot_free()`. Канал (PostgresChannel) сам решает, какие
сообщения считать priority-кандидатами. Список priority-команд
читается через `lib.channels.priority_commands.get_priority_commands()`
(из `CommandRouter._priority` с fallback на захардкоженный
`_DEFAULT_PRIORITY_COMMANDS`). Claim фильтрует через
`AND content = ANY(%s)` — параметризованный список, не хардкод.

**Решение.** `MessageExchange._poll_loop` сначала вызывает опциональный
хук канала `poll_priority_inbound`, и только если тот вернул `False`
(нет priority-кандидатов) — переходит к обычному `poll_inbound` с
проверкой `is_slot_free()`. Канал (PostgresChannel) сам решает, какие
сообщения считать priority-кандидатами (для транспорта через таблицу
`agent_conversation_messages` это `content = '/stop'`), и реализует
`poll_priority_inbound` через параметризованный claim:

```
MessageExchange._poll_loop:
    poll_priority = getattr(channel, "poll_priority_inbound", None)
    while running:
        if poll_priority:
            handled = await poll_priority(exchange)
            if handled:
                await asyncio.sleep(0)   # yield, не busy-loop
                continue
        if exchange.is_slot_free():
            handled = await channel.poll_inbound(exchange)
            ...
```

**Граница ответственности:**

- `MessageExchange` знает только: «есть priority inbound path».
- `PostgresChannel.poll_priority_inbound` знает: как искать priority
  кандидатов в БД (через `_claim_one(priority_contents=...)`,
  где `priority_contents` — список всех priority-команд из
  `nanobot.command.router.CommandRouter`).
- `nanobot.command.router.CommandRouter.is_priority` и
  `nanobot.command.builtin.cmd_stop` знают: что делать с командой
  после её доставки через `bus.publish_inbound` → `AgentLoop.run()`.

Это сохраняет архитектурную границу: `MessageExchange` не знает
конкретных команд, канал не знает как они обрабатываются, библиотека
nanobot не знает откуда они пришли.

**Priority polling — независим от обычных слотов:**

- **Не вызывает `exchange.acquire_slot()`** — обычный semaphore не
  затрагивается; priority-команды не считаются «занятыми слотами».
- **Не вызывает `exchange.add_inflight()`** — `_inflight` остаётся
  нетронутым (для других каналов это индикатор занятости).
- **Не добавляет в `_chat_inflight`** — даже если chat активен
  обычной задачей, priority команда всё равно проходит (это та самая
  задача, которую нужно прервать).
- **Не создаёт assistant-placeholder** — priority команда не
  возвращает контент пользователю (это управляющая команда).
- **Не вызывает `_release_slot`** — slot не занимался.

После `_handle_message` priority path освобождает локальные ресурсы:
`_claimed_ids.discard`, `_msg_ctx.pop`, `_msg_chat.pop`.
Сама отмена активной задачи происходит **внутри** AgentLoop через
библиотечный `cmd_stop` → `_cancel_active_tasks(effective_key)` —
priority polling доставляет `/stop` в шину, дальше работает
стандартный механизм nanobot.

**Двойная защита — DB safety net.** Помимо priority polling path,
`_claim_one` содержит фильтр
`AND status != 'cancelled'` в WHERE (2 места — основной WHERE и
подзапрос по соседним задачам). Если пользователь
помечает сообщение как `cancelled` ДО того, как polling его
захватил — polling его пропускает (race-free по `UPDATE ... WHERE
id=(...)`). После claim — повторный `fetchval` re-check; если
между SELECT подзапроса и UPDATE захвата AW пометил `cancelled`,
polling не диспатчит и освобождает claim. В `_finalize_turn` —
ещё один re-check: если user стал cancelled пока LLM работала,
финальный ответ не публикуется, освобождаются слот и контекст.

**Сценарии:**

| Состояние | Поведение |
|---|---|
| `max_concurrent=1`, A работает, A `/stop` | priority polling доставляет `/stop` → `cmd_stop` отменяет A **до** завершения LLM |
| `max_concurrent=2`, A+B работают, A `/stop` | priority polling доставляет `/stop` → отменяется A, B продолжает |
| A–J работают, F `/stop` | priority polling доставляет `/stop` → отменяется только F, остальные 9 не задеты |
| row cancelled до claim | polling skip через `AND status != 'cancelled'` |
| row cancelled после claim (race) | re-check fetchval → drop + cleanup |
| row cancelled во время LLM | `_finalize_turn` drop response, slot released |

Тесты: `tests/test_user_stop_signal.py` (DB safety net + race checks),
`tests/test_user_stop_signal_priority.py` (priority polling path +
структурные проверки `_poll_loop`).

**Модель захвата — одна.** Таблицы аренды `agent_worker_claims` больше нет
(удалена миграцией `sql/migrations/V006__drop_agent_worker_claims.sql`),
протокол lease/heartbeat/reclaim снят из канала. Захват задачи — один
`UPDATE ... RETURNING` в `_claim_one`: состояние захвата хранится в самой строке
задачи (`status='processing'`). Эксклюзивность обеспечивает внешний
`AND status = 'pending'` — если задачу уже взял другой захват, повторный UPDATE
не срабатывает, двойная обработка невозможна.

**Что это значит по мульти-машинности.** Несколько инстансов gateway на общей
таблице по-прежнему не схлопываются в двойную обработку (это гарантирует
MVCC-перепроверка UPDATE), но HA-механизма больше нет: упавший инстанс не
отдаёт задачу, пока её не вернёт `_unstick_loop`. Отказоустойчивость уровня HA
сознательно потеряна.

**Почему не `FOR UPDATE SKIP LOCKED`.** План перехода предполагал заменить опрос
на `SELECT ... FOR UPDATE SKIP LOCKED`. Это не сделано: проект разворачивается на
Greenplum 6.5 (ядро PostgreSQL 9.4, см. `sql/README.md`), где `SKIP LOCKED`
(появился в PostgreSQL 9.5) недоступен, а Greenplum при `SELECT ... FOR UPDATE`
берёт блокировку уровня **таблицы** — такой захват заблокировал бы всех
читателей и писателей `agent_conversation_messages`. Корректности `SKIP LOCKED`
здесь и не нужен: он даёт только снижение задержки при конкурентных захватах,
а эксклюзивность обеспечивает `AND status = 'pending'`.

**Диагностика.** Ключевой гейт — `tests/test_postgres_channel.py`
(lifecycle-инвариант) и `tests/test_single_mode_audit.py` (runtime-перехват SQL:
в single-режиме не выполняется ни одного обращения к таблице аренды).

#### Воркеры не берут задачи — «зависшая» `processing`-задача блокирует чат

**Симптом.** `очередь: pending=1, pending=2, …` растёт, но в логе нет строки
`→ worker … взял задачу`, и задачи не уходят из `pending`.

**Первопричина.** `_claim_one` выбирает кандидата только из чата БЕЗ активной
`user`-задачи в статусе `processing` (условие `NOT EXISTS (... m2.status='processing'
в том же chat_id)`, `postgres_channel.py`). Если в чате застряла хоть одна задача в
`processing`, **весь чат блокируется** — все его новые `pending`-сообщения не берутся.

**Почему задача зависает в `processing`:**
- воркер (gateway-процесс) был убит жёстко (`Ctrl+Break`, `kill -9`, отвал хоста),
  не успев `_return_claimed_to_pool`/`_finalize_turn` — задача висит `processing`,
  пока её не вернёт в пул `_unstick_loop` (интервал `unstick_interval`,
  по умолчанию `max(60, processing_timeout/5)` = 120 сек);
- shortcut slash-команда (например, `/compact`), которая **минует**
  `_assemble_outbound`, не кладётся `_final_turn` и не финализируется →
  `send()` merge'ит ответ, `status` остаётся `processing`, задача не финализируется.

**Кто вернёт задачу в пул.** Таблицы аренды больше нет, поэтому и диагностировать
нечего: единственный механизм — `_unstick_loop` с интервалом
`unstick_interval` (по умолчанию 120 сек), который возвращает `processing`-строки
с `updated_at` старше `processing_timeout` в `pending` (или в `failed`, если исчерпан
`max_stuck_retries`). Пока цикл не прошёл, задача остаётся `processing` — это
ожидаемое окно, а не признак поломки.

**Найти заблокированный чат (read-only):**
```sql
-- какие user-задачи висят в processing и как долго
SELECT id, chat_id, status, updated_at, NOW() - updated_at AS age
FROM public.agent_conversation_messages
WHERE role='user' AND status='processing'
ORDER BY updated_at ASC;
```
Строка с `age` больше `processing_timeout` — её вернёт следующий тик `_unstick_loop`.

**Разблокировать сейчас (не дожидаясь цикла):**
```sql
-- вернуть задачу в пул, чтобы её репроцессил живой воркер
UPDATE public.agent_conversation_messages
SET status='pending', updated_at=NOW() WHERE id = '<task_id>';
```
После этого живой воркер возьмёт задачу в течение `poll_interval`, и чат разблокируется.

**Профилактика (правило `_final_turn`).** Любой обработчик, возвращающий финальный
`OutboundMessage` в обход `_assemble_outbound` (shortcut-команда, синтетический финал),
обязан ставить в `metadata` `FINAL_TURN_KEY="_final_turn"` (из `lib/utils/outbound_meta.py`).
Иначе `postgres_channel.send()` трактует ответ как промежуточную публикацию и НЕ
финализирует оборот → `status='completed'` не ставится, слот не освобождается,
чат блокируется. Пример корректного паттерна — обработчик compact-команды
(`RuntimePatcher.patch_compact_command`, ставит `_final_turn` во все свои
`OutboundMessage`).

### Lifecycle-инвариант оборота (PostgresChannel)

Жизненный цикл одной задачи в `PostgresChannel` от claim до terminal status
описан инвариантом:

> **Каждая захваченная задача имеет ровно один завершённый lifecycle:
> `claim → processing → terminal status → release local state`.**

После ответа агента должны быть закрыты:

* user-сообщение: `processing → completed | error | failed`;
* assistant-сообщение: `processing → completed` (или удалено в `error`/`failed`);
* `_msg_ctx`, `_msg_chat`, `_chat_inflight`, `_claimed_ids`, `exchange._inflight`
  — все пусты для этого `user_msg_id`/`chat_id`.

Ключевые инварианты реализации:

1. **DB-first порядок в `_finalize_turn`** (`postgres_channel.py:_finalize_turn`).
   Сначала выполняется транзакция (UPDATE assistant → UPDATE user), и
   только после успешного commit снимаются
   `_msg_ctx`, `_claimed_ids`, `_release_slot`. Раньше `pop`/`release` шли
   до транзакции, и при ошибке БД локальное состояние рассинхронизировалось
   с БД.
2. **Единый резолвер `_resolve_turn_context`**. Один источник истины для `user_msg_id` /
   `assistant_msg_id` / `chat_id`:
   `origin_message_id` → `message_id` → `_msg_ctx` →
   `answer_id → SELECT assistant.reply_to`. Заменяет разбросанные fallback'ы.
3. **`send_delta(stream_end=True)` делегирует в `_finalize_turn`**.
   Синтетический `OutboundMessage` с накопленным буфером пробрасывается
   в общий финализатор. `stream_end` всегда завершает lifecycle, даже
   при пустом `delta` (раньше при пустом буфере DB-финализация
   пропускалась из-за `if content and assistant_msg_id:`, и задача
   оставалась в `processing` → polling зависал).
4. **Детерминированный failed при нерезолвенном контексте
   (`_cleanup_unresolvable_turn`)** — если outbound с `_final_turn`
   не содержит ни одного id и `_msg_ctx` пуст, канал маркирует задачу
   failed и снимает локальные хвосты. Раньше такие аномалии делали
   silent no-op, оставляя `_inflight` занятым.
5. **`_unstick_processing` возвращает список восстановленных `user_msg_id`** —
   `_unstick_loop` чистит локальное состояние для каждого.
   Раньше восстановление БД не синхронизировалось с `_inflight`,
   и воркер продолжал считать слот занятым.
6. **Lifecycle-логи `_lifecycle_log(phase, ...)`** — каждая фаза
   (`claimed`, `assistant_created`, `final_received`, `db_committed`,
   `local_released`, `failed`, `unresolvable_cleanup`) пишет одну
   строку `TASK lifecycle task=<id> phase=<phase> ...`. По ним можно
   реконструировать сценарий зависшего процесса.

Тесты:

* `tests/test_postgres_channel.py::TestPostgresChannelTurnLifecycle` —
  7 unit-тестов на lifecycle (включая `stream_end` с пустым delta
  и восстановление по `answer_id`).
* `tests/test_postgres_channel.py::TestPostgresChannelLifecycleDiagnostics` —
  проверяет наличие `final_received`/`db_committed`/`local_released` в DEBUG-логе.
* `tests/integration/test_postgres_channel_lifecycle_stress.py` —
  opt-in (под `NANOBOT_INTEGRATION=1`) integration-тест против
  реальной PostgreSQL: серия из 4 разных финалов + проверка,
  что `exchange.inflight` пуст и `_claim_one` поднимает
  следующую задачу без перезапуска процесса.

### `lib/services/llm_client.py` — единая точка вызова LLM

Единственное место, откуда делаются запросы к LLM-провайдеру: ретраи,
таймауты, логирование через `loguru`, redaction секретов. Параметры
(API-ключ, base URL, модель) — через `config.require_setting("providers",
"llm")`. Используется навыком `audit_analyzer`, утилитой `tools/build_vectors.py`
и другими потребителями. Прямые `httpx`-вызовы к LLM в новом коде запрещены.

### `lib/utils/node_access.py` — обход настроек

Хелперы для безопасного обхода `SETTINGS` / `config.json` / `project.json`
с поддержкой `require_setting` (строгий) и `get_setting` (с fallback).
Удаляет ad-hoc `cfg.get("a", {}).get("b", default)` по кодовой базе. Потребители:
`config_service.py`, `channel_factory.py`, `runtime_patcher.py`.

### `lib/utils/logging_utils.py` — настройка `loguru`

Один модуль с пресетами `setup(level=..., json=..., redact_keys=...)`,
вызываемый из `ApplicationContext.create()` и CLI-цикла. Гарантирует
одинаковый формат логов и redaction секретов во всех точках входа
(`gateway.py`, `cli_agent.py`).

### `lib/utils/project_version.py` — версия проекта

`project_version()` возвращает версию текущего проекта. Канонический источник —
`project.json` → `project.version` (актуальный релизный тег `vX.Y.Z` без префикса
`v`), закоммичен на `master` и распространяется во все релизные ветки.
Git-теги и CHANGELOG для этого ненадёжны: релизные ветки `release/vX.Y`
ответвляются от `master` и не мержатся обратно, поэтому `git describe` и первый
релизный блок `CHANGELOG.md` на `master` отстают от актуального тега.
Fallback при отсутствии ключа — `git describe --tags`, затем `"dev"`.
Используется в стартовом баннере `gateway.py`, чтобы показать версию проекта
рядом с версией библиотеки nanobot (`__version__`).

### `lib/utils/outbound_meta.py` — фильтрация outbound

Скрывает internal-сообщения из пользовательского потока. Раньше фильтр
был в каждом канале свой → поведение UI расходилось с
Postgres/Redis. Теперь — один, через `MessageExchange`.

### `scripts/backfill_media_aw.py` — миграция media в AW-формат

Утилита для существующих развёртываний: читает `agent_conversation_messages`,
конвертирует старые `{filename, data}` в `{filename, file_id, mime_type,
file_size}` (payload → `data_store/cache/sessions/_shared/attachments/`,
в БД — только `file_id`). Идемпотентна: записи с уже проставленным
`file_id` пропускаются, HTTP/HTTPS-ссылки не трогает. CLI:
`python scripts/backfill_media_aw.py [--dry-run]`.

### `lib/services/runtime_health.py` — Health / Readiness

`RuntimeHealth` (liveness) и `RuntimeReadiness` (готовность с учётом
зависимостей) дают операционную картину процесса. Это не HTTP-эндпойнт —
используется в `ApplicationContext.start()` (логирует итоговый readiness)
и аварийными script'ами после deploy.

- **Health (liveness):** `RuntimeHealth.is_alive()` / `status()` — процесс жив,
  asyncio-loop работает, не в shutdown. Пульс отвечает всегда.
- **Readiness:** `RuntimeReadiness.register(name, fn, required=True)` — каждый чек
  это быстрый идемпотентный предикат (`ComponentStatus` или `None` = UP);
  `check()` прогоняет все check'и в `try/except` и сводит в `ReadinessReport`.
- **Статусы:** `READY` (required + optional UP), `DEGRADED` (required OK, optional
  DOWN), `NOT_READY` (required DOWN). Сводное правило — `compute_overall_status`.
  Required зависимости: PG, DuckDB cache; optional: vector search, Redis.
- **Объекты** `ctx.runtime_health` / `ctx.runtime_readiness` создаются
  в `ApplicationContext.create()`.

---

## 📁 Структура проекта

```
nanobot/
├── docs/                                  # каталог технической документации (навигация — docs/README.md)
├── tools/                                # инфраструктурные CLI-утилиты
│   ├── build_vectors.py                  #   сборка векторных индексов (вне навыка)
│   └── check_indexes.py                  #   declared-vs-runtime diff по векторным индексам
├── sql/                                  # DDL сгруппированы по доменам
│   ├── README.md                          #   порядок применения, каталог
│   ├── session/                           #   session_meta + session_messages
│   ├── channels/                          #   seed_messages.sql (тестовые данные)
│   ├── logs/                              #   agent_gateway_logs (DbLoggingService, имя через logging.db.table_name)
│   ├── audit_analyzer/                    #   домен oarb.* + векторы (GP)
│   └── migrations/                        #   инкрементальные миграции (например, logs)
│
├── lib/                                  # сервисный слой
│   ├── core/                             #   bootstrap ApplicationContext + фабрики
│   │   ├── application_context.py        #     create/start/stop, связывает все общие сервисы
│   │   ├── agent_factory.py              #     AgentLoop + ToolAudit hook + фабрика DatabaseLogging (per-turn)
│   │   ├── bus_factory.py                #     MessageBus + обёртки publish_inbound/outbound
│   │   ├── project_settings.py           #     pydantic-валидация merged SETTINGS (fail-fast)
│   │   ├── skill_config.py               #     параметризованный runtime API для skill'ов
│   │   ├── skill_registration.py         #     декларативная регистрация skill-ресурсов
│   │   └── infra_registration.py         #     регистрация инфраструктурных ресурсов (vector storage)
│   ├── services/                         #   сервисный слой
│   │   ├── config_service.py             #    SETTINGS-аксессор + pre-resolve env + таймауты
│   │   ├── session_storage.py            #    выбор PGSessionManager / SessionManager
│   │   ├── runtime_patcher.py            #    12 monkey-patch'ей upstream nanobot.agent.loop.AgentLoop
│   │   ├── project_tool_loader.py        #    stateless loader project tools (workspace/tools/*.py)
│   │   ├── channel_factory.py            #    ChannelManager + Redis/Postgres каналы
│   │   ├── transcription_service.py      #    openai/groq key/URL/language
│   │   ├── preload_service.py            #    FAISS preload + audit_cache refresh
│   │   ├── db_logging_service.py         #    worker, batch INSERT, без JSONL-fallback, get_stats()
│   │   ├── db_logging_bus.py             #    обёртки publish_inbound/outbound
│   │   ├── llm_config.py                 #    resolve_llm_config() — общий резолв LLM для навыка/бенчмарка
│   │   ├── duckdb_cache_store.py         #     локальный кэш + FAISS-индексы в памяти
│   │   ├── cache_load_service.py         #     разовая синхронная загрузка кэша из PG
│   │   ├── cache_provider.py             #     интерфейс CacheProvider + SearchResult
│   │   ├── cache_provider_impl.py        #     PostgresDuckDbProvider + фабрика и модульные функции
│   │   ├── text_splitter.py              #     чанкование текстов для индексаторов
│   │   ├── vector_index_service.py       #     VectorIndexBuildService — build-слой (провайдер + эмбеддинг)
│   │   ├── table_registry.py             #     pluggable-реестр ресурсов (skill + infra namespaces)
│   │   ├── context_compaction.py         #     ContextCompactionService — единая точка сжатия контекста
│   │   ├── consolidator_locale.py        #     monkeypatch Jinja2-шаблонов из workspace/overrides/
│   │   ├── runtime_health.py             #     RuntimeHealth/RuntimeReadiness (liveness + readiness)
│   │   ├── runtime_events_subscriber.py  #     подписка на runtime-события → turn-метрики
│   │   ├── compaction_event_subscriber.py#     событие context_compacted → шина
│   │   ├── session_cold_sync_service.py  #     daemon: upstream JSONL → PG cold-storage mirror
│   │   ├── llm_observer.py               #     обёртки observer-pipeline (fail-soft)
│   │   ├── llm_usage_store_factory.py    #     фабрика upstream LLMUsageStore
│   │   └── llm_client.py                 #     call_llm / call_llm_async (OpenAI-compatible HTTP)
│   │   # DDL для DbLoggingService (agent_gateway_logs, имя через logging.db.table_name) — в sql/logs/
│   ├── cli/                              #  вынесено из cli_agent.py
│   │   ├── console_loop.py               #   REPL + typewriter + consume_outbound
│   │   ├── display_config.py             #   DisplayConfig
│   │   └── hook_loader.py                #   сканирование workspace/hooks/*.py
│   ├── hooks/                            #  фреймворковые хуки (не плагины)
│   │   ├── tool_audit_hook.py            #     хук аудита вызовов инструментов
│   │   ├── database_logging_hook.py      #     AgentHook для tool-событий + run_finished в БД; per-turn инстанс через make_db_logging_hook_factory
│   │   └── terminal_tool_print_hook.py   #     вывод результатов tool'ов в терминал (gateway.print_tools)
│   ├── lifecycle/                        #  цикл запуска и graceful shutdown
│   │   ├── gateway_runner.py             #   run_forever с exponential backoff (1с → 30с)
│   │   └── shutdown_coordinator.py       #   LIFO graceful shutdown
│   ├── channels/                         #   каналы
│   │   ├── postgres_channel.py           #     канал через таблицу agent_conversation_messages
│   │   ├── redis_channel.py              #     канал через Redis-очереди (BRPOP/LPUSH)
│   │   ├── priority_commands.py          #     команды с приоритетом над обычной очередью
│   │   └── message_exchange.py           #     общий формат сообщений каналов (MessageExchange)
│   ├── session/                          #   хранилище сессий
│   │   └── pg_session_manager.py         #     cold-storage mirror поверх upstream JSONL SessionManager
│   └── utils/                            #   утилиты сервисного слоя
│       ├── sql_safety.py                 #     SQL Security Guard (read-only AST-политика)
│       ├── outbound_meta.py              #     фильтрация служебных outbound
│       ├── text_utils.py, project_version.py,
│       │   duckdb_query.py, retry.py, node_access.py, logging_utils.py
│
├── workspace/                            # runtime-данные и плагины-хуки
│   ├── hooks/                            # плагины: самодостаточные AgentHook (cls(workspace_dir=...))
│   │   ├── session_file_redirect_hook.py #     перенаправление write/edit + media тула message в data_store/cache/sessions/
│   │   ├── recent_files_hook.py          #     сбор созданных файлов для auto-attach в media
│   │   └── debug_stream_diag.py          #     диагностика стриминга
│   ├── tools/                            # кастомные tool'ы (auto-discover через patch_project_tools)
│   │   ├── compact_context.py, history_search_tool.py,
│   │   │   legal_summarizer_query.py, example.py
│   ├── utils/                            # утилиты workspace
│   │   ├── db.py, media.py, jsonb.py, session_file_store.py,
│   │   │   session_key.py, clean_text.py, office_files.py
│   ├── skills/audit_analyzer/            # навык: тонкий CLI поверх провайдера
│   │   ├── SKILL.md                      #   пользовательская документация
│   │   ├── scripts/
│   │   │   ├── cli.py                    #   точка входа (python scripts/cli.py ...)
│   │   │   ├── skill_config.py           #   конфиг из SETTINGS + build_cache_provider()
│   │   │   ├── generated_sql_mode.py     #   режим generated_sql: LLM → SQL → EXPLAIN → выполнение
│   │   │   ├── llm.py                    #   LLM-клиент (OpenAI-compatible HTTP)
│   │   │   ├── output.py                 #   форматирование JSON-вывода
│   │   │   └── predefined/               #   predefined SQL из PG-реестра (DB-first)
│   │   │       ├── db_loader.py          #     lookup скриптов в public.agent_predefined_scripts
│   │   │       ├── mode.py               #     predefined.run() — выполнение через CacheProvider.query_sql
│   │   │       ├── builder.py, validator.py, models.py  #   ParamDefinition/ScriptDefinition
│   └── skills/office_files/              # навык: чтение docx/xlsx/xls/pdf/pptx/csv/txt
│       ├── SKILL.md                      #   пользовательская документация
│       └── (utils: workspace/utils/office_files.py)
│
│       # legal_summarizer — структура scripts:
│       # см. секцию «legal_summarizer — внутренняя структура» ниже.
│
├── gateway.py                            #  тонкий оркестратор
├── cli_agent.py                          #  тонкий оркестратор
├── config.py                             # SETTINGS (project.json + config.json + .secrets.env)
└── project.json                          # конфигурация (channels.*, skills.*, gateway, cli, logging.db)
```

---
## legal_summarizer — внутренняя структура

Структура `workspace/skills/legal_summarizer/scripts/`: вся жилая логика — это
Python-пакет внутри `scripts/` (корневой `pyproject.toml::pythonpath` включает
`workspace/skills/legal_summarizer` и `workspace/skills/legal_summarizer/scripts`,
импорты плоские: `from application.service import ...`, `from document.physical import ...`).
CLI-обёртки — `cli.py` / `cli_query.py`:

```
scripts/
├── cli.py                     # практики CLI (audit query) + разовые операции
├── cli_query.py               # QA по пакетам документов (tool legal_summarizer_query)
│
├── application/               # оркестратор — единственная точка над всем графом:
│   ├── service.py             #   run / inspect / estimate / quick_estimate / load_text /
│   │                          #   load_structure / make_operation_id
│   ├── canonical.py           #   inspect_canonical / run_canonical_pipeline /
│   │                          #   build_pipeline_result
│   ├── pipeline_structure.py  #   run_canonical_pipeline impl
│   ├── brief_context.py       #   BriefContextBuilder.build_brief_chunk
│   │                          #   (BRIEF CONTRACT: один документ → ровно один Chunk)
│   ├── brief_compression.py   #   детерминированная weighted компрессия секций
│   ├── execution_orchestration.py   #   координатор batch-исполнения
│   ├── context_builder.py     #   построение контекста для reducers
│   ├── chunk_selection.py     #   выбор Chunk'ов под вопрос
│   ├── document_io.py         #   чтение/сохранение документов
│   ├── estimation.py          #   оценочные проходы (без LLM)
│   ├── inspection.py          #   inspect-режим
│   ├── manifest_builder.py    #   сборка NormalizedManifest
│   ├── operation_id.py        #   make_operation_id
│   ├── question_context.py    #   контекст вопроса (single_context_block)
│   └── section_index.py       #   индексирование секций
│
├── cache/                     # долговечные per-operation-state:
│   ├── manifest.py            #   NormalizedManifest, resume API
│   └── document_cache.py      #   document-level кеш хunk'ов (по session_key+document_id)
│
├── chunking/                  # чанкинг поверх document-блоков:
│   ├── chunker.py, chunks.py, order.py, packing.py,
│   │   importance_score.py, structural_packing.py, _text_helpers.py
│
├── document/                  # работа с PhysicalDocument (включая бывший domain/):
│   ├── loader.py              #   DocumentLoader (PDF/DOCX/TXT)
│   ├── physical.py            #   PhysicalDocument, block extraction
│   ├── structure.py           #   DocumentStructure, Block, Chunk (бывший domain/models.py)
│   ├── identity.py            #   DocumentIdentity (fingerprint = sha256)
│   ├── numbering.py           #   ArticleNumberingDetector
│   ├── heading.py, hierarchy.py, list_detection.py,
│   │   pdf_outline.py, title.py, block_lookup.py,
│   │   repair.py, validation.py, section_helpers.py,
│   │   analysis.py, safety_merge.py, block_ownership.py
│
├── execution/                 # чистое исполнение batch-плана (выше document):
│   ├── pipeline.py            #   process_context_batch, run_one_batch_async
│   ├── hierarchical.py        #   reduce_chunks_hierarchical, reduce_sections_to_document,
│   │                          #   deterministic_truncate
│   ├── map_reduce.py          #   flat map-reduce стратегия
│   └── config.py              #   ExecutionConfig
│
├── llm/                       # LLM-клиент + sanitization (лист):
│   ├── client.py              #   chat(), LLMRunner
│   ├── calls.py               #   _run_all_calls (single-flight + retry)
│   ├── prompts.py, prompts_runtime.py
│   ├── retry.py               #   build_repair_prompt (LLM-driven)
│   ├── sanitize.py            #   strip_think_blocks, extract_subject
│   ├── single_flight.py       #   asyncio.Semaphore-based gate
│   ├── tokens.py              #   token_estimator, TokenBudget, MID_REDUCE_GROUP_SIZE
│   └── config.py              #   get_chunking_config / get_execution_config / …
│                              #   (бывший ``skill_config.py``)
│
├── output/                    # вывод пользователю:
│   └── presenter.py           #   prepare_output, build_confirmation_options
│
└── planning/                  # выбор стратегии + plan (выше document/retrieval):
    ├── strategy.py            #   select_strategy (direct / map_flat / map_hierarchical)
    └── plan.py                #   ExecutionPlan, PlannedBatch
```

Бывшие слои `domain/` и `infrastructure/` упразднены: pure-данные (`identity`,
`numbering`, `tokens`, конфиги) разложены по слоям-владельцам
(`document/`, `llm/`, `execution/`), а dev-tooling (архитектурные проверки,
`assert_no_legacy`) вынесено из production-пакета в корневой `tools/`
(`tools/architecture_guard.py`, `tools/legacy_audit.py`).
До этого пакет переезжал дважды: `src/legal_summarizer/` → корень Skill →
`scripts/` (плоские импорты для CLI).

Граница слоёв (§65, `docs/TARGET_ARCHITECTURE.md`) автоматически
проверяется в `tests/architecture/test_layer_boundaries.py`: домен
не может импортировать ничего, документ — retrieval/execution/llm/
planning, retrieval — execution/llm, planning — llm, execution —
document/retrieval/llm.

### Ключевые invariants (legal_summarizer)

- **#4.** Single-call strategy (`strategy="single"`) → ровно 1 LLM call для
  документов, помещающихся в `single_call_threshold` chars или
  `TokenBudget.direct_call_tokens` (opt-in через `direct_strategy_min_chars`).
- **#5.** Map-reduce без opt-in → `len(batches)` map-вызовов + ≥1 reduce-вызов.
  Opt-in DIRECT переключает на single path для подходящих документов.
- **#8.** Cache separation: document cache keyed по `(session_key, document_id)` —
  immutable chunks для follow-up вопросов. Manifest keyed по `operation_id` —
  mutable state. Разные namespace, не конфликтуют.
- **#15.** Section reduce вызывается **только** при `should_use_hierarchical_reduce`
  ИЛИ `select_reduce_strategy == ReduceStrategy.HIERARCHICAL`.
  Legacy criterion: `count_meaningful_sections >= 3`. Новый criterion:
  token-budget first, sections second.
- **#20.** LLM-trim секций заменён на truncation `[:max_chars]`.
  `section_trim_calls` всегда 0 в stats.

### Brief: всегда ровно один Chunk (BRIEF CONTRACT)

`legal_summarizer --length brief` (default) собирает через
`application.brief_context.build_brief_chunk` **ровно один**
`Chunk` — компактное структурное представление всего документа.
Это **архитектурный инвариант**, а не настройка:

* `len(ctx.chunks) == 1` → `strategy="direct"`, `plan=None`
  (см. `context_builder.build_execution_context`).
* Никакого map-reduce, никакого fallback на несколько chunks.
* Источники: `DocumentAnalysis.physical` и `DocumentAnalysis.structure`
  напрямую — `analysis.chunks` (canonical) **не используется**.

Структура итогового `chunk.text`:

```text
DOCUMENT STRUCTURE
<outline всех значимых structural nodes в pre-order>

DOCUMENT CONTENT
[Preamble]
<preamble blocks>
[<Section heading>]
<все physical blocks subtree в document order>
```

`max_chars` рассчитывается **динамически**:

```text
max_chars = agents.defaults.contextWindowTokens
          * chunking.brief_input_ratio
          * brief_context.chars_per_token
```

Fallback: `brief_context.max_chars_fallback` (если контекстное окно
неизвестно). Текущие дефолты: 65536 tokens × 0.13 × 3.5 = ~29800 chars.

При превышении `max_chars` сжатие идёт **по тексту секций**
(`application.brief_compression`):

1. Все headings секций сохраняются.
2. Document structure (outline) сохраняется с собственным budget
   (`brief_context.structure_max_chars`).
3. Тексты сокращаются по безопасной границе
   (paragraph → newline → sentence → word → hard char).
4. Сокращённые секции получают явный маркер
   `[BRIEF: section content truncated]` — LLM понимает, что
   отсутствие дальнейшего текста не означает, что в документе этого
   больше нет.
5. Целые секции никогда не удаляются (даже при переполнении).
6. Таблицы передаются атомарно (на уровне блока, не строки).

Удалённые legacy-модули: `chunking/importance_brief.py`,
`chunking/brief_budget.py`, `application/brief_from_analysis.py`.
Удалённые config-ключи: `chunking.brief_coverage_ratio`,
`chunking.brief_max_chars_per_chunk`, `chunking.brief_max_input_chars`.
`retrieval.followup.build_followup_response(mode="brief")` теперь
raises `NotImplementedError` (brief — chunk-selection concern,
а не retrieval).

### Opt-in флаги (default OFF для back-compat)

- `chunking_config.direct_strategy_min_chars > 0` → DIRECT strategy для
  средних документов. Default 0 = старое поведение.
- `PackingConfig.allow_adjacent_sections=True` → locality-aware packing.
  Default False = strict section-locality.

### Тесты

`tests/test_resume_scenarios.py` (10 tests),
`tests/test_information_preservation.py` (10 tests), `tests/benchmarks/test_quality_benchmark.py`
(12 tests), `tests/benchmarks/test_acceptance_matrix.py` (9 tests).

---

