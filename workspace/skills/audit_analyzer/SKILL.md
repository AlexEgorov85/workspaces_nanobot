---
name: audit_analyzer
description: Анализ аудиторских проверок — три режима получения данных: predefined SQL-скрипты, семантический поиск по FAISS, LLM-генерация SELECT как fallback.
metadata: {"nanobot":{"emoji":"📊","always":true}}
---

# Audit Analyzer

Анализ аудиторских проверок: нарушения, отчёты, плановые/фактические даты.
Skill **использует только tool'ы** (никаких внутренних CLI/скриптов — всё
выполнение идёт через generic infrastructure tools).

## Режимы получения данных

Ты **сам** выбираешь один из трёх режимов — никакой tool не решает за тебя,
какой режим применить. Решение принимается на основании запроса пользователя
и каталогов ниже.

### 1. Predefined scripts (приоритет 1)

Используй, если запрос пользователя **точно** соответствует одному из
скриптов, перечисленных в разделе **Predefined scripts** ниже.

Преимущества:
- SQL заранее проверен и валидирован;
- результат детерминирован;
- LLM не вызывается — нулевая latency и расход токенов;
- параметры валидируются по JSONB-схеме.

Tool:
```
run_predefined_script(name="<из каталога>", params={"date_from": "...})
```

Если имя не знаешь или оно не подходит — переходи к режиму 2.

### 2. Семантический поиск (приоритет 2)

Используй, если запрос про **смысл**, а не точные числа/агрегации:
«найди похожие нарушения», «похожие отчёты», «проверки по X».

Tool:
```
vector_search(query="...", index_name="<из каталога>")
```

`index_name` берётся **только** из раздела **Vector indexes** ниже. Никаких
других имён — если подходящего индекса нет, переходи к режиму 3.

### 3. NL → SELECT (приоритет 3, fallback)

Используй **только** если ни predefined script, ни vector index не подходят.
Типичные случаи: точные подсчёты, GROUP BY, фильтры по нескольким колонкам,
динамика по периодам, JOIN'ы.

Tool:
```
nl_sql_generate(query="...")
```

Tool подтянет hints через `column_descriptions` и выполнит SELECT в общем
DuckDB-кеше.

## Decision tree

```
Q: Запрос пользователя точно соответствует одному из predefined скриптов?
  YES → run_predefined_script
  NO ↓

Q: Запрос — про смысл/похожие, а не про точные агрегации?
  YES → vector_search (выбери index из каталога)
  NO ↓

  nl_sql_generate
```

### Примеры выбора

| Запрос пользователя | Режим | Tool / параметры |
|---|---|---|
| «Сводка по статусам аудитов» | predefined | `run_predefined_script(name="audit_status_summary")` |
| «Нарушения за 2024 год» | predefined | `run_predefined_script(name="violations_by_period", params={"date_from": "2024-01-01", "date_to": "2024-12-31"})` |
| «Найди похожие нарушения про пожарную безопасность» | vector | `vector_search(query="пожарная безопасность", index_name="violations_index")` |
| «Сколько проверок было в 2025 году?» | NL→SQL | `nl_sql_generate(query="сколько проверок в 2025")` |
| «Топ-5 организаций по числу нарушений» | NL→SQL | `nl_sql_generate(query="топ-5 организаций по числу нарушений")` |
| «Покажи динамику проверок по месяцам 2024» | NL→SQL | `nl_sql_generate(query="проверки по месяцам 2024")` |
| «Что в violations.description про охрану труда?» | vector | `vector_search(query="охрана труда", index_name="violations_index")` |

### Когда НЕ использовать

| Запрос | Не делай |
|---|---|
| COUNT / GROUP BY / ORDER BY | не вызывай `vector_search` |
| Точный SQL уже знаешь | не вызывай `nl_sql_generate` — используй `duckdb_query(sql="...")` |
| Несколько JOIN'ов и сложных условий | не вызывай `vector_search` |
| Семантика без чёткого числа | не вызывай `nl_sql_generate` — это не агрегация |

## Predefined scripts

Источник истины — таблица `public.agent_predefined_scripts` (DDL:
`sql/audit_analyzer/create_public_agent_predefined_scripts.sql`).
Через `PgDuckDbSyncService` данные попадают в общий runtime-снапшот DuckDB,
откуда их читает tool `run_predefined_script`.

