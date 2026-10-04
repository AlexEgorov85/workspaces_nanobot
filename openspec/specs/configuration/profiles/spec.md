# Configuration Profiles (Профили конфигурации)

## Purpose

Определение того, как разрешается prod/test профиль проекта и правила, управляющие поведением на основе профиля. Разрешение профиля происходит во время загрузки конфигурации; бизнес-логика НЕ ДОЛЖНА ветвиться по профилю.

## Scope

`shared` — профиль разрешается агентом, а оверлей имён таблиц применяет платформа; расхождение имён валит старт
Реализация: `profiles/test.jsonc` + `mcp-platform/platform.json → profiles.test`

## Responsibility

Profiles отвечают за:
- определение механизма разрешения активного профиля (prod/test)
- установку правил применения profile-specific overlays
- запрет ветвления бизнес-логики по профилю

## Boundary

### Owns
- механизмом разрешения профиля на этапе загрузки конфигурации
- применением profile-specific overlays к config.json
- предоставлением resolved profile через SETTINGS для infrastructure

### Does Not Own
- бизнес-логикой, которая ветвится по профилю
- runtime-переключением профиля
- созданием новых профилей без OpenSpec change

### May Depend On
- config.json (базовая конфигурация)
- session_manager.json (локальные overrides)
- .secrets.env (секреты)

### Must Not Depend On
- runtime-компонентов (ApplicationContext, channels, services)
- бизнес-логики Skills/Tools

## Public Contract

Profile resolution предоставляет:
- разрешение активного профиля (prod/test) до инициализации runtime
- применение profile-specific overlays в документированном порядке
- доступ к resolved profile через SETTINGS для infrastructure code

## Requirements

### Requirement: Разрешение до инициализации runtime

The system SHALL resolve the active profile (`prod` or `test`)
BEFORE any runtime component is constructed. The profile SHALL be
obtained exclusively from the application entrypoint's explicit startup
contract — the `--profile` CLI argument for `gateway.py`, and the
entrypoint's own fixed contract for `cli_agent.py`. The system SHALL
NOT read the profile from any environment variable, configuration file,
or implicit default.

The active profile is chosen at the application startup boundary and,
once resolved, becomes part of immutable runtime configuration. No
internal component SHALL receive the profile as a constructor argument,
through an environment variable, or through re-resolution.

#### Scenario: Профиль разрешён при загрузке конфигурации

- **КОГДА** `config.json`, `session_manager.json` и `.secrets.env` объединены
- **ТОГДА** активный профиль ДОЛЖЕН быть разрешён и сохранён в `SETTINGS` до запуска любого другого runtime-кода

#### Scenario: Application entrypoint parses --profile

- **WHEN** an application entrypoint resolves its profile from its own
  startup contract (the `--profile` argument for `gateway.py`, the fixed
  `test` profile for `cli_agent.py`)
- **THEN** the resolved value SHALL be the active profile for the
  lifetime of the process
- **AND** profile-dependent runtime configuration (e.g.
  `SETTINGS["logging"]["db"]["table_name"]`) SHALL reflect that profile

#### Scenario: Application entrypoint without --profile fails fast

- **WHEN** `gateway.py` is invoked without `--profile`
- **THEN** the system SHALL raise
  `ConfigurationError("--profile is required")`
- **AND THEN** `gateway.py` SHALL exit the process with status code 2
- **AND THEN** no runtime component SHALL be constructed
- **AND THEN** `SETTINGS` SHALL NOT be exposed as resolved
  configuration

#### Scenario: CLI entrypoint works without --profile

- **WHEN** `cli_agent.py` is invoked without `--profile`
- **THEN** CLI SHALL NOT raise `ConfigurationError`
- **AND** CLI SHALL start normally with the fixed profile `test`

#### Scenario: Exit code 2 for unsupported profile

- **WHEN** `gateway.py` is invoked with `--profile=<unsupported>`
- **THEN** the system SHALL raise
  `ConfigurationError("--profile=<v> is not supported (allowed: prod, test)")`
- **AND THEN** `gateway.py` SHALL exit the process with status code 2

#### Scenario: Profile resolved at config load

