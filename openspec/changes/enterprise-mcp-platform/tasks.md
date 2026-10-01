# Tasks — enterprise-mcp-platform

**Baseline (зафиксирован до изменений, ветка `refactor/mcp-platform`,
`BASE_COMMIT=8ef9d08`):** `4 failed, 4057 passed, 39 skipped, 1 xpassed`
за 134 с. Четыре предсуществующих падения:

| Тест | Статус |
|---|---|
| `tests/benchmarks/test_acceptance_matrix.py::test_acceptance_matrix_required_modules_exist` | ❌ |
| `tests/test_information_preservation.py::test_info_preservation_e2e_mock_passes_keywords` | ❌ |
| `tests/test_information_preservation.py::test_info_preservation_e2e_partial_summary_below_threshold` | ❌ |
| `tests/test_resume_scenarios.py::test_resume_integration_run_writes_manifest` | ❌ |

> **Правило применимости:** после шага 1 допустимы ровно эти же четыре падения.
> После фазы 5 (перенос кластера снимка) строка допустимых падений
> **пересобирается** — тесты кэша уезжают вместе с кодом, и чинить их на старой
> архитектуре бессмысленно.
>
> **Известный флак, не входящий в строку:** `test_legal_summarizer_running_subprocess.py::test_running_marker_arrives_before_run_completes`
> падает под нагрузкой полного прогона и проходит в изоляции. Проверяется одним
> запуском файла; зелёный — значит падение не связано с фазой. Подробности в
> `mcp-platform/docs/BASELINE.md`.

**Ограничение на каждом шаге:** не переносить несколько доменов за раз;
не удалять старую реализацию до прохождения интеграционных тестов;
не оставлять две рабочие реализации одного механизма.

**Один владелец на разделяемый ресурс.** Пул и очередь PostgreSQL, векторные
индексы и LLM-клиент принадлежат одному сервису каждый. Ни одна capability не
создаёт свой пул, свой FAISS или свой HTTP-клиент, и обход запрещён
архитектурным стражем, а не договорённостью — фаза 2, пункт 2.15.

**Порядок фаз жёсткий, но не из-за снимка.** Раньше capability `audit` (фаза 4)
обязана была переехать на PostgreSQL **до** удаления DuckDB (фаза 5): `db_loader`
читал реестр через `CacheProvider`, и обратный порядок оставил бы `run_script` без
источника. Снимок остаётся, `audit` читает его через capability `data` и файл не
открывает, поэтому это ограничение снято. Фазы 4 и 5 — переезд, не переписывание.

---

## Фаза 0 — мёртвый и сломанный код

- [x] 0.1 Удалить `workspace/utils/structure_cache.py` — импортирует несуществующий `extract_structure`
- [x] 0.2 Удалить `tools/extract_office_structure.py` — то же
- [x] 0.3 Удалить `lib/utils/table_utils.py` + `tests/test_table_utils.py`
- [ ] 0.4 ~~Удалить `lib/utils/retry.py`~~ — **перенесён в фазу 3** (пункт 3.14).
      `retry_on_exception` импортируется двумя живыми модулями:
      `cache_provider_impl.py:300` (ленивый импорт) и `llm_client.py:28`. Оба
      уезжают в `mcp-platform/libs/` только в фазе 3, поэтому удалять модуль
      в фазе 0 нельзя — это оставит два неразрешимых импорта. Дублировать
      реализаю в двух местах тоже нельзя.
- [ ] 0.5 Удалить `workspace/skills/audit_analyzer/err1.log` — **заблокировано
      политикой удаления**, файл не отслеживается git. Требуется ручное удаление.
- [ ] 0.6 Удалить `workspace/data_store/cache/**/*.py` (82 черновых скрипта) —
      **заблокировано политикой удаления**, файлы не отслеживаются git. Требуется
      ручное удаление. Удалять нужно **только** `.py`: в этих же папках лежат
      реальные результаты прошлых сессий (PDF, JSON, `attachments/`, `results/`),
      каталог `documents/` пуст.
- [x] 0.7 Поправить `AGENTS.md`: запись про `lib/utils/table_utils.py` не соответствует коду

**Смежные правки, не входившие в исходный пункт:**

- `tests/test_core_infrastructure_independence.py::CORE_SERVICES` — убраны
  `lib/utils/table_utils.py` (удалён) и `lib/services/pg_duckdb_sync_service.py`
  (удалён ранее, список фильтровался по `.exists()` и молча пропускал запись).
- `docs/ARCHITECTURE.md:1587,1600` — из дерева файлов убраны `table_utils.py`
  и `structure_cache.py`.
- `docs/architecture/nanobot-inventory.json` — перегенерирован
  (`python tools/scan_nanobot_inventory.py`).

**Приёмка:** `pytest` — те же 4 падения. `tools/architecture_guard.py` — exit 0.

> **Поправка к приёмке (проверено, 4-я неверная предпосылка плана):** критерий
> «`ruff check` — чисто» **не выполняется и не выполнялся** для главного проекта.
> Фактический baseline: `ruff check lib workspace` → **218 ошибок**, из них
> удалённые в этой фазе файлы приносили ровно 2 (`E402` в `test_table_utils.py`,
> `E501` в `extract_office_structure.py`). Изменение дало **−2 ошибки, ни одной
> новой**. `ruff check mcp-platform` — действительно чисто, это отдельная цель.
> Расчистка 218 ошибок — самостоятельная задача, не смешивать с миграцией.
> Косвенно то же подтверждает `docs/audit/reports/13-tools.md`: по всей команде
> CI — 653 ошибки, `tools/` и `tests/` исключены в `pyproject.toml`, но CI зовёт
> `ruff check lib workspace tests tools` явно.


---

## Фаза 1 — принятые удаления

Каждый набор — отдельный коммит, после каждого пересборка baseline.

- [x] 1.1 `agent_worker_claims`: DROP-миграция `sql/migrations/V006__drop_agent_worker_claims.sql`, удаление `sql/workers/`
- [x] 1.2 Удалить протокол аренды из `lib/channels/postgres_channel.py`
      (`_lease_loop`, `_reclaim_needed`, `_reclaim_and_heal`, `_delete_claim`;
      `_claim_one` + `_claim_one_single` слиты в `_claim_one`;
      `_release_all_leases` → `_return_claimed_to_pool`;
      `_leases` → `_claimed_ids`; удалён ставший неиспользуемым `import psycopg2`).
      **Без перехода на `FOR UPDATE SKIP LOCKED` — см. поправку ниже.**
- [x] 1.3 Удалить `tools/check_worker_pool_integrity.py` + тесты worker_pool
      (`tests/integration/test_worker_pool_concurrency.py`,
      `tests/integration/test_worker_pool_real_bot.py`,
      `tests/test_postgres_channel_static_audit.py`)
- [x] 1.4 Убрать ставшие мёртвыми настройки `channels.postgres.*`:
      **`claims_table`, `claim_strategy`, `lease_interval`** (не 8 — см. поправку).
      Попутно: ключ `claims_table` убран из `PROFILE_OWNED_RUNTIME_KEYS` и
      `EXPECTED_RUNTIME_TABLE_NAMES` (`config.py`) и из `_EXPECTED_KEYS`
      (`lib/services/schema_validation.py`) — иначе `validate_runtime_isolation`
      и проверка профиля падали бы с `ConfigurationError`. Проверка схемы
      теперь смотрит 5 runtime-таблиц, а не 6.
