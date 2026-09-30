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
> После шага 5 (удаление DuckDB) строка допустимых падений **пересобирается** —
> чинить тесты удалённой подсистемы бессмысленно.
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

**Порядок фаз жёсткий.** Capability `audit` (фаза 4) переезжает на PostgreSQL
**до** удаления DuckDB (фаза 5), потому что `db_loader` сегодня читает реестр
через `CacheProvider`, то есть из локального снимка. Обратный порядок оставит
`run_script` без источника.

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

- [ ] 1.1 `agent_worker_claims`: DROP-миграция, удаление `sql/workers/`
- [ ] 1.2 Удалить протокол аренды из `lib/channels/postgres_channel.py`; заменить
      опросом с `FOR UPDATE SKIP LOCKED`
- [ ] 1.3 Удалить `tools/check_worker_pool_integrity.py`
- [ ] 1.4 Удалить 8 настроек `channels.postgres.*` из `project.json` и `project_settings.py`
- [ ] 1.5 Удалить `benchmarks/`, `benchmarks/db.py`, `tools/legal_benchmark.py`,
      `tools/legacy_audit.py`, `tools/test_audit.py`; DROP `agent_benchmark_runs`,
      `agent_benchmark_results`; удалить секцию `benchmark.*`
- [ ] 1.6 Удалить `streamlit_app.py`, `lib/services/subprocess_manager.py`,
      spawn-логику в `gateway.py`, секцию `streamlit.*`, `tests/test_streamlit_app.py`
- [ ] 1.7 Удалить `workspace/tools/example.py` и запись `ExampleTool` из
      `runtime_inventory.py`
- [ ] 1.8 Обновить `AGENTS.md`, `CHANGELOG.md`, `lib/channels/README.md`, `README.md`
- [ ] 1.9 Править `tests/test_config_keys.py` (`REQUIRED_KEYS`)

**Приёмка:** 4 101 → ожидаемо ~3 950 тестов; ни одного падения сверх предсуществующих.
`gateway.py` и `cli_agent.py` стартуют.

---

## Фаза 2 — реестр инструментов, `libs/enterprise_data` и capability `data`

> Реестр идёт первым: capability `data` — первый, кто его использует.
> Сервер один (`enterprise-mcp`), поэтому всё дальнейшее — это добавление
> каталогов в `capabilities/`, а не новые процессы.

- [ ] 2.1 `libs/enterprise_common`: `ToolDefinition` (name, description, handler,
      category, version, enabled, tags, permissions) и `ToolRegistry`
- [ ] 2.2 Загрузчик: рекурсивный обход `capabilities/*/tools/*.py` →
      `create_tool(container)` → discovery, импорт, вызов точки входа,
      регистрация
- [ ] 2.3 Валидация при загрузке: импорт, наличие `create_tool`, тип
      `ToolDefinition`, непустые `name`/`description`, уникальность `name`,
      callable `handler`, валидная схема аргументов
- [ ] 2.4 **Fail-fast:** ошибка одного файла валит старт сервера целиком, с
      путём до файла и `name`. Тест на каждый пункт валидации
- [ ] 2.5 `servers/enterprise/server.py` — bootstrap реестра без единого
      `@mcp.tool()`; переписать `servers/_template` под этот вид
- [ ] 2.6 Перенести `workspace/utils/db.py` → `mcp-platform/libs/enterprise_data/db.py`
- [ ] 2.7 Перенести `lib/utils/sql_safety.py` → `mcp-platform/libs/enterprise_data/sql_safety.py`
- [ ] 2.8 Перенести `workspace/utils/jsonb.py` и `clean_text.py`
- [ ] 2.9 Перенести `tests/test_utils_db.py` и `tests/test_sql_safety.py`
- [ ] 2.10 `capabilities/data/`: `service/` (логика, тестируется без MCP) +
      `tools/*.py` (по файлу на операцию)
- [ ] 2.11 Операции capability `data` — **только инфраструктура**, без доступа к
      данным модели: `log_event`, `history_search` (изоляция по
      `user_id`/`session_id` как часть контракта), `schema_check`. Операции
      `query_sql` и `upsert_records` **не создаются**: произвольного SQL на
      поверхности агента не существует
- [ ] 2.12 **Два входа в очередь.** Сервис владельца пула получает
      `submit(job)` — блокирующий, для работы с данными, и `accept(event)` —
      неблокирующий, буфер писателя журнала. Одна очередь означает, что
      `generate_sql` с четырьмя вызовами LLM конкурирует с записью журнала за
      воркеры; потеря события безвозвратна и не сопровождается ошибкой
