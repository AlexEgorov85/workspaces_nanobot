---
name: audit_analyzer
description: Анализ аудиторских проверок — три режима (predefined / vector / generated_sql).
metadata: {"nanobot":{"emoji":"📊","always":true}}
---

# Audit Analyzer

Навык для работы с данными аудиторских проверок (нарушения, отчёты,
плановые/фактические даты). Доступ к данным — **через три режима**, каждый
из которых является частью контракта навыка; CLI маршрутизирует
запросы и сериализует результат в плоский JSON.

## Три режима навыка

```text
                audit_analyzer
                       │
          ┌────────────┼────────────┐
          ↓            ↓            ↓
       predefined    vector    generated_sql
          │            │            │
          ↓            ↓            ↓
       DB scripts   Vector Core    LLM + schema
          │            │            │
          └────────────┼────────────┘
                       ↓
                    Core
                       ↓
              Cache / infrastructure
```

| Режим | Когда использовать |
|---|---|
| `predefined`     | Существующий SQL-скрипт **точно** соответствует запросу, параметры известны (или все optional). |
| `vector`         | Семантический поиск: похожие проверки / нарушения / отчёты / свободный текст. |
| `generated_sql`  | predefined не подходит, данные живут в доступной schema, задача решается свободным SQL. |

### Что нельзя

- Не делать **fallback между режимами**. Ошибка выбранного режима — это его ошибка, не повод перепрыгивать в другой.
- Не придумывать таблицы / колонки / индексы / имена скриптов. Они берутся строго из реестров и schema.
- Не выполнять DDL / DML вне режима predef'ined-контракта. `generated_sql` генерирует **только** SELECT.

## Когда выбирать какой режим

### `predefined`

Используй, **когда выполняются ОБА условия**:

1. **Весь смысл** запроса совпадает с назначением одного из 6 скриптов ниже.
2. **Параметры** либо явно известны, либо все параметры скрипта optional.

Примеры:

- «покажи нарушения за 2024» → подходит `violations_by_type` с `date_from=2024-01-01`.
- «топ-5 объектов по проверкам» → подходит `top_audited_objects` с `limit=5`.
- «сводка по статусам похожих проверок» → **не подходит ни один** (нужен свободный SQL — переходи в `generated_sql`).

### `vector`

Используй, **когда запрос требует семантического поиска**:

- похожие проверки / нарушения / отчёты;
- fuzzy-матчинг по произвольному тексту;
- синонимы / перифразы (когда exact-match по термину не срабатывает);
- семантически близкие документы / описания.

Используй **только** логические имена индексов из каталога ниже. Технические детали (FAISS, embeddings, blob-storage) не трогай — это слой Core.

### `generated_sql`

Используй, **когда predefined не подходит** и:

