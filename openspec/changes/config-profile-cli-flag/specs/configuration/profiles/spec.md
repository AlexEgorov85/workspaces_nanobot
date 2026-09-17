## Purpose

Этот delta специфицирует жёсткий lifecycle для профиля конфигурации,
закрытое множество поддерживаемых профилей (`prod`, `test`), и
запрет на профильный environment fallback. Все существующие
`from config import SETTINGS` импорты продолжают работать через
compatibility proxy без изменений.

## Error Lifecycle Contract

Единый контракт обработки ошибок профиля разделяет **внутренний
exception contract** от **внешнего CLI contract**:

```text
argv
  ↓
application entrypoint CLI parser
  ├── missing --profile      → ConfigurationError("--profile is required")
  └── --profile not in whitelist → ConfigurationError("--profile=<v> is not supported (allowed: prod, test)")

import config
  ↓
config._initialize_settings(profile)
  ├── profile not in whitelist → ConfigurationError (defensive re-validation)
  └── SETTINGS already initialized → ConfigurationError("SETTINGS already initialized")

SETTINGS access before initialization
  └── → ConfigurationError("SETTINGS not initialized: call _initialize_settings(profile)")

application entrypoint top-level
  └── translates any startup ConfigurationError → process exit code 2
```

Принципы:

1. `ConfigurationError` — единственный тип исключения для всех ошибок профиля и инициализации конфигурации.
2. **Внутренний контракт** (между entrypoint-кодом и `config`-модулем) — exception. Любой код, вызывающий `_initialize_settings(...)` или читающий `SETTINGS`, может перехватить `ConfigurationError` программно (полезно для тестов и embedded-сценариев).
3. **Внешний контракт** (CLI поведение для пользователя) — process exit code 2 при любой startup `ConfigurationError`. Entry-points не должны «глотать» эти ошибки и валиться позже с непонятным trace.
4. `task B.1.1` НЕ должен сокращать «ConfigurationError → exit 2» в пользу «просто `sys.exit(2)` без exception». Оба шага обязательны: `_initialize_settings` бросает `ConfigurationError`; entrypoint top-level ловит и превращает в exit 2.

## MODIFIED Requirements

### Requirement: Resolution happens before runtime initialization

The system SHALL resolve the active profile (`prod` or `test`)
BEFORE any runtime component is constructed. The profile SHALL be
obtained exclusively from the `--profile` CLI argument passed to
the application entrypoint. The system SHALL NOT read the profile
from any environment variable, configuration file, or implicit
default.

#### Scenario: Application entrypoint parses --profile

- **WHEN** an application entrypoint (`gateway.py`, `cli_agent.py`,
  `streamlit_app.py`) parses `--profile=<value>` from `argv`
- **THEN** `<value>` SHALL be the active profile for the lifetime of
  the process

#### Scenario: Application entrypoint without --profile fails fast

- **WHEN** an application entrypoint is invoked without `--profile`
- **THEN** the system SHALL raise
  `ConfigurationError("--profile is required")`
- **AND THEN** the application entrypoint top-level SHALL exit the
  process with status code 2
- **AND THEN** no runtime component SHALL be constructed
- **AND THEN** `SETTINGS` SHALL NOT be exposed as resolved
  configuration

#### Scenario: Exit code 2 for unsupported profile

- **WHEN** an application entrypoint is invoked with
  `--profile=<unsupported>`
- **THEN** the system SHALL raise
  `ConfigurationError("--profile=<v> is not supported (allowed: prod, test)")`
- **AND THEN** the application entrypoint top-level SHALL exit the
  process with status code 2

#### Scenario: Profile resolved at config load

- **WHEN** the application entrypoint has parsed `--profile=<value>`
  and called `config._initialize_settings(profile=<value>)` before
  any other runtime import
- **THEN** `SETTINGS` SHALL be fully constructed (with profile
  overlay applied) before `ApplicationContext.create()` is called
  and before any channel, service, or AgentLoop construction begins

#### Scenario: Environment variables do not participate in profile resolution

- **WHEN** arbitrary environment variables are present in the
  process environment
- **AND WHEN** the application entrypoint is invoked with
  `--profile=test`
- **THEN** the resolved profile SHALL be `test`
- **AND THEN** profile-dependent runtime configuration (e.g.
  `SETTINGS["logging"]["db"]["table_name"]`) SHALL reflect `test`
