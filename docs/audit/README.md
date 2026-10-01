# Аудит кодовой базы — мастер-отчёт

Полный разбор продуктового кода: **215 файлов · 52 997 строк · 1619 символов**
(220 классов, 631 метод, 768 функций). Для каждого символа вынесен вердикт:
зачем он нужен, что делает, можно ли удалить и почему.

**Дата аудита:** 2026-09-30 · **Ветка:** `docs/skill-tool-boundary-agent-facing` · **База:** `693995d`
**Библиотека:** фактически установлена `nanobot-ai 0.3.5` (в документации указан 0.3.0 — см. находку D-1).

> **Границы охвата.** Разбиралось состояние рабочего дерева на момент старта аудита
> (закоммичено позже как `693995d`). В ходе аудита в репозиторий добавили новую
> подсистему **`mcp-platform/` — 10 файлов, 327 строк** (коммиты `f22c128`, `33d9e2d`,
> `693995d`: «phase 0-2», изолированный скелет MCP-серверов с архитектурным гардом).
> **Она не аудировалась** — не существовала на момент инвентаризации.
> Её файлы: `libs/` (3), `servers/_template/` (4), `tests/` (2), корневые `__init__.py` (2).

---

## 1. Как читать этот отчёт

| Файл | Что внутри |
|---|---|
| **Этот файл** | сводка, карта находок, план действий |
| [`AUDIT_PROTOCOL.md`](AUDIT_PROTOCOL.md) | правила, по которым выставлялись вердикты |
| [`_scripts/build_inventory.py`](_scripts/build_inventory.py) | AST-сканер, породивший брифы (воспроизводимо) |
| [`_data/`](_data/) | машинные данные: `inventory.json`, `orphans.md`, `dead_symbols.md`, `duplicates.md`, `test_gaps.md`, `_briefs/` |

### Отчёты по подсистемам

| # | Подсистема | Отчёт | Файлов | Вердиктов (О/У/Уд/Сл) |
|---|---|---|---|---|
| 01 | Ядро и точки входа (`lib/core`, `config.py`, `gateway.py`, `cli_agent.py`, `streamlit_app.py`) | [01-core-entrypoints.md](reports/01-core-entrypoints.md) | 12 | 127 / 27 / 29 / 4 |
| 02 | Кэш (`cache_provider*`, `duckdb_cache_store`, `cache_load_service`, `table_registry`) | [02-services-cache.md](reports/02-services-cache.md) | 16 | 83 / 17 / 23 / 1 |
| 03 | Рантайм-патчи и наблюдаемость (`runtime_patcher`, `context_compaction`, подписчики) | [03-services-runtime.md](reports/03-services-runtime.md) | 10 | 96 / 18 / 12 / 1 |
| 04 | Данные, LLM, сессии (`db_logging_service`, `llm_observer`, `session_cold_sync`) | [04-services-data.md](reports/04-services-data.md) | 12 | 77 / 21 / 4 / 2 |
| 05 | Хуки, CLI, lifecycle, сессии, события | [05-hooks-cli-lifecycle.md](reports/05-hooks-cli-lifecycle.md) | 13 | 44 / 23 / 6 / 0 |
| 06 | Каналы + `lib/utils` | [06-channels-utils.md](reports/06-channels-utils.md) | 16 | 74 / 12 / 5 / 1 |
| 07 | `workspace/tools`, `workspace/hooks`, `workspace/utils` | [08-workspace-plugins.md](reports/08-workspace-plugins.md) | 18 | 221 / 19 / 13 / 1 |
| 08 | Скилл `audit_analyzer` | [09-skill-audit-analyzer.md](reports/09-skill-audit-analyzer.md) | 13 | 39 / 9 / 5 / 4 |
| 09 | Скилл `legal_summarizer`: application / cache / chunking / execution | [10-skill-legal-core.md](reports/10-skill-legal-core.md) | 31 | 107 / 31 / 24 / 3 |
| 10 | Скилл `legal_summarizer`: слой `document` | [11-skill-legal-document.md](reports/11-skill-legal-document.md) | 18 | 47 / 14 / 24 / 1 |
| 11 | Скилл `legal_summarizer`: retrieval / llm / output / planning / CLI | [12-skill-legal-retrieval-cli.md](reports/12-skill-legal-retrieval-cli.md) | 32 | 48 / 21 / 40 / 8 |
| 12 | `tools/` (20 standalone-скриптов) | [13-tools.md](reports/13-tools.md) | 20 | 76 / 51 / 26 / 1 |
| 13 | `benchmarks/` | [14-benchmarks.md](reports/14-benchmarks.md) | 9 | 34 / 25 / 12 / 2 |

