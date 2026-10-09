# Skill / Tool architecture

**Нормативный документ-контракт** между `Skill` и `Tool`.
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
- ✅ «use `scripts/cli.py --mode vector` with `--index-name violations_index`»
- ❌ «call a `vector_search` tool class with `query=...`»
- ❌ «import a Python tool class»

---

## 6. Read-only SQL — Agent-facing tool `duckdb_query`

Agent-facing tool `duckdb_query` **существует**:
`workspace/tools/duckdb_query_tool.py`, регистрируется через
`lib/services/project_tool_loader.py` и получает живой `ctx.cache_provider`
через `set_provider`. Работает **в процессе gateway** — отдельный процесс
к `cache.duckdb` не открывается (DuckDB отдаёт `File is already open in
<PID>`, даже в `read_only`), поэтому in-process доступ — единственный
рабочий путь к capability.

Альтернативный путь — skill-side CLI:

```text
python scripts/cli.py --mode predefined --script <name>
```

Read-only политика сохранена как infra-контракт Core:

Разрешено: `SELECT`, `WITH ... SELECT`.
Запрещено: `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `CREATE`, `TRUNCATE`, `COPY`, `ATTACH`, `DETACH`, `INSTALL`, `LOAD`, `EXPORT`, `IMPORT`, `CALL`, multi-statement.

Реализация: `lib/utils/sql_safety.py::validate_sql` (последняя граница перед execution).

Исторические лимиты (`gateway.duckdb_query.*`) удалены вместе с tool'ом.

---

## 7. Semantic search — Agent-facing tool `vector_search`

Agent-facing tool `vector_search` **существует**:
`workspace/tools/vector_search_tool.py`, `set_provider` даёт ему живой
`CacheProvider.search_vector` (FAISS в памяти gateway).

Альтернативный путь — CLI skill'а:

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
        → вызов CLI skill'а `scripts/cli.py --mode predefined --script <name>`
        → выполнение SQL через generic `CacheProvider.query_sql`.
Step 2: запрос не соответствует ни одному predefined → сообщить пользователю
        (прямой доступ к свободному SQL и vector search у агента есть —
        tool'ы `duckdb_query` / `vector_search`).
Step 3: (operator/benchmark) для NL→SQL — CLI `--mode generated_sql`;
        для семантического поиска — CLI `--mode vector` с `--index-name`.
Step 4: при ошибке → прочитать message, переформулировать/уточнить запрос,
        повторить (retry — задача Agent, не tool'а).
Step 5: do not use unknown tables or indexes.
Step 6: do not use DDL/DML.
Step 7: do not use vector/search для COUNT/GROUP BY.
```

Skill `audit_analyzer` — **CLI-only**: автономный skill-side CLI
`scripts/cli.py --mode <predefined | generated_sql | vector>` (единый entry-point,
вызывается агентом через `tools.exec`; также используется бенчмарками/CI).
Generic tools `duckdb_query` (точный SELECT) и `vector_search` (семантика)
**существуют** и доступны агенту напрямую, но доменную логику аудита они
не подменяют: решение procedure остаётся за `SKILL.md` skill'а.

Tool'ы `run_predefined_script` и `nl_sql_generate` отсутствуют: их логика
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

---

## 12. Skill `audit_formulation_strengthener`

Skill для аудиторов: принимает **текст отклонения** + **файлы ВНД** (`.pdf`/`.docx`/`.txt`),
возвращает **человекочитаемый отчёт** в строгом русском юридическом стиле.

**Акт НЕ передаётся** — только отклонение и ВНД.

### 12.1 Особенности (отличающие от других skills)

| Особенность | Значение |
|---|---|
| `tables` | `[]` (пустой — ВНД приходят файлами, не из БД) |
| `vector_indexes` | `[]` (пустой — без embeddings, map по чанкам через LLM) |
| Pipeline | `analyze → search (map → фильтр/top-K) → synthesize` |
| LLM-вызовы | `1 + N + 1` (N = число чанков ВНД) |
| Формат отчёта | `.md` / `.txt` / `.docx` (по умолчанию `.md`), **не JSON** |

### 12.2 Архитектурный контракт

Skill **разрешено**:

