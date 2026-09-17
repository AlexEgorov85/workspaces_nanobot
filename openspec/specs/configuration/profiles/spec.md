# Configuration Profiles

## Purpose
Define how the project's prod/test profile is resolved and the rules that govern profile-driven behavior. Profile resolution happens at configuration load time; business logic SHALL NOT branch on profile.

## Source of Truth

- Permanent invariants: `docs/TARGET_ARCHITECTURE.md` (Profiles section).
- Implementation: `docs/PROFILES.md` (descriptive).

## Requirements

### Requirement: Resolution happens before runtime initialization

The system SHALL resolve the active profile (`prod` or `test`) during configuration resolution, BEFORE any runtime component (`ApplicationContext`, channels, services) is constructed.

#### Scenario: Profile resolved at config load

- **WHEN** `project.json`, `config.json`, and `.secrets.env` are merged
- **THEN** the active profile SHALL be resolved and stored on `SETTINGS` before any other runtime code runs.

### Requirement: Profile overlays applied in documented order

The system SHALL apply profile-specific overlays onto `project.json` according to the documented merge order.

#### Scenario: Test profile resolves test tables

- **WHEN** the active profile is `test`
- **THEN** test-specific table suffixes SHALL be selected where applicable, and the resolution SHALL NOT silently fall back to non-test names.

### Requirement: Profile surfaced for infrastructure use

The system SHALL expose the resolved profile as part of `SETTINGS` for use by infrastructure code (DB connection helpers, cache paths, channel configuration) ONLY.

#### Scenario: Infrastructure reads profile

- **WHEN** a connection helper needs the active profile
- **THEN** it SHALL read `SETTINGS.profile` and SHALL NOT branch on profile in business logic.

## Negative Requirements

The system SHALL NOT:

- contain `if profile == "prod"` / `if profile == "test"` branches in business logic.
- fall back to a "default profile" if profile resolution fails (fail fast on misconfiguration).
- allow profile switching at runtime (after configuration resolution).
- silently ignore unknown profile keys.
- introduce a third profile (`dev`, `staging`, etc.) without an explicit OpenSpec change.
