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

> Правка 2026-10-04 — **только** тело сценария «PG недоступен — hot path
> работает»: имя события, которого не публикует ни один код. Нормативный текст
> и остальные четыре сценария перенесены дословно.
>
> **Это требование stale шире, чем эта правка.** Сценарий «Зеркалирование
> agent_session_messages (full re-read)» всё ещё показывает `utils.db.transaction()`
> и `SessionMirror._sync_cycle()`, которых в коде нет; «Single-flight защита от
> перекрытия циклов» задаёт backoff как `min(interval, interval * 2^n)` — код
> использует `max(interval, backoff)` с потолком `_BACKOFF_CAP_SEC`, и старый
> `min` в докстринге прямо назван ошибкой; «Метрики sync-сервиса» перечисляет 16
> метрик, из которых в `get_stats()` не осталось ни одной из упомянутых
> `pool_*` / `rows_synced_total` / `stale_*`. Всё это требует отдельной правки
> (`MODIFIED` с проверкой каждого имени метрики) и сюда не входит — см.
> «Известные смежные расхождения» в `proposal.md`. Заголовки сценариев
> сохранены все: архив отказывается выбрасывать сценарий из `MODIFIED`-блока.

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
- **AND** зеркало сессий публикует потерю sync-цикла как `agent.degraded`
  через `try_log_event` и отходит назад по времени до восстановления
  (без блокировки hot path)
  (`_log_failure()` и `_publish()` в `lib/gateway/mirror/mirror_poller.py`;
  счётчик `cycles_failed_total` и backoff — в `_run()` / `_compute_delay()`
  того же модуля).
- **AND** `event_type="session_cold_sync_failed"` MUST NOT упоминаться: его не
  публикует **ни один** код репозитория, а `DbLoggingService.log_sync_event`
  зеркалом не вызывается — вызов идёт через `try_log_event`. Имя при этом
  объявлено в каноне другого capability — `logging-db` (строки 1847 и 1873,
  отображение в `agent.degraded` через `name` и `metadata.cause`), то есть это
  **объявление без издателя**; закрывать его надо там, а не здесь (см.
  «Известные смежные расхождения» в `proposal.md`).
- **AND** обещание «расширение документируется в `CHANGELOG.md` под
  `## [Unreleased]`» MUST NOT оставаться: расширять нечего, раз событие не
  публикуется.

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
> уникальность по нему держит писатель — `data.mirror_session` удаляет все
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
  сессия исчезает из `list_sessions()`, и операция `data.cleanup_session_mirror`
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

#### Scenario: data.history_search читает из agent_gateway_logs

- **WHEN** агент вызывает операцию `data.history_search`
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

### Requirement: Подсистема зеркала собирается только вместе с клиентом платформы

Зеркало обращается к данным через операции платформы, поэтому клиент
`enterprise-mcp` — его обязательная зависимость. Сборка MUST происходить ПОСЛЕ
создания клиента: подсистема, собранная раньше, получает `None` вместо клиента и
выключается навсегда с текстом «платформа недоступна» — при платформе живой.

Отказ такого молчания MUST быть заметен: подсистема, которая ничего не пишет,
обязана сообщить причину (в баннере запуска и в журнале), а не выглядеть
работающей.

Порядок сборки MUST проверяться машинно
(`tests/test_mcp_health_wiring.py::TestWiring::test_mirror_and_monitor_are_built_after_the_client`).

#### Scenario: Клиент платформы не объявлен

- **WHEN** раздел `gateway.agent.enterprise_mcp` выключен или отсутствует
- **THEN** зеркало MUST не собираться, а причина MUST называться явно

### Requirement: Правила использования пула PG-соединений

`SessionMirror` SHALL обращаться к PostgreSQL только через операции платформы.
В модуле SHALL NOT быть ни собственного пула, ни `connect()`, ни `create_pool()`,
ни прямых обращений к `utils.db` из зеркала.

Полные правила (DI, advisory lock, threading, батчи, метрики,
shutdown order) — в `openspec/changes/archive/2026-09-27-storage-hybridization/design.md`
§ «Connection pool» (D-Pool). Здесь фиксируется только
нормативный контракт.

> Правка 2026-10-04. Прежняя первая фраза требования («SHALL использовать общий
> пул `utils.db`») описывала мир, в котором зеркало ходило в базу само. После
> перевода зеркала на операции платформы в модуле нет **ни одного** упоминания
> `utils.db` (поиск по `lib/gateway/mirror/*.py` — 0 совпадений), а пул
> обслуживает платформа. Вторая фраза («SHALL NOT быть собственных пулов»)
> остаётся в силе без изменений: запрет второго владельца пула как раз теперь и
> держится на отсутствии у зеркала доступа к базе.
>
> Заголовки сценариев сохранены все — архив отказывается выбрасывать сценарий из
> `MODIFIED`-блока. Правлены **тела** тех трёх, чьи посылки больше не верны, и
> добавлены новые.

#### Scenario: Пул — единый, через DI

- **WHEN** `SessionMirror` обращается к PostgreSQL
- **THEN** он MUST NOT обращаться к ней сам: доступ к данным идёт вызовами
  операций платформы (`session_mirror_state` на цикл, `mirror_session` на
  запись, `cleanup_session_mirror` на уборку), а пул обслуживает процесс
  платформы
  (`lib/gateway/mirror/session_mirror.py:77-80` — `MIRROR_OPERATIONS`;
  `lib/gateway/mirror/mirror_poller.py:446-451` — единая точка вызова `_call()`)
- **AND** он MUST NOT создавать `SimpleConnectionPool` / `psycopg2.pool` /
  `connect()` / `create_pool` и MUST NOT импортировать `utils.db` — это
  проверяемо целиком: поиск по `lib/gateway/mirror/*.py` даёт 0 совпадений
- **AND** сессия клиента MUST переиспользоваться между вызовами цикла, а не
  подниматься на каждый вызов операции
  (``_ensure_session()` в `lib/services/enterprise_mcp_client.py`` — одна живая сессия на
  процесс)

> Прежняя редакция требовала `utils.db.transaction()` / `utils.db.run()` «для
> всех операций». Таких операций у зеркала больше нет.

#### Scenario: Разграничение реплик — данными, а не lock'ом

- **WHEN** `SessionMirror` начинает sync-цикл
- **THEN** он SHALL NOT брать advisory-lock: блокировки не
  переживают границу между вызовами платформы, из которых
  состоит цикл
- **AND** разграничение SHALL обеспечиваться `replica_id` в
  первичном ключе, а не блокировкой
- **AND** внутри одной реплики наложение итераций SHALL
  предотвращаться `asyncio.Lock`
  (`lib/gateway/mirror/mirror_poller.py:182,340` — `asyncio.Lock` на цикл;
  advisory-lock не берётся нигде)

