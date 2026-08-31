# Changelog

Все значимые изменения в проекте **nanobot — Personal AI Agent** будут задокументированы в этом файле.

Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.0.0/), проект придерживается [Semantic Versioning](https://semver.org/lang/ru/).

Релизные ветки именуются как `release/vX.Y`, теги патч-релизов — `vX.Y.Z`.

## [Unreleased]

> Состояние тестов на момент правки: **1590 passed, 14 skipped** (`pytest`).
> Содержит три больших блока: `refactor/skills-tools-cleanup`
> (generic tools + audit_analyzer cleanup, +архитектурные тесты),
> `refactor/core-extract-duckdb-faiss` (lib → generic, table_registry,
> AST-SQL-guard, миграции схемы) и **`refactor/vector-index-infra`**
> (vector-storage как инфраструктурный ресурс через `register_infra`).
> Ключевые совместимости: skill `audit_analyzer` сохранён для
> CLI/бенчмарка/e2e; snapshot DuckDB публикуется по пути
> `table_registry.snapshot_path()` → `workspace/data_store/duckdb/cache.duckdb`.
> `audit_vectors` теперь попадает в DuckDB-кэш через инфра-регистрацию
> (`gateway.vector_index.storage_table`).

### Added

- **Tool `nl_sql_generate`** (`workspace/tools/nl_sql_generate.py`) —
  generic NL→SELECT pipeline: генерирует SELECT по whitelist'у таблиц
  из `TableRegistry`, валидирует через `EXPLAIN` и выполняет в общем
  DuckDB-кеше. Заменил режим `generated_sql` навыка `audit_analyzer`
  в виде generic tool (skill CLI-режим сохранён для бенчмарков).
  Использует shared infra: `lib.services.nl_sql_runner.NlSqlRunner`
  (общий pipeline), `lib.services.schema_formatter.SchemaFormatter`
  (internal service для описания схемы), `workspace.tools.column_descriptions`
  (in-process lookup hints). Параметры: `query`, `max_rows`,
  `no_few_shot`, `skip_hints`, `hints_max_matches`, `context`.
  Конфиг: `gateway.nl_sql_generate.*`.

- **Tool `column_descriptions`** (`workspace/tools/column_descriptions.py`) —
  структурированный словарь подсказок (термин → колонка) для подмешивания
  в system prompt `nl_sql_generate`. Заменил бывший
  `workspace/skills/audit_analyzer/scripts/column_hints.py`. Словарь
  читается из inline `entries` в `config.json` или опционально из
  внешнего JSON-файла через `data_file`. Параметры: `term`,
  `match_all`, `max_matches`. Конфиг: `tools.column_descriptions.*`
  (хранение в `config.json`, рядом с `tools.legal_summarizer_query`).

- **Internal service `SchemaFormatter`** (`lib/services/schema_formatter.py`) —
  формирует описание схемы БД для LLM system prompt. Использует
  `TableRegistry` (whitelist) + `CacheProvider.get_schema` +
  `lib.utils.sql_safety.format_schema`. Кешируется на уровне процесса
  (TTL). **Не является tool'ом** — это internal helper, вызываемый из
  `NlSqlRunner` через DI / in-process call, дешевле по токенам, чем
  отдельный `schema_describe` tool.

- **`NlSqlRunner`** (`lib/services/nl_sql_runner.py`) — общий NL→SELECT
  pipeline (whitelist + LLM retry + EXPLAIN + execute). Переиспользуется
  tool'ом `nl_sql_generate`.

### Changed

- **`docs/skill-tool-architecture.md`** — добавлены §8.1 «Контракт
  `nl_sql_generate`» и §8.2 «Контракт `column_descriptions`».

- **`docs/skill-tool-inventory.md`** — добавлены строки `nl_sql_generate`
  и `column_descriptions` в сводную таблицу.

- **`workspace/TOOLS.md`** — добавлены секции `nl_sql_generate` и
  `column_descriptions` с примерами использования.

- **`tools/generate_predefined_scripts_sql.py`** — переведён на прямой
  импорт `lib.core.skill_config.get_predefined_scripts_table("audit_analyzer")`
  вместо удалённого `workspace/skills/audit_analyzer/scripts/skill_config.py`.

- **`config.json::tools.column_descriptions`** — добавлена секция с
  inline `entries` (термин→колонка) для `nl_sql_generate`-hints.

### Changed (skill audit_analyzer → tool-only)

- **Skill `audit_analyzer` полностью переведён на tool-only**. Удалён
  каталог `scripts/` целиком (`cli.py`, `predefined.py`, `predefined_mode.py`,
  `db_loader.py`, `scripts_registry.py`, `column_hints.py`, `protocol.py`,
  `output.py`, `llm.py`, `skill_config.py`, `generated_sql_mode.py`,
  `__init__.py`). Удалён также `cache/schema.json` (снимок схемы —
  legacy-артефакт). Skill теперь содержит только `SKILL.md` +
  `references/` (`schema.md`, `vector_indexes.md`, `sql_guidance.md`).

  Все запросы идут через generic tools: `nl_sql_generate` (NL→SELECT),
  `duckdb_query` (точный SELECT), `vector_search` (семантика),
  `column_descriptions` (подсказки). Документация skill'а обновлена:
  `SKILL.md` (decision procedure → tool-only), `references/sql_guidance.md`
  (рекомендуемый путь — `nl_sql_generate`), `references/schema.md` (как
  читать схему через tools), `references/vector_indexes.md` (NL→SELECT
  → `nl_sql_generate`).

- **`project.json::skills.audit_analyzer`** — удалены секции `cli` и `llm`
  (после перевода skill'а на tool-only обе секции не нужны).

- **`tests/test_config_keys.py::REQUIRED_KEYS`** — удалены ключи
  `skills.audit_analyzer.cli.*` и `skills.audit_analyzer.llm.*` (skill
  больше не имеет собственного CLI / LLM-политики — это generic tools).
  `gateway.nl_sql_generate.*` сохранены в `REQUIRED_KEYS`.

- **`benchmarks/items/{simple,medium,hard}.yaml`** — переписаны с
  `audit_analyzer/scripts/cli.py --mode ...` на вызовы `nl_sql_generate`
  / `duckdb_query`. Заголовки yaml дополнены комментарием о tool-only.

- **`README.md`** — убрана команда `python workspace/skills/audit_analyzer/scripts/cli.py`,
  добавлена сноска о работе skill'а через tool'ы агента.

### Removed

- **`workspace/skills/audit_analyzer/scripts/`** (целиком): все 12 файлов
  удалены. Логика полностью перенесена в `lib/services/nl_sql_runner.py`,
  `lib/services/schema_formatter.py`, `workspace/tools/nl_sql_generate.py`,
  `workspace/tools/column_descriptions.py`.

- **`workspace/skills/audit_analyzer/cache/schema.json`** — снимок схемы
  не используется skill'ом после перехода на tool-only; актуальная схема
  читается через `duckdb_query` (information_schema) или `nl_sql_generate`.

- **`tests/test_db_loader.py`** — тестировал удалённый `db_loader.py`.

- **`tests/test_skill_config_lookup.py`** — тестировал удалённый
  `workspace/skills/audit_analyzer/scripts/skill_config.py` (lookup через
  TableRegistry теперь покрывается `test_table_registry.py`).

- **`workspace/skills/audit_analyzer/__init__.py`** — пустой файл, не
  нужен (Python не требует `__init__.py` для распознавания пакета через
  `tools.project_loader`).

### Added (legacy tools, см. ниже)

- **Tool `duckdb_query`** (`workspace/tools/duckdb_query_tool.py`) —
  generic read-only SQL-tool, выполняет SELECT-запросы в DuckDB-кэш.
  Не знает конкретных таблиц / Skills. Использует
  `lib.utils.sql_safety.validate_sql` как последнюю границу безопасности
  (SELECT-only, multi-statement запрещён). Параметры: `sql`, `params`,
  `max_rows`. Конфиг: `gateway.duckdb_query.*`.

- **Tool `vector_search`** (`workspace/tools/vector_search_tool.py`) —
  generic семантический поиск по указанному FAISS-индексу. Не знает
  имён конкретных индексов; получает `index_name` от вызывающей стороны.
  Использует `lib.services.cache_provider.CacheProvider.search_vector`.
  Параметры: `query`, `index_name`, `top_k`, `threshold`. Конфиг:
  `gateway.vector_search.*`.

- **Утилиты `lib/utils/sql_safety.py`** и **`lib/utils/text_utils.py`** —
  перенесены из skill'а `audit_analyzer` (бывших
  `scripts/database.py`/`scripts/output.py`) для переиспользования
  обоими tool'ами и skill'ами. Контракты сохранены 1:1.

- **`Skill audit_analyzer/references/`** — progressive disclosure:
  `schema.md`, `vector_indexes.md`, `sql_guidance.md`. Позволяют
  агенту загружать детальные знания по необходимости, не раздувая
  `SKILL.md` (см. docs/TARGET_ARCHITECTURE.md §10).

- **AST-политика SQL Security Guard (`lib/utils/sql_safety.py`)** —
  read-only SQL-валидация на sqlglot (вместо строковых эвристик):
  запрет SELECT INTO / опасных функций / системных каталогов /
  multi-statement; `validate_sql_report` для audit trail. См.
  docs/DATABASE.md § «Инфраструктурные границы P0».

- **Migration framework (`tools/migrate.py`)** — версионные миграции
  схемы: `sql/migrations/schema_migrations.sql` (tracking-таблица
  `public.schema_migrations` с SHA256-checksum) и `V001__baseline.sql`
  (точка отсчёта, без DDL). Runner применяет ожидающие миграции
  транзакционно (`python tools/migrate.py --apply`), поддерживает
  `--status` / `--dry-run` / `--verify` / `--baseline` / `--force`.
  Порядок и правила — в `sql/README.md` § «Миграции схемы».

- **Тесты:** `tests/test_duckdb_query_tool.py`,
  `tests/test_vector_search_tool.py`, `tests/test_skill_tool_independence.py`,
  `tests/test_architecture_tool_domain_free.py`,
  `tests/test_skill_tool_integration.py`,
  `tests/test_core_infrastructure_independence.py`,
  `tests/test_sql_safety.py`, `tests/test_text_utils.py`,
  `tests/test_contract/` (контракт поверхности nanobot 0.3.0) —
  тесты новых tool'ов, утилит и архитектурные тесты (TARGET §28).

- **Документация:** `docs/skill-tool-architecture.md`,
  `docs/refactor_baseline.md`, `docs/skill-tool-inventory.md`,
  `docs/runtime_patches.md`, `docs/table-registry.md`,
  `docs/architecture/nanobot-inventory.md` (JSON + сканер
  `tools/scan_nanobot_inventory.py`), `docs/core-infrastructure.md`
  (границы core vs skill).

### Changed

- **`workspace/skills/audit_analyzer/SKILL.md`** переписан: убраны
  дубли с разделами, добавлен «Контракт зависимостей» (явно указано,
  что skill использует `lib/utils/` через back-compat re-export);
  decision procedure для выбора tool'ов (TARGET §8); ссылки на
  `references/`; явное отделение от Python-реализаций tool'ов;
  снят DEPRECATED-блок для agent-flow. Удалён раздел «Runtime context»
  (он врал — providers не регистрировались).
- **`workspace/skills/audit_analyzer/scripts/database.py`** — дубли
  `validate_sql`/`format_schema` удалены; реализация теперь только в
  `lib/utils/sql_safety.py` (TARGET §4). Оставлен back-compat
  re-export для публичного API skill'а.
- **`workspace/skills/audit_analyzer/scripts/output.py`** — дубль
  `_sanitize_value` удалён; реализация только в
  `lib/utils/text_utils.py`. Оставлен back-compat re-export.
- **`lib/utils/text_utils.py`** — добавлены `sanitize_value`,
  `truncate_middle`; дублирование `_sanitize_value`/`_truncate` в skill
  и tool устранено.
- **`benchmarks/items/{simple,medium,hard}.yaml`** — вызовы
  `audit_analyze.bat` заменены на `python scripts/cli.py`.
- **`project.json`** — секции `gateway.audit_predefined.*`,
  `gateway.audit_vector.*`, `gateway.audit_sql.*` заменены на
  `gateway.duckdb_query.*` и `gateway.vector_search.*`.
- **`lib/services/audit_memory_store.py`** — `schema` default
  `"oarb"` → `"main"`; docstring переписан как generic
  infrastructure (имя класса сохранено для back-compat).
- **`lib/services/audit_sync_service.py`** — `schema` default
  `"oarb"` → `"main"`; docstring переписан.
- **`lib/services/audit_settings.py`** — функция `audit_vector_settings`
  принимает optional kwarg `section: Tuple[str, ...]` (по умолчанию
  `("skills", "audit_analyzer")`). Позволяет будущим skills читать
  настройки из произвольной секции.

### Removed

- **`workspace/tools/audit_analyzer_tool.py`** — три tool'а
  (`audit_run_predefined_script`, `audit_search_vector`,
  `audit_generate_sql`) удалены. Они нарушали §3, §22.1, §22.2
  docs/TARGET_ARCHITECTURE.md (импортировали skill через `importlib`).
  Функциональность перенесена в skill workflow + generic tools.
- **`tests/test_tools_audit_analyzer.py`** — 1326 строк тестов
  удалённого file. Заменён на targeted-тесты (`test_duckdb_query_tool.py`,
  `test_vector_search_tool.py`) + architectural tests.
- **`workspace/skills/audit_analyzer/audit_analyze.bat`** и
  **`audit_analyze.sh`** — обёртки вокруг `scripts/cli.py` удалены.
  CLI теперь запускается напрямую: `python scripts/cli.py --mode ...`.
- **`workspace/skills/audit_analyzer/data_store/cache/*`** — ad-hoc
  артефакты одного прогона (`fetch_shell.py`, `_dump_report_text.py`,
  `_explore_audit7.py`, `_verify_audit7.{py,out}`,
  `audit_types_query.py`, отчёты в `.md`/`.json`). Не runtime, уже
  в `.gitignore`.
- **`workspace/skills/audit_analyzer/scripts/generated/`** — каталог с
  одноразовым dump-скриптом `fetch_audit_title.py`. Никем не вызывался.
- **`workspace/skills/audit_analyzer/tests/e2e_test.py`** — standalone
  скрипт (не pytest, не CI).
- **`workspace/skills/audit_analyzer/scripts/__init__.py`** —
  legacy-фасад с `run_predefined`/`run_sql`/`run_vector`. Никем не
  импортировался.
- **`workspace/skills/audit_analyzer/providers.py`** — runtime-context
  providers, которые были описаны в `SKILL.md`, но никем не
  регистрировались. Регистрация через `ApplicationContext.start()`
  нарушила бы docs/TARGET_ARCHITECTURE.md §4 (lib не должен зависеть от
  skill).
- **`lib/services/preload_service.preload_audit_cache`**,
  **`background_audit_cache_refresh`**, **`start_audit_cache_tasks`** /
  **`stop_tasks`**, **`get_audit_cache_config`** / **`_audit_settings`** —
  legacy CLI-путь к `audit_cache.duckdb` и фоновые задачи удалены
  (писатель — только `AuditMemoryStore.publish()` через gateway).
  `cli_agent.py::_run_patched_repl` больше не обновляет `audit_cache.duckdb`
  локально (по дизайну).

### Moved

- **`sql/audit_analyzer/create_public_agent_vector_index_config.sql`** →
  **`sql/vectors/create_vector_index_config.sql`**.
- **`sql/audit_analyzer/create_public_agent_vector_index_store.sql`** →
  **`sql/vectors/create_vector_index_store.sql`**.

  Эти таблицы — generic FAISS-метаданные, исторически лежали в
  `sql/audit_analyzer/`. После переноса `sql/README.md` обновлён:
  векторы — отдельный раздел, audit_analyzer — только доменные таблицы.

### Block: vector-index-infra

#### Added

- **`TableRegistry.register_infra(key, resources)`** — отдельный namespace
  для инфраструктурных ресурсов runtime'а (не привязан к домену skill'а).
  Парные методы: `unregister_infra`, `get_infra`, `infra_keys`. Агрегаторы
  (`table_names`, `vector_names`, `resources`, `tracking_column_for`)
  объединяют skills + infra; `resources_by_label` смотрит только skills
  (label — доменная метка).
- **`lib.core.infra_registration.register_vector_storage()`** — единая
  точка регистрации vector-storage через `gateway.vector_index.storage_table`.
  Делегируется из `ApplicationContext._register_infra_resources` и из
  standalone `tools/build_vectors.py`.
- **`lib.core.skill_config`** — параметризованный runtime API для skill'ов
  (`get_db_tables(skill_name)`, `get_llm_config(skill_name)`,
  `get_embedding_config(skill_name)`, `get_vector_*` и т.д.). Единая точка
  для всех skill'ов — подготовка к N skill'ам. Старый
  `workspace/skills/audit_analyzer/scripts/skill_config.py` стал тонкой
  обёрткой с `_SKILL_NAME="audit_analyzer"`.
- **Тесты:** `tests/test_register_infra` (12 кейсов), `tests/test_infra_registration.py`
  (6 кейсов), `tests/test_skill_config_api.py` (16 кейсов multi-skill).
- **Документация:** `docs/table-registry.md` переписан (vector_indexes[]
  больше не имеет `source`; раздел `lookup API` дополнен `register_infra`).

#### Changed

- **`gateway.vector_index.storage_tables` (list) → `gateway.vector_index.storage_table`**
  (str) — единая общая storage-таблица для runtime'а. Мигрированы
  все 4 читателя: `lib/services/cache_provider_impl.py`,
  `tools/build_vectors.py`, `workspace/skills/audit_analyzer/scripts/skill_config.py`,
  `lib/core/project_settings.py`.
