# Skill / Tool architecture

**Документ-контракт** для рефакторинга `refactor/skills-tools-cleanup`.
Цель — зафиксировать архитектурные правила и служить reference при code review.

---

## 1. Главный принцип

`Skill` и `Tool` — **независимые механизмы**.

Связь между ними — только через агентский runtime:

```text
SKILL instructions
        |
        v
      Agent
        |
        v
   selects Tool
        |
        v
Tool executes capability
```

Skill **не вызывает** Tool программно.
Tool **не импортирует** Skill.

---

## 2. Что разрешено

```python
# Skill
from lib.services.cache_provider_impl import build_cache_provider
from lib.utils.sql_safety import validate_sql

# Tool
from lib.services.cache_provider_impl import build_cache_provider
from lib.utils.sql_safety import validate_sql
```

Skill и Tool могут использовать **общую инфраструктуру** (`lib/utils`, `lib/services`).

---

## 3. Что запрещено

```python
# В Tool — ЗАПРЕЩЕНО
from workspace.skills.audit_analyzer import ...
from workspace.skills.audit_analyzer.scripts import ...
spec_from_file_location(...)
sys.path.insert(.../skills...)

# В Skill — ЗАПРЕЩЕНО
from workspace.tools import ...
from workspace.tools.duckdb_query_tool import ...
```

Это правило проверяется AST-тестами в `tests/test_skill_tool_independence.py`.

---

## 4. Что Tool не должен знать

- Названия конкретных Skills.
- Audit-таблицы (`oarb.audits`, `oarb.violations`, и т.п.).
- Audit-vector indexes (`audits_index`, `violations_index`).
- Бизнес-смысл параметров.
- Domain-specific routing (`if caller == "audit_analyzer"`).
- Конкретные отчёты, скрипты, registry.

Tool — это generic capability. Например, `duckdb_query` умеет делать SELECT,
`vector_search` умеет делать semantic search. Какой skill их использует — не его дело.

---

## 5. Что Skill не должен знать

- Конкретный Python-класс Tool.
- Конкретную реализацию Tool в коде.
- Внутренний contract Tool за пределами публичного (name, description, parameters).

Skill пишет инструкции **в терминах capability**, а не в терминах Python:
- ✅ «use `vector_search` with `index_name='violations_index'`»
- ❌ «call `VectorSearchTool.execute(query=...)`»
- ❌ «import VectorSearchTool»

---

## 6. Контракт `duckdb_query`

```json
{
  "sql": "SELECT year, count(*) FROM audits GROUP BY year",
  "params": { },
  "max_rows": 100
}
```

Ответ:

```json
{
  "status": "success",
  "columns": ["year", "count"],
  "rows": [[2024, 120]],
  "row_count": 1,
  "returned_rows": 1,
  "truncated": false
}
```

Ошибка:

```json
{
  "status": "error",
  "error_type": "sql_error",
  "message": "INSERT not allowed"
}
```

### Read-only policy

