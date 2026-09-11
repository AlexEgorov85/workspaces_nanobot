---
name: audit_analyzer
description: Анализ аудиторских проверок — predefined SQL-скрипты из PostgreSQL, семантический поиск по FAISS-индексам Core, свободный SQL через Core Data.
metadata: {"nanobot":{"emoji":"📊","always":true}}
---

# Audit Analyzer

Навык для работы с данными аудиторских проверок (нарушения, отчёты,
плановые/фактические даты). Три способа получения данных — все через
generic Core capability.

## Архитектура

```
Agent / LLM
       │
       ▼
audit_analyzer Skill
       │
       ├── predefined analytical operations
       │      └── predefined.run() → Core CacheProvider → DuckDB-PG снимок
       │
       ├── semantic search
       │      └── Core Vector capability (FAISS по runtime-БД)
       │
       └── свободный SQL через Core Data
              └── Core Data/DuckDB capability (SELECT-only)

       ▼
Core
 ├── Cache (DuckDB-PG snapshot)
 ├── Vector access (FAISS + embeddings)
 └── Data/DuckDB access (SELECT-only gate)

       ▼
Storage
 ├── PostgreSQL (source of truth)
 ├── DuckDB cache (PG snapshot)
 └── vector storage (FAISS + agent_vector_index_store)
```

**Важно:** в CLI три режима (`predefined`, `generated_sql`, `vector`).
«Свободный SQL» — это **не CLI-режим**, а capability, через которую Agent
выполняет SQL напрямую через generic `CacheProvider.query_sql` /
`DuckDBService.execute_readonly`.

**Skill не владеет:**

- логикой выполнения SQL (это `CacheProvider.query_sql` / generic SQL tool);
- логикой FAISS-поиска (это `CacheProvider.search_vector`);
- выбором embedding-модели (это `gateway.vector.embedding`);
- LLM-вызовами (Core skill helper, если нужен).

**Skill владеет только:**

- каталогом predefined скриптов (через DB-реестр `public.agent_predefined_scripts`);
- каталогом FAISS-индексов (логические имена);
- правилами выбора между ними.

## Источник predefined скриптов

Канонический источник SQL — `public.agent_predefined_scripts` в PostgreSQL.
Python `REGISTRY` (legacy) удалён в Phase 7: единый путь — DB-first lookup
через `scripts/predefined/db_loader.py`.

```
PostgreSQL
    ↓ seed/migration
public.agent_predefined_scripts (6 скриптов)
    ↓ PgDuckDbSyncService
DuckDB-PG snapshot (workspace/data_store/duckdb/cache.duckdb)
    ↓ db_loader.load_script / load_all
ScriptDefinition
    ↓ predefined.run(name, db, params, *, predefined_table=...)
CacheProvider.query_sql(sql, values)
    ↓
result {row_count, columns, rows (dict по именам колонок)}
```

В БД сейчас лежат **6 скриптов**, прочитанных напрямую через psycopg2:

- `analytics_by_year_month`
- `audit_dynamics`
- `audit_effectiveness`
- `audit_types_stats`
- `top_audited_objects`
- `violations_by_type`

Это — **единственный канонический список**.

## Каталог predefined scripts

| Скрипт | Назначение | Параметры |
|---|---|---|
| `analytics_by_year_month` | Аналитика проверок по годам и месяцам | `year` (опц., число) |
| `audit_dynamics` | Динамика проверок по периодам (month/quarter/week) | `period` (опц., enum: month/quarter/week, default `month`), `date_from` (опц., date) |
| `audit_effectiveness` | Оценка эффективности: проверки × нарушения × severity | `date_from`, `date_to`, `min_violations` (все опц.) |
| `audit_types_stats` | Статистика по типам проверок | `audit_type` (опц., like), `date_from` (опц., date) |
| `top_audited_objects` | Топ проверяемых объектов по количеству проверок | `auditee_entity` (опц., like), `date_from` (опц., date), `limit` (опц., default `10`) |
| `violations_by_type` | Статистика нарушений по типам | `violation_code` (опц., like), `date_from` (опц., date) |

