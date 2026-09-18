# Design — unify-agent-event-logging-pipeline

## Context

См. `proposal.md` (раздел Why). Текущее состояние
кода без правок:

- `workspace/utils/event_log.py:30-98` — `record_event()`
  с прямым `INSERT INTO "<schema>"."<table>"` через
  `utils.db.execute` и собственным чтением
  `SETTINGS["logging"]["db"]` /
  `SETTINGS["channels"]["postgres"]["dsn"]`.
- `workspace/utils/event_log.py:105-142` —
  `record_sync_event()` как обёртка над `record_event`.
- `workspace/utils/event_log.py:145-197` —
  `emit_sync_event()` с dual-sink: при переданном
  и запущенном `service` идёт через
  `service.log_sync_event(...)`, иначе **fallback**
  в `record_sync_event()` (прямой INSERT). Это
  «скрытый второй механизм» — то, что надо устранить.
- `lib/services/context_compaction.py:316-360` —
  `ContextCompactionService._record_event_log` импортирует
  `workspace.utils.event_log.record_event` и зовёт его
  через `asyncio.to_thread`, минуя `DbLoggingService`.
  Гейт — `self.notify_in_history` (смешение concerns).
- `lib/services/context_compaction.py:312-314` —
  блок `if self.notify_in_history:` гасит **оба**
  эффекта (`_write_history_notice` + `_record_event_log`),
  что и есть decoupling-баг.
- `lib/services/pg_duckdb_sync_service.py:155-197` —
  `_log_sync_event` делегирует в `emit_sync_event`
  (dual-sink → fallback).
- `lib/services/duckdb_cache_store.py:46-74` —
  `_emit_sync_event` (внутренняя обёртка над
  `workspace.utils.event_log.emit_sync_event`).
- `lib/services/preload_service.py:36-61` —
  `_emit_health_event` (dual-sink).
