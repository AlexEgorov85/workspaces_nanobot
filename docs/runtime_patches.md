# Runtime patches — compatibility layer с nanobot-ai

Документ-каталог всех monkey-patches, которые проект применяет к приватным API `nanobot-ai`
для интеграции. Каждый patch описан по формату, требуемому TARGET_ARCHITECTURE.md §20:

```text
purpose       — что делает
reason        — почему без patch нельзя
nanobot version — на какой версии написано
public alternative checked? — рассмотрена ли альтернатива через штатный extension point
upgrade risk  — что может сломаться при апгрейде
test          — какой тест покрывает
```

---

## Сводная таблица

| patch | purpose | nanobot version | public alternative | upgrade risk | test |
|---|---|---|---|---|---|
| `patch_context_bridge_seed` | seed контекст-bridge при старте loop | 0.3.0 | нет | средний | integration через `gateway.py` |
| `patch_context_governor` | лимиты вывода tool-результатов | 0.3.0 | нет | низкий | `tests/test_context_governor*.py` |
| `patch_save_turn` | persist turn в БД + context-window блок | 0.3.0 | нет | средний | `tests/test_*.py` (postgres-channel) |
| `patch_session_content_cleanup` | чистка media в saved turns | 0.3.0 | нет | низкий | smoke |
| `patch_async_session_saves` | async-save для subagents | 0.3.0 | нет | средний | integration |
| `patch_exec_limits` | окружение subprocess + PATH | 0.3.0 | нет | низкий | integration |
| `patch_tool_limits` | потолки для builtin tools | 0.3.0 | нет | низкий | smoke |
| `patch_assemble_outbound` | metadata.context_window в outbound | 0.3.0 | нет | средний | integration |
| `patch_subagent_logging` | логирование subagent events | 0.3.0 | нет | низкий | smoke |
| `patch_project_tools` | auto-discover `workspace/tools/*.py` | 0.3.0 | нет | низкий | `tests/test_tools_project_loader.py` |
| `patch_compaction_tracking` | нотификация сжатия контекста | 0.3.0 | нет | средний | integration |
| `patch_compact_command` | slash-команда `/compact` | 0.3.0 | нет | низкий | `tests/test_*.py` |
| `patch_auto_compact_idle_guard` | отключение idle-compact | 0.3.0 | да, через `idleCompactAfterMinutes: 0` в config | низкий | smoke |

## Общий комментарий

Все patches написаны как **замена приватного hook'а**, потому что в `nanobot 0.3.0` ещё не
появились официальные extension points для перечисленных выше задач. После появления таких
points соответствующий patch должен быть удалён (см. TARGET_ARCHITECTURE.md §20).

Каждый patch локализован в одном методе `RuntimePatcher`. Тесты покрывают поведение,
а не сам факт monkey-patch'а (это сознательное ограничение — мы тестируем результат).