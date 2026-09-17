# Runtime Context

## Purpose
Define the boundary between `ApplicationContext` (long-lived shared infrastructure) and per-session/per-execution state. The boundary ensures runtime infrastructure does not accumulate ephemeral data and that lifecycle transitions are deterministic.

## Source of Truth

- Permanent invariants: `docs/TARGET_ARCHITECTURE.md` (`ApplicationContext` section).
- Implementation: `docs/ARCHITECTURE.md` (descriptive).

## Requirements

### Requirement: Single shared infrastructure root

The system SHALL expose `ApplicationContext` as the single assembly point for runtime services.

#### Scenario: Services wired through ApplicationContext

- **WHEN** a runtime service is needed
- **THEN** it SHALL be obtained through `ApplicationContext` or its documented accessor, not constructed ad hoc.

### Requirement: Session state lives outside ApplicationContext

The system SHALL store per-session state (messages, metadata, per-turn deltas) in `PGSessionManager` or the channel layer, NOT in `ApplicationContext`.

#### Scenario: Session lookup

- **WHEN** session metadata is required
- **THEN** the system SHALL read it from `PGSessionManager`, not from `ApplicationContext` attributes.

### Requirement: Deterministic lifecycle

The system SHALL define a deterministic `ctx.start()` / `ctx.stop()` lifecycle whose ordering SHALL NOT depend on the caller or on the active profile.

#### Scenario: Independent lifecycle

- **WHEN** two callers invoke `ctx.start()` concurrently
- **THEN** the resulting lifecycle ordering SHALL be deterministic and SHALL NOT vary between runs.

## Negative Requirements

The system SHALL NOT:

- store per-session data on `ApplicationContext` (its lifetime outlives any single session).
- store runtime-wide configuration on a session or message object.
- introduce a parallel application context (no `ApplicationContext2`, no shadow registry, no override mechanism).
- introduce a fallback application-context path (no `try_new` then `legacy_new`).
- add a profile-specific branch in `ApplicationContext` (per `openspec/specs/configuration/profiles/spec.md`, profile is resolved at config time, business logic SHALL NOT branch on profile).