Плюс 3 пустых пакетных маркера `lib/channels/__init__.py`, `lib/utils/__init__.py`,
`workspace/hooks/__init__.py` (0 байт). **Вердикт: `Оставить`** — без них `lib.channels`
и `lib.utils` не импортируются как обычные пакеты; удаление ломает все существующие
абсолютные импорты.

### Сводка по вердиктам

| Вердикт | Символов | Доля |
|---|---:|---:|
| `Оставить` | 1073 | 66 % |
| `Упростить` | 288 | 18 % |
| `Удалить` | 223 | 14 % |
| `Слить с <файл>` | 29 | 2 % |
| `Перенести` | 3 | <1 % |
| **`НЕ РАЗОБРАНО`** | **0** | — |

**Покрытие: 212 из 215 продуктовых файлов**, 0 непокрытых символов. Тестовые файлы
(312) не аудировались как субъекты, но использовались как свидетельство использования.

> Расхождение с `inventory.json` (1619 символов против 1616 вердиктов) — агенты
> дополнительно нашли мёртвые модульные символы вручную, не попавшие в AST-каркас.
> Цифры выше — сумма по отчётам.

---

## 2. Что важно знать до чтения деталей

**Код функционально богаче, чем выглядит.** Ключевое наблюдение аудита: значимая
часть подсистем не «мёртвая потому, что не вызывается», а **живая, но не работает** —
пайплайн разорван в середине, инвариант не enforced, обработчик не подключён. Такие
находки формально не попадают в «удалить», но они важнее мёртвого кода.

**Три подсистемы старше, чем кажется.** `benchmarks/`, наблюдатель LLM-usage
и слой heading-скoring в `legal_summarizer` перестали работать при апгрейде
`nanobot` 0.3.0 → 0.3.5, а их тесты — зелёные, потому что мокают ровно там,
где ломается прод.

**Документация систематически отстала.** Удаление `cache_ownership.py`,
`pg_duckdb_sync_service.py`, `compact_command.py`, `unified_execution.py` и
перенос скиллов не доведены до конца в `AGENTS.md`, `docs/`, docstring'ах
и `SKILL.md`. Найдено 28 ссылок на несуществующие символы в 11 файлах одного
только слоя `document/`.

---

## 3. Находки по критичности

### Уровень A — ломает работу прямо сейчас