- **WHEN** the application entrypoint has resolved its profile
  and called `config._initialize_settings(profile=<value>)` before
  any other runtime import
- **THEN** `SETTINGS` SHALL be fully constructed (with profile
  overlay applied) before `ApplicationContext.create()` is called
  and before any channel, service, or AgentLoop construction begins
- **AND THEN** `ApplicationContext.create()` SHALL NOT receive the
  profile as a parameter

#### Scenario: Environment variables do not participate in profile resolution

- **WHEN** a process is started with arbitrary environment variables
  whose names or values look like profile selection
- **AND** the entrypoint determines its profile from its explicit
  startup contract
- **THEN** the active profile SHALL be determined ONLY by that
  explicit startup contract
- **AND** no environment variable SHALL override or supply the active
  profile
- **AND** this scenario constrains ONLY **profile resolution**; other
  aspects of `SETTINGS` (secrets, `${VAR}` substitution, external URLs)
  MAY legitimately be influenced by environment variables

### Requirement: Profile overlays применяются в документированном порядке

Система ДОЛЖНА применять profile-specific overlays к `config.json` согласно документированному порядку слияния. При активном профиле `test` система ДОЛЖНА выбирать test-specific table suffixes (`*_test`) в пяти runtime-ключах (`channels.postgres.{table_name, messages_table, meta_table}`, `logging.db.{table_name, question_runs_table}`), НЕ ДОЛЖНА молча fallback на non-test имена, и для каждого из пяти разрешённых runtime-имён ДОЛЖНА существовать таблица `public.agent_*_test` в БД. Отсутствие таблицы SHALL приводить к ошибке выполнения первого же обращения канала/сервиса/инструмента, а не к тихой подмене на prod-таблицу. Test-таблицы создаются DDL из `sql/<domain>/create_public_agent_*_test.sql` и применяются через `python tools/apply_test_profile_tables.py` (или эквивалентный ручной psql-запуск тех же пяти скриптов). Отдельной миграции для них нет: они не входят в `sql/migrations/`.

#### Scenario: Test профиль разрешает test таблицы
- **КОГДА** активный профиль равен `test`
- **ТОГДА** должны быть выбраны test-specific table suffixes, и разрешение НЕ ДОЛЖНО молча fallback на non-test имена

#### Scenario: Тестовая среда запускается под профилем test
- **КОГДА** активный профиль равен `test` и в БД применена миграция V005
- **ТОГДА** первые обращения `PostgresChannel` (`agent_conversation_messages_test`), `SessionMirror` (`agent_session_meta_test`, `agent_session_messages_test`), `DbLoggingService` (`agent_gateway_logs_test`, `agent_question_runs_test`) SHALL завершаться без `relation does not exist`

### Requirement: Профиль доступен для infrastructure use

The system SHALL expose the resolved profile as part of `SETTINGS`.
The canonical access path for **new and modified code** is the
mapping access `SETTINGS["profile"]`. The compatibility
mechanism `_LazySettings.__getattr__` continues to expose
attribute access for existing consumers of generic attr access
on the configuration tree; this MAY include `SETTINGS.profile`
when `_inner_dict["profile"]` exists, since generic attr passthrough
is part of the proxy's documented contract for backward
compatibility. The exposed profile value SHALL reflect the CLI
argument that was passed to `_initialize_settings`, not any value
derived from environment variables or implicit defaults.

#### Scenario: Infrastructure читает профиль

- **КОГДА** connection helper нуждается в активном профиле
- **ТОГДА** он ДОЛЖЕН прочитать `SETTINGS.profile` и НЕ ДОЛЖЕН ветвиться по профилю в бизнес-логике

#### Scenario: Infrastructure reads profile

- **WHEN** a connection helper needs the active profile
- **THEN** it SHALL read `SETTINGS["profile"]` (mapping access) and
  SHALL NOT branch on profile in business logic

#### Scenario: Profile value reflects CLI resolution, not environment

- **WHEN** `SETTINGS["profile"]` is read
- **THEN** its value SHALL equal the CLI argument that was passed
  to `_initialize_settings`, not a value read from any environment
  variable

