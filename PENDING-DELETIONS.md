# Файлы, которые нельзя удалить самому

Ассистент работает в окружении, где удаление файла заблокировано политикой
безопасности: команда удаления не выполняется, потому что в системе нет
служебного лаунчера `mavis-trash` (есть только `mavis-trash.cmd` и
`mavis-trash.js`, а обёртки вызывать запрещено). Обойти это запрещено и не
нужно: файл, который больше не нужен, просто перестаёт участвовать в работе
(код вырезается, имя меняется на `_`-префикс, если на это смотрит загрузчик).

Поэтому удаление сводится к списку ниже. **После удаления строку убрать отсюда.**

## Кластер снимка в агенте (фаза 5, п. 5.1–5.4, 5.10, 3.14)

Снятие с агента сделано соседним воркером (`c660b6f`): `build_cache_provider`,
`_init_cache_runtime`, `CacheSettings`, `check_duckdb_cache` и `_make_preload` из
кода убраны, навык `audit_analyzer` ходит в capability `audit` платформы. Остался
сам кластер — в рантайме он уже никем не вызывается.

**Проверено по `import` (после `c660b6f`), production-код агента:**

| Модуль | Кто импортирует помимо самого кластера |
|---|---|
| `preload_service.py` | **никто** — сосед снял последнего вызывающего |
| `cache_load_service.py` | **никто** |
| `duckdb_cache_store.py` | только `cache_provider.py:399` (взаимная связка) |
| `duckdb_query.py` | только `duckdb_cache_store.py:1077–1374` |
| `cache_provider.py` | `tools/build_vectors.py:437`, `tools/check_indexes.py:182` |
| `cache_provider_impl.py` | `tools/build_vectors.py:73`, `tools/check_indexes.py:47,121` |
| `vector_index_service.py` | `tools/build_vectors.py:78` |
| `retry.py` | `cache_provider_impl.py:297` и `llm_client.py:28` (мёртвый файл) |

Вывод: единственное, что держит кластер снаружи, — две утилиты
`tools/build_vectors.py` и `tools/check_indexes.py`. `llm_client.py` production-
импортёров не имеет вовсе, поэтому `retry.py` уходит вместе с ним же.

**Пункт 3.6. Перенос сделан и проверен на живой базе.**

| Утилита агента | Куда уехала | Состояние |
|---|---|---|
| `tools/build_vectors.py` | `mcp-platform/libs/vectors/builder.py` (конвейер) + `mcp-platform/servers/enterprise/build_index.py` (операторский вход) | 23 теста на подставных БД и эмбеддере **плюс живой прогон 2026-10-01** |
| `tools/check_indexes.py` | логика целиком: `preload.py::compute_index_health` (missing/orphan/stale/divergence) + `signature.py::verify_index_signature` + `runtime.py::list_runtime_vector_indexes` | покрыто платформой. **Не переносилось:** CLI-обёртка с exit-кодами 0/1/2 для CI/pre-deploy. Это новая потребность, а не порт — если она нужна, это отдельная маленькая точка входа поверх `compute_index_health` |

**Живая проверка 2026-10-01** — PostgreSQL, Ollama `mxbai-embed-large`
(1024 измерения), снимок `C:\Users\Алексей\.cache\nanobot\duckdb\cache.duckdb`:

* `--dry-run` по всем индексам: 120 строк, **120 хешей совпали** с посчитанными
  агентским кодом 13 сентября. Совпали формат `search_text`, `content_hash` и
  `norm_pk` — то есть порт считает то же, чем наполнены production-данные, а
  не просто проходит собственные тесты.
* `--full-rebuild --index audit_reports_index`: 10 чанков переэмбеддены
  настоящим провайдером, 10 вставок, 0 ошибок; хеши до и после идентичны.
* Настоящий сервер как подпроцесс + клиент агента + `vector_search` по трём
  индексам: точные попадания первыми результатами, `_meta` с `request_id`
  доехал по проводу.

Операторский вход — не capability: операций у него нет, в реестр он не
попадает, модель его не видит. Сборка — писатель **PostgreSQL**, снимок она
не открывает, поэтому после сборки требуется перезагрузка снимка.

