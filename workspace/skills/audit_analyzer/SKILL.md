---
name: audit_analyzer
description: Анализ аудиторских проверок — SQL-отчёты по oarb.*, семантический поиск по FAISS, LLM-генерация SELECT с retry.
metadata: {"nanobot":{"emoji":"📊","always":true}}
---

# Audit Analyzer

Анализ аудиторских проверок: нарушения, отчёты, плановые/фактические даты.

## Что есть в данных

**Таблицы PostgreSQL** (полные схемы — `references/schema.md`):

- `oarb.audits` — аудиторские проверки (id, title, audit_type, planned_date, actual_date, status, auditee_entity).
- `oarb.audit_reports` — отчёты о проверках (id, audit_id, report_number, report_date, full_text).
- `oarb.report_items` — пункты отчётов (id, report_id, item_number, item_title, item_content).
- `oarb.violations` — нарушения (id, audit_id, violation_code, description, severity, status, responsible, deadline).

**Vector-индексы** (назначение — `references/vector_indexes.md`):

- `audits_index` — поиск по заголовкам проверок (default).
- `violations_index` — поиск по описаниям нарушений.
- `audit_reports_index` — поиск по полным текстам отчётов.

**Predefined reports** (реестр `public.agent_predefined_scripts`):

- `analytics_by_year_month` — аналитика проверок по годам/месяцам.
- `violations_by_type` — статистика нарушений по кодам.
- `top_audited_objects` — топ проверяемых объектов.
- `audit_effectiveness` — оценка эффективности проверок.
- `audit_dynamics` — динамика проверок по периодам.
- `audit_types_stats` — статистика по типам проверок.

## Что использовать

| Задача | Capability |
|---|---|
| Аггрегация / фильтр / группировка | `duckdb_query` с явным SELECT |
| Семантический поиск похожих документов | `vector_search` с `index_name` |
| Сначала найти похожие, потом посчитать | `vector_search` → `duckdb_query` (WHERE id IN (...)) |
| Готовый отчёт из реестра | Запустить CLI `python scripts/cli.py --mode predefined --script NAME --params '...'` через свой shell-инструмент |
| Свободный NL-вопрос → SQL | Прочитать `references/sql_guidance.md`, сформулировать SELECT, передать в `duckdb_query` |

## Что не делать

- Не использовать неизвестные таблицы или индексы.
- Не использовать DDL/DML — `duckdb_query` запрещает (`INSERT/UPDATE/DELETE/DROP/...` отвергаются).
- Не подставлять пользовательские значения в SQL строкой — использовать `params` (`:year`, `:date_from`).

## CLI (если агент хочет запустить готовый отчёт)

```bash
python scripts/cli.py --mode predefined --script analytics_by_year_month --params '{"year": 2024}'
python scripts/cli.py --mode sql --query "топ-10 объектов по нарушениям"
python scripts/cli.py --mode vector --query "финансовые нарушения" --index-name violations_index --top-k 5
```

CLI — это не Tool и не зависит от nanobot runtime. Используется для
предопределённых отчётов из реестра и LLM-генерации SQL в retry-цикле.

## References

- `references/schema.md` — структура таблиц `oarb.*`.
- `references/vector_indexes.md` — описание FAISS-индексов.
- `references/sql_guidance.md` — правила NL → SELECT.

Загружать по необходимости — когда уже выбрана capability и нужны
точные имена колонок, индексов или формат SELECT.