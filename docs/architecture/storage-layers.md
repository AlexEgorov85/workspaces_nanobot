# Storage layers: session hybrid model

Этот документ описывает модель хранения сессий после
`storage-hybridization` (см. OpenSpec change
`storage-hybridization`, спека
`openspec/specs/storage/session-hybridization/spec.md`).

## Архитектурная диаграмма

```text
┌─────────────────────────────────────────────────────────────────────┐
│                       Agent / Channels / Bus                         │
│                                                                     │
│   hot path: get_or_create / save / list_sessions / read_session_…   │
└─────────────────────────────┬───────────────────────────────────────┘
                              │ super().get_or_create(...) / super().save(...)
                              ▼
        ┌──────────────────────────────────────────────┐
        │   nanobot.session.manager.SessionManager      │
        │   (upstream, hot path owner)                   │
        │                                                │
        │   JsonlSessionStore                             │
        │   ~/.cache/nanobot/sessions/<workspace_id>/   │
        │   <key>.jsonl                                   │
        └─────────────────────┬────────────────────────┘
                              │ every 30 sec
                              │ batch (default 50), sorted by session_key
                              │ last-write-wins по updated_at
                              ▼
        ┌──────────────────────────────────────────────┐
        │   SessionColdSyncService                       │
        │   (lib/services/session_cold_sync_service.py)  │
        │                                                │
        │   • daemon thread (threading.Lock)             │
        │   • pg_try_advisory_xact_lock (D-Pool.2)       │
        │   • batch with sort by session_key             │
        │   • leader-election via pg_advisory_lock       │
        └─────────────────────┬────────────────────────┘
                              │ utils.db.transaction() / utils.db.run()
                              │ (единый пул, DI — без собственного psycopg2-пула)
                              ▼
        ┌──────────────────────────────────────────────┐
        │   PostgreSQL (cold-storage mirror)             │
        │                                                │
        │   public.agent_session_meta                    │
        │   public.agent_session_messages                │
        └──────────────────────────────────────────────┘
```

## Hot path: upstream `SessionManager`

Upstream `nanobot.session.manager.SessionManager` через
`JsonlSessionStore` — единственный hot-path writer для сессий:

- `get_or_create(key)` → читает/создаёт сессию в JSONL;
- `save(session)` → пишет сессию в JSONL (атомарно через
  `os.replace`);
- `list_sessions()` → возвращает список всех upstream-сессий;
- `read_session_metadata(key)` / `read_session_file(key)` /
  `read_session_snapshot(key)` → читают JSONL;
- `fork_session_before_user_index(...)` → новая upstream-сессия;
- `rename_model_preset(...)` → переименование preset;
- `update_session_metadata(...)` / `delete_session(...)` /
  `save_runtime_checkpoint(...)` / `restore_sessions_to_workspace(...)`.

Hot-path контракт зафиксирован в `tests/contract/test_session_manager_api.py`.

## `PGSessionManager` как compatibility layer

`lib/session/pg_session_manager.py` — тонкая обёртка над upstream
`SessionManager`. Hot-path методы (`get_or_create`, `save`,
`list_sessions`, `read_session_metadata`, `read_session_file`,
`delete_session`) делегируются в `super()`. Никаких прямых
`INSERT/UPDATE` в `agent_session_meta` /
`agent_session_messages` в hot path.

Класс сохранён для обратной совместимости 56 call-sites в коде;
конструкторские параметры (`dsn`, `schema`, `meta_table`,
`messages_table`) принимаются и пробрасываются в sync-сервис.

**Архитектурный инвариант**: ни один runtime-модуль вне
`SessionColdSyncService` НЕ пишет в `agent_session_meta` /
`agent_session_messages` напрямую. Проверяется через
`tests/test_storage_hybridization.py::TestNoDirectSQLToSessionTables`.

## Cold-storage mirror: `SessionColdSyncService`

Фоновый сервис в `lib/services/session_cold_sync_service.py`,
запускается daemon-потоком через `ApplicationContext.start()`.
Каждые `sync_interval_sec` (по умолчанию 30 секунд):

