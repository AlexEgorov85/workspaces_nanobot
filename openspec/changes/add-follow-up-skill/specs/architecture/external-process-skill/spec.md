## Purpose

Определить, как навык, реализованный **вне** этого репозитория (свой код,
свой venv, свои модели), подключается к агенту, не нарушая границу
Skill/Tool и не создавая второго реестра. Первый такой навык —
`follow_up`; контракт общий для любого следующего.

## Requirements

### Requirement: External-process skill lives under workspace/skills

The system SHALL represent an external-process skill as a directory
`workspace/skills/<name>/` containing `SKILL.md` (agent instructions) and a
launcher `scripts/<name>_mcp` (POSIX) with a `.cmd` twin (Windows).

#### Scenario: Agent discovers the skill

- **WHEN** gateway builds the system prompt
- **THEN** `SkillsLoader` SHALL pick up `workspace/skills/<name>/SKILL.md`
  like any other skill, and `metadata.nanobot.always` SHALL control whether
  it is always in context.

### Requirement: Registration goes through nanobot-ai MCP only

The system SHALL register the skill's tools through
`config.json::tools.mcpServers.<name>` (stdio), the generic MCP mechanism
of `nanobot-ai`, and SHALL declare the skill in `project.json::skills.<name>`.

#### Scenario: Static configuration in git

- **WHEN** `tools.mcpServers.<name>` is committed
- **THEN** `command` SHALL be the launcher path relative to the repository
  root, SHALL NOT contain machine-specific absolute paths and SHALL NOT
  contain `${VAR}` references (an unset variable fails
  `resolve_config_env_vars` at startup).

### Requirement: Machine-specific settings stay out of git

The system SHALL read machine-specific settings (implementation root,
interpreter, device) from `workspace/skills/<name>/<name>.env.local`
(matched by `*.env.local` in `.gitignore`), with environment variables
taking precedence.

#### Scenario: Machine without the implementation

- **WHEN** the local file is absent or incomplete
- **THEN** the launcher SHALL exit with code 3, SHALL write the reason to
  stderr only (stdout is the JSON-RPC channel), and gateway SHALL log
  `failed to connect` for that server and continue startup.

#### Scenario: Launcher passes the agent context

- **WHEN** the launcher starts the implementation
- **THEN** it SHALL set `NANOBOT_HOME` to the repository root derived from
  its own location, so the implementation reads the agent's model settings
  from the same `config.json` / `.secrets.env`.

### Requirement: Launcher has no project dependencies

The launcher SHALL use only the Python standard library and SHALL run under
any interpreter available on `PATH` (`python` on Windows, the shebang
interpreter on POSIX).

## Negative Requirements

The system SHALL NOT:

- import the external implementation from `lib/`, `workspace/tools/` or any
  other skill (no fallback in-process path);
- install the external implementation's dependencies into the project venv
  (no shared environment);
- add a second registry of skills or tools besides `project.json::skills.*`
  and `config.json::tools.mcpServers.*`;
- add a legacy or compatibility branch for the case when the launcher is
  not configured — the only behavior is exit 3 and a logged skip;
- introduce a dependency on OpenSpec from production code.
