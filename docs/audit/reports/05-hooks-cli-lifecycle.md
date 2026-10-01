# Аудит: Хуки, CLI, lifecycle, сессии, события

## Сводка группы
Файлов: 16 · LOC: 2139 · классов: 8 · методов: 40 · функций: 25

Подсистема отвечает за три независимых вещи: (1) перехват событий агента
(`lib/hooks/`) — аудит tool-вызовов, живой вывод в терминал, запись в
`agent_gateway_logs`; (2) интерактивный REPL CLI-агента (`lib/cli/`) — тонкая
надстройка над приватным REPL upstream; (3) lifecycle и хранение сессий
(`lib/lifecycle/`, `lib/session/`, `lib/events/`) — рестарт-цикл gateway,
graceful shutdown, cold-storage mirror сессий, project-local типы событий.

**Ключевые находки** (с путём и строкой):

- **Баг (высокий): CLI-REPL вешается при выходе.** `lib/cli/console_loop.py:476`
  ждёт `await asyncio.gather(bus_task)`, а `agent.stop()` вызывается только
  на `:479`. `AgentLoop.run()` крутится `while self._running`
  (`nanobot/agent/loop.py:1267`) и завершается только по `stop()` (`:1591`) —
  цикл ждёт сам себя. Воспроизведено (`/tmp/repro_console_exit.py`): gather
  не возвращается. **Вердикт: Упростить** (переставить `agent.stop()` перед
  gather). Ни один тест не заходит в `finally` `run_repl`.
- **Баг (высокий): `bus.drain()` в shutdown недостижим.** `ctx.stop()` вызывается
  из `gateway.py:301` и `cli_agent.py:166,209` — **после** выхода из
  `asyncio.run(...)`, т.е. вне event loop. `application_context.py:635`
  `asyncio.get_event_loop()` на закрытом loop → `RuntimeError` → `except
  RuntimeError: pass` (`:640-641`) → drain молча не выполняется. Заявленный
  инвариант «дождаться in-flight handler'ов перед закрытием пула» не
  выполняется ни в одном entrypoint. **Вердикт: Упростить**.
- **Баг (высокий): `PGSessionManager.invalidate()` — no-op ломает
  `delete_session`.** `lib/session/pg_session_manager.py:112-114` переопределяет
  upstream `SessionManager.invalidate` (который делает `self._cache.pop(key)`
  — `nanobot/session/manager.py:1865-1868`) пустым `return None`. Upstream
  `delete_session` начинается именно с `self.invalidate(key)`
  (`nanobot/session/manager.py:1872`) → кеш-элемент выживает → следующий
  `get_or_create(key)` (`nanobot/session/manager.py:1750-1752`) возвращает
  удалённую сессию из памяти. Docstring `:113` («upstream сам управляет кешем»)
  фактически неверен: `invalidate()` **и есть** API управления кешем.
  **Вердикт: Упростить** (удалить override).
- **Баг (средний): `PGSessionManager.flush_all()` теряет несохранённые сессии
  при shutdown.** `lib/session/pg_session_manager.py:120-125` возвращает `0`
  вместо upstream `flush_all()` («Re-save every cached session with fsync for
  durable shutdown», `nanobot/session/manager.py:1847-1863`). Оба entrypoint'а
  вызывают его как «flush перед выходом» (`gateway.py:279`,
  `console_loop.py:482`) и по нулю **никогда** не печатают
  «Flushed N session(s)» — мёртвый лог. Для режима `storage=postgres`
  (дефолт) — потеря durability. **Вердикт: Упростить** (удалить override).
- **Баг (средний): `ShutdownCoordinator` не имеет таймаутов, но docstring
  обещает обратное.** `lib/lifecycle/shutdown_coordinator.py:71-73` заявляет
  «один зависший сервис не должен оставить процесс висящим навечно», а
  `shutdown_all()` (`:75-79`) вызывает `stop_fn()` синхронно без какого-либо
  ограничения. Глотание исключений ≠ защита от зависания: `DbLoggingService.stop()`
  с недоступной PG заблокирует shutdown навсегда. **Вердикт: Упростить**.
- **Баг (средний): порядок shutdown противоречит собственному комментарию.**
  Регистрация — `runtime_events_subscriber` (`application_context.py:568`),
  `db_logging_service` (`:579`), `session_cold_sync_service` (`:589`); LIFO →
  sync → db_logging → **subscriber**. Комментарий `application_context.py:624-625`
  утверждает, что drain вызывается «ДО остановки RuntimeEventsSubscriber» —
  к моменту drain (`:621` уже отработал) подписчик отписан. In-flight
  `TurnCompleted`/`SubagentTurnCompleted` при остановке теряются молча.
  Дополнительно `runtime_events_subscriber.stop()` вызывается повторно
  (`:650`) после того, как `shutdown_all()` уже его остановил. **Вердикт:
  Упростить**.
- **`drain` vs `drain_calls` (вопрос 2 брифа) — это НЕ равнозначное
  дублирование.** `drain` (`tool_audit_hook.py:115-127`) — настоящий API,
  вызывается из `RuntimePatcher.patch_assemble_outbound`
  (`lib/services/runtime_patcher.py:1523`). `drain_calls` (`:129-139`) —
  **ноль вызывающих в продукте** (только `tests/`). Порядок удаления:
  (1) удалить `drain_calls` (`:129-139`) — безопасно, продукта не касается;
  (2) удалить поле `self._calls` (`:39`) и его заполнение
  `before_execute_tools` (`:69-71`) — попутно устраняется **утечка памяти**:
  `_calls[session_key]` никогда не вычищается, т.е. словарь растёт на одну
  запись на каждый уникальный `session_key` за жизнь процесса; (3) удалить
  `tests/test_tool_audit_hook.py`-покрытие `drain_calls`. **Вердикт: Удалить**.