- [ ] 2.13 **Обязать серверный предел стоимости запроса.** `statement_timeout` в
      коде нет: `psycopg2.connect` не передаёт `options=`, а `autocommit=True`
      делает `SET LOCAL statement_timeout` нооп. Внести либо на соединении в
      момент коннекта, либо в замыкании задания с явным сбросом. Внести
      `max_rows` на сервере, а не в SQL модели
- [ ] 2.14 **Запретить старт без `sqlglot`.** Без AST-ветки guard проверяет
      только первый блокируемый оператор, `INTO` и multi-statement, и пропускает
      `pg_sleep`, `information_schema` и `UPDATE`/`DELETE` после `--`-комментария
- [ ] 2.15 **Архитектурный страж сервисов.** Проверка, которая валит сборку при
      обходе: вне сервисов-владельцев нет `psycopg2.connect` / `*ConnectionPool` /
      `create_pool`, вне владельца индексов нет `faiss` и `IndexFlatIP`, вне
      `libs/llm` нет HTTP-вызовов провайдера. Сообщение называет файл и
      конструкцию. Аналог существующего
      `tests/test_storage_hybridization.py::TestNoDirectSQLToSessionTables`
- [ ] 2.16 Перенаправить потребителей на `enterprise-mcp`:
      `history_search_tool.py` (оставить ~30-строчный адаптер),
      `db_logging_service.py`, `benchmarks` (уже удалён), `streamlit` (уже удалён),
      `schema_validation.py` (остаётся в агенте, `fetch` внедряется)
- [ ] 2.17 Архитектурный тест: `mcp-platform` не импортирует `nanobot`, `lib`, `workspace`

**Приёмка:** `cd mcp-platform && pytest` — зелёные. Сервер поднимается в
подпроцессе с заблокированным `import nanobot`. `capabilities/data/` не
содержит `psycopg2.pool` / `ThreadedConnectionPool` (единый пул). Добавление
файла в `capabilities/data/tools/` не требует правок `server.py`. Ни одна
зарегистрированная операция не принимает SQL от вызывающей стороны.

---

## Фаза 3 — capability `vectors` и `llm`

> `vectors` идёт **до** удаления DuckDB: FAISS собирается из снапшота, и
> удаление снапшота раньше гасит векторный поиск. Обе capability — каталоги в
> том же процессе, новых серверов не заводится.

- [ ] 3.1 Извлечь из `lib/utils/duckdb_query.py` в `libs/vectors`:
      `build_faiss_index`, `group_vector_hits`, `build_raw_items`
- [ ] 3.2 Извлечь из `lib/services/cache_provider.py` концепции `SearchResult`,
      `IndexIntegrityError`
- [ ] 3.3 Перенести векторную половину `cache_provider_impl.py`:
      `get_embedding`, `read_embedding_config`, `read_vector_index_config`,
      `compute/verify_index_signature`, `list_runtime_vector_indexes`
- [ ] 3.4 Перенести `vector_index_service.py`, `text_splitter.py`, `preload_service.py`
- [ ] 3.5 **Переписать** чтение векторов: из PostgreSQL напрямую, а не из снапшота
- [ ] 3.6 Перенести `tools/build_vectors.py` (сборка эмбеддингов), `tools/check_indexes.py`
- [ ] 3.7 Конфигурация: `gateway.vector.index.indexes.*` → конфиг `enterprise-mcp`
- [ ] 3.8 Операции как `capabilities/vectors/tools/*.py` через реестр фазы 2:
      `vector_search`, `list_indexes`, `index_stats`
- [ ] 3.9 **Ленивая загрузка индекса:** FAISS собирается по первому
      векторному запросу, не на старте процесса. Локом — параллельные запросы
      ждут один прогрев. Состояние (`нет` / `собирается` / `готов` / `ошибка`)
      наблюдаемо. `list_indexes` и `index_stats` отвечают без поднятия индекса
- [ ] 3.10 Перенести `lib/services/llm_client.py` и `llm_config.py`
      → `mcp-platform/libs/llm/` без изменения поведения
- [ ] 3.11 `capabilities/llm/`: `service/` (вызов провайдера) + `tools/complete.py`
- [ ] 3.12 Конфигурация провайдера и ключи переезжают в конфиг `enterprise-mcp`,
      из `config.json` агента
- [ ] 3.13 Удалить `llm_client.py` и `llm_config.py` из агента; убедиться, что
      в `lib/` не осталось потребителей
