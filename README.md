# nanobot — Personal AI Agent (Deployment)

Локальная инсталляция фреймворка **[nanobot](https://opencode.ai)** — персонального AI-агента, запущенного с **кастомными доработками**: PostgreSQL-каналы, Redis-интеграция, Streamlit UI, система бенчмарков и три пользовательских навыка.

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

# Для навыка data-analyzer (дополнительно)
pip install -r workspace\skills\data-analyzer\requirements.txt
```

### 2. Настройка окружения

Создайте файл `.secrets.env` в корне проекта (он в `.gitignore`, не попадёт в репозиторий):

```ini
# Skills: audit_analyzer
llm_api_key=ваш_ключ_mistral
```

Настройки подключения к БД и каналам — в `.env` (уже в репозитории, отредактируйте под себя):

```ini
# PostgreSQL
dsn=postgresql://user:password@localhost:5432/nanobot

# Redis (опционально, отключён по умолчанию)
enabled=false
```

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
- **`config.json`** — провайдеры, каналы, инструменты, API (формат nanobot)
- **`.env`** — настройки подключения, навыков, режимов (читается `config.py`)
- **`.secrets.env`** — секреты (API-ключи) — в `.gitignore`

---

## Структура проекта

```
~/.nanobot/
├── README.md                       ← этот файл
├── config.json                     # Центральная конфигурация nanobot
├── config.py                       # Загрузчик .env → AttrDict SETTINGS
├── .env                            # Настройки (PG, Redis, Gateway, CLI, навыки)
├── .secrets.env                    # Секреты (API-ключи) — в .gitignore
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
│   └── session/
│       ├── pg_session_manager.py   # Хранение сессий в PostgreSQL (замена JSONL)
│       └── sql/
│           ├── create_session_tables.sql    # DDL для session_meta / session_messages (PG)
│           └── create_session_tables_gp.sql # DDL для Greenplum 6.25
│
├── scripts/                        # Диагностические и вспомогательные скрипты
│
├── benchmarks/                     # Система автоматического тестирования агента
│   ├── runner.py                   #   Запуск бенчмарков
│   ├── items/                      #   YAML-файлы с заданиями (simple, medium, hard)
│   ├── loader.py / evaluator.py    #   Загрузка и оценка
│   ├── scorer.py / reporter.py     #   Подсчёт баллов и отчёты
│   ├── models.py                   #   Pydantic-модели
│   ├── hooks.py / db.py            #   Хуки и сохранение в БД
│   └── sql/                        #   DDL для БД бенчмарков
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
│   │   ├── audit_analyzer/         # 📊 Анализ аудиторских проверок
│   │   ├── data-analyzer/          # 📁 Анализ файлов (LLM + Pandas)
│   │   └── html_presentation_generator/  # 📊 HTML-презентации из Markdown
│   │
│   ├── sessions/                   # JSONL-файлы сессий (fallback)
│   ├── memory/
│   │   ├── MEMORY.md               # Долговременная память
│   │   └── history.jsonl           # История взаимодействий
│   ├── data_store/cache/           # Кэш результатов инструментов
│   └── cron/jobs.json              # Cron-задачи (dream каждые 2ч)
│
├── requirements.txt                # Зависимости проекта
└── history/                        # История CLI
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
| `audit_analyzer` | Анализ аудиторских проверок — SQL, векторный поиск, LLM-генерация | `scripts/cli.py` (3 режима) |
| `data-analyzer` | Анализ файлов: `llm_text` (семантический) и `pandas` (табличный) | `scripts/analyze.py` |
| `html_presentation_generator` | Генерация HTML-презентаций из Markdown с Mermaid | `tool.py` (агентский tool) |

### 9. Benchmarks (`benchmarks/`)

Автоматическая оценка качества агента:
- YAML-определения тестов (difficulty 1–10)
- Типы: `single` (один вопрос) и `multi_step` (последовательность шагов)
- Скоринг по ключевым словам, файлам, использованным инструментам
- Сохранение результатов в JSON/Markdown/PostgreSQL
- Сравнение прогонов (`--compare`)

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

---

## Ключевые связи между файлами

| Файл | Импортирует | Настраивается через |
|------|-----------|-------------------|
| `gateway.py` | `lib.channels.postgres_channel`, `lib.channels.redis_channel`, `lib.session.pg_session_manager`, `utils.session_file_store`, `utils.db` (lazy), `hooks.tool_audit_hook` | `.env`, `config.json` |
| `cli_agent.py` | `lib.session.pg_session_manager`, `hooks.tool_audit_hook` | CLI-аргументы, `.env`, `config.json` |
| `pg_agent_worker.py` | `workspace.utils.db` | `.env` |
| `streamlit_app.py` | `workspace.utils.db` | `.env` |
| `lib/session/pg_session_manager.py` | `workspace.utils.db` | `.env` |
| `lib/channels/postgres_channel.py` | `workspace.utils.db` | `.env` |
| `lib/channels/redis_channel.py` | `redis.asyncio` | `.env` |
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
| Настроить таймауты | `.env` → секции `gateway`, `cli` или `streamlit` |
| Настроить подключение к БД | `.env` → `dsn`, `schema` |
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
- **pandas, openpyxl, tiktoken** — анализ данных (data-analyzer)
- **Markdown, beautifulsoup4** — генерация HTML-презентаций
- **PyYAML** — конфиги бенчмарков
- **requests** — HTTP-клиент (синхронный)
- **anthropic, openai** — опциональные LLM-провайдеры
