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
                    │  (провайдеры, каналы, │
                    │   инструменты, API)   │
                    └──────────┬───────────┘
                               │
          ┌────────────────────┼────────────────────┐
          ▼                    ▼                    ▼
   ┌─────────────┐    ┌──────────────┐    ┌──────────────────┐
   │  cli_agent   │    │   gateway    │    │  pg_agent_worker │
   │  (терминал)  │    │  (сервер)    │    │  (пакетный режим)│
   └─────────────┘    └──────┬───────┘    └──────────────────┘
                             │
                    ┌────────┴────────┐
                    ▼                 ▼
             ┌──────────┐     ┌──────────────┐
             │  Redis   │     │ PostgreSQL /  │
             │  Channel │     │ Greenplum     │
             └──────────┘     └──────┬───────┘
                                     │
                            ┌────────┴────────┐
                            ▼                 ▼
                     ┌────────────┐    ┌──────────────┐
                     │ Streamlit  │    │ PGSession    │
                     │ UI (веб)   │    │ Manager      │
                     └────────────┘    └──────────────┘
```

Конфигурация:
- **`config.json`** — настройки nanobot: провайдеры, каналы, инструменты, API (формат nanobot)
- **`project.json`** — настройки проекта: каналы `channels.*`, навыки `skills.*`, `cli`/`gateway`/`streamlit`/`benchmark`
- **`.secrets.env`** — секреты (API-ключи, `DATABASE_URL`) — в `.gitignore`

Порядок мержа в `config.py` (поздний источник перекрывает ранний): `project.json` → `config.json` → `.secrets.env` (секреты — наивысший приоритет). Значения вида `${VAR}` подставляются из `os.environ` (секреты — из `.secrets.env`).

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
├── gateway.py                      # [Entry point] Gateway-сервер
├── cli_agent.py                    # [Entry point] CLI-агент (терминальный режим)
├── pg_agent_worker.py              # [Entry point] Пакетный обработчик PG→nanobot→PG
├── streamlit_app.py                # [Entry point] Streamlit веб-чат
│
├── lib/                            # Внутренняя реализация
│   ├── channels/
│   │   ├── postgres_channel.py     # Канал через таблицу conversation_messages
│   │   ├── redis_channel.py        # Канал через Redis-очереди (BRPOP/LPUSH)
│   │   └── sql/
│   │       └── seed_messages.sql   # Тестовые данные для PostgresChannel
│   ├── session/
│   │   ├── pg_session_manager.py   # Хранение сессий в PostgreSQL (замена JSONL)
│   │   └── sql/
│   │       ├── create_session_tables.sql    # DDL для session_meta / session_messages (PG)
│   │       └── create_session_tables_gp.sql # DDL для Greenplum 6.25
│   └── services/                    # Универсальный слой данных (навыки, gateway)
│       ├── cache_provider.py        #   Интерфейс CacheProvider + SearchResult
│       ├── cache_provider_impl.py   #   PostgresDuckDbProvider (DuckDB-кеш + FAISS)
│       └── text_splitter.py         #   Чанкование текстов для индексаторов
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

Долгоживущий сервер с каналами связи. Запускает:
- **MessageBus** — центральная шина сообщений
- **PGSessionManager** (или JSONL) — хранилище сессий
- **PostgresChannel** — приём/отправка через БД
- **RedisChannel** — приём/отправка через Redis
- **Streamlit UI** — веб-чат на `:8501`
- **ToolAuditHook** — мониторинг вызовов инструментов
- Monkey-patch `_normalize_tool_result` — сохранение больших результатов в `data_store/`
- Monkey-patch `agent._assemble_outbound` — инъекция tool audit в metadata исходящих сообщений

Автоматический restart с exponential backoff (1с → 30с) при падении.

**Запуск:** `python gateway.py`

### 3. Postgres Worker (`pg_agent_worker.py`)

Пакетный обработчик: читает `agent_questions` из БД → отправляет агенту → пишет ответы в `agent_responses`.

Поддерживает сессии: вопросы с одинаковым `session_id` используют общую историю.

**Режимы:**
- `--once` — один батч и выход
- `--interval N` (по умолч. 30с) — непрерывный цикл

### 4. PGSessionManager (`lib/session/pg_session_manager.py`)

Замена штатного `SessionManager`. Хранит сессии в двух таблицах:
- **session_meta** — метаданные сессии (ключ, даты, заголовок)
- **session_messages** — сообщения (seq, role, content, tool_calls, media, reasoning)

При недоступности БД — graceful degradation на JSONL-файлы.

### 5. PostgresChannel (`lib/channels/postgres_channel.py`)

Канал через таблицу `conversation_messages`:
- Поллинг новых сообщений (status='pending')
- Потоковая запись reasoning в `metadata.reasoning`
- Автоматическая разблокировка зависших сообщений (retry 3 раза)
- Медиафайлы: кодирование в data URL → хранение в БД → декодирование на стороне агента

### 6. RedisChannel (`lib/channels/redis_channel.py`)

Канал через Redis-списки:
- **Inbox:** BRPOP из `nanobot:inbox`
- **Outbox:** LPUSH в `nanobot:outbox:{chat_id}`
- Формат JSON повторяет InboundMessage/OutboundMessage

### 7. Streamlit UI (`streamlit_app.py`)

Тонкий веб-клиент:
- Пользователь → INSERT в `conversation_messages` (status='pending')
- Блокирующий поллинг ответа с отображением reasoning в реальном времени
- Чистый чат-интерфейс

### 8. Skills (пользовательские навыки)

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

### 9. Benchmarks (`benchmarks/`)

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

Проект содержит **75+ unit-тестов** в директории `tests/`.

### Запуск тестов

```bash
# Все тесты
pytest tests/ -v