**Контракт:** все параметры **optional**, ни один скрипт не требует
обязательных значений. Если параметр не передан — соответствующий
`{% if param %}` Jinja-блок в SQL не активируется, фильтр не применяется.

**`type=like` оборачивает значение в `%...%`.** Если параметр объявлен
как `like`, передача `"Плановая"` означает substring-поиск
(`ILIKE '%Плановая%'`) — это **не exact match**. Для exact match передавайте
точное значение без wildcard'ов вручную (если seed БД это позволяет),
либо используйте другой скрипт. Актуальный каталог и типы параметров —
через `--list-scripts`.

### Подробное описание скриптов

#### `analytics_by_year_month`

- **Возвращает**: `audit_year`, `audit_month`, `audit_count`, `month_name`.
- **Когда использовать**: «сколько проверок в каждом году», «динамика по годам».
- **Параметры**:
  - `year` (number, опц.) — фильтр по конкретному году.

#### `audit_dynamics`

- **Возвращает**: `period` (например, `2024-Q3` или `2024-06`), `audit_count`, `unique_objects`, `total_violations`.
- **Когда использовать**: «помесячная / поквартальная динамика», «как менялось количество проверок».
- **Параметры**:
  - `period` (enum: `month` / `quarter` / `week`, опц., default `month`).
  - `date_from` (date, опц.) — нижняя граница.

#### `audit_effectiveness`

- **Возвращает**: `audit_id`, `audit_title`, `actual_date`, `violations_count`, `violation_types_count`, `severity_level`.
- **Когда использовать**: «какие проверки самые проблемные», «уровень серьёзности нарушений».
- **Параметры**:
  - `date_from` (date, опц.) — нижняя граница.
  - `date_to` (date, опц.) — верхняя граница.
  - `min_violations` (number, опц.) — минимальное число нарушений для фильтра.
- **Семантика severity**: `severity_level` синтезируется по количеству
  нарушений (`0 → «Без нарушений»`, `1–3 → «Допустимые»`, `4–10 →
  «Серьезные»`, `>10 → «Критические»`). Замена на реальный `v.severity`
  — отдельная задача.

#### `audit_types_stats`

- **Возвращает**: `audit_type`, `audit_count`, `unique_objects`, `total_violations`, `avg_severity`, `last_audit_date`.
- **Когда использовать**: «по каким типам проверок больше всего нарушений».
- **Параметры**:
  - `audit_type` (like, опц.) — фильтр по типу (например, `'финансовый'`).
  - `date_from` (date, опц.) — нижняя граница.
- **Vector-validation для `audit_type`**: `vector_field=audit_type`,
  `vector_top_k=3`, `vector_source=audits`, `vector_min_score=0.7` —
  если введённое значение не находит прямого совпадения, fuzzy-matcher
  может через vector-search предложить подходящее значение.

#### `top_audited_objects`

- **Возвращает**: `auditee_entity`, `audit_count`, `years_covered`, `last_audit_date`.
- **Когда использовать**: «какие объекты проверяются чаще всего».
- **Параметры**:
  - `auditee_entity` (like, опц.) — фильтр по объекту.
  - `date_from` (date, опц.) — нижняя граница.
  - `limit` (limit, опц., default `10`) — топ-N.

#### `violations_by_type`

- **Возвращает**: `violation_code`, `violation_count`, `affected_audits`.
- **Когда использовать**: «топ кодов нарушений», «по каким кодам больше всего проблем».
- **Параметры**:
  - `violation_code` (like, опц.) — фильтр по коду/типу.
  - `date_from` (date, опц.) — нижняя граница.

### Когда predefined «соответствует»

Выбирай predefined script **только если выполняются оба условия**:

1. **Весь смысл** запроса соответствует назначению скрипта.
2. **Параметры** либо явно известны, либо все параметры скрипта
   optional и могут быть опущены.

Похожее слово в запросе ≠ подходящий predefined:

- «покажи нарушения за 2024» — **подходит** `violations_by_type` с `date_from=2024-01-01`.
- «топ-5 объектов по проверкам» — **подходит** `top_audited_objects` с `limit=5`.
- «сводка по статусам похожих проверок» — **не подходит ни один** (нужен дополнительный фильтр; используй свободный SQL).

