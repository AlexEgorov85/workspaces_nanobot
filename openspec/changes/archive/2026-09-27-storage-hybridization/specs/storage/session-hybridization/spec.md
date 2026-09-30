## Purpose

Определяет нормативный контракт гибридной модели хранения сессий:
горячее хранилище (upstream JSONL через `nanobot.session.manager.SessionManager`)
обслуживает hot path операций `get_or_create` / `save` / `list_sessions`,
холодное хранилище (PostgreSQL через наш `PGSessionManager`-as-mirror)
обслуживает multi-instance, observability и durability. Цель —
предотвратить "расползание" session data по сторам и обеспечить
чёткие границы владения каждым слоем.

## ADDED Requirements

### Requirement: Hot path сессий через upstream SessionManager

Горячее хранилище сессий ДОЛЖЕН реализовывать upstream
`nanobot.session.manager.SessionManager` через
`JsonlSessionStore`. Все операции hot path
(`get_or_create`, `save`, `get_cached`, `read_session_metadata`,
`read_session_file`, `read_session_snapshot`,
`list_sessions`, `fork_session_before_user_index`,
`rename_model_preset`, `update_session_metadata`,
`save_runtime_checkpoint`, `delete_session`,
`set_delete_observer`) MUST выполняться через этот класс.
Прямой SQL `INSERT/UPDATE/DELETE` в таблицы `agent_session_meta`
и `agent_session_messages` в hot path ЗАПРЕЩЁН.

#### Scenario: get_or_create использует upstream SessionManager

- **WHEN** агент запрашивает сессию через
  `session_manager.get_or_create(session_key)`
- **THEN** сессия читается/создаётся через upstream
  `SessionManager` (JSONL-стор в
  `get_runtime_subdir("sessions")` / workspace-relative root).
- **AND** НЕ выполняется прямой `INSERT` в
  `agent_session_meta` в рамках этого вызова.

#### Scenario: save использует upstream SessionManager

- **WHEN** агент завершает turn и вызывает
  `session_manager.save(session)`
- **THEN** сессия пишется в upstream JSONL-стор через
  upstream `SessionManager.save`.
- **AND** НЕ выполняется прямой `INSERT` в
  `agent_session_messages` в рамках этого вызова.

### Requirement: Cold-storage mirror в PostgreSQL

PostgreSQL ДОЛЖЕН использоваться как cold-storage mirror для
сессий: дублирует JSONL-содержимое в таблицы
`agent_session_meta` и `agent_session_messages` для multi-instance
deploy, observability и disaster-recovery. Запись в PG MUST
идти через отдельный фоновый сервис `SessionColdSyncService`,
не из hot path операций upstream `SessionManager`.

#### Scenario: SessionColdSyncService зеркалит в PG

- **WHEN** `SessionColdSyncService` запускается по расписанию
  (например, каждые 30 секунд)
- **THEN** сервис читает upstream JSONL-стор и записывает
  изменившиеся сессии в PG `agent_session_meta` /
  `agent_session_messages` через `last-write-wins` по
  `updated_at` (upstream `Session.updated_at`).
- **AND** запись в PG НЕ блокирует hot path
  (`get_or_create` / `save`).
- **AND** DDL `agent_session_meta` / `agent_session_messages`
  НЕ изменяется в рамках этого change: колонка `updated_at`
  уже присутствует (см.
  `sql/session/create_public_agent_session_meta.sql`).
  Колонка `version` НЕ вводится — конфликт-резолюция
  опирается на существующий `updated_at`.

#### Scenario: Зеркалирование agent_session_messages (full re-read)

- **WHEN** `SessionColdSyncService._sync_cycle()` обрабатывает
  сессию, для которой `agent_session_meta.updated_at`
  в PG < `upstream_session.updated_at`
- **THEN** сервис загружает полный snapshot через
  `session_manager.read_session_snapshot(key)` (sync API,
  возвращает `Session`-объект с полным списком `messages`).
