# Work brief `03-services-runtime`

Product files: **10**, LOC: **4757**

## `lib/services/runtime_patcher.py` — 2252 LOC (code 1834)
- module: `lib.services.runtime_patcher`
- docstring: RuntimePatcher — ВСЕ monkey-patch'и к фреймворку nanobot в одном месте. Устраняет дублирование между gateway.py и cli_agent.py: 1. ``patch_context_governor`` — большие результаты инструментов выгружаются в ``data_store/`
- static importers (12): `lib/core/application_context.py`, `lib/services/runtime_inventory.py`, `tests/test_application_context_single_application_point.py`, `tests/test_cli_agent.py`, `tests/test_gateway.py`, `tests/test_recent_files_hook.py`, `tests/test_runtime_inventory.py`, `tests/test_runtime_patcher.py`, `tests/test_runtime_patcher_e2e.py`, `tests/test_smoke_postgres_channel_media.py`, `tests/test_subagent_logging.py`, `tools/demo_internal_fallback.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 5, module functions: 6

### class `ContextWindowNotSeededError` — lines 102-114 (13 LOC), 0 methods
- bases: RuntimeError
- decorators: —
- docstring: ``DatabaseLoggingContextBridge`` не засеян для ``session_key``. Bridge seed'ит ``TurnRuntimeAdmitted``-подписка из ``RuntimeEventsSubscriber.start()``. Если подписчик не активен (например, в standalone-тестах без ``Appli
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `PatchSpec` — lines 215-252 (38 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Метаданные одного monkey-patch. Описывает ЗАЧЕМ патч существует, какой nanobot-API трогает, есть ли публичная альтернатива и какой риск при апгрейде nanobot. Нужен для audit-trail в ``PatchReport.details`` и для быстрой 
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `PatchReport` — lines 445-500 (56 LOC), 3 methods
- bases: object
- decorators: —
- docstring: Отчёт о применении патчей: что применено / пропущено / упало. Состояния: * ``applied`` — патч успешно применён; * ``skipped`` — патч не применён **по конфигурации** (порог = 0, фича выключена и т.п.); это не дефект; * ``
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 459-463 | `(self) -> None` | 1 | 0 | 6 | — |
| `to_dict` | 465-471 | `(self) -> dict` | 3 | 0 | 24 | — |
| `render` | 473-500 | `(self, *, specs: dict[str, PatchSpec] | None=None) -> str` | 11 | 0 | 3 | Человекочитаемая сводка для startup-диагностики. Формат: Runtime patches ---------------- ✓ context_governor ✓ |

### class `_OutboundSilencer` — lines 503-527 (25 LOC), 3 methods
- bases: object
- decorators: —
- docstring: Прокси для подавления outbound'а при вызове upstream ``TurnDelivery.fail``. Используется в ``RuntimePatcher.patch_turn_delivery_fail._wrap_fail``: на время вызова оригинального ``fail()`` ``self.bus`` подменяется на этот
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 520-521 | `(self, inner: Any) -> None` | 1 | 0 | 6 | — |
| `__getattr__` | 523-524 | `(self, name: str) -> Any` | 1 | 0 | 0 | — |
| `publish_outbound` | 526-527 | `(self, msg: Any) -> None` | 1 | 0 | 3 | — |

### class `RuntimePatcher` — lines 569-2246 (1678 LOC), 17 methods
- bases: object
- decorators: —
- docstring: Применение всех локальных доработок к фреймворку nanobot.
- name referenced in 12 file(s); tests: 9

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `apply_all` | 572-628 | `(self, config: Any, settings: Any, workspace_dir: Any, agent: Any, tool_audit_hook: Any, *` | 1 | 0 | 2 | Применить все патчи и вернуть отчёт. Args: config: runtime-конфиг nanobot (для ``session_key`` в патче). setti |
| `patch_specs` | 631-638 | `() -> dict[str, PatchSpec]` | 1 | 0 | 3 | Метаданные всех зарегистрированных патчей. Используется в startup-логах (через ``PatchReport.render(specs=...) |
| `_record` | 641-659 | `(report: PatchReport, name: str, result: tuple[bool, str]) -> None` | 4 | 12 | 3 | Записать результат одного патча в ``PatchReport``. ``True`` → ``applied`` (если в detail нет маркера ``[INTERN |
| `_format_workspace_hint` | 662-681 | `(workspace_dir: Any) -> str` | 6 | 0 | 0 | Краткая подсказка с путём до workspace в лог-сообщении. Используется в логах отдельных патчей (``patch_session |
| `patch_context_governor` | 689-775 | `(self, config: Any, settings: Any, workspace_dir: Any) -> tuple[bool, str]` | 15 | 1 | 2 | Выгружать большие результаты инструментов в data_store/. Алгоритм обёртки ``ContextGovernor.normalize_tool_res |
| `patch_save_turn` | 781-882 | `(self, settings: Any, workspace_dir: Any, agent: Any) -> tuple[bool, str]` | 25 | 1 | 2 | Архивировать большие результаты инструментов вместо усечения. ``_save_turn`` (nanobot/agent/loop.py) при сохра |
| `patch_document_text_threshold` | 888-1033 | `(self, settings: Any) -> tuple[bool, str]` | 25 | 1 | 2 | Единый универсальный механизм встраивания документов в user-промпт. ``nanobot.utils.document.reference_non_ima |
| `patch_session_content_cleanup` | 1035-1068 | `(self) -> tuple[bool, str]` | 3 | 1 | 1 | Вычищать невалидные символы из контента при добавлении сообщения. ``nanobot.session.manager.Session.add_messag |
| `patch_async_session_saves` | 1074-1144 | `(self, agent: Any) -> tuple[bool, str]` | 8 | 1 | 2 | Не блокировать event loop синхронным ``sessions.save()``. ``nanobot.agent.loop`` вызывает ``self.sessions.save |
| `patch_session_dir_watch` | 1150-1251 | `(self, agent: Any, workspace_dir: Any) -> tuple[bool, str]` | 22 | 1 | 1 | Снять показания вокруг ``SessionManager.save`` для расследования. Временный диагностический патч: ошибки вида  |
| `_bump_schema_max` | 1254-1282 | `(cls: Any, names: tuple, maximum: int) -> bool` | 9 | 3 | 1 | Поднять ``maximum`` у параметров схемы инструмента. ``tool_parameters`` хранит схему в замыкании ``parameters` |
| `patch_exec_limits` | 1288-1339 | `(self, settings: Any) -> tuple[bool, str]` | 11 | 1 | 2 | Поднять лимит вывода exec/shell-инструмента. nanobot режет вывод команды до ``MAX_OUTPUT_CHARS`` (50K символов |
| `patch_exec_timeout_cap` | 1345-1380 | `(self, settings: Any) -> tuple[bool, str]` | 6 | 1 | 1 | Поднять хардкод-потолок таймаута exec выше 600 сек. nanobot жёстко ограничивает per-call таймаут ``_MAX_TIMEOU |
| `patch_tool_limits` | 1386-1424 | `(self, settings: Any) -> tuple[bool, str]` | 16 | 1 | 2 | Поднять потолки инструментов, которые усекают вывод с маркером. Читаемые ключи из ``settings.gateway.tool_resu |
| `patch_assemble_outbound` | 1430-1580 | `(self, agent: Any, tool_audit_hook: Any, recent_files_hook: Any=None) -> tuple[bool, str]` | 28 | 1 | 4 | Подменить ``agent._assemble_outbound`` обёрткой, дописывающей аудит. Сигнатура upstream ``AgentLoop._assemble_ |
| `patch_turn_delivery_fail` | 1587-1805 | `(self, settings: Any, db_logging_service: Any=None, agent_id: str | None=None) -> tuple[bo` | 28 | 1 | 3 | Заменить ``TurnDelivery.fail`` обёрткой с заготовленным текстом. Upstream-``nanobot.agent.turn_delivery.TurnDe |
| `patch_subagent_logging` | 1811-2246 | `(self, db_logging_service: Any, session_manager: Any=None, *, bus: Any=None) -> tuple[bool` | 77 | 1 | 3 | Логировать подагентов: tool-события, итог запуска и историю. ``SubagentManager._run_subagent`` (``nanobot/agen |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_getloaded` | 40-49 | `(name: str)` | 2 | 1 | Вернуть уже импортированный модуль либо None. Используем ``sys.modules`` вместо ``import``: ``import nanobot.. |
| `_session_key_of` | 52-61 | `(msg: Any) -> str` | 2 | 1 | Вернуть session_key сообщения (``""`` если его нет/не строка). Нужен для дренажа аудита конкретной сессии: раз |
| `_resolve_media_path` | 64-84 | `(media_paths: list[str], basename: str) -> str` | 8 | 0 | Найти путь в ``media_paths`` по совпадению с ``basename``. Используется патчем ``patch_document_text_threshold |
| `_attach_context_window` | 117-211 | `(agent: Any, session_key: str, result: Any) -> None` | 28 | 1 | Внедрить ``metadata["context_window"]`` в финальный outbound. Метрика M1 (занятость окна): ``prompt_tokens`` п |
| `_classify_skip` | 429-442 | `(detail: str) -> bool` | 4 | 1 | True, если причина — конфигуративный skip (а не реальный сбой). ``_record`` использует это, чтобы решить: дета |
| `_resolve_agent_id` | 530-566 | `(config: Any, agent: Any) -> str | None` | 11 | 2 | Резолв идентификатора активного агента для передачи в патчи. Источники по убыванию приоритета: 1. ``config.age |

## `lib/services/context_compaction.py` — 597 LOC (code 530)
- module: `lib.services.context_compaction`
- docstring: ContextCompactionService — единая точка записи факта сжатия контекста. Три входа — один путь записи (заметка в ``agent_conversation_messages``, loguru INFO, опциональный Rich-вывод в терминал gateway): 1. **Ручной запуск
- static importers (6): `lib/cli/console_loop.py`, `tests/test_compaction_event_subscriber.py`, `tests/test_context_compaction.py`, `tests/test_context_compaction_user_id.py`, `tests/test_unified_event_logging_contract.py`, `workspace/tools/compact_context.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 2

### class `ContextCompactionService` — lines 63-574 (512 LOC), 15 methods
- bases: object
- decorators: —
- docstring: Единая точка запуска сжатия контекста (tool агента + CLI /compact).
- name referenced in 6 file(s); tests: 4

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 66-74 | `(self, agent: Any, settings: Any=None, *, db_logging_service: Any=None) -> None` | 2 | 0 | 6 | — |
| `enabled` | 77-78 | `(self) -> bool` | 2 | 0 | 9 | — |
| `notify_in_history` | 81-82 | `(self) -> bool` | 2 | 0 | 1 | — |
| `print_to_terminal` | 85-86 | `(self) -> bool` | 2 | 0 | 1 | — |
| `compact` | 88-197 | `(self, session_key: str | None=None, *, idle: bool=False, force: bool=False, max_suffix: i` | 23 | 0 | 2 | Сжать контекст сессии и вернуть отчёт. Args: session_key: ключ сессии. ``None`` — берётся из текущего request  |
| `format_report` | 200-234 | `(report: dict) -> str` | 24 | 2 | 3 | Человекочитаемое представление отчёта. Структура текста (для ``ok=True`` и ``archived > 0``): 1. Полная сводка |
| `_estimate` | 236-253 | `(self, session: Any, runtime: Any) -> tuple[int, str]` | 5 | 2 | 1 | — |
| `_estimate_fallback` | 256-285 | `(session: Any, runtime: Any) -> tuple[int, str]` | 15 | 1 | 2 | Грубая fallback-оценка токенов, если ``estimate_session_prompt_tokens`` упал. Считаем примерный размер по ``me |
| `_current_session_key` | 288-293 | `() -> str | None` | 2 | 1 | 1 | — |
| `_empty` | 295-307 | `(self, reason: str) -> dict` | 1 | 7 | 1 | — |
| `_notify` | 309-324 | `(self, session_key: str, report: dict) -> None` | 4 | 2 | 3 | — |
| `_record_event_log` | 326-388 | `(self, session_key: str, report: dict, text: str) -> None` | 10 | 2 | 4 | Записать событие ``context_compacted`` в долговечный журнал ``agent_gateway_logs``. Закрывает gap №1 из ``docs |
| `record_external_compaction` | 390-435 | `(self, *, session_key: str, mode: str, summary: str | None, archived_msgs: int, kept_msgs:` | 5 | 0 | 2 | Записать факт сжатия, выполненного штатным кодом nanobot. Используется из обёрток ``runtime_patcher.patch_comp |
| `notify_session_compacted` | 437-516 | `(self, *, session_key: str, phase: str, compaction_id: str) -> None` | 7 | 0 | 2 | Записать факт compaction-фазы upstream (``ContextCompactionEvent``). Вызывается из ``CompactionEventSubscriber |
| `_write_history_notice` | 518-574 | `(self, session_key: str, report: dict) -> None` | 13 | 2 | 3 | Записать заметку о сжатии в ``agent_conversation_messages``. Поддерживает session_key видов ``postgres:<chat_i |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_get_setting` | 47-60 | `(settings: Any, *keys: str, default: Any=None) -> Any` | 6 | 1 | Прочитать значение из SETTINGS (dict или объект с атрибутами). |
| `_current_request_sender_id` | 577-597 | `() -> str | None` | 6 | 3 | ``RequestContext.sender_id`` текущего request (или ``None``). Используется :py:meth:`ContextCompactionService. |

## `lib/services/runtime_inventory.py` — 370 LOC (code 308)
- module: `lib.services.runtime_inventory`
- docstring: Канонические списки runtime-инвентаря. Single source of truth для ожидаемых хуков, project tools и runtime-патчей. Используется: * в ``ApplicationContext._log_connected_hooks()`` и после ``apply_all()`` для prominent-лог
- static importers (4): `lib/core/application_context.py`, `tests/test_runtime_inventory.py`, `tests/test_runtime_patcher.py`, `tools/diagnose_startup.py`
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 3, module functions: 11

### class `HookSpec` — lines 37-44 (8 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Каноническая спецификация одного хука.
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `ToolSpec` — lines 48-55 (8 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Каноническая спецификация одного project tool'а.
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `RuntimePatchSpec` — lines 59-65 (7 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Каноническая спецификация одного runtime-патча.
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `canonical_framework_hooks` | 68-85 | `() -> list[HookSpec]` | 1 | 2 | Фреймворковые хуки из ``lib/hooks/*`` (всегда в ``ctx.hooks``). |
| `canonical_plugin_hooks` | 88-112 | `() -> list[HookSpec]` | 1 | 2 | Plugin-хуки из ``workspace/hooks/*`` (auto-scan через allowlist). |
| `canonical_hook_factories` | 115-125 | `() -> list[HookSpec]` | 1 | 1 | Per-turn hook factories (создаются по одному инстансу на request). |
| `canonical_project_tools` | 128-159 | `() -> list[ToolSpec]` | 1 | 2 | Project tools из ``workspace/tools/*.py`` (auto-discover). |
| `canonical_runtime_patches` | 162-184 | `() -> list[RuntimePatchSpec]` | 2 | 3 | Все runtime-патчи из ``RuntimePatcher.patch_specs()``. ``required`` берётся напрямую из ``PatchSpec.required`` |
| `collect_actual_hook_names` | 187-195 | `(ctx: object) -> tuple[list[str], int]` | 4 | 1 | Вернуть ``(имена классов в ctx.hooks, кол-во factories)``. Безопасно работает с любым ctx-like объектом (для т |
| `diff_hooks` | 198-237 | `(actual_hook_names: list[str], *, actual_factory_count: int) -> dict[str, list[str]]` | 11 | 3 | Сравнить фактические хуки с каноническими списками. Args: actual_hook_names: имена классов хуков в ``ctx.hooks |
| `diff_project_tools` | 240-282 | `(registered: list[str], skipped_disabled: list[str], *, failed: list[str] | None=None) -> ` | 8 | 4 | Сравнить фактический список project tools с каноническим. Args: registered: tool'ы, успешно зарегистрированные |
| `diff_runtime_patches` | 285-324 | `(applied: list[str], skipped: list[tuple[str, str]], failed: list[tuple[str, str]]) -> dic` | 10 | 3 | Сравнить фактический ``PatchReport`` с каноническим списком патчей. Args: applied: имена патчей из ``patch_rep |
| `parse_project_tools_detail` | 327-360 | `(detail: str) -> dict[str, list[str]]` | 14 | 2 | Распарсить ``detail`` из ``ProjectToolsLoadResult.detail`` в structured-формат. Формат ``detail`` (см. ``lib/s |
| `diff_project_tools_from_detail` | 363-370 | `(detail: str) -> dict[str, list[str]]` | 1 | 0 | Сравнить ``project_tools`` ``detail`` с каноническим списком. |

## `lib/services/project_tool_loader.py` — 349 LOC (code 304)
- module: `lib.services.project_tool_loader`
- docstring: Loader для кастомных tool'ов из ``workspace/tools/*.py``. Заменяет ``RuntimePatcher.patch_project_tools`` (который жил в ``runtime_patcher.py`` до opencode change ``runtime-patcher-composition-cleanup``). Это **stateless
- static importers (5): `lib/core/application_context.py`, `tests/test_application_context_single_application_point.py`, `tests/test_context_compaction.py`, `tests/test_gateway.py`, `tests/test_tools_project_loader.py`
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 1, module functions: 3

### class `ProjectToolsLoadResult` — lines 39-78 (40 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Результат регистрации project tools. Attributes: registered: имена tool'ов, успешно зарегистрированных в ``agent.tools`` (порядок: как прошли discovery и DI). disabled: имена tool'ов, пропущенных по конфигурации (``Tool.
- name referenced in 4 file(s); tests: 3

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_discover` | 81-141 | `(workspace_dir: Path) -> list[type]` | 19 | 1 | Discover ``Tool``-подклассы в ``<workspace>/tools/*.py``. Приватная функция — наружу торчит только ``register_ |
| `_build_tool_context` | 144-193 | `(agent: Any, settings: Any, cache_store: Any, db_logging_service: Any) -> Any` | 5 | 1 | Собрать ``ToolContext`` из атрибутов ``AgentLoop``. Тот же набор kwargs, что был в ``RuntimePatcher.patch_proj |
| `register_project_tools` | 196-349 | `(agent: Any, workspace_dir: Any, *, settings: Any=None, cache_store: Any=None, db_logging_` | 25 | 3 | Discover + DI + register project tools из ``<workspace>/tools/``. Best-effort с частичным успехом: ошибка одно |

## `lib/services/runtime_events_subscriber.py` — 334 LOC (code 277)
- module: `lib.services.runtime_events_subscriber`
- docstring: Подписчик на runtime-события nanobot 0.3.5. Использует публичный pub-sub API: * `nanobot.bus.queue.MessageBus.subscribe(handler, EventType)` (`nanobot/bus/queue.py:92`) — локальный fan-out, доступен **только** при публик
- static importers (2): `lib/core/application_context.py`, `tests/test_runtime_events_subscriber.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 2

### class `RuntimeEventsSubscriber` — lines 98-331 (234 LOC), 6 methods
- bases: object
- decorators: —
- docstring: Observer-сервис для runtime-событий nanobot 0.3.5. Сигнатура полностью описана в `openspec/changes/runtime-events- subscription/specs/runtime/runtime-events-observability/spec.md` и расширена в `openspec/changes/post-0.3
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 107-115 | `(self, bus: Any, db_logging_service: Any | None=None) -> None` | 1 | 0 | 6 | — |
| `start` | 117-156 | `(self) -> None` | 2 | 0 | 21 | Зарегистрировать подписки на TurnRuntimeAdmitted через ``bus.subscribe(...)``. KOMMIT-1 (runtime-events-subscr |
| `stop` | 158-176 | `(self) -> None` | 3 | 0 | 16 | Дерегистрировать подписки в LIFO-порядке. Вызывать ДО ``MessageBus.drain()`` (порядок важен: in-flight handler |
| `_handle_turn_runtime_admitted` | 178-198 | `(self, event: TurnRuntimeAdmitted) -> None` | 6 | 0 | 2 | Seed лимита окна/модели в мост ``_CONTEXT_BRIDGE``. Вызывается из upstream `RuntimeEventPublisher.turn_runtime |
| `_handle_turn_completed` | 200-281 | `(self, event: TurnCompleted) -> None` | 24 | 0 | 2 | Записать ``LogEvent(event_type="turn_completed")`` в ``agent_gateway_logs`` через ``DbLoggingService``. Пишет  |
| `_handle_subagent_turn_completed` | 283-331 | `(self, event: SubagentTurnCompleted) -> None` | 10 | 0 | 2 | Записать ``LogEvent(event_type="subagent_run_finished")``. Контракт payload ИДЕНТИЧЕН ``_SubagentLoggingHook._ |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_set_subagent_default_bus` | 58-76 | `(bus: Any) -> None` | 3 | 1 | Установить bus для автопривязки к новым инстансам ``_SubagentLoggingHook``. Использует monkey-patch set_defaul |
| `_set_subagent_subscriber_registered` | 79-95 | `(registered: bool) -> None` | 3 | 1 | Отметить, что ``SubagentLoggingSubscriber`` активен. Это сигнал ``_SubagentLoggingHook._finalize`` пропустить  |

## `lib/services/schema_validation.py` — 284 LOC (code 237)
- module: `lib.services.schema_validation`
- docstring: Startup schema validation — проверка наличия обязательных runtime-таблиц. Единая точка проверки перед подъёмом сервисов ``ApplicationContext.start()``. Источник имён — ``SETTINGS["channels"]["postgres"]`` и ``SETTINGS["l
- static importers (4): `lib/core/application_context.py`, `tests/test_application_context_schema_validation.py`, `tests/test_gateway_entrypoint_schema_validation.py`, `tests/test_schema_validation.py`
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 4, module functions: 2

### class `MissingTable` — lines 85-99 (15 LOC), 1 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Описание одной недостающей таблицы. Attributes: schema: имя схемы (например, ``"public"``). name: короткое имя таблицы.
- name referenced in 4 file(s); tests: 3

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `full_name` | 97-99 | `(self) -> str` | 1 | 0 | 2 | Полное имя в формате ``schema.table``. |

### class `SchemaValidationError` — lines 102-123 (22 LOC), 2 methods
- bases: ConfigurationError
- decorators: —
- docstring: Блокирует старт при отсутствии обязательных runtime-таблиц. Наследник ``ConfigurationError`` попадает в единый startup-boundary ``gateway.main()`` / ``cli_agent.main()`` (exit 2 + stderr).
- name referenced in 4 file(s); tests: 3

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 109-112 | `(self, missing: list[MissingTable], profile: str) -> None` | 1 | 0 | 6 | — |
| `_build_message` | 114-123 | `(self) -> str` | 3 | 1 | 1 | — |

### class `_MissingConfigKeys` — lines 126-153 (28 LOC), 2 methods
- bases: SchemaValidationError
- decorators: —
- docstring: Срабатывает, когда в settings отсутствуют ожидаемые ключи. Это уже ошибка конфигурации, но используем тот же класс, чтобы entrypoint не различал «нет таблиц» / «нет ключей в settings». Формат сообщения — отдельный (``Mis
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 135-139 | `(self, missing_keys: list[str], profile: str) -> None` | 1 | 0 | 6 | — |
| `_build_config_message` | 141-153 | `(self) -> str` | 3 | 1 | 1 | — |

### class `SchemaValidationService` — lines 156-275 (120 LOC), 3 methods
- bases: object
- decorators: —
- docstring: Сервис pre-startup проверки схемы. Использование:: svc = SchemaValidationService() svc.validate(ctx.settings, fetch=fetch) # None → ОК # либо try: svc.validate(ctx.settings, fetch=fetch) except SchemaValidationError as e
- name referenced in 3 file(s); tests: 2

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `expected_table_names` | 173-202 | `(settings: Any) -> list[tuple[str, str]]` | 11 | 1 | 3 | Извлечь список ``(schema, table_name)`` из merged SETTINGS. Принимает как сырой dict, так и ``_LazySettings``  |
| `check_tables` | 205-243 | `(fetch: Callable[..., list[dict[str, Any]]], expected: list[tuple[str, str]], *, timeout_s` | 8 | 1 | 2 | Один SELECT к ``information_schema.tables``. Args: fetch: callable с сигнатурой ``(sql, *params) -> list[dict] |
| `validate` | 246-275 | `(cls, settings: Any, *, fetch: Callable[..., list[dict[str, Any]]], timeout_sec: float=DEF` | 4 | 0 | 5 | Верхний уровень: получить expected → проверить → raise при missing. Args: settings: merged SETTINGS (с ``profi |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_hint_for_profile` | 38-58 | `(profile: str) -> str` | 3 | 2 | Actionable-подсказка для создания runtime-таблиц по профилю. Возвращает команду CLI, которую оператор должен з |
| `_unwrap_settings` | 61-81 | `(settings: Any) -> dict[str, Any]` | 5 | 1 | Развернуть ``_LazySettings`` proxy в сырой ``dict``. ``ctx.settings`` хранит ``_LazySettings`` (см. ``config.p |

## `lib/services/runtime_health.py` — 210 LOC (code 169)
- module: `lib.services.runtime_health`
- docstring: RuntimeHealth / RuntimeReadiness — operational status. Различает два понятия: * ``health`` (liveness): процесс жив, asyncio-loop работает, не в shutdown. Это «пульс» — отвечает всегда, если процесс не висит. * ``readines
- static importers (2): `lib/core/application_context.py`, `tests/test_runtime_health.py`
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 4, module functions: 2

### class `ComponentStatus` — lines 37-52 (16 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Состояние одной зависимости. Attributes: name: имя компонента (например, ``"postgres"``, ``"duckdb_cache"``). required: True если без этого компонента задачи не могут обрабатываться. False — optional (например, vector se
- name referenced in 3 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `ReadinessReport` — lines 56-78 (23 LOC), 1 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Итог проверки зависимостей. Содержит список ``components`` (по одной записи на зависимость) и агрегированный ``status``. Вычисляется через ``compute_overall_status``.
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `to_dict` | 66-78 | `(self) -> dict[str, Any]` | 2 | 0 | 24 | — |

### class `RuntimeHealth` — lines 102-145 (44 LOC), 6 methods
- bases: object
- decorators: —
- docstring: Liveness-проверка процесса. Минимальнаяльная: пульс процесса жив, asyncio-loop работает. Можно расширить таймером последнего heartbeat-callback (если процесс висит на синхронном вызове, ``is_alive()`` всё равно вернёт Tr
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 110-112 | `(self) -> None` | 1 | 0 | 6 | — |
| `mark_started` | 114-115 | `(self) -> None` | 1 | 0 | 1 | — |
| `mark_stopped` | 117-118 | `(self) -> None` | 1 | 0 | 1 | — |
| `is_alive` | 120-125 | `(self) -> bool` | 3 | 1 | 2 | — |
| `status` | 127-128 | `(self) -> HealthStatus` | 2 | 0 | 15 | — |
| `get_stats` | 130-145 | `(self) -> dict[str, Any]` | 4 | 0 | 7 | Агрегированные operational stats. Включает базовый liveness (``started_at``, ``uptime_seconds``). Расширения ( |

### class `RuntimeReadiness` — lines 148-205 (58 LOC), 3 methods
- bases: object
- decorators: —
- docstring: Readiness-проверка зависимостей. Вычисляется **по запросу** — без фонового опроса. Это даёт оператору моментальный снимок состояния. Если нужна непрерывная проверка (heartbeat-style), расширяется отдельным компонентом. К
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 160-161 | `(self) -> None` | 1 | 0 | 6 | — |
| `register` | 163-173 | `(self, name: str, fn: Any, *, required: bool=True) -> None` | 1 | 0 | 12 | Зарегистрировать проверку. Args: name: имя компонента (postgres / duckdb / vector_search / ...). fn: callable( |
| `check` | 175-205 | `(self) -> ReadinessReport` | 5 | 0 | 8 | Запустить все проверки и вернуть итоговый отчёт. |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `compute_overall_status` | 81-99 | `(components: list[ComponentStatus]) -> ReadinessStatus` | 9 | 3 | Свести список компонентов в общий статус. Правила: * Хотя бы один required DOWN → ``NOT_READY``. * Required вс |
| `_now` | 208-210 | `() -> float` | 1 | 1 | — |

## `lib/services/channel_factory.py` — 191 LOC (code 155)
- module: `lib.services.channel_factory`
- docstring: ChannelFactory — создание и настройка всех каналов связи. Перенесено из gateway.py: * ``ChannelManager`` (стандартные каналы nanobot: Telegram, Slack и т.д.); * Redis-канал по секции ``settings.channels.redis``; * Postgr
- static importers (5): `gateway.py`, `tests/test_channel_factory.py`, `tests/test_gateway.py`, `tests/test_gateway_live_media_e2e.py`, `tests/test_parallel_modes.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 0

### class `ChannelFactory` — lines 21-191 (171 LOC), 4 methods
- bases: object
- decorators: —
- docstring: Фабрика каналов: стандартные (Telegram/Slack/...) + Redis + Postgres. Стандартные каналы nanobot регистрируются самим ``ChannelManager`` на основе ``config.channels.<name>.enabled``. Эта фабрика добавляет Redis/Postgres,
- name referenced in 5 file(s); tests: 4

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 34-42 | `(self, transcription: Any | None=None, print_worker_activity: bool=False, db_logging_servi` | 1 | 0 | 6 | — |
| `create_all` | 44-78 | `(self, config: Any, settings: Any, bus: Any, session_manager: Any) -> tuple[Any, list[str]` | 2 | 0 | 5 | Создать и настроить все каналы. Args: config: runtime-конфиг nanobot (для ``config.channels.send_progress`` и  |
| `_add_redis` | 84-123 | `(self, channels: Any, config: Any, settings: Any, bus: Any) -> list[str]` | 13 | 1 | 2 | Зарегистрировать Redis-канал (если включён в ``settings.channels.redis``). Канал поверх Redis pub/sub: читает  |
| `_add_postgres` | 129-191 | `(self, channels: Any, config: Any, settings: Any, bus: Any) -> list[str]` | 15 | 1 | 3 | Зарегистрировать Postgres-канал (если включён в ``settings.channels.postgres``). Канал поверх таблицы ``agent_ |

## `lib/services/compaction_event_subscriber.py` — 106 LOC (code 90)
- module: `lib.services.compaction_event_subscriber`
- docstring: CompactionEventSubscriber — единый наблюдатель upstream-событий компакции. Подписывается на ``OutboundMessage.event`` типа ``nanobot.events.ContextCompactionEvent`` (через фильтрацию ``bus.outbound`` при чтении каналом —
- static importers (1): `tests/test_compaction_event_subscriber.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 0

### class `CompactionEventSubscriber` — lines 34-106 (73 LOC), 3 methods
- bases: object
- decorators: —
- docstring: Подписчик на ``OutboundMessage.event`` типа ``ContextCompactionEvent``. Канал (postgres/redis/streamlit) вызывает ``feed(outbound)`` при каждом ``OutboundMessage``. Subscriber фильтрует события по ``isinstance(msg.event,
- name referenced in 1 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 48-53 | `(self, *, compaction_service: Any=None) -> None` | 1 | 0 | 6 | — |
| `set_service` | 55-62 | `(self, compaction_service: Any) -> None` | 1 | 0 | 1 | Установить ``ContextCompactionService`` после конструирования. Подходит для случая, когда ``RuntimePatcher.app |
| `feed` | 64-106 | `(self, outbound: Any) -> None` | 12 | 0 | 2 | Обработать один ``OutboundMessage``. Если ``outbound.event`` — ``ContextCompactionEvent``, зовёт ``ContextComp |

## `lib/services/consolidator_locale.py` — 64 LOC (code 52)
- module: `lib.services.consolidator_locale`
- docstring: Переопределение системных шаблонов nanobot из ``workspace/overrides/``. Consolidator (auto-compact токенов/idle и ручное сжатие через ``ContextCompactionService``) рендерит инструкцию извлечения фактов из ``nanobot/templ
- static importers (2): `lib/core/application_context.py`, `tests/test_consolidator_locale.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 2

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_overrides_dir` | 30-33 | `() -> Path` | 1 | 2 | Каталог переопределений шаблонов (``workspace/overrides``). |
| `apply_template_overrides` | 36-64 | `(overrides_dir: str | Path | None=None) -> bool` | 8 | 2 | Подложить каталог переопределений в loader шаблонов nanobot. Идемпотентен: повторный вызов не вкладывает ``Cho |

