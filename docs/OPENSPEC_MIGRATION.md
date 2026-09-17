# OpenSpec Migration

## Purpose

This document is the self-contained manual for migrating `workspaces_nanobot`
to Spec-Driven Development using OpenSpec. It is written to be executed
sequentially by a weak LLM agent (Cline or equivalent) without reference to
external planning material.

The document replaces the original 42-section draft. Its structure is fixed:
four stages, every step in a uniform format. Read Section 1 (PRINCIPLES)
fully before executing any other step — those rules bind every later action.

## Authority Hierarchy (binding, cannot be overridden by a change)

```
OpenSpec spec                → normative behavior of a capability/change
docs/TARGET_ARCHITECTURE.md  → permanent architectural invariants
workspace/AGENTS.md          → AI agent operating rules
```

When a conflict arises between layers, the agent MUST NOT resolve it
autonomously. The agent writes `BLOCKED.md` (see STEP 4.4) and stops the
change.

## Document Structure

1. **PRINCIPLES** — declarative rules that bind every step.
2. **BOOTSTRAP** — one-time setup (Steps 2.1 – 2.10).
3. **WORKFLOW** — lifecycle of every change after bootstrap (Steps 3.1 – 3.11).
4. **ENFORCEMENT** — operational rules referenced continuously (Steps 4.1 – 4.8).

Each STEP uses this format:

```
STEP n.m — <title>
Files:        <paths created, modified, or referenced>
Action:       <what the agent does>
Must:         <positive constraints>
Must NOT:     <negative constraints>
Command:      <one or more shell commands, if applicable>
Expected:     <observable outcome>
Acceptance:   <checklist the agent ticks>
STOP CONDITION: <when to stop and request human decision>
```

STOP CONDITIONs are hard stops. The agent does not continue past a STOP
CONDITION. Every STOP CONDITION escalates to the human reviewer.

---

## 1. PRINCIPLES

### 1.1 SDD Model

Every significant change follows this sequence:

```
Idea
  ↓
Explore
  ↓
Propose
  ↓
Spec
  ↓
Design
  ↓
Tasks
  ↓
Human review
  ↓
Implementation (only after APPROVE)
  ↓
Tests
  ↓
Benchmark
  ↓
Architecture checks
  ↓
PR
  ↓
Merge + Archive
  ↓
Updated openspec/specs/
```

Code is never the first source of truth for a behavior change. The OpenSpec
artifacts are.

### 1.2 What Requires Spec (mandatory)

A change requires an OpenSpec change when it affects any of:

- behavior
- public API
- configuration
- runtime
- architectural boundaries
- Skill contract
- Tool contract
- service
- storage
- cache
- vector index
- DB schema
- channel
- benchmark contract
- security
- performance contract
- component lifecycle

### 1.3 What Does NOT Require Spec (exemptions)

- documentation typo
- formatting
- comment-only edits
- trivial non-behavioral maintenance

If a technical change has architectural impact, the exemption does not apply.

### 1.4 Negative Requirements (mandatory in every spec)

Every `spec.md` MUST contain at least one `SHALL NOT` clause for each of the
following categories, where applicable to the change:

- no fallback path
- no legacy compatibility branch
- no duplicate implementation
- no second registry
- no alternative execution path
- no profile-specific branches in business logic
- no Skill importing concrete infrastructure tools
- no new dependency on OpenSpec from production code (`lib/`, `workspace/`,
  `ApplicationContext`, `AgentRuntime`, `Skill`, `Tool`)

### 1.5 Architectural Invariants

OpenSpec MUST NOT become a runtime dependency. Production code never imports
from OpenSpec. OpenSpec exists only at:

- development workflow level
- documentation level
- verification level
- CI level

Production runtime (`lib/`, `workspace/`, `ApplicationContext`, `AgentRuntime`,
`Skill`, `Tool`) must remain OpenSpec-independent.

---

## 2. BOOTSTRAP

Execute Steps 2.1 – 2.10 in order. Do not skip steps.

### STEP 2.1 — Verify Node.js

