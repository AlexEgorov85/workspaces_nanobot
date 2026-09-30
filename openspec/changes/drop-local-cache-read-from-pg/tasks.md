# Tasks: кэш — снимок на момент загрузки, чтение без удержания файла

Принцип исполнения: **один механизм на каждую операцию, дублирование
запрещено**. Каждая задача фазы проверяется одним конкретным способом.

Статус отмечен: `[x]` — выполнено, `[ ]` — к исполнению.

Итог: **45 из 45 задач выполнены.** Две задачи (4.3 и 6.2) закрыты не правкой
кода, а решением владельца от 2026-09-30, менявшим формулировку и судьбу
смежного change'а; обе переписаны явно, чтобы след решения был виден в
артефакте, а не молча убран. Единственное действие, оставшееся за владельцем, —
удалить каталог superseded-change'а (см. задачу 6.2).

## Фаза 0 — Откат политико-ориентированного слоя

`CachePolicy` и `CacheEngine` создавались под два кэша. С одним кэшем все шесть
полей политики — константы.

- [x] 0.1 Удалить `lib/services/cache_engine.py`.
- [x] 0.2 Удалить блок `CachePolicy`, пять перечислений
      (`CacheDirection`/`CacheHold`/`CacheRefresh`/`CacheRetention`/`CacheRowState`)
      и третий namespace (`_policies`, `register_cache`, `get_cache`,
      `cache_policies`, `unregister_cache`) из `lib/services/table_registry.py`;
      восстановить `clear()`; убрать импорт `Enum`, если он больше не нужен.
- [x] 0.3 Удалить `tests/test_cache_engine.py` (29) и `tests/test_cache_policy.py` (30).
- [x] 0.4 Проверить, что ни один потребитель не импортирует `CachePolicy` /
      `CacheEngine`.
      **verify:** `grep -rn "CachePolicy\|CacheEngine\|cache_engine" lib tools tests workspace` — ноль; `python -m pytest tests/test_table_registry.py tests/test_single_cache_interface.py -q` — зелёные.

## Фаза 1 — Удаление слоя владения

- [x] 1.1 Перенести `CacheAccessMode` в `lib/services/cache_provider.py`.
- [x] 1.2 Обновить импорты: `duckdb_cache_store.py`, `skill_config.py`,
      `vector_index_service.py`, `tools/check_indexes.py`,
      `tools/build_vectors.py`, `tests/test_cache_provider_open_failure.py`,
      `tests/test_cache_provider_mode.py`.
      **verify:** 21 тест `tests/test_cache_provider_mode.py` — зелёные.
- [x] 1.3 Удалить fencing из `pg_duckdb_sync_service.py`: kw-only параметр
      `ownership_coordinator`, поле `_ownership_coordinator`,
      `_handle_ownership_lost`, `_initial_load_with_fence`,
      `_sync_cycle_with_fence`.
      **verify:** `grep -n "fence\|ownership\|Ownership\|generation" lib/services/pg_duckdb_sync_service.py` — ноль.
- [x] 1.4 Убрать claim из `application_context.py`: импорт координатора,
      `coord.try_claim()`, лог claim'а, выбор режима от claim'а, ветка
      «READER, sync не создаётся», передача `ownership_coordinator=coord`,
      `ownership_coordinator.release()` в `stop()`. `_make_sync_services`
      возвращает 2-кортеж.
      **verify:** `grep -n "claim\|coord\|CacheOwnership" lib/core/application_context.py` — ноль (кроме пометки DEPRECATED и `ShutdownCoordinator`).
- [x] 1.5 Удалить `lib/services/cache_ownership.py` (438 строк).
- [x] 1.6 Удалить `tests/test_cache_ownership_claim.py` (59 вхождений).
- [x] 1.7 Переписать `tests/test_application_context_cache_lifecycle.py` под
      новую схему (9 тестов проверяют удалённый слой).
      **verify:** весь файл зелёный.
- [x] 1.8 **Судьба `V005`.** Проверить `tools/migrate.py` на БД без применённой
      V005 и на уже применённой. Решение: файл
      `sql/migrations/V005__create_agent_cache_ownership.sql` удаляется —
      для чистых БД таблица не создаётся, для применённых остаётся мёртвой.
      **verify:** `python tools/migrate.py --status` на обеих базах; решение записано в change.
