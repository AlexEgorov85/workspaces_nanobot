# Design — improve history_search pagination and logging observability

## Context

См. `proposal.md` (раздел Why) — пять связанных проблем
с пагинацией, семантикой truncation и наблюдаемостью записи
событий `agent_gateway_logs`, проявляющиеся при использовании
`history_search` для recovery после `context_compacted`.

Текущее состояние кода (без правок):

- `workspace/tools/history_search_tool.py:50-139` — параметры
  tool (нет `offset`, нет `has_more`); `:325-362` —
  truncation-логика с одним булевым `truncated`.
- `workspace/tools/history_search_tool.py:283-289` — SQL
  `ORDER BY "timestamp" DESC LIMIT %s` без tie-breaker'а.
- `lib/services/db_logging_service.py:117` — хардкод
  `flush_interval_sec = 5.0` в конструкторе.
- `lib/services/db_logging_service.py:209-212` — `log_event`
  кладёт в `queue.Queue` без диагностики по типам.
- `lib/services/db_logging_service.py:548-606` — worker-loop
  flush'ит батч по `flush_interval_sec` или `batch_size`.
- `lib/hooks/database_logging_hook.py:402-419` —
  `DatabaseLoggingHook.after_run` пишет `run_finished`.
- `lib/services/runtime_patcher.py:1370-1398` — subagent
  logging patch пишет `subagent_run_finished`.

Ограничения:

- Python ≥ 3.14, `from __future__ import annotations` обязателен.
- `nanobot 0.3.0` API (`tool_parameters`, `ctx._settings_ref`,
  `current_request_session_key`) — менять нельзя, только читать.
- БД — PostgreSQL/Greenplum 6.5, без `make_interval`, без
  `ON CONFLICT`. Все SQL — `%s`-параметризованные.
- Профиль конфигурации (`--profile`) — `logging.db.flush_interval_sec`
  не входит в profile-owned runtime-ключи (см. AGENTS.md),
  наследуется из `project.json` или задаётся явно в `config.json`.
- `ConfigurationResolver` — единственный путь от raw JSON к
  `SETTINGS`; `DbLoggingService` НЕ читает конфиг напрямую.

## Goals / Non-Goals

**Goals:**

- Дать агенту детерминированную пагинацию с честным признаком
  наличия следующей страницы (`has_more`).