- **AND** this scenario constrains ONLY **profile resolution**
  specifically; other aspects of `SETTINGS` are out of scope for
  this change and may legitimately be influenced by environment
  variables used for other purposes (secrets, external service URLs,
  etc.)

### Requirement: Profile surfaced for infrastructure use

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

#### Scenario: Infrastructure reads profile

- **WHEN** a connection helper needs the active profile
- **THEN** it SHALL read `SETTINGS["profile"]` (mapping access) and
  SHALL NOT branch on profile in business logic

#### Scenario: Profile value reflects CLI resolution, not environment

- **WHEN** `SETTINGS["profile"]` is read
- **THEN** its value SHALL equal the CLI argument that was passed
  to `_initialize_settings`, not a value read from any environment
  variable

## ADDED Requirements

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
level (`--profile=<value>` validation in application entrypoints)
and at the `_initialize_settings(profile=...)` level (defensive
re-validation).

#### Scenario: Unsupported profile fails fast

- **WHEN** the application entrypoint is invoked with
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

### Requirement: Application entrypoint requires --profile

An **application entrypoint** is defined as a process entrypoint
that constructs the runtime via `ApplicationContext` and consumes
resolved `SETTINGS`. In this change, the application entrypoints
are `gateway.py`, `cli_agent.py`, and `streamlit_app.py`. These
entrypoints SHALL require `--profile=<value>` as a CLI argument.
A standalone utility that does not construct the runtime is NOT
required to accept or propagate `--profile`; its own contract
governs whether and how it consumes configuration.

#### Scenario: Application entrypoint without --profile fails

- **WHEN** `gateway.py`, `cli_agent.py`, or `streamlit_app.py` is
  invoked without `--profile`
- **THEN** the system SHALL raise
  `ConfigurationError("--profile is required")`
- **AND THEN** no runtime component SHALL be constructed

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

### Requirement: All application entrypoints share identical lifecycle contract

The three application entrypoints (`gateway.py`, `cli_agent.py`,
`streamlit_app.py`) SHALL follow the SAME lifecycle and the SAME
error-translation contract. No entrypoint SHALL define a private
exception policy. Differences between entrypoints are limited to
how argv is sourced (CLI argparse vs Streamlit's `--` passthrough);
the error and initialization contract is identical.

**Streamlit-specific note:** Streamlit's runpy-based execution
re-executes the script on `st.rerun()`, so module-level code in
`streamlit_app.py` runs multiple times within a single process.
To honor both the Streamlit lifecycle and this change's strict
«second call → already initialized» contract, `streamlit_app.py`
SHALL guard its module-level `_initialize_settings(...)` call with
a `_initialized` flag set on the module itself after the first
successful call. The guard prevents the second CALL from
happening; the `_initialize_settings` function itself stays
strict. **This guard is not auto-init, profile switching, or a
fallback — it is explicit protection against Streamlit's
physical re-execution of the script body.**

#### Scenario: All entrypoints fail with exit code 2 without --profile

- **WHEN** each of `gateway.py`, `cli_agent.py`, `streamlit_app.py`
  is invoked without `--profile`
- **THEN** each SHALL raise `ConfigurationError("--profile is required")`
- **AND THEN** each SHALL exit the process with status code 2

#### Scenario: All entrypoints fail with exit code 2 with invalid profile

- **WHEN** each of `gateway.py`, `cli_agent.py`, `streamlit_app.py`
  is invoked with `--profile=<unsupported>`
- **THEN** each SHALL raise `ConfigurationError("--profile=<v> is not supported (allowed: prod, test)")`
- **AND THEN** each SHALL exit the process with status code 2

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

## REMOVED Requirements

### Requirement: Profile resolved at config load via environment fallback
**Reason**: The legacy contract allowed the previously-named
environment variable (referred to in older deployment descriptors)
to set the active profile silently when CLI `--profile` was absent.
This created duplication of source of truth and a silent failure
mode where `--profile=prod` after a defaulted environment produced
a banner that did not match runtime behaviour.
**Migration**: Replace any usage of that legacy env var with
explicit `--profile=<value>` argument passed to the application
entrypoint command. This applies to `docker-compose.yml`, `k8s`
manifests, `systemd` units, GitHub Actions jobs, and any other
deployment descriptors. Code that currently relies on env-fallback
(most prominently the module-level `SETTINGS` build in
`config.py:517-518`) is replaced by explicit
`config._initialize_settings(profile=...)` called from each
application entrypoint before any other runtime import.
