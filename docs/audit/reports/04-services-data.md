# Аудит: 04 — Сервисы данных, LLM и сессии

## Сводка группы

Файлов: 12 · LOC: 3331 · классов: 10 · методов: 80 · функций: 25 · **символов разобрано: 115** · `НЕ РАЗОБРАНО`: 0

Подсистема: долговременный журнал агента (`agent_gateway_logs` / `agent_question_runs`),
LLM-клиент и observer-пайплайн, cold-storage mirror сессий JSONL→PG, конфиг-сервис,
транскрибация, чанкинг, управление subprocess'ами.

### Ключевые находки

- `lib/core/agent_factory.py:208` — **observer-пайплайн LLM не подключён вообще.**
  `_wrap_provider_snapshot_loader` зовёт `getattr(config, "build_provider_snapshot", None)`
  на экземпляре `nanobot.config.Config`, а `build_provider_snapshot` — модульная функция
  `nanobot.providers.factory`, а **не** метод `Config`. Эмпирическая проверка:
  `AgentFactory._wrap_provider_snapshot_loader(Config(), object(), None)` → `None`.
  ⇒ `provider_snapshot_loader=None` → upstream подставляет дефолтный loader без observer'а
  (`AgentLoop.from_config` → `model_presets.make_preset_snapshot_loader(config, None)`).
  `gateway.usage_store.enabled: true`, `llm_usage_store_factory.create_usage_store()` и весь
  `llm_observer.py` — мёртвый слой. Дубликатной записи `event_type="llm_usage"` в
  `DbLoggingService` запрещено (`tests/test_storage_hybridization.py:160`), т.е. **источника
  LLM-usage в системе нет вообще**. Вердикт: `Слить с`/починить.
- `lib/services/session_cold_sync_service.py:170` + `:176` + `:239` — **финальный flush при
  `stop()` — no-op.** `stop()` сначала ставит `_running = False`, потом зовёт `_sync_cycle()`
  → `_do_sync_batch()`, где первая же проверка `if not self._running: return` выходит до
  синка сессий и до `_cleanup_missing`. Проверено: при 2 upstream-сессиях и `_running=False`
  синканул 0 сессий, cleanup не выполнялся. Гарантия D21 «shutdown order» не реализована.
- `lib/services/session_cold_sync_service.py:464-484` — **leader-election не даёт взаимного
  исключения.** `_try_advisory_xact_lock` берёт `pg_try_advisory_xact_lock` внутри
  `_run_in_tx` → `with transaction() as conn: return fn(conn)`, т.е. lock освобождается на
  следующем же COMMIT, а полезная работа (`:233-249`, отдельные транзакции на других
  соединениях пула) идёт уже без него. Docstring'и `:20-22` и `:465-471` описывают
  несуществующую гарантию.
- `lib/services/db_logging_service.py:223/269/281` — `_stop_event` **только пишется, ни разу
  не читается** (нет `is_set()` в `_worker`). Если на shutdown очередь полна и sentinel
  (`_FlushSentinel`, `:283`) отброшен, worker-цикл не имеет условия выхода и живёт вечно,
  при этом `stop()` уже присвоил `self._thread = None` (`:287`) → `is_running()` == `False`,
  т.е. утечка daemon-потока, о которой рантайм не знает.
- `lib/services/db_logging_service.py:769` — purge-проверка стоит **после**
  `continue` для `_QuestionRunRecord`, поэтому поток `register_request`/`finish_request`
  полностью отключает периодическую очистку (и `_purge_old`, и безусловный
  `purge_empty_outbound`).
- `lib/services/session_cold_sync_service.py:664-669` — `resolve_default_sqlite_path()` мёртвая
  (0 ссылок в репозитории) и дублирует `llm_usage_store_factory._default_sqlite_path()`;
  её docstring `:667` утверждает, что используется в `llm_usage_store_factory` — это **ложь**
  (там своя приватная копия).
- `lib/services/llm_usage_store_factory.py:36` — docstring ссылается на
  `nanobot/llm_usage/store.py: _DEFAULT_DB_NAME`; такого символа нет, а upstream-путь —
  `get_data_dir() / "llm_usage.sqlite3"`, а не `~/.cache/nanobot/usage/usage.db`. Два разных
  файла (что видно и по `.gitignore:83-85`, где игнорируется именно `llm_usage.sqlite3`).
- `lib/services/db_logging_service.py:211,215-216` — три мёртвых поля (`_dialect`,
  `_connect_backoff_sec`, `_connect_backoff_max_sec`): пишутся, не читаются. Соответственно
  **мёртвые настройки** `logging.db.dialect`, `logging.db.connect_backoff_sec`,
  `logging.db.connect_backoff_max_sec` — объявлены в `project.json:518-520`, внесены в
  `REQUIRED_KEYS` (`tests/test_config_keys.py:174-176`), но не влияют ни на что.
- `project.json:509-522` — в `logging.db` **нет** `retention_days` и `purge_interval_sec`,
  хотя `AGENTS.md` (раздел «Очистка журнала событий») документирует их как настраиваемые, а
  `_make_db_logging` (`lib/core/application_context.py:1207-1208`) читает через
  `db_cfg.get(..., 90)` / `db_cfg.get(..., 3600.0)`. Дрейф документации ↔ конфига.
- `tests/test_config_keys.py` — в `REQUIRED_KEYS` нет `logging.db.question_runs_table`,
  хотя `_make_db_logging` (`application_context.py:1195-1196`) кидает `ConfigurationError`
  при пустом значении, т.е. это фактически required.
- `lib/services/db_logging_service.py:583-611` — `log_sync_event()` мёртвая (0 ссылок в коде,
  тестах, SQL, CI, конфиге), docstring `:588` называет удалённый `PgDuckDbSyncService`.
  `log_error()` (`:564`) — тоже 0 продуктовых вызовов. Оба перечислены как публичный контракт
  в `openspec/specs/logging-db/spec.md:56,59,86-87`, но фактические sync-producer'ы
  (`cache_load_service.py:334`, `duckdb_cache_store.py:1204,1241`,
  `session_cold_sync_service.py:508-584`) пишут через `try_log_event`.
- `lib/services/llm_client.py` — `call_llm_async` **не существует**, хотя объявлен в
  `AGENTS.md` (строка обзора `lib/services/llm_client.py`), `CHANGELOG.md` и
  `docs/ARCHITECTURE.md:1564`. Модуль sync-only.
- `lib/services/transcription_service.py:72-82` — docstring `get_language` утверждает, что
  `""` трактуется как `None`; код возвращает `""` как есть. Тесты покрывают только `None` и
  отсутствие атрибута, поэтому расхождение не ловится.
- `lib/services/session_storage.py:13` — module docstring обещает возврат `(manager, mode)`,
  фактически возвращается `(mode, manager)` (`:140`).
- `lib/services/subprocess_manager.py:113-117` — `__enter__`/`__exit__` мертвы: `gateway.py`
  создаёт менеджер и зовёт `spawn_streamlit`/`terminate_all` явно, `with` не используется нигде.

### Счётчики вердиктов

**Вердикты (по символам):** Оставить **77** · Упростить **21** · Удалить **4** · Слить с **2** · `НЕ РАЗОБРАНО`: **0**
— разбивка по файлам в таблице «Сводка по вердиктам» в конце отчёта.

Плюс 5 **файловых** вердиктов (`Упростить`: `db_logging_service.py`, `session_cold_sync_service.py`,
`config_service.py`, `subprocess_manager.py`, `transcription_service.py`) и 1 файловый
`Слить с` (`llm_observer.py` — слой не выполняет заявленной функции из-за бага в вызывающем коде).

