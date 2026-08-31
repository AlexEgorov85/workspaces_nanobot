# SQL guidance (NL → SELECT)

Правила формирования SELECT-запросов для `duckdb_query` / `nl_sql_generate`,
когда skill обрабатывает NL-запрос или точный SELECT.

## Рекомендуемый путь: tool `nl_sql_generate`

Для NL→SELECT в большинстве случаев используй tool `nl_sql_generate`
(`workspace/tools/nl_sql_generate.py`). Он сам:

1. По whitelist'у таблиц из `TableRegistry` собирает system prompt с описанием схемы.
2. Подтягивает hints через `column_descriptions` (термин → колонка).
3. Генерирует SELECT через LLM с retry-циклом (валидация `sql_safety` + `EXPLAIN`).
4. Выполняет запрос в общем DuckDB-кеше и возвращает JSON.

Аргументы: `query` (обяз.), `max_rows`/`no_few_shot`/`skip_hints`/`hints_max_matches` (опц.).
Конфиг — `gateway.nl_sql_generate.*` в `project.json`.

Используй `duckdb_query` напрямую, только если уже знаешь точный SELECT
(см. шаблон ниже) или когда `nl_sql_generate` не подходит (например,
очень специфическая диагностика, для которой не нужен LLM).

## Принципы

1. **Только SELECT/WITH/EXPLAIN.** Никаких DDL/DML.
2. **Один statement.** Без `; DROP ...`.
3. **Используй только таблицы из `references/schema.md`.**
4. **Добавляй `LIMIT`**, если пользователь не указал явно (по умолчанию 100).
5. **Используй prepared parameters** (`?` или `:name`) вместо интерполяции.

## Шаблон рассуждения

```text
1. Понять, какие таблицы задейрены (см. references/schema.md).
2. Понять, какие колонки нужны для ответа.
3. Определить JOIN-связи, если ответ затрагивает несколько таблиц.
4. Сформировать SELECT.
5. Если фильтр по тексту — использовать vector_search вместо LIKE.
6. Передать SELECT в tool duckdb_query ИЛИ передать NL-запрос
   в nl_sql_generate (он сгенерирует SELECT сам).
7. Если результат пустой — попробовать альтернативный JOIN или фильтр.
```

## Примеры

### «Сколько проверок по годам?»

```sql
SELECT EXTRACT(year FROM actual_date) AS year, COUNT(*) AS cnt
FROM audits
GROUP BY year
ORDER BY year
```

Через `nl_sql_generate`:
```
nl_sql_generate(query="сколько проверок по годам")
```

### «Топ-10 организаций по числу нарушений за 2024»

```sql
SELECT a.auditee_entity, COUNT(*) AS violations_count
FROM audits a
JOIN violations v ON v.audit_id = a.id
WHERE EXTRACT(year FROM a.actual_date) = :year
GROUP BY a.auditee_entity
ORDER BY violations_count DESC
LIMIT 10
```

Через `nl_sql_generate`:
```
nl_sql_generate(query="топ-10 организаций по числу нарушений за 2024")
```

### «Динамика проверок по месяцам за 2023-2024»

```sql
SELECT
    EXTRACT(year FROM actual_date) AS year,
    EXTRACT(month FROM actual_date) AS month,
    COUNT(*) AS audits_count
FROM audits
WHERE actual_date BETWEEN :date_from AND :date_to
GROUP BY year, month
ORDER BY year, month
```

## Ограничения tool'ов

`duckdb_query` и `nl_sql_generate` гарантируют:

- SELECT-only (см. `lib/utils/sql_safety.py::validate_sql`).
- `max_rows` — из конфига (`gateway.duckdb_query.max_rows` или
  `gateway.nl_sql_generate.max_rows`, default 1000).
- `max_result_chars` — обрезка JSON-ответа через `truncate_middle`.

Если результат пустой или ошибка — **не пытайся** генерировать DDL для
«исправления». Tool'ы остаются в read-only режиме. Сообщи пользователю
о причине.

## Когда НЕ использовать NL→SELECT

- Точный фильтр по `id` или известному набору значений → `duckdb_query`
  с явным SQL.
- Семантический поиск по смыслу → `vector_search` (а не LIKE).
- Сложные многошаговые JOIN-ы, где NL может не угадать правильную
  структуру → `duckdb_query` с явным SQL.
