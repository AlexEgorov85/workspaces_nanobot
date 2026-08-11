# Audit Analyzer Skill — Техническая документация для разработчиков

> **Назначение:** Внутренняя документация для разработчиков навыка `audit_analyzer`.  
> Описывает архитектуру, режимы инициализации, работу с кешем DuckDB, управление векторными индексами в БД и служебные скрипты.  
> Для пользовательской документации см. [`SKILL.md`](./SKILL.md).  
> **Служебные файлы:** SQL-скрипты, утилиты миграции и вспомогательные модули перемещены в директорию [`DEVELOPMENT/`](./DEVELOPMENT/).

---

## 📋 Оглавление

1. [Архитектура навыка](#архитектура-навыка)
2. [Режимы работы CLI](#режимы-работы-cli)
3. [Режим `init`: Загрузка DuckDB кеша](#режим-init-загрузка-duckdb-кеша)
4. [Векторные индексы в PostgreSQL](#векторные-индексы-в-postgresql)
5. [Скрипт `build_vectors.py`](#скрипт-build_vectorspy)
6. [In-Memory кеш DuckDB](#in-memory-кеш-duckdb)
7. [Миграции и изменения](#миграции-и-изменения)
8. [Отладка и логирование](#отладка-и-логирование)
9. [Тестирование](#тестирование)

---

## 🏗 Архитектура навыка

```
┌─────────────────────────────────────────────────────────────┐
│                     audit_analyzer                          │
├─────────────────────────────────────────────────────────────┤
│  CLI Entry Point (cli.py)                                   │
│  ├── Режим: predefined → PredefinedScriptRunner             │
│  ├── Режим: sql → Direct SQL Executor                       │
│  ├── Режим: vector → Vector Search + LLM Context            │
│  └── Режим: init → DuckDB Cache Loader                      │
├─────────────────────────────────────────────────────────────┤
│  Core Components:                                           │
│  • DuckDB In-Memory Manager                                 │
│  • PostgreSQL Vector Index Reader                           │
│  • LLM Context Builder                                      │
│  • Script Registry (predefined scripts)                     │
├─────────────────────────────────────────────────────────────┤
│  Data Layer:                                                │
│  • PostgreSQL (oarb.audit_vectors, oarb.vector_index_store) │
│  • DuckDB (in-memory cache)                                 │
│  • Local FS (опционально: .parquet, .json кеш)              │
└─────────────────────────────────────────────────────────────┘
```

**Точка входа:** `scripts/cli.py`

**Standalone-скрипты:**
- Windows: `audit_analyze.bat`
- Linux/macOS: `audit_analyze.sh`

**Служебные скрипты разработчика:**
- Векторная индексация: [`DEVELOPMENT/build_vectors.py`](./DEVELOPMENT/build_vectors.py)
- Миграция данных: [`DEVELOPMENT/migrate_vectors_to_db.py`](./DEVELOPMENT/migrate_vectors_to_db.py)
- Работа с БД: [`DEVELOPMENT/database.py`](./DEVELOPMENT/database.py)
- Сплиттер текста: [`DEVELOPMENT/text_splitter.py`](./DEVELOPMENT/text_splitter.py)

---

## 🚀 Режимы работы CLI

| Режим | Назначение | Основной класс/модуль |
|-------|-----------|----------------------|
| `predefined` | Выполнение заранее написанных SQL-скриптов из реестра | `PredefinedScriptRunner` |
| `sql` | Прямое выполнение пользовательского SQL-запроса | `DirectSQLExecutor` |
| `vector` | Векторный поиск по аудитам + генерация ответа через LLM | `VectorSearchEngine` |
| `init` | **Инициализация**: загрузка данных из PostgreSQL в DuckDB in-memory кеш | `DuckDBCacheLoader` |

### Примеры запуска

```bash
# Режим predefined
python cli.py --mode predefined --script analytics_by_year_month --params year=2024,month=12

# Режим sql
python cli.py --mode sql --query "SELECT COUNT(*) FROM audits WHERE year=2024"

# Режим vector
python cli.py --mode vector --query "финансовые нарушения в бюджетных учреждениях" --top-k 5 --threshold 0.75

# Режим init (только при старте агента)
python cli.py --mode init --cache-path /tmp/audit_cache.duckdb
```

---

## 🔄 Режим `init`: Загрузка DuckDB кеша

### Назначение

Режим `init` используется **только при поднятии агента** для предварительной загрузки данных из PostgreSQL в in-memory базу DuckDB. Это ускоряет последующие запросы в режимах `predefined` и `sql`, так как DuckDB работает быстрее PostgreSQL для аналитических запросов на небольших данных.

### Когда используется

- При старте нанобота (если в конфиге включен `in_memory.enabled: true`)
- При ручном обновлении кеша (например, после массового обновления данных в PostgreSQL)

### Параметры режима

| Параметр | Описание | По умолчанию |
|----------|---------|-------------|
| `--cache-path` | Путь к файлу кеша DuckDB (опционально, для persistence) | In-memory (без файла) |
| `--batch-size` | Размер пакета для загрузки данных | 10000 |
| `--tables` | Список таблиц для загрузки (через запятую) | Все таблицы из конфига |
| `--force` | Принудительная перезапись существующего кеша | false |

### Алгоритм работы

1. Подключение к PostgreSQL по параметрам из `.secrets.env`
2. Чтение списка таблиц из конфига (`duckdb.tables`)
3. Для каждой таблицы:
   - Выполнение `SELECT * FROM <table>` (или с фильтрами, если указаны)
   - Создание соответствующей таблицы в DuckDB
   - Пакетная вставка данных
4. (Опционально) Сохранение кеша в файл по `--cache-path`
5. Логирование статистики: количество строк, время загрузки

### Пример использования в коде агента

```python
# В main.py или session manager при инициализации сессии
if config.audit_analyzer.in_memory.enabled:
    subprocess.run([
        sys.executable,
        "scripts/cli.py",
        "--mode", "init",
        "--cache-path", config.audit_analyzer.in_memory.cache_path
    ])
```

### Конфигурация

В `project.json` или `config.json`:

```json
{
  "audit_analyzer": {
    "in_memory": {
      "enabled": true,
      "cache_path": "/var/cache/nanobot/audit_cache.duckdb",
      "auto_init_on_start": true,
      "tables": ["audits", "violations", "objects"]
    }
  }
}
```

---

## 🗂 Векторные индексы в PostgreSQL

### Миграция v1.5.0

До версии 1.5.0 векторные индексы хранились в файлах `.faiss`. Начиная с v1.5.0, все индексы мигрированы в PostgreSQL для:
- Централизованного управления
- Поддержки транзакций
- Упрощения бэкапов
- Совместного доступа из нескольких инстансов

### Таблицы БД

#### `oarb.audit_vectors`

Основная таблица с векторными представлениями аудиторских записей.

```sql
CREATE TABLE oarb.audit_vectors (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    audit_id BIGINT NOT NULL,
    content_hash VARCHAR(64) UNIQUE NOT NULL,
    vector VECTOR(768) NOT NULL,  -- или другой размерность в зависимости от модели
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_audit_vectors_vector ON oarb.audit_vectors USING ivfflat (vector vector_cosine_ops);
CREATE INDEX idx_audit_vectors_audit_id ON oarb.audit_vectors (audit_id);
```

#### `oarb.vector_index_store`

Реестр доступных векторных индексов (мета-информация).

```sql
CREATE TABLE oarb.vector_index_store (
    index_name VARCHAR(128) PRIMARY KEY,
    description TEXT,
    model_name VARCHAR(64) NOT NULL,
    vector_dimension INT NOT NULL,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    config JSONB DEFAULT '{}'
);
```

#### `oarb.vector_index_config`

Конфигурация параметров индексации и поиска.

```sql
CREATE TABLE oarb.vector_index_config (
    index_name VARCHAR(128) REFERENCES oarb.vector_index_store(index_name),
    param_key VARCHAR(64) NOT NULL,
    param_value TEXT NOT NULL,
    PRIMARY KEY (index_name, param_key)
);
```

### Параметры поиска

Передаются через CLI в режиме `vector`:

| Параметр | Описание | По умолчанию |
|----------|---------|-------------|
| `--top-k` | Количество результатов для возврата | 5 |
| `--threshold` | Порог схожести (0.0–1.0) | 0.7 |
| `--index-name` | Имя индекса (из `oarb.vector_index_store`) | `default_audit_index` |

**Пример:**
```bash
python cli.py --mode vector --query "нарушения закупок" --top-k 10 --threshold 0.8 --index-name procurement_index
```

---

## 🛠 Скрипт `build_vectors.py`

Мощный инструмент для управления векторными индексами.

**Расположение:** [`DEVELOPMENT/build_vectors.py`](./DEVELOPMENT/build_vectors.py)

### Команды

| Команда | Назначение |
|---------|-----------|
| `--full-rebuild` | Полная перестройка индекса с удалением старых данных |
| `--check` | Проверка целостности индекса (сравнение с исходными данными) |
| `--status` | Отображение статуса индекса (количество записей, дата обновления) |
| `--dry-run` | Тестовый запуск без записи в БД |
| `--index-name <name>` |指定 конкретный индекс для операции |
| `--batch-size <N>` | Размер пакета для обработки | 1000 |
| `--parallel <N>` | Количество параллельных потоков | 4 |

### Примеры использования

```bash
# Полная перестройка индекса по умолчанию
python build_vectors.py --full-rebuild

# Перестройка конкретного индекса
python build_vectors.py --full-rebuild --index-name procurement_index

# Проверка целостности
python build_vectors.py --check --index-name default_audit_index

# Статус всех индексов
python build_vectors.py --status

# Тестовый запуск (без записи в БД)
python build_vectors.py --full-rebuild --dry-run --batch-size 100
```

### Алгоритм `--full-rebuild`

1. Чтение всех аудиторских записей из PostgreSQL
2. Генерация эмбеддингов через настроенную LLM-модель
3. Вычисление хеша контента для дедупликации
4. Пакетная вставка в `oarb.audit_vectors`
5. Обновление мета-информации в `oarb.vector_index_store`
6. Пересчет индексов IVFFlat/HNSW

---

## 💾 In-Memory кеш DuckDB

### Архитектура

```
PostgreSQL (источник)
     ↓
[Режим init]
     ↓
DuckDB In-Memory (кеш)
     ↓
[Режимы predefined / sql]
     ↓
Быстрые аналитические запросы
```

### Преимущества

- **Скорость:** DuckDB оптимизирован для OLAP-запросов
- **Изоляция:** Запросы не нагружают основную БД
- **Гибкость:** Возможность работать с локальными файлами (Parquet, JSON)

### Ограничения

- Данные актуальны только на момент инициализации
- Требует дополнительной памяти (размер кеша ≈ размер выбранных таблиц)
- Не подходит для частых обновлений (требуется переинициализация)

### Рекомендации по использованию

| Сценарий | Рекомендация |
|----------|-------------|
| Маленький датасет (< 1M строк) | Включить `in_memory.enabled: true` |
| Частые обновления данных | Использовать прямой режим (без кеша) |
| Несколько инстансов агента | Общий кеш через файл (`cache_path`) |
| Ограниченная память | Отключить кеш, использовать PostgreSQL напрямую |

---

## 🔄 Миграции и изменения

### v1.5.0 (Текущая)

- ✅ **Векторные индексы мигрированы в PostgreSQL**
  - Удалена зависимость от FAISS файлов
  - Добавлены таблицы: `audit_vectors`, `vector_index_store`, `vector_index_config`
  - Поддержка нескольких индексов одновременно

- ✅ **Режим `init` для DuckDB кеша**
  - Автоматическая инициализация при старте агента
  - Поддержка persistence через файл кеша

- ✅ **Удалены устаревшие навыки**
  - `data-analyzer` → функциональность перенесена в `audit_analyzer`
  - `html_presentation_generator` → заменено на Streamlit UI

### v1.4.0

- Переименование навыка: `db_analyzer` → `audit_analyzer`
- Переход с `psycopg` на `psycopg2`
- Добавлен режим `vector` с поддержкой порогов схожести

### v1.3.0

- Первоначальная реализация с FAISS файлами
- Режимы: `predefined`, `sql`, `vector`

---

## 🐞 Отладка и логирование

### Уровни логирования

| Уровень | Описание | Когда использовать |
|---------|---------|-------------------|
| `DEBUG` | Детальная информация о каждом запросе, SQL-дамп | Разработка новых скриптов |
| `INFO` | Общая статистика, время выполнения | Продакшен |
| `WARNING` | Проблемы с подключением, медленные запросы | Мониторинг |
| `ERROR` | Критические ошибки (БД, LLM, файлы) | Всегда включен |

### Переменные окружения для отладки

```bash
export AUDIT_ANALYZER_LOG_LEVEL=DEBUG
export AUDIT_ANALYZER_DUMP_SQL=true
export AUDIT_ANALYZER_PROFILE_QUERIES=true
```

### Лог-файлы

По умолчанию логи пишутся в стандартный вывод (stdout). Для сохранения в файл:

```bash
python cli.py --mode sql --query "..." 2>&1 | tee logs/audit_analyzer_$(date +%Y%m%d_%H%M%S).log
```

### Профилирование запросов

При включенном `AUDIT_ANALYZER_PROFILE_QUERIES=true` каждый запрос выводит:
- Время выполнения
- План выполнения (EXPLAIN ANALYZE)
- Количество обработанных строк

---

## 🧪 Тестирование

### Unit-тесты

Расположение: `/workspace/tests/skills/test_audit_analyzer.py`

Запуск:
```bash
pytest tests/skills/test_audit_analyzer.py -v
```

### Интеграционные тесты

Требуют подключения к реальной БД с тестовыми данными:

```bash
# Настройка тестовой БД
psql -h localhost -U nanobot -d nanobot_test -f benchmarks/sql/create_benchmark_tables.sql

# Запуск интеграционных тестов
pytest tests/integration/test_audit_analyzer_integration.py -v --db-url=postgresql://nanobot:test@localhost/nanobot_test
```

### Тесты производительности

Для проверки скорости загрузки кеша и выполнения запросов:

```bash
python DEVELOPMENT/benchmark_performance.py --mode init --iterations 10
python DEVELOPMENT/benchmark_performance.py --mode vector --queries-file tests/vector_queries.txt
```

---

## 📎 Приложения

### A. Полный список параметров CLI

```bash
python cli.py --help

Режимы:
  --mode {predefined,sql,vector,init}
  
Общие параметры:
  --config <path>          Путь к конфигу (по умолчанию: project.json)
  --verbose                Подробный вывод
  --log-level {DEBUG,INFO,WARNING,ERROR}
  
Параметры для predefined:
  --script <name>          Имя скрипта из реестра
  --params <key=value,...> Параметры для скрипта
  
Параметры для sql:
  --query <SQL>            SQL-запрос
  
Параметры для vector:
  --query <text>           Текстовый запрос для векторного поиска
  --top-k <N>              Количество результатов (по умолчанию: 5)
  --threshold <float>      Порог схожести (по умолчанию: 0.7)
  --index-name <name>      Имя индекса (по умолчанию: default_audit_index)
  
Параметры для init:
  --cache-path <path>      Путь к файлу кеша DuckDB
  --batch-size <N>         Размер пакета (по умолчанию: 10000)
  --tables <t1,t2,...>     Список таблиц для загрузки
  --force                  Принудительная перезапись кеша
```

### B. Структура реестра predefined-скриптов

Файл: `scripts_registry.json`

```json
{
  "analytics_by_year_month": {
    "description": "Аналитика нарушений по году и месяцу",
    "sql_template": "SELECT ... FROM audits WHERE year={year} AND month={month}",
    "required_params": ["year", "month"],
    "output_format": "table"
  },
  "top_objects_by_violations": {
    "description": "Топ объектов по количеству нарушений",
    "sql_template": "SELECT object_name, COUNT(*) as violation_count ... GROUP BY object_name ORDER BY violation_count DESC LIMIT {limit}",
    "required_params": [],
    "optional_params": ["limit"],
    "default_params": {"limit": 10},
    "output_format": "markdown"
  }
}
```

### C. Контакты и поддержка

- **Ответственный разработчик:** [Указать имя/команду]
- **Канал поддержки:** [Slack/Telegram/Email]
- **Документация:** 
  - Пользовательская: [`SKILL.md`](./SKILL.md)
  - Архитектура: [`../../../README.md`](../../../README.md)
  - Векторные индексы: [`DEVELOPMENT/VECTOR_INDEXING.md`](./DEVELOPMENT/VECTOR_INDEXING.md)

---

*Последнее обновление: 2025-01-XX*  
*Версия документа: 1.5.0*