Разрешено: `SELECT`, `WITH ... SELECT`.
Запрещено: `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `CREATE`, `TRUNCATE`, `COPY`, `ATTACH`, `DETACH`, `INSTALL`, `LOAD`, `EXPORT`, `IMPORT`, `CALL`, multi-statement.

Реализация: `lib/utils/sql_safety.py::validate_sql` (последняя граница перед execution).

### Лимиты (configurable через `gateway.duckdb_query.*`)

| ключ | default | диапазон |
|---|---|---|
| `enable` | true | — |
| `max_rows` | 1000 | 1..10000 |
| `max_result_chars` | 50000 | 1000..200000 |
| `query_timeout_sec` | 30 sec | 1..300 |

Tool **не привязан к конкретной схеме**: SQL-запросы должны быть
fully-qualified (`schema.table`), `schema_name` в конфиге отсутствует.
Доступные таблицы определяются `TableRegistry` (см. `docs/table-registry.md`).

---

## 7. Контракт `vector_search`

```json
{
  "query": "финансовые нарушения",
  "index_name": "violations_index",
  "top_k": 5,
  "threshold": 0.5
}
```

Ответ:

```json
{
  "status": "success",
  "index_name": "violations_index",
  "query": "...",
  "results": [
    {"id": "123", "score": 0.82, "text": "...", "metadata": {"document_id": "..."}}
  ],
  "count": 1,
  "truncated": false
}
```

### Конфиг (`gateway.vector_search.*`)

| ключ | default | диапазон |
|---|---|---|
| `enable` | true | — |
| `default_top_k` | 5 | 1..100 |
| `max_top_k` | 50 | 1..100 |
| `default_threshold` | 0.0 | 0.0..1.0 |
| `max_query_chars` | 4000 | 100..16000 |
| `max_result_chars` | 16000 | 1000..200000 |
| `timeout_sec` | 30 | 1..120 |

---

## 8. Decision procedure в `SKILL.md` (audit_analyzer)

```text
Step 1: NL→SELECT → use nl_sql_generate tool (LLM with retry + EXPLAIN).
Step 2: exact SELECT is known → use duckdb_query.
Step 3: semantic similarity → use vector_search.
Step 4: find similar docs, then aggregate → vector_search first, then nl_sql_generate.
Step 5: do not use unknown tables or indexes.
Step 6: do not use DDL/DML.
```

Skill `audit_analyzer` полностью tool-only: у него больше нет `scripts/cli.py`
или иной back-compat обвязки. Все запросы идут через generic tools
`workspace/tools/` (см. `docs/skill-tool-inventory.md`). Раньше skill
содержал `scripts/cli.py` с режимами `--mode predefined`, `--mode generated_sql`,
`--mode vector` — эти режимы мигрированы в tool'ы (см. §8.1 для
`nl_sql_generate` — replacement режима `generated_sql`, и §7 для `vector_search`).
Режим `predefined` (готовые SQL-скрипты из реестра `public.agent_predefined_scripts`)
теперь вызывается через `duckdb_query` напрямую (агент читает `sql_template` из
реестра через `nl_sql_generate` или `duckdb_query`, либо формирует SELECT сам
по образцу из `references/sql_guidance.md`).

---

## 8.1. Контракт `nl_sql_generate`

Tool генерирует SELECT по NL-запросу, валидирует через EXPLAIN и выполняет
в общем DuckDB-кеше. Заменяет режим `generated_sql` навыка `audit_analyzer`
(и любой другой skill с NL→SELECT; skill после перевода на tool-only
больше не имеет CLI-обёртки для этого режима). Domain-free: whitelist
таблиц приходит из `TableRegistry`, не зашит в код.

```json
{
  "query": "сколько проверок в 2024 по месяцам",
  "max_rows": 100,
  "no_few_shot": false,
  "skip_hints": false,
  "hints_max_matches": 5
}
```

Ответ (success):

```json
{
  "status": "success",
  "sql": "SELECT ...",
  "columns": ["month", "count"],
  "rows": [[1, 12], [2, 8]],
  "row_count": 12,
  "returned_rows": 12,
  "truncated": false
}
```

Ответ (error):

```json
{
  "status": "error",
  "error_type": "generation_failed" | "sql_error" | "explain_failed" | "missing_infrastructure",
  "message": "...",
  "sql": "последний сгенерированный SQL"
}
```

### Конфиг (`gateway.nl_sql_generate.*` в `project.json`)

| ключ | default | диапазон | описание |
|---|---|---|---|
| `enable` | true | — | выключить tool |
| `max_retries` | 3 | 0..10 | retry-цикл LLM при ошибке |
| `schema_max_chars` | 12000 | 1000..100000 | обрезка описания схемы |
| `few_shot_top_n` | 2 | 0..10 | сколько примеров из реестра подмешивать |
| `max_result_chars` | 50000 | 1000..200000 | truncate_middle по JSON-ответу |
| `max_rows` | 1000 | 1..10000 | потолок возвращаемых строк; используется также как лимит для auto-predefined |
| `hints_max_matches` | 5 | 0..50 | сколько hints подмешать в system prompt |

### Архитектура pipeline

### Архитектура pipeline

```text
┌────────────────────────────────────────────────────────────────────┐
│ NlSqlGenerateTool.execute(query, ...)                              │
│                                                                     │
│  1. hints = ColumnDescriptionsResolver.lookup(query, max_matches=…)│
│  2. runner = NlSqlRunner(provider=CacheProvider,                   │
│                           schema_formatter=SchemaFormatter(),      │
│                           config=NlSqlRunnerConfig(...))           │
│  3. result = runner.run(query, hints_block=hints_block)            │
│  4. → {sql, columns, rows, row_count} (tool contract)              │
└────────────────────────────────────────────────────────────────────┘
                              ▲                                          │
                              │                                          ▼
                ┌─────────────────────────┐      ┌────────────────────────────────┐
                │ ColumnDescriptions       │      │ NlSqlRunner                    │
                │ Resolver                │      │ (lib/services/)                │
                │ (lib/services/)         │      │ + whitelist TableRegistry      │
                │ + tokenize + match      │      │ + schema SchemaFormatter       │
                │ + inline/data_file dict │      │ + few-shot registry             │
                └─────────────────────────┘      │ + LLM retry (max_retries+1)   │
                ▲                                  │ + validate_sql + EXPLAIN      │
                │                                  │ + provider.query_sql          │
                │                                  └────────────────────────────────┘
                │
   ┌────────────────────────────┐
   │ workspace/tools/            │
   │ column_descriptions.py      │
   │ (тонкий adapter: ctx →      │
   │ resolver → JSON-контракт)   │
   └────────────────────────────┘
