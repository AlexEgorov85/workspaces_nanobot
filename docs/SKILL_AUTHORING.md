# Создание своего skill'а — пошаговый гайд

> Практическое руководство для разработчика. Нормативные правила —
> `docs/TARGET_ARCHITECTURE.md` (§3, §21, §22, §26, §30, §31), контракт
> Skill ↔ Tool — `docs/skill-tool-architecture.md`, модель ресурсов —
> `docs/table-registry.md`. Этот документ — мост между «как должно быть»
> и практическим «что нажимать».

---

## 0. TL;DR

**Skill** — доменный пакет для агента:

- инструкции (когда применять, какой capability выбрать);
- Python-скрипты (детерминированные/CLI процедуры, map-reduce);
- опциональные данные/промпты/references (progressive disclosure);
- декларация PG-таблиц/vector-индексов в `project.json`.

Skill **не вызывает** Tool программно (`TARGET_ARCHITECTURE.md:209-228`), Tool **не знает** о Skill (§22.1). Связь — через agent runtime: skill описывает capability терминами, агент решает какой tool вызвать.

Универсальная структура:

```
workspace/skills/<skill_name>/
    SKILL.md
    scripts/
    references/         # опционально
    prompts/            # опционально
```

Все ключевые инварианты проверяются автоматически — см. §10 «Архитектурные тесты».

---

## 1. Когда создавать Skill, а когда Tool

Перед написанием пройдите decision-чеклист `docs/TARGET_ARCHITECTURE.md:973-1037` (§30). Короткая версия:

| Сценарий | Создаём |
|---|---|
| Доменная логика «как решать задачу X в нашей БД» | **Skill** |
| Generic возможность «выполнить SELECT» / «найти семантически» | **Tool** (`workspace/tools/`) |
| LLM-фолбэк на естественном языке для конкретного домена | **Skill** (`generated_sql`/`map_reduce` режим) |
| Тонкая обёртка вокруг generic utility для домена | **Skill** (как `office_files` поверх `workspace/utils/`) |
| Универсальный SQL validator / chunker / splitter | **`lib/utils`** |

Если вы сомневаетесь — посмотрите на существующие skill'ы (`audit_analyzer`, `legal_summarizer`, `office_files`) как референс.

---

## 2. Структура каталога

### 2.1 Минимум (один режим, без таблиц)

```text
workspace/skills/<skill_name>/
├── SKILL.md
├── __init__.py
└── scripts/
    ├── __init__.py
    ├── cli.py
    └── skill_config.py        # обёртка над lib.core.skill_config
```

### 2.2 Полная (несколько режимов + БД + LLM)

```text
workspace/skills/<skill_name>/
├── SKILL.md
├── __init__.py
├── scripts/
│   ├── __init__.py
│   ├── cli.py                 # точка входа CLI
│   ├── skill_config.py        # тонкая обёртка над lib.core.skill_config
│   ├── llm.py                 # LLM-клиент (если нужен)
│   ├── db_loader.py           # работа с DuckDB-кэшем
│   ├── predefined.py          # режим 1 (готовые отчёты)
│   ├── predefined_mode.py
│   ├── generated_sql_mode.py  # режим 2 (NL → SELECT)
│   └── scripts_registry.py    # работа с label="scripts_registry"
├── references/
│   ├── schema.md
│   ├── vector_indexes.md
│   └── sql_guidance.md        # правила формулировки SELECT
├── prompts/
│   ├── summarize_system.md
│   └── reduce_system.md
└── cache/
    └── schema.json            # дамп схемы для reference
```

### 2.3 Чего НЕ должно быть

- **Никаких `register.py`** — мёртвый паттерн, проверяется
  `tests/test_skill_config_lookup.py::TestNoRegisterPy`. Регистрация —
  декларация в `project.json::skills.<name>` + `_auto_register_skills`
  в `lib/core/application_context.py:681-693`.
- **Не дублировать `skill_config.py`** с бизнес-логикой. Только тонкая
  обёртка (`lib/core/skill_registration.py:9-16`); никакого `register_*`.
