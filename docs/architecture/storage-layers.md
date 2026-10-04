# Storage layers: session hybrid model

Этот документ описывает модель хранения сессий после
`storage-hybridization` (см. OpenSpec change
`storage-hybridization`, спека
`openspec/specs/storage/session-hybridization/spec.md`).

## Архитектурная диаграмма

```
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
                              │ признак изменения — source_digest
                              ▼
        ┌──────────────────────────────────────────────┐
        │   зеркало сессий                       │
        │   (lib/gateway/mirror/)  │
        │                                                │
        │   • daemon thread (threading.Lock)             │
        │   • batch with sort by session_key             │
        │   • решение о записи — на платформе            │
        └─────────────────────┬────────────────────────┘
                              │ операции платформы (state / write / cleanup)
                              │ через enterprise_mcp: SQL и пул — на платформе
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

## Стор сессий: `SanitizingSessionStore`, а не подкласс

`lib/session/pg_session_manager.py` — **не** наследник `SessionManager` и не
обёртка над PostgreSQL. Класса `PGSessionManager` в проекте нет: он был
compatibility layer'ом, пока hot path ходил в PG, и снят вместе с переходом на
гибрид.

Модуль экспортирует `SanitizingSessionStore`, `build_session_manager`,
`clean_session_content`. `build_session_manager()` собирает upstream
`SessionManager` поверх `SanitizingSessionStore` — то есть менеджер остаётся
классом библиотеки, а поведение агента добавлено слоем `SessionStore`.
Hot-path методы (`get_or_create`, `save`, `list_sessions`,
`read_session_metadata`, `read_session_file`, `delete_session`) — целиком
upstream (JSONL); никаких прямых `INSERT/UPDATE` в `agent_session_meta` /
`agent_session_messages` от менеджера не происходит и не происходило.

Единственное отличие стора от `JsonlSessionStore` — санитизация NUL при
записи. Наследование, а не композиция, выбрано из-за
`SessionManager.save_runtime_checkpoint`: он ускоряет оборот только при
`self._store is self._jsonl_store`.

Точка выбора режима — `SessionStorageService.create()`
(`lib/services/session_storage.py`). `storage="postgres"` означает «холодное
зеркало включено»: имена таблиц проверяются там и уходят в
`зеркало сессий`, а сам `SessionManager` про PostgreSQL не знает.

**Архитектурный инвариант**: ни один runtime-модуль вне
`зеркало сессий` НЕ пишет в `agent_session_meta` /
`agent_session_messages` напрямую. Проверяется через
`tests/test_storage_hybridization.py::TestNoDirectSQLToSessionTables`.

## Cold-storage mirror: `зеркало сессий`

Фоновый сервис в `lib/gateway/mirror/`,
запускается daemon-потоком через `ApplicationContext.start()`.
Каждые `sync_interval_sec` (по умолчанию 30 секунд):
1. Собирает аргументы операции зеркала (`MirrorEntry` →
   `build_write_arguments`: `session_key`, `replica_id`, `source_digest`,
   `updated_at`, `created_at`, `messages`) и вызывает операцию платформы.
   Решение о записи, транзакция и пул — на стороне платформы, в процессе
   `enterprise-mcp`; агент их не повторяет.
   процессе `enterprise-mcp`; агент их не повторяет.
2. Читает upstream JSONL-список через `session_manager.list_sessions()`.
3. Сортирует сессии по `session_key` для детерминированного порядка.
4. Отправляет батчами (`batch_size`, default 50) — для каждой сессии читает
   `session_manager.read_session_snapshot(key)` и сверяет дайджест;
   last-write-wins, `stale_tolerance` и `sync_lag_threshold` решаются
   платформой, а ответ приходит с вердиктом (`unchanged` / `skipped_equal` /
   `skipped_stale` / записан).
5. Cleanup: операция сравнивает список на платформе с upstream и удаляет из
   PG любую сессию, которой нет в upstream (single-writer rule). Пустой
   список upstream удаление запрещает (`cleanup_guarded_total`).
6. Advisory lock освобождается на платформе, на COMMIT.

Метрики (`get_stats()`):

- `cycles_total`, `cycles_failed_total`, `last_cycle_seconds`;
- `consecutive_failures`, `last_success_ts`, `last_success_lag_seconds`;
- `resource`, `enabled`, `disabled_reason`, `replica_id`;
- `source_count`, `mirror_count`, `written_total`,
  `skipped_unchanged_total`, `unreadable_total`, `source_missing_total`;
- `cleanup_guarded_total`, `deleted_total`;
- `sync_interval_sec`, `missing_cycles_threshold`;
- из ресурса `SessionMirror`: `messages_written_total`,
  `skipped_stale_total`, `stale_tolerance_seconds`,
  `sync_lag_threshold_seconds`, `upstream_session_count`,
  `mirror_session_count`, `sessions_written_total`,
  `snapshot_missing_total`, `cleanup_guarded_total`,
  `deleted_sessions_total`.

## Правила использования пула (D-Pool)

`зеркало сессий` **не держит соединения с БД**: `psycopg2` в пакете зеркала не
встречается ни разу, `_db_run`/`fetchval`/`execute` нет. Всё общение с
PostgreSQL идёт операциями платформы (`OP_STATE` / `OP_MIRROR` /
`OP_CLEANUP` в `lib/gateway/mirror/session_mirror.py`), а пул, advisory lock и
транзакция принадлежат процессу `enterprise-mcp`. Полные правила пула
зафиксированы в архивированном
`openspec/changes/archive/2026-09-27-storage-hybridization/design.md`
§ «Connection pool» (D-Pool.1 — D-Pool.7) — для платформенной стороны.
Краткая сводка:

- **Ни одного пула в агенте.** Зеркало получает готовый вердикт, БД не
  трогает; платформенные гарды —
  `mcp-platform/tests/test_architecture_boundaries.py`.
- **Per-transaction advisory lock** (`pg_try_advisory_xact_lock`) — живёт
  внутри платформенной операции: долгоживущего соединения у агента нет,
  lock освобождается автоматически на COMMIT/ROLLBACK.
- **Threading:** цикл в daemon-потоке агента; работа с БД выполняется
  синхронно внутри вызова операции, соединение платформенного пула берётся и
  возвращается в одном job'е.
- **Батчи с сортировкой** по `session_key` — детерминированный порядок
  блокировок.
- **Shutdown order** (D21): `stop()` ДО закрытия upstream `SessionManager`,
  чтобы успеть синхронизировать последние dirty-сессии.

## Multi-instance deploy

Механизма выбора лидера в коде нет: `pg_try_advisory` не встречается ни в
`lib/`, ни в `mcp-platform/`. Несколько реплик на общей таблице не
разрушаются, потому что запись арбитражна, а не сериализована: платформа
выполняет чтение зеркала, решение и запись **одной транзакцией**
(`mirror_session`, `capabilities/data/service/main.py`), а переход строки —
условным `UPDATE`. Блокировки строки нет сознательно: на Greenplum 6.5
`SELECT ... FOR UPDATE` взял бы блокировку уровня таблицы.

Поэтому и D-Pool.2 (`pg_try_advisory_xact_lock`) в этом документе — историческая
запись, а не действующее правило: его нет ни в коде агента, ни в коде
платформы.

Escape hatch: `gateway.session_cold_sync.enabled=false` —
sync-сервис не запускается вообще.

## Чистая миграция

`storage-hybridization` — **чистая** миграция. Исторические
сессии из снятого `PGSessionManager` (если они есть в PG) ДОЛЖНЫ
быть перенесены в upstream JSONL **до** deploy отдельным скриптом
(вне scope этого change). После deploy upstream JSONL — единственный
source of truth; всё, чего нет в `list_sessions()`, удаляется
первым же sync-циклом.

См. `docs/MIGRATION.md` § «Storage migration (storage-hybridization)».

## D23: Stale-detection и reverse-lag detection

`зеркало сессий` защищает PG от перезаписи устаревшими
данными и детектит аномалии sync'а:

**Stale (зеркало впереди JSONL + tolerance):** если
`agent_session_meta.updated_at > jsonl.updated_at + stale_tolerance` —
sync пропускается для этой сессии (`skipped_stale_total += 1`),
однократно логируется `agent.degraded` с префиксом `session_stale_detected`
(TTL 60s in-memory dedup, чтобы не флудить). Дефолт tolerance — 120 сек
(`gateway.session_cold_sync.stale_tolerance_seconds`). Защита от сценария
«зеркало обновил внешний writer (миграция, admin) — откаченный файл не
должен затереть более новое зеркало».

**Reverse-lag (JSONL впереди зеркала + threshold):** если
`jsonl.updated_at > agent_session_meta.updated_at + sync_lag_threshold` —
логируется `agent.degraded` с префиксом `sync_lag_exceeded` (для
observability). Событие не блокирующее: sync всё равно выполняется.
Дефолт threshold — 3600 секунд
(`gateway.session_cold_sync.sync_lag_threshold_seconds`).
Срабатывает, если sync-сервис долго не запускался (например, после deploy).

Оба события пишутся через `DbLoggingService.try_log_event` (без прямого
`INSERT` в `agent_gateway_logs`). Счётчик в `get_stats()` называется
`skipped_stale_total`; `stale_detected_total` и `sync_lag_exceeded_total`
в коде отсутствуют (0 совпадений).

## Failure modes

| Сценарий | Поведение |
|---|---|
| PG недоступен | цикл логирует `agent.degraded` с префиксом `<ресурс> cycle failed:`, инкрементирует `consecutive_failures`, backoff |
| Upstream JSONL пуст | цикл пишет 0 строк; удаление в PG запрещено (`cleanup_guarded_total`) |
| Сессия есть в PG, но не в upstream | cleanup удаляет её из PG |
| Сессия впереди зеркала (stale) | запись запрещена (`skipped_stale_total`), лог `session_stale_detected` |
| JSONL значительно опережает PG | лог `sync_lag_exceeded` (sync всё равно выполняется) |
| Дайджест совпал | вердикт `unchanged`, записи нет |

## Тесты

- `tests/contract/test_session_manager.py` — round-trip через
  JsonlSessionStore.
- `tests/test_session_cold_sync_service.py` — mock-smoke
  `зеркало сессий` (включая архитектурный гард
  `test_no_new_pool_created`).
- `tests/test_pg_session_manager.py` — `SanitizingSessionStore` и
  `build_session_manager` (18 тестов: санитизация NUL/control-символов, сборка
  менеджера поверх стора, поведение `clean_session_content`).
- `tests/test_storage_hybridization.py` — архитектурные гарды
  (no-direct-SQL, no-new-pool, no-DbLoggingService-llm_usage,
  docstring-инвариант).
- `tests/test_storage_hybridization_factory.py` — mock-smoke
  `_make_usage_store` и `_make_session_mirror`.
- `tests/test_storage_hybridization_lifecycle.py` — mock-smoke
  lifecycle (start/stop, observer attach, usage store close).

## Файлы

- `lib/gateway/mirror/` — sync-сервис.
- `lib/session/pg_session_manager.py` — compatibility layer.
- `lib/core/application_context.py` — регистрация сервиса
  в lifecycle.
- `lib/core/project_settings.py` — `UsageStoreSettings`,
  `SessionColdSyncSettings`.
- ~~`lib/services/llm_usage_store_factory.py`~~ — **снят**: хранилище создаёт
  библиотека (`nanobot.llm_usage.get_llm_usage_store()`).
- ~~`lib/services/llm_observer.py`~~ — **снят**: подписка observer'а свёрнута в
  `AgentFactory._wrap_provider_snapshot_loader`.
- `lib/services/runtime_health.py` — агрегация метрик.
- `tests/test_storage_hybridization.py` — архитектурные гарды.