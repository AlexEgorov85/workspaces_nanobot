## MODIFIED Requirements

### Requirement: Resolution happens before runtime initialization

The system SHALL resolve the active profile (`prod` or `test`) during
configuration resolution, BEFORE any runtime component
(`ApplicationContext`, channels, services) is constructed. The profile
SHALL be resolved from the CLI argument `--profile` exclusively; the
system SHALL NOT read the profile from any environment variable.

#### Scenario: Profile resolved at config load from CLI argument

- **WHEN** `project.json`, `config.json`, and `.secrets.env` are merged
- **AND WHEN** the entry point has parsed `--profile=<value>` from `argv` BEFORE any runtime import
- **THEN** the resolved profile SHALL be `<value>` and SHALL be stored on `SETTINGS` before any other runtime code runs

#### Scenario: Entry point without --profile fails fast

- **WHEN** the entry point is invoked without `--profile` and no `_initialize_settings(profile=...)` call has been made
- **THEN** the system SHALL raise `ConfigurationError("--profile is required")` before any runtime component is constructed

#### Scenario: Environment variable profile is ignored

- **WHEN** the entry point is invoked with `--profile=test` and `NANOBOT_PROFILE=prod` is set in the process environment
- **THEN** the resolved profile SHALL be `test` (CLI argument wins; environment variable SHALL NOT influence the resolution)

#### Scenario: Profile resolved at config load

- **WHEN** the entry point has parsed `--profile=<value>` from `argv` and called `config._initialize_settings(profile=<value>)` before any other runtime import
- **THEN** `SETTINGS` SHALL be fully constructed (with profile overlay applied) before `ApplicationContext.create()` is called and before any channel, service, or AgentLoop construction begins

### Requirement: Profile surfaced for infrastructure use

The system SHALL expose the resolved profile as part of `SETTINGS` for
use by infrastructure code (DB connection helpers, cache paths, channel
configuration) ONLY. The exposed profile value SHALL reflect the CLI
argument that was passed to `_initialize_settings`, not any value
derived from environment variables.

#### Scenario: Infrastructure reads profile

- **WHEN** a connection helper needs the active profile
- **THEN** it SHALL read `SETTINGS.profile` and SHALL NOT branch on profile in business logic

#### Scenario: Profile value reflects CLI resolution, not environment

- **WHEN** `SETTINGS.profile` is read
- **THEN** its value SHALL equal the CLI argument that was passed to `_initialize_settings`, not a value read from any environment variable

## ADDED Requirements

### Requirement: Profile resolution happens before SETTINGS construction

The system SHALL resolve the active profile from the CLI argument
BEFORE constructing `SETTINGS`. `SETTINGS` SHALL NOT be built at module
import time. The first read of `SETTINGS` SHALL trigger configuration
construction, and that construction SHALL require an explicit resolved
profile to have been registered with
`config._initialize_settings(profile=...)`.

#### Scenario: SETTINGS construction is lazy

- **WHEN** `import config` executes
- **THEN** no profile is resolved, no `SETTINGS` dict is constructed, and no profile-related environment variable is read

#### Scenario: First SETTINGS access without initialization fails fast

- **WHEN** Python code accesses `config.SETTINGS[...]` before `config._initialize_settings(profile=...)` has been called from an entry point
- **THEN** the access SHALL raise `ConfigurationError("SETTINGS not initialized: call _initialize_settings(profile) from the entry point")`

#### Scenario: Entry point initializes SETTINGS before first access

- **WHEN** `gateway.py`, `cli_agent.py`, or `streamlit_app.py` starts and parses `--profile` before any other runtime import
- **THEN** `config._initialize_settings(profile=...)` SHALL be called synchronously, before any code reads `config.SETTINGS`

### Requirement: Subprocess must not inherit profile from environment

The system SHALL NOT transmit any profile-related environment variable
to subprocesses spawned by runtime tools (notably `tools.exec`). A
subprocess that needs to act under a specific profile SHALL receive
`--profile=<value>` as part of its `command` argument; if no profile is
passed, the subprocess SHALL fail with `ConfigurationError` per the
entry-point contract rather than silently inheriting a profile from a
leaked environment variable.

#### Scenario: Subprocess without --profile fails

- **WHEN** a runtime tool invokes `exec` with a `command` that runs a Python entry point
- **AND WHEN** the `command` does not include `--profile=...`
- **THEN** the subprocess SHALL exit with code 2 and `ConfigurationError`; the parent shell SHALL NOT set `NANOBOT_PROFILE` in the child environment as a workaround

## REMOVED Requirements

### Requirement: Profile resolved at config load via environment fallback
**Reason**: The legacy contract allowed `NANOBOT_PROFILE` env var to set
the active profile silently when CLI `--profile` was absent. This
created duplication of source of truth and a silent failure mode where
`--profile=prod` after a `test`-defaulted environment produced a banner
that did not match runtime behaviour (`SETTINGS` table names already
locked to `_test`).
**Migration**: Replace any usage of `NANOBOT_PROFILE` env var with
explicit `--profile=<value>` argument passed to the entry point
command. This applies to `docker-compose.yml`, `k8s` manifests,
`systemd` units, GitHub Actions jobs, and any other deployment
descriptors. Code that currently relies on env-fallback (most
prominently the module-level `SETTINGS` build in `config.py:517-518`)
is replaced by lazy `SETTINGS` initialization via
`config._initialize_settings(profile=...)` called from each entry
point.