- **Не импортировать `workspace.tools.*`** в skill (§3.3).
- **Не класть абсолютные пути** в `--file` аргументах CLI — см. `workspace/AGENTS.md:40-58`.
- **Не делать `pip install`** в коде — `requirements.txt` уже полный
  (`workspace/AGENTS.md:7-34`).

---

## 3. SKILL.md — что и как писать

### 3.1 Frontmatter (обязательно)

```yaml
---
name: <skill_name>            # совпадает с ключом в project.json::skills
description: <одна строка>    # как skill выбирается агентом
metadata: {"nanobot":{"emoji":"📊","always":true}}
```

`description` — это всё, что видит LLM-маршрутизатор при выборе skill'а.
Сделайте его конкретным: «SQL-отчёты по oarb.*, семантический поиск по FAISS,
LLM-генерация SELECT», а не «работа с аудитами».

`metadata.nanobot.always: true` — skill всегда виден агенту. Используйте
`false` если skill нужен только по явному запросу.

### 3.2 Структура основной части

Все три существующих skill'а следуют одной структуре. Используйте как шаблон:

1. **Заголовок H1** с именем skill.
2. **Одно-двухстрочное описание** назначения.
3. **Decision procedure / Когда использовать** — самая важная секция.
4. **Режимы работы** (если несколько) — детали с примерами CLI.
5. **Доменные таблицы и индексы** — что доступно.
6. **Что не делать** — запреты.
7. **References / Что внутри** — ссылки на детальные документы.

### 3.3 Decision procedure — обязательная секция

Если у skill'а >1 режима, нужна decision procedure
(`audit_analyzer/SKILL.md:13-19`):

```markdown
| Задача | Режим | Инструмент |
|---|---|---|
| Аггрегация / фильтр по полям | Predefined / Generated SQL | `scripts/cli.py --mode predefined`, `duckdb_query` |
| Свободный вопрос про данные (SELECT) | Generated SQL | `duckdb_query` tool |
| Семантический поиск по смыслу | Vector | `vector_search` tool |
| Известный отчёт из реестра | Predefined | `scripts/cli.py --mode predefined` |
```

Для однорежимных skill'ов (`legal_summarizer`, `office_files`) — секции
«Когда использовать» + «Когда не вызывать».

### 3.4 Имена таблиц/индексов

**Не зашивайте как константы.** Имена — настраиваемые в `project.json`.
См. `audit_analyzer/SKILL.md:81-89`:

> Имена таблиц и индексов ниже — значения текущей инсталляции,
> настраиваемые в `project.json` (`skills.audit_analyzer.tables[*].name`,
> `skills.audit_analyzer.vector_indexes[*].name`). В других развёртываниях
> они могут отличаться; не зашивайте их в код/промпты как константы.

### 3.5 Секция «Что не делать»

Всегда явно фиксируйте запреты. Примеры из существующих skill'ов:

- `audit_analyzer/SKILL.md:91-94` — не использовать неизвестные таблицы/
  индексы, не использовать DDL/DML, не подставлять пользовательские
  значения в SQL строкой.
- `legal_summarizer/SKILL.md:78-82` — не редактировать `subject` от LLM,
  не подставлять пользовательский текст в `system` промпт.
- `office_files/SKILL.md:64-72` — OCR недоступен, защищённые паролем
  файлы бросают исключение, `.doc` не поддерживается.

### 3.6 Anti-patterns в SKILL.md

- ❌ Описывать конкретные Python-классы tools. Пишите в терминах capability
  («use `vector_search` with `index_name='audits_index'`»), не в терминах
  Python («call `VectorSearchTool.execute(...)`»). См. `skill-tool-architecture.md:80-89`.
- ❌ Дублировать полную схему БД в SKILL.md. Используйте progressive
  disclosure — большие reference-файлы выносите в `references/`.
- ❌ Подмешивать «как именно реализован Python внутри runtime» —
  skill описывает capability, а не код.