- **Заявленный контракт порядка хуков выполняется, но его обоснование ложно
  (вопрос 1 брифа).** Фактический порядок в `agent_factory.py:117-137`:
  `[*project_hooks, ToolAuditHook, TerminalToolPrintHook]` — совпадает с
  AGENTS.md. Но комментарий `agent_factory.py:125-128` («использует те же
  `tool_events`/`tool_results`/`tool_calls`, которые `ToolAuditHook`
  заполняет») неверен: эти атрибуты заполняет сам upstream `AgentLoop`, а
  `ToolAuditHook` их только читает. Реальной зависимости порядка нет.
  Дополнительно `agent_factory.py:100-101` в docstring перечисляет только
  2 позиции из 3. **Вердикт: Оставить** (порядок) + **Упростить** (docstring).
- **Клон allowlist'а плагинов (вопрос 3 брифа).** `hook_loader.py:126-130`
  (`_allowed_hook_names`) и `runtime_inventory.py:88-108`
  (`canonical_plugin_hooks`) — две рукописные копии одного факта, не связанные
  ничем: связь `StreamDiagnosisHook` ↔ файл `debug_stream_diag` неявная
  (имя класса ≠ имя файла). Порядок в `scan_and_register` детерминирован
  (`sorted(hooks_dir.iterdir())`, `:65` → алфавитный), пустой каталог → `[]`
  (`:60-61`), защиты от двойной регистрации **нет** (`:87` перезаписывает
  `sys.modules`, список `hooks` каждый раз новый) — безвредно, т.к. в продукте
  единственный call-site (`application_context.py:407`) вызывается один раз на
  процесс. **Вердикт: Упростить** (выводить `_allowed_hook_names` из
  `canonical_plugin_hooks`).
- **REPL остаётся форком приватного API (вопрос 4 брифа).** `run_repl`
  (`console_loop.py:150-486`) — «копия upstream `run_interactive`», которая
  тянет **11 приватных символов** upstream через `nanobot_cli_compat.py:47-57`
  (`_init_prompt_session`, `_is_exit_command`, `_read_interactive_input_async`,
  `_restore_terminal`, `_flush_pending_tty_input`, `_print_agent_response`,
  `_print_interactive_response`, `_maybe_print_interactive_progress`,
  `_ReasoningBuffer`) + `_AUX_HELPERS:34` (`_model_display`,
  `_sanitize_surrogates`). `_resolve()` (`nanobot_cli_compat.py:62-97`)
  аллоритм **all-or-nothing**: переименование одного символа в upstream роняет
  весь CLI на старте (`AttributeError` из `run_repl:182` до всякого I/O).
  Коммиты `91698ee`/`32d61a7e` («переход на upstream consumer», «возврат к
  upstream-структуре») структурное расхождение сократили, а **число приватных
  имён увеличили** (было 4). **Вердикт: Оставить** с явной пометкой риска.
- **Инвариант `FINAL_TURN_KEY` к CLI не относится (вопрос 4 брифа).**
  `RuntimePatcher.patch_compact_command`, на который ссылается AGENTS.md:43,
  **не существует** — удалён в `openspec/changes/archive/2026-09-27-post-0.3.5-patches-cleanup`
  (проверено: 0 совпадений в `lib/`). CLI-путь `_run_cli_compact`
  (`console_loop.py:124-142`) **не публикует ни одного outbound-сообщения** —
  он вызывает `ContextCompactionService.compact()` напрямую, поэтому
  `_final_turn` проставлять нечего и незачем. Факт сжатия в CLI пишется
  один раз. Актуальный путь публикации — `CompactionEventSubscriber`
  (см. `openspec/specs/runtime/context/spec.md:204`). **Строка, которая лжёт:
  AGENTS.md:43, README.md:193, `docs/ARCHITECTURE.md:263,270,475-476,1377`.**
- **Метаданные `_tool_audit` / `context_window` физически не доходят до
  рендера (вопрос 4 брифа).** `StreamedResponseEvent` обрабатывается на
  `console_loop.py:274-290` с `continue` и в `turn_response` **никогда** не
  попадает; `_tool_audit`/`context_window` читаются только из
  `turn_response[0].metadata` (`:448-451`). Основной путь (streaming) →
  `turn_response` пуст → `_print_tool_events`/`_print_context_window` (`:461-464`)
  не вызываются. Код это признаёт в комментарии `:452-459` («известный gap»).
  Итог: 4 из 8 полей `DisplayConfig` (`show_tool_calls`, `show_tool_results`,
  `show_tool_params`, `show_context_window`) **не влияют ни на что** в
  нормальном режиме. **Вердикт: Упростить**.
- **Прямого SQL в hot path нет — гард верен (вопрос 6(a) брифа).**
  `tests/test_storage_hybridization.py:84-105` — AST-проверка: в `lib/**`,
  кроме `session_cold_sync_service.py`, нет выражений
  `INSERT/UPDATE/DELETE ... agent_session_meta|agent_session_messages`.
  `PGSessionManager` проверку проходит: все методы — чистые `super()`-делегаты.
  **Вердикт: Оставить** (инвариант соблюдён).
- **`delete_session` согласован с PG только асинхронно (вопрос 6(в) брифа).**
  `pg_session_manager.py:116-118` удаляет JSONL; строки в
  `agent_session_meta/messages` удаляются фоновым
  `SessionColdSyncService` при следующем проходе
  (`session_cold_sync_service.py:413-422`, ветка «файл исчез»). Консистентность
  = **eventually consistent** с задержкой до `sessions.sync.sync_interval_sec`
  (дефолт 30 с, `application_context.py:1713`); при выключенном sync —
  строки-сироты остаются навсегда. **Вердикт: Оставить** с оговоркой.