- данные лежат в **доступной schema** (whitelist задаётся ресурсной конфигурацией skill'а);
- задача решается свободным SQL (агрегации, JOIN, фильтры по датам/кодам);
- есть подходящие few-shot примеры из реестра predefined-скриптов (pass-through через `_select_few_shot`).

**Не выдумывай** таблицы и колонки. Если нужных данных нет в whitelist — LLM обязана вернуть `<NO_MATCH>` (это **честный** success-результат, не ошибка). Не подменяй таблицу на «похожую».

Retry — задача **Agent'а** (обычно 2–3 переформулировки), не skill'а.

## Источник predefined скриптов

Канонический источник SQL — `public.agent_predefined_scripts` в PostgreSQL.
Python `REGISTRY` (legacy) удалён в Phase 7; единственный путь — DB-first lookup
через `scripts/predefined/db_loader.py`. Sql-функция `load_all` живёт
в `predefined`-подсистеме и **не дублируется** в `generated_sql_mode`.

```
PostgreSQL
    ↓ seed/migration
public.agent_predefined_scripts (6 скриптов)
    ↓ PgDuckDbSyncService
DuckDB-PG snapshot (default ~/.cache/nanobot/duckdb/cache.duckdb — см. resolve_publish_path)
    ↓ db_loader.load_script / load_all
ScriptDefinition
    ↓ predefined.run(name, db, params, *, predefined_table=...)
CacheProvider.query_sql(sql, values)
    ↓
result {status, data.result.row_count / columns / rows}
```

В БД сейчас лежат **6 скриптов** (имя / назначение / параметры):

- `analytics_by_year_month` — аналитика проверок по годам и месяцам.
  Параметры: `year` (опц., число).
- `audit_dynamics` — динамика проверок по периодам (month/quarter/week).
  Параметры: `period` (опц., enum: month/quarter/week, default `month`), `date_from` (опц., date).
- `audit_effectiveness` — оценка эффективности: проверки × нарушения × severity.
  Параметры: `date_from`, `date_to`, `min_violations` (все опц.).
- `audit_types_stats` — статистика по типам проверок.
  Параметры: `audit_type` (опц., like), `date_from` (опц., date).
- `top_audited_objects` — топ проверяемых объектов по количеству проверок.
  Параметры: `auditee_entity` (опц., like), `date_from` (опц., date), `limit` (опц., default `10`).
- `violations_by_type` — статистика нарушений по типам.
  Параметры: `violation_code` (опц., like), `date_from` (опц., date).

**Контракт:** все параметры **optional**, ни один скрипт не требует
обязательных значений. Если параметр не передан — соответствующий
`{% if param %}` Jinja-блок в SQL не активируется, фильтр не применяется.

**`type=like` оборачивает значение в `%...%`.** Если параметр объявлен
как `like`, передача `"Плановая"` означает substring-поиск
(`ILIKE '%Плановая%'`) — это **не exact match**. Для exact match передавайте
точное значение без wildcard'ов вручную (если seed БД это позволяет),
либо используйте другой скрипт. Актуальный каталог и типы параметров —
через `--list-scripts`.

## Каталог vector indexes

Имена — логические; конфигурация в `project.json::gateway.vector.index.indexes.*`
(`VectorIndexConfig`). Skill передаёт **только** логические имена в generic
vector capability. Технические детали (как именно Core хранит индексы)
— забота Core, skill их не видит.

| Индекс | Бизнес-назначение |
|---|---|
| `audits_index`         | Семантический поиск по аудиторским проверкам. |
| `violations_index`     | Семантический поиск по нарушениям (по описанию и коду). |
| `audit_reports_index`  | Семантический поиск по полным текстам отчётов. |

Конфигурация (модель эмбеддингов, chunking, метрика, threshold) — в
`gateway.vector.index.indexes.<name>` проекта. PG-реестр
`public.agent_vector_index_config` остаётся в репо как legacy SQL-артефакт;
runtime его **не читает**.

### Конвенции vector (CLI --mode vector)

- `index_name` — строковый идентификатор из таблицы выше.
- `--top-k` (дефолт `5`, потолок `50`).
- `--threshold` (дефолт `0.0` = без фильтра, диапазон `[0.0, 1.0]`).
- `audits_index` — дефолт при `--mode vector` без `--index-name`.
- Неизвестный `--index-name` — ошибка (не авто-подбор другого).

## SQL guidance (режим generated_sql)

Режим `generated_sql` (NL→SQL через LLM) — **часть контракта навыка**:
используется для точных аналитических запросов, когда predefined
не подходит.

```bash
python scripts/cli.py --mode generated_sql --query '<запрос на NL>'
```

### Правила

- Генерируется только `SELECT` / `WITH` — `validate_sql`
  (`lib/utils/sql_safety.py`) отвергнет DDL/DML.
- Один statement. Без `; DROP ...`.
- Полностью квалифицированные имена таблиц: `schema.table` (например,
  `oarb.audits`).
- `LIMIT` добавляется автоматически.
- Если нужных данных нет в whitelist таблиц → LLM возвращает `<NO_MATCH>`.
  Это **честный success** (никаких подстановок «похожей» таблицы).

### Retry при ошибке

`generated_sql` CLI вернёт структурированный JSON:

```json
{
  "mode": "generated_sql",
  "status": "error",
  "data": { "message": "...", "sql": "..." }
}
```

Agent-цикл:

1. Прочитай `message`.
2. Переформулируй запрос (уточни таблицу / колонки / период).
3. Повтори вызов.

**Retry — задача Agent**, не skill'а. Внутри режима skill делает
свои `MAX_ATTEMPTS` попыток с передачей ошибки обратно в LLM для
исправления SQL — это не «смена режима», а фикс сгенерированного SQL.

### Пустой результат — нормально

```json
{"status": "success", "data": {"columns": [...], "rows": [], "row_count": 0}}
```

Не интерпретируй как сбой. Сообщи пользователю «нет данных за указанный
период».

## Доменная модель (бизнес-глоссарий)

Техническая schema (колонки, типы) — в DuckDB-кэше, читается через
`CacheProvider.get_schema()`. Никаких ручных копий schema в SKILL.md.

**Бизнес-термины домена:**

- **Проверка (Audit)** — запись о проведённой/плановой инспекции; сущность,
  через которую чаще всего идёт выборка.
- **Нарушение (Violation)** — факт, обнаруженный в проверке; имеет код,
  описание, severity, статус, ответственного и срок устранения.
- **Отчёт (Audit Report)** — документ по результатам проверки; содержит
  пункты (Report Items) и привязан к проверке.
- **Пункт отчёта (Report Item)** — структурная единица отчёта; к ней
  могут быть привязаны нарушения.

Связи между сущностями описываются в DB-реестре (`agent_predefined_scripts`,
где `sql_template` уже использует правильные JOIN). Для `generated_sql`
— LLM получает schema и few-shot через `predefined.db_loader.load_all`;
явный JOIN-гайд здесь не нужен, потому что примеры уже в few-shot.

## Жёсткие правила

- Не выдумывай скрипт predefined — только из каталога выше.
- Не выдумывай `index_name` — только из каталога vector indexes.
- Date-параметры — строго `YYYY-MM-DD` (валидация в `scripts/predefined/validator.py`).
- Не обращайся к `public.agent_predefined_scripts` через SQL напрямую —
  это реестр, а не доменная таблица.
- Не вызывай `exec` / `python` для выполнения SQL напрямую — только через CLI.

## Runtime boundary

**Skill** владеет:

- каталогом predefined-скриптов (через DB-реестр);
- правилами выбора режима по характеру запроса;
- форматом вывода `{mode, status, data}` (одинаков для всех трёх режимов).

**Skill не владеет:**

- логикой выполнения SQL (`CacheProvider.query_sql` / Core);
- выбором embedding-модели (захардкожено в `cache_provider_impl`;
  bearer-токен — из env `EMBED_TOKEN`);
- деталями хранения и валидации vector-индексов (Core);
- LLM-протоколами (`lib/services/llm_client.py` / Core).

Доступ агента — через CLI (`scripts/cli.py`). Generic tools
(`duckdb_query_tool`, `vector_search_tool`) удалены в Phase 8 —
Agent обращается к данным через маршрутизацию по CLI, и выбор режима —
обязанность Agent'а (decision tree выше).

## Как добавить новый predefined-скрипт

1. Сделать DDL в БД: `INSERT INTO public.agent_predefined_scripts (...)`.
2. Дождаться синхронизации (`PgDuckDbSyncService` опубликует снимок; путь —
   см. `resolve_publish_path()` в `lib/core/application_context.py`).
3. Описать в разделе «Каталог predefined scripts» этого файла.
4. Добавить тест в
   `tests/test_audit_analyzer_predefined.py`.

Удаление/изменение существующих скриптов — это DDL в БД, не правка
skill'а.

## Discovery (актуальный каталог)

Имена скриптов и индексов **не прописаны жёстко** в этом файле — они
читаются из БД при старте CLI. Чтобы получить актуальный каталог:

```bash
# Список predefined-скриптов (имя, описание, параметры)
python workspace/skills/audit_analyzer/scripts/cli.py --list-scripts

# Список FAISS-индексов (имя, источник, embed-колонки, chunking)
python workspace/skills/audit_analyzer/scripts/cli.py --list-indexes
```

Оба возвращают JSON в stdout и не требуют `--mode`. Ошибки доступа к БД
возвращаются как `{"status": "error", "data": {"error_type": "registry_unavailable", ...}}`.

## Тесты

- `tests/test_audit_analyzer_predefined.py` —
  contract-тесты 6 скриптов + параметры + DB-first lookup + no-fallback.
- `tests/test_audit_analyzer_behavior.py` —
  Agent-loop контракт.
- `tests/test_audit_analyzer_generated_sql.py` —
  generated_sql pipeline (validate_sql, EXPLAIN, MAX_ATTEMPTS, NO_MATCH,
  DDL/DML-отказ).
- `tests/test_audit_analyzer_cli.py` —
  CLI parser, mode routing, _run_vector validation.
- `tests/test_audit_analyzer_mode_selection.py` —
  три режима в SKILL.md (decision tree, ноль fallback'ов между ними).
