# Разработка nanobot: audit_analyzer и универсальный слой данных

> **Назначение:** техническая документация для разработчиков. Описывает архитектуру
> навыка `audit_analyzer`, универсальный инфраструктурный слой данных
> (`lib/services`), а также **v2.0.0 bootstrap-слой** (`lib/core/ApplicationContext`,
> `lib/lifecycle/`, `lib/cli/`, `lib/services/DbLoggingService`/`RuntimePatcher`/...)
> — общий для `gateway.py` и `cli_agent.py`. Управление DuckDB-кешем,
> векторными индексами и SQL-скрипты для развёртывания нужных таблиц.
> Пользовательская документация навыка — [`workspace/skills/audit_analyzer/SKILL.md`](workspace/skills/audit_analyzer/SKILL.md).
> Обзор проекта — [`README.md`](README.md).

---

## 📋 Оглавление

1. [Архитектура](#архитектура)
2. [Сервисный слой v2.0.0 (ApplicationContext + lib/)](#сервисный-слой-v200-applicationcontext--lib)
3. [Структура проекта](#структура-проекта)
4. [Универсальный слой данных lib/services](#универсальный-слой-данных-libservices)
5. [Конфигурация навыка](#конфигурация-навыка)
6. [CLI навыка: режимы](#cli-навыка-режимы)
7. [Жизненный цикл кеша](#жизненный-цикл-кеша)
   - [Управление синхронизацией](#управление-синхронизацией)
8. [Векторная индексация](#векторная-индексация)
9. [SQL-скрипты: создание таблиц](#sql-скрипты-создание-таблиц)
10. [Полная таблица связей между файлами (v2.0.0)](#полная-таблица-связей-между-файлами-v200)
11. [Тестирование](#тестирование)
12. [Изменения и миграции](#изменения-и-миграции)

---

## 🏗 Архитектура

Инфраструктура (DuckDB-кеш, векторные индексы, эмбеддинги) вынесена из навыка
в **универсальный слой** `lib/services` — он не завязан на предметную область
«аудит» и может переиспользоваться любым навыком. Навык `audit_analyzer` остался
тонким CLI: он конфигурирует провайдера из своих настроек и работает с ним
напрямую (без промежуточных обёрток-шимов).

```
                        ┌──────────────────────────────────────────────┐
  PostgreSQL (канон) ───►│  lib/services (универсальный слой данных)   │
  oarb.audits,           │  • CacheProvider (интерфейс)                │
  oarb.violations, ...   │  • PostgresDuckDbProvider (реализация)      │
                        │  • AuditSyncService   (поллинг PG, worker)  │
                        │  • AuditMemoryStore   (in-memory DuckDB+FAISS)│
                        │  • get_embedding (Ollama)                    │
                        └───────────────┬──────────────────────────────┘
                                        │ query_sql / get_schema / explain
                                        │ search_vector / preload_indexes
                ┌───────────────────────┼────────────────────────────┐
                ▼                       ▼                            ▼
        DuckDB-кеш (снимок)      FAISS-индексы (store в БД)      эмбеддинг
        oarb.* (аналитика)       oarb.audit_vectors →           Ollama /api/embed
                                 oarb.vector_index_store

        Владелец кеша:             Потребители (только чтение):
        gateway.py                 навык CLI (scripts/cli.py)
        (AuditSyncService →        (режимы predefined/sql/vector)
         AuditMemoryStore →
         publish() temp+os.replace)
```

**Потоки данных**

- `gateway.py` — единственный владелец файла кеша навыка. `AuditSyncService`
  (worker-поток, единственное подключение к PG) инкрементально синхронизирует
  таблицы в `AuditMemoryStore` (чисто in-memory DuckDB), а после каждого цикла
  `store.publish()` атомарно записывает снимок (temp + `os.replace`) в файл
  кеша навыка (`in_memory_cache_path`).
- Навык CLI (`predefined`/`sql`) — запросы выполняются по опубликованному кешу
  (или напрямую по PG, если кеш выключен). Создание/обновление кеша его не
  касается. Единый интерфейс бэкенда: `get_schema / query_sql / explain`.
- `--mode vector` — семантический поиск по FAISS-индексу: провайдер загружает
  индекс из `oarb.vector_index_store` (BYTEA), при промахе пересобирает из
  `oarb.audit_vectors`, эмбеддинг запроса получает через Ollama.

---

## 🆕 Сервисный слой v2.0.0 (ApplicationContext + lib/)

В v2.0.0 gateway и cli_agent сократились с 696/865 до 132/165 строк за счёт
вынесения всей инициализации в `ApplicationContext` (см. подробности в
`README.md` v2.0.0 changelog). Этот раздел — про
**внутреннее устройство** нового слоя, нужно при добавлении новых
сервисов или изменении lifecycle.

### Точки входа → общий bootstrap

```
gateway.py (132)  ─┐
                   ├─► ApplicationContext.create(...)
cli_agent.py (165) ─┘   │
                       ▼
              lib/core/ApplicationContext
              ├─ ConfigService (config.json, SETTINGS, pre-resolve env)
              ├─ SessionStorageService (PGSessionManager / SessionManager)
              ├─ DbLoggingService (worker, batch INSERT, JSONL fallback)
              ├─ AuditSyncService + AuditMemoryStore (audit_analyzer)
              ├─ MessageBus (через BusFactory, с обёрткой под логгеры)
              ├─ AgentLoop (через AgentFactory, hooks=[ToolAudit, DbLogging])
              ├─ RuntimePatcher (ContextGovernor + _assemble_outbound)
              ├─ PreloadService (FAISS / audit_cache)
              └─ TranscriptionService
```

### `lib/core/` (ApplicationContext + фабрики)

- **`application_context.py:ApplicationContext`** — единственный класс,
  собирающий все общие сервисы. Поля (помимо путей и конфига):
  `bus`, `agent`, `tool_audit_hook`, `hooks`, `session_manager`,
  `storage_mode`, `db_logging_service`, `audit_sync_service`,
  `audit_memory_store`, `config_service`, `runtime_patcher`,
  `transcription_service`, `subprocess_manager`, `preload_service`.
  Метод `start()` использует `ShutdownCoordinator` для регистрации
  сервисов; `stop()` — LIFO graceful shutdown.
  **Graceful degradation:** если БД недоступна, сервис остаётся `None`,
  gateway/cli работают без него (с предупреждением в логах).
- **`agent_factory.py:AgentFactory`** — `create(config, bus, session_manager=...,
  cron_service=..., db_logging_service=...)` → `(agent, hooks)`.
  Lazy-import `workspace.hooks.database_logging_hook` через try/except
  (если модуль не подключён — хук просто не добавляется).
- **`bus_factory.py:BusFactory`** — `create()` возвращает `MessageBus`,
  опционально обернув `publish_inbound`/`publish_outbound` async-логгерами
  из `db_logging_bus.py`. **Без monkey-patch'ей**: оригинальные методы
  шины сохраняются в замыкании.

### `lib/services/` (новые сервисы v2.0.0 + старые audit/кэш)

Полный список модулей (новые и pre-existing) — см. раздел [«Полная таблица связей»](#полная-таблица-связей-между-файлами-v200) ниже. Здесь — только
**новые** (v2.0.0), с краткой мотивацией:

| Сервис | Мотивация (почему выделен) |
|--------|---------------------------|
| `config_service.py` | Дубликат `_load_runtime_config` + `SETTINGS`-аксессора между gateway и cli. Pre-resolve `${PROVIDER_API_KEY}` от .secrets.env (см. ниже). |
| `session_storage.py` | Выбор `PGSessionManager` / `SessionManager` (auto / postgres / file) с поддержкой `session_manager.json` override. |
| `runtime_patcher.py` | `ContextGovernor.normalize_tool_result` + `agent._assemble_outbound` — оба monkey-patch'а в одном классе с fallback при изменении API nanobot. |
| `channel_factory.py` | `ChannelManager` + Redis + Postgres каналы + транскрипция (вынесено из gateway). |
| `transcription_service.py` | openai/groq key/URL/language (вынесено из gateway). |
| `subprocess_manager.py` | Streamlit spawn + terminate/kill. |
| `preload_service.py` | Разделяет FAISS preload (gateway) и audit_cache refresh (cli). |
| `db_logging_service.py` | **Новый** — структурированный журнал агента в `gateway_logs`. |
| `db_logging_bus.py` | **Новый** — обёртки `publish_inbound`/`publish_outbound` для `DbLoggingService`. |

### Pre-resolve `${VAR}` от `.secrets.env`

`nanobot._load_runtime_config` резолвит `${MISTRAL_API_KEY}` только из
`os.environ` и при отсутствии падает `ValueError`. Между тем `config.py`
для провайдерских ключей использует провайдер-скоупинг формат
`.secrets.env`:

```
# providers: mistral
api_key=XavGPsHjtNt3uOtFGUhabUuad5PRm2D0W
```

— в `os.environ` это не попадает как `MISTRAL_API_KEY`. Решение
(`ConfigService._pre_resolve_env_refs`): прочитать `config.json`,
найти `${VAR}` плейсхолдеры, для каждого `*_API_KEY` без env — достать
ключ из `SETTINGS.providers.<lower>.api_key` (туда `config.py` уже
подставил значение) и положить в `os.environ` ДО `_load_runtime_config`.
**Gateway больше НЕ требует `export MISTRAL_API_KEY=...` в shell.**

### Race-condition fix: callbacks ДО `ctx.start()`

`AuditSyncService` — worker-поток, который делает `initial_load` сразу
после `start()`. Если `set_on_new_records_callback(upsert_records)` ещё
не вызван к этому моменту, `_dispatch` скипает записи → DuckDB остаётся
пустым → `preload_vector_indexes` видит "нет данных" несмотря на
данные в `oarb.audit_vectors`. **Fix:** в `gateway.py:main()` callbacks
устанавливаются **ДО** `ctx.start()`. Тогда worker-тред при первом
`_do_initial_load` уже видит настроенные callbacks → `upsert_records`
вызывается → FAISS preload находит данные.

---

## 📁 Структура проекта

```
nanobot/
├── DEVELOPMENT.md                        # этот документ
├── tools/                                # инфраструктурные CLI-утилиты
│   └── build_vectors.py                  #   сборка векторных индексов (вне навыка)
├── sql/                                  # DDL всех таблиц, нужных навыку
│   ├── create_audit_source_tables_gp.sql # REFERENCE-схема домена (audits, violations, ...)
│   ├── create_audit_vectors_table_gp.sql # oarb.vector_index_store + oarb.audit_vectors
│   └── create_vector_index_config_gp.sql # oarb.vector_index_config
│
├── lib/                                  # ⭐ v2.0.0: сервисный слой
│   ├── core/                             #   bootstrap ApplicationContext + фабрики
│   │   ├── application_context.py        #     create/start/stop, связывает все общие сервисы
│   │   ├── agent_factory.py              #     AgentLoop + ToolAudit + DatabaseLogging hooks
│   │   └── bus_factory.py                #     MessageBus + обёртки publish_inbound/outbound
│   ├── services/                         #   сервисный слой (v2.0.0 + pre-existing)
│   │   ├── config_service.py             # ⭐   SETTINGS-аксессор + pre-resolve env + таймауты
│   │   ├── session_storage.py            # ⭐   выбор PGSessionManager / SessionManager
│   │   ├── runtime_patcher.py            # ⭐   все monkey-patch'и (ContextGovernor + _assemble_outbound)
│   │   ├── channel_factory.py            # ⭐   ChannelManager + Redis/Postgres каналы
│   │   ├── transcription_service.py      # ⭐   openai/groq key/URL/language
│   │   ├── subprocess_manager.py         # ⭐   Streamlit spawn + terminate/kill
│   │   ├── preload_service.py            # ⭐   FAISS preload + audit_cache refresh
│   │   ├── db_logging_service.py         # ⭐   worker, batch INSERT, JSONL fallback, get_stats()
│   │   ├── db_logging_bus.py             # ⭐   обёртки publish_inbound/outbound
│   │   ├── audit_memory_store.py         #     in-memory DuckDB-зеркало + атомарный publish()
│   │   ├── audit_sync_service.py         #     фоновый поллинг PG (worker-поток)
│   │   ├── cache_provider.py             #     интерфейс CacheProvider + SearchResult
│   │   ├── cache_provider_impl.py        #     PostgresDuckDbProvider + фабрика и модульные функции
│   │   ├── text_splitter.py              #     чанкование текстов для индексаторов
│   │   └── sql/
│   │       └── create_logs_table.sql     # ⭐ DDL для DbLoggingService (gateway_logs)
│   ├── cli/                              # ⭐ вынесено из cli_agent.py
│   │   ├── console_loop.py               #   REPL + typewriter + consume_outbound
│   │   ├── display_config.py             #   DisplayConfig
│   │   └── hook_loader.py                #   сканирование workspace/hooks/*.py
│   ├── lifecycle/                        # ⭐ цикл запуска и graceful shutdown
│   │   ├── gateway_runner.py             #   run_forever с exponential backoff (1с → 30с)
│   │   └── shutdown_coordinator.py       #   LIFO graceful shutdown
│   ├── channels/                         #   каналы
│   │   ├── postgres_channel.py           #     канал через таблицу conversation_messages
│   │   └── redis_channel.py              #     канал через Redis-очереди (BRPOP/LPUSH)
│   ├── session/                          #   хранилище сессий
│   │   └── pg_session_manager.py         #     хранение сессий в PostgreSQL (замена JSONL)
│   └── (см. lib/core/, lib/cli/, lib/lifecycle/ выше)
│
├── workspace/                            # runtime-данные и хуки
│   ├── hooks/
│   │   ├── tool_audit_hook.py            #   хук аудита вызовов инструментов
│   │   └── database_logging_hook.py      # ⭐ AgentHook для tool-событий + run_finished в БД
│   └── skills/audit_analyzer/            # навык: тонкий CLI поверх провайдера
│       ├── SKILL.md                      #   пользовательская документация
│       ├── audit_analyze.bat / .sh       #   точки входа
│       ├── scripts/
│       │   ├── cli.py                    #   парсинг аргументов, маршрутизация режимов
│       │   ├── skill_config.py           #   конфиг из SETTINGS + build_cache_provider()
│       │   ├── database.py               #   Database (прямой PG, fallback) + QueryBackend
│       │   ├── sql_mode.py               #   режим sql: LLM → SQL → EXPLAIN → выполнение
│       │   ├── predefined_mode.py        #   режим predefined: готовые SQL-шаблоны
│       │   ├── predefined.py             #   резолв параметров (+ векторный поиск по source)
│       │   ├── scripts_registry.py       #   ScriptDefinition / ParamDefinition / реестр
│       │   ├── llm.py                    #   LLM-клиент (OpenAI-compatible HTTP)
│       │   └── output.py                 #   форматирование JSON-вывода
│       └── tests/
│           └── e2e_test.py               #   сквозной тест навыка (нужна живая БД)
│
├── gateway.py                            # ⭐ v2.0.0: 132 строки, тонкий оркестратор
├── cli_agent.py                          # ⭐ v2.0.0: 165 строк, тонкий оркестратор
├── pg_agent_worker.py                    # [legacy, не через ApplicationContext]
├── streamlit_app.py                      # [web-клиент, не через ApplicationContext]
├── config.py                             # SETTINGS (project.json + config.json + .secrets.env)
└── project.json                          # конфигурация (channels.*, skills.*, gateway, cli, logging.db)
```

---

## 🔌 Универсальный слой данных lib/services

**Интерфейс** — `lib/services/cache_provider.py`:

- `CacheProvider` (ABC): `is_ready()`, `refresh()`, `check_stale()`,
  `preload_indexes()`, `search_vector()`, `query_sql()`, `explain()`,
  `get_schema()`, `close()`.
- `SearchResult` (dataclass): `content`, `score`, `source`, `table`, `pk_value`,
  `chunk`, `matched_chunks`, `row`.

**Реализация** — `lib/services/cache_provider_impl.py`:

- `PostgresDuckDbProvider` — единый провайдер: DuckDB-кеш + векторные индексы.
  Конструктор принимает только конфигурацию (схему, таблицы, пути, модель
  эмбеддинга) — без привязки к «аудиту».
- Модульные функции: `get_embedding()` (Ollama `/api/embed`),
  `load_cache_from_postgres(cache_path, db_config)`, `check_cache_stale(...)`,
  `read_vector_index_config(cfg)` (конфиг индексов из БД → fallback в настройках),
  `read_embedding_config(cfg)`, `build_cache_provider(cfg, base_dir)` (фабрика
  провайдера из конфиг-секции навыка).
- Тяжёлые зависимости (`duckdb`, `psycopg2`, `faiss`, `numpy`, `pandas`, `httpx`)
  импортируются **лениво** внутри методов — импорт модуля остаётся лёгким,
  и gateway может управлять жизненным циклом без побочных эффектов.
- Если передан `dsn` — провайдер сам вызывает `utils.db.configure(dsn)`
  (идемпотентно), поэтому пригоден для использования автономно.

**Единый интерфейс бэкенда запросов.** `Database` (прямой PG) и
`PostgresDuckDbProvider` (кеш) реализуют одинаковые методы
`get_schema / query_sql / explain` (протокол `QueryBackend` в
`scripts/database.py`), поэтому режимы `predefined`/`sql` работают с любым
бэкендом без ветвлений.

**Фабрика провайдера** — универсальная `lib.services.cache_provider_impl.build_cache_provider(cfg, base_dir)`
собирает провайдера из конфиг-секции навыка (DuckDB-кеш, индексы, эмбеддинг).
Навык делегирует ей через `scripts/skill_config.build_cache_provider()`, тот же
набор настроек читает `gateway.py::_build_audit_services()` и индексатор
`tools/build_vectors.py`.

---

## ⚙️ Конфигурация навыка

Секция `skills.audit_analyzer` в `project.json`:

| Ключ | Назначение | Значение / по умолчанию |
|------|-----------|-------------|
| `llm_provider` / `llm_model` / `llm_api_base` | LLM для генерации SQL | `mistral` / `mistral-large-latest` / `https://api.mistral.ai/v1` |
| `llm_max_tokens` / `llm_temperature` | Параметры генерации | `8192` / `0.1` |
| `db_schema` | Схема с таблицами аудита | `oarb` |
| `db_tables` | Таблицы, доступные агенту | `audit_reports, audits, report_items, violations` (значение project.json; код по умолч. — пустой список) |
| `schema_cache` | Файловый кеш схемы (enabled/path/ttl_seconds) | резерв: блок настроен (`enabled: true`, path `cache/schema.json`, TTL 86400), но `load_db_config()` не передаёт его в `Database` — кеш схемы фактически не работает |
| `in_memory_enabled` | Включить DuckDB-кеш | `true` |
| `in_memory_engine` | Движок кеша | `duckdb` |
| `in_memory_cache_path` | Путь к файлу кеша (отн. навыка) | `cache/audit_cache.duckdb` |
| `poll_interval_sec` | Период инкрементального поллинга PG в `AuditSyncService` | `60` |
| `full_resync_every` | Полная перезагрузка таблицы каждые N циклов (сверка удалений) | `10` |
| `sync_write_table` | Таблица журнала взаимодействий (создаётся автоматически) | `audit_interactions` |
| `embedding_base_url` | Ollama `/api/embed` | `http://localhost:11434/api/embed` |
| `embedding_model` | Модель эмбеддинга | `mxbai-embed-large:latest` |
| `embedding_dimension` | Размерность вектора | `1024` |
| `mode_vector_db_table` | Таблица сырых векторов (источник индекса) | `oarb.audit_vectors` (значение project.json; код по умолч. — пусто) |
| `mode_vector_store_table` | Таблица сериализованных FAISS-индексов | `oarb.vector_index_store` |
| `cli_default_mode` | Режим по умолчанию (резерв; CLI требует `--mode`) | `predefined` |
| `cli_max_retries` | Ретраи HTTP-запросов LLM-клиента (`llm.py`) | `3` |
| `cli_timeout_sec` | Таймаут запроса к LLM (`llm.py`) | `60` |

> Примечание: ретраи *генерации* SQL в режиме `sql` захардкожены в
> `sql_mode.py` (`MAX_RETRIES = 2` → до 3 попыток) и от `cli_max_retries`
> не зависят.

DSN подключается через `channels.postgres.dsn` в `project.json` /
`DATABASE_URL` в `.secrets.env` (`utils.db.resolve_dsn()`). Навык собственного
DSN не хранит.

---

## 🚀 CLI навыка: режимы

Точка входа: `workspace/skills/audit_analyzer/audit_analyze.bat` (или
`.sh`), либо `python scripts/cli.py`.

```
audit_analyze --mode {predefined,sql,vector} [опции]
```

| Режим | Назначение | Ключевые флаги |
|-------|-----------|----------------|
| `predefined` | Выполнение готовых SQL-шаблонов из реестра | `--script`, `--params` |
| `sql` | Генерация SELECT через LLM по текстовому запросу | `--query`, `--context` |
| `vector` | Семантический поиск по FAISS-индексу | `--query`, `--index-name`, `--top-k`, `--threshold`, `--vector-index` |

Примеры:

```bash
# predefined — готовый шаблон с параметрами
audit_analyze --mode predefined --script analytics_by_year_month --params '{"year": 2024}'

# sql — генерация SQL через LLM и выполнение
audit_analyze --mode sql --query 'сколько аудитов было в 2024 по месяцам'

# vector — топ-3 по схожести
audit_analyze --mode vector --query 'пожарная безопасность' --index-name audits_index --top-k 3

# vector — всё выше порога 0.7
audit_analyze --mode vector --query 'статусы аудитов' --index-name audits_index --threshold 0.7
```

**Как выбирается бэкенд запросов:** если `in_memory_enabled: true` — CLI строит
провайдера (`build_cache_provider()`), открывает DuckDB-кеш на чтение и работает
по нему; иначе — `Database` (прямой PostgreSQL). Кеш создаёт и обновляет
**gateway** (см. раздел «Жизненный цикл кеша»); CLI про это не знает. Если файла
кеша нет — CLI завершается с `FileNotFoundError`: «Кеш создаёт и обновляет
gateway автоматически — запустите его (python gateway.py)».

**Векторный поиск в predefined:** строковые параметры с
`validation.vector_source` (например, `violation_code`, `auditee_entity`,
`audit_type`) резолвятся через семантический поиск — провайдер подставляет
лучшее совпадение из индекса `{source}_index`.

---

## 🔄 Жизненный цикл кеша

**Владелец файла кеша навыка — `gateway.py`.** Навык (CLI) про создание и
обновление кеша больше не знает: `--mode init` и `--force` удалены.

Пара сервисов строится в `gateway.py::_build_audit_services()` (возвращает
`(None, None)`, если `in_memory_enabled` выключен, нет DSN или таблиц):

- **`AuditSyncService`** — единственный владелец подключения к PostgreSQL
  (worker-поток). При старте выполняет полную загрузку таблиц, далее каждые
  `poll_interval_sec` (по умолч. 60 с) инкрементально опрашивает таблицы по
  track-колонке (`updated_at`; для `audit_vectors` — `id`). Новые/изменённые
  строки передаёт в callback `on_new_records` → `AuditMemoryStore.upsert_records`.
  Структуру таблиц собирает из PG `information_schema` (+ `pg_description`):
  колонки, типы, NOT NULL, комментарии → callback `on_schema` →
  `AuditMemoryStore.ensure_schema` (создание пустых таблиц, типы из PG).
  Каждые `full_resync_every` циклов (по умолч. 10) делает полную перезагрузку
  таблицы через `on_replace_records` → `AuditMemoryStore.replace_records`
  (сверка удалённых строк; курсор поллинга не откатывается).
  Дополнительно создаёт таблицу журнала `oarb.audit_interactions`
  (`sync_write_table`), куда через `submit_write()` пишутся ответы агента.
- **`AuditMemoryStore`** — живое зеркало в чисто in-memory DuckDB
  (`cache_path=""`) + FAISS-индексы. `ensure_schema()` создаёт таблицы с типами
  из PG и сохраняет комментарии + исходные PG-типы в мета-таблицу
  `__nanobot_meta.__schema_meta` (входит в снимок). `get_schema()` возвращает
  исходные PG-типы и комментарии (без них — DuckDB-тип из information_schema).
  `publish()` атомарно записывает снимок таблиц (ATTACH во временный файл →
  `os.replace`) в `publish_path` = `in_memory_cache_path` навыка. Без изменений
  (`_dirty` = False) файл не перезаписывается; если снимок занят читателем
  (CLI) — публикация откладывается до следующего цикла, ошибка не теряет данные.

Схема в `gateway.py::run()`:

```
_build_audit_services() ──► (store, sync_service)
sync_service.set_on_new_records_callback(store.upsert_records)
sync_service.set_on_replace_records_callback(store.replace_records)
sync_service.set_on_schema_callback(store.ensure_schema)
sync_service.set_on_sync_callback(store.publish)     # снимок после каждого цикла
sync_service.start(initial_load=True)                # полная загрузка + поллинг
_preload_vector_indexes(store)                       # прогрев FAISS в память
...
(finally) store.publish() → store.close()            # финальный снимок при выходе
```

**Правило одного писателя.** DuckDB допускает только один процесс-писатель на
файл, поэтому gateway никогда не открывает целевой файл на запись: он пишет во
временный файл и атомарно подменяет его `os.replace()`. Навык (CLI) открывает
опубликованный снимок только на чтение и видит целостные данные в любой момент.

**cli_agent.py** по-прежнему содержит унаследованную фоновую загрузку/свежесть
кеша (`load_cache_from_postgres` / `check_cache_stale`) как резерв; основной
владелец и источник снимка — `gateway.py`.

### 🔧 Управление синхронизацией

#### Полный цикл обновления данных (что происходит по шагам)

1. **Старт gateway** (`gateway.py::run()`): `_build_audit_services()` читает
   секцию `skills.audit_analyzer` из `project.json`. Сервисы создаются, только
   если `in_memory_enabled: true`, задан DSN и есть таблицы; иначе — `(None, None)`
   и синхронизация не запускается.
2. **Initial load**: `AuditSyncService._do_initial_load()` для каждой таблицы из
   `db_tables` (+ таблица векторов) делает:
   - `_fetch_schema()` — запрос структуры из PG `information_schema.columns`
     + `pg_description` (колонки, типы, NOT NULL, комментарии таблиц/колонок);
   - `_ensure_table_schema()` → колбека `on_schema` → `store.ensure_schema()` —
     создаёт таблицу в in-memory DuckDB **с типами из PG** (включая пустые);
   - `_fetch_all()` → `SELECT *` → колбека `on_new_records` → `store.upsert_records()`.
3. **Поллинг**: worker-поток каждые `poll_interval_sec` вызывает
   `_poll_table()`: `SELECT * WHERE "<track>" > <последняя_метка>`. Track-колонка:
   `updated_at` (доменные таблицы) или `id` (`audit_vectors`). Новые/изменённые
   строки → `upsert_records()` (upsert по `id`: DELETE + INSERT).
4. **Публикация снимка**: после initial load и каждого цикла поллинга
   `_fire_sync_callback()` → `store.publish()` — атомарный снимок (ATTACH во
   временный файл → `os.replace`) в файл кеша навыка. `publish()` — no-op, если
   данных не менялось (`_dirty = False`) или `publish_path` пуст.
5. **Полная пересинхронизация** (сверка удалений): каждые `full_resync_every`
   циклов поллинга `_poll_table()` вместо инкрементального запроса делает
   `_fetch_all()` + `_dispatch_replace()` → `store.replace_records()` — таблица
   перезаписывается целиком (структура и типы сохраняются), удалённые в PG
   строки исчезают. Курсор поллинга не откатывается (новое значение только если
   больше текущего).
6. **Журнал ответов**: при старте создаётся таблица
   `"<schema>"."<sync_write_table>"` (`audit_interactions`); ответы агента
   ставятся в очередь через `submit_write()` и записываются worker-потоком.
7. **Завершение**: при остановке gateway `sync_service.stop()` дописывает
   очередь, затем в `finally` — финальный `store.publish()` и `store.close()`.

#### Как связаны компоненты (callbacks)

Колбеки подключаются в `gateway.py::run()` (строки ~608-613) — это единственная
точка связывания `AuditSyncService` и `AuditMemoryStore`:

| Событие в AuditSyncService | Колбека | Метод store | Что делает |
|---------------------------|---------|-------------|-----------|
| новые/изменённые строки | `set_on_new_records_callback` | `upsert_records` | инкрементальный апдейт |
| полная перезагрузка таблицы | `set_on_replace_records_callback` | `replace_records` | сверка удалений |
| структура из PG | `set_on_schema_callback` | `ensure_schema` | типы, NOT NULL, комментарии |
| завершение цикла | `set_on_sync_callback` | `publish` | публикация снимка для CLI |

Чтобы изменить поведение (например, публиковать снимок реже или дополнительно
инвалидировать индексы) — правишь именно эту секцию `gateway.py`.

#### Управляющие ключи (`skills.audit_analyzer` в `project.json`)

| Ключ | Где читается | По умолч. | Эффект |
|------|-------------|-----------|--------|
| `in_memory_enabled` | `gateway.py:511` | `false` | Выключает весь кеш и синхронизацию: CLI ходит напрямую в PG. `true` — включает |
| `in_memory_cache_path` | `gateway.py:524` | `cache/audit_cache.duckdb` | Куда публикуется снимок (отн. `workspace/skills/audit_analyzer/`). Меняя — меняешь файл, который читает CLI |
| `db_schema` / `db_tables` | `gateway.py:516-518` | `oarb` / 4 таблицы | Какие таблицы синхронизировать. Список — массив строк; добавил таблицу → она появится в кеше после следующего цикла |
| `poll_interval_sec` | `gateway.py:549` | `60` | Частота инкрементального поллинга, сек. Меньше → свежее кеш, больше запросов к PG |
| `full_resync_every` | `gateway.py:552` | `10` | Полная перезагрузка таблиц каждые N циклов поллинга. `0` — отключить (удалённые строки останутся в кеше) |
| `sync_write_table` | `gateway.py:550` | `audit_interactions` | Таблица журнала ответов агента (создаётся автоматически в схеме `db_schema`) |
| `mode_vector_db_table` | `gateway.py:517` | `oarb.audit_vectors` | Таблица векторов, включается в синхронизацию и прогревается в FAISS |

`config.py` мержит `project.json` в `SETTINGS`; после правки `project.json`
перезапуск gateway обязателен.

#### Требования к таблицам источника

- Колонка `id` (для upsert `DELETE + INSERT` по ключу; если её нет — таблица
  пересоздаётся из батча целиком).
- Колонка `updated_at` (инкрементальный поллинг). Для `audit_vectors` — `id`.
  Тип track-колонки должен быть сравнимым (`>`) — `timestamp`/`bigint`.
- Безопасность поллинга: строки, изменённые **и** удалённые между циклами,
  подхватятся полной пересинхронизацией (`full_resync_every`).

#### Практические сценарии

- **Свежее кеш, чаще опрос**: `poll_interval_sec: 15`.
- **Меньше нагрузки на PG**: `poll_interval_sec: 300`, `full_resync_every: 5`
  (реже опрос, но регулярная сверка удалений).
- **Не нужна сверка удалений / большие таблицы**: `full_resync_every: 0`.
- **Выключить кеш совсем**: `in_memory_enabled: false` (CLI → прямой PG).
- **Добавить таблицу в анализ**: вставить её имя в `db_tables` и перезапустить
  gateway.

#### Мониторинг

- `AuditMemoryStore.get_stats()`: `tables` (кол-во строк), `dirty`,
  `upserts`, `publishes`, `publish_errors`, `last_upsert_at`, `last_publish_at`,
  `last_error`, `indexes_in_memory`, `vector_sources`.
- `AuditSyncService.get_stats()`: `polls`, `full_resyncs`, `reconnects`,
  `errors`, `queue_size`, `last_sync` (метка на таблицу), `connected`.
- Внешние признаки работы: mtime файла кеша
  (`workspace/skills/audit_analyzer/cache/audit_cache.duckdb`) обновляется после
  каждого publish; лог gateway: `audit_analyzer sync started (in-memory cache +
  vectors, публикация кеша навыка: <path>)`.

---

## 🔍 Векторная индексация

### Таблицы

| Таблица | Назначение |
|---------|-----------|
| `oarb.audit_vectors` | Сырые векторы `REAL[]` + метаданные (`content`, `search_text`, `table`, `pk_value`, `chunk_index/count`, `row_data` JSONB, `content_hash`, `max_src_track`, `synced_at`). Строится `build_vectors.py` |
| `oarb.vector_index_store` | Сериализованный FAISS-индекс `BYTEA` (source, metadata, dimension, vector_count, updated_at). Пересобирается провайдером при промахе или после изменений |
| `oarb.vector_index_config` | Конфигурация индексов (основной источник; fallback — `vector_indexes` в project.json) |

### Пайплайн

```
source table          build_vectors.py                 audit_vectors               vector_index_store
(oarb.audits) ──► read → embed → insert          ──►   REAL[] + row_data     ──►   BYTEA (FAISS)
(oarb.violations)                                                                    │
                                                                                     ▼
                                                                          PostgresDuckDbProvider
                                                                          (search: deserialize из store,
                                                                           кеш в памяти, пересборка при промахе)
```

### `build_vectors.py`

Инструмент в корневом [`tools/build_vectors.py`](tools/build_vectors.py) — вне навыка:
создание индексов это инфраструктура, а не задача навыка (навык только запрашивает).

| Флаг | Действие |
|------|----------|
| *(без флагов)* | Инкрементальная синхронизация: новые/изменённые/удалённые строки |
| `--full-rebuild` | Полная перестройка (очистка + все строки) |
| `--check` | Быстрая проверка сигнатуры (COUNT + MAX track_column), синхронизация только при изменениях |
| `--status` | Состояние индексов без синхронизации |
| `--dry-run` | Репетиция без вставки в БД |
| `--index <name>` | Собрать только один индекс |
| `--batch-size` / `--chunk-size` / `--chunk-overlap` | Параметры эмбеддинга и чанкования |
| `--db-table` | Таблица векторов (по умолч. `oarb.audit_vectors`) |

```bash
# из корня проекта:
python tools/build_vectors.py --status
python tools/build_vectors.py --full-rebuild
python tools/build_vectors.py --check          # например, при старте контейнера
python tools/build_vectors.py --index audits_index
```

Алгоритм (на индекс):
1. Конфиг: `oarb.vector_index_config` → fallback `skills.audit_analyzer.vector_indexes`.
2. Загрузка строк источника, сравнение с `audit_vectors` по `(source, pk_value)`,
   классификация NEW / CHANGED (изменился `content_hash`) / DELETED.
3. Эмбеддинг через Ollama батчами; длинные тексты режутся на чанки
   (`--chunk-size`, по умолч. 500, перекрытие `--chunk-overlap`, по умолч. 80)
   — универсальный `lib/services/text_splitter.py`.
4. INSERT/UPDATE в `audit_vectors`; после изменений провайдеру делается
   `invalidate_cache(index)` + `rebuild_and_store_index(index, db_table)`
   (пересборка FAISS и сохранение в `vector_index_store`).

Чанкование: один документ → несколько векторов-чанков; `content` и `row_data`
всегда полные; при поиске из нескольких чанков одного документа возвращается
только один с наибольшим score (`matched_chunks` показывает число совпавших).

### Сброс in-memory кеша индекса

```python
from workspace.skills.audit_analyzer.scripts.skill_config import build_cache_provider
provider = build_cache_provider()
provider.invalidate_cache('audits_index')   # один индекс
provider.invalidate_cache()                 # все
```

---

## 🗃 SQL-скрипты: создание таблиц

Все DDL собраны в корневом каталоге [`sql/`](sql/). Порядок применения:

```bash
# 1. Схема домена (REFERENCE — уточняется владельцем данных)
psql "$DATABASE_URL" -f sql/create_audit_source_tables_gp.sql

# 2. Векторные таблицы (oarb.audit_vectors + oarb.vector_index_store)
psql "$DATABASE_URL" -f sql/create_audit_vectors_table_gp.sql

# 3. Конфигурация индексов (oarb.vector_index_config)
psql "$DATABASE_URL" -f sql/create_vector_index_config_gp.sql

# 4. Сборка индексов
python tools/build_vectors.py --full-rebuild
```

| Файл | Таблицы | Статус |
|------|---------|--------|
| `sql/create_audit_source_tables_gp.sql` | `oarb.audits`, `oarb.violations`, `oarb.audit_reports`, `oarb.report_items` | **REFERENCE** — минимальный набор колонок из кода, уточняет владелец данных |
| `sql/create_audit_vectors_table_gp.sql` | `oarb.vector_index_store`, `oarb.audit_vectors` (+ индексы) | рабочий |
| `sql/create_vector_index_config_gp.sql` | `oarb.vector_index_config` | рабочий |

Совместимо с PostgreSQL 13+ и Greenplum 6+ (на GP таблицы без `DISTRIBUTED BY`
распределяются hash по первой колонке).

---

## 🔗 Полная таблица связей между файлами (v2.0.0)

### Точки входа (тонкие оркестраторы)

| Файл | Строк | Что делает | Настраивается через |
|------|------:|-----------|-------------------|
| `gateway.py` | 132 | Сервер: каналы, Streamlit, FAISS preload, restart-loop | `project.json` (`channels.*`, `gateway`, `logging.db`) |
| `cli_agent.py` | 165 | REPL: ввод → `MessageBus` → `AgentLoop` | CLI-аргументы, `project.json` (`cli`) |
| `pg_agent_worker.py` | 310 | Legacy пакетный режим (НЕ через ApplicationContext) | `project.json` → `channels.postgres.dsn` |
| `streamlit_app.py` | 502 | Тонкий web-клиент (НЕ через ApplicationContext) | `project.json` → `channels.postgres`, `streamlit` |

### Bootstrap и сервисный слой

| Файл | Что делает |
|------|-----------|
| `lib/core/application_context.py` | ⭐ Единый bootstrap всех общих сервисов |
| `lib/core/agent_factory.py` | ⭐ Создание AgentLoop с хуками (ToolAudit + DatabaseLogging) |
| `lib/core/bus_factory.py` | ⭐ MessageBus + обёртки publish_inbound/outbound |
| `lib/services/config_service.py` | ⭐ Загрузка конфига, SETTINGS-аксессор, pre-resolve env, таймауты |
| `lib/services/session_storage.py` | ⭐ Выбор PGSessionManager / SessionManager |
| `lib/services/runtime_patcher.py` | ⭐ Все monkey-patch'и (ContextGovernor + _assemble_outbound) |
| `lib/services/channel_factory.py` | ⭐ ChannelManager + Redis/Postgres |
| `lib/services/transcription_service.py` | ⭐ openai/groq key/URL/language |
| `lib/services/subprocess_manager.py` | ⭐ Streamlit spawn + terminate/kill |
| `lib/services/preload_service.py` | ⭐ FAISS preload + audit_cache refresh |
| `lib/services/db_logging_service.py` | ⭐ Worker-поток, batch INSERT, JSONL fallback |
| `lib/services/db_logging_bus.py` | ⭐ Обёртки publish_inbound/outbound для логгера |
| `lib/cli/console_loop.py` | ⭐ REPL/typewriter/consume_outbound (вынесено из cli_agent.py) |
| `lib/cli/display_config.py` | ⭐ DisplayConfig |
| `lib/cli/hook_loader.py` | ⭐ Сканирование workspace/hooks/*.py |
| `lib/lifecycle/gateway_runner.py` | ⭐ Цикл с exponential backoff |
| `lib/lifecycle/shutdown_coordinator.py` | ⭐ LIFO graceful shutdown |
| `workspace/hooks/database_logging_hook.py` | ⭐ AgentHook для tool-событий + run_finished |

### Pre-existing (не тронуты рефакторингом)

| Файл | Что делает |
|------|-----------|
| `lib/session/pg_session_manager.py` | Хранение сессий в PostgreSQL (замена JSONL) |
| `lib/channels/postgres_channel.py` | Канал через таблицу conversation_messages |
| `lib/channels/redis_channel.py` | Канал через Redis-очереди (BRPOP/LPUSH) |
| `lib/services/audit_sync_service.py` | Синхронизация audit-таблиц из PG в in-memory DuckDB |
| `lib/services/audit_memory_store.py` | DuckDB-кеш + FAISS-индексы + publish-snapshot |
| `lib/services/cache_provider.py` | Интерфейс CacheProvider + SearchResult |
| `lib/services/cache_provider_impl.py` | Реализация кеша (PostgresDuckDbProvider) |
| `lib/services/text_splitter.py` | Чанкование текстов |
| `workspace/utils/db.py` | Глобальный singleton `configure(dsn)` + sync/async коннекторы |
| `workspace/skills/audit_analyzer/` | Навык: тонкий CLI поверх `lib/services` |

### Где что править

| Что нужно сделать | Файл |
|-----------------|------|
| Сменить модель/провайдера | `config.json` → `agents.defaults.model` |
| Настроить таймауты | `project.json` → секции `gateway`, `cli` или `streamlit` |
| Настроить подключение к БД | `project.json` → `channels.postgres` (`dsn`, `schema`, `table_name`) |
| Включить Redis-канал | `project.json` → `channels.redis.enabled` |
| Настроить навык | `project.json` → `skills.<имя>` |
| Добавить API-ключ | `.secrets.env` (провайдер-скоупинг формат) |
| Настроить БД-логирование | `project.json` → `logging.db` (`enabled`, `flush_interval_sec`, `batch_size`, `min_level`) |
| Изменить сервисный слой | `lib/services/<service>.py` (например, `db_logging_service.py`) |
| Изменить bootstrap | `lib/core/application_context.py` |
| Изменить lifecycle (backoff/shutdown) | `lib/lifecycle/gateway_runner.py` / `shutdown_coordinator.py` |
| Добавить канал связи | Написать класс унаследовав `BaseChannel`, подключить через `lib/services/channel_factory.py` |
| Добавить хук агента | Создать файл в `workspace/hooks/` с подклассом `AgentHook` |
| Добавить тест бенчмарка | YAML-файл в `benchmarks/items/` |
| Настроить Streamlit UI | `streamlit_app.py` |
| Изменить личность агента | `workspace/SOUL.md` |
| Дать инструкции агенту | `workspace/AGENTS.md` |
| Расширить навык `audit_analyzer` | `workspace/skills/audit_analyzer/scripts/` + `lib/services/audit_*` (кеш) |

---

## 🧪 Тестирование

```bash
# Юнит-тесты v2.0.0 сервисного слоя (не требуют БД)
python -m pytest tests/test_config_service.py tests/test_session_storage.py \
                    tests/test_runtime_patcher.py tests/test_transcription_service.py \
                    tests/test_channel_factory.py tests/test_subprocess_manager.py \
                    tests/test_preload_service.py tests/test_db_logging_service.py \
                    tests/test_hooks_database_logging.py tests/test_bus_factory.py \
                    tests/test_agent_factory.py tests/test_gateway_runner.py \
                    tests/test_shutdown_coordinator.py tests/test_console_loop.py \
                    tests/test_application_context.py -q

# Тесты воркеров (некоторые требуют БД)
python -m pytest tests/test_pg_session_manager.py tests/test_pg_agent_worker.py -q

# Юнит-тесты audit/кэша (sync+memory)
python -m pytest tests/test_audit_memory_store.py tests/test_audit_sync_service.py -q

# Полный набор (без БД; 701 passed после v2.0.0)
python -m pytest tests -q

# Сквозной тест навыка (требует живого PostgreSQL)
python workspace/skills/audit_analyzer/tests/e2e_test.py
```

E2E проверяет все режимы: predefined (реальный SQL по шаблонам), sql
(LLM → EXPLAIN → выполнение), vector (FAISS + Ollama embedding), а также
резолв параметров через семантический поиск.

**Новые test-файлы v2.0.0** (полный список в `README.md`):
- `test_application_context.py` — bootstrap и lifecycle
- `test_config_service.py` — pre-resolve env, таймауты, SETTINGS-аксессор
- `test_session_storage.py` — выбор PG/File/auto
- `test_runtime_patcher.py` — оба monkey-patch'а с fallback
- `test_db_logging_service.py` — worker, batch, JSONL fallback
- `test_bus_factory.py` — обёртки publish_inbound/outbound
- `test_console_loop.py` — REPL/typewriter/print_tool_events
- `test_gateway_runner.py` — exponential backoff
- `test_shutdown_coordinator.py` — LIFO graceful shutdown
- `test_subprocess_manager.py` — Streamlit spawn/terminate
- ... и т.д.

---

## 📝 Изменения и миграции

### 2026-08 — v2.0.0: ApplicationContext + сервисный слой (текущая)

- **`gateway.py` (696 → 132) и `cli_agent.py` (865 → 165 строк)** — тонкие
  оркестраторы. Вся инициализация вынесена в `lib/core/ApplicationContext`.
- **Новый сервисный слой** (`lib/services/`):
  - `ConfigService` — единая точка загрузки конфига, SETTINGS-аксессор,
    инъекция ключей, таймауты. **Pre-resolve `${PROVIDER_API_KEY}`** —
    автоматически достаёт ключ из `SETTINGS.providers.<name>.api_key` (туда
    `config.py` подставил значение из `.secrets.env`) и кладёт в `os.environ`
    ДО `_load_runtime_config`. Gateway больше НЕ требует
    `export MISTRAL_API_KEY=...` в shell.
  - `SessionStorageService` — выбор `PGSessionManager` / `SessionManager`
    (auto / postgres / file) с поддержкой `session_manager.json` override.
  - `RuntimePatcher` — оба monkey-patch'а (`ContextGovernor.normalize_tool_result`
    + `agent._assemble_outbound`) в одном классе с fallback при изменении API
    nanobot. Дубликат в cli_agent удалён.
  - `ChannelFactory` — `ChannelManager` + Redis/Postgres каналы +
    транскрипция.
  - `SubprocessManager` — Streamlit spawn + terminate/kill.
  - `PreloadService` — разделяет FAISS preload (gateway) и audit_cache
    refresh (cli).
  - `TranscriptionService` — openai/groq key/URL/language.
- **`DbLoggingService`** — структурированный журнал событий агента в
  PostgreSQL (таблица `gateway_logs`, см. `lib/services/sql/create_logs_table.sql`).
  Worker-поток, batch INSERT через `psycopg2.extras.execute_batch`,
  JSONL fallback при недоступности БД, `get_stats()` для мониторинга.
  Подключён через `BusFactory` (обёртки `publish_inbound`/`publish_outbound`)
  + `DatabaseLoggingHook` (AgentHook для tool-событий и run_finished).
- **Новые модули**:
  - `lib/core/application_context.py` — bootstrap
  - `lib/core/agent_factory.py`, `bus_factory.py` — фабрики
  - `lib/lifecycle/gateway_runner.py` — цикл с exponential backoff (1с → 30с)
  - `lib/lifecycle/shutdown_coordinator.py` — LIFO graceful shutdown
  - `lib/cli/console_loop.py`, `display_config.py`, `hook_loader.py` — вынесено из cli_agent.py
  - `workspace/hooks/database_logging_hook.py` — AgentHook для БД
- **Race-condition fix:** callbacks на `AuditSyncService` (`set_on_new_records_callback`
  + `set_on_sync_callback`) устанавливаются в `gateway.py:main()` ДО `ctx.start()`.
  Без этого worker-тред успевает сделать `initial_load` раньше → `AuditMemoryStore`
  пустой → `preload_vector_indexes` показывает "нет данных в кэше" несмотря на
  наличие строк в `oarb.audit_vectors`. Восстановлено отображение
  `✓ vector index 'audits_index' built in memory: 10 vectors`.
- **Graceful degradation:** если `psycopg2` не установлен или DSN пуст —
  `ApplicationContext.create()` создаётся, битый сервис остаётся `None`,
  gateway/cli работают без него.
- **`pg_agent_worker.py` и `streamlit_app.py` НЕ тронуты** — у них другая
  архитектура (legacy-воркер и тонкий web-клиент через PG-канал).
- **Тесты:** 701 unit-тестов (было 594, +107). Полный changelog: `README.md`
  (v2.0.0 секция) и ниже в этом документе («Изменения и миграции»).

### 2026-08 — Структура таблиц из PG + сверка удалений

- `AuditSyncService` собирает структуру таблиц из PG `information_schema`
  (+ `pg_description`): колонки, типы, NOT NULL, комментарии таблиц/колонок —
  и передаёт в store через `set_on_schema_callback` → `ensure_schema`.
- `AuditMemoryStore.ensure_schema()` создаёт таблицы с типами из PG (маппинг
  `_map_pg_type`), в т.ч. пустые; комментарии и исходные PG-типы сохраняются
  в `__nanobot_meta.__schema_meta` (входит в снимок и публикуется).
- `get_schema()` (store и `PostgresDuckDbProvider`) возвращает исходные PG-типы
  и комментарии — схема кеша совпадает с прямой PostgreSQL.
- `AuditMemoryStore.replace_records()` — полная перезапись содержимого таблицы
  (структура и типы сохраняются); `AuditSyncService` вызывает её каждые
  `full_resync_every` циклов — удалённые в PG строки уходят из кеша.

### 2026-08 — Gateway — владелец кеша навыка

- Создание и обновление файла кеша полностью перенесено в `gateway.py`;
  навык (CLI) про это больше не знает: удалены `--mode init` и `--force`.
- Новые сервисы в `lib/services`: `AuditMemoryStore` (in-memory DuckDB +
  FAISS, `publish()` атомарным снимком через temp + `os.replace`) и
  `AuditSyncService` (worker-поток, инкрементальный поллинг PG по track-колонке).
- `AuditMemoryStore.publish()` публикует снимок после каждого цикла синхронизации
  (`on_sync_callback`) и при завершении gateway; пропускает ещё не синхронизированные
  таблицы, не перезаписывает файл без изменений (флаг `_dirty`).
- `AuditSyncService` пишет журнал взаимодействий в `oarb.audit_interactions`
  (создаётся автоматически, `sync_write_table` / `poll_interval_sec` в project.json).
- При отсутствии файла кеша CLI завершается с `FileNotFoundError` и подсказкой
  запустить gateway.

### 2026-08 — Универсальный слой данных, чистка навыка

- Инфраструктура вынесена из навыка в `lib/services` (интерфейс
  `CacheProvider` + реализация `PostgresDuckDbProvider`, модульные функции
  загрузки/проверки кеша и эмбеддинга).
- Удалены промежуточные обёртки из навыка: `InMemoryDatabase`,
  `scripts/vector_mode.py` — CLI и агент работают с провайдером напрямую.
- Унифицирован интерфейс бэкенда запросов: `get_schema / query_sql / explain`
  (`QueryBackend`) — реализован и в `Database` (прямой PG), и в провайдере.
- Фоновая загрузка/свежесть кеша в `gateway.py` и `cli_agent.py` переведена
  на провайдера / модульные функции `lib/services`.
- Документация разработчика перенесена из навыка в корень: `DEVELOPMENT.md` +
  `sql/` (все DDL нужных таблиц).
- Индексация вынесена из навыка: `build_vectors.py` → корневой `tools/`,
  `text_splitter.py` → `lib/services/`; удалён legacy-мигратор
  `migrate_vectors_to_db.py`. Навык остался тонким CLI — создание индексов
  это инфраструктура, а не задача навыка.
- Удалены debug-скрипты `check_status.py` и `cache/query_audit.py`.

### 2026-07 — DuckDB-кеш

- Введён DuckDB-кеш навыка: `InMemoryDatabase` → `load_from_postgres()` /
  `check_stale()` (сравнение `MAX(updated_at)`), фоновый опрос свежести раз в час
  в `gateway.py` / `cli_agent.py`.
- Добавлен `--mode init` для ручного создания/обновления кеша.

### 2026-06 — Векторные индексы в PostgreSQL

- Векторные индексы мигрированы из `.faiss`-файлов в БД:
  `oarb.audit_vectors`, `oarb.vector_index_store`, `oarb.vector_index_config`
  (29.06.2026).

### 2026-05/06 — Ранняя история

- (05.2026) Первоначальная реализация навыка `db_analyzer` на FAISS-файлах
  (режимы predefined/sql/vector).
- (06.2026) Переименование `db_analyzer` → `audit_analyzer`, переход
  `asyncpg` → `psycopg2` (совместимость с Greenplum).