- `lib/core/application_context.py:950-967` —
  `_record_sync_skipped` напрямую зовёт
  `record_sync_event` (используется в `_make_sync_services`
  при ранних return'ах до `db_logging_service.start()`).
- `lib/channels/postgres_channel.py:600-636` —
  `_journal_event`: **good pattern** — проверяет
  `svc.is_running()` и в случае `False` тихо выходит,
  без fallback. Эталон для остальных producers.
- `lib/services/db_logging_service.py:107-993` —
  канонический сервис с async-очередью,
  worker-потоком, `LogEvent`-API,
  специализированными `log_inbound` / `log_outbound`
  / `log_tool_call` / `log_tool_result` / `log_llm_call`
  / `log_error` / `log_sync_event`, всеми метриками
  и retention-логикой.
- `tests/test_event_log.py` — тестирует прямой INSERT
  bypass'а; целиком удаляется.
- `tests/test_context_compaction.py:837-854` —
  `test_notify_skips_event_log_when_notify_disabled`
  фиксирует текущее (неправильное) поведение: при
  `notify_in_history=false` `_record_event_log` не
  вызывается. Этот тест переписывается.

Ограничения:

- Python ≥ 3.14, `from __future__ import annotations` обязателен.
- `nanobot 0.3.0` API — менять нельзя, только читать.
- Существующие конструкторы
  `ContextCompactionService(agent, settings)` вызываются
  в 5 местах (`lib/commands/compact_command.py:48`,
  `lib/cli/console_loop.py:149`,
  `workspace/tools/compact_context.py:128`,
  `lib/services/runtime_patcher.py:1879`,
  плюс тесты). Все они должны продолжать работать
  с тем же positional/kwarg интерфейсом.
- Существующий DI-паттерн через атрибуты
  (`ctx._agent_ref`, `ctx._settings_ref`,
  `ctx._db_logging_service`) уже используется в
  `patch_subagent_logging` (`runtime_patcher.py:1399-1423`).
- Профили конфигурации (`--profile`) не меняются —
  logging-конфиг и так не входит в profile-owned
  runtime-ключи (см. AGENTS.md).
- `DbLoggingService` остаётся единственным местом
  с правом INSERT в `agent_gateway_logs` —
  никакого слоя «обёртка вокруг `DbLoggingService`».

## Goals / Non-Goals

**Goals:**

- Устранить второй runtime-механизм записи в
  `agent_gateway_logs` (`workspace.utils.event_log`):
  прямой INSERT, dual-sink fallback, sync-bypass.
- Зафиксировать единственный путь:
  `producer → db_logging_service.log_event(LogEvent(...)) → queue → worker → INSERT`.
- Развязать concerns в
  `ContextCompactionService._notify`:
  `_write_history_notice` остаётся под
  `notify_in_history`; `_record_event_log` —
  всегда при `enabled=True`.
- Сохранить public-API минимум:
  специализированные builder-методы на
  `DbLoggingService` остаются как удобные шорткаты,
  но внутри все они строят `LogEvent` и зовут
  `log_event`. Никаких новых публичных методов.
- Сохранить backward compatibility существующих
  сигнатур конструктора
  `ContextCompactionService(agent, settings=None)`
  (новый аргумент — опциональный kwarg с дефолтом
  `None`).
- Внедрить architecture guard — тест, который
  не даст вернуть прямой INSERT через пару
  месяцев после merge.

**Non-Goals:**

- Менять схему таблицы `agent_gateway_logs` или
  `agent_question_runs` (JSONB остаётся JSONB,
  миграций нет).
- Менять batching-модель `DbLoggingService`
  (worker-поток остаётся, sync-INSERT'ы не вводятся).
- Менять полезную нагрузку `context_compacted`
  payload (snapshot остаётся — изменение контракта
  payload отдельный follow-up change).
- Добавлять новые публичные методы
  `DbLoggingService` (`log_compacted`, `log_sync_*`
  специализированные); существующее API достаточно.
- Вводить OpenTelemetry, JSONL-fallback, новый
  event-bus, ретрай-механизмы, метрики retention,
  изменения `request_id` model.
- Делать из `DbLoggingService` «универсальную шину
  всего приложения» — его область строго
  persistent structured agent logging.
  Обычный operational `logger`/`loguru` остаётся
  отдельным concern.

## Decisions

### D1. `db_logging_service` пробрасывается через атрибут на `agent`

`RuntimePatcher.apply_all` уже принимает
`db_logging_service` и передаёт его в
`patch_subagent_logging` (`runtime_patcher.py:1399-1423`).
Расширяем `apply_all` так, чтобы после успешного
вызова он **выставлял** `agent._db_logging_service`
(только если `db_logging_service is not None` —
не затираем уже выставленное значение).

`ContextCompactionService.__init__(agent, settings=None, *, db_logging_service=None)`
с `None`-дефолтом; внутри:

```python
self._db_logging_service = (
    db_logging_service
    or getattr(agent, "_db_logging_service", None)
)
```

Это даёт два пути:

- **explicit** (тесты, fine-grained контроль):
  `ContextCompactionService(agent, settings, db_logging_service=fake)`;
- **implicit** (через runtime_patcher):
  `ContextCompactionService(agent, settings=None)` —
  сервис берётся с `agent._db_logging_service`.

Все 5 существующих call-site'ов (`compact_command.py`,
`console_loop.py`, `compact_context.py`,
`runtime_patcher.py:1879`, тесты) остаются без
изменений сигнатуры — db_logging_service
резолвится автоматически при работе под
`ApplicationContext`, а в тестах/CLI без него
остаётся `None` (silent no-op).

**Альтернативы:**

- Передавать `db_logging_service` явно через
  ВСЕ 5 call-site'ов: больше boilerplate, риск
  забыть в одном из мест (особенно в `compact_command.py`,
  где `ctx` — это `CommandContext`, не
  `ApplicationContext`).
- Заводить global singleton `get_db_logging_service()`:
  противоречит composition-root принципу.

**Выбрано:** implicit lookup через `agent._db_logging_service`
плюс optional explicit override. Соответствует
существующему паттерну `_agent_ref` / `_settings_ref`.

### D2. `_notify` разделяет concerns через два независимых условия

В `ContextCompactionService._notify`:

```python
# всегда при archived > 0 — независимый structured event
await self._record_event_log(session_key, report, text)

# UI-уведомление — под notify_in_history
if self.notify_in_history:
    await self._write_history_notice(session_key, report)

# terminal output — под print_to_terminal
if self.print_to_terminal:
    Console().print(...)
```

Порядок: structured event → history notice → terminal.
`enabled=False` обрабатывается в `compact()` раньше
(`if not self.enabled: return self._empty(...)`),
в `_notify` мы попадаем только если `enabled=True`.

`record_external_compaction(...)` тоже наследует это
поведение: он зовёт `_notify`, который сам решает,
что делать с `notify_in_history`. Раньше в
`record_external_compaction` была ранняя проверка
`if not self.notify_in_history: return` —
она **удаляется**, иначе `_record_event_log` всё равно
не вызывался бы.

**Альтернативы:**

- Ввести два независимых флага в
  `gateway.compact.*` (`notify_in_history` и
  `notify_in_db_log`): шире scope, чем нужно; пользователь
  должен иметь возможность «выключить UI-уведомления,
  но сохранить observability».
- Ничего не менять: продолжаем гасить и UI, и observability
  одним флагом — наружу это выглядит как «конфиг сломал
  history_search».

**Выбрано:** structured event logging **всегда** при
`enabled=True`; `notify_in_history` управляет только
UI-стороной. Это закрывает **gap №1** из
`docs/architecture/HISTORY_SEARCH_ANALYSIS.md` —
`history_search` теперь всегда находит `context_compacted`,
независимо от `notify_in_history`.

### D3. `_record_event_log` через `db_logging_service.log_event(LogEvent(...))`

Заменяем `asyncio.to_thread(record_event, ...)` на
`db_logging_service.log_event(LogEvent(...))`:

- `LogEvent(event_type="context_compacted", level="INFO",
  session_id=session_key, channel="system",
  actor="system", name="consolidator",
  summary=text[:200] if text else "context compacted",
  payload={...})` — payload без изменений.
- `db_logging_service is None` или
  `is_running() == False` →
  silent no-op + `logger.debug("context_compacted
  persistence unavailable for {}", session_key)`.

**Альтернативы:**

- Sync `INSERT` через пул напрямую с ленивым импортом:
  возрождает тот же anti-pattern, который мы
  устраняем.
- Async-обёртка через `asyncio.to_thread` над
  `record_event`: легаси-путь, удаляется.

**Выбрано:** через `DbLoggingService.log_event` —
путь симметричен `database_logging_hook` и
`_SubagentLoggingHook`. Событие уходит в общую
очередь, метрики `written_by_type` инкрементируются.

### D4. `emit_sync_event` и `record_sync_event` удаляются без deprecated-обёрток

`workspace/utils/event_log.py` удаляется целиком.
Все 4 call-site'а (`pg_duckdb_sync_service`,
`duckdb_cache_store`, `preload_service`,
`application_context._record_sync_skipped`)
переписываются на **прямой вызов**
`db_logging_service.log_sync_event(...)`
(или `log_event(LogEvent(...))` если нужен нестандартный
payload). Если `db_logging_service is None` или
`is_running() == False` — silent no-op по образцу
`postgres_channel._journal_event:618-621`.