---

## 4. Регистрация в `project.json`

### 4.1 Секция `skills.<name>` — канонический формат

`lib/core/project_settings.py:386-424` (`SkillSettings(BaseModel)` с
`extra="forbid"` — опечатки ловятся на старте):

```jsonc
"skills": {
  "<skill_name>": {
    "enabled": true,                          // OPTIONAL, default true
    "tables": [ ... ],                        // OPTIONAL — §4.2
    "vector_indexes": [ ... ],                // OPTIONAL — §4.3
    "cli": { ... },                           // OPTIONAL — §4.4
    "llm": { "max_tokens": 8192, "temperature": 0.1 },   // OPTIONAL
    "chunking": { ... }                       // OPTIONAL — §4.4
  }
}
```

### 4.2 Секция `tables` — главная

`lib/core/project_settings.py:277-311` — единый список ресурсов
(`str | TableEntry`).

| Поле | Тип | Описание |
|---|---|---|
| `name` | str (required) | Формат `"schema.table"` (контракт `TableResource.__post_init__`, `lib/services/table_registry.py:58-63`); голые имена запрещены. |
| `type` | `"table"` (default) \| `"vector"` | Какой `Resource` создаёт `_auto_register_skills`. |
| `label` | str \| null | opaque-метка. Таблица с label НЕ попадает в LLM-схему; доступ через `TableRegistry.resources_by_label(label)`. Runtime-sync игнорирует. |
| `tracking_column` | str \| null | Колонка для инкрементального поллинга. Дефолт `updated_at` для type=table, `id` для type=vector. |

**Объектная форма:**

```jsonc
"tables": [
  {"name": "oarb.audits", "tracking_column": "updated_at"},
  {"name": "oarb.violations"},
  {"name": "public.agent_predefined_scripts", "label": "scripts_registry"}
]
```

**Строковая форма (минимум):** `"oarb.audits"` ≡ `{"name": "oarb.audits"}`.

**Неизвестные ключи запрещены** (`extra="forbid"`, `lib/core/project_settings.py:306`).

### 4.3 Секция `vector_indexes`

`lib/core/project_settings.py:314-343`. Минимальный generic-контракт: **только `name`**:

```jsonc
"vector_indexes": [
  {"name": "audits_index"}
]
```

`VectorIndexEntry.model_config = ConfigDict(extra="forbid")` —
`source`/`embedding` и прочие legacy-поля НЕ пройдут pydantic.

**Что НЕ должно быть в `vector_indexes[]`**:
- `source` — теперь в PG-реестре (`gateway.vector.index.config_table`);
- `embedding` — теперь в `gateway.vector.embedding` (общий runtime).

### 4.4 Опциональные runtime-секции

| Секция | Поля |
|---|---|
| `cli` | `default_mode`, `default_format`, `max_retries`, `timeout_sec` (`project_settings.py:346-356`) |
| `llm` | `max_tokens`, `temperature` (не выбор модели!) (`project_settings.py:359-368`) |
| `chunking` | `chunk_size`, `chunk_overlap`, `single_call_threshold` (`project_settings.py:371-383`) |

**Выбор модели/провайдера — в `config.json`** (`agents.defaults.*`).
`skills.<name>.llm` — только execution policy.

### 4.5 Что НЕ должно быть в `skills.<name>`

| Legacy ключ | Куда перенесён |
|---|---|
| `embedding.*` | → `gateway.vector.embedding` |
| `cache.*` (был мёртвым) | — (удалён) |
| `sync.*` | → `gateway.sync.*` |
| `vector_index.*` | → `gateway.vector.index.*` |

Обратной совместимости нет — runtime-проверка даст fail-fast.

### 4.6 Валидация

Pydantic-валидация в `ApplicationContext.create()`
(`lib/core/application_context.py:117-119`) — fail-fast с `ConfigurationError`
и списком всех проблем сразу. При добавлении новой обязательной настройки —
добавьте запись в `REQUIRED_KEYS` (`tests/test_config_keys.py:31-171`).

