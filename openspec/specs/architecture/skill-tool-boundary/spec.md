# Skill / Tool Boundary

## Purpose
Define the architectural boundary between Skills (project-specific agent capabilities) and Tools (generic infrastructure). The boundary keeps domain logic in Skills and reusable plumbing in Tools.

## Source of Truth

- Permanent invariants: `docs/TARGET_ARCHITECTURE.md` (skill/tool boundary principles).
- Implementation details: `docs/skill-tool-architecture.md` (descriptive, not normative).
- Authoring guide: `docs/SKILL_AUTHORING.md`.

## Requirements

### Requirement: Skill layer holds domain logic

The system SHALL keep project-specific domain logic inside the Skill layer (`workspace/skills/<name>/`).

#### Scenario: Skill implements its own scripts

- **WHEN** a Skill needs domain logic (e.g., SQL composition, output formatting, skill-specific orchestration)
- **THEN** that logic SHALL live in `workspace/skills/<name>/scripts/` and SHALL NOT be re-implemented as a generic Tool.

### Requirement: Tool layer holds generic infrastructure

The system SHALL keep generic, reusable infrastructure functionality in Tool implementations under `workspace/tools/`.

#### Scenario: Tool is reusable across Skills

- **WHEN** a Tool is defined under `workspace/tools/`
- **THEN** it SHALL be usable from any Skill via the agent tool-call interface.

### Requirement: Independence

The system SHALL keep Skills and Tools independently developable: neither layer requires a compile-time dependency on the other.

#### Scenario: Skill consumes Tool via tool-call

- **WHEN** a Skill needs functionality that is implemented as a Tool
- **THEN** the Skill SHALL invoke the Tool through the standard tool-call mechanism, not through a direct module import.

## Negative Requirements

The system SHALL NOT:

- import concrete Tool implementations from inside Skill code (`workspace/skills/<name>/scripts/`, `workspace/skills/<name>/SKILL.md`).
- import concrete Skill logic from inside Tool code (`workspace/tools/<tool>.py`).
- introduce a fallback path that bypasses this boundary (no "legacy Skill import" or "secondary Tool call" mechanism).
- create a second registry of Skills or Tools outside the one declared in `project.json::skills.*`.
- add an alternative execution path (e.g., Tool directly callable without going through the tool-call interface).