- [x] 1.9 Гарантировать **единственность определения** `CacheAccessMode`
      (риск из design D). Дубликат означает, что `READ_ONLY`-защита молча
      перестаёт срабатывать.
      **verify:** `grep -rn "class CacheAccessMode" lib tools tests workspace` — ровно одно вхождение.

## Фаза 2 — Загрузка кэша как разовая синхронная операция

Цель: один механизм загрузки, ноль фоновой работы, один путь записи.

- [x] 2.1 Зафиксировать baseline: состав и объём `cache.duckdb`, длительность
      загрузки, **свободные слоты пула до и после** (`utils.db.get_stats()`).
      **verify:** значения записаны в этот change.
      **Снято на живой PostgreSQL 13.22** (профиль `prod`), 2026-09-30.
      Таблиц в снимке — **6**, строк — **391**, файл — **3 852 КБ**; загрузка
      **6.20 с** на холодную и **1.04 с** на тёплую. Слоты пула: `max_conn=4`,
      `connected_workers=0` до загрузки и **0 после 20 вызовов `query_sql`**;
      сразу после загрузки — 2 (тёплые воркеры пула, а не удержание
      загрузчика). Полная таблица — «Baseline» в `design.md`.
- [x] 2.2 Загрузка выполняется **синхронно** в `ApplicationContext.create()`
      (`_init_cache_runtime`, стадия 1) и не порождает потоков. Фоновый
      `_worker` с очередью и `set_on_*_callback` удаляется.
      **verify:** тест: после `start()` в процессе нет потока синхронизации — `tests/test_cache_load_service.py::TestMaxWorkers`.
- [x] 2.3 Удалить инкрементальные пути записи: `_poll_changes`,
      `set_on_new_records_callback` и `upsert_records` из рантайм-пути.
      `upsert_records` сохраняется как примитив `CacheIngestion`
      (используется фикстурами `tests/test_duckdb_cache_store.py`), но MUST
      NOT вызываться из production-кода.
      **verify:** `tests/test_cache_load_service.py::TestLoad::test_uses_single_write_path`; `tests/test_cache_load_service.py::TestLoadServiceHasNoBackgroundMachinery` — зелёные.
- [x] 2.4 Удалить параметры непрерывного режима из кода и конфигурации:
      `poll_interval_sec`, `max_queue_size`, `reconnect_backoff`,
      `reconnect_backoff_max`, `full_resync_every`.
      **verify:** секция `gateway.sync.*` убрана из `project.json` и `SyncSettings`; `REQUIRED_KEYS` очищен; `tests/test_config_keys.py` — 180 тестов зелёные.
- [x] 2.5 Убрать **дублирующуюся привязку** колбэков из `gateway.py:162` и
      `benchmarks/runner.py:620` — она дублирует composition root.
      **verify:** `grep -rn "set_on_new_records_callback\|set_on_replace_records_callback" gateway.py benchmarks tools lib` — ноль; ровно одно место привязки — в `cache_load_service.py`.
- [x] 2.6 Слот пула занимается только на время загрузки и освобождается.
      **verify:** тест: `test_load_runs_then_writer_is_closed` и `test_read_provider_opened_after_writer_closed` в `tests/test_application_context_cache_lifecycle.py`.
- [x] 2.7 PG недоступен на старте → кэш пуст, потребитель получает явную
      ошибку, устаревшие значения не подставляются.
      **verify:** `tests/test_cache_load_service.py::TestLoad::test_connection_loss_raises_cache_load_error` — обрыв соединения даёт `CacheLoadError`; `tests/test_application_context_cache_lifecycle.py::test_read_provider_opened_after_writer_closed` — провайдер не выдаётся вслепую. Отсутствующая таблица, в отличие от недоступного PG, исключением не является: `test_missing_table_recorded_not_raised`.

## Фаза 3 — Чтение без удержания файла

- [x] 3.1 `query_sql`, `get_schema`, `explain`, `search_vector`,
      `preload_indexes` открывают файл **на время операции** и закрывают сразу
      после. Постоянного соединения процесс не держит.
      **verify:** `tests/test_cache_no_file_hold.py::test_connection_closed_after_each_read` и `::test_schema_and_explain_also_release` (7 тестов в файле).