Блокер снят: 3.6 выполнен и подтверждён на данных, поэтому кластер снимка
больше никем в агенте не держится и удаляется одним проходом.

**УДАЛЕНО 2026-10-01.** Код вырезан, файлы переименованы в `_`-имена
(4892 строки в 9 файлах), как и принято в этом реестре. Перед сносом
проверено разбором AST по 509 файлам агента: `duckdb`, `faiss`, `numpy`,
`pyarrow` не импортирует ни один исполняемый модуль, поэтому пакеты убраны из
`requirements.txt` агента (п. 10.3 закрыт).

| Файл | Строк | Куда уехал |
|---|---|---|
| `lib/services/_duckdb_cache_store.py` | 1455 | `mcp-platform/libs/enterprise_data/snapshot/store.py` |
| `lib/services/_cache_provider.py` | 442 | `snapshot/reader.py` + `store.py` |
| `lib/services/_cache_provider_impl.py` | 488 | `snapshot/store.py` (вместе с `resolve_cache_path`) |
| `lib/services/_cache_load_service.py` | 472 | `mcp-platform/libs/enterprise_data/loader.py` |
| `lib/services/_preload_service.py` | 318 | `mcp-platform/libs/vectors/preload.py` |
| `lib/services/_vector_index_service.py` | 71 | `mcp-platform/libs/vectors/builder.py`, `indexing.py` |
| `lib/utils/_duckdb_query.py` | 338 | `mcp-platform/libs/enterprise_data/snapshot/query.py` |
| `tools/_build_vectors.py` | 1023 | `mcp-platform/servers/enterprise/build_index.py` |
| `tools/_check_indexes.py` | 285 | операция `index_stats` capability `vectors` |

Тесты агента на снятый код — 16 файлов, 4423 строки. Перед удалением инварианты
разложены по адресатам, а не выброшены:

* `TestComputeIndexHealthNewSources` → **портирован** в
  `mcp-platform/tests/test_vectors_index_health.py` (5 тестов, 5/5 мутаций);
* `TestBuildFaissIndexMinimalMeta` → **портирован** в
  `mcp-platform/tests/test_vectors_index_metadata.py` (3 теста): «meta
  содержит только metric» охранял память процесса, и на платформе его не
  охранял никто;
* `test_vector_search_silent_failure.py` (14) → перекрыт
  `mcp-platform/tests/test_vectors_indexing.py` (`TestAsVector`,
  `TestBuildFaissIndex`, `TestBuildRawItems`); структурная проверка «не читать
  `conn` после блока чтения» на платформе невозможна по построению — владельцу
  индекса передаётся только `fetch_fn`;
* `TestNoHardcodedTableNames` → перекрыт `tests/test_no_hardcoded_table_names.py`;
* `TestRemovedMethodsNoCallers`, `TestLoadIndexOnlyUsesCachePath`,
  `TestPreloadIndexesUsesOnlyConfig` → **вакуумны**, охраняли удалённый модуль.

```bash
# кластер снимка:
git rm lib/services/_duckdb_cache_store.py \
      lib/services/_cache_provider.py \
      lib/services/_cache_provider_impl.py \
      lib/services/_cache_load_service.py \
      lib/services/_preload_service.py \
      lib/services/_vector_index_service.py \
      lib/utils/_duckdb_query.py \
      tools/_build_vectors.py \
      tools/_check_indexes.py

# тесты кластера:
git rm tests/_test_duckdb_cache_store.py \
      tests/_test_cache_provider_mode.py \
      tests/_test_cache_provider_open_failure.py \
      tests/_test_cache_no_file_hold.py \
      tests/_test_cache_load_service.py \
      tests/_test_preload_service.py \
      tests/_test_cache_provider_meta.py \
      tests/_test_get_embedding_auth.py \
      tests/_test_build_vectors_cli.py \
      tests/_test_check_indexes.py \
      tests/_test_shared_cache_path_across_profiles.py \
      tests/_test_cache_readiness_and_skill_role.py \
      tests/_test_single_cache_interface.py \
      tests/_test_remove_vector_index_store_guards.py \
      tests/_test_vector_search_silent_failure.py
git rm -r tests/integration/_test_vector_build_e2e.py

# после переноса 3.6:
git rm lib/services/duckdb_cache_store.py \
      lib/services/cache_load_service.py \
      lib/services/cache_provider.py \
      lib/services/cache_provider_impl.py \
      lib/services/vector_index_service.py \
      lib/services/preload_service.py \
      lib/utils/duckdb_query.py \
      lib/utils/retry.py
```