- **Дублирования записи в `DbLoggingService` нет (вопрос 8 брифа).** Хук пишет
  `run_started`/`run_finished`/`tool_call`/`tool_result`/`llm_call`/`error`;
  `RuntimeEventsSubscriber` — `turn_completed`/`subagent_run_finished`. Наборы
  `event_type` не пересекаются, единственный writer — `DbLoggingService`
  (батч-пул). `hook_factories` (`agent_factory.py:144-194`) корректны: фабрика
  запекает `session_key`/`request_id` в свежий per-turn инстанс, дефолтит
  `request_id` через `register_request` (`database_logging_hook.py:207-212`),
  чистит индекс в `after_run` (`finally`, `:521-523`). **Вердикт: Оставить**.
- **Событийный слой чист (вопрос 7 брифа).** `lib/events/` содержит ровно один
  тип — `SubagentTurnCompleted` (`subagent.py:26-46`). Он живой: публикуется
  `runtime_patcher.py:2092`, подписка `runtime_events_subscriber.py:141`,
  покрыт `tests/test_runtime_events_subscriber.py`. Мёртвых типов нет.
  **Вердикт: Оставить**.

**Вердикты:** Оставить 44 · Упростить 23 · Удалить 6 · Перенести 0

---

## `lib/hooks/tool_audit_hook.py` — 190 LOC

**Назначение.** Фреймворковый хук аудита tool-вызовов: накапливает per-call
записи (имя, аргументы, статус, превью результата) и отдаёт их по запросу.
**Что делает.** `before_execute_tools` снимает снимок имён/аргументов всех
вызовов итерации (`:55-85`); `after_iteration` дополняет снимок статусом,
ошибкой и превью (`:87-113`); `drain(session_key)` возвращает накопленное и
**вычищает bucket** (`:115-127`).
**Зачем нужен.** `RuntimePatcher.patch_assemble_outbound` кладёт результат
`drain()` в `metadata["_tool_audit"]` исходящего сообщения — каналы и CLI
рендерят его в UI. Без хука в `agent_gateway_logs`/UI не будет видно, какие
инструменты реально вызывались.
**Вердикт.** `Упростить` — класс нужен, но держит мёртвое параллельное
состояние (`_calls`) с двумя утечками; `format_tool_params` не имеет
вызывающих.
**Обоснование.** `drain_calls` не вызывается ни одним продуктом-кодом, а
`_pending_start` (`:41`, заполняется `:73`, читается только через `.get()`
`:104`) никогда не удаляется — оба словаря растут на одну запись на каждый
уникальный `session_key` за жизнь процесса. При длинноживущем gateway это
неограниченный рост.

#### class `ToolAuditHook` (строки 23-139, 6 методов)
**Назначение.** Накопитель per-call аудита с bucketing'ом по `session_key`.
**Зачем нужен.** Питает `_tool_audit` в outbound-метаданных.
**Вердикт.** `Упростить`.
**Обоснование.** Убрать `_calls` и `drain_calls` (см. врезку выше), оставить
`_entries`/`_pending_start`; в идеале вынести bucketing'овое состояние в
`contextvars` (уже так сделано в `database_logging_hook.py`), чтобы проблема
не могла повториться.
Атрибуты: `_entries: dict[str, list[dict]]`, `_pending_start: dict[str, float]`,
`_calls: dict[str, list[dict]]` (последний — удалить).

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 36-47 | Инициализация трёх словарей | — | `agent_factory.py:120` | Упростить — убрать `_calls` (`:39`) |
| `_bucket_key` | 50-53 | `session_key` из ctx, `""` если нет | — | `before_execute_tools:57`, `after_iteration:90` | Оставить |
| `before_execute_tools` | 55-85 | Снимок имён/аргументов + старт времени | заполняет `_entries`/`_pending_start` | upstream `AgentLoop` | Упростить — убрать ветку `_calls` (`:69-71`) |
| `after_iteration` | 87-113 | Дополняет записи статусом/ошибкой/превью | — | upstream `AgentLoop` | Оставить |
| `drain` | 115-127 | Вернуть+вычистить записи сессии | единственный потребитель | `runtime_patcher.py:1523` | Оставить |
| `drain_calls` | 129-139 | Вернуть+вычистить снимки вызовов | **никто** | только `tests/` | Удалить |

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `format_tool_params` | 142-190 | `list[dict]` → `dict[str, str]` для рендера аргументов | рендерит **CLI**: `_print_tool_events` (`console_loop.py:77`) делает `", ".join(f"{k}={v}" ...)` **сам**, минуя эту функцию | только `tests/`, CHANGELOG | Удалить |

---

## `lib/hooks/terminal_tool_print_hook.py` — 164 LOC

**Назначение.** Живой построчный вывод результата каждого tool-вызова в
loguru-канал терминала.
**Что делает.** `before_execute_tools` запоминает `time.monotonic()` на каждый
вызов (`:120-124`); `after_iteration` печатает `✓ name → preview (Nms)` либо
`✗ name — error`, попутно **вычищая** свой bucket (`:128`). Пишет только в
loguru, не в БД.
**Зачем нужен.** Даёт оператору живой фидбек в длинных `write_stdin`/`exec`.
**Вердикт.** `Оставить`.
**Обоснование.** Единственный хук с корректным освобождением состояния
(`_starts.pop`), ни одной утечки. Замечание: при исключении внутри итерации
`after_iteration` не вызовется и bucket останется — но он перезаписывается
(не растёт) при следующем `before_execute_tools` для того же ключа.

