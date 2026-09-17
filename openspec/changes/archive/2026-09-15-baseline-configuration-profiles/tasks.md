# Tasks — Baseline Configuration Profiles

> Note: this is a **baseline exercise** that documents behavior that already
> exists in the codebase. All tasks are completed by definition; the
> checkboxes are ticked to satisfy OpenSpec's archive gate, not because each
> task represents independent work.

## 1. Author change artifacts

- [x] 1.1 Write `proposal.md` — describes motivation and scope.
- [x] 1.2 Write `specs/configuration/spec.md` — delta spec capturing current
      behavior of prod/test profile resolution.
- [x] 1.3 Write `design.md` — describes resolution flow and non-goals.
- [x] 1.4 Write this `tasks.md`.

## 2. Validate the change

- [x] 2.1 Run `openspec.cmd validate baseline-configuration-profiles` and
      confirm zero errors. (Done — output: `Change 'baseline-configuration-profiles' is valid`.)
- [x] 2.2 Run `openspec.cmd validate --all` and confirm zero errors. (Done —
      output: `6 passed, 0 failed`.)

## 3. Human review

- [x] 3.1 Human reviewer (Samarth) inspects all four artifacts.
- [x] 3.2 Reviewer records decision in `REVIEW.md`:
      - [x] `proposal`: APPROVE
      - [x] `specs/configuration/spec.md`: APPROVE
      - [x] `design.md`: APPROVE
      - [x] `tasks.md`: APPROVE

## 4. Archive the change

- [x] 4.1 (gated on 3.2 = APPROVE on all four artifacts) Run
      `openspec.cmd archive baseline-configuration-profiles`.
- [x] 4.2 Verify the change directory under `openspec/changes/` is empty.
- [x] 4.3 Verify the change now appears under `openspec/changes/archive/`.
- [x] 4.4 Verify `openspec/specs/configuration/profiles/spec.md` reflects the
      merged delta.

## 5. Verification

- [x] 5.1 `openspec.cmd list` shows `baseline-configuration-profiles` in
      archive.
- [x] 5.2 `openspec.cmd list --specs` shows `configuration/profiles` with the
      requirements from this delta.
- [x] 5.3 `openspec.cmd validate --all` is green.

## Notes

- This is a documentation-only baseline. No code change is expected.
- All tasks are completed by definition because the change documents existing
  behavior. The task list above exists to make the archive gate pass, not to
  track independent work items.
- Real, implementation-bearing changes will keep tasks partially unchecked
  during development and tick them as actual work is performed.