- [x] 1.5 Удалить `benchmarks/`, `tools/legal_benchmark.py`, `tools/test_audit.py`;
      DROP `agent_benchmark_runs`, `agent_benchmark_results`
      (`sql/migrations/V007__drop_benchmark_tables.sql`); удалить секцию `benchmark.*`.
      **`tools/legacy_audit.py` ОСТАВЛЕН** — план называл его служебным, но это
      движок живого guard'а `tests/test_no_legacy_imports.py`. **НЕ удалён**
      `tests/benchmarks/` — имя каталога вводит в заблуждение, это тесты
      quality-бенчмарков навыка `legal_summarizer`.
- [x] 1.6 Удалить `streamlit_app.py`, `lib/services/subprocess_manager.py`,
      spawn-логику в `gateway.py`, секцию `streamlit.*`,
      `tests/test_streamlit_app.py`, `tests/test_subprocess_manager.py`
- [x] 1.7 Удалить `workspace/tools/example.py` и запись `ExampleTool` из
      `runtime_inventory.py` (образец контракта Tool перенесён на
      `workspace/tools/history_search_tool.py`)
- [x] 1.8 Обновить `AGENTS.md`, `CHANGELOG.md`, `lib/channels/README.md`,
      `README.md`, `docs/ARCHITECTURE.md`, `docs/TROUBLESHOOTING.md`,
      `sql/README.md`, `openspec/specs/runtime/startup-schema-validation`
- [x] 1.9 Править `tests/test_config_keys.py` (`REQUIRED_KEYS`)

**Приёмка:** 4 101 → ожидаемо ~3 950 тестов; ни одного падения сверх предсуществующих.

### Поправки к фазе 1 (проверено, две неверные предпосылки плана)

**1.2 — `FOR UPDATE SKIP LOCKED` не делается, инструкция отменена.** Проект
разворачивается на **Greenplum 6.5 (ядро PostgreSQL 9.4**, `sql/README.md:197`),
где `SKIP LOCKED` (появился в PostgreSQL 9.5) недоступен, а Greenplum при
`SELECT ... FOR UPDATE` берёт блокировку уровня **ТАБЛИЦЫ** — такой захват
заблокировал бы всех читателей и писателей `agent_conversation_messages`.
Корректности `SKIP LOCKED` и не нужен: внешний `AND status = 'pending'` уже
исключает повторный захват, а `SKIP LOCKED` даёт только снижение задержки
при конкурентных захватах. Захват оставлен без изменений.

**1.4 — мёртвых настроек 3, а не 8.** `PostgresChannelSettings` содержит 8 полей,
но живыми в single-режиме остаются `poll_interval`, `unstick_interval`,
`processing_timeout`, `error_retry_delay`, `worker_id` (последний печатается в
логах и в выводе активности). Удалены только те, чей код удалён: `claims_table`,
`claim_strategy`, `lease_interval`.

### Обнаруженные дефекты — оба исправлены в фазе 3

Оба предсуществующие, оба — изменение поведения, а не удаление кода, поэтому
вынесены за рамки фазы 1 и потребовали отдельного решения. Решение принято
перед правкой фазы 3: пока повтор задачи был недостижим, а свободное `logger`
роняло ветку обработки ошибки, любое изменение захвата усложняло и без того
неоднозначный код.

1. ~~**Ветка повтора `status='error'` в `_claim_one` недостижима.**~~ **Исправлено.**
   Подзапрос выбирал задачу и по `error`, и по `pending`, но внешний
   `AND status = 'pending'` отсекал строку, выбранную по `error`. `_mark_failed`
   переводил задачу в `error` с обещанием вернуть её в пул после
   `error_retry_delay` — повторного захвата не происходило, задача оставалась
   в `error` навсегда. Настройка была контрактом без механизма.
   **Починка:** внешний `WHERE` повторяет условие подзапроса, а не сужает
   его, иначе внешний фильтр не перепроверяет под конкурентный UPDATE, а
   запрещает. Отсюда второй эффект, важнее самого дефекта: backoff-параметр
   теперь передаётся **дважды**, и в priority-пути список команд стоит между
   двумя его значениями. Порядок позиционных параметров —
   `(backoff, [команды], backoff)`; перестановка здесь не синтаксическая
   ошибка, а тихая подмена числа в `ANY(%s)`, и весь priority-путь молча
   перестаёт видеть задачи.
   Тесты: `tests/test_postgres_channel_error_retry.py` — 4 регрессионные
   проверки структуры SQL (внешний `WHERE` допускает `error`; не схлопнут до
   `pending`; backoff передан дважды; порядок параметров) плюс 3 инварианта
   (защита от `cancelled`, совпадение числа плейсхолдеров и параметров,
   priority-фильтр не уехал во внешний `WHERE`). Все 4 регрессионные
   проверены на тексте SQL из `HEAD` до починки — все 4 на нём падают.
2. ~~**`NameError` в `postgres_channel.py`**~~ **Исправлено.** Свободное имя
   `logger` вместо `self.logger` в обработчике исключения подписки
   `compaction_event_subscriber`: падение подписчика поднимало `NameError`
   поверх исходной ошибки, то есть маскировало её и теряло. Правка
   однострочная, тест — `test_send_does_not_raise_when_subscriber_fails`
   в том же файле.

### Номер миграции

`V006`, а не `V005`: в истории уже был `V005__create_agent_cache_ownership.sql`
(удалён вместе с `cache_ownership.py`), и базы, где он успел примениться, хранят
`005` в `public.schema_migrations`. Номера миграций не переиспользуются.

`gateway.py` и `cli_agent.py` стартуют.

---

## Фаза 2 — реестр инструментов, `libs/enterprise_data` и capability `data`

> Реестр идёт первым: capability `data` — первый, кто его использует.
> Сервер один (`enterprise-mcp`), поэтому всё дальнейшее — это добавление
> каталогов в `capabilities/`, а не новые процессы.

- [x] 2.1 `libs/enterprise_common`: `ToolDefinition` (name, description, handler,
      category, version, enabled, tags, permissions) и `ToolRegistry`
- [x] 2.2 Загрузчик: рекурсивный обход `capabilities/*/tools/*.py` →
      `create_tool(container)` → discovery, импорт, вызов точки входа,
      регистрация
- [x] 2.3 Валидация при загрузке: импорт, наличие `create_tool`, тип
      `ToolDefinition`, непустые `name`/`description`, уникальность `name`,
      callable `handler`, валидная схема аргументов
- [x] 2.4 **Fail-fast:** ошибка одного файла валит старт сервера целиком, с
      путём до файла и `name`. Тест на каждый пункт валидации
- [x] 2.5 `servers/enterprise/server.py` — bootstrap реестра без единого
      `@mcp.tool()`; переписать `servers/_template` под этот вид
- [x] 2.6 Перенести `workspace/utils/db.py` → `mcp-platform/libs/enterprise_data/db.py`.
      **Отклонение:** пункт сформулирован как «перенос», фактически это копия.
      Пул нужен обеим сторонам: канал агента уходит в MCP (2.18), но
      `pg_session_manager`, `session_cold_sync_service` и `context_compaction`
      по плану остаются в агенте и пишут в PostgreSQL напрямую. Удаление
      агентской копии — вместе с маршрутизацией этих трёх, отдельным пунктом.
      В платформенной копии `resolve_dsn()` переписан: `config` агента —
      запрещённый импорт, DSN приходит из `DATABASE_URL`/`PG_DSN`
