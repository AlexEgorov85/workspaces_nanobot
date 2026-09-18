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
  на `ctx` (`ctx._agent_ref`, `ctx._settings_ref`)
  используется в `patch_project_tools`
  (`runtime_patcher.py:1638-1642`). Это
  симметричный паттерн, **которым мы пользуемся**
  для `ToolContext` (см. D1, `compact_context.py`);
  для `agent`-scoped DI (см. D1) используется
  публичное поле `agent.db_logging_service`,
  без префикса `_`.
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
DI — **только explicit**: `db_logging_service`
передаётся через аргумент конструктора /
патча, без скрытых атрибутов на `agent` и без
lookup'а через `getattr(agent, ...)`.

Контракт:

```python
# RuntimePatcher.apply_all(...) — расширение сигнатуры:
self._record(report, "compact_tracking", self.patch_compaction_tracking(
    agent, settings, db_logging_service=db_logging_service))
self._record(report, "compact_command", self.patch_compact_command(
    agent, settings, db_logging_service=db_logging_service))

# patch_compaction_tracking — расширение:
def patch_compaction_tracking(
    self, agent, settings, *, db_logging_service=None
) -> tuple[bool, str]:
    ...
    svc = ContextCompactionService(
        agent, settings=settings, db_logging_service=db_logging_service,
    )
    ...

# ContextCompactionService — explicit kwarg (НЕ optional с fallback
# на getattr):
def __init__(
    self,
    agent: Any,
    settings: Any = None,
    *,
    db_logging_service: Any,
) -> None:
    self._db_logging_service = db_logging_service
```

`db_logging_service` — обязательный kwarg. `None` —
это **значение**, означающее «сервис недоступен»,
а не «искать где-то ещё». Producer, получивший
`None`, обязан вести себя как «сервис недоступен»
(silent no-op для persistence + loguru-WARNING
через `_missing_db_logging_service_warning`,
см. D6).

Все 5 production call-site'ов
(`compact_command.py`, `console_loop.py`,
`compact_context.py`, `runtime_patcher.py:1879`,
`runtime_patcher.py:1884-1885` через
`patch_compaction_tracking`) **обновляются**:

| call-site | источник `db_logging_service` |
| --- | --- |
| `runtime_patcher.apply_all` | `db_logging_service=ctx.db_logging_service` (уже передаётся) |
| `runtime_patcher.patch_compaction_tracking` | kwarg из `apply_all` |
| `runtime_patcher.patch_compact_command` | kwarg из `apply_all` (если создаёт сервис) |
| `compact_command.py:48` (`cmd_compact`) | через `getattr(ctx.loop, "db_logging_service", None)` |
| `console_loop.py:149` (`_run_cli_compact`) | через `getattr(agent, "db_logging_service", None)` |
| `compact_context.py:128` (tool `create`) | через `getattr(ctx, "_db_logging_service", None)` (`ToolContext`, симметричное `ctx._agent_ref` / `ctx._settings_ref`) |
| Тесты | явная передача `db_logging_service=mock` или `None` |

Для call-site'ов, у которых нет прямого
`ApplicationContext` (`compact_command.py`,
`console_loop.py`), DI идёт через `agent`.
Но **не через скрытый неформальный атрибут**:
добавляем **публичное поле** `agent.db_logging_service`
(не `_db_logging_service`) и
`ApplicationContext._wire_agent_db_logging()`,
который выставляет его на этапе `start()`. Это
не скрытый канал, а **документированный DI-канал**
с явным naming'ом и одной точкой присваивания.

Контракт:

```python
# lib/core/application_context.py — новый метод:
def _wire_agent_db_logging(self) -> None:
    self.agent.db_logging_service = self.db_logging_service

# compact_command.py:
svc = ContextCompactionService(
    ctx.loop, settings=settings,
    db_logging_service=getattr(ctx.loop, "db_logging_service", None),
)

# console_loop.py:
svc = ContextCompactionService(
    agent, settings=None,
    db_logging_service=getattr(agent, "db_logging_service", None),
)

# compact_context.py — через ToolContext:
return cls(service=ContextCompactionService(
    agent, settings=settings,
    db_logging_service=getattr(ctx, "_db_logging_service", None),
))
```

**Альтернативы:**

- **Полностью explicit через ВСЕ call-site'ы
  без `agent.db_logging_service`**: требует
  пробрасывать `db_logging_service` через
  `CommandContext`, `AgentLoop`, `ToolContext`,
  `RunRepl` locals — это глубокие изменения
  в публичных API nanobot 0.3.0, которые по
  AGENTS.md запрещено трогать.
- **Скрытый атрибут `_db_logging_service`**:
  фактически уже было отвергнуто: producer
  начинает знать о неформальном канале,
  контракт неявный, и при тестах/standalone
  легко получить «пустое» значение без понимания,
  почему.
- **Global singleton `get_db_logging_service()`**:
  противоречит composition-root принципу
  (см. `openspec/specs/runtime/context/spec.md`).

**Выбрано:** explicit kwarg во всех сигнатурах
производителей + одно документированное поле
`agent.db_logging_service` (публичное, не
`_db_logging_service`) для call-site'ов, у которых
нет прямого доступа к `ApplicationContext`.
Поле присваивается **в одном месте**
(`ApplicationContext._wire_agent_db_logging`)
и читается через `getattr(agent,
"db_logging_service", None)` — если атрибут
отсутствует (тест создал `agent` напрямую),
возвращается `None` (silent no-op + loguru-WARNING).

Скрытого «через `_db_logging_service`» больше
нет: producer либо получил сервис явно через
kwarg, либо получил `None` явно через
`getattr(..., None)`. Никаких неявных
lookup'ов.

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
- Поведение при `db_logging_service is None` или
  `is_running() == False` — **silent no-op для
  persistence + loguru-warning на уровне `WARNING`**.
  **Не DEBUG** — это диагностически значимое
  событие (production-развёртывание без
  observability), и оператор должен это видеть.

Контракт единый для всех producer'ов
(`ContextCompactionService._record_event_log`,
`PgDuckDbSyncService._log_sync_event`,
`DuckDbCacheStore` caller's,
`PreloadService._emit_health_event`,
`ApplicationContext._record_sync_skipped`-замена):

```python
def _try_log_event(svc, log_event: LogEvent, *, producer: str, event_type: str) -> bool:
    """Единая точка входа producer'а в DbLoggingService.

    Returns:
        True — событие поставлено в очередь;
        False — сервис недоступен/не запущен, событие
        потеряно, зафиксировано в loguru WARNING.
    """
    if svc is None:
        logger.warning(
            "{}: structured event {} not persisted "
            "(DbLoggingService is None)",
            producer, event_type,
        )
        return False
    if not svc.is_running():
        logger.warning(
            "{}: structured event {} not persisted "
            "(DbLoggingService not running)",
            producer, event_type,
        )
        return False
    return bool(svc.log_event(log_event))
```

Уровень `WARNING` — единый для всех producer'ов.
Никаких `DEBUG`, `INFO`, `ERROR`, `EXCEPTION` —
это разброд, который запрещён invariant'ом
D6.

**Альтернативы:**

- Sync `INSERT` через пул напрямую с ленивым импортом:
  возрождает тот же anti-pattern, который мы
  устраняем.
- Async-обёртка через `asyncio.to_thread` над
  `record_event`: легаси-путь, удаляется.
- Per-producer `logger.debug(...)`: создаёт
  разнобой в логах (часть producer'ов пишет
  DEBUG, часть WARNING, часть молчит), затрудняет
  grep/CI-алёрты.

**Выбрано:** единая helper-функция
`_try_log_event(svc, log_event, producer, event_type)`
с фиксированным уровнем WARNING. Располагается
в `lib/services/db_logging_service.py` рядом
с `LogEvent` и публикуется как `db_logging_service.try_log_event(...)` —
producer'ы зовут его единообразно.

Путь симметричен `database_logging_hook` и
`_SubagentLoggingHook`: событие уходит в общую
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

В `_make_sync_services` все ранние return'ы используют
**единый** контракт через
`DbLoggingService.try_log_event(...)` (см. D3) или
`DbLoggingService.log_sync_event(...)` —
с `_try_log_event`-семантикой (silent no-op для
persistence при недоступности + loguru-WARNING).
**Никакого «или loguru-warning напрямую»**: либо
запись через `DbLoggingService`, либо ничего
в `agent_gateway_logs`.

Конкретно:

```python
# Было:
def _record_sync_skipped(event_type, reason, detail):
    try:
        from workspace.utils.event_log import record_sync_event
        record_sync_event(...)
    except Exception:
        pass

# Стало (в _make_sync_services):
if not ctx.db_logging_service:
    logger.warning("PgDuckDbSyncService skipped: {}", reason)
else:
    ctx.db_logging_service.log_sync_event(
        event_type=event_type,
        summary=f"PgDuckDbSyncService skipped: {reason}",
        payload={"reason": reason, "detail": detail},
        level="WARN",
    )
```

Если `db_logging_service` сконфигурирован — запись
через него (event timeline).
Если нет — только loguru-WARNING в терминал.
Helper `_record_sync_skipped` **удаляется целиком**.

**Альтернативы:**

- Сохранить `_record_sync_skipped` и переписать
  его на `db_logging_service.log_error(...)`:
  плодит точку входа для одного edge-case'а.
- Ранний return без `loguru-WARNING` (полностью
  silent): теряем diagnosability для оператора.

**Выбрано:** helper удаляется; `_make_sync_services`
использует **либо** `db_logging_service.log_sync_event(...)`
**либо** `logger.warning(...)` в одном из двух
местах (не «или» в одном выражении). Это устраняет
архитектурное «или» из старого tasks §4.4.

### D6. Architecture guard: ownership-based + разделение production/import

`tests/test_unified_event_logging_pipeline.py`
содержит **два** независимых guard-класса:

#### D6.1. `TestNoProductionDirectWriters` (production-allowlist)

Обход production runtime-путей:
`lib/**/*.py`, `workspace/**/*.py`,
`tools/*.py`, `cli_agent.py`, `gateway.py`,
`streamlit_app.py`. **Исключаются** `tests/`
(тесты могут содержать SQL fixture).

Allowlist (единственные файлы, где легитимны
INSERT в logging-DB таблицы):

```python
LOGGING_OWNERS = {
    "lib/services/db_logging_service.py",  # единственный owner
}
```

Guard проверяет:

1. **Ownership INSERT**: ни один production-файл
   вне `LOGGING_OWNERS` не должен содержать
   `INSERT` в `agent_gateway_logs` /
   `agent_question_runs`. Detection — AST-парсинг
   `ast.Call(func=ast.Attribute(attr='execute'),
   args=[ast.Constant(value=sql), ...])` где
   `sql` (после f-string evaluation через
   `ast.literal_eval` или как литерал) содержит
   имя logging-таблицы. Либо regex-fallback для
   случаев с `f"INSERT INTO ... {table_name}"`,
   где `table_name` берётся из
   `settings.logging.db.table_name` — этот
   случай отдельно проверяется unit-тестом
   на «dynamic-table INSERT» (D6.4).
2. **Ownership table name access**: ни один
   production-файл вне `LOGGING_OWNERS` не должен
   читать `logging.db.table_name` /
   `logging.db.schema` для целей INSERT.
   Detection — AST/grep по
   `["logging"]["db"]["table_name"]`,
   `.logging.db.table_name`,
   `get_setting(..., "logging", "db", "table_name", ...)`
   с проверкой контекста использования.

#### D6.2. `TestNoDeletedModuleImports` (global guard)

Обход **всего** Python-кода включая `tests/`,
`tools/`, `lib/`, `workspace/`, application
entrypoints:

1. **Запрещённый импорт**:
   `from workspace.utils.event_log`,
   `import workspace.utils.event_log` —
   `ast`-парсинг `ast.Import` /
   `ast.ImportFrom` с `module='workspace.utils.event_log'`.
2. **Запрещённые вызовы функций**:
   `record_event(...)`, `record_sync_event(...)`,
   `emit_sync_event(...)` как `ast.Call(func=ast.Name(...))`
   в **любом** Python-файле репозитория, кроме
   `docs/`/`.md` (в docstring'ах `.py` AST
   игнорирует `Expr(value=Constant(...))` — это
   безопасно).

Оба guard-класса параметризованы по путям
→ failure message указывает конкретный файл,
строку и нарушенное правило.

#### D6.3. Negative tests (in-tree fixtures)

Отдельные negative-тесты с `tmp_path`-фикстурами:
создать `.py`-файл с запрещённым импортом /
INSERT — guard падает; удалить — guard зелёный.
Это unit-тесты самого guard'а (страховка от
регрессии в самом AST/regex), а **не** основной
acceptance — основной acceptance это D6.1 и D6.2
на реальном репозитории.

#### D6.4. Coverage

- `git grep -n 'INSERT INTO .* agent_gateway_logs' -- '*.py'` —
  только `lib/services/db_logging_service.py`
  (после выполнения change);
- `git grep -nE '\b(record_event|record_sync_event|emit_sync_event)\(' -- '*.py'` —
  пусто;
- `git grep -n 'from workspace.utils.event_log\|import workspace.utils.event_log' -- '*.py'` —
  пусто;
- `git grep -n 'logging\.db\.table_name\|logging\["db"\]\["table_name"\]' -- '*.py'`
  вне `lib/services/db_logging_service.py` —
  пусто.

Все 4 проверки прогоняются в
`tests/test_unified_event_logging_pipeline.py`
как `TestRepositoryGrepBaseline` (CI-проверка
на regression после merge).

**Альтернативы:**

- Только regex по `agent_gateway_logs`:
  ложные срабатывания на docstring'ах
  и пропускает `f"INSERT INTO ... {table}"`,
  где `table` берётся из настроек.
- Allowlist с размытым критерием «модуль
  не использует psycopg2 для записи»:
  не-питон-френдли, требует ручного аудита.
- `pytest-regex` с YAML-списком expected matches:
  overhead на поддержку списка, не адаптируется
  к rename'ам.

**Выбрано:** AST + ownership-allowlist для
production (точное определение «кто может
писать»), AST для global import guard
(включая тесты). Разделение **обязательно** —
production и тесты смешивать нельзя: тесты
легитимно могут содержать SQL fixture для
проверки поведения.

### D7. Lifecycle ordering: `db_logging_service.start()` ДО любых emitter'ов

`ApplicationContext.start()` уже упорядочен
по шагам (см. `application_context.py:267-346`).
Шаг `db_logging_service.start()` происходит
**до** `RuntimePatcher.apply_all` (патчеру
передаётся уже запущенный сервис через
`db_logging_service=ctx.db_logging_service`) —
это гарантирует, что к моменту, когда
`patch_compaction_tracking` создаёт
`ContextCompactionService` с этим `db_logging_service`,
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

### D8. `patch_compaction_tracking` остаётся активным при `notify_in_history=false`

Текущее поведение
(`runtime_patcher.py:1882-1883`):

```python
if not svc.notify_in_history:
    return False, "gateway.compact.notify_in_history=false"
```

— патч целиком отключается, если
`notify_in_history=false`. Это значит, что
**auto-compaction** (idle-сжатие +
token-budget сжатие через `_wrap_auto_compact_archive`
и `_wrap_maybe_consolidate_by_tokens`) не пишет
`context_compacted` в `agent_gateway_logs`
в этом режиме.

После change патч должен оставаться активным
при `gateway.compact.enabled=true`, **включая**
`notify_in_history=false`. Что меняется:

```python
def patch_compaction_tracking(
    self, agent, settings, *, db_logging_service=None
) -> tuple[bool, str]:
    if agent is None:
        return False, "agent is None"
    try:
        from lib.services.context_compaction import ContextCompactionService
    except Exception as exc:
        return False, f"import failed: {exc}"
    try:
        svc = ContextCompactionService(
            agent, settings=settings,
            db_logging_service=db_logging_service,
        )
        if not svc.enabled:
            return False, "gateway.compact.enabled=false"
        # УДАЛЕНО: ранний return при notify_in_history=false
        # Теперь patch остаётся активным; разделение concerns
        # делает _notify внутри record_external_compaction.
        self._wrap_auto_compact_archive(agent, svc)
        self._wrap_maybe_consolidate_by_tokens(agent, svc)
    except Exception as exc:
        return False, f"patch failed: {exc}"
    return True, "auto compaction tracking patched"
```

Эффект:

- `gateway.compact.enabled=false` →
  patch disabled (как раньше);
- `gateway.compact.enabled=true`,
  `notify_in_history=true` →
  patch active, `_record_event_log` + `_write_history_notice`
  оба работают (как раньше);
- `gateway.compact.enabled=true`,
  `notify_in_history=false` →
  patch **active** (раньше — disabled);
  `_record_event_log` работает
  (structured event в `agent_gateway_logs`),
  `_write_history_notice` — нет.

**Альтернативы:**

- Сохранить ранний return и писать structured
  event **в обход** auto-compaction path
  (например, добавить второй hook):
  дублирование кода, расхождение с ручным
  `compact()`.
- Сделать patch активным, но завернуть
  `record_external_compaction` в условие
  `if svc.notify_in_history:`: ровно то, что
  мы убираем в `record_external_compaction`
  (D2), поэтому не подходит.

**Выбрано:** patch активен при
`enabled=true` независимо от `notify_in_history`;
решение о UI-стороне принимается внутри
`_notify` (D2).

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