### Requirement: Test-only bootstrap of SETTINGS is permitted in `tests/conftest.py` as a documented exception

> **Reason:** Реализация ввела
> `tests/conftest.py::autouse`-фикстуру `_bootstrap_config_lifecycle`
> (коммит `a5b77b9`), которая вызывает
> `config._initialize_settings(profile="test")` для legacy-тестов,
> читающих `SETTINGS[...]` напрямую без явного init. Формально это
> нарушает исходный инвариант spec «autouse-fixture для
> `_initialize_settings` НЕ добавлен». После анализа (см.
> `tasks.md` § Anti-patterns DEVIATION) принято решение легитимизировать
> это как **test-only exception** с явными границами.

The original invariant required that no autouse-fixture
calls `_initialize_settings(profile)`. This requirement NARROWS
that prohibition to **production runtime code only** and
explicitly ALLOWS a single, well-bounded autouse-fixture in
`tests/conftest.py` with the following constraints:

- **Scope:** only in `tests/conftest.py` (not in any
  `workspace/`, `lib/`, `tools/`, `gateway.py`, `cli_agent.py`,
  `streamlit_app.py`).
- **Behavior:** the autouse-fixture MAY call
  `config._initialize_settings(profile="test")` once per pytest
  session **iff** `config.is_settings_initialized()` returns
  `False`. If the proxy is already initialized (e.g., by an
  earlier test that explicitly calls `_initialize_settings`),
  the fixture SHALL be a no-op.
- **No silent recovery:** the fixture SHALL NOT swallow
  `ConfigurationError` raised by `_initialize_settings` for
  reasons other than «already initialized». If whitelist
  validation fails or any other unexpected error occurs, the
  fixture SHALL propagate it.
- **No fallback semantics:** the fixture is bootstrap-only.
  Acceptance tests that explicitly verify lifecycle behavior
  (`tests/test_profile_lifecycle.py`) and subprocess-based
  entrypoint acceptance tests (`tests/test_gateway.py`,
  `tests/test_cli_agent.py`, `tests/test_streamlit_app.py`)
  MUST NOT depend on the fixture — they run in fresh
  subprocesses and verify behavior independently.
- **Documented intent:** `tests/conftest.py` MUST contain a
  comment block explaining why the fixture exists, the
  constraints above, and the reference to this requirement.

#### Scenario: Autouse-fixture initializes SETTINGS once per pytest session

- **WHEN** pytest collects and runs a test that reads
  `SETTINGS["..."]` without explicitly calling
  `_initialize_settings`
- **AND WHEN** `config.is_settings_initialized()` returns `False`
- **THEN** the autouse-fixture SHALL call
  `_initialize_settings(profile="test")` before the test body runs
- **AND THEN** `SETTINGS["profile"]` SHALL be `"test"` for the
  duration of the test
- **AND THEN** `_initialize_settings` SHALL NOT be called again
  by the fixture for subsequent tests in the same session
  (subsequent calls hit the `is_settings_initialized() == True`
  branch and become no-ops)

#### Scenario: Autouse-fixture is no-op when SETTINGS already initialized

- **WHEN** a test (e.g., `test_profile_lifecycle.py::test_double_init_fails`)
  has already called `_initialize_settings(profile="prod")`
- **AND WHEN** a subsequent test that depends on the fixture runs
  in the same session
- **THEN** the autouse-fixture SHALL skip its
  `_initialize_settings(...)` call
- **AND THEN** `SETTINGS["profile"]` SHALL remain `"prod"`
  (the profile is immutable post-init)

#### Scenario: Legacy tests rely on autouse-fixture

- **GIVEN** legacy tests that read `SETTINGS["..."]` directly
  (e.g., `tests/test_utils_db.py`, `tests/test_config.py`,
  `tests/test_application_context_logging.py`,
  `tests/test_storage_hybridization_factory.py`,
  `tests/integration/test_worker_pool_*.py`,
  `tests/integration/test_postgres_channel_lifecycle_stress.py`)
- **WHEN** pytest runs them without explicit
  `_initialize_settings` in the test body
- **THEN** the autouse-fixture SHALL provide `SETTINGS` so these
  tests can run without modification