- [x] 2.7 Перенести `lib/utils/sql_safety.py` → `mcp-platform/libs/enterprise_data/sql_safety.py`
- [x] 2.8 Перенести `workspace/utils/jsonb.py` и `clean_text.py`
- [x] 2.9 Перенести `tests/test_utils_db.py` и `tests/test_sql_safety.py`
- [x] 2.10 `capabilities/data/`: `service/` (логика, тестируется без MCP) +
      `tools/*.py` (по файлу на операцию)
- [x] 2.11 Операции capability `data` — **только инфраструктура**, без доступа к
      данным модели: `log_event`, `history_search` (изоляция по
      `user_id`/`session_id` как часть контракта), `schema_check`. Операции
      `query_sql` и `upsert_records` **не создаются**: произвольного SQL на
      поверхности агента не существует
- [x] 2.18 **Операции очереди задач** (`claim_task`, `update_task_status`) —
      решение владельца, сверх исходного списка 2.11. Канал агента обращается
      к capability `data`, а та читает таблицу задач и возвращает готовый
      формат. Право есть только у профиля `runtime`: модель, захватившая
      задачу, увела бы её у живого воркера. Операции зарегистрированы в MCP, но
      агент **не** включает их в список, отдаваемый модели, — фильтрация на
      стороне агента, а не на сервере
- [x] 2.12 **Два входа в очередь.** Сервис владельца пула получает
      `submit(job)` — блокирующий, для работы с данными, и `accept(event)` —
      неблокирующий, буфер писателя журнала. Одна очередь означает, что
      `generate_sql` с четырьмя вызовами LLM конкурирует с записью журнала за
      воркеры; потеря события безвозвратна и не сопровождается ошибкой
- [x] 2.13 **Обязать серверный предел стоимости запроса.** `statement_timeout` в
      коде нет: `psycopg2.connect` не передаёт `options=`, а `autocommit=True`
      делает `SET LOCAL statement_timeout` нооп. Внести либо на соединении в
      момент коннекта, либо в замыкании задания с явным сбросом. Внести
      `max_rows` на сервере, а не в SQL модели
- [x] 2.14 **Запретить старт без `sqlglot`.** Без AST-ветки guard проверяет
      только первый блокируемый оператор, `INTO` и multi-statement, и пропускает
      `pg_sleep`, `information_schema` и `UPDATE`/`DELETE` после `--`-комментария
- [x] 2.15 **Архитектурный страж сервисов.** Проверка, которая валит сборку при
      обходе: вне сервисов-владельцев нет `psycopg2.connect` / `*ConnectionPool` /
      `create_pool`, вне владельца индексов нет `faiss` и `IndexFlatIP`, вне
      `libs/llm` нет HTTP-вызовов провайдера. Сообщение называет файл и
      конструкцию. Аналог существующего
      `tests/test_storage_hybridization.py::TestNoDirectSQLToSessionTables`
- [ ] 2.16 Перенаправить потребителей на `enterprise-mcp`. **Частично закрыт:**
      `history_search` (решение владельца от 2026-09-30 — тонким адаптером, а не
      удалением). Агент больше не строит SQL: tool подставляет личность из
      `RequestContext` и вызывает операцию capability `data`. Клиент —
      `lib/services/enterprise_mcp_client.py`, объявление сервера одно
      (`project.json → enterprise_mcp`), `tools.mcpServers` в `config.json`
      намеренно пуст: вторая копия процесса была бы вторым владельцем пула.
      **Осталось:** `db_logging_service.py` и `schema_validation.py` — там
      агент выступает клиентом, а не модель, поэтому это отдельный механизм
- [x] 2.17 Архитектурный тест: `mcp-platform` не импортирует `nanobot`, `lib`, `workspace`
- [ ] 2.19 **Контракт идентичности на сервере.** Валидатор загрузки требует,
      чтобы `handler` каждой операции объявлял `session_id` и `user_id`: без
      любого из них сервер не поднимается. В опубликованной `inputSchema` оба
      параметра **необязательные** — иначе модель их заполняет. Вызов без
      идентичности → `identity_missing`, работа не выполняется. Тесты: операция
      без `session_id` роняет старт с именем файла; `session_id` отсутствует в
      `required` схемы; пустой вызов даёт `identity_missing`, а не результат
- [ ] 2.20 **Подстановка идентичности на стороне агента.** Патч
      `MCPToolWrapper.execute` вставляет `session_id`/`user_id` из
      `RequestContext` в аргументы **с перезаписью** значения, присланного
      моделью. Причина: `execute` отправляет в `tools/call` ровно `kwargs`
      модели (`arguments=kwargs`), поэтому объявить идентичность аргументом
      без этого патча — значит отдать её модели. Исключение из «патчи → хуки»:
      точки расширения для аргументов вызова в наноботе нет. Внести патч в
      `docs/architecture/runtime-patcher-inventory.md`. Тест: модель, приславшая
      чужой `session_id`, не влияет на изоляцию; буфер журнала снимает
      идентичность при записи, а не при flush'е (иначе батч переживёт
      `RequestContext` и начнёт молча ронять события)
- [ ] 2.21 **Подстановка `request_id`.** Тем же патчом агент подставляет
      `request_id` из `DbLoggingService.get_request_id(session_key)` — по
      умолчанию `null`, отсутствие не является ошибкой. Значение обязано
      совпадать с PK оборота в `agent_question_runs`, отдельный `question_id` не
      заводим. **Ограничение:** `user_id` берётся только из
      `RequestContext.sender_id` — lookup'ом `user_id` по индексу `session_key`
      пользоваться нельзя, доступ закрыт намеренно
      (`db_logging_service.py:253-256`). Тесты: вызов вне оборота проходит без
      `identity_missing`; `request_id` не меняет видимые данные; отмена
      `clear_request` не ломает вызовы

**Приёмка:** `cd mcp-platform && pytest` — зелёные. Сервер поднимается в
подпроцессе с заблокированным `import nanobot`. `capabilities/data/` не
содержит `psycopg2.pool` / `ThreadedConnectionPool` (единый пул). Добавление
файла в `capabilities/data/tools/` не требует правок `server.py`. Ни одна
зарегистрированная операция не принимает SQL от вызывающей стороны.

> **Приёмка неполная.** Пункт 2.16 открыт: канал, журнал и `history_search`
> в агенте по-прежнему ходят в PostgreSQL напрямую. Capability `data` готова
> принять их, но транспорта MCP-клиента в агенте нет. Маршрутизация канала —
> первый шаг фазы 3.

---

## Фаза 3 — capability `vectors` и `llm`

> `vectors` читает снимок через capability `data` и файл не открывает, поэтому больше не
> зависит от того, что будет с кластером снимка. Обе capability — каталоги в
> том же процессе, новых серверов не заводится.

- [x] 3.1 Извлечь из `lib/utils/duckdb_query.py` в `libs/vectors`:
      `build_faiss_index`, `group_vector_hits`, `build_raw_items`