- [x] 3.2 Измерить плату формы: стоимость запроса при открытии на операцию
      против постоянного соединения; отдельно — стоимость `search_vector`,
      который подтягивает payload каждого hit'а.
      **verify:** числа записаны в этот change; при существенной разнице — предложить кэш payload в RAM (следствие той же схемы, не её изменение).
      **Снято:** `query_sql` — **13.9 мс** на операцию на живой базе против
      ≈ 0.257 мс на постоянном соединении, то есть ≈ 15 мс на запуск навыка
      (2 запроса). Вывод: плата приемлема, кэш payload в RAM **не предложен** —
      он вернул бы удержание файла, то есть откатил бы решение фазы 3.
      `search_vector` на живых индексах — 1.91 с на 3 hit'а с полной
      гидратацией payload. Таблица — «Baseline» в `design.md`.
- [x] 3.3 Тест на два процесса: gateway работает, второй читает кэш без отказа.
      **verify:** `tests/test_cache_no_file_hold.py::test_file_readable_by_another_process` — файл открывается на чтение из другого процесса, пока провайдер не держит writer.
- [x] 3.4 **Граница «навык ↔ базовый интерфейс».** Убрать утечку реализации в
      код навыков и их документацию: `predefined/db_loader.py` и
      `predefined/mode.py` называли `DuckDbCacheStore`, «generic DuckDB-сервис»
      и путь `workspace/data_store/duckdb/cache.duckdb`. Заменено на
      формулировки про базовый интерфейс: `DuckDBService` →
      `CacheQueryService` (срез `CacheProvider`), `DuckDBServiceProtocol` в
      `__all__` → `CacheQueryService`, «DuckDB-PG снимок» → «локальный кэш
      (снимок на момент загрузки)».
      **verify:** `tests/test_skill_cache_boundary.py` — 12 тестов, зелёные.
      **Поправка к verify этого пункта.** Первоначальная формулировка требовала
      нуля вхождений подстроки `duckdb` по всему `workspace/skills`. Она
      невыполнима и не соответствует замыслу: собственные тесты навыка
      (`audit_analyzer/tests/`) собирают in-memory базу движка как фикстуру —
      это test doubles, а не знание навыка о хранилище, — а доктринги могут
      называть диалект запросов. Гард проверяет то, что действительно является
      утечкой: имена конкретных реализаций и путь к файлу кэша — во всём коде
      навыков; имена по движку — в идентификаторах и `__all__`; каталог
      `tests/` внутри skill'а исключён осознанно. Это зафиксировано в
      докстринге гарда, чтобы исключение не выглядело лазейкой.
- [x] 3.5 Гард границы: тест запрещает появление имён конкретных реализаций,
      пути файла и термина «DuckDB» в коде и документации навыков. Рядом с
      существующим `tests/test_single_cache_interface.py`.
      **verify:** тест падает на искусственно добавленном упоминании —
      **проверено мутационно**, три независимые мутации в
      `predefined/mode.py`, каждая поймана и каждая откачена:

      | Мутация | Пойман тестом |
      |---|---|
      | `PROBE = "DuckDbCacheStore"` | `test_skill_code_does_not_name_concrete_implementation` |
      | путь `/tmp/data_store/duckdb/cache.duckdb` | `test_skill_code_does_not_reach_cache_file` |
      | `def DuckDBAdapter:` | `test_skill_code_has_no_implementation_named_identifiers` |

      После отката файл восстановлен побайтово (0 следов, контрольный прогон
      12 passed).
- [x] 3.6 Проверить, что навык получает роль только для чтения: путь
      `skill-side build_cache_provider()` → `lib.core.skill_config` →
      `open_cache_provider(mode=READ_ONLY)` возвращает `CacheProvider`, и
      роль `CacheIngestion` оттуда недоступна.
      **verify:** `tests/test_cache_readiness_and_skill_role.py` (8 тестов) —
      аннотация возврата `CacheProvider`, запрошенный режим `READ_ONLY`
      (проверяется подменой `open_cache_provider` и подписью перехвата), и
      структурное разделение ролей: `CacheProvider` не наследует
      `CacheIngestion`, ни один метод записи не просочился в роль чтения.

## Фаза 4 — Порядок старта и готовность

- [x] 4.1 Порядок: пул + валидация схемы → **загрузка (RW) → close** → прогрев
      FAISS → проверка подписей и состава → READY.
      **verify:** `tests/test_application_context_cache_lifecycle.py::test_has_single_synchronous_entry_point` и `::test_load_runs_then_writer_is_closed` — загрузка идёт в `create()`, единственной стадией; провайдер чтения открывается только после закрытия writer'а.