- **AND THEN** the legacy tests SHALL NOT need their own
  `_initialize_settings` call

#### Scenario: Migration path to remove autouse-fixture

- **WHEN** all legacy tests that read `SETTINGS[...]` directly
  have been migrated to either: (a) explicit
  `_initialize_settings(profile="test")` in their setup, or (b)
  subprocess-based acceptance tests that verify behavior in a
  fresh process
- **THEN** the autouse-fixture in `tests/conftest.py` SHALL be
  removed
- **AND THEN** the original invariant
  («autouse-fixture НЕ добавлен») SHALL be restored without
  modification to this requirement
- **AND THEN** this requirement SHALL be reverted to
  its original wording via a new OpenSpec change

> **Migration note:** at the time of this requirement creation, ~12 test
> files in `tests/` and `tests/integration/` directly read
> `SETTINGS[...]` without explicit init. Migrating all of them
> is out of scope for this change (would require touching every
> legacy test). The autouse-fixture is the pragmatic bridge
> until a dedicated migration change is opened.

### Requirement: SETTINGS construction is explicit and order-checked

The system SHALL construct `SETTINGS` ONLY through
`config._initialize_settings(profile=...)`. Construction SHALL NOT
occur during `import config`. Access to `SETTINGS` before
`_initialize_settings` SHALL fail fast. There SHALL be no auto-init,
no implicit default, and no environment fallback. No module imported
by an application entrypoint before `_initialize_settings` SHALL
access resolved `SETTINGS` (import-order contract).

#### Scenario: SETTINGS construction is deferred past import

- **WHEN** `import config` executes
- **THEN** no profile SHALL be resolved
- **AND THEN** no configuration SHALL be constructed
- **AND THEN** no environment variable SHALL be read for the
  purpose of profile resolution

#### Scenario: SETTINGS access before initialization fails fast

- **WHEN** Python code accesses `config.SETTINGS[...]` before
  `config._initialize_settings(profile=...)` has been called
- **THEN** the access SHALL raise `ConfigurationError("SETTINGS not
  initialized: call _initialize_settings(profile=...) from the
  application entrypoint")`

#### Scenario: Double initialization fails

- **WHEN** `config._initialize_settings(profile="prod")` is called
- **AND WHEN** it is called a second time with any value
- **THEN** the second call SHALL raise
  `ConfigurationError("SETTINGS already initialized")`

#### Scenario: Import-order contract protects against early SETTINGS reads

- **WHEN** an application entrypoint imports a module before calling
  `_initialize_settings(profile=...)`
- **AND WHEN** that module attempts to read `config.SETTINGS`
- **THEN** the access SHALL raise the uninitialized `SETTINGS`
  error per the scenario «SETTINGS access before initialization
  fails fast»
- **AND THEN** the application entrypoint SHALL NOT have proceeded
  to runtime construction under an uninitialized profile

### Requirement: Profiles are limited to a whitelist

The system SHALL accept only the profiles `prod` and `test`. Any
other value SHALL cause startup to fail before any runtime
component is constructed and before `SETTINGS` is exposed as
resolved configuration. This rule applies both at the CLI parser
level (`--profile=<value>` validation in `gateway.py`) and at the
`_initialize_settings(profile=...)` level (defensive re-validation).

#### Scenario: Unsupported profile fails fast

- **WHEN** `gateway.py` is invoked with
  `--profile=staging` or `--profile=dev` or any value not in
  `{prod, test}`
- **THEN** the system SHALL raise
  `ConfigurationError("--profile=<value> is not supported (allowed:
  prod, test)")`
- **AND THEN** no runtime component SHALL be constructed
- **AND THEN** `SETTINGS` SHALL NOT be exposed as resolved
  configuration

#### Scenario: Whitelist is enforced at _initialize_settings

- **WHEN** `config._initialize_settings(profile="dev")` is called
- **THEN** the call SHALL raise
  `ConfigurationError("profile='dev' is not supported (allowed:
  prod, test)")`

### Requirement: Application entrypoint profile contract различает gateway и CLI