- [ ] 3.14 Удалить `lib/utils/retry.py` из агента (перенесено из фазы 0, пункт
      0.4). К этому моменту оба импортёра — `cache_provider_impl.py` и
      `llm_client.py` — уже уехали в `mcp-platform/libs/`. Проверить grep по
      `retry_on_exception` перед удалением; `mcp-platform` получает свою копию
      в `libs/enterprise_common/retry.py`

**Приёмка:** `vector_search` возвращает те же результаты, что до шага 4.
Зависимость вектора запроса есть служебная зависимость на сервис
эмбеддингов — это зависимая должна быть названа явно в контракте.
Сервер отвечает на SQL-операции сразу после старта, не дожидаясь сборки индекса.
Индекс собирается один раз на процесс, а не по разу на capability.
В агенте не осталось ни строки LLM-клиента.

---

## Фаза 4 — capability `audit`: переезд на PostgreSQL

> **Порядок обязателен.** `db_loader` сегодня читает реестр через
> `CacheProvider`, то есть из локального DuckDB-снимка. Удаление снапшота раньше
> переезда оставит `run_script` без источника. Поэтому `audit` идёт **до**
> фазы 5.

- [ ] 4.1 Резолв `scripts_registry` из `TableRegistry` перенести в сервис
      capability `audit`; `resources_by_label("scripts_registry")` — единственный
      путь к имени таблицы реестра
- [ ] 4.2 **Перевести `db_loader` на PostgreSQL напрямую**, минуя `CacheProvider`.
      Значения параметров — отдельным списком позиционных аргументов
- [ ] 4.3 **Плейсхолдеры под диалект драйвера.** `sql_template` описан с `?`,
      и загрузчик реестра использует `?` в `WHERE name = ?`; у `psycopg2` — `%s`.
      Правка в двух местах: сборщик и загрузчик
- [ ] 4.4 Схема по умолчанию при отсутствии точки в имени — `public`, а не `main`
      (текущее значение взято из DuckDB)
- [ ] 4.5 Перенести пайплайн `predefined` (загрузчик, валидатор параметров,
      сборщик шаблона) в `capabilities/audit/service/`
- [ ] 4.6 Перенести пайплайн `generated_sql`: описание схемы, few-shot из
      реестра, промпт с `<NO_MATCH>`, цикл попыток, `validate_sql`, `EXPLAIN`
- [ ] 4.7 **Проверка состава таблиц в коде.** Сегодня белый список существует
      только как текст в промпте: `validate_sql` проверяет вид оператора, но не
      имя таблицы, и `SELECT * FROM public.agent_gateway_logs` проходит без
      возражений
- [ ] 4.8 **Потолок строк для сгенерированного запроса.** Сегодня `LIMIT`
      дописывает только сборщик predefined; сгенерированный запрос уходит в базу
      как есть
- [ ] 4.9 **Не принимать `context` от вызывающей стороны.** Аргумент подклеивает
      текст агента в начало сообщений генератору
- [ ] 4.10 Операции `capabilities/audit/tools/`: `list_scripts`, `run_script`,
      `generate_sql`. Ни одна не принимает SQL
- [ ] 4.11 `list_scripts` отдаёт `parameters` объектом с типами, обязательностью,
      значениями по умолчанию и `validation`, а не склеенной строкой имён;
      добавить `long_description` и `returns`
- [ ] 4.12 `run_script` **не возвращает текст SQL**; текст уходит в журнал
- [ ] 4.13 **Убрать молчаливые пути.** `load_all` возвращает `{}` на исключении,
      `load_script` — `None`. Оба обязаны стать `registry_unavailable`
- [ ] 4.14 Единая модель ошибок: `mode` и `error_type` сегодня расходятся между
      библиотекой и CLI, машиночитаемого кода нет ни на одном пути
- [ ] 4.15 Обернуть вызовы в MCP-поверхность, удалить регистрацию audit-tool'ов
      из `project_tool_loader`
- [ ] 4.16 Разложить по capability
      `capabilities/audit/{skill/SKILL.md, tools/*.py, service/}`; проверить,
      что skill и операции ведут к одному сервису и не вложены друг в друга

**Приёмка:** ни один зарегистрированный инструмент не принимает SQL. Реестр
читается из PostgreSQL, а не из снимка. Сгенерированный запрос, ссылающийся на
таблицу вне разрешённого списка, отклоняется **до** выполнения. Скрипт с
неизвестным именем возвращает `not_found`, сломанный реестр —
`registry_unavailable`, а не пустой каталог.