| # | Находка | Место | Статус |
|---|---|---|---|
| A-1 | **CLI зависает на выходе (дедлок).** `await gather(bus_task)` ждёт `AgentLoop.run()`, а `agent.stop()` вызывается после — цикл ждёт сам себя | `lib/cli/console_loop.py:476,479` | воспроизведено |
| A-2 | **Вложения из Postgres недоступны агенту и UI.** `SessionFileStore` безусловно дописывает `cache/sessions` к `base_dir`, а каналы передают `data_store/cache` → путь становится `data_store/cache/**cache**/sessions/` | `workspace/utils/session_file_store.py:147` + `lib/channels/postgres_channel.py:209` | воспроизведено |
| A-3 | **`bus.drain()` недостижим.** `ctx.stop()` вызывается уже после выхода из `asyncio.run` → `RuntimeError` гасится. Заявленный инвариант «дождаться in-flight» не работает ни в одном entrypoint | `lib/core/application_context.py:635` | подтверждено |
| A-4 | **`delete_session` не удаляет сессию.** `invalidate()` = no-op, запись воскресает из кеша при следующем `get_or_create` | `lib/session/pg_session_manager.py:112` | воспроизведено |
| A-5 | **Наблюдатель компакции не подключён.** `CompactionEventSubscriber` не конструируется нигде; `ChannelFactory` не передаёт его в канал → upstream-события сжатия никогда не пишутся в `agent_gateway_logs` | `lib/services/channel_factory.py:179` | подтверждено, 4 точки docstring'а не выполняют контракт |
| A-6 | **`NameError` в обработчике исключений канала.** `logger.opt(...)` ссылается на несуществующее модульное `logger` (есть только `self.logger`) — любой сбой subscriber'а = краш вместо warning'а | `lib/channels/postgres_channel.py:1590` | проверено AST |
| A-7 | **Задачи в статусе `error` никогда не возвращаются в пул.** Подзапрос выбирает `status='error'`, внешний `UPDATE … AND status='pending'` его отбрасывает → 0 строк, хотя `_mark_failed` и `streamlit.error_window_sec` обещают повтор | `lib/channels/postgres_channel.py:1187` | подтверждено |
| A-8 | **Обход SQL-guard'а: `EXPLAIN ANALYZE <DML>`.** `sqlglot` отдаёт внутренний текст как `Literal`, `[]` трактуется как «нарушений нет» → валидатор возвращает `None`. В PG `EXPLAIN ANALYZE` **выполняет** оператор | `lib/utils/sql_safety.py:239` | воспроизведено |
| A-9 | **Отладочный хук активен в проде и пишет открытый текст модели.** Нет флага `enabled`, только исключение из allowlist'а; пишет полный контент + reasoning в `data_store/debug_stream.log` без ротации и лимита | `workspace/hooks/debug_stream_diag.py` | подтверждено |
| A-10 | **`benchmarks/` не запускается вообще.** `config.SETTINGS` не инициализирован на импорте → `ValueError` даже для `--dry-run` | `benchmarks/db.py:52` | воспроизведено |
| A-11 | **56 из 57 бенчмарк-сценариев ждут несуществующих tool'ов** (`run_predefined_script`, `vector_search`, `nl_sql_generate`, `duckdb_query`) | `benchmarks/` YAML-артефакты | подтверждено |
| A-12 | **3 из 20 скриптов `tools/` падают при запуске** (`extract_office_structure.py`, `legal_benchmark.py`, `generate_comments_sql.py`) | `tools/` | воспроизведено |

### Уровень B — неверная логика при штатной работе