#### Scenario: Пул исчерпан — цикл пропущен

- **WHEN** обращение к данным зеркала бросает исключение (процесс платформы
  мёртв, отказ по коду, сеть недоступна)
- **THEN** `SessionMirror` инкрементирует `cycles_failed_total`, публикует
  `agent.degraded` уровнем WARN с классом исключения в тексте и ждёт следующий
  цикл через backoff
  (`lib/gateway/mirror/mirror_poller.py:302-312` — `except Exception` в петле,
  `_cycles_failed_total`, `_log_failure`; `:527-536` — публикация `agent.degraded`)
- **AND** задержка до следующего прохода MUST расти, а не приближаться к нулю,
  и MUST быть ограничена потолком
  (`:314-334` — `_compute_delay()`, `_BACKOFF_BASE_SEC = 1.0`,
  `_BACKOFF_CAP_SEC = 16 * 60.0`; прежний `min` при отказе стучался чаще
  штатного — 2 с против 30)
- **AND** hot path НЕ затрагивается (sync работает в фоне)
- **AND** счётчик `cycles_skipped_pool_busy`, класс `PoolError` и
  `event_type="session_cold_sync_failed"` MUST NOT упоминаться: пула, который
  мог бы быть исчерпан, у зеркала нет, а счётчик несуществующей причины — это
  отчётность о воображаемой поломке

#### Scenario: Батчи с сортировкой по session_key

- **WHEN** цикл проходит по сессиям зеркала
- **THEN** список MUST обходиться в отсортированном порядке по ключу сессии
  (`lib/gateway/mirror/mirror_poller.py:363-367` — `sorted(by_key)`)
- **AND** батчинг ради разграничения блокировок MUST NOT упоминаться: зеркало
  больше не берёт блокировок PostgreSQL, поэтому сортировка остаётся ради
  воспроизводимого порядка прохода, а не ради ABBA-deadlock с
  `DbLoggingService`. Общий пул исчез — исчезла и причина, по которой порядок
  был обязателен
- **AND** блокирующее чтение файлов MUST уходить в `asyncio.to_thread`, чтобы не
  занимать event loop шлюза, который обслуживает ещё и обороты
  (`lib/gateway/mirror/mirror_poller.py:201-207` — контракт `list_entries`;
  `:372` — `asyncio.to_thread(self.digest_of, …)`)

#### Scenario: Запрет второго владельца пула распространяется и на зеркало

- **WHEN** в проекте ищут, кто занимается владением пулом PG
- **THEN** зеркало MUST NOT числиться среди владельцев: перечень его операций
  MUST совпадать с тем, что платформа реально регистрирует, и каждая операция
  MUST быть `runtime-only`, то есть недоступна модели
  (`mcp-platform/servers/enterprise/capabilities/data/service/registry.py:62-64`
  — `JOB_AUDIENCE_RUNTIME`; страж
  `tests/test_session_mirror_wire.py::test_mirror_operation_is_registered_on_the_platform`,
  `::test_no_mirror_operation_is_left_unused`)
- **AND** имя таблицы зеркала MUST приходить от платформы, а не из настроек
  агента: оверлей профиля применяется на платформе, и агент, читавший имя из
  своей конфигурации, писал бы мимо контура
  (`lib/gateway/mirror/session_mirror.py:13-19` — имена таблиц живут только на
  платформе; тот же довод, по которому ушли канал и журнал)

### Requirement: Состояние зеркала запрашивается один раз на цикл, а не на сессию

Цикл зеркалирования MUST спрашивать состояние холодного зеркала своей реплики
**одним** вызовом `session_mirror_state`, а не по вызову на сессию. Без этого
синхронизация обязана была бы опрашивать зеркало по каждой сессии, чтобы узнать,
что она не изменилась: на сотнях сессий это сотни обращений в цикл, и ни одно из
них не меняло данных.

- **Один вызов — один SELECT.** Операция MUST отдавать по сессиям дайджест
  источника, метку и число строк одним запросом по своей реплике, а не собирать
  состояние вызовами на сессию
  (`session_mirror_state()` в `capabilities/data/service/main.py` — `SELECT session_key,
  source_digest, updated_at, message_count … WHERE replica_id = %s`).
- **Ответ — фильтр, а не решение.** Решение о записи MUST принимать
  `mirror_session`, перечитывая строку в своей транзакции. Поэтому
  рассинхронизация между состоянием и правдой MUST приводить только к лишнему
  вызову и MUST NOT приводить к записи устаревшего
  (`session_mirror_state()` в `capabilities/data/service/main.py` — докстринг).
- **Неизменившаяся сессия MUST не догоняться.** Совпадение дайджеста источника с
  дайджестом в состоянии MUST завершаться инкрементом счётчика пропусков без
  чтения источника и без вызова записи
  (`lib/gateway/mirror/mirror_poller.py:378-383`).
- Операция MUST оставаться `runtime-only`: её зовёт фоновая подсистема шлюза, а
  не модель (`mcp-platform/servers/enterprise/capabilities/data/service/registry.py:62-64`;
  permission `data:session_mirror_state`).

#### Scenario: Состояние реплики запрашивается один раз за цикл

- **WHEN** цикл зеркалирования обработал 200 сессий источника
- **THEN** операция `session_mirror_state` MUST быть вызвана **один** раз, с
  одним аргументом `replica_id`
  (`lib/gateway/mirror/mirror_poller.py:359-367` — один вызов, затем проход по
  `sorted(by_key)` с уже полученными данными)
- **AND** вызовов состояния по одной сессии на каждую сессию НЕ SHALL быть
  (проверяется
  `tests/test_session_cold_sync_service.py::TestCycle::test_unchanged_session_never_reaches_the_platform`:
  за цикл с одной сессией подряд вызовов ровно два — `session_mirror_state` и
  `cleanup_session_mirror`; при опросе на сессию к ним добавился бы вызов на
  каждую)

#### Scenario: Сессия не изменилась — цикл её не догоняет

- **WHEN** дайджест файла сессии совпал с `source_digest` в состоянии зеркала
- **THEN** цикл MUST инкрементировать `skipped_unchanged_total` и перейти к
  следующей сессии
  (`lib/gateway/mirror/mirror_poller.py:378-383`)
- **AND** чтение источника и вызов `mirror_session` по этой сессии MUST NOT
  выполняться: без этой проверки каждый проход читал и разбирал всё целиком
- **AND** при расхождении с состоянием цикл MUST читать источник и звать запись
  независимо от того, что было в ответе (`:385-393`)

#### Scenario: Устаревшее состояние не приводит к записи устаревшего

