# Nanobot Dependency Inventory

> Инвентаризация всех зависимостей `workspaces_nanobot` от `nanobot-ai==0.3.0`.
> Машино-читаемая версия: [`nanobot-inventory.json`](nanobot-inventory.json).
> Регенерация JSON: `python tools/scan_nanobot_inventory.py`.

**Дата скана:** 2026-08-24 · **nanobot pinned:** 0.3.0 · **файлов просканировано:** 85

## Легенда классификации

| Класс | Значение | Риск апгрейда |
|---|---|---|
| 🟢 GREEN | public/stable extension point | низкий |
| 🟡 YELLOW | public API, но tightly coupled (внутренние CLI-хелперы, корневая обёртка) | средний |
| 🟠 ORANGE | internal implementation (нестабильный контракт) | высокий |
| 🔴 RED | private API (`_name`), monkey patch, private state | критический |

---

## 1. Сводка

| Метрика | Значение |
|---|---|
| Всего прямых импортов `nanobot.*` | 41 |
| GREEN / YELLOW / ORANGE / RED импортов | 32 / 0* / 4 / 5* |
| Точек `getattr(obj, "_private")` | 20 |
| Точек `setattr` (monkey patch) | 2 (+ присваивания методов внутри RuntimePatcher) |

\* часть YELLOW-точек (многострочные импорты `_init_prompt_session`, `_read_interactive_input_async` и т.п.)
и RED-классификация уточняются вручную ниже — автоматический сканер консервативен.

---

## 2. Прямые импорты по файлам

### 2.1. Каналы (GREEN)

| Файл:строка | Импорт | Назначение |
|---|---|---|
| `lib/channels/postgres_channel.py:44-46` | `InboundMessage`, `OutboundMessage`, `MessageBus`, `BaseChannel` | транспорт сообщений |
| `lib/channels/redis_channel.py:96-98` | те же | транспорт сообщений |
| `lib/services/channel_factory.py:65` | `ChannelManager` | реестр каналов |

### 2.2. Ядро (GREEN)

| Файл:строка | Импорт | Назначение |
|---|---|---|
| `lib/core/agent_factory.py:106` | `AgentLoop` | `AgentLoop.from_config(...)` — штатная точка сборки |
| `lib/core/bus_factory.py:70` | `MessageBus` | создание шины |
| `lib/core/application_context.py:617` | `CronService` | cron для CLI-режима |

### 2.3. Хуки (GREEN)

| Файл:строка | Импорт | Назначение |
|---|---|---|
| `lib/hooks/base_tool_tracking_hook.py:18` | `AgentHook` | базовый класс хуков |
| `lib/hooks/tool_audit_hook.py:14` | `AgentHookContext` | тип контекста |
| `lib/hooks/database_logging_hook.py:28` | `AgentHookContext`, `AgentRunHookContext` | аннотации |
| `lib/cli/hook_loader.py:41` | `AgentHook` | `issubclass` при auto-scan плагинов |
| `workspace/hooks/recent_files_hook.py:35` | `AgentHook` | плагин-хук |
| `workspace/hooks/session_file_redirect_hook.py:48` | `AgentHook` | плагин-хук |

### 2.4. Tools (GREEN)

| Файл:строка | Импорт | Назначение |
|---|---|---|
| `workspace/tools/duckdb_query_tool.py:43` | `Tool`, `ToolResult`, `tool_parameters` | generic SQL tool |
| `workspace/tools/vector_search_tool.py:43` | `Tool`, `tool_parameters` | generic vector tool |
| `workspace/tools/compact_context.py:34` | `Tool`, `ToolResult`, `tool_parameters` | ручное сжатие |
| `workspace/tools/example.py:39` | `Tool`, `tool_parameters` | шаблон |

### 2.5. Сессии (YELLOW/RED)

| Файл:строка | Импорт | Класс | Примечание |
|---|---|---|---|
| `lib/session/pg_session_manager.py:35` | `Session`, `SessionManager` | GREEN | публичный контракт |
| `lib/session/pg_session_manager.py:35` | `_message_preview_text` | **RED** | приватная функция превью; альтернатива — своя реализация (~10 строк) |
| `lib/services/session_storage.py:152` | `SessionManager` | GREEN | file-mode фолбэк |

### 2.6. Сервисы (ORANGE)

