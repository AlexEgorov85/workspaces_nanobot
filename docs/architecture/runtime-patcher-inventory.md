# RuntimePatcher Inventory

> Каталог всех monkey-patch'ей к `nanobot-ai==0.3.0`.
> Определение: [`lib/services/runtime_patcher.py`](../../lib/services/runtime_patcher.py).
> См. также [nanobot-inventory.md](nanobot-inventory.md).

**Принцип (TARGET_ARCHITECTURE §20):** каждый patch обязан иметь purpose,
target, nanobot version, проверенную public alternative, upgrade risk и тест.
Если upstream даёт официальный extension point — patch заменяется на него.

**Единственная точка применения:** `RuntimePatcher.apply_all(...)` из
`ApplicationContext.create()` (`lib/core/application_context.py`). Каждый патч
в try/except → при изменении API nanobot патч уходит в `PatchReport.skipped/failed`,
процесс не падает.

---

## Классификация

| Категория | Значение |
|---|---|
| **KEEP** | upstream не даёт точки расширения — патч необходим |
| **ISOLATE+TESTS** | патч нужен, но требует contract-тестов на целевой API |
| **REVIEW** | возможно есть публичная альтернатива — проверить при апгрейде |

---

## Сводная таблица

| # | Патч | Target (nanobot API) | Тип target | Risk | Категория |
|---|---|---|---|---|---|
| 1 | `context_bridge_seed` | `agent._state_build` | private async method | HIGH | KEEP |
| 2 | `context_governor` | `ContextGovernor.normalize_tool_result` | internal staticmethod | HIGH | ISOLATE+TESTS |
| 3 | `save_turn` | `agent._save_turn` | private method | HIGH | KEEP |
| 4 | `session_content_cleanup` | `Session.add_message` | public метод (обёртка класса) | MEDIUM | KEEP |
| 5 | `async_save` | `agent.sessions.save` | public (меняет семантику sync→executor) | MEDIUM | KEEP |
| 6 | `exec_limits` | константы `MAX_OUTPUT_CHARS` и схема tools | private class attrs | HIGH | REVIEW* |
| 7 | `tool_limits` | `_MAX_CHARS`, `_DEFAULT_*`, `_MAX_FILE_BYTES` | private class attrs | HIGH | REVIEW* |
| 8 | `assemble_outbound` | `agent._assemble_outbound` | private method | CRITICAL | KEEP |
| 9 | `subagent_logging` | `_SubagentHook` (подмена класса в модуле) | private class | CRITICAL | ISOLATE+TESTS |
| 10 | `project_tools` | `ToolContext(...)` + DI setattr | internal ctor + собственная конвенция | HIGH | KEEP |
| 11 | `compact_tracking` | `AutoCompact._archive`, `Consolidator.maybe_consolidate_by_tokens` | private + public | HIGH | KEEP |
| 12 | `compact_command` | `agent.commands.exact/prefix` | public CommandRouter | LOW | KEEP |
| 13 | `idle_guard` | `auto_compact.check_expired` | public (no-op замена) | LOW | REVIEW |

\* REVIEW для лимитов: проверить, появились ли в nanobot ≥0.3.x конфигурируемые потолки
tool-вывода; если да — патчи заменяются конфигом.

---

## Детальный каталог

### 1. `patch_context_bridge_seed(agent)` — runtime_patcher.py:212

```yaml
PATCH: context_bridge_seed
target: AgentLoop._state_build (private async)
nanobot_version: 0.3.0
purpose: >
  На старте оборота засеять в мост DatabaseLoggingHook лимит
  контекстного окна и модель; по-итерационный usage хука дополняет мост,
  финальный блок context_window собирается в assemble_outbound.
why_not_hook: >
  Хук before_run не имеет доступа к prompt_tokens лимиту модели;
  лимит живёт только внутри AgentLoop.state_build.
public_alternative: нет (проверено 0.3.0); следить за runtime events.
replacement: если nanobot начнёт публиковать usage в runtime events — убрать патч.
risk: HIGH (переименование _state_build ломает seed → метрика окна пропадает,
  но не падает: getattr с дефолтом None → skipped).
tests: tests/test_runtime_patcher.py::test_context_bridge_seed*
```

### 2. `patch_context_governor(config, settings, workspace_dir)` — runtime_patcher.py:259

```yaml
PATCH: context_governor
target: ContextGovernor.normalize_tool_result (internal staticmethod)
nanobot_version: 0.3.0
purpose: >
  Большие результаты инструментов (> persist_threshold) выгружать в
  workspace/data_store/cache/sessions/<session_key>/, в контекст класть
  короткую ссылку data_store/<path>. Экономия токенов + сохранение данных.
why_not_hook: >
  Усечение происходит ВНУТРИ governor до передачи результата агенту;
  hook'и получают уже усечённый результат — данные потеряны бы безвозвратно.
public_alternative: нет в 0.3.0.
risk: HIGH (сигнатура staticmethod может измениться).
tests: tests/test_runtime_patcher_e2e.py, tests/test_gateway.py:261-266
```

