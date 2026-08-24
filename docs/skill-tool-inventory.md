# Skill / Tool inventory (2026-08-24)

Зафиксированное состояние перед началом рефакторинга `refactor/skills-tools-cleanup`.
Полные данные собраны в plan-mode и доступны в чате.

## Сводная таблица

| component | path | type | depends_on_skill | depends_on_tool | depends_on_shared_infra | status |
|---|---|---|---|---|---|---|
| `audit_analyzer` Skill | `workspace/skills/audit_analyzer/SKILL.md` + `scripts/` + `tests/` | Skill (domain) | — | — (через LLM) | `lib/services/cache_provider_impl.py` (через `scripts/skill_config.build_cache_provider`) | active, partly deprecated for agent-flow |
| `compact_context` tool | `workspace/tools/compact_context.py` | Tool | — | — | `lib/services/context_compaction.py` | active |
| `audit_run_predefined_script` tool | `workspace/tools/audit_analyzer_tool.py::AuditRunPredefinedScriptTool` | Tool (anti-pattern) | `workspace.skills.audit_analyzer.scripts.{database,db_loader,predefined,predefined_mode,output,skill_config}` через `importlib.util.spec_from_file_location` | — | то же | **TO BE REMOVED** |
| `audit_search_vector` tool | `workspace/tools/audit_analyzer_tool.py::AuditSearchVectorTool` | Tool (anti-pattern) | `workspace.skills.audit_analyzer.scripts.{output,skill_config}` через `importlib` | — | `lib/services/cache_provider_impl.py` | **TO BE REMOVED** |
| `audit_generate_sql` tool | `workspace/tools/audit_analyzer_tool.py::AuditGenerateSqlTool` | Tool (anti-pattern) | `workspace.skills.audit_analyzer.scripts.{database,llm,output,skill_config}` через `importlib` | — | `lib/services/cache_provider_impl.py` | **TO BE REMOVED** |
| `example_tool` | `workspace/tools/example.py` | Tool (template) | — | — | — | reference |

## Целевая зависимость (после рефакторинга)

```
Skill (audit_analyzer) ─┐
                        ├──► shared infrastructure (lib/utils, lib/services) ◄──┐
Tool (duckdb_query)  ───┤                                                        │
Tool (vector_search)  ──┘                                                        │
                                                                                 │
Skill (audit_analyzer) не импортирует Tool,                                       │
Tool не импортирует Skill,                                                       │
связь — только через LLM-agent runtime.                                          │
```

## Зависимости, которые должны исчезнуть

1. `from workspace.skills...` в `workspace/tools/audit_analyzer_tool.py` (все вхождения через `importlib.util.spec_from_file_location` + 1 прямой `from workspace.skills.audit_analyzer.scripts.skill_config import build_cache_provider` на строке 452).
2. Hard-coded упоминания `public.agent_predefined_scripts`, `audits_index` в tool-описаниях (description), используемых для выбора tool'а агентом.
3. Runtime-context providers (`_PredefinedScriptsProvider`, `_AuditSchemaProvider`), живущие в tool-файле, — должны переехать в skill (`workspace/skills/audit_analyzer/providers.py`).