An **application entrypoint** is defined as a process entrypoint
that constructs the runtime via `ApplicationContext` and consumes
resolved `SETTINGS`. Entrypoints SHALL NOT share an identical
`--profile` contract; the contract is defined per entrypoint.

`gateway.py` SHALL require `--profile=<value>` as a CLI argument.
`cli_agent.py` SHALL NOT accept `--profile` and SHALL always use the
fixed profile `test`. A standalone utility that does not construct the
runtime is NOT required to accept or propagate `--profile`; its own
contract governs whether and how it consumes configuration.

Environment variables SHALL NOT be a source of profile selection for
any entrypoint.

#### Scenario: Gateway requires --profile

- **WHEN** `gateway.py` запускается без `--profile`
- **THEN** the system SHALL raise
  `ConfigurationError("--profile is required")`
- **AND THEN** no runtime component SHALL be constructed

#### Scenario: CLI rejects --profile

- **WHEN** `cli_agent.py` запускается с `--profile=<value>` (в любой
  форме, включая short forms)
- **THEN** the system SHALL raise `ConfigurationError` с сообщением,
  указывающим, что CLI использует фиксированный профиль `test`
- **AND THEN** `cli_agent.py` SHALL завершить процесс с кодом 2
- **AND** процесс MUST NOT запускать `ApplicationContext`

#### Scenario: Standalone utility does not require --profile

- **WHEN** a standalone utility (e.g. `tools/build_vectors.py`)
  is invoked directly
- **AND WHEN** it does not consume resolved `SETTINGS` at its
  module level
- **THEN** its invocation contract is independent of `--profile`

#### Scenario: Streamlit invocation is explicitly defined

- **WHEN** Streamlit is invoked as
  `streamlit run streamlit_app.py -- --profile=prod`
- **THEN** the resolved profile SHALL be `prod`
- **AND THEN** the supported invocation pattern is exactly that
  form (no other invocation pattern is part of this change)

### Requirement: Integration test verifies runtime configuration, not only banner

The change SHALL include an end-to-end test that verifies a
profile-dependent runtime setting (for example, the name of a
runtime table such as `agent_gateway_logs` vs `agent_gateway_logs_test`)
matches the `--profile` argument after a full application start,
NOT only the banner string. This protects against the historical
failure mode where the banner said `prod` but the runtime used
`test`-suffixed tables.

#### Scenario: prod profile selects prod-suffixed tables

- **WHEN** `python gateway.py --profile=prod` is invoked
- **THEN** `SETTINGS["logging"]["db"]["table_name"]` SHALL equal
  `agent_gateway_logs`
- **AND THEN** a `history_search` call against that table SHALL
  address `public.agent_gateway_logs` (not `*_test`)

#### Scenario: test profile selects test-suffixed tables

- **WHEN** `python gateway.py --profile=test` is invoked
- **THEN** `SETTINGS["logging"]["db"]["table_name"]` SHALL equal
  `agent_gateway_logs_test`

#### Scenario: Profile comes only from --profile, table selection follows

- **WHEN** arbitrary unrelated environment variables are set in the
  process environment alongside `python gateway.py --profile=prod`
- **THEN** `SETTINGS["logging"]["db"]["table_name"]` SHALL equal
  `agent_gateway_logs` (prod), not `agent_gateway_logs_test`
- **AND THEN** the banner SHALL also reflect `prod`
- **AND THEN** both indicators SHALL agree

### Requirement: Profile is passed to application subprocesses only through --profile

For every subprocess spawned by application runtime that is itself
an application entrypoint (`gateway.py`, `cli_agent.py`,
`streamlit_app.py`), the parent SHALL pass the active profile as
an explicit `--profile=<value>` CLI argument. The profile value
SHALL be derived from `SETTINGS["profile"]`. The profile SHALL
NOT be transported via environment variables, files, IPC, or any
other side-channel.

#### Scenario: Application subprocess receives --profile explicitly

- **WHEN** the application runtime spawns an application entrypoint
  subprocess
- **THEN** the parent's `argv` for the child SHALL include
  `--profile=<value>` matching `SETTINGS["profile"]`

#### Scenario: Profile source is parent's SETTINGS, not env

