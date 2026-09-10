# SQL guidance (audit_analyzer)

Этот файл описывает **как Agent формирует SQL** для `duckdb_query`.

## Когда использовать duckdb_query

- Ни predefined, ни vector search не подходят.
- Нужны: COUNT, GROUP BY, JOIN, фильтры по колонкам, динамика по датам.
- Точные SQL-выражения: «SELECT id FROM oarb.audits WHERE actual_date = ?».

## Общие правила

- Только `SELECT` / `WITH` / `EXPLAIN`. Никаких DDL/DML — `duckdb_query`
  отвергнет через `validate_sql` (`lib/utils/sql_safety.py`).
- Один statement. Без `; DROP ...`.
- Полностью квалифицированные имена таблиц: `schema.table` (например,
  `oarb.audits`).
- Параметры — позиционные `?` или именованные `:name` (см. контракт
  `duckdb_query.params`).
- `LIMIT` добавляется автоматически (`max_rows` из конфига
  `gateway.duckdb_query.max_rows`).

## Процесс (Agent reasoning)

1. Определи нужную таблицу (см. `references/schema.md`).
2. Определи нужные колонки.
3. Сформируй минимальный `SELECT`.
4. Используй явные `JOIN` для связей.
5. Для агрегатов — `COUNT` / `SUM` / `AVG` + `GROUP BY`.
6. Для дат используй `actual_date` / `report_date` / `deadline`.
7. Вызови `duckdb_query(sql, params)`.
8. Используй результат для формирования ответа.

## Retry при ошибке

`duckdb_query` вернёт структурированную ошибку:

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
3. Повтори `duckdb_query`.

**Retry — задача Agent**, не отдельного Python-сервиса. Ограничение числа
попыток — на стороне Agent (обычно не больше 2-3).

## Что делать при пустом результате

Если SQL корректный, но `rows == []`:

```json
{
  "status": "success",
  "columns": [...],
  "rows": [],
  "row_count": 0
}
```

Это нормальный ответ. Сообщи пользователю «нет данных за указанный
период», не интерпретируй пустой результат как сбой.

## Что не придумывать

Не придумывай:

- таблицы;
- колонки;
- индексы (`index_name` берётся строго из `references/vector_indexes.md`);
- значения enum.

Используй `references/schema.md`. Если данных нет в skill — это признак
того, что запрос нужно переформулировать или использовать другой способ
получения данных.

## Не вызывай удалённые tool'ы

- Не вызывай `run_predefined_script` / `nl_sql_generate` /
  `column_descriptions` — их больше нет.
- Не вызывай `scripts/sql_generator.py` — helper удалён; используй
  `scripts/cli.py --mode generated_sql` (тот же pipeline через
  `generated_sql_mode.run()`).
- Не используй `LIKE '%...%'` для семантического поиска — для этого
  есть `vector_search`.

## Если нужен CLI

```bash
# Predefined
python workspace/skills/audit_analyzer/scripts/cli.py \
    --mode predefined --script NAME --params '{...}'

# NL → SQL
python workspace/skills/audit_analyzer/scripts/cli.py \
    --mode generated_sql --query '...'

# Vector search
python workspace/skills/audit_analyzer/scripts/cli.py \
    --mode vector --query '...' --index-name NAME --top-k 5
```

## Что НЕ делать

- Не вызывай `exec` / `python` для выполнения SQL.
- Не формируй DDL (`CREATE` / `DROP` / `ALTER`) — будет отвергнуто.
- Не строй сложные подзапросы, если можно обойтись `JOIN`.
- Не вызывай `predefined.run()` с произвольным SQL — predefined использует
  свой шаблон; свободный SQL идёт через `duckdb_query`.
