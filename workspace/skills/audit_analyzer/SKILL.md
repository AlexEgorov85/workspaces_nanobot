---
name: audit_analyzer
description: Анализ аудиторских проверок — SQL-отчёты по oarb.*, семантический поиск по FAISS, LLM-генерация SELECT.
metadata: {"nanobot":{"emoji":"📊","always":true}}
---

# Audit Analyzer

Анализ аудиторских проверок: нарушения, отчёты, плановые/фактические даты.
Skill **использует только tool'ы** (никаких внутренних CLI/скриптов — всё
выполнение идёт через generic infrastructure tools).

## Decision procedure: задача → tool

| Задача | Tool |
|---|---|
| NL-запрос на естественном языке → SELECT → результат | `nl_sql_generate` |
| Точный SQL уже известен → выполнить SELECT | `duckdb_query` |
| Семантический поиск по смыслу (не точному слову) | `vector_search` (с `index_name` из `references/vector_indexes.md`) |
| Подсказки по колонкам для текущего термина | `column_descriptions` (опц., вызывается `nl_sql_generate` автоматически) |

**Правило выбора:**

1. **NL → SELECT**: `nl_sql_generate(query="...")`. Сам подтянет hints через
   `column_descriptions` (термин → колонка) и выполнит запрос в общем
   DuckDB-кеше.
2. **Точный SELECT**: `duckdb_query(sql="SELECT ... FROM oarb.audits ...")`,
   когда SQL уже знаешь (например, скопировал из `references/sql_guidance.md`).
3. **Семантический поиск**: `vector_search(query="...", index_name="...")`,
   когда запрос про «смысл», а не точные слова.
4. **Подсказки отдельно**: `column_descriptions(term="...")` —
   обычно не нужно напрямую, `nl_sql_generate` подтянет сам.

## Доменные таблицы и индексы

> Имена таблиц и индексов ниже — значения текущей инсталляции, настраиваемые в
> `project.json` (`skills.audit_analyzer.tables[*].name`,
> `skills.audit_analyzer.vector_indexes[*].name`). В других развёртываниях они
> могут отличаться; не зашивайте их в код/промпты как константы.

- `oarb.audits`, `oarb.violations`, `oarb.audit_reports`, `oarb.report_items` —
  колонки и связи см. `references/schema.md`.
- Vector-индексы (`audits_index`, `violations_index`, `audit_reports_index`)
  — назначение и embed-колонки см. `references/vector_indexes.md`.
- Реестр предопределённых скриптов — `public.agent_predefined_scripts`
  (конфигурируется через `label: "scripts_registry"`).

## Что не делать

- Не использовать неизвестные таблицы или индексы.
- Не использовать DDL/DML (запрещено в `duckdb_query` и `nl_sql_generate`
  через `lib/utils/sql_safety.py::validate_sql`).
- Не подставлять пользовательские значения в SQL строкой — параметры через
  `:name` (для `duckdb_query.params`) или args в `nl_sql_generate`.
- Не вызывать `scripts/cli.py` (в skill'е больше нет CLI — он удалён,
  всё через tool'ы).

## Как работает NL → SELECT

`nl_sql_generate` использует общий pipeline в `lib/services/nl_sql_runner.py`:

1. Whitelist таблиц из `TableRegistry` (не знает про домен).
2. `SchemaFormatter` (internal service) формирует описание схемы для LLM.
3. `column_descriptions.lookup()` подмешивает подсказки термин→колонка
   в system prompt (если `skip_hints=False`).
4. LLM генерирует SELECT.
5. `validate_sql` — последняя граница безопасности (SELECT-only, single-statement).
6. `provider.explain()` — синтаксическая проверка.
7. `provider.query_sql()` — выполнение в общем DuckDB-кеше.
8. JSON-ответ с `sql`, `columns`, `rows`, `row_count`.

Retry-цикл: до `gateway.nl_sql_generate.max_retries` (default 3) при
ошибках валидации / EXPLAIN / execute.

## Семантический поиск: пример

```
vector_search(
  query="пожарная безопасность",
  index_name="violations_index",
  top_k=5,
  threshold=0.5,
)
```

Метаданные индексов (`audits_index`, `violations_index`, `audit_reports_index`)
живут в `public.agent_vector_index_config`. Выбор `index_name` — на стороне
skill'а (см. `references/vector_indexes.md`).

## References

- `references/schema.md` — структура таблиц `oarb.*`.
- `references/vector_indexes.md` — описание FAISS-индексов.
- `references/sql_guidance.md` — правила формулировки SELECT.
