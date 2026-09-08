---
name: audit_analyzer
description: Анализ аудиторских проверок — три режима получения данных: predefined SQL-скрипты, семантический поиск по FAISS, свободный SQL через duckdb_query.
metadata: {"nanobot":{"emoji":"📊","always":true}}
---

# Audit Analyzer

Ты работаешь с данными аудиторских проверок (нарушения, отчёты,
плановые/фактические даты).

Доступны **ровно три способа** получения данных. Agent выбирает режим сам
по каталогам ниже (decision tree в `references/predefined_scripts.md`,
`references/vector_indexes.md`, `references/sql_guidance.md`).

## Три режима получения данных

### 1. PREDEFINED — приоритет 1

Используй, если запрос **точно** соответствует одному из 5 скриптов
в каталоге (см. `references/predefined_scripts.md`).

Преимущества:

- SQL заранее проверен и валидирован;
- результат детерминирован;
- LLM не вызывается;
- параметры валидируются по типизированной схеме.

### Запуск через CLI

Навык поставляет CLI с тремя режимами (`scripts/cli.py --mode ...`).

Целевое использование из runtime / shell / тестов:

```bash
# Predefined script
python workspace/skills/audit_analyzer/scripts/cli.py \
    --mode predefined \
    --script violations_by_period \
    --params '{"date_from": "2024-01-01", "date_to": "2024-12-31"}'

# NL → SQL (требует LLM-ключ в окружении)
    python workspace/skills/audit_analyzer/scripts/cli.py \
        --mode generated_sql \
        --query 'сколько аудитов в 2024 по месяцам'

# Vector search
python workspace/skills/audit_analyzer/scripts/cli.py \
    --mode vector \
    --query 'пожарная безопасность' \
    --index-name audits_index --top-k 5
```

JSON-результат (плоский, для парсинга в агенте) — в stdout.

### 2. VECTOR SEARCH — приоритет 2

Используй для **семантического поиска**: «найди похожие …», «… про X».
Выбери `index_name` **только** из каталога
`references/vector_indexes.md` — никаких других имён.

```
vector_search(query="...", index_name="<из каталога>", top_k=5)
```

### 3. SQL — fallback

Используй **только** если первые два режима не подходят. Это путь для
свободных аналитических запросов и агрегаций: COUNT, GROUP BY, JOIN, фильтры по колонкам.

```
duckdb_query(sql="SELECT ... FROM oarb.<table> WHERE ...", params={...})
```

Agent формирует SQL **сам** по `references/schema.md` и
`references/sql_guidance.md` (никаких SQL-generator-helper'ов).
Если `duckdb_query` вернул `sql_error` — исправь SQL и повтори
(retry выполняет Agent, не отдельный сервис).

## Decision tree

```
Q: Запрос точно соответствует одному из 5 predefined scripts?
  YES → python scripts/cli.py --mode predefined --script NAME --params '{...}'
  NO ↓

Q: Запрос про смысл/похожие (не точные числа)?
  YES → vector_search(index_name=<из vector_indexes.md>)
  NO ↓

  duckdb_query(sql=<формирует Agent сам>)
```

## Каталог predefined scripts

| Script | Параметры | Назначение |
|---|---|---|
| `audit_status_summary` | — | Сводка по статусам аудитов |
| `top_violations_by_type` | — | Топ кодов нарушений |
| `violations_by_period` | `date_from`, `date_to` (ISO-date, обязательны) | Нарушения за период |
| `audits_by_period` | `date_from`, `date_to` (ISO-date, обязательны) | Проверки за период |
| `audit_effectiveness_summary` | `min_violations` (опц.) | Сводка эффективности (проверки × нарушения × severity) |

Подробности и длинные описания — в `references/predefined_scripts.md`.

## Каталог vector indexes

| Index | Источник | Когда использовать |
|---|---|---|
| `audits_index` | `oarb.audits` (embed: `title`) | поиск проверок по смыслу заголовка |
| `violations_index` | `oarb.violations` (embed: `description`) | поиск нарушений по смыслу описания |
| `audit_reports_index` | `oarb.audit_reports` (embed: `title`, `full_text`) | поиск по отчётам целиком |

Подробности — в `references/vector_indexes.md`.

## Жёсткие правила

- SQL выполняй **только** через `duckdb_query` (не через `exec` / `python`).
- Не выбирай `index_name` сам — только из `references/vector_indexes.md`.
- Не выдумывай скрипт predefined — только из каталога выше.
- Не вызывай удалённые инструменты `run_predefined_script` /
  `nl_sql_generate` / `column_descriptions` — их больше нет.
- `predefined.run()` валидирует параметры сам; `vector_search` валидирует
  `index_name` через runtime-реестр; `duckdb_query` валидирует SQL через
  `validate_sql` (SELECT-only).
- Не используй `vector_search` для COUNT / GROUP BY / точных фильтров.
- Не используй `LIKE '%...%'` через `duckdb_query` для семантического поиска.
- Не выбирай `predefined`, если **обязательных параметров** нет — переходи
  к `duckdb_query` (см. `references/sql_guidance.md`).

## References

- `references/schema.md` — структура таблиц `oarb.*`.
- `references/vector_indexes.md` — каталог FAISS-индексов.
- `references/sql_guidance.md` — правила формирования SQL.
- `references/predefined_scripts.md` — каталог predefined scripts.
- `references/architecture.md` — архитектурный контракт skill'а.

## Что нельзя делать

- Не использовать `public.agent_predefined_scripts` через `duckdb_query` —
  реестр SQL хранится в `predefined/scripts.py` (внутри skill'а).
- Не использовать `/python scripts/sql_generator.py` или любой другой
  LLM-helper для генерации SQL — Agent формирует SQL сам.
- Не передавать сгенерированный SQL в `predefined.run()` — это для
  использования `duckdb_query`.
- Не выдумывать SQL для задачи, которую решает `predefined` script — определи
  params и вызови `predefined.run()`.