- **AND** записывает сообщения в `agent_session_messages`
  через явную **транзакцию** (НЕ autocommit):
  ```python
  with utils.db.transaction() as conn:
      conn.execute("DELETE FROM agent_session_messages WHERE session_key = %s", (key,))
      conn.execute("INSERT INTO agent_session_messages (...) VALUES (...)", [...])
  ```
  `transaction()` оборачивает в BEGIN/COMMIT; на исключении —
  ROLLBACK автоматически. Атомарность per session_key
  гарантируется.
- **AND** читатели (`PostgresChannel`, `history_search`)
  используют READ COMMITTED (default в PG); они видят
  либо старую версию сообщений (до DELETE), либо новую
  (после INSERT), но НЕ промежуточное состояние (после
  DELETE и до INSERT) — благодаря явной транзакции.
- **AND** если `agent_session_meta.updated_at` в PG ==
  `upstream_session.updated_at` (с точностью до
  микросекунды), sync пропускает эту сессию — данные
  идентичны, `SessionManager.save` атомарно перезаписывает
  JSONL через `os.replace`, что гарантирует identical
  `updated_at` ⇔ identical content.
- **AND** для первой синхронизации (новая upstream JSONL-сессия)
  сообщения ВСЕГДА перечитываются (это доминирующий
  сценарий для первой записи в PG).
- **AND** upstream JSONL — единственный source of truth:
  PG-сессии без upstream-двойника считаются устаревшими
  и удаляются cleanup-циклом (см. scenario «Удалённая
  upstream-сессия → diff-based cleanup в PG»).

#### Scenario: PG недоступен — hot path работает

- **WHEN** PostgreSQL недоступен (DSN не сконфигурирован,
  pool exhausted, network error)
- **THEN** upstream `SessionManager` (JSONL-стор) продолжает
  обслуживать `get_or_create` / `save` без ошибок.
- **AND** `SessionColdSyncService` логирует потерю sync-цикла
  через `DbLoggingService.log_sync_event(...)` с
  `event_type="session_cold_sync_failed"` и пропускает
  его до восстановления PG (без блокировки hot path).
- **AND** `event_type="session_cold_sync_failed"` —
  расширение списка sync-event_type'ов, зафиксированного
  в спеке `logging-db` (requirement «Sync-события через
  DbLoggingService»). Расширение документируется в
  `CHANGELOG.md` под `## [Unreleased]`.

#### Scenario: Single-flight защита от перекрытия циклов

- **WHEN** sync-цикл длится дольше `sync_interval_sec`
  (например, 45 сек при интервале 30 сек из-за большого
  workspace или медленного PG)
- **THEN** следующий цикл НЕ запускается параллельно:
  `SessionColdSyncService` использует внутренний
  `threading.Lock` (sync-код, не async) вокруг
  `_sync_cycle()`.
- **AND** если lock уже занят — `_sync_loop` пропускает
  итерацию, инкрементирует метрику
  `cycles_skipped_lock_busy`.
- **AND** `start()` через `_lifecycle.shutdown` ждёт
  завершения текущего цикла (через `lock.acquire()`).
- **AND** накопление лага предотвращается через
  экспоненциальный backoff при ошибках PG: если
  предыдущий цикл упал, задержка до следующего =
  `min(sync_interval_sec, sync_interval_sec * 2^consecutive_failures)`
  (cap = `sync_interval_sec * 2^5` = 16 минут при дефолте).

#### Scenario: Метрики sync-сервиса

- **WHEN** `SessionColdSyncService` работает
- **THEN** следующие метрики доступны через
  `SessionColdSyncService.get_stats() -> dict`:
  - `cycles_total`: количество выполненных циклов;
  - `cycles_failed_total`: количество упавших циклов;
  - `cycles_skipped_lock_busy`: циклов пропущено из-за занятого
    advisory-lock (другая реплика — лидер);
  - `cycles_skipped_pool_busy`: циклов пропущено из-за исчерпания
    пула (D-Pool.5);
  - `consecutive_failures`: счётчик подряд упавших;
  - `last_success_ts` (Unix timestamp): время последнего
    успешного цикла;
  - `last_success_lag_seconds`: разница между
    `last_success_ts` и текущим временем;
  - `pool_size`: текущий размер пула `utils.db` (`int | None`);
  - `pool_available`: свободные соединения в пуле (`int | None`);
  - `pool_wait_seconds`: время последнего цикла, включая ожидание
    lease'а (D-Pool.6);
  - `rows_synced_total`: количество синхронизированных
    сессий (метаданных);
  - `messages_synced_total`: количество синхронизированных
    сообщений;
  - `upstream_session_count`: количество сессий в
    `list_sessions()` на последнем успешном цикле;
  - `pg_session_count`: количество строк в
    `agent_session_meta` на последнем успешном цикле;
  - `stale_sync_skipped_total`: количество sync-пропусков из-за
    `pg > jsonl + stale_tolerance` (включая dedup TTL, см.
    requirement «Stale-detection и reverse-lag detection»);
  - `stale_detected_total`: количество уникальных событий
    `session_stale_detected` (после dedup TTL);
  - `sync_lag_exceeded_total`: количество событий
    `sync_lag_exceeded`.