| # | Находка | Место |
|---|---|---|
| B-1 | **Терять chunk из reduce-входа безвозвратно.** `status="completed"` выставляется вне проверки наличия summary; при resume `_queued_batches` его пропускает, а файла нет | `legal_summarizer/execution/map_reduce.py:219` |
| B-2 | **Финальный flush `SessionColdSyncService.stop()` = no-op.** `stop()` ставит `_running=False` до `_sync_cycle()`, тот сразу выходит | `lib/services/session_cold_sync_service.py:170` |
| B-3 | **Observer LLM не подключён.** `getattr(config, "build_provider_snapshot", None)` ищет атрибут на `Config`, а это модульная функция `nanobot.providers.factory` → всегда `None` | `lib/core/agent_factory.py:208` |
| B-4 | **`page_count == 0` для каждого непустого DOCX.** `para_to_page` никогда не заполняется | `legal_summarizer/document/physical.py:338` |
| B-5 | **Валидация структуры вычисляется и выбрасывается.** `validate_structure` работает каждый прогон, но ни один потребитель в `scripts/` её не читает | `legal_summarizer/application/pipeline_structure.py:308` |
| B-6 | **`status: "failed"` возвращает exit 0.** Агент по коду возврата считает провал успехом | `legal_summarizer/application/service.py:320` → `cli.py:427` |
| B-7 | **Инвариант `max_active_llm_calls == 1` не enforced.** Лок берётся только в map-фазе; reduce и `service.py` без лока | `legal_summarizer/llm/single_flight.py` |
| B-8 | **Ветка retry в `SessionColdSyncService` не повторяет** — `retries += 1` без повторного вызова, метрика врёт | `legal_summarizer/application/execution_orchestration.py:118` |
| B-9 | **Утечка daemon-потока при shutdown.** Sentinel теряется при `queue.Full`, `_thread` обнуляется → `is_running()` врёт | `lib/services/db_logging_service.py:223` |
| B-10 | **`sql_safety` fail-open на `ParseError`.** Любой неподдерживаемый синтаксис проходит ми всех AST-правил | `lib/utils/sql_safety.py:178` |
| B-11 | **Битый snapshot блокирует вопросы навсегда.** Не вызывается `cache.invalidate()` → follow-up не восстановится | `legal_summarizer/application/service.py:133` |
| B-12 | **Нет heartbeat у арендованной задачи.** `updated_at` обновляется только при claim; рассинхрон таймаутов 120 с (код) против 600 с (конфиг) → живой агент переводится в `pending` | `lib/channels/postgres_channel.py:938` |

### Уровень C — безопасность и приватность

| # | Находка | Место |
|---|---|---|
| C-1 | Две дыры с одним корнем: `sql_safety` и `duckdb_cache_store._classify_sql` одинаково относят `EXPLAIN` к read-only; реальная защита только в режиме соединения | `lib/utils/sql_safety.py` + `lib/services/duckdb_cache_store.py:246` |
| C-2 | `sql_safety`: audit-trail отсутствует — `validate_sql_report`, `query_hash`, `SqlPolicy`, `allow_catalog_access` имеют 0 продуктовых вызывающих | `lib/utils/sql_safety.py` |
| C-3 | `format_schema` вставляет имена и комментарии без экранирования → низкий риск prompt-injection | `lib/utils/sql_safety.py:394` |
| C-4 | `media.serialize` читает файлы целиком в память и кодирует в base64 в jsonb-колонку, без лимита размера | `workspace/utils/media.py:126` |
| C-5 | Пароль в DSN в исходнике скрипта | `tools/smoke_post_cleanup.py:31` |
| C-6 | `workspace/utils/db.py::configure` переписывает глобальный DSN без переподключения живых воркеров; 9 мест вызова, разделения агентских и сервисных соединений нет | `workspace/utils/db.py:882` |
| C-7 | Allowlist хука `session_file_redirect_hook` смешивает два корня (`<repo>/workspace`), разрешая запись в `lib/`, `sql/`, `workspace/skills/` | `workspace/hooks/session_file_redirect_hook.py:66` |

### Уровень D — расхождения кода и документации