Функциональных багов найдено: **6**. Кросс-подсистемных находок: **11**.

---

## `lib/services/db_logging_service.py` — 1142 LOC

**Назначение.** Единственная точка записи долговременного журнала агента
(`agent_gateway_logs` + `agent_question_runs`) с неблокирующей очередью и worker-потоком.

**Что делает.** Продюсеры (`BusFactory`-обёртки, хуки, `runtime_patcher`, каналы,
`cache_load_service`, `session_cold_sync_service`, `context_compaction`) кладут `LogEvent`
в `queue.Queue(maxsize=10000)`. Один daemon-поток дренирует очередь, собирает батчи до
`batch_size` или `flush_interval_sec`, вставляет их через **общий пул `utils.db`**
(`_db_run` → `run(fn)`, сервис своего `psycopg2`-соединения не держит), и раз в
`purge_interval_sec` чистит мусор. Отдельная ветка `_QuestionRunRecord` делает
`UPDATE`/`INSERT` в `agent_question_runs` по `request_id`. Счётчики в `self._stats` живут
весь lifetime инстанса.

**Зачем нужен.** Единственный путь к долговременному журналу: `history_search`-тул
(`workspace/tools/history_search_tool.py`) читает `agent_gateway_logs` напрямую из PG, всё
остальное наблюдение (`/health`, UI, расследование инцидентов) — отсюда. Без асинхронного
очередирования hot-path агента блокировался бы на INSERT.

**Вердикт.** `Упростить`
**Обоснование.** Ядро (очередь + батчинг + безусловный purge пустых outbound) реализовано
правильно и покрыто 39 717 байт тестов. Но поверх ядра лежат: три write-only поля под три
мёртвых настройки, write-only `_stop_event` при реальной утечке потока на shutdown, две
мёртвые публичные функции, заданные спецификацией, и 4-кратное дублирование
«_db_run + заклозка курсора».

**Доказательства.** Импортёры: `lib/core/application_context.py:1198`, `lib/core/bus_factory.py:23`,
`lib/hooks/database_logging_hook.py`, `lib/services/{runtime_patcher,preload_service,duckdb_cache_store,cache_load_service,context_compaction,session_cold_sync_service,runtime_events_subscriber}.py`,
`lib/channels/postgres_channel.py:630`. Тесты: `tests/test_db_logging_service.py` (39 717 B),
`tests/test_unified_event_logging_contract.py`, `tests/test_hooks_database_logging.py`,
`tests/test_application_context_logging.py`. Спека: `openspec/specs/logging-db/spec.md` (817+ строк).

#### class `LogEvent` (129–157, 0 методов)

Dataclass-контракт одного события журнала. `event_type`, `level`, `session_id`, `channel`,
`summary`, `payload: dict`, `user_id`, `request_id`, `timestamp`, плюс `metadata`.
Используется всеми продюсерами и `psycopg2.extras.execute_jsonb` в `_insert_batch`.
**Вердикт.** `Оставить`. Стабильный публичный контракт; удаление ломает 8+ модулей и
спеку `logging-db/spec.md:55`.

#### class `_FlushSentinel` (161–162, 0 методов)

Маркер конца очереди, который `_worker` возвращает наружу при flush.
**Вердикт.** `Оставить`. Альтернатива (flush по таймауту без маркера) не различает
«очередь пуста» и «shutdown».

#### class `_QuestionRunRecord` (166–183, 0 методов)

Внутренний маркер для upsert'а в `agent_question_runs` (не `LogEvent` → не попадает в
`agent_gateway_logs`). Содержит request-контекст + `upsert: bool`.
**Вердикт.** `Оставить`. Обойти его `isinstance`-веткой в `_worker` нельзя без потери типа
в очереди.

#### class `DbLoggingService` (186–1126, 32 метода)

Оркестратор очереди, worker'а, purge и контекста вопросов. Ниже по методам.