---

## Фаза 5 — удаление DuckDB

> Фаза выполняется **после** фазы 4. `db_loader` читает реестр через
> `CacheProvider`, то есть из снимка; удаление снапшота раньше переезда оставит
> `run_script` без источника.

Атомарный коммит без MCP-работы.

- [ ] 5.1 Удалить `duckdb_cache_store.py` (1455)
- [ ] 5.2 Удалить `cache_load_service.py` (472)
- [ ] 5.3 Удалить `cache_provider.py` (440) и DuckDB-часть `duckdb_query.py` (338)
- [ ] 5.4 Удалить `lib/core/skill_registration.py` (98)
- [ ] 5.5 Удалить cache-API из `lib/core/skill_config.py`
- [ ] 5.6 Удалить секцию `CacheSettings` из `project_settings.py`
- [ ] 5.7 Удалить 88 строк кэш-обвязки из `application_context.py`:
      `resolve_cache_path`, `_warn_if_cache_path_on_nfs`, `_init_cache_runtime`,
      `check_duckdb_cache`, `check_vector_search`
- [ ] 5.8 Удалить из `table_registry.py` только агрегацию имён в список загрузки.
      **Сохранить** `resources_by_label("scripts_registry")`, `register_infra`,
      `tracking_column_for`
- [ ] 5.9 Удалить `sql/vectors/create_vector_index_config.sql` и
      `create_vector_index_store.sql`
- [ ] 5.10 Удалить 10 тестовых модулей: `test_duckdb_cache_store.py`,
      `test_cache_provider_meta.py`, `test_single_cache_interface.py`,
      `test_cache_no_file_hold.py`, `test_cache_provider_open_failure.py`,
      `test_cache_provider_mode.py`, `test_cache_load_service.py`,
      `test_table_registry.py`, `test_skill_cache_boundary.py`,
      `test_shared_cache_path_across_profiles.py`
- [ ] 5.11 Переписать `tests/test_application_context*` (5 файлов)
- [ ] 5.12 Удалить `duckdb` и `pyarrow` из `requirements.txt`
- [ ] 5.13 **Пересобрать baseline** и зафиксировать новую строку падений
- [ ] 5.14 Поправить `tests/test_docs_consistency.py` (проверяет упоминания DuckDB)

**Приёмка:** в `requirements.txt` нет `duckdb`. `grep -R "duckdb" lib/ workspace/`
пусто. Векторный поиск работает. `run_script` работает — реестр читается из
PostgreSQL, а не из того, что удалили.

---

## Фаза 6 — патчи → хуки

- [ ] 6.1 `turn_delivery_fail` → хук на `finalize_content` (замена текста) +
      `on_error` (логирование `turn_failed`). Проверить: пользователь получает
      **один** fallback-ответ
- [ ] 6.2 `save_turn` → хук на `after_execute_tool`: архивирование результата
      в момент возврата tool'а
- [ ] 6.3 `async_save` → обёртка в `session_storage.py` при создании
      `PGSessionManager`; патч удалить
- [ ] 6.4 `session_content_cleanup` → `PGSessionManager.save` через
      `libs/enterprise_data/clean_text.py`; патч удалить
- [ ] 6.5 Удалить `session_dir_watch`
- [ ] 6.6 `assemble_outbound` → удалить целиком: `_final_turn` перевести на
      `TurnEndEvent`, `media` и `_tool_audit` — на публикацию из хука через
      `turn_context.events`, потребитель — канал
- [ ] 6.7 `document_text_threshold` → порог переносится в нативный
      document-tool агента; патч удаляется
- [ ] 6.8 **Проверить слот `media` одним реальным ходом с вложением.** Если он
      не заполняется нигде, кроме этапа сборки outbound, публикация `media`
      удаляется вместе с патчем и решение закрывается без остаточного кода
- [ ] 6.9 Обновить `runtime_inventory.canonical_runtime_patches()` и
      `tests/test_runtime_patcher.py::TestPatchSpecs::test_inventory_is_exact`
- [ ] 6.10 Обновить `docs/architecture/runtime-patcher-inventory.md`:
      категории, тесты, risk пересчитать

**Приёмка:** патчей 12 → **4** на этом шаге: `2` полностью необходимых
(`exec_limits`, `subagent_logging`) + 2 частичных (`context_governor`,
`tool_limits`). Каждый оставшийся имеет заполненное «Условие удаления».

---

## Фаза 7 — логирование через `enterprise-mcp`

