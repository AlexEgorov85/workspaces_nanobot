# Skill / Tool architecture

**Нормативный документ-контракт** между `Skill` и `Tool`.
Цель — зафиксировать архитектурные правила и служить reference при code review.

---

## 1. Главный принцип

`Skill` и `Tool` — **независимые механизмы**.

Skill и Tool — **параллельные потребители** общей runtime-инфраструктуры:

```text
              AgentRuntime
               /         \
              v           v
           Skill         Tool
              \           /
               \         /
                v       v
        shared runtime infrastructure
        (lib/services, lib/core, lib/utils)
```

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

**Shared infrastructure — не Tool-слой.** Наличие callable-функции в `lib/`
не делает её Tool'ом: Tool'ом становится только capability, которую агент
выбирает и вызывает самостоятельно (см. §6 и §7).

---

## 2. Что разрешено

```python
# Skill
from lib.services.cache_provider import CacheProvider   # интерфейс
from lib.utils.sql_safety import validate_sql
from lib.core import skill_config as _lib                # runtime API

# Tool
from lib.services.cache_provider import CacheProvider   # тот же интерфейс
from lib.utils.sql_safety import validate_sql
```

Skill и Tool могут использовать **общую инфраструктуру** (`lib/utils`, `lib/services`, `lib/core`).

Skill обращается к инфраструктуре **напрямую** — через существующий
runtime/application interface, а не через Tool. Наличие callable-функции в
`lib/` не превращает её ни в Tool, ни в обязанность Skill'а искать Tool.

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
from workspace.tools.history_search_tool import ...
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

Tool — это generic capability. Какой skill их использует — не его дело.

---

## 5. Что Skill не должен знать

- Конкретный Python-класс Tool.
- Конкретную реализацию Tool в коде.
- Внутренний contract Tool за пределами публичного (name, description, parameters).

Skill пишет инструкции **в терминах capability**, а не в терминах Python:
- ✅ «выполни семантический поиск по индексу `violations_index`»
  (в текущей реализации — `scripts/cli.py --mode vector --index-name ...`)
- ❌ «call a `vector_search` tool class with `query=...`»
- ❌ «import a Python tool class»

Конкретный интерфейс доставки capability (skill-side CLI, script, Tool) —
деталь реализации. Норма фиксирует **форму** инструкции (в терминах
capability), а не конкретный способ её выполнения.

---

## 6. Read-only SQL — не Agent-facing tool

Agent-facing tool `duckdb_query` **не существует**. Read-only SQL не является
Agent-facing capability: агент не выбирает свободный SQL самостоятельно —
это внутренняя операция доменного Skill'а. Данные доступны через
предопределённые скрипты Skill'а (в текущей реализации —
`scripts/cli.py --mode predefined --script <name>`).

Это и есть канонический пример «generic, но **не** agent-facing»: свободный
SELECT по произвольным таблицам потребовал бы от агента знать схему и домен,
а это работа Skill'а, а не самостоятельное действие Tool'а.

Read-only политика сохранена как infra-контракт Core:

Разрешено: `SELECT`, `WITH ... SELECT`.
Запрещено: `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `CREATE`, `TRUNCATE`, `COPY`, `ATTACH`, `DETACH`, `INSTALL`, `LOAD`, `EXPORT`, `IMPORT`, `CALL`, multi-statement.

Реализация: `lib/utils/sql_safety.py::validate_sql` (последняя граница перед execution).

Исторические лимиты (`gateway.duckdb_query.*`) удалены вместе с tool'ом.

---

## 7. Semantic search — не Agent-facing tool

Agent-facing tool `vector_search` **не существует**. Semantic search не является
Agent-facing capability: агент не выбирает семантический поиск самостоятельно —
это внутренняя операция Skill'а, оперирующая доменными индексами.

Текущий операционный интерфейс Skill'а:

```text
python scripts/cli.py --mode vector --query '<текст>' --index-name <name>
```

Ответ CLI (JSON):

```json
{
  "status": "success",
  "data": {
    "results": [
      {"id": "123", "score": 0.82, "text": "...", "source": "...", ...}
    ],
    "count": 1
  }
}
```

Исторические ключи конфига (`gateway.vector_search.*`) удалены вместе с tool'ом.

## 8. Decision procedure в `SKILL.md` (audit_analyzer)

```text
Step 1: запрос соответствует predefined из `SKILL.md` (каталог скриптов)
        → выполнение SQL через `CacheProvider.query_sql`.
Step 2: запрос не соответствует ни одному predefined → сообщить пользователю
        (прямой доступ к свободному SQL и vector search у агента нет).
Step 3: (operator/benchmark) NL→SQL и семантический поиск — внутренние
        операции Skill'а, не Agent-facing capability.