- [x] 4.2 `runtime_health` выставляет READY только после загрузки; до готовности
      запросы дают явную ошибку неготовности, а не пустой результат.
      **verify:** `tests/test_cache_readiness_and_skill_role.py` (5 тестов) —
      проверяется **настоящая** production-проверка
      `application_context._register_readiness_checks`, а не её копия:
      кэш загружен → `duckdb_cache` UP и итог READY; кэша нет / `is_ready()`=False
      / `is_ready()` бросает → required-компонент DOWN и итог NOT_READY.
      Вторая половина: до готовности `execute_readonly` возвращает словарь с
      `error`, а не пустой список, — иначе «нет данных» неотличимо от «нет
      связи».
- [x] 4.3 Признак времени последней загрузки опубликован **ровно одним
      каналом**, чтобы потребитель мог оценить актуальность данных.
      **Формулировка переписана решением владельца от 2026-09-30.** Первоначально
      задача требовала доступности времени «и в выдаче `query_sql`, и в
      health-summary». Это требование отменено: кэш — снимок, а не живая
      проекция, и повторение одного признака во втором месте создало бы два
      источника истины для одного факта. Единственный канал — событие
      `event_type="cache_load_done"` с полем `payload.loaded_at` в
      `agent_gateway_logs`; агент читает его tool'ом `history_search`.
      **verify:** `tests/test_cache_load_service.py::TestLoad::test_snapshot_time_published_in_event` — `loaded_at` публикуется событием;
      `tests/test_cache_load_service.py::TestLoad::test_records_load_time` — фиксируется при загрузке.
      Спеки приведены в соответствие: `data/cache-provider` («Кэш — снимок на
      момент загрузки») и `runtime/entrypoints` («Загрузка фиксирует время
      снимка») теперь прямо называют канал и запрещают дублирование в
      health-summary и в выдаче запроса.
- [x] 4.4 `ApplicationContext.stop()` закрывает провайдер кэша.
      **verify:** `tests/test_application_context_role.py::test_stop_closes_cache_provider` (+ `::test_stop_skips_close_when_no_cache_provider`, `::test_stop_is_idempotent_when_not_started`).

## Фаза 5 — Согласование со спеками и документацией

- [x] 5.1 `data/cache-provider` — дельта уже подготовлена: REMOVED ownership,
      MODIFIED 5 требований, ADDED 4 требования (снимок, чтение без удержания,
      отсутствие данных ядра, единственность enum).
      **verify:** `openspec validate drop-local-cache-read-from-pg --strict` → `Change 'drop-local-cache-read-from-pg' is valid`, exit 0.
- [x] 5.2 `runtime/entrypoints` — дельта подготовлена: REMOVED 2 ownership-
      требования, MODIFIED `CacheProvider API` и `CacheSyncService`.
      **verify:** тот же валидатор.
- [x] 5.3 `data/vector-indexes` — дельта подготовлена: файл открывается на
      время операции.
      **verify:** тот же валидатор.
- [x] 5.4 Убедиться, что `logging-db` не требует правок.
      **verify:** `openspec/specs/logging-db` не изменён этим change'ом (в `git status` отсутствует), и дельты `logging-db` в change'е нет.
- [x] 5.5 Обновить `AGENTS.md` (описание `cache_ownership.py`,
      `pg_duckdb_sync_service.py`, `CacheProvider`), `README.md`,
      `CHANGELOG.md`, `docs/DATABASE.md`, `docs/table-registry.md`,
      `docs/VECTOR_INDEXES.md`, `docs/ARCHITECTURE.md`,
      `docs/TROUBLESHOOTING.md`, а также устаревший ADR
      `docs/architecture/decisions/audit-analyzer-runtime-boundary.md:140-142`
      (дефект второго соединения закрыт в коде ранее).
      **verify:** `python -m pytest tests/test_docs_consistency.py -q` → 3 passed, 1 skipped.

## Фаза 6 — Актуализация прочих change

