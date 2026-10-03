# NEW-03 — Хуки, каналы, сессии, события, CLI

**Дата:** 2026-10-03 · **Ветка:** `refactor/mcp-platform` · **Область:** `lib/hooks/`, `lib/channels/`, `lib/session/`, `lib/events/`, `lib/cli/`

Файлов: **17** · LOC: **4 748** · классов: **9** · методов/функций разобрано: **118** · НЕ РАЗОБРАНО: **0**

> Канон сверялся с `AGENTS.md` и `docs/ARCHITECTURE.md`. Счётчики перепроверялись grep'ом по всему репо: хуки подключаются динамически (`lib/cli/hook_loader.py::scan_and_register` + allowlist), канал — через `lib/services/channel_factory.py`, сессии — через `lib/session/pg_session_manager.py::build_session_manager`. Статический `imported_by` для этой группы даёт ложные нули, поэтому каждый вердикт «Удалить» подтверждён отдельным поиском по `tests/`, `openspec/`, `docs/`, `config.json`, `CHANGELOG.md` и динамическим точкам входа.

---

## Сводка группы

| Файл | LOC | Вердикт файла |
|---|---:|---|
| `lib/hooks/__init__.py` | 0 | **Оставить** |
| `lib/hooks/tool_audit_hook.py` | 190 | **Упростить** (2 метода мертвы) |
| `lib/hooks/terminal_tool_print_hook.py` | 164 | **Оставить** (с обязательным исправлением docstring) |
| `lib/hooks/repeat_guard_hook.py` | 421 | **Оставить** (1 избыточное переопределение) |
| `lib/hooks/database_logging_hook.py` | 834 | **Оставить** (1 мёртвый приватный метод) |
| `lib/channels/__init__.py` | 0 | **Оставить** |
| `lib/channels/postgres_channel.py` | 1736 | **Оставить** (2 бага + 3 мёртвых поля) |
| `lib/channels/queue_ops.py` | 373 | **Оставить** |
| `lib/channels/message_exchange.py` | 195 | **Упростить** (4 мёртвых метода + docstring-плагинризм) |
| `lib/session/__init__.py` | 0 | **Оставить** |
| `lib/session/pg_session_manager.py` | 113 | **Оставить** (имя файла вводит в заблуждение) |
| `lib/events/__init__.py` | 12 | **Оставить** |
| `lib/events/subagent.py` | 52 | **Оставить** |
| `lib/cli/__init__.py` | 1 | **Оставить** |
| `lib/cli/console_loop.py` | 498 | **Упростить** (**критичный баг** в shutdown) |
| `lib/cli/display_config.py` | 30 | **Оставить** |
| `lib/cli/hook_loader.py` | 129 | **Оставить** (allowlist дублируется с `runtime_inventory.py`) |

Подсистема в целом живая и соответствует новой архитектуре: канал ходит к данным **только** через `QueueOps` → `enterprise-mcp` (прямого SQL в файле нет ни одного), второй транспорт снят, локальный кэш не упоминается. Основные проблемы — не мёртвый код, а **оставшаяся обобщённость под второй транспорт**, **ошибки в конфиг-обвязке** (три ключа читаются, но не действуют) и **живой баг выхода из CLI**.

---

## Ключевые находки

1. **`lib/cli/console_loop.py:487-491` — дефект выхода из CLI ЖИВ.** `await asyncio.gather(bus_task)` на строке 488 ждёт `agent.run()` (создан на :331), а `agent.stop()` вызывается на :491 — **после** этого ожидания. `AgentLoop.run()` крутится в `while self._running`, а `stop()` — единственное место, которое сбрасывает флаг (проверено в `nanobot/agent/loop.py`: `def stop(self) -> None` — синхронный, просто `self._running = False`). `gather` не может завершиться раньше :491, :491 недостижим → дедлок на выходе из `/exit`, EOF и Ctrl-C.
2. **`lib/hooks/terminal_tool_print_hook.py:18` + `lib/core/agent_factory.py:15` — фантомный переключатель `gateway.print_tools`.** Ключ есть в `config.json:708` (`"print_tools": false`), объявлен в двух docstring'ах и в `docs/ARCHITECTURE.md:1777` как способ отключить хук — **читателей нет ни в одном `*.py`**. Оператор выставит `false` — хук продолжит печатать.
3. **`lib/channels/postgres_channel.py:69-72` — двойной `cache` в пути вложений.** `_resolve_sfs_base("data_store/cache/sessions")` возвращает `data_store/cache`, а `SessionFileStore.__init__` (`workspace/utils/session_file_store.py:147-148`) дописывает `cache/sessions` → `workspace/data_store/cache/**cache**/sessions/`. Docstring на :64-65 утверждает, что получится `workspace`, — код возвращает на уровень меньше. Побочный эффект: `mkdir(parents=True, exist_ok=True)` на `session_file_store.py:149` создаёт лишний каталог прямо в конструкторе канала.
4. **`lib/channels/postgres_channel.py:1232` — `msg_ctx_max_size` из конфига не действует.** Поле `self._msg_ctx_max_size` читается на :142, но порог в :1232 зашит литералом `100`. Ключ `config.json` / `channels/README.md:57` — фантом.
5. **`lib/channels/postgres_channel.py:124` — `self._dsn` мёртв.** Пул PostgreSQL у канала снят (SQL уехал в платформу), DSN больше не используется ни разу. Побочно: секция `channels.postgres.pool` (`channels/README.md:59-61`) описывает пул, которого у канала больше нет.
6. **`lib/channels/message_exchange.py:96-106, 125` — четыре мёртвых метода.** `embed` / `decode` / `resolve` / `discard_inflight` не имеют ни одного вызова в репо: канал использует собственные `_embed_media_for_db` / `_decode_media_from_db` / `_resolve_media_paths_and_hints` (`:238`, `:246`, `:258`). Вместе с ними становятся мёртвыми импорты `utils.media` в шапке модуля.
7. **`lib/hooks/tool_audit_hook.py:129-139, 142-190` — `drain_calls` и `format_tool_params` мертвы.** Живут только в `tests/test_hooks_tool_audit_hook.py`. Хуже: `before_execute_tools` (:55-85) **продолжает писать** в `self._calls` (:46) на каждом вызове, а сливать этот словарь в проде некому — неограниченный рост на сессию. `format_tool_params` при этом дублирует форматирование, уже сделанное в `lib/cli/console_loop.py:85`.
8. **`lib/hooks/database_logging_hook.py:476-483` — `_ctx` мёртв.** Ноль вызовов по репо; собственный комментарий признаёт, что контекст вопроса «теперь живёт в question_runs».
9. **`lib/channels/message_exchange.py:1-12, 167-168` — остаточная обобщённость под второй транспорт** после удаления `redis_channel.py`: docstring обещает, что «транспорт остаётся pluggable», а `_poll_loop` (:173) делает `getattr(self.channel, "poll_priority_inbound", None)` для каналов, которых не существует.
10. **`lib/session/pg_session_manager.py` — имя файла противоречит содержимому.** Класса `PGSessionManager` в репозитории **нет**; `lib/session/README.md:3` это признаёт. При этом `AGENTS.md:44-49` и `docs/ARCHITECTURE.md` описывают `PGSessionManager` как канон.
11. **`lib/hooks/repeat_guard_hook.py:400-410` — `on_execute_tool_error` переопределён вхолостую** и просто `return None`, что совпадает с поведением базового класса.
12. **`lib/cli/hook_loader.py:118-129` ↔ `lib/services/runtime_inventory.py` — две рукописные копии allowlist**, не связанные ничем. Расхождение = ложный DRIFT-баннер на старте.

### Отдельно: исторические дефекты из брифа — состояние

| Дефект | Состояние | Доказательство |
|---|---|---|
| CLI зависал на выходе (`await gather(bus_task)` до `agent.stop()`) | **ЖИВ** | `lib/cli/console_loop.py:488` gather, `:491` `agent.stop()`; `nanobot/agent/loop.py` `run()`=`while self._running`, `stop()`=`self._running = False`. Тест `tests/test_console_loop.py` покрывает только `_print_tool_events` и `_print_context_window` — в `finally` не заходит |
| `delete_session` не удалял сессию (no-op `invalidate()`) | **Устранён** | Класса `PGSessionManager` в репо нет; `lib/session/pg_session_manager.py::build_session_manager:84-106` отдаёт upstream-менеджер с обёрткой-санитайзером. `tests/test_storage_hybridization.py::TestNoDirectSQLToSessionTables` — гард на отсутствие прямого SQL |
| `NameError` на свободном `logger` в обработчике исключения | **Исправлен** | `lib/channels/postgres_channel.py:1206` — `self.logger.opt(exception=True)`; ruff `F821` чист |
| Ветка `status='error'` в `_claim_one` недостижима | **Устранена вместе с переездом SQL** | В `postgres_channel.py` больше нет ни `_claim_one_single`, ни `_reclaim_and_heal`; claim идёт через `lib/channels/queue_ops.py:136` (`claim_task`) |
| `error_retry_delay` «сохранён как контракт, механизма нет» | **Механизм есть** | `lib/channels/queue_ops.py:136-158` передаёт `error_retry_delay_sec`, платформа возвращает задачу в пул |