- **`skills.<name>.vector_indexes[].source` — поле удалено.** PG-таблица
  исходных строк — инфраструктурная декларация, живёт в
  `public.agent_vector_index_config` (runtime-БД). Имена индексов (`name`)
  остаются в `vector_indexes[]`.
- **`audit_vectors` теперь попадает в DuckDB-кэш.** `ApplicationContext._register_infra_resources`
  читает `gateway.vector_index.storage_table` и регистрирует
  `VectorResource` через `register_infra("vector_index.storage", ...)`.
  `_make_sync_services` использует `table_registry.resources()` (skills + infra).
- **`tools/build_vectors.py`** — теперь явно вызывает `register_vector_storage()`
  (был standalone-запуск с пустым реестром, `vector_names()` был пуст,
  скрипт выходил с ошибкой).

#### Removed

- **`gateway.vector_index.cache_tables` — удалён.** Ключ никем не читался
  (sync берёт список из `TableRegistry` → `skills.*.tables[]`).
  Мигрированы: `project.json`, `VectorIndexSettings`, `test_config_keys`.

#### Fixed

- **`tests/test_application_context.py`** — мок `ConfigurationError` добавлен
  в fake `config` модуль (был пропуск теста; 7 тестов падали).
- **`tools/build_vectors.py`** — `db_table = args.db_table` перезаписывал
  уже корректный `split('.', 1)`, в результате скрипт сообщал
  `oarb.oarb.audit_vectors`. Парсинг `--db-table` теперь поддерживает
  полное имя (`schema.table`) и обрезанное (`table` в той же схеме).
- **`build_cache_provider` (`lib/services/cache_provider_impl.py`)** —
  читал `storage_table`/`default_root` из `cfg["gateway"]` (skill-секции),
  а не из глобального `gateway.vector_index` (инфра-секция). В результате
  провайдер строился с пустым `vector_db_table=""`, и `search_vector`
  молча возвращал 0 результатов с сообщением
  `Индекс '<name>' не найден в кэше`. Теперь источник — глобальный
  `SETTINGS["gateway"]["vector_index"]` (приоритет), fallback —
  `cfg["tables"][type="vector"]` для standalone-режима.

### Block: skill-configuration-boundary

> Жёсткое разделение между **domain binding** (`skills.<name>.*`) и
> **shared runtime infrastructure** (`gateway.*`). Удалена обратная
> совместимость для legacy-путей — fail-fast на уровне runtime.
> Skill `audit_analyzer` сохранён для CLI/бенчмарка/e2e.
>
> Правило (TARGET_ARCHITECTURE §skills.* boundary):
>
>   * Меняется при смене домена skill'а → `skills.<name>.*`.
>   * Меняется при смене инфраструктуры, но не домена → `gateway.*`.
>   * Меняется при смене deployment'а → `channels.*` или env.

#### Added

- **`EmbeddingSettings` (`pydantic`)** в `lib/core/project_settings.py` —
  новая модель в `GatewaySettings.vector.embedding`. Поля: `base_url`,
  `model`, `dimension`, `http_timeout_sec`, **`auth_token`** (bearer-токен
  для `Authorization: Bearer <token>`, для эмбеддеров за reverse proxy).
  Рекомендуемый способ задания `auth_token` — через переменную окружения:
  `"auth_token": "${EMBED_TOKEN}"` + `EMBED_TOKEN=...` в `.secrets.env`.
- **`VectorInfrastructureSettings`** — новая модель для объединённой
  секции `gateway.vector.{embedding,index}`. Канонический путь для
  всей vector-инфраструктуры.
- **`register_embedding_config()`** (`lib/core/skill_registration.py`) —
  читает `gateway.vector.embedding` и кладёт в
  `TableRegistry.set_embedding_config(...)`. Вызывается один раз
  из `ApplicationContext._register_infra_resources()`.