### 3. `patch_save_turn(settings, workspace_dir, agent)` — runtime_patcher.py:351

```yaml
PATCH: save_turn
target: AgentLoop._save_turn (private)
nanobot_version: 0.3.0
purpose: >
  Архивация больших tool-результатов в data_store/ ДО усечения истории
  оборота; сериализация сообщений с защитой от потери медиа-ссылок.
why_not_hook: >
  after_run получает уже записанную историю; точка «до усечения» только здесь.
public_alternative: нет.
risk: HIGH.
tests: tests/test_runtime_patcher.py::test_save_turn*
```

### 4. `patch_session_content_cleanup()` — runtime_patcher.py:451

```yaml
PATCH: session_content_cleanup
target: Session.add_message (nanobot.session.manager)
nanobot_version: 0.3.0
purpose: >
  Санитизация content/kwargs от NUL-символов (\x00) и unicode-escape артефактов
  на источнике — иначе psycopg2 падает "A string literal cannot contain NUL".
why_not_hook: >
  Запись идёт из многих мест AgentLoop; перехват только в channel/hook
  не покрывает все пути записи.
public_alternative: нет; альтернатива — санитизация в PGSessionManager.save
  (рассмотреть при апгрейде: перенос логики из глобального патча в наш адаптер
  уберёт мутацию чужого класса).
risk: MEDIUM (патчим публичный метод, сигнатура стабильна).
tests: tests/test_runtime_patcher.py::test_session_content_cleanup*
```

### 5. `patch_async_session_saves(agent)` — runtime_patcher.py:490

```yaml
PATCH: async_save
target: agent.sessions.save (наш PGSessionManager!)
nanobot_version: 0.3.0
purpose: >
  Обёртка save() через ThreadPoolExecutor(max_workers=1): синхронный
  psycopg2-вызов не блокирует event loop и не взаимно-блокируется
  с postgres channel.
why_not_otherway: >
  Патчим СОБСТВЕННЫЙ менеджер сессий, не класс nanobot — это адаптация
  нашего адаптера, риск ограничен нашим кодом.
public_alternative: не требуется.
risk: MEDIUM.
tests: tests/test_runtime_patcher.py::test_async_session_saves*
```

### 6. `patch_exec_limits(settings)` — runtime_patcher.py:597

```yaml
PATCH: exec_limits
target: >
  exec_session.MAX_OUTPUT_CHARS / DEFAULT_MAX_OUTPUT_CHARS,
  shell.MAX_OUTPUT_CHARS / ExecTool._MAX_OUTPUT (+ JSON-Schema maximum
  через _bump_schema_max)
nanobot_version: 0.3.0
purpose: >
  Поднять потолок вывода exec/shell-tools (дефолт nanobot режет ~50K символов);
  значения из gateway.tool_result_limits.* в project.json.
public_alternative: >
  ПРОВЕРИТЬ при апгрейде — возможно в новых версиях tool limits конфигурируются
  штатно через config.json (tools.exec секция).
risk: HIGH (все константы приватные).
tests: tests/test_runtime_patcher.py::test_exec_limits*
```

### 7. `patch_tool_limits(settings)` — runtime_patcher.py:651

```yaml
PATCH: tool_limits
target: >
  ReadFileTool._MAX_CHARS, ListDirTool._DEFAULT_MAX,
  search._DEFAULT_HEAD_LIMIT, _DEFAULT_FILE_HEAD_LIMIT, GrepTool._MAX_FILE_BYTES
nanobot_version: 0.3.0
purpose: конфигурируемые потолки read_file/list_dir/grep.
public_alternative: см. exec_limits — REVIEW при апгрейде.
risk: HIGH.
tests: tests/test_runtime_patcher.py::test_tool_limits*
```

### 8. `patch_assemble_outbound(agent, tool_audit_hook, recent_files_hook=...)` — runtime_patcher.py:695

```yaml
PATCH: assemble_outbound
target: AgentLoop._assemble_outbound (private)
nanobot_version: 0.3.0
purpose: >
  Главный интеграционный патч. Внедряет в metadata финального outbound:
  - _tool_audit (аудит вызовов инструментов из ToolAuditHook.drain);
  - context_window {used, limit, pct, model} (метрика M1);
  - media (auto-attach созданных файлов из RecentFilesHook);
  - _final_turn=True (маркер финализации оборота для postgres-канала);
  плюс синтетический OutboundMessage, если MessageTool вернул None.
why_not_hook: >
  Ни один хук nanobot не вызывается ПОСЛЕ сборки outbound; канал получает
  сообщение только отсюда. Без патча теряются аудит/UI-метрики/финализация.
public_alternative: runtime events (OutboundMessage.event) покрывают часть,
  но не дают injection в metadata — следить за развитием.
risk: CRITICAL — ломается семантика DB-логирования, UI-метрик и финализации
  оборота канала. getattr-защита даёт graceful skip, но функциональность деградирует.
tests: tests/test_runtime_patcher.py (много), tests/test_recent_files_hook.py,
  tests/test_gateway.py, tests/test_parallel_modes.py
```