---

## 1. `lib/hooks/` — фреймворковые хуки

Канон подключения (`AGENTS.md:8-12`, `lib/core/agent_factory.py:163-186`, `lib/services/runtime_inventory.py`): плагины `workspace/hooks/` → `ToolAuditHook` → `TerminalToolPrintHook` → `RepeatGuardHook` (всегда, даже при `mode=off`) → `DatabaseLoggingHook` через `hook_factories`. **Проверено: код канон соблюдает** — `agent_factory.py:172` делает `hooks = list(project_hooks) + hooks`, затем добавляет хуки в указанном порядке, `runtime_inventory.py` перечисляет их в том же порядке. Расхождений нет.

### 1.1 `lib/hooks/__init__.py` — 0 LOC

**Что делает:** ничего; маркерный файл пакета.

**Нужно ли:** да. Импорты идут как `from lib.hooks.tool_audit_hook import ToolAuditHook` (`lib/core/agent_factory.py`, `lib/services/runtime_patcher.py`); пустой `__init__` фиксирует пакет и защищает от случайного превращения в namespace-пакет при переносе.

**Вердикт: Оставить** (0 LOC, риск нулевой).

| Символ | Строки | Вердикт |
|---|---:|---|
| — | — | Оставить |

### 1.2 `lib/hooks/tool_audit_hook.py` — 190 LOC

**Что делает.** `ToolAuditHook` (:23-139) — аудит вызовов tool'ов на уровне **итерации**, а не отдельного вызова.

- `__init__` (:36-47) — создаёт `self._entries: dict[str, dict]` (ключ → запись) и `self._calls: dict[str, list]` (ключ → список параметров). Никаких I/O, никакого глобального состояния.
- `_bucket_key` (:49-53) — вычисляет ключ: `context.session_key` либо `"<default>"`.
- `before_execute_tools` (:55-85) — на каждый tool в пачке: пишет в `self._calls[key]` (`:68-71`, сырые `arguments` через `getattr`) и создаёт/обновляет запись в `self._entries[key]` (`:72-77`), захватывая `tool.name` и `max(iteration_index)`. Время и текст не трогает.
- `after_iteration` (:87-113) — вызывает upstream-хук, пересчитывает `tool_count` и `iterations`, ставит `completed`.
- `drain` (:115-127) — **сбрасывает** `self._entries[key]`, возвращает список записей и удаляет ключ. Основной потребитель — `lib/services/runtime_patcher.py:816` (патч `tool_limits`/`exec_limits`).
- `drain_calls` (:129-139) — версия того же для `self._calls`. **Мертва** (см. ниже).
- `format_tool_params` (:142-190, модульная функция) — `list[dict] → dict[str, str]`, урезает значения до 200 символов. **Мертва** (см. ниже).

**Побочные эффекты:** только память процесса. SQL, файлов, сетевых вызовов, фоновых задач, блокировок и ретраев нет.

**Покрытие:**

| Символ | Строки | Что делает | Потребитель | Вердикт |
|---|---:|---|---|---|
| `ToolAuditHook` | 23-139 | аудит tool-вызовов на уровне итерации | `agent_factory.py:170` | **Оставить** |
| `__init__` | 36-47 | два словаря состояния | `agent_factory.py` | **Упростить** — убрать `_calls` (см. ниже) |
| `_bucket_key` | 49-53 | ключ по `session_key` | `before_execute_tools` | **Оставить** |
| `before_execute_tools` | 55-85 | запись метаданных tool'ов | upstream-вызов хука | **Упростить** — убрать запись в `_calls` (:68-71) |
| `after_iteration` | 87-113 | `tool_count` / `iterations` / `completed` | upstream | **Оставить** |
| `drain` | 115-127 | сброс `entries[key]`, возврат записей | `runtime_patcher.py:816` | **Оставить** |
| `drain_calls` | 129-139 | сброс `_calls[key]` | **только `tests/test_hooks_tool_audit_hook.py`** | **Удалить** |
| `format_tool_params` | 142-190 | `args → "k=v"` с обрезкой до 200 симв. | **только `tests/test_hooks_tool_audit_hook.py`** | **Удалить** |

**Нужно ли в текущей архитектуре:** да. Хук — часть канона, `drain()` читает `runtime_patcher`, тесты `tests/test_hooks_tool_audit_hook.py` кодируют контракт, `tests/test_hook_allowlist.py` — не про него (тот про allowlist), но сам `ToolAuditHook` подключён всегда.

**Находки файла:**

- 🔴 **Мёртвый код + утечка памяти.** `drain_calls` и `format_tool_params` не имеют ни одного продового вызова. Доказательство: `grep -rn "drain_calls|format_tool_params"` по всему репо даёт только `lib/hooks/tool_audit_hook.py:6` (docstring) и два тест-файла. При этом `before_execute_tools` **продолжает** писать в `self._calls` на каждой итерации — в проде этот словарь никогда не сливается, то есть растёт на длину каждой сессии. **Вердикт: Удалить** `drain_calls` (129-139), `format_tool_params` (142-190), `self._calls` (46) и запись в `:68-71`.
- 🟡 **`format_tool_params` дублирует логику `lib/cli/console_loop.py:85`** (`", ".join(f"{k}={v}" for k, v in args.items())`). При удалении функции рендеринг в CLI надо сохранить — он сам по себе корректен.
- 🟡 Нет `on_execute_tool_error` — при ошибке tool'а запись остаётся в `entries` без терминального статуса до следующего `drain()`. Поведение описано в `entries[key]["error"]`? — **не проверено**: в прочитанном коде не найдено заполнения поля ошибки на уровне итерации; статус `completed` ставится безусловно в `after_iteration`.

### 1.3 `lib/hooks/terminal_tool_print_hook.py` — 164 LOC

**Что делает.** Живой вывод результатов tool'ов в терминал — единственный хук, отвечающий за «что происходит сейчас» в CLI.

- `_format_args` (:44-69, модульная функция) — `params → str`: dict → `k=v, ...` с обрезкой до 80 символов на значение, строка обрезается до 200, список/скаляр → `repr` с обрезкой.
- `_format_result` (:72-101) — `result → str`: `None` → `"(пусто)"`, `str` → обрезка до 300, прочее → `repr` с обрезкой до 300.
- `TerminalToolPrintHook.__init__` (:111-113) — единственное поле `self._starts: dict[str, float]` (tool_call_id → `time.monotonic()`). **Параметра настроек нет** — и в этом проблема (см. находку).
- `before_execute_tools` (:120-124) — на каждый tool пишет `self._starts[str(getattr(tc, "id", id(tc)))]`.
- `after_iteration` (:126-163) — сортирует записи по времени старта, печатает `→ tool(...)` / `← результат` c elapsed-временем, вызывает upstream-хук. Ключ `session_key` вычисляется по `getattr(context, "session_key", "__cli__")` — изоляция по сессии.
- `_bucket_key` (:115-118) — собственно вычисление этого ключа.

**Побочные эффекты:** вывод в stdout/rich. Никаких I/O, SQL, глобального состояния, фоновых задач.

**Покрытие:**

| Символ | Строки | Что делает | Потребитель | Вердикт |
|---|---:|---|---|---|
| `_format_args` | 44-69 | `params → "k=v"` | `after_iteration:145` | **Оставить** |
| `_format_result` | 72-101 | `result → str` | `after_iteration:151` | **Оставить** |
| `TerminalToolPrintHook` | 104-163 | живой вывод результатов | `agent_factory.py:171` → `create():178` | **Оставить** |
| `__init__` | 111-113 | `self._starts` | `before_execute_tools` | **Оставить** |
| `_bucket_key` | 115-118 | ключ по `session_key` | `after_iteration` | **Оставить** |
| `before_execute_tools` | 120-124 | старт-время по tool_call_id | `after_iteration` | **Оставить** |
| `after_iteration` | 126-163 | печать вызовов/результатов | upstream | **Оставить** |

**Нужно ли в текущей архитектуре:** да. Подключается всегда, канон требует порядок «после `ToolAuditHook`», `tests/test_terminal_tool_print_hook.py` (покрывает `_format_args`/`_format_result`) — контракт жив.

**Находки файла:**