> Если скрипта из каталога нет в реестре — будет `script_not_found`.
> Прежде чем звать `run_predefined_script(name=...)` для редкого скрипта,
> можешь проверить через `duckdb_query(sql="SELECT name FROM public.agent_predefined_scripts ORDER BY name")`.

### Известные скрипты на этой инсталляции

Каталог ниже заполняется администратором БД. Чтобы получить **актуальный**
список, выполни:

```
duckdb_query(sql="SELECT name, description, parameters FROM public.agent_predefined_scripts ORDER BY name")
```

**Стандартный набор** (может отличаться от заполненного в БД):

| Script | Когда использовать | Параметры |
|---|---|---|
| `audit_status_summary` | Сводка по статусам аудитов (Завершена / В работе / Запланирована) | нет |
| `top_violations_by_type` | Топ типов/кодов нарушений | нет |
| `violations_by_period` | Нарушения за период с фильтром по дате | `date_from` (date, required), `date_to` (date, required) |
| `audits_by_period` | Аудиторские проверки за период | `date_from` (date, required), `date_to` (date, required) |
| `audit_effectiveness_summary` | Сводка по эффективности: проверки × нарушения × severity | нет |

Если имена из таблицы выше не совпадают с реальным реестром — следуй
реальному реестру (через `duckdb_query`), а не этому каталогу. Каталог —
это **навигация**, источник истины — таблица в БД.

### Подробное описание каждого скрипта

#### `audit_status_summary`

- **Назначение**: агрегация `oarb.audits` по `status`.
- **Когда использовать**: пользователь спрашивает «сколько аудитов по статусам» / «распределение проверок по статусам».
- **Когда НЕ использовать**: когда нужны подробности по конкретным проверкам — здесь только счётчик.
- **Параметры**: нет.

#### `top_violations_by_type`

- **Назначение**: топ кодов нарушений (`oarb.violations.violation_code`).
- **Когда использовать**: «самые частые нарушения», «топ кодов», «нарушения по типам».
- **Когда НЕ использовать**: когда нужны нарушения по конкретному коду (нужен `nl_sql_generate` с WHERE).
- **Параметры**: нет.

#### `violations_by_period`

- **Назначение**: нарушения в заданный период.
- **Когда использовать**: «нарушения за 2024», «что было выявлено в Q1».
- **Когда НЕ использовать**: когда период не указан и неочевиден.
- **Параметры**: `date_from`, `date_to` — обе обязательные даты в ISO-формате (`YYYY-MM-DD`).

#### `audits_by_period`

- **Назначение**: проверки в заданный период (по `actual_date`).
- **Когда использовать**: «проверки за 2024», «что проверяли в Q2».
- **Когда НЕ использовать**: когда период не указан.
- **Параметры**: `date_from`, `date_to`.

#### `audit_effectiveness_summary`

- **Назначение**: сводка эффективности — проверки × число нарушений × severity.
- **Когда использовать**: «какие проверки самые проблемные», «уровень серьёзности по проверкам».
- **Когда НЕ использовать**: когда нужны JOIN'ы по другим таблицам.
- **Параметры**: нет.

## Vector indexes

Доступные FAISS-индексы перечислены ниже. `index_name` в `vector_search` —
**только** эти имена. Tool `vector_search` не знает про эти имена — выбор
за тобой.

Метаданные индексов (источник, embed-колонка, signature) живут в
`public.agent_vector_index_config`. Подробное описание —
в `references/vector_indexes.md`.

### Каталог индексов

| Index | Источник | Embed-колонка | Когда использовать |
|---|---|---|---|
| `audits_index` | `oarb.audits` | `title` | поиск проверок по смыслу заголовка |
| `violations_index` | `oarb.violations` | `description` | поиск нарушений по смыслу описания |
| `audit_reports_index` | `oarb.audit_reports` | `title`, `full_text` | поиск по отчётам целиком |

### Подробное описание каждого индекса

#### `audits_index`

- **Что индексируется**: `oarb.audits.title` — заголовок проверки.
- **Что можно найти**: проверки по теме («проверки по пожарной безопасности», «бухгалтерские ревизии», «проверки в школах»).
- **Когда использовать**: пользователь ищет проверки по **смыслу заголовка**.
- **Когда НЕ использовать**:
  - точные числа (COUNT/GROUP BY) — `nl_sql_generate`;
  - поиск по `id` — `duckdb_query WHERE id = ?`;
  - фильтры по `actual_date`/`status`/`audit_type` — `nl_sql_generate`;
  - поиск по содержимому отчётов — `audit_reports_index`.