- **AND** эти метрики экспортируются в health-check endpoint
  через `RuntimeHealth` (см. `lib/services/runtime_health.py`).

### Requirement: Multi-instance политика через pg_advisory_xact_lock

При наличии нескольких реплик gateway `SessionColdSyncService`
SHALL использовать `pg_try_advisory_xact_lock(hashtext(
'storage_hybridization_session_cold_sync'))` для автоматического
leader-election в рамках одной транзакции sync-цикла: ровно одна
реплика получает lock и выполняет sync, остальные пропускают
цикл. Это устраняет необходимость внешней координации
(Kubernetes labels, deployment manifests).

Дополнительный escape hatch: `gateway.session_cold_sync.enabled=false`
(default `true`) — sync-сервис не запускается вообще
(для реплик, которые по политике не должны синхронизировать).

> **Изменение модели lock:** первоначальная версия спеки
> использовала session-scoped `pg_try_advisory_lock` с явным
> `pg_advisory_unlock` в `finally`. Реальная имплементация и
> дизайн `storage-hybridization` D-Pool перешли на **per-transaction**
> (`pg_try_advisory_xact_lock`) — lock автоматически
> освобождается на COMMIT/ROLLBACK, без отдельного
> `pg_advisory_unlock`, без долгоживущего соединения.

#### Scenario: Leader-election через pg_try_advisory_xact_lock

- **WHEN** две реплики gateway стартуют одновременно и
  первая итерация `_sync_loop` запускается в обеих