- **WHEN** между вызовом `session_mirror_state` и записью строка зеркала изменилась
- **THEN** запись MUST приниматься по решению `mirror_session`, перечитывающему
  строку в своей транзакции, а не по данным из ответа состояния
  (`session_mirror_state()` в `capabilities/data/service/main.py` — докстринг)
- **AND** последствие рассинхронизации MUST ограничиваться лишним вызовом, а
  невнесением устаревших данных

#### Scenario: Операция объявлена на платформе и не достаётся модели

- **WHEN** `SessionMirror` перечисляет свои операции
- **THEN** `session_mirror_state` MUST входить в их набор
  (`lib/gateway/mirror/session_mirror.py:77-80` — `MIRROR_OPERATIONS`)
- **AND** сверка MUST ловить случай, когда операция описана в коде агента, но
  отсутствует в реестре платформы
  (`tests/test_session_mirror_wire.py::test_mirror_operation_is_registered_on_the_platform`,
  `::test_no_mirror_operation_is_left_unused`)

### Requirement: Счёт пропусков ведёт платформа, порог приходит аргументом операции уборки

Порог уборки MUST доезжать до платформы **аргументом операции**
`delete_after_missed_cycles`, а инкремент пропусков и сравнение с порогом MUST
выполнять транзакция платформы по колонке `missing_cycles`. Настройка агента —
источник числа, а не владелец счётчика.

Поведение (не удалять по первому пропуску, не убирать по пустому списку) описано
сценарием «Удалённая сессия удаляется не с первого пропуска» требования
«Multi-instance изоляция через `replica_id` в ключе». Это требование фиксирует
**контракт операции**, а не повторяет поведение.

- Агент MUST передавать порог из своей настройки в вызов уборки вместе со
  списком присутствующих сессий и `replica_id`
  (`lib/gateway/mirror/mirror_poller.py:416-420`).
- Область MUST оставаться своей репликой: уборка MUST читать и удалять только
  строки с своим `replica_id`
  (`cleanup_session_mirror()` в `capabilities/data/service/main.py` — `SELECT` своей реплики и удаление; там же в докстринге зафиксировано
  прежнее вычитание по всем строкам при
  двух репликах уничтожало зеркала друг друга каждый цикл).
- Уборка MUST возвращать счётчики, по которым видно, что произошло
  (`cleanup_session_mirror()` в `capabilities/data/service/main.py` — возвращаемые счётчики): `scanned`, `missing`, `reappeared`, `deleted_sessions`,
  `deleted_keys`, `deleted_messages`.
- Операция MUST оставаться `runtime-only`
  (`registry.py:62-64`; permission `data:cleanup_session_mirror`).

#### Scenario: Порог доезжает аргументом, а не настройкой базы

- **WHEN** вызывающий уборку передаёт `delete_after_missed_cycles = 3`
- **THEN** операция MUST использовать ровно это значение как порог удаления
  (`mcp-platform/servers/enterprise/capabilities/data/tools/cleanup_session_mirror.py:32-51`;
  `cleanup_session_mirror()` в `capabilities/data/service/main.py` — `threshold = max(1, int(delete_after_missed_cycles))`)
- **AND** счётчик пропусков MUST расти и обнуляться в той же транзакции платформы,
  а не в памяти вызывающего: иначе перезапуск процесса обнулял бы историю
  пропусков и сессия удалялась бы при первом же пропуске после рестарта
  (`cleanup_session_mirror()` в `capabilities/data/service/main.py` — чтение `missing_cycles` из строки зеркала)

#### Scenario: Первый пропуск не удаляет, второй — удаляет

- **WHEN** сессия отсутствует в `present_keys` при пороге 2
- **THEN** после первого пропуска её строки MUST остаться, счётчик — вырасти на 1
  (`cleanup_session_mirror()` в `capabilities/data/service/main.py` —
  `if int(missed) >= threshold`)
  (проверяется
  `mcp-platform/tests/test_session_mirror_operations.py::TestCleanup::test_single_miss_does_not_delete`)
- **AND** после второго пропуска подряд её строки MUST быть удалены по обоим
  слоям — метаданных и сообщений
  (`::test_second_miss_deletes`; `cleanup_session_mirror()` в `capabilities/data/service/main.py` — путь `doomed`)
- **AND** вернувшаяся сессия MUST обнулить свой счётчик и уборкой не затрагиваться
  (`cleanup_session_mirror()` в `capabilities/data/service/main.py` — сброс `missing_cycles`; `::test_reappearing_session_resets_the_counter`)

#### Scenario: Уборка не выходит за свою реплику

- **WHEN** в зеркале есть строки другой реплики
- **THEN** уборка MUST читать и удалять только строки с своим `replica_id`
  (`cleanup_session_mirror()` в `capabilities/data/service/main.py`;
  `::test_cleanup_never_reaches_another_replica`, `::test_cleanup_reads_only_own_rows`)
- **AND** отсутствие `replica_id` MUST отвергаться, а не означать «всё зеркало»
  (`cleanup_session_mirror()` в `capabilities/data/service/main.py` — отказ без `replica_id`; `::test_cleanup_requires_a_replica_id`)

#### Scenario: Пустой список источника — решение вызывающего, зеркало не тронуто

- **WHEN** список присутствующих сессий пуст, а зеркало реплики не пусто
- **THEN** вызывающий MUST не звать уборку вовсе и доложить причину
  (`lib/gateway/mirror/mirror_poller.py:401-414` — инкремент
  `cleanup_guarded_total` и событие `agent.degraded` уровнем WARN)
- **AND** решение MUST оставаться за вызывающим, а не за платформой: пустой
  список с диска бывает не «потому что всё удалили», а потому что каталог
  недоступен, и молчаливая уборка всего зеркала на таком сбое стоила бы месяцев
  переписки
  (`mcp-platform/servers/enterprise/capabilities/data/tools/cleanup_session_mirror.py:6-11`)

### Requirement: Отказ по объёму вызова зеркала громкий, а не усечение

Верхняя граница объёма одного вызова зеркала MUST быть объявлена константой
платформы и проверяться **до** записи. Сессия, которая в границу не помещается,
MUST отклоняться с названным порогом, а не усекаться молча.

- Граница MUST быть не «сколько влезает», а порогом, за которым вызывающий
  обязан перейти на постраничную запись: одна операция на сессию — это один
  аргумент вызова MCP, и очень большая сессия рано или поздно упрётся в память
  процесса платформы
  (`MIRROR_MAX_PAYLOAD_BYTES` в `capabilities/data/service/main.py` —
  `MIRROR_MAX_PAYLOAD_BYTES`, 8 × 1024 × 1024).
- Проверка MUST идти по фактическому телу вызова — байты сериализованных
  сообщений в UTF-8, — а не по оценке числа сообщений
  (`mirror_session()` в `capabilities/data/service/main.py`).
