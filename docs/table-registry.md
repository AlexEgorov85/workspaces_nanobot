# Resource Model: декларативное описание ресурсов skill'а

Документ описывает, как skill объявляет свои PG-таблицы и vector-индексы
в `project.json`, как эти декларации превращаются в `TableResource` /
`VectorResource` и попадают в `lib/services/table_registry.py`. Это
единственный путь, которым skill заявляет о своих ресурсах runtime-инфраструктуре.

## Зачем это нужно

Каждый skill читает свои данные через общий DuckDB-снапшот
(`workspace/data_store/duckdb/cache.duckdb`). Чтобы snapshot содержал нужные
таблицы, sync-слой (`PgDuckDbSyncService` + `DuckDbCacheStore`) должен знать,
что именно синхронизировать. До рефакторинга это знание было разбросано:

- по плоским полям skill'а (`db_tables`, `db_additional_tables`, `mode_vector_*`,
  `track_column_overrides`);
- по отдельным `scripts/register.py` файлам, которые каждый skill писал сам;
- по back-compat фасадам (`build_cache_provider`, `_flatten_skill_config`).

Resource Model решает это так: skill — это **декларация в JSON**, а
`ApplicationContext._auto_register_skills` превращает её в runtime-объекты
в `table_registry`. Никаких `register.py`, никаких правок `lib/` под новый skill.

## Короткий ответ

Декларация ресурсов split по доменам:

- **Доменные таблицы** skill'а (что нужно в DuckDB-кэше) — в
  `skills.<name>.tables[]`. Регистрируются как `TableResource` /
  `VectorResource` через `skill_registration`.
- **Имена индексов**, которые использует skill — в
  `skills.<name>.vector_indexes[]` (только `name`).
- **Storage сырых эмбеддингов** (общий runtime, не привязан к skill'у) —
  в `gateway.vector.index.storage_table`. Регистрируется через
  `TableRegistry.register_infra`.

```jsonc
// project.json
"gateway": {
  "vector": {
    "index": {
      "storage_table": "oarb.audit_vectors",
      "default_root": "data_store/vectors",
      "backend": "faiss"
    }
  }
},
"skills": {
  "audit_analyzer": {
    "enabled": true,
    "tables": [
      {"name": "oarb.audit_reports"},
      {"name": "oarb.audits"},
      {"name": "oarb.violations"},
      {"name": "public.agent_predefined_scripts", "label": "scripts_registry"}
    ],
    "vector_indexes": [
      {"name": "audits_index"},
      {"name": "violations_index"},
      {"name": "audit_reports_index"}
    ]
  }
}
```

Какие индексы строить и из каких source-таблиц — описывается в
`public.agent_vector_index_config` (runtime-БД), это **инфраструктурная
декларация**, не часть skill'а.

## Resource: декларативная модель

Каноническая модель — в `lib/services/table_registry.py` (dataclass'ы
`TableResource`, `VectorResource`, `SkillRegistration`). Это единый источник
истины; всё, что приложение знает о ресурсах skill'а, живёт здесь.

### TableResource

Описание одной PostgreSQL-таблицы, которую skill хочет видеть в DuckDB-кэше.
Ресурс ничего не открывает, не выполняет и не знает о DuckDB — это DTO,
которым sync-слой пользуется как входными данными.

- `name` — полное имя таблицы в формате `schema.table` (всегда fully qualified).
- `tracking_column` — колонка для инкрементального отслеживания изменений.
  Если не задана, sync-слой использует generic-дефолт `updated_at`.
- `label` — опциональная opaque-метка. Если задана, таблица исключается
  из описания схемы для LLM (см. `skill_config.get_db_tables()`) и
  доступна только через `TableRegistry.resources_by_label()`. Типичный
  кейс — реестр метаданных вроде `public.agent_predefined_scripts`
  (`label="scripts_registry"`). Runtime-sync её игнорирует.

`TableResource` — frozen-dataclass. Никаких legacy-полей (`db_schema`, `db_tables`,
`predefined_scripts_table`, `mode_vector_*`, `track_column_overrides`)
на нём нет.

### VectorResource

Описание одной PG-таблицы сырых эмбеддингов, поверх которой строится
vector-индекс. Архитектурно это **отдельный вид ресурса**, потому что
vector-таблица попадает в два независимых pipeline'а сразу: обычный
table-sync (PG → DuckDB) и vector-индексация (FAISS / pgvector / Qdrant).

- `name` — полное имя таблицы в формате `schema.table` (qualified всегда).
- `tracking_column` — по умолчанию `id` (строки не апдейтятся, монотонный PK).

Параметры самого эмбеддинга (модель, размерность, URL Ollama) живут в
секции `embedding.*`; параметры индекса — в `vector_indexes[]`.
Это разделение намеренное: ресурс описывает **что** читаем, конфиг — **как**.

### VectorIndexEntry

Один vector-storage индекс в `vector_indexes: [...]`. Минимальный контракт —
имя индекса. Алгоритм построения (FAISS / pgvector / Qdrant) — runtime-параметр
конкретного бэкенда, не часть декларации.

- `name` — логическое имя индекса (`"audits_index"`).

Storage сырых эмбеддингов — **не** здесь (см. `gateway.vector.index.storage_table`
и `register_infra`). Source-таблица (PG-таблица, из которой `tools/build_vectors.py`
читает строки для эмбеддингов) — тоже **не** здесь; это инфраструктурная
декларация в `public.agent_vector_index_config`.

Backend-specific параметры (для FAISS: `text_chunk_size`, `text_chunk_overlap`,
`build_batch_pause_sec`; для Qdrant: `collection_name`) — OPTIONAL ключи
с `extra="allow"`. Читаются конкретным runtime-бэкендом, не валидируются.

### SkillRegistration

Контейнер ресурсов одного skill'а: `name`, `resources` (tuple `TableResource`
и/или `VectorResource`), `enabled`. Это единственная точка сборки, через
которую skill попадает в реестр.

