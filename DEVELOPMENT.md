# Разработка nanobot: audit_analyzer и универсальный слой данных

> **Назначение:** техническая документация для разработчиков. Описывает архитектуру
> навыка `audit_analyzer`, универсальный инфраструктурный слой данных
> (`lib/services`), управление DuckDB-кешем, векторными индексами и SQL-скрипты
> для развёртывания нужных таблиц.
> Пользовательская документация навыка — [`workspace/skills/audit_analyzer/SKILL.md`](workspace/skills/audit_analyzer/SKILL.md).
> Обзор проекта — [`README.md`](README.md).

---

## 📋 Оглавление

1. [Архитектура](#архитектура)
2. [Структура проекта](#структура-проекта)
3. [Универсальный слой данных lib/services](#универсальный-слой-данных-libservices)
4. [Конфигурация навыка](#конфигурация-навыка)
5. [CLI навыка: режимы](#cli-навыка-режимы)
6. [Жизненный цикл кеша (gateway / cli_agent)](#жизненный-цикл-кеша-gateway--cli_agent)
7. [Векторная индексация](#векторная-индексация)
8. [SQL-скрипты: создание таблиц](#sql-скрипты-создание-таблиц)
9. [Тестирование](#тестирование)
10. [Изменения и миграции](#изменения-и-миграции)

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
                        │  • load_cache_from_postgres / check_stale    │
                        │  • get_embedding (Ollama)                    │
                        └───────────────┬──────────────────────────────┘
                                        │ query_sql / get_schema / explain
                                        │ search_vector / preload_indexes
                ┌───────────────────────┼────────────────────────────┐
                ▼                       ▼                            ▼
        DuckDB-кеш (файл)        FAISS-индексы (store в БД)      эмбеддинг
        oarb.* (аналитика)       oarb.audit_vectors →           Ollama /api/embed
                                 oarb.vector_index_store

        Потребители:  gateway.py        cli_agent.py        навык CLI (scripts/cli.py)
                      (фон: preload,    (фон: preload,      (режимы predefined/sql/vector/init)
                       refresh, stale)   refresh, stale)
```

**Потоки данных**

- `--mode init` / `refresh()` — копирует таблицы из PostgreSQL в DuckDB-файл (кеш).
- `--mode predefined` / `sql` — запросы выполняются по кешу (или напрямую по PG,
  если кеш выключен). Единый интерфейс бэкенда: `get_schema / query_sql / explain`.
- `--mode vector` — семантический поиск по FAISS-индексу: провайдер загружает
  индекс из `oarb.vector_index_store` (BYTEA), при промахе пересобирает из
  `oarb.audit_vectors`, эмбеддинг запроса получает через Ollama.

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
├── lib/services/                         # универсальный слой данных
│   ├── cache_provider.py                 #   интерфейс CacheProvider + SearchResult
│   ├── cache_provider_impl.py            #   PostgresDuckDbProvider + фабрика и модульные функции
│   └── text_splitter.py                  #   чанкование текстов для индексаторов
│
├── gateway.py                            # долгоживущий сервер: фоновые задачи кеша
├── cli_agent.py                          # CLI-агент: фоновая загрузка/свежесть кеша
├── config.py                             # SETTINGS (project.json + .secrets.env)
├── project.json                          # конфигурация (skills.audit_analyzer.*)
│
└── workspace/skills/audit_analyzer/      # навык: тонкий CLI поверх провайдера
    ├── SKILL.md                          #   пользовательская документация
    ├── audit_analyze.bat / .sh           #   точки входа
    ├── scripts/
    │   ├── cli.py                        #   парсинг аргументов, маршрутизация режимов
    │   ├── skill_config.py               #   конфиг из SETTINGS + build_cache_provider()
    │   ├── database.py                   #   Database (прямой PG, fallback) + QueryBackend
    │   ├── sql_mode.py                   #   режим sql: LLM → SQL → EXPLAIN → выполнение
    │   ├── predefined_mode.py            #   режим predefined: готовые SQL-шаблоны
    │   ├── predefined.py                 #   резолв параметров (+ векторный поиск по source)
    │   ├── scripts_registry.py           #   ScriptDefinition / ParamDefinition / реестр
    │   ├── llm.py                        #   LLM-клиент (OpenAI-compatible HTTP)
    │   └── output.py                     #   форматирование JSON-вывода
    └── tests/
        └── e2e_test.py                   #   сквозной тест навыка (нужна живая БД)
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
набор настроек читает `gateway.py::_build_audit_provider()` и индексатор
`tools/build_vectors.py`.

---

## ⚙️ Конфигурация навыка

Секция `skills.audit_analyzer` в `project.json`:

| Ключ | Назначение | По умолчанию |
|------|-----------|-------------|
| `llm_provider` / `llm_model` / `llm_api_base` | LLM для генерации SQL | `mistral` / `mistral-large-latest` / API Mistral |
| `llm_max_tokens` / `llm_temperature` | Параметры генерации | `8192` / `0.1` |
| `db_schema` | Схема с таблицами аудита | `oarb` |
| `db_tables` | Таблицы, доступные агенту | `audit_reports, audits, report_items, violations` |
| `schema_cache` | Файловый кеш схемы (enabled/path/ttl_seconds) | выключен (резерв) |
| `in_memory_enabled` | Включить DuckDB-кеш | `true` |
| `in_memory_engine` | Движок кеша | `duckdb` |
| `in_memory_cache_path` | Путь к файлу кеша (отн. навыка) | `cache/audit_cache.duckdb` |
| `embedding_base_url` | Ollama `/api/embed` | `http://localhost:11434/api/embed` |
| `embedding_model` | Модель эмбеддинга | `mxbai-embed-large:latest` |
| `embedding_dimension` | Размерность вектора | `1024` |
| `mode_vector_db_table` | Таблица сырых векторов (источник индекса) | `oarb.audit_vectors` |
| `mode_vector_store_table` | Таблица сериализованных FAISS-индексов | `oarb.vector_index_store` |
| `cli_default_mode` | Режим по умолчанию (резерв; CLI требует `--mode`) | `predefined` |
| `cli_max_retries` | Ретраи LLM при генерации SQL | `3` |
| `cli_timeout_sec` | Таймаут LLM | `60` |

DSN подключается через `channels.postgres.dsn` в `project.json` /
`DATABASE_URL` в `.secrets.env` (`utils.db.resolve_dsn()`). Навык собственного
DSN не хранит.

---

## 🚀 CLI навыка: режимы

Точка входа: `workspace/skills/audit_analyzer/audit_analyze.bat` (или
`.sh`), либо `python scripts/cli.py`.

```
audit_analyze --mode {predefined,sql,vector,init} [опции]
```

| Режим | Назначение | Ключевые флаги |
|-------|-----------|----------------|
| `predefined` | Выполнение готовых SQL-шаблонов из реестра | `--script`, `--params` |
| `sql` | Генерация SELECT через LLM по текстовому запросу | `--query`, `--context` |
| `vector` | Семантический поиск по FAISS-индексу | `--query`, `--index-name`, `--top-k`, `--threshold`, `--vector-index` |
| `init` | Загрузка DuckDB-кеша из PostgreSQL | `--force` |

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

# init — принудительная перезагрузка кеша
audit_analyze --mode init --force
```

**Как выбирается бэкенд запросов:** если `in_memory_enabled: true` — CLI строит
провайдера (`build_cache_provider()`), открывает DuckDB-кеш и работает по нему;
иначе — `Database` (прямой PostgreSQL).

**Векторный поиск в predefined:** строковые параметры с
`validation.vector_source` (например, `violation_code`, `auditee_entity`,
`audit_type`) резолвятся через семантический поиск — провайдер подставляет
лучшее совпадение из индекса `{source}_index`.

---

## 🔄 Жизненный цикл кеша (gateway / cli_agent)

Оба процесса при старте поднимают DuckDB-кеш и следят за его свежестью,
используя универсальный провайдер / модульные функции из `lib/services`.

**gateway.py** (секция 11):

- `_build_audit_provider()` — строит провайдера из `SETTINGS.skills.audit_analyzer`
  (возвращает `None`, если кеш и векторные индексы не настроены).
- `_preload_audit_cache(provider)` — если кеш-файл свежий (< 1 ч) — пропуск,
  иначе `provider.refresh()` (копирует таблицы из PG в DuckDB).
- `_preload_vector_indexes(provider)` — `provider.preload_indexes()` прогревает
  FAISS-индексы в память.
- `_background_audit_cache_refresh(provider)` — каждый час `provider.check_stale()`
  сверяет `MAX(updated_at)` таблиц в PG с метаданными кеша; при изменениях —
  `provider.refresh()`.

**cli_agent.py** — аналогично, но использует модульные функции напрямую:
`load_cache_from_postgres()` и `check_cache_stale()`.

Свежесть определяется по таблице `__cache_meta` внутри DuckDB-файла
(`max_updated_at` на таблицу), сравнением с `SELECT MAX(updated_at)`
в канонической БД.

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

## 🧪 Тестирование

```bash
# Юнит-тесты инфраструктуры и агента (не требуют БД)
python -m pytest tests/test_gateway.py tests/test_cli_agent.py -q

# Сквозной тест навыка (требует живого PostgreSQL)
python workspace/skills/audit_analyzer/tests/e2e_test.py
```

E2E проверяет все режимы: predefined (реальный SQL по шаблонам), sql
(LLM → EXPLAIN → выполнение), vector (FAISS + Ollama embedding), а также
резолв параметров через семантический поиск.

---

## 📝 Изменения и миграции

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

### 2026-06 — Векторные индексы в PostgreSQL

- Векторные индексы мигрированы из `.faiss`-файлов в БД:
  `oarb.audit_vectors`, `oarb.vector_index_store`, `oarb.vector_index_config`.
- Введён `--mode init` для загрузки DuckDB-кеша и фоновая перезагрузка кеша
  по свежести (`MAX(updated_at)`).

### 2025 — Ранняя история

- Переименование `db_analyzer` → `audit_analyzer`, переход на `psycopg2`.
- Первоначальная реализация на FAISS-файлах (режимы predefined/sql/vector).
