# SQL guidance (NL → SELECT)

Правила формирования SELECT-запросов для `duckdb_query`, когда skill
обрабатывает свободный запрос на естественном языке.

## Принципы

1. **Только SELECT/WITH/EXPLAIN.** Никаких DDL/DML.
2. **Один statement.** Без `; DROP ...`.
3. **Используй только таблицы из `references/schema.md`.**
4. **Добавляй `LIMIT`**, если пользователь не указал явно (по умолчанию 100).
5. **Используй prepared parameters** (`:year`, `:date_from`) вместо интерполяции.

## Шаблон рассуждения

```text
1. Понять, какие таблицы задейрены (см. references/schema.md).
2. Понять, какие колонки нужны для ответа.
3. Определить JOIN-связи, если ответ затрагивает несколько таблиц.
4. Сформировать SELECT.
5. Если фильтр по тексту — использовать vector_search вместо LIKE.
6. Передать SELECT в tool duckdb_query.
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

## Ограничения tool'а

`duckdb_query` гарантирует:

- SELECT-only (см. `lib/utils/sql_safety.py::validate_sql`).
- max_rows — из конфига (`gateway.duckdb_query.max_rows`, default 1000).
- max_result_chars — обрезка JSON-ответа (`gateway.duckdb_query.max_result_chars`).
- query_timeout — best-effort через `PRAGMA threads=1; SET statement_timeout=...`.

Если результат пустой или ошибка — **не пытайся** генерировать DDL для
«исправления». Skill остаётся в read-only режиме. Сообщи пользователю
о причине.

## Когда использовать predefined вместо свободного SELECT

Если запрос пользователя соответствует одному из именованных отчётов
(см. `SKILL.md` → predefined reports), предпочтительно использовать
`scripts/predefined_mode.py` — это deterministic и параметризовано через
реестр `public.agent_predefined_scripts`.