- [x] 6.1 `fix-cache-process-boundary` — сверить объём: дефект второго
      соединения закрыт в коде ранее, форма чтения закрывает остальное.
      **verify:** каждый пункт change'а либо закрыт, либо явно остаётся.
      **Сверка выполнена.** Дефект второго соединения закрыт (см. `design.md`,
      § про `list_runtime_vector_indexes` и `fetch_fn`), и форма чтения
      без удержания файла снимает его класс целиком. У change'а остаются
      открытые пункты **в другой области** — граница skill-CLI с
      `lib.services`: `predefined`-скрипты по-прежнему импортируют
      `read_vector_index_config` и `list_runtime_vector_indexes` напрямую
      (`workspace/skills/audit_analyzer/scripts/cli.py:88, 333`), плюс
      пункты 2.6, 6.1–6.3, 6.5, 6.6 (документация и `CHANGELOG`). Этот
      change их не закрывает и не претендует на это.
- [x] 6.2 `cache-architecture-alignment` — **закрыт как superseded решением
      владельца от 2026-09-30.**
      Конфликт дельт подтверждён сверкой: его
      `specs/runtime/entrypoints/spec.md` содержит требование «`CacheOwnershipCoordinator`
      MUST определять режим cache ДО открытия» и REMOVED «Ownership contract —
      atomic claim + real fencing», тогда как наш change удаляет координатор
      целиком. Оба варианта разрешения конфликта были рассмотрены владельцем;
      выбран первый: **не переписывать**, а закрыть, потому что область
      `cache-architecture-alignment` целиком покрыта этим change'ом.
      **verify:** в `proposal.md` проставлен явный статус **SUPERSEDED** с
      датой, указанием «не мержится», перечнем того, что его перекрыло, и
      ожидаемым результатом валидации после удаления.
      **Остаётся за владельцем:** удалить каталог
      `openspec/changes/cache-architecture-alignment/` вручную. Удаление через
      инструменты заблокировано политикой рантайма, обходные пути запрещены.
      После удаления `openspec validate --changes --strict` даёт
      **8 passed / 0 failed** — падение, оставшееся на 2026-09-30, закрыто.
- [x] 6.3 `split-core-and-skills-cache` и `migrate-cache-to-sqlite3` — отменены,
      удалены вручную владельцем.
      **verify:** оба каталога отсутствуют — проверено `Test-Path`, оба `False`.

## Фаза 7 — Проверка целостности

- [x] 7.1 `python -m pytest tests/ -q` — **без падений**: **3956 passed**,
      31 skipped, 1 xpassed за 110.7 с. Базовые 7 падений устранены (разбор
      ниже), так что «без новых падений» переросло в «полностью зелёный
      прогон».
- [x] 7.2 Ручной сценарий: gateway работает, CLI читает кэш навыков без отказа.
      **Снято на живой PostgreSQL:** `--list-scripts` (0.66 с), `--list-indexes`
      — успешно; чтение идёт через `CacheProvider` в READ_ONLY, writer уже
      отпущен.
- [x] 7.3 Ручной сценарий: `audit_analyzer` отдаёт predefined-запросы и
      vector search.
      **Снято на живой PostgreSQL:** predefined `analytics_by_year_month` —
      1.12 с на реальных данных; vector search по `violations_index`,
      `audits_index`, `audit_reports_index` — success. Попутно вскрылись и
      исправлены два молчаливых дефекта (см. «Дефекты, вскрытые сквозным
      прогоном»).
- [x] 7.4 Проверить по `utils.db.get_stats()`, что кэш не занимает слоты пула
      в рабочем режиме (сценарий D5). **Снято на живой PostgreSQL:** после
      загрузки `connected_workers=2/4`, и это **не** удержание загрузчика, а
      тёплые воркеры пула. До и после 20 вызовов `query_sql` занятых слотов
      **0 из 4**. Цель меры подтверждена: чтение кэша не расходует соединения
      PostgreSQL.
- [x] 7.5 Журналирование не изменилось: события пишутся в PG напрямую.
      **Проверено:** `cache_load_done` уходит через
      `CacheLoadService._log_sync_event` → `DbLoggingService.try_log_event` →
      `agent_gateway_logs`; модульного fallback'а нет. Дельта добавила поля в
      payload события, канал прежний —
      `tests/test_cache_load_service.py::TestLoad::test_snapshot_time_published_in_event`.
- [x] 7.6 `tools/architecture_guard.py` и `tools/diagnose_startup.py` — в
      startup-инвентаре не осталось упоминаний ownership.
      **verify:** `grep -n "ownership\|Ownership\|fenc"` по обоим инструментам
      и по `lib/services/runtime_inventory.py` — ноль.