| Файл:строка | Импорт | Класс | Назначение |
|---|---|---|---|
| `lib/services/context_compaction.py:142` | `replay_max_messages_for_context` | GREEN | расчёт replay-окна |
| `lib/services/context_compaction.py:280` | `current_request_session_key` | ORANGE | request context API, контракт не устаканился |
| `lib/services/runtime_patcher.py:295` | `ContextGovernor` | ORANGE | цель патча `normalize_tool_result` |
| `lib/services/runtime_patcher.py:296` | `ensure_nonempty_tool_result` | GREEN* | утилита nanobot |
| `lib/services/runtime_patcher.py:470,521` | `Session` | GREEN | типизация патчей сессий |
| `lib/services/runtime_patcher.py:770` | `OutboundMessage` | GREEN | синтетический outbound |
| `lib/services/runtime_patcher.py:892` | `_SubagentHook` | **RED** | подмена класса подагента целиком |
| `lib/services/runtime_patcher.py:1095` | `import nanobot.agent.subagent as _mod` | ORANGE | setattr модуля |
| `lib/services/runtime_patcher.py:1188` | `Tool as _T` | GREEN | issubclass при discover |
| `lib/services/runtime_patcher.py:1218` | `ToolContext` | ORANGE | конструктор с ~17 kwargs — глубокая завязка |

### 2.7. CLI / конфиг (RED/YELLOW)

| Файл:строка | Импорт | Класс | Назначение |
|---|---|---|---|
| `lib/cli/console_loop.py:176` | `InboundMessage` | GREEN | публикация в шину из REPL |
| `lib/cli/console_loop.py:177-186` | `_init_prompt_session`, `_is_exit_command`, `_model_display`, `_read_interactive_input_async`, `_restore_terminal`, `_sanitize_surrogates` | **RED** | приватные хелперы REPL `nanobot cli`; изоляция: тонкий адаптер `lib/cli/nanobot_cli_compat.py` |
| `lib/commands/compact_command.py:39` | `OutboundMessage` | GREEN | результат slash-команды |
| `lib/services/config_service.py:115` | `_load_runtime_config` | **RED** | приватная сборка runtime-конфига; альтернативы нет — фиксировать в contract tests |
| `lib/services/config_service.py:116` | `sync_workspace_templates` | YELLOW | публичная, но нестабильная утилита |
| `lib/services/consolidator_locale.py:47` | `prompt_templates as _pt` | ORANGE | мутация `_pt._environment().loader` (Jinja2 ChoiceLoader) |
| `gateway.py:47` | `__logo__`, `__version__` | YELLOW | баннер |

---

## 3. Динамические обращения к private API

### 3.1. `getattr(obj, "_private")` — точки чтения

| Файл:строка | Выражение | Зачем | Risk |
|---|---|---|---|
| `runtime_patcher.py:77` | `agent._last_usage` | накопленный usage итераций (context-window) | HIGH |
| `runtime_patcher.py:231` | `agent._state_build` | seed context bridge | HIGH |
| `runtime_patcher.py:381` | `agent._save_turn` | архивация больших tool-results | HIGH |
| `runtime_patcher.py:748` | `agent._assemble_outbound` | главный патч outbound metadata | CRITICAL |
| `runtime_patcher.py:1226` | `agent._exec_session_manager` | проброс в ToolContext | HIGH |
| `runtime_patcher.py:1232` | `agent._image_generation_provider_configs` | проброс в ToolContext | HIGH |
| `runtime_patcher.py:1389` | `auto_compact._archive` | tracking авто-сжатия | HIGH |
| `runtime_patcher.py:1479` | `auto_compact._ttl` | idle-minutes guard | MEDIUM |
| `context_compaction.py:115-117` | `agent.consolidator/sessions/runtime_for_session` | публичные атрибуты AgentLoop, защитные getattr | LOW |
| `tools/*.py` | `ctx._settings_ref` / `ctx._agent_ref` | собственная DI-конвенция проекта (не nanobot) | LOW (наша) |

### 3.2. `setattr` / переприсваивание — точки записи

Все monkey patches сосредоточены в `RuntimePatcher` (см. §4) + два DI-setattr
(`ctx._agent_ref/_settings_ref`) + `BusFactory._wrap` (обёртка publish_inbound/outbound)
+ `consolidator_locale.apply_template_overrides` (Jinja2 loader).

---

## 4. Связанные документы

- [runtime-patcher-inventory.md](runtime-patcher-inventory.md) — каталог каждого патча.
- [../../tests/contract/](../../tests/contract/) — contract tests, фиксирующие используемый API nanobot.

## 5. Политика обновления инвентаря

1. После любого изменения импортов `nanobot.*` — перезапустить
   `python tools/scan_nanobot_inventory.py` и закоммитить обновлённый JSON.
2. При апгрейде nanobot — сверить diff инвентаря с changelog upstream:
   каждая RED/ORANGE строка должна быть либо подтверждена, либо мигрирована.
3. Новые RED-зависимости без записи в этом документе = архитектурное нарушение.