- [ ] 7.1 Локальный буфер в процессе агента, ограниченный размером, дроп при переполнении
- [ ] 7.2 Батчевый асинхронный flush в `enterprise-mcp: log_event`
- [ ] 7.3 Локальный fallback для сбоев самого `enterprise-mcp` (файл/stderr) — петля не замыкается
- [ ] 7.4 `logging.db.retention_days` и purge пустых outbound → в конфиг `enterprise-mcp`
- [ ] 7.5 `db_logging_bus.py` остаётся в агенте
- [ ] 7.6 Интеграционный тест: недоступность `enterprise-mcp` не блокирует ход

**Приёмка:** при остановленном `enterprise-mcp` ходы проходят, логи теряются,
счётчик потерь растёт.

---

## Фаза 8 — document-tool и `legal_summarizer`

> **Решение принято:** доступ к офисным файлам остаётся нативным tool'ом
> агента, `office_files.py` **не переезжает** в `mcp-platform`. Офисные пакеты
> остаются в `requirements.txt` агента — это зафиксированное исключение из
> цели «установка Nanobot не тянет enterprise-стек».

- [ ] 8.1 Написать нативный document-tool агента поверх `office_files.py`;
      порог длины текста переносится из патча в его собственный код
- [ ] 8.2 Проверить, не расходятся ли две копии парсера: домен `legal`
      переезжает в платформу и тоже разбирает документы. Дублировать модуль
      нельзя — домен получает уже извлечённый текст
- [ ] 8.3 Перенести `tests/test_office_files.py` не трогая: тест остаётся
      в проекте агента
- [ ] 8.4 Перенести `workspace/skills/legal_summarizer/**` (80 модулей, 142 теста)
- [ ] 8.5 Переписать 4 архитектурных guard-теста legal под новый расклад файлов
- [ ] 8.6 `workspace/tools/legal_summarizer_query.py` → MCP-вызов, ~30 строк
- [ ] 8.7 Разложить legal по capability
      `capabilities/legal_summarizer/{skill/SKILL.md, tools/*.py, service/}`

**Приёмка:** legal работает; в репозитории агента нет ни одного импорта legal.
Парсер офисных файлов существует в проекте агента в единственном экземпляре.

---

## Фаза 9 — остатки `audit_analyzer` в агенте

> Перенос кода capability `audit` уже сделан в фазе 4. Здесь остаётся только
> сторона skill'а: описание, CLI-обвязка и уборка того, что осталось в агенте.

- [ ] 9.1 `workspace/skills/audit_analyzer/SKILL.md` переписать под новую
      поверхность: три операции вместо трёх режимов CLI, параметры — из
      `list_scripts`, авторитетен реестр, а не текст
- [ ] 9.2 Удалить `scripts/cli.py`, `predefined/`, `generated_sql_mode.py`,
      `llm.py`, `skill_config.py` из репозитория агента — они живут в
      `mcp-platform/capabilities/audit/`
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

- [ ] 10.1 Разделить `requirements.txt`: runtime агента / `enterprise-mcp`
- [ ] 10.2 Офисные пакеты (`python-docx`, `openpyxl`, `pypdf`, `python-pptx`)
      **остаются** в требованиях агента — решение принято осознанно
- [ ] 10.3 Убрать из требований агента то, что уехало: `sqlglot`, `duckdb`,
      `pyarrow`, `faiss`, клиентский LLM-пакет
- [ ] 10.4 Обновить `AGENTS.md`, `CHANGELOG.md`, `docs/`
- [ ] 10.5 Для `enterprise-mcp`: старт без Nanobot, health, discovery, нормальный запрос,
      некорректный запрос, сбой инфраструктуры, таймаут
- [ ] 10.6 Прогнать тесты всех переехавших модулей в `mcp-platform`:
      `legal_summarizer` (80 скриптов + 142 тестовых файла), `audit_analyzer`,
      `llm_client`, `db.py`, `sql_safety`, `jsonb`, `clean_text`
- [ ] 10.7 `test_office_files.py` остаётся в прогоне агента — он не переезжает
- [ ] 10.8 Прогнать архитектурный страж сервисов (2.15) по всему `mcp-platform`:
      ни одна capability не открывает соединение, не собирает индекс и не
      создаёт HTTP-клиент LLM

**Приёмка:** чистое окружение с `nanobot-ai==0.3.5` поднимается и работает.
Сервер не требует Nanobot. Платформа обновляется независимо от агента.
Страж сервисов зелёный, и добавление capability, которая обходит сервис, валит
сборку.
