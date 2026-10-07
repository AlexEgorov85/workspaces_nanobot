# storage/session-hybridization Specification — дельта

## Purpose

Закрыть разрыв между каноном и кодом: вещи, реализованные при переводе зеркала
сессий на платформу, не описаны ни одним требованием. Добавляем требования и
**правим одно требование**, нормативный текст которого описывает пул, которого у
зеркала больше нет.

## Scope

`shared` — холодное зеркало сессий: платформа владеет данными
(`mcp-platform/servers/enterprise/capabilities/data/`), агент ведёт фоновое
зеркалирование через операции (`lib/gateway/mirror/`).

Не в объёме: состав таблиц и DDL, состав и словарь событий журнала, контракт
вызовов операций как таковой (`runtime/call-contract`).

## ADDED Requirements

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

## MODIFIED Requirements

### Requirement: Правила использования пула PG-соединений

`SessionMirror` SHALL обращаться к PostgreSQL только через операции платформы.
В модуле SHALL NOT быть ни собственного пула, ни `connect()`, ни `create_pool()`,
ни прямых обращений к `lib.utils.db` из зеркала.

Полные правила (DI, advisory lock, threading, батчи, метрики,
shutdown order) — в `openspec/changes/archive/2026-09-27-storage-hybridization/design.md`
§ «Connection pool» (D-Pool). Здесь фиксируется только
нормативный контракт.

> Правка 2026-10-04. Прежняя первая фраза требования («SHALL использовать общий
> пул `lib.utils.db`») описывала мир, в котором зеркало ходило в базу само. После
> перевода зеркала на операции платформы в модуле нет **ни одного** упоминания
> `lib.utils.db` (поиск по `lib/gateway/mirror/*.py` — 0 совпадений), а пул
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
  `connect()` / `create_pool` и MUST NOT импортировать `lib.utils.db` — это
  проверяемо целиком: поиск по `lib/gateway/mirror/*.py` даёт 0 совпадений
- **AND** сессия клиента MUST переиспользоваться между вызовами цикла, а не
  подниматься на каждый вызов операции
  (``_ensure_session()` в `lib/services/enterprise_mcp_client.py`` — одна живая сессия на
  процесс)

> Прежняя редакция требовала `lib.utils.db.transaction()` / `lib.utils.db.run()` «для
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
> agent_session_messages (full re-read)» всё ещё показывает `lib.utils.db.transaction()`
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
  - `pool_size`: текущий размер пула `lib.utils.db` (`int | None`);
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