```

`SchemaFormatter` и `ColumnDescriptionsResolver` — **internal services**
в `lib/services/`. `SchemaFormatter` формирует текстовое описание схемы
для system prompt и кешируется на уровне процесса (TTL). Resolver —
чистый механизм tokenize+match без доменных знаний (словарь
термин→колонка живёт во внешней конфигурации). Tool'ы обращаются к
ним через DI / in-process call, не через function calling — это
дешевле по токенам и не плодит лишний шаг в pipeline агента.

Общий pipeline (`NlSqlRunner`) переиспользуется tool'ом `nl_sql_generate`;
skill `audit_analyzer` после перевода на tool-only не имеет собственного
CLI-обёртки — все вызовы идут через `nl_sql_generate` напрямую.

---

## 8.2. Контракт `column_descriptions`

Tool возвращает структурированный словарь подсказок (термин → колонка)
для подмешивания в system prompt `nl_sql_generate`. Заменил бывший
`workspace/skills/audit_analyzer/scripts/column_hints.py`
(удалён вместе со всем `scripts/` skill'а после перехода на tool-only).

```json
{ "term": "объекты проверок", "max_matches": 5 }
```
или
```json
{ "match_all": true }
```

Ответ:

```json
{
  "status": "success",
  "term": "объекты проверок",
  "matches": [
    {"terms": ["audited objects", "объекты проверок"],
     "columns": ["oarb.audits.auditee_entity"]}
  ],
  "count": 1
}
```

### Конфиг (`tools.column_descriptions.*` в `config.json`)

| ключ | default | диапазон | описание |
|---|---|---|---|
| `enable` | true | — | выключить tool |
| `data_file` | — | — | путь к JSON-файлу со словарём (относительно cwd) |
| `max_result_chars` | 16000 | 1000..200000 | truncate_middle по JSON-ответу |
| `entries` | — | — | inline-словарь (fallback если `data_file` не задан) |

### Формат `data_file`

```json
{
  "synonym 1|synonym 2|синоним": [
    "schema.table.column"
  ]
}
```

Ключ может содержать `|` — список синонимов; совпадение с любым из
них считается положительным. Поиск case-insensitive, токены ≥ 3
символов. Словарь — **домен-данные конкретного skill'а**; resolver
(generic механизм) не знает, какие именно ключи там лежат.

### In-process API

Resolver живёт в `lib/services/column_descriptions.py` как
`ColumnDescriptionsResolver`. Это generic механизм — без знания о
конкретных таблицах/индексах. Используется двумя путями:

- `ColumnDescriptionsResolver.lookup(term, max_matches=5)` —
  синхронный in-process вызов из `NlSqlGenerateTool.execute()`
  (не через function calling). Возвращает список
  `{"terms": [...], "columns": [...]}` для подмешивания в hints_block.
- `ColumnDescriptionsTool` (`workspace/tools/column_descriptions.py`)
  — тонкий adapter над resolver: читает конфиг через
  `ctx._settings_ref.tools.column_descriptions.{entries,data_file}`
  и публикует matches через function calling для отладки или
  ручного использования агентом.

Source-словарь resolver получает из inline-`entries` (конфиг) или
`data_file` (JSON). Если оба не заданы — resolver возвращает
пустые matches.

---

## 9. Observability

Все tool-вызовы логируются через `lib/hooks/tool_audit_hook.py` —
это гарантирует наличие `tool_call_id`, `session_id`, `duration_ms`,
`status`, `error_type` (см. TARGET_ARCHITECTURE.md §26).

Дополнительное логирование внутри tool'а **не дублирует** audit-hook.

---

## 10. Проверка соответствия

| Проверка | Тест |
|---|---|
| Tool не импортирует Skill | `tests/test_skill_tool_independence.py::test_tools_do_not_import_skills` |
| Skill не импортирует Tool | `tests/test_skill_tool_independence.py::test_skills_do_not_import_tools` |
| Tool description без domain | `tests/test_architecture_tool_domain_free.py::test_tool_descriptions_have_no_audit_strings` |
| Tool код без domain names | `tests/test_architecture_tool_domain_free.py::test_tool_code_has_no_audit_strings` |

Любое падение этих тестов — архитектурная регрессия.

---

## Resource `label` — opaque marker для Skill-логики

`TableResource.label` — опциональная opaque-метка на dataclass-ресурсе таблицы,
позволяющая skill'у найти «свою» таблицу по семантической роли, не зная её
реального имени в PostgreSQL. Поле объявлено в `lib/services/table_registry.py`,
заполняется из `project.json::skills.<name>.tables[]` (объектная форма).

Runtime-sync (`PgDuckDbSyncService`, `DuckDbCacheStore`) **игнорирует** `label` —
это **не** routing marker и **не** влияет на cache/DuckDB. Значение label —
domain knowledge конкретного skill'а; `lib/` не содержит конкретных констант
label.

### Контракт

- `TableResource.label: str | None = None` — поле dataclass, **opaque для runtime**.
- Задаётся через `tables[]` в `project.json` в объектной форме: `{"name": "...", "label": "..."}`
  (см. `TableEntry` в `lib/core/project_settings.py`).
- Runtime-sync (`PgDuckDbSyncService`, `DuckDbCacheStore`) **игнорирует** label —
  это **не** routing marker.

### Lookup

```python
from lib.services.table_registry import table_registry