- Отказ MUST называть сессию и порог и MUST указывать, что увеличение порога —
  осознанное решение оператора
  (`mirror_session()` в `capabilities/data/service/main.py` — отказ по объёму).

#### Scenario: Сессия не помещается в один вызов — отказ, а не усечение

- **WHEN** сообщения сессии сериализуются в больше 8 МБ
- **THEN** `mirror_session` MUST отклонить вызов с `InvalidRequestError`, назвав
  ключ сессии и порог
  (`mirror_session()` в `capabilities/data/service/main.py` — отказ)
- **AND** молчаливое усечение сессии MUST NOT происходить: оборванная сессия в
  холодном хранилище неотличима от целой и обнаруживается только при
  восстановлении
  (проверяется
  `mcp-platform/tests/test_session_mirror_operations.py::TestInputGuards::test_oversized_payload_is_refused_by_name`)

#### Scenario: Граница проверяется до записи, а не после

- **WHEN** вызов не превышает порог
- **THEN** запись MUST выполняться обычным порядком
  (проверка стоит до выборки прежней строки и до любого `INSERT`/`UPDATE` —
  `mirror_session()` в `capabilities/data/service/main.py` — проверка стоит раньше любой выборки и любой записи)
- **AND** отказ по объёму MUST быть того же класса, что и отказ по
  неразбираемому `updated_at`: негодный вызов отвергается целиком, а не
  приводится в частично записанное состояние
  (`mirror_session()` в `capabilities/data/service/main.py` — отказ по `updated_at`; `::TestInputGuards::test_unparsable_timestamp_is_refused`)


## Responsibility

Спека отвечает за **распределение двух слоёв хранения сессий** и за
границу между ними. Ключевое, что следует из чтения кода: предмет — это не
«агент пишет сессии в PostgreSQL», а строго обратное утверждение.

Hot path принадлежит upstream-библиотеке: единственный writer сессий —
`nanobot.session.manager.SessionManager`, а наш вклад — `SessionStore`,
который санитизирует NUL на границе записи
(`lib/session/pg_session_manager.py:71-82`). PostgreSQL — **cold-storage
mirror** в отдельной подсистеме шлюза `lib/gateway/mirror/`, которая
работает вне оборота.

Четыре обязанности:

1. hot path сессий — только upstream `SessionManager`;
2. зеркало сессий в PostgreSQL как единственный потребитель
   cold-storage;
3. изоляция реплик через `replica_id` в ключе — данными, а не блокировкой;
4. single-writer per layer: ни одного прямого `INSERT`/`UPDATE` в таблицы
   сессий из агентского кода.

Владелец по `## Scope` — `agent`. Но запись в PostgreSQL выполняет **не
агент**, а платформа: зеркало ходит в операции `data.mirror_session`,
`data.cleanup_session_mirror`, `data.session_mirror_state`
(`lib/gateway/mirror/session_mirror.py:77-80`). Ни одного прямого SQL в
зеркале нет — это следствие, которое стоит знать до чтения любого
требования про «зеркалирование в PG».

## Boundary

**Граница — процесс агента (шлюз).** Зеркало — фоновая задача этого
процесса, и агент о ней не знает: у него нет ни одного вызова из оборота,
зеркала нет в его инвентаре
(`lib/core/application_context.py:1707-1708`).

Внутри границы:

- сборка и одновременный hot path — `lib/session/pg_session_manager.py`;
- зеркало — `lib/gateway/mirror/mirror_poller.py` (механизм) и
  `lib/gateway/mirror/session_mirror.py` (ресурс «сессии»);
- жизненный цикл — `gateway.py:607-616`;
- сборка из конфигурации — `lib/core/application_context.py:1713-1754`;
- DDL — `sql/migrations/V010__agent_session_mirror_replica_key.sql`,
  `sql/migrations/V011__agent_session_mirror_indexes.sql`.

Вне границы:

- **операции записи в PostgreSQL.** Зеркало вызывает
  `data.mirror_session` / `data.cleanup_session_mirror` /
  `data.session_mirror_state` (`session_mirror.py:77-80`); транзакции,
  `upsert`, `missing_cycles` и уборка выполняет платформа;
- **сами таблицы** `agent_session_meta` / `agent_session_messages` — их
  владелец платформа, агент только объявляет форму через аргументы;
- **`agent_gateway_logs` и `agent_conversation_messages`** — соседние
  таблицы; аудит-тень в них независима от зеркала (см. требование
  «Audit shadow в PG независим от session mirror»);
- **разбор документа** и прочие capability — не сессии.

## Public Contract

**Два наблюдаемых интерфейса.**

**1. Хот-путь** — `build_session_manager(workspace)`
(`lib/session/pg_session_manager.py:85`). Возвращает класс библиотеки
`SessionManager`, а не подкласс. Возвращаемый объект публичен по
upstream-API: `get_or_create`, `save`, `add_message`, `delete_session`,
`update_session_metadata`, `read_session_snapshot`, `list_sessions`,
`save_runtime_checkpoint`, `restore_sessions_to_workspace`.

Единственная своя семантика — `SanitizingSessionStore`
(`pg_session_manager.py:71-82`): `save()` сперва вычищает контент всех
сообщений, потом отдаёт запись базовому классу. Остальные примитивы
протокола (`load`/`delete`/`read`/`read_metadata`/`update_metadata`/
`list_sessions`) наследуются без изменений.

**2. Зеркало** — `SessionMirror` (`session_mirror.py:121`), наследник
`MirrorPoller` (`mirror_poller.py:126`). Публичная поверхность:

| Элемент | Где | Смысл |
|---|---|---|
| `list_entries()` | `session_mirror.py:174` | сессии, которые сейчас есть на диске |
| `digest_of(entry)` | `session_mirror.py:190` | SHA-256 файла-сессии |
| `read_source(key)` | `session_mirror.py:195` | снимок сессии целиком |
| `build_write_arguments(...)` | `session_mirror.py:198` | девять аргументов операции `mirror_session` |
| `after_write(key, result)` | `session_mirror.py:218` | счётчики по вердикту платформы |
| `extra_stats()` | `session_mirror.py:230` | ресурсные счётчики в `get_stats()` |
| `get_stats()` | `mirror_poller.py:553` | 17 ключей наблюдаемости |
| `start()` / `stop(timeout_sec=30.0)` | `mirror_poller.py:272` / `281` | жизненный цикл |
| `enabled` / `replica_id` / `disabled_reason` | `mirror_poller.py:258-268` | свойства |

Три class-level константы, объявленные как единственный источник правды о
том, какие операции платформы использует зеркало
(`session_mirror.py:77-80`):