Files:        none
Action:       Check the Node.js version available to the project.
Command:      `node --version`
Must:         Node.js ≥ 20.19 (per current OpenSpec CLI requirements).
Must NOT:     install or upgrade Node.js without explicit human confirmation.
Expected:     a version string ≥ `v20.19`.
Acceptance:
- [ ] Node.js version ≥ 20.19 confirmed.
STOP CONDITION: if version < 20.19, STOP and request human decision on
installing or upgrading Node.js. Do not proceed.

### STEP 2.2 — Verify OpenSpec CLI

Files:        none
Action:       Check whether the OpenSpec CLI is installed.
Command:      `openspec --version`
Expected:     a version string.
Acceptance:
- [ ] OpenSpec CLI present.
STOP CONDITION: if `openspec` is not found, STOP and request human decision
on whether to install via `npm install -g @fission-ai/openspec@latest`. Do not
auto-install.

### STEP 2.3 — Discover the actual OpenSpec CLI surface

Files:        `openspec/changes/bootstrap/cli-inventory.md`
Action:       Run `--help` on every command the agent intends to use, and
              record actual output. Do not assume syntax.
Commands:     run each and capture output
              `openspec --help`
              `openspec init --help`
              `openspec config --help`
              `openspec validate --help`
              `openspec list --help`
              `openspec view --help`
              `openspec status --help`
              `openspec doctor --help`
              `openspec archive --help`
Must:         record observed commands, subcommands, flags, and exit codes.
Must NOT:     invent command syntax. If a command is missing, record that fact.
Expected:     a markdown inventory file with one section per command, each
              showing observed output verbatim.
Acceptance:
- [ ] inventory file written.
- [ ] every command listed above has an entry.
- [ ] any missing command is explicitly listed as `NOT FOUND`.
STOP CONDITION: if any expected command (`init`, `validate`, `list`, `archive`)
is absent from the CLI, STOP and request human decision.

### STEP 2.4 — Run `openspec init`

Files:        `openspec/` (created)
              `.clinerules/` (created, if Cline integration is supported)
Action:       Initialize OpenSpec in the project root.
Command:      `openspec init`
Must:         select profile = `core` (workflows: propose / explore / apply /
              update / sync / archive). Do not enable expanded workflows in
              bootstrap.
Must NOT:     create a custom spec schema, `tools/spec.py`, or a second
              acceptance test framework. Do not modify any file outside
              `openspec/` and `.clinerules/`.
Expected:     `openspec/specs/` exists; `openspec/changes/` exists;
              `.clinerules/workflows/opsx-*.md` files exist (verify, do not
              assume).
Acceptance:
- [ ] `openspec/` directory created.
- [ ] profile = `core` confirmed.
- [ ] Cline workflow files present (or absence recorded with reason).
STOP CONDITION: if profile ≠ `core` after init, STOP and request human
decision. If `openspec init` modified any file outside `openspec/` and
`.clinerules/`, STOP.

### STEP 2.5 — Verify init result via Git diff

Files:        git working tree (no commits expected yet)
Action:       Inspect what `openspec init` actually created, including any
              files outside `openspec/` and `.clinerules/`.
Command:      `git status --short`
              `git diff` (no staged or unstaged changes expected)
Expected:     a documented list of all new files, all modifications to
              existing files (if any), nothing in `lib/` / `workspace/` /
              `tests/`.
Acceptance:
- [ ] no unexpected files in `lib/`, `workspace/`, `tests/`, root config.
- [ ] no modifications to existing source files.
STOP CONDITION: any unexpected modification or creation → STOP and request
human decision.

### STEP 2.6 — Run `openspec doctor`

Files:        none
Action:       Verify OpenSpec project is healthy.
Command:      `openspec doctor`
Expected:     all checks pass.
Acceptance:
- [ ] zero warnings and zero errors.
STOP CONDITION: any failure → capture output to
`openspec/changes/bootstrap/doctor-output.txt` and request human decision.

### STEP 2.7 — Configure repository

Files:        `.gitignore` (read-only inspection; do not modify)
Action:       Confirm `openspec/` is tracked in Git and is NOT gitignored.
Command:      `git check-ignore openspec/` (must exit non-zero)
              `git ls-files openspec/` (must list contents)
Must:         `openspec/` MUST be committed to the repository.
Must NOT:     add `openspec/` to `.gitignore`. Do not gitignore the
              migration artifacts.