#### class `TerminalToolPrintHook` (строки 104-164, 4 метода)
**Назначение.** Репортер tool-вызовов в терминал.
**Зачем нужен.** Живой фидбек; при удалении теряется единственный источник
видимости tool-вызовов в CLI/gateway-логе.
**Вердикт.** `Оставить`.
**Обоснование.** Состояние освобождается корректно; вывод идёт через
`logger.info`/`logger.error`, а не через `console`, поэтому не конфликтует с
upstream-рендером. Оговорка: заявленная в `agent_factory.py:125-128`
зависимость от `ToolAuditHook` ложна (см. врезку выше) — фактического
влияния порядка нет.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 111-113 | `_starts: dict[str, list[float]]` | — | `agent_factory.py:132` | Оставить |
| `_bucket_key` | 115-118 | `session_key` из ctx | — | `:121`, `:127` | Оставить |
| `before_execute_tools` | 120-124 | Засечь `monotonic` на каждый вызов | — | upstream | Оставить |
| `after_iteration` | 126-164 | Напечатать ok/error + длительность | — | upstream | Оставить |

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_format_args` | 44-69 | Компактное однострочное представление аргументов | — | `after_iteration:144` | Оставить |
| `_format_result` | 72-101 | Однострочное превью результата | — | `after_iteration:155` | Оставить |

---

## `lib/hooks/database_logging_hook.py` — 559 LOC

**Назначение.** Единственный хук, пишущий телеметрию оборота в
`agent_gateway_logs`/`agent_question_runs`; создаётся per-turn через
`hook_factories`.
**Что делает.** Хранит модульный «мост» `_CONTEXT_BRIDGE`
(`dict[session_key → {context_window, iteration_usage}]`, под
`threading.Lock`) для передачи usage/окна из патчей в каналы; per-turn инстанс
пишет `run_started` в `before_iteration`, `tool_call`/`tool_result`/`error`
по ходу инструментов, `llm_call` в `after_iteration`, `run_finished` +
`log_question_run` в `after_run`, и в `finally` чистит request-индекс.
Побочные эффекты: `register_request` может **создать** request_id сам
(`:207-212`), если inbound не зарегистрировал вопрос.
**Зачем нужен.** Разделение per-turn инстансов — конкурентная безопасность:
разные вопросы не путают события. Удаление хука = полная потеря
`agent_gateway_logs`.
**Вердикт.** `Оставить`.
**Обоснование.** Проверено: двойной записи нет, контракт `hook_factories`
корректен, покрытие тестами широкое. Два дефекта качества: мёртвый `_ctx`
и рост `_CONTEXT_BRIDGE` для не-`postgres` каналов (см. ниже).

#### class `DatabaseLoggingHook` (строки 283-524, 10 методов)
**Назначение.** Per-turn писатель телеметрии оборота.
**Зачем нужен.** Единственный producer `agent_question_runs` и основной
producer `agent_gateway_logs`.
**Вердикт.** `Оставить`.
**Обоснование.** Всё состояние, требующее изоляции, — в инстансе; модуль хранит
только передаточные данные (`_CONTEXT_BRIDGE`), которые чистятся по
`pop_context_bridge`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 310-340 | Поля инстанса + `print_llm_calls`/`get_model` | — | `make_db_logging_hook_factory:191` | Оставить |
| `_capture_context` | 346-352 | Подхватить `session_key`/`request_id` на каждый хук | — | `before_execute_tool:365`, `after_execute_tool:386`, `on_execute_tool_error:410`, `before_iteration:439`, `after_iteration:448` | Оставить |
| `_ctx` | 354-361 | Legacy-доступ к identity по строковому ключу | **никто** | 0 вызывающих | Удалить |
| `before_execute_tool` | 363-382 | Пишет `tool_call` | — | upstream | Оставить |
| `after_execute_tool` | 384-406 | Пишет `tool_result` | — | upstream | Оставить |
| `on_execute_tool_error` | 408-432 | Пишет `tool_result` со `status=error` | — | upstream | Оставить |
| `before_iteration` | 438-445 | Пишет `run_started` | — | upstream | Оставить |
| `after_iteration` | 447-484 | Пишет `llm_call` + печать токенов | — | upstream | Оставить |
| `_print_llm_tokens` | 486-505 | Две строки токенов в CLI | — | `after_iteration` | Оставить |
| `after_run` | 507-524 | Пишет `run_finished` + `question_run`, чистит индекс | — | upstream | Оставить |

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `seed_context_window` | 60-75 | Засеять лимит окна/модель в мост | — | `runtime_events_subscriber.py:54,192` | Оставить |
| `_store_iteration_usage` | 78-92 | Сохранить usage итерации | — | `runtime_patcher.py:152` | Оставить |
| `_store_context_window` | 95-102 | Сохранить готовый блок `context_window` | — | `runtime_patcher.py:149,211` | Оставить |
| `get_context_window` | 105-136 | Прочитать блок окна (считает usage из итераций) | — | `postgres_channel.py:744` | Оставить |
| `get_iteration_usage` | 139-146 | Прочитать usage сессии | — | `runtime_patcher.py:152` | Оставить |
| `pop_context_bridge` | 149-154 | Снять мост сессии | — | `postgres_channel.py:1473` | Оставить (см. риск ниже) |
| `make_db_logging_hook_factory` | 157-226 | Фабрика per-turn инстансов | — | `agent_factory.py:283` | Оставить |
| `_current_request_sender_id` | 229-250 | `RequestContext.sender_id` текущего request | — | `make_db_logging_hook_factory:201` | Оставить |
| `_usage_to_dict` | 253-280 | `LLMUsage | dict | None → dict | None` | — | `after_iteration` | Оставить |
| `_make_run_event` | 527-559 | `AgentRunHookContext` → `LogEvent` | — | `after_run:509-510` | Оставить |

**Риск (не баг):** `pop_context_bridge` вызывается только из
`postgres_channel.py:1473`. `seed_context_window` при этом вызывается на
`TurnRuntimeAdmitted` для **любого** `session_key`
(`runtime_events_subscriber.py:54`), а `_store_context_window` — на общем
outbound-пути (`runtime_patcher.py:211`). Значит для websocket/redis/streamlit/CLI
сессий записи моста не удаляются → рост `_CONTEXT_BRIDGE` на одну запись на
сессию за жизнь процесса. Стоит либо вынести `pop` в `DatabaseLoggingHook.after_run`
(`finally`, рядом с `clear_request`), либо чистить по TTL.

---

## `lib/cli/console_loop.py` — 486 LOC

**Назначение.** Интерактивный REPL CLI-агента: рисует промпт, публикует
`InboundMessage`, потребляет outbound, печатает ответ.
**Что делает.** Тянет 9 приватных хелперов upstream через
`nanobot_cli_compat` (`:183-196`), опционально `StreamRenderer`
(`:202-205`), в цикле читает ввод, обрабатывает `/exit` и `/compact`, публикует
сообщение с `metadata={"_wants_stream": True}` (`:383-391`), ждёт
`StreamedResponseEvent` с таймаутом `turn_wait_timeout`
(`:396-404`, дефолт 300 с, `:235-243`).
**Зачем нужен.** Это единственный интерактивный вход агента; вся отрисовка
делегирована upstream, своё — только таймаут и пост-блоки метаданных.
**Вердикт.** `Упростить`.
**Обоснование.** Два дефекта уровня «ломает пользователя»: дедлок на выходе
(`:476`/`:479`) и недостижимые пост-блоки (`:448-464`). Плюс 1 неиспользуемый
импорт и избыточное условие (`:411-413`).

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_warn_no_vt` | 44-54 | Warning об отключённых цветах | — | `run_repl:217,350` | Упростить — дедупликация лежит в вызываемом `ensure_console_colors` (`windows_terminal.py:168` `_WARNED`), здесь её нет; docstring `:45` вводит в заблуждение |
| `_print_tool_events` | 65-93 | Рендер `_tool_audit` | — | `run_repl:462` **только при непустом `turn_response`** | Упростить — недостижим на основном streaming-пути (см. врезку); либо чинить сбор метаданных, либо удалять вместе с 4 мёртвыми полями `DisplayConfig` |
| `_print_context_window` | 96-116 | Рендер `context_window` | — | `run_repl:464`, там же | Упростить — то же |
| `_run_cli_compact` | 124-142 | CLI `/compact` через `ContextCompactionService` | — | `run_repl:360` | Оставить — корректен, публикации в bus не делает (FINAL_TURN_KEY здесь неприменим) |
| `run_repl` | 150-486 | Главный REPL-цикл | — | `cli_agent.py:164,201` | Упростить — (1) `agent.stop()` перед `gather` (баг №1); (2) `import sys` (`:33`) не используется; (3) `not isinstance(response_msg.event, StreamedResponseEvent)` (`:411-413`) всегда истинно — `StreamedResponseEvent` не может попасть в `turn_response`; (4) `bg.done()` (`:480) падает с `AttributeError` внутри `finally`, если `background_task_factory` вернёт не-task |

---

## `lib/cli/nanobot_cli_compat.py` — 147 LOC

**Назначение.** Единственная точка, скрывающая version-specific расположение
приватных REPL-хелперов upstream (`nanobot.cli.terminal` vs
`nanobot.cli.commands`).
**Что делает.** `_best_module` выбирает кандидата с максимальным числом
совпадений по именам (`:100-124`); `_resolve` кэширует результат в
`_resolved` и **бросает `AttributeError` со списком отсутствующих имён**, если
хоть одно имя недоступно (`:88-94`).
**Зачем нужен.** `console_loop.py` не должен содержать version-specific
импортов приватных символов (проверяется `tests/contract/test_nanobot_cli_compat.py:66`).
**Вердикт.** `Упростить`.
**Обоснование.** Механизм решает реальную задачу, но контракт «всё или
ничего» на 11 символах — хрупкий: одно переименование в upstream убивает CLI
целиком. Плюс docstring `get_repl_helpers` (`:130-132`) перечисляет 6 символов
из 11 (информация лжёт), а `console_loop.py:187-189` достаёт
`_flush_pending_tty_input` через `.get(..., lambda: None)`, что недостижимо:
`_resolve` уже бросил бы исключение раньше.

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_resolve` | 62-97 | Резолв + кэш 11 приватных символов | — | `get_repl_helpers`, `model_display` | Оставить (с риском, зафиксирован во врезке) |
| `_best_module` | 100-124 | Выбор кандидата по score | — | `_resolve:81` | Оставить |
| `get_repl_helpers` | 127-134 | Публичный доступ к хелперам | — | `console_loop.py:182,186`; contract-тест | Упростить — привести docstring к фактическим 11 символам |
| `get_logo_version` | 137-141 | `(__logo__, __version__)` из публичного API | — | `console_loop.py:219` | Оставить |
| `model_display` | 144-147 | Адаптер над приватным `_model_display` | — | `console_loop.py:220` | Оставить |

---

## `lib/cli/display_config.py` — 30 LOC

**Назначение.** Флаги отображения CLI-REPL, читаются из секции `cli`
конфига.
**Что делает.** `from_settings` (`:21-30`) маппит `dict` → dataclass.
**Зачем нужен.** Управление тем, что REPL показывает.
**Вердикт.** `Упростить`.
**Обоснование.** 3 из 8 полей не читаются **нигде** в продукте:
`show_reasoning` (тест `test_cli_agent.py:129` проверяет только парсинг),
`show_progress`, `typewriter_speed` — последнее осталось после удаления
`_typewriter` из `console_loop` (см. `tests/test_console_loop.py:11-13`).
Оставшиеся 4 поля тоже не работают на основном пути (врезка выше). Фактически
работающая конфигурация — **ноль полей**.

#### class `DisplayConfig` (строки 9-30, 1 метод)
**Назначение.** Конфиг отображения REPL.
**Зачем нужен.** Единственный источник флагов для `_print_tool_events`/`_print_context_window`.
**Вердикт.** `Упростить`.
**Обоснование.** Сократить до реально читаемых полей; при этом 4 «живых»
поля тоже требуют починки на стороне сбора метаданных, иначе dataclass
можно удалить целиком вместе с `_print_*`.
Поля: `show_reasoning` (мёртвое), `show_tool_calls`, `show_tool_results`,
`show_tool_params`, `show_progress` (мёртвое), `show_context_window`,
`typewriter_speed` (мёртвое).

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `from_settings` | 21-30 | `dict` → `DisplayConfig` | — | `cli_agent.py:161,201`; `test_cli_agent.py:129` | Упростить — убрать 3 мёртвых ключа из маппинга |

---

## `lib/cli/hook_loader.py` — 130 LOC

**Назначение.** Авто-сканирование `workspace/hooks/` и инстанцирование
плагинов-х��ков.
**Что делает.** Пропускает не-`.py`/underscore-файлы (`:66`), отсекает всё
вне allowlist **без импорта** (`:68-77`), импортирует через
`spec_from_file_location` под именем `hooks.<stem>` (`:78-95`), находит
`AgentHook`-подклассы и инстанцирует как `cls(workspace_dir=...)` (`:99-114`).
Порядок — алфавитный по пути (`sorted(hooks_dir.iterdir())`, `:65`).
**Зачем нужен.** Плагины в `workspace/hooks/` подключаются без правки кода
ядра; allowlist — ревью-гейт.
**Вердикт.** `Упростить`.
**Обоснование.** Механика корректна (пустой/отсутствующий каталог → `[]`,
`:60-61`; битый плагин не ломает старт, `:90-98`). Упрощения: (1)
`_allowed_hook_names` должен выводиться из
`runtime_inventory.canonical_plugin_hooks()` по `Path(spec.source).stem`, иначе
две копии факта разъедутся; (2) добавить idempotency-guard (иначе второй
`ApplicationContext` в том же процессе — как в `benchmarks/` — регистрирует
дубли).

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `scan_and_register` | 31-115 | Скан+инстанцирование плагинов | — | `application_context.py:407` (единственный продовый call-site) | Упростить — дедупликация инстансов + вывод allowlist из инвентаря |
| `_allowed_hook_names` | 118-130 | Allowlist из 3 имён файлов | — | `scan_and_register:63` | Упростить — клон `runtime_inventory.py:88-108` |

---

## `lib/lifecycle/shutdown_coordinator.py` — 120 LOC

**Назначение.** Упорядоченная (LIFO) остановка фоновых сервисов.
**Что делает.** `register` резолвит stop-функцию через
`_resolve_stop_fn` и кладёт пару `(name, stop_fn)` в список (`:65`);
`shutdown_all` обходит `reversed()`, глотая исключения (`:75-79`).
В проде зарегистрировано ровно 3 компонента: `runtime_events_subscriber`
(`application_context.py:568`), `db_logging_service` (`:579`),
`session_cold_sync_service` (`:589`).
**Зачем нужен.** Гарантирует, что зависимости остановятся после
потребителей, и что один зависший сервис не помешает остановить остальные.
**Вердикт.** `Упростить`.
**Обоснование.** Порядок LIFO выбран правильно, но заявленная в docstring
гарантия «не зависнет навечно» (`:71-73`) **не обеспечена ничем**: нет ни
таймаута, ни изоляции потока. Плюс `_resolve_stop_fn` (`:112`) диспетчеризует
по порядку `close` → `stop` → `shutdown` → `terminate` — для duck-typing это
тихая подмена метода.

#### class `ShutdownCoordinator` (строки 38-83, 4 метода)
**Назначение.** Реестр компонентов с LIFO-остановкой.
**Зачем нужен.** Единственная точка упорядоченного shutdown.
**Вердикт.** `Упростить`.
Атрибут: `_components: list[tuple[str, Callable[[], None]]]`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 46-47 | Пустой реестр | — | `application_context.py:522`; 3 теста | Оставить |
| `register` | 49-65 | Добавить компонент | — | `application_context.py:568,579,589` | Упростить — проверять уникальность `name` (docstring `:59` требует, но не проверяет); двойная регистрация = двойной `stop()` |
| `shutdown_all` | 67-79 | Остановить всё в LIFO | — | `application_context.py:621` | Упростить — добавить таймаут на каждый `stop_fn` (иначе docstring `:71-73` лжёт) |
| `clear` | 81-83 | Очистить реестр | — | только тесты | Оставить |

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_resolve_stop_fn` | 86-120 | Найти `close`/`stop`/`shutdown`/`terminate` | — | `register:65` | Упростить — duck-typing по первому найденному имени молча вызывает не тот метод; для трёх известных типов лучше явный контракт (`Protocol` с `stop()`) |

---

## `lib/lifecycle/gateway_runner.py` — 113 LOC

**Назначение.** Рестарт-цикл gateway с exponential backoff.
**Что делает.** `run_forever` крутит `run_once()`: нормальный возврат → выход,
`KeyboardInterrupt` → выход с INFO, иное `Exception` → warning + traceback,
`sleep(delay)`, `delay = min(delay*2, max_delay)` (`:95-108`). Читает дефолты
из `gateway.restart_initial_delay_sec` (1.0) / `restart_max_delay_sec` (30.0)
(`:66-75`), `sleep` инъецируется для тестов.
**Зачем нужен.** Единственное место, где решается «перезапустить ли gateway
после падения».
**Вердикт.** `Оставить`.
**Обоснование.** Логика корректна и полностью покрыта
`tests/test_gateway_runner.py` (6 тестов, инъекция `sleep` работает).
Замечание к владению: `except Exception` перехватывает **всё**, включая
`SystemExit`-подобные ошибки конфигурации — при опечатке в `project.json`
gateway будет бесконечно рестартовать с растущей паузой вместо fail-fast;
стоит сузить до «ожидаемых» исключений (список) либо ограничить число рестартов.
`reset_backoff` при этом не нужен (см. ниже).

#### class `GatewayRunner` (строки 42-113, 3 метода)
**Назначение.** Backoff-перезапуск gateway.
**Зачем нужен.** Устойчивость к падениям на старте.
**Вердикт.** `Оставить`.
Атрибуты: `_initial_delay`, `_max_delay`, `_sleep`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 51-76 | Конфиг делey + инъекция `sleep` | — | `gateway.py:165`; `test_gateway_runner.py:11,22,36` | Оставить |
| `run_forever` | 78-108 | Цикл рестартов | — | `gateway.py:165`; 6 тестов | Оставить |
| `reset_backoff` | 110-113 | Вернуть начальную задержку | docstring говорит «используется в тестах» — **в тестах не используется** | 0 вызывающих | Удалить |

---

## `lib/session/pg_session_manager.py` — 137 LOC

**Назначение.** Cold-storage mirror поверх upstream `SessionManager`:
управляет сессиями через upstream JSONL, а PG-зеркало ведёт отдельный
`SessionColdSyncService`.
**Что делает.** `__init__` (`:51-76`) резолвит workspace, **обязательно**
требует `messages_table`/`meta_table` (иначе `ValueError`, `:61-66`),
конфигурирует глобальный пул `utils.db` через `configure(dsn)` (`:74-76`) и
сохраняет 5 атрибутов, из которых **ни один не читается**. Остальные методы —
`super()`-делегаты, кроме двух no-op-ов, которые ломают поведение
(`invalidate`, `flush_all`).
**Зачем нужен.** Режим `storage=postgres` (дефолт) — PG остаётся долговременным
зеркалом, JSONL — источником истины для hot path.
**Вердикт.** `Упростить`.
**Обоснование.** Класс после change `storage-hybridization` — это 5 делегатов
в `super()` + валидация конструктора. Пять делегирующих методов (`get_or_create`,
`save`, `list_sessions`, `read_session_metadata`, `read_session_file`) и
`delete_session` можно **полностью удалить** — они не добавляют ничего, кроме
дублирующего кода и docstring'ов. Два override'а — не no-op'ы, а регрессии
(врезка выше).

#### class `PGSessionManager` (строки 36-137, 12 методов)
**Назначение.** Session manager для режима postgres.
**Зачем нужен.** Выбор storage-режима: `lib/services/session_storage.py:124`.
**Вердикт.** `Упростить`.
**Обоснование.** Оставить `__init__` (валидация имён таблиц — реальный
fail-fast) и `close`; удалить 6 чистых делегатов, исправить 2 override'а,
удалить `_validate_ident`/`_quote` вместе с мёртвыми `_fq_*`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 51-76 | Fail-fast на именах таблиц; `configure(dsn)` | — | `session_storage.py:124` | Упростить — удалить `_schema`/`_meta_table`/`_messages_table`/`_fq_meta`/`_fq_messages` (`:68-72`, ни одного чтения) и локальный импорт `json`/`datetime` нерелевантен; **побочный эффект**: `utils.db.configure(dsn)` глобально перенастраивает DSN процесса из конструктора |
| `close` | 78-80 | No-op (пул общий) | — | вызывающий цикл shutdown | Оставить |
| `get_or_create` | 82-89 | `super().get_or_create` | — | upstream | Упростить — удалить override (пустое тело + docstring) |
| `save` | 91-98 | `super().save` | — | upstream | Упростить — удалить override |
| `list_sessions` | 100-102 | `super().list_sessions` | — | upstream/UI | Упростить — удалить override |
| `read_session_metadata` | 104-106 | `super().read_session_metadata` | — | upstream/UI | Упростить — удалить override |
| `read_session_file` | 108-110 | `super().read_session_file` | — | UI | Упростить — удалить override |
| `invalidate` | 112-114 | Заявлен как no-op | **нарушает контракт upstream** | `nanobot/session/manager.py:1872` (внутри `delete_session`) | Упростить — **удалить override**, вернуть поведение `super()` (иначе `delete_session` не работает) |
| `delete_session` | 116-118 | `super().delete_session` | — | `benchmarks/runner.py:414,443,525` | Упростить — удалить override |
| `flush_all` | 120-125 | Заявлен как no-op, возвращает `0` | **ломает durable shutdown** | `gateway.py:279`, `console_loop.py:482` | Упростить — **удалить override**, вернуть `super()` |
| `_validate_ident` | 128-130 | Валидация SQL-идентификатора | — | только `_quote:136` | Удалить |
| `_quote` | 133-137 | Квотирование `schema.table` | — | только `__init__:71-72` | Удалить |

---

## `lib/events/subagent.py` — 49 LOC

**Назначение.** Project-local тип runtime-события «оборот субагента завершён».
**Что делает.** `frozen=True` dataclass, наследующий `nanobot.events.AgentEvent`;
payload совпадает с `subagent_run_finished` в `agent_gateway_logs`.
**Зачем нужен.** Штатный `TurnCompleted` через subagent не публикуется
(обход `TurnDelivery`), поэтому нужен собственный тип, чтобы
`RuntimeEventsSubscriber` мог посчитать метрики субагентов.
**Вердикт.** `Оставить`.
**Обоснование.** Живой: publish `runtime_patcher.py:2092`, подписка
`runtime_events_subscriber.py:141`, тесты `tests/test_runtime_events_subscriber.py`.
Мёртвых типов в `lib/events/` нет. Замечание: docstring `:10,14` ссылается на
`runtime_patcher.py:1682,1712-1737`, фактические строки — `2092` и `2118+`
(строки-старые); и «не существует в 0.3.5» (`subagent.py:5`) конфликтует с
`AGENTS.md`, где версия библиотеки — `0.3.0` (фактически установлена `0.3.5`,
`nanobot_ai-0.3.5.dist-info`) — стоит зафиксировать одну версию в документации.

#### class `SubagentTurnCompleted` (строки 26-46, 0 методов, 11 полей)
**Назначение.** Событие финального завершения subagent-оборота.
**Зачем нужен.** Единственный канал наблюдаемости субагентов.
**Вердикт.** `Оставить**.
**Обоснование.** Активно публикуется и обрабатывается; удаление теряет
`subagent_run_finished`-телеметрию.

---

## `lib/hooks/__init__.py` — 0 LOC
**Назначение.** Пакетный маркер. **Вердикт.** `Оставить`. Обоснование:
на него ссылаются импорты вида `lib.hooks.database_logging_hook`; без него
`lib.hooks` не был бы пакетом.

## `lib/session/__init__.py` — 0 LOC
**Назначение.** Пакетный маркер. **Вердикт.** `Оставить**.

## `lib/cli/__init__.py` — 1 LOC
**Назначение.** Пакетный маркер + docstring. **Вердикт.** `Оставить**.

## `lib/lifecycle/__init__.py` — 1 LOC
**Назначение.** Пакетный маркер + docstring. **Вердикт.** `Оставить**.

## `lib/events/__init__.py` — 12 LOC
**Назначение.** Реэкспорт `SubagentTurnCompleted`. **Вердикт.** `Оставить**.
**Обоснование:** хотя весь код импортирует `lib.events.subagent` напрямую
(`runtime_patcher.py:2075`, `runtime_events_subscriber.py:53`, тест
`test_runtime_events_subscriber.py:24`), реэкспорт — корректная публичная точка
для внешних импорёров; удаление даст 3 правки импортов без выигрыша.

---

## Покрытие

Разобрано **73 символа** из 73 (8 классов, 40 методов, 25 функций) + 5
пакетных `__init__.py`. Символов с вердиктом `НЕ РАЗОБРАНО`: **0**.

## Сводка функциональных багов

| # | Баг | Локация | Severity |
|---|---|---|---|
| 1 | CLI-REPL дедлочится при `/exit` и Ctrl+C: `await gather(agent.run())` до `agent.stop()` | `lib/cli/console_loop.py:476` vs `:479` | высокий |
| 2 | `MessageBus.drain()` никогда не выполняется: `ctx.stop()` вне event loop, `RuntimeError` глотается | `lib/core/application_context.py:635-641`; вызовы из `gateway.py:301`, `cli_agent.py:166,209` | высокий |
| 3 | `PGSessionManager.invalidate()` = no-op → `delete_session` не вычищает кеш → сессия «воскресает» | `lib/session/pg_session_manager.py:112-114` | высокий |
| 4 | `PGSessionManager.flush_all()` = no-op → потеря несохранённых сессий + мёртвый лог «Flushed N» в обоих entrypoint'ах | `lib/session/pg_session_manager.py:120-125`; `gateway.py:279`, `console_loop.py:482` | средний |
| 5 | `ShutdownCoordinator` без таймаутов; docstring `:71-73` обещает защиту от зависания, которой нет | `lib/lifecycle/shutdown_coordinator.py:67-79` | средний |
| 6 | Порядок shutdown противоречит комментарию: подписчик отписывается **до** drain; `stop()` вызывается дважды | `lib/core/application_context.py:621,624-625,650` | средний |
| 7 | `_tool_audit` / `context_window` не доходят до рендера на streaming-пути (признано в коде как «известный gap») | `lib/cli/console_loop.py:274-290,448-464` | средний |
| 8 | `await` на синхронном `delete_session() -> bool` → `TypeError`, проглоченный `except Exception: pass`; чистка сессий в бенчмарках не работает | `benchmarks/runner.py:414,443,525` | средний |
| 9 | Утечки памяти на уникальный `session_key`: `_calls` (никогда не вычищается) и `_pending_start` (только `.get()`) | `lib/hooks/tool_audit_hook.py:39,41,69-71` | низкий |
| 10 | `_CONTEXT_BRIDGE` не чистится для не-`postgres` каналов (`pop_context_bridge` вызывается только из postgres_channel) | `lib/hooks/database_logging_hook.py:149-154`; `postgres_channel.py:1473` | низкий |

## Кросс-подсистемные находки

1. **Строки, которые лгут (docs ↔ код).** `RuntimePatcher.patch_compact_command`
   не существует (удалён в `post-0.3.5-patches-cleanup`), но на него ссылаются
   `AGENTS.md:43`, `README.md:193`, `docs/ARCHITECTURE.md:263,270,475-476,1377`
   вместе с обоснованием про `FINAL_TURN_KEY`. Инвариант надо либо
   переформулировать под `CompactionEventSubscriber`, либо удалить.
2. **Версия библиотеки.** Фактически установлена `nanobot 0.3.5`
   (`nanobot_ai-0.3.5.dist-info`), `AGENTS.md` и заголовок `AUDIT_PROTOCOL.md:9`
   говорят `0.3.0`, `lib/events/subagent.py:5` — `0.3.5`. Три версии в трёх
   местах; риск при апгрейде недооценивается именно из-за этого.
3. **`benchmarks/` ↔ `lib/session/`:** три вызова `await ...delete_session(...)`
   сломаны TypeError'ом (находка №8) — очистка сессии между итерациями
   бенчмарка не происходит. Тесты этого не ловят, т.к. мокают метод как async.
   **Правка в `lib/`: удалить override `delete_session`/`invalidate` НЕ решит
   проблему — нужно убрать `await` в `benchmarks/runner.py`.**
4. **`lib/cli/hook_loader.py:126-130` ↔ `lib/services/runtime_inventory.py:88-108`:**
   два независимых allowlist'а плагинов (см. врезку). `tools/diagnose_startup.py`
   сверяет реальность со вторым списком, поэтому расхождение первого даст
   ложный DRIFT/CRITICAL-баннер либо, наоборот, пропущенный плагин.
5. **`lib/core/agent_factory.py:15` ↔ `lib/hooks/terminal_tool_print_hook.py:18`:**
   `gateway.print_tools` объявлен как переключатель хука, но не читается ни
   в одном `*.py` (уже зафиксировано в `reports/01-core-entrypoints.md:658`).
   Здесь это означает дополнительно: `_tool_audit`-подобный живой вывод
   tool'ов в терминал включён всегда, несмотря на `print_tools: false` в
   `project.json:374`.
6. **`lib/core/agent_factory.py:174`** — `_agent_box` как изменяемый список +
   чтение `_agent_box[0]` (`:161`) — работает только пока `from_config` вызван
   ровно один раз. Комментарий `:109-112` отдельно оговаривает «ровно один раз»
   для другой причины; хрупкость стоит закрыть, заменив контейнер на
   одноклеточный объект.
