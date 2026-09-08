# Predefined SQL scripts — каталог для Agent

Каталог предопределённых SQL-скриптов для `audit_analyzer`.

Источник истины — таблица `public.agent_predefined_scripts` в PostgreSQL
(DDL: `sql/audit_analyzer/create_public_agent_predefined_scripts.sql`).
Через `PgDuckDbSyncService` записи попадают в общий runtime-снапшот DuckDB
(`workspace/data_store/duckdb/cache.duckdb`) и читаются агентом через
`duckdb_query`.

> **Эта таблица — единственный runtime-источник predefined SQL.** Если
> скрипта из этого каталога нет в таблице, он не работает. Синхронизация
> каталога и таблицы — задача администратора, не Agent.

## Как Agent использует predefined

Чтобы выполнить скрипт `X` с параметрами `params`:

1. **Узнать имя таблицы** predefined-скриптов. Это ресурс с
   `label="scripts_registry"` в `TableRegistry`. Имя можно получить
   через `lib.core.skill_config.get_predefined_scripts_table("audit_analyzer")`
   (если доступен через Python), либо прочитав настройки `project.json`:
   секция `skills.audit_analyzer.tables[]` с `label="scripts_registry"`.

   В этом проекте таблица — `public.agent_predefined_scripts`.

2. **Прочитать `sql_template` и `parameters`** скрипта через `duckdb_query`:

   ```sql
   SELECT name, sql_template, parameters, max_rows_default
   FROM public.agent_predefined_scripts
   WHERE name = ?
   -- params: ["<имя_скрипта>"]
   ```

3. **Подставить параметры**. SQL использует позиционные `?`-placeholder'ы
   в порядке объявления ключей в `parameters`. Передать через
   `duckdb_query(... params={"p1": v1, "p2": v2, ...})`.

4. **Выполнить SQL** через `duckdb_query`.

`duckdb_query` сам прогоняет SQL через `validate_sql` (SELECT-only gate).

## Каталог скриптов

| Script | Параметры | Назначение |
|---|---|---|
| `audit_status_summary` | нет | Сводка по статусам аудитов |
| `top_violations_by_type` | нет | Топ кодов нарушений |
| `violations_by_period` | `date_from` (date, required), `date_to` (date, required) | Нарушения за период |
| `audits_by_period` | `date_from` (date, required), `date_to` (date, required) | Аудиторские проверки за период |
| `audit_effectiveness_summary` | нет | Сводка эффективности (проверки × нарушения × severity) |

### Подробное описание

#### `audit_status_summary`

- **Источник**: `oarb.audits`.
- **Назначение**: агрегация по `status`.
- **Когда использовать**: «сколько аудитов по статусам», «распределение проверок».
- **Когда НЕ использовать**: нужны подробности по конкретным проверкам → свободный SQL.
- **Параметры**: нет.

#### `top_violations_by_type`

- **Источник**: `oarb.violations`.
- **Назначение**: топ кодов нарушений (`violation_code`).
- **Когда использовать**: «самые частые нарушения», «топ кодов».
- **Когда НЕ использовать**: нужны нарушения по конкретному коду → свободный SQL с `WHERE violation_code = ?`.
- **Параметры**: нет.

#### `violations_by_period`

- **Источник**: `oarb.violations`.
- **Назначение**: нарушения в заданный период.
- **Когда использовать**: «нарушения за 2024», «что выявлено в Q1».
- **Когда НЕ использовать**:
  - период не указан и неочевиден из контекста;
  - нужны дополнительные фильтры по `severity` / `status` → свободный SQL.
- **Параметры**: `date_from`, `date_to` — обязательные ISO-даты (`YYYY-MM-DD`).

#### `audits_by_period`

- **Источник**: `oarb.audits`.
- **Назначение**: проверки в заданный период (по `actual_date`).
- **Когда использовать**: «проверки за 2024», «что проверяли в Q2».
- **Когда НЕ использовать**:
  - период не указан;
  - нужны фильтры по `status` / `audit_type` → свободный SQL.
- **Параметры**: `date_from`, `date_to` — обязательные ISO-даты.

#### `audit_effectiveness_summary`

- **Источник**: `oarb.audits` × `oarb.violations` × `oarb.violations.severity`.
- **Назначение**: сводка эффективности — проверки × нарушения × severity.
- **Когда использовать**: «какие проверки самые проблемные», «уровень серьёзности».
- **Когда НЕ использовать**: нужны JOIN'ы с другими таблицами → свободный SQL.
- **Параметры**: нет.

## Что значит «соответствует predefined»

Выбирай predefined script **только если выполняются оба условия**:

1. **Весь смысл** запроса соответствует назначению скрипта.
2. **Параметры** запроса позволяют выполнить скрипт (например, для
   `violations_by_period` обе даты должны быть заданы).

Похожее слово в запросе ≠ подходящий predefined:

- «покажи нарушения» без периода — **не** `violations_by_period`.
- «топ нарушений за 2024» — **не** `top_violations_by_type`
  (скрипт не принимает период).
- «сводка по статусам похожих проверок» — **не** `audit_status_summary`
  (нужен дополнительный фильтр; не реализован в скрипте).

В таких случаях — переходи к свободному SQL через `duckdb_query`
(см. `references/sql_guidance.md` и `references/schema.md`).

## Чего не делать (для Agent'а)

- Не выполняй SQL напрямую через `exec`/`python` — только `duckdb_query`.
- Не вызывай `vector_search` для задачи, которую решает predefined script
  (predefined всегда приоритетнее).
- Не выдумывай имя скрипта — бери только из каталога выше.
- Не передавай лишние параметры — `duckdb_query` их проигнорирует, но
  скрипт может упасть на неожиданных `?`-placeholder'ах.
- Не генерируй SQL «по мотивам» predefined — это уже свободный SQL,
  используй его явно.

## Как добавить новый скрипт (для администратора)

```sql
INSERT INTO public.agent_predefined_scripts
  (name, description, sql_template, parameters, max_rows_default)
VALUES
  ('my_new_script', 'Краткое описание',
   'SELECT ... FROM oarb.<table> WHERE <col> = ?',
   '{"p1": {"type": "string", "required": true}}'::jsonb,
   1000);
```

После INSERT дождаться `PgDuckDbSyncService` и синхронизировать каталог
в этом файле (добавить имя и описание).
