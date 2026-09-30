# OpenSpec CLI Inventory (captured during STEP 2.3)

Captured from OpenSpec CLI v1.13.0 on Windows PowerShell at `C:\Users\Алексей\.nanobot`.
Commands were probed via `openspec.cmd --help` and `<command> --help` to record actual
syntax. No assumptions are made beyond observed output.

## Top-level commands

```
init [options] [path]              Initialize OpenSpec in your project
update [options] [path]            Update OpenSpec instruction files
list [options]                     List items (changes by default). Use --specs to list specs
view [options]                     Display an interactive dashboard of specs and changes
change                             Manage OpenSpec change proposals
archive [options] [change-name]    Archive a completed change and update main specs
spec                               Manage and view OpenSpec specifications
config [options]                   View and modify global OpenSpec configuration
schema                             Manage workflow schemas [experimental]
store                              Create and manage stores - standalone OpenSpec repos you register on this machine
doctor [options]                   Report relationship health for the resolved OpenSpec root
context [options]                  Print the working context for the resolved OpenSpec root
workset [options]                  Compose, keep, and open personal working views (purely local)
validate [options] [item-name]     Validate changes and specs
show [options] [item-name]         Show a change or spec
feedback [options] <message>       Submit feedback about OpenSpec
completion                         Manage shell completions for OpenSpec CLI
status [options]                   Display artifact completion status for a change
instructions [options] [artifact]  Output enriched instructions for artifacts, apply, or archive
templates [options]                Show resolved template paths for all artifacts in a schema
schemas                            List available workflow schemas with descriptions
new                                Create new items
help [command]                     display help for command
```

## init options

```
--tools <tools>        Configure AI tools non-interactively. Accepted: amazon-q, antigravity,
                       auggie, bob, claude, cline, command-code, codeartsagent, codex, devin,
                       forgecode, codebuddy, continue, costrict, crush, cursor, factory,
                       gemini, github-copilot, hermes, iflow, junie, kilocode, kimi, kiro,
                       lingma, minimax-code, vibe, oh-my-pi, opencode, pi, codeassistant,
                       qoder, qwen, rovodev, roocode, trae, zed, zcode, agents.
                       Also accepted: windsurf (now devin).
--language <language>  Write new OpenSpec artifacts in this language
--force                Auto-cleanup legacy files without prompting
--profile <profile>    Override global config profile (core or custom)
--no-animation         Show a static welcome screen instead of the animated one
--copilot-cloud        Set up GitHub Copilot cloud coding-agent files without prompting
--no-copilot-cloud     Skip GitHub Copilot cloud coding-agent files without prompting
-h, --help             display help for command
```

## config subcommands

```
path                         Show config file location
list [options]               Show all current settings
get <key>                    Get a specific value (raw, scriptable)
set [options] <key> <value>  Set a value (auto-coerce types)
unset <key>                  Remove a key (revert to default)
reset [options]              Reset configuration to defaults
edit                         Open config in $EDITOR
profile [preset]             Configure workflow profile (interactive picker or preset shortcut)
help [command]               display help for command
```

`openspec config` (no subcommand) without preset returns:
> Interactive mode required. Use `openspec config profile core` or set config via
> environment/flags.

## Initial configuration (post-init)

```
profile: core
delivery: both
telemetry:
  noticeSeen: true
  anonymousId: 9439e83b-3d03-4318-8d1c-5bdf6f7ed63a
  enabled: false

Profile settings:
  profile: core (explicit)
  delivery: both (explicit)
  workflows: propose, explore, apply, update, sync, archive (from core profile)
```

## Available schemas

```
spec-driven
  Default OpenSpec workflow - proposal → specs → design → tasks
  Artifacts: proposal → specs → design → tasks
```

Only `spec-driven` is registered initially. New schemas can be added via the
`schema` command.

## Available extended workflows

```
new, continue, ff, bulk-archive, verify, onboard
```

Not enabled by the core profile. Activation: `openspec config profile`.

## Cline integration

Created by `openspec init --tools cline`:
- `.cline/skills/` — 6 skill files (`openspec-{explore,propose,apply-change,update-change,archive-change,sync-specs}/SKILL.md`).
- `.clinerules/workflows/` — 6 workflow files (`opsx-{explore,propose,apply,update,archive,sync}.md`).

The Cline slash commands are `/opsx-explore`, `/opsx-propose`, `/opsx-apply`,
`/opsx-archive`, `/opsx-sync`, `/opsx-update` (with **dashes**, not colons —
note: the migration plan originally said `/opsx:propose`, which is incorrect;
this file records the actual syntax).

## Execution quirks on this Windows host

- **PowerShell execution policy** blocks `*.ps1` scripts including
  `npm.ps1` and `openspec.ps1`. Workaround: use the `.cmd` wrapper, e.g.
  `openspec.cmd` instead of `openspec`, `npm.cmd` instead of `npm`.
- **Telemetry notice on stderr** raises `$LASTEXITCODE = 1` despite successful
  execution. Disabling telemetry via `openspec.cmd config set telemetry.enabled false`
  silences the notice but does not change the exit code behaviour on stderr writes.
  When scripting, check for the expected output strings rather than exit code.
- **Working directory**: `openspec` resolves the root from the current working
  directory upward. Always `cd` into the project root before running commands.