- **WHEN** the application runtime constructs the child's `argv`
- **THEN** the profile value SHALL be read from
  `SETTINGS["profile"]`
- **AND THEN** no second `_resolve_mode`, no env lookup, no default
  SHALL be invoked at the boundary

### Requirement: Streamlit st.rerun does not trigger "already initialized"

`streamlit_app.py` SHALL guard its module-level
`_initialize_settings(...)` call with a `_initialized` flag set on the
module itself after the first successful call.

**Rationale:** Streamlit's runpy-based execution re-executes the script
on `st.rerun()`, so module-level code in `streamlit_app.py` runs
multiple times within a single process. To honor both the Streamlit
lifecycle and the strict «second call → already initialized» contract
of `config._initialize_settings`, the guard prevents the second CALL
from happening; the `_initialize_settings` function itself stays
strict.

**This guard is not auto-init, profile switching, or a fallback** — it
is explicit protection against Streamlit's physical re-execution of the
script body.

`streamlit_app.py` receives its profile through argv
(`streamlit run streamlit_app.py -- --profile=<value>`), NOT through
environment variables. `streamlit_app.py` is out of scope for this
change beyond this documented contract; its removal is tracked by the
separate change `remove-streamlit-runtime`.

#### Scenario: Streamlit st.rerun does not trigger "already initialized"

- **WHEN** `streamlit run streamlit_app.py -- --profile=prod`
  succeeds and `_initialize_settings("prod")` is called once
- **AND WHEN** `st.rerun()` re-executes the script body
- **THEN** the guard SHALL skip the second `_initialize_settings(...)`
  call
- **AND THEN** the `_initialize_settings` function SHALL NOT have
  been called a second time within this process
- **AND THEN** the application SHALL continue running with the
  already-published `SETTINGS`
- **AND THEN** `SETTINGS["profile"]` SHALL continue to be `"prod"`

### Requirement: CLI entrypoint имеет фиксированный профиль test

`cli_agent.py` MUST NOT принимать `--profile` CLI-аргумент и MUST NOT читать профиль из переменных окружения. CLI MUST использовать фиксированный профиль `test` при вызове `config._initialize_settings(profile="test")`.

Это требование вытекает из принципа: CLI — локальный test/dev entrypoint, а не production deployment interface. CLI не должен создавать ложную универсальность через `--profile`/`--profile=prod`/`--profile=staging`. Это уменьшает поверхность конфигурации и количество комбинаций для тестирования.

Gateway entrypoint `gateway.py` MAY принимать `--profile`; это требование CLI не распространяется на gateway.

"test" в контексте CLI НЕ означает урезанный runtime: CLI MUST иметь тот же AgentLoop, Skills, Tools, DuckDB, Vector search, Memory, Logging, Prompts, Runtime patches, что и gateway. Различие только в:
- profile (CLI == "test" fixed, gateway == "test" или "prod" в зависимости от `--profile`);
- transport (CLI == in-memory bus, gateway == PostgresChannel).

После `config._initialize_settings(profile="test")` runtime-компоненты НЕ ДОЛЖНЫ ветвиться по `profile == "test"` — выбор профиля происходит только на этапе resolution, не в runtime-коде.

`ApplicationContext.create()` MUST NOT принимать `profile` как параметр. Профиль MUST быть разрешён ДО `create()` через `_initialize_settings(profile=...)`. После resolution `ApplicationContext` получает профиль через `SETTINGS["profile"]`, не через constructor-arg.

#### Scenario: CLI не принимает --profile

- **WHEN** пользователь запускает `python cli_agent.py --profile=test`
- **THEN** CLI MUST отклонить флаг с `ConfigurationError`
- **AND** процесс MUST NOT запускать ApplicationContext
- **AND** `cli_agent.py` MUST завершиться с кодом 2

#### Scenario: CLI использует profile="test" по умолчанию

- **WHEN** пользователь запускает `python cli_agent.py` (без `--profile`)
- **THEN** CLI MUST вызвать `config._initialize_settings(profile="test")` (fixed)
- **AND** `SETTINGS["profile"]` MUST быть `"test"`
- **AND** процесс MUST NOT требовать `--profile` для старта

