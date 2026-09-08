---
name: audit_analyzer
description: Анализ аудиторских проверок — два способа получения данных (predefined SQL через PG-таблицу + duckdb_query, и vector_search), плюс свободный SQL через duckdb_query.
metadata: {"nanobot":{"emoji":"📊","always":true}}
---

# Audit Analyzer

Ты работаешь с данными аудиторских проверок.

Для получения данных доступны **только два generic tool'а**:

- `duckdb_query`
- `vector_search`

Других способов получения audit data не используй. Любой SQL идёт
через `duckdb_query`.

## 1. Predefined SQL

Сначала проверь каталог ниже. Подробности — в
`references/predefined_scripts.md`.

| Script | Параметры | Назначение |
|---|---|---|
| `audit_status_summary` | — | Сводка по статусам аудитов |
| `top_violations_by_type` | — | Топ кодов нарушений |
| `violations_by_period` | `date_from`, `date_to` (YYYY-MM-DD, обязательны) | Нарушения за период |
| `audits_by_period` | `date_from`, `date_to` (YYYY-MM-DD, обязательны) | Проверки за период |
| `audit_effectiveness_summary` | — | Сводка эффективности (проверки × нарушения × severity) |

Если запрос **точно соответствует** одному из predefined script'ов:

1. прочитай его `sql_template` и `parameters` через
   `duckdb_query(sql="SELECT name, sql_template, parameters FROM "
   "public.agent_predefined_scripts WHERE name = ?", params=["<name>"])`;
2. определи значения параметров из пользовательского запроса;
3. вызови `duckdb_query(sql="<sql_template>", params={...})`.

**Predefined всегда приоритетнее**, потому что результат детерминирован
и SQL заранее проверен. Не используй predefined, если обязательных
параметров нет.

## 2. Vector search

Используй `vector_search`, если пользователь ищет информацию
по **смыслу** («найди похожие …», «что-нибудь про X»).

Каталог индексов (подробности и score-интерпретация — в
`references/vector_indexes.md`):

| Index | Источник | Embed-колонка | Когда использовать |
|---|---|---|---|
| `audits_index` | `oarb.audits` | `title` | поиск проверок по смыслу заголовка |
| `violations_index` | `oarb.violations` | `description` | поиск нарушений по смыслу описания |
| `audit_reports_index` | `oarb.audit_reports` | `title`, `full_text` | поиск по отчётам целиком |

Не используй `vector_search` для числовых агрегаций.

## 3. Свободный SQL

Если predefined и vector search не подходят:

1. прочитай `references/schema.md` для понимания данных;
2. прочитай `references/sql_guidance.md` для правил формирования SQL;
3. сформируй `SELECT` сам (или вызови skill-side helper
   `scripts/sql_generator.py` через `exec`, если предпочитаешь);
4. выполни его через `duckdb_query`;
5. если tool вернул `sql_error` — прочитай `message`, исправь SQL,
   повтори `duckdb_query`.

Retry — задача Agent, не отдельного сервиса.

## Правила

- SQL выполняй **только** через `duckdb_query`.
- Не выполняй SQL через `exec` / `python`.
- Не придумывай таблицы и колонки — читай `references/schema.md`.
- Не придумывай `index_name` — бери только из `references/vector_indexes.md`.
- Не используй `vector_search` для COUNT / GROUP BY / точных фильтров.
- Не используй SQL (`LIKE '%...%'`) для семантического поиска.
- Не создавай Python wrapper только ради вызова tool.
- Не вызывай удалённые инструменты `run_predefined_script` /
  `nl_sql_generate` — их больше нет.

## References

- `references/schema.md` — структура таблиц `oarb.*`.
- `references/vector_indexes.md` — каталог FAISS-индексов.
- `references/sql_guidance.md` — правила формирования SQL.
- `references/predefined_scripts.md` — каталог predefined scripts.
- `references/architecture.md` — архитектурный контракт skill'а.

## Skill-side helper (опционально)

`scripts/sql_generator.py` — автономный генератор SQL по NL-запросу
через прямой LLM API-вызов. Используй его, если предпочитаешь не
генерировать SQL сам. Helper возвращает только SQL, выполнение — через
`duckdb_query`.

Пример:

```bash
python scripts/sql_generator.py \
    --query "Сколько проверок за 2024 год?" \
    --schema-file references/schema.md \
    --tables oarb.audits
```