Acceptance:
- [ ] `git check-ignore openspec/` exits non-zero.
- [ ] `openspec/` is tracked in Git.
STOP CONDITION: if `openspec/` is gitignored, STOP. Never proceed with
`openspec/` gitignored.

### STEP 2.8 — Author first permanent specs

Files (create):
- `openspec/specs/architecture/skill-tool-boundary/spec.md`
- `openspec/specs/runtime/context/spec.md`
- `openspec/specs/configuration/profiles/spec.md`
- `openspec/specs/data/cache/spec.md`
- `openspec/specs/data/vector-indexes/spec.md`

Action:       Author 5 permanent specs that codify existing normative rules
              from `docs/TARGET_ARCHITECTURE.md` and `workspace/AGENTS.md`.
              Each spec must contain:
                - `## Purpose`
                - `## Requirements` (SHALL)
                - `## Negative Requirements` (SHALL NOT)
Must:         each spec MUST have ≥ 3 SHALL clauses and ≥ 2 SHALL NOT clauses.
              each spec MUST reference `docs/TARGET_ARCHITECTURE.md` for
              permanent architectural invariants, not restate them.
Must NOT:     convert `docs/TARGET_ARCHITECTURE.md` into OpenSpec format
              wholesale. Do not create empty spec directories. Do not add
              specs for capabilities not yet exercised by any change.
Expected:     5 spec files present, each parseable by `openspec validate`.
Acceptance:
- [ ] all 5 spec files exist.
- [ ] `openspec validate` passes for the specs.
STOP CONDITION: any `openspec validate` failure → fix the failing spec (do
not modify OpenSpec config to skip validation). If failure is unrecoverable,
STOP.

### STEP 2.9 — Baseline exercise (configuration profiles, retroactive)

Files (create):
- `openspec/changes/baseline-configuration-profiles/proposal.md`
- `openspec/changes/baseline-configuration-profiles/specs/configuration/spec.md`
- `openspec/changes/baseline-configuration-profiles/design.md`
- `openspec/changes/baseline-configuration-profiles/tasks.md`

Action:       Author a baseline OpenSpec change for the already-completed
              configuration profiles work. The purpose is to exercise the
              workflow on a known outcome, not to validate the
              implementation. After artifacts are written and reviewed:
              archive via `openspec archive` (or equivalent per
              `cli-inventory.md`).
Must:         each artifact MUST follow the OpenSpec core schema (verify
              via `openspec validate`). The change MUST NOT propose any new
              implementation — it is documentation-only.
Must NOT:     modify any code as part of this change. Do not introduce a
              new change structure that OpenSpec does not support.
Expected:     change archived; `openspec/specs/configuration/profiles/spec.md`
              is now the authoritative description of profile behavior.
Acceptance:
- [ ] `openspec validate` passes.
- [ ] human reviewer APPROVE received (recorded in
      `openspec/changes/baseline-configuration-profiles/REVIEW.md`).
- [ ] change archived.
- [ ] `openspec list` shows the change in archive.
STOP CONDITION: any deviation from `openspec` core schema → STOP. Any
disagreement from the human reviewer → STOP.

### STEP 2.10 — First real pilot (NEW, not previously started)

Files:        `openspec/changes/<pilot-name>/` (created in Step 3.2)
Action:       Identify a small, previously-unstarted change to use as the
              first real process pilot. Discuss with the human reviewer to
              agree on scope.
Must:         change must be small (single-component, expected < 200 LOC),
              previously unstarted, with at least one test that can be added
              or modified.
Must NOT:     pick a change that affects multiple subsystems, runtime, or
              has open design questions. Do not pick a Spike for this
              pilot.
Expected:     change name agreed with human; proceeding to STEP 3.1.
Acceptance:
- [ ] pilot change scope agreed with human.
STOP CONDITION: pilot scope disagreement → STOP and request human decision.

---

## 3. WORKFLOW

After bootstrap, every change follows Steps 3.1 – 3.11. The first real pilot
starts at STEP 3.1.

### STEP 3.1 — Explore

Files:        `openspec/changes/<name>/exploration.md`
Action:       Run `/opsx:explore` (Cline workflow) or the CLI equivalent
              documented in `cli-inventory.md`. Capture problem framing,
              affected systems, constraints, and open questions.
Must:         exploration MUST be written before any proposal.
Must NOT:     skip exploration, even for "obvious" changes. Do not write
              `proposal.md` in this step.