- [x] 3.2 Извлечь из `lib/services/cache_provider.py` концепции `SearchResult`,
      `IndexIntegrityError`. **Решение:** одно определение живёт в
      `libs/enterprise_data/snapshot/contracts.py`, `libs/vectors` импортирует
      оттуда. Владелец индекса не знает о DuckDB, а хранилище не знает о
      FAISS, поэтому общее место у них — нейтральный модуль контрактов, а не
      один из владельцев
- [x] 3.3 Перенести векторную половину `cache_provider_impl.py`:
      `get_embedding`, `read_embedding_config`, `read_vector_index_config`,
      `compute/verify_index_signature`, `list_runtime_vector_indexes`
- [x] 3.4 Перенести `vector_index_service.py`, `text_splitter.py`, `preload_service.py`
- [x] 3.5 Чтение векторов оставить на снимке, но получать его **только** через capability
      `data`: ни собственного `duckdb.connect`, ни пути к файлу. Снимок открывает
      composition root (`server.py`), строки отдаёт `DataService`, владелец
      индекса получает их через `container.get("data")`. Снимок **не
      обязателен**: без него сервер поднимается, а чтение отдаёт
      `infrastructure_error` («снимок недоступен»), а не «индексов нет» —
      разница между «нечего искать» и «нечем искать» обязана быть видна
- [ ] 3.6 Перенести `tools/build_vectors.py` (сборка эмбеддингов), `tools/check_indexes.py`.
      **Не начато.** Обе утилиты работают через агентские `cache_provider` и
      `vector_index_service`, которые уезжают в фазах 4/5; до их переезда
      перенос утилит завёл бы вторую реализацию сборки
- [x] 3.7 Конфигурация: `gateway.vector.index.indexes.*` → конфиг `enterprise-mcp`.
      **Решение:** не «переезд», а передача. Агент остаётся источником
      истины: `project.json → gateway.vector.index` экспортируется в
      окружение процесса (`ENTERPRISE_VECTOR_*`), а платформа его читает —
      прямой импорт `config`/`project.json` запрещён стражем. Дублировать
      объявления индексов в конфиге платформы значило бы завести второе
      место, где они правятся. Путь снимка приходит из единственного
      механизма агента `resolve_cache_path()`
- [x] 3.8 Операции как `capabilities/vectors/tools/*.py` через реестр фазы 2:
      `vector_search`, `list_indexes`, `index_stats`
- [x] 3.9 **Ленивая загрузка индекса:** FAISS собирается по первому
      векторному запросу, не на старте процесса. Локом — параллельные запросы
      ждут один прогрев. Состояние (`нет` / `собирается` / `готов` / `ошибка`)
      наблюдаемо. `list_indexes` и `index_stats` отвечают без поднятия индекса
- [x] 3.10 Перенести `lib/services/llm_client.py` и `llm_config.py`
      → `mcp-platform/libs/llm/`. **Отклонение от «без изменения
      поведения»:** тексты двух ошибок называют переменные платформы вместо
      `agents.defaults.model`/`providers.*.apiBase` (ссылаться на источник,
      которого в процессе нет, было бы ложью), а `RuntimeError` стал
      `InfrastructureError` — незаданная модель означает неполную
      конфигурацию процесса, и retry для неё бессмыслен
- [x] 3.11 `capabilities/llm/`: `service/` (вызов провайдера) + `tools/complete.py`.
      Плюс операция `embed`: векторизация — тоже вызов провайдера, и её
      место у того же владельца HTTP
- [x] 3.12 Конфигурация провайдера и ключи переезжают из `config.json` агента.
      **Решение:** агент экспортирует их в окружение процесса
      (`ENTERPRISE_LLM_*`), платформа читает `os.environ`. Прямой импорт
      `config` запрещён стражем, а второй экземпляр конфигурации разошёлся
      бы с первым при первой же правке модели. **Резолв ленивый, не
      fail-fast в конструкторе:** один процесс обслуживает несколько
      capability, и забытая переменная провайдера не должна снимать из работы
      `history_search`. Незаданный провайдер отдаёт `infrastructure_error` на
      своей операции и виден как `configured: false` в health-отчёте
- [~] 3.13 Удалить `llm_client.py` и `llm_config.py` из агента. **Сделано,
      кроме самих файлов:** удаление восстановимое, а лаунчер на этой машине
      недоступен, поэтому два файла ждут ручного удаления — на них стоит
      `xfail` в `tests/test_llm_goes_through_mcp.py`, который сам станет
      зелёным после удаления. Потребители переведены:
      `lib/core/skill_config.py::get_llm_config` удалён (функции про LLM в
      runtime API больше нет), оба `scripts/skill_config.py` очищены,
      `_llm_env_from_settings` из `enterprise_mcp_client` удалён (агент больше
      не экспортирует `ENTERPRISE_LLM_*` и не знает адреса, модели и ключа),
      а `audit_analyzer/scripts/llm.py` и
      `legal_summarizer/scripts/llm/client.py` ходят в модель через
      `libs/enterprise_client/llm.py`. Настройки провайдера переехали в
      `platform.json` (секция `llm`), поэтому правка модели больше не
      трогает две копии. Ключ из `agents.defaults`/`providers.*` агента в
      окружение сервера больше не попадает.
- [ ] 3.14 Удалить `lib/utils/retry.py` из агента. **Заблокировано до фазы 5:**
      единственный оставшийся импортёр — `lib/services/cache_provider_impl.py:300`
      (ленивый импорт), а он уезжает вместе с кластером снимка. Платформа уже
      получила свою копию в `libs/enterprise_common/retry.py`

**Приёмка (как проверялось).** Сквозной прогон через настоящий процесс
`enterprise-mcp`, живая база и живые провайдеры:

- discovery отдаёт 10 операций: `log_event`, `history_search`, `schema_check`,
  `claim_task`, `update_task_status`, `complete`, `embed`, `vector_search`,
  `list_indexes`, `index_stats`;
- `history_search` (работавший до переноса) продолжает работать;
- `list_indexes` читает **настоящий** снимок: 3 объявленных индекса,
  состояние `missing`, FAISS не поднят;
- `vector_search` на индексе без векторов отдаёт `not_found` с текстом
  «индекс не загружен или пуст», а не «индексов нет»;
- `embed` возвращает вектор от локального эмбеддера (Ollama), `complete` —
  ответ облачного чат-провайдера;
- сессия закрывается чисто, без ошибок anyio на shutdown.

Платформа: **1188 passed**. Полный прогон агента — см. раздел «Приёмка фаз».

### Дефекты, найденные при переносе фазы 3 (все закрыты)

1. **Операции `llm` читали приватный `self._cfg` мимо ленивого свойства
   `config`.** Собранный сервером `LlmService()` (без аргументов) держал
   `self._cfg = None`, поэтому `complete` отдавал
   `[internal_error] AttributeError: 'NoneType' object has no attribute 'model'`
   — при живом и правильно настроенном сервере, то есть выглядело как поломка
   сервера, а не как недонастроенная capability. Юнит-тесты дефект не видели:
   они всегда передают готовый `config=`, и при нём `_cfg` заполнен сразу.
   Нашёл сквозной прогон через настоящий процесс. Проверено на коде до
   правки: тот же `AttributeError`, после — `OK text='ответ' model='model-env'`.
