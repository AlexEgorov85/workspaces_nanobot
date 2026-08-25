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

Единый список ресурсов `tables: [...]` хранится в `project.json` в секции
`skills.<name>`. Vector-индексы — в `vector_indexes: [...]`. Регистрация
ресурсов происходит автоматически при старте шлюза/CLI.

```jsonc
// project.json
"skills": {
  "audit_analyzer": {
    "enabled": true,
    "tables": [
      {"name": "oarb.audit_reports"},
      {"name": "oarb.audits"},
      {"name": "oarb.violations"},
      {"name": "public.agent_predefined_scripts", "label": "scripts_registry"},
      {"name": "oarb.audit_vectors", "tracking_column": "id"}
    ],
    "vector_indexes": [
      {"name": "audits_index", "source": "oarb.audit_vectors"}
    ]
  }
}
```

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

Один vector-storage индекс в `vector_indexes: [...]`. Минимальный контракт:
имя индекса + источник данных. Алгоритм построения (FAISS / pgvector / Qdrant)
— runtime-параметр конкретного бэкенда, не часть декларации.

- `name` — логическое имя индекса (`"audits_index"`).
- `source` — PG-таблица-источник (сырые эмбеддинги).
- `backend` — `"faiss"` (по умолчанию), `"pgvector"`, `"qdrant"` и т.п.
- `default_path` — путь к файлу индекса (для FAISS).

Backend-specific параметры (для FAISS: `text_chunk_size`, `text_chunk_overlap`,
`build_batch_pause_sec`; для Qdrant: `collection_name`) — это OPTIONAL ключи
с `extra="allow"`. Они читаются конкретным runtime-бэкендом, не валидируются
на уровне pydantic.

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
  "vector_indexes": [ ... ],   // vector-индексы (min-контракт: name + source)
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

Список vector-индексов skill'а. Каждый элемент — объект `VectorIndexEntry`
с обязательными полями `name` и `source`. Все остальные поля — optional
и backend-specific (read-only через `extra="allow"`):

```json
"vector_indexes": [
  {
    "name": "audits_index",
    "source": "oarb.audit_vectors",
    "default_path": "data_store/vectors/audits_index",
    "text_chunk_size": 500,
    "text_chunk_overlap": 80,
    "build_batch_pause_sec": 0.5
  }
]
```

## Пример: audit_analyzer (реальный сниппет из project.json)

```jsonc
"skills": {
  "audit_analyzer": {
    "enabled": true,
    "tables": [
      {"name": "oarb.audit_reports"},
      {"name": "oarb.audits"},
      {"name": "oarb.report_items"},
      {"name": "oarb.violations"},
      {"name": "public.agent_predefined_scripts", "label": "scripts_registry"},
      {"name": "oarb.audit_vectors", "tracking_column": "id"}
    ],
    "vector_indexes": [
      {
        "name": "audits_index",
        "source": "oarb.audit_vectors"
      }
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
  `oarb.violations` — из `tables[]` (строковые и объектные элементы);
- `TableResource(name="public.agent_predefined_scripts", label="scripts_registry")` —
  из `tables[]` с явным label;
- `VectorResource(name="oarb.audit_vectors", tracking_column="id")` — из
  `tables[]` с `type="vector"`.

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

### knowledge: объектный формат + vector + label

```jsonc
"skills": {
  "knowledge": {
    "enabled": true,
    "tables": [
      {"name": "kb.articles"},
      {"name": "kb.tags"},
      {"name": "kb.kb_embeddings", "tracking_column": "ingested_at", "type": "vector"},
      {"name": "kb.kb_search_index", "label": "scripts_registry"},
      {"name": "public.kb_user_collections"}
    ],
    "vector_indexes": [
      {"name": "kb_index", "source": "kb.kb_embeddings"}
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
- `kb.kb_embeddings` — vector-источник (`type="vector"`, `tracking_column="ingested_at"`);
  одновременно объявлен и как TableResource, и как источник для `vector_indexes[]`;
- `kb.kb_search_index` помечен `label="scripts_registry"`;
- `public.kb_user_collections` — внешняя таблица.

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

`ApplicationContext.create()` вызывает `_auto_register_skills(ctx)`
(`lib/core/application_context.py`). Эта функция делегирует
`lib/core/skill_registration.register_skill_from_config`, который:

1. Читает `skill_cfg["tables"]` — единый список ресурсов (str | dict).
2. Для каждого элемента создаёт `TableResource(name, label?, tracking_column?)`.
3. Читает `skill_cfg["vector_indexes"]` — для каждого элемента создаёт
   `VectorResource(name=source, tracking_column="id")` (если source ещё
   не зарегистрирован как TableResource).
4. Дедупликация по имени: если имя встречается дважды, второй пропускается.
5. Регистрирует результат через `table_registry.register(SkillRegistration(...))`.
6. Если задан `embedding.*`, пишет его в embedding-конфиг реестра.

## lookup API: TableRegistry

Глобальный singleton `table_registry` живёт в
`lib/services/table_registry.py`. Основные методы:

| Метод | Назначение |
|---|---|
| `table_names()` | Имена всех `TableResource` всех enabled-registrations, в порядке регистрации. |
| `vector_names()` | Имена всех `VectorResource` всех enabled-registrations. |
| `resources()` | Все ресурсы (таблицы + векторы) одной плоской tuple. |
| `resources_by_label(label)` | `TableResource` с указанным `label` (enabled только). |
| `skill_for_table(table)` | `SkillRegistration`, владеющая таблицей (включая vector). |
| `tracking_column_for(table)` | Track-колонка для таблицы (per-resource override, иначе `updated_at` для таблиц, `id` для vector). |
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
   (fully qualified имена) и при необходимости `vector_indexes: [...]`.
2. Если используется role-based lookup (например, реестр SQL-шаблонов) —
   элемент в `tables[]` помечен `label` через объектную форму.
3. Если у таблицы нестандартная track-колонка — задана per-resource
   через `TableEntry.tracking_column`.
4. Если есть vector — элемент в `tables[]` с `type="vector"` + секция
   `vector_indexes[]` + `embedding.*`.
5. Если skill отключён — `enabled: false` в корне секции.
6. Регрессионный тест: `tests/test_resource_universality.py`.
7. Smoke: `python cli_agent.py` стартует без ошибок.

## Релевантные тесты

- `tests/test_table_registry.py` — поведение `TableResource`/`VectorResource`/
  `SkillRegistration`/`TableRegistry`.
- `tests/test_resource_universality.py` — DoD «новый skill без правок `lib/`».
- `tests/test_auto_register_skills.py` — поведение `_auto_register_skills`:
  парсинг `tables[]` и `vector_indexes[]`.
- `tests/test_project_settings.py` — pydantic-валидация `TableEntry`/
  `VectorIndexEntry`, fail-fast на опечатках.
- `tests/test_skill_config_lookup.py` — `resources_by_label("scripts_registry")`
  в skill-коде (audit_analyzer).
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