## Каталог vector indexes

| Индекс | Источник | Embed-колонки | Chunking | Score |
|---|---|---|---|---|
| `audits_index` | `oarb.audits` | `title`, `audit_type`, `auditee_entity`, `status` | — | `0.6+` высокая, `0.4–0.6` умеренная |
| `violations_index` | `oarb.violations` | `description` (chunk 500/80), `violation_code` | `description`: 500/80 | `0.65+` высокая, `0.5–0.65` умеренная |
| `audit_reports_index` | `oarb.audit_reports` | `full_text` (chunk 500/80), `title` | `full_text`: 500/80 | `0.55+` высокая, `0.4–0.55` умеренная |

Конфигурация — в `public.agent_vector_index_config` (seed:
`sql/audit_analyzer/seed_default_indexes.sql`). Skill передаёт **только**
логическое имя (`audits_index` / `violations_index` / `audit_reports_index`)
в generic vector capability — FAISS-детали (`agent_vector_index_store`,
`oarb.audit_vectors`, сериализация, embedding-blob) знает только Core.

**Известное предупреждение:** legacy FAISS-индексы могут иметь
`signature_status: "INVALID"` — это by design (`verify_index_signature`
помечает индексы без signature как INVALID; поиск работает, но данные
могут быть stale). Рекомендуется `python tools/build_vectors.py
--full-rebuild`.

### Советы по recall (vector_search)

Качество recall зависит от embedding-модели (по умолчанию —
`mxbai-embed-large`) и от формулировки запроса:

1. **Конкретные термины лучше абстрактных.** «Нарушения сроков отчётности»
   лучше сформулировать через конкретный термин домена («дедлайн»,
   «просроченная отчётность», «не представлена в срок»).
2. **Синонимы и перифразы.** Если запрос не сработал — попробуйте
   переформулировать через смежные термины: «пожарная безопасность» →
   «пожарная служба», «эвакуационные выходы».
3. **Длина запроса имеет значение.** Оптимально 3–7 слов.
4. **Threshold + top_k.** Если результаты нерелевантные — `threshold=0.5`
   (или выше). Если результатов мало — увеличьте `top_k` и уменьшите
   `threshold`.
5. **Несколько попыток.** Если первый запрос не сработал — попробуйте
   синонимы.

### Конвенции vector_search

- `index_name` — строковый идентификатор. Метаданные индексов (источник,
  embed-колонки, signature) живут в `public.agent_vector_index_config`.
  Сериализованный FAISS BYTEA — в `public.agent_vector_index_store`.
- `top_k` ограничен `max_top_k` из конфига
  (`gateway.vector_search.max_top_k`, по умолчанию 50).
- `threshold` ограничен `[0.0, 1.0]`. По умолчанию —
  `gateway.vector_search.default_threshold` (0.0 = без фильтра).
- `audits_index` — дефолт для CLI при `--mode vector` без `--index-name`.

## Схема домена (`oarb.*`)

Полная схема таблиц для свободного SQL. Все таблицы живут в схеме
`oarb`, доступны через DuckDB-кэш как `oarb.<table>` (полное имя
обязательно в SELECT).

### `oarb.audits` — аудиторские проверки

| column | type | description |
|---|---|---|
| `id` | integer | первичный ключ |
| `title` | varchar(500) | название проверки |
| `audit_type` | varchar(100) | тип проверки |
| `planned_date` | date | плановая дата |
| `actual_date` | date | фактическая дата |
| `status` | varchar(50) | статус («Запланирована», «В работе», «Завершена», ...) |
| `auditee_entity` | varchar(500) | проверяемая организация |
| `created_at` | timestamptz | метка создания (sync) |
| `updated_at` | timestamptz | метка обновления (sync) |

### `oarb.violations` — нарушения