Expected:     `exploration.md` exists with these sections:
              - Problem statement
              - Affected components
              - Constraints
              - Open questions
Acceptance:
- [ ] `exploration.md` exists.
- [ ] all four required sections present.
STOP CONDITION: if exploration reveals the problem is misframed, STOP and
request human decision before proceeding to STEP 3.2.

### STEP 3.2 — Propose

Files (create):
- `openspec/changes/<name>/proposal.md`
- `openspec/changes/<name>/specs/<capability>/spec.md`
- `openspec/changes/<name>/design.md`
- `openspec/changes/<name>/tasks.md`

Action:       Run `/opsx:propose` (Cline workflow) or the CLI equivalent.
              The change directory and its four artifacts are created.
Must:         `proposal.md` MUST answer "why this change".
              `specs/<capability>/spec.md` MUST contain ≥ 3 SHALL clauses
              and ≥ 2 SHALL NOT clauses (per Section 1.4).
              `design.md` MUST describe the technical solution at architecture
              level (boxes and arrows), NOT at class/method level.
              `tasks.md` MUST be a checklist of executable steps. Each task
              uses the STEP format from this document.
Must NOT:     skip `specs/<capability>/spec.md` even for small changes. Do
              not inline design into `proposal.md`. Do not inline tasks into
              `design.md`.
Expected:     4 artifacts present, parseable by `openspec validate`.
Acceptance:
- [ ] all 4 artifacts present.
- [ ] `openspec validate` passes.
- [ ] spec contains SHALL and SHALL NOT clauses.
STOP CONDITION: any schema validation failure → STOP and fix in the proposal.
Do not proceed to review with broken artifacts.

### STEP 3.3 — Human review

Files:        `openspec/changes/<name>/REVIEW.md`
              all 4 proposal artifacts
Action:       Human reviewer inspects each artifact and records a decision
              in `REVIEW.md`:
              - `Status: APPROVE | REQUEST CHANGES | REJECT`
              - For each of the 4 artifacts: `APPROVE | REQUEST CHANGES |
                REJECT`
Must:         human reviewer is Samarth (the single mandatory reviewer for
              this project). ALL FOUR artifacts MUST be APPROVE before
              proceeding. The decision MUST be persisted in `REVIEW.md`,
              not just stated in chat.
Must NOT:     begin implementation before all four artifacts are APPROVE.
              Treat a chat message as a substitute for `REVIEW.md`.
Expected:     `REVIEW.md` exists with `Status: APPROVE`.
Acceptance:
- [ ] `REVIEW.md` exists.
- [ ] all four artifacts marked APPROVE.
STOP CONDITION: any artifact = REQUEST CHANGES or REJECT → STOP, return to
STEP 3.1 or 3.2 as appropriate.

### STEP 3.4 — Apply

Files:        implementation files per `tasks.md`
              `openspec/changes/<name>/tasks.md` (updated as tasks complete)
Action:       Run `/opsx:apply` (Cline workflow) or the CLI equivalent.
              Implement the tasks in order. Tick each task in `tasks.md` as
              it is completed.
Must:         execute only tasks listed in `tasks.md`. Update task checkboxes
              inline as work progresses. Run all task-level verifications
              (unit tests, lint) listed in `tasks.md` before marking a task
              done.
Must NOT:     add tasks not in `tasks.md` without going through STEP 3.3.
              Modify `spec.md` or `design.md` without going through STEP 3.3.
              Introduce fallback paths, legacy compatibility branches, or
              duplicate implementations (per Section 1.4). Implement
              features not in `spec.md`.
Expected:     all tasks in `tasks.md` ticked; all task-level verifications
              passing.
Acceptance:
- [ ] all tasks ticked.
- [ ] all task-level verifications green.
STOP CONDITION: spec/code conflict discovered → STOP, write `BLOCKED.md`
(see STEP 4.4), request human decision. Do not continue implementation.

### STEP 3.5 — Tests

Files:        new/modified test files per `tasks.md`
Action:       Run the test suite for affected components.
Command:      `pytest tests/ -k <affected>` for partial run.
              `pytest tests/` for full run.
Must:         all existing tests MUST continue to pass.
Must NOT:     skip tests, mark tests as xfail, or delete tests to make them
              pass.