2. **Эмбеддер ошибочно считался тем же провайдером, что и чат.** Порт исходил
   из «один провайдер, одна конфигурация» и бил в `api_base` чат-провайдера +
   `api/embed` — на живом развёртывании гарантированный 404 (чат в облаке,
   эмбеддер локальный Ollama). Второе следствие того же допущения было хуже
   вслух: в теле уходила **модель чата** вместо модели эмбеддингов — вернулся
   бы 200 с вектором не той размерности, и подпись индекса разошлась бы с
   фактически использованной моделью ровно на той операции, ради которой всё
   затевалось. Добавлены отдельные `ENTERPRISE_EMBED_API_BASE/API_KEY/PATH/
   MODEL`; **пустые значения наследуют чатные**, поэтому конфигурация с одним
   провайдером не меняется.
3. **Вторая копия HTTP-клиента в `libs/vectors/embedding.py`** — поймана
   расширенным стражем (`httpx` → владелец `libs/llm`), который сработал на
   первой же проверке. Строковое правило по `chat/completions` её пропускало:
   у эндпойнта эмбеддингов такой подстроки в URL нет. Векторизация переехала
   в `libs/llm` как операция `embed` с отдельным правом `llm:embed`.


---

## Фаза 4 — capability `audit`: перенос конвейера

> **Объём фазы сокращён.** Раньше здесь был переезд на PostgreSQL: загрузчик
> реестра переписывался под `psycopg2`, схема по умолчанию менялась
> с `main` на `public`, а фаза 5 удаляла снимок. Снимок остаётся, поэтому 4.3 и 4.4
> **отменяются**: реестр и его шаблоны остаются в диалекте снимка. Требования к
> поверхности — проверка списка таблиц, потолок строк, запрет `context` от
> вызывающей стороны — сохраняются без изменений.

- [x] 4.1 Резолв `scripts_registry` из `TableRegistry` перенести в сервис
      capability `audit`; `resources_by_label("scripts_registry")` — единственный
      путь к имени таблицы реестра.
      **Сделано:** `libs/audit/registry_loader.py`. Метка реестра разрешается
      вызывающей стороной и приходит позиционным аргументом: платформе читать
      `project.json` запрещено стражем, и `resources_by_label` в коде платформы
      не встречается — только в докстринге, как «что поменялось»
- [x] 4.2 `db_loader` перенести в capability `audit` **с сохранением поведения**:
      чтение реестра идёт через операцию capability `data`, значение параметра —
      отдельным позиционным аргументом. Миграции на PostgreSQL нет.
      **Сделано:** тот же `registry_loader.py` — чтение через
      `reader(text, params)`, то есть колбэк `DataService.snapshot_query`; имя
      скрипта уходит параметром `?`, а не подстановкой в текст запроса
- [ ] 4.3 ~~Плейсхолдеры под диалект драйвера~~ — **ОТМЕНЕНО**. `sql_template` и загрузчик
      используют `?`, и это диалект снимка; менять его на `%s` без смены
      исполнителя нельзя
- [ ] 4.4 ~~Схема по умолчанию `public`~~ — **ОТМЕНЕНО**. `main` — схема исполнителя;
      подстановка `public` сломала бы шаблоны реестра, записанные без
      указания схемы
- [x] 4.5 Перенести пайплайн `predefined` (загрузчик, валидатор параметров,
      сборщик шаблона) в `capabilities/audit/service/`.
      **Сделано:** `libs/audit/predefined.py`; сервис импортирует его из
      `libs.audit`, собственный экземпляр на вызов не создаёт
- [x] 4.6 Перенести пайплайн `generated_sql`: описание схемы, few-shot из
      реестра, промпт с `<NO_MATCH>`, цикл попыток, `validate_sql`, `EXPLAIN`.
      **Сделано:** `libs/audit/generated_sql.py`; проверка синтаксиса идёт
      через `explain_query` снимка и обязательна
- [x] 4.7 **Проверка состава таблиц в коде.** Сегодня белый список существует
      только как текст в промпте: `validate_sql` проверяет вид оператора, но не
      имя таблицы, и `SELECT * FROM public.agent_gateway_logs` проходит без
      возражений.
      **Сделано:** `libs/audit/guard.py` + `test_audit_lib_table_guard.py`;
      белый список передаётся полностью квалифицированными именами
- [x] 4.8 **Потолок строк для сгенерированного запроса.** Сегодня `LIMIT`
      дописывает только сборщик predefined; сгенерированный запрос уходит в базу
      как есть.
      **Сделано:** `enforce_row_limit` / `DEFAULT_ROW_CEILING` в
      `libs/audit/generated_sql.py`, применение потолка **проверяется повторным
      разбором** запроса, иначе модель могла бы обойти его формой вложенного
      SELECT; `RowLimitNotAppliedError` + `test_audit_lib_row_limit.py`
- [x] 4.9 **Не принимать `context` от вызывающей стороны.** Аргумент подклеивает
      текст агента в начало сообщений генератору.
      **Сделано:** аргумента нет ни в одной операции — `capabilities/audit/tools/*.py`
      его не упоминают; личность подставляет вызывающий, закрывая колбэк
- [x] 4.10 Операции `capabilities/audit/tools/`: `list_scripts`, `run_script`,
      `generate_sql`. Ни одна не принимает SQL — проверено тестом на
      сигнатуру обработчика **и** на опубликованную схему, а не на текст
      описания. Все три помечены `runtime-only` с отдельным правом: это
      инфраструктура, а не инструмент в руках модели
- [x] 4.11 `list_scripts` отдаёт `parameters` объектом с типами, обязательностью,
      значениями по умолчанию и `validation`, а не склеенной строкой имён;
      добавляются `long_description` и `returns`. Поле `validation` присутствует
      всегда: его отсутствие читалось бы как «правил нет», а не «не задано»
- [x] 4.12 `run_script` **не возвращает текст SQL**; текст уходит в журнал
      процесса. Проверено тестом в обе стороны: в ответе ключа `sql` нет, и
      текст действительно попадает в журнал
- [x] 4.13 **Молчаливые пути убраны** на уровне capability: сломанный реестр
      даёт `registry_unavailable`, а не пустой каталог, который выглядел бы как
      «скриптов нет»; строка реестра без имени тоже отказ, а не тихий пропуск
- [x] 4.14 Единая модель ошибок: библиотека бросает доменное исключение с
      `code`, адаптер переводит его в код конверта по таблице
      (`not_found` → `not_found`, `validation_failed` → `invalid_params`,
      `generation_failed` → `not_answerable`, `guard_unavailable` →
      `upstream_unavailable`, …). Неизвестный внутренний код не доезжает до
      вызывающей стороны как есть — иначе модель не знает, что с ним делать.
      Таблица перевода зафиксирована тестом
- [ ] 4.15 Убрать регистрацию audit-tool'ов из `project_tool_loader` и удалить
      `scripts/cli.py` агента. **Заблокировано до фазы 9:** agent-tools для
      этих режимов в `project_tool_loader` не регистрировались и сейчас — доступ
      идёт через skill-side `scripts/cli.py`, который уезжает вместе с
      `audit_analyzer` в фазе 9. Проверено: удалять нечего.
      **Перепроверено 2026-10-01:** в `project_tool_loader.py` нет ни одного
      упоминания `audit` или `predefined` — первая половина выполнена. Вторая
      половина (удаление `cli.py`) — это работа фазы 9, пункт 9.2
