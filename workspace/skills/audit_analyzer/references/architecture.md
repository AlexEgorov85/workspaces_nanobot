# Audit Analyzer Architecture

## Граница ответственности

Skill `audit_analyzer` владеет **только** domain-знаниями:
- структура таблиц `oarb.*` (`references/schema.md`);
- каталог из 5 predefined SQL-скриптов (`predefined/scripts.py` +
  `references/predefined_scripts.md`);
- каталог из 3 FAISS-индексов (`references/vector_indexes.md`);
- правила выбора одного из трёх режимов (decision tree).

Skill НЕ владеет логикой выполнения SQL, поиска FAISS или LLM-вызова —
всё это generic core services.

## Архитектура

```
Agent / user
  ↓
scripts/cli.py --mode <predefined | generated_sql | vector>
  ↓ маршрутизация
+--------+--------+--------+
|         |        |        |
PREDEFINED         GENERATED_SQL    VECTOR
|         |        |
↓         ↓        ↓
predefined.run   generated_sql_mode.run   vector_search (generic tool)
↓         ↓        ↓
CacheProvider.query_sql    LLM + validate_sql + CacheProvider.query_sql   CacheProvider.search_vector
↓
DuckDB / FAISS (generic core)
```

Тонкие tool'ы (`workspace/tools/duckdb_query_tool.py`,
`workspace/tools/vector_search_tool.py`) — для Agent, не для CLI.

## Что такое `scripts/cli.py`

CLI — единственная **целевая** точка вызова навыка из shell/runtime.
Поддерживает 3 режима (`predefined`, `generated_sql`, `vector`),
валидирует параметры, возвращает плоский JSON.

Внутри CLI:
- `predefined.run()` — pipeline для predefined (lookup +
  ParameterValidator + DynamicQueryBuilder + ``CacheProvider.query_sql``);
- `generated_sql_mode.run()` — LLM → SQL с retry-циклом (``MAX_RETRIES = 3``);
- `_run_vector()` — вызов generic `vector_search` tool поверх
  ``CacheProvider.search_vector``;
- `llm.py` — тонкая обёртка над `lib.services.llm_client`;
- `column_hints.py` — пронумерованные подсказки «термин → колонка» для
  system prompt generated_sql_mode;
- `output.py` — плоский формат JSON;
- `skill_config.py` — обёртка над `lib.core.skill_config` для удобства
  импорта из `scripts/`.

Никаких audit-specific знаний в core: `CacheProvider.query_sql`,
`CacheProvider.search_vector`, `lib.services.llm_client.call_llm` —
всё generic.

## Core Services

Skill использует **только generic** core services:

| Сервис | Назначение | Skill-конкретика |
|---|---|---|
| `lib.services.DuckDbCacheStore` | generic SELECT-only SQL | не знает про audit_analyzer |
| `lib.services.CacheProvider` | generic FAISS-поиск | не знает про audits_index/violations_index |
| `lib.utils.sql_safety.validate_sql` | SELECT-only gate | не знает про oarb.* |

Имена индексов/скриптов/таблиц — **домен skill'а**, не core.

## Workflow по режимам

### PREDEFINED

```
ScriptDefinition         ─┐  predefined/scripts.py
   ↓ name + params
ParameterValidator       ─┤  predefined/validator.py
   ↓ merged + err|none
DynamicQueryBuilder      ─┤  predefined/builder.py  (Jinja2-подобный {% if %})
   ↓ SQL + positional params   (:param → ?)
CacheProvider.query_sql(sql, params)
   ↓
result {row_count, columns, rows (dict по именам колонок)}
```

Все компоненты — внутри skill'a. Не делает HTTP/LLM-вызовов.

### VECTOR

```
vector_search(query, index_name, top_k, threshold)
  ↓
CacheProvider.search_vector(...)      ← из runtime-БД public.agent_vector_index_config
  ↓
FAISS score + content + metadata
```

Skill только передаёт `index_name` из своего каталога
(`references/vector_indexes.md`); tool ничего не знает про эти имена.

### SQL

```
Agent reasoning (по references/schema.md, sql_guidance.md)
  ↓
SQL SELECT
  ↓
duckdb_query(sql, params)
  ↓
validate_sql(sql)             ← SELECT-only gate в workspace/tools/duckdb_query_tool.py
DuckDBService.execute_readonly(sql, params, max_rows)
  ↓
result
```

Retry (при `sql_error`) — задача **Agent**, не отдельный сервис.

## Зависимости

`audit_analyzer` импортирует:

- `lib.services.DuckDbCacheStore` (через `lib.core.skill_config` /
  `workspace/tools/duckdb_query_tool.py` / `CacheProvider`)
- `lib.utils.sql_safety.validate_sql` (через `workspace/tools/duckdb_query_tool.py`)
- `workspace.utils.*` (при необходимости)

`audit_analyzer` НЕ импортирует:

- `lib.services.llm_client` — нет LLM-вызовов;
- `lib.core.skill_config.get_predefined_scripts_table` — реестр не в БД.

## Forbidden

Запрещены:

- audit-specific tools в core (`vector_search`, `duckdb_query` — generic);
- `run_predefined_script` как отдельный tool (это режим skill);
- `nl_sql_generate` как отдельный tool (Agent формирует SQL сам);
- LLM-генерация SQL — только через CLI-режим `generated_sql`
  (`scripts/generated_sql_mode.py`), не отдельным standalone-helper'ом;
- Python wrappers вокруг `duckdb_query` или `vector_search`;
- `public.agent_predefined_scripts` lookup из runtime skill'a (реестр
  хранится в `predefined/scripts.py`);
- `audit_specific` SQL routing / prompts в core.

Допустимы:

- `predefined.run(name, db, params)` — режим skill, generic DB-сервис;
- `vector_search(query, index_name=<из vector_indexes>)` — generic tool;
- `duckdb_query(sql, params)` — generic tool;
- ссылки на `lib.utils.sql_safety.validate_sql` — generic SELECT-only gate.