1. Захватывает per-transaction advisory lock
   `pg_try_advisory_xact_lock(hashtext('storage_hybridization_session_cold_sync')::bigint)`.
   Если lock занят (другая реплика — лидер) — пропускает цикл
   (`cycles_skipped_lock_busy += 1`).
2. Читает upstream JSONL-список через `session_manager.list_sessions()`.
3. Сортирует сессии по `session_key` для детерминированного порядка
   блокировок (исключает ABBA-deadlock с `DbLoggingService`).
4. Обрабатывает батчами (`batch_size`, default 50) — для каждой
   сессии читает `session_manager.read_session_snapshot(key)`,
   сравнивает `updated_at` с PG (`last-write-wins`), делает
   UPSERT в `agent_session_meta` + DELETE+INSERT в
   `agent_session_messages` (явная транзакция).
5. Cleanup: сравнивает `pg_keys` с `upstream_keys`; удаляет из PG
   любую сессию, которой нет в upstream (single-writer rule).
6. Освобождает advisory lock (автоматически на COMMIT).

Метрики (`get_stats()`):

- `cycles_total`, `cycles_failed_total`,
  `cycles_skipped_lock_busy`, `cycles_skipped_pool_busy`;
- `consecutive_failures`, `last_success_ts`,
  `last_success_lag_seconds`;
- `pool_size`, `pool_available`, `pool_wait_seconds` (D-Pool.6);
- `rows_synced_total`, `messages_synced_total`;
- `upstream_session_count`, `pg_session_count`.

## Правила использования пула (D-Pool)

`SessionColdSyncService` использует **единый** пул
`workspace/utils/db.py`. Никаких собственных psycopg2-пулов.
Полные правила зафиксированы в архивированном
`openspec/changes/archive/2026-09-27-storage-hybridization/design.md` § «Connection
pool» (D-Pool.1 — D-Pool.7). Краткая сводка:

- **DI через `utils.db.transaction()` / `utils.db.run()`** —
  никаких `psycopg2.pool.*` / `connect()` / `create_pool()`
  внутри модуля. Гард —
  `tests/test_storage_hybridization.py::TestNoNewPoolCreated`.
- **Per-transaction advisory lock** (`pg_try_advisory_xact_lock`)
  — нет долгоживущего соединения, lock освобождается
  автоматически на COMMIT/ROLLBACK.