`llm_client.py` и `llm_config.py` в этот список не попали: они удаляются
отдельно и уже перечислены ниже, в «Обязательно удалить». В проход кластера они
попадают только как держатели `retry.py` и `httpx`.

**Тесты делятся на три группы — одна команда `git rm` здесь не годится.**

Удаляются вместе с кластером, потому что их предмет и есть кластер:

```bash
git rm tests/test_duckdb_cache_store.py \
      tests/test_cache_provider_mode.py \
      tests/test_cache_load_service.py \
      tests/test_cache_no_file_hold.py \
      tests/test_cache_provider_open_failure.py \
      tests/test_cache_provider_meta.py \
      tests/test_single_cache_interface.py \
      tests/test_preload_service.py \
      tests/test_get_embedding_auth.py \
      tests/test_vector_search_silent_failure.py \
      tests/integration/test_vector_build_e2e.py
```

Требуют **правки**, а не удаления: импорт инцидентный, а тест проверяет другое.
Их нельзя убирать молча — прогон упадёт на `ImportError`.

| Файл | Что в нём импортировано |
|---|---|
| `tests/test_application_context.py:444–500` | `resolve_cache_path` (6 мест) |
| `tests/test_auto_register_skills.py:272–287` | `read_embedding_config` (3 места) |
| `tests/test_skill_config_api.py:140,152` | `read_vector_index_config` |
| `tests/test_unified_event_logging_contract.py:301,315` | `CacheLoadService`, `_emit_health_event` |
| `tests/test_application_context_cache_lifecycle.py:74,86,94` | `CacheLoadService` — сосед уже вырезал отсюда большую часть |

Решение при выполнении, предмет смешанный (проверены только импорты):

| Файл | Импорт | Чем занят |
|---|---|---|
| `tests/test_cache_readiness_and_skill_role.py:136,155` | `DuckDbCacheStore`, `CacheProvider`, `CacheStore` | 4.2 readiness + 3.6 роль навыка |
| `tests/test_remove_vector_index_store_guards.py:243–278` | `build_faiss_index`, `compute_index_health` | guard'ы вокруг удалённого store |
| `tests/test_shared_cache_path_across_profiles.py:80,93` | `resolve_cache_path` | владение `gateway.cache.local_path` профилем |
| `tests/_test_sql_safety.py:202,212` | `duckdb_query`, `duckdb` | отключён (`_` в имени), гонять нельзя |

Вместе с утилитами уезжают по 3.6 их собственные тесты и сами утилиты:

```bash
git rm tools/build_vectors.py \
      tools/check_indexes.py \
      tests/test_build_vectors_cli.py \
      tests/test_check_indexes.py
```

Приёмка фазы 5 — `grep -R "duckdb" lib/ workspace/` пуст — достижима только
после этого: сейчас не пуст, потому что файлы стоят в обоих репозиториях.

### Зависимости, которые уходят вместе с кластером (п. 10.3)

Пять пакетов нельзя вычистить раньше: каждый импортируется удаляемым кодом.
Проверено по `import`, в пределах агента:

| Пакет | Импортируется из |
|---|---|
| `httpx` | `cache_provider_impl.py:282`, `llm_client.py:156` |
| `pyarrow` | `duckdb_cache_store.py:96` |
| `numpy` | `duckdb_cache_store.py:1360`, `duckdb_query.py:278,318`, `build_vectors.py:856` |
| `faiss-cpu` | `duckdb_cache_store.py:1366`, `duckdb_query.py:278`, `build_vectors.py:855` |
| `duckdb` | `duckdb_cache_store.py:491`, `tests/_test_sql_safety.py:202` |