- ✅ Использовать `lib.services.llm_client.call_llm` (через тонкую обёртку `scripts/llm.py`).
- ✅ Использовать `lib.services.text_splitter.split_text` для чанкования ВНД.
- ✅ Использовать `lib.core.skill_config.{get_llm_config, get_cli_config, get_max_retries, get_chunking_config, get_tool_config}`.
- ✅ Импортировать `workspace.utils.office_files.extract_text` для извлечения текста.
- ✅ Импортировать `lib.utils.text_utils.sanitize_value` для JSON-safe нормализации.

Skill **запрещено** (дополнительно к общим правилам):

- ❌ Импортировать что-либо из `workspace.skills.*` (включая `legal_summarizer`, `audit_analyzer`) — skill полностью автономен.
- ❌ Хранить ВНД в PostgreSQL или DuckDB (ВНД приходят каждый раз заново).
- ❌ Создавать vector-индексы для ВНД.
- ❌ Использовать `audit_analyzer`-специфичные ресурсы (`audits_index`, `violations_index`).
- ❌ Возвращать JSON как пользовательский артефакт (только `.md`/`.txt`/`.docx`).
- ❌ Добавлять skill-specific ключи в `project.json::skills.<name>` — `SkillSettings(extra="forbid")`.
- ❌ Модифицировать `sys.path` где-либо кроме `scripts/cli.py` (единственная разрешённая точка bootstrap).
- ❌ Класть артефакт мимо дерева сессий. Путь резолвит `scripts/paths.py`
  (единственный вызов из `cli.main()`): короткое имя файла `--output report.md`
  попадает в `workspace/data_store/cache/sessions/<session_key>/`, путь с
  каталогом используется как есть, но вне дерева сессий CLI предупреждает в
  stderr. Причина — CLI-скилл это подпроцесс, а `SessionFileRedirectHook`
  перехватывает только `write`/`edit`.

### 12.3 Pipeline

```text
CLI: --violation "..." --vnd vnd1.pdf --vnd vnd2.docx --output report.md

analyze (1 LLM):
  violation → {normalized, key_concepts, severity, suggested_vnd_sections}

search (N LLM, map по чанкам → фильтр → top-K):
  для каждого чанка ВНД:
    chunk + violation → LLM → {relation_type, relevance_score, why_matches}
  filter: score < 0.3 → отбрасываем
  re-rank: top-10 по score, evidence_id F1..FN

synthesize (1 LLM):
  analyze_data + search_findings → LLM →
    {title, violation_summary, established_facts, deviation_analysis,
     vnd_citations[{evidence_id, excerpt, relation_explanation}],
     verdict, recommended_formulation}
  validate citations:
    - evidence_id в реестре
    - excerpt — подстрока text_excerpt находки (.strip() обеих, без whitespace-collapse)
    - relation_type — из находки (не из LLM)
  dropped → citations_dropped + строка в отчёте

render: Report JSON → markdown → опционально .docx
```

### 12.4 Регистрация

В `project.json`:

```json
{
  "skills": {
    "audit_formulation_strengthener": {
      "enabled": true,
      "tables": [],
      "vector_indexes": [],
      "cli":   { "default_mode": "all", "timeout_sec": 120, "max_retries": 3 },
      "llm":   { "max_tokens": 4096, "temperature": 0.1 },
      "chunking": { "chunk_size": 6000, "chunk_overlap": 400 },
      "execution": { "max_chunks_for_execution": 50 }
    }
  }
}
```

Skill-specific константы **в Python-коде** (не в JSON):

- `scripts/modes/search.py::TOP_K_CANDIDATES = 10`
- `scripts/modes/search.py::MIN_RELEVANCE_SCORE = 0.3`

### 12.5 Тесты

- Unit + contract + CLI тесты в `tests/`. Все LLM-фазы замоканы на границе
  `scripts.llm.chat_json/chat`.
- Архитектурные инварианты в `tests/test_architecture.py` (AST + plain string).
- `python -m pytest` из `workspace/skills/audit_formulation_strengthener/` — зелёный.

См. `workspace/skills/audit_formulation_strengthener/references/testing.md`.

### 12.6 Что НЕ делает

- ❌ Не хранит ВНД в БД (stateless).
- ❌ Не использует embeddings (map по чанкам через LLM).
- ❌ Не принимает акт — только отклонение.
- ❌ Не возвращает JSON как пользовательский артефакт.
- ❌ Не имеет таблиц и vector-индексов.
- ❌ Не импортирует другие скиллы.
- ❌ Не использует confirmation-меню / `--confirm` (флаг удалён).