- **Threading**: sync-код в daemon-потоке, соединение берётся
  и возвращается в одном worker-потоке пула (lease живёт в одном
  job'е).
- **Батчи с сортировкой** по `session_key` — детерминированный
  порядок блокировок.
- **Pool-busy → `cycles_skipped_pool_busy`** без падения.
- **Shutdown order** (D21): `stop()` ДО закрытия upstream
  `SessionManager`, чтобы успеть синхронизировать последние
  dirty-сессии.

## Multi-instance deploy

`pg_try_advisory_xact_lock` реализует leader-election без внешней
координации:

- каждая реплика пытается захватить lock в начале цикла;
- только одна реплика получает `True` и выполняет sync;
- остальные инкрементируют `cycles_skipped_lock_busy`;
- при краше реплики-держателя lock'а PG автоматически освобождает
  xact-lock на ROLLBACK.

Escape hatch: `gateway.session_cold_sync.enabled=false` —
sync-сервис не запускается вообще.

## Чистая миграция

`storage-hybridization` — **чистая** миграция. Исторические
сессии из старого `PGSessionManager` (если они есть в PG) ДОЛЖНЫ
быть перенесены в upstream JSONL **до** deploy отдельным скриптом
(вне scope этого change). После deploy upstream JSONL — единственный
source of truth; всё, чего нет в `list_sessions()`, удаляется
первым же sync-циклом.

См. `docs/MIGRATION.md` § «Storage migration (storage-hybridization)».

## D23: Stale-detection и reverse-lag detection

`SessionColdSyncService` защищает PG от перезаписи устаревшими
данными и детектит аномалии sync'а:

**Stale (PG свежее JSONL + tolerance):** если
`pg_meta.updated_at > jsonl_meta.updated_at + stale_tolerance` —
sync пропускается для этой сессии (`sync_skipped_stale_total += 1`),
однократно логируется `event_type="session_stale_detected"` (с TTL
60s in-memory dedup, чтобы не флудить). Дефолт tolerance — 120 сек
(`gateway.session_cold_sync.stale_tolerance_seconds`). Защита от
сценария «PG был обновлён внешним writer'ом (миграция, admin)
после deploy storage-hybridization; sync не должен перезаписать
актуальные данные устаревшими из JSONL».

**Reverse-lag (JSONL свежее PG + threshold):** если
`jsonl_meta.updated_at > pg_meta.updated_at + sync_lag_threshold` —
логируется `event_type="sync_lag_exceeded"` (для observability).
Дефолт threshold — 3600 секунд
(`gateway.session_cold_sync.sync_lag_threshold_seconds`).
Срабатывает, если sync-сервис долго не запускался (например,
после deploy или из-за lock_busy). При срабатывании sync всё равно
выполняется — это не блокирующий детект.

Оба события пишутся через `DbLoggingService.try_log_event` (без
прямого `INSERT` в `agent_gateway_logs`). Метрики
`stale_detected_total`, `sync_skipped_stale_total`,
`sync_lag_exceeded_total` публикуются в `get_stats()`.

## Failure modes

| Сценарий | Поведение |
|---|---|
| PG недоступен | sync-цикл логирует `event_type="session_cold_sync_failed"`, инкрементирует `consecutive_failures`, backoff |
| Advisory lock занят (multi-instance) | `cycles_skipped_lock_busy += 1`, цикл пропущен |
| Пул исчерпан | `cycles_skipped_pool_busy += 1`, цикл пропущен, hot path не затронут |
| Upstream JSONL пуст | sync пишет 0 строк, `upstream_session_count = 0` |
| Сессия есть в PG, но не в upstream | cleanup удаляет её из PG |
| Stale-сессия (PG свежее JSONL) | пропуск sync (`sync_skipped_stale_total`), лог `session_stale_detected` |
| JSONL значительно опережает PG | лог `sync_lag_exceeded` (sync всё равно выполняется) |

## Тесты

- `tests/contract/test_session_manager_api.py` — контракт на
  upstream `SessionManager` (14 методов, signatures, persistence).
- `tests/contract/test_session_manager.py` — round-trip через
  JsonlSessionStore.
- `tests/test_session_cold_sync_service.py` — mock-smoke
  `SessionColdSyncService` (включая архитектурный гард
  `test_no_new_pool_created`).
- `tests/test_pg_session_manager.py` — `PGSessionManager` как
  compatibility layer (17 тестов: super()-делегирование,
  no-op методы, docstring-инвариант).
- `tests/test_storage_hybridization.py` — архитектурные гарды
  (no-direct-SQL, no-new-pool, no-DbLoggingService-llm_usage,
  PGSessionManager docstring).
- `tests/test_storage_hybridization_factory.py` — mock-smoke
  `_make_usage_store` и `_make_session_cold_sync_service`.
- `tests/test_storage_hybridization_lifecycle.py` — mock-smoke
  lifecycle (start/stop, observer attach, usage store close).

## Файлы

- `lib/services/session_cold_sync_service.py` — sync-сервис.
- `lib/session/pg_session_manager.py` — compatibility layer.
- `lib/core/application_context.py` — регистрация сервиса
  в lifecycle.
- `lib/core/project_settings.py` — `UsageStoreSettings`,
  `SessionColdSyncSettings`.
- `lib/services/llm_usage_store_factory.py` — фабрика
  `LLMUsageStore`.
- `lib/services/llm_observer.py` — подключение observer-pipeline.
- `lib/services/runtime_health.py` — агрегация метрик.
- `tests/test_storage_hybridization.py` — архитектурные гарды.