Атрибуты (одной строкой): `_queue`, `_thread`, `_running`, `_stop_event` (мёртв),
`_state_lock`, `_schema_ok`, `_min_level`, `_batch_size`, `_flush_interval`,
`_retention_days`, `_purge_interval_sec`, `_summary_max_chars`, `_connect_backoff_sec` /
`_connect_backoff_max_sec` (мёртвы), `_dialect` (мёртв), `_table_name`,
`_question_runs_table`, `_schema`, `_request_map`, `_stats`, `_last_purge_ts`,
`_stale_logged_at` (нет — в другом классе).

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 189–259 | Собрать очередь, счётчики, конфиг | DI-конструктор | `application_context.py:1198-1210` | **Упростить** — выбросить `dialect`, `connect_backoff_sec`, `connect_backoff_max_sec` (3 write-only поля, 3 мёртвые настройки) |
| `start` | 265–276 | Поднять daemon-поток, сбросить `_stop_event` | Жизненный цикл | `application_context.py:582` | Оставить |
| `stop` | 278–288 | `join(timeout)`, сбросить `_running`/`_thread` | Graceful shutdown | `ShutdownCoordinator` (через `application_context:579`) | **Упростить** — (1) не обнулять `_thread`, если поток жив; (2) добавить проверку `_stop_event.is_set()` в `_worker` как страховку от потерянного sentinel при `queue.Full` (см. баг №4) |
| `is_running` | 290–291 | Флаг работы потока | Диагностика | `application_context` readiness; тесты | Оставить, но см. `stop` — в текущем виде врёт при утёкшем потоке |
| `log_event` | 297–300 | Фильтр по `min_level` + `put_nowait` | Основной путь записи | `log_inbound/outbound/tool_call/tool_result/llm_call/error/sync_event`; `database_logging_hook.py:509`; `runtime_patcher.py:2158`; `postgres_channel.py:630`; `runtime_events_subscriber.py:268,316` | Оставить |
| `register_request` | 315–366 | Upsert-контекст вопроса + запомнить `session_key → request_id` | Привязка вопроса к сессии | `postgres_channel.py` (агентский слой) | Оставить |
| `get_request_id` | 368–377 | Lookup `request_id` по `session_key` | Для корреляции `run_*`/`tool_*` | продюсеры `run_finished` | Оставить |
| `clear_request` | 379–384 | Снять привязку вопроса | Гигиена `_request_map` | продюсеры конца прогона | Оставить |
| `finish_request` | 386–405 | Финальный status/summary/response вопроса | Закрытие строки в `agent_question_runs` | `postgres_channel.py` | Оставить |
| `log_inbound` | 407–439 | Событие входящего сообщения | Журнал диалога | `db_logging_bus.make_inbound_logger` | Оставить |
| `log_outbound` | 441–468 | Событие исходящего сообщения (+media через `serialize`) | Журнал + `RecentFilesHook`-совместимость | `db_logging_bus.make_outbound_logger` | Оставить |
| `log_tool_call` | 470–489 | Tool-вызов: имя + аргументы | Аудит tool'ов | `database_logging_hook` | Оставить |
| `log_tool_result` | 491–522 | Результат tool'а + `latency_ms` | Аудит tool'ов | `database_logging_hook` | Оставить |
| `log_llm_call` | 524–562 | Полный промпт+ответ за итерацию | Диагностика качества/стоимости | `database_logging_hook` | Оставить |
| `log_error` | 564–581 | Ошибка → `event_type="error"` | Централизованный лог ошибок | **0 продуктовых вызовов** (только тесты + `logging-db/spec.md:59,86`) | **Упростить** — либо найти 1 call-site, либо снять требование спеки; попутно `error[:200]` хардкодом вместо `self._summary_max_chars` (`:571` vs `:588`) |
| `log_sync_event` | 583–611 | Sync-producer событие | Спека `logging-db/spec.md:56,425` требует, но продюсеры зовут `try_log_event` | **0 ссылок в репозитории** | **Удалить** — docstring `:588` называет удалённый `PgDuckDbSyncService`; удаление требует правки `logging-db/spec.md:56,414,425,457` и `openspec/specs/storage/session-hybridization/spec.md:117` |
| `get_stats` | 613–621 | Диагностический снимок счётчиков | Спека `logging-db/spec.md:817-857`; `TROUBLESHOOTING.md` | **0 продуктовых вызовов** (`runtime_health.py` не знает про сервис) | Оставить — спека-контракт; заодно отметить отсутствие потребителя |
| `_compute_oldest_queued_age_sec` | 623–645 | Возраст самого старого `LogEvent` | Метрика backpressure | `get_stats:619` | Оставить; читает `self._queue.queue` без `q.mutex` и без учёта вопросов-в-очереди — эмпирически `RuntimeError` на CPython 3.14 **не воспроизводится**, но это документированно-внутренний атрибут `queue.Queue` |
| `_should_log` | 651–658 | Сравнение уровня с `min_level` | Дешёвый ранний выход | `log_event:298` | Оставить |
| `_enqueue` | 660–699 | Резолв `user_id` + `put_nowait` под `try` | Никогда не блокирует и не бросает | `log_event:299` | Оставить — этот путь закрывает вопрос «теряются ли события»: `queue.Full` → `False`, исключение → `False`, оба без потерь исключений |
| `_resolve_event_user_id` | 701–723 | Резолв `user_id` по 3 ветвям | Security boundary | `_enqueue:689` | Оставить |
| `_worker` | 725–796 | Цикл drain → батч → flush → purge | Сердце сервиса | `start:274` | **Упростить** — перенести purge-проверку выше `continue` для `_QuestionRunRecord` (`:769`), см. баг №5 |
| `_db_run` | 802–808 | `run(fn)` на соединении пула `utils.db` | Единый способ взять соединение | `_flush_batch:876`, `_handle_question_run:939`, `purge_empty_outbound:1064`, `purge_old:1107` | **Слить с `workspace/utils/db.py`** — 4-я копия 7-строчного `try/return run(fn)/except` (см. `duplicates.md:75-78`, где отмечены ещё 2 копии) |
| `_ensure_schema` | 810–845 | Проверить существование обеих таблиц | Fail early при отсутствии DDL; DDL **не** провижинит | `_flush_batch:872`, `_handle_question_run:935` | Оставить |
| `_flush_batch` | 847–890 | `execute_batch` + инкремент `written_by_type` | Запись батча логов | `_worker:778` | Оставить |
| `_insert_batch` | 892–917 | `execute_batch` на данном соединении | Реализация вставки | `_flush_batch:874` | Оставить |
| `_handle_question_run` | 919–948 | Ветка worker'а для вопрос-рунов | Отдельная транзакция и upsert | `_worker:769` | Оставить |
| `_upsert_question_run` | 950–1021 | Upsert в `agent_question_runs` без `ON CONFLICT` | Greenplum 6.x не поддерживает `ON CONFLICT` | `_handle_question_run:937` | Оставить |
| `_drop_batch` | 1023–1031 | Счётчик `failed` + WARNING, без fallback-файла | Не терять молча | `_flush_batch:866` | Оставить |
| `purge_empty_outbound` | 1037–1072 | Удалить `outbound_final`/`outbound_delta` с пустым `content` и без `media` | Чистка stream-мусора **независимо от `retention_days`** | только `_purge_old:1124` | **Упростить** — слить с `purge_old` в один метод с двумя шагами (обе реализации на 80 % идентичны: `_work`-заклозка + cursor try/finally + `_db_run`); бонус — `DELETE` без `LIMIT` на большой таблице |
| `purge_old` | 1074–1116 | `DELETE` по `retention_days` для логов и вопрос-рунов | Ограничение роста БД | только `_purge_old:1126` | **Упростить** — см. выше; `LIMIT`-батчинга нет |
| `_purge_old` | 1118–1126 | Шаг очистки из worker-цикла; пустой outbound — всегда | Развязывает cadence и retention | `_worker:792` | Оставить — **проверено, что безусловная очистка реализована**: `purge_empty_outbound()` вызывается до проверки `self._retention_days > 0` |
| `_count_by_type` | 1129–1142 | Распределение батча по `event_type` | Метрика `written_by_type` | `_flush_batch:881` | Оставить |

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `try_log_event` | 34–101 | Defensive-обёртка: `None`-сервис → WARNING+`False`, исключение → WARNING+`False` | Единый безопасный вход для 8 producer'ов | `application_context.py:1396,1421,1457,1590`; `runtime_patcher.py:1772`; `preload_service.py:71,304`; `duckdb_cache_store.py:1204,1241`; `cache_load_service.py:334`; `context_compaction.py:378`; `session_cold_sync_service.py:508,524,556,584` | Оставить. **Docstring `:43` лжёт**: называет `PgDuckDbSyncService._log_sync_event` и `PreloadService._emit_health_event` (первый удалён, второй — другое имя); поправить текст |
| `_json_safe` | 104–125 | Рекурсивно привести payload к JSON-серизуемому | Промпт/ответ содержат dataclass'ы и объекты | `_enqueue` | Оставить |
| `_count_by_type` | 1129–1142 |см. выше (метод)| — | — | Оставить |

**Ответ на вопрос (а) «все ли пути покрыты try/except».** Да на пути продюсера:
`try_log_event` (`:34`), `log_event` → `_enqueue` (`:660`, `put_nowtry` под `try`),
`_flush_batch` (`:885`), `_handle_question_run` (`:941`), `_drop_batch` (`:1028`).
**Провенансы потери событий:** `queue.Full` → событие отбрасывается с `stats["dropped"]`
(сознательный backpressure, задокументирован в `logging-db/spec.md:142`), и при переполнении
очереди на shutdown — **безвозвратно** (см. баг №4). Fallback-файла нет намеренно
(`_drop_batch` docstring `:1024`).

---

## `lib/services/db_logging_bus.py` — 166 LOC

**Назначение.** Фабрики async-логгеров, которые `BusFactory` навешивает на
`MessageBus.publish_inbound` / `publish_outbound` вместо monkey-patch'а приватных методов.

**Что делает.** `make_inbound_logger(service, agent_id)` возвращает
`async def _log(msg: InboundMessage)`, который разбирает `msg.text` на
`text`/`media`, сериализует media через `utils.media.serialize` и кладёт `outbound`-подобное
событие в `DbLoggingService`. `make_outbound_logger` — то же для исходящих, с `latency_ms` и
`tokens` из `msg.metadata`.

**Зачем нужен.** `docs/ARCHITECTURE.md:118` фиксирует решение «без monkey-patch'ей».
Удаление вернёт `lib/core/bus_factory.py:23` к пустому импорту и потеряет лог диалога.

**Вердикт.** `Оставить`.
**Обоснование.** Два файла симметричны по смыслу, но не по данным (`InboundMessage.text`
vs `OutboundMessage.content` + `metadata.tokens`), сливать нечего.

**Доказательства.** `lib/core/bus_factory.py:23`, `lib/core/application_context.py:362`.
Тесты: `tests/test_hooks_database_logging.py:322-457` (7 кейсов).

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `make_inbound_logger` | 49–107 | async-логгер входящих | Журнал входящих | `bus_factory.py:23,` `application_context.py:362` | Оставить |
| `make_outbound_logger` | 110–166 | async-логгер исходящих | Журнал исходящих + media | там же | Оставить |

---

## `lib/services/session_cold_sync_service.py` — 669 LOC