### 9. `patch_subagent_logging(db_logging_service, session_manager)` — runtime_patcher.py:855

```yaml
PATCH: subagent_logging
target: nanobot.agent.subagent._SubagentHook (подмена ЦЕЛОГО КЛАССА в модуле)
nanobot_version: 0.3.0
purpose: >
  БД-логирование запусков подагентов: события тулов → DbLoggingService,
  итог subagent_run_finished, персист истории subagent:<task_id>.
  Штатный _SubagentHook пишет только debug в loguru.
why_not_hook: >
  SubagentManager создаёт внутренний хук жёстко; фабрики хуков туда не пробрасываются.
public_alternative: нет в 0.3.0; мониторить появление hook-factory для subagents.
risk: CRITICAL (любой рефактор SubagentRunner вверх по потоку).
tests: tests/test_runtime_patcher.py::test_subagent_logging*
```

### 10. `patch_project_tools(agent, workspace_dir, settings=...)` — runtime_patcher.py:1106

```yaml
PATCH: project_tools
target: ToolContext(...) конструктор (~17 kwargs) + agent.tools.register
nanobot_version: 0.3.0
purpose: >
  Auto-discover workspace/tools/*.py (pkgutil), создание ToolContext со всеми
  зависимостями AgentLoop, регистрация tool'ов через штатный registry.
  DI настроек: setattr(ctx, "_settings_ref", settings), setattr(ctx, "_agent_ref", agent)
  — обход ограничения pydantic ToolsConfig (отбрасывает неизвестные секции).
why_not_builtin: >
  Штатный loader nanobot грузит tool'ы только из config.json; кастомные секции
  конфига он отбрасывает. Это штатное РАСШИРЕНИЕ registry (register()),
  не его подмена.
public_alternative: частично есть (config-based tools); custom DI остаётся нашим.
risk: HIGH (сигнатура ToolContext.__init__).
tests: tests/test_runtime_patcher.py, tests/test_tools_project_loader.py
```

### 11. `patch_compaction_tracking(agent, settings)` — runtime_patcher.py:1305

```yaml
PATCH: compact_tracking
target: AutoCompact._archive (private) + Consolidator.maybe_consolidate_by_tokens (public)
nanobot_version: 0.3.0
purpose: >
  Единый путь записи факта сжатия: оба механизма авто-сжатия nanobot
  уведомляют ContextCompactionService._notify → служебная заметка в
  agent_conversation_messages (как при ручном /compact).
why_not_hook: compaction не публикует событий в 0.3.0.
public_alternative: следить за consolidation events.
risk: HIGH (_archive приватный; maybe_consolidate_by_tokens публичный).
tests: tests/test_runtime_patcher.py, tests/test_context_compaction.py
```

### 12. `patch_compact_command(agent, settings)` — runtime_patcher.py:1347

```yaml
PATCH: compact_command
target: agent.commands.exact(...) / agent.commands.prefix(...) (CommandRouter)
nanobot_version: 0.3.0
purpose: регистрация slash-команды /compact по образцу builtin cmd_new.
why_not_patch: это НЕ patch — штатная регистрация команды через публичный router.
risk: LOW.
tests: tests/test_runtime_patcher.py::test_compact_command*, tests/test_context_compaction.py
```

### 13. `patch_auto_compact_idle_guard(agent)` — runtime_patcher.py:1459

```yaml
PATCH: idle_guard
target: auto_compact.check_expired (замена no-op лямбдой)
nanobot_version: 0.3.0
purpose: >
  При idleCompactAfterMinutes=0 гасит дорогой sessions.list_sessions() на каждом
  тике AgentLoop.run (проект использует token-budget компакцию, idle выключен).
why_not_config: >
  nanobot 0.3.0 сам вызывает check_expired без проверки флага disabled —
  заглушка единственный способ.
public_alternative: ПРОВЕРИТЬ при апгрейде (возможно исправлено upstream).
risk: LOW (graceful: getattr с fallback).
tests: tests/test_runtime_patcher.py::test_idle_guard*
```

---

## Политика ведения

1. **Новый patch без записи здесь** = архитектурное нарушение (TARGET §20).
2. **При апгрейде nanobot:** пройти таблицу сверху вниз, для каждого патча сверить
   целевой API по changelog upstream; контракт каждого целевого API фиксируется
   в `tests/contract/` (см. test_compaction_api, test_subagent_api и др.).
3. **Кандидаты на удаление:** №6, №7 (если upstream даст конфигурацию лимитов),
   №13 (если починят guard). Отслеживать в [Unreleased] CHANGELOG.
4. **Запрещено** добавлять патчи вне этого класса (единственное исключение исторически:
   `BusFactory._wrap` для MessageBus и Jinja2-loader в `consolidator_locale` — оба
   задокументированы в nanobot-inventory.md §3.2).