| # | Находка | Место |
|---|---|---|
| D-1 | Фактически установлен `nanobot 0.3.5`, а 0.3.0 указан в `AGENTS.md`, docstring'ах и `tools/scan_nanobot_inventory.py:148`. `benchmarks/` не пережил апгрейд: `wants_streaming()` и `aclose()` появились в 0.3.5 | репозиторий |
| D-2 | Фантомный переключатель `gateway.print_tools`: ключ есть в конфиге, читателей нет; при этом `agent_factory.py:15` и `terminal_tool_print_hook.py:18` документируют его как выключатель. Оператор выставит `false` — хук продолжит печатать | `project.json` + `lib/core/agent_factory.py` |
| D-3 | 7 ссылок на удалённый `patch_compact_command`; инвариант `FINAL_TURN_KEY` в CLI `/compact` неприменим, т.к. CLI не публикует outbound | `AGENTS.md:43`, `README.md:193`, `docs/ARCHITECTURE.md` |
| D-4 | `runtime_inventory.py:153` = `ExampleTool` против фактически регистрируемого `example_tool` → ложный DRIFT-баннер на каждом старте и exit 2 у `diagnose_startup.py` | `lib/services/runtime_inventory.py` |
| D-5 | `SKILL.md` скилла `audit_analyzer`: 4 из 6 описанных скриптов не существуют; предписанная команда `audit_analyze` удалена; ложно «все параметры optional» | `workspace/skills/audit_analyzer/SKILL.md` |
| D-6 | 28 ссылок на несуществующие символы в 11 файлах слоя `document/`; каталог `scripts/structure/` не существует | `legal_summarizer/scripts/document/` |
| D-7 | `call_llm_async` заявлен в `AGENTS.md`, `CHANGELOG.md`, `docs/ARCHITECTURE.md` — функции нет | `lib/services/llm_client.py` |
| D-8 | `docs/skill-tool-inventory.md` перечисляет 5 удалённых core-модулей и 3 удалённых tool'а как живые | `docs/skill-tool-inventory.md` |

---

## 4. Кандидаты на удаление

### 4.1 Файлы-кандидаты: 0 импортёров в production, 0 в тестах

| Файл | LOC | Основание |
|---|---:|---|
| `workspace/hooks/debug_stream_diag.py` | 70 | хук без `enabled`, пишет открытый текст модели в прод; `runtime_inventory` уже помечает его REMOVED. **См. A-9** |
| `workspace/utils/structure_cache.py` | 62 | `ImportError` — импортирует удалённый `office_files.extract_structure`; 0 импортёров |
| `workspace/skills/legal_summarizer/scripts/application/canonical.py` | 244 | назван «production-flow», но 0 production-импортёров; 25 попаданий только в 6 тест-файлов |
| `.../chunking/importance_score.py` | 108 | 0 ссылок в production и в тестах; docstring лжёт о потребителях |
| `.../chunking/order.py` | 44 | то же |
| `.../retrieval/` — 11 модулей | ~700 | прод-путь: `cli.py:405 → service.py:380 → pipeline_structure.py:180 → retrieval/index.py:78 → normalizer.py:70 + query.py:80`. Живы только `index.py`, `query.py`, `normalizer.py` |
| `.../document/block_ownership.py` | 109 | байт-идентичен функциям в `structure.py`, 0 импортёров. Канон — `structure.py` |
| `.../document/block_lookup.py` | 52 | 0 импортёров; `PhysicalDocument.blocks_by_ord` уже покрывает задачу |
| `.../document/safety_merge.py` | 147 | не импортируется пайплайном; слияние — no-op при смежных диапазонах |
| `tools/test_audit.py` | 735 | абсолютный Windows-путь, `mkdir` на импорте, 7 мёртвых локалей, недостижимый код |
| `tools/legal_benchmark.py` | 197 | сломан, нет entrypoint, дублирует `benchmarks/` |
| `tools/extract_office_structure.py` | 64 | сломан, потребителей нет |
| `tools/demo_internal_fallback.py` | 142 | тавтологичен; покрыт 25+ тестами `test_runtime_patcher.py` |

> `workspace/tools/example.py` — **оставить.** Это осознанный шаблон из `AGENTS.md`.
> Он не виден модели только потому, что `config.json:719` выставляет `"enable": false`;
> в blacklist'а загрузчика он не внесён, и при переключении флага зарегистрируется.

### 4.2 Мёртвые классы и функции (0 ссылок во всём репозитории)