Expected:     tests green; coverage report shows new code covered.
Acceptance:
- [ ] existing tests still pass.
- [ ] new tests cover new behavior.
- [ ] any new code has a corresponding test.
STOP CONDITION: any test failure → STOP, return to STEP 3.4 to fix. Do not
modify tests to skip failures.

### STEP 3.6 — Benchmark (if applicable)

Files:        new/modified benchmark cases per `tasks.md`
              `openspec/changes/<name>/evidence/benchmark-<timestamp>.txt`
Action:       If the change affects a benchmarked behavior, run the relevant
              benchmark.
Must:         benchmark result MUST match the desired behavior described in
              `spec.md`.
Must NOT:     modify benchmark scoring to make a failing case pass.
Expected:     benchmark passes; result recorded in evidence file.
Acceptance:
- [ ] benchmark passes.
- [ ] evidence file present.
STOP CONDITION: benchmark fails → STOP, return to STEP 3.4 to fix.

### STEP 3.7 — Architecture verification

Files:        log of `tools/architecture_guard.py` invocation
Action:       Run the project's architecture guard.
Command:      `python tools/architecture_guard.py` (or equivalent per
              project).
Must:         zero violations.
Must NOT:     modify the guard to skip checks.
Expected:     zero violations.
Acceptance:
- [ ] architecture guard passes.
STOP CONDITION: any violation → STOP, fix per guard's remediation guidance,
re-run.

### STEP 3.8 — OpenSpec verify (when expanded workflow is enabled)

Files:        `openspec/changes/<name>/verification.md`
Action:       Run `/opsx:verify` or the CLI equivalent. This STEP is
              OPTIONAL until the team decides to enable the expanded
              workflow (post-baseline-exercise, see STEP 2.9).
Must:         this STEP MUST be treated as optional during the first pilot.
              Enabling it as a hard gate requires explicit human decision.
Must NOT:     enable verify as a hard gate without first discussing the
              cost (extra CI time, flake risk).
Expected:     `verification.md` generated.
Acceptance:
- [ ] verification artifact generated (when enabled).
STOP CONDITION: verification reports drift between spec and code → STOP,
write `BLOCKED.md`.

### STEP 3.9 — Open PR

Files:        branch `<type>/<change-name>` (matches `openspec/changes/<name>/`)
              all change artifacts committed
Action:       Push the change branch and open a PR.
Must:         PR title MUST match `<change-name>`. PR body MUST list the
              four change artifacts.
Must NOT:     squash commits that lose task-level traceability.
Expected:     PR open; CI status visible.
Acceptance:
- [ ] PR open.
- [ ] CI status pending (not yet red).
STOP CONDITION: CI red before review → fix and push, do not request review
yet.

### STEP 3.10 — Final review and merge (with archive)

Files:        `REVIEW.md` updated with final decision.
              archive operation produces updated `openspec/specs/`.
Action:       Human reviewer gives final review on the PR. On APPROVE:
              1. Merge to `master`.
              2. Archive the change as the FINAL commit on the change
                 branch, included in the same PR (default per STEP 4.1).
                 Equivalent: merge first, then archive in a follow-up
                 commit if the in-PR archive creates merge friction.
Must:         merge commit MUST include the archive operation (specs
              updated, change moved to `openspec/changes/archive/`). No
              merge to `master` if `openspec/changes/<name>/` is still
              active.
Must NOT:     merge before all CI checks pass. Use force-push.
Expected:     change merged to `master`; `openspec/specs/` updated;
              `openspec/changes/<name>/` empty (archived).
Acceptance:
- [ ] merge complete.
- [ ] archive complete.
- [ ] `openspec list` shows the change in archive.
- [ ] `openspec/specs/` updated to reflect the change.
STOP CONDITION: CI red → STOP, fix, do not merge. Any disagreement from
reviewer → STOP.

### STEP 3.11 — Post-merge documentation sync (when applicable)

Files:        `docs/ARCHITECTURE.md`, `docs/DATABASE.md`, `docs/PROFILES.md`,
              other `docs/*.md` as affected
Action:       If the change alters implementation description in any
              `docs/*.md` file, update those files in a follow-up commit.
              Per Section "Authority Hierarchy", OpenSpec specs are
              normative; `docs/*.md` are descriptive. When a change
              introduces an `openspec/specs/<capability>/spec.md`, the
              same change MUST obsolete any normative text in `docs/*.md`
              covering the same capability (per STEP 4.8).