**Назначение.** Фоновый daemon-mirror upstream JSONL-сессий в PostgreSQL
(`agent_session_meta` / `agent_session_messages`), чтобы сессии переживали рестарт/смену машины.

**Что делает.** Поток с backoff-циклом (`_worker:186`) каждые `sync_interval_sec` вызывает
`_do_sync_batch`: leader-election → `session_manager.list_sessions()` → для каждого ключа
stale-check по `updated_at` + LWW-запись meta/messages → `_cleanup_missing` для исчезнувших.
Все операции идут транзакциями через общий пул `utils.db`.

**Зачем нужен.** Без него `PGSessionManager` (cold-storage mirror) нечем заполнять: hot-path
делегирует upstream JSONL, а холодное чтение идёт из PG.

**Вердикт.** `Упростить`
**Обоснование.** SQL-логика корректна (валидация идентификаторов, `_quote`, Greenplum-safe),
но: leader-election фиктивен, финальный flush — no-op, один символ мёртвый, один дублирует
чужой модуль, три поля/константы мёртвые, два лог-метода — 90 % дублируют друг друга.

**Доказательства.** `lib/core/application_context.py:589-592` (`shutdown.register`).
Тесты: `tests/test_session_cold_sync_service.py` (25 783 B), `tests/test_storage_hybridization_lifecycle.py`.
Спека: `openspec/specs/storage/session-hybridization/spec.md`.

#### class `SessionColdSyncService` (62–661, 28 методов)

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 74–140 | Конфиг, счётчики, in-memory dedup-кеш | DI-конструктор | `application_context.py:589-592` | **Упростить** — удалить `_pool_acquire_timeout_sec` (`:97`, write-only) |
| `enabled` | 143–144 | Флаг активности | `escape hatch` для multi-instance | `application_context.py` | Оставить |
| `start` | 146–158 | Поднять daemon-поток | Жизненный цикл | `application_context.py:596` | Оставить |
| `stop` | 160–184 | `join` + «финальный flush» | D21 shutdown order | `ShutdownCoordinator` (`:590`) | **Упростить** — «финальный flush» (`:176`) фактически ничего не синкает: баг №2 |
| `_worker` | 186–197 | Цикл `start` → `sleep` → `stop` с backoff | Периодический sync | `start:154` | Оставить |
| `_compute_delay` | 199–206 | Backoff `1.0 * 2**min(failures,5)` | Защита от retry-шторма | `_worker:194` | Оставить |
| `_do_sync_batch` | 208–252 | Один цикл: lock → read → LWW → cleanup | Основной алгоритм | `_sync_cycle:258`, `stop:176` | **Упростить** — `if not self._running: return` (`:239`) убивает финальный flush; leader-election (`:227`) не покрывает `:233-249` |
| `_sync_cycle` | 254–260 | Алиас `_do_sync_batch` («backward-compat») | Ничего: 0 внешних вызовов кроме `stop:176` | `stop:176` | **Упростить** — удалить однострочный алиас, вызывать `_do_sync_batch` напрямую |
| `_sync_session_with_detection` | 262–310 | stale-detect + reverse-lag + LWW на ключ | Ядро одного ключа | `_do_sync_batch:242` | Оставить; в docstring/коде `:302-303` **дублируется строка комментария** («3. EQUAL …») — косметика |
| `_do_lww_sync` | 312–317 | Записать meta + messages | LWW | `_sync_session_with_detection` | Оставить |
| `_is_stale_logged_recently` | 319–328 | Дедуп stale-логов за 60 с | Не спамить журнал | `_log_stale:551` | Оставить; `_stale_logged_at` **не чистится** для ключей, переставших быть stale — ограниченный рост |
| `_read_upstream` | 330–331 | `session_manager.list_sessions()` | Источник правды | `_do_sync_batch:233` | Оставить |
| `_upsert_meta` | 333–365 | `INSERT/UPDATE` в `agent_session_meta` | Холодный read-path | `_do_lww_sync:315` | Оставить |
| `_replace_messages` | 367–396 | Полная перезапись сообщений | Холодный read-path | `_do_lww_sync:316` | Оставить |
| `_cleanup_missing` | 398–433 | Удалить из PG исчезнувшие сессии | JSONL — единственный source of truth | `_do_sync_batch:245` | Оставить |
| `_read_pg_updated_at` | 435–446 | `SELECT updated_at` по ключу | LWW-основание | `_sync_session_with_detection` | Оставить |
| `_count_pg_sessions` | 448–453 | `COUNT(*)` по meta-таблице | Метрика расхождения | `_do_sync_batch:247` | Оставить |
| `_select_all_pg_keys` | 455–462 | Все ключи PG | Вход для `_cleanup_missing` | `_cleanup_missing:400` | Оставить |
| `_try_advisory_xact_lock` | 464–484 | `pg_try_advisory_xact_lock` в одной транзакции | Задумано как leader-election | `_do_sync_batch:227` | **Упростить** — lock освобождается на выходе из `_run_in_tx` (`:486-496`), т.е. до начала полезной работы; либо перевести всю пачку в одну транзакцию, либо удалить и задокументировать single-instance (как сделано для кэша в `AGENTS.md`) |
| `_run_in_tx` | 486–496 | `with transaction() as conn: return fn(conn)` | Транзакционный хелпер | `:469`, `_upsert_meta`, `_replace_messages`, `_cleanup_missing`, `_read_pg_updated_at`, `_count_pg_sessions`, `_select_all_pg_keys` | Оставить |
| `_log_failure` | 498–513 | `session_cold_sync_failed` | Диагностика | `_worker:196` | Оставить |
| `_log_deleted` | 515–529 | `session_cold_sync_deleted` | Диагностика | `_cleanup_missing:429` | Оставить |
| `_log_stale` | 531–561 | `session_stale_detected` + dedup | D23 | `_sync_session_with_detection:288` | **Упростить** — 31 строка, 90 % совпадает с `_log_lag_exceeded` (ср. `duplicates.md`); слить в `_log_detected(kind, ...)` |
| `_log_lag_exceeded` | 563–589 | `sync_lag_exceeded` | Reverse-lag | `_sync_session_with_detection:302` | **Упростить** — см. выше |
| `get_stats` | 591–627 | Метрики циклов/батчей/пула | «D-Pool.6» в AGENTS.md | **0 продуктовых вызовов** | Оставить как публичный контракт; **удалить ключ `cycles_skipped_pool_busy` (`:611`) — он всегда 0**, т.к. `_cycles_skipped_pool_busy` (`:122`) нигде не инкрементится |
| `_read_pool_size` | 629–649 | `pool_size`/`pool_available` из `utils.db.get_stats()` | Метрики пула | `get_stats:613` | Оставить |
| `_validate_ident` | 652–654 | Guard на SQL-инъекцию в имени таблицы | Security boundary | `_quote:658` | Оставить |
| `_quote` | 657–661 | `"schema"."table"` | Security boundary | `_upsert_meta`, `_replace_messages`, `_cleanup_missing`, `_read_pg_updated_at` | Оставить |

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `resolve_default_sqlite_path` | 664–669 | `~/.cache/nanobot/usage/usage.db` | — | **0 ссылок** (код, тесты, SQL, CI, docs) | **Удалить** — дубликат `llm_usage_store_factory._default_sqlite_path()`; docstring `:667` («Используется в `lib.services.llm_usage_store_factory`») лжёт |

**Мёртвые поля/константы в модуле:** `_pool_acquire_timeout_sec` (`:97`),
`_cycles_skipped_pool_busy` (`:122`), `_POOL_BUSY_BACKOFF_SEC` (`:58`) — все три
заведены «под pool-busy учёт», которого нет.