```
OP_MIRROR   = "data.mirror_session"
OP_CLEANUP  = "data.cleanup_session_mirror"
OP_STATE    = "data.session_mirror_state"
MIRROR_OPERATIONS = (OP_MIRROR, OP_CLEANUP, OP_STATE)
```

## Inputs

**Хот-путь:** сообщения оборота → upstream `SessionManager.save(...)` →
`SanitizingSessionStore.save()` → JSONL-файл на диске. Санитизация
выполняется на контенте всех сообщений
(`pg_session_manager.py:61-68`); мусор (не-`dict` сообщения, отсутствие
атрибута `messages`) пропускается.

**Зеркало:**

- список сессий — `self._session_manager.list_sessions()`
  (`session_mirror.py:183`);
- дайджест файла — `file_digest(locator)`; `None`, если файл сейчас
  пригоден (`session_mirror.py:190-193`);
- снимок сессии — `read_session_snapshot(key)`
  (`session_mirror.py:196`);
- девять аргументов записи (`session_mirror.py:205-216`):
  `session_key`, `replica_id`, `source_digest`, `updated_at`,
  `created_at`, `last_consolidated`, `metadata`, `messages`,
  плюс пороги `stale_tolerance_seconds` и
  `sync_lag_threshold_seconds`;
- аргументы уборки (`mirror_poller.py:420-424`): `replica_id`,
  `present_keys`, `delete_after_missed_cycles`.

**Личность служебного вызова** — отдельный вход, который легко пропустить:
зеркало подписывает свои вызовы платформы служебной личностью, а не
пользовательской (`mirror_poller.py:433-448`):
`session_id = "session-mirror:<replica_id>"`, `user_id = SERVICE_USER`.
Без подписи сервер ответил бы отказом `identity_missing`, потому что
зеркало работает вне оборота.

## Outputs

**Хот-путь:** JSONL-файл сессии. PostgreSQL в этот путь **не входит**.

**Зеркало:**

- строки в `agent_session_meta` / `agent_session_messages` — через
  операцию платформы `data.mirror_session`, с вердиктом в ответе;
- удалённые строки зеркала при уборке — по
  `delete_after_missed_cycles` (дефолт 2, `mirror_poller.py:167`);
- события журнала через `_publish` (`mirror_poller.py:485`): отказ цикла
  (`agent.degraded`, `mirror_poller.py:534`), удаление строки
  (`mirror_poller.py:545`), пропуск уборки на пустом источнике
  (`mirror_poller.py:408-417`).

**Наблюдаемость** — `get_stats()` (`mirror_poller.py:553-582`) с
17+ ключами: `resource`, `enabled`, `disabled_reason`, `replica_id`,
`cycles_total`, `cycles_failed_total`, `consecutive_failures`,
`last_cycle_seconds`, `last_success_ts`, `last_success_lag_seconds`,
`source_count`, `mirror_count`, `written_total`,
`skipped_unchanged_total`, `unreadable_total`, `source_missing_total`,
`cleanup_guarded_total`, `deleted_total`, `sync_interval_sec`,
`missing_cycles_threshold`, плюс `extra_stats()` ресурса —
`messages_written_total`, `skipped_stale_total`,
`stale_tolerance_seconds`, `sync_lag_threshold_seconds`
(`session_mirror.py:230-235`).

Что важно про форму счётчиков: `skipped_unchanged_total` растёт на
каждом проходе без изменений — это самый частый исход, и он специально
сделан дешёвым: проверка дайджеста происходит **до** чтения источника и
до обращения к данным (`mirror_poller.py:382-387`).

## State

Состояние предмета разнесено по трём уровням, и это важно для чтения
инвариантов.

**1. Хот-путь** — JSONL-файлы сессий под управлением библиотеки. Своё
состояние у агента отсутствует: агент не кеширует сессии и не держит
открытых дескрипторов.

**2. Cold-storage** — таблицы `agent_session_meta` /
`agent_session_messages`. Ключ — **`(replica_id, session_key)`**, а не
`session_key`: первичный ключ переопределён в `V010`, а `replica_id`
сделан `NOT NULL`. Причина зафиксирована в шапке миграции: составной ключ
упорядочивает строки и разводит реплики по разным страницам индекса, то
есть упирается не в «последнюю запись победит», а в блокировки и
очередь обслуживания.

Неочевидное свойство схемы, взятое из комментариев `V010` к колонкам и
следующее из них как требование:

- `source_digest` — **основной** признак неизменности, а не `updated_at`:
  upstream не поднимает `updated_at` при изменении только `metadata`
  (`JsonlSessionStore.update_metadata` переписывает лишь поля первой
  строки);
- `message_count` — счётчик строк зеркала, проверенный по факту: расхождение
  обнаруживается **до** и после, и полное переписывание запускается при
  нём;
- `missing_cycles` — счётчик подряд пропущенных списков; ноль приводит к
  первому пропуску без удаления, что спасает от каталога, который создаётся
  заново при каждом запуске (сеть, каталог на NFS);
- `synced_at` — момент последней записи. Поле намеренно **не** значит
  «данные не менялись»: пока обновлён `updated_at`, счётчик не
  различается.

**3. В памяти процесса** — счётчики `MirrorPoller` (строки 189-201
`mirror_poller.py`) и `_cycle_lock: asyncio.Lock`
(`mirror_poller.py:186`). Блокировка одна на весь цикл, и она
single-flight: второй цикл, стартовавший параллельно, ждёт, а не
дублирует запись.

## Dependencies

Прямые, в дереве агента:

- `nanobot.session.manager.SessionManager`, `JsonlSessionStore`,
  `Session` — хот-путь и стор (`pg_session_manager.py:44`);
- `nanobot` `SessionStore` — `Protocol`, объявленный библиотекой
  (`nanobot/session/manager.py:526`), и `store=` в конструкторе
  `SessionManager` (строка 1647 по комментарию модуля);
- `workspace.utils.clean_text.clean_text` — санитизация NUL
  (`pg_session_manager.py:46`);
- `lib.gateway.mirror.MirrorPoller` — механизм зеркала;
- `lib.services.enterprise_mcp_client.CallIdentity` — личность служебного
  вызова (`mirror_poller.py:443`, импорт ленивый);
- `lib.core.application_context.ApplicationContext` — сборка и владение
  (`ctx.session_manager`, `ctx.enterprise_mcp`,
  `ctx.db_logging_service`);
- пул PostgreSQL — общий, через DI; зеркало берёт его на платформе, а не
  создаёт свой.

Платформенные зависимости (вызываемые операции): `data.mirror_session`,
`data.cleanup_session_mirror`, `data.session_mirror_state`. Их состав
объявлен одной константой `MIRROR_OPERATIONS`
(`session_mirror.py:80`), и сверка её с тем, что платформа реально
регистрирует, — работа теста `tests/test_session_mirror_wire.py`
(`session_mirror.py:76`).