# Тесты конкретного модуля
pytest tests/test_pg_session_manager.py -v
pytest tests/test_benchmarks_runner.py -v

# С покрытием
pytest tests/ --cov=. --cov-report=html
```

### Структура тестов

| Файл | Что тестирует |
|------|--------------|
| `test_cli_agent.py` | CLI-агент (vanilla/patched режимы) |
| `test_gateway.py` | Gateway, MessageBus, каналы |
| `test_pg_session_manager.py` | PGSessionManager (сессии в БД) |
| `test_postgres_channel.py` | PostgresChannel (поллинг, streaming) |
| `test_redis_channel.py` | RedisChannel (BRPOP/LPUSH) |
| `test_benchmarks_*.py` | Система бенчмарков (loader, evaluator, scorer, reporter, runner) |
| `test_utils_db.py` | Утилиты БД (sync/async коннекторы) |
| `test_hooks_tool_audit_hook.py` | Хук аудита инструментов |

---

## Миграции и изменения

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

## Ключевые связи между файлами

| Файл | Импортирует | Настраивается через |
|------|-----------|-------------------|
| `gateway.py` | `lib.channels.postgres_channel`, `lib.channels.redis_channel`, `lib.session.pg_session_manager`, `utils.session_file_store`, `utils.db` (lazy), `hooks.tool_audit_hook` | `project.json` (`channels.*`, `gateway`), `config.json` |
| `cli_agent.py` | `lib.session.pg_session_manager`, `hooks.tool_audit_hook` | CLI-аргументы, `project.json` (`cli`), `config.json` |
| `pg_agent_worker.py` | `workspace.utils.db` | `project.json` → `channels.postgres.dsn` |
| `streamlit_app.py` | `workspace.utils.db` | `project.json` → `channels.postgres`, `streamlit` |
| `lib/session/pg_session_manager.py` | `workspace.utils.db` | `project.json` |
| `lib/channels/postgres_channel.py` | `workspace.utils.db` | `project.json` → `channels.postgres` |
| `lib/channels/redis_channel.py` | `redis.asyncio` | `project.json` → `channels.redis` |
| `workspace/utils/db.py` | `config` (SETTINGS) | `configure(dsn)` — глобальный singleton |

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
| Добавить API-ключ | `.secrets.env` |
| Добавить канал связи | Написать класс унаследовав `BaseChannel`, подключить в `gateway.py` |
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