**Альтернативы:**

- Сохранить `emit_sync_event` как deprecated-обёртку,
  которая печатает `DeprecationWarning`:
  маскирует старый механизм под новый, ровно то,
  от чего мы избавляемся.

**Выбрано:** полное удаление. После архитектурного
refactor не должно быть скрытого legacy.

### D5. `ApplicationContext._record_sync_skipped` исчезает

Функция `_record_sync_skipped(event_type, reason, detail)`
использовалась в `_make_sync_services` при ранних
return'ах с тихими причинами отказа (до
`db_logging_service.start()`). После change:

- В `_make_sync_services` все ранние return'ы
  используют `logger.warning` с structured-полями
  (`extra={...}` или форматированная строка) —
  этого достаточно для diagnosability.
- Если `ctx.db_logging_service` уже сконфигурирован
  (как в production-сценарии), вызывающий код
  может опционально вызвать
  `ctx.db_logging_service.log_error(...)` для
  observability — но **без `_record_sync_skipped`
  helper'а** (прямой вызов).

**Альтернативы:**

- Сохранить `_record_sync_skipped` и переписать
  его на `db_logging_service.log_error(...)`:
  плодит точку входа для одного edge-case'а.

**Выбрано:** helper удаляется. Каждый ранний return
в `_make_sync_services` либо логирует loguru-warning,
либо зовёт `ctx.db_logging_service.log_error(...)`
напрямую (если observability важна для оператора).

### D6. Architecture guard через AST-обход + regex-precise проверки

`tests/test_unified_event_logging_pipeline.py`
содержит класс `TestNoDirectWriters` с параметризованным
тестом, который обходит ВСЕ `.py`-файлы в
`lib/`, `workspace/` (кроме `workspace/utils/event_log.py`,
которого больше нет), `tools/`, `cli_agent.py`,
`gateway.py`, `streamlit_app.py`. Для каждого файла:

1. **Прямой INSERT в `agent_gateway_logs`** —
   regex
   `r'INSERT\s+INTO\s+["\']?(?:[a-zA-Z_][\w]*\.)?["\']?["\']?agent_gateway_logs["\']?'`
   (case-insensitive, multiline). Исключения:
   `lib/services/db_logging_service.py` (единственное
   место, где INSERT в эту таблицу легитимен).
2. **Импорт `workspace.utils.event_log`** — regex
   `r'from\s+workspace\.utils\.event_log\b'`
   или `r'import\s+workspace\.utils\.event_log\b'`.
   Это ошибка в любом файле (модуль удалён).
3. **Вызовы `record_event(`, `record_sync_event(`,
   `emit_sync_event(`** как вызовы функций —
   `ast`-парсинг + `ast.walk` для `ast.Call`,
   проверка `func.id in {"record_event",
   "record_sync_event", "emit_sync_event"}`.
   Исключение: docstring/test-fixture случаи —
   для этого `ast` подходит лучше regex'а (не
   ловит просто упоминания в комментариях).

Guard запускается в `pytest tests/test_unified_event_logging_pipeline.py`
(или общем `pytest tests/`). Тест параметризован
по файлам → failure message указывает конкретный
файл и правило.

**Альтернативы:**

- Только regex: ложные срабатывания на docstring'ах
  и комментариях (например, история change в
  `CHANGELOG.md`-цитатах в Python-файлах).
