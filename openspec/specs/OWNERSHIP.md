# Владение спецификациями

Сводная раскладка: чьим кодом описана каждая спека. Нужна потому, что проект
разделён на два дерева — агента (`lib/`, `workspace/`, `gateway.py`) и платформу
`mcp-platform` (корпоративный MCP-сервер), — и половина работы состоит в переезде
подсистем между ними. Без этой таблицы вопрос «кто это чинит» не имеет ответа,
кроме как прочитать все 23 спеки.

Раздел `## Scope` в каждой спеке — источник истины, этот файл — его раскладка.
Значение берётся из первой метки в обратных кавычках тела `## Scope` и
проверяется `tools/validate_component_specs.py`: незнакомое значение — ошибка,
пропущенный раздел — тоже.

Быстрый ответ на вопрос «про что этот MCP?»:

```bash
grep -rl '`platform`' openspec/specs --include=spec.md
```

## `platform` — предмет реализован в `mcp-platform`

Три спеки описывают код, который уехал из агента целиком. Ни одна из них не
обновлялась под переезд — это отдельная работа (см. «Что дальше»).

| Спека | Уехало в | Наследник |
|---|---|---|
| `data/cache-provider/spec.md` | capability `data` | `mcp-platform/libs/enterprise_data/snapshot/store.py` (`DuckDbSnapshotStore`) |
| `data/vector-indexes/spec.md` | capability `vectors` | `mcp-platform/libs/vectors/`, объявления в `platform.json → vectors.indexes` |
| `skills/legal-summarizer-query/spec.md` | capability `legal_summarizer` | `mcp-platform/libs/legal_summarizer/`; в агенте осталась только обёртка `workspace/tools/legal_summarizer_query.py` |

## `shared` — контракт между агентом и платформой

Здесь нормативные требования адресованы обеим сторонам: одна решает, другая
исполняет. При переезде такие спеки меняются, а не исчезают.

| Спека | Агент | Платформа |
|---|---|---|
| `configuration/profiles/spec.md` | `profiles/test.jsonc`, `config.json` | `platform.json → profiles.test`, `PROFILE_OWNED_KEYS` |
| `logging-db/spec.md` | `lib/services/db_logging_service.py`, `log_transport.py` | операции `log_events`, `log_event`, `purge_logs` |
| `tools-history-search/spec.md` | `workspace/tools/history_search_tool.py` решает область поиска | SQL и изоляция по `session_id` в `history_search` |

## `agent` — предмет реализован в агенте

| Спека | Где смотреть |
|---|---|
| `architecture/component-model/spec.md` | мета-шаблон самой спецификации |
| `architecture/skill-tool-boundary/spec.md` | `workspace/skills/`, `workspace/tools/`, `lib/services/project_tool_loader.py` |
| `documentation/component-registry/spec.md` | `COMPONENTS.md`, `tools/validate_component_specs.py` |
| `infrastructure/test-profile-tables/spec.md` | `sql/*/create_public_*_test.sql`, `tools/apply_test_profile_tables.py` |
| `runtime/agent-hooks/spec.md` | `lib/core/agent_factory.py`, `lib/hooks/` |
| `runtime/anti-loop/spec.md` | `lib/hooks/repeat_guard_hook.py` |
| `runtime/context/spec.md` | `lib/core/application_context.py` |
| `runtime/entrypoints/spec.md` | `gateway.py`, `cli_agent.py`, `lib/lifecycle/` |
| `runtime/error-fallback/spec.md` | `lib/services/turn_delivery_factory.py` |
| `runtime/runtime-events-subscription/spec.md` | `lib/services/runtime_events_subscriber.py` |
| `runtime/runtime-patcher/spec.md` | `lib/services/runtime_patcher.py` |
| `runtime/startup-schema-validation/spec.md` | `lib/services/schema_validation.py` |
| `storage/session-hybridization/spec.md` | `lib/session/pg_session_manager.py` |
| `storage/usage-store/spec.md` | `lib/core/agent_factory.py` |
| `upgrade-compatibility/spec.md` | `requirements.txt`, `tests/contract/` |
| `validation/component-spec-validation/spec.md` | `tools/validate_component_specs.py` |

`COMPONENTS.md` — реестр компонентов, не спека: раздела `## Scope` в ней нет по
той же причине, по какой его нет у этого файла.

## Требует решения

- **`storage/session-recovery/spec.md`** помечена `agent` по замыслу, но
  **реализации нет ни в одном дереве**. Поиск `SessionRecovery` /
  `session_recovery` по `lib/`, `workspace/`, `gateway.py`, `cli_agent.py`,
  `config.json`, `tools/` даёт ноль совпадений. Архивный change
  `2026-09-27-session-recovery` утверждает, что `SessionRecoveryService` создан в
  `ApplicationContext.create()`, — такого кода нет. Либо спека описывает
  нереализованное намерение, либо подсистема не была сделана. По OpenSpec это
  оформляется change-каталогом, а не правкой существующей спеки.
- **Три спеки `platform` устарели по содержанию.** Разметка фиксирует, чей это
  код, но не переписывает контракт под новое место. Особенно заметно на
  `skills/legal-summarizer-query`: спека описывает subprocess-IPC со
  `scripts/cli_query.py`, которого в агенте уже нет, а фактический вызов идёт
  через MCP-операцию `query_operation`.
- **`COMPONENTS.md` всё ещё перечисляет `CacheProvider` и `VectorIndexService`
  как компоненты агента** и указывает файлы реализации, которых нет. Валидатор
  честно предупреждает об этом на каждом запуске (предупреждение, не ошибка:
  код может быть в другой ветке).