Step 4: при ошибке → прочитать message, переформулировать/уточнить запрос,
        повторить (retry — задача Agent, не tool'а).
Step 5: do not use unknown tables or indexes.
Step 6: do not use DDL/DML.
Step 7: do not use vector/search для COUNT/GROUP BY.
```

### Что здесь нормативно, а что — as-is

**Нормативно:**

- `audit_analyzer` — **Skill**: доменная логика, оркестрация и capability
  доступа к доменным данным живут в Skill.
- Все три capability (predefined SQL, NL→SQL, vector search) — **внутренние
  операции Skill'а**, а не Agent-facing Tools. Агент не выбирает их
  самостоятельно.
- Generic tools `duckdb_query` (точный SELECT) и `vector_search` (семантика)
  **не создаются**.

**As-is (текущая реализация, не норма):**

- Skill `audit_analyzer` сейчас предоставляет эти capability через
  skill-side CLI `scripts/cli.py --mode <predefined | generated_sql | vector>`,
  который агент запускает через `tools.exec`; тот же интерфейс используют
  бенчмарки и CI.
- Tool'ы `run_predefined_script` и `nl_sql_generate` отсутствуют: их логика
  живёт в Skill (`predefined.run`, `generated_sql_mode.run`), а обращение к
  модели skill-side helper делает через клиент платформы
  (`libs/enterprise_client/llm.py` → операция `complete`). Собственного
  вызова провайдера у навыка нет, и настроек модели он не знает.

CLI — **операционный** интерфейс доставки capability, а не архитектурное
требование Skill'а. Норма не предписывает его наличие; она запрещает
`Skill → Tool` и создание Agent-facing Tools под внутренние операции Skill'а.

Подробности текущего состояния — в `docs/skill-tool-inventory.md` и
`workspace/skills/audit_analyzer/SKILL.md`.

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

Загрузка кэша (`CacheLoadService`, `DuckDbCacheStore`) **игнорирует** `label` —
это **не** routing marker и **не** влияет на кэш. Значение label —
domain knowledge конкретного skill'а; `lib/` не содержит конкретных констант
label.

### Контракт

- `TableResource.label: str | None = None` — поле dataclass, **opaque для runtime**.
- Задаётся через `tables[]` в `project.json` в объектной форме: `{"name": "...", "label": "..."}`
  (см. `TableEntry` в `lib/core/project_settings.py`).
- Загрузка кэша (`CacheLoadService`, `DuckDbCacheStore`) **игнорирует** label —
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
(predefined-скрипты — DB-first, отдельная SQL-генерация удалена) и
используется skill'ом `scripts/generated_sql_mode.py` для few-shot retrieval:

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
`duckdb_cache_store.py`, `cache_load_service.py`) — архитектурная регрессия.

---

## 11. Контракт `legal_summarizer_query`

Tool `legal_summarizer_query` (`workspace/tools/legal_summarizer_query.py`)
— единственный живой generic-tool, подключённый к конкретному skill'у
через **subprocess-boundary** (не импорт). Связь Skill↔Tool всё равно
**через агентский runtime** (§1), но контракт IPC между wrapper и
`cli_query.py` — отдельная нормативная поверхность.

### 11.1. IPC-протокол между wrapper и `cli_query.py`

| exit code | stdout `status` | семантика |
| --- | --- | --- |
| 0 | `"ok"` | success |
| ≠ 0 | `"error"` (JSON-объект) | domain error (pass-through) |
| ≠ 0 | что-то иное | process failure → `cli_failed` |
| 0 | пустой stdout | → `empty_response` |
| 0 | stdout не JSON | → `invalid_json` |

Wrapper НЕ ставит свой envelope поверх domain error: `error_type`,
`operation_id`, `path`, `version_observed`, `message` доходят до агента
as is. Это включает три manifest-причины:

* `manifest_not_found`
* `manifest_corrupted`
* `manifest_unsupported_version`

Диагностика `manifest` живёт в `workspace/skills/legal_summarizer/scripts/cache/manifest.py::diagnose_manifest`
(operation-level API в том же модуле, где `load_manifest`).

### 11.2. Что Tool НЕ делает

- Tool **не импортирует** skill (это уже общее правило §3).
- Tool **не интерпретирует** `status` поля CLI — он только пробрасывает
  dict с `status == "error"` как есть (строгая проверка, не "есть status").
- Tool **не пытается** выровнять `chunks_total` (manifest) и `chunk_count`
  (per-chunk файлы) — расхождение документировано, а не правится кодом.

### 11.3. Где контракт зафиксирован

| Документ | Что внутри |
| --- | --- |
| `openspec/specs/skills/legal-summarizer-query/spec.md` | нормативная спека (ADDED Requirements, scenarios) |
| `workspace/skills/legal_summarizer/SKILL.md` § «IPC contract for follow-up queries» | таблица exit code × status × error_type; семантика `chunks_total` vs `chunks` |
| `workspace/tools/legal_summarizer_query.py` docstring | исчерпывающий список wrapper-уровневых и CLI-pass-through error_type |

### 11.4. Тесты

| Тест | Что проверяет |
| --- | --- |
| `tests/test_legal_summarizer_query_ipc.py` | IPC-сценарии между wrapper и CLI через моки `subprocess.run`: success / domain error / process failure / pass-through полей |
| `tests/test_legal_summarizer_query_manifest_integration.py` | **полный путь** manifest на диске → `cli_query.py` subprocess → wrapper pass-through; три manifest-причины через **реальные** файлы во временной директории |
| `workspace/skills/legal_summarizer/tests/test_manifest_diagnose.py` | диагностика манифеста: missing / corrupted / version=1 / без version / `version="abc"` |
| `workspace/skills/legal_summarizer/tests/architecture/test_document_cache_boundaries.py::test_operation_level_manifest_whitelist_enforced` | `diagnose_manifest` живёт в whitelist operation-level API |

Любая попытка:

* ввести ещё один `error_type`-префикс (`cli_manifest_*`) — нарушает
  нормативную спеку и ломает grep'абельность кода;
* подменить `status == "error"` на «наличие status» — ломает pass-through
  контракт и пропускает случайный `exit 1 + {"status":"ok"}`;
* убрать `diagnose_manifest` из whitelist operation-level API —
  регрессия архитектурного guard (§10).