## Декларация через project.json

Секция `skills.<name>` описывается моделью `SkillSettings` в
`lib/core/project_settings.py`. Pydantic-валидация запускается в
`ApplicationContext.create()` и падает fail-fast на опечатках и неверных
типах (`ConfigurationError` со списком всех проблем).

Корневая секция skill'а:

```jsonc
{
  "enabled": true,             // OPTIONAL; false — skill пропускается при регистрации
  "tables": [ ... ],           // единый список ресурсов (str | TableEntry)
  "vector_indexes": [ ... ],   // OPTIONAL; список имён индексов (только name)
  "embedding": { ... },        // OPTIONAL; параметры эмбеддинга
  "cache": { ... },            // OPTIONAL; параметры in-memory кэша
  "cli": { ... },              // OPTIONAL; параметры CLI
  "llm": { ... }               // OPTIONAL; переопределение LLM
}
```

Все секции, кроме `tables[]`, опциональны.

### Секция tables

Единый список ресурсов skill'а. Каждый элемент — либо строка, либо объект
`TableEntry`. Имена таблиц должны быть **fully qualified** (`schema.table`);
голые имена **не** дополняются автоматически.

Строковая форма — min-контракт, без дополнительных атрибутов:

```json
"tables": [
  "oarb.audit_reports",
  "oarb.audits"
]
```

Объектная форма (`TableEntry`) позволяет задать `label`, `tracking_column`:

```json
"tables": [
  {"name": "oarb.audit_reports"},
  {
    "name": "public.agent_predefined_scripts",
    "label": "scripts_registry"
  },
  {
    "name": "oarb.audit_vectors",
    "tracking_column": "id"
  }
]
```

Неизвестные ключи внутри `TableEntry` запрещены (`extra="forbid"` на
pydantic-модели `TableEntry`) — опечатку `trackin_column` pydantic
ловит на старте, до попытки зарегистрировать ресурс.