| column | type | description |
|---|---|---|
| `id` | integer | первичный ключ |
| `audit_id` | integer | FK → `oarb.audits.id` |
| `report_id` | integer | FK → `oarb.audit_reports.id` (опц.) |
| `item_id` | integer | FK → `oarb.report_items.id` (опц.) |
| `violation_code` | varchar(100) | код нарушения |
| `description` | text | описание нарушения |
| `recommendation` | text | рекомендация по устранению |
| `severity` | varchar(20) | критичность («низкая» / «средняя» / «высокая» / «критическая») |
| `status` | varchar(50) | статус («открыто» / «в работе» / «закрыто») |
| `responsible` | varchar(200) | ответственный |
| `deadline` | date | срок устранения |
| `created_at` | timestamptz | метка создания (sync) |
| `updated_at` | timestamptz | метка обновления (sync) |

### `oarb.audit_reports` — отчёты о проверках

| column | type | description |
|---|---|---|
| `id` | integer | первичный ключ |
| `audit_id` | integer | FK → `oarb.audits.id` |
| `report_number` | varchar(100) | номер отчёта |
| `report_date` | date | дата отчёта |
| `title` | varchar(500) | заголовок отчёта |
| `full_text` | text | полный текст отчёта |
| `created_at` | timestamptz | метка создания (sync) |
| `updated_at` | timestamptz | метка обновления (sync) |

### `oarb.report_items` — пункты отчётов

| column | type | description |
|---|---|---|
| `id` | integer | первичный ключ |
| `report_id` | integer | FK → `oarb.audit_reports.id` |
| `item_number` | varchar(20) | номер пункта |
| `item_title` | varchar(500) | заголовок пункта |
| `item_content` | text | содержимое |
| `order_index` | integer | порядок отображения |
| `created_at` | timestamptz | метка создания (sync) |
| `updated_at` | timestamptz | метка обновления (sync) |

### Сводные JOIN-связи

```sql
-- Проверка → отчёт → пункты → нарушения
audits a
JOIN audit_reports r ON r.audit_id = a.id
JOIN report_items ri ON ri.report_id = r.id
LEFT JOIN violations v ON v.item_id = ri.id

-- Проверка → нарушения напрямую
audits a
LEFT JOIN violations v ON v.audit_id = a.id
```

### Домен-соглашения

- Все таблицы имеют `updated_at` — синхронизация идёт инкрементально по этой колонке.
- Идентификаторы — `BIGSERIAL` (в skill описаны как `integer` для краткости).
- Текстовые поля могут содержать `NULL`.
- Статусы и severity — свободный текст (`varchar`), не PG-enum. Конкретный
  набор ярлыков определяется данными и может расширяться без миграции схемы.

## SQL guidance (для свободного SQL через Core)

Когда predefined и vector не подходят — Agent формирует SELECT сам и
передаёт в generic `CacheProvider.query_sql` (или в Core Data
capability).

### Правила

- Только `SELECT` / `WITH` / `EXPLAIN`. Никаких DDL/DML — `validate_sql`
  (`lib/utils/sql_safety.py`) отвергнет.
- Один statement. Без `; DROP ...`.
- Полностью квалифицированные имена таблиц: `schema.table` (например,
  `oarb.audits`).
- Параметры — позиционные `?` или именованные `:name`.
- `LIMIT` добавляется автоматически (`max_rows` из
  `gateway.duckdb_query.max_rows`).

### Процесс (Agent reasoning)

1. Определи нужную таблицу (см. секцию «Схема домена»).
2. Определи нужные колонки.
3. Сформируй минимальный `SELECT`.
4. Используй явные `JOIN` для связей.
5. Для агрегатов — `COUNT` / `SUM` / `AVG` + `GROUP BY`.
6. Для дат используй `actual_date` / `report_date` / `deadline`.

### Retry при ошибке

`query_sql` вернёт структурированную ошибку:

```json
{
  "status": "error",
  "error_type": "sql_error",
  "message": "..."
}
```

Agent-цикл:

1. Прочитай `message`.
2. Исправь SQL (синтаксис / имя таблицы / колонки).
3. Повтори вызов.

**Retry — задача Agent**, не отдельного Python-сервиса. Ограничение числа
попыток — на стороне Agent (обычно не больше 2-3).

### Пустой результат — нормально