Must:         docs MUST reflect post-change reality, not pre-change.
Must NOT:     leave stale implementation docs in place after a change that
              obsoletes them.
Expected:     docs in sync with code.
Acceptance:
- [ ] affected `docs/*.md` files updated.
- [ ] no normative duplication between OpenSpec and `docs/`.
STOP CONDITION: doc update requires architectural decision → STOP and start
a new change.

---

## 4. ENFORCEMENT

### STEP 4.1 — Git rules

Files:        repository configuration (branch protection)
              this document (canonical reference)
Action:       Establish and document Git rules for SDD:
              - `openspec/` is committed, NOT gitignored.
              - one branch per change, named `<type>/<change-name>`
                matching `openspec/changes/<change-name>/`.
              - archive is the final commit on the change branch, part of
                the same PR (default). Fallback: post-merge archive commit
                only if in-PR archive creates merge friction.
              - no merge to `master` if `openspec/changes/<name>/` is
                still active.
              - no force-push to `master` under any circumstance.
Must:         documented in this document AND reinforced via pre-merge
              check (manual or CI per STEP 4.2).
Must NOT:     allow direct push to `master`. Allow archive outside the
              merge PR without justification.
Expected:     branch protection on `master` enforces no direct push.
Acceptance:
- [ ] branch protection rules configured.
- [ ] `openspec/` tracked in Git.
- [ ] rules documented.
STOP CONDITION: branch protection cannot be configured → STOP and request
human decision.

### STEP 4.2 — CI validation

Files:        `.github/workflows/ci.yml` (or equivalent CI config)
Action:       Add a CI step running `openspec validate`.
Must:         `openspec validate` MUST run on every PR. Failure MUST
              block merge.
Must NOT:     set `continue-on-error: true` on the validate step. Suppress
              validation failures via CI config.
Expected:     CI step visible in PR checks; failure blocks merge.
Acceptance:
- [ ] CI step present.
- [ ] failure blocks merge.
STOP CONDITION: cannot configure CI → STOP and request human decision.

### STEP 4.3 — Agent operating rules

Files:        `workspace/AGENTS.md` (append a section; do NOT replace)
Action:       Add a section to `workspace/AGENTS.md` documenting:
              - the SDD workflow (reference this document).
              - the Authority Hierarchy.
              - the BLOCKED protocol (STEP 4.4).
              - the "no fallback / no duplicate / no legacy path" rule
                (Section 1.4).
              - the cooldown rule (STEP 4.5).
              - the spike rule (STEP 4.6).
              - the rollback rule (STEP 4.7).
              - the documentation synchronization rule (STEP 4.8).
Must:         `workspace/AGENTS.md` is the ONLY normative source for agent
              operating rules. OpenSpec specs MUST NOT redefine agent
              behavior.
Must NOT:     replace `workspace/AGENTS.md` with OpenSpec content.
              Duplicate content from `docs/TARGET_ARCHITECTURE.md` or this
              document verbatim — link instead.
Expected:     `workspace/AGENTS.md` contains the section.
Acceptance:
- [ ] section added.
- [ ] no content from `docs/TARGET_ARCHITECTURE.md` duplicated.
- [ ] cross-references to this document present.
STOP CONDITION: ambiguity about what belongs in `workspace/AGENTS.md` vs
OpenSpec spec → STOP and request human decision.

### STEP 4.4 — BLOCKED protocol

Files:        `openspec/changes/<name>/BLOCKED.md`
Action:       When the agent detects a conflict between spec and code (or
              between spec and an architectural invariant, or between spec
              and `workspace/AGENTS.md`), the agent MUST:
              1. Stop all implementation immediately.
              2. Create `openspec/changes/<name>/BLOCKED.md` with the
                 format below.
              3. Report BLOCKED status to the user with a short message
                 referencing the file.
              4. Do not modify `spec.md`, `design.md`, `tasks.md`, or
                 implementation until the human resolves the conflict.