### Секция vector_indexes

Список имён индексов, которые использует skill. Каждый элемент — объект
`VectorIndexEntry` с обязательным полем `name`. Все остальные поля —
optional и backend-specific (read-only через `extra="allow"`):

```json
"vector_indexes": [
  {"name": "audits_index"},
  {"name": "violations_index"},
  {"name": "audit_reports_index"}
]
```

Имена читаются build-tool'ами (`tools/build_vectors.py`) и `get_vector_index_path()`
для вычисления пути к FAISS-файлу (`<default_root>/<name>`).

## Пример: audit_analyzer (реальный сниппет из project.json)

```jsonc
"gateway": {
  "vector": {
    "index": {
      "storage_table": "oarb.audit_vectors",
      "default_root": "data_store/vectors",
      "backend": "faiss"
    }
  }
},
"skills": {
  "audit_analyzer": {
    "enabled": true,
    "tables": [
      {"name": "oarb.audit_reports"},
      {"name": "oarb.audits"},
      {"name": "oarb.report_items"},
      {"name": "oarb.violations"},
      {"name": "public.agent_predefined_scripts", "label": "scripts_registry"}
    ],
    "vector_indexes": [
      {"name": "audits_index"},
      {"name": "violations_index"},
      {"name": "audit_reports_index"}
    ],
    "embedding": {
      "base_url": "http://localhost:11434/api/embed",
      "model": "mxbai-embed-large:latest",
      "dimension": 1024,
      "http_timeout_sec": 60
    }
  }
}
```

Что попадает в `table_registry`:

- `TableResource(name="oarb.audit_reports")`, `oarb.audits`, `oarb.report_items`,
  `oarb.violations` — из `tables[]`;
- `TableResource(name="public.agent_predefined_scripts", label="scripts_registry")` —
  из `tables[]` с явным label;
- `VectorResource(name="oarb.audit_vectors")` — из `gateway.vector.index.storage_table`
  через `register_infra("vector.storage", ...)`.

## Примеры для новых skill'ов

### sales: минимальный skill

```jsonc
"skills": {
  "sales": {
    "enabled": true,
    "tables": [
      {"name": "sales.orders"},
      {"name": "sales.customers"},
      {"name": "sales.line_items"}
    ]
  }
}
```

Никакого кода на Python — skill готов к регистрации.

### knowledge: объектный формат + label

```jsonc
"gateway": {
  "vector": {
    "index": {
      "storage_table": "kb.kb_embeddings",
      "default_root": "data_store/vectors",
      "backend": "faiss"
    }
  }
},
"skills": {
  "knowledge": {
    "enabled": true,
    "tables": [
      {"name": "kb.articles"},
      {"name": "kb.tags"},
      {"name": "kb.kb_search_index", "label": "scripts_registry"},
      {"name": "public.kb_user_collections"}
    ],
    "vector_indexes": [
      {"name": "kb_index"}
    ],
    "embedding": {
      "base_url": "http://localhost:11434/api/embed",
      "model": "mxbai-embed-large:latest",
      "dimension": 1024
    }
  }
}
```

Здесь:

- `kb.articles`, `kb.tags` — обычные таблицы, дефолтный `tracking_column`;
- `kb.kb_search_index` помечен `label="scripts_registry"`;
- `public.kb_user_collections` — внешняя таблица;
- `kb.kb_embeddings` (vector-storage) — общий runtime, объявлен в
  `gateway.vector.index.storage_table`, регистрируется через `register_infra`.

## label как opaque marker

`label` — это непрозрачная строка, которую skill назначает ресурсу, а
затем ищет через `TableRegistry.resources_by_label()` в собственном коде.
Runtime-инфраструктура (`lib/`) **никогда** не интерпретирует значение
label: sync, FAISS, кэш и tool'ы работают со всеми `TableResource`
одинаково, независимо от наличия label.

### Пример: чтение scripts_registry из skill-кода