`httpx` можно снять раньше остальных: оба его импортёра уже стоят в списке
удаления, а `llm_client.py` мёртв. Остальные ждут 3.6. `sqlglot` из требований
уже убран (осталась строка-комментарий).

## Снимок не пересоздавался — исправлено 2026-10-02

Запись в реестр, а не список удаления: дефект найден при переносе загрузчика
на платформу и уже исправлен, но починить его заново никто не станет, а
поведение легко принять за правильное.

`SnapshotLoadService` вызывает `replace_records` — то есть берёт **объявленные**
таблицы целиком. Таблица, оставшаяся от прежнего объявления, не объявлена
сейчас, а потому не удаляется **никогда**. Проверено опытом, а не рассуждением:
прибор добавил `oarb.stale_marker`, выполнил обычную загрузку — остаток выжил,
таблиц в снимке стало 7 вместо 6. Значит снимок копил мусор, которого нет ни в
одном объявлении, и этот мусор виден любому, кто перечислит таблицы снимка.

| Что | Где |
|---|---|
| `DuckDbSnapshotStore.reset()` — снос всех пользовательских схем | `mcp-platform/libs/enterprise_data/snapshot/store.py` |
| Объявление роли записи: `CacheIngestion.reset()` | `mcp-platform/libs/enterprise_data/snapshot/contracts.py` |
| Отказ заглушки: `UnavailableSnapshot.reset()` | `mcp-platform/libs/enterprise_data/snapshot/unavailable.py` |
| Стирание перед загрузкой; `--fresh` делает умолчание видимым | `mcp-platform/servers/enterprise/load_snapshot.py` |

Решения, которые стоит знать, прежде чем «упрощать»:

* **Чистка объектами внутри файла, а не файлом.** Файл держат читатели (агент,
  `enterprise-mcp` открывают его на чтение), а удалять его оператору нечем.
  `DROP SCHEMA ... CASCADE` по всем схемам кроме системных — тоже удаление,
  но локальное: держателей оно не затрагивает.
* **Системные схемы — в `_SYSTEM_SCHEMAS`.** `main` сносить нельзя: это
  умолчание DuckDB, и снос схемы снёс бы само подключение. `__nanobot_meta`
  там **нет** намеренно: комментарии к колонкам не должны переживать таблицу,
  которой больше нет.
* **`DISTINCT` в выборке схем обязателен.** DuckDB отдаёт по строке на каталог,
  и `main` приходит трижды; без `DISTINCT` список удалённых схем и лог
  показывали бы одно имя несколько раз.
* **Стирание идёт до загрузки.** Обратный порядок выглядел бы бережнее, но
  оставил бы «почти прежний» снимок, который читается как свежий. Пустой
  снимок виден сразу: `get_stats()` сообщает ноль таблиц.
* **`--table` снимок не стирает, `--fresh` с ним отвергается** (код 2). Стереть
  всё и положить обратно одну таблицу — значит тихо оставить снимок неполным
  при отчёте «загружено 1/1, ошибок 0».
* **`reset()` объявлен в ABC, а не только в реализации:** писатель ровно один, и
  подмена хранилища обязана уметь его позвать, а не падать на `AttributeError`
  в момент загрузки.

Живая проверка 2026-10-02: `python -m servers.enterprise.load_snapshot` снёс 3
схемы (`__nanobot_meta`, `oarb`, `public`), загрузил 6/6 таблиц и 325 строк, 0
ошибок. `oarb.stale_marker` в файле отсутствует, 57 комментариев колонок
восстановлены, файл ужался с 10 760 192 до 5 517 312 байт — мусор освобождён
по-настоящему, а не только скрыт. Отказ `--fresh --table` проверен на живом
файле: код 2, размер и mtime не изменились.

## Обязательно удалить

