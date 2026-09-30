# Work brief `05-hooks-cli-lifecycle`

Product files: **16**, LOC: **2139**

## `lib/hooks/database_logging_hook.py` — 559 LOC (code 430)
- module: `lib.hooks.database_logging_hook`
- docstring: DatabaseLoggingHook — AgentHook для логирования событий агента в БД. Реализуется как ``AgentHook`` (async-методы из nanobot.agent.hook) для tool-событий, и использует ``BusFactory`` (обёртки ``publish_inbound`` / ``publi
- static importers (20): `lib/channels/postgres_channel.py`, `lib/core/agent_factory.py`, `lib/services/runtime_events_subscriber.py`, `lib/services/runtime_patcher.py`, `tests/_patcher_fixtures.py`, `tests/contract/test_composite_hook_lifecycle.py`, `tests/contract/test_database_logging_get_model.py`, `tests/contract/test_usage_to_dict_contract.py`, `tests/test_agent_factory.py`, `tests/test_cli_agent.py`, `tests/test_database_logging_bridge.py`, `tests/test_hooks_database_logging.py`, `tests/test_postgres_channel.py`, `tests/test_recent_files_hook.py`
- string/dynamic refs: 3
- test files touching it: — none —
- classes: 1, module functions: 10

### class `DatabaseLoggingHook` — lines 283-524 (242 LOC), 10 methods
- bases: AgentHook
- decorators: —
- docstring: Агентский хук — пересылает tool- и run-события в DbLoggingService. Живёт в ``lib/hooks/``: это фреймворковый хук, а не плагин ``workspace/hooks/``. Он требует обязательный ``db_logging_service`` в конструкторе, который `
- name referenced in 7 file(s); tests: 5

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 310-340 | `(self, db_logging_service: Any, agent_id: str | None=None, *, session_key: str | None=None` | 1 | 0 | 6 | — |
| `_capture_context` | 346-352 | `(self, context: Any) -> None` | 2 | 3 | 1 | Подхватить session_key и request_id текущего вопроса. |
| `_ctx` | 354-361 | `(self, key: str) -> str | None` | 3 | 0 | 0 | — |
| `before_execute_tool` | 363-382 | `(self, context: AgentHookContext, tool_call: Any, tool: Any, params: Any) -> None` | 5 | 0 | 4 | — |
| `after_execute_tool` | 384-406 | `(self, context: AgentHookContext, tool_call: Any, tool: Any, params: Any, result: Any) -> ` | 5 | 0 | 5 | — |
| `on_execute_tool_error` | 408-432 | `(self, context: AgentHookContext, tool_call: Any, tool: Any, params: Any, error: Any) -> N` | 5 | 0 | 1 | — |
| `before_iteration` | 438-445 | `(self, context: Any) -> None` | 2 | 0 | 4 | — |
| `after_iteration` | 447-484 | `(self, context: Any) -> None` | 10 | 0 | 6 | — |
| `_print_llm_tokens` | 486-505 | `(self, context: Any) -> None` | 9 | 1 | 1 | Вывести в терминал две строки о токенах итерации (CLI-режим). Раньше использовался ``Rich Console.print("[dim] |
| `after_run` | 507-524 | `(self, context: AgentRunHookContext) -> None` | 8 | 0 | 4 | — |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `seed_context_window` | 60-75 | `(session_key: str | None, *, limit: int=0, model: str='') -> None` | 5 | 10 | Засеять лимит окна/модель в мост на старте оборота. Вызывается из патча ``agent._state_build`` (RuntimePatcher |
| `_store_iteration_usage` | 78-92 | `(session_key: str | None, usage: Any) -> None` | 4 | 5 | Записать по-итерационный usage оборота для сессии (неблокирующий). Принимает как ``dict`` (legacy), так и ``LL |
| `_store_context_window` | 95-102 | `(session_key: str | None, block: dict | None) -> None` | 4 | 3 | Записать готовый блок ``context_window`` для сессии (из патча). |
| `get_context_window` | 105-136 | `(session_key: str | None) -> dict | None` | 19 | 4 | Вернуть блок ``context_window`` сессии (без удаления). Предпочитаем готовый блок, собранный патчем ``_assemble |
| `get_iteration_usage` | 139-146 | `(session_key: str | None) -> dict | None` | 7 | 2 | Прочитать по-итерационный usage сессии (без удаления). |
| `pop_context_bridge` | 149-154 | `(session_key: str | None) -> None` | 3 | 5 | Снять с моста все данные сессии (финализация/ошибка оборота). |
| `make_db_logging_hook_factory` | 157-226 | `(db_logging_service: Any, agent_id: str | None=None, print_llm_calls: bool=False, get_mode` | 6 | 3 | Фабрика: создать СВЕЖИЙ ``DatabaseLoggingHook`` на КАЖДЫЙ оборот. Передаётся в ``AgentLoop`` как ``hook_factor |
| `_current_request_sender_id` | 229-250 | `() -> str | None` | 6 | 3 | ``RequestContext.sender_id`` текущего request (или ``None``). Единственная точка обращения к identity-store из |
| `_usage_to_dict` | 253-280 | `(usage: Any) -> dict | None` | 12 | 4 | Унифицированный адаптер ``LLMUsage \| dict \| None -> dict \| None``. Возвращает ``dict`` с per-turn полями (``pr |
| `_make_run_event` | 527-559 | `(context: AgentRunHookContext, session_key: str | None=None, request_id: str | None=None)` | 7 | 1 | Сформировать LogEvent из AgentRunHookContext (без жёсткой связки). |

## `lib/cli/console_loop.py` — 486 LOC (code 384)
- module: `lib.cli.console_loop`
- docstring: ConsoleLoop — интерактивный REPL CLI-агента поверх MessageBus + AgentLoop. Структура REPL — зеркало upstream ``run_interactive`` из ``nanobot/cli/agent.py`` (HKUDS/nanobot @ main). Не изобретаем — делегируем: * ``cli_ter
- static importers (2): `cli_agent.py`, `tests/test_console_loop.py`
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 0, module functions: 5

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_warn_no_vt` | 44-54 | `(message: str | None) -> None` | 3 | 1 | Показать предупреждение об отключённых цветах (один раз за сессию). |
| `_print_tool_events` | 65-93 | `(events: list, cfg: DisplayConfig) -> None` | 24 | 2 | Рендер ``_tool_audit`` блока (per-call tool events). |
| `_print_context_window` | 96-116 | `(block: Any, cfg: DisplayConfig) -> None` | 17 | 2 | Рендер ``context_window`` блока (UI индикатор занятости). |
| `_run_cli_compact` | 124-142 | `(agent: Any, command: str, chat_id: str, cli_channel: str) -> None` | 5 | 1 | CLI-команда ``/compact``: локальное сжатие контекста. |
| `run_repl` | 150-486 | `(agent: Any, config: Any, *, session: str | None=None, display: DisplayConfig | None=None,` | 69 | 2 | Главный REPL — копия upstream ``run_interactive``. Args: agent: AgentLoop (создан через наш ApplicationContext |

## `lib/hooks/tool_audit_hook.py` — 190 LOC (code 159)
- module: `lib.hooks.tool_audit_hook`
- docstring: Модуль сбора аудита вызовов инструментов агента. Предоставляет хук ``ToolAuditHook``, который аккумулирует каждый вызов инструмента (имя, аргументы, статус, ошибка, превью результата) на протяжении всех итераций оборота,
- static importers (3): `lib/core/agent_factory.py`, `tests/contract/test_composite_hook_lifecycle.py`, `tests/test_hooks_tool_audit_hook.py`
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 1, module functions: 1

### class `ToolAuditHook` — lines 23-139 (117 LOC), 6 methods
- bases: AgentHook
- decorators: —
- docstring: Аккумулирует каждый вызов инструмента (имя, аргументы, статус, ошибка, превью результата) на протяжении всех итераций оборота, чтобы вызывающая сторона могла вставить полный аудит-трейл в ``OutboundMessage.metadata["_too
- name referenced in 3 file(s); tests: 2

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 36-47 | `(self) -> None` | 1 | 0 | 6 | Инициализирует внутренние структуры хранения. Создаёт словари (ключ — ``session_key``, ``""`` для оборотов без |
| `_bucket_key` | 50-53 | `(ctx: Any) -> str` | 2 | 2 | 3 | Вернуть ``session_key`` из контекста (``""`` если его нет/не строка). |
| `before_execute_tools` | 55-85 | `(self, ctx: Any) -> None` | 5 | 0 | 3 | Вызывается перед выполнением инструментов в итерации. Сохраняет снимок имён и аргументов всех инструментов тек |
| `after_iteration` | 87-113 | `(self, ctx: Any) -> None` | 12 | 0 | 6 | Вызывается после завершения итерации. Обновляет статус и, при необходимости, ошибку или превью результата для  |
| `drain` | 115-127 | `(self, session_key: str | None=None) -> list[dict[str, Any]]` | 2 | 0 | 7 | Возвращает записи вызовов для одной сессии и очищает их bucket. Args: session_key: ключ сессии оборота. ``None |
| `drain_calls` | 129-139 | `(self, session_key: str | None=None) -> list[dict]` | 2 | 0 | 1 | Возвращает снимки вызовов для одной сессии и очищает их bucket. Args: session_key: ключ сессии оборота (``None |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `format_tool_params` | 142-190 | `(params: list[dict]) -> dict[str, str]` | 12 | 1 | Форматирует список параметров инструментов в словарь строк. Принимает ``p["arguments"]`` в одной из форм: * ** |

## `lib/hooks/terminal_tool_print_hook.py` — 164 LOC (code 135)
- module: `lib.hooks.terminal_tool_print_hook`
- docstring: Хук живого вывода вызовов инструментов в терминал. Печатает результат каждого ``tool_call`` сразу после итерации агента: * **ошибка** — подробно (``✗ name(args) — error_text``): ProgressHook nanobot'а ошибки **не** печат
- static importers (3): `lib/core/agent_factory.py`, `tests/contract/test_composite_hook_lifecycle.py`, `tests/test_terminal_tool_print_hook.py`
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 1, module functions: 2

### class `TerminalToolPrintHook` — lines 104-164 (61 LOC), 4 methods
- bases: AgentHook
- decorators: —
- docstring: Живой терминальный вызов для каждого ``tool_call`` итерации. Печатает результат сразу в ``after_iteration``: ошибки — подробно (имя + аргументы + текст ошибки), успехи — кратко (имя + длительность).
- name referenced in 3 file(s); tests: 2

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 111-113 | `(self) -> None` | 1 | 0 | 6 | — |
| `_bucket_key` | 116-118 | `(ctx: Any) -> str` | 2 | 2 | 3 | — |
| `before_execute_tools` | 120-124 | `(self, ctx: Any) -> None` | 3 | 0 | 3 | — |
| `after_iteration` | 126-164 | `(self, ctx: Any) -> None` | 13 | 0 | 6 | — |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_format_args` | 44-69 | `(arguments: Any) -> str` | 9 | 1 | Компактное однострочное представление аргументов tool-вызова. Args: arguments: ``dict`` аргументов (или произв |
| `_format_result` | 72-101 | `(result: Any) -> str` | 7 | 1 | Однострочный превью результата tool-вызова. Многострочный текст схлопывается в одну строку, ``\s+`` → пробел.  |

## `lib/cli/nanobot_cli_compat.py` — 147 LOC (code 105)
- module: `lib.cli.nanobot_cli_compat`
- docstring: Адаптер приватных CLI-хелперов ``nanobot``. Upstream не публикует REPL-хелперы как стабильный API — они живут в приватных модулях и мигрируют между версиями: * ``nanobot <= 0.3.0`` — ``nanobot.cli.commands`` (``_init_pro
- static importers (2): `lib/cli/console_loop.py`, `tests/contract/test_nanobot_cli_compat.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 5

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_resolve` | 62-97 | `() -> dict[str, Any]` | 9 | 1 | Найти все хелперы, кэшируя результат. Raises: AttributeError: если хотя бы один хелпер недоступен ни в одном и |
| `_best_module` | 100-124 | `(candidates: tuple[str, ...], names: tuple[str, ...]) -> Any` | 10 | 1 | Кандидат с наибольшим числом доступных ``names`` (порядок решает ничью). Модуль выбирается даже неполным, чтоб |
| `get_repl_helpers` | 127-134 | `() -> dict[str, Any]` | 1 | 2 | Вернуть словарь приватных REPL-хелперов upstream. Состав: ``_init_prompt_session``, ``_is_exit_command``, ``_r |
| `get_logo_version` | 137-141 | `() -> tuple[str, str]` | 1 | 2 | Вернуть ``(__logo__, __version__)`` из публичного ``nanobot``. |
| `model_display` | 144-147 | `(config: Any) -> tuple[str, str]` | 1 | 2 | Адаптер над приватным ``_model_display`` (defensive копия результата). |

## `lib/session/pg_session_manager.py` — 137 LOC (code 110)
- module: `lib.session.pg_session_manager`
- docstring: Compatibility layer: PGSessionManager теперь — cold-storage mirror. После ``storage-hybridization`` (см. спеку ``openspec/specs/storage/session-hybridization/spec.md``) этот класс **НЕ пишет** в ``agent_session_meta`` / 
- static importers (3): `lib/services/session_storage.py`, `tests/test_pg_session_manager.py`, `tests/test_storage_hybridization.py`
- string/dynamic refs: 3
- test files touching it: — none —
- classes: 1, module functions: 0

### class `PGSessionManager` — lines 36-137 (102 LOC), 12 methods
- bases: SessionManager
- decorators: —
- docstring: Cold-storage mirror поверх upstream ``SessionManager``. Hot-path методы (``get_or_create``, ``save``, ``list_sessions``, ``read_session_metadata``, ``read_session_file``) делегируются в ``super()`` (upstream JSONL). Ника
- name referenced in 4 file(s); tests: 3

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 51-76 | `(self, workspace: Path, dsn: str='', schema: str='public', messages_table: str='', meta_ta` | 4 | 0 | 6 | — |
| `close` | 78-80 | `(self) -> None` | 1 | 0 | 30 | No-op: PG-соединения живут в общем пуле ``utils.db``. |
| `get_or_create` | 82-89 | `(self, key: str) -> Session` | 1 | 0 | 7 | Делегирует в upstream ``SessionManager`` (JSONL). Раньше этот метод читал/писал ``agent_session_meta`` / ``age |
| `save` | 91-98 | `(self, session: Session, *, fsync: bool=False) -> None` | 1 | 0 | 14 | Делегирует в upstream ``SessionManager.save`` (JSONL). Никаких прямых ``INSERT/UPDATE`` в ``agent_session_meta |
| `list_sessions` | 100-102 | `(self) -> list[dict[str, Any]]` | 1 | 0 | 3 | Делегирует в upstream ``SessionManager.list_sessions``. |
| `read_session_metadata` | 104-106 | `(self, key: str) -> dict[str, Any] | None` | 1 | 0 | 3 | Делегирует в upstream ``SessionManager.read_session_metadata``. |
| `read_session_file` | 108-110 | `(self, key: str) -> dict[str, Any] | None` | 1 | 0 | 1 | Делегирует в upstream ``SessionManager.read_session_file``. |
| `invalidate` | 112-114 | `(self, key: str) -> None` | 1 | 0 | 3 | No-op: ``SessionManager`` (upstream) сам управляет кешем. |
| `delete_session` | 116-118 | `(self, key: str) -> bool` | 1 | 0 | 2 | Делегирует в upstream ``SessionManager.delete_session``. |
| `flush_all` | 120-125 | `(self) -> int` | 1 | 0 | 1 | No-op: upstream ``SessionManager`` сам флашит JSONL при shutdown. Возвращает 0 (нет кеша для flush'а — кеш жив |
| `_validate_ident` | 128-130 | `(part: str) -> None` | 3 | 1 | 3 | — |
| `_quote` | 133-137 | `(cls, ident: str) -> str` | 4 | 2 | 3 | — |

## `lib/cli/hook_loader.py` — 130 LOC (code 107)
- module: `lib.cli.hook_loader`
- docstring: HookLoader — авто-сканирование workspace/hooks/*.py для AgentHook-подклассов. ``workspace/hooks/`` — это директория ПЛАГИНОВ проекта: каждый ``*.py`` файл должен содержать самодостаточный ``AgentHook``-подкласс, который 
- static importers (4): `lib/core/application_context.py`, `tests/test_active_files_hook.py`, `tests/test_cli_agent.py`, `tests/test_hook_allowlist.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 2

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `scan_and_register` | 31-115 | `(hooks_dir: Path, workspace_dir: Path) -> list[Any]` | 17 | 3 | Сканировать ``hooks_dir`` и вернуть список инстанцированных плагинов. Каждый ``*.py`` файл (исключая ``_*``) и |
| `_allowed_hook_names` | 118-130 | `() -> frozenset[str]` | 1 | 3 | Allowlist имён плагинов в ``workspace/hooks/``. Защита от случайного добавления плагина, который не прошёл рев |

## `lib/lifecycle/shutdown_coordinator.py` — 120 LOC (code 92)
- module: `lib.lifecycle.shutdown_coordinator`
- docstring: ShutdownCoordinator — упорядоченный graceful shutdown сервисов. Регистрирует экземпляры по имени, останавливает в обратном порядке регистрации (LIFO). Это гарантирует, что зависимости остановлены ПОСЛЕ своих потребителей
- static importers (3): `lib/core/application_context.py`, `tests/test_shutdown_coordinator.py`, `tests/test_unified_event_logging_lifecycle.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 1

### class `ShutdownCoordinator` — lines 38-83 (46 LOC), 4 methods
- bases: object
- decorators: —
- docstring: Регистр компонентов для упорядоченной остановки (LIFO). Attributes: _components: список пар ``(name, stop_fn)`` в порядке регистрации. ``shutdown_all()`` обходит его ``reversed()``.
- name referenced in 3 file(s); tests: 2

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 46-47 | `(self) -> None` | 1 | 0 | 6 | — |
| `register` | 49-65 | `(self, name: str, component: Any) -> None` | 1 | 0 | 12 | Зарегистрировать компонент для последующей остановки. Допустимо: * объект с методом ``close()`` / ``stop()`` / |
| `shutdown_all` | 67-79 | `(self) -> None` | 3 | 0 | 2 | Остановить компоненты в обратном порядке (LIFO). Исключения каждого компонента глотаются и логируются через `` |
| `clear` | 81-83 | `(self) -> None` | 1 | 0 | 12 | Очистить реестр. Используется только в тестах. |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_resolve_stop_fn` | 86-120 | `(component: Any) -> Callable[[], None]` | 6 | 2 | Найти подходящий stop-метод у компонента. Порядок поиска: ``close`` → ``stop`` → ``shutdown`` → ``terminate``. |

## `lib/lifecycle/gateway_runner.py` — 113 LOC (code 96)
- module: `lib.lifecycle.gateway_runner`
- docstring: GatewayRunner — главный цикл gateway с перезапуском (exponential backoff). При необработанном исключении gateway перезапускается с увеличивающейся паузой (1s → 2s → 4s → 8s → 16s → 30s). Чистое завершение (clean shutdown
- static importers (2): `gateway.py`, `tests/test_gateway_runner.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 0

### class `GatewayRunner` — lines 42-113 (72 LOC), 3 methods
- bases: object
- decorators: —
- docstring: Цикл перезапуска gateway с exponential backoff. Attributes: _initial_delay: пауза перед первым рестартом (сек). _max_delay: потолок паузы (сек). После _max_delay backoff больше не растёт. _sleep: инъекция ``time.sleep`` 
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 51-76 | `(self, *, initial_delay: float | None=None, max_delay: float | None=None, sleep: Callable[` | 4 | 0 | 6 | Args: initial_delay: секунды перед первым рестартом после падения. По умолчанию — ``gateway.restart_initial_de |
| `run_forever` | 78-108 | `(self, run_once: Callable[[], None]) -> None` | 5 | 0 | 1 | Запускать ``run_once`` бесконечно, перезапуская при падениях. Управляющие сигналы: * ``run_once()`` бросает `` |
| `reset_backoff` | 110-113 | `(self) -> float` | 1 | 0 | 0 | Вернуть начальную задержку. Используется в тестах и при явном сбросе backoff после успешного цикла. |

## `lib/events/subagent.py` — 49 LOC (code 38)
- module: `lib.events.subagent`
- docstring: Кастомный runtime-event для subagent-оборотов. Определяется в нашем namespace (см. ``openspec/changes/post-0.3.5-patches-cleanup/design.md D1``), потому что ``nanobot.events.SubagentTurnCompleted`` не существует в 0.3.5.
- static importers (5): `lib/events/__init__.py`, `lib/services/runtime_events_subscriber.py`, `lib/services/runtime_patcher.py`, `tests/test_runtime_events_subscriber.py`, `tests/test_subagent_logging.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 0

### class `SubagentTurnCompleted` — lines 26-46 (21 LOC), 0 methods
- bases: AgentEvent
- decorators: dataclass(frozen=True)
- docstring: Финальное завершение subagent-оборота. Поля соответствуют payload ``subagent_run_finished`` в ``agent_gateway_logs``: ``task_id``, ``task``, ``final_content``, ``tools_used``, ``stop_reason``, ``request_id``, ``parent_re
- name referenced in 5 file(s); tests: 2

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

## `lib/cli/display_config.py` — 30 LOC (code 24)
- module: `lib.cli.display_config`
- docstring: DisplayConfig — настройки вывода CLI-агента.
- static importers (4): `cli_agent.py`, `lib/cli/console_loop.py`, `tests/test_cli_agent.py`, `tests/test_console_loop.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 0

### class `DisplayConfig` — lines 9-30 (22 LOC), 1 methods
- bases: object
- decorators: dataclass
- docstring: Какие блоки показывать и как (typewriter, скорость).
- name referenced in 5 file(s); tests: 2

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `from_settings` | 21-30 | `(cls, cli_settings: dict) -> DisplayConfig` | 8 | 0 | 2 | — |

## `lib/events/__init__.py` — 12 LOC (code 8)
- module: `lib.events`
- docstring: Project-local event types extending nanobot.events.AgentEvent. Импортируются через ``bus.subscribe(handler, EventType)`` — ``MessageBus.publish`` (см. ``nanobot/bus/queue.py:118``) принимает любой dataclass, наследующий 
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 0

## `lib/cli/__init__.py` — 1 LOC (code 1)
- module: `lib.cli`
- docstring: CLI-специфичный код: REPL, рендер вывода, загрузка хуков.
- static importers (2): `tests/contract/test_nanobot_cli_compat.py`, `tests/test_hook_allowlist.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 0

## `lib/lifecycle/__init__.py` — 1 LOC (code 1)
- module: `lib.lifecycle`
- docstring: Lifecycle: цикла запуска/перезапуска и graceful shutdown.
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 0

## `lib/hooks/__init__.py` — 0 LOC (code 0)
- module: `lib.hooks`
- docstring: — NONE —
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 0

## `lib/session/__init__.py` — 0 LOC (code 0)
- module: `lib.session`
- docstring: — NONE —
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 0