Принципиальная асимметрия: `SessionManager.save_runtime_checkpoint`
деградирует до полной перезаписи транскрипта, если `self._store is not
self._jsonl_store`. Наш `build_session_manager` поэтому одной строкой
возвращает идентичность: `manager._jsonl_store = store`
(`pg_session_manager.py:106`). Без неё каждая безопасная точка
восстановления молча переписывала бы транскрипт целиком.

## Configuration

Секция `gateway.session_cold_sync`, разбирается в
`SessionColdSyncSettings` (`lib/core/project_settings.py:157-173`) и
собирается в `_build_session_mirror`
(`lib/core/application_context.py:1716-1754`).

| Ключ | Дефолт в коде | Значение в `config.json` | Используется? |
|---|---|---|---|
| `enabled` | `True` | `true` (строка 708) | да |
| `sync_interval_sec` | `30.0` | `30.0` (709) | да, приводится к `≥ 1.0` |
| `stale_tolerance_seconds` | `120` | `120` (711) | да |
| `sync_lag_threshold_seconds` | `3600` | `3600` (712) | да |
| `missing_cycles_threshold` | `2` | **не задан** | да, приводится к `≥ 1` |
| `replica_id` | `None` → имя хоста | **не задан** | да |
| `batch_size` | — | `50` (710) | **нет** |

Валидация при сборке: `sync_lag_threshold_seconds` обязан быть
`≥ stale_tolerance_seconds`, иначе `ValueError` на старте
(`session_mirror.py:166-170`). Это единственная проверка, которая
выполняется до фоновой работы.

Расхождение, найденное при сверке с кодом и требующее внимания:
**`batch_size` объявлен и настроен, но не читается.** Поиск по `lib/`
показывает, что единственное чтение `batch_size` — это
`channels.postgres.batch_size` для `DbLoggingService`
(`lib/core/application_context.py:1608`); зеркало его не читает, а метода
`_sync_batches` в коде нет. Синхронизация идёт по одной сессии
(`_sync_one`, `mirror_poller.py:373`), порядок детерминирован сортировкой
по ключу (`mirror_poller.py:367-371`). Требование «размер батча ≤
`batch_size`» в текущем коде не имеет точки исполнения; это зафиксировано
в `## Forbidden Behavior` и `## Invariants`.

## Lifecycle

**Сборка** — `_build_session_mirror(ctx)`
(`lib/core/application_context.py:1713-1754`). Возвращает `None`, если
`ctx.session_manager is None`. Собирается **после** клиента платформы
(шаг 7a-0), потому что от него зависит; раньше это было ошибкой сборки.

**Выключение по умолчанию** при отсутствии клиента: если
`enterprise_mcp is None`, зеркало выключается с причиной
«платформа недоступна (enterprise_mcp не задан)»
(`mirror_poller.py:178-180`). Это отказ «мягкий»: процесс поднимается и
работает, а не падает.

**Старт** — `gateway.py:607-616`, строго после подъёма каналов и после
рукопожатия с платформой: клиент, чья сессия только что поднялась, и
есть тот, кто зеркало использует. При `enabled == False` печатается
строка `session_mirror: выключено (<причина>)` (`gateway.py:611-614`).
Исключение при старте ловится и логируется предупреждением
(`gateway.py:615-616`) — неуспех зеркала не роняет шлюз.

**Цикл** (`_run`, `mirror_poller.py:306-316`): `_cycle()` под
`_cycle_lock`, затем сон `_compute_delay()`. Исключение внутри цикла не
убивает задачу: `_consecutive_failures` растёт, отказ логируется, цикл
продолжается.

**Задержка между проходами** (`_compute_delay`, `mirror_poller.py:318-338`)
— экспоненциальный backoff: `_BACKOFF_BASE_SEC * 2 ** min(failures, 32)`,
прижатый между `sync_interval_sec` и `_BACKOFF_CAP_SEC` (16 минут,
`mirror_poller.py:56`). Направление намеренное: отказ **отодвигает**
следующую попытку. Раньше стоял `min`, и при отказе цикл стучался чаще
обычного (2 с против штатных 30) — год отказа выглядел бы как усердная
работа.

**Финальный проход** (`stop`, `mirror_poller.py:281-304`): задача
отменяется, затем выполняется **ещё один** `_cycle()` с тем же таймаутом.
Финальный проход обязателен и идёт **до** закрытия источника: это
последний шанс догнать то, что изменилось за время работы. Ошибка при
остановке логируется и не роняет выход.

## Data Ownership

Владение здесь главный предмет спеки, и оно разделено строго.

| Данные | Единственный writer | Что делает зеркало |
|---|---|---|
| JSONL-файлы сессий (hot path) | upstream `SessionManager` | читает, **не пишет** |
| `agent_session_meta` | платформа, операция `data.mirror_session` | инициирует запись аргументами |
| `agent_session_messages` | платформа, тот же вызов | инициирует запись |
| `agent_gateway_logs` | `DbLoggingService` | публикует свои события через `_publish` |

Правило, которое спека делает нормативным: **ни одного прямого
`INSERT`/`UPDATE` в таблицы сессий из агентского кода.** Проверяется
стражем `tests/test_storage_hybridization.py::TestNoDirectSQLToSessionTables`
(назван в докстринге `pg_session_manager.py:31-32`).

Второе правило — про разграничение реплик: оно обеспечивается **данными**,
а не блокировкой. Ключ `(replica_id, session_key)` и `NOT NULL` на
`replica_id` означают, что реплики не конкурируют за одну строку вообще,
поэтому гонки за файл не существует и обрабатывать её не нужно.

Третье правило — про раздельность хранения и аудита: зеркало сессий и
аудит-тень независимы. `agent_conversation_messages` (заметки о сжатии
контекста) и `agent_gateway_logs` (событие `context_compacted`) пишутся
не зеркалом, а `ContextCompactionService`; удаление сессии из хот-пути не
обязано удалять аудит, и наоборот.

## Error Behavior

Отказы зеркала **не поднимаются в агент и не видны модели**: это фоновая
подсистема, у которой нет вызова из оборота. Все они логируются и
пересчитываются.

**1. Платформа недоступна** → зеркало выключено с причиной
(`mirror_poller.py:178-180`), процесс работает без cold-storage.

**2. Отказ цикла** (`_run`, `mirror_poller.py:312-315`): исключение
ловится, `_consecutive_failures` и `cycles_failed_total` растут, отказ
логируется (`agent.degraded`, `mirror_poller.py:531-534`), цикл
продолжается с backoff. Цикл не умирает.