| Файл | Почему | Команда |
|---|---|---|
| `mcp-platform/servers/enterprise/capabilities/data/tools/_claim_task.py` | Заглушка на месте удалённой операции `claim_task` (коммит `944535e`). Код операции вырезан, файл оставлен пустым намеренно: загрузчик по соглашению пропускает модули с именем, начинающимся с `_`, поэтому операция не публикуется. Сам файл не нужен | `git rm mcp-platform/servers/enterprise/capabilities/data/tools/_claim_task.py` |
| `mcp-platform/servers/enterprise/capabilities/data/tools/_update_task_status.py` | То же для `update_task_status`: обе операции над очередью задач удалены решением владельца, в capability `data` очередь не осталась | `git rm mcp-platform/servers/enterprise/capabilities/data/tools/_update_task_status.py` |
| `mcp-platform/_live_audit_tables.py` | Черновой прогон по таблицам аудита, в git не отслеживается, к миграции не относится | удалить вручную (файл не отслеживается, `git rm` не подходит) |
| `lib/services/llm_client.py` | Мёртвый код: общение с моделью принадлежит платформе, навыки ходят в `mcp-platform/libs/llm` через `libs/enterprise_client/llm.py`. Production-импортёров не осталось: сосед снял последнего вызывающего из `project_settings.py`. Держат его только `tests/test_dependency_direction.py:143` (упоминание в комментарии) и страж `tests/test_llm_goes_through_mcp.py::test_agent_has_no_llm_client_module`, помеченный `xfail` до ручного удаления | `git rm lib/services/llm_client.py` |
| `lib/services/llm_config.py` | То же. Импортируется из `llm_client.py:39` и из `tests/test_llm_config.py` (6 мест) — то есть пока жив первый, второй нельзя выкинуть молча. Пока жив и `llm_client.py`, `lib/utils/retry.py` (пункт 3.14) считался заблокированным вторым держателем | `git rm lib/services/llm_config.py` |

Порядок важен: `llm_client.py` удаляется раньше `llm_config.py`, иначе падает
`tests/test_llm_config.py`. Вместе с ними уходит и сам тест мёртвого модуля —
предмет у него ровно один:

```bash
git rm tests/test_llm_config.py
```
| `mcp-platform/.sessions_demo/` | Демонстрационный каталог файлов сессии, оставшийся после прогона `SessionWorkspace` вручную. Содержит только синтетические артефакты `sess-DEMO-1`, к проекту не относится | удалить вручную (каталог не отслеживается) |
| `sql/vectors/create_vector_index_config.sql` | Мёртвый DDL: самая конфигурация индексов живёт в `project.json`. `migrate.py` обходит только `sql/migrations/`, файл не исполняется. Код вырезан, осталась заглушка (фаза 5, п. 5.9) | `git rm sql/vectors/create_vector_index_config.sql` |
| `sql/vectors/create_vector_index_store.sql` | Мёртвый DDL: persisted FAISS-кеш удалён ещё change `remove-vector-index-store`, таблица снесена миграцией `V003`. Не исполняется. То же, что и предыдущий (фаза 5, п. 5.9) | `git rm sql/vectors/create_vector_index_store.sql` |
| `project.json → gateway.vector.index.*` | **Дубликат объявлений.** `mcp-platform/platform.json` сам признаёт его: «остаётся до переноса build-инструментов (фаза 8)». Перенос сделан (`mcp-platform/libs/vectors/builder.py`), условие выполнено. Читает агент: `lib/core/infra_registration.py:39` ← `lib/core/application_context.py:1292`. Правка чужой части конфига — сначала согласовать | вырезать секцию из `project.json` |
| `project.json → skills.audit_analyzer.tables` | **Дубликат объявлений.** Объявление переехало в `mcp-platform/platform.json → audit.tables`; агент больше не отдаёт его процессу. Читает только `tools/generate_comments_sql.py:36` — оставшийся черновик. Таблица объявляется дважды в двух конфигах, и разъедутся они молча | вырезать секцию + `git rm tools/generate_comments_sql.py` |

## Python-слой навыка `audit_analyzer` (фаза 9)

Навык перестал владеть данными: запросы строит и проверяет capability `audit`
платформы, агент ходит до неё инструментом `audit_analyzer_query`. Всё
перечисленное не импортируется ничем, но лежит на диске и убирается вручную.
Код внутри каждого файла вырезан, осталась заглушка-описание: загрузчик по
соглашению пропускает модули с именем, начинающимся с `_`.

