# storage/session-hybridization Specification

## Purpose
Определяет нормативный контракт гибридной модели хранения сессий:
горячее хранилище (upstream JSONL через `nanobot.session.manager.SessionManager`)
обслуживает hot path операций `get_or_create` / `save` / `list_sessions`,
холодное хранилище (PostgreSQL через `SessionMirror`-as-mirror)
обслуживает multi-instance, observability и durability. Цель —
предотвратить "расползание" session data по сторам и обеспечить
чёткие границы владения каждым слоем.

## Scope

`agent` — зеркало холодного хранилища сессий — агентское
Реализация: `lib/session/pg_session_manager.py`, `lib/gateway/mirror/`

## Requirements

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
идти через отдельный фоновый сервис `SessionMirror`,
не из hot path операций upstream `SessionManager`.

#### Scenario: SessionMirror зеркалит в PG

- **WHEN** `SessionMirror` запускается по расписанию
  (например, каждые 30 секунд)
- **THEN** сервис читает upstream JSONL-стор и записывает
  изменившиеся сессии в PG `agent_session_meta` /
  `agent_session_messages` через `last-write-wins` по
  `updated_at` (upstream `Session.updated_at`).
- **AND** запись в PG НЕ блокирует hot path
  (`get_or_create` / `save`).
- **AND** DDL `agent_session_meta` / `agent_session_messages`
  ИЗМЕНЁН этим change: в ключ добавлен `replica_id`, введены
  `source_digest`, `missing_cycles`, `message_count`, `synced_at`
  (миграции `V010`, `V011`). Прежняя редакция требовала DDL не трогать
  и обосновывалась тем, что «конфликт-резолюция опирается на
  существующий `updated_at`» — а этот столбец и оказался непригоден как
  признак изменения (см. требование про stale-detection).
- **AND** сообщения зеркалируются ВСЕМИ колонками DDL, а не пятью:
  прежняя запись теряла `tool_calls`, `tool_call_id`, `name`,
  `reasoning_content`, `thinking_blocks`, `media`, `cli_apps`,
  `mcp_presets`, `injected_event`, `_command`, `_channel_delivery`, и для
  аварийного восстановления это была невосстановимая потеря.

#### Scenario: Зеркалирование agent_session_messages (full re-read)

- **WHEN** `SessionMirror._sync_cycle()` обрабатывает
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
- **AND** если `agent_session_meta.source_digest` в PG ==
  `source_digest` файла сессии — sync пропускает эту сессию, содержимое
  идентично.