scripts_table = table_registry.resources_by_label("scripts_registry")[0]
```

Метод `TableRegistry.resources_by_label(label: str) -> tuple[TableResource, ...]`
проходит по всем регистрациям, фильтрует `enabled` (как `table_resources()`),
собирает ресурсы с совпадающим `label`, дедуплицирует по `name`. Неизвестный
label возвращает `()`. Disabled-ресурсы пропускаются.

### DoD

Skill может объявить свою метку и находить соответствующую таблицу без знания
её реального имени в PG. Это позволяет добавлять новые Skill-специфичные роли
(например, `label="users_lookup"`, `label="events_stream"`) без правок `lib/`.

### Пример: audit_analyzer + scripts_registry

В `project.json` (секция `skills.audit_analyzer`, имена таблиц — настраиваемые):

```json
"skills": {
  "audit_analyzer": {
    "tables": [
      {"name": "oarb.audits"},
      {"name": "oarb.violations"},
      {"name": "public.agent_predefined_scripts", "label": "scripts_registry"}
    ]
  }
}
```

`ApplicationContext._auto_register_skills` создаёт:

- `TableResource(name="oarb.audits")` (label=None)
- `TableResource(name="oarb.violations")` (label=None)
- `TableResource(name="public.agent_predefined_scripts", label="scripts_registry")`

После перевода skill'а `audit_analyzer` на tool-only `scripts/db_loader.py`
больше нет — реестр предопределённых скриптов читается напрямую
через `lib.core.skill_config.get_predefined_scripts_table("audit_analyzer")`
(используется утилитой `tools/generate_predefined_scripts_sql.py` и
`NlSqlRunner` для few-shot retrieval):

### Negative contract

- Tool **не должен** читать `label` (см. TARGET §5/§6 — Tool не знает domain).
- Runtime-sync **не должен** интерпретировать `label` как routing marker.
- `lib/` **не должен** содержать конкретных значений label (например,
  `"scripts_registry"` как константу в `lib/`). Это **domain knowledge skill'а**.

### Тесты

| Тест | Что проверяет |
|---|---|
| `tests/test_table_registry.py::TestLabelLookup` | unit-тесты метода `resources_by_label()` (default `None`, constructor, поиск, неизвестный label, disabled-пропуск, независимость от track-колонки) |
| `tests/test_auto_register_skills.py::TestAutoRegisterPredefinedScriptsTable` | интеграционные тесты через `_auto_register_skills` (label ставится для `predefined_scripts_table`) |
| `tests/test_skill_config_lookup.py::TestGetPredefinedScriptsTableRegistryPath` | end-to-end через `skill_config.get_predefined_scripts_table()` (lookup через registry) |

Любое использование `label` в `lib/services/runtime`-слое (`cache_provider_impl.py`,
`duckdb_cache_store.py`, `pg_duckdb_sync_service.py`) — архитектурная регрессия.