- [x] 4.16 Разложить по capability `capabilities/audit/{service, tools/}`;
      `SKILL.md` навыка остаётся в агенте до фазы 9. Скилл и операции ведут к
      одному сервису: `container.get("audit")`, собственный экземпляр на
      операцию не создаётся.
      **Сделано:** `capabilities/audit/service/main.py` + `tools/{list_scripts,run_script,generate_sql}.py`;
      все три операции объявлены как `create_tool(container)`, то есть берут
      единственный сервис из контейнера

**Приёмка:** ни один зарегистрированный инструмент не принимает SQL. Реестр
читается через capability `data`, и capability `audit` не открывает файл снимка. Сгенерированный
запрос, ссылающийся на таблицу вне разрешённого списка, отклоняется
**до** выполнения. Скрипт с неизвестным именем возвращает `not_found`, сломанный
реестр — `registry_unavailable`, а не пустой каталог.

---

## Фаза 5 — перенос кластера снимка во владельца

> Фаза выполняется **после** фазы 4: обе capability читают снимок, и перенос
> кластера меняет только место жительства. Снимок **остаётся** — отменяется
> только выписывание его из проекта.

Атомарный коммит без MCP-работы. Ни одна строка кластера не удаляется.

- [x] 5.1 Перенести `duckdb_cache_store.py` (1455) в `libs/enterprise_data`.
      **Сделано частично, в фазе 3:** читающая половина портирована в
      `libs/enterprise_data/snapshot/`, писатель — `snapshot/writer.py`
      (закрыл `CacheIngestion`, который до этого был заглушкой с
      `NotImplementedError`)
- [x] 5.2 Перенести `cache_load_service.py` (472) — единственный писатель снимка.
      **Сделано:** `libs/enterprise_data/loader.py`. `load()` блокирует до конца,
      фоновых потоков не порождает, число потоков ограничено `max_conn`,
      ошибка соединения fail loudly, отсутствующая таблица даёт `missing_tables`
      без исключения, результат несёт `loaded_at`.
      `FOR UPDATE SKIP LOCKED` не переносится: целевая БД Greenplum 6.5 /
      PostgreSQL 9.4
- [x] 5.3 Перенести `cache_provider.py` (440) вместе с `CacheAccessMode`; в
      `libs/vectors` уходят `SearchResult` и `IndexIntegrityError`.
      **Сделано в фазе 3:** контракты в `snapshot/contracts.py`,
      `SearchResult`/`IndexIntegrityError` — **одно определение**, общее место
      `snapshot/contracts.py`, `libs/vectors` импортирует оттуда
- [x] 5.4 Перенести исполнитель запросов из `lib/utils/duckdb_query.py`:

      `run_query`, `explain_query`, `build_schema`, `rewrite_duck_sql`
- [x] 5.5 ~~Перенести~~ **Удалить** `_capture_schema_meta` из
      `cache_provider_impl.py`; остальное ушло в фазу 3.
      **Формулировка плана была неверна — переносить нечего.** У функции 0
      production-вызовов, она дублирует `DuckDbCacheStore._save_schema_meta`
      (константы `_META_SCHEMA`/`_META_TABLE` были объявлены в обоих файлах), и
      вердикт аудита — «Удалить» (`docs/audit/reports/02-services-cache.md`).
      **Сделано:** удалены функция, дубль констант и неиспользуемый импорт
      `CacheProvider, SearchResult` (из модуля их никто не импортировал);
      из `tests/test_cache_provider_meta.py` убраны 4 теста, 9 тестов
      `TestIndexSignature` остались — они живые. Инвариант повторной записи
      метаданных, который покрывал удалённый `test_capture_schema_meta_drops_previous`,
      перенесён на живой путь: `test_ensure_schema_replaces_previous_meta` в
      `tests/test_duckdb_cache_store.py`.
      **`sys.path`-хак (строки 247-250) оставлен:** около тридцати файлов в
      `lib/`, `tools/` и `workspace/` делают `from utils.db import ...`, а
      `gateway.py` добавляет пути уже внутри `main()`, то есть после импорта
      модулей. Удаление хака — отдельное решение на границе импортов.
- [ ] 5.6 Убрать cache-API из `lib/core/skill_config.py`. `TableRegistry` и
      `skill_registration.py` **остаются** — они описывают состав снимка
- [ ] 5.7 Секция `CacheSettings` переезжает из `project_settings.py` в конфиг
      сервера как путь снимка
- [ ] 5.8 Убрать 88 строк кэш-обвязки из `application_context.py`:
      `resolve_cache_path`, `_warn_if_cache_path_on_nfs`, `_init_cache_runtime`,
      `check_duckdb_cache`, `check_vector_search`
- [ ] 5.9 Удалить `sql/vectors/create_vector_index_config.sql` и
      `create_vector_index_store.sql` — это legacy прошлого шага, а не снимок
- [ ] 5.10 Перенести 10 тестовых модулей кэша в `mcp-platform` как стражи
      capability `data`: `test_duckdb_cache_store.py`,
      `test_cache_provider_meta.py`, `test_single_cache_interface.py`,
      `test_cache_no_file_hold.py`, `test_cache_provider_open_failure.py`,
      `test_cache_provider_mode.py`, `test_cache_load_service.py`,
      `test_table_registry.py`, `test_skill_cache_boundary.py`,
      `test_shared_cache_path_across_profiles.py`
- [x] 5.11 Добавить страж: вне `libs/enterprise_data` запрещены `duckdb.connect`,
      `ATTACH` и импорт `duckdb`.
      **Сделано — пункт был выполнен, но не отмечен** (проверено чтением кода):
      `mcp-platform/tests/test_architecture_boundaries.py:77-83` — список
      запрещённых конструкций, строки 313-347 — сценарии, которые сначала
      провоцируют страж, а потом проверяют, что молчание провоцирующего кода
      ловится, и что слово «duckdb» в тексте ошибки нарушением не считается.
      Маркер `httpx` расширен на эмбеддер: строковое правило по
      `chat/completions` пропускало копию HTTP-клиента в `libs/vectors`.
- [ ] 5.12 Переписать `tests/test_application_context*` (5 файлов)
- [ ] 5.13 **Пересобрать baseline** и зафиксировать новую строку падений
- [ ] 5.14 Проверить, что `duckdb` и `pyarrow` в зависимостях сервера, а в
      зависимостях агента — нет
- [x] 5.15 **Снимок не обязателен для старта, а его причина доезжает до
      клиента.** Найдено при сверке 5.11. `_snapshot()` вызывался из
      `_build_container()` без перехвата, поэтому `CacheBusyError` поднимался
      из `build()` и **весь процесс MCP падал на старте** — вместе с
      `history_search`, `log_event`, `llm` и `audit`, которые от снимка не
      зависят. Это не экзотика: агент и `enterprise-mcp` — два разных
      процесса, а `READ_WRITE`-сессия загрузчика исключает читателей во всех
      процессах (проверено эмпирически на duckdb 1.5.4: два `READ_ONLY`
      читателя работают одновременно, `READ_WRITE` + `READ_ONLY` — «File is
      already open in … (PID …)»). Докстринг `_snapshot()` при этом снимок уже
      объявлял необязательным: намерение было записано, а реализация покрывала
      только незаданный путь.
      **Сделано:** `libs/enterprise_data/snapshot/unavailable.py` —
      `UnavailableSnapshot(CacheStore)`: DuckDB не открывает, на каждой
      операции поднимает `InfrastructureError` с кодом и текстом исходной
      ошибки. `_snapshot()` отдаёт его и для незаданного пути, и для ошибки
      открытия. Конверт собирается из `exc.code`
      (`libs/enterprise_common/loader.py:209`), поэтому клиент различает
      `cache_busy` («файл держит процесс X», X назван в тексте) и
      `cache_open_error`, а не получает общий `infrastructure_error` с чужой
      подсказкой «проверьте регистрацию в server.py».
      Векторные чтения закрыты отдельно: их нет в ABC, `capability vectors`
      зовёт их напрямую, и без заглушки она получила бы `AttributeError`
      вместо причины. `is_ready()` возвращает `False`, а не падает, чтобы
      health видел состояние; причина доступна через `get_stats()`.
      Тесты — `mcp-platform/tests/test_snapshot_optional_startup.py` (10 шт.).
      Регрессия проверена на коде до правки: тело функции из HEAD отдавало
      `CacheBusyError` наружу.
      **Решение владельца:** ретрая на занятый файл нет — окно занятости равно
      длительности загрузки снимка, ретрай только упрётся в него.

