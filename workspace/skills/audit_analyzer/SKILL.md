---
name: audit_analyzer
description: Анализ аудиторских проверок — SQL-отчёты, векторный поиск, генерация SQL через LLM.
metadata: {"nanobot":{"emoji":"📊","always":true}}
---

# Audit Analyzer

Domain Skill для анализа аудиторских проверок (нарушения, отчёты, плановые/фактические даты).

Этот skill использует **общие infrastructure tools** (`duckdb_query`, `vector_search`)
через агентский runtime. Skill не импортирует эти tool'ы напрямую — он описывает
процедуру выбора в `SKILL.md` и передаёт параметры в `tool_call`.

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
   |       -> run via scripts/predefined_mode.py (CLI / internal)
   |
   +-- NL -> SELECT (free-form question about the data)
   |       -> compose SELECT per references/sql_guidance.md,
   |          then duckdb_query
   |
   +-- do not use unknown tables or indexes
   +-- do not use DDL/DML (read-only — enforced by duckdb_query)
```

## Доступные данные (домен)

Skill владеет следующими знаниями:

- **Таблицы** (см. `references/schema.md`):
  - `oarb.audits`, `oarb.audit_reports`, `oarb.report_items`, `oarb.violations`.
- **Vector indexes** (см. `references/vector_indexes.md`):
  - `audits_index`, `violations_index`, `audit_reports_index`.
- **Predefined reports** (реестр `public.agent_predefined_scripts`):
  - `analytics_by_year_month`, `violations_by_type`, `top_audited_objects`,
    `audit_effectiveness`, `audit_dynamics`, `audit_types_stats`.

Skill **не знает** Python-реализацию tool'ов и не вызывает их программно.

## Workflows

### 1. Аггрегация / фильтр / группировка

Использовать tool `duckdb_query` с явным SELECT.

Пример (агрегация по годам):

```text
tool_call: duckdb_query(
    sql="SELECT EXTRACT(year FROM actual_date) AS year, COUNT(*) "
        "FROM audits GROUP BY year ORDER BY year",
    max_rows=10,
)
```

### 2. Семантический поиск

Использовать tool `vector_search` с явным `index_name`.

Пример (поиск похожих нарушений):

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

### 4. Predefined reports (реестр)

Использовать `scripts/predefined_mode.py` через CLI:

```bash
audit_analyze.sh --mode predefined --script analytics_by_year_month \
                 --params '{"year": 2024}'
```

Агенту использовать CLI-обёртку через свой shell-tool (см. SKILL.md в исходной
редакции — режим не выделен в отдельный tool, т.к. это deterministic workflow
skill'а, см. TARGET_ARCHITECTURE.md §9).

### 5. NL → SELECT

Прочитать `references/sql_guidance.md`, сформулировать SELECT,
передать в `duckdb_query`. Финальная граница безопасности — в tool'е
(`lib/utils/sql_safety.py::validate_sql`).

## CLI (standalone)

Запуск через `audit_analyze.bat` (Windows) или `audit_analyze.sh` (Linux):

```bash
# Windows (PowerShell / cmd) — key=value без кавычек:
audit_analyze.bat --mode predefined --script analytics_by_year_month --params year=2024
audit_analyze.bat --mode sql --query "топ-10 объектов по нарушениям"

# Векторный поиск:
audit_analyze.bat --mode vector --query "финансовые нарушения" \
                   --index-name violations_index --top-k 3

# Linux:
audit_analyzer.sh --mode predefined --script analytics_by_year_month \
                   --params '{"year": 2024}'
```

CLI — это не Tool. CLI использует `scripts/cli.py` напрямую и не зависит от
nanobot runtime (TARGET_ARCHITECTURE.md §11).

## References

Progressive disclosure (TARGET_ARCHITECTURE.md §10):

- `references/schema.md` — структура таблиц `oarb.*`.
- `references/vector_indexes.md` — назначение и метаданные FAISS-индексов.
- `references/sql_guidance.md` — правила NL→SELECT.

Агент загружает reference при необходимости — когда уже решил, какую
capability вызвать, и хочет уточнить параметры (имена таблиц/колонок,
имена индексов, формат SELECT). Не нужно держать всё содержимое в
контексте сразу.

## Predefined reports

Реестр `public.agent_predefined_scripts` содержит 6 именованных отчётов.
Skill знает их имена и параметры; агенту они известны из `SKILL.md`/decision
procedure. При необходимости — загрузить `references/schema.md` для уточнения
колонок.

## Контракт зависимостей

Skill не зависит от `lib/`, `workspace/tools/`, `nanobot`. Это позволяет
запускать CLI skill'а (см. `audit_analyze.bat/.sh`) в изоляции — без
запущенного gateway или других частей проекта (TARGET_ARCHITECTURE.md §11).