- [x] 7.7 Фактическое сокращение подсистемы кэша: **−782 строки**
      production-кода (удалено 2254: `cache_ownership.py` 438,
      `pg_duckdb_sync_service.py` 905, тесты 853, `V005` 58; добавлено 472
      в `cache_load_service.py` плюс +89 правок в существующих файлах).
      **verify:** числа записаны в `CHANGELOG.md` вместе с change'ом.

## Дефекты, вскрытые сквозным прогоном по живой базе

Оба нашлись только на реальных данных — на фикстурах они не воспроизводятся,
потому что фикстура отдаёт вектор списком, а не строкой.

1. **`duckdb_cache_store.py::search_vector` — гидратация вне блока соединения.**
   Восстановление payload'ов шло с `conn=self._conn` уже **после** выхода из
   `with self._read_conn()`, где `_conn` к этому моменту равен `None`. Форма
   чтения (задача 3.1) вскрыла это: раньше постоянное соединение маскировало
   порядок выходов. Исправлено — гидратация перенесена внутрь блока.
2. **`lib/utils/duckdb_query.py::build_faiss_index` — тихий `(None, None)`.**
   Функция ждала `list`/`tuple`, а колонка `embedding` в снимке хранится как
   `VARCHAR` (тип `REAL[]` из PostgreSQL не переносится), поэтому индекс
   молча не строился, а вызов возвращал пустоту без ошибки. Добавлен парсер
   `_as_vector()` (str/bytes/list/tuple/ndarray), размерность берётся из
   разобранного вектора.

Покрыто `tests/test_vector_search_silent_failure.py` (14 тестов).

**Внешний follow-up, этим change не покрыт.** Корректнее чинить маппинг типов
при `ensure_schema`, чтобы `embedding` приходил в DuckDB массивом, а не
строкой. Парсер `_as_vector()` — компенсация на приёмной стороне; он делает
поиск рабочим, но не устраняет причину.

## Разбор падений, устранённых в фазе 7

Ни одно из семи не было следствием этого change — все оказались либо
рассинхроном окружения, либо устаревшими тестами, либо загрязнением состояния
между тестами. Важно: **ни одно не лечилось замазыванием** — везде чинилась
причина, а не симптом.

| # | Падение | Причина | Что сделано |
|---|---|---|---|
| 1 | `TestFlushIntervalSecPropagation` × 3 | тесты передавали `profile=` в `ApplicationContext.create()`, который по спеке `runtime/entrypoints` обязан отвергать его `TypeError` | убран `profile=`; фикстуре добавлен недостающий фейк `nanobot.agent.tools.registry` |
| 2 | `TestHookLoader` | тест создавал хук `my_hook.py`, которого нет в allowlist — проверял отказ, а не загрузку | имя приведено к allowlist-имени; добавлен отдельный тест, фиксирующий пропуск файла вне allowlist |
| 3 | `test_profile_lifecycle` × 3 | в окружении не было `streamlit` | установлен пин `streamlit==1.56.0` |
| 4 | `test_config.py` | фикстуры подменяли `sys.modules["config"]` фейком **без восстановления**; тест, идущий позже, читал фейковый `SETTINGS` | teardown восстанавливает настоящий `config` |
| 5 | `test_application_context.py` × 8 | та же фикстура не регистрировала подмодуль `nanobot.agent.tools`, хотя `agent_factory.py` его импортирует; в полном прогоне это маскировалось реальным импортом от других тестов | подпакет добавлен в фейк |
| 6 | `test_profile_lifecycle::test_cli_agent_starts_without_profile_flag` | `SessionStorageService` пишет resolved DSN в `os.environ` (в проде нужно для skill-subprocess); фейковый `postgresql://test` утекал в процесс pytest и наследовался subprocess-тестами, которые упирались в ретраи подключения к хосту `test` | teardown восстанавливает `DATABASE_URL` |
| 7 | `test_legal_summarizer_running_subprocess` | **две независимые причины.** (а) Стаб имитировал долгий вызов через `sleep(5.0)`, и тест требовал успеть прочитать RUNNING-маркер за это окно; под нагрузкой родитель не успевал, ребёнок завершался, и проверка «процесс жив при маркере» падала при полностью корректном коде. (б) Ребёнок наследовал мутированное окружение процесса pytest: к этому моменту другие тесты уже выставили `DATABASE_URL`/`LLM_API_KEY`, и CLI уходил в сетевые ретраи вместо печати в stdout | (а) `sleep` заменён на файл-сигнал от родителя — перекрытие гарантировано событием, тест стал строже. (б) ребёнку передаётся очищенное окружение без переменных, уводящих в сеть/БД: проверяемое свойство (отсутствие буферизации stdout) от них не зависит. Набор, где падение воспроизводилось, после правки: 497 passed за 19 с вместо 1 failed за 195 с |
| 8 | `test_utils_db.py::TestPool` — два теста | `test_third_transaction_waits_for_free_worker`: `both_held` был `Event`, который срабатывал от **первого** холдера, а нужен момент, когда заняты **оба** воркера. На загруженной машине второй поток не успевал взять воркер, третья транзакция честно занимала свободный — тест проходил по неверной причине. `test_auto_scale_when_worker_leased`: lease держался через `sleep(0.2)`, окно успевало истечь, обычная операция уходила на освободившийся воркер | первый: счётчик под блокировкой вместо `Event` (Barrier не годится — `wait()` звали трое); второй: `sleep` заменён на `Event`, главный поток отправляет операцию и лишь затем отпускает lease. Оба теста стали строже |