- **Score-интерпрес**: `0.6+` — высокая схожесть; `0.4–0.6` — умеренная; `<0.4` — низкая.

#### `violations_index`

- **Что индексируется**: `oarb.violations.description` — описание нарушения.
- **Что можно найти**: «нарушения, похожие на …», «нарушения про X», семантически близкие случаи.
- **Когда использовать**: пользователь ищет **похожие** нарушения по смыслу.
- **Когда НЕ использовать**:
  - числовые агрегации;
  - фильтры по `severity`/`status`/`deadline` — `nl_sql_generate`;
  - точные коды (`WHERE violation_code = ...`) — `nl_sql_generate`.
- **Score-интерпрес**: `0.65+` — высокая; `0.5–0.65` — умеренная.

#### `audit_reports_index`

- **Что индексируется**: `oarb.audit_reports.title` + `full_text`.
- **Что можно найти**: «отчёты с выводами о неэффективности», «отчёты про X».
- **Когда использовать**: пользователь ищет **отчёты** по смыслу их содержания.
- **Когда НЕ использовать**:
  - точные данные (числа, статусы) — `nl_sql_generate`;
  - JOIN'ы с другими таблицами — `nl_sql_generate`.
- **Score-интерпрес**: `0.55+` — высокая; `0.4–0.55` — умеренная.

### Конвенции

- `top_k` ограничен `max_top_k` из конфига (`gateway.vector_search.max_top_k`, default 50).
- `threshold` в диапазоне `[0.0, 1.0]`. Default = `gateway.vector_search.default_threshold` (0.0 — без фильтра).
- Если запрашиваемый `index_name` не зарегистрирован — `vector_search` вернёт ошибку `missing_index`. Используй только имена из каталога выше.

## Доменные таблицы

> Имена таблиц — значения текущей инсталляции, настраиваются в
> `project.json` (`skills.audit_analyzer.tables[*].name`). В других
> развёртываниях могут отличаться; не зашивай их как константы.

- `oarb.audits` — аудиторские проверки.
- `oarb.violations` — нарушения.
- `oarb.audit_reports` — отчёты о проверках.
- `oarb.report_items` — пункты отчётов.

Подробности (колонки, связи) — в `references/schema.md`.
Подробности по написанию SELECT — в `references/sql_guidance.md`.

## Что не делать

- Не выдумывай имена predefined-скриптов, которых нет в каталоге и в реестре.
- Не вызывай `vector_search` с `index_name`, которого нет в каталоге.
- Не используй DDL/DML (запрещено во всех SQL-инструментах через
  `lib/utils/sql_safety.py::validate_sql`).
- Не подставляй пользовательские значения в SQL строкой — параметры
  передавай через `?` (для `duckdb_query.params`) или `params` (для
  `run_predefined_script`).
- Не вызывай `scripts/cli.py` (в skill'е больше нет CLI — он удалён,
  всё через tool'ы).
- **Не пытайся самостоятельно выбирать predefined/vector через keyword-search
  или fuzzy-match** — твоя задача прочитать каталог и выбрать по смыслу
  запроса. Tool'ы не делают auto-routing.

## Как работает NL → SELECT

`nl_sql_generate` использует общий pipeline в `lib/services/nl_sql_runner.py`:

1. Whitelist таблиц из `TableRegistry` (не знает про домен).
2. `SchemaFormatter` (internal service) формирует описание схемы для LLM.
3. `column_descriptions.lookup()` подмешивает подсказки термин→колонка
   в system prompt (если `skip_hints=False`).
4. Few-shot примеры из `public.agent_predefined_scripts` подмешиваются как
   контекст для LLM (но **не выполняются автоматически** — это просто подсказки).
5. LLM генерирует SELECT.
6. `validate_sql` — последняя граница безопасности (SELECT-only, single-statement).
7. `provider.explain()` — синтаксическая проверка.
8. `provider.query_sql()` — выполнение в общем DuckDB-кеше.
9. JSON-ответ с `sql`, `columns`, `rows`, `row_count`.

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

Tool вернёт top-k ближайших описаний нарушений с score.

## References

- `references/schema.md` — структура таблиц `oarb.*`.
- `references/vector_indexes.md` — расширенное описание FAISS-индексов.
- `references/sql_guidance.md` — правила формулировки SELECT.
- `references/predefined_scripts.md` — реестр `public.agent_predefined_scripts`
  и схема параметров.