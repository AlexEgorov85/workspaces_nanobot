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
Step 1: запрос соответствует predefined из references/predefined_scripts.md
        → CLI `--mode predefined --script <name>` (реестр в `predefined/scripts.py`,
        выполнение через `predefined.run` → generic DuckDB).
Step 2: запрос про смысл/похожие → vector_search с index_name из
        references/vector_indexes.md.
Step 3: свободный SQL → Agent читает references/schema.md +
        references/sql_guidance.md, формирует SELECT сам,
        duckdb_query(sql=<...>, params=<...>).
Step 4: при ошибке SQL → Agent читает message, исправляет,
        повторяет duckdb_query (retry — задача Agent, не tool'а).
Step 5: do not use unknown tables or indexes.
Step 6: do not use DDL/DML.
Step 7: do not use vector_search для COUNT/GROUP BY.
Step 8: do not use LIKE для семантического поиска.
```

Skill `audit_analyzer` — **CLI + generic tools**: автономный skill-side CLI
`scripts/cli.py --mode <predefined | generated_sql | vector>` (единый entry-point,
вызывается агентом через `tools.exec` с абсолютным путём; также используется
бенчмарками/CI). Дополнительно агент может выполнять те же операции напрямую
через generic tools `workspace/tools/duckdb_query_tool.py` (точный SELECT)
и `workspace/tools/vector_search_tool.py` (семантика).
Подробности — в `docs/skill-tool-inventory.md` и `workspace/skills/audit_analyzer/SKILL.md`.

Раньше (рефакторинг `refactor/skills-tools-cleanup`) CLI был удалён в пользу
tool-only, но позже восстановлен (коммиты `f4b646e`, `94fadf2`, `9e646ef`):
режимы `--mode predefined`, `--mode generated_sql`, `--mode vector` — активны.
Tool'ы `run_predefined_script` и `nl_sql_generate` при этом удалены: их логика
живёт в CLI skill'а (`predefined.run`, `generated_sql_mode.run`) и skill-side
helper `scripts/skill_config.py` / `scripts/llm.py` (прямой вызов
`lib.services.llm_client.call_llm` для LLM-генерации SQL).

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

При восстановлении CLI (commit `f4b646e`) skill-обвязка вернулась:
`scripts/db_loader.py` при этом не восстанавливался — реестр
предопределённых скриптов читается через
`lib.core.skill_config.get_predefined_scripts_table("audit_analyzer")`
(используется утилитой `tools/generate_predefined_scripts_sql.py` и
CLI skill'а `scripts/generated_sql_mode.py` для few-shot retrieval):

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
| `tests/test_skill_config_api.py::TestPredefinedScripts::test_lookup_from_table_registry` | end-to-end через `skill_config.get_predefined_scripts_table()` (lookup через registry) |

Любое использование `label` в `lib/services/runtime`-слое (`cache_provider_impl.py`,
`duckdb_cache_store.py`, `pg_duckdb_sync_service.py`) — архитектурная регрессия.
