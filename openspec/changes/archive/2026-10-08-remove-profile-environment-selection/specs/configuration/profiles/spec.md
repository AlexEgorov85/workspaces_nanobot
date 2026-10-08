## ADDED Requirements

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

## MODIFIED Requirements

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

- **КОГДА** `project.json`, `config.json` и `.secrets.env` объединены
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

## REMOVED Requirements

### Requirement: Application entrypoint requires --profile

**Reason**: Требование утверждало, что ВСЕ application entrypoints
(`gateway.py`, `cli_agent.py`, `streamlit_app.py`) используют
идентичный контракт «требуется обязательный `--profile`». Это
противоречит зафиксированному решению: `cli_agent.py` имеет
фиксированный профиль `test` и `--profile` не принимает вообще.
Универсальный контракт требовал от CLI фиктивной универсальности и
создавал взаимоисключающие ожидания в тестах — `cli_agent.py` без
`--profile` должен был одновременно «успешно стартовать с профилем
test» и «упасть с `--profile is required`».

**Migration**: Контракт разделён на два явных требования. `gateway.py`
требует `--profile` (whitelist `prod` / `test`). `cli_agent.py`
использует фиксированный профиль `test` и отклоняет `--profile` с
`ConfigurationError` + exit 2. Требование заменено на «Application
entrypoint profile contract различает gateway и CLI».

### Requirement: All application entrypoints share identical lifecycle contract

**Reason**: Требование требовало от всех трёх entrypoints одинакового
поведения при отсутствии `--profile` (`ConfigurationError("--profile
is required")` + exit 2). Для `cli_agent.py` это неверно: CLI не имеет
выбора профиля, поэтому «отсутствие `--profile`» для него — норма, а
не ошибка. Унифицированное требование порождало неразрешимое
противоречие между двумя валидными моделями поведения CLI.

Общая часть контракта сохраняется: все entrypoints используют единый
error-translation (`ConfigurationError` → `exit 2` через boundary в
`main()`), lifecycle-gate `_initialize_settings` до чтения `SETTINGS`,
и единый pool разрешённых профилей `{prod, test}`.

**Migration**: Требование заменено на «Application entrypoint profile
contract различает gateway и CLI». Унифицированный error-lifecycle
остаётся нормой (см. `runtime/entrypoints`).

Streamlit-специфичная часть требования (guard против повторного
`_initialize_settings` при `st.rerun()`) сохраняется без изменений —
`streamlit_app.py` не меняется в этом change, его удаление —
отдельный change `remove-streamlit-runtime`.