---

## `lib/services/config_service.py` — 284 LOC

**Назначение.** Сборка финального runtime-конфига nanobot: `config.json` + `config.example.json`
+ `${VAR}`-подстановка + таймауты.

**Что делает.** `load()` читает `config.json` рядом со скриптом, мержит с шаблоном
`sync_templates`, предварительно резолвит `${VAR}` **из SETTINGS** (не из `os.environ`),
подставляет api_key провайдеров и пишет таймауты в `os.environ` + `Settings`.

**Зачем нужен.** Без предрезолва `${LLM_API_KEY}` upstream `_load_runtime_config` падает
`ValueError` — это единственная точка, где секреты долетают до конфига нашего агента.

**Вердикт.** `Упростить`
**Обоснование.** Механика правильная, но `load()` дублирует параметры
`script_dir`/`workspace_dir`, которые уже есть в `__init__`, а `apply_provider_keys` вынесен
«на публичку» без единого внешнего вызова.

**Доказательства.** `lib/core/application_context.py:1122-1125` (создание), `:274` (`load()`),
`:285-287` (`get_int`), `:319` (`get_str`); `gateway.py:305,321,336,352` (`settings_section`).
Тесты: `tests/test_config_service.py` (8 222 B), `tests/test_config_resolver.py`.
**Замечание к брифу:** бриф указал `get_int`/`get_str` как «0 ref files» — это ошибка AST-подсчёта
(вызовы идут как `ctx.config_service.get_int(...)`); оба метода живые.

#### class `ConfigService` (26–284, 9 методов)

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 39–50 | Хранить `script_dir`/`workspace_dir`/`settings_override` | DI | `application_context.py:1122`, `gateway.py` (4×) | Оставить |
| `settings` | 57–70 | Доступ к глобальному/lazy `SETTINGS` | Единая точка чтения | `gateway.py` | Оставить |
| `settings_section` | 72–82 | Top-level секция как dict | Мягкое чтение в `gateway.py` | `gateway.py:305,321,336,352` | Оставить (там же 3× обёрнут в `try/except` без необходимости — `settings_section` не бросает) |
| `get_int` | 84–95 | Int по пути в dict/AttrDict | Типобезопасное чтение | `application_context.py:285-287` | Оставить — **опровергает бриф** |
| `get_str` | 97–104 | Str по пути | То же | `application_context.py:319` | Оставить — **опровергает бриф** |
| `load` | 110–157 | Основная загрузка+сборка конфига | Точка входа настроек | `application_context.py:274` | **Упростить** — убрать параметры `script_dir`/`workspace_dir` (дублируют инстанс; в рантайме `load()` зовут без аргументов); `sync_templates` — тоже мёртвый кноуб (никогда не `False`) |
| `_pre_resolve_env_refs` | 159–240 | Рекурсивный резолв `${VAR}` из SETTINGS | Ключевая механика | `load:126` | Оставить; `:240` содержит ложный `# noqa: F821` (никакого undefined name там нет) — убрать |
| `apply_provider_keys` | 246–257 | Подставить api_key провайдеров | Секреты в конфиг | `load:156` (**единственный** вызов) | **Упростить** → приватный `_apply_provider_keys`; публичность без потребителей |
| `apply_timeouts` | 259–284 | `llm_timeout`/`exec_timeout`/`max_iterations` в env + Settings | Единая точка таймаутов | `application_context.py:280-284` | Оставить |

---

## `lib/services/text_splitter.py` — 217 LOC

**Назначение.** Чанкинг длинных текстовых значений колонок при построении эмбеддингов.

**Что делает.** `split_text` перебирает разделители от приоритетных к запасным
(`_recursive_split`), каждый проход — `_split_by_separator` + `_merge_into_chunks` (жадное
склеивание с перекрытием, гарантированно завершается), последний рубеж — `_split_by_chars`.
`build_chunks` нарезает все `embedding_cols` одной строки таблицы.

**Зачем нужен.** Векторизатор `tools/build_vectors.py` не должен отдавать в эмбеддинг
многостраничную ячейку.

**Вердикт.** `Оставить`
**Обоснование.** Живой, но **только для build-инструмента**: единственный продуктовый
потребитель — `tools/build_vectors.py` (standalone, по `AUDIT_PROTOCOL.md` §1 это не повод
считать мёртвым). Дублирования с `workspace/utils/clean_text.py` и
`lib/utils/text_utils.py` **нет** — это разные задачи (санитизация vs разбиение);
второе чанк-разбиение есть в `legal_summarizer` (управляется
`skills.legal_summarizer.chunking.*`, другая форма данных — документы, не строки таблиц).

**Доказательства.** `tools/build_vectors.py` (импортёр), `tests/test_text_splitter.py`.
Runtime-импортёров (`lib/`, `workspace/hooks/`, `workspace/tools/`, `lib/core/`) **нет**.

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `split_text` | 17–41 | Публичное рекурсивное разбиение | Точка входа | `build_chunks:200`, `tests` | Оставить |
| `_recursive_split` | 44–60 | Перебор разделителей | Стратегия | `split_text:37` | Оставить |
| `_split_by_separator` | 63–80 | Разбить по одному разделителю | Примитив | `_recursive_split:53` | Оставить |
| `_merge_into_chunks` | 86–130 | Жадное склеивание с overlap | Примитив; «никогда не теряет хвост» | `_split_by_separator:76` | Оставить; docstring ссылается на «старую реализацию»/«Баг А»/«Баг Б» — историческая справка, кандидат на сокращение |
| `_split_by_chars` | 133–153 | Посимвольный последний рубеж | Гарантия завершения | `_recursive_split:57` | Оставить |
| `build_chunks` | 156–217 | Чанки одной строки | Вход для `build_vectors` | `tools/build_vectors.py` | Оставить |

---

## `lib/services/llm_client.py` — 199 LOC

**Назначение.** Единственный HTTP-клиент к LLM (OpenAI-compatible `/chat/completions`)
для skill'ов, бенчмарков и standalone-утилит.

**Что делает.** `call_llm` собирает payload (model/messages/temperature/max_tokens + params
из конфига), через `_post_json` делает POST с retry только на 429/5xx/таймаут
(`retry_on_exception`), возвращает `choices[0].message.content`. `call_llm_json` поверх этого
чистит markdown-обёртку и парсит JSON-объект.

**Зачем нужен.** Консолидация: раньше каждый skill имел свою копию.

**Вердикт.** `Оставить`
**Обоснование.** Дублирования нет: `workspace/skills/audit_analyzer/scripts/llm.py:13` и
`workspace/skills/legal_summarizer/scripts/llm/client.py` — тонкие обёртки (`from
lib.services.llm_client import call_llm`); `benchmarks/evaluator.py` делегирует.
**Замечание:** `call_llm_async`, обещанный в `AGENTS.md`, `CHANGELOG.md`,
`docs/ARCHITECTURE.md:1564`, **не существует** — модуль sync-only.