```json
{"status": "success", "columns": [...], "rows": [], "row_count": 0}
```

Не интерпретируй как сбой. Сообщи пользователю «нет данных за указанный
период».

### Что не придумывать

- таблицы;
- колонки;
- индексы (`index_name` берётся строго из каталога выше);
- значения enum.

## Decision tree

```
Q: Запрос ТОЧНО соответствует одному из 6 predefined scripts
   И параметры известны (или все optional)?
  YES → используй predefined capability (loader → Core query_sql)
  NO ↓

Q: Запрос про смысл/похожие, а не точные числа?
  YES → используй vector capability с index_name из каталога выше
  NO ↓

  → используй analytical SQL capability (Core Data/DuckDB, SELECT-only)
```

## Жёсткие правила

- Не выбирай `index_name` сам — только из каталога выше.
- Не выдумывай скрипт predefined — только из каталога выше.
- Не используй vector search для COUNT / GROUP BY / точных фильтров.
- Date-параметры — строго `YYYY-MM-DD` (валидация в `scripts/predefined/validator.py`).
- Если predefined-скрипт вернул SQL error (например, устаревший SQL в
  seed) — **не повторяй попытку**, переходи к `generated_sql` или
  analytical SQL capability. Этот случай не «diagnostic», а legacy-данные.
- Не используй `LIKE '%...%'` для семантического поиска — для этого
  есть `vector_search`.
- Не вызывай `exec` / `python` для выполнения SQL — только через Core Data.
- Не обращайся к `public.agent_predefined_scripts` через Core SQL
  напрямую — это реестр, а не доменная таблица.

## Runtime boundary

Skill реализует **только**:

- `predefined.run(script_name, db, params, *, predefined_table)` —
  DB-only lookup через `db_loader`. `predefined_table` обязателен
  (например, `"public.agent_predefined_scripts"`). Не делает
  HTTP/LLM-вызовов. Никакого fallback на Python `REGISTRY` (удалён в
  Phase 7).
- Доступ к логическим FAISS-индексам через generic Core Vector API
  (`CacheProvider.search_vector`).
- Доступ к доменным таблицам через generic Core Data API
  (`CacheProvider.query_sql` / `CacheProvider.execute_readonly`).

Skill НЕ использует Agent-facing tools (`vector_search_tool`,
`duckdb_query_tool`) — они живут в Core и доступны через generic
Capability, а не как часть Skill API.

Legacy mode `generated_sql` и соответствующий CLI-режим
(`scripts/cli.py --mode generated_sql`) — оставлены для обратной
совместимости, не для нового кода.

## Как добавить новый predefined-скрипт

1. Сделать DDL в БД: `INSERT INTO public.agent_predefined_scripts (...)`.
2. Дождаться синхронизации (`PgDuckDbSyncService` опубликует снимок в
   `workspace/data_store/duckdb/cache.duckdb`).
3. Описать в разделе «Каталог predefined scripts» этого файла.
4. Добавить тест в
   `workspace/skills/audit_analyzer/tests/test_audit_analyzer_predefined.py`.

Удаление/изменение существующих скриптов — это DDL в БД, не правка
skill'а.

## Discovery (актуальный каталог)

Имена скриптов и индексов **не прописаны жёстко** в этом файле — они
читаются из БД при старте CLI. Чтобы получить актуальный каталог:

```bash
# Список predefined-скриптов (имя, описание, параметры)
python workspace/skills/audit_analyzer/scripts/cli.py --list-scripts

# Список FAISS-индексов (имя, источник, embed-колонки, chunking)
python workspace/skills/audit_analyzer/scripts/cli.py --list-indexes
```

Оба возвращают JSON в stdout и не требуют `--mode`. Ошибки доступа к БД
возвращаются как `{"status": "error", "data": {"error_type": "registry_unavailable", ...}}`.

## Тесты

- `workspace/skills/audit_analyzer/tests/test_audit_analyzer_predefined.py` —
  contract-тесты 6 скриптов + параметры + DB-first lookup + no-fallback.
- `workspace/skills/audit_analyzer/tests/test_audit_analyzer_behavior.py` —
  Agent-loop контракт.