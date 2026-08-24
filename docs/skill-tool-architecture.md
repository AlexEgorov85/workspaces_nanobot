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
| `query_timeout` | 30 sec | 1..300 |

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
Step 1: numerical / aggregation / grouping → use duckdb_query.
Step 2: semantic similarity → use vector_search.
Step 3: find similar docs, then aggregate → vector_search first, then duckdb_query.
Step 4: predefined report → run via scripts.predefined_mode (CLI / internal).
Step 5: NL→SELECT → use references/sql_guidance.md, then duckdb_query.
Step 6: do not use unknown tables or indexes.
Step 7: do not use DDL/DML.
```

Skill не обязан превращать свои scripts в Tools (см. TARGET_ARCHITECTURE.md §9).
Скрипты skill'а — штатная часть package для deterministic workflows и CLI.

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