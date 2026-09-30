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
> После шага 4 (удаление DuckDB) строка допустимых падений **пересобирается** —
> чинить тесты удалённой подсистемы бессмысленно.

**Ограничение на каждом шаге:** не переносить несколько доменов за раз;
не удалять старую реализацию до прохождения интеграционных тестов;
не оставлять две рабочие реализации одного механизма.

---

## Фаза 0 — мёртвый и сломанный код

- [ ] 0.1 Удалить `workspace/utils/structure_cache.py` — импортирует несуществующий `extract_structure`
- [ ] 0.2 Удалить `tools/extract_office_structure.py` — то же
- [ ] 0.3 Удалить `lib/utils/table_utils.py` + `tests/test_table_utils.py`
- [ ] 0.4 Удалить `lib/utils/retry.py` (оба импортёра уезжают)
- [ ] 0.5 Удалить `workspace/skills/audit_analyzer/err1.log`
- [ ] 0.6 Удалить `workspace/data_store/cache/**/*.py` (83 черновых скрипта)
- [ ] 0.7 Поправить `AGENTS.md`: запись про `lib/utils/table_utils.py` не соответствует коду

**Приёмка:** `pytest` — те же 4 падения. `tools/architecture_guard.py` — exit 0.
`ruff check` — чисто.

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
- [ ] 2.11 Операции: `query_sql` (read-only, AST-валидация), `log_event`,
      `history_search` (изоляция по `user_id`/`session_id` как часть контракта),
      `upsert_records`, `schema_check`
- [ ] 2.12 **Обязать серверный предел стоимости запроса.** `statement_timeout` в
      коде нет: `psycopg2.connect` не передаёт `options=`, а `autocommit=True`
      делает `SET LOCAL statement_timeout` нооп. Внести либо на соединении в
      момент коннекта, либо в замыкании задания с явным сбросом. Внести
      `max_rows` на сервере, а не в SQL модели
- [ ] 2.13 **Запретить старт без `sqlglot`.** Без AST-ветки guard проверяет
      только первый блокируемый оператор, `INTO` и multi-statement, и пропускает
      `pg_sleep`, `information_schema` и `UPDATE`/`DELETE` после `--`-комментария
- [ ] 2.14 Перенаправить потребителей на `enterprise-mcp`:
      `history_search_tool.py` (оставить ~30-строчный адаптер),
      `db_logging_service.py`, `benchmarks` (уже удалён), `streamlit` (уже удалён),
      `schema_validation.py` (остаётся в агенте, `fetch` внедряется)
- [ ] 2.15 Архитектурный тест: `mcp-platform` не импортирует `nanobot`, `lib`, `workspace`

**Приёмка:** `cd mcp-platform && pytest` — зелёные. Сервер поднимается в
подпроцессе с заблокированным `import nanobot`. `capabilities/data/` не
содержит `psycopg2.pool` / `ThreadedConnectionPool` (единый пул). Добавление
файла в `capabilities/data/tools/` не требует правок `server.py`.

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

**Приёмка:** `vector_search` возвращает те же результаты, что до шага 4.
Зависимость вектора запроса есть служебная зависимость на сервис
эмбеддингов — это зависимая должна быть названа явно в контракте.
Сервер отвечает на `query_sql` сразу после старта, не дожидаясь сборки индекса.
В агенте не осталось ни строки LLM-клиента.

---

## Фаза 4 — удаление DuckDB

Атомарный коммит без MCP-работы.

- [ ] 4.1 Удалить `duckdb_cache_store.py` (1455)
- [ ] 4.2 Удалить `cache_load_service.py` (472)
- [ ] 4.3 Удалить `cache_provider.py` (440) и DuckDB-часть `duckdb_query.py` (338)
- [ ] 4.4 Удалить `lib/core/skill_registration.py` (98)
- [ ] 4.5 Удалить cache-API из `lib/core/skill_config.py`
- [ ] 4.6 Удалить секцию `CacheSettings` из `project_settings.py`
- [ ] 4.7 Удалить 88 строк кэш-обвязки из `application_context.py`:
      `resolve_cache_path`, `_warn_if_cache_path_on_nfs`, `_init_cache_runtime`,
      `check_duckdb_cache`, `check_vector_search`