**3. Отказ отдельной записи** (`_sync_one`): исключение поднимается в
цикл, то есть трактуется как отказ цикла, — платформа может отказать по
своим причинам, и это должно быть видно.

**4. Нечитаемый источник** (`mirror_poller.py:376-380`): `digest is None` →
`_unreadable_total += 1` и возврат без угадывания. Единица прямо сейчас
переписывается либо недоступна; гадать нельзя.

**5. Источник исчез между списком и чтением**
(`mirror_poller.py:389-392`): `source is None` →
`_source_missing_total += 1`, возврат.

**6. Нечитаемый ответ платформы** (`_call`, `mirror_poller.py:470-480`):
`json.loads` не дал словаря → `ValueError` с указанием операции и
причины, а не молчание. Нечитаемый ответ хуже отсутствия ответа,
потому что выглядит как пустой успех.

**7. Отказ при остановке** (`stop`, `mirror_poller.py:297-304`):
таймаут финального прохода или исключение логируются; выход не роняется.

**8. Некорректная конфигурация** — единственный отказ, который
**поднимается на старте**: `sync_lag_threshold_seconds <
stale_tolerance_seconds` → `ValueError`
(`session_mirror.py:166-170`). Молча чинить нельзя: два порога задают
противоречивый смысл «устарело» и «отстало по времени».

**9. Пустой список источника** (`_cleanup_absent`,
`mirror_poller.py:403-418`): уборка **пропускается**, если список пуст и
`guard_empty_source()` истинно, при этом публикуется `agent.degraded` с
прежним количеством строк зеркала. Каталог может оказаться пустым не
потому, что данные исчезли, а потому, что он не подмонтирован или сорван;
«удалить всё» из такого состояния — потеря данных.

## Invariants

1. **Hot path принадлежит библиотеке.** Единственный writer сессий —
   upstream `SessionManager`; агент не пишет в `agent_session_meta` /
   `agent_session_messages` напрямую.
2. **Нет прямого SQL к таблицам сессий** из агентского кода — все записи
   идут операциями платформы.
3. **Ключ холодного хранения — `(replica_id, session_key)`**, а
   `replica_id` — `NOT NULL` (`V010`). Изоляция реплик обеспечена
   данными, а не lock'ом.
4. **`replica_id` не выводится из ничего не значащего.** Без явного
   значения берётся `default_replica_id()` — имя хоста
   (`mirror_poller.py:172`); выдуманное значение писало бы в журнал чужую
   машину.
5. **Служебный вызов подписывается служебной личностью**
   (`mirror_poller.py:433-448`), а не личностью пользователя, и реплика
   входит **в имя сессии**, а не теряется в ней: следы двух машин должны
   различаться в журнале.
6. **Дайджест — основной признак неизменности, а не `updated_at`**
   (комментарий к колонке в `V010`). Равные `updated_at` при разных
   дайджестах — это расхождение, а не «ничего не изменилось».
7. **Размер выборки детерминирован** по `updated_at`, а `message_count`
   используется для обнаружения расхождения.
8. **Один цикл за раз** — `_cycle_lock` (`mirror_poller.py:186`,
   `344`). Перекрывающиеся циклы запрещены.
9. **Порядок обработки детерминирован** — сортировка по ключу до обхода
   (`mirror_poller.py:367-371`). Это исключает ABBA-порядок блокировок с
   `DbLoggingService`, которая пишет в тот же пул.
10. **Уборка не удаляет по первому пропуску** — `missing_cycles`
    отсчитывает подряд пропуски, а `delete_after_missed_cycles` передаётся
    платформе.
11. **Уборка запрещена на пустом источнике** (`guard_empty_source`,
    `mirror_poller.py:243-250`): пустой список — не «всё удалили».
12. **Проверка дайджеста предшествует чтению источника**
    (`mirror_poller.py:382-387`): неизменившаяся сессия не должна стоить ни
    чтения, ни обращения к данным.
13. **Отказ отодвигает следующую попытку**, а не приближает её
    (`_compute_delay`, `mirror_poller.py:318-338`).
14. **Финальный проход при остановке обязателен** и идёт до закрытия
    источника (`mirror_poller.py:281-304`).
15. **`batch_size` не участвует в работе зеркала.** Ключ объявлен
    (`project_settings.py:171`) и настроен (`config.json:710`), но
    зеркалом не читается, метода `_sync_batches` нет; обработка идёт по
    одной сессии. Любая будущая реализация батчинга обязана сохранить
    инварианты 8 и 9.
16. **Санитизация стоит на границе записи, а не в патче.** NUL вычищается
    в `SanitizingSessionStore.save()`, рядом с потребителем; PostgreSQL не
    принимает NUL в text-литералах (`pg_session_manager.py:15-21`).

## Forbidden Behavior

1. **Писать в `agent_session_meta` / `agent_session_messages` из агента**
   (прямой SQL, `INSERT`, `UPDATE`, свой writer). Нарушение
   single-writer, который держит страж
   `tests/test_storage_hybridization.py::TestNoDirectSQLToSessionTables`.
2. **Возвращать к прямой записи в PostgreSQL как в хот-путь.** PostgreSQL —
   cold-storage; выигрыш в скорости даёт JSONL.
3. **Хранить сессии в PostgreSQL, отказав в JSONL.** Утверждается, что
   миграция выполнена без UX-пробела («UX-gap без legacy-зеркала»); отказ
   от неё означает потерю транскриптов.
4. **Делать `batch_size` фактором поведения без реализации батчинга.**
   Ключ настроен (`config.json:710`), но не читается; объявлять его
   действующим — то же, что обвинить его в том, чего он не делает.
5. **Делать уборку на пустом списке источника.** Неподмонтированный каталог
   выглядит как «удалено всё».
6. **Удалять строку зеркала с первого пропуска** — счётчик
   `missing_cycles` существует именно для этого.
7. **Сортировать порядок обхода недетерминированно** (порядок каталога,
   `list` без сортировки): это возврат ABBA-риска с общим пулом.
8. **Запускать два цикла одновременно** — single-flight обязателен.
9. **Читать источник до проверки дайджеста** — самый частый исход обязан
   стоить почти ничего.
10. **Приближать интервал при отказе** (`min` вместо `max` в
    `_compute_delay`): год отказа зеркала выглядел бы как усердная
    работа.
11. **Подписывать служебные вызовы личностью пользователя** — следы зеркала
    попали бы в область видимости чужого сеанса.
12. **Терять `replica_id` внутри имени сессии служебного вызова** —
    `mirror` чужой реплики перестал бы отличаться от своего.
13. **Угадывать при отсутствии дайджеста** («наверное, не изменилось»).
14. **Проглатывать нечитаемый ответ платформы** — вместо этого
    `ValueError` с указанием операции.
