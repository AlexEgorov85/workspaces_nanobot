### 4f98e49 - remove _tmp_checks.py (AlexEgorov85, 4 minutes ago)


### d6c8237 - remove regenerated workspace artifacts (AlexEgorov85, 4 minutes ago)


### 450092d - refactor: replace _tool_events with _tool_audit, remove duplication (AlexEgorov85, 5 minutes ago)

- _tool_events (framework, phase/result) removed from postgres_channel metadata
- _tool_audit (ToolAuditHook, status/arguments/result_preview) is now the only tool events key
- CLI and Streamlit consumers updated to read _tool_audit
- Backwards compat: renderers still handle old _tool_events format for history

### 206265a - remove accidentally resurrected fibonacci.py (AlexEgorov85, 11 minutes ago)


### a6d8535 - fix: merge reasoning and _reasoning into single metadata.reasoning key (AlexEgorov85, 11 minutes ago)