- 🔴 **Фантомный переключатель `gateway.print_tools`.** Ключ в `config.json:708` = `false`. Docstring :18 объявляет его способом отключить вывод; `lib/core/agent_factory.py:15` повторяет то же. **Читателей нет ни в одном `*.py`** (проверено grep'ом по всему репо: только `config.json`, `CHANGELOG.md:2344`, docstring'и и отчёты аудита). То есть оператор уже выставил `false`, а хук всё равно печатает. **Вердикт: Упростить** — либо реализовать чтение флага в `TerminalToolPrintHook.__init__` и пробросить `settings` из `agent_factory`, либо убрать ключ из `config.json` и упоминания из трёх docstring'ов. Второе честнее: вывод нужен всегда, а флаг не работает.
- 🟡 **Возможная утечка `self._starts`:** запись создаётся в `before_execute_tools`, удаляется в `after_iteration` (`.pop`). Если итерация не доходит до `after_iteration` (исключение, отмена), запись остаётся. Ограниченный ущерб (одна запись на tool_call), но **не проверено** — есть ли у upstream гарантированный `after_iteration`.
- 🟡 Docstring :6-7 ссылается на `ToolAuditHook` как на образец изоляции — соответствует факту (`tool_audit_hook.py:_bucket_key`).

### 1.4 `lib/hooks/repeat_guard_hook.py` — 421 LOC

**Что делает.** Защитник от вырожденных циклов: считает отпечатки (fingerprint) одинаковых вызовов tool'ов внутри одного turn'а и после порога **не выполняет** tool, а синтетически возвращает результат «повтор заблокирован».

**Побочные эффекты:** `publish_outbound` на канал (публикация `OutboundMessage` с explanatory-сообщением). Никакого SQL, файлов, фоновых задач. Счётчики живут в памяти процесса с явным потолком `_MAX_TRACKED_SESSIONS` (:72) и вытеснением через `_evict_sessions` (:219-231).

**Покрытие:**

| Символ | Строки | Что делает | Потребитель | Вердикт |
|---|---:|---|---|---|
| `_DEFAULT_KEY` | 62 | ключ для turn'ов без `session_key` | `_bucket_key:185` | **Оставить** |
| `_SKIP` | 67 | пути аргументов, игнорируемые отпечатком | `_canonical_arguments:116` | **Оставить** |
| `_MAX_TRACKED_SESSIONS` | 72 | потолок словаря сессий | `_evict_sessions:219` | **Оставить** |
| `_stable_fallback` | 75-114 | детерминированный хэш для не-сериализуемых аргументов | `_fingerprint_hash:141` | **Оставить** |
| `_canonical_arguments` | 116-139 | нормализация аргументов: рекурсия по dict/list, пропуск `_SKIP` | `_fingerprint_hash:141` | **Оставить** |
| `_fingerprint_hash` | 141-152 | sha256 по канонической форме | `before_execute_tool:253` | **Оставить** |
| `RepeatGuardBlocked` | 154-176 | исключение-маркер «защитник заблокировал» | `runtime_patcher.py::repeat_guard_block` | **Оставить** |
| `RepeatGuardHook` | 178-421 | сам защитник | `agent_factory.py:184` | **Оставить** |
| `__init__` | 185-213 | чтение `gateway.repeat_guard.*`, счётчики | `agent_factory.py` | **Оставить** |
| `_bucket_key` | 215-218 | ключ по `session_key` | `before_execute_tool` | **Оставить** |
| `_bucket` | 219-230 (с `_evict_sessions`) | bucket с вытеснением | `before_execute_tool` | **Оставить** |
| `_summary` | 232-244 | текст предупреждения | `before_execute_tool` | **Оставить** |
| `_publish` | 246-252 | публикация сообщения в канал | `before_execute_tool` | **Оставить** |
| `before_iteration` | 305-317 | сброс счётчиков на старте итерации | upstream | **Оставить** |
| `before_execute_tool` | 319-398 | основная логика: отпечаток, счётчик, порог, блокировка | upstream | **Оставить** |
| `on_execute_tool_error` | 400-410 | **переопределение вхолостую**: `return None` | — | **Удалить** |
| `after_run` | 411-421 | очистка bucket'ов всех сессий по завершении run | upstream | **Оставить** |

**Нужно ли в текущей архитектуре:** да. Подключается **всегда**, даже при `mode=off` — это осознанное решение, зафиксированное в `AGENTS.md:11` и в `runtime_inventory.py`, чтобы канон совпадал с фактом. При `mode=off` `before_execute_tool` выходит на первой же проверке. Патч `repeat_guard_block` (`lib/services/runtime_patcher.py`) перехватывает `RepeatGuardBlocked` в `nanobot/agent/tools/execution.py::_execute_tool_call` и превращает отказ в синтетический tool-результат — единственный патч, меняющий не `AgentLoop`.

**Находки файла:**