| Файл | Что было | Строк |
|---|---|---|
| `lib/utils/_sql_safety.py` | SQL Security Guard агента; копия живёт в `mcp-platform/libs/enterprise_data/sql_safety.py` | 425 |
| `workspace/skills/audit_analyzer/scripts/_cli.py` | CLI навыка (`--mode predefined/vector/generated_sql`) | 411 |
| `workspace/skills/audit_analyzer/scripts/_generated_sql_mode.py` | NL→SQL в агенте; порт в `mcp-platform/libs/audit/` | 424 |
| `workspace/skills/audit_analyzer/scripts/_llm.py` | HTTP-клиент модели; выбора модели у агента больше нет | — |
| `workspace/skills/audit_analyzer/scripts/_skill_config.py` | Обёртка над `lib.core.skill_config` для одного навыка | — |
| `workspace/skills/audit_analyzer/scripts/_output.py` | Сериализация вывода CLI | — |
| `workspace/skills/audit_analyzer/scripts/_package_init.py` | `__init__.py` пакета `scripts` (Python не даёт файлу с `_` быть пакетом) | — |
| `workspace/skills/audit_analyzer/scripts/_removed_predefined/` | 6 модулей DB-first реестра скриптов; порт в `mcp-platform/libs/audit/predefined.py` + `registry_loader.py` | 931 |
| `workspace/skills/audit_analyzer/_removed_tests/` | 4 файла тестов навыка (не в `tests/`, а внутри скилла) | ~1784 |

Тесты агента на снятый код (pytest не собирает `_test_*.py`):

| Файл | Строк |
|---|---|
| `tests/_test_audit_analyzer_cli.py` | 411 |
| `tests/_test_audit_analyzer_mode_selection.py` | 311 |
| `tests/_test_audit_analyzer_generated_sql.py` | 424 |
| `tests/_test_skill_cache_boundary.py` | 146 |
| `tests/_test_skill_tool_integration.py` | 211 |
| `tests/_test_sql_safety.py` | 250 |

Удалить одним проходом:

```bash
git rm lib/utils/_sql_safety.py \
  workspace/skills/audit_analyzer/scripts/_cli.py \
  workspace/skills/audit_analyzer/scripts/_generated_sql_mode.py \
  workspace/skills/audit_analyzer/scripts/_llm.py \
  workspace/skills/audit_analyzer/scripts/_skill_config.py \
  workspace/skills/audit_analyzer/scripts/_output.py \
  workspace/skills/audit_analyzer/scripts/_package_init.py \
  tests/_test_audit_analyzer_cli.py \
  tests/_test_audit_analyzer_mode_selection.py \
  tests/_test_audit_analyzer_generated_sql.py \
  tests/_test_skill_cache_boundary.py \
  tests/_test_skill_tool_integration.py \
  tests/_test_sql_safety.py
git rm -r workspace/skills/audit_analyzer/scripts/_removed_predefined \
          workspace/skills/audit_analyzer/_removed_tests
```

Не отслеживается git, `git rm` не подходит:

| Файл | Что это |
|---|---|
| `workspace/skills/audit_analyzer/err1.log` | Мусорный лог, оставшийся от ручных прогонов CLI |

## Черновики в корне

Отладочные пробы и скрипты, оставшиеся от прошлых заходов. Всё это не часть
проекта, но перед удалением стоит убедиться, что соседний воркер сейчас не
работает с ними.