**Приёмка:** `grep -R "duckdb" lib/ workspace/` пуст — в агенте снимка нет. В
`mcp-platform` движок есть: снимок открывается, `run_script` работает, векторный
поиск работает. Ни одна capability не открывает файл снимка.

---

## Фаза 6 — патчи → хуки

- [ ] 6.1 `turn_delivery_fail` → хук на `finalize_content` (замена текста) +
      `on_error` (логирование `turn_failed`). Проверить: пользователь получает
      **один** fallback-ответ.
      **Формулировка плана нереализуема на nanobot 0.3.5** — исправлено
      решением `docs/architecture/decisions/turn-delivery-public-extension.md`:
      `finalize_content` вызывается только `runner.py` на `response.content`
      и текст ошибки из `turn_delivery.py:341` не видит никогда.
      Перенос делается **внедрением через публичный параметр**
      `AgentLoop(turn_delivery_factory=...)`: подкласс `TurnDelivery`
      переопределяет `fail()`, фабрика-подкласс отдаёт его вместо upstream.
      Патч удаляется целиком, `_OutboundSilencer` уходит вместе с ним
- [x] 6.2 `save_turn` → хук на `after_execute_tool`: архивирование результата
      в момент возврата tool'а. **Сделано:** `lib/hooks/tool_result_archive_hook.py`
      (`ToolResultArchiveHook`), фабрика `_make_tool_result_archive_hook` в
      `ApplicationContext`, гейт прежний — `gateway.persist_threshold > 0`.
      Патч удалён из `_PATCH_SPECS`
- [x] 6.3 `async_save` → обёртка в `session_storage.py` при создании
      `PGSessionManager`; патч удалить. **Сделано:** обёртка в
      `lib/services/session_storage.py`, покрыта
      `tests/test_session_storage_async_save.py`
- [x] 6.4 `session_content_cleanup` → `PGSessionManager.save` через
      `libs/enterprise_data/clean_text.py`; патч удалить. **Сделано:**
      очистка перенесена в `PGSessionManager.save`, покрыта
      `tests/test_pg_session_manager.py`
- [x] 6.5 Удалить `session_dir_watch`. **Сделано:** патч удалён целиком
      (диагностика расследована, нужды нет)
- [ ] 6.6 `assemble_outbound` → удалить целиком: `_final_turn` перевести на
      `TurnEndEvent`, `media` и `_tool_audit` — на публикацию из хука через
      `turn_context.events`, потребитель — канал.
      **Нереализуемо в этой формулировке:** `TurnEndEvent` в nanobot 0.3.5
      не существует (в `agent.loop` есть `AgentEvent`, `StreamDeltaEvent`,
      `StreamEndEvent`, `StreamedResponseEvent`, `TurnContext`, `EventSink`,
      `TurnRoute`; модуля `nanobot.agent.events` нет).
      Нужно отдельное решение: оставить `_final_turn`/`media` на патче либо
      спроектировать перенос на существующие `EventSink` /
      `RuntimeEventPublisher`. Патч работает; спека возвращена в
      `_PATCH_SPECS` (её вычеркнули при реализации 6.2–6.5, из-за чего
      `diagnose_startup` печатал ложный DRIFT)
- [x] 6.7 `document_text_threshold` → порог переносится в нативный
      document-tool агента; патч удаляется. **Сделано** вместе с 6.11:
      `workspace/tools/document_read.py` (auto-discover, `config_key=document_read`),
      патч удалён
- [ ] 6.8 **Проверить слот `media` одним реальным ходом с вложением.** Если он
      не заполняется нигде, кроме этапа сборки outbound, публикация `media`
      удаляется вместе с патчем и решение закрывается без остаточного кода.
      **Не начато** — зависит от 6.6
- [x] 6.9 Обновить `runtime_inventory.canonical_runtime_patches()` и
      `tests/test_runtime_patcher.py::TestPatchSpecs::test_inventory_is_exact`.
      **Сделано:** канон строится из `patch_specs()` (12 → 7), тест выражен
      правилом, а не выпиской чисел. Добавлен страж
      «`_PATCH_SPECS` == записываемые в `apply_all` == `patch_*`-методы»
- [ ] 6.10 Обновить `docs/architecture/runtime-patcher-inventory.md`:
      категории, тесты, risk пересчитать. **Не начато:** документ описывает
      состояние до фазы 6

- [x] 6.11 Написать нативный document-tool агента поверх `office_files.py`;
      порог длины текста переносится из патча в его собственный код.
      **Сделано:** `workspace/tools/document_read.py`; инвариант «путь к
      файлу есть в ответе **всегда**», в том числе на усечённом ответе
- [ ] 6.12 Проверить, не расходятся ли две копии парсера: домен `legal`
      переезжает в платформу и тоже разбирает документы. Дублировать модуль
      нельзя — домен получает уже извлечённый текст. **Не начато** —
      зависит от фазы 11 (`legal_summarizer`)
- [x] 6.13 `tests/test_office_files.py` остаётся в проекте агента: сам модуль
      `office_files.py` не переезжает. **Сделано:** файл на месте, тест зелёный

**Приёмка:** патчей 12 → **4** на этом шаге: `2` полностью необходимых
(`exec_limits`, `subagent_logging`) + 2 частичных (`context_governor`,
`tool_limits`). Каждый оставшийся имеет заполненное «Условие удаления».

---

## Фаза 7 — логирование через `enterprise-mcp`

> **Состояние на 2026-10-01.** Платформенная сторона сделана: операции
> `log_event`/`log_events`, контекст вопроса и purge живут в capability `data`
> (`tests/test_data_journal_operations.py`). Агентская сторона **не начата**:
> `lib/services/db_logging_service.py` по-прежнему пишет в PostgreSQL сам и не
> импортирует `EnterpriseMcpClient` — батчевого flush, локального буфера и
> fallback нет. Это та же агентская сторона, что и незакрытый остаток 2.16.

- [ ] 7.1 Локальный буфер в процессе агента, ограниченный размером, дроп при переполнении
- [ ] 7.2 Батчевый асинхронный flush в `enterprise-mcp: log_event`
- [ ] 7.3 Локальный fallback для сбоев самого `enterprise-mcp` (файл/stderr) — петля не замыкается
- [ ] 7.4 `logging.db.retention_days` и purge пустых outbound → в конфиг `enterprise-mcp`
- [x] 7.5 `db_logging_bus.py` остаётся в агенте — файл на месте
      (`lib/services/db_logging_bus.py`)