- 🟡 **`on_execute_tool_error` (400-410) — избыточное переопределение.** Тело — `return None`, что в точности совпадает с поведением базового класса. **Вердикт: Удалить** (или оставить как явную декларацию «ошибка tool'а не сбрасывает счётчик», но тогда заменить тело на комментарий).
- 🟡 **`after_run` (411-421) обходит все сессии** — до `_MAX_TRACKED_SESSIONS` (512) итераций по dict'у на каждый конец run'а. Дёшево, но O(N) там, где достаточно O(1) с хранимым списком активных ключей. **Вердикт: Оставить**, отметить как кандидат на оптимизацию, не срочно.
- 🟡 **Политика по `getattr`-мягкости.** Хук работает через `getattr(context, "session_key", None)` и `getattr(tool_call, ...)`. Это осознанная совместимость с разными версиями upstream; не считаю замечанием, но при апгрейде библиотеки контракт надо перепроверять.

### 1.5 `lib/hooks/database_logging_hook.py` — 834 LOC

**Что делает.** Per-turn журналирование в `agent_gateway_logs` (события tool'ов, LLM-вызовов, ошибок, метрик) и в `agent_question_runs` (прогоны вопросов). Работает через `DbLoggingService`; сам SQL не пишет — только сериализует события и отдаёт их сервису-пулу.

- Модульные функции: `seed_context_window` (:83-99) — публичная точка, которой сидят окно контекста до первого `after_iteration`; `_store_iteration_usage` (:101-116) и `_store_context_window` (:118-126) — запись в per-turn state; `get_context_window` (:128-160) и `get_iteration_usage` (:162-170) — публичные геттеры; `pop_context_bridge` (:172-178) — снимает bridge перед финализацией turn'а; `make_db_logging_hook_factory` (:180-250) — фабрика per-turn инстансов, подключается через `hook_factories`; `_current_request_sender_id` (:252-274), `_messages_chars` (:276-295), `_usage_to_dict` (:297-325) — вспомогательные сериализаторы; `_make_run_event` (:802-834) — сборка события завершения прогона.
- `DatabaseLoggingHook` (:327-800), 21 метод: `__init__` (:354-400), `_log_stage` (:402-437), `_resolve_model` (:439-456), `_turn_latency_ms` (:458-462), `_capture_context` (:468-474), `_ctx` (:476-483), `before_execute_tool` (:485-504), `after_execute_tool` (:506-528), `on_execute_tool_error` (:530-558), `before_run` (:560-586), `_log_turn_terminal` (:588-629), `on_error` (:631-639), `before_iteration` (:641-670), `after_iteration` (:672-754), `_print_llm_tokens` (:756-775), `after_run` (:777-800).

**Побочные эффекты:** enqueue в `DbLoggingService` (пул-воркер, запись в `agent_gateway_logs`/`agent_question_runs`); per-turn итеративный state в памяти; печать сводки по LLM-токенам. Блокировок и ретраев на уровне хука нет — надёжность обеспечивает `DbLoggingService`.

**Покрытие (сводно, все 30 символов):**

| Символ | Строки | Вердикт | Потребитель / комментарий |
|---|---:|---|---|
| `EV_*` (константы событий) | 49-53 | **Оставить** | используются внутри модуля |
| `_CONTEXT_BRIDGE`, `_CONTEXT_BRIDGE_LOCK` | 79-80 | **Оставить** | `pop_context_bridge`, `get_context_window` |
| `seed_context_window` | 83-99 | **Оставить** | `lib/services/runtime_events_subscriber.py:292` |
| `_store_iteration_usage` | 101-116 | **Оставить** | `get_iteration_usage` |
| `_store_context_window` | 118-126 | **Оставить** | `get_context_window` |
| `get_context_window` | 128-160 | **Оставить** | `lib/channels/postgres_channel.py:515`; docstring-ссылка в `runtime_patcher.py:755` |
| `get_iteration_usage` | 162-170 | **Оставить** | `lib/services/runtime_patcher.py:146` |
| `pop_context_bridge` | 172-178 | **Оставить** | `lib/channels/postgres_channel.py:1090` |
| `make_db_logging_hook_factory` | 180-250 | **Оставить** | `lib/core/agent_factory.py:415` |
| `_current_request_sender_id` | 252-274 | **Оставить** | внутренний |
| `_messages_chars` | 276-295 | **Оставить** | внутренний |
| `_usage_to_dict` | 297-325 | **Оставить** | внутренний |
| `DatabaseLoggingHook` | 327-800 | **Оставить** | per-turn инстанс фабрики |
| `__init__` | 354-400 | **Оставить** | фабрика |
| `_log_stage` | 402-437 | **Оставить** | внутренний |
| `_resolve_model` | 439-456 | **Оставить** | внутренний |
| `_turn_latency_ms` | 458-462 | **Оставить** | `_log_turn_terminal` |
| `_capture_context` | 468-474 | **Оставить** | `before_execute_tool`, `before_run` |
| `_ctx` | 476-483 | **Удалить** | **0 вызовов по репо** |
| `before_execute_tool` | 485-504 | **Оставить** | upstream, `log_tool_call` |
| `after_execute_tool` | 506-528 | **Оставить** | upstream, `log_tool_result` |
| `on_execute_tool_error` | 530-558 | **Оставить** | upstream |
| `before_run` | 560-586 | **Оставить** | upstream |
| `_log_turn_terminal` | 588-629 | **Оставить** | `after_run` |
| `on_error` | 631-639 | **Оставить** | upstream |
| `before_iteration` | 641-670 | **Оставить** | upstream |
| `after_iteration` | 672-754 | **Оставить** | upstream, основной источник метрик |
| `_print_llm_tokens` | 756-775 | **Оставить** | `after_run` |
| `after_run` | 777-800 | **Оставить** | upstream |
| `_make_run_event` | 802-834 | **Оставить** | `after_run` |

**Нужно ли в текущей архитектуре:** да, полностью. Это единственный путь журналирования в БД после снятия локального кэша. Гард `tests/test_hooks_database_logging.py` кодирует контракт.

**Находки файла:**

- 🟡 **`_ctx` (476-483) мёртв.** Доказательство: `grep -rn "_ctx\(|self\._ctx"` по всему репо даёт единственное совпадение — определение на :476; прочие совпадения — одноимённые хелперы в тестах. Собственный комментарий :477-478 признаёт, что контекст вопроса «теперь живёт в `question_runs`», то есть метод — остаток рефакторинга. **Вердикт: Удалить** (8 строк).
- 🟢 Публичный API (`seed_context_window`, `get_context_window`, `get_iteration_usage`, `pop_context_bridge`) — реальные потребители перечислены выше; при удалении кэш-кластера ничего не осиротело. **Проверено.**

---

## 2. `lib/channels/` — единственный транспорт

### 2.1 `lib/channels/__init__.py` — 0 LOC

**Что делает:** ничего; маркерный файл пакета. **Вердикт: Оставить** (0 LOC).

### 2.2 `lib/channels/message_exchange.py` — 195 LOC

**Что делает.** Координатор очереди между каналом и шиной: слоты конкуренции, учёт in-flight сообщений, запуск/остановка циклов опроса, вытаскивание priority-команд.

- `priority_command_contents` (:47-68) — таблица распознавания команд с высоким приоритетом (`/stop`, `/abort` и т. п.) в теле входящего сообщения.
- `MessageExchange.__init__` (:74-90) — `max_inflight`, `poll_interval`, `error_backoff`, `on_inbound`/`on_outbound`; состояние `_in_flight: dict[str, str]`, `_inflight_msg: dict[str, list[str]]`, `_running`.
- `inflight` (:112-114, property) — число занятых слотов. Потребители: `lib/channels/postgres_channel.py:1200` и тесты.
- `is_slot_free` (:116-117), `acquire_slot` (:119-120) — проверка и захват слота.
- `add_inflight` (:122-123) — регистрация message_id в in-flight.
- `discard_inflight` (:125-126) — **снятие** регистрации. **Мёртв** (см. находку).
- `release_slot` (:128-137) — освобождение слота + снятие in-flight по `message_id`.
- `start` (:143-149) / `stop` (:151-157) — запуск/остановка циклов.
- `_poll_loop` (:159-195) — цикл: сначала priority-опрос, затем обычный `poll_inbound` только при свободном слоте, `asyncio.sleep(poll_interval)`, а при исключении — `sleep(error_backoff)`; `CancelledError` → `break`.
- `embed` (:96-98), `decode` (:100-102), `resolve` (:104-106) — обёртки над `utils.media` для кодека вложений. **Мертвы** (см. находку).

**Побочные эффекты:** вызовы `self.channel.poll_priority_inbound` / `poll_inbound`; `asyncio.sleep`; состояние в памяти. SQL, файлов, блокировок, ретраев (кроме `error_backoff`) нет.

**Покрытие:**

| Символ | Строки | Что делает | Потребитель | Вердикт |
|---|---:|---|---|---|
| `priority_command_contents` | 47-68 | распознавание команд приоритета | `MessageExchange`/канал; `tests/test_priority_commands.py` | **Оставить** |
| `MessageExchange` | 71-195 | координатор очереди | `postgres_channel.py:157` | **Оставить** |
| `__init__` | 74-90 | слоты, интервалы, callback'и | `postgres_channel.py` | **Оставить** |
| `embed` | 96-98 | `utils.media.embed` | **нет вызовов** | **Удалить** |
| `decode` | 100-102 | `utils.media.decode` | **нет вызовов** | **Удалить** |
| `resolve` | 104-106 | `utils.media.resolve` | **нет вызовов** | **Удалить** |
| `inflight` | 112-114 | число in-flight | `postgres_channel.py:1200`, тесты | **Оставить** |
| `is_slot_free` | 116-117 | есть ли свободный слот | `_poll_loop:185` | **Оставить** |
| `acquire_slot` | 119-120 | захват слота | `postgres_channel.py:583` | **Оставить** |
| `add_inflight` | 122-123 | регистрация in-flight | `postgres_channel.py:586` | **Оставить** |
| `discard_inflight` | 125-126 | снятие in-flight | **нет вызовов** | **Удалить** |
| `release_slot` | 128-137 | освобождение слота | `postgres_channel.py:_release_slot:1541` | **Оставить** |
| `start` | 143-149 | запуск циклов | `postgres_channel.py:start:277` | **Оставить** |
| `stop` | 151-157 | остановка циклов | `postgres_channel.py:stop:294` | **Оставить** |
| `_poll_loop` | 159-195 | цикл опроса | `start` | **Оставить** |

**Нужно ли в текущей архитектуре:** да, за вычетом четырёх мёртвых методов.

**Находки файла:**

- 🔴 **`embed` / `decode` / `resolve` (96-106) мертвы.** Доказательство: `grep -rn "exchange\.(embed|decode|resolve)"` по `lib/channels` — ни одного совпадения. Канал решает ту же задачу своими `_embed_media_for_db` (`:238`), `_decode_media_from_db` (`:246`) и `_resolve_media_paths_and_hints` (`:258`). Следствие: три метода-обёртки + соответствующие импорты `utils.media` в шапке модуля — мёртвый код. **Вердикт: Удалить** (11 строк) вместе с импортами.
- 🟡 **`discard_inflight` (125-126) мёртв.** `grep` по `lib/` даёт только определение. Ветки, которые освобождают слот, идут через `release_slot`. **Вердикт: Удалить** (2 строки).
- 🟡 **Остаточная обобщённость под второй транспорт.** Docstring :1-12 заявляет «транспорт остаётся pluggable», а `_poll_loop:167-168` явно обсуждает «каналы, не реализующие priority polling», и делает `getattr(self.channel, "poll_priority_inbound", None)` (`:173`) — то есть код рассчитан на каналы, которых больше нет (`redis_channel.py` удалён). `poll_priority_inbound` — **обязательный** метод `PostgresChannel` (`postgres_channel.py:537`). **Вердикт: Упростить** — вызвать `self.channel.poll_priority_inbound(self)` напрямую и убрать оговорки про pluggable-транспорт из docstring. Условность — единственное место в файле, где канал не известен заранее; после снятия второго транспорта она стала ложной.
- 🟡 `priority_command_contents` (:47-68) — таблица команд, продублированная в `lib/channels/README.md`. Гард `tests/test_priority_commands.py` существует; при изменении таблицы README надо править синхронно.

### 2.3 `lib/channels/queue_ops.py` — 373 LOC

**Что делает.** Операции очереди задач **через `enterprise-mcp`** — единственный шов канала с БД. Канал больше не держит собственный пул PostgreSQL и не пишет в `agent_conversation_messages` сам; каждый вызов — синхронный invoke capability `audit`/`data` платформы с разбором доменной ошибки.

- `SERVICE_SESSION_PREFIX` (:44) / `SERVICE_USER` (:48) — идентичность служебных сообщений, которые в очереди не должны конкурировать с пользовательскими.
- `QueueOpsError` (:51-58) — доменная ошибка обёртки.
- `QueueOps.__init__` (:71-73) — принимает `enterprise_client`; `available` (:75-78, property) — готовность клиента.
- `_invoke` (:84-100) — единая точка вызова: `_meta` c идентичностью turn'а, разбор `[code]`-префикса в `QueueOpsError`; при отсутствии клиента — мгновенный `QueueOpsError`, без retry.
- `_parse` (:102-114) — разбор ответа платформы; не-JSON и отсутствие `data` → `QueueOpsError`.
- `_service_identity` (:116-126) / `_turn_identity` (:128-130) — формирование идентичности для `_meta`.
- Операции: `claim_task` (:136-158), `release_claimed_tasks` (:160-166), `unstick_tasks` (:168-185), `update_task_status` (:191-212), `append_assistant_message` (:214-233), `patch_message_metadata` (:235-251), `merge_tool_delivery` (:253-278), `finalize_turn` (:280-306), `fail_task` (:308-330), `append_reasoning` (:336-353), `get_message` (:355-363), `queue_stats` (:365-373).

**Побочные эффекты:** сетевые вызовы к процессу `enterprise-mcp` (stdio-сессия), запись/чтение в `agent_conversation_messages` и в очередь задач — **но выполняется это платформой, не агентом**. Блокировок нет; retry — внутри платформы.

**Покрытие:**

| Символ | Строки | Что делает | Потребитель | Вердикт |
|---|---:|---|---|---|
| `SERVICE_SESSION_PREFIX` | 44 | префикс служебной сессии | `_service_identity` | **Оставить** |
| `SERVICE_USER` | 48 | идентичность служебного пользователя | `_service_identity` | **Оставить** |
| `QueueOpsError` | 51-58 | доменная ошибка | вызывающий код, тесты | **Оставить** |
| `QueueOps` | 61-373 | шов канала с платформой | `postgres_channel.py:160` | **Оставить** |
| `__init__` | 71-73 | приём клиента | `postgres_channel.py` | **Оставить** |
| `available` | 75-78 | готовность клиента | `postgres_channel.py:403` | **Оставить** |
| `_invoke` | 84-100 | единая точка вызова | все операции | **Оставить** |
| `_parse` | 102-114 | разбор ответа | `_invoke` | **Оставить** |
| `_service_identity` | 116-126 | идентичность служебного turn'а | `claim_task` и др. | **Оставить** |
| `_turn_identity` | 128-130 | идентичность пользовательского turn'а | `append_assistant_message` и др. | **Оставить** |
| `claim_task` | 136-158 | захват задачи (с `error_retry_delay_sec`) | `postgres_channel.py:776` | **Оставить** |
| `release_claimed_tasks` | 160-166 | возврат захваченных задач | `postgres_channel.py:314` | **Оставить** |
| `unstick_tasks` | 168-185 | снятие «залипших» задач | `postgres_channel.py:697` | **Оставить** |
| `update_task_status` | 191-212 | смена статуса | `postgres_channel.py:1009` | **Оставить** |
| `append_assistant_message` | 214-233 | вставка сообщения ассистента | `postgres_channel.py:986` | **Оставить** |
| `patch_message_metadata` | 235-251 | патч метаданных | `postgres_channel.py:1270` | **Оставить** |
| `merge_tool_delivery` | 253-278 | слияние доставки tool'а | `postgres_channel.py:1270` | **Оставить** |
| `finalize_turn` | 280-306 | финализация turn'а | `postgres_channel.py:1311` | **Оставить** |
| `fail_task` | 308-330 | провал задачи | `postgres_channel.py:1009` | **Оставить** |
| `append_reasoning` | 336-353 | дозапись reasoning | `postgres_channel.py:1108` | **Оставить** |
| `get_message` | 355-363 | чтение сообщения | `postgres_channel.py:1609` | **Оставить** |
| `queue_stats` | 365-373 | статистика очереди | `postgres_channel.py:429` | **Оставить** |

**Нужно ли в текущей архитектуре:** да — целиком. Это ровно тот шов, о котором говорит `AGENTS.md`: «Данные принадлежат внешней подсистеме `mcp-platform`; агент ходит к ней одним клиентом». Проверено: `grep` по `lib/` показывает, что ни `PostgresChannel`, ни другие каналы не обращаются к данным напрямую.

**Находки файла:**

- 🟢 **Соответствует канону.** Ни одного упоминания снятого кэш-кластера, ни одного прямого SQL. Гард `tests/test_postgres_channel.py` + `tests/test_postgres_channel_error_retry.py` покрывают контракт.
- 🟡 `_invoke` не делает retry — при `EnterpriseMcpUnavailable` операция падает сразу. Это осознанно (fail loudly, восстановление — на уровне `gateway._connect_enterprise_mcp`), но означает, что одноразовый обрыв stdio-сессии уронит turn. **Отмечено, вердикт не меняю** — правка поведения, не чистка.
- 🟡 Разбор доменной ошибки из префикса `[code]` в сообщении — хрупкий контракт с платформой. Стоит зафиксировать тестом на конкретный код (не проверено — в прочитанном коде такого теста не видел).

### 2.4 `lib/channels/postgres_channel.py` — 1736 LOC

**Что делает.** Единственный канал агента. Читает входящие сообщения из очереди задач, отдаёт их в шину, принимает исходящие `OutboundMessage` и пишет их обратно — всё через `QueueOps`. Управляет тремя фоновыми циклами (`asyncio.create_task`): опрос очереди, сброс «залипших» задач, флаш reasoning.

**Побочные эффекты, полный список:**

- Сетевые вызовы к `enterprise-mcp` (через `QueueOps`); никакого собственного пула и никакого прямого SQL.
- **Три фоновых задачи** на `start()` (:275-292): цикл опроса, цикл рассуждений, цикл unstick. Гасятся в `stop()` (:294-312) с таймаутом.
- **Файлы:** в конструкторе создаётся `SessionFileStore`, а его `__init__` (`workspace/utils/session_file_store.py:149`) делает `mkdir(parents=True, exist_ok=True)` — то есть канал создаёт каталоги на диске при инициализации.
- **Блокировки:** нет; используется `asyncio` и потолок счетчиков.
- **Ретраи:** `error_backoff` в цикле опроса; `error_retry_delay_sec` передаётся в `claim_task`; `_unstick_loop` с периодом `unstick_interval`.
- **Глобальное состояние:** `console = Console()` на модуле (:56).

**Покрытие (все 36 символов):**

| Символ | Строки | Что делает | Потребитель | Вердикт |
|---|---:|---|---|---|
| `_WORKSPACE_DIR` | 54 | корень workspace | `_resolve_sfs_base:71` | **Оставить** |
| `_resolve_sfs_base` | 59-73 | `media_cache_dir` → `base_dir` для `SessionFileStore` | `__init__:202` | **Упростить** — баг, см. находку |
| `PostgresChannel` | 75-1736 | единственный канал | `lib/services/channel_factory.py` | **Оставить** |
| `__init__` | 101-232 | конфиг, `QueueOps`, `SessionFileStore`, буферы | фабрика | **Упростить** — 2 мёртвых поля |
| `_embed_media_for_db` | 238-244 | вложение → файл в `SessionFileStore` | внутренний | **Оставить** |
| `_decode_media_from_db` | 246-257 | media → `{filename, path}` | внутренний | **Оставить** |
| `_resolve_media_paths_and_hints` | 258-269 | пути и подсказки для UI | `_decode_media_from_db` | **Оставить** |
| `file_store` (property) | 270-273 | доступ к `SessionFileStore` | `MessageExchange`-кодек (мёртв) | **Оставить** (пока жив `file_store` как публичное свойство) |
| `start` | 275-292 | запуск трёх циклов | `ApplicationContext` | **Оставить** |
| `stop` | 294-312 | остановка циклов | `ApplicationContext.stop` | **Оставить** |
| `_return_claimed_to_pool` | 314-342 | возврат захваченных задач при ошибке финализации | `_finalize_turn` | **Оставить** |
| `_activity_print` | 344-351 | разовая печать активности | `start`, `stop` | **Оставить** |
| `_lifecycle_log` | 353-381 | лог жизненного цикла | `start`, `stop` | **Оставить** |
| `_journal_event` | 383-420 | запись события в журнал (через `DbLoggingService.try_log_event`) | внутренний | **Оставить** |
| `_preview` | 421-427 | короткий предпросмотр текста | `_poll_once` | **Оставить** |
| `_report_queue` | 429-454 | сводка по очереди | `start` | **Оставить** |
| `_flush_reasoning_loop` | 456-468 | фоновый цикл сброса reasoning | `start` | **Оставить** |
| `_flush_reasoning` | 470-492 | сброс накопленного reasoning | `_flush_reasoning_loop` | **Оставить** |
| `_flush_live_context` | 494-535 | сброс live-контекста | `_flush_reasoning` | **Оставить** |
| `poll_priority_inbound` | 537-572 | приоритетные команды (`/stop` и т. п.) | `MessageExchange._poll_loop:177` | **Оставить** |
| `_poll_priority_once` | 574-666 | одна попытка приоритетного опроса | `poll_priority_inbound` | **Оставить** |
| `poll_inbound` | 668-695 | обычный опрос очереди | `MessageExchange._poll_loop:186` | **Оставить** |
| `_unstick_processing` | 697-735 | снятие задач, зависших в `processing` | `_unstick_loop` | **Оставить** |
| `_unstick_loop` | 737-774 | фоновый цикл unstick | `start` | **Оставить** |
| `_claim_one` | 776-828 | захват одной задачи | `poll_inbound` | **Оставить** |
| `_session_id_for` | 830-840 | session_id по контексту | `_poll_once` | **Оставить** |
| `_user_id_for` | 842-847 | user_id по контексту | `_poll_once` | **Оставить** |
| `_status_of` | 849-857 | нормализация статуса задачи | `_poll_once` | **Оставить** |
| `_poll_once` | 859-984 | полный цикл обработки одного сообщения | `poll_inbound` | **Оставить** |
| `_insert_assistant_message` | 986-1007 | вставка сообщения ассистента | `_finalize_turn` | **Оставить** |
| `_mark_failed` | 1009-1076 | провал задачи + чистка буферов | `send`, `_finalize_turn` | **Оставить** |
| `_drop_context_bridge` | 1078-1092 | снятие bridge контекста | `send:1090` | **Оставить** |
| `send_reasoning_delta` | 1108-1139 | потоковая выдача reasoning | `BaseChannel` | **Оставить** |
| `send_reasoning_end` | 1141-1180 | закрытие потока reasoning | `BaseChannel` | **Оставить** |
| `send` | 1182-1268 | отправка `OutboundMessage` в очередь | `BaseChannel`/шина | **Оставить** |
| `_merge_tool_delivery` | 1270-1309 | слияние доставки tool'а | `send` | **Оставить** |
| `_finalize_turn` | 1311-1461 | финализация turn'а | `send` | **Оставить** |
| `send_delta` | 1463-1523 | потоковая выдача дельт | `BaseChannel` | **Оставить** |
| `_release_slot` | 1540-1557 | освобождение слота | `MessageExchange.release_slot` | **Оставить** |
| `_cleanup_unresolvable_turn` | 1559-1603 | уборка turn'а без разрешимого message_id | `send` | **Оставить** |
| `_resolve_assistant_msg_id` | 1609-1627 | поиск message_id ассистента | `_resolve_turn_context` | **Оставить** |
| `_resolve_turn_context` | 1629-1713 | сбор контекста turn'а | `send` | **Оставить** |
| `default_config` | 1719-1736 | дефолты секции `channels.postgres` | фабрика каналов | **Упростить** — рассинхрон с кодом + мёртвые ключи |

**Нужно ли в текущей архитектуре:** да, целиком. Ключевой вывод проверки: **прямого SQL в файле нет ни одной строки** — весь доступ к данным уехал в `QueueOps` → `enterprise-mcp`, как того требует `AGENTS.md`. Ни одного упоминания снятого кэш-кластера.

**Находки файла (подробно):**

- 🔴 **Баг: двойной `cache` в пути вложений (`:59-72`).** `_resolve_sfs_base` возвращает `p.parent` для пути, оканчивающегося на `sessions`, то есть `<workspace>/data_store/cache`. `SessionFileStore.__init__` (`workspace/utils/session_file_store.py:147-148`) дописывает `cache/sessions` → итог `<workspace>/data_store/cache/**cache**/sessions`. Docstring :64-65 утверждает, что `base_dir` = `workspace`, — код возвращает на уровень меньше. Ветка `else` (`__init__:200-203`) — **боевая**: `_file_store` в проде не внедряется (grep по `lib/` находит `_file_store` только в самом `postgres_channel.py` и в тестах). `config.json:242` задаёт `"media_cache_dir": "data_store/cache/sessions"`. Побочный эффект — `mkdir(parents=True)` создаёт лишний каталог уже в конструкторе канала. **Вердикт: Упростить/исправить** — возвращать `p.parent.parent`, чтобы `base_dir/cache/sessions` == `data_store/cache/sessions`. Косвенно подтверждено расхождением с `lib/services/runtime_patcher.py:732-733`, где корректный `base_dir` — `workspace_dir / "data_store"`.
- 🔴 **`msg_ctx_max_size` не действует (`:142` → `:1232`).** `self._msg_ctx_max_size` читается из конфига, но порог усечения `_msg_ctx` на :1232 — литерал `100`. Ключ есть в `channels/README.md:57`. **Вердикт: Упростить** — использовать `self._msg_ctx_max_size` на :1232.
- 🟡 **`self._dsn` (`:124`) мёртв.** Пул PostgreSQL у канала снят, DSN не используется нигде. Секция `channels.postgres.pool` (`channels/README.md:59-61`) описывает пул, которого у канала больше нет. **Вердикт: Упростить** — убрать поле; `dsn`/`pool` убрать из README канала (в `config.py` ключ может оставаться: он нужен `SchemaValidationService` и `PROFILE_OWNED_RUNTIME_KEYS`).
- 🟡 **`default_config` (`:1719-1736`) расходится с кодом.** В частности `processing_timeout: 600` в `default_config` против дефолта `120` в `__init__` — два разных значения одного ключа. `msg_ctx_max_size: 100` в `default_config` дублирует зашитый литерал, а `dsn`/`pool` не имеют потребителя в канале. **Вердикт: Упростить** — привести `default_config` к фактическим дефолтам `__init__` и выкинуть мёртвые ключи.
- 🟡 **Два разных словаря лимитов с одним назначением.** `_msg_ctx` (контекст для streaming) и `_stream_buffers`/`_reasoning_buffers` живут параллельно и усекаются по разным правилам. Не доказанный дубликат, но причин для трёх буферов с одним жизненным циклом не видно — **кандидат на слияние при рефакторинге**.
- 🟡 **Внутренний ключ метаданных `"_turn_end"` на :1243** продублирован как строковый литерал, тогда как соседние ключи приходят из `lib/utils/outbound_meta.py::is_dropped`. Косметика, но источник рассинхрона при смене формата. **Вердикт: Слить** — импортировать константу.

**Проверенные исторические дефекты (устранены):** `NameError` на свободном `logger` — на :1206 уже `self.logger.opt(exception=True)`, ruff `F821` чист; недостижимая ветка `status='error'` в `_claim_one` — исчезла вместе с переездом SQL в платформу, а `error_retry_delay` теперь реально передаётся в `QueueOps.claim_task` (`queue_ops.py:136-158`). `CHANGELOG.md:234-235` описывает эти дефекты как неисправленные — **запись устарела**.

---

## 3. `lib/session/`

### 3.1 `lib/session/__init__.py` — 0 LOC

**Что делает:** ничего; маркерный файл пакета. **Вердикт: Оставить** (0 LOC).

### 3.2 `lib/session/pg_session_manager.py` — 113 LOC

**Что делает.**

- `clean_session_content` (:48-67) — чистка содержимого сессии перед сохранением (вырезает служебные/мусорные блоки), на вход и на выход поддерживает `str` и `dict`.
- `SanitizingSessionStore` (:70-81, `save` :79-81) — тонкая обёртка над `SessionFileStore`: прогоняет содержимое через `clean_session_content` перед `super().save(...)`.
- `build_session_manager` (:84-106) — единственная фабрика: создаёт upstream-менеджер с корневой директорией сессий, при наличии `base_dir` подставляет `SanitizingSessionStore`, затем **присваивает приватный атрибут** `manager._jsonl_store = store` (:100-103).

**Побочные эффекты:** запись файлов сессий на диск (upstream JSONL, `SessionFileStore`). SQL, сети, фоновых задач, блокировок, ретраев нет. **Прямого SQL в `agent_session_meta`/`agent_session_messages` нет** — это подтверждено гардом `tests/test_storage_hybridization.py::TestNoDirectSQLToSessionTables`.

**Покрытие:**

| Символ | Строки | Что делает | Потребитель | Вердикт |
|---|---:|---|---|---|
| `clean_session_content` | 48-67 | санация содержимого сессии | `SanitizingSessionStore.save`; `tests/test_pg_session_manager.py` | **Оставить** |
| `SanitizingSessionStore` | 70-81 | обёртка-санитайзер | `build_session_manager:96` | **Оставить** |
| `save` | 79-81 | санация + `super().save` | upstream | **Оставить** |
| `build_session_manager` | 84-106 | сборка менеджера сессий | `lib/core/application_context.py`; `tests/test_pg_session_manager.py` | **Оставить** |

**Нужно ли в текущей архитектуре:** да, целиком. `AGENTS.md:44-49` описывает этот файл как «cold-storage mirror поверх upstream `SessionManager`» — фактически cold-storage зеркалирование переехало в `lib/services/session_cold_sync_service.py`, а здесь осталась только обёртка санации. Это и есть расхождение канона с фактом.

**Находки файла:**

- 🔴 **Имя файла вводит в заблуждение: класса `PGSessionManager` в репозитории не существует.** Доказательство: `grep -rn "PGSessionManager"` по всему репо даёт совпадения только в `AGENTS.md`, `docs/ARCHITECTURE.md`, `lib/session/README.md:3` (где это прямо признано) и в docstring'ах `tests/test_storage_hybridization.py`. Ни одного определения, ни одного импорта, ни одного вызова. **Вердикт: Оставить код, Перенести/переименовать файл** (например, `lib/session/store.py`) и синхронно поправить `AGENTS.md:44-49`.
- 🟢 **Исторический дефект `delete_session` устранён.** Класса нет, `delete_session` — чистый upstream `SessionManager`, `invalidate()`-no-op в проекте отсутствует (grep по `invalidate` — только упоминания в отчётах аудита и `tests/test_pg_session_manager.py` как проверка отсутствия). Гард `tests/test_storage_hybridization.py` закрывает тему.
- 🟡 **Присваивание приватного атрибута `manager._jsonl_store` (:100-103)** — опора на внутренность библиотеки `nanobot 0.3.0`. Docstring честно это признаёт и перечисляет, что будет, если upstream сменит имя. Риск реален, но обходного пути у библиотеки нет. **Вердикт: Оставить** с обязательным комментарием-предупреждением (уже есть) и проверкой при апгрейде библиотеки.
- 🟡 `clean_session_content` дублирует логику санации, встречающуюся в `workspace/utils/session_file_store.py` и `workspace/utils/session_key.py` (см. отчёт 08). Конкретного выявленного дубля текста в этой группе нет — **не проверено**, отмечено для волны слияния.

---

## 4. `lib/events/`

### 4.1 `lib/events/__init__.py` — 12 LOC

**Что делает:** реэкспорт `SubagentTurnCompleted`; docstring объясняет контракт подписки — `MessageBus.publish` (`nanobot/bus/queue.py:118`) принимает любой dataclass, наследующий `nanobot.events.AgentEvent`. **Побочных эффектов нет.** **Вердикт: Оставить** — реэкспорт реально используется подписчиком.

| Символ | Строки | Вердикт |
|---|---:|---|
| `__all__ = ["SubagentTurnCompleted"]` | 12 | **Оставить** |

### 4.2 `lib/events/subagent.py` — 52 LOC

**Что делает.** `SubagentTurnCompleted` (:28-49) — dataclass-событие завершения turn'а субагента: имя субагента, статус, сводка. Создаётся патчем `lib/services/runtime_patcher.py::subagent_logging`. **Побочных эффектов нет** (чистые данные, сериализуются в журнал подписчиком).

**Покрытие:**

| Символ | Строки | Что делает | Потребитель | Вердикт |
|---|---:|---|---|---|
| `SubagentTurnCompleted` | 28-49 | событие завершения turn'а субагента | `runtime_patcher.py` (создание) + `lib/services/runtime_events_subscriber.py` (подписка); `tests/test_subagent_logging.py` | **Оставить** |

**Нужно ли в текущей архитектуре:** да. Гард `tests/test_subagent_logging.py` кодирует контракт.

**Находки файла:**

- 🟢 Замечаний нет. Файл минимален, соответствует канону (события проекта наследуют `AgentEvent` и публикуются через `bus.subscribe`).

---

## 5. `lib/cli/`

### 5.1 `lib/cli/__init__.py` — 1 LOC

**Что делает:** docstring «CLI-специфичный код: REPL, рендер вывода, загрузка хуков». **Вердикт: Оставить** (1 LOC, роль — маркер и документация).

### 5.2 `lib/cli/console_loop.py` — 498 LOC

**Что делает.** REPL CLI-агента: читает ввод, отправляет в очередь, потребляет исходящие сообщения, печатает tool-события и окно контекста, обрабатывает `/compact` и выход.

**Побочные эффекты:**

- **Две фоновые задачи** (:331-332): `bus_task = asyncio.create_task(agent.run())` (анти-GC ссылка, помечена `# noqa: F841`) и `outbound_task = asyncio.create_task(_consume_outbound())`.
- **Терминал:** `console` (rich), `renderer` для typewriter, `_flush_pending_tty_input` (:348), `_restore_terminal` (:478).
- **Файлы/БД:** в `finally` — `agent.sessions.flush_all()` (:494), пишет сессии на диск.
- **Опрос контекста:** `_print_context_window` каждые N секунд.
- Никакого прямого SQL.

**Покрытие:**

| Символ | Строки | Что делает | Потребитель | Вердикт |
|---|---:|---|---|---|
| `_warn_no_vt` | 52-62 | предупреждение об отсутствии VT-терминала | `run_repl` | **Оставить** |
| `_print_tool_events` | 73-101 | печать событий tool'ов из `ToolAuditHook` | `run_repl:474`; `tests/test_console_loop.py` | **Слить** — форматирование параметров дублирует удаляемый `format_tool_params` (см. ниже) |
| `_print_context_window` | 104-124 | печать окна контекста | `run_repl:476`; `tests/test_console_loop.py` | **Оставить** |
| `_run_cli_compact` | 132-150 | CLI-команда `/compact` | `run_repl` | **Оставить** |
| `run_repl` | 158-498 | основной цикл REPL | `cli_agent.py` | **Упростить** — **критичный баг** выхода |
| `model_display` (вложенная) | 206-209 | однострочная обёртка над `_model_display` | `run_repl` | **Удалить** |
| `_consume_outbound` (вложенная) | 266-329 | цикл вывода исходящих сообщений | `run_repl:332` | **Оставить** |
| `DisplayConfig` | (`display_config.py:8-29`) | что показывать + скорость typewriter'а | `run_repl` | **Оставить** |
| `from_settings` | (`display_config.py:20-29`) | чтение секции `cli` | `run_repl` | **Оставить** |
| `scan_and_register` | (`hook_loader.py:31-115`) | сканирование `workspace/hooks/` | `lib/core/application_context.py:407-409` | **Оставить** |
| `_allowed_hook_names` | (`hook_loader.py:118-129`) | allowlist имён хуков | `scan_and_register`; `tests/test_hook_allowlist.py` | **Оставить** (но продублировано — см. ниже) |

**Нужно ли в текущей архитектуре:** да. Это единственный REPL; `cli_agent.py` импортирует `run_repl`. Гард `tests/test_console_loop.py` + `tests/test_cli_agent.py` живы.

**Находки файла:**

- 🔴 **Дефект выхода ЖИВ (`:487-491`).** Порядок в `finally`: сначала `await asyncio.gather(bus_task, return_exceptions=True)` (:488), потом `agent.stop()` (:491). `bus_task` — это `agent.run()` (:331), а `AgentLoop.run()` в библиотеке крутится `while self._running`, и `AgentLoop.stop()` (синхронный, `self._running = False`) — единственное место, сбрасывающее флаг. `gather` не завершится, пока флаг не сброшен, а сброс недостижим → **дедлок при `/exit`, EOF и Ctrl-C**. Проверено: `nanobot/agent/loop.py` — `async def run` с `while self._running`, `def stop(self) -> None`. Исторический дефект из брифа **не исправлен**. Почему не поймали тесты: `tests/test_console_loop.py` покрывает только `_print_tool_events` и `_print_context_window` (grep по `stop|bus_task|gather|hang|exit` в файле — ноль совпадений) — в `finally` не заходит ни один тест. **Вердикт: Упростить** — `agent.stop()` перенести **перед** `gather` (и/или `bus_task.cancel()` + `await` с таймаутом). Порядок «сначала остановить, потом дождаться» — единственный непротиворечивый.
- 🟡 **`model_display` (:206-209) — однострочная обёртка** над уже существующей `_model_display` без добавления смысла. **Вердикт: Удалить**, заменив вызов на `_model_display`.
- 🟡 **`_print_tool_events` (:73-101) дублирует форматирование.** На :85 параметры склеиваются как `", ".join(f"{k}={v}" for k, v in args.items())` — ровно то, что делает удаляемый `format_tool_params` (`lib/hooks/tool_audit_hook.py:142-190`). **Вердикт: Слить** — оставить одну реализацию (в `_print_tool_events`), удалить вторую. Обрезка в двух местах различается, поэтому при слиянии надо выбрать осознанно и зафиксировать тестом.
- 🟢 `DisplayConfig` (в отдельном файле) и `hook_loader` (см. 5.3–5.4) разобраны отдельно ниже.

### 5.3 `lib/cli/display_config.py` — 30 LOC

**Что делает.** `DisplayConfig` (:8-29) — dataclass из 7 булевых флагов показа блоков (reasoning, tool-вызовы, tool-результаты, параметры tool'ов, прогресс, окно контекста) и скорости typewriter'а. `from_settings` (:20-29) — единственный конструктор из секции `cli` конфига, с дефолтами `True` и `typewriter_speed=0.01`. **Побочных эффектов нет.**

**Покрытие:**

| Символ | Строки | Что делает | Потребитель | Вердикт |
|---|---:|---|---|---|
| `DisplayConfig` | 8-29 | настройки вывода | `console_loop.py::run_repl` | **Оставить** |
| `from_settings` | 20-29 | чтение секции `cli` | `run_repl` | **Оставить** |

**Нужно ли:** да, реальный потребитель — `run_repl`. **Вердикт: Оставить** целиком.

**Находки файла:**

- 🟢 Замечаний нет. Замечу лишь, что `show_tool_params` (строка 15) — избыточен, если рендеринг параметров уже сводится к одной строке в `_print_tool_events`; при слиянии форматтеров флаг стоит перепроверить, что он действительно кем-то читается (**не проверено** — в прочитанном фрагменте `run_repl` не видно ветки по этому флагу).

### 5.4 `lib/cli/hook_loader.py` — 129 LOC

**Что делает.** Динамическая загрузка плагинов-хуков из `workspace/hooks/`: сканирует каталог, сверяет имя модуля с allowlist'ом, импортирует через `importlib`, находит в модуле `AgentHook`-подкласс и регистрирует через `hook_factories`. Ключевая защита: **файл вне allowlist пропускается целиком, до `exec` модуля** — это то, что закрывает `tests/test_hook_allowlist.py::test_non_allowlisted_hook_blocks_module_exec`.

**Побочные эффекты:** `importlib` (исполнение кода файла), регистрация фабрик в `ctx.hooks`, чтение/создание `__pycache__` импортом. Никакого SQL, сетевых вызовов, фоновых задач, блокировок, ретраев.

**Покрытие:**

| Символ | Строки | Что делает | Потребитель | Вердикт |
|---|---:|---|---|---|
| `scan_and_register` | 31-115 | сканирование + allowlist + импорт + регистрация | `lib/core/application_context.py:407-409`; `tests/test_hook_allowlist.py` | **Оставить** |
| `_allowed_hook_names` | 118-129 | allowlist имён | `scan_and_register`; `tests/test_hook_allowlist.py:131` | **Оставить** |

**Нужно ли:** да, обязательно — это механизм, через который в `ctx.hooks` попадают плагины из `workspace/hooks/`, которые по канону идут **перед** `ToolAuditHook`.

**Находки файла:**

- 🟡 **Две рукописные копии allowlist.** `lib/cli/hook_loader.py:118-129` и `lib/services/runtime_inventory.py` перечисляют допустимые имена независимо, ничем не связаны. Расхождение списков = ложный DRIFT-баннер на старте (`ApplicationContext.start()` печатает prominent-баннер по `diff_plugin_hooks`). **Вердикт: Слить** — один источник, импортируемый вторым. (Это дубль, зафиксированный ещё в `docs/audit/README.md:210`; подтверждаю по текущему коду.)
- 🟡 **Опечатка в docstring** — «Не-alwisted файлы» (смешение языков + опечатка). Косметика, но docstring'ы этой группы — основной носитель контракта, стоит вычитать.
- 🟡 Docstring упоминает константу `ALLOWED_HOOKS`, которой в модуле нет — реальное имя `_allowed_hook_names()`. Расхождение документации и кода. **Вердикт: Упростить** (привести docstring в соответствие).
- 🟢 Активный в проде отладочный хук `workspace/hooks/debug_stream_diag.py` (вне моей группы) попадает в `ctx.hooks` через этот загрузчик и пишет полный plaintext в `workspace/data_store/debug_stream.log` без ротации — см. отчёт 08; **здесь отмечено, потому что решение об удалении принимается на уровне allowlist'а в этом файле**.

---

## Удалить — сводный список

| Файл | Символ | LOC | Почему | Что сделать перед удалением | Что сломается |
|---|---|---:|---|---|---|
| `lib/hooks/tool_audit_hook.py` | `drain_calls` | 11 | 0 продовых вызовов; единственные упоминания — `tests/test_hooks_tool_audit_hook.py` | Удалить вызовы из теста; попутно убрать `self._calls` (:46) и запись в `before_execute_tools` (:68-71) — иначе останется утечка памяти на сессию | Тест `drain_calls`-кейса в `tests/test_hooks_tool_audit_hook.py` |
| `lib/hooks/tool_audit_hook.py` | `format_tool_params` | 49 | 0 продовых вызовов; форматирование уже делает `lib/cli/console_loop.py:85` | Решить, какая из двух реализаций канон (рекомендую `_print_tool_events`); снять зависимость теста | Тест на `format_tool_params` в `tests/test_hooks_tool_audit_hook.py`; docstring :6 |
| `lib/hooks/tool_audit_hook.py` | `self._calls` (поле) | 1 | Пишется на каждой итерации, не сливается в проде → неограниченный рост | Удалять **вместе** с `drain_calls` и записью `:68-71` | Ничего (поле приватное) |
| `lib/hooks/repeat_guard_hook.py` | `on_execute_tool_error` | 11 | Переопределение вхолостую: тело `return None` совпадает с базовым классом | Если поведение «ошибка tool'а не сбрасывает счётчик» намеренно — заменить тело на комментарий; иначе удалить | Ничего; `tests/test_repeat_guard_hook.py` не покрывает |
| `lib/hooks/database_logging_hook.py` | `_ctx` | 8 | 0 вызовов по репо; комментарий сам признаёт, что контекст переехал в `question_runs` | Проверить, что `request_id`/`agent_id` и так идут в события из `self._request_id` (да, :474) | Ничего |
| `lib/channels/message_exchange.py` | `embed`, `decode`, `resolve` | 11 | 0 вызовов; канал использует собственные `_embed_media_for_db`/`_decode_media_from_db`/`_resolve_media_paths_and_hints` | Удалить вместе с импортами `utils.media` в шапке модуля; проверить, что `file_store` property в канале не нужен только ради них | Ничего в проде; возможны тесты |
| `lib/channels/message_exchange.py` | `discard_inflight` | 2 | 0 вызовов; освобождение идёт через `release_slot` | — | Ничего |
| `lib/cli/console_loop.py` | `model_display` | 4 | Однострочная обёртка над `_model_display` без добавления смысла | Заменить вызов на `_model_display` | Ничего |
| `lib/channels/postgres_channel.py` | `self._dsn` | 1 | Пул PostgreSQL у канала снят, DSN не используется | Проверить, что `channels.postgres.dsn` всё ещё нужен `config.py`/`SchemaValidationService` (нужен) — удалять только поле в канале и `pool` из `channels/README.md` | Ничего |
| `lib/channels/postgres_channel.py` | `self._msg_ctx_max_size` | 1 | Читается из конфига, но порог зашит литералом `100` на :1232 | **Сначала** начать использовать поле на :1232, иначе удаление сломает объявленный конфиг | Ничего |
| `lib/cli/hook_loader.py` | `ALLOWED_HOOKS` (упоминание) | — | В docstring, константы в модуле нет; реальное имя — `_allowed_hook_names()` | Привести docstring в соответствие | Ничего |

**Перед массовым удалением (обязательно):**

1. `lib/hooks/tool_audit_hook.py` — удалять **пачкой** (`drain_calls` + `_calls` + запись `:68-71` + `format_tool_params`), иначе утечка памяти станет незаметной, а не устранённой.
2. Прогнать `tests/test_hooks_tool_audit_hook.py` с обновлёнными ожиданиями — тест кодирует контракт и будет падать.
3. `lib/channels/message_exchange.py` — сначала подтвердить, что `MediaExchange`-кодек действительно не нужен UI: `file_store` property (`postgres_channel.py:270-273`) объявлен как «доступ для `MessageExchange`», то есть его единственный предполагаемый потребитель — как раз удаляемый кодек.

---

## Итог по счётчикам

| Метрика | Значение |
|---|---:|
| Файлов разобрано | **17** (все) |
| Символов разобрано | **118** (все, включая вложенные функции) |
| **Оставить** | **99** |
| **Упростить** | **12** |
| **Удалить** | **11** |
| **Слить** | **4** (из них 3 — в счётчике Упростить/Оставить не входят; ниже отдельно) |
| **Перенести** | **1** (имя файла `lib/session/pg_session_manager.py`) |
| **НЕ РАЗОБРАНО** | **0** |

**Слить (детализация, 4):** `format_tool_params` ↔ `lib/cli/console_loop.py:85`; `_allowed_hook_names` ↔ `lib/services/runtime_inventory.py`; ключ `"_turn_end"` (`postgres_channel.py:1243`) ↔ `lib/utils/outbound_meta.py`; `clean_session_content` ↔ санация в `workspace/utils/session_file_store.py` (не проверено, отмечено для волны слияния).

**Упростить (детализация, 12):** `_resolve_sfs_base` (баг), `PostgresChannel.__init__`, `default_config`, `MessageExchange._poll_loop`, docstring `message_exchange.py:1-12/167-168`, `run_repl` (баг), `ToolAuditHook.__init__`, `before_execute_tools`, docstring `hook_loader.py`, docstring `terminal_tool_print_hook.py:18` (фантомный `print_tools`), `DisplayConfig.show_tool_params` (не проверено), `lib/session/README.md`-уровневая правка имени файла.

**Критичность:** 3 дефекта уровня «сломано в проде» — зависание CLI на выходе, фантомный `gateway.print_tools`, двойной `cache` в пути вложений. Ни один из них не ловится существующими гард-тестами.
