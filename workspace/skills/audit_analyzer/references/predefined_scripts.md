# Predefined SQL scripts

Predefined scripts — режим навыка `audit_analyzer`: готовые SQL-скрипты,
которые Agent вызывает по имени через `predefined.run(name, db, params)`.

Реестр SQL хранится **внутри skill'а** (`workspace/skills/audit_analyzer/predefined/scripts.py`),
а не в таблице PostgreSQL. Источник истины — Python-литералы
`ScriptDefinition`-объектов; SQL проверен и валидирован. Изменение
predefined SQL требует редактирования этого skill'а (не INSERT в БД).

## Архитектура

```
ScriptDefinition        (predefined/models.py)
    ↓ name + params
ParameterValidator      (predefined/validator.py)
    ↓ validated params + error|none
DynamicQueryBuilder     (predefined/builder.py)
    ↓ SQL + positional params
CacheProvider.query_sql(sql, params)
    ↓
result: row_count/columns/rows (rows — список dict по именам колонок)/error
```

`DuckDBService` — generic `lib.services.DuckDbCacheStore` (см.
`workspace/tools/duckdb_query_tool.py`). Никакого domain-знания
в skill'е о БД нет: имя таблицы, схема колонок, JOIN — всё закодировано
в SQL самого скрипта.

## Как вызывать predefined

### Через CLI (целевой путь)

```bash
python workspace/skills/audit_analyzer/scripts/cli.py \
    --mode predefined \
    --script <имя> \
    --params '<JSON или key=value>'
```

JSON-результат (плоский) — в stdout.

### Через Python API (для runtime / unit-тестов)

```python
from workspace.skills.audit_analyzer.predefined import run

result = run(
    script_name="violations_by_period",
    db=<CacheProvider или DuckDB-fixture>,
    params={"date_from": "2024-01-01", "date_to": "2024-12-31"},
)
```

Внутри skill'а: ``predefined/mode.py::run`` выполняет SQL через
``CacheProvider.query_sql(sql, params)`` (generic core), ``rows`` — список
dict по именам колонок. Это **единый** контракт результатов для всех
режимов (predefined / generated_sql / vector-обвязки CLI).

## Каталог скриптов

| Script | Параметры | Назначение |
|---|---|---|
| `audit_status_summary` | — | Сводка по статусам аудитов |
| `top_violations_by_type` | — | Топ кодов нарушений |
| `violations_by_period` | `date_from`, `date_to` (required, ISO-date) | Нарушения за период |
| `audits_by_period` | `date_from`, `date_to` (required, ISO-date) | Проверки за период |
| `audit_effectiveness_summary` | `min_violations` (number, опц.) | Сводка эффективности |

### Подробное описание

#### `audit_status_summary`

- **Назначение**: агрегация `oarb.audits` по `status`.
- **Когда использовать**: «сколько аудитов по статусам», «распределение проверок».
- **Когда НЕ использовать**: нужны подробности по конкретным проверкам
  или фильтры по датам/типу — свободный SQL.
- **Параметры**: нет.

#### `top_violations_by_type`

- **Назначение**: топ кодов нарушений (`violation_code`).
- **Когда использовать**: «самые частые нарушения», «топ кодов».
- **Когда НЕ использовать**: нужны нарушения по конкретному коду — свободный SQL.
- **Параметры**: нет.

#### `violations_by_period`

- **Назначение**: нарушения в заданный период (через JOIN `oarb.violations` × `oarb.audits`).
- **Когда использовать**: «нарушения за 2024», «что выявлено в Q1».
- **Когда НЕ использовать**: период не указан, нужны дополнительные фильтры
  по `severity` / `status`.
- **Параметры**: `date_from`, `date_to` — обязательные ISO-даты (`YYYY-MM-DD`).

#### `audits_by_period`

- **Назначение**: проверки в заданный период (по `actual_date`).
- **Когда использовать**: «проверки за 2024», «что проверяли в Q2».
- **Когда НЕ использовать**: период не указан, нужны фильтры по
  `status` / `audit_type`.
- **Параметры**: `date_from`, `date_to` — обязательные ISO-даты.

#### `audit_effectiveness_summary`

- **Назначение**: сводка эффективности — проверки × число нарушений × severity.
- **Когда использовать**: «какие проверки самые проблемные», «уровень серьёзности».
- **Когда НЕ использовать**: нужны JOIN'ы с другими таблицами или детализация по `auditee_entity`.
- **Параметры**: `min_violations` (число, опционально) — минимальное число
  нарушений для включения проверки в отчёт. Полезно, чтобы исключить
  «Без нарушений»-строки и сосредоточиться на проблемных проверках.

⚠️ **Качество данных**: если в результате одна проверка содержит >50%
всех нарушений — это признак битого сида (`oarb.violations.audit_id`
не распределены по проверкам). Используйте свободный SQL с
проверкой распределения по `audit_id`.

## Что значит «соответствует predefined»

Выбирай predefined script **только если выполняются оба условия**:

1. **Весь смысл** запроса соответствует назначению скрипта (см. подробное
   описание каждого script).
2. **Параметры** запроса позволяют выполнить скрипт (например, для
   `violations_by_period` обе даты должны быть заданы).

Похожее слово в запросе ≠ подходящий predefined:

- «покажи нарушения» без периода — **не** `violations_by_period`
  (период не указан).
- «топ нарушений за 2024» — **не** `top_violations_by_type`
  (скрипт не принимает период).
- «сводка по статусам похожих проверок» — **не** `audit_status_summary`
  (нужен дополнительный фильтр; не реализован в скрипте).

В таких случаях — переходи к свободному SQL через `duckdb_query`
(см. `references/sql_guidance.md`).

## Что НЕ делать

- Не обращайся к таблице `public.agent_predefined_scripts` через
  `duckdb_query` — реестр более не хранится в БД.
- Не выполняй SQL напрямую через `exec` / `python` — только `predefined.run()`
  или `duckdb_query`.
- Не вызывай `vector_search` для задачи, которую решает predefined script
  (predefined всегда приоритетнее).
- Не выдумывай имя скрипта — бери только из каталога выше.
- Не передавай лишние параметры — `predefined.run()` вернёт ошибку
  `unknown_params`.
- Не передавай `date_from` / `date_to` как `int` / `datetime` — только
  ISO-строку (`YYYY-MM-DD`).

## Как добавить новый скрипт

1. Добавить `ScriptDefinition` в `workspace/skills/audit_analyzer/predefined/scripts.py`.
2. Использовать позиционные `?`-placeholder'ы в SQL (или `:param` —
   `DynamicQueryBuilder` конвертирует).
3. Описать в разделе «Каталог скриптов» этого файла.
4. Обновить `SKILL.md` (раздел «Predefined scripts»).
5. Добавить тест в `tests/test_audit_analyzer_predefined.py`.

Удаление/изменение существующих скриптов — это PR в skill, не DDL в БД.
