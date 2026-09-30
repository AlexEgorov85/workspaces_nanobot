## Purpose

Определить, как навык, чей сервер работает **отдельным процессом** (своя
база, свои фоновые потоки), подключается к агенту, не нарушая границу
Skill/Tool и не создавая второго реестра. Первый такой навык —
`follow_up`; контракт общий для любого следующего.

## Requirements

### Requirement: External-process skill lives under workspace/skills

The system SHALL represent an external-process skill as a directory
`workspace/skills/<name>/` containing `SKILL.md` (agent instructions), the
server code, a machine-readable list of the server's tools (`tools.json`),
and a launcher `scripts/<name>_mcp` (POSIX) with a `.cmd` twin (Windows).

#### Scenario: Agent discovers the skill

- **WHEN** gateway builds the system prompt
- **THEN** `SkillsLoader` SHALL pick up `workspace/skills/<name>/SKILL.md`
  like any other skill, and `metadata.nanobot.always` SHALL control whether
  it is always in context.

### Requirement: Registration goes through the project tool loader

The system SHALL expose the skill's tools to the agent through a module
`workspace/tools/<name>.py` discovered by the project tool loader, one
`Tool` per server tool, with names, descriptions and schemas taken from
`tools.json`, and SHALL declare the skill in `project.json::skills.<name>`.

#### Scenario: No config.json change

- **WHEN** the skill is added
- **THEN** `config.json` SHALL NOT contain `tools.mcpServers.<name>`: the
  tool module owns the server process, and a second entry would start a
  second server over the same data where the framework reads that section.

#### Scenario: The server runs out of process

- **WHEN** gateway registers the skill's tools
- **THEN** the tool module SHALL start the server through the launcher with
  the gateway's interpreter and environment, keep one MCP session per
  gateway process, restart a dead server on the next call, and return a
  tool error (not an exception) when the server is unavailable.

#### Scenario: Warm-up only in the long-running gateway

- **WHEN** tools are registered by a CLI or by the project's tests
- **THEN** the server SHALL NOT be started until the first call.

### Requirement: No per-machine configuration

The launcher SHALL find the server code inside the skill directory, and the
server SHALL take the model settings, the database connection
(`channels.postgres.dsn`) and the schema (`channels.postgres.schema`,
`public` excluded) from the agent's configuration via `NANOBOT_HOME`, so
that a checkout of the repository is enough to run the skill.

#### Scenario: Machine where the skill cannot start

- **WHEN** the server code is absent
- **THEN** the launcher SHALL exit with code 3, SHALL write the reason to
  stderr only (stdout is the JSON-RPC channel), and the agent's tool call
  SHALL return the reason as a tool error.

### Requirement: Launcher has no project dependencies

The launcher SHALL use only the Python standard library and SHALL run under
any Python ≥ 3.8.

## Negative Requirements

The system SHALL NOT:

- import the skill's server code from `lib/`, `workspace/tools/` or any
  other skill, nor import project code from the server;
- pin in the skill's `requirements.txt` packages already pinned in the root
  `requirements.txt`;
- contain host names, database names, schema names of a particular site,
  or identifiers and facts of real audit checks in the skill's code;
- add a second registry of skills or tools besides `project.json::skills.*`
  and the project tool loader;
- introduce a dependency on OpenSpec from production code.