- **AND** если метки времени равны, но дайджесты разошлись — sync
  ВЫПОЛНЯЕТСЯ. Прежняя формулировка («identical `updated_at` ⇔ identical
  content») была ложным инвариантом: `JsonlSessionStore.update_metadata`
  переписывает только поле `metadata` первой строки файла, оставляя
  `updated_at` прежним, а `SessionManager.save` сохраняет метку как есть.
  На правке metadata это правило замирало навсегда, и разошедшееся зеркало
  было уже нечем починить. Признак изменения — дайджест; `updated_at` решает
  только направление конфликта. См. change
  `2026-10-03-session-mirror-mcp`.
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
- **AND** зеркало сессий логирует потерю sync-цикла
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
  `SessionMirror` использует `asyncio.Lock` вокруг тела
  цикла (сервис — задача event loop, а не daemon-поток, потому
  что клиент платформы привязан к своему loop'у).
- **AND** остановка сервиса ждёт завершения текущего цикла:
  `stop()` через `_lifecycle.shutdown` дожидается текущей
  итерации, иначе корутина потерялась бы молча.
- **AND** накопление лага предотвращается через
  экспоненциальный backoff при ошибках PG: если
  предыдущий цикл упал, задержка до следующего =
  `min(sync_interval_sec, sync_interval_sec * 2^consecutive_failures)`
  (cap = `sync_interval_sec * 2^5` = 16 минут при дефолте).

#### Scenario: Метрики sync-сервиса

- **WHEN** `SessionMirror` работает
- **THEN** следующие метрики доступны через
  `SessionMirror.get_stats() -> dict`:
  - `cycles_total`: количество выполненных циклов;
  - `cycles_failed_total`: количество упавших циклов;
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

### Requirement: Multi-instance изоляция через replica_id в ключе

При нескольких репликах gateway зеркало разграничивается СОСТАВНЫМИ
первичными ключами, а не соглашением в коде.
`SessionMirror` SHALL передавать `replica_id` в каждую операцию
зеркала; цикл очистки SHALL ограничиваться своей репликой.

Ключи таблиц:

- `agent_session_meta` — `PRIMARY KEY (replica_id, session_key)`;
- `agent_session_messages` — `PRIMARY KEY (replica_id, session_key, seq)`.

> **Поправка 2026-10-03.** Ключ сообщений изменён. Прежде было два
> уникальных ключа — `PRIMARY KEY (id)` и
> `UNIQUE (replica_id, session_key, seq)`, — а Greenplum 6 допускает на
> хеш-распределённой таблице ровно один `UNIQUE`/`PRIMARY KEY` и требует,
> чтобы он включал все столбцы распределения. Такая таблица не создавалась
> вовсе. Решение владельца: составной ключ объявлен единственным,
> уникальность по нему держит писатель — `mirror_session` удаляет все
> сообщения сессии и вставляет заново с `seq = 0…N-1`, поэтому дубль на
> одну позицию невозможен по построению. `id` остался обычной колонкой:
> он нужен для разбора неустойчивых позиций, потому что `seq` меняет
> смысл при сдвиге нумерации после консолидации.

> **Заменяет leader-election на advisory lock.** Предыдущая редакция требовала
> `pg_try_advisory_xact_lock` на весь цикл. Требование снято: xact-lock живёт
> до COMMIT, а цикл состоит из нескольких вызовов платформы и такой транзакции
> не образует. Замена lock'а — не упрощение, а починка: без `replica_id` в
> ключе реплики затирали зеркала друг друга, и advisory lock этого не
> предотвращал (он сериализовал циклы, но не разграничивал принадлежность).
> См. change `2026-10-03-session-mirror-mcp`.

Идентичность реплики SHALL переживать перезапуск: по умолчанию это имя
машины (`gateway.session_cold_sync.replica_id` переопределяет для нескольких
реплик на одной). `os.getpid()` запрещён — после рестарта реплика получила бы
новое имя, её прежние строки осиротели бы, и очистка их не видела бы.

#### Scenario: Ровно одна строка на (реплика, сессия)

- **WHEN** две реплики зеркалируют одну и ту же сессию `K`
- **THEN** каждая пишет в СВОЮ строку `(replica_id, K)`, а не конкурирует
  за одну общую.
- **AND** очистка реплики `A` SHALL удалять только строки с
  `replica_id = A`; строки реплики `B` не читаются и не трогаются.
- **AND** следствие: сценарий «зеркало впереди, но принадлежит другой
  реплике» невозможен, и «холодное впереди» однозначно означает откат
  СОБСТВЕННОГО файла (восстановление, смена машины).

#### Scenario: Удалённая сессия удаляется не с первого пропуска

- **WHEN** сессия `K` реплики `A` отсутствует в `list_sessions()` N циклов
  подряд
- **THEN** строки зеркала удаляются при `N >= missing_cycles_threshold`
  (default `2`), не при первом пропуске.
- **AND** пустой список сессий SHALL НЕ являться основанием для уборки при
  непустом зеркале: каталог сессий лежит на диске, и пуст он бывает не
  «потому что всё удалили», а потому что не подмонтирован. Уборка
  пропускается с записью `cleanup_guarded_total`.

Leader-election между репликами **не применяется**, и это не недосмотр.

> **Изменение модели lock (2026-10-03).** Спека требовала
> `pg_try_advisory_xact_lock(hashtext('storage_hybridization_session_cold_sync'))`
> для выбора лидера. Требование **снято**: цикл состоит из нескольких
> вызовов платформы, и одной транзакции он не образует — lock физически не
> переживает границу между вызовами. Взамен разграничение сделано данными:
> `replica_id` в первичном ключе, так что реплики пишут и убирают **свои**
> строки и не конкурируют за общую. Leader-election поверх этого был бы
> второй, избыточной проверкой того же факта.
>
> Что осталось в коде: локальный `asyncio.Lock` на цикл — он не допускает
> наложения итераций **внутри одной реплики**, и это другая задача.
> Счётчик `cycles_skipped_lock_busy` удалён вместе с lock'ом: он был
> мёртвым, потому что пути, который его наполнял, не существовало.

Дополнительный escape hatch: `gateway.session_cold_sync.enabled=false`
(default `true`) — sync-сервис не запускается вообще
(для реплик, которые по политике не должны синхронизировать).

#### Scenario: реплики не конкурируют за одну и ту же строку

- **WHEN** две реплики gateway работают одновременно
- **THEN** каждая пишет и убирает **только** строки со своим
  `replica_id`, а не соревнуется за общую строку сессии
- **AND** leader-election SHALL NOT применяться: отдельного lock'а нет,
  а разграничение обеспечено составным первичным ключом
- **AND** наложение итераций **внутри** одной реплики SHALL
  предотвращаться локальным `asyncio.Lock`

#### Scenario: Crash реплики

- **WHEN** реплика, ведущая sync, падает (segfault, kill -9, network
  partition)
- **THEN** разграничение реплик SHALL сохраняться без всякой
  координации: у каждой свои строки по `replica_id`, поэтому падение одной
  реплики не оставляет занятых блокировок и не мешает другой
- **AND** потеря реплики SHALL быть видна как осиротевшие строки, а её
  очистка SHALL зависеть от `missing_cycles` порога, а не от
  освобождения какого-либо lock'а

### Requirement: Stale-detection и reverse-lag detection

`SessionMirror` SHALL детектировать две аномалии и
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

- **WHEN** `SessionMirror._sync_session(key)` обнаруживает
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

- **WHEN** `SessionMirror._sync_session(key)` обнаруживает
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
  пока `SessionMirror` не публикует события
  `session_stale_detected` (см. tasks `session-recovery` —
  «жёсткая зависимость от Части A»).

#### Scenario: enabled=false отключает sync

- **WHEN** в `config.json` на реплике установлено
  `"gateway": {"session_cold_sync": {"enabled": false}}`
- **THEN** зеркало сессий НЕ запускает фоновую
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
  через `SessionMirror` (не блокирует hot path)
  и сохраняет обновлённые метаданные в `agent_session_meta.metadata`.

#### Scenario: delete_session ловится diff-циклом

- **WHEN** upstream `SessionManager.delete_session(key)` вызывается
  для удаления сессии
- **THEN** сессия удаляется из upstream JSONL-стора.
- **AND** `SessionMirror` НЕ подписывается на
  `set_delete_observer(...)`: требование снято, подписка не была
  реализована ни в одной версии, а удаление ловится diff-циклом —
  сессия исчезает из `list_sessions()`, и операция `cleanup_session_mirror`
  убирает её строки по своему `replica_id` после подтверждения
  (`missing_cycles_threshold`).
- **AND** удаление НЕ блокирует hot path: оно происходит в своём цикле.

#### Scenario: save_runtime_checkpoint и restore_sessions_to_workspace

- **WHEN** upstream `SessionManager.save_runtime_checkpoint(key)`
  или `restore_sessions_to_workspace(...)` вызывается
- **THEN** upstream JSONL-стор обновляется синхронно.
- **AND** `SessionMirror` подхватывает изменения
  в своём sync-цикле через `list_sessions()` /
  `read_session_metadata(...)` и зеркалирует в PG.

### Requirement: Single-writer per layer в session storage

Сессионное состояние (history, checkpoints, provider state)
MUST иметь ровно один hot-path writer — upstream
`SessionManager`. Холодное зеркало SHALL писать
`SessionMirror`,
NOT отдельным primary writer. Никаких "двойных записей"
hot-path данных в JSONL и PG одновременно — upstream JSONL
SHALL всегда писаться первым; PG SHALL обновляться
асинхронно через `SessionMirror`.

#### Scenario: Нет двойной записи в hot path

- **WHEN** агент вызывает `session_manager.save(session)`
- **THEN** upstream `SessionManager.save(session)` пишет в
  JSONL, и это единственный обязательный side-effect.
- **AND** запись в PG `agent_session_messages` ЗАПРЕЩЕНА
  в рамках этого вызова (mirror идёт отдельным
  `SessionMirror`-циклом).

#### Scenario: rename_model_preset делегируется в upstream

- **WHEN** пользователь меняет model preset для сессии через
  `session_manager.rename_model_preset(...)`
- **THEN** операция выполняется через upstream
  `SessionManager.rename_model_preset`.
- **AND** mirror-операция в PG идёт асинхронно через
  `SessionMirror` (не блокирует hot path).

#### Scenario: Равные updated_at при разных дайджестах

- **WHEN** `upstream_session.updated_at` ==
  `agent_session_meta.updated_at` в PG, но `source_digest` разошлись
- **THEN** `SessionMirror` SHALL зеркалировать сессию.
- **AND** tie-break по `>=`/`==` к меткам времени НЕ применяется: равенство
  меток не означает тождества содержимого. Атомарная перезапись JSONL через
  `os.replace` гарантирует согласованность ФАЙЛА, а не равенство метки
  времени и содержимого — `update_metadata` меняет содержимое, не трогая
  метку.
- **AND** вердикт операции несёт `reason="digest_only_change"`, чтобы
  отличать этот случай от обычного отставания зеркала.

#### Scenario: Удалённая upstream-сессия → diff-based cleanup в PG

- **WHEN** `SessionManager.delete_session(key)` удаляет
  upstream JSONL-сессию
- **THEN** в следующем sync-цикле
  `SessionMirror._sync_cycle()` сравнивает
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
- **AND** `SessionMirror` подхватывает обе:
  - если target отсутствует в PG → INSERT;
  - если source уже в PG → только проверка `updated_at`.

#### Scenario: restore_sessions_to_workspace

- **WHEN** `SessionManager.restore_sessions_to_workspace()`
  выполняется (например, при cold-start из бэкапа)
- **THEN** upstream JSONL восстанавливает несколько сессий
  одновременно; `list_sessions()` возвращает их все.
- **AND** `SessionMirror` обрабатывает каждую
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
- **AND** `SessionMirror` НЕ трогает
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

`SessionMirror` работает только с upstream-снимками из
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
  truth; `SessionMirror` зеркалирует его в PG.
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

`SessionMirror` SHALL использовать общий пул `utils.db`.
В модуле SHALL NOT быть собственных psycopg2-пулов,
`connect()` или `create_pool()`.

Полные правила (DI, advisory lock, threading, батчи, метрики,
shutdown order) — в `openspec/changes/archive/2026-09-27-storage-hybridization/design.md`
§ «Connection pool» (D-Pool). Здесь фиксируется только
нормативный контракт.

#### Scenario: Пул — единый, через DI

- **WHEN** `SessionMirror` обращается к PG
- **THEN** он использует `utils.db.transaction()` /
  `utils.db.run()` для всех операций.
- **AND** НЕ создаёт собственный `SimpleConnectionPool` /
  `psycopg2.pool` / `connect()` / `create_pool`.

#### Scenario: Разграничение реплик — данными, а не lock'ом

- **WHEN** `SessionMirror` начинает sync-цикл
- **THEN** он SHALL NOT брать advisory-lock: блокировки не
  переживают границу между вызовами платформы, из которых
  состоит цикл
- **AND** разграничение SHALL обеспечиваться `replica_id` в
  первичном ключе, а не блокировкой
- **AND** внутри одной реплики наложение итераций SHALL
  предотвращаться `asyncio.Lock`

#### Scenario: Пул исчерпан — цикл пропущен

- **WHEN** `utils.db.run(...)` бросает `RuntimeError` /
  `TimeoutError` / `PoolError` (пул временно исчерпан)
- **THEN** `SessionMirror` инкрементирует
  `cycles_skipped_pool_busy`, логирует
  `event_type="session_cold_sync_failed"` через
  `DbLoggingService.try_log_event` и ждёт следующий цикл
  через backoff.
- **AND** hot path НЕ затрагивается (sync работает в фоне).

#### Scenario: Батчи с сортировкой по session_key

- **WHEN** `SessionMirror._sync_batches()` обрабатывает
  upstream-список сессий
- **THEN** список сортируется по `session_key` ДО батчинга
  (детерминированный порядок блокировок — исключает ABBA-deadlock
  с `DbLoggingService`, которая тоже пишет в этот пул).
- **AND** размер батча ≤ `gateway.session_cold_sync.batch_size`
  (default `50`).
