# Long-term Memory

This file stores important information that should persist across sessions.

## Project Context

- **Folder Analyzer Skill**: All 8 stages completed (2026-05-20).

## Technical Practices

- **File Operations**: Always use explicit `utf-8` encoding for cyrillic path handling in Windows.
- **Automation**: Prefer `exec` with absolute paths for Windows compatibility.
- **Reporting**: Generate structured reports in Markdown and JSON formats with `--output` argument support.
- **Large File Analysis**: Use map-reduce pattern with temporary file cleanup.