| Файл | Кто оставил |
|---|---|
| `.tmp_call_contract_block.md` | ассистент |
| `.tmp_design_211.md` | ассистент |
| `.tmp_registry_block.md` | ассистент |
| `.tmp_rename_calls.py` | ассистент |
| `.tmp_splice_registry.py` | ассистент |
| `.tmp_strip_claims.py` | ассистент |
| `mcp-platform/.tmp_contracts.py` | ассистент |
| `mcp-platform/.tmp_inventory.py` | ассистент |
| `mcp-platform/.tmp_rename_ctx.py` | ассистент |
| `mcp-platform/.tmp_set_policy.py` | ассистент |
| `mcp-platform/.tmp_container_fix.py` | ассистент (правки контейнера применены и закоммичены, скрипт больше не нужен) |
| `mcp-platform/.tmp_journal_demo.py` | ассистент (ручной прогон писателя журнала, роль изменилась) |
| `mcp-platform/.tmp_meta_probe.py` | ассистент (проба доставки `params._meta` по проводу, проверка стала тестом) |
| `mcp-platform/.tmp_probe_live.py` | ассистент (разведка живой инфраструктуры перед переносом 3.6) |
| `mcp-platform/.tmp_probe_rebuild.py` | ассистент (живая сверка хешей при пересборке индекса) |
| `.tmp_live_mcp_probe.py` | ассистент (сквозной прогон настоящего MCP: сервер, клиент, `vector_search`) |
| `.tmp_probe_values.py` | ассистент (проверка, что генерация SQL использует настоящие значения колонок) |
| `.tmp_values_out.txt` | ассистент (сохранённый вывод того же прогона) |
| `mcp-platform/.tmp_probe_values.py` | ассистент, **перезаписан заглушкой**: в корне платформы он импортировал код агента и ломал `test_architecture_boundaries` |
| `mcp-platform/.tmp_nopg/` | ассистент (заглушка `sitecustomize`, запрещающая `psycopg2.connect`; **переименована в `.nopg_guard/`**: уборка в дереве сносит `.tmp_*`, а молча пропавший заглушка означает недостоверный «успех») |
| `.nopg_guard/sitecustomize.py` | ассистент (заглушка, запрещающая `psycopg2.connect`; живёт в корне агента — в платформе ломала `test_settings_registry` и `test_architecture_boundaries`) |
| `mcp-platform/.nopg_guard/sitecustomize.py` | ассистент, **перезаписан заглушкой** после переноса прибора в агента |
| `mcp-platform/.tmp_probe_prompt.py` | ассистент (перехват настоящего промпта модели: схема + ACTUAL VALUES) |
| `mcp-platform/.tmp_prompt_out.txt` | ассистент (вывод того же прогона) |
| `.tmp_probe_audit.py`, `.tmp_audit_out.txt` | ассистент (сквозная проверка capability audit при запрещённом PG) |
| `.tmp_probe_pipeline.py`, `.tmp_pipeline_out.txt` | ассистент (проверка замкнутости: правка в PG → векторы → снимок → поиск; строка возвращалась как была) |
| `mcp-platform/.tmp_probe_stale.py` | ассистент (создал и проверил остаток `oarb.stale_marker`; на нём доказано, что обычная загрузка снимок **не** пересоздаёт. Свою задачу выполнил — вывод перенесён в `tests/test_snapshot_reset.py`) |
| `tests/test_user_stop_signal.dump`, `tests/test_user_stop_signal_priority.dump` | ассистент |
| `-v` (корень, 0 байт) | Служебный мусор: пустой файл от неверно процитированного флага `-v`. Не принадлежит проекту ни по смыслу, ни по содержимому. Владелец не установлен — удалить как «свой» без разбора рискованно | `rm -- ./-v` |

## Чего делать не надо

Не удалять и не «чинить» файлы соседнего воркера, даже если они выглядят
недоделанными: у него идёт своя работа по фазе 8.

**Обновлено после того, как фаза 8 была завершена и закоммичена.** Раньше здесь
стояло «его незакоммиченные правки в `platform.json`, `pyproject.toml`,
`requirements.txt`, `docs/MCP-CONTRACTS.md` и `libs/enterprise_common/settings.py`
не коммитить». Это больше не так: работа лежит в истории, и трогать её заново не
нужно.

| Коммит | Что в нём |
|---|---|
| `baad75f` | контейнер без мёртвого поля, сервис замыкается операцией |
| `b8a1fdc` | слой исполнения: `execution/`, `session/`, `eventing/`, контракт вызова |

Что осталось за соседним воркером и по-прежнему не его: спека change'а
(`openspec/changes/enterprise-mcp-platform/`) и `mcp-platform/tests/
test_dependency_declaration.py` (страж объявлений зависимостей, п. 5.14). Их
не коммитить и не переписывать, пока их автор сам этого не сделает.