- **THEN** каждая реплика вызывает
  `SELECT pg_try_advisory_xact_lock(hashtext('storage_hybridization_session_cold_sync')::bigint)`
  в начале цикла (через `utils.db.transaction()`, в той же
  транзакции, где идёт sync).
  - Явный `::bigint` cast — `hashtext()` возвращает
    `int4`; `pg_try_advisory_xact_lock` имеет две перегрузки
    `(bigint)` и `(int, int)`. Cast делает выбор перегрузки
    детерминированным и устраняет implicit cast.
  - Имя ключа `storage_hybridization_session_cold_sync`
    namespace-уникальное (с префиксом change'а), чтобы не
    пересечься с другими advisory-lock'ами в проекте.
- **AND** только одна реплика получает `True` (lock acquired);
  остальные получают `False` (lock already held).
- **AND** реплика с lock'ом выполняет `_sync_batch()` в той же
  транзакции.
- **AND** остальные пропускают цикл и инкрементируют
  метрику `cycles_skipped_lock_busy`.
- **AND** lock автоматически освобождается на COMMIT/ROLLBACK
  — никакого отдельного `pg_advisory_unlock` не требуется.

#### Scenario: Crash реплики-держателя lock

- **WHEN** реплика-держатель `pg_try_advisory_xact_lock` падает
  (segfault, kill -9, network partition)
- **THEN** транзакция ROLLBACK'ится автоматически при разрыве
  соединения; xact-scoped advisory lock освобождается
  (документированное поведение PostgreSQL для transaction
  locks).
- **AND** следующая реплика захватывает lock в следующем
  цикле и продолжает sync. Время обнаружения —
  не более `sync_interval_sec`.

### Requirement: Stale-detection и reverse-lag detection

`SessionColdSyncService` SHALL детектировать две аномалии и
публиковать через `DbLoggingService.try_log_event(...)` события
для observability:

1. **Stale (PG свежее JSONL):** если в PG
   `agent_session_meta.updated_at` для ключа `K` больше
   `upstream_session.updated_at` для того же `K` более чем на
   `stale_tolerance_seconds` (default `120`) — sync для этого
   ключа SHALL пропускаться (`continue`), и SHALL публиковаться
   событие `event_type="session_stale_detected"` с
   `payload={"session_key": K, "jsonl_updated_at": ...,
   "pg_updated_at": ...}`.
2. **Reverse lag (JSONL свежее PG):** если
   `upstream_session.updated_at` больше PG `updated_at` более
   чем на `sync_lag_threshold_seconds` (default `3600`) — sync
   для этого ключа SHALL выполняться нормально (LWW — mirror
   обновится), и SHALL публиковаться событие
   `event_type="sync_lag_exceeded"` с теми же полями.

Параметры SHALL быть конфигурируемыми через
`gateway.session_cold_sync.stale_tolerance_seconds` и
`sync_lag_threshold_seconds` (см. `SessionColdSyncSettings`
в `lib/core/project_settings.py`).

Stale-detection — защита cold-storage от перезаписи устаревшими
upstream-данными (сценарии «volume restore», «host migration»,
«multi-instance misconfiguration»). Reverse-lag detection —
observability для диагностики сломанного sync.

#### Scenario: Stale сессия — sync пропущен, событие опубликовано

- **WHEN** `SessionColdSyncService._sync_session(key)` обнаруживает
  `pg.updated_at > jsonl.updated_at + stale_tolerance`
- **THEN** sync для этого ключа SHALL быть пропущен (никаких
  `INSERT`/`UPDATE` в `agent_session_meta` /
  `agent_session_messages`).
- **AND** через `DbLoggingService` SHALL быть опубликовано
  событие `event_type="session_stale_detected"` с `payload`,
  содержащим `session_key`, `jsonl_updated_at`, `pg_updated_at`.
- **AND** метрика `stale_sync_skipped_total` SHALL быть
  инкрементирована (включая повторные skip после dedup TTL).

#### Scenario: Equal или within-tolerance PG-сессия — silent skip

- **WHEN** `pg.updated_at >= jsonl.updated_at`, но
  `pg.updated_at - jsonl.updated_at <= stale_tolerance`
- **THEN** sync SHALL пропустить эту сессию silently (текущее
  поведение до добавления stale-detection; регрессионные
  contract-тесты на этот случай обязательны — см. tasks
  storage-hybridization этап 0).

#### Scenario: Reverse lag — sync выполняется, событие опубликовано

- **WHEN** `SessionColdSyncService._sync_session(key)` обнаруживает
  `jsonl.updated_at > pg.updated_at + sync_lag_threshold`
- **THEN** sync для этого ключа SHALL выполниться нормально
  (LWW — mirror обновится).
- **AND** через `DbLoggingService` SHALL быть опубликовано
  событие `event_type="sync_lag_exceeded"`.
- **AND** метрика `sync_lag_exceeded_total` SHALL быть
  инкрементирована.

#### Scenario: Stale-детект как предпосылка для session-recovery

- **WHEN** change `session-recovery` активен в любом режиме
- **THEN** `SessionRecoveryService` SHALL использовать тот же
  staleness-детектор (single source of truth) для получения
  списка stale-сессий — никакого собственного re-implementation
  условия `pg > jsonl + tolerance` в `SessionRecoveryService`.
- **AND** режим `detect-only` SHALL оставаться no-op до тех пор,
  пока `SessionColdSyncService` не публикует события
  `session_stale_detected` (см. tasks `session-recovery` —
  «жёсткая зависимость от Части A»).

#### Scenario: enabled=false отключает sync

- **WHEN** в `project.json` на реплике установлено
  `"gateway": {"session_cold_sync": {"enabled": false}}`
- **THEN** `SessionColdSyncService` НЕ запускает фоновую
  задачу на этой реплике.
- **AND** upstream `SessionManager` (JSONL) продолжает
  обслуживать `get_or_create` / `save` локально — каждая
  реплика имеет свой локальный JSONL.
- **AND** `RuntimeHealth.get_stats()["session_cold_sync"]["enabled"]`
  возвращает `False` для observability.

#### Scenario: update_session_metadata зеркалируется

- **WHEN** upstream `SessionManager.update_session_metadata(key, payload)`
  вызывается для изменения метаданных сессии
- **THEN** upstream JSONL-стор обновляется синхронно.
- **AND** mirror-операция в PG выполняется асинхронно
  через `SessionColdSyncService` (не блокирует hot path)
  и сохраняет обновлённые метаданные в `agent_session_meta.metadata`.

#### Scenario: delete_session и set_delete_observer

- **WHEN** upstream `SessionManager.delete_session(key)` вызывается
  для удаления сессии
- **THEN** сессия удаляется из upstream JSONL-стора.
- **AND** `SessionColdSyncService` подписан на
  `set_delete_observer(...)` upstream API и удаляет
  соответствующие строки из PG `agent_session_meta` /
  `agent_session_messages` в своём цикле (не блокирует hot path).

#### Scenario: save_runtime_checkpoint и restore_sessions_to_workspace

- **WHEN** upstream `SessionManager.save_runtime_checkpoint(key)`
  или `restore_sessions_to_workspace(...)` вызывается
- **THEN** upstream JSONL-стор обновляется синхронно.
- **AND** `SessionColdSyncService` подхватывает изменения
  в своём sync-цикле через `list_sessions()` /
  `read_session_metadata(...)` и зеркалирует в PG.

### Requirement: Single-writer per layer в session storage

Сессионное состояние (history, checkpoints, provider state)
MUST иметь ровно один hot-path writer — upstream
`SessionManager`. `PGSessionManager` (или его
наследник/переименование) SHALL быть cold-storage mirror,
NOT отдельным primary writer. Никаких "двойных записей"
hot-path данных в JSONL и PG одновременно — upstream JSONL
SHALL всегда писаться первым; PG SHALL обновляться
асинхронно через `SessionColdSyncService`.

#### Scenario: Нет двойной записи в hot path

- **WHEN** агент вызывает `session_manager.save(session)`
- **THEN** upstream `SessionManager.save(session)` пишет в
  JSONL, и это единственный обязательный side-effect.
- **AND** запись в PG `agent_session_messages` ЗАПРЕЩЕНА
  в рамках этого вызова (mirror идёт отдельным
  `SessionColdSyncService`-циклом).

#### Scenario: rename_model_preset делегируется в upstream

- **WHEN** пользователь меняет model preset для сессии через
  `session_manager.rename_model_preset(...)`
- **THEN** операция выполняется через upstream
  `SessionManager.rename_model_preset`.
- **AND** mirror-операция в PG идёт асинхронно через
  `SessionColdSyncService` (не блокирует hot path).

#### Scenario: Равные updated_at (LWW tie-break)

- **WHEN** `upstream_session.updated_at` ==
  `agent_session_meta.updated_at` в PG
- **THEN** `SessionColdSyncService` SHALL пропустить
  обновление mirror (апдейт не нужен — данные идентичны).
- **AND** tie-break через `>=` НЕ используется: точное
  равенство трактуется как «нет изменений» (SessionManager
  атомарно перезаписывает JSONL через `os.replace`, что
  гарантирует identical `updated_at` ⇔ identical content).

#### Scenario: Удалённая upstream-сессия → diff-based cleanup в PG

- **WHEN** `SessionManager.delete_session(key)` удаляет
  upstream JSONL-сессию
- **THEN** в следующем sync-цикле
  `SessionColdSyncService._sync_cycle()` сравнивает
  `upstream_keys = {s["key"] for s in session_manager.list_sessions()}`
  с `pg_keys = {row["session_key"] for row in SELECT session_key FROM agent_session_meta}`.
- **AND** для каждого `key ∈ pg_keys \ upstream_keys`
  (т.е. сессия есть в PG, но НЕ в upstream JSONL):
    - удаляет строки из `agent_session_meta` и
      `agent_session_messages` для этого `session_key`;
    - логирует факт через
      `try_log_event(event_type="session_cold_sync_deleted",
      payload={"session_key": key})` для observability.
- **AND** upstream JSONL — единственный source of truth. Если
  историческая сессия осталась в PG до deploy (без upstream
  двойника), оператор должен выполнить отдельный скрипт
  миграции ДО deploy (см. requirement «Чистая миграция на
  upstream SessionManager»); иначе она будет удалена первым
  же sync-циклом.

#### Scenario: Fork создаёт новую сессию

- **WHEN** `SessionManager.fork_session_before_user_index(
  source_key="a", target_key="b", before_user_index=2)`
  вызывается
- **THEN** upstream JSONL создаёт новый файл
  `<workspace_id>/b.jsonl` (новая сессия).
- **AND** `list_sessions()` возвращает ОБЕ сессии
  (source и target) — fork не удаляет source.
- **AND** `SessionColdSyncService` подхватывает обе:
  - если target отсутствует в PG → INSERT;
  - если source уже в PG → только проверка `updated_at`.

#### Scenario: restore_sessions_to_workspace

- **WHEN** `SessionManager.restore_sessions_to_workspace()`
  выполняется (например, при cold-start из бэкапа)
- **THEN** upstream JSONL восстанавливает несколько сессий
  одновременно; `list_sessions()` возвращает их все.
- **AND** `SessionColdSyncService` обрабатывает каждую
  сессию индивидуально по watermark'у. Дубликаты не
  создаются (`ON CONFLICT (session_key) DO UPDATE`).
- **AND** upstream JSONL — единственный source of truth:
  все сессии в `list_sessions()` зеркалируются в PG; всё,
  чего нет в upstream (включая исторические PG-сессии без
  миграции), удаляется cleanup-циклом.

### Requirement: Contract tests на upstream SessionManager API

Использование upstream `SessionManager` MUST быть покрыто
контрактными тестами, фиксирующими публичный API nanobot
0.3.5: имена методов, сигнатуры, контракт persistence.
Тесты MUST запускаться первыми при апгрейде upstream и
MUST падать, если upstream меняет совместимый API.

#### Scenario: SessionManager публичные методы существуют

- **WHEN** контрактный тест проверяет наличие методов
  `get_or_create` / `save` / `list_sessions` /
  `read_session_metadata` / `fork_session_before_user_index` /
  `rename_model_preset` в `nanobot.session.manager.SessionManager`
- **THEN** все методы MUST быть доступны с ожидаемыми
  сигнатурами.
- **AND** тест MUST падать, если какой-либо метод
  отсутствует или меняет сигнатуру.

#### Scenario: JsonlSessionStore — дефолтный стор

- **WHEN** `SessionManager(workspace=path)` создаётся без
  явного `store` параметра
- **THEN** используется `JsonlSessionStore` (дефолт upstream).
- **AND** сессии пишутся в
  `get_runtime_subdir("sessions")` или workspace-relative
  root.

### Requirement: Audit shadow в PG независим от session mirror

Таблица `agent_conversation_messages` MUST продолжать
использоваться как UI-shadow для compaction-notice (через
`ContextCompactionService._write_history_notice`) согласно
спеке `logging-db`. Эта таблица НЕ ДОЛЖНА использоваться как
mirror для session messages — это **разные** persistence-модели
(UI-notice vs session data). Никаких изменений в
`ContextCompactionService` в рамках этого change.

#### Scenario: compaction-notice остаётся в agent_conversation_messages

- **WHEN** `ContextCompactionService.compact()` завершается
  успешно с `archived_msgs > 0`
- **THEN** `_write_history_notice` пишет в
  `agent_conversation_messages` согласно спеке `logging-db`
  (requirement «Single writer invariant of agent_gateway_logs»).
- **AND** `SessionColdSyncService` НЕ трогает
  `agent_conversation_messages` — это не его concern.

#### Scenario: history_search читает из agent_gateway_logs

- **WHEN** агент вызывает tool `history_search`
- **THEN** поиск идёт по `agent_gateway_logs` согласно
  спеке `tools-history-search`. Mirror-таблицы сессий
  (`agent_session_meta` / `agent_session_messages`) НЕ
  участвуют в search-path.

### Requirement: Чистая миграция на upstream SessionManager

`storage-hybridization` SHALL быть **чистой миграцией**:
исторические сессии из старого `PGSessionManager` (если они
есть в PG) MUST быть перенесены в upstream JSONL **до** deploy.
Сам `storage-hybridization` SHALL NOT делать автоматическую
миграцию legacy данных (см. proposal — «миграция legacy →
JSONL — отдельный будущий change»).

`SessionColdSyncService` работает только с upstream-снимками из
`SessionManager.list_sessions()`; всё, чего нет в upstream
JSONL, считается устаревшим и удаляется cleanup-циклом.

#### Scenario: Чистая миграция перед deploy

- **WHEN** оператор deploy'ит `storage-hybridization` в проект,
  где уже есть PG-сессии в `agent_session_meta` /
  `agent_session_messages`
- **THEN** оператор СНАЧАЛА выполняет отдельный скрипт
  миграции (вне scope этого change), переносящий PG-сессии
  в upstream JSONL через `SessionManager.get_or_create(...)`.
- **AND** после миграции upstream JSONL становится source of
  truth; `SessionColdSyncService` зеркалирует его в PG.
- **AND** исторические сессии, оставшиеся в PG без upstream
  двойника, удаляются первым же sync-циклом (cleanup-rule:
  upstream JSONL — единственный source of truth).

#### Scenario: UX-gap без legacy-зеркала

- **WHEN** пользователь открывает сессию, созданную до deploy
- **THEN** агент отвечает «новая сессия» (создаёт upstream
  JSONL-сессию через `get_or_create(key)`).
- **AND** если миграция была выполнена — история подгружается
  из upstream JSONL.
- **AND** если миграция НЕ была выполнена — пользователь видит
  пустую историю (legacy PG-история не подтягивается).
- **AND** это поведение задокументировано в `CHANGELOG.md` под
  `## [Unreleased]` → `Changed` (с пометкой: «run migration
  script перед deploy для сохранения истории»).

### Requirement: Правила использования пула PG-соединений

`SessionColdSyncService` SHALL использовать общий пул `utils.db`.
В модуле SHALL NOT быть собственных psycopg2-пулов,
`connect()` или `create_pool()`.

Полные правила (DI, advisory lock, threading, батчи, метрики,
shutdown order) — в `openspec/changes/storage-hybridization/design.md`
§ «Connection pool» (D-Pool). Здесь фиксируется только
нормативный контракт.

#### Scenario: Пул — единый, через DI

- **WHEN** `SessionColdSyncService` обращается к PG
- **THEN** он использует `utils.db.transaction()` /
  `utils.db.run()` для всех операций.
- **AND** НЕ создаёт собственный `SimpleConnectionPool` /
  `psycopg2.pool` / `connect()` / `create_pool`.

#### Scenario: Advisory lock — per-transaction (xact-scoped)

- **WHEN** `SessionColdSyncService` начинает sync-цикл
- **THEN** он вызывает
  `SELECT pg_try_advisory_xact_lock(hashtext('storage_hybridization_session_cold_sync')::bigint)`
  внутри одной транзакции.
- **AND** если результат `False` — цикл пропускается
  (`cycles_skipped_lock_busy += 1`).
- **AND** lock автоматически освобождается на COMMIT/ROLLBACK
  (xact-scoped) — никакого долгоживущего соединения.

#### Scenario: Пул исчерпан — цикл пропущен

- **WHEN** `utils.db.run(...)` бросает `RuntimeError` /
  `TimeoutError` / `PoolError` (пул временно исчерпан)
- **THEN** `SessionColdSyncService` инкрементирует
  `cycles_skipped_pool_busy`, логирует
  `event_type="session_cold_sync_failed"` через
  `DbLoggingService.try_log_event` и ждёт следующий цикл
  через backoff.
- **AND** hot path НЕ затрагивается (sync работает в фоне).

#### Scenario: Батчи с сортировкой по session_key

- **WHEN** `SessionColdSyncService._sync_batches()` обрабатывает
  upstream-список сессий
- **THEN** список сортируется по `session_key` ДО батчинга
  (детерминированный порядок блокировок — исключает ABBA-deadlock
  с `DbLoggingService`, которая тоже пишет в этот пул).
- **AND** размер батча ≤ `gateway.session_cold_sync.batch_size`
  (default `50`).
