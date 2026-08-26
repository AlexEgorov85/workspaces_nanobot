---
name: audit_analyzer
description: Анализ аудиторских проверок — SQL-отчёты по oarb.*, семантический поиск по FAISS, LLM-генерация SELECT.
metadata: {"nanobot":{"emoji":"📊","always":true}}
---

# Audit Analyzer

Анализ аудиторских проверок: нарушения, отчёты, плановые/фактические даты.

Работает в **трёх режимах**. Выбирай режим под задачу, не комбинируй.

## Режимы работы

### 1. Predefined — готовые отчёты из реестра

Используй, когда вопрос пользователя совпадает с одним из готовых отчётов:

| Скрипт | Когда подходит |
|---|---|
| `analytics_by_year_month` | аналитика проверок по годам/месяцам |
| `violations_by_type` | статистика нарушений по кодам |
| `top_audited_objects` | топ проверяемых объектов |
| `audit_effectiveness` | оценка эффективности проверок |
| `audit_dynamics` | динамика проверок по периодам |
| `audit_types_stats` | статистика по типам проверок |

Запуск через CLI:

```bash
python scripts/cli.py --mode predefined --script analytics_by_year_month --params '{"year": 2024}'
```

Полный список скриптов и параметров — `references/schema.md`.

### 2. SQL — свободный вопрос на естественном языке

Используй, когда ни один predefined-скрипт не подходит, но вопрос
можно выразить через SELECT по доменным таблицам.

```bash
python scripts/cli.py --mode sql --query "топ-10 объектов по нарушениям"
```

Возвращает JSON с `rows`, `columns`, `sql`. Не подходит, если
нужен семантический поиск — для этого режим `vector`.

### 3. Vector — семантический поиск

Используй, когда нужно найти документы **по смыслу**, а не точному слову
(«пожарная безопасность», «финансовые нарушения», «дебиторка»).

| Индекс | Что ищет |
|---|---|
| `audits_index` | по заголовкам проверок |
| `violations_index` | по описаниям нарушений |
| `audit_reports_index` | по полным текстам отчётов |

```bash
python scripts/cli.py --mode vector --query "финансовые нарушения" --index-name violations_index --top-k 5
```

## Доменные таблицы и индексы

- `oarb.audits`, `oarb.violations`, `oarb.audit_reports`, `oarb.report_items` — что в них, см. `references/schema.md`.
- Vector-индексы — `references/vector_indexes.md`.
- Predefined-скрипты — реестр `public.agent_predefined_scripts`.

## Что не делать

- Не использовать неизвестные таблицы или индексы.
- Не использовать DDL/DML.
- Не подставлять пользовательские значения в SQL строкой — параметры через `:name` (для CLI) или `params` в `duckdb_query`.

## References

- `references/schema.md` — структура таблиц `oarb.*`.
- `references/vector_indexes.md` — описание FAISS-индексов.
- `references/sql_guidance.md` — правила формулировки SELECT.