- [ ] 4.8 Удалить из `table_registry.py` только агрегацию имён в список загрузки.
      **Сохранить** `resources_by_label("scripts_registry")`, `register_infra`,
      `tracking_column_for`
- [ ] 4.9 Удалить `sql/vectors/create_vector_index_config.sql` и
      `create_vector_index_store.sql`
- [ ] 4.10 Удалить 10 тестовых модулей: `test_duckdb_cache_store.py`,
      `test_cache_provider_meta.py`, `test_single_cache_interface.py`,
      `test_cache_no_file_hold.py`, `test_cache_provider_open_failure.py`,
      `test_cache_provider_mode.py`, `test_cache_load_service.py`,
      `test_table_registry.py`, `test_skill_cache_boundary.py`,
      `test_shared_cache_path_across_profiles.py`
- [ ] 4.11 Переписать `tests/test_application_context*` (5 файлов)
- [ ] 4.12 Удалить `duckdb` и `pyarrow` из `requirements.txt`
- [ ] 4.13 **Пересобрать baseline** и зафиксировать новую строку падений
- [ ] 4.14 Поправить `tests/test_docs_consistency.py` (проверяет упоминания DuckDB)

**Приёмка:** в `requirements.txt` нет `duckdb`. `grep -R "duckdb" lib/ workspace/`
пусто. Векторный поиск работает.

---

## Фаза 5 — патчи → хуки

- [ ] 5.1 `turn_delivery_fail` → хук на `finalize_content` (замена текста) +
      `on_error` (логирование `turn_failed`). Проверить: пользователь получает
      **один** fallback-ответ
- [ ] 5.2 `save_turn` → хук на `after_execute_tool`: архивирование результата
      в момент возврата tool'а
- [ ] 5.3 `async_save` → обёртка в `session_storage.py` при создании
      `PGSessionManager`; патч удалить
- [ ] 5.4 `session_content_cleanup` → `PGSessionManager.save` через
      `libs/enterprise_data/clean_text.py`; патч удалить
- [ ] 5.5 Удалить `session_dir_watch`
- [ ] 5.6 `assemble_outbound` → удалить целиком: `_final_turn` перевести на
      `TurnEndEvent`, `media` и `_tool_audit` — на публикацию из хука через
      `turn_context.events`, потребитель — канал
- [ ] 5.7 `document_text_threshold` → порог переносится в нативный
      document-tool агента; патч удаляется
- [ ] 5.8 **Проверить слот `media` одним реальным ходом с вложением.** Если он
      не заполняется нигде, кроме этапа сборки outbound, публикация `media`
      удаляется вместе с патчем и решение закрывается без остаточного кода
- [ ] 5.9 Обновить `runtime_inventory.canonical_runtime_patches()` и
      `tests/test_runtime_patcher.py::TestPatchSpecs::test_inventory_is_exact`
- [ ] 5.10 Обновить `docs/architecture/runtime-patcher-inventory.md`:
      категории, тесты, risk пересчитать

**Приёмка:** патчей 12 → **4** на этом шаге: `2` полностью необходимых
(`exec_limits`, `subagent_logging`) + 2 частичных (`context_governor`,
`tool_limits`). Каждый оставшийся имеет заполненное «Условие удаления».

---

## Фаза 6 — логирование через `enterprise-mcp`

- [ ] 6.1 Локальный буфер в процессе агента, ограниченный размером, дроп при переполнении
- [ ] 6.2 Батчевый асинхронный flush в `enterprise-mcp: log_event`
- [ ] 6.3 Локальный fallback для сбоев самого `enterprise-mcp` (файл/stderr) — петля не замыкается
- [ ] 6.4 `logging.db.retention_days` и purge пустых outbound → в конфиг `enterprise-mcp`
- [ ] 6.5 `db_logging_bus.py` остаётся в агенте
- [ ] 6.6 Интеграционный тест: недоступность `enterprise-mcp` не блокирует ход