- **`SkillsSettings._validate_skill_sections`** (`@model_validator(mode="before")`)
  — реально валидирует каждую `skills.<name>` через `SkillSettings` с
  `extra="forbid"`. Без этого pydantic не спускался бы в типизированные
  секции (`SkillsSettings` имеет `extra="allow"` для forward-compat
  по именам skill'ов).

#### Changed

- **`SkillSettings`** (`lib/core/project_settings.py`) — `extra="forbid"`,
  удалены секции `embedding` и `cache`. Остались `enabled`, `tables`,
  `vector_indexes`, `cli`, `llm`. Это явная граница: skill описывает
  только domain binding, не shared infrastructure.
- **`cache_provider_impl.get_embedding()`** — добавлен `Authorization:
  Bearer <auth_token>` если `auth_token` задан в
  `gateway.vector.embedding`. Поддержка Ollama / open-webui / LiteLLM /
  клаудных провайдеров, выставленных за reverse proxy с авторизацией.
- **`cache_provider_impl.read_embedding_config()`** — без аргумента;
  источник — `SETTINGS['gateway']['vector']['embedding']`. Параметр
  `cfg` удалён.
- **`cache_provider_impl.build_cache_provider()`** — больше не читает
  `cfg["cache"]`; `cache_path` всегда из `table_registry.snapshot_path()`.
- **`skill_config.get_embedding_config()` / `get_embedding_model()`** —
  теперь skill-независимые, читают из `table_registry.embedding_config()`
  (общий runtime-конфиг).
- **`lib/core/skill_config.py`** — добавлена `get_in_memory_cache_path()`,
  удалены `get_in_memory_config(skill_name, skill_root)` и
  `is_in_memory_enabled()` (были мёртвыми: `enabled` нигде не
  проверялся, `engine` нигде не использовался, `max_age_sec` /
  `refresh_interval_sec` не пробрасывались в `PostgresDuckDbProvider`).
- **`project.json`** — `skills.audit_analyzer.embedding` и
  `skills.audit_analyzer.cache` удалены. Добавлен `gateway.vector.embedding`
  (с подсказкой про `${EMBED_TOKEN}` в комментарии).
- **`lib/core/infra_registration.py`** — `INFRA_KEY_VECTOR_STORAGE`
  переименован с `"vector_index.storage"` на `"vector.storage"`.
  Источник: `gateway.vector.index.storage_table`.

#### Removed

- **`skills.<name>.embedding`** (секция в `project.json`) — embedding —
  общая runtime-инфраструктура, не свойство домена skill'а.
- **`skills.<name>.cache`** (секция в `project.json`) — все поля были
  мёртвыми; DuckDB snapshot — общий `table_registry.snapshot_path()`.
- **`skills.<name>.vector_indexes[].source`** — поле `source` удалено
  из `VectorIndexEntry`. Source-таблица (PG-таблица исходных строк)
  хранится в `public.agent_vector_index_config` (runtime-БД).
- **`gateway.vector_index.*`** — устаревший путь удалён из
  `GatewaySettings` (поле `vector_index`) и из `project.json`.
  Единственный канонический путь — `gateway.vector.index.*`.
  Обратной совместимости нет (fail-fast через runtime).
- **`skill_config.get_in_memory_config(skill_name, skill_root)`** и
  **`is_in_memory_enabled(skill_name)`** — заменены на
  `get_in_memory_cache_path(skill_root)`.

#### Migration notes

- `project.json::gateway.vector_index.*` → `gateway.vector.index.*`
  (переименование секции). Если у вас внешние скрипты/документация,
  ссылающиеся на `gateway.vector_index`, обновите их.
- `project.json::skills.<name>.embedding` → `gateway.vector.embedding`.
- `project.json::skills.<name>.cache` — удалено. Если вы полагались
  на `enabled`/`engine`, замените на `table_registry.snapshot_path()`
  (путь — runtime-константа).
- `skills.<name>.vector_indexes[].source` — поле больше не нужно.
  Source-таблица хранится в `public.agent_vector_index_config`.
- `skill_config.get_in_memory_config(name, root)` →
  `skill_config.get_in_memory_cache_path(root)`.
- `skill_config.get_embedding_config(name)` / `get_embedding_model(name)`
  — убран параметр `skill_name` (embedding — общий runtime).
- `skill_config.is_in_memory_enabled(name)` — удалено.
- `lib.core.infra_registration.INFRA_KEY_VECTOR_STORAGE` —
  `"vector_index.storage"` → `"vector.storage"`.

#### Fixed (configuration contract hardening)

По следам review-анализа текущего состояния `project.json` /
`lib/core/project_settings.py`. Ужесточение конфигурационного контракта —
без расширения поверхности API.

- **`VectorIndexEntry` теперь `extra="forbid"`.** Раньше старый
  `source` (и любые другие legacy-поля) проходили через pydantic как
  extra-ключи, что подрывало рефакторинг «source перенесён в
  runtime-БД». Теперь legacy `vector_indexes[].source` падает на
  старте gateway с `ConfigurationError`, а не проходит молча.
- **`TableEntry.type` теперь `Literal["table", "vector"]`.** Раньше
  принимался любой `str` (включая `"banana"`), что противоречило
  документации. Теперь `type="banana"` падает с `ValidationError`.
- **Legacy `gateway.vector_index.*` теперь fail-fast на validation.**
  Раньше `_StrictOptional(extra="allow")` пропускал legacy-секцию
  как extra-поле, и `register_vector_storage` молча её игнорировал
  (юзер получал «всё стартануло, но DuckDB-кеш пустой»). Теперь
  `GatewaySettings._reject_legacy_renamed_sections` поднимает
  `ConfigurationError` с явным hint на новый путь
  `gateway.vector.index.*`. Реестр legacy-ключей — `_LEGACY_GATEWAY_KEYS`
  в `lib/core/project_settings.py`; добавлять при следующих rename'ах.
- **Удалён мёртвый `ProjectSettings.version` (top-level).** Раньше
  модель принимала `version` на верхнем уровне, но никто не читал
  (реальный источник — `project.json::project.version` через
  `lib.utils.project_version`). Введён `ProjectMetadataSettings`:
  `project.json::project.*` теперь канонический namespace для
  project metadata. `ProjectMetadataSettings(extra="forbid")` —
  неизвестные ключи в `project.*` тоже падают.

#### Added (follow-up: auth_token by default)

- **`project.json::gateway.vector.embedding.auth_token`** теперь задан
  как `"${EMBED_TOKEN}"` по умолчанию. Если в `.secrets.env` есть
  `EMBED_TOKEN` — подставится в `Authorization: Bearer <token>`.
- **`cache_provider_impl.get_embedding()`** — guard от неразрешённого
  `${VAR}`-плейсхолдера: если `auth_token` после `.strip()` начинается
  с `${`, трактуется как «без авторизации». Без этого локальный Ollama
  без reverse proxy получал бы `Authorization: Bearer ${EMBED_TOKEN}`
  и падал с 401.
- **`.secrets.env.example`** — `EMBED_TOKEN=YOUR_EMBED_TOKEN`
  раскомментирован как шаблон. Пустое значение безопасно для локального
  Ollama (см. выше guard).
- **`tests/test_get_embedding_auth.py`** — 6 тестов на ветки
  `base_url`/`auth_token`/`${placeholder}`/empty/whitespace.

## [2.4.0] — 2026-08-20

> **MINOR-релиз:** метрика занятости контекстного окна (`metadata.context_window`),
> ручное сжатие контекста (`/compact` + tool), поддержка кастомных tool'ов из
> `workspace/tools/` (включая audit-tool'ы), мульти-машинный пул воркеров в
> `PostgresChannel` с режимами аренды `single` (по умолчанию) и `worker_pool`,
> терминальная наблюдаемость (`[task-worker]`/`[db-worker]`, токены LLM,
> `probe_connections`), кастомизация шаблонов nanobot через `workspace/overrides/`,
> закрытие потери данных при усечении больших результатов инструментов и оптимизации
> БД-пула (теги db-job'ов, гейт reclaim, idle-guard, кеш чтения сессий). Итог тестов:
> **1137 passed, 14 skipped**.

### Added

- **Метрика занятости контекстного окна (`metadata.context_window`)** —
  блок `{used, limit, pct (4 знака, clamp 0..1), model}` в metadata
  финального outbound (S1) + живое обновление processing-строки (T2) +
  рендер прогресс-бара в Streamlit и однострочной метки в CLI (M1).
  Канал `postgres_channel` пишет блок в `agent_conversation_messages`
  JSONB через `_flush_live_context` (мост per-iteration usage
  `lib/hooks/database_logging_hook._CONTEXT_BRIDGE`); патч
  `RuntimePatcher.patch_context_bridge_seed` сеет лимит/модель на
  старте оборота; `_attach_context_window` в `_wrap` `_assemble_outbound`
  собирает блок из usage последней итерации ÷ лимит окна. Управление
  UI: `cli.show_context_window` в `project.json` (bool, дефолт `true`).
  Тесты: `tests/test_database_logging_bridge.py`,
  `tests/test_runtime_patcher.py::TestPatchContextBridgeSeed`,
  `tests/test_postgres_channel.py::TestPostgresChannelContextWindow`,
  `tests/test_streamlit_app.py::TestRenderContextWindow`,
  `tests/test_console_loop.py::TestPrintContextWindow`.

- **Ручное сжатие контекста** — `ContextCompactionService`
  (`lib/services/context_compaction.py`) — единая точка записи факта
  сжатия. Четыре ручных/авто-входа: настоящая slash-команда `/compact`
  (`lib/commands/compact_command.py`, регистрация через
  `RuntimePatcher.patch_compact_command` в `CommandRouter` — детерминированно
  **до** LLM на любом канале: postgres, streamlit, telegram), CLI-команда
  `/compact` (`lib/cli/console_loop.py`), tool `compact_context`
  (`workspace/tools/compact_context.py`; старый `lib/tools/compact_context_tool.py`
  и `patch_compact_tool` удалены, регистрация через `patch_project_tools`)
  и авто-сжатие nanobot (обёртки `patch_compaction_tracking`). Обёртка над
  штатным `Consolidator.maybe_consolidate_by_tokens` / `compact_idle_session`
  nanobot 0.3.0: замеряет `tokens_before`/`tokens_after` (при падении нативного
  `estimate_session_prompt_tokens` — `_estimate_fallback` по символам),
  `archived_msgs` и возвращает отчёт. Ручные пути ставят `force=True`
  (жёсткое сжатие независимо от порога токенов — явная команда пользователя);
  явный `force=False` возвращает в token-budget режим. При `archived > 0`
  пишется заметка (`metadata.kind="context_compact"`, `role='assistant'`,
  `status='completed'`) в `agent_conversation_messages` — видна в Streamlit
  как стиль `.compact-notice`, но НЕ попадает в контекст промпта (контекст
  строится из `PGSessionManager`). Управляется секцией `gateway.compact.*`
  в `project.json` (`enabled`, `notify_in_history`, `print_to_terminal`;
  все опциональны, дефолт `true`/`true`/`false`).
- **Переопределение системных шаблонов nanobot из `workspace/overrides/`** —
  `lib/services/consolidator_locale.py` на старте приложения
  (`ApplicationContext.start()`) подкладывает каталог `workspace/overrides/`
  в Jinja2-loader шаблонов nanobot (`ChoiceLoader` с приоритетом
  переопределений; `_environment()` кэшируется, поэтому правится тот же
  объект; идемпотентно, при отсутствии каталога — no-op). Файлы кладутся
  по имени шаблона, как в `render_template`, например
  `workspace/overrides/agent/consolidator_archive.md`. Сейчас переопределён
  `agent/consolidator_archive.md` — русскоязычная инструкция Consolidator
  (правило «пиши факты на языке диалога»), чтобы факты из русских диалогов
  извлекались на русском. Тесты: `tests/test_consolidator_locale.py`.

- **Заметки о сжатии в истории диалога для всех путей** — патч
  `runtime_patcher.patch_compaction_tracking` оборачивает
  `AutoCompact._archive` (idle) и `Consolidator.maybe_consolidate_by_tokens`
  (token-budget). После каждого успешного авто-сжатия пишется
  заметка в `agent_conversation_messages` через общий метод
  `ContextCompactionService.record_external_compaction`, который
  сводит замеры и зовёт тот же `_notify` + `_write_history_notice`,
  что и ручной `compact()`. Один путь, один формат, один и тот же
  текст `format_report` — для пользователя и для логов ручное и
  автоматическое сжатие неразличимы.
- **Полное логирование промпта и ответа LLM** — событие `llm_call`
  в `agent_gateway_logs`: `DbLoggingService.log_llm_call` (payload
  `prompt`/`response`, метаданные `iteration`/`model`/`finish_reason`/`usage`)
  и `DatabaseLoggingHook.after_iteration`, которое на каждую итерацию
  пишет полные `messages` и `LLMResponse` (через `_json_safe` — несеризуемые
  объекты сводятся к строке, батч не теряется).
- **Токены LLM в терминале** — при `print_llm_calls=True`
  `DatabaseLoggingHook` выводит на каждую итерацию две строки:
  `→ LLM: отправлен промпт (X токенов)` и `← LLM: получен ответ (Y токенов)`
  из `usage.prompt_tokens` / `usage.completion_tokens`. В CLI включается в
  `cli_agent.py`; в gateway — отключаемой опцией `gateway.print_llm_calls`
  (`project.json`, `false` по умолчанию).
- **Активность пула воркеров в терминале gateway** — отключаемая опция
  `gateway.print_worker_activity` (`project.json`, `false` по умолчанию).
  `PostgresChannel` выводит через Rich-консоль: `→ worker <id> взял задачу
  <task_id> (chat ...)`, `← worker <id> закончил задачу ... [completed]`
  (а также `[error]`/`[failed]`, `[streamed/completed]`) и строку размера
  очереди `очередь: pending=N, error=M (итого K)` (печатается при изменении).
  Флаг пробрасывается из `gateway.py` через `ChannelFactory(print_worker_activity=...)`
  в конфиг канала.

- **Live e2e пула воркеров: реальный `gateway.py` + живой LLM**
  (`tests/integration/test_worker_pool_real_bot.py`, opt-in через
  `NANOBOT_LIVE_E2E=1`). Три сценария: (1) один gateway обрабатывает
  user-сообщение через реальный AgentLoop + LLM, проверяется `completed`
  + снятие claim; (2) два gateway-процесса с разными auto-`worker_id`
  делят N задач в одной БД, проверяются distinct worker_id в логах и
  отсутствие потерь; (3) `kill -9` во время обработки оставляет
  orphan-claim, имитация истечения lease + запуск
  `tools/check_worker_pool_integrity.py --fix` возвращает задачу в
  `pending` и снимает claim. Тесты используют изолированные `chat_id`
  (`e2e_pool_<rand>`) и чистят свои данные в teardown.

- **Закрыта потеря данных при усечении больших результатов инструментов.**
  Раньше вывод exec/shell резался до 50K символов с маркером
  `... (N chars truncated) ...` (`nanobot/agent/tools/shell.py`)
  и середина пропадала безвозвратно; история сессии (`_save_turn`) усекала
  строковые результаты до 16K символов. Теперь (все уровни через
  `RuntimePatcher`): `patch_exec_limits` поднимает потолки вывода exec
  и `maximum` в JSON-Schema параметров `max_output_chars`/`max_output_tokens`;
  `patch_save_turn` пишет большой `role=="tool"` результат **полным** файлом
  в `data_store` через `SessionFileStore` (ссылка
  `[Result saved to data_store/<path> (<size> KB)]` в истории вместо
  усечённого текста); `patch_tool_limits` поднимает потолки
  `read_file`/`grep`/`list_dir`. `SessionFileStore.save` получил параметр
  `dedupe=True` (sha1) — повторные обороты не плодят копии файлов.
  Конфигурация — `gateway.tool_result_limits` в `project.json`
  (все ключи опциональны, дефолты в коде). Каждый патч с fallback:
  при изменении API nanobot — причина в `PatchReport`, процесс не падает.

- **Мульти-машинный пул воркеров в `PostgresChannel` (таблица
  `agent_worker_claims`).** Устраняет двойной захват одной задачи в
  Greenplum 6.5: эксклюзивность аренды гарантирует **UNIQUE PK `(task_id)`
  `INSERT ... RETURNING`**, а не MVCC-перепроверку `UPDATE ... WHERE
  status='pending'`. Каждая задача защищена lease (срок = `processing_timeout`),
  heartbeat обновляет `lease_until`; reclaim возвращает задачи «мёртвого»
  воркера в пул. Разведены статусы `error` (повторяемая ошибка, повтор после
  `error_retry_delay`) и `failed` (терминальный, не повторяется) — раньше оба
  сводились к `failed`. `stop()` освобождает аренды. Владелец задачи
  определяется только по `agent_worker_claims.worker_id` — колонка в
  `agent_conversation_messages` не требуется. Новые ключи
  `channels.postgres.{worker_id, claims_table, lease_interval,
  error_retry_delay}`; `streamlit.error_window_sec` (быв. `failed_window_sec`,
  теперь окно повтора `error`-задач). Диагностика:
  `tools/check_worker_pool_integrity.py --fix`. DDL:
  `sql/workers/create_public_agent_worker_claims.sql`.
  Гейт-тесты: `tests/integration/test_worker_pool_concurrency.py` (C1–C5,
  opt-in `NANOBOT_INTEGRATION=1`) — 5 зелёных против реального PostgreSQL.

- **Подробное поэтапное логирование и доработка инкрементальности
  `tools/build_vectors.py`.** Все сообщения — через `loguru` в stderr (без
  ANSI-цветов): этапы пометкой `[index]` (конфиг → состояние БД/источника →
  классификация новых/изменённых/удалённых → удаление → чанки → эмбеддинг с
  прогрессом → пересборка FAISS → итог), ошибки любого этапа — с traceback,
  сбой одного индекса больше не роняет прогон. Новый флаг `--verbose` (уровень
  DEBUG). Исправлен критичный детект CHANGED/DELETED: `pk_value` сравнивается
  как строка (`TEXT` в БД vs числовой PK источника, `_norm_pk`), раньше каждый
  старт переписывал индекс целиком. CHANGED-строки: вставляются новые чанки до
  удаления старых (`DELETE ... content_hash <> <new>`) — при сбое эмбеддинга
  старый вектор сохраняется. `--check` переведён на `COUNT(DISTINCT pk_value)`
  (чанкование не ломает быструю проверку).

- **Повтор эмбеддинга при ошибке в `build_vectors.py`.** При неудачном
  получении вектора скрипт ждёт регулируемое время (флаг `--embedding-retry-wait`,
  default **5** с) и повторяет запрос ещё раз; при повторной неудаче — ошибка
  фиксируется, прогон продолжается. `get_embedding` не изменялся.

- **Поддержка кастомных tool'ов из `workspace/tools/*.py`** — патч
  `RuntimePatcher.patch_project_tools` (см. `lib/services/runtime_patcher.py`)
  сканирует `workspace/tools/` тем же механизмом, что встроенный
  `ToolLoader.discover` (`nanobot/agent/tools/loader.py`), собирает tool-классы
  (наследники `nanobot.agent.tools.base.Tool`, у которых
  `__module__` начинается с `workspace.tools.`) и регистрирует их в
  `agent.tools` через `Tool.create(ctx)` + `enabled(ctx)`. Конфиг per-tool
  в `config.json`/`project.json` через стандартные `config_key` + pydantic
  `config_cls` (конвенции nanobot, без своего базового класса). `ToolContext`
  собирается из полей `AgentLoop` тем же способом, что
  `AgentLoop._register_default_tools` (`loop.py:597-630`); `agent` и
  `settings` дополнительно пробрасываются через `setattr` как
  `ctx._agent_ref` / `ctx._settings_ref` (в вашей версии nanobot
  `ToolContext.__init__` не принимает `metadata`). Шаблон:
  `workspace/tools/example.py`. Тесты:
  `tests/test_tools_project_loader.py`.

- **Tool'ы `audit_run_predefined_script` и `audit_search_vector`** — нативный
  дубль skill'а `audit_analyzer`. По конвенции nanobot (один tool = одно
  действие, см. `_FsTool` в `nanobot/agent/tools/filesystem.py`) разделены
  на два tool-класса с общим приватным базовым `_AuditToolBase`:

    * `AuditRunPredefinedScriptTool` (`audit_run_predefined_script`) —
      выполнить готовый SQL-скрипт из реестра
      `public.agent_predefined_scripts`. Параметры: `script` (обязательно),
      `params` (опционально).
    * `AuditSearchVectorTool` (`audit_search_vector`) — семантический поиск
      по FAISS-индексу. Параметры: `query` (обязательно), `index_name`,
      `top_k`, `threshold`.

  Оба наследуют логику skill'а (тот же DuckDB-кэш, тот же реестр скриптов,
  тот же `CacheProvider.search_vector`) через
  `importlib.util.spec_from_file_location`. Skill остаётся работоспособным
  для CLI/sql-режима (LLM-генерация SELECT не переносится). Конфиг в
  `project.json` → `gateway.audit_predefined.*` (`enable`,
  `max_result_chars`) и `gateway.audit_vector.*` (`enable`,
  `default_top_k`, `default_index_name`, `max_result_chars`). Реализация:
  `workspace/tools/audit_analyzer_tool.py`. Тесты:
  `tests/test_tools_audit_analyzer.py` (32 теста, включая общую
  базу `_AuditToolBase` и изоляцию между двумя tool'ами).

- **`runtime_context_provider` для `audit_run_predefined_script`** —
  встроенный механизм nanobot (см. `nanobot/runtime_context.py:47-49`),
  через который `AgentLoop` (`loop.py:744-752`) добавляет в system prompt
  список доступных предопределённых скриптов **до** любого вызова tool'а.
  Это избавляет LLM от необходимости угадывать имена скриптов и не
  требует отдельного tool'а `audit_list_predefined_scripts` (который бы
  добавлял лишний round-trip). Реализация: `_PredefinedScriptsProvider`
  в `workspace/tools/audit_analyzer_tool.py`. Список скриптов
  загружается через `predefined.list_all_scripts()` (skill'овский
  реестр) и кешируется на уровне класса; сбросить можно через
  `tool.invalidate_scripts_cache()`. Тесты:
`tests/test_tools_audit_analyzer.py::TestPredefinedScriptsProvider`
   (9 тестов: форматирование, кеш, обработка ошибок, корректный
   `RuntimeContextBlock`).

- **Версия проекта в стартовом баннере gateway.** `gateway.py` выводит
  `project.version` из `project.json` (канонический источник версии, без
  префикса `v`; читается через `lib/utils/project_version.py`). Раньше
  баннер был статичным и расходился с фактической версией репозитория.
- **Активность db-worker пула соединений в терминале gateway.** Флаг
  `gateway.print_db_activity` (`project.json`, `false` по умолчанию) включает
  вывод через Rich: `→ db-worker <N> [<тег вызывающего>] взял job
  [очередь-БД] M`, `← db-worker <N> закончил job ... [ok]`. Флаг
  пробрасывается из `ApplicationContext` в конфиг пула
  `workspace/utils/db.py` как ключ `print_activity` (`set_pool_config`).
- **`probe_connections` — прогрев пула соединений при старте gateway.**
  `workspace/utils/db.py::probe_connections(count, timeout)` принудительно
  поднимает `count` соединений (по умолчанию `min_conn`) и фактически
  проверяет доступность БД, не бросая исключение при недоступности, а
  собирая статус по каждому соединению. `gateway.py` на старте зовёт
  `probe_connections()` — если БД недоступна, воркеры/каналы не стартуют
  вслепую. Тесты: `tests/test_utils_db.py::TestPool`.
- **Гейт Streamlit `streamlit.enabled`.** `false` полностью выключает
  запуск Streamlit-subprocess и стриминговый endpoint (UI на :8501 не
  стартует), а не только скрывает его из меню. Дефолт в `project.json` и в
  `REQUIRED_KEYS` (`tests/test_config_keys.py`) синхронизирован.
- **Метки-теги каждого db-job'а в пуле (`Job.tag` / `_caller_tag`).**
  Публичные функции `db.execute/fetch/fetchone/fetchval` (sync/async),
  `db.run`, транзакции begin/end и `probe_connections` помечают свой job
  меткой `файл:строка` вызывающей стороны (`_caller_tag(frames_back=2)`);
  прокси транзакций (`_ConnectionProxy._run`) — цепочкой внешних фреймов
  `файл:строка <- файл:строка …` (для поиска корня цикла). Метка печатается
  в активности `[db-worker]` и в loguru-строке воркера — по ней видно,
  какой модуль генерирует запрос (например, постоянный поток
  `nanobot/agent/loop.py` → `list_sessions`). Теги не меняют публичный API:
  необязательный аргумент `_tag`/точное позиционное поведение сохранены.
- **Быстрый гейт `_reclaim_needed` + перенос reclaim из горячего пути.**
  Тяжёлый `_reclaim_and_heal` (одна транзакция из 4 UPDATE/DELETE) больше
  НЕ выполняется в `poll_inbound` (читается каждые `poll_interval`) — он
  вынесен в фоновый `_lease_loop` по таймеру `lease_interval`, став
  единственным источником reclaim/heal. Перед запуском `_lease_loop`
  проверяет `_reclaim_needed` (есть ли хоть одна `processing`-строка или
  хоть один claim): на пустом столе транзакция пропускается целиком
  (остаётся один лёгкий `SELECT ... EXISTS` на тик). Снижает нагрузку на БД
  при простое и задержке поллинга. Тесты:
  `tests/test_postgres_channel.py`.
- **Заглушка бесполезного перечисления сессий при выключенном idle-компакте
  (`RuntimePatcher.patch_auto_compact_idle_guard`).** `AgentLoop.run` при
  отсутствии входящих раз в секунду зовёт `AutoCompact.check_expired()`
  (`nanobot/agent/loop.py:1034`), а тот даже при `idleCompactAfterMinutes=0`
  делает `sessions.list_sessions()` — дорогой N+1 (перечисление всех сессий
  + отдельный запрос превью каждой), сотни запросов в секунду вхолостую.
  Патч при `auto_compact._ttl <= 0` заменяет `check_expired` на no-op
  (нагрузка практически обнуляется, остаётся легитимный поллинг каналов);
  при `ttl > 0` патч пропускается, token-budget сжатие не затронуто.
  Тесты: `tests/test_runtime_patcher.py::TestAutoCompactIdleGuard`.
- **Оптимизация чтения сессий (`PGSessionManager`).** (1) `_load` теперь
  выполняет транзакционное чтение (meta + messages) целиком ОДНИМ `run`-job'ом
  пула на сыром psycopg-соединении вместо ~15 обращений через прокси-курсор
  (execute/description/fetchone/fetchall + begin/commit) — уходят «пачки»
  строк в логе `[db-worker]`; (2) `read_session_file` для активной сессии
  возвращает payload из in-memory кэша (как `get_or_create`), не читая БД
  повторно — повторные вызовы web/REST не порождают лишних обращений;
  при промахе грузит из БД и кладёт в кэш; несуществующая сессия → `None`.
  Ошибка БД пробрасывается (без JSONL-отката). Тесты:
  `tests/test_pg_session_manager.py`.

- **Переключатель режима аренды задач `channels.postgres.claim_strategy`**
  (`"single"` (дефолт) | `"worker_pool"`). Возвращает поведение
  одиночного инстанса из v2.3.1 — захват задачи через `UPDATE ... RETURNING`
  без таблицы `agent_worker_claims` — как опциональную настройку
  (по умолчанию), в дополнение к существующему мульти-машинному пулу
  воркеров (`worker_pool` с `INSERT INTO claims` + lease/heartbeat).
  Single-режим использует `_claim_one_single` (один SQL через `fetchone`)
  и `_unstick_processing` в фоновой задаче вместо lease-loop. Физически
  0 INSERT/SELECT/UPDATE/DELETE к `agent_worker_claims` в hot-path. Тесты:
  `tests/test_parallel_modes.py` (12), `tests/test_single_mode_audit.py`
  (14 — runtime-перехват SQL), `tests/test_postgres_channel_static_audit.py`
  (8 — статический AST-аудит гардов).

- **Фоновый unstick `processing`-сообщений в single-режиме**
  (`channels.postgres.unstick_interval`, дефолт `max(60, processing_timeout/5)
  = 120 сек`). Раньше `_unstick_processing` запускался на каждом poll
  (каждые 10 сек — 5-6 лишних SQL при пустой таблице). Теперь фоновая
  задача с интервалом `unstick_interval`. Снижает нагрузку на БД
  в ~5 раз на пустом столе.

### Fixed

- **`track_column_overrides` не работал в рантайме**: `AuditSyncService._track_column_for`
  импортировал несуществующую функцию `skill_for_table` из `table_registry`
  (это метод синглтона); ImportError глотался, и поллинг всегда падал в
  fallback `updated_at`/`id`. Теперь lookup идёт через
  `table_registry.skill_for_table(table)` — per-table track-колонки из
  регистрации skill'а применяются. Регресс-тесты: registry-path и
  disabled-skill fallback (`tests/test_audit_sync_service.py`).

- **Дубликат таблицы при разных формах записи**: `register.py` skill'а
  `audit_analyzer` не нормализовал `db_additional_tables`
  (`[["public", "agent_predefined_scripts"]]`) перед проверкой
  «уже есть в списке», поэтому таблица попадала в sync дважды
  (вложенной формой и строкой). Теперь нормализация
  `normalize_table_names` выполняется до дедупликации; плюс защитная
  дедупликация списка таблиц в `_make_sync_services` (сохранение порядка)
  и нормализация в `tools/build_vectors.py`.

- **Изоляция тестов канала**: `TestChannelFactoryClaimStrategy._setup`
  (`tests/test_parallel_modes.py`) оставлял фейковые модули в
  `sys.modules["lib.channels.postgres_channel"]` → 55 ошибок ImportError
  в `test_postgres_channel.py` при полном прогоне; после наивной очистки
  тесты канала, наоборот, уходили связками в живой PostgreSQL.
  Фикс двусторонний: autouse-фикстура восстановления sys.modules в
  `test_parallel_modes.py` + форс-реимпорт канала под фейковым
  `utils.db` в фикстуре `mock_db_and_psycopg`
  (`tests/test_postgres_channel.py`). Полный прогон без integration:
  1411 passed, 14 skipped.

- **Устаревшие ожидания** `gateway.print_worker_activity` /
  `gateway.print_db_activity` в `tests/test_config_keys.py`: ожидался
  дефолт `True`, тогда как в `project.json` и по документации — `false`.

- **Сохранение сессии падало с `A string literal cannot contain NUL (0x00)`**
  когда в контент сообщения (бинарь из `exec`/`read_file` или LLM-вывод)
  попадал NUL-байт при записи в PostgreSQL. Введён канонический
  `workspace/utils/clean_text.py` (убирает NUL и литеральные `\u0000`..`\u0003`),
  который применяется на двух уровнях: патч `RuntimePatcher.patch_session_content_cleanup`
  чистит контент на источнике через `Session.add_message`, а
  `utils.db._sanitize_param` — страховка на границе БД для всех параметров
  `execute`/`mogrify` (в т.ч. `execute_values`). Раньше `_sanitize_param`
  наоборот *превращал* escape `\u0000` в настоящий NUL, что и порождало ошибку.
  Документация: раздел «Санитизация NUL-байта» в `docs/DATABASE.md`.

- **Ответ «терялся» (статус `failed`), когда агент завершал оборот
  инструментом `message(...)` без последующего plain-text.** Тул публикует
  свой outbound через шину **промежуточно** — в момент исполнения, до конца
  оборота, а `PostgresChannel.send()` трактовал любое сообщение как финал:
  снимал `_msg_ctx` (`pop`), освобождал слот и удалял claim, помечал `completed`,
  а `_release_slot` снимал задачу с heartbeat ещё ДО записи в БД. Оборот при
  этом не завершён → другой воркер мог reclaim-нуть задачу и довести до
  `failed`; финальный `_assemble_outbound` при подавлении (`_sent_in_turn` +
  «пустой финал») вообще не публиковался, и корректной финализации не было.
  Теперь: патч `RuntimePatcher.patch_assemble_outbound` ставит на финальный
  outbound маркер `metadata["_final_turn"]` (а при подавленном финале — шлёт
  синтетический outbound с этим маркером, чтобы канал закрыл оборот).
  `PostgresChannel.send()` финализирует (completed + claim + слот + `_msg_ctx`)
  **только** на этом маркере (или на legacy `_turn_end`/`latency_ms`),
  а промежуточные публикации `message(...)` merge'ит в assistant-строку
  (накопление `content` + media без дублей), не трогая слот/claim/аренду.
  `_release_slot` в финализации перенесён ПОСЛЕ успешной записи (и на ошибке
  через `_mark_failed`), закрывая гонку с reclaim на другом воркере.
  Метаданные `utils/outbound_meta.py` получили контрактный ключ `FINAL_TURN_KEY`.
  Тесты: `tests/test_postgres_channel.py`, `tests/test_runtime_patcher.py`.

- **`SessionFileRedirectHook` теперь перенаправляет и `media` тула
  `message`, а не только write-инструменты.** `MessageTool` в nanobot
  резолвит относительные пути относительно корня workspace
  (`workspace / path`), а файлы, созданные агентом, живут в
  `data_store/cache/sessions/<session_key>/`. Из-за этого прикрепление
  файла по относительному пути (как велит `workspace/AGENTS.md`) — или по
  «абсолютному» пути чужого workspace (`/home/<user>/<project>/workspace/
  <file>`) — не находило файл: `utils.media.serialize` писал `Media file
  not found, keeping path`, и в БД уходил AW-dict с пустым
  `mime_type`/`file_size`. Раньше коррекция была только в auto-attach
  `RuntimePatcher._wrap`, который не срабатывает, когда агент сам вызвал
  `message()` (`MessageTool._sent_in_turn` → `_assemble_outbound` → `None`).
  Теперь `before_execute_tool` для тула `message` переписывает каждый
  media-элемент, который не существует в том виде, как его увидит
  `_resolve_media`: ищет реальный файл в текущей session-папке (по
  относительному пути и по basename, включая `attachments/` и `results/`)
  и подставляет его. URL/`data:`-схемы и существующие пути не трогаются.
  Тесты: `tests/test_session_file_redirect_hook.py` (10, включая 2 e2e через
  реальный `MessageTool._resolve_media`) + live e2e
  `tests/test_gateway_live_media_e2e.py` (реальный gateway + живой Postgres
  + живой LLM на изолированной таблице; опт-ин через `NANOBOT_LIVE_E2E=1`).

- **Reclaim-запрос неверно собирался на PostgreSQL без явного каста типа.**
  `PostgresChannel._release_all_leases` строил `task_id = ANY(%s)` без
  приведения к `uuid[]`; на некоторых БД (psycopg2/greenplum) параметр
  интерпретировался как `text[]`, и очистка чужих истёкших lease не
  срабатывала. Каст явно указан: `ANY(%s::uuid[])`.

- **`ContextCompactionService._write_history_notice` вызывал sync-функцию
  через `await`.** `utils.db.execute` возвращает command tag, а не корутину —
  `await execute(...)` падал на `'str' object can't be awaited`. Теперь вызов
  обёрнут в `asyncio.to_thread` (как sync-IO в `postgres_channel`).

- **`AuditRunPredefinedScriptTool` мог вернуть «Cache is not ready» на
  пустом кэше.** Провайдер собирался с закрытым DuckDB-кэшем; перед чтением
  реестра теперь вызывается `provider.open_cache()`, а провайдер дополнительно
  инжектируется и в «плоский» `sys.modules["db_loader"]` (отдельный инстанс
  модуля внутри `predefined.py`), иначе `get_provider()` внутри
  `load_registry()` бросал «провайдер не задан».

### Tests

- Итоговое состояние набора: **1137 passed, 14 skipped** (`pytest`).
  К покрытию релиза добавлены: `tests/test_tools_project_loader.py`,
  `tests/test_tools_audit_analyzer.py`, `tests/test_consolidator_locale.py`,
  `tests/test_recent_files_hook.py`, `tests/test_runtime_patcher.py`
  (`TestAutoCompactIdleGuard`), `tests/test_postgres_channel.py`
  (`_reclaim_needed`), `tests/test_pg_session_manager.py` (кеш
  `read_session_file`), `tests/test_utils_db.py` (теги db-job'ов,
  `probe_connections`) и интеграционные
  `tests/integration/test_worker_pool_concurrency.py`,
  `tests/integration/test_worker_pool_real_bot.py`.

## [2.3.1] — 2026-08-18

> **PATCH-релиз:** закрытые системные баги медиа-вложений (auto-attach устаревших
> путей, авто-подключение `SessionFileRedirectHook` в gateway) и перенос фреймворковых
> хуков в `lib/hooks/` (один `AgentLoop`); новый skill `office_files` — решение
> проблемы чтения офисных файлов (docx/xlsx/xls/pdf/pptx/csv/txt). Итог тестов:
> **906 passed**.

### Added

- **`workspace/hooks/recent_files_hook.py` — `RecentFilesHook` + auto-attach
  в `OutboundMessage.media`.** Закрывает два системных бага:
  (1) агент создаёт файл через `write_file`, но **забывает** приложить
  его в `message({"media": [...]})` — в БД уходит пустой `media` и в
  таблице нет вложения;
  (2) агент прикладывает несуществующий путь (например, `.docx` после
  блокировки `pip install` SSRF-guard'ом) — `media.py:serialize` пишет
  warning `Media file not found, keeping path`, а в БД уходит dict с
  пустым `mime_type`/`file_size`, и UI его не отображает. Хук в
  `after_execute_tool` собирает `params["path"]` (уже перенаправленный
  `SessionFileRedirectHook`, поэтому путь **реальный**), а в
  `RuntimePatcher._wrap` после `tool_audit_hook.drain` мы дренируем
  `recent_files_hook.drain(session_key)` и подмешиваем в `result.media`
  только то, чего там ещё нет (по `Path(p).name`) и что существует на
  диске (`Path(p).is_file()`). Сессионная изоляция по `session_key` —
  конкурентные вопросы не путают файлы. Хук auto-discover'ится тем же
  `ApplicationContext.scan_and_register`, что и `SessionFileRedirectHook`;
  порядок в `AgentLoop.hooks`: `RecentFilesHook` → `SessionFileRedirectHook`
  → `ToolAuditHook` (чтобы `params["path"]` уже был перенаправлен к моменту
  `after_execute_tool` `RecentFilesHook`).
  `RuntimePatcher.apply_all` теперь принимает `recent_files_hook` как
  keyword-only параметр. Тесты: `tests/test_recent_files_hook.py` (13)
  + `tests/test_smoke_postgres_channel_media.py` (3 e2e).

- **`workspace/skills/office_files/` — решение проблемы чтения офисных
  файлов.** `workspace/utils/office_files.py` (`extract_text` / `extract_tables` /
  `summarize` / `read_xlsx_sheet`): маршрутизация по расширению через
  `mimetypes`, чтение `.docx`/`.xlsx`/`.xls`/`.pdf`/`.pptx`/`.csv`/`.txt`.
  Зависимости добавлены в `requirements.txt` (`python-docx`, `openpyxl`,
  `xlrd`, `pypdf`, `pdfplumber`, `python-pptx`, `Pillow`, `chardet`) — в
  контракте явно запрещён `pip install` на лету (SSRF-guard режет зеркало
  PyPI). Документация: `workspace/skills/office_files/SKILL.md`. Тесты:
  `tests/test_office_files.py` (196 строк).

### Fixed

- **`hook_loader.scan_and_register`: `importlib.import_module` →
  `importlib.util.spec_from_file_location`.** Раньше плагины
  `workspace/hooks/*.py` импортировались top-level по имени файла, и при
  запуске gateway (в `sys.path` только `workspace/`, без `workspace/hooks/`)
  падало `No module named 'session_file_redirect_hook'` / `'recent_files_hook'`.
  Теперь каждый файл загружается через `spec_from_file_location` под именем
  `hooks.<stem>` (кэшируется индексом в `sys.modules`) — не зависит от порядка
  добавления `workspace/` и `workspace/hooks/` в `sys.path`. Тесты:
  `test_cli_agent.py` (`test_finds_workspace_hooks_without_hooks_dir_in_syspath`,
  `test_finds_real_workspace_hooks`), `test_application_context.py` адаптирован.

- **auto-attach: устаревшие пути в `message(media=...)` после
  `SessionFileRedirectHook` теперь заменяются реальными.**
  Агент записывает файл через `write_file`, хук перенаправляет его в
  `data_store/cache/sessions/<key>/`, но модель в `message()` прикладывает
  исходный (до редиректа) путь — `utils.media.serialize` не находил файл
  (`Media file not found, keeping path`), и в БД уходил AW-dict с пустым
  `mime_type`/`file_size`. Раньше auto-attach (по basename) пропускал
  такой путь как «уже есть» в `result.media` и оставлял битую ссылку.
  Теперь `RuntimePatcher._wrap` заменяет первую несуществующую запись с
  тем же basename реальным перенаправленным путём из
  `recent_files_hook.drain(session_key)` (живые файлы не дублируются,
  отсутствующие вложения по-прежнему отбрасываются). Тесты:
  `tests/test_recent_files_hook.py` (новый
  `test_patcher_replaces_stale_redirected_path`).

- **gateway: `SessionFileRedirectHook` теперь подключается автоматически.**
  До фикса `lib.cli.hook_loader.scan_and_register` вызывался только в
  `cli_agent.py`, и в gateway-режиме `write`/`edit`/`create_file`/`write_file`
  шли в исходный путь (`/home/<user>/<project>/workspace/<file>` или
  `C:\Users\<user>\workspace\<file>`), минуя политику
  `data_store/cache/sessions/<session_key>/`. Симптом — `Media file not found,
  keeping path` в `utils.media.serialize` и потеря вложений в таблице сообщений
  (особенно на Linux с абсолютными NFS-путями, где `Path(p).is_file()`
  возвращал `False` к моменту сериализации). Теперь auto-scan выполняется в
  `ApplicationContext.create()` для всех точек входа (gateway, cli_agent,
  streamlit) до создания агента: плагины передаются в `AgentFactory.create(
  project_hooks=...)`, который ставит их первыми в `hooks` (правки
  `params["path"]` видны в `ToolAuditHook`) и создаёт `AgentLoop` один раз.
  `AgentFactory.create()` возвращает `(agent, hooks, hook_factories)`.
  `cli_agent.py` упрощён: убран дублирующий
  `scan_and_register` + `from_config(hooks=...)`. Тесты: `test_agent_factory.py`,
  `test_cli_agent.py`, `test_application_context.py` (новые регрессионные
  `test_auto_scan_hooks_includes_session_file_redirect` и
  `test_agent_created_once_with_merged_hooks`).

### Changed

- **Attack на корень Warning'ов: фреймворковые хуки переехали из
  `workspace/hooks/` в `lib/hooks/`.** `workspace/hooks/` — теперь только
  плагины с жёстким контрактом `cls(workspace_dir=...)`. `base_tool_tracking_hook.py`,
  `tool_audit_hook.py`, `database_logging_hook.py` (и `DatabaseLoggingHook`)
  перенесены в `lib/hooks/` и провязываются явно через `AgentFactory`/
  `ApplicationContext`. Это устранило сами причины gateway-warning'ов
  `__init__() got an unexpected keyword argument 'workspace_dir'` и
  `missing required positional argument: 'db_logging_service'`:
  `lib/cli/hook_loader.scan_and_register` больше не нуждается ни в маркере
  `_skip_auto_register`, ни в `inspect.signature`, ни в поиске
  `ToolAuditHook` — теперь он инстанцирует каждый найденный плагин
  единообразно. Заодно починен дубль `ToolAuditHook` в `AgentLoop.hooks`
  (сканированный инстанс + инстанс от `AgentFactory`). Ранее ломался
  `database_logging_hook.py` (эм-даш в разорванном docstring давал
  `SyntaxError`). Сигнатуры `scan_and_register` упрощены до возврата
  списка хуков. **`AgentLoop` теперь создаётся ровно один раз**: плагины
  сканируются ДО создания агента и передаются в `AgentFactory.create(
  project_hooks=...)`, который собирает `hooks = project_hooks +
  [ToolAuditHook]` и вызывает `from_config` однократно — убран двойной
  лог `Registered N tools` при старте (раньше агент строился дважды:
  в `AgentFactory` и в пересборке после auto-scan). Полный список
  подключённых хуков выводится ОДНОЙ строкой один раз после создания
  агента (`Hooks connected: RecentFilesHook, SessionFileRedirectHook,
  ToolAuditHook [+ N hook factory (per-turn)]`) — единая точка вывода,
  сканер успех молчит (раньше печатался только сканированные плагины,
  а фреймворковые хуки в лог не попадали). Обновлены импорты:
  `agent_factory.py`, `runtime_patcher.py`, `benchmarks/hooks.py`, тесты.
  Итог: **906 passed**.

## [2.3.0] — 2026-08-18

> **MINOR-релиз:** единая платформа медиа-вложений (кодек + `MessageExchange` +
> `SessionFileStore`), backfill-скрипт для миграции legacy-формата в AW, единый
> LLM-клиент в `lib.services.llm_client`, унификация служебных путей настроек
> (`lib.utils.node_access`), логирования (`lib.utils.logging_utils`) и фильтрации
> outbound, чистка тестов от заглушек. Итог тестов: **859 passed** (900 собранных
> − 42 удалённых).

### Added

- **`lib/channels/message_exchange.py` — общий `MessageExchange` для
  PostgresChannel / RedisChannel / Streamlit.** Раньше у каждого канала был
  свой кодинг inbound/outbound + локальный поллер, что вело к дрейфу поведения.
  Новый движок инкапсулирует: кодирование/декодирование сообщений (включая
  общий JSONB-кодек `lib/utils/media_jsonb.py`), поллинг и публикацию outbound.
  `PostgresChannel` и `RedisChannel` переведены на `MessageExchange`;
  `streamlit_app.py` использует тот же движок для чтения истории.
- **`lib/utils/media.py` — единый кодек media и `SessionFileStore`.**
  Раньше `_embed_media_for_db` / `_decode_media_from_db` жили в каждом канале
  отдельно, а вложения сессий сохранялись рядом с `pg_session_manager`.
  Теперь: (1) `media` принимает и dict AW-формата `{filename, file_id,
  mime_type, file_size}`, и старый dict `{filename, data}`, и строку
  `data:<mime>;base64,…`, и URL; (2) `SessionFileStore` — общий стор
  вложений под `data_store/cache/sessions/<key>/attachments/`, переиспользует
  `SessionFileRedirectHook` для определения папки; (3) `JSONB-декодер` для
  media вынесен в `lib/utils/media_jsonb.py` и тестируется отдельно.
- **`scripts/backfill_media_aw.py` — AW-миграция legacy-медиа в
  `agent_conversation_messages`.** Скрипт читает существующие строки,
  конвертирует старый dict-формат `{filename, data}` (data URL) в AW-формат
  `{filename, file_id, mime_type, file_size}`: payload сохраняется в
  `data_store/cache/sessions/_shared/attachments/`, в БД пишется только
  `file_id`. Идемпотентен: записи с уже проставленным `file_id` пропускаются,
  HTTP/HTTPS-ссылки не трогает. CLI: `python scripts/backfill_media_aw.py
  [--dry-run]`.
- **`lib/services/llm_client.py` — единая точка вызова LLM.** Вместо разрозненных
  `httpx`-вызовов в навыках и утилитах — один клиент с ретраями, таймаутами
  и общим логированием (через `loguru`). Параметры читаются из
  `config.require_setting("providers", "llm")`. Потребители: `tools/`,
  навык `audit_analyzer`, future-proof для остальных мест.
- **`lib/utils/node_access.py` — единый доступ к настройкам.** Хелперы для
  безопасного обхода `SETTINGS`/`config.json`/`project.json` с поддержкой
  `require_setting` и `get_setting`-fallback. Удалены дублирующие ad-hoc
  обращения в `audit_settings.py`, `application_context.py`,
  `cache_provider_impl.py`.
- **`lib/utils/logging_utils.py` — единая настройка `loguru`.** Раньше
  конфигурация логгера была inline в каждом entry-point (`cli_agent.py`,
  `gateway.py`, `streamlit_app.py`). Теперь — один модуль с пресетами
  (`setup(level=..., json=..., redact_keys=...)`), вызываемый из
  `ApplicationContext.create()` и из CLI-цикла. Гарантирует одинаковый
  формат и redaction секретов во всех точках входа.
- **`lib/utils/outbound_filter.py` — единая фильтрация служебных outbound.**
  Скрывает internal-сообщения (`system`, `audit`, `tool_audit`,
  `_assemble_outbound`-артефакты) из пользовательского потока. Раньше каждый
  канал фильтровал по-своему, и поведение в `Streamlit` расходилось с
  `PostgresChannel`. Теперь фильтр один — через `MessageExchange`.
- **`tools/build_vectors.py` — параметры эмбеддинга только из настроек.**
  Удалён параметр `--model`/fallback на локальный default; всё через
  `audit_vector_settings()`. Это закрывает класс ошибок «модель в CLI
  перебивает БД».

### Changed

- **AW-формат media в переписке: `{filename, data}` → `{filename, file_id,
  mime_type, file_size}`.** Старые dict-форматы продолжают читаться
  (обратная совместимость через `lib/utils/media.py`); новые записи и
  Streamlit используют AW-формат. См. backfill-скрипт для миграции
  существующих данных.
- **`MessageExchange` заменил inline-реализации в `PostgresChannel` и
  `RedisChannel`.** Внутренние методы `_embed_media_for_db`,
  `_decode_media_from_db`, `poll_once` остались как тонкие обёртки над
  общим движком; публичный API каналов не изменился.
- **`get_embedding` унифицирован в `lib.services.vector_index_service`.**
  Параметры (модель, размерность, retry) — только из
  `audit_vector_settings()`; единый `retry_on_exception` декоратор вместо
  локальных `try/except` в каждом вызове.
- **`audit_sync_service.database` — убран дубль `_REWRITE_TO_CHAR`.** SQL
  переписан так, что экранирование выполняется на уровне параметров
  psycopg2, а не вручную в коде.

### Fixed

- **`tests/test_gateway.py` — `fake_config` обзавёлся `get_setting`.** Раньше
  тест падал `AttributeError`, потому что импортируемая зависимость
  (`ApplicationContext.create`) зовёт `get_setting(...)` напрямую.
  Добавлен stub, восстанавливающий ожидаемое поведение фикстуры.
- **`config.py` — добавлен импорт `Any`.** Сломанный `list[tuple[str, Any]]`
  в подсказках типа (до правки падал `NameError: name 'Any' is not defined`
  при импорте в `py 3.14`).

### Tests

- **Удалены 42 «теста-галочки»** (не давали никакой проверки или дублировали
  код под тестом). Разбор всех 42 файлов тестов vs исходники показал: ~87% тестов
  реальные, но ~13% — mock-only или пустые. Удалённое поквартально:
  - `test_benchmarks_models.py` — убраны 11 тестов, пересказывавших дефолты
    датаклассов (сломанный дефолт «чинился» правкой самого теста); остался
    осмысленный `test_hash` (`__hash__`/`__eq__`).
  - `test_cli_agent.py` — `test_defaults` датакласса `DisplayConfig`,
    `test_empty_noop` и `test_dict_settings` (без assert'ов).
  - `test_benchmarks_runner.py` — 3 smoke-теста без assert'ов
    (`test_no_workspace_returns_early`, `test_skips_nonexistent_file`,
    `test_cleanup_called_on_success`).
  - `test_config_service.py` — 5 тестов «не должно упасть» без проверок
    (`test_no_providers_attribute_noop`, `test_missing_provider_section_skipped`,
    `test_exec_timeout_errors_suppressed`, `test_no_config_json_noop`,
    `test_invalid_json_noop`).
  - `test_application_context.py` — 3 lifecycle-теста без единого assert'а
    (`test_start_runs_and_stops`, `test_double_start_is_safe`,
    `test_double_stop_is_safe`).
  - `test_pg_session_manager.py` — 6 тестов (`test_init_defaults`,
    `test_close_noop`, `test_invalidate_removes_from_cache`,
    `test_invalidate_missing`, и два, мокавших саму `_load`:
    `test_read_session_file_found`/`test_read_session_file_not_found`).
  - `test_utils_session_file_store.py` — `TestCsvVal` (4 эхо-теста однострочной
    функции `_csv_val`) и `test_default_limits` (дефолты конструктора).
  - по 1 тесту: `test_console_loop` (`test_empty_noop`),
    `test_subprocess_manager` (`test_terminate_all_with_no_processes`),
    `test_hooks_tool_audit_hook` (`test_empty_state`), `test_benchmarks_db`
    (`test_db_ok_true` — трюизм из мок-фикстуры), `test_streamlit_app`
    (`test_default_fq_table` — дублирует format-string), `test_config`
    (`test_settings_is_attrdict`.
- **Починен сломанный assert в `test_cli_agent.py:317`.**
  `assert os.environ[...] == "WARNING" if False else True` из-за приоритета
  тернарника всегда сводился к `assert True` (ветка вообще не читала `os`).
  Заменён на реальную проверку `NANOBOT_LOG_LEVEL`; добавлен второй тест
  `test_defaults_to_warning`.
- **`test_shutdown_coordinator::test_clear` усилен** — вместо пустого вызова
  теперь `assert order == []` (после `clear()` хендлеры не выполняются).
- **`test_db_loader.py` оставлен с `pytest.skip`** при отсутствии DuckDB-кэша:
  это честный портабельный guard интеграционных тестов, а не заглушка
  (при наличии кэша тесты реально выполняются). Заглушек и «мёртвых»
  assert'ов в наборе не осталось.
- Итог: **900 → 859 тестов, все проходят** (857 удалено/исправлено + 1 новый).

### Migration notes

- **Формат media в `agent_conversation_messages` обновлён до AW.**
  Новые записи пишутся в `{filename, file_id, mime_type, file_size}`;
  старые записи `{filename, data}` продолжают читаться. Для перевода
  существующих данных в новый формат — `python scripts/backfill_media_aw.py`
  (поддерживает `--dry-run`; идемпотентен).
- **`nanobot==0.3.0` закреплён в `requirements.txt`.** Если развёртывание
  на `nanobot<0.3.0` — обновите: `pip install --upgrade 'nanobot==0.3.0'`.

### Tests

- **Удалены 42 «теста-галочки»** (не давали никакой проверки или дублировали
  код под тестом). Разбор всех 42 файлов тестов vs исходники показал: ~87% тестов
  реальные, но ~13% — mock-only или пустые. Удалённое поквартально:
  - `test_benchmarks_models.py` — убраны 11 тестов, пересказывавших дефолты
    датаклассов (сломанный дефолт «чинился» правкой самого теста); остался
    осмысленный `test_hash` (`__hash__`/`__eq__`).
  - `test_cli_agent.py` — `test_defaults` датакласса `DisplayConfig`,
    `test_empty_noop` и `test_dict_settings` (без assert'ов).
  - `test_benchmarks_runner.py` — 3 smoke-теста без assert'ов
    (`test_no_workspace_returns_early`, `test_skips_nonexistent_file`,
    `test_cleanup_called_on_success`).
  - `test_config_service.py` — 5 тестов «не должно упасть» без проверок
    (`test_no_providers_attribute_noop`, `test_missing_provider_section_skipped`,
    `test_exec_timeout_errors_suppressed`, `test_no_config_json_noop`,
    `test_invalid_json_noop`).
  - `test_application_context.py` — 3 lifecycle-теста без единого assert'а
    (`test_start_runs_and_stops`, `test_double_start_is_safe`,
    `test_double_stop_is_safe`).
  - `test_pg_session_manager.py` — 6 тестов (`test_init_defaults`,
    `test_close_noop`, `test_invalidate_removes_from_cache`,
    `test_invalidate_missing`, и два, мокавших саму `_load`:
    `test_read_session_file_found`/`test_read_session_file_not_found`).
  - `test_utils_session_file_store.py` — `TestCsvVal` (4 эхо-теста однострочной
    функции `_csv_val`) и `test_default_limits` (дефолты конструктора).
  - по 1 тесту: `test_console_loop` (`test_empty_noop`),
    `test_subprocess_manager` (`test_terminate_all_with_no_processes`),
    `test_hooks_tool_audit_hook` (`test_empty_state`), `test_benchmarks_db`
    (`test_db_ok_true` — трюизм из мок-фикстуры), `test_streamlit_app`
    (`test_default_fq_table` — дублирует format-string), `test_config`
    (`test_settings_is_attrdict`.
- **Починен сломанный assert в `test_cli_agent.py:317`.**
  `assert os.environ[...] == "WARNING" if False else True` из-за приоритета
  тернарника всегда сводился к `assert True` (ветка вообще не читала `os`).
  Заменён на реальную проверку `NANOBOT_LOG_LEVEL`; добавлен второй тест
  `test_defaults_to_warning`.
- **`test_shutdown_coordinator::test_clear` усилен** — вместо пустого вызова
  теперь `assert order == []` (после `clear()` хендлеры не выполняются).
- **`test_db_loader.py` оставлен с `pytest.skip`** при отсутствии DuckDB-кэша:
  это честный портабельный guard интеграционных тестов, а не заглушка
  (при наличии кэша тесты реально выполняются). Заглушек и «мёртвых»
  assert'ов в наборе не осталось.
- Итог: **900 → 859 тестов, все проходят** (857 удалено/исправлено + 1 новый).

## [2.2.0] — 2026-08-17

> **Minor-релиз:** единый пул соединений PostgreSQL (одна очередь + N воркеров)
> вместо connect-per-op, перенос записи сессий в `data_store/cache/sessions/`
> через новый `SessionFileRedirectHook`, сохранение комментариев таблиц/колонок
> и исходных PG-типов в DuckDB-кэш, единый dict-формат media в переписке.
> Конкурентно-безопасные хуки аудита: `ToolAuditHook` и `DatabaseLoggingHook`
> изолируют состояние по сессии/обороту (при параллельных вопросах события
> и аудит вызовов больше не «путаются»). Удалена неиспользуемая
> write-функциональность `AuditSyncService` (таблица `audit_interactions` и
> конфиг-ключ `sync_write_table`); вопросы/ответы живут только в
> `agent_question_runs`. Удалены скрипты миграции v1.4→v2.0 и индексы из
> create-скриптов (только таблица + COMMENT). Публичный API `utils.db` сохранён.

### Added

- **Единый пул соединений PostgreSQL (`workspace/utils/db.py`).** Вместо
  connect-per-op — общая job-очередь + пул воркеров (`min_conn`/`max_conn`,
  по умолчанию `1`/`4`); каждый воркер владеет единственным psycopg2-соединением
  и выполняет задачи последовательно. Транзакции (`transaction()` /
  `async_transaction()`) получают эксклюзивную аренду соединения (`lease_id`).
  Реконнект с backoff внутри воркера, retry-able задачи переподнимаются до
  `job_max_retries`. Проблема «too many connections» решается на уровне
  архитектуры (не больше `max_conn` соединений с процесса), а не ретраями.
  Публичный API сохранён: `configure/resolve_dsn/run/set_pool_config/get_stats/
  start/shutdown` + sync/async `execute/fetch/fetchone/fetchval/transaction`.
- **Неподключённые воркеры уступают очередь подключённым.** В `_take_job`
  воркер без живого соединения берёт обычную задачу, только когда в пуле нет
  ни одного воркера с живым соединением — иначе задачи обслуживает подключённый
  воркер, а неподключённые не тратят время на retry-connect. При полной
  недоступности БД задачи быстро падают с ошибкой подключения, а не висят
  в очереди вечно. Транзакции (`lease_id != 0`) не затрагиваются.
- **`ApplicationContext`: конфигурация и жизненный цикл общего пула.** `create()`
  читает секцию `channels.postgres.pool` и применяет через `set_pool_config()`;
  `start()/stop()` вызывают `utils.db.start()/shutdown()`. Хелперы
  `_configure_db_pool` / `_start_db_pool` / `_stop_db_pool`;
  тест `test_pool_config_applied_from_settings`. `lib/core/application_context.py`.
- **`workspace/hooks/session_file_redirect_hook.py` — `SessionFileRedirectHook`.**
  AgentHook, подключаемый автоматически через `hook_loader`. Перехватывает
  `write`/`edit`/`create_file`/`write_file` в `before_execute_tool` и
  перенаправляет целевой путь в `data_store/cache/sessions/<session_key>/<file>`,
  если исходный не попадает в whitelist служебных путей (`AGENTS.md`, `lib/`,
  `tests/`, `benchmarks/`, `data_store/`, `**/*.py` и т.д.). Имя папки — из
  `context.session_key` (`cli:1`, `telegram:8281248569`). Кросс-платформенный:
  зарезервированные Windows-имена (`CON`, `PRN`, `NUL`, `COM*`, `LPT*`)
  санитизируются, недопустимые символы вырезаются. Реализует политику
  `workspace/AGENTS.md` «new files must be saved under `data_store/cache/`».
- **Сохранение комментариев таблиц/колонок и исходных PG-типов в DuckDB-кэш.**
  `cache_provider_impl._capture_schema_meta` снимает с PG `COMMENT ON
  TABLE/COLUMN` и `data_type` (включая `varchar(N)`) и кладёт в
  `__nanobot_meta.__schema_meta` файла-снимка. `build_schema()` (`
  lib/utils/duckdb_query.py`) подставляет эти комментарии в описание схемы
  и использует `pg_type` вместо инференса DuckDB. Тесты `tests/test_cache_provider_meta.py`.
- **Проверка «too many connections» на живой БД.** Лимит воспроизведён через
  не-суперюзерную роль (`ALTER ROLE ... CONNECTION LIMIT N`): при `max_conn=10`
  пул держит ровно N подключений, задачи сверх лимита получают ошибку без
  зависания. Замечание: PostgreSQL игнорирует `CONNECTION LIMIT` для ролей
  superuser — проверять только на не-привилегированных ролях.

### Changed

- **`DbLoggingService` — вставки через общий пул `utils.db`.** Убраны собственные
  `_conn/_connect/_close`; `_flush_batch` уходит в пул через `run(...)`. Добавлены
  счётчики `written` / `batch_count` / `question_runs` и кэш `_schema_ok`.
  `lib/services/db_logging_service.py`.
- **`AuditSyncService` — синхронизация через общий пул `utils.db`.** Весь SQL
  через `run(lambda conn: ...)` (`_fetch_all`, `_fetch_incremental`,
  `_fetch_schema`); убраны
  `_conn/_connect/_close_connection`; `_reconnect` сбрасывает `_last_sync`;
  `connected` в `get_stats()` читается из `utils.db.get_stats()`.
  `lib/services/audit_sync_service.py`.
- **`cache_provider_impl` — bulk-load через общий пул `utils.db`.**
  `load_cache_from_postgres`/`check_cache_stale` переведены на
  `utils.db.run(lambda pg_conn: ...)` вместо прямого `psycopg2.connect(dsn)`;
  сообщение `'DSN is not configured'` заменено на `'No cache metadata'`.
  `lib/services/cache_provider_impl.py`.
- **Единый формат media в переписке — dict `{filename, data}`.**
  `PostgresChannel._embed_media_for_db` больше не пишет «голые» data URL:
  локальный файл и уже готовый `data:`-URL оборачиваются в dict
  `{"filename": "<имя>", "data": "data:<mime>;base64,..."}` (имя файла
  сохраняется для агента); HTTP/HTTPS-ссылки остаются строками.
  Чтение (`_decode_media_from_db`) по-прежнему принимает и старые строковые
  data URL — обратная совместимость сохранена. Streamlit уже работал с
  dict-форматом. `lib/channels/postgres_channel.py`.

### Removed

- **Убрана write-функциональность `AuditSyncService`** (`f85b8ed`):
  `submit_write()` / `_write_answer()` / `_ensure_write_table()` / `COMMAND_WRITE`,
  параметры `write_table`/`write_schema`, конфиг-ключ
  `skills.audit_analyzer.sync_write_table` и связанные тесты. Вопросы/ответы
  агента — единственный источник в `public.agent_question_runs`
  (`DbLoggingService`); дублирующая запись в `audit_interactions` удалена.
  `lib/services/audit_sync_service.py`, `lib/services/audit_settings.py`,
  `project.json`, `lib/core/application_context.py`.

### Fixed

- **`ToolAuditHook`: конкурентные вопросы больше не путают `_tool_audit`.**
  Раньше всё состояние хука (записи вызовов, снимки аргументов, счётчик
  пачки) лежало в общих для всех оборотов списках. Обороты разных сессий
  (вопросов) обрабатываются конкурентно, поэтому в `_entries` смешивались
  вызовы разных обсуждений, а `drain()` в конце оборота отдавал чужие
  записи в `metadata._tool_audit`. Теперь состояние изолируется по
  `session_key` (`_entries`/`_calls`/`_pending_start` — словари с bucket-ом
  на сессию), а `drain(session_key)`/`drain_calls(session_key)` забирают
  только записи текущего вопроса. Обёртка `_assemble_outbound` в
  `RuntimePatcher.patch_assemble_outbound` передаёт ключ из `msg.session_key`.
  `workspace/hooks/tool_audit_hook.py`, `lib/services/runtime_patcher.py`,
  `tests/test_hooks_tool_audit_hook.py` (`TestConcurrentSessionsIsolated`).

- **`_CursorProxy` поддерживает итерацию (`workspace/utils/db.py`).**
  Транзакционный курсор не реализовывал протокол iterable, из-за чего
  `PGSessionManager` падал с `TypeError: '_CursorProxy' object is not
  iterable` в `for row in cur:` (блокировало старт оборота после
  auto-compact). Добавлен `__iter__`, выполняющий `fetchall()` одним job-ом —
  поведение совпадает с psycopg2. Регрессия: `test_transaction_cursor_iteration`.
- **`_CursorProxy.execute` больше не передаёт `()` вместо `None` (`8d43dfb`).**
  psycopg2 при `params=()` пытается делать `%`-форматирование SQL и падает на
  литералах `%` в данных (например, «16.7%» в контенте сообщения сессии) —
  это ломало `execute_values` при сохранении таких сессий (иногда `IndexError:
  tuple index out of range`). Теперь `params` передаётся as-is (`None` означает
  «параметров нет», форматирование не выполняется). `workspace/utils/db.py`.
- **`PGSessionManager` соблюдает контракт базового `SessionManager`.**
  `__init__` теперь вызывает `super().__init__(workspace=self.workspace)`, что
  задаёт `sessions_dir`/`legacy_sessions_dir`. Ранее фреймворковые
  WebUI-эндпоинты (`/api/sessions`, `/api/sessions/<key>/webui-thread`)
  падали с `AttributeError: 'PGSessionManager' object has no attribute
  'sessions_dir'`. Регрессия: `test_init_sets_framework_contract`.
- **Пул: `_maybe_shrink` не падает на воркере без `_idle_since`.**
  При старте/shutdown `worker._idle_since` может быть `None` — вычитание
  `time.monotonic() - None` роняло поток `TypeError` (всплывало как
  предупреждение при teardown). Добавлен гард `_idle_since is not None`.
  Регрессия: `test_maybe_shrink_skips_never_idle_worker`.

### Tests

- **Тесты пула переписаны полностью** (`tests/test_utils_db.py`, 40):
  `TestPool` (переиспользование соединения, не-закрытие между операциями,
  параллельные транзакции на разных соединениях, авто-масштаб при аренде,
  переполнение очереди, `get_stats`), `TestTransaction`, `TestAsyncAPI`,
  `TestAsyncTransaction`. Новые тесты поведения при недоступных подключениях:
  `test_unconnected_worker_yields_to_connected` (симуляция `CONNECTION LIMIT`
  роли) и `test_connect_failure_returns_error_fast`. Исправлена гонка в
  `test_parallel_transactions_use_separate_connections` (барьер перенесён
  внутрь транзакций).
- Новые/обновлённые: `test_cache_provider_meta.py` (schema-meta в кэш),
  `test_application_context.py` (`test_pool_config_applied_from_settings`),
  `test_audit_sync_service.py` (в т.ч. −58 строк удалённой write-функциональности),
  `test_db_logging_service.py` (фикстура
  `fake_psycopg2` патчит реальный psycopg2 и сбрасывает пул в teardown),
  `test_postgres_channel.py` (`test_embed_data_wraps_in_dict`,
  `test_embed_local_file_wraps_in_dict`).
- Регрессии рантайм-багов: `test_transaction_cursor_iteration`,
  `test_execute_none_params_not_converted_to_tuple`,
  `test_init_sets_framework_contract`, `test_maybe_shrink_skips_never_idle_worker`;
  новые factory-тесты хуков `TestDatabaseLoggingHookFactory`
  (`tests/test_hooks_database_logging.py`) и `TestConcurrentSessionsIsolated`
  (`tests/test_hooks_tool_audit_hook.py`).
- Итог: **868 passed**, `py_compile` всех изменённых модулей OK.

### Migration notes

- **Удалён каталог `sql/auto_migrate_1.4_2.0/` и DO-блоки `CREATE INDEX`.**
  В `8f1ec22` убраны генераторы и скрипты миграции v1.4→v2.0, все
  `DO`-блоки `CREATE INDEX` и триггер/функция из create-скриптов — остались
  только `CREATE TABLE` + `COMMENT ON TABLE/COLUMN`. Если на развёртывании
  нужны индексы/триггер, подавайте их отдельно (актуальные пути:
  `sql/session/`, `sql/logs/`, `sql/benchmarks/`, `sql/channels/`,
  `sql/audit_analyzer/`). `benchmarks/db.py:ensure_tables()` и
  `DbLoggingService` ссылаются на обновлённые create-скрипты.
- **`skills.audit_analyzer.sync_write_table` удалён** — уберите его из
  `project.json` (оставленный ключ игнорируется, ошибки не вызовет). Таблица
  `audit_interactions` больше не пишется: вопросы/ответы агента читайте из
  `public.agent_question_runs` (`DbLoggingService`). Существующие строки
  `audit_interactions` можно удалить вручную, если не нужны.
- **Формат media изменился** на dict `{filename, data}` для новых записей.
  Старые строковые data URL в `agent_conversation_messages.media` продолжают
  читаться (`_decode_media_from_db` принимает оба варианта).
- **`PGSessionManager` инициализирует базовый `SessionManager`** — заводит
  служебную папку `<workspace>/sessions` (`sessions_dir`), требуемую
  WebUI-эндпоинтам фреймворка. Рантайм-контракт сохранён; данные сессий
  по-прежнему в PostgreSQL.

## [2.1.0] — 2026-08-14

> **Minor-релиз:** строгие настройки (никаких тихих fallback в коде),
> cache-only векторный поиск навыка, единый сервисный слой для FAISS-индексов
> и генераторы миграции v1.4 → v2.0.

### Added

- **`config.require_setting(*keys)` + `ConfigurationError`.** Строгий доступ
  к `SETTINGS`: отсутствие обязательного ключа — ошибка конфигурации, а не
  молчаливая подстановка `default`. `get_setting` остаётся для случаев, где
  fallback действительно нужен. `config.py:211-257`.
- **`lib/services/audit_settings.py` — единый источник правды** для настроек
  навыка `audit_analyzer`. Dataclass `AuditVectorSettings` читает все ключи
  секции `skills.audit_analyzer` строго через `require_setting` (без литералов
  в коде). Помощник `normalize_additional_tables` приводит
  `db_additional_tables` к виду `schema.table`. Потребители: gateway,
  `ApplicationContext`, `AuditSyncService`, `AuditMemoryStore`,
  `cache_provider_impl`, `tools/build_vectors.py`.
- **`lib/services/vector_index_service.py` — единый build-слой FAISS.**
  - `get_embedding(text, ...)` — единственная точка создания эмбеддинга
    (Ollama `/api/embed`); параметры берутся из `audit_vector_settings()`.
  - `VectorIndexBuildService` — держит ОДИН `PostgresDuckDbProvider`,
    пересобирает индекс (`rebuild_and_store`) и сохраняет blob в
    `agent_vector_index_store`. Используется навыком и `build_vectors.py`.
- **Cache-only векторный поиск навыка.** `PostgresDuckDbProvider.search_vector`
  строит FAISS-индекс ТОЛЬКО из локального снимка DuckDB (`audit_cache.duckdb`,
  `_load_index_from_cache`) — без обращения к PostgreSQL. Проверка размерности
  индекса vs эмбеддинг запроса с понятной ошибкой. `cache_provider_impl.py:680-760`.
- **`lib/utils/duckdb_query.py:build_faiss_index(records)`** — общий помощник
  построения `IndexFlatIP` из записей (используется кэш-путе и памяти).
- **Gateway: пересоздание снапшота при каждом старте.** Устаревший файл кеша
  удаляется до `initial_load`, чтобы CLI/skill не читали данные прошлого
  запуска. `gateway.py:57-64`.
- **`AuditMemoryStore.publish(force=...)` + отчёт о публикации.** Первая
  публикация после старта — принудительная (`force=True`), даже если
  `initial_load` не нашёл строк. При публикации выводятся таблицы и число
  строк. `audit_memory_store.py`.
- **Генераторы миграции v1.4 → v2.0** (`sql/auto_migrate_1.4_2.0/`):
  `vector_indexes_migration.sql`, `predefined_scripts_migration.sql`,
  `populate_agent_vector_index_config.sql` (сгенерированы Python-скриптами);
  добавлен сгенерированный `workspace/skills/audit_analyzer/scripts/generated/fetch_audit_title.py`.

### Changed

- **Убраны тихие fallback и авто-дефолты по всей кодовой базе:**
  - `PGSessionManager` — исключён JSONL-fallback: ошибки БД пробрасываются
    (раньше молча падали на файлы). `messages_table`/`meta_table` обязательны.
    `lib/session/pg_session_manager.py`.
  - `DbLoggingService` — удалён JSONL-fallback: при недоступности БД события
    выбрасываются (счётчик `failed` + `last_error`), скрытой записи в файл нет.
  - `session_storage.py` — невалидный `session_manager.json` теперь ошибка
    (раньше тихо `{}`); отсутствие `messages_table`/`meta_table` → ошибка.
  - `preload_service.py` — удалены `try/except`-обёртки: ошибки чтения конфига
    больше не маскируются `(None, None)`.
  - `benchmarks/db.py` — `benchmark.runs_table`/`results_table` обязательны.
  - `benchmarks/evaluator.py` — LLM-судья больше не возвращает нейтральные
    `0.5` при сбое: проверка считается НЕ пройденной (`0.0`).
  - `streamlit_app.py` — `_get_extension_from_mime` не подставляет `.bin`
    для неизвестного MIME (возвращает `""`).
  - `postgres_channel.py` / `application_context.py` — `fallback_path` для
    логов убран; обязательные таблицы проверяются явно.
- **`project.json`: только DSN без частей.** `channels.postgres` больше не
  содержит `host`/`port`/`dbname`/`user` — подключение только через
  `"dsn": "${DATABASE_URL}"` (из `.secrets.env`). Удалён
  `logging.db.fallback_path`.
- **`cache_provider_impl.py` — конфиг только из БД.** `read_vector_index_config`
  и `read_embedding_config` больше не имеют fallback на `cfg`/проектные
  литералы — источник один: `agent_vector_index_config` + `audit_vector_settings()`.
- **`AuditSyncService` не провижинит схему.** Удалено авто-создание таблицы
  записи (`_ensure_write_table`): сервис проверяет существование и отключает
  запись с явной ошибкой, если таблицы нет. Обработка `UndefinedTable` для
  отсутствующих таблиц-источников. `audit_sync_service.py`.
- **`build_vectors.py` — единый сервисный слой.** Вместо собственных
  `httpx`/эмбеддинг-реализаций используется `vector_index_service.get_embedding`
  и `VectorIndexBuildService.rebuild_and_store`. Убран прямой импорт `httpx`.

### Fixed

- **`db_loader.load_registry` падал на `ParamDefinition(**None)`** при
  параметрах без явного определения (значение `null` в JSONB `parameters`).
  Теперь такие параметры пропускаются, а не валят загрузку реестра.
  `db_loader.py:156-166`.
- **Тесты под новый строгий конфиг:** `test_session_storage` —
  `test_invalid_json_is_ignored` → `test_invalid_json_raises` (невалидный
  `session_manager.json` поднимает ошибку); `test_streamlit_app` —
  `test_unknown_mime_gets_bin` → `test_unknown_mime_returns_empty`
  (неизвестный MIME возвращает `""`). Это тесты, противоречившие новому
  поведению, заявленному в этом релизе.

### Tests

- Обновлены: `test_audit_memory_store` (publish включает векторную таблицу,
  поиск из опубликованного снимка, проверка размерности эмбеддинга),
  `test_audit_sync_service`, `test_benchmarks_evaluator`, `test_config_keys`
  (`require_setting`/`ConfigurationError`), `test_db_logging_service`,
  `test_pg_session_manager`, `test_session_storage`, `test_postgres_channel`,
  `test_utils_db` — под строгий конфиг и cache-only поиск.
- **852 теста — все проходят** (после фикса `db_loader` из раздела Fixed).

### Migration notes

- **`channels.postgres.host/port/dbname/user` удалены.** Если вы задавали
  подключение частями — переведите в полный DSN:
  `"dsn": "${DATABASE_URL}"` в `project.json` + `DATABASE_URL=...` в
  `.secrets.env`.
- **Удалён `logging.db.fallback_path`.** Уберите его из `project.json`;
  поведение при недоступности БД — дроп событий со счётчиком `failed`
  (в `get_stats()`).
- **`AuditSyncService` больше не создаёт таблицы.** Если `oarb.audit_interactions`
  (или иная `sync_write_table`) отсутствует — запишите DDL из
  `sql/created_tables.sql` заранее; иначе запись ответов навыка отключится
  с явной ошибкой в логе.
- **Векторный поиск навыка — только локальный снимок.** Индекс строится из
  `audit_cache.duckdb`; убедитесь, что gateway публикует снапшот после
  синхронизации (см. `047dc3b`). При расхождении размерностей пересоберите
  снимок той же моделью эмбеддинга.

---

## [2.0.1] — 2026-08-14

> **Patch-релиз:** регрессии и баги, обнаруженные сразу после выхода v2.0.0.
> Совместимость с `nanobot 0.3.0`, восстановленный снапшот DuckDB-кеша,
> починенный `build_vectors.py`, GP-6.5-only SQL.

### Fixed

- **Gateway: регресс публикации DuckDB-снапшота.** После `af37488`
  (ApplicationContext-рефакторинг) callback `on_sync_callback` только
  ставил `asyncio.Event` для FAISS-preload, но не вызывал
  `memory_store.publish()` — данные копились в in-memory DuckDB
  `AuditMemoryStore`, а файл
  `workspace/skills/audit_analyzer/cache/audit_cache.duckdb` не создавался.
  На Linux это было особенно заметно: CLI/skill читают снимок строго
  с диска. Фикс: добавлен `memory_store.publish()` в обёртку
  `on_sync_callback` (после установки Event) + финальный `publish()`
  перед `ctx.stop()`. `gateway.py:73-83, 95-101`.
- **Gateway: `connect_timeout=10` для PostgreSQL.** В `AuditSyncService`
  выставлен таймаут 10с (раньше дефолтный ~2 минуты маскировал
  DNS/файрвол-проблемы за счёт длинных задержек). Дополнительно
  добавлено логирование и инкремент `stats` при неудачных PG-коннектах.
  `lib/services/audit_sync_service.py:568`.
- **`tools/build_vectors.py`: NameError на первом чанке.** После
  переименования `_get_embeddings` → `_get_embedding` в `ba7bb58`
  цикл `build_index` остался со старым именем — падал с `NameError`
  при первом чанке. Дополнительно `_get_embedding` валидирует тип
  первого вектора (`list[float]`) и логирует WARN, если сервер
  эмбеддингов вернул >1 вектора на 1 текст. `tools/build_vectors.py:74-110, 380`.
- **`PostgresChannel`: совместимость outbound-сигнатур с `nanobot 0.3.0`.**
  До фикса `ChannelManager._send_reasoning_end` / `_send_delta` /
  `_send_stream_event` бросали `TypeError: ... unexpected keyword
  argument 'stream_id'`. Изменены сигнатуры:
  - `send_reasoning_delta(chat_id, delta, metadata=None, *, stream_id=None)`
  - `send_reasoning_end(chat_id, metadata=None, *, stream_id=None)`
  - `send_delta(chat_id, delta, metadata=None, *, stream_id=None,
    stream_end=False, resuming=False)`
  Внутри `stream_id` принимается, но для рассуждений канал
  ключуется по `assistant_msg_id`; `del stream_id` для совместимости.
  Буфер `_stream_buffers` теперь ключуется по `stream_id`
  (fallback: `meta["_stream_id"]` → `chat_id`).
  `lib/channels/postgres_channel.py:618-830`.

### Changed

- **`build_vectors.py`: одиночные вызовы `/api/embed`.** Цикл
  эмбеддинга переписан с батчей на одиночные запросы (`input=text`),
  добавлен CLI-аргумент `--pause-sec` (default 5.0 или
  `build_pause_sec` из `project.json`). Это даёт более ровную нагрузку
  на Ollama при больших индексах.
- **`sql/`: один файл = одна таблица, GP 6.5 only.** Объединённые
  `create_*_tables.sql` разделены на отдельные скрипты по одной таблице
  (`create_<schema>_<table>.sql`); удалены дубли PG13+ вариантов
  (оставлен только GP 6.5). `COMMENT ON TABLE/COLUMN` перенесён
  внутрь файлов создания. Все скрипты: `DISTRIBUTED BY`, `pgcrypto`,
  `BIGINT IDENTITY`, без FK; `CREATE INDEX IF NOT EXISTS` заменён на
  `DO`-блок с проверкой `pg_indexes`.
- **Удалены устаревшие SQL-каталоги:** `sql/comments/`,
  `sql/snapshot/`, `sql/migrations/` (DONE-миграции не нужны в
  e2e-инсталляциях). Новый `sql/auto_migrate_1.4_2.0/` (см. ниже)
  переехал с inline-SQL на Python-генераторы. Скрипты «обновлены»:
  `sql/README.md`.

### Added

- **`sql/auto_migrate_1.4_2.0/` — генераторы миграции v1.4 → v2.0.**
  - `generate_vector_indexes_migration.py` — перенос конфигов
    vector-индексов из JSON v1.4 в `public.agent_vector_index_config`
    (upsert через `DO`-блок, GP 6.5-совместимо).
  - `generate_predefined_scripts_migration.py` — перенос реестра
    SQL-скриптов из `scripts_registry.py` v1.4 в
    `public.agent_predefined_scripts` (DELETE+INSERT, GP 6.5 совместимо).
  - `created_tables.sql` — готовый DDL всех таблиц v2.0 для Greenplum 6.5
    (UUID, `DISTRIBUTED BY`, комментарии). Применяется как отдельный
    шаг перед миграцией данных.
  Требование: `CREATE EXTENSION IF NOT EXISTS "uuid-ossp";`

### Migration notes

- **С v2.0.0 → v2.0.1**: код-совместимо. Требуется перезапуск gateway,
  если запущен на `nanobot >= 0.3.0` (signatures `send_*` изменились).
- **Пользователям v1.4 → v2.0**: используйте генераторы из
  `sql/auto_migrate_1.4_2.0/` (см. `sql/auto_migrate_1.4_2.0/README.md`).
  Полный сценарий — три шага: создание таблиц → миграция
  vector_indexes → миграция predefined_scripts.
- **Обновите `tools/build_vectors.py`** — без фикса `81fbc28`
  первая же попытка индексации упадёт с `NameError`.

---

## [2.0.0] — 2026-08-13

> **Главный релиз:** выделен сервисный слой и единый bootstrap-контекст
> (`ApplicationContext`). `gateway.py` и `cli_agent.py` стали тонкими
> оркестраторами. Аудит-инфраструктура (`audit_analyzer`) переехала в
> универсальный слой `lib/services`, gateway — единственный владелец
> DuckDB-кеша навыка. Расширен `agent_`-префикс на все таблицы агента,
> вынесены magic-числа в `project.json`, нейтрализован LLM-провайдер.

### Added

**Конфигурация: единый стиль settings-чтения.** Добавлен хелпер
`config.get_setting(*keys, default=...)` для безопасного доступа к вложенным
ключам `SETTINGS` с fallback.

**Тест-каркас `tests/test_config_keys.py`:** проверяет наличие и дефолты
всех обязательных ключей в `project.json` через JSONC-парсер из `config.py`.
При добавлении новой настройки — добавляйте запись в `REQUIRED_KEYS`.

**Новые ключи `project.json` (из рефакторинга hardcoded-значений):**
- `channels.postgres.{max_stuck_retries, msg_ctx_max_size, media_cache_dir}` и под-секция `pool.{min_conn, max_conn, pool_timeout}`;
- `channels.redis.{error_backoff_sec, reply_to_max_size, reply_to_trim_to}`;
- `skills.audit_analyzer.{sync_max_queue_size, reconnect_backoff_sec, reconnect_backoff_max_sec, cache_max_age_sec, cache_refresh_interval_sec, embedding_http_timeout_sec, mode_vector_store_table, vector_index_default_path, cli_default_format, text_chunk_size, text_chunk_overlap, build_batch_pause_sec}`;
- `cli.repl_idle_timeout_sec`;
- `streamlit.{files_dir, failed_window_sec}` (теперь `error_window_sec`);
- `gateway.{restart_initial_delay_sec, restart_max_delay_sec, streamlit_port, streamlit_log_filename, subprocess_shutdown_timeout_sec}`;
- `logging.db.{dialect, fallback_path, connect_backoff_sec, connect_backoff_max_sec, summary_max_chars}`.

**`agent_question_runs` хранит полный текст вопроса и ответа (без обрезки):**
добавлены колонки `question` (полный текст сообщения пользователя),
`response` (полный ответ агента) и `media` (JSON-список приложенных файлов).
`question`/`media` заполняются в `register_request` (inbound), `response` —
в `finish_request` (after_run/подагент). В `summary` остаётся краткая
версия (обрезанная) для быстрого просмотра.

**Логирование вложенных файлов (media):** `log_inbound`/`log_outbound`
теперь кладут `media` (список `media` из `InboundMessage`/`OutboundMessage`)
в `payload["media"]` события `agent_gateway_logs`. Файлы от пользователя
(inbound) и вложения агента (outbound) больше не теряются в логах.
В `register_request` media также сохраняется в `agent_question_runs.media`
(вопросные вложения не затираются при финализации через `COALESCE`).

**Колонки `question`/`response`/`media` покрыты миграциями** в
`create_logs_table.sql(+_gp)` и `migrate_logs_v1.sql(+_gp)` — идемпотентное
`ADD COLUMN` для существующих установок.

**Сервисный слой v2.0.0 (`lib/`)**

- **`lib/core/application_context.py:ApplicationContext`** — единый bootstrap
  всех общих сервисов. Поля: `bus`, `agent`, `tool_audit_hook`, `hooks`,
  `session_manager`, `db_logging_service`, `audit_sync_service`,
  `audit_memory_store`, `config_service`, `runtime_patcher`,
  `transcription_service`, `subprocess_manager`, `preload_service`. Метод
  `start()` использует `ShutdownCoordinator`, `stop()` — LIFO graceful
  shutdown. Graceful degradation: при недоступности БД сервис остаётся `None`.
- **`lib/core/agent_factory.py:AgentFactory`** — `create(...)` возвращает
  `(agent, hooks)`; создаёт `AgentLoop` с `ToolAuditHook` + `DatabaseLoggingHook`.
- **`lib/core/bus_factory.py:BusFactory`** — оборачивает `publish_inbound` /
  `publish_outbound` async-логгерами `DbLoggingService` без monkey-patch'ей.
- **`lib/services/config_service.py`** — единый SETTINGS-аксессор,
  `_load_runtime_config`, pre-resolve `${VAR}` из `.secrets.env`.
- **`lib/services/session_storage.py`** — выбор `PGSessionManager` /
  `SessionManager` (auto / postgres / file).
- **`lib/services/runtime_patcher.py`** — оба monkey-patch'а в одном классе
  (`ContextGovernor.normalize_tool_result` + `agent._assemble_outbound`) с
  fallback при изменении API nanobot.
- **`lib/services/channel_factory.py`** — `ChannelManager` + Redis + Postgres
  каналы + транскрипция.
- **`lib/services/transcription_service.py`** — openai/groq key/URL/language.
- **`lib/services/subprocess_manager.py`** — Streamlit spawn + terminate/kill.
- **`lib/services/preload_service.py`** — FAISS preload (gateway) +
  audit_cache refresh (cli).
- **`lib/services/db_logging_service.py`**  — структурированный журнал
  агента в `gateway_logs`: worker-поток, единственное psycopg2-соединение,
  неблокирующая очередь, batch INSERT через `execute_batch`, JSONL-fallback
  при недоступности БД, `get_stats()`. Методы `log_inbound`, `log_outbound`,
  `log_tool_call`, `log_tool_result` (с `latency_ms`), `log_error`.
- **`lib/services/db_logging_bus.py`**  — обёртки `publish_inbound` /
  `publish_outbound` для `DbLoggingService`.
- **`sql/logs/create_logs_table.sql`** — DDL для `gateway_logs`
  (UUID, JSONB, индексы по `timestamp` / `session_id` / `event_type` / `level`).
- **`lib/lifecycle/gateway_runner.py`** — `run_forever` с exponential backoff
  (1с → 30с) при падении.
- **`lib/lifecycle/shutdown_coordinator.py`** — LIFO graceful shutdown.
- **`lib/cli/console_loop.py`** — REPL + typewriter + `consume_outbound`
  (вынесено из `cli_agent.py`).
- **`lib/cli/display_config.py`** — `DisplayConfig`.
- **`lib/cli/hook_loader.py`** — сканирование `workspace/hooks/*.py`.
- **`workspace/hooks/database_logging_hook.py`** — `AgentHook` для tool-событий
  + `after_run` summary в БД.

**`audit_analyzer` — универсальный слой данных (`lib/services`)**

- **`lib/services/cache_provider.py`** — интерфейс `CacheProvider`
  (`is_ready` / `refresh` / `check_stale` / `preload_indexes` / `search_vector` /
  `query_sql` / `explain` / `get_schema` / `close`) + dataclass `SearchResult`.
- **`lib/services/cache_provider_impl.py`** — `PostgresDuckDbProvider`
  (DuckDB-кеш + FAISS-индексы). Модульные функции: `get_embedding`
  (Ollama `/api/embed`), `load_cache_from_postgres`, `check_cache_stale`,
  `read_vector_index_config`, `read_embedding_config`, `build_cache_provider`.
  Тяжёлые зависимости импортируются лениво внутри методов.
- **`lib/services/text_splitter.py`** — чанкование текстов для индексаторов
  (вынесено из навыка).
- **`lib/services/audit_memory_store.py`** — in-memory DuckDB-зеркало +
  FAISS-индексы + атомарный `publish()` (ATTACH temp + `os.replace`).
  `ensure_schema()` создаёт таблицы с типами из PG, сохраняет
  `pg_type` + комментарии в `__nanobot_meta.__schema_meta`. Снапшот
  публикуется в файл `in_memory_cache_path` навыка; `publish()` no-op,
  если `_dirty=False` или `publish_path` пуст.
- **`lib/services/audit_sync_service.py`** — фоновый worker-поток, единственный
  psycopg2-коннекшн. `_fetch_schema` собирает структуру из PG
  (`information_schema` + `pg_description`). Callbacks: `on_new_records`,
  `on_replace_records`, `on_schema`, `on_sync`. Полная пересинхронизация
  каждые `full_resync_every` циклов.

**Инфраструктура `audit_analyzer`**

- **`tools/build_vectors.py`** — индексатор вынесен в корень проекта
  (вне навыка). Флаги: `--full-rebuild`, `--check`, `--status`,
  `--dry-run`, `--index`, `--batch-size`, `--chunk-size`, `--chunk-overlap`,
  `--db-table`. Чанкование через `lib/services/text_splitter.py`.
- **`sql/audit_analyzer/create_audit_source_tables_gp.sql`** — REFERENCE DDL домена
  (`oarb.audits`, `oarb.violations`, `oarb.audit_reports`, `oarb.report_items`).
- **`sql/audit_analyzer/create_audit_vectors_table_gp.sql`** — `oarb.audit_vectors` +
  `oarb.vector_index_store` + индексы.
- **`sql/audit_analyzer/create_vector_index_config_gp.sql`** — `oarb.vector_index_config`.

**Конфигурация и секреты**

- **`project.json`** (JSONC с `//` и `/* */` комментариями) — новый формат
  проектных настроек. Порядок мержа: `project.json → config.json →
  .secrets.env` (поздний перекрывает ранний).
- Новые секции `project.json`: `channels.*` (postgres/redis), `skills.*`,
  `cli`, `benchmark`, `streamlit`, `gateway`, `logging.db`.
- Механизм подстановки секретов `${VAR}` из `.secrets.env` /
  `os.environ` при чтении конфигурации.
- **`.secrets.env.example`** — шаблон переменных окружения.
- **`workspace/utils/db.py:resolve_dsn()`** — единое разрешение DSN
  (`configure()` → `channels.postgres.dsn` → `DATABASE_URL`/`PG_DSN`),
  идемпотентная настройка глобального коннектора.

**Документация**

- `DEVELOPMENT.md` — техническая документация: архитектура сервисного
  слоя v2.0.0, полная таблица связей между файлами (`lib/core/`,
  `lib/services/`, `lib/cli/`, `lib/lifecycle/`), жизненный цикл кеша,
  раздел «Управление синхронизацией» (callbacks, ключи конфига,
  мониторинг, требования к таблицам источника).
- `README.md` — обновлён под v2.0.0: mermaid-диаграммы, 11 компонентов,
  таблица БД, запуск.

### Changed

- **Точки входа → тонкие оркестраторы:**
  `gateway.py` сократился с 696 до 132 строк, `cli_agent.py` — с 865 до 165.
- **`audit_analyzer` — тонкий CLI поверх `lib/services`.** Удалены
  `InMemoryDatabase`, `vector_mode.py`, `check_status.py`,
  `cache/query_audit.py`. Навык работает с `PostgresDuckDbProvider`
  через `build_cache_provider()`; `Database` (прямой PG) и провайдер
  кеша реализуют единый протокол `QueryBackend` (`get_schema` /
  `query_sql` / `explain`).
- **Gateway — единственный владелец файла кеша навыка.** `AuditSyncService`
  инкрементально синхронизирует таблицы в `AuditMemoryStore`, после
  каждого цикла `store.publish()` атомарно записывает снимок
  (`temp + os.replace`) в файл кеша навыка. CLI открывает снимок только
  на чтение.
- **Pre-resolve `${VAR}` от `.secrets.env`**: gateway больше НЕ требует
  `export LLM_API_KEY=...` в shell — `ConfigService._pre_resolve_env_refs`
  кладёт ключи в `os.environ` ДО `_load_runtime_config`.
- **`--force` в `audit_analyzer` удалён.**
  `cli.py` завершается с `FileNotFoundError`, указывающим на gateway.
- **`audit_analyzer.SCRIPTS_REGISTRY`** вынесен из `scripts_registry.py`
  в отдельный `predefined_scripts.py` (позднее — реестр перенесён в БД,
  см. ниже).
- **`requirements.txt`** — убраны неиспользуемые пакеты (`requests`,
  `sentence-transformers`, `anthropic`, `openai`), версии — точные
  `=X.Y.Z` для полной воспроизводимости.
- **`config.py`** — удалена загрузка `.env` (защитный fallback) и
  константа `_ENV_FILE` (больше не используется); merge-order
  комментарий обновлён.
- **Документация:** ASCII-арт заменён на mermaid-диаграммы,
  `REFACTORING_PLAN.md` удалён (план завершён).
- **Все ранее хардкоженные magic-числа и пути вынесены в `project.json`**:
  timeout'ы, интервалы, retry-лимиты, пороги, размеры пулов и очередей,
  пути к кешу/индексам, chunk-параметры — теперь управляются через настройки.
- `cli_agent.py` (`--mode`): снят `required=True`. Значение по умолчанию
  берётся из `skills.audit_analyzer.cli_default_mode` в `project.json`.
- `channels.postgres.processing_timeout`: дефолт унифицирован на `120`
  (раньше расходился между `project.json=120` и `postgres_channel.py=600`).
- `skills.audit_analyzer.vector_index_default_path`: дефолт сменён с
  `~/.nanobot/vectors/audits_index` на `workspace/data_store/vectors/audits_index`.
- `DbLoggingService`: удалены мёртвые параметры `min_conn`/`max_conn`
  (не подключались к реальному пулу).
- **`DbLoggingService._render_sql` исправлена для PostgreSQL:** наивная
  `replace("agent_gateway_logs", ...)` ломала `RENAME TO` в миграциях
  (`RENAME TO "public"."agent_gateway_logs"`). Теперь цель `RENAME TO`
  и schema-qualified `public.agent_gateway_logs` не квалифицируются повторно.
- `lib/channels/postgres_channel.py`: `_session_media_dir` стал инстанс-методом
  (раньше — static с глобальной `_DATA_STORE_DIR`); путь управляется ключом
  `channels.postgres.media_cache_dir`.
- `tools/build_vectors.py`: default'ы `--chunk-size` / `--chunk-overlap` /
  `--batch-pause` читаются из `skills.audit_analyzer.*`.
- **Единый `agent_`-префикс для оставшихся таблиц агента.**
  `public.predefined_scripts` → `public.agent_predefined_scripts`;
  `conversation_messages` → `public.agent_conversation_messages` (канал Web/Streamlit);
  векторные таблицы перенесены из `oarb` в `public`:
  `oarb.vector_index_config` → `public.agent_vector_index_config`,
  `oarb.vector_index_store` → `public.agent_vector_index_store`.
  Данные переносятся миграцией `sql/migrations/migrate_agent_table_names_v1.sql`
  (идемпотентно — сохраняет строки). Обновлены конфиг-ключи
  `channels.postgres.table_name`, `skills.audit_analyzer.{predefined_scripts_table,
  mode_vector_index_config_table, mode_vector_store_table}`, DDL, seed'ы,
  комментарии, `generate_predefined_scripts_sql.py` и тесты. Доменные таблицы
  навыка (`oarb.audits/violations/audit_reports/report_items/audit_vectors`)
  и `audit_interactions` не затронуты.
- **Навык `audit_analyzer` теперь читает ВСЕ данные из DuckDB-кэша.**
  Прямой psycopg2 в read-only потоке навыка не используется.
  Раньше `db_loader` ходил в PG через `utils.db.fetch` — теперь через
  `cache_provider.query_sql(...)` (тот же DuckDB-файл, что и для аудит-данных).
- `lib/services/cache_provider_impl.py`: копирование PG → DuckDB переведено
  с `pd.DataFrame(records)` на `COPY ... TO STDOUT` + `read_csv_auto`
  (без `pandas`, без pyarrow-IPC). Сохраняются типы JSONB → JSON,
  TIMESTAMPTZ → TIMESTAMPTZ, NUMERIC → DECIMAL, UUID → UUID.
- `lib/services/audit_memory_store.py`: `pd.DataFrame(records)` →
  `pyarrow.Table.from_pylist` + `conn.register`. Сохраняет `list[float]`
  как `DOUBLE[]` (раньше через `pd` → `DOUBLE[]`, через CSV → `VARCHAR`).
- Удалён `workspace/skills/audit_analyzer/scripts/predefined_scripts.py`
  (267 строк) — реестр перенесён в БД.
- `cache_provider_impl.py` теперь поддерживает `additional_tables`
  (минимальное расширение для копирования таблиц из произвольных схем).

### Removed

- Навыки **`data-analyzer`** и **`html_presentation_generator`** —
  вычищены зависимости из `requirements.txt`, блоки из
  `project.json`/`config.json`. Все артефакты аудита этих навыков
  удалены (`webui/`, `ws/`, `media/`, `workspace/data_store/cache/*.html`,
  `count_numbers.py`).
- **`pg_agent_worker.py`** и `tests/test_pg_agent_worker.py` — старый
  standalone Postgres-воркер, не использовался в v2.0.0.
- Мёртвые ключи конфига: `schema_cache`, `cli_default_format`,
  `_ENV_FILE` (не читались кодом).
- Мусорные артефакты: `lib/channels/workspace/` (баг путей v1.4.0),
  `__pycache__/` старых хуков.
- `webui/` (старый SPA-dist из v1.3.0), `ws/`, `media/`.
- `REFACTORING_PLAN.md` (план завершён).
- Legacy `workspace/skills/audit_analyzer/DEVELOPMENT/*` —
  перенесены в корень (`DEVELOPMENT.md`) и `tools/build_vectors.py`.
- Legacy мигратор `migrate_vectors_to_db.py`.
- Legacy-таблицы **`public.agent_questions`** и **`public.agent_responses`**.
  Они не использовались ни одним Python-модулем; их роль полностью покрыта
  `agent_gateway_logs` (вопрос → `inbound`, ответ → `outbound_final` в
  `payload.content`) + `agent_question_runs` (контекст).
- `pandas` из `requirements.txt` (заменён на `pyarrow`).

### Fixed

- **Race-condition FAISS preload**: callbacks на `AuditSyncService`
  устанавливаются **ДО** `ctx.start()`, иначе worker-тред при первом
  `_do_initial_load` скипает записи → DuckDB остаётся пустым →
  `preload_vector_indexes` видит «нет данных». Теперь в `gateway.py:main()`
  callbacks идут раньше `start()`.
- **Совместимость с nanobot 0.2.2**: инъекция провайдерских API-ключей
  из `.secrets.env` в конфиг на старте (провайдер-скоупинг формат
  `.secrets.env` не попадал в `os.environ` как `LLM_API_KEY`).
- **`PostgresChannel.send`** — runtime-progress события больше не
  затирают `media` сообщений-инструментов.
- **Streamlit**: `_get_extension_from_mime` корректно выводит расширение
  через `mimetypes` (с fallback `.bin`); ожидание ответа агента больше
  не имеет таймаута — на статусе `failed` re-check 5 минут, далее
  бесконечное ожидание возврата в `processing`.
- **Аттачменты в `PostgresChannel`**: `_decode_media_from_db` корректно
  обрабатывает dict-entries `{filename, data}` и сохраняет оригинальные
  имена файлов в session cache. `_poll_once` добавляет
  `[Attachment: name (saved at path)]` к пользовательскому контенту.
  Исправлено разрешение workspace dir и санитизация session key для
  Windows-путей.
- **`database.py`** (навык): упрощён на 78 строк без изменения поведения
  (удалён мёртвый Schema cache).
- **`lib/session/pg_session_manager.py`**: удалён нерабочий `sys.path`
  hack, указывавший на несуществующий путь.
- **README/DEVELOPMENT**: устранены неточности — убраны упоминания
  удалённых навыков, исправлена заметка о GP-схеме, счётчик seed-записей,
  ссылки на бенчмарки, ссылка nanobot (была `opencode.ai`).
- **Тесты:** `test_exception` использует объект с `__getattr__`,
  поднимающим исключение (т.к. `_get` использует `getattr`, не `.get()`).
  Pre-resolve использует `patch.dict` для nanobot.
- `audit_memory_store._replace_locked` / `_upsert_locked`: добавлены транзакции
  `BEGIN/COMMIT/ROLLBACK` для DELETE + INSERT (ранее при ошибке INSERT
  таблица оставалась пустой). DDL (ALTER/DROP) остаётся вне транзакции
  (DuckDB не откатывает DDL).
- `db_loader.get_provider()` теперь fail-fast без инжекции — ранее молча
  создавал второй CacheProvider, что приводило к Windows-блокировкам файла.
- `db_loader._parse_parameters()` корректно обрабатывает `None` и пустые
  строки (раньше бросал `TypeError`).

### Renamed

- **Контракт LLM-провайдера нейтрализован.** Имя env-переменной для ключа
  провайдера сменено: `MISTRAL_API_KEY` → `LLM_API_KEY`. Больше нет привязки
  к конкретному вендору в имени переменной. Изменения:
  - `config.py`: ключ из любой непустой `SETTINGS.providers.*.api_key`
    всегда экспортируется как `LLM_API_KEY`.
  - `config.json`: `${MISTRAL_API_KEY}` → `${LLM_API_KEY}` в
    `providers.minimax.apiKey` и `providers.mistral.apiKey`.
  - `lib/services/config_service.py:_pre_resolve_env_refs`:
    `LLM_API_KEY` берётся из первой непустой `SETTINGS.providers.*.api_key`.
  - Дефолты `project.json` (`llm_provider`, `llm_model`, `llm_api_base`)
    нейтральны (`openai-compatible` / `gpt-4o-mini` / OpenAI URL); выбрать
    Mistral/MiniMax/etc. — задать `llm_api_base` явно.
  - `.secrets.env.example`: секция `# providers: mistral` →
    `# providers: llm` (любое имя секции допустимо).

  Действия при миграции:
  - В `.secrets.env` переименуйте секцию `# providers: mistral` в
    `# providers: llm` (необязательно, но рекомендуется для ясности).
  - Если вы где-то задавали `MISTRAL_API_KEY` в shell через `export` —
    переименуйте в `LLM_API_KEY`.
  - В `project.json` при необходимости укажите свой `llm_api_base`
    (например, `https://api.minimax.io/v1`).

### Security

- **`.gitignore` (новый, корневой):** защита `.secrets.env`
  (КРИТИЧНО — API-ключи и DSN), Python (`__pycache__/`, `*.pyc`),
  pytest/coverage, артефакты удалённых навыков, runtime
  (`workspace/data_store/`, `workspace/sessions/`), DuckDB, IDE.
- API-ключи вынесены из кода и конфигурации в `.secrets.env`.

### Tests

- **701 → 683 unit-теста** (`-18` после удаления `test_pg_agent_worker.py`).
- `+107` новых тестов в v2.0.0: `test_application_context.py`,
  `test_agent_factory.py`, `test_bus_factory.py`, `test_config_service.py`,
  `test_session_storage.py`, `test_runtime_patcher.py`,
  `test_transcription_service.py`, `test_channel_factory.py`,
  `test_subprocess_manager.py`, `test_preload_service.py`,
  `test_db_logging_service.py`, `test_hooks_database_logging.py`,
  `test_gateway_runner.py`, `test_shutdown_coordinator.py`,
  `test_console_loop.py`.
- Покрытие `audit_analyzer`: `TestSchema`, `TestReplace`,
  `TestSchemaAndResync`, `test_map_pg_type`, `publish()`,
  `on_sync_callback`.

### Migration notes

- Если у вас уже есть `session_manager.json` с плоскими `min_conn`/`max_conn`/
  `pool_timeout` — продолжает работать (legacy-fallback в `session_storage.py`).
  Рекомендуется перенести в `channels.postgres.pool.*` в `project.json`.
- Если вы переопределяли `~/.nanobot/vectors/audits_index` через свой скрипт —
  теперь значение по умолчанию другое (`workspace/data_store/vectors/audits_index`).
  Чтобы сохранить старое — задайте `vector_index_default_path` явно.
- **`agent_question_runs`: новые колонки `question`/`response`/`media`.**
  Для существующих установок выполните `create_logs_table.sql` (и
  `migrate_logs_v1.sql`) — они содержат идемпотентное `ADD COLUMN`; или
  вручную: `ALTER TABLE agent_question_runs ADD COLUMN IF NOT EXISTS
  question TEXT, ADD COLUMN IF NOT EXISTS response TEXT,
  ADD COLUMN IF NOT EXISTS media TEXT;`
- **Миграция `agent_`-префикса:** `sql/migrations/migrate_agent_table_names_v1.sql`
  переименовывает/переносит `public.predefined_scripts`,
  `public.conversation_messages`, `oarb.vector_index_config`,
  `oarb.vector_index_store` под новые имена с сохранением данных.
  Доменные таблицы навыка (`oarb.audits/violations/...`) не затрагиваются.

---

## [1.5.0] — 2026-07-22

### Added
- Векторные поисковые индексы перенесены из файлов в PostgreSQL/Greenplum:
  - `oarb.audit_vectors` — сырые эмбеддинги `REAL[]` с метаданными (строит `build_vectors.py`);
  - `oarb.vector_index_store` — сериализованный FAISS-индекс `BYTEA` (ищет `vector_mode.py`);
  - `oarb.vector_index_config` — конфигурация индексов (таблицы/колонки), чанкование, автосинхронизация.
  - Параметры `--top-k` / `--threshold` задаются аргументами CLI (`--index-name`), а не конфигом.
- DuckDB-кеш для `audit_analyzer` с фоновым обновлением (`in_memory_enabled`, `cache/audit_cache.duckdb`); `init`-режим загрузки кеша из PostgreSQL.
- Передача файлов между агентами через БД как base64 `data URL` вместо файловых ссылок.
- 75 unit-тестов по runner/gateway/streamlit (`tests/`), исправлены найденные баги.
- `requirements.txt` со всеми зависимостями.
- Инъекция провайдерских API-ключей из `.secrets.env` в конфиг на старте (совместимость с nanobot 0.2.2).
- Инструкция разработчика по векторным индексам (docs).

### Changed
- Конфигурация мигрирована из кода в `.env` (+ исправлена коллизия имени `scripts/config.py`); из `.env` в `.secrets.env` вынесены API-ключи.
- Реорганизована структура: модули `lib/channels` и `lib/session`, добавлены README для них; итоговое расположение `sql/session/`, `sql/channels/`, `scripts/`, `logs/`.
- README исправлен (неточности), добавлены README для `lib/channels` и `lib/session`.

### Fixed
- Совместимость с Greenplum 6.25: ручной UPSERT вместо `ON CONFLICT` в `PGSessionManager`.
- Хранение файлов сессии в `data_store/cache/sessions/{session_key}`.
- Убран `ThreadedConnectionPool` — вызывал double free на Windows с asyncio.
- DSN берётся из `gateway_settings.py`; retry LLM при 429; вывод реальной ошибки БД в fallback-сообщениях.
- gssencmode=disable для GP 6.25 / PG 9.4 (и URI, и key=value DSN; через kwargs `connect()`, а не модификацией строки).
- Отдельные счётчики retry в `_connect()`: 50 попыток для «too many connections», 15 для остальных.
- Исправлен индекс `parents` (3 вместо 2 — работает на всех версиях Python); при retry удаляется assistant-placeholder вместо установки `status='failed'`.

### Security
- API-ключи вынесены из кода и конфигурации в `.secrets.env` (файл в `.gitignore`).

---

## [1.4.0] — 2026-06-16

### Added
- Русские docstrings во всех `.py`.
- Хук `_run_sync` fallback для случая, когда нет event loop (Streamlit, CLI) — использует временный пул.

### Changed
- Стек БД переведён с `asyncpg` → `psycopg2`, API переведён с async на sync.
- Убран общий пул: каждый запрос создаёт и закрывает собственное подключение; удалён модуль `db_api`; импорты переведены на функции модульного уровня.
- `DB_RETRYABLE_ERRORS` экспортирован из `db.py` (убрана дубликация в `pg_session_manager`).
- Навык `db_analyzer` переименован в `audit_analyzer`; `config.json` грузится из папки `gateway.py`.

### Fixed
- Совместимость с PG 9.4 и GP 6.25: `DISTRIBUTED BY`, pgcrypto, schema-introspection; удалены все DDL (`ensure_tables`) — таблицы должны существовать заранее.
- Раздельные счётчики retry: `TooManyConnectionsError` — 50×, остальные ошибки — 10×.
- Таймаут 30с на `pool.acquire()` (канал больше не зависает); предотвращена утечка соединения в `_get_conn` при ошибке `_init_jsonb`.
- `ON CONFLICT` → `UPDATE+INSERT` для GP6; `IF NOT EXISTS` → проверки через `pg_catalog`; убраны касты `::jsonb` из DML; синтаксис `session ON CONFLICT` исправлен, `msg_timestamp` дедуплицирован.
- `pool_max_conn` снижен до 1 против «too many connections» на Greenplum.
- Streamlit ожидает ответ агента без `st.rerun` (обход лимита `maxReruns`).

### Removed
- Пул соединений (включая шаринг одного пула между async/sync через `run_coroutine_threadsafe`) — перевыделение ресурсов на каждый запрос.
- Все DDL и `::jsonb`-касты.

---

## [1.3.0] — 2026-06-10

### Added
- Единый слой БД `SharedDB` (один psycopg2-коннекшн с блокировкой) + конфигурируемый асинхронный пул (`min_size`/`max_size`); sync-методы используют отдельные подключения.
- HTTP **DB API Server** — доступ к PostgreSQL из любых процессов; автоочистка БД; поддержка DSN для subprocess-процессов.
- **Self-review** система: `ReviewAgentLoop`, `RepeatGuardHook`, навык response-verification; метаданные `_review` (quality, attempts, issues, tool_repeat_stopped).
  - Ревьюер разбит на 8 независимых проверок с русскими промптами; fast-path по приветствию; фиксы multi-turn контекста.
  - **Fresh Data Rule** — агент обязан делать свежие tool-вызовы, а не переиспользовать историю.
  - Check 1 (Tool Usage) — детект обхода инструментов и ответа «из памяти»; Check 3 (Error Honesty) — детект «нет данных» вместо реальных ошибок инструментов.
  - `on max_iterations` — подстановка ответа «could not get data» вместо галлюцинированного контента.
- `ToolAuditHook` — запись всех tool-вызовов (статус/ошибки/аргументы) в `metadata._tool_audit`; `ToolParamsHook` влит в него.
- **Benchmark-фреймворк**: русские YAML-элементы, хук-фиксы, поддержка `qwen3-coder`; `fix bechmark` на точке реза ветки.
- Нативный инструмент `db_analyzer` для gateway (с валидацией параметров predefined-скриптов и защитой от необработанных исключений; позже откатан в ветку).
- Streamlit запускается как subprocess вместе со всеми каналами; тонкий клиент через `conversation_messages` + единый `AgentLoop` в gateway.
- UI: file-based история по умолчанию (`--storage` для DB), сворачиваемое reasoning, отображение tool events, загрузка хуков.
- Redis-канал `redis_channel.py`; блок `session_manager` в конфиге (читается из сырого JSON в обход валидации Pydantic) + совместимость с PG 9.4.20.

### Changed
- `psycopg2`/`asyncpg` → единый `asyncpg SharedDB` для каналов, сессий, навыка и CLI (`:param` → `%s`).
- Единый DSN в `gateway_settings.py` (убраны дубликаты из навыка); унифицирована конфигурация gateway.
- `conversation_id` → `chat_id` для блокировок по чатам; per-chat locking.
- Убран `INDEX.json` — каждый результат сохраняется отдельным файлом; ограничение `MAX_OUTPUT` у ExecTool до 10M; `processing_timeout` 600 → 120 с.
- `_tool_events` → `_tool_audit` без дублирования; слияние `reasoning` и `_reasoning` в ключ `metadata.reasoning`.

### Fixed
- Двойное кодирование JSONB в postgres_channel (хелпер `_decode_jsonb`, backward compat для старых записей); JSONB-декодер в SharedDB (asyncpg возвращает `dict`).
- Путь workspace в data-analyzer и захардкоженный путь в e2e-тесте.
- Обработка переполнения диска в `_normalize_with_persist`; gateway обёрнут в автоперезапуск при краше; limit роста INDEX.json (preview убран).
- Транзакционный `_mark_failed`; гонка UPSERT в `PGSessionManager` (`ON CONFLICT`); соответствие `seed_messages.sql` DDL; исправлена двойная JSONB-кодировка в `pg_agent_worker`.
- `%s`-плейсхолдеры для asyncpg; `to` (/quote) очистка в навыке; не переконфигурировать SharedDB.
- postgres channel: поллинг, `allow_from`, `timezone.UTC`, создание каталогов.

### Removed
- WebSocket-канал (конфиг + примеры `gateway_settings`), `webui-dist/` (SPA) и код `_patch_webui_dist`, `patches/` (reviewer, review_agent_loop) — мёртвый код из benchmark-dev.
- Мёртвые файлы: `temp_loop.py`, `create_table.sql`, `test_file_*.py`, регенерированные артефакты workspace, `_tmp_checks.py`, `fibonacci.py`; `connection_string` из docstring.
- `ResponseReviewHook`, `INDEX.json`, `DbAnalyzerTool` (revert).

---

## [1.2.0] — 2026-05-29

### Added
- **Streamlit-чат** с live-отображением рассуждений агента (`streamlit_app.py`).
- CLI: стриминг reasoning и ответа в реальном времени (вывод tool-выводов скрыт).

### Changed
- PostgresChannel переведён на **единотабличную** архитектуру (`conversation_messages`) с батчингом reasoning и контролем конкурентности (макс. параллельных сообщений).
- Вывод CLI переписан: typewriter-эффект, хуки, константы конфигурации.

---

## [1.1.0] — 2026-05-27

### Changed
- Модель конфигурации обновлена до `gpt-oss:20b-cloud`; исправлены стрелочные символы в presentation-инструменте.
- `db_analyzer`: класс `Database`, кеш схемы, фильтр таблиц, прямой DSN; улучшенный формат схемы для LLM (`NOT NULL`, `varchar(N)`, `schema.table`).
- `cli_agent`: добавлены константы `_CONFIG_PATH` и `_WORKSPACE_DIR`; скан `workspace/skills/` на предмет `tool.py`.

### Fixed
- Отображение рассуждений в `cli_agent` — по-дельтам, без накопления, с Rich markup; устранено дублирование ответа; откат пере-скана навыков (два дублирующих коммита).
- Показ результатов tool-вызовов (`show_tool_results`).
- Исправлен остаток merge-конфликта в `config.json`; трекинг `config.json` (секреты санитизированы).

---

## [1.0.0] — 2026-05-27

### Added
- Навыки `db_analyzer` и `html_presentation_generator` (полный код, E2E-тесты, исправленный `.gitignore`); разрешение `vector_source`, JSON-safe вывод.
- CLI-режим vector: параметры `--top-k` и `--threshold` (примеры для Linux в SKILL.md).

### Changed
- CLI: `--params` поддерживает формат `key=value` (фикс кавычек для Windows); примеры в SKILL.md.


---