---
name: audit_analyzer
description: Анализ аудиторских проверок — три способа получения данных: predefined SQL-скрипты, семантический поиск по FAISS, LLM-генерация SELECT как fallback. Не знает про конкретные ресурсы datasource — каталог рендерится runtime.
metadata: {"nanobot":{"emoji":"📊","always":true}}
---

# Audit Analyzer

Анализ аудиторских проверок: нарушения, отчёты, плановые/фактические даты.

Skill **не знает** ни одного физического имени таблицы, индекса или скрипта
текущего datasource. Каталог доступных ресурсов рендерится runtime'ом из
auto-populated env-vars `SKILL_<NAME>_*` (см. `lib/utils/skill_catalog.py`).
Маркеры `{{...}}` ниже заменяются на актуальные таблицы при загрузке SKILL.md.

## Три способа получения данных

Получить audit data можно **только** одним из трёх способов. Agent выбирает
способ сам по каталогам ниже. Никаких других путей нет.

### 1. PREDEFINED SCRIPT — приоритет 1

Используй, если запрос пользователя **соответствует** одному из скриптов
в каталоге **Predefined scripts** (рендерится runtime, см. ниже).

Преимущества: SQL заранее проверен, результат детерминирован, LLM не вызывается.

```
run_predefined_script(name="<из каталога>", params={...})
```

### 2. VECTOR SEARCH — приоритет 2

Используй для **семантического поиска**: «найди похожие …», «… про X».
Выбери `index_name` **только** из каталога **Vector indexes** (рендерится
runtime, см. ниже).

```
vector_search(query="...", index_name="<из каталога>")
```

### 3. NL → SQL — fallback

Используй, **только если** первые два способа не подходят. Это единственный
путь для свободных SQL-запросов: COUNT, GROUP BY, JOIN'ы, фильтры по колонкам.

```
nl_sql_generate(query="...")
```

## Decision tree

```
Q: Запрос соответствует одному из predefined scripts из runtime-каталога?
  YES → run_predefined_script
  NO ↓

Q: Запрос — про смысл/похожие?
  YES → vector_search с index из runtime-каталога
  NO ↓

  nl_sql_generate
```

## Жёсткие правила

- **Никогда** не выполняй SELECT напрямую. Любой SQL идёт через один из
  трёх tools выше.
- **Никогда** не выбирай `index_name` автоматически — только из runtime-каталога.
- **Никогда** не ищи predefined scripts через другой tool. Если скрипта
  нет в каталоге — значит его нет.
- **Никогда** не используй SQL для задачи, которую решает predefined script.
- **Никогда** не используй vector search для агрегаций и числовых расчётов.
- **Никогда** не используй NL→SQL, если задача решается vector search.

### Что значит «соответствует predefined»

Выбирай predefined script **только если выполняются оба условия**:

1. **Весь смысл** запроса соответствует назначению скрипта (см. `long_description`
   в `agent_predefined_scripts`).
2. **Параметры** запроса позволяют выполнить скрипт (например, для скриптов
   с period обе даты должны быть заданы).

Похожее слово в запросе ≠ подходящий predefined. Например:

- «покажи нарушения» без периода — **не** predefined с period
  (период не указан).
- «топ нарушений за 2024» — **не** `top_*`, если скрипт не принимает период.
- «сводка по статусам похожих проверок» — **не** `*_summary`, если нужен
  дополнительный фильтр; не реализован в скрипте.

В таких случаях — переходи к режиму 2 (vector) или 3 (NL→SQL).

## Predefined scripts

{{SCRIPTS_CATALOG}}

## Vector indexes

{{VECTORS_CATALOG}}

## Доменные правила (НЕ физическая schema)

Эти правила применяются **вне зависимости от datasource**:

- **Whitelist таблиц**: `nl_sql_generate` использует только таблицы из
  `TableRegistry` (рендерятся в SKILL.md через `{{TABLES_CATALOG}}`,
  если маркер присутствует). Все запросы идут через whitelist.
- **SQL safety**: только SELECT/WITH/EXPLAIN. Никаких DDL/DML.
- **Семантика enum-значений**: если колонка имеет `allowed_values` в
  runtime-БД (CHECK constraint или lookup), они подмешиваются в
  system prompt `nl_sql_generate`. LLM не должна выдумывать значения.
- **Каталог динамический**: при добавлении новой таблицы/скрипта/индекса
  в datasource-конфигурацию каталог в этом SKILL.md обновится автоматически
  при следующем запуске gateway (через `ApplicationContext._populate_skill_catalog_env`).

## Что НЕ делать

- ❌ Не редактируй каталог выше руками — он генерируется runtime'ом.
  Используй `python tools/render_skill_catalog.py audit_analyzer --check`
  в CI, чтобы поймать расхождение SKILL.md и runtime-каталога.
- ❌ Не подставляй имена таблиц/индексов/скриптов руками в вызовы tools —
  бери только из каталога выше.
- ❌ Не галлюцинируй enum-значения — если нет в runtime-БД, уточни у пользователя.
- ❌ Не используй прямой SQL-доступ в обход трёх capability.

## Архитектура

- `references/` — папка удалена. Технические детали — в `docs/ARCHITECTURE.md`.
- Auto-populated env-vars `SKILL_<NAME>_*` — см. `lib/utils/skill_catalog.py`
  и `docs/ARCHITECTURE.md` (раздел «Skill catalog rendering»).
- Adding/removing/renaming datasource resources — config/DB operation,
  не правка skill'а. Это архитектурный инвариант
  (`docs/TARGET_ARCHITECTURE.md`).
