# Design — Baseline Configuration Profiles

## Context

The project ships two runtime profiles, `prod` and `test`. Each profile
selects a different set of PostgreSQL tables (test tables append a `_test`
suffix), a different cache root path, and a different channel pool size.
This split was introduced to allow the test suite to run against an isolated
schema without touching production data.

## Resolution flow

```
config.py::load_settings()
        │
        ├─ read project.json (mode-aware overlays via profiles/<mode>.jsonc)
        ├─ read config.json
        ├─ read .secrets.env
        │
        ▼
   SETTINGS.profile  ← string ("prod" | "test")
        │
        ▼
   ApplicationContext.start()
        │
        ├─ CacheProvider.__init__  ← reads SETTINGS.profile for cache root
        ├─ PostgresChannel.__init__ ← reads SETTINGS.profile for table suffix
        └─ AgentFactory(...)       ← no profile branching
```

Profile selection is performed **once**, at the boundary between
configuration resolution and runtime construction. After `start()`,
runtime components do not re-read `SETTINGS.profile`.

## Why this is the baseline

The implementation is already merged. This change records the design **as it
is** so that future modifications to profile resolution start from a known
specification, not from reading the code.

Future changes that alter profile semantics — adding a third profile, changing
the merge order, or moving resolution out of `config.py` — must be implemented
as separate OpenSpec changes that update this spec via deltas.

## Non-goals

- No refactor of profile resolution.
- No addition of new profiles.
- No change to the merge order of `project.json`, `config.json`, `.secrets.env`.
- No removal of the existing `profiles/` overlay directory.

## Cross-references

- `docs/PROFILES.md` — descriptive (current implementation).
- `docs/MIGRATION.md` — historical record of the prod/test introduction.
- `docs/TARGET_ARCHITECTURE.md` — permanent invariant: "profile is resolved at
  configuration time, business logic SHALL NOT contain profile-specific
  branches".
- `openspec/specs/configuration/profiles/spec.md` — canonical spec (target of
  this change's archive merge).
