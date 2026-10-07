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
# Skill — процесс навыка, своего MCP-клиента у него нет.
# К данным ходит через клиент платформы; адрес платформы он не знает.
from libs.enterprise_client.llm import LlmClient            # операция llm.complete
# к данным — операции capability audit / data платформы

# Агент (gateway / CLI) — композиционный корень
from lib.core import skill_config as _lib                    # runtime API для skill'ов
```

Skill и Tool могут использовать **общую инфраструктуру** (`lib/utils`,
`lib/services`, `lib/core`) — то, что ещё не уехало в платформу.

Skill обращается к данным **напрямую**, но не к слоям хранения агента: в
агента их больше нет. Открытие файла снимка, чтение и генерация SQL
принадлежат capability `data`/`audit` платформы; навык вызывает операции.
Наличие callable-функции в `lib/` не превращает её ни в Tool, ни в
обязанность Skill'а искать Tool.

> **История этого раздела.** До 2026-10-01 пример был таким:
> `from lib.services.cache_provider import CacheProvider` плюс
> `from lib.utils.sql_safety import validate_sql`. Оба модуля удалены —
> интерфейс `CacheProvider` переехал в capability `data` платформы, а
> SQL-guard переехал в `mcp-platform/libs/audit/guard.py` и
> `libs/enterprise_data/snapshot/sql_guard.py`. Приводить пример в порядок
> было поздно: он учил импортировать несуществующие модули.
> См. `docs/architecture/decisions/audit-analyzer-runtime-boundary.md`.

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
        → операция capability `audit` платформы (выполнение SQL над снимком).
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
  (`libs/enterprise_client/llm.py` → операция `llm.complete`). Собственного
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

`label` — опциональная opaque-метка на объявлении таблицы, позволяющая capability
найти «свою» таблицу по семантической роли, не зная её реального имени в
PostgreSQL.

> **Владелец поменялся.** Метка объявляется в
> `mcp-platform/platform.json → audit.tables` (объектная форма
> `{"name": ..., "label": ...}`) и разбирается при построении конфигурации
> capability в `mcp-platform/servers/enterprise/server.py::_audit_config`.
> Прежний владелец — `TableResource.label` в `lib/services/table_registry.py` (удалён `8d63240`)
 и
> lookup `TableRegistry.resources_by_label()` — снят 2026-10-01 вместе с
> локальным кэшем.

### Контракт

- `label` — **opaque для runtime**: строка, которую платформа не интерпретирует,
  кроме сравнения с известным значением.
- Задаётся в `platform.json → audit.tables` в объектной форме.
- Разбор: `_audit_config()` идёт по списку записей; запись с меткой
  `scripts_registry` возвращается отдельно и **в доменные таблицы не попадает** —
  аудит не должен читать собственные скрипты в обход проверки строк.
- Это **не** routing marker: ни загрузка снимка, ни FAISS-индексация метку не
  читают.

### Lookup

Lookup-а как метода больше нет — разделение выполняется один раз при сборке
конфигурации (`server.py::_audit_config`):

```python
registry, tables = "", []
for name, label in settings.get("ENTERPRISE_AUDIT_TABLES"):
    if label == SCRIPTS_REGISTRY_LABEL:
        registry = name
    else:
        tables.append(name)
```

Каталог скриптов уезжает в capability отдельным ключом (`scripts_registry.table`),
доменные таблицы — в `audit.tables`. Реестр, пришедший из окружения голой
строкой, остаётся пустым, и capability отвечает `registry_unavailable`: строка
не умеет сказать «это реестр», а выдать его за доменную таблицу хуже, чем не
выдать.

### DoD

Capability может объявить свою метку и находить соответствующую таблицу без
знания её реального имени в PG. Это позволяет добавлять новые доменные роли
(например, `label="users_lookup"`) без правок кода платформы.

### Пример: audit_analyzer + scripts_registry

В `mcp-platform/platform.json` (секция `audit`, имена таблиц настраиваемые):

```json
"audit": {
  "tables": [
    {"name": "oarb.audits"},
    {"name": "oarb.violations"},
    {"name": "public.agent_predefined_scripts", "label": "scripts_registry"}
  ],
  "row_ceiling": 500
}
```

`_audit_config()` из этого объявления собирает конфигурацию capability:

- `audit.tables` → `["oarb.audits", "oarb.violations"]` (без метки);
- `scripts_registry.table` → `"public.agent_predefined_scripts"`.

### Negative contract

- Tool агента **не должен** читать `label`: он работает с операциями capability и
  не знает домена.
- Синхронизация снимка и сборка индексов **не должны** интерпретировать `label`
  как routing marker.
- Общий код платформы **не должен** содержать доменных значений метки: это
  domain knowledge объявления.

### Тесты

Контракт проверяется на стороне capability `audit`: разбор объявления и отказ
`registry_unavailable` при реестре без метки — в
`mcp-platform/tests/test_audit_capability.py`, поведение реестра — в
`test_audit_lib_registry.py` (в том числе `registry_corrupt` на битой строке).

Прежние тесты `tests/test_table_registry.py::TestLabelLookup`,
`test_auto_register_skills.py` и `test_skill_config_api.py` удалены вместе с
реестром.

Любое использование `label` в слое, который работает с данными
(`mcp-platform/libs/vectors/`, `libs/enterprise_data/`) — архитектурная
регрессия: навык и capability решают, что проверять, по объявлению
ресурса, а не разбирают реестр по имени.

---

## 11. Контракт доступа к разобранному документу

Tool `legal_summarizer_query` и IPC-протокол к `cli_query.py` **удалены**;
§11.1–11.4 описывали вещь, которой в репозитории нет уже в трёх местах сразу:
сам tool снят (change `2026-10-03-mcp-native-tools`, п. D6), навык
`workspace/skills/legal_summarizer/` (нет в дереве репозитория)
 уехал на платформу раньше, а IPC-граница
(subprocess) исчезла вместе с ним. Таблицы exit code × status и перечень
manifest-причин описывали протокол, которого больше нет.

Что пришло на замену — не Tool, а операция платформы:

| Было (Tool) | Стало (операция) |
|---|---|
| `legal_summarizer_query(operation_id, field, max_chunk_summary_chars)` | `mcp_enterprise_platform_query_operation` (операция `platform.query_operation`) с той же семантикой полей (`stats` / `articles` / `chunks` / `sections` / `tree` / `all`) |
| wrapper переводил ошибки CLI в свой JSON | сервер отдаёт закрытый конверт `_execution` / `{"error": {"code", ...}}`; модель читает его напрямую |
| «область видимости» задавалась аргументом инструмента | область задаётся личностью вызова, модель её не выбирает |

Общий контракт вызовов — `workspace/skills/enterprise_mcp/SKILL.md`.

**Спека ещё не переведена — это долг.** `openspec/specs/skills/legal-summarizer-query/spec.md`
по-прежнему описывает снятый tool и IPC к `cli_query.py` как нормативный контракт,
то есть как действующее требование. Пока она не обновлена, ссылаться на неё как на
источник истины нельзя: действующий контракт — SKILL выше.

**Что из прежнего §11 осталось в силе как правило, а не как описание.**
Tool по-прежнему не должен интерпретировать доменную ошибку и не должен
подменять её своей. Просто «не подменять» теперь означает «не переписывать
конверт платформы» — обёртки, которая могла бы это сделать, больше нет.

---