15. **Пропускать финальный проход при остановке.**
16. **Приводить `stale_tolerance_seconds` выше
    `sync_lag_threshold_seconds` «по значению»:** это
    противоречивая конфигурация и она обязана падать на старте, а не
    молча переопределяться.
17. **Смешивать зеркало сессий с аудитом:** удаление сессии не должно
    трогать `agent_conversation_messages` или `agent_gateway_logs`.
18. **Возвращать санитизацию в патч** `patch_session_content_cleanup`:
    санитизация живёт в `SessionStore`, рядом с потребителем.

## Consumers

| Потребитель | Что использует | Где |
|---|---|---|
| upstream `AgentLoop` и каналы | `build_session_manager(...)` → хот-путь | `lib/session/pg_session_manager.py:85` |
| `ApplicationContext` | `ctx.session_manager` как источник зеркала | `lib/core/application_context.py:1713` |
| `gateway.py` | `mirror.start()` / выключено-строка | `gateway.py:607-616` |
| Платформа `enterprise-mcp` | операции `data.mirror_session`, `data.cleanup_session_mirror`, `data.session_mirror_state` | `session_mirror.py:77-80` |
| `runtime_health` | `get_stats()` зеркала | `lib/services/runtime_health.py:134-137` |
| `docs/architecture/storage-layers.md` | модель хранения | упомянут в `pg_session_manager.py:37` |
| Администратор | счётчики `get_stats()` в живности | — |

Потребитель, который стоит выделить отдельно: **модель агента зеркалом не
пользуется.** У него нет ни одного вызова из оборота, и зеркала нет в его
инвентаре (`lib/core/application_context.py:1707-1708`). Это осознанное
решение, а не пробел: обратное означало бы, что агент может читать
холодное хранилище иначе, чем через штатные операции платформы.

## Implementation

Все пути проверены `Test-Path`; все существуют.

**Хот-путь:**

- `lib/session/pg_session_manager.py` — `build_session_manager` (85),
  `SanitizingSessionStore` (71), `clean_session_content` (49);
- `workspace/utils/clean_text.py` — санитизация.

**Зеркало:**

- `lib/gateway/mirror/mirror_poller.py` — `MirrorPoller` (126),
  `default_replica_id` (94), `MirrorEntry` (108), константы
  `_BACKOFF_BASE_SEC` (55) и `_BACKOFF_CAP_SEC` (56),
  `SERVICE_SESSION_PREFIX` (74), `SERVICE_USER` (80);
- `lib/gateway/mirror/session_mirror.py` — `SessionMirror` (121),
  `OP_MIRROR`/`OP_CLEANUP`/`OP_STATE`/`MIRROR_OPERATIONS` (77-80),
  `file_digest` (83), `_isoformat` (113);
- `lib/gateway/mirror/__init__.py` — публичный экспорт пакета.

**Сборка и жизненный цикл:**

- `lib/core/application_context.py` — `_build_session_mirror`
  (1713-1754), вызов на шаге 7a-0, `ctx.session_mirror` (196);
- `./gateway.py` — старт зеркала (607-616);
- `lib/core/project_settings.py` — `SessionColdSyncSettings` (157-173);
- `./config.json` — секция `gateway.session_cold_sync` (707-713).

**DDL:**

- `sql/migrations/V010__agent_session_mirror_replica_key.sql` —
  `replica_id`, backfill, `NOT NULL`, новый первичный ключ, комментарии к
  колонкам, управляемое удаление `legacy`-строк через
  `nanobot.cleanup_legacy_session_mirror`;
- `sql/migrations/V011__agent_session_mirror_indexes.sql` — индексы под
  access-pattern зеркала;
- `sql/migrations/V013__test_profile_session_mirror_replica_key.sql` —
  тот же ключ в тестовом профиле.

**Документация:** `docs/architecture/storage-layers.md` — общая модель
хранения, упомянута в докстринге `lib/session/pg_session_manager.py:37`.

Чего в дереве **не существует**:
`lib/services/session_cold_sync_service.py` (в комментарии миграции `V010`
упомянуто имя `SessionColdSyncService._cleanup_missing`
как историческое — реализация называется `MirrorPoller`), а также
`lib/session/pg_session_manager.py`-подобного файла, который писал бы
сессии в PG напрямую.

## Verification

Все пути проверены `Test-Path`; все существуют.

**Инвариант «нет прямого SQL»:**

- `tests/test_storage_hybridization.py` — основной страж слоёв хранения;
  класс `TestNoDirectSQLToSessionTables` закрывает запрет прямых
  `INSERT`/`UPDATE` в таблицы сессий;
- `tests/test_pg_session_manager.py` — поведение `SanitizingSessionStore`
  и `build_session_manager`;
- `tests/test_session_storage.py` — хот-путь целиком;
- `tests/contract/test_session_manager.py`,
  `tests/contract/test_session_manager_api.py`,
  `tests/contract/test_session_manager_contract.py` — контракт
  upstream-API `SessionManager` и `JsonlSessionStore`;
- `tests/contract/test_session_dir_name_contract.py` — имя каталога
  сессий.

**Зеркало:**

- `tests/test_session_mirror_wire.py` — сверка `MIRROR_OPERATIONS` с тем,
  что платформа реально регистрирует
  (`lib/gateway/mirror/session_mirror.py:76`). Это страж против операции,
  описанной в коде, но отсутствующей в реестре: такой
  случай проходит чтение кода, компиляцию и все тесты с подставным
  клиентом, а падает в рантайме на каждом цикле;
- `tests/test_session_cold_sync_service.py` — циклы, детекты, уборка;
- `tests/test_storage_hybridization_lifecycle.py` — жизненный цикл
  зеркала: старт, финальный проход, выключение;
- `tests/test_storage_hybridization_factory.py` — сборка из
  `ApplicationContext`, включая зависимость от клиента платформы;
- `tests/test_config_keys.py` — разбор `gateway.session_cold_sync`;
- `tests/test_runtime_health.py` — появление счётчиков зеркала в отчёте
  живости.

**Обвязка и документация:**

- `tests/test_service_identity.py` — служебная личность вызовов зеркала;
- `tests/test_docs_consistency.py` — согласованность документации;
- `docs/architecture/storage-layers.md` — модель хранения, на которую
  ссылается модуль.

Честная граница покрытия, найденная при сверке: **требование о размере
батча не имеет стража, потому что нет самого батчинга.** Ни один тест не
может провалить `batch_size`, потому что зеркало его не читает: ключ
настроен в `./config.json:710`, объявлен в
`lib/core/project_settings.py:171` и молча не используется. Соответствие
`./config.json` и читаемого кода в этой части автоматической проверки не
имеет.