| Символ | Место | Комментарий |
|---|---|---|
| `VectorIndexBuildService` | `lib/services/vector_index_service.py:41` | 0 вызовов; `tools/build_vectors.py:78` импортирует только re-export `get_embedding`. FAISS он тоже не строит |
| `IndexIntegrityError` | `lib/services/cache_provider.py:68` | `raise` — 0 во всём репо, но спека `cache-provider/spec.md:143` требует проверку. **Выбор: реализовать или удалить вместе с требованием** |
| `execute_readonly` | `lib/services/duckdb_cache_store.py:1134` | 0 prod-вызовов; docstring утверждает «используется `CacheProvider.execute_readonly`» — метода не существует |
| `_capture_schema_meta` | `lib/services/cache_provider_impl.py:392` | 0 prod-вызовов, дублирует `DuckDbCacheStore._save_schema_meta` |
| `tracking_column_for` | `lib/services/table_registry.py:300` | логически неверен (колонка первой подходящей регистрации) и не вызывается |
| `snapshot_path` | `lib/services/table_registry.py:327` | возвращает legacy-путь, объявленный неподдерживаемым |
| `get_active_profile` | `config.py:662` | 0 ссылок после удаления env-выбора профиля |
| 6 функций `skill_config` | `lib/core/skill_config.py` | `get_in_memory_cache_path`, `get_vector_index_path`, `get_vector_indexes`, `load_db_config`, `get_tool_config`, `get_embedding_model` — скиллы их не делегируют (проверено по обеим обёрткам) |
| `build_logging_bus` | `lib/core/bus_factory.py:104` | 0 вызовов |
| `is_stream_delta` | `lib/utils/outbound_meta.py:48` | ключа `_stream_delta` нет и в установленном `nanobot` |
| `drain_calls` | `lib/hooks/tool_audit_hook.py:129` | 0 вызовов; заодно устраняется утечка `_calls` |
| `get_timeout_sec`, `get_max_retries` | `legal_summarizer/llm/config.py` | обе мертвы |
| `_MAX_WAIT` | `streamlit_app.py:120` | цикл опроса намеренно бесконечен; держится наедине ассертом |
| `__enter__`/`__exit__` | `lib/services/subprocess_manager.py:113` | 0 вызовов |
| `_INFRA_KEY_VECTOR_STORAGE` | `lib/core/application_context.py:1615` | мёртвая константа **с неверным значением** (`vector_index.storage` вместо `vector.storage`) |
| 5 мёртвых функций heading-скоринга | `legal_summarizer/document/heading.py` | `apply_confidence_penalties`, `HeadingEvidence`, `compute_evidence`, `apply_evidence_scoring`, `filter_above_threshold` — все вызовы в тестах |
| Кластер `_effective_level`/`_resolve_level` | `legal_summarizer/document/hierarchy.py` | 54 LOC, 0 вызывающих во всём репо |
| `__enter__` сносок в LLM | `benchmarks/hooks.py:47` | `dict(context.usage)` всегда `TypeError` |

### 4.3 Дублирование, подтверждённое чтением кода

| Дубль | Канон | Комментарий |
|---|---|---|
| `build_block_ownership` — `document/structure.py:428` и `document/block_ownership.py:41` | `structure.py` | байт-идентичны; `block_ownership.py` имеет 0 импортёров, несмотря на свой docstring «canonical» |
| `block_to_node` — **три** копии | метод на `DocumentStructure` | `structure.py:297`, `structure.py:478`, `block_ownership.py` |
| `deterministic_truncate` (`execution/hierarchical.py:11`) ≡ `fit_input` (`chunking/_text_helpers.py:54`) + третья обёртка `_fit_input` | `chunking/_text_helpers.py` | байт-в-байт |
| `_collect_owner_section_ids` (`chunker.py:483`) ≡ `_collect_section_ids_for_range` (`structural_packing.py:141`) | `structural_packing.py` | байт-идентичны, **оба живые** (2 и 5 вызовов) |
| `_resolve_sfs_base` — `postgres_channel.py:67` и `redis_channel.py:104` | общий модуль | идентичные 4 строки |
| allowlist хуков — `lib/cli/hook_loader.py:126` и `runtime_inventory.py:88` | один источник | две рукописные копии, не связанные ничем → источник ложного DRIFT-баннера |
| `resolve_dsn` — `tools/migrate.py:75` и `workspace/utils/db.py:867` | `workspace/utils/db.py` | env-ветка `migrate.py` мертва |
| `contextWindowTokens` — `pipeline_structure._read_context_window_tokens` и `brief_context._resolve_context_window_tokens` | один | |
| sys.path-хак | один helper | **4 копии** в `benchmarks/` и скиллах; скрытый контракт на CWD |
| `_usage_to_dict` — `database_logging_hook.py:253` и `runtime_events_subscriber.py:217` | один | обе деградируют в тихое `None` |
| `try/return run(fn)/except` | `workspace/utils/db.py` | 4-я копия в `db_logging_service.py:802` |
| `prompts.py` + `prompts_runtime.py` | один | обоснование разделения устарело на 2 рефакторинга; ноль пересечений символов |
| Три баннера diff→gate→`Panel`→fallback | один | ~150 LOC в `application_context.py:761-995` |
| Три ~24-строчных «skip»-блока | `_record_sync_skipped` | хелпер существует, помечен DEPRECATED — и именно он нужен |

### 4.4 Мёртвые настройки

- `logging.db.dialect`, `logging.db.connect_backoff_sec`, `logging.db.connect_backoff_max_sec` — 3 мёртвых поля в сервисе ⇒ 3 мёртвых ключа в `project.json:518-520` и в `REQUIRED_KEYS`.
- `retention_days` и `purge_interval_sec` читаются с дефолтами, но отсутствуют в `project.json`, хотя описаны в `AGENTS.md`.
- `gateway.cache` — секции **нет вообще**, а код читает `gateway.cache.local_path` (это же подтвердил независимый аудит: NFS/DuckDB-проблема пользователя).
- `gateway.print_tools` — есть в конфиге, читателей нет.
- `HeartbeatSettings` — 0 потребителей, секции `gateway.heartbeat` нет ни в `project.json`, ни в коде.
- `VectorIndexSettings.enable` и `default_root` — объявлены, в подсистеме кэша не читаются; `AGENTS.md:108` обещает «гейт `enable`», которого нет.

---

## 5. Рекомендуемый порядок работ

Волны выстроены так, чтобы каждая давала самостоятельную ценность и не ломала
инварианты, которые проверяются в CI. Оценка LOC — по продуктовому коду, без тестов.

### Волна 0 — починить то, что сломано (без удаления кода)

| Действие | Файлы | Почему первым |
|---|---|---|
| A-1 дедлок CLI | `lib/cli/console_loop.py` | блокирует использование CLI |
| A-3 `bus.drain()` | `lib/core/application_context.py` | не теряются in-flight задачи |
| A-4 `invalidate()` | `lib/session/pg_session_manager.py` | удаление сессий работает как задумано |
| A-6 `NameError` | `lib/channels/postgres_channel.py` | одно исправление, снимает класс крашей |
| A-7 мёртвый `error`-retry | `lib/channels/postgres_channel.py` | задачи перестают теряться |
| A-5 подключить подписчик | `lib/services/channel_factory.py` | наблюдаемость сжатия |
| A-9 выключить/удалить debug-хук | `workspace/hooks/debug_stream_diag.py` | утечка открытого текста модели |
| A-8 + B-10 закрыть SQL-guard | `lib/utils/sql_safety.py` | security-граница |
| C-5 убрать пароль из DSN | `tools/smoke_post_cleanup.py` | ротация секретов |

### Волна 1 — удалить доказуемо мёртвое (0 ссылок, без изменений в вызывающем коде)

~2000 LOC. Всё перечислено в §4.1–4.2. **Требует:** поправить `tests/test_config_keys.py`
(`REQUIRED_KEYS`) для мёртвых настроек; удалить ~11 тест-файлов к `retrieval/`;
переписать 7 тест-файлов, кодирующих не-используемый heading-скоринг
(`test_heading_*`, `test_integration_nk_realistic`, `test_structure_no_empty_reduce`,
`test_structure_low_quality_pdf`) — **это отдельное решение владельца**: удаление
скоринга уничтожает предмет этих тестов, и нужно понять, валидировали они
задуманное поведение или фиксировали брошенный дизайн.