`BLOCKED.md` format (mandatory):
```
# BLOCKED

Status: BLOCKED

Reason:
<one-paragraph description>

Conflict:
- Spec: <relevant spec clause>
- Architecture: <relevant invariant or code constraint>
- Agent rules: <relevant workspace/AGENTS.md clause, if applicable>

Decision required:
[ ] RE-SPEC       — change specification
[ ] RE-DESIGN     — change technical solution
[ ] IMPLEMENT     — change code to satisfy spec
[ ] REJECT        — close this change

Blocked task:
<n>.<m> — <task title>

Agent must not continue implementation until this file is updated to
status RESOLVED by the human reviewer.
```

Chat message format (mandatory):
```
BLOCKED: human decision required.

Change: <name>
Conflict: <one-line summary>
Decision: RE-SPEC / RE-DESIGN / IMPLEMENT / REJECT
Details: openspec/changes/<name>/BLOCKED.md
```

Must:         BLOCKED applies to the entire CHANGE, not just the current
              task. Any cron / scheduled / subagent / retry path is
              forbidden from executing the change while BLOCKED.
Must NOT:     auto-resolve BLOCKED via "reasonable interpretation".
              Modify spec, design, tasks, or code after writing BLOCKED.md.
              Continue with subsequent tasks.
Expected:     `BLOCKED.md` exists; agent has stopped; human decision
              pending.
Acceptance:
- [ ] BLOCKED.md exists.
- [ ] chat message emitted.
- [ ] agent has not modified spec/design/tasks/code after BLOCKED.
STOP CONDITION: multiple BLOCKED scenarios in the same change → keep one
BLOCKED.md per change, list each scenario as a numbered section.

### STEP 4.5 — Cooldown rule

Files:        `workspace/AGENTS.md` (operational note)
              this document (canonical reference)
Action:       Establish the rule: a change that has been BLOCKED for
              ≥ 7 days without a human resolution is considered abandoned.
              The agent MUST NOT auto-resolve. The agent MUST surface the
              abandoned status in the next user-facing report.
Must:         cooldown is exactly 7 days.
Must NOT:     auto-archive, auto-reject, or auto-modify a BLOCKED change
              after cooldown. Silently age out BLOCKED changes.
Expected:     BLOCKED changes older than 7 days are reported, not silently
              aged.
Acceptance:
- [ ] cooldown rule documented in `workspace/AGENTS.md`.
- [ ] cooldown rule referenced from this document.
STOP CONDITION: rule conflict with `workspace/AGENTS.md` → STOP and
request human decision.

### STEP 4.6 — Spike / PoC

Files:        `openspec/changes/spike-<name>/`
              `openspec/changes/spike-<name>/proposal.md`
              `openspec/changes/spike-<name>/design.md`
Action:       When a change is exploratory (research, prototyping,
              proof-of-concept), the agent MAY create a Spike instead of
              a full change.
Must:         `proposal.md` MUST include `kind: spike` in its frontmatter.
              Spike MUST have `proposal.md` + `design.md` only (no
              `specs/` required). Spike MUST declare an explicit TTL
              (default: 30 days). After TTL, the spike is archived as
              abandoned UNLESS it has graduated. Graduation = a new full
              OpenSpec change that supersedes the spike.
Must NOT:     mark a Spike as production code. Transfer spike code to
              production paths without a full change. Extend spike TTL
              silently.
Expected:     spike exists in `openspec/changes/spike-<name>/` with
              explicit TTL.
Acceptance:
- [ ] `kind: spike` present in `proposal.md` frontmatter.
- [ ] TTL declared.
- [ ] no `specs/` directory.
STOP CONDITION: spike graduation requires spec → start a new full change
(do not retrofit the spike).

### STEP 4.7 — Rollback / superseding change

Files:        `openspec/changes/archive/<old-name>/SUPERSEDED.md`
              `openspec/changes/<new-name>/` (new change)
Action:       To reverse an archived change, create a new change that
              explicitly supersedes it. Do NOT revert the Git history of
              the original change.
Must:         the new change MUST reference the superseded change in
              `proposal.md`. The original archive entry MUST be
              annotated with `SUPERSEDED.md`. The original spec MUST
              NOT be deleted; it MUST be marked as superseded in
              `openspec/specs/`.
Must NOT:     revert Git history. Delete the original spec. Treat
              supersede as a no-op; it is itself a change requiring full
              review per Section 3.
