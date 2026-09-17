## ADDED Requirements

### Requirement: Active profile resolved at configuration load

The system SHALL resolve the active profile (`prod` or `test`) during
configuration resolution, before any runtime component is constructed.

#### Scenario: Profile resolved before ApplicationContext.start

- **WHEN** the merged configuration is read by `config.py`
- **THEN** the active profile SHALL be selected from `project.json`'s
  `profiles.<mode>` overlay (or equivalent key) and exposed via `SETTINGS`
  before `ApplicationContext.start()` is called.

### Requirement: Profile drives infrastructure only, not business logic

The system SHALL consume the resolved profile from `SETTINGS` only inside
infrastructure code (DB connection helpers, cache path resolution, channel
configuration).

#### Scenario: Skill reads only behavior, not profile

- **WHEN** a Skill's script needs to decide what data to read or how to format
  output
- **THEN** it SHALL read behavior from configuration values that have already
  been resolved for the active profile; it SHALL NOT itself inspect
  `SETTINGS.profile` to branch behavior.

### Requirement: No silent fallback on profile misconfiguration

The system SHALL fail fast with a clear error if profile resolution is
ambiguous or if a required profile-specific key is missing.

#### Scenario: Missing test-table declaration

- **WHEN** the active profile is `test` and a required `*_test` table name is
  not declared for a feature
- **THEN** startup SHALL fail with a configuration error rather than silently
  falling back to the prod table name.

## Negative Requirements

The system SHALL NOT:

- contain `if profile == "prod"` / `if profile == "test"` branches in
  business logic (Skill scripts, channel handlers, gateway logic).
- introduce a third profile (`dev`, `staging`, etc.) without an explicit
  OpenSpec change.
- allow profile switching at runtime after configuration resolution.
- silently ignore unknown profile keys.
