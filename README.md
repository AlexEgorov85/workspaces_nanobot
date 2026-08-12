# nanobot — Personal AI Agent (Deployment)

Локальная инсталляция фреймворка **[nanobot](https://opencode.ai)** — персонального AI-агента, запущенного с **кастомными доработками**: PostgreSQL-каналы, Redis-интеграция, Streamlit UI, система бенчмарков и пользовательский навык audit_analyzer.

> **Имя агента:** Aura (🐈)  
> **Базовая модель:** `ministral-14b-2512` (Mistral)  
> **ОС:** Windows  
> **Язык:** Русский / English

---

## Быстрый старт

### 1. Установка зависимостей

```bash
# Виртуальное окружение (рекомендуется)
python -m venv .venv
.venv\Scripts\activate

# Фреймворк nanobot
pip install nanobot

# Зависимости проекта
pip install -r requirements.txt
```

### 2. Настройка окружения

Скопируйте шаблон `.secrets.env.example` в `.secrets.env`:

```bash
cp .secrets.env.example .secrets.env
```

Отредактируйте `.secrets.env` (он в `.gitignore`, не попадёт в репозиторий):

```ini
DATABASE_URL=postgresql://user:password@localhost:5432/nanobot

# providers: mistral
api_key=ваш_ключ_mistral

# Skills: audit_analyzer
llm_api_key=ваш_ключ_mistral
```

Все остальные настройки лежат в конфигурационных файлах (без дублирования):

| Файл | Что хранит |
|------|-----------|
| `config.json` | Настройки nanobot: агенты, провайдеры, каналы, инструменты, API, gateway (формат nanobot) |
| `project.json` | Настройки проекта: каналы `channels.*` (postgres, redis), навыки `skills.*`, `cli`, `gateway`, `streamlit`, `benchmark` |
| `.secrets.env` | Секреты (API-ключи, `DATABASE_URL`) — подставляются в конфиг через `${VAR}` |

### 3. База данных

Создайте таблицы сессий в PostgreSQL:

```bash
psql -d nanobot -f lib/session/sql/create_session_tables.sql
# Для Greenplum:
psql -d nanobot -f lib/session/sql/create_session_tables_gp.sql
```

Таблица канала (`conversation_messages`) и таблицы воркера (`agent_questions`, `agent_responses`) создаются автоматически.

Тестовые данные для канала:
```bash
psql -d nanobot -f lib/channels/sql/seed_messages.sql
```

### 4. Запуск

```bash
# CLI-агент (patched-режим с PostgreSQL)
python cli_agent.py -P -s dev

# Gateway + Streamlit UI
python gateway.py

# Пакетный обработчик
python pg_agent_worker.py --once
```

---

## Архитектура

```
                 ┌──────────────────────┐
                 │    config.json        │
                 │ (провайдеры, каналы, │
                 │  инструменты, API)    │
                 └──────────┬───────────┘
                            │
                 ┌──────────┴───────────┐
                 ▼                      ▼
         ┌─────────────┐       ┌──────────────┐
         │ project.json │       │ .secrets.env  │
         │ (channels.*,  │       │ (API-ключи,   │
         │  skills.*,   │       │  DATABASE_URL)│
         │  cli/gateway/ │       └──────┬───────┘
         │  streamlit,   │              │
         │  benchmark,   │              │
         │  logging.db)  │              │
         └──────┬───────┘              │
                │                      │
                └──────────┬───────────┘
                           │
                           ▼
               ┌────────────────────────┐
               │  config.py: SETTINGS     │   (глобал,
               │  + _resolve_env_refs     │    project.json + config.json + .secrets.env)
               └────────────┬───────────┘
                            │
                            ▼
              ┌──────────────────────────────┐
              │  ApplicationContext (Bootstrap) │
              │  Создаёт и связывает ВСЕ общие   │
              │  сервисы: Config, Sessions,      │
              │  Bus, Agent, DB-Logging, Audit   │
              └──────────┬─────────────────────┘
                         │
        ┌────────────────┼─────────────────────┐
        ▼                ▼                       ▼
   ┌────────┐   ┌────────────────┐       ┌─────────────┐
   │ gateway │   │ lib/services/  │       │  lib/core/   │
   │  (132)  │   │  Сервисный слой │       │ AgentFactory │
   │ тонкий  │   │ ConfigService  │       │ BusFactory   │
   │ оркестр.│   │ SessionStorage │       └─────────────┘
   └────────┘   │ ChannelFactory │
                │ SubprocessMgr  │
                │ PreloadService │
                │ RuntimePatcher │
                │ DbLoggingSvc   │
                │ TranscriptionSvc│
                └───────┬────────┘
                        │
                        ▼
                ┌──────────────────┐
                │  MessageBus       │
                │  (publish inbound │
                │   / outbound)     │
                └──────────────────┘

Каналы сообщений:
   PostgresChannel    RedisChannel    (опционально)
        │                  │
        ▼                  ▼
   PostgreSQL          Redis (inbox/outbox)
        │
        ▼
   Streamlit UI (поллинг conversation_messages, веб-чат)
```

**Ключевые компоненты:**

- **`config.py`** — собирает `SETTINGS` из `project.json + config.json + .secrets.env`, резолвит `${VAR}`. Глобальный singleton.
- **`lib/core/application_context.py:ApplicationContext`** — единая точка создания и связывания сервисов (`create`/`start`/`stop`). Использует graceful degradation — если БД недоступна, сервис просто не создаётся.
- **`lib/services/`** — переиспользуемые сервисы (см. таблицу ниже).
- **`lib/lifecycle/`** — `GatewayRunner` (цикл с backoff) + `ShutdownCoordinator` (LIFO graceful shutdown).
- **`lib/cli/`** — `DisplayConfig`, `hook_loader`, `console_loop` (вынесено из `cli_agent.py`).
- **`workspace/hooks/database_logging_hook.py`** — `AgentHook` для логирования tool-событий и run-level summary в БД.
- **Точки входа:** `gateway.py` (132 строки), `cli_agent.py` (165 строк), `pg_agent_worker.py` (310 строк, legacy, не трогали), `streamlit_app.py` (502 строки, отдельный клиент через PG-канал, не трогали).

Конфигурация:
- **`config.json`** — настройки nanobot: провайдеры, каналы, инструменты, API (формат nanobot)
- **`project.json`** — настройки проекта: каналы `channels.*`, навыки `skills.*`, `cli`/`gateway`/`streamlit`/`benchmark`/`logging.db`
- **`.secrets.env`** — секреты (API-ключи, `DATABASE_URL`) — в `.gitignore`

Порядок мержа в `config.py` (поздний источник перекрывает ранний): `project.json` → `config.json` → `.secrets.env` (секреты — наивысший приоритет). Значения вида `${VAR}` подставляются из `os.environ` (секреты — из `.secrets.env`).

**Pre-resolve env** (`ConfigService._pre_resolve_env_refs`): если в `config.json` есть `"apiKey": "${MISTRAL_API_KEY}"`, а env-переменная не задана (ключ лежит в `.secrets.env` через провайдер-скоупинг `# providers: mistral\napi_key=...`), сервис достаёт ключ из `SETTINGS.providers.mistral.api_key` и кладёт в `os.environ` ДО вызова `nanobot._load_runtime_config`. Это значит, что `MISTRAL_API_KEY` НЕ нужно экспортировать в shell — gateway стартует без `export MISTRAL_API_KEY=...`.

---

## Структура проекта

```
~/.nanobot/
├── README.md                       ← этот файл
├── config.json                     # Настройки nanobot (агенты, провайдеры, каналы, инструменты, API)
├── project.json                    # Настройки проекта (channels.*, skills.*, cli, gateway, streamlit, benchmark)
├── config.py                       # Сборка SETTINGS: project.json + config.json + .secrets.env
├── .secrets.env                    # Секреты (API-ключи, DATABASE_URL) — в .gitignore
├── .secrets.env.example            # Шаблон для .secrets.env
│
├── gateway.py                      # [Entry point] Gateway-сервер (тонкий оркестратор, 132 строки)
├── cli_agent.py                    # [Entry point] CLI-агент (тонкий оркестратор, 165 строк)
├── pg_agent_worker.py              # [Entry point] Пакетный обработчик PG→nanobot→PG (legacy, 310 строк)
├── streamlit_app.py                # [Entry point] Streamlit веб-чат (отдельный клиент, 502 строки)
│
├── lib/                            # Внутренняя реализация
│   ├── core/                        # ⭐ ApplicationContext + фабрики
│   │   ├── application_context.py  #   Единый bootstrap: create/start/stop, связывает все сервисы
│   │   ├── agent_factory.py        #   Создание AgentLoop с хуками (ToolAudit + DatabaseLogging)
│   │   └── bus_factory.py          #   MessageBus + опциональная обёртка publish_inbound/outbound для логгеров
│   │
│   ├── services/                    # ⭐ Сервисный слой (вынесено из gateway.py и cli_agent.py)
│   │   ├── config_service.py        #   Загрузка конфига, SETTINGS-аксессор, инъекция ключей, таймауты
│   │   ├── session_storage.py       #   Фабрика SessionManager / PGSessionManager (auto/postgres/file)
│   │   ├── runtime_patcher.py       #   Все monkey-patch'и (ContextGovernor + _assemble_outbound) в одном месте
│   │   ├── channel_factory.py      #   ChannelManager + Redis/Postgres каналы + транскрипция
│   │   ├── subprocess_manager.py    #   Streamlit spawn + graceful terminate/kill
│   │   ├── preload_service.py      #   FAISS preload (gateway) + audit_cache refresh (cli)
│   │   ├── transcription_service.py # openai/groq: API-ключ/URL/язык
│   │   ├── db_logging_service.py    # ⭐ Worker-поток, batch INSERT, JSONL fallback, get_stats()
│   │   ├── db_logging_bus.py        #   Обёртки publish_inbound/outbound для DbLoggingService
│   │   ├── audit_sync_service.py    #   [был] Синхронизация audit-таблиц из PG в in-memory DuckDB
│   │   ├── audit_memory_store.py    #   [был] DuckDB-кеш + FAISS-индексы + publish-snapshot
│   │   ├── cache_provider.py        #   [был] Интерфейс CacheProvider + SearchResult
│   │   ├── cache_provider_impl.py   #   [был] Реализация кеша (PostgresDuckDbProvider)
│   │   ├── text_splitter.py         #   [был] Чанкование текстов
│   │   └── sql/
│   │       └── create_logs_table.sql  # DDL для DbLoggingService (gateway_logs)
│   │
│   ├── cli/                         # ⭐ Вынесено из cli_agent.py
│   │   ├── console_loop.py          #   REPL + typewriter + consume_outbound
│   │   ├── display_config.py        #   DisplayConfig (show_reasoning/tool_calls/...)
│   │   └── hook_loader.py           #   Сканирование workspace/hooks/*.py для AgentHook-подклассов
│   │
│   ├── lifecycle/                   # ⭐ Цикл запуска и graceful shutdown
│   │   ├── gateway_runner.py        #   run_forever() с exponential backoff (1с → 30с)
│   │   └── shutdown_coordinator.py  #   LIFO-остановка сервисов, изоляция ошибок
│   │
│   ├── channels/
│   │   ├── postgres_channel.py     # Канал через таблицу conversation_messages
│   │   ├── redis_channel.py        # Канал через Redis-очереди (BRPOP/LPUSH)
│   │   └── sql/
│   │       └── seed_messages.sql   # Тестовые данные для PostgresChannel
│   │
│   ├── session/
│   │   ├── pg_session_manager.py   # Хранение сессий в PostgreSQL (замена JSONL)
│   │   └── sql/
│   │       ├── create_session_tables.sql    # DDL для session_meta / session_messages (PG)
│   │       └── create_session_tables_gp.sql # DDL для Greenplum 6.25
│
├── workspace/                      # Runtime-данные и хуки (на уровень выше lib/)
│   ├── hooks/
│   │   ├── tool_audit_hook.py      #   [был] Хук аудита вызовов инструментов
│   │   └── database_logging_hook.py # ⭐ AgentHook для логирования tool-событий + run_finished в БД
│
├── scripts/                        # Пустая директория для будущих скриптов (.gitkeep)
│
├── tools/                          # Инфраструктурные CLI-утилиты
│   └── build_vectors.py            #   Сборка векторных индексов (вне навыка)
│
├── benchmarks/                     # Система автоматического тестирования агента
│   ├── runner.py                   #   Запуск бенчмарков
│   ├── items/                      #   YAML-файлы с заданиями (simple, medium, hard)
│   │   └── _template.yaml          #   Шаблон для создания новых тестов
│   ├── loader.py / evaluator.py    #   Загрузка и оценка
│   ├── scorer.py / reporter.py     #   Подсчёт баллов и отчёты
│   ├── models.py                   #   Pydantic-модели
│   ├── hooks.py / db.py            #   Хуки и сохранение в БД
│   ├── results/runs/               #   Результаты прогонов
│   └── sql/                        #   DDL для БД бенчмарков
│       ├── create_benchmark_tables.sql      # Таблицы benchmark_runs, benchmark_results (PG)
│       └── create_benchmark_tables_gp.sql   # Версия для Greenplum
│
├── tests/                          # Unit-тесты (pytest)
│
├── workspace/
│   ├── AGENTS.md                   # Инструкции агенту (политики, heartbeat, cron)
│   ├── HEARTBEAT.md                # Периодические задачи (проверка каждые 30 мин)
│   ├── SOUL.md                     # Системная личность агента
│   ├── TOOLS.md                    # Описание инструментов
│   ├── USER.md                     # Профиль пользователя (Алексей)
│   │
│   ├── hooks/
│   │   └── tool_audit_hook.py      # Хук аудита вызовов инструментов
│   │
│   ├── utils/
│   │   ├── db.py                   # Единый коннектор PostgreSQL (sync + async)
│   │   └── session_file_store.py   # Файловое хранилище результатов сессий
│   │
│   ├── skills/
│   │   └── audit_analyzer/         # 📊 Анализ аудиторских проверок
│   │       ├── SKILL.md            #   Документация навыка
│   │       ├── audit_analyze.bat   #   Standalone-запуск (Windows)
│   │       ├── audit_analyze.sh    #   Standalone-запуск (Linux)
│   │       ├── scripts/            #   Модули режимов (cli.py, predefined.py, sql_mode.py, ...)
│   │       ├── cache/              #   DuckDB-кеш для предопределённых скриптов
│   │       └── tests/              #   Unit-тесты навыка
│   │
│   ├── prompts/                    # Prompt overrides (dream, evaluator)
│   ├── sessions/                   # JSONL-файлы сессий (fallback)
│   ├── memory/
│   │   ├── MEMORY.md               # Долговременная память
│   │   └── history.jsonl           # История взаимодействий
│   ├── data_store/cache/           # Кэш результатов инструментов
│   └── cron/jobs.json              # Cron-задачи (dream каждые 2ч)
│
├── requirements.txt                # Зависимости проекта
```

**Примечания:**
- `scripts/` — пустая директория для будущих скриптов (содержит только `.gitkeep`)
- `logs/` — создаётся при первом запуске для хранения логов
- `workspace/memory/history.jsonl` — история взаимодействий агента (файл существует)
- `workspace/memory/MEMORY.md` — файл долговременной памяти (существует)

---

## Компоненты

### 1. CLI Agent (`cli_agent.py`)

Интерактивный терминальный интерфейс. Два режима:

| Режим | Флаг | Хранилище | Хуки |
|-------|------|-----------|------|
| **vanilla** | (по умолчанию) | JSONL-файлы | ToolAuditHook |
| **patched** | `--patched / -P` | PGSessionManager (или file) | ToolAuditHook + из `workspace/hooks/` |

**Примеры:**
```bash
python cli_agent.py                           # vanilla
python cli_agent.py -P                        # patched, авто-storage
python cli_agent.py -P -s my-session          # patched + именованная сессия
python cli_agent.py -P -S postgres            # patched, принудительно PostgreSQL
```

### 2. Gateway (`gateway.py`)

Долгоживущий сервер с каналами связи. После рефакторинга `gateway.py` — **тонкий оркестратор** (132 строки): вся инициализация — в `ApplicationContext`.

Что `gateway.py` делает:
- `ApplicationContext.create(...)` — собирает конфиг, сессии, агента, аудит-сервисы, БД-логирование
- Регистрирует callbacks на `AuditSyncService` **ДО** `ctx.start()` (race-condition fix — иначе FAISS preload видит "нет данных")
- `ChannelFactory.create_all()` — `ChannelManager` + Redis + Postgres каналы + транскрипция
- `SubprocessManager.spawn_streamlit()` — запуск Streamlit UI на `:8501` (логи в `logs/streamlit.log`)
- `GatewayRunner().run_forever()` — главный цикл с exponential backoff (1с → 30с) при падении
- Корректный shutdown: `channels.stop_all()` → Streamlit `terminate_all()` → `agent.close_mcp()/stop()` → `agent.sessions.flush_all()`

**Запуск:** `python gateway.py`

### 3. ApplicationContext (`lib/core/application_context.py`)

Единый bootstrap всех общих сервисов. Используется обеими точками входа (gateway + cli). Создаёт и связывает:
- `ConfigService` + `RuntimeConfig` (из `config.json`)
- `SessionStorageService` → `PGSessionManager`/`SessionManager`
- `DbLoggingService` (если `enable_db_logging=True` и есть DSN)
- `AuditSyncService` + `AuditMemoryStore` (если `enable_audit=True`)
- `MessageBus` (с обёрткой под логгеры если есть `DbLoggingService`)
- `AgentLoop` (через `AgentFactory`) с `ToolAuditHook` + `DatabaseLoggingHook`
- `RuntimePatcher.apply_all()` — все monkey-patch'и в одном месте
- `PreloadService`, `TranscriptionService`

**Флаги:** `enable_db_logging`, `enable_audit`, `enable_cron`, `storage_override`. Graceful degradation: если БД недоступна, контекст создаётся, битый сервис остаётся `None`.

**Публичный API:**
```python
ctx = ApplicationContext.create(script_dir, workspace_dir, enable_db_logging=True)
ctx.start()           # запустить фоновые сервисы
# ... работа ...
ctx.stop()            # LIFO graceful shutdown через ShutdownCoordinator
```

### 4. DbLoggingService (`lib/services/db_logging_service.py`)

Структурированный журнал событий агента в PostgreSQL (таблица `gateway_logs`). Worker-поток с **единственным** psycopg2-соединением, **неблокирующая** очередь (`log_*` → `put_nowait`), батч-вставка через `psycopg2.extras.execute_batch`. При недоступности БД — лог пишется в JSONL-файл (`fallback_path`).

**Методы:** `log_inbound`, `log_outbound` (с `kind="outbound_final"` / `"outbound_delta"`), `log_tool_call`, `log_tool_result` (с `latency_ms`), `log_error`. Все вызовы `O(1)` — `True` (в очереди) или `False` (очередь полная).

**`get_stats()`** для мониторинга: `written`, `failed`, `queue_size`, `fallback_written`, `connected`, `last_error`.

**Подключение к агенту:** через `BusFactory(inbound_logger=..., outbound_logger=...)` (bus-обёртки) + `DatabaseLoggingHook` (AgentHook для tool-событий и run-level summary).

**DDL:** `lib/services/sql/create_logs_table.sql` (UUID, JSONB, индексы по `timestamp`/`session_id`/`event_type`/`level`).

**Полезные SQL-запросы для аудита:**
```sql
-- Последние 10 событий
SELECT timestamp, level, event_type, session_id, summary
FROM gateway_logs ORDER BY timestamp DESC LIMIT 10;

-- Статистика по типам событий за час
SELECT event_type, COUNT(*) FROM gateway_logs
WHERE timestamp > NOW() - INTERVAL '1 hour'
GROUP BY event_type ORDER BY 2 DESC;

-- Самые медленные инструменты за сутки
SELECT payload->>'tool' AS tool,
       AVG((metadata->>'latency_ms')::float) AS avg_ms,
       COUNT(*) AS calls
FROM gateway_logs
WHERE event_type = 'tool_result' AND timestamp > NOW() - INTERVAL '24 hours'
GROUP BY payload->>'tool' ORDER BY avg_ms DESC;
```

### 5. Postgres Worker (`pg_agent_worker.py`)

Пакетный обработчик: читает `agent_questions` из БД → отправляет агенту → пишет ответы в `agent_responses`.

Поддерживает сессии: вопросы с одинаковым `session_id` используют общую историю.

**Режимы:**
- `--once` — один батч и выход
- `--interval N` (по умолч. 30с) — непрерывный цикл

### 6. PGSessionManager (`lib/session/pg_session_manager.py`)

Замена штатного `SessionManager`. Хранит сессии в двух таблицах:
- **session_meta** — метаданные сессии (ключ, даты, заголовок)
- **session_messages** — сообщения (seq, role, content, tool_calls, media, reasoning)

При недоступности БД — graceful degradation на JSONL-файлы.

### 7. PostgresChannel (`lib/channels/postgres_channel.py`)

Канал через таблицу `conversation_messages`:
- Поллинг новых сообщений (status='pending')
- Потоковая запись reasoning в `metadata.reasoning`
- Автоматическая разблокировка зависших сообщений (retry 3 раза)
- Медиафайлы: кодирование в data URL → хранение в БД → декодирование на стороне агента

### 8. RedisChannel (`lib/channels/redis_channel.py`)

Канал через Redis-списки:
- **Inbox:** BRPOP из `nanobot:inbox`
- **Outbox:** LPUSH в `nanobot:outbox:{chat_id}`
- Формат JSON повторяет InboundMessage/OutboundMessage

### 9. Streamlit UI (`streamlit_app.py`)

Тонкий веб-клиент:
- Пользователь → INSERT в `conversation_messages` (status='pending')
- Блокирующий поллинг ответа с отображением reasoning в реальном времени
- Чистый чат-интерфейс

### 10. Skills (пользовательские навыки)

| Навык | Назначение | Точка входа |
|-------|-----------|-------------|
| `audit_analyzer` | Анализ аудиторских проверок — SQL-отчёты, векторный поиск, LLM-генерация SQL | Standalone: `audit_analyze.bat` / `audit_analyze.sh`<br>CLI: `workspace/skills/audit_analyzer/scripts/cli.py` (4 режима) |

**Режимы `audit_analyzer`:**

| Режим | Описание | Пример |
|-------|----------|--------|
| `predefined` | Готовые SQL-скрипты с параметрами | `--mode predefined --script analytics_by_year_month --params year=2024` |
| `sql` | LLM генерирует SQL по текстовому запросу | `--mode sql --query "топ-10 объектов по нарушениям"` |
| `vector` | Семантический поиск по векторному индексу | `--mode vector --query "финансовые нарушения" --index-name violations_index --top-k 3` |
| `init` | Загрузка DuckDB-кеша из PostgreSQL | `--mode init --force` (принудительная перезагрузка) |

**Примеры standalone-запуска:**

```bash
# Windows (PowerShell / cmd)
audit_analyze.bat --mode predefined --script analytics_by_year_month --params year=2024
audit_analyze.bat --mode sql --query "топ-10 объектов по нарушениям"
audit_analyze.bat --mode vector --query "финансовые нарушения" --index-name violations_index --top-k 3

# Linux
./audit_analyze.sh --mode predefined --script analytics_by_year_month --params '{"year": 2024}'
./audit_analyze.sh --mode sql --query "топ-10 объектов по нарушениям"
./audit_analyze.sh --mode vector --query "финансовые нарушения" --index-name violations_index --threshold 0.5
```

Параметры векторного поиска:
- `--top-k N` — вернуть ровно N лучших результатов (по умолчанию 5)
- `--threshold X` — вернуть все результаты выше порога схожести X (0.0–1.0); если задан, `--top-k` игнорируется

### 11. Benchmarks (`benchmarks/`)

Автоматическая оценка качества агента:
- YAML-определения тестов (difficulty 1–10)
- Типы: `single` (один вопрос) и `multi_step` (последовательность шагов)
- Скоринг по ключевым словам, файлам, использованным инструментам, LLM-судье
- Сохранение результатов в JSON/Markdown/PostgreSQL
- Сравнение прогонов (`--compare`)

**Создание своих тестов:**

Скопируйте шаблон `benchmarks/items/_template.yaml` и заполните:

```yaml
- id: "my-test-id"
  name: "Human readable name"
  difficulty: 5          # 1-10: 1-3 simple, 4-7 medium, 8-10 hard
  category: "general"    # e.g. basic, data_analysis, coding, research
  type: "single"         # single | multi_step
  new_session: true      # true = fresh session, false = continue conversation
  question: "Ask the agent something"
  max_iterations: 30
  timeout: 60
  expect:
    tools: ["exec", "glob"]
    keywords_include: ["expected", "keyword"]
    match_type: "keyword"  # keyword | functional | llm_judge
```

**Таблицы БД для бенчмарков:**

```bash
# PostgreSQL
psql -d nanobot -f benchmarks/sql/create_benchmark_tables.sql

# Greenplum
psql -d nanobot -f benchmarks/sql/create_benchmark_tables_gp.sql
```

Создаются таблицы:
- `benchmark_runs` — метаданные прогона (дата, конфиг, версия агента)
- `benchmark_results` — результаты по каждому тесту (id, score, детали)

**Результаты прогонов** сохраняются в `benchmarks/results/runs/`.

---

## База данных

### Таблицы сессий (`session_meta`, `session_messages`)
```sql
-- Создание: psql -d <db> -f lib/session/sql/create_session_tables.sql
```

### Таблица канала (`conversation_messages`)
Создаётся автоматически или вручную. Схема:
```
id, chat_id, user_id, role, content, media, buttons,
metadata (JSONB), reply_to, status, created_at, updated_at
```

### Таблицы worker'а (`agent_questions`, `agent_responses`)
Определяются в `pg_agent_worker.py`. Пакетный режим.

### Таблицы бенчмарков (`benchmark_runs`, `benchmark_results`)
```bash
# Создание: psql -d <db> -f benchmarks/sql/create_benchmark_tables.sql
```

---

## Векторные индексы (в PostgreSQL)

**v1.5.0:** Векторные индексы перенесены из файлов `.faiss` в таблицы PostgreSQL.

### Таблицы

| Таблица | Назначение |
|---------|-----------|
| `oarb.audit_vectors` | Векторные эмбеддинги текстов аудитов и нарушений |
| `oarb.vector_index_store` | Хранилище векторных индексов (FAISS в бинарном формате) |
| `oarb.vector_index_config` | Конфигурация индексов (параметры, метаданные) |

### Миграция со старых файловых индексов

Не требуется: мигратор из `.faiss`-файлов удалён (legacy). Новые индексы
создаются сразу в БД через `tools/build_vectors.py`.

### Создание векторных индексов

```bash
# 1. Таблицы (см. sql/ в корне проекта)
psql -d nanobot -f sql/create_audit_vectors_table_gp.sql
psql -d nanobot -f sql/create_vector_index_config_gp.sql

# 2. Сборка индексов (инфраструктурная утилита, вне навыка)
python tools/build_vectors.py --full-rebuild
```

### Поиск через векторный режим

См. раздел [Skills](#8-skillsпользовательские-навыки) — режим `vector`.

---

## Тестирование

Проект содержит **701 unit-тест** в директории `tests/` (по состоянию после рефакторинга v2.0.0).

### Запуск тестов

```bash
# Все тесты
pytest tests/ -q

# Тесты конкретного модуля
pytest tests/test_db_logging_service.py -v
pytest tests/test_application_context.py -v

# С покрытием по lib/ (новый сервисный слой)
pytest tests/ --cov=lib --cov-report=term-missing
```

### Структура тестов (по сервисному слою)

| Файл | Что тестирует |
|------|--------------|
| `test_application_context.py` | ⭐ `ApplicationContext` — bootstrap всех сервисов, lifecycle |
| `test_agent_factory.py` | ⭐ `AgentFactory` — создание AgentLoop с хуками |
| `test_bus_factory.py` | ⭐ `BusFactory` — MessageBus + обёртки логгеров |
| `test_config_service.py` | ⭐ `ConfigService` — загрузка конфига, pre-resolve env, таймауты |
| `test_session_storage.py` | ⭐ `SessionStorageService` — выбор PG/File/auto |
| `test_runtime_patcher.py` | ⭐ `RuntimePatcher` — оба monkey-patch'а, fallback |
| `test_transcription_service.py` | ⭐ `TranscriptionService` — openai/groq |
| `test_channel_factory.py` | ⭐ `ChannelFactory` — Redis/Postgres каналы |
| `test_subprocess_manager.py` | ⭐ `SubprocessManager` — Streamlit spawn/terminate |
| `test_preload_service.py` | ⭐ `PreloadService` — FAISS + audit_cache |
| `test_db_logging_service.py` | ⭐ `DbLoggingService` — worker, batch, fallback |
| `test_hooks_database_logging.py` | ⭐ `DatabaseLoggingHook` — AgentHook для tool-событий |
| `test_gateway_runner.py` | ⭐ `GatewayRunner` — exponential backoff |
| `test_shutdown_coordinator.py` | ⭐ `ShutdownCoordinator` — LIFO graceful shutdown |
| `test_console_loop.py` | ⭐ REPL/typewriter/print_tool_events |
| `test_cli_agent.py` | CLI-агент (vanilla/patched режимы) |
| `test_gateway.py` | Gateway-оркестратор (обновлён под ApplicationContext) |
| `test_pg_session_manager.py` | PGSessionManager (сессии в БД) |
| `test_postgres_channel.py` | PostgresChannel (поллинг, streaming) |
| `test_redis_channel.py` | RedisChannel (BRPOP/LPUSH) |
| `test_benchmarks_*.py` | Система бенчмарков (loader, evaluator, scorer, reporter, runner) |
| `test_utils_db.py` | Утилиты БД (sync/async коннекторы) |
| `test_hooks_tool_audit_hook.py` | Хук аудита инструментов |

---

## Миграции и изменения

### v2.0.0 (текущая)

- **ApplicationContext + сервисный слой:** `gateway.py` (696 → 132 строк) и `cli_agent.py` (865 → 165 строк) теперь — тонкие оркестраторы. Вся инициализация вынесена в `lib/core/ApplicationContext` и переиспользуемые сервисы в `lib/services/`, `lib/core/`, `lib/lifecycle/`, `lib/cli/`.
- **DbLoggingService:** структурированный журнал событий агента в PostgreSQL (`gateway_logs`). Worker-поток, batch INSERT, JSONL fallback при недоступности БД, `get_stats()`. Подключён через `BusFactory` (обёртки `publish_inbound`/`publish_outbound`) + `DatabaseLoggingHook` (AgentHook для tool-событий).
- **Pre-resolve `${VAR}`:** `ConfigService._pre_resolve_env_refs()` автоматически подставляет `MISTRAL_API_KEY` (и другие `*_API_KEY`) из `SETTINGS.providers.<name>.api_key` (туда `config.py` уже подставил значение из `.secrets.env`). **Gateway больше НЕ требует `export MISTRAL_API_KEY=...` в shell.**
- **RuntimePatcher:** оба monkey-patch'а (`ContextGovernor.normalize_tool_result` и `agent._assemble_outbound`) — в одном классе, с fallback при изменении API nanobot. Дублирование в gateway и cli устранено.
- **Race-condition fix:** callbacks на `AuditSyncService` (`set_on_new_records_callback` + `set_on_sync_callback`) устанавливаются в `gateway.py:main()` ДО `ctx.start()`, иначе worker-тред успевает сделать `initial_load` раньше — `AuditMemoryStore` остаётся пустым, и `preload_vector_indexes` показывает "нет данных в кэше". Восстановлено отображение `✓ vector index 'audits_index' built in memory: 10 vectors` и т.п.
- **Graceful degradation:** если `psycopg2` не установлен или DSN пуст — `ApplicationContext.create()` создаётся, битый сервис остаётся `None`, gateway работает без него (с предупреждением).
- **`pg_agent_worker.py` и `streamlit_app.py` НЕ тронуты** — у них другая архитектура (legacy-воркер и тонкий web-клиент через PG-канал). `streamlit_app.py` — отдельный клиент, работающий через polling таблицы `conversation_messages`; он НЕ должен использовать `ApplicationContext` (это бы создало второго in-process агента).
- **Тесты:** 701 unit-тест (было 594, +107 новых). Все существующие тесты обновлены под новые модули. Pre-existing тесты на `audit_memory_store.py`/`audit_sync_service.py` не трогали (они были в working tree до рефакторинга).

Подробный план: `REFACTORING_PLAN.md` (16 шагов, все отмечены как выполненные).

### v1.5.0

- **Векторные индексы в БД:** Переход с файлов `.faiss` на таблицы PostgreSQL (`audit_vectors`, `vector_index_store`, `vector_index_config`)
- **Удалённые навыки:** `data-analyzer`, `html_presentation_generator` (устарели)
- **Новый режим `init`:** Загрузка DuckDB-кеша для `audit_analyzer`

### v1.4.0

- **Переход на psycopg2:** Замена DB-API драйвера для совместимости с Greenplum
- **Переименование навыка:** `db_analyzer` → `audit_analyzer`
- **Standalone-скрипты:** Добавлены `audit_analyze.bat` / `audit_analyze.sh`

### Конфигурация

- **Переход с `.env` на `project.json` + `.secrets.env`:** Разделение публичных настроек и секретов
- **Мерж конфигов:** `project.json` → `config.json` → `.secrets.env` (последний перекрывает)

---

## Ключевые связи между файлами (v2.0.0)

Точки входа (тонкие оркестраторы) — обе идут через `ApplicationContext`:

| Файл | Строк | Что делает | Настраивается через |
|------|------:|-----------|-------------------|
| `gateway.py` | 132 | Сервер: каналы, Streamlit, FAISS preload, restart-loop | `project.json` (`channels.*`, `gateway`, `logging.db`) |
| `cli_agent.py` | 165 | REPL: ввод → `MessageBus` → `AgentLoop` | CLI-аргументы, `project.json` (`cli`) |
| `pg_agent_worker.py` | 310 | Legacy пакетный режим (НЕ через ApplicationContext) | `project.json` → `channels.postgres.dsn` |
| `streamlit_app.py` | 502 | Тонкий web-клиент (НЕ через ApplicationContext) | `project.json` → `channels.postgres`, `streamlit` |

Bootstrap и сервисный слой:

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

Прочее:

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

---

## Поток сообщения (gateway + Streamlit)

```
Streamlit         PostgresChannel        Agent
   │                    │                  │
   │ INSERT (pending)   │                  │
   │───────────────────>│                  │
   │                    │ UPDATE processing│
   │                    │──────────────────>│
   │                    │ reasoning_delta   │
   │                    │<─────────────────│
   │ (poll: reasoning)  │                  │
   │<───────────────────│                  │
   │                    │  финальный ответ  │
   │                    │<─────────────────│
   │ (poll: completed)  │                  │
   │<───────────────────│                  │
```

---

## Разработка

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

### Запуск

```bash
# CLI-агент
python cli_agent.py -P -s dev

# Gateway + Streamlit
python gateway.py

# Бенчмарки
python benchmarks/runner.py --tags simple
python benchmarks/runner.py --compare runs/run1 runs/run2

# Пакетный воркер
python pg_agent_worker.py --once
```

---

## Зависимости

- **nanobot** — фреймворк (`pip install nanobot`)
- **psycopg2-binary** — PostgreSQL
- **redis>=5.0** — Redis-канал
- **streamlit** — веб-чат
- **loguru** — логирование
- **httpx** — HTTP-клиент (асинхронный)
- **duckdb** — встраиваемая аналитическая БД (audit_analyzer)
- **faiss-cpu, numpy, sentence-transformers** — векторный поиск (audit_analyzer)
- **PyYAML** — конфиги бенчмарков
- **requests** — HTTP-клиент (синхронный)
- **anthropic, openai** — опциональные LLM-провайдеры
- **pytest** — запуск unit-тестов (опционально, для разработки)

---

## Лицензия

[Укажите лицензию, если применимо]