- [ ] 7.6 Интеграционный тест: недоступность `enterprise-mcp` не блокирует ход

**Приёмка:** при остановленном `enterprise-mcp` ходы проходят, логи теряются,
счётчик потерь растёт.

> **Фазы 8 в плане нет:** нумерация идёт 7 → 9. Либо это опечатка и кусок плана
> потерян, либо фаза была исключена осознанно — тогда это должно быть записано
> здесь, иначе приёмка «по спеке» неполна. Отмечено при разборе 2026-10-01.

---

## Фаза 9 — остатки `audit_analyzer` в агенте

> Перенос кода capability `audit` уже сделан в фазе 4. Здесь остаётся только
> сторона skill'а: описание, CLI-обвязка и уборка того, что осталось в агенте.

> **Блокер, найденный 2026-10-01: 9.1 и 9.2 в этой формулировке разрушают
> доступ, а не заменяют его.** В `workspace/tools/` агента нет ни одного tool'а,
> который зовёт capability `audit` — только `compact_context`, `document_read`,
> `history_search_tool`, `legal_summarizer_query`. Единственный путь агента к
> данным audit_analyzer — `scripts/cli.py`, и его как раз предлагает удалить
> 9.2. Значит, до удаления CLI должен появиться путь от агента к capability
> `audit`, иначе скилл теряет данные целиком. Это решение владельца (какая
> поверхность: agent-tool поверх MCP-клиента или иная), а не задача на
> раскладку файлов.

- [ ] 9.1 `workspace/skills/audit_analyzer/SKILL.md` переписать под новую
      поверхность: три операции вместо трёх режимов CLI, параметры — из
      `list_scripts`, авторитетен реестр, а не текст.
      **Начинать после решения по блокеру выше** (нет пути от агента к capability
      `audit`, кроме `cli.py`)
- [ ] 9.2 Удалить `scripts/cli.py`, `predefined/`, `generated_sql_mode.py`,
      `llm.py`, `skill_config.py` из репозитория агента — они живут в
      `mcp-platform/capabilities/audit/`
      **Все пять на месте, проверено 2026-10-01.** Удалять последними: пока
      `cli.py` жив, он держит единственный путь агента к данным. 9.3 не начат:
      `scripts/llm.py` и `lib/utils/sql_safety.py` в агенте ещё есть
- [ ] 9.3 Удалить остатки `sql_safety` и LLM-клиента из агента, если после
      фаз 4 и 3 они ещё используются
- [ ] 9.4 Поправить `predefined/models.py`: докстринг утверждает, что реестр
      лежит в `scripts.py`, а не в PostgreSQL. Кодом это опровергнуто —
      Python-реестра нет
- [ ] 9.5 Разобраться с расхождением каталога скриптов: `SKILL.md` перечисляет
      шесть имён, seed кладёт пять других и `violations_by_period` требует
      обязательные параметры. Авторитетен `list_scripts`, текст приводится к
      фактическому состоянию БД
- [ ] 9.6 Поправить `docs/DATABASE.md`, где документированы
      `skills.audit_analyzer.llm.max_tokens` и `.temperature` — таких секций
      в `project.json` больше нет

**Приёмка:** агент не содержит `sqlglot`, `duckdb`, `faiss`, имён `oarb.*` и
ни строки кода аудита. `skills.audit_analyzer/SKILL.md` описывает три операции
и не содержит ни одного имени таблицы.

---

## Фаза 10 — зависимости и финальная проверка

> Финальной эта фаза была при плане с `legal_summarizer` в 8-й. Теперь
> `legal_summarizer` — фаза 11, а здесь остаётся всё, кроме домена legal.

- [ ] 10.1 Разделить `requirements.txt`: runtime агента / `enterprise-mcp`
- [ ] 10.2 Офисные пакеты (`python-docx`, `openpyxl`, `pypdf`, `python-pptx`)
      **остаются** в требованиях агента — решение принято осознанно
- [ ] 10.3 Убрать из требований агента то, что уехало: `sqlglot`, `duckdb`,
      `pyarrow`, `faiss`, клиентский LLM-пакет. `duckdb` и `pyarrow` при этом **переходят в
      манифест сервера**, а не исчезают
- [ ] 10.4 Обновить `AGENTS.md`, `CHANGELOG.md`, `docs/`
- [ ] 10.5 Для `enterprise-mcp`: старт без Nanobot, health, discovery, нормальный запрос,
      некорректный запрос, сбой инфраструктуры, таймаут
- [ ] 10.6 Прогнать тесты всех переехавших модулей в `mcp-platform`:
      `audit_analyzer`, `llm_client`, `db.py`, `sql_safety`, `jsonb`,
      `clean_text`. Тесты `legal_summarizer` проверяются в фазе 11
- [ ] 10.7 `test_office_files.py` остаётся в прогоне агента — он не переезжает
- [ ] 10.8 Прогнать архитектурный страж сервисов (2.15) по всему `mcp-platform`:
      ни одна capability не открывает соединение, не собирает индекс, не
      создаёт HTTP-клиент LLM и не открывает файл снимка

**Приёмка:** чистое окружение с `nanobot-ai==0.3.5` поднимается и работает.
Сервер не требует Nanobot. Платформа обновляется независимо от агента.
Страж сервисов зелёный, и добавление capability, которая обходит сервис, валит
сборку.


---

## Фаза 11 — `legal_summarizer` (последняя)

> **Перенесена в конец по решению владельца.** Домен `legal_summarizer` — самый
> чистый актив проекта: 80 модулей, ноль импортов `nanobot`, ноль обращений к БД,
> ноль DuckDB. Именно поэтому его перенос ничего не разблокирует для остальных
> фаз, а вот `document-tool` агента (бывшие 8.1–8.3) разблокирует пункт 6.7 —
> поэтому он вынесен в фазу 6, а не остался здесь.

- [ ] 11.1 Перенести `workspace/skills/legal_summarizer/**` (80 модулей,
      142 тестовых файла)
- [ ] 11.2 Переписать архитектурные guard-тесты legal под новый расклад файлов
- [ ] 11.3 `workspace/tools/legal_summarizer_query.py` → MCP-вызов, ~30 строк
- [ ] 11.4 Разложить legal по capability
      `capabilities/legal_summarizer/{skill/SKILL.md, tools/*.py, service/}`
- [ ] 11.5 **Привязать кэш документа к `session_id` контракта, а не к
      окружению.** Сегодня ключ сессии резолвится как `SESSION_KEY` в env →
      fallback на имя файла → `__nosession__`, причём `SESSION_KEY` **не
      выставляется нигде в репозитории**, поэтому кэш оказывается в папке,
      названной по документу, а не по сессии. Источник ключа — `session_id` из
      2.19/2.20. Дополнительно: корень кэша перестаёт выводиться из
      `Path(__file__).parents[N]` (после переезда это каталог платформы, а не
      репозиторий агента) и задаётся конфигурацией; `document_id` — по
      контент-хешу, а не по `resolved_path+size+mtime`

**Приёмка:** legal работает; в репозитории агента нет ни одного импорта legal.
Парсер офисных файлов существует в проекте агента в единственном экземпляре.