#### Scenario: CLI не читает профиль из переменных окружения

- **WHEN** `cli_agent.py` запускается в окружении, содержащем
  произвольные переменные, чьи имена или значения выглядят как
  выбор профиля
- **THEN** CLI MUST игнорировать эти переменные
- **AND** `SETTINGS["profile"]` MUST быть `"test"`
- **AND** этот сценарий ограничивает ТОЛЬКО выбор профиля; секреты и
  `${VAR}` substitution MAY legitimately читать environment

#### Scenario: ApplicationContext.create() не принимает profile

- **WHEN** application entrypoint вызывает `ApplicationContext.create(...)`
- **THEN** он MUST NOT передавать `profile` как параметр
- **AND** `inspect.signature(ApplicationContext.create)` MUST NOT содержать `profile` в `parameters`
- **AND** передача `profile=` через `**kwargs` MUST приводить к `TypeError`

#### Scenario: Runtime не ветвится по profile

- **WHEN** runtime-компонент (например, `lib/services/db_logging_service.py`) выполняет код
- **THEN** компонент НЕ ДОЛЖЕН содержать `if profile == "test"` или аналогичных branch'ей
- **AND** компонент получает уже resolved `SETTINGS` и работает с конфигурацией напрямую

#### Scenario: Gateway сохраняет --profile механизм

- **WHEN** пользователь запускает `python gateway.py --profile=prod` или `python gateway.py --profile=test`
- **THEN** gateway MUST принять `--profile` (текущее поведение сохраняется)
- **AND** `SETTINGS["profile"]` MUST соответствовать переданному значению

## Forbidden Behavior

Система НЕ ДОЛЖНА:

- содержать ветки `if profile == "prod"` / `if profile == "test"` в бизнес-логике
- fallback на "профиль по умолчанию" если разрешение профиля не удалось (fail fast на misconfiguration)
- позволять переключение профиля на runtime (после разрешения конфигурации)
- молча игнорировать неизвестные profile keys
- создавать третий профиль (`dev`, `staging`, etc.) без явного OpenSpec change

## Dependencies

- `docs/TARGET_ARCHITECTURE.md` — глобальные архитектурные принципы
- `config.json` — базовая конфигурация с профилями
- `lib/services/config_service.py:ConfigService` — реализация разрешения

## Configuration

Профили определяются в `config.json`:

```json
{
  "profiles": {
    "prod": { ... },
    "test": { ... }
  }
}
```

Разрешение происходит через:
1. Базовый profile из config.json
2. Overrides из session_manager.json
3. Переменные окружения из .secrets.env

## Lifecycle

1. **Загрузка**: config.json читается при старте
2. **Разрешение**: активный профиль определяется из явного startup-контракта entrypoint (argv `--profile` для `gateway.py`; фиксированный `test` для `cli_agent.py`)
3. **Слияние**: profile-specific overlays применяются к базовой конфигурации
4. **Фиксация**: resolved profile сохраняется в SETTINGS
5. **Использование**: infrastructure читает SETTINGS.profile при необходимости

## State

Resolved profile хранится в SETTINGS как строка (`"prod"` или `"test"`).

## Invariants

- Профиль разрешается ровно один раз при старте
- После разрешения профиль не изменяется
- Бизнес-логика не ветвится по профилю
- Infrastructure использует профиль только для конфигурации

## Error Behavior

- Ошибка разрешения профиля → fail fast, система не запускается
- Неизвестный профиль → ошибка валидации конфигурации

## Consumers

- ConfigService — разрешение конфигурации
- Database connection helpers — выбор таблиц/суффиксов
- CacheProvider — выбор путей кеша
- ChannelManager — конфигурация каналов

## Implementation

Основная реализация:
- `lib/services/config_service.py:ConfigService`

Связанные компоненты:
- `config.json` — определение профилей
- `docs/PROFILES.md` — описание реализации

## Verification

Валидация включает:
1. Проверка отсутствия `if profile ==` в бизнес-логике (code review, grep)
2. Проверка fail fast behaviour при misconfiguration (тесты)
3. Проверка однократного разрешения профиля (тесты)