---

## 5. Runtime API для skill'ов (`lib.core.skill_config`)

Единая точка (`lib/core/skill_config.py`) — **никакой копипасты** между skill'ами.

### 5.1 Доступные функции

| Функция | Назначение |
|---|---|
| `get_db_tables(skill_name)` | Доменные таблицы без label (для LLM-схемы) |
| `get_db_schema(skill_name)` | Имя схемы по первой таблице |
| `get_predefined_scripts_table(skill_name)` | Имя реестра SQL-шаблонов через `resources_by_label("scripts_registry")` |
| `get_llm_config(skill_name)` | LLM execution policy для skill'а |
| `get_cli_config(skill_name)` | `default_mode`, `timeout_sec`, `max_retries` |
| `get_chunking_config(skill_name)` | Map-reduce параметры |
| `get_in_memory_cache_path(skill_root)` | Путь к общему DuckDB snapshot |
| `get_vector_index_path(skill_name, skill_root)` | Путь к FAISS-индексу |
| `get_vector_db_table(skill_name)` | Имя storage-таблицы векторов |
| `get_vector_store_table()` | Сериализованные FAISS BYTEA |
| `build_cache_provider(skill_name, skill_root)` | CacheProvider для DuckDB |
| `get_vector_indexes(skill_name)` | Метаданные индексов из runtime-БД |
| `get_embedding_config()` / `get_embedding_model()` | Общий runtime (без `skill_name`) |
| `load_db_config(skill_name)` | `{"schema", "tables"}` |
| `get_max_retries(skill_name)` | — |
| `get_tool_config(skill_name)` | Полная секция skill'а |

### 5.2 Конвенция `skill_config.py` в skill'е

Тонкая обёртка (`audit_analyzer/scripts/skill_config.py:1-93`):

```python
from lib.core import skill_config as _lib
_SKILL_NAME = "<skill_name>"

def get_db_tables() -> list[str]:
    return _lib.get_db_tables(_SKILL_NAME)
# ...
```

Главное — никакой бизнес-логики, только тонкий alias.
**Антипаттерн:** вызывать `lib.core.skill_config` напрямую с литералом
в каждом месте кода.

### 5.3 Standalone-регистрация

Если skill запускается без поднятого gateway (CLI, утилита) — он
регистрирует себя сам (`audit_analyzer/scripts/cli.py:86-110`):

```python
def _ensure_registered() -> None:
    from lib.core.infra_registration import register_vector_storage
    from lib.core.skill_registration import (
        register_embedding_config,
        register_skill_from_config,
    )
    from config import SETTINGS

    cfg = SETTINGS.get("skills", {}).get("<skill_name>", {})
    register_skill_from_config("<skill_name>", cfg)
    register_vector_storage()
    register_embedding_config()
```

`register_skill_from_config` (`lib/core/skill_registration.py:63-99`)
идемпотентен — повторный вызов безопасен.

---

## 6. TableRegistry и модель ресурсов

### 6.1 Что попадает в реестр

`ApplicationContext._auto_register_skills`
(`lib/core/application_context.py:681-693`) запускается при старте gateway:

```python
def _auto_register_skills(ctx):
    from lib.core.skill_registration import register_skill_from_config
    skills = ctx.config_service.settings_section("skills") or {}
    for name, cfg in skills.items():
        register_skill_from_config(name, cfg)
```

### 6.2 SkillRegistration

`lib/services/table_registry.py:94-133`:

```python
@dataclass(frozen=True)
class SkillRegistration:
    name: str
    resources: tuple[Resource, ...]   # TableResource | VectorResource
    enabled: bool = True
```

`__post_init__` (`table_registry.py:58-63, 83-88`) проверяет формат `schema.table`.

### 6.3 Lookup по label

Если ресурс имеет `label` — он исключён из LLM-схемы. Доступ через
`TableRegistry.resources_by_label(label)` (`table_registry.py:233-247`):

