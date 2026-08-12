# nanobot — Personal AI Agent (Deployment)

Локальная инсталляция фреймворка **[nanobot-ai](https://github.com/HKUDS/nanobot)** (PyPI: `nanobot-ai`) — персонального AI-агента, запущенного с **кастомными доработками**: PostgreSQL-каналы, Redis-интеграция, Streamlit UI, система бенчмарков и пользовательский навык audit_analyzer.

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

Таблица канала (`conversation_messages`) создаётся автоматически.

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
```

---

## Архитектура

```mermaid
flowchart TB
    %% Конфигурация
    subgraph CFG["Конфигурация"]
        CONFIG["config.json<br>провайдеры, каналы, API"]
        PROJECT["project.json<br>channels.*, skills.*,<br>cli/gateway/streamlit,<br>benchmark, logging.db"]
        SECRETS[".secrets.env<br>(в .gitignore)"]
    end

    %% SETTINGS
    CFG -->|"merger"| SETTINGS["config.py: SETTINGS<br>project.json → config.json → .secrets.env"]

    %% Bootstrap
    SETTINGS --> CTX["ApplicationContext<br>(lib/core/)"]
    CTX -->|"create/start/stop"| SVC["Сервисный слой<br>(lib/services/)"]
    CTX --> CORE["Фабрики<br>lib/core/<br>agent_factory, bus_factory"]
    CTX --> LIFE["Lifecycle<br>lib/lifecycle/<br>gateway_runner,<br>shutdown_coordinator"]

    %% Сервисы
    subgraph SERVICES["lib/services/ (v2.0.0)"]
        CFG_SVC["config_service"]
        SESS_SVC["session_storage"]
        CHAN_SVC["channel_factory"]
        PATCHER["runtime_patcher"]
        LOG_SVC["db_logging_service"]
        TRANS_SVC["transcription_service"]
        SUB_SVC["subprocess_manager"]
        PRELOAD["preload_service"]
    end

    SVC --> CFG_SVC & SESS_SVC & CHAN_SVC & PATCHER & LOG_SVC & TRANS_SVC & SUB_SVC & PRELOAD

    %% Точки входа
    GATEWAY["gateway.py\nтонкий оркестратор"]
    CLI["cli_agent.py\nтонкий оркестратор"]
    CTX --> GATEWAY
    CTX --> CLI

    %% Шина
    BUS["MessageBus<br>(publish inbound/outbound)"]
    CTX --> BUS
    BUS --> AGENT["AgentLoop<br>(+ ToolAuditHook + DatabaseLoggingHook)"]

    %% Pre-existing (не через ApplicationContext)
    subgraph LEGACY["Pre-existing точки входа (НЕ через ApplicationContext)"]
        STREAMLIT["streamlit_app.py\nweb-клиент"]
    end

    %% Каналы
    PG["PostgreSQL"]
    REDIS["Redis<br>(опционально)"]
    BUS --> PG
    BUS --> REDIS
    PG --> STREAMLIT

    %% Стили
    classDef v2 fill:#fff3cd,stroke:#d39e00,stroke-width:2px
    classDef legacy fill:#f8d7da,stroke:#c82333
    classDef infra fill:#d1ecf1,stroke:#0c5460

    class CTX,CORE,LIFE,SVC,LOG_SVC,PATCHER v2
    class STREAMLIT legacy
    class CFG,SETTINGS,BUS,PG,REDIS,AGENT infra
```

**Поток инициализации:** конфиги (3 файла) → `config.py` собирает `SETTINGS` → `ApplicationContext.create()` инициализирует и связывает все общие сервисы → `MessageBus` → `AgentLoop` с хуками → `gateway.py`/`cli_agent.py` (тонкие оркестраторы) запускают каналы и lifecycle.

Подробности по сервисам и таблица связей — в **[DEVELOPMENT.md](DEVELOPMENT.md)**.

---

## Структура проекта

Краткий обзор. **Полное дерево с описанием каждого модуля** (lib/core/, lib/services/, lib/cli/, lib/lifecycle/, workspace/skills/audit_analyzer/ и т.д.) — в [DEVELOPMENT.md → Структура проекта](DEVELOPMENT.md#структура-проекта).

```
nanobot/
├── README.md               ← этот файл
├── DEVELOPMENT.md          ← техническая документация
├── CHANGELOG.md            ← история релизов (keep-a-changelog)
│
├── config.json             # Настройки nanobot (агенты, провайдеры, каналы, API, gateway)
├── project.json            # Настройки проекта (channels.*, skills.*, cli, gateway, logging.db)
├── config.py               # Сборка SETTINGS: project.json + config.json + .secrets.env
│
├── gateway.py              #  v2.0.0: 132 строки, тонкий оркестратор
├── cli_agent.py            #  v2.0.0: 165 строк, тонкий оркестратор
├── streamlit_app.py        # [отдельный клиент] поллинг conversation_messages
│
├── lib/                    #  v2.0.0: сервисный слой (core/services/cli/lifecycle)
├── workspace/              # Runtime-данные, хуки, skills/, memory/
├── tests/                  # 701 unit-тест
├── benchmarks/             # YAML-тесты, runner, scorer, reporter
├── tools/                  # Инфраструктурные CLI-утилиты (build_vectors.py)
├── requirements.txt
```

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

### 5. PGSessionManager (`lib/session/pg_session_manager.py`)

Замена штатного `SessionManager`. Хранит сессии в двух таблицах:
- **session_meta** — метаданные сессии (ключ, даты, заголовок)
- **session_messages** — сообщения (seq, role, content, tool_calls, media, reasoning)

При недоступности БД — graceful degradation на JSONL-файлы.

### 6. PostgresChannel (`lib/channels/postgres_channel.py`)

Канал через таблицу `conversation_messages`:
- Поллинг новых сообщений (status='pending')
- Потоковая запись reasoning в `metadata.reasoning`
- Автоматическая разблокировка зависших сообщений (retry 3 раза)
- Медиафайлы: кодирование в data URL → хранение в БД → декодирование на стороне агента

### 7. TranscriptionService (`lib/services/transcription_service.py`)

Сервис для транскрибации аудио в текст. Поддерживает:
- OpenAI Whisper
- Groq Whisper
- Локальные модели (опционально)

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

См. раздел [Skills](#10-skillsпользовательские-навыки) — режим `vector`.

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
| `test_application_context.py` |  `ApplicationContext` — bootstrap всех сервисов, lifecycle |
| `test_agent_factory.py` |  `AgentFactory` — создание AgentLoop с хуками |
| `test_bus_factory.py` |  `BusFactory` — MessageBus + обёртки логгеров |
| `test_config_service.py` |  `ConfigService` — загрузка конфига, pre-resolve env, таймауты |
| `test_session_storage.py` |  `SessionStorageService` — выбор PG/File/auto |
| `test_runtime_patcher.py` |  `RuntimePatcher` — оба monkey-patch'а, fallback |
| `test_transcription_service.py` |  `TranscriptionService` — openai/groq |
| `test_channel_factory.py` |  `ChannelFactory` — Redis/Postgres каналы |
| `test_subprocess_manager.py` |  `SubprocessManager` — Streamlit spawn/terminate |
| `test_preload_service.py` |  `PreloadService` — FAISS + audit_cache |
| `test_db_logging_service.py` |  `DbLoggingService` — worker, batch, fallback |
| `test_hooks_database_logging.py` |  `DatabaseLoggingHook` — AgentHook для tool-событий |
| `test_gateway_runner.py` |  `GatewayRunner` — exponential backoff |
| `test_shutdown_coordinator.py` |  `ShutdownCoordinator` — LIFO graceful shutdown |
| `test_console_loop.py` |  REPL/typewriter/print_tool_events |
| `test_cli_agent.py` | CLI-агент (vanilla/patched режимы) |
| `test_gateway.py` | Gateway-оркестратор (обновлён под ApplicationContext) |
| `test_pg_session_manager.py` | PGSessionManager (сессии в БД) |
| `test_postgres_channel.py` | PostgresChannel (поллинг, streaming) |
| `test_redis_channel.py` | RedisChannel (BRPOP/LPUSH) |
| `test_benchmarks_*.py` | Система бенчмарков (loader, evaluator, scorer, reporter, runner) |
| `test_utils_db.py` | Утилиты БД (sync/async коннекторы) |
| `test_hooks_tool_audit_hook.py` | Хук аудита инструментов |

---

## Запуск

```bash
# CLI-агент
python cli_agent.py -P -s dev

# Gateway + Streamlit
python gateway.py

# Бенчмарки
python benchmarks/runner.py --tags simple
python benchmarks/runner.py --compare runs/run1 runs/run2
```

## Документация

- **[DEVELOPMENT.md](DEVELOPMENT.md)** — техническая документация: архитектура
  (включая v2.0.0 service layer, `ApplicationContext`, race-condition fix),
  описание компонентов, audit_analyzer, жизненный цикл кеша, векторная
  индексация, SQL-скрипты, **где что править**, changelog, **полная таблица
  связей между файлами** (lib/core/, lib/services/, lib/cli/, lib/lifecycle/).

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

## Лицензия

MIT License
