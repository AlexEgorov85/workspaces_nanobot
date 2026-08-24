---
name: audit_analyzer
description: Анализ аудиторских проверок — SQL-отчёты по oarb.*, семантический поиск по FAISS, LLM-генерация SELECT с retry.
metadata: {"nanobot":{"emoji":"📊","always":true}}
---

# Audit Analyzer

Domain Skill для анализа аудиторских проверок: нарушения, отчёты,
плановые/фактические даты.

Skill использует **общие infrastructure tools** (`duckdb_query`,
`vector_search`) через агентский runtime. Skill описывает процедуру
выбора в этом `SKILL.md` и передаёт параметры в `tool_call` — он
**не импортирует** Python-классы tool'ов и не вызывает их программно
(TARGET_ARCHITECTURE.md §3, §22.2).

## Decision procedure

```text
User request
   |
   +-- structured aggregation / filter / grouping / numeric analysis
   |       -> use duckdb_query with explicit SELECT
   |
   +-- semantic similarity / find similar violations / reports
   |       -> use vector_search with index_name
   |
   +-- find similar docs, then aggregate
   |       -> vector_search first, then duckdb_query
   |
   +-- predefined report (analytics_by_year_month, top_audited_objects, ...)
   |       -> run via scripts/cli.py --mode predefined --script NAME
   |          (через свой shell-инструмент)
   |
   +-- NL -> SELECT (free-form question about the data)
   |       -> compose SELECT per references/sql_guidance.md,
   |          then duckdb_query
   |
   +-- do not use unknown tables or indexes
   +-- do not use DDL/DML (read-only — enforced by duckdb_query)
```

## Доступные данные (домен)

Skill владеет знаниями о следующих сущностях (полные описания —
progressive disclosure через `references/`):

- **Таблицы** (`references/schema.md`):
  - `oarb.audits`, `oarb.audit_reports`, `oarb.report_items`,
    `oarb.violations`.
- **Vector indexes** (`references/vector_indexes.md`):
  - `audits_index`, `violations_index`, `audit_reports_index`.
- **Predefined reports** (реестр `public.agent_predefined_scripts`):
  - `analytics_by_year_month`, `violations_by_type`,
    `top_audited_objects`, `audit_effectiveness`, `audit_dynamics`,
    `audit_types_stats`.

Детальные параметры, формат SELECT и описания индексов —
в `references/schema.md`, `references/sql_guidance.md`,
`references/vector_indexes.md`. Загрузить по необходимости.

## Workflows

### 1. Аггрегация / фильтр / группировка

Tool `duckdb_query` с явным SELECT. Финальная граница безопасности —
`lib/utils/sql_safety.py::validate_sql` (SELECT-only, multi-statement запрещён).

```text
tool_call: duckdb_query(
    sql="SELECT EXTRACT(year FROM actual_date) AS year, COUNT(*) "
        "FROM audits GROUP BY year ORDER BY year",
    max_rows=10,
)
```

### 2. Семантический поиск

Tool `vector_search` с явным `index_name`.

```text
tool_call: vector_search(
    query="нарушения пожарной безопасности",
    index_name="violations_index",
    top_k=5,
)
```

### 3. Поиск + аггрегация

```text
1. vector_search(query=..., index_name=...) -> ids
2. duckdb_query(sql="SELECT ... WHERE id IN (...) GROUP BY ...")
```

### 4. Predefined report

Запустить CLI skill'а через свой shell-инструмент:

```bash
python workspace/skills/audit_analyzer/scripts/cli.py --mode predefined \
    --script analytics_by_year_month --params '{"year": 2024}'
```

Подробнее — в секции «CLI (standalone)» ниже.

### 5. NL → SELECT

Прочитать `references/sql_guidance.md`, сформулировать SELECT,
передать в `duckdb_query`. Не пытаться генерировать DDL/DML.

## CLI (standalone)

CLI — отдельный способ запустить capability навыка, **не Tool**.
CLI не зависит от nanobot runtime (TARGET §11). Используется для
бенчмарков и ручных прогонов.

Запуск через `python scripts/cli.py`:

```bash
# predefined — именованные отчёты из реестра
python scripts/cli.py --mode predefined --script analytics_by_year_month --params year=2024
python scripts/cli.py --mode predefined --script violations_by_type \
                     --params '{"date_from": "2024-01-01"}'

# sql — LLM-генерация SELECT с retry-циклом
python scripts/cli.py --mode sql --query "топ-10 объектов по количеству нарушений"

# vector — семантический поиск напрямую через CacheProvider
python scripts/cli.py --mode vector --query "финансовые нарушения" \
                     --index-name violations_index --top-k 3
```

Параметры:

| Аргумент | Обязательный | Описание |
|:---|:---:|:---|
| `--mode` | да | Режим: `predefined`, `sql`, `vector` |
| `--script` | для `predefined` | Имя скрипта из `public.agent_predefined_scripts` |
| `--query` | для `sql`/`vector` | Запрос на естественном языке |
| `--params` | нет | Параметры скрипта: `key=value` или JSON |
| `--index-name` | для `vector` | Имя FAISS-индекса |
| `--top-k` | нет | Кол-во результатов (по умолч. 5) |
| `--threshold` | нет | Порог схожести 0.0–1.0 |
| `--vector-index` | нет | Каталог с FAISS-индексами (override) |
| `--context` | нет | История чата в JSON (для sql-режима) |

## Контракт зависимостей

Skill импортирует только shared infrastructure из `lib/utils/`:

- `lib/utils/sql_safety.py::validate_sql`, `format_schema`
  (через back-compat re-export в `scripts/database.py`);
- `lib/utils/text_utils.py::sanitize_value`
  (через back-compat re-export в `scripts/output.py`).

Skill **не импортирует** `workspace/tools/*` (TARGET §22.2) и не
требует изменений в `lib/` для своей работы. Это позволяет запускать
CLI skill'а в изоляции — без gateway или других частей проекта.

## Predefined reports (быстрый reference)

Имена 6 именованных отчётов в `public.agent_predefined_scripts`:

- `analytics_by_year_month` — аналитика проверок по годам/месяцам.
- `violations_by_type` — статистика нарушений по кодам.
- `top_audited_objects` — топ проверяемых объектов.
- `audit_effectiveness` — оценка эффективности проверок.
- `audit_dynamics` — динамика проверок по периодам.
- `audit_types_stats` — статистика по типам проверок.

Полные параметры каждого скрипта — в `public.agent_predefined_scripts`
(реестр в PostgreSQL). Загрузить при необходимости.