### Волна 2 — слить дубли и починить документацию

~1500 LOC дубликатов (§4.3) + D-1…D-8. Порядок безопасен: правки локальные,
наборы тестов не меняются. Особое внимание: `runtime_inventory` и `hook_loader`
должны иметь **один** источник правды, иначе DRIFT-баннер будет мигать.

### Волна 3 — пересмотреть подсистемы, а не чинить их

| Подсистема | Вопрос владельцу | Объём |
|---|---|---|
| `benchmarks/` | Восстанавливать или удалять? Сценарии, тесты и логика рассинхронизированы; ни одного реального прогона в истории | 2 671 LOC |
| observer LLM | Нужна ли метрика расхода токенов/стоимости? Сейчас нет ни потребителя, ни источника | ~200 LOC |
| `tools/` (6 одноразовых) | Для каждого: процедура из `docs/`, или разовый артефакт? | ~1 800 LOC |
| heading-скоринг скилла | Удалять (см. волну 1) или дотащить до прода? | ~300 LOC |
| `retrieval/` (11 модулей) | Удалять остров целиком — 100% покрыт тестами, которые тестируют только его | ~700 LOC |

### Волна 4 — двойной путь чтения конфигурации

`lib/core/project_settings.py` валидирует ~120 полей, а потребители берут raw `SETTINGS`
через `ConfigService.settings_section()`; `ctx.project_settings` читается в одном месте
(`flush_interval_sec`). Плюс `_LazySettings` не поддерживает протокол `Mapping`,
хотя используется как `dict`. Это архитектурное решение, не чистка.

---

## 6. Ограничения аудита

- **Тесты не запускались целиком.** Часть выводов подтверждена воспроизведением
  (A-1…A-4, A-8, A-10, A-12), часть — статическим анализом и чтением. Что именно
  воспроизведено, указано в отчётах.
- **Нет подключения к PostgreSQL/Greenplum.** Не проверены на живой БД: SQL с
  псевдонимом в `check_worker_pool_integrity.py:188`, поведение advisory-lock на
  Greenplum 6.5, `EXPLAIN ANALYZE` в DuckDB/PG-совместимом режиме.
- **LLM-вызовы не выполнялись.** Оценка «до 16 вызовов на инвокацию» в
  `generated_sql_mode` — расчётное верхнее значение.
- **Статический анализ даёт ложные «сироты»:** скиллы импортируются от корня
  `scripts/`, tool'ы и хуки подключаются сканированием каталога. Первая версия
  инвентаризатора показала 113 «модулей без импортёров»; после учёта алиасов — 57,
  и все оставшиеся объяснимы (entry points, пакетные маркеры, standalone-скрипты).
  Каждый вердикт `Удалить` в отчётах перепроверен grep'ом вручную.
- **`mcp-platform/` (10 файлов, 327 строк) не аудировался** — подсистема добавлена
  в репозиторий уже после того, как инвентаризация зафиксировала 215 файлов.
  Это единственный известный пробел в охвате.

---

## 7. Приложение: воспроизведение

```bash
# пересобрать инвентаризацию и брифы (AST-сканер)
python docs/audit/_scripts/build_inventory.py

# машинные данные
#   docs/audit/_data/inventory.json     полный граф символов
#   docs/audit/_data/orphans.md         модули без статических импортёров
#   docs/audit/_data/dead_symbols.md    символы с 0 ссылок
#   docs/audit/_data/duplicates.md      структурно идентичные тела функций
#   docs/audit/_data/test_gaps.md       модули без тестов
#   docs/audit/_data/_briefs/*.md       входные каркасы для аудиторов
```