**Доказательства.** `workspace/skills/audit_analyzer/scripts/llm.py:13`,
`workspace/skills/legal_summarizer/scripts/llm/client.py`, `benchmarks/evaluator.py`.
Тесты: `tests/test_llm_config.py`; сам `llm_client` в `test_gaps.md:43` (LOC 199, покрытия нет).
**Кросс-подсистемно:** `tests/test_dependency_direction.py:143-144` фиксирует, что skill'ы
импортируют `lib/services` — долг, помеченный как «надо чинить отдельной задачей».

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_resolve_cfg` | 31–41 | `cfg` или `resolve_llm_config()` | Единый резолв | `call_llm:58`, `call_llm_json:113` | Оставить |
| `call_llm` | 44–97 | Вызов LLM → текст | Публичный API | `audit_analyzer/scripts/llm.py`, `legal_summarizer` | Оставить |
| `call_llm_json` | 100–140 | Вызов LLM → dict или `None` | Структурированный ответ | `audit_analyzer`, `legal_summarizer`, `benchmarks/evaluator.py` | Оставить; `max_retries=0` по умолчанию против `max_retries=3` у `call_llm` — задокументировано в docstring, но легко спутать; «съедает» любое исключение без логирования (в модуле нет `logger`) |
| `_post_json` | 144–176 | httpx POST + backoff | Транспорт | `call_llm`, `call_llm_json` | Оставить; лишняя пустая строка `:143` |
| `_parse_json_object` | 179–199 | Чистка ```-обёрток + `json.loads` | Толерантность к LLM | `call_llm_json:129` | Оставить |

**Проверка `retry_on_exception` (`lib/utils/retry.py:34-70`):** `max_retries=0` → `range(1,1)`
пуст → выполняется единственный `return fn()` (`:70`) — семантика «одноразовый вызов»
корректна. Исключение, брошенное из `_on_retry` (не-429 HTTPStatusError), корректно
пробрасывается наружу, а не съедается циклом. **Замечание:** в `_post_json` retry-декоратор
применён к обычной функции, а не к bound-method провайдера — риск при upgrade: приватных
API upstream не касается, публичный `httpx` да.

---

## `lib/services/subprocess_manager.py` — 149 LOC

**Назначение.** Запуск и корректное завершение дочерних процессов (Streamlit UI).

**Что делает.** `spawn_streamlit` поднимает `sys.executable -m streamlit run streamlit_app.py`
с логом в `gateway/logs/streamlit.log` и `CREATE_NEW_PROCESS_GROUP`; `terminate_all` шлёт
`SIGTERM`, ждёт `shutdown_timeout_sec`, добивает `taskkill /F /T`.

**Зачем нужен.** `gateway.py:207` поднимает UI вместе с gateway'ем; без `terminate_all`
дочерние процессы переживают родителя.

**Вердикт.** `Упростить`
**Обоснование.** Работает, но `__enter__`/`__exit__` мертвы, а `spawn_streamlit(port=...)` и
`terminate_all(timeout_sec=...)` в рантайме всегда вызываются без аргументов.
**Кросс-подсистемно:** `openspec/specs/runtime/entrypoints/spec.md:569` требует, чтобы
`gateway.py` **не** поднимал streamlit и чтобы `SubprocessManager.spawn_streamlit` был удалён
change'ом `remove-streamlit-runtime`. Такого change'а в `openspec/changes/` нет, `streamlit_app.py`
существует ⇒ **требование спеки не выполнено**, `AGENTS.md` (точки входа) тоже устарел.

**Доказательства.** `gateway.py:207` (`spawn_streamlit`), `gateway.py` shutdown
(`terminate_all`). Тесты: `tests/test_subprocess_manager.py` (3 677 B).

#### class `SubprocessManager` (33–149, 5 методов)

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 41–50 | Список процессов + `log_dir` | Состояние | `gateway.py` | Оставить |
| `spawn_streamlit` | 56–111 | Запуск Streamlit | UI на :8501 | `gateway.py:207` | **Упростить** — параметр `port` в рантайме не передаётся; плюс открытый вопрос по `entrypoints/spec.md:569` |
| `__enter__` | 113–114 | Context-manager вход | — | **0 вызовов** | **Удалить** |
| `__exit__` | 116–117 | Context-manager выход | — | **0 вызовов** | **Удалить** (вместе с `__enter__`) |
| `terminate_all` | 123–149 | SIGTERM → wait → `taskkill /F /T` | Graceful | `gateway.py` shutdown | Оставить; `timeout_sec` в рантайме не передаётся |

---

## `lib/services/session_storage.py` — 140 LOC

**Назначение.** Фабрика `SessionManager`: upstream JSONL + зеркало в PG, либо PostgreSQL-only.

**Что делает.** `create()` по `storage` (`auto`/`jsonl`/`postgres`) и по `channels.postgres.pool`
(с legacy-fallback на плоские `min_conn`/`max_conn`/`pool_timeout` в `pg_cfg`) выбирает
`PostgresSessionManager` или `JsonlSessionManager`; при `configure_db=True` вызывает
`utils.db.configure(dsn)`; при `return_file_manager` возвращает ещё и file-manager.

**Зачем нужен.** Единственное место, где решается «где живут сессии».

**Вердикт.** `Упростить`
**Обоснование.** Ядро выбора режима нужно; мёртвы только два обломка — неиспользуемый
параметр `workspace_dir` и legacy-flat ветка (в репозитории нет `session_manager.json`; ветка
жива только если оператор положит такой файл вручную).

**Доказательства.** `lib/core/application_context.py:314-323`; тесты
`tests/test_session_storage.py` (6 868 B), `tests/test_config_resolver.py:312-326`.

#### class `SessionStorageError` (26–27)