```python
from lib.services.table_registry import table_registry
scripts_table = table_registry.resources_by_label("scripts_registry")[0]
```

`skill_config.get_predefined_scripts_table()` использует этот путь
(`lib/core/skill_config.py:82-98`). Подробности — `skill-tool-architecture.md:222-303`.

### 6.4 Инфраструктурные ресурсы

Через `gateway.vector.index.storage_table` регистрируется **общий storage**
сырых эмбеддингов (`lib/core/infra_registration.py:27-53`):

```python
INFRA_KEY_VECTOR_STORAGE = "vector.storage"

def register_vector_storage():
    table_registry.register_infra(INFRA_KEY_VECTOR_STORAGE, (
        VectorResource(name=storage_table, tracking_column="id"),
    ))
```

Регистрируется через `ApplicationContext._register_infra_resources()`
(`lib/core/application_context.py:699-717`).

---

## 7. Контракт Skill ↔ Tool

### 7.1 Главный принцип (`TARGET_ARCHITECTURE.md §22.1-§22.9`)

```text
SKILL instructions → Agent → selects Tool → Tool executes capability
```

Skill **не вызывает** Tool программно (TARGET §22.2,
`tests/test_skill_tool_independence.py:53-67`).
Tool **не импортирует** Skill (TARGET §22.1,
`tests/test_skill_tool_independence.py:70-84`).

### 7.2 Что РАЗРЕШЕНО в Skill

`skill-tool-architecture.md:34-44`:

```python
from lib.services.cache_provider_impl import build_cache_provider
from lib.utils.sql_safety import validate_sql
from lib.utils.text_utils import sanitize_value
from lib.services.table_registry import table_registry
from lib.core.skill_config import get_db_tables, get_vector_index_path, ...
```

Skill и Tool могут использовать **общую инфраструктуру** (`lib/utils`, `lib/services`).

### 7.3 Что ЗАПРЕЩЕНО

**В Tool** (`skill-tool-architecture.md:50-60`,
`tests/test_architecture_tool_domain_free.py`):

```python
from workspace.skills.<anything> import ...           # ЗАПРЕЩЕНО
spec_from_file_location(...)                          # ЗАПРЕЩЕНО
sys.path.insert(.../skills...)                        # ЗАПРЕЩЕНО
```

**В Skill** (`skill-tool-architecture.md:57-60`,
`tests/test_skill_tool_independence.py`):

```python
from workspace.tools import ...                       # ЗАПРЕЩЕНО
from workspace.tools.duckdb_query_tool import ...      # ЗАПРЕЩЕНО
```

### 7.4 Что Tool не должен знать

Запрещены домен-идентификаторы: `audit`, `violations`, `audits_index`,
`audit_analyzer`. Любой `if caller == "...":` routing — запрещён
(TARGET §22.9). Tool — generic capability.

### 7.5 Что Skill не должен знать

Skill пишет инструкции в терминах capability, не Python:

- ✅ «use `vector_search` with `index_name='violations_index'`»
- ❌ «call `VectorSearchTool.execute(query=...)`»
- ❌ «import VectorSearchTool»

### 7.6 Generic Tool'ы, доступные Skill'ам

| Tool | Контракт | Конфиг |
|---|---|---|
| `duckdb_query` | `{sql, params, max_rows}` → `{status, columns, rows, ...}` | `gateway.duckdb_query.*` |
| `vector_search` | `{query, index_name, top_k, threshold}` → `{status, results, ...}` | `gateway.vector_search.*` |
| `compact_context` | `{session_key, force}` | `gateway.compact.*` |

Полные контракты — `docs/skill-tool-architecture.md:93-180`. Для
добавления нового generic tool — скопируйте `workspace/tools/example.py`.

---

## 8. Storage policy и пути

Из `workspace/AGENTS.md`:

- Новые файлы — под `data_store/cache/sessions/<session_key>/`. **НЕ**
  пишите в корень проекта.
- Используйте **относительные пути** в `write_file`/`write`/`edit` —
  `SessionFileRedirectHook` (`workspace/hooks/session_file_redirect_hook.py`)
  сам перенаправит их.
