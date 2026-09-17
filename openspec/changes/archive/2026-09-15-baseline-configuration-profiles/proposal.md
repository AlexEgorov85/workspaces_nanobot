## Why

The project recently introduced explicit `prod` / `test` configuration profiles
resolved at configuration load time (see `docs/MIGRATION.md`, `docs/PROFILES.md`,
and the `profiled/` overlay machinery in `config.py` / `profiles/`). The change
is functionally complete but, until now, the project has no Spec-Driven
Development workflow in place. This change exists to exercise the OpenSpec
workflow on a known outcome — the resolved configuration profile behavior — so
that the workflow itself can be validated end-to-end before any new change is
attempted.

This is **not** an implementation change. No code, configuration, or
documentation is modified by this change.

## What Changes

- No code, configuration, or runtime behavior is modified.
- A baseline OpenSpec change is created capturing the **current** behavior of
  configuration profile resolution as a delta spec, design notes, and a
  completed task list.
- On archive, the delta spec is merged into
  `openspec/specs/configuration/profiles/spec.md` so that the canonical spec
  reflects the as-built behavior.
- `docs/PROFILES.md` and `docs/TARGET_ARCHITECTURE.md` are not modified by this
  change. Future changes that introduce new profiles or change resolution
  semantics will update those documents.

## Capabilities

### New Capabilities

- (none — `configuration/profiles` is already declared as a capability from
  STEP 2.8 of the OpenSpec bootstrap. This change merges a delta into the
  existing canonical spec.)

### Modified Capabilities

- `configuration/profiles` — captures the current behavior of prod/test profile
  resolution as a documented spec delta.

## Impact

- Affected files:
  - `openspec/changes/baseline-configuration-profiles/*` (new artifacts)
  - `openspec/specs/configuration/profiles/spec.md` (delta merged on archive)
- No source code affected.
- No runtime behavior affected.
- No configuration affected.
- NoSkill or Tool affected.