```python
from lib.services.table_registry import table_registry

resources = table_registry.resources_by_label("scripts_registry")
if not resources:
    raise RuntimeError("skills.<name>.tables[] не содержит элемента с label='scripts_registry'")
predefined_table = resources[0].name  # qualified 'schema.table'
```

### label не влияет на sync

Это намеренное архитектурное решение: runtime-sync видит все
`TableResource` одинаково. Если skill нужно пометить таблицу
как «не для инкрементального поллинга», он не ставит label, а конфигурирует
`tracking_column` (или просто оставляет дефолт `updated_at`).

## auto-register: как декларация попадает в реестр

`ApplicationContext.create()` вызывает `_auto_register_skills(ctx)` →
`_register_infra_resources(ctx)` (`lib/core/application_context.py`).

**`_auto_register_skills`** делегирует
`lib/core/skill_registration.register_skill_from_config`, который:

1. Читает `skill_cfg["tables"]` — единый список ресурсов (str | dict).
2. Для каждого элемента создаёт `TableResource(name, label?, tracking_column?)`.
3. `vector_indexes[]` **не** регистрирует ресурсы (это инфраструктурная
   зона — storage и source-table).
4. Дедупликация по имени: если имя встречается дважды, второй пропускается.
5. Регистрирует результат через `table_registry.register(SkillRegistration(...))`.
6. Если задан `embedding.*`, пишет его в embedding-конфиг реестра.

**`_register_infra_resources`** делегирует
`lib/core/infra_registration.register_vector_storage()`, который:

1. Читает `gateway.vector.index.storage_table`.
2. Регистрирует `VectorResource(name=storage_table, tracking_column="id")`
   через `table_registry.register_infra("vector.storage", ...)`.

Та же логика используется в standalone-режиме (`tools/build_vectors.py`)
— `register_vector_storage()` вызывается там явно, чтобы реестр был
заполнен при ручном запуске без `ApplicationContext`.

## lookup API: TableRegistry

Глобальный singleton `table_registry` живёт в
`lib/services/table_registry.py`. Два независимых namespace'а:

- `_registrations` — skill-ресурсы (через `register(SkillRegistration(...))`);
- `_infra` — runtime-ресурсы общего назначения (через
  `register_infra(key, resources)`).

Агрегаторы (`table_names`, `vector_names`, `resources`,
`tracking_column_for`) **объединяют** оба namespace'а — сборка runtime'а
(`_make_sync_services`) не различает источник ресурса.

Основные методы:

| Метод | Назначение |
|---|---|
| `register(SkillRegistration)` | Регистрация skill'а (доменные таблицы + вектора). |
| `register_infra(key, resources)` | Регистрация runtime-ресурса общего назначения (storage сырых эмбеддингов). |
| `unregister_infra(key)` | Удалить инфра-регистрацию. |
| `get_infra(key)` / `infra_keys()` | Lookup инфра-ресурсов по ключу namespace'а. |
| `table_names()` | Имена всех `TableResource` (skills + infra), в порядке регистрации. |
| `vector_names()` | Имена всех `VectorResource` (skills + infra). |
| `resources()` | Все ресурсы (таблицы + векторы) одной плоской tuple. |
| `resources_by_label(label)` | `TableResource` skill'ов с указанным `label` (инфру **не** смотрит — label доменная метка). |
| `skill_for_table(table)` | `SkillRegistration`, владеющая таблицей (только skill-ресурсы). |
| `tracking_column_for(table)` | Track-колонка для таблицы (skills + infra; `id` для vector). |
| `names()` / `enabled_names()` | Имена зарегистрированных skill'ов (все/только enabled). |
| `set_embedding_config(**kwargs)` / `embedding_config()` | Generic-конфиг эмбеддингов (не per-skill). |
| `snapshot_path(workspace_path)` | Путь к общему DuckDB-снапшоту. |

## track column

Отслеживание изменений для инкрементального поллинга — свойство конкретной
таблицы, а не навыка в целом. В Resource Model оно живёт прямо на ресурсе:

- `TableResource.tracking_column` — явно заданное значение или `None`;
- `VectorResource.tracking_column` — по умолчанию `id`.

Логика lookup (в `SkillRegistration.tracking_column_for`):

1. Если ресурс найден и `tracking_column` задан — вернуть его.
2. Если ресурс — `VectorResource` без явной `tracking_column` — вернуть `id`.
3. Если ресурс не найден или это `TableResource` без `tracking_column` —
   вернуть `updated_at` как generic-дефолт.

## Контроль синхронизации: gateway.sync.*

Все sync-параметры (`poll_interval_sec`, `full_resync_every`,
`max_queue_size`, `reconnect_backoff_sec`, `reconnect_backoff_max_sec`)
живут в `gateway.sync.*` (глобальные, не per-skill).
Определены в `GatewaySettings.sync` (`lib/core/project_settings.py`).

Наблюдаемость:

- `PgDuckDbSyncService.get_stats()` — `polls`, `full_resyncs`, `reconnects`,
  `errors`, размер очереди;
- отключить skill без удаления конфига: `"enabled": false` в
  корне секции `skills.<name>`.

## Где лежит снапшот

Единый файл для всех skill'ов: `workspace/data_store/duckdb/cache.duckdb`
(`TableRegistry.snapshot_path()`). Запросы — через tool `duckdb_query`
(read-only SELECT).

## Definition of Done для нового skill'а

Чек-лист:

1. В `project.json` добавлена секция `skills.<name>` с `tables: [...]`
   (fully qualified имена). Если используются индексы — также
   `vector_indexes: [{name: ...}]` (только имена).
2. Если используется role-based lookup (например, реестр SQL-шаблонов) —
   элемент в `tables[]` помечен `label` через объектную форму.
3. Если у таблицы нестандартная track-колонка — задана per-resource
   через `TableEntry.tracking_column`.
4. Если используются эмбеддинги — `embedding.*` в корне skill'а.
5. Если skill отключён — `enabled: false` в корне секции.
6. Runtime API skill'а — через `lib.core.skill_config` (параметризован
   по `skill_name`), не собственный `skill_config.py`.
7. Регрессионный тест: `tests/test_resource_universality.py`.
8. Smoke: `python cli_agent.py` стартует без ошибок.

## Релевантные тесты

- `tests/test_table_registry.py` — поведение `TableResource`/`VectorResource`/
  `SkillRegistration`/`TableRegistry`/`register_infra`.
- `tests/test_resource_universality.py` — DoD «новый skill без правок `lib/`».
- `tests/test_auto_register_skills.py` — поведение `_auto_register_skills`:
  парсинг `tables[]`; `vector_indexes[].source` не регистрируется как ресурс.
- `tests/test_infra_registration.py` — `register_vector_storage` через
  `gateway.vector.index.storage_table`.
- `tests/test_project_settings.py` — pydantic-валидация `TableEntry`/
  `VectorIndexEntry`, fail-fast на опечатках.
- `tests/test_skill_config_lookup.py` — `resources_by_label("scripts_registry")`
  в skill-коде (audit_analyzer).
- `tests/test_skill_config_api.py` — единый `lib.core.skill_config` API,
  multi-skill сценарии.
- `tests/test_config_keys.py` — обязательные ключи конфига.

Полный test-run: `python -m pytest tests/ -q` — без регрессий.

## См. также

- `lib/services/table_registry.py` — каноническая модель.
- `lib/core/project_settings.py` — pydantic-модель `SkillSettings`.
- `lib/core/skill_registration.py` — `register_skill_from_config`,
  `build_resources_for_skill`.
- `lib/core/application_context.py` — `_auto_register_skills`.
- `docs/skill-tool-architecture.md` — контракт Skill ↔ Tool.
- `docs/DATABASE.md` — § PgDuckDbSyncService / DuckDbCacheStore и § «Конфигурация навыка».