- **Запрещены** абсолютные пути вида `/home/<user>/<project>/...` —
  на сервере таких путей нет.

Для CLI skill'а с файловым входом (`legal_summarizer --file`):

```python
path = Path("data_store/cache/sessions/<session_key>/<file>")
# SessionFileRedirectHook сам найдёт файл и подставит реальный путь.
text = extract_text(path)
```

---

## 9. Тестирование skill'а

### 9.1 Что тестировать

| Слой | Тесты |
|---|---|
| **Unit (skill)** | `tests/test_skill_legal_summarizer.py` — smoke через `monkeypatch` LLM-вызовов |
| **Unit (db_loader)** | `tests/test_db_loader.py` — реальный DuckDB-кэш + `TableRegistry` |
| **Integration** | `tests/test_skill_tool_integration.py` — Skill scenarios + Tool execution |
| **Architecture** | `tests/test_skill_tool_independence.py`, `tests/test_architecture_tool_domain_free.py` |
| **Resource universality** | `tests/test_resource_universality.py` — DoD «новый skill без правок lib/» |

### 9.2 Шаблон теста skill'а

По `tests/test_db_loader.py:1-66`:

```python
import sys
from pathlib import Path
import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "workspace" / "skills" / "<skill_name>" / "scripts"))


@pytest.fixture
def provider():
    from skill_config import build_cache_provider
    from lib.core.skill_registration import register_skill_from_config
    from lib.services.table_registry import table_registry

    register_skill_from_config("<skill_name>", _CFG_FOR_REGISTRATION)
    p = build_cache_provider()
    if not p.open_cache():
        pytest.skip("DuckDB-кэш не найден — нужен реальный gateway refresh")
    return p


@pytest.fixture(autouse=True)
def _reset_registry():
    from lib.services.table_registry import table_registry
    table_registry.clear()
    yield
    table_registry.clear()
```

### 9.3 Что НЕ нужно тестировать

Не пишите `tests/test_<skill>_register.py` с ручной регистрацией через
`lib/core.skill_registration` — registration-test уже в
`tests/test_auto_register_skills.py`. Лучше покройте доменную логику.

---

## 10. Архитектурные тесты — обязательно зелёные

Перед коммитом убедитесь, что эти 4 теста проходят (поломан любой =
архитектурная регрессия):

```bash
pytest tests/test_skill_tool_independence.py          -v
pytest tests/test_architecture_tool_domain_free.py    -v
pytest tests/test_resource_universality.py            -v
pytest tests/test_auto_register_skills.py             -v
```

Что они проверяют:

- `test_skill_tool_independence.py` — Skill не импортирует Tool, Tool не импортирует Skill.
- `test_architecture_tool_domain_free.py` — Tool не содержит audit/домен-строк в коде и описаниях.
- `test_resource_universality.py` — DoD: добавление skill'а не требует правок `lib/`.
- `test_auto_register_skills.py` — декларативная регистрация работает (label, tracking_column, отключённые skill'ы).

---

## 11. Best practices — сводка

### 11.1 DO

✅ Пишите `SKILL.md` в терминах capability, не Python-классов Tool'ов.

✅ Используйте `lib.core.skill_config` через тонкую обёртку, не импортируйте напрямую с литералами.

✅ Декларируйте ресурсы как JSON в `project.json` — никаких `register.py`.

✅ Все таблицы — fully qualified `schema.table`. Голые имена → `TableResource().__post_init__` бросит `ValueError`.

✅ `label` — для таблиц-реестров метаданных (которые не нужны в LLM-схеме).

✅ Проверяйте `extra="forbid"` в `SkillSettings` — опечатка `tablse` ловится на старте.

✅ Соблюдайте storage policy из `workspace/AGENTS.md` — относительные пути.

✅ Имена индексов в `vector_indexes[]` — только `name`. Никаких `source`/`embedding`.

✅ Используйте progressive disclosure — большие знания выносите в `references/` (TARGET §10, §25).

✅ При standalone-вызове самостоятельно регистрируйтесь через `_ensure_registered()`.

✅ Запускайте 4 архитектурных теста (см. §10).

✅ Покрывайте минимум один сценарий unit-тестом по шаблону `tests/test_db_loader.py`.

### 11.2 DON'T (anti-patterns)

❌ `from workspace.tools import ...` в Skill (TARGET §22.2).

❌ Hardcode домен-имён в Skill (TARGET §22.3).

❌ Прятать домен-логику в `lib/services` (TARGET §22.9).

❌ Создавать ещё один `register.py`.

❌ `pip install` в коде skill'а. Все библиотеки в `requirements.txt`.

❌ Абсолютные пути `/home/<user>/<project>/...`.

❌ Tool, который знает о Skill (поймает `test_architecture_tool_domain_free.py`).

❌ Multi-statement SQL или DDL/DML. Безопасность — `lib.utils.sql_safety.validate_sql()`.

❌ Секреты в `project.json` — `${VAR}` + `.secrets.env`.

❌ Дублировать LLM-клиент, чанкинг, офисные утилиты — всё это в `lib/services/` и `workspace/utils/`.

❌ Generic infrastructure в `skills.<name>` (embedding, sync, FAISS root, cache).

❌ Зашивать имена таблиц/индексов в код или промпты как строковые константы.

❌ Описывать конкретные Python-классы tools в SKILL.md.

---

## 12. Definition of Done — чек-лист

Перед коммитом нового skill'а:

1. ☐ Каталог `workspace/skills/<name>/{SKILL.md, scripts/}` создан.
2. ☐ `SKILL.md` написан по §3: правильный frontmatter, decision procedure, «Что не делать».
3. ☐ `__init__.py` пустые.
4. ☐ В `project.json` добавлена секция `skills.<name>` с fully qualified таблицами (если есть).
5. ☐ Если используется `label="scripts_registry"` (или другое) — явно отмечено.
6. ☐ Если у таблицы нестандартная track-колонка — задана per-resource.
7. ☐ Если vector — `gateway.vector.index.storage_table` настроен в общем инфра-слое + `vector_indexes[]` в skill-секции.
8. ☐ CLI skill'а (если есть) использует `lib.core.skill_config` + сам регистрируется через `_ensure_registered()` для standalone.
9. ☐ Skill **НЕ импортирует** `workspace.tools`.
10. ☐ Skill использует generic Tool'ы без домен-routing.
11. ☐ Unit-тест минимум на один сценарий skill'а.
12. ☐ `pytest tests/test_skill_tool_independence.py tests/test_architecture_tool_domain_free.py tests/test_resource_universality.py tests/test_auto_register_skills.py -v` — без падений.
13. ☐ `pytest tests/ -q` — без регрессий.
14. ☐ `python cli_agent.py` стартует без ошибок (smoke).
15. ☐ Если новая обязательная настройка — запись в `REQUIRED_KEYS` (`tests/test_config_keys.py:31-171`).
16. ☐ Документация обновлена:
    - `docs/skill-tool-inventory.md` (строка в сводной таблице);
    - `docs/README.md` (если добавился новый файл);
    - корневой `AGENTS.md` (если новый ключ config);
    - `CHANGELOG.md` (секция `[Unreleased]`).

---

## 13. Пошаговый сценарий создания нового skill'а

### Шаг 1. Спроектируйте

- Это Skill или Tool? (см. §1)
- Какие таблицы/индексы? Сколько режимов?
- Будет ли CLI? Нужен ли LLM? Чанкинг?

### Шаг 2. Создайте структуру каталога

```bash
mkdir -p workspace/skills/<name>/{scripts,references,prompts}
touch workspace/skills/<name>/__init__.py
touch workspace/skills/<name>/scripts/__init__.py
```

### Шаг 3. SKILL.md (см. §3)

### Шаг 4. Объявите в `project.json` (см. §4)

```jsonc
"skills": {
  "<name>": {
    "enabled": true,
    "tables": [
      {"name": "<schema>.<table>"}
    ],
    "vector_indexes": [
      {"name": "<index_name>"}
    ],
    "cli": {
      "default_mode": "<mode>",
      "timeout_sec": 60
    },
    "llm": {
      "max_tokens": 8192,
      "temperature": 0.1
    }
  }
}
```

Если нужны embeddings — настройте **общий** `gateway.vector.embedding`.
Если vector-индексы — `gateway.vector.index.storage_table`.

### Шаг 5. Реализуйте `scripts/`

По образцу `audit_analyzer/scripts/`:
- `cli.py` — точка входа.
- `skill_config.py` — обёртка над `lib.core.skill_config`.
- `<core>.py` — основная логика (split на режимы).
- Если есть LLM — `scripts/llm.py` (паттерн `audit_analyzer/scripts/llm.py`).

### Шаг 6. Тесты (см. §9)

### Шаг 7. Документация (см. §12 п.16)

### Шаг 8. Проверки

```bash
pytest tests/test_skill_tool_independence.py \
       tests/test_architecture_tool_domain_free.py \
       tests/test_resource_universality.py \
       tests/test_auto_register_skills.py -v

pytest tests/ -q
python cli_agent.py          # smoke
```

---

## 14. Сводка референсных файлов

### Нормативные документы
- `docs/TARGET_ARCHITECTURE.md` — нормативный контракт (§3, §22.1-§22.9, §30, §31).
- `docs/skill-tool-architecture.md` — Skill ↔ Tool contract.
- `docs/skill-tool-inventory.md` — текущее состояние реестра skill'ов.
- `docs/table-registry.md` — Resource Model, label semantics, DoD.

### Reference для runtime API
- `lib/core/skill_config.py` — параметризованный runtime API.
- `lib/core/skill_registration.py` — декларативная регистрация ресурсов.
- `lib/core/infra_registration.py` — регистрация инфра-ресурсов.
- `lib/core/project_settings.py` — pydantic-валидация (`extra="forbid"`).
- `lib/services/table_registry.py` — каноническая модель `TableResource`/`VectorResource`/`SkillRegistration`.
- `lib/utils/sql_safety.py::validate_sql` — SQL security boundary.

### Конфигурация
- `project.json` — главная карта; раздел `skills.*`.
- `tests/test_config_keys.py:31-171` — `REQUIRED_KEYS`.

### Существующие skill'ы как reference
- `workspace/skills/audit_analyzer/` — самый полный: SKILL.md + 10 модулей scripts + 3 references + cache/schema.json.
- `workspace/skills/legal_summarizer/` — skill без PG-таблиц, с LLM-map-reduce + prompts.
- `workspace/skills/office_files/` — skill-обёртка над `workspace/utils/office_files.py`.

### Тесты для архитектурных инвариантов
- `tests/test_skill_tool_independence.py`
- `tests/test_architecture_tool_domain_free.py`
- `tests/test_resource_universality.py`
- `tests/test_auto_register_skills.py`
- `tests/test_skill_config_api.py`
- `tests/test_skill_config_lookup.py`
- `tests/test_project_settings.py`
- `tests/test_skill_tool_integration.py`
- `tests/test_db_loader.py` — паттерн fixture для skill'ов с DuckDB.

### Hooks и runtime
- `lib/hooks/tool_audit_hook.py` — автоматическая audit trail для всех tool'ов.
- `workspace/hooks/session_file_redirect_hook.py` — перенаправление файлов в `data_store/cache/sessions/<key>/`.
- `workspace/hooks/recent_files_hook.py` — автоприкрепление созданных файлов.
- `workspace/tools/{duckdb_query_tool,vector_search_tool,compact_context}.py` — generic tools.
- `workspace/tools/example.py` — шаблон нового tool'а.

При изменении `TARGET_ARCHITECTURE.md` или `skill-tool-architecture.md`
синхронизировать этот документ.