`RuntimeError` с метаданными конфигурации (dsn, таблицы) — «fail loudly при неверной
конфигурации». **Вердикт.** `Оставить** (единая точка диагностики для `:118-124`).

#### class `SessionStorageService` (30–140, 1 метод)

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `create` | 42–140 | Собрать `SessionManager` | DI для `ApplicationContext` | `application_context.py:314-323` | **Упростить** — убрать параметр `workspace_dir` (0 передач); legacy-flat ветку `:118-124` либо задокументировать, либо удалить; **поправить module docstring `:13` — обещает `(manager, mode)`, возвращает `(mode, manager)`** (`:140`) |

---

## `lib/services/llm_observer.py` — 110 LOC

**Назначение.** Оборачивание upstream `provider_snapshot_loader` так, чтобы каждый вызов
snapshot'а записывал LLM-вызов в `LLMUsageStore`.

**Что делает.** `wrap_provider_snapshot_loader(base_loader, store, bus)` возвращает
`wrapped(*, preset_name=None, **kwargs)`: зовёт `base_loader`, затем
`attach_llm_observer(snapshot.provider, store)` и `attach_fallback_model_observer(..., bus)`;
обе обёртки fail-soft (`return False` при любом исключении, `logger.debug`).

**Зачем нужен.** Метаданные LLM-вызовов (provider/model/tokens/latency) — для оценки стоимости
и для `llm_usage`-событий. По спеке `openspec/specs/storage/usage-store/spec.md` это
единственный источник (дубль в `DbLoggingService` запрещён архитектурным тестом
`tests/test_storage_hybridization.py:160`).

**Вердикт.** `Слить с` (в текущем виде — мёртвый слой) — **требует починки, а не удаления**
**Обоснование.** Собственный код трёх функций корректен и fail-soft реален (обе обёртки глушат
исключения). Проблема в точке подключения: `lib/core/agent_factory.py:208` ищет
`build_provider_snapshot` как **метод `Config`**, а это модульная функция
`nanobot.providers.factory`. Проверено эмпирически:

```
>>> hasattr(Config(), "build_provider_snapshot")
False
>>> AgentFactory._wrap_provider_snapshot_loader(Config(), object(), None)
None
```

Upstream при `provider_snapshot_loader=None` подставляет
`model_presets.make_preset_snapshot_loader(config, None)` =
`lambda name: build_provider_snapshot(config, preset_name=name)` — **без observer'а**.
Правильный `base_loader`: `functools.partial(nanobot.providers.factory.build_provider_snapshot, config)`.
**Тест-гейп, который это скрыл:** `tests/contract/test_llm_observer_api.py` проверяет
`wrap_provider_snapshot_loader` только с самодельным stub-`base_loader`; ни один тест не
проходит путь `AgentFactory.create(usage_store=...)` с настоящим `nanobot.config.Config`.

Побочно: возвращаемые `False` из `attach_llm_observer` / `attach_fallback_model_observer`
игнорируются вызывающим кодом (`:105-106`) — сигнал fail-soft теряется.

**Доказательства.** `lib/core/agent_factory.py:208-210` (единственный caller),
`lib/core/application_context.py:1767-1770` → `:1799` (`AgentFactory.create(usage_store=ctx.usage_store)`).
Спека: `openspec/specs/storage/usage-store/spec.md:19-53`.

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `attach_llm_observer` | 31–51 | `set_llm_call_observer(store.record)` | Запись вызовов | только `wrap_provider_snapshot_loader:105` | Оставить (внутренний хелпер; вердикт на слой — выше) |
| `attach_fallback_model_observer` | 54–80 | `set_fallback_model_observer` для `FallbackProvider` | События семантического фолбэка | только `wrap_provider_snapshot_loader:106` | Оставить |
| `wrap_provider_snapshot_loader` | 83–110 | Обёртка base-loader'а | Точка подключения observer'а | `agent_factory.py:210` | **Слить с / починить** — слой не достигает цели из-за бага в вызывающем коде |

---

## `lib/services/llm_config.py` — 88 LOC

**Назначение.** Единая точка сборки LLM-конфига (provider/model/api_base/api_key/параметры)
для skill'ов и бенчмарков.

**Что делает.** `resolve_llm_config(overrides)` читает `SETTINGS.llm` / `SETTINGS.agents` /
`SETTINGS.skills.*` + `overrides` и возвращает плоский dict. `ensure_llm_env` кладёт
`LLM_API_KEY` в `os.environ` для резолва `${LLM_API_KEY}`.

**Зачем нужен.** Единый источник для HTTP-клиента `llm_client` и skill'ов; без
`ensure_llm_env` nanobot-лоадеры падают `ValueError`.

**Вердикт.** `Оставить`. Симметричная пара «собрать конфиг / выложить секрет в env»,
дублирования с `workspace/skills/*/scripts/skill_config.py` нет — там тонкая обёртка.

**Доказательства.** `lib/services/llm_client.py:36`, `workspace/skills/audit_analyzer/scripts/llm.py`,
`workspace/skills/legal_summarizer/scripts/llm/client.py`. Тесты: `tests/test_llm_config.py`.

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `resolve_llm_config` | 26–71 | Собрать LLM-конфиг | Единая точка | `llm_client._resolve_cfg:36`, `llm_config.ensure_llm_env:86`, `audit_analyzer/scripts/llm.py` | Оставить |
| `ensure_llm_env` | 74–88 | Гарантировать `LLM_API_KEY` в env | Для `${VAR}`-резолва | `audit_analyzer/scripts/llm.py` | Оставить |

---

## `lib/services/llm_usage_store_factory.py` — 85 LOC

**Назначение.** Создание `nanobot.llm_usage.store.LLMUsageStore` по конфигурации.

**Что делает.** `create_usage_store(usage_config)` при `enabled: false` возвращает `None`
(graceful degradation), иначе берёт `sqlite_path` из конфига или `_default_sqlite_path()`
(`~/.cache/nanobot/usage/usage.db`), `mkdir(parents=True, exist_ok=True)` и создаёт store.

**Зачем нужен.** Единственный владелец `LLMUsageStore` в приложении (в проде см. выше —
владелец store, который никто не наполняет).

**Вердикт.** `Упростить`
**Обоснование.** Сама фабрика нужна, но (а) **docstring `:36` лжёт**: ссылается на
`nanobot/llm_usage/store.py: _DEFAULT_DB_NAME`, которого нет; upstream-путь —
`get_config_path().parent / "llm_usage.sqlite3"` (проверено), т.е. наш файл и файл upstream'а
различаются (`.gitignore:83-85` игнорирует именно `llm_usage.sqlite3`); (б) после починки
`agent_factory.py:208` `_default_sqlite_path` стоит либо синхронизировать с upstream'ом,
либо явно задокументировать как «наш, отдельный».

**Доказательства.** `lib/core/application_context.py:1767-1770` → `ctx.usage_store`
(единственный потребитель); закрывается в `application_context.stop()` (`usage_store.close()`).
Тесты: `tests/contract/test_usage_store_api.py` (тесты самого upstream-класса, не фабрики).

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_ensure_dir` | 24–27 | `mkdir(parents=True, exist_ok=True)` | Директория под SQLite | `create_usage_store:78` | Оставить |
| `_default_sqlite_path` | 30–38 | `~/.cache/nanobot/usage/usage.db` | Фолбэк пути | `create_usage_store:74` | **Упростить** — поправить docstring `:36`; продублирована в `session_cold_sync_service.py:664-669` (та мёртвая) |
| `create_usage_store` | 41–85 | Создать store или `None` | DI для observer'а | `application_context.py:1767` | Оставить |

---

## `lib/services/transcription_service.py` — 82 LOC

**Назначение.** Тонкий доступ к настройкам транскрибации аудио (provider / api_key / base_url / language).

**Что делает.** Читает `SETTINGS.channels.<CHANNEL>.transcription` и `skills.legal_summarizer`
для `transcription.*`; каждый геттер глушит `AttributeError` и возвращает `""`/`None`.

**Зачем нужен.** `PostgresChannel` (`lib/channels/postgres_channel.py`) должен знать язык и
endpoint, не таская `SETTINGS` напрямую.

**Вердикт.** `Упростить`
**Обоснование.** Четыре «мягких» геттера — правильная идея, но реализация — почти пустая
обёртка, а docstring `get_language` **лжёт** про трактовку `""`.

**Доказательства.** `lib/core/application_context.py` (создание),
`lib/channels/postgres_channel.py` (потребление `transcription_language`). Тесты:
`tests/test_transcription_service.py` (2 899 B — покрыты `None` и «нет атрибута», **не**
покрыта ветка `""`).

#### class `TranscriptionService` (23–82, 5 методов)

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 31–32 | Сохранить `config` | DI | `application_context.py` | Оставить |
| `provider` | 35–40 | `"openai"`/`"groq"`/`""` | Выбор endpoint | `postgres_channel.py` | Оставить |
| `get_api_key` | 42–55 | Ключ активного провайдера | Заголовок авторизации | `postgres_channel.py` | Оставить |
| `get_base_url` | 57–70 | Базовый URL транскрипции | `""` ⇒ стандартный endpoint | `postgres_channel.py` | Оставить |
| `get_language` | 72–82 | Язык распознавания | Параметр провайдера | `postgres_channel.py` | **Упростить** — либо исправить docstring `:75-77` («`""` трактуется как `None`» — неверно), либо реально приводить `""` к `None` |

---

## Сводка по вердиктам

| Файл | Оставить | Упростить | Удалить | Слить с | Всего символов |
|---|---|---|---|---|---|
| `db_logging_service.py` | 28 | 7 | 1 | 1 | 38 |
| `db_logging_bus.py` | 2 | 0 | 0 | 0 | 2 |
| `session_cold_sync_service.py` | 19 | 7 | 1 | 0 | 30 |
| `config_service.py` | 6 | 3 | 0 | 0 | 10 |
| `text_splitter.py` | 6 | 0 | 0 | 0 | 6 |
| `llm_client.py` | 5 | 0 | 0 | 0 | 5 |
| `subprocess_manager.py` | 2 | 1 | 2 | 0 | 6 |
| `session_storage.py` | 2 | 1 | 0 | 0 | 3 |
| `llm_observer.py` | 2 | 0 | 0 | 1 | 3 |
| `llm_config.py` | 2 | 0 | 0 | 0 | 2 |
| `llm_usage_store_factory.py` | 2 | 1 | 0 | 0 | 3 |
| `transcription_service.py` | 3 | 1 | 0 | 0 | 6 |
| **Итого** | **77** | **21** | **4** | **2** | **114** |

Итог по символам: 114 (10 классов + 80 методов + 24 функции уровня модуля;
`_count_by_type` в таблице методов `db_logging_service.py` — это тот же символ, что и
функция уровня модуля, не считается дважды), `НЕ РАЗОБРАНО: 0`.

## Функциональные баги

1. **`agent_factory.py:208` — observer-пайплайн LLM не подключён** (баг №1 выше).
   Воспроизведено: `AgentFactory._wrap_provider_snapshot_loader(Config(), ..., None) → None`.
   Последствие: `LLMUsageStore` не наполняется; источника LLM-usage в системе нет
   (дубль `event_type="llm_usage"` запрещён `tests/test_storage_hybridization.py:160`).
2. **`session_cold_sync_service.py:170/176/239` — финальный flush при `stop()` ничего
   не синкает.** Воспроизведено: 2 сессии на входе → `sessions_synced=0`, cleanup не выполнялся.
3. **`session_cold_sync_service.py:464-484` + `:486-496` — advisory lock не покрывает
   полезную работу** ⇒ leader-election между инстансами отсутствует фактически.
4. **`db_logging_service.py:223/281/283/287` — утечка worker-потока при переполненной
   очереди на shutdown:** sentinel отброшен, `_stop_event` никогда не читается, `_thread`
   обнуляется ⇒ сервис считает поток остановленным, а он живёт.
5. **`db_logging_service.py:769` — поток `agent_question_runs` отключает purge целиком**
   (проверка `_purge_old` стоит за `continue`).
6. **`db_logging_service.py:571` — `log_error` хардкодит `error[:200]`**, игнорируя
   настройку `logging.db.summary_max_chars`, которую применяет `log_sync_event:588`.

## Кросс-подсистемные находки

1. **Мёртвые настройки** (объявлены и валидируются, но не читаются):
   `logging.db.dialect` (`project.json:518` → `application_context.py:1203` → `db_logging_service.py:211`),
   `logging.db.connect_backoff_sec` (`:519` → `:215`),
   `logging.db.connect_backoff_max_sec` (`:520` → `:216`).
   Последние две — остаток от перехода на общий пул `utils.db` (см. `_db_run:802-808`).
   **Требуемая работа:** удалить ключи из `project.json`, из `__init__` и из
   `REQUIRED_KEYS` в `tests/test_config_keys.py:174-176`.
2. **Немёртвый, но незадекларированный конфиг:** `logging.db.retention_days` и
   `logging.db.purge_interval_sec` читаются (`application_context.py:1207-1208`) с
   дефолтами 90/3600.0, но **отсутствуют в `project.json:509-522`**, хотя `AGENTS.md`
   описывает их как настраиваемые. Либо добавить в `project.json`, либо поправить `AGENTS.md`.
3. **Пропуск в `REQUIRED_KEYS`:** `logging.db.question_runs_table` отсутствует
   (`tests/test_config_keys.py:161-180`), хотя при пустом значении
   `_make_db_logging` (`application_context.py:1195-1196`) кидает `ConfigurationError`.
4. **Требование спеки не выполнено:** `openspec/specs/runtime/entrypoints/spec.md:569`
   требует удаления `SubprocessManager.spawn_streamlit` и запрет `gateway.py` поднимать
   streamlit; change `remove-streamlit-runtime` в `openspec/changes/` отсутствует,
   `streamlit_app.py` на месте, `AGENTS.md` перечисляет его как точку входа.
5. **Дрейф документации:** `call_llm_async` упоминается в `AGENTS.md`, `CHANGELOG.md`,
   `docs/ARCHITECTURE.md:1564` — функции нет.
6. **Дрейф документации:** docstring `db_logging_service.py:43` и `:588` называют удалённый
   `PgDuckDbSyncService`; `session_cold_sync_service.py:20-22` и `:465-471` описывают
   несуществующий leader-election; `session_cold_sync_service.py:667` и
   `llm_usage_store_factory.py:36` лгут о перекрёстном использовании/имени файла.
7. **Два разных файла LLM-usage:** `~/.cache/nanobot/usage/usage.db` (наш) против
   `get_config_path().parent / "llm_usage.sqlite3"` (upstream). После починки observer'а
   надо решить, какой из них канонический; `.gitignore:83-85` игнорирует только upstream'овский.
8. **Мёртвый путь синхронизации:** `log_sync_event` — единственная функция `DbLoggingService`,
   которую спека (`logging-db/spec.md:414,425,457`) считает обязательной, но не вызывает
   ни один продюсер; `session-hybridization/spec.md:117` тоже ссылается на неё.
9. **`get_stats()` без потребителей:** ни `DbLoggingService.get_stats()`, ни
   `SessionColdSyncService.get_stats()` не вызываются из рантайма — `lib/services/runtime_health.py`
   о них не знает. Метрики доступны только вручную (тесты, `TROUBLESHOOTING.md`).
10. **Долг границы слоёв** (помечен в самом коде): `tests/test_dependency_direction.py:143-144`
    — skill'ы импортируют `lib/services/llm_client`; в `AGENTS.md` заявлено, что
    `lib/core/skill_config.py` — единая точка для skill'ов, но LLM-клиент остался в `lib/services/`.
11. **Q2 (владение DuckDB-соединением) — дублей нет:** низкоуровневое выполнение SQL
    существует в одном экземпляре — `lib/utils/duckdb_query.py` (владельца этого файла —
    группа 07). `lib/services/duckdb_cache_store.py` его **импортирует**
    (`rewrite_duck_sql`/`run_query`), а соединение открывает/закрывает сам в `_read_conn()`;
    `lib/services/cache_provider_impl.py::list_runtime_vector_indexes` файл не открывает
    вовсе (требует `fetch_fn`). В `lib/services/duckdb_query.py` файла **не существует** —
    путь из задания относится к `lib/utils/`.

## Проверки, выполненные

- `python -m pytest tests/test_db_logging_service.py tests/test_session_cold_sync_service.py
  tests/test_llm_config.py tests/test_session_storage.py tests/test_subprocess_manager.py
  tests/test_text_splitter.py tests/test_transcription_service.py tests/test_config_service.py
  tests/contract/test_llm_observer_api.py -q` → **146 passed in 31.78s**.
- Эмпирическая проверка observer'а: `python -c "from nanobot.config import Config;
  from lib.core.agent_factory import AgentFactory; print(AgentFactory._wrap_provider_snapshot_loader(Config(), object(), None))"`
  → `None`; `hasattr(Config(), 'build_provider_snapshot')` → `False`;
  `nanobot.providers.factory.build_provider_snapshot` существует как модульная функция.
- Эмпирическая проверка финального flush'а `SessionColdSyncService` (in-process harness,
  `_running=False`, 2 upstream-сессии) → `sessions_synced=0`, cleanup не выполнялся.
- Интроспекция `nanobot.llm_usage` (`llm_usage_store_path()`, `get_data_dir()`,
  `LLMUsageStore.__init__`) для сверки пути/имени файла.
- Поиск `RuntimeError` при итерации `queue.Queue.queue` / `deque` под конкурентным
  `popleft()` (CPython 3.14) — **не воспроизводится**; в отчёте отмечено как
  хрупкость (доступ к внутреннему атрибуту без `q.mutex`), а не как баг.