**Попутно найдено и исправлено в `workspace/utils/db.py`.** У `_Worker._ensure_connected()`
проверка «подключён ли» и сам `psycopg2.connect()` шли без синхронизации. Добавлен
`_conn_lock` с повторной проверкой под локом, retry-цикл вынесен в
`_connect_with_backoff()`. Оговорка: сегодня метод вызывается только из потока
самого воркера, поэтому пойманного двойного подключения нет — это защитная
фиксация инварианта «пул из 4 слотов не теряет соединения», того же ресурса,
ради экономии которого существует кэш.

**Расхождение окружения закрыто.** Проект пинит `nanobot-ai==0.3.5`,
`streamlit==1.56.0`, `sqlglot==30.17.0`; стояли 0.3.0 и ничего больше.
Побочный эффект, о котором стоит знать: без `sqlglot` не импортировался
`lib/utils/sql_safety.py` — то есть SQL Security Guard (P0-граница из
`docs/DATABASE.md`) в этом окружении просто не работал.

## Решения владельца, закрывшие последние две задачи

Обе задачи упирались не в правку кода, а в решение о целевой схеме. 2026-09-30
владелец принял оба; след решений зафиксирован в артефактах, а не убран молча.

**4.3 — время загрузки: один канал, не два.** Выбран вариант «переписать
формулировку»: время снимка публикуется **только** событием
`cache_load_done.payload.loaded_at`; health-summary и выдача запроса его не
повторяют. Отвергнутый вариант — протянуть `get_stats()` в health-summary
(≈10 строк). Причина выбора: кэш объявлен снимком, а не живой проекцией; два
места с одним и тем же фактом — это два источника истины, и они разъедутся,
как только одна из сторон забудет обновиться. Побочный эффект решения:
`CacheLoadService.get_stats()` остался методом без вызывающей стороны.

**6.2 — `cache-architecture-alignment`: superseded, не переписывать.** Выбран
вариант «закрыть как superseded» перед «переписать дельты на снимке и чтении
без удержания». Причина: область этого change'а покрыта
`drop-local-cache-read-from-pg` целиком, а его требование про
`CacheOwnershipCoordinator` в основные спеки попадать не должно — координатора
больше не существует, и переписывать текст вокруг отсутствующего механизма
значит закрепить в спеке несуществующую сущность.

## Осталось за владельцем

Одно действие, и оно не делегируется исполнителю: **удалить вручную** каталог
`openspec/changes/cache-architecture-alignment/`. Удаление файлов заблокировано
политикой рантайма, обходные пути (git rm, del, скрипты) запрещены. Статус
SUPERSEDED и ожидаемый результат проставлены в его `proposal.md`.

Ожидаемый результат после удаления — `openspec validate --changes --strict`:
**8 passed / 0 failed** (сейчас 7 passed / 1 failed, единственное падение —
этот каталог). Прогон `pytest tests/ -q --ignore=tests/integration` от удаления
не зависит и остаётся зелёным.