- Чётко разделить два разных truncation-механизма
  (`results_truncated` для выборки, `payload_truncated` для
  payload'а конкретного события).
- Дать наблюдаемость «пишется ли `run_finished` вообще» через
  счётчики `written_by_type` и `oldest_queued_age_sec` в
  `get_stats()`.
- Вынести `flush_interval_sec` в конфигурацию через
  стандартный resolver chain, без хардкода.

**Non-Goals:**

- Менять схему таблицы `agent_gateway_logs` (JSONB остаётся
  JSONB, миграций нет).
- Менять batching-модель `DbLoggingService` (worker-поток
  остаётся, синхронные INSERT'ы не вводятся).
- Переписывать payload-сериализацию для разных `event_type`
  в единую Pydantic-схему.
- Изменять поведение `runtime/context` (отдельная тема,
  не блокирует этот change).
- Вводить cursor-пагинацию — для первой версии достаточно
  `offset` + `has_more`. Cursor — отдельный change, если
  потребуется.

## Decisions

### D1. `has_more` через `LIMIT N+1` с поправкой на `results_truncated`

SQL запрашивает `effective_limit + 1` строк. Если фактически
получено `> effective_limit`, лишняя строка отбрасывается и
`db_has_more = true`. Финальное значение вычисляется **после**
truncation-проходов:

```python
db_has_more = (len(rows_from_db) > effective_limit)
if db_has_more:
    rows_from_db = rows_from_db[:effective_limit]

# после всех truncation-проходов
results_truncated = ...
has_more = db_has_more or results_truncated
```

Это важно: `db_has_more` сам по себе **недостаточен**.
Если в БД ровно `effective_limit` подходящих событий
(`db_has_more = false`), но `max_result_chars` выбросил
часть из них (`results_truncated = true`), агент ещё
не видел эти отброшенные события и следующая страница
обязательна. Возврат `has_more = false` при
`results_truncated = true` означал бы потерю событий
при recovery.

Дешевле, чем отдельный `COUNT(*)`, и не требует изменения
API БД.

Альтернативы:

- `COUNT(*) OVER ()` — добавляет оконную функцию к
  основному запросу; на Greenplum 6.5 поддерживается,
  но лишний overhead.
- `EXISTS`-подзапрос — два запроса вместо одного.
- Cursor по `id` последней строки — стабильнее при
  активных INSERT'ах, но усложняет контракт.

Выбрано: `LIMIT N+1` + композитная формула `has_more`.
Для типичных объёмов (десятки-сотни событий на сессию)
между страницами обычно 0 новых INSERT'ов, и `has_more=true`
корректно указывает «есть ещё подходящие». Если в будущем
нужна строгая консистентность — cursor добавляется
отдельным change.

### D2. Детерминированная сортировка `timestamp DESC, id DESC`

SQL: `ORDER BY "timestamp" DESC, "id" DESC LIMIT %s OFFSET %s`,
где `id` — UUID `agent_gateway_logs.id`. Без tie-breaker'а
порядок строк с равным `timestamp` НЕ гарантирован между
запусками одного и того же запроса, что приводит к
повторам/пропускам на границе страниц. UUID как
вторичный ключ детерминирует порядок.

Альтернативы:

- `ORDER BY "timestamp" DESC, id ASC` — выбор между ASC и
  DESC не имеет значения для корректности, главное — стабильность.
- Сортировка только по `id DESC` — теряет временной порядок.

Выбрано: `timestamp DESC, id DESC` (от новых к старым,
среди равных — UUID).

### D3. Раздельные truncation-флаги без немедленного удаления `truncated`

JSON-ответ tool'а расширяется полями `results_truncated`
(на ответе) и `payload_truncated` (на каждом событии).
Старое поле `truncated` сохраняется как deprecated
алиас `results_truncated` в течение одного MINOR-релиза.
Удаление `truncated` — отдельный follow-up change с
собственным proposal/design/tasks. Текущий change
**не** фиксирует дату/релиз удаления, чтобы не делать
ложных обещаний.

Альтернативы:

- Удалить `truncated` сразу — ломает существующих агентов,
  читающих это поле.
- Сохранять `truncated` вечно — не стимулирует миграцию
  агентов на новый контракт.

Выбрано: deprecation-окно в один MINOR-релиз. Удаление —
follow-up change.

### D4. `flush_interval_sec` только через SETTINGS

`LoggingDbSettings.flush_interval_sec` валидируется Pydantic
(`0.5 ≤ value ≤ 60.0`, дефолт `5.0`). Значение проходит
через `ConfigurationResolver` → `ProjectSettings` →
`ApplicationContext` → `DbLoggingService.__init__`.

`DbLoggingService` НЕ читает `config.json`/`project.json`
напрямую — только принимает `flush_interval_sec` параметром
конструктора. Это исключает ситуацию вида «Pydantic поле
добавили → конфиг его принимает → ApplicationContext не
передал → сервис продолжает 5.0».

Runtime-тест фиксирует цепочку:

```python
settings = ...
ctx = ApplicationContext.create(settings, ...)
assert ctx.db_logging_service._flush_interval == 2.0
```

Альтернативы:

- ENV (`LOGGING_DB_FLUSH_INTERVAL_SEC`) — менее явно,
  расходится с конвенцией остальных настроек.
- Прямое чтение `config.json` в `DbLoggingService` —
  нарушает архитектурный invariant «SETTINGS — единственный
  resolved source».

Выбрано: SETTINGS через resolver chain.

### D5. `written_by_type` инкрементируется только после успешного INSERT

В `_flush_batch` после `self._db_run(_work)` без исключения —
собирается `Counter` по `event_type` батча и прибавляется
к `self._stats["written_by_type"]` под `_state_lock`.

В `_enqueue` инкремент ЗАПРЕЩЁН — это вводит различие
«поставлено в очередь» vs «реально записано». Если событие
было в очереди, но БД упала до flush'а, счётчик его не
учитывает (зато `failed` инкрементируется — диагностический
сигнал есть).

Альтернативы:

- Считать в `_enqueue` (мгновенно) — врёт при ошибке flush'а.
- Считать через SQL `GROUP BY event_type` каждый раз —
  лишний запрос на чтение.

Выбрано: после успешного `_flush_batch`.

### D6. `oldest_queued_age_sec` через явное поле `queued_at` на `LogEvent`

`LogEvent.queued_at: float | None` заполняется в `_enqueue`
значением `time.time()`. `get_stats()` линейно сканирует
`self._queue.queue` (deque), фильтрует по `isinstance(item, LogEvent)`
и возвращает `max(time.time() - event.queued_at)` —
возраст **самого старого** `LogEvent` в очереди
(исключая `_QuestionRunRecord` и `_FlushSentinel`).

Семантика «самого старого» (max возраста, а не первый
по FIFO) — намеренная: контракт метрики — это
«возраст самой старой ожидающей записи», а не «возраст
первого элемента очереди». В типичной FIFO-очереди
эти значения совпадают, но контракт не должен быть
привязан к внутреннему порядку.

Если `LogEvent` в очереди нет — `None`.

Альтернативы:

- Отдельный поток, обновляющий `stats` раз в секунду —
  лишний поток и пробуждение, не нужно.
- Возвращать первый по FIFO — семантически уже, и
  связывает контракт с внутренней реализацией очереди.

Выбрано: `max(time.time() - event.queued_at)` среди
`LogEvent`, ленивое вычисление при `get_stats()`.

### D7. Документация payload как snapshot

Схема `payload` в `workspace/TOOLS.md` описывает **текущую**
форму данных. Поля, сериализованные как JSON-string
(например, `tool_result.payload.result`), явно помечаются
как таковые — это описание фактического поведения, а не
нормативный контракт на будущее. Изменение формы данных
требует отдельного change.

Альтернативы:

- Зафиксировать форму payload как нормативный контракт —
  превращает существующий технический debt (двойной JSON
  в `tool_result`) в часть SDD.

Выбрано: snapshot с явным указанием «текущая форма, изменение
требует отдельного change».

## Risks / Trade-offs

- **R1**: `LIMIT N+1` запрашивает на 1 строку больше, чем
  агент увидит в ответе. → **Mitigation**: на больших
  таблицах overhead минимален (одна лишняя строка в JSON
  parse + отбрасывание); на маленьких — отсутствует.

- **R2**: `offset` без cursor'а нестабилен при активных
  INSERT между страницами. → **Mitigation**: сценарий
  recovery предполагает, что агент листает быстро
  (десятки секунд); `has_more` позволяет остановиться.
  Если строгая стабильность понадобится — cursor отдельным
  change.

- **R3**: Снижение `flush_interval_sec` до 0.5 увеличивает
  нагрузку на БД. → **Mitigation**: дефолт остаётся 5.0,
  нижняя граница 0.5 жёсткая, оператор снижает осознанно
  и под наблюдением.

- **R4**: Deprecated `truncated` остаётся в JSON-ответе
  на один релиз — шум для агентов, которые читают
  `truncated` и не знают о новых полях. → **Mitigation**:
  в `workspace/TOOLS.md` deprecated помечено явно.

- **R5**: `written_by_type` занимает память пропорционально
  числу уникальных `event_type`. → **Mitigation**: число
  типов фиксировано (8 enum + `error` + `outbound_delta`),
  десятки байт.

- **R6**: Документирование payload как snapshot может
  разойтись с реальностью. → **Mitigation**: TOOLS.md
  синхронизируется в том же изменении, где меняется код;
  регрессионные тесты покрывают наличие всех типов
  в фикстурах.

## Migration Plan

Изменения runtime/tool-уровня, миграций БД нет.

**Применение:**

1. Merge change через стандартный PR-флоу.
2. Новые опциональные ключи `logging.db.flush_interval_sec`
   имеют дефолт `5.0`, существующие `project.json`/`config.json`
   работают без правок.
3. Поле `truncated` в ответе `history_search` deprecated —
   один MINOR-релиз совместимости.
4. Удаление `truncated` — отдельный follow-up change
   (новый proposal/design/tasks).

**Откат:**

- `flush_interval_sec`: поменять значение в `project.json`,
  перезапустить gateway. Никаких миграций.
- `offset`, `has_more`, `results_truncated`, `payload_truncated`,
  `written_by_type`, `oldest_queued_age_sec`: revert одного
  PR.
- Детерминированная сортировка: revert одного PR.

## Open Questions

- **Q1**: Стоит ли в будущем добавить cursor-пагинацию для
  строгой консистентности при активных INSERT'ах? → Запрос
  вне scope текущего change; решается отдельным change,
  если появится use-case (например, log-archive UI).
- **Q2**: Нужно ли публиковать `written_by_type` и
  `oldest_queued_age_sec` в `runtime_health`? → Сейчас
  только в `get_stats()`. Изменение `runtime_health`
  формально — отдельный change (`runtime/context`),
  не блокирует текущий.