Expected:     clear audit trail of what was reversed and why.
Acceptance:
- [ ] SUPERSEDED.md present in old archive.
- [ ] superseding change archived with reference.
- [ ] original spec marked superseded in `openspec/specs/`.
STOP CONDITION: supersede requires architectural change → STOP and start
a higher-scope change.

### STEP 4.8 — Documentation synchronization

Files:        `docs/ARCHITECTURE.md`, `docs/DATABASE.md`,
              `docs/PROFILES.md`, other `docs/*.md`
Action:       After every change is merged and archived, check whether
              `docs/*.md` files describe the now-current implementation.
              If they describe the previous implementation, update them
              in a follow-up commit.
Must:         each `docs/*.md` file describes the CURRENT implementation,
              not historical. The change that introduces an
              `openspec/specs/<capability>/spec.md` MUST be the same
              change that obsoletes the corresponding `docs/*.md`
              normative section. OpenSpec specs are normative;
              `docs/*.md` are descriptive.
Must NOT:     keep two normative sources for the same capability. Let
              `docs/*.md` retain obsolete normative text after an
              OpenSpec spec exists for the same capability.
Expected:     no two layers (OpenSpec + docs) describe the same
              normative content differently.
Acceptance:
- [ ] no normative duplication detected.
- [ ] obsolete normative sections in `docs/` removed or marked
      descriptive-only.
STOP CONDITION: duplicated normative content detected → write
`BLOCKED.md` for an upcoming cleanup change. Do not silently let it
accumulate.

---

## CRITICAL REMINDERS

1. **No Spec, no significant implementation.** Every change in Section 1.2
   requires an OpenSpec change. Section 1.3 lists the only exemptions.
2. **No fallback, no duplicate, no legacy path.** Every spec MUST contain
   the negative requirements from Section 1.4. The agent MUST NOT
   introduce any of these patterns during implementation.
3. **OpenSpec is dev tooling, not runtime.** Section 1.5. No production
   code imports from OpenSpec.
4. **BLOCKED is change-level state.** STEP 4.4. While a change is
   BLOCKED, no automation may execute any task in that change.
5. **Archive is part of the merge.** STEP 3.10 default. The merge commit
   includes the archive operation. No merge to `master` while the
   change is still active in `openspec/changes/`.
6. **Spike is a change with reduced schema.** STEP 4.6. `kind: spike` in
   frontmatter, TTL declared, no `specs/`, mandatory graduation path
   via a new full change.

---

## Appendix A — File map after bootstrap

```
openspec/
├── specs/
│   ├── architecture/
│   │   └── skill-tool-boundary/spec.md
│   ├── runtime/
│   │   └── context/spec.md
│   ├── configuration/
│   │   └── profiles/spec.md
│   ├── data/
│   │   ├── cache/spec.md
│   │   └── vector-indexes/spec.md
│   └── ...
├── changes/
│   ├── archive/
│   │   └── baseline-configuration-profiles/
│   └── <active-changes>/
└── ...
.clinerules/
└── workflows/
    └── opsx-*.md
docs/
└── OPENSPEC_MIGRATION.md   (this file)
```

## Appendix B — STOP CONDITION summary

| Trigger                                                  | Action                                    |
| -------------------------------------------------------- | ----------------------------------------- |
| Node.js / OpenSpec CLI missing or wrong version          | STOP, request human decision              |
| `openspec init` modifies unexpected files                | STOP, request human decision              |
| `openspec validate` fails                                | Fix in proposal (or STOP if unrecoverable)|
| Human reviewer REQUEST CHANGES or REJECT                 | Return to STEP 3.1 or 3.2                |
| Spec/code conflict during implementation                | Write BLOCKED.md, STOP, request decision  |
| Test failure                                             | Return to STEP 3.4 to fix                |
| Benchmark failure                                        | Return to STEP 3.4 to fix                |
| Architecture guard violation                            | Fix per guard, re-run                     |
| CI red before merge                                      | Fix, do not merge                         |
| Cooldown (BLOCKED > 7 days)                              | Report, do NOT auto-resolve               |
| Spike TTL expired without graduation                     | Archive as abandoned                      |
| Supersede requires architectural change                 | Start higher-scope change                 |
| Normative duplication detected                           | Write BLOCKED.md for cleanup change       |
