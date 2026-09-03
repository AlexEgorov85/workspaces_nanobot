# SQL guidance (NL → SELECT fallback)

Это **технические детали** для fallback-пути `nl_sql_generate`. Не
инструкция выбирать другой tool — основной контракт выбора режима
в `SKILL.md` (только три способа).

## Pipeline `nl_sql_generate`

1. **Whitelist таблиц** из `TableRegistry` (только зарегистрированные
   `schema.table`, никаких посторонних).
2. **SchemaFormatter** формирует описание схемы для LLM (system prompt).
3. **column_descriptions.lookup()** подмешивает подсказки термин→колонка.
4. **Few-shot** примеры из `public.agent_predefined_scripts` подмешиваются
   как контекст для LLM (но **не выполняются** автоматически).
5. **LLM** генерирует SELECT.
6. **validate_sql** — SELECT-only gate (см. `lib/utils/sql_safety.py`).
7. **provider.explain** — синтаксическая проверка.
8. **provider.query_sql** — выполнение в общем DuckDB-кеше.
9. JSON-ответ с `sql`, `columns`, `rows`, `row_count`.

Retry-цикл: до `gateway.nl_sql_generate.max_retries` (default 3).

## Правила для LLM-промта (когда генерирует SELECT)

1. **Только SELECT/WITH/EXPLAIN.** Никаких DDL/DML.
2. **Один statement.** Без `; DROP ...`.
3. **Только таблицы** из `references/schema.md` (полные имена
   `schema.table`).
4. **LIMIT** добавляется автоматически (потолок из конфига).
5. **Prepared parameters** (`?` или `:name`) — не строковая интерполяция.

## Когда fallback-путь **не нужен**

- Запрос соответствует predefined скрипту из `SKILL.md` → `run_predefined_script`.
- Семантический поиск (похожие, смысл) → `vector_search` с индексом из `SKILL.md`.

## Ограничения

`nl_sql_generate` гарантирует:

- SELECT-only (см. `lib/utils/sql_safety.py::validate_sql`).
- `max_rows` — из конфига `gateway.nl_sql_generate.max_rows` (default 1000).
- `max_result_chars` — обрезка JSON-ответа через `truncate_middle`.

Если результат пустой или ошибка — **не пытайся** генерировать DDL для
«исправления». Tool остаётся в read-only режиме. Сообщи пользователю
о причине.