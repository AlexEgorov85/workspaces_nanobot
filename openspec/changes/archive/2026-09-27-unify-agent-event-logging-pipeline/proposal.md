## Why

В runtime сейчас существуют **два независимых пути записи
structured agent events в `agent_gateway_logs`**:

1. `DbLoggingService` (очередь + worker-поток + батч + INSERT
   через общий пул `utils.db`) — канонический путь;
2. `workspace/utils/event_log.py` (sync `INSERT INTO
   "<schema>"."<table>"` через `utils.db.execute`) — обходной
   путь, оставшийся от момента, когда `DbLoggingService`
   ещё не покрывал `context_compacted` и sync-события.

`workspace/utils/event_log.emit_sync_event` формально
делегирует в `DbLoggingService.log_sync_event`, но при
`service is None` или `service.is_running() == False`
**тихо падает обратно на прямой INSERT** через
`record_sync_event` — то есть в репозитории физически
существует «скрытый второй writer», который активируется
ровно тогда, когда первый ещё не стартовал (ранний startup
`ApplicationContext._record_sync_skipped`) или не сконфигурирован
(тесты, standalone-утилиты).

Это порождает класс архитектурных проблем, а не одну
точечную:

- два независимых timestamp-семантики (`NOW()` при sync INSERT
  vs время flush'а при queue-INSERT), что ломает порядок
  событий в `agent_gateway_logs`;
- два независимых error-handling пути (sync-прямой INSERT
  глотает ошибки и теряет событие молча, queue-INSERT
  инкрементирует `stats["failed"]` и сохраняет `last_error`);
- два независимых lifecycle (sync-INSERT живёт без
  `ApplicationContext.start()`, queue-INSERT зависит от
  worker-потока);
- два независимых способа читать logging-DB конфиг
  (`SETTINGS["logging"]["db"]["*"]` напрямую в
  `record_event` vs `ProjectSettings.logging.db` через
  resolver chain в `DbLoggingService`);
- обход `written_by_type` / `oldest_queued_age_sec` /
  `_ensure_schema` / `purge_empty_outbound` / `purge_old`;
- смешение concerns в `ContextCompactionService._notify`:
  `notify_in_history=False` гасит и UI-заметку в
  `agent_conversation_messages`, и `context_compacted`
  событие в `agent_gateway_logs`, хотя это **разные
  concerns** (UI-уведомление vs observability-trail).

`context_compacted` обязан жить в долговечном журнале
независимо от `notify_in_history` — сейчас при
`notify_in_history=false` structured event
`context_compacted` в `agent_gateway_logs` не
создаётся (факт подтверждается
`tests/test_context_compaction.py:837-854`,
`TestNotifyRecordsEventLog::test_notify_skips_event_log_when_notify_disabled`).
То есть `history_search(event_type="context_compacted")`
в этом режиме возвращает пустой результат. После
изменения событие должно создаваться в
`agent_gateway_logs` всегда при `enabled=True`,
а `notify_in_history` управляет только UI-стороной
(заметкой в `agent_conversation_messages`).

## What Changes

- **Удалить** модуль `workspace/utils/event_log.py` целиком:
  `record_event`, `record_sync_event`, `emit_sync_event`.
  Не оставлять deprecated-обёрток: после архитектурного
  refactor не должно быть «скрытого legacy», который
  маскирует прежний механизм.
- **Удалить** тест `tests/test_event_log.py` (он тестировал
  именно тот прямой INSERT, который ликвидируется).
- **Изменить** `ContextCompactionService`:
  - инжектировать `db_logging_service: DbLoggingService`
    через конструктор (composition root
    `ApplicationContext._make_compaction_service`);
  - `_record_event_log` пишет `context_compacted` через
    `db_logging_service.log_event(LogEvent(...))` (sync,
    через пул) — никаких прямых `INSERT`;
  - **развязать concerns**: `_write_history_notice` остаётся
    под `notify_in_history`, `_record_event_log` —
    **всегда** (пока `enabled=True`), независимо от
    `notify_in_history`;
  - запись `context_compacted` не должна ломать compaction
    даже если `DbLoggingService` недоступен / остановлен:
    business-операция остаётся успешной, observability
    теряется (фиксируется в loguru).
- **Изменить** `PgDuckDbSyncService._log_sync_event`,
  `DuckDbCacheStore._emit_sync_event`,
  `PreloadService._emit_health_event`,
  `ApplicationContext._record_sync_skipped` — все sync-события
  идут **только** через инжектированный `db_logging_service`
  (`log_sync_event` / `log_event(LogEvent(...))`). Если
  сервис `None` или не запущен — **no-op for business +
  operational WARNING** (как
  `postgres_channel.py:_journal_event`), без fallback direct
  INSERT.
- **Запретить** runtime-коду вне `lib/services/db_logging_service.py`
  выполнять прямой `INSERT INTO "<schema>"."<table>"` против
  `agent_gateway_logs` (или любой таблицы, заданной
  `logging.db.table_name`). Это касается и `utils.db.execute`,
  и `utils.db.run`, и любых новых SQL-путей.
- **Запретить** producer'ам structured events читать
  logging-DB конфиг (`logging.db.*`, `channels.postgres.dsn`
  для целей INSERT в журнал). Они получают уже сконфигурированный
  сервис через composition root и не должны знать ни DSN,
  ни `table_name`, ни `schema`.
- **Добавить** architecture guard: тест, который проверяет,
  что в runtime-коде вне `lib/services/db_logging_service.py`
  нет ни одного вызова `INSERT INTO ... agent_gateway_logs`
  и нет импорта удалённого модуля
  `workspace.utils.event_log`. Тест запускается в `pytest`
  и валится, если запрет нарушен.
- **Зафиксировать границу контракта**: Skill invocation
  is out of scope. Skills в текущей архитектуре — это
  content (`SKILL.md`), инжектируемый в agent context
  через `SkillsLoader.load_skills_for_context(...)` /
  `build_skills_summary(...)`, а не runtime-callable
  сущность. Поэтому dedicated `event_type="skill_call"`
  НЕ вводится и `DbLoggingService.log_skill_call(...)`
  НЕ существует. Загрузка `SKILL.md` в context не
  порождает event; вызов Skill-скриптов агентом
  через `tools.exec("python skills/<name>/scripts/cli.py ...")`
  логируется как штатная пара `event_type="tool_call"`
  / `event_type="tool_result"` с характерным payload
  (имя tool'а — `exec`). Это фиксируется в спеке как
  requirement «Skill invocation is out of scope» —
  страховка от попыток будущего разработчика добавить
  отдельный `skill_call` event_type, для которого
  нет runtime-call site'а в текущей версии nanobot.
- **Документация**:
  - `docs/ARCHITECTURE.md` — зафиксировать «`DbLoggingService`
    — единственный runtime writer of `agent_gateway_logs`»;
  - `AGENTS.md` (Project Layout, Configuration) — убрать
    упоминания `workspace/utils/event_log.py` и
    «dual-sink helper»;
  - `CHANGELOG.md` → блок `## [Unreleased]` → категории
    `Removed` и `Changed`;
  - `docs/skill-tool-inventory.md` — пометить удалённый модуль.

**BREAKING**: `workspace/utils/event_log` удаляется публично.
Утилиты и тесты, которые импортировали
`record_event` / `record_sync_event` / `emit_sync_event`,
должны мигрировать на `db_logging_service.log_event(...)`
через composition root. Это изменение контракта публичного
API проекта (см. `docs/skill-tool-boundary.md` — Skill не
должен импортировать infra-модули; этот модуль был
именно infra). MAJOR по SemVer не требуется, потому что
`event_log` исторически не экспортировался через
публичный API skill'ов — это внутренний runtime-helper;
MINOR с пометкой `Changed` достаточен.

## Capabilities

### New Capabilities

- `logging-db`: канонический путь для контракта
  `DbLoggingService` (единственный runtime writer
  `agent_gateway_logs` и `agent_question_runs`,
  требование «нет direct INSERT вне сервиса», развязка
  `context_compacted` от `notify_in_history`, поведение
  при недоступности сервиса). Ранее `logging-db` уже
  вводился как capability в change
  `improve-history-search-pagination-and-logging`
  (дельты `## ADDED Requirements` для `flush_interval_sec`
  и метрик `written_by_type` / `oldest_queued_age_sec`);
  эта спецификация дополняет его через собственные
  `## ADDED Requirements` (single-writer invariant,
  decoupled concerns, no fallback writer,
  configuration boundary, architecture guard). При
  архивировании обеих change'ей канонический
  `openspec/specs/logging-db/spec.md` собирается из
  суммы `## ADDED Requirements`.

### Modified Capabilities

Нет.

## Impact

- **Код:**
  - `workspace/utils/event_log.py` — удалить целиком
    (197 строк: `record_event`, `record_sync_event`,
    `emit_sync_event`).
  - `lib/services/context_compaction.py` —
    `ContextCompactionService.__init__(agent, settings,
    *, db_logging_service)` — keyword-only
    обязательный параметр (без дефолта;
    composition root обязан передать явно),
    `_record_event_log` использует
    `db_logging_service.log_event(LogEvent(...))`,
    `_notify` разделяет `_write_history_notice` (под
    `notify_in_history`) и `_record_event_log` (всегда при
    `enabled=True`).
  - `lib/core/application_context.py` —
    `_make_compaction_service` принимает
    `db_logging_service` и пробрасывает его в
    `ContextCompactionService` явным kwarg
    (composition root); `_record_sync_skipped` —
    фиксированный контракт: если `db_logging_service`
    сконфигурирован и запущен — вызвать
    `db_logging_service.log_sync_event(...)`; если нет
    — loguru-warning на уровне `WARNING` через
    `logger.warning(...)` (без записи в
    `agent_gateway_logs`, **никакого** прямого INSERT
    ни в каком режиме).
  - `lib/services/pg_duckdb_sync_service.py` —
    `_log_sync_event` использует
    `self._db_logging_service.log_sync_event(...)` напрямую,
    без `emit_sync_event`.
  - `lib/services/runtime_patcher.py` —
    `patch_compaction_tracking` принимает `db_logging_service`
    явным kwarg и передаёт его в
    `ContextCompactionService(...)`. Patch остаётся
    активным при `gateway.compact.enabled=true`,
    **включая** режим `notify_in_history=false`:
    раньше весь patch отключался при
    `notify_in_history=false`
    (`runtime_patcher.py:1882-1883`), что гасило
    и `_record_event_log` для auto-compaction.
    После изменения отключается только
    `_write_history_notice`, а `context_compacted`
    остаётся в `agent_gateway_logs`.
  - `lib/services/duckdb_cache_store.py` —
    `_emit_sync_event` (внутренняя обёртка) удаляется;
    caller's используют инжектированный
    `db_logging_service` напрямую.
  - `lib/services/preload_service.py` —
    `_emit_health_event` использует
    `self._db_logging_service.log_sync_event(...)`
    напрямую.
- **Тесты:**
  - удалить `tests/test_event_log.py`;
  - обновить `tests/test_context_compaction.py`:
    `TestNotifyRecordsEventLog::test_notify_skips_event_log_when_notify_disabled`
    переписывается — теперь ожидается, что
    `_record_event_log` вызывается даже при
    `notify_in_history=False`, а `_write_history_notice`
    не вызывается;
  - добавить `tests/test_unified_event_logging_pipeline.py`:
    архитектурный guard (нет `INSERT INTO ... agent_gateway_logs`
    вне `DbLoggingService`); unit-тесты
    «`notify_in_history=False` → `context_compacted`
    всё равно логируется»; «`db_logging_service=None`
    → no-op for business + WARNING без INSERT»;
    «decoupled concerns».
- **Документация:** `docs/ARCHITECTURE.md` § «Структурированное
  логирование» (полный rewrite), `AGENTS.md` (Project Layout,
  Configuration), `CHANGELOG.md` `[Unreleased]`,
  `docs/skill-tool-inventory.md` (помечает удалённый модуль).
- **Совместимость:** для существующих deployment'ов
  поведение `context_compacted` меняется только если
  `notify_in_history=False` (было: событие теряется;
  стало: событие пишется в `agent_gateway_logs`,
  UI-заметка в `agent_conversation_messages` не создаётся).
  Это **закрывает gap №1** из
  `docs/architecture/HISTORY_SEARCH_ANALYSIS.md` —
  `history_search` теперь всегда находит `context_compacted`,
  независимо от UI-настройки.
- **Совместимость (sync-события):** при отсутствии
  `db_logging_service` (тесты, standalone-утилиты)
  sync-события больше **не** пишутся прямым INSERT
  — это **наблюдаемое** изменение, но оно соответствует
  цели change и не ломает функциональность (sync-код
  работает по-прежнему, observability теряется в этих
  режимах — что и так документировано как «журнал
  отключается, остаётся только терминальный вывод»,
  см. `postgres_channel.py:_journal_event`).