- Только AST: пропускает f-string'и с динамической
  подстановкой имени таблицы (но в проекте таких
  нет — `agent_gateway_logs` всегда литерал,
  проверяется grep'ом).
- `pytest-regex`-style test с хранением expected
  matches в YAML: overhead на поддержку списка.

**Выбрано:** AST для вызовов функций (точность) +
regex для INSERT-литералов (быстро, нет ложных
срабатываний на строках в f-string). Docstring'и
в docstring'ах `ContextCompactionService` и
`DbLoggingService` упоминают `record_event` /
`record_sync_event` / `INSERT INTO` в режиме
«до этого change», что **не должно** триггерить
guard — AST-парсинг корректно их игнорирует
(вызовы функций как `func.id` ищутся только в
телах функций и module-level, не в `Expr(value=Constant(...))`
узлах docstring'ов).

### D7. Lifecycle ordering: `db_logging_service.start()` ДО любых emitter'ов

`ApplicationContext.start()` уже упорядочен
по шагам (см. `application_context.py:267-346`).
Шаг `db_logging_service.start()` происходит
**до** `RuntimePatcher.apply_all` (патчеру
передаётся уже запущенный сервис) — это
гарантирует, что когда `patch_compaction_tracking`
создаёт `ContextCompactionService` и тот
резолвит `agent._db_logging_service`,
сервис уже запущен.

Verify: `application_context.py` `_make_db_logging_service`
вызывается до `_make_runtime_patcher` /
`apply_all` — нужно сверить при реализации,
что порядок не изменился.

**Изменения не требуются** — порядок уже корректен;
мы только фиксируем его в спеке как invariant
(чтобы будущие правки не сдвинули шаги).

**Альтернативы:**

- Делать `db_logging_service.start()` после
  `apply_all`: ломает subagent logging patch.

**Выбрано:** сохранить существующий порядок
и явно задокументировать.

### D8. `compact_command.py` — fallback к `agent._db_logging_service`

Команда `cmd_compact(ctx, settings=None)` имеет
доступ к `ctx.loop` (= `agent`), но не имеет
явного `db_logging_service`. Полагаемся на
D1: `ContextCompactionService(ctx.loop, settings)`
внутри резолвит `agent._db_logging_service`
через `getattr`. Никаких изменений в
`compact_command.py` не нужно.

**Альтернативы:**

- Добавить `db_logging_service` параметр в
  `RuntimePatcher.patch_compact_command`:
  патч уже получает `settings`, но не получает
  `db_logging_service`. Расширение сигнатуры
  нужно только если D1 не сработает (т.е.
  если атрибут на `agent` не выставлен).

**Выбрано:** проверить в реализации, что
`runtime_patcher.apply_all` действительно
выставляет `agent._db_logging_service` ДО
вызова `patch_compact_command`. Если нет —
расширить сигнатуру (это D1-fallback).

## Risks / Trade-offs

- **R1**: AST-парсинг всех `.py`-файлов при каждом
  `pytest`-запуске может добавить ~0.5–1 сек.
  → **Mitigation:** guard обходит только фиксированный
  список путей, не всю файловую систему; для проекта
  размера ~100 `.py`-файлов парсинг — десятки мс.

- **R2**: Изменение поведения
  `notify_in_history=false` → observability-trail
  в `agent_gateway_logs` **включается** (раньше
  был выключен). Это **заметное** изменение для
  существующих deployment'ов с выключенным
  UI-уведомлением.
  → **Mitigation:** changelog explicit, tests на
  scenario «`notify_in_history=false` →
  `context_compacted` всё равно записан».
  Это и есть закрытие **gap №1**.

- **R3**: Standalone-утилиты
  (`tools/build_vectors.py` и т.п.) больше не
  пишут structured events через прямой
  INSERT — наблюдаемость этих утилит в
  `agent_gateway_logs` теряется.
  → **Mitigation:** утилиты пишут в loguru
  (`logger.warning` / `logger.info` с extra-полями);
  для продакшн-deployment'ов утилита запускается
  через `gateway.py` с поднятым
  `DbLoggingService`. Документировано в
  `docs/ARCHITECTURE.md`.

- **R4**: Auto-тесты существующих патчей
  (`test_subagent_logging.py`,
  `test_hooks_database_logging.py`) уже
  проверяют, что `_SubagentLoggingHook`
  / `DatabaseLoggingHook` правильно эмитят
  события. После change `_record_event_log`
  идёт через `DbLoggingService.log_event` —
  форма LogEvent та же, payload тот же,
  тесты должны проходить без правок.
  → **Mitigation:** прогнать test suite как
  часть `tasks.md §5` (regression check).

- **R5**: `tests/test_event_log.py` удаляется
  целиком. Этот тест покрывал:
  - `record_event` с правильными параметрами;
  - skip при `logging.db.enabled=false`;
  - skip при отсутствии DSN;
  - truncation summary до 200 символов.
  После удаления **нет** тестов на
  direct INSERT bypass'а (его не должно
  существовать). Если кто-то вернёт его —
  architecture guard его поймает (D6).
  → **Mitigation:** architecture guard + явная
  пометка в CHANGELOG.

- **R6**: В `runtime_patcher.apply_all` —
  расширение сигнатуры `patch_compaction_tracking`
  и `patch_compact_command` параметром
  `db_logging_service` для случая, когда D1-fallback
  нужен. Это влияет на контракт patch-методов.
  → **Mitigation:** параметр опциональный (kwarg
  с `None`-дефолтом); старые тесты продолжают
  работать; новые тесты могут передавать явно.

## Migration Plan

Применение:

1. Merge change через стандартный PR-флоу
   (одна фича-ветка, без feature-флагов —
   поведение `notify_in_history=false` —
   единственное breaking в плане observability,
   и это закрытие документированного gap).
2. Существующие `project.json` / `config.json`
   работают без правок — настройки
   `logging.db.*` не изменились.
3. Полный прогон `pytest tests/`:
   целевой результат — `1480+ passed, ~22 skipped`
   (baseline из `CHANGELOG.md`); `test_event_log.py`
   удаляется, новые тесты добавляются.

Откат:

- Revert одного PR восстанавливает
  `workspace/utils/event_log.py`,
  `ContextCompactionService._record_event_log`,
  dual-sink fallback, прямую зависимость от
  `notify_in_history` в `_record_event_log`.
  Дополнительных миграций данных нет.

## Open Questions

- **Q1**: Стоит ли в будущем ввести явный флаг
  `gateway.compact.log_to_db` (отдельно от
  `notify_in_history`)? Сейчас — нет: решает
  D2 (structured event всегда при `enabled=True`).
  Если операторы потребуют «жёстко выключить даже
  observability для context compaction» —
  отдельный follow-up change с новой опцией.

- **Q2**: Не следует ли вынести
  `_emit_health_event` из `PreloadService` в
  отдельный мини-helper `agent_gateway_logs.emit(...)`,
  который принимает только payload? Сейчас —
  нет: helper-функция в `PreloadService` —
  один caller, нет смысла выносить. Если
  число callers вырастет — отдельный refactor.

- **Q3**: Не нужно ли переписать
  `lib/services/db_logging_service.py:107-993`
  на pydantic-модели вместо dataclass? Нет —
  вне scope этого change; `LogEvent` остаётся
  dataclass до тех пор, пока не появится
  отдельная задача «миграция на pydantic v2».