**Приёмка:** при остановленном `enterprise-mcp` ходы проходят, логи теряются,
счётчик потерь растёт.

---

## Фаза 7 — document-tool и `legal_summarizer`

> **Решение принято:** доступ к офисным файлам остаётся нативным tool'ом
> агента, `office_files.py` **не переезжает** в `mcp-platform`. Офисные пакеты
> остаются в `requirements.txt` агента — это зафиксированное исключение из
> цели «установка Nanobot не тянет enterprise-стек».

- [ ] 7.1 Написать нативный document-tool агента поверх `office_files.py`;
      порог длины текста переносится из патча в его собственный код
- [ ] 7.2 Проверить, не расходятся ли две копии парсера: домен `legal`
      переезжает в платформу и тоже разбирает документы. Дублировать модуль
      нельзя — домен получает уже извлечённый текст
- [ ] 7.3 Перенести `tests/test_office_files.py` не трогая: тест остаётся
      в проекте агента
- [ ] 7.4 Перенести `workspace/skills/legal_summarizer/**` (80 модулей, 142 теста)
- [ ] 7.5 Переписать 4 архитектурных guard-теста legal под новый расклад файлов
- [ ] 7.6 `workspace/tools/legal_summarizer_query.py` → MCP-вызов, ~30 строк
- [ ] 7.7 Разложить legal по capability
      `capabilities/legal_summarizer/{skill/SKILL.md, tools/*.py, service/}`

**Приёмка:** legal работает; в репозитории агента нет ни одного импорта legal.
Парсер офисных файлов существует в проекте агента в единственном экземпляре.

---

## Фаза 8 — `audit_analyzer`

- [ ] 8.1 Обернуть `scripts/cli.py` в MCP-поверхность поверх capability `data` / `vectors`
- [ ] 8.2 Резолв `scripts_registry` из `TableRegistry` перенести в audit-сервис
- [ ] 8.3 SQL-безопасность (`sql_safety`) — внутри платформы, не в агенте
- [ ] 8.4 Удалить регистрацию audit-tool'ов из `project_tool_loader`
- [ ] 8.5 Разложить перенесённые домены по capability
      `capabilities/<name>/{skill/SKILL.md, tools/*.py, service/}`; проверить,
      что skill и операции ведут к одному сервису и не вложены друг в друга

**Приёмка:** агент не содержит `sqlglot`, `duckdb`, `faiss`, имён `oarb.*`.

---

## Фаза 9 — зависимости и финальная проверка

- [ ] 9.1 Разделить `requirements.txt`: runtime агента / `enterprise-mcp`
- [ ] 9.2 Офисные пакеты (`python-docx`, `openpyxl`, `pypdf`, `python-pptx`)
      **остаются** в требованиях агента — решение принято осознанно
- [ ] 9.3 Убрать из требований агента то, что уехало: `sqlglot`, `duckdb`,
      `pyarrow`, `faiss`, клиентский LLM-пакет
- [ ] 9.4 Обновить `AGENTS.md`, `CHANGELOG.md`, `docs/`
- [ ] 9.5 Для `enterprise-mcp`: старт без Nanobot, health, discovery, нормальный запрос,
      некорректный запрос, сбой инфраструктуры, таймаут
- [ ] 9.6 Прогнать тесты всех переехавших модулей в `mcp-platform`:
      `legal_summarizer` (80 скриптов + 142 тестовых файла), `audit_analyzer`,
      `llm_client`, `db.py`, `sql_safety`, `jsonb`, `clean_text`
- [ ] 9.7 `test_office_files.py` остаётся в прогоне агента — он не переезжает

**Приёмка:** чистое окружение с `nanobot-ai==0.3.5` поднимается и работает.
Сервер не требует Nanobot. Платформа обновляется независимо от агента.
