---
name: audit_analyzer
description: Анализ аудиторских проверок — три режима получения данных: predefined SQL-скрипты, семантический поиск, генерация SQL по запросу.
metadata: {"nanobot":{"emoji":"📊","always":true}}
---

# Audit Analyzer

Навык для работы с данными аудиторских проверок (нарушения, отчёты,
плановые/фактические даты). Три режима получения данных.

## Три режима

### 1. Predefined — приоритет 1

Используй, когда запрос **точно** соответствует одному из скриптов
в каталоге ниже.

Через CLI:

```bash
python workspace/skills/audit_analyzer/scripts/cli.py \
    --mode predefined \
    --script <имя скрипта> \
    --params '{...}'
```

### 2. Vector search — приоритет 2

Используй для семантического поиска («найди похожие …», «… про X»).

Через CLI:

```bash
python workspace/skills/audit_analyzer/scripts/cli.py \
    --mode vector \
    --query '...' \
    --index-name <имя индекса> --top-k 5
```

### 3. Генерация SQL — fallback

Используй, когда первые два режима не подходят: агрегации, COUNT,
GROUP BY, JOIN, точные фильтры. Передай запрос словами — навык сам
сгенерирует SQL и получит данные.

## Decision tree

```
Q: Запрос ТОЧНО соответствует одному из 5 predefined scripts
   И все обязательные параметры известны?
  YES → --mode predefined
  NO ↓

Q: Запрос про смысл/похожие, а не точные числа?
  YES → --mode vector с index_name из каталога ниже
  NO ↓

  --mode generated_sql с запросом словами
```

## Каталог predefined scripts

| Скрипт | Назначение |
|---|---|
| `audit_status_summary` | Сводка по статусам аудитов |
| `top_violations_by_type` | Топ кодов нарушений |
| `violations_by_period` | Нарушения за период (нужны `date_from`, `date_to`) |
| `audits_by_period` | Проверки за период (нужны `date_from`, `date_to`) |
| `audit_effectiveness_summary` | Сводка эффективности (проверки × нарушения × severity) |

Подробности и схемы параметров — в `references/predefined_scripts.md`.

## Каталог vector indexes

| Индекс | Назначение |
|---|---|
| `audits_index` | Поиск проверок по смыслу заголовка |
| `violations_index` | Поиск нарушений по смыслу описания |
| `audit_reports_index` | Поиск по отчётам целиком |

Подробности — в `references/vector_indexes.md`.

## Жёсткие правила

- Не выбирай `index_name` сам — только из каталога выше.
- Не выдумывай скрипт predefined — только из каталога выше.
- Не используй vector search для COUNT / GROUP BY / точных фильтров.
- Не выбирай predefined, если **обязательных параметров** нет — переходи
  к генерации SQL.

## Используемые runtime tools

Навык оперирует только двумя runtime-инструментами из `workspace/tools/`:

- **`vector_search`** — для семантического поиска (векторные индексы из
  каталога выше). **Запрещено** делать семантический поиск через
  `LIKE '%...%'` в SQL — это даёт неточный результат и ломает инвариант
  "семантический поиск = vector_search".
- **`duckdb_query`** — для выполнения SQL (как predefined-скрипты,
  так и ad-hoc SELECT для агрегаций / COUNT / GROUP BY / JOIN / точных
  фильтров). Все SQL-запросы выполняются параметризованно, никакого
  string-concatenation пользовательского ввода.

**Запрещённые инструменты** (архитектурный guard):

- `run_predefined_script` — удалён; **не вызывай** его. Predefined
  вызывается через `predefined.run()` и/или через CLI-режим
  `--mode predefined`.
- `nl_sql_generate` — удалён; **не вызывай** его. Генерация SQL
  делается агентом напрямую и затем передаётся в `duckdb_query`.
- `scripts/sql_generator.py` — удалённый helper для ad-hoc SQL;
  **не используй** его — формируй SELECT сам и передавай в `duckdb_query`.

Если требуется capability, которая раньше делалась удалёнными
инструментами — обращайся к `predefined.run()` (для known SQL) или
формируй SELECT сам и передавай в `duckdb_query` (для ad-hoc).

## References

- `references/schema.md` — структура таблиц.
- `references/vector_indexes.md` — каталог векторных индексов.
- `references/sql_guidance.md` — правила формирования SQL.
- `references/predefined_scripts.md` — каталог predefined scripts.
- `references/architecture.md` — архитектурный контракт skill'а.
