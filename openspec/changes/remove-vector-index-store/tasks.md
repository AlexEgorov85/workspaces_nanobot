## 1. Код провайдера: удаление store-путей

- [ ] 1.1 Удалить `_save_index_to_store`, `_load_index_from_store`, `_load_vectors_from_db`, `rebuild_and_store_index`, `_compute_index_signature_from_config` в `lib/services/cache_provider_impl.py`. Verify: `grep -rn '_save_index_to_store\|_load_index_from_store\|_load_vectors_from_db\|rebuild_and_store_index\|_compute_index_signature_from_config' lib tools workspace` возвращает 0 матчей.
- [ ] 1.2 Удалить `VectorIndexBuildService.rebuild_and_store` в `lib/services/vector_index_service.py`. Verify: `grep -rn 'rebuild_and_store' lib tools workspace` возвращает 0 матчей.
- [ ] 1.3 Удалить поле `_vector_store_table` из `PostgresDuckDbProvider` и все его чтения (`_load_index_from_store`, `preload_indexes`, etc.). Verify: `grep -n '_vector_store_table' lib/services/cache_provider_impl.py` возвращает 0 матчей.
- [ ] 1.4 Переписать `_load_index` — единственный путь `_load_index_from_cache` (DuckDB-снапшот); удалить ветки для `_load_index_from_store` / `_load_vectors_from_db` / `_load_index_from_files`. Verify: unit-тест `test_load_index_only_uses_cache_path` (см. § 5.5) проходит.
- [ ] 1.5 Удалить `_load_index_from_files` (нет `.faiss`-файлов). Verify: `grep -n '_load_index_from_files' lib/services/cache_provider_impl.py` возвращает 0 матчей.

## 2. Payload через DuckDB SELECT

- [ ] 2.1 Изменить `build_faiss_index` в `lib/utils/duckdb_query.py` — возвращает только `(idx, meta)` где `meta = {"metric": metric}`, без дублирующих `content`/`search_text`/`row` (решение D3, вариант A). Verify: unit-тест `test_build_faiss_index_returns_only_metric_meta` — `len(meta["metadata"]) == 0`, `"content" not in meta`.
- [ ] 2.2 Изменить сигнатуру `build_raw_items` в `lib/utils/duckdb_query.py` — принимает `conn, vector_db_table` и для каждого hit'а делает DuckDB `SELECT content, search_text, row_data FROM <storage_table> WHERE source = ? AND pk_value = ? AND chunk_index = ?`. Verify: unit-тест `test_build_raw_items_queries_duckdb_per_hit` — на 3 hit'ах делает 3 SELECT'а, возвращает правильный `content`/`row` для каждого.
- [ ] 2.3 Обновить вызов `build_raw_items` в `search_vector` (`lib/services/cache_provider_impl.py:1377`) — прокинуть `self._conn, self._vector_db_table`. Verify: integration-тест `test_search_vector_hydrates_payload_from_cache` (см. § 5.6) проходит.

## 3. Preload и health-summary

- [ ] 3.1 Изменить `preload_indexes` (`lib/services/cache_provider_impl.py:1267`): источник имён — только `gateway.vector.index.indexes.*` (без `SELECT DISTINCT source FROM <signature_table>`). Verify: unit-тест `test_preload_indexes_uses_only_indexes_config` — без обращения к `<signature_table>`.
- [ ] 3.2 Изменить `compute_index_health` в `lib/services/preload_service.py:97`: `orphan` берётся из DuckDB-снапшота `<storage_table>` (DISTINCT source); `stale` — из `loaded_items[i]["signature_status"]`, без обращения к `<signature_table>`. Verify: unit-тесты `test_health_orphan_from_storage`, `test_health_stale_from_loaded_items` (см. § 5.7).
- [ ] 3.3 Сделать `_check_index_signature` inline при `_load_index` — гарантировать, что `meta["_signature_status"]` всегда заполнен для `loaded_items`. Verify: unit-тест `test_signature_status_always_set_in_loaded_items`.

## 4. Конфигурация и проекционные операторы

- [ ] 4.1 Удалить поле `signature_table` из `VectorIndexSettings` (`lib/core/project_settings.py:113`). Verify: `python -c "from lib.core.project_settings import VectorIndexSettings; VectorIndexSettings()"` не падает; `VectorIndexSettings(**{"signature_table": "public.foo"})` падает с ValidationError.
- [ ] 4.2 Перевести `tools/check_indexes.py` с чтения `public.agent_vector_index_store` на чтение DuckDB-снапшота `<storage_table>` (DISTINCT source) для `orphan`. Verify: ручной прогон `python tools/check_indexes.py` на тестовом стенде возвращает тот же `declared-but-missing` / `orphan`, что и раньше, но без обращения к `<signature_table>`.
- [ ] 4.3 Перевести `workspace/skills/audit_analyzer/scripts/cli.py:292` — функция каталога runtime-индексов читает не PG-таблицу-сигнатуру, а провайдерский `provider.preload_indexes()` / DuckDB-снапшот `<storage_table>`. Verify: smoke `python -m workspace.skills.audit_analyzer.scripts.cli --mode list-indexes` (или эквивалентный вызов) возвращает индексы без падения.

## 5. Тесты

- [ ] 5.1 Переписать `tests/integration/test_vector_build_e2e.py` под сценарий «INSERT в `<storage_table>` → `preload_indexes` → `search_vector`» (без обращения к `<signature_table>`). Verify: тест зелёный; в нём нет ни одной ссылки на `_STORE_TABLE = "public.agent_vector_index_store"` (старое поле `_STORE_TABLE` удалено).
- [ ] 5.2 Удалить контрактные тесты `_save_index_to_store`, `_load_index_from_store`, `rebuild_and_store_index`, `_compute_index_signature_from_config`. Verify: `grep -rn '_save_index_to_store\|_load_index_from_store\|rebuild_and_store_index\|_compute_index_signature_from_config' tests` возвращает 0 матчей.
- [ ] 5.3 Добавить тест `test_no_callers_of_removed_methods`: `ast`-обход `lib`, `tools`, `workspace` проверяет, что на удалённые методы провайдера (`_save_index_to_store`, `_load_index_from_store`, `rebuild_and_store_index`, `_compute_index_signature_from_config`, `VectorIndexBuildService.rebuild_and_store`) нет вызывающих. Verify: тест зелёный; добавляет защиту от случайного восстановления.
- [ ] 5.4 Добавить тест `test_no_hardcoded_table_names`: `grep -rn 'public\.agent_vector_index_store\|oarb\.audit_vectors' lib tools --include='*.py'` возвращает 0 матчей (имена таблиц должны проходить через `gateway.vector.index.*`). Verify: тест зелёный (или явно whitelist'ит файл `tools/generate_comments_sql.py`, который генерирует DDL-комментарии и **должен** знать конкретные имена).
- [ ] 5.5 Добавить unit-тест `test_load_index_only_uses_cache_path`: при `_load_index` monkeypatch'аются `_load_index_from_store`, `_load_index_from_db`, `_load_index_from_files` и проверяется, что они **не вызываются**. Verify: тест зелёный; даёт защиту от «случайного» возврата store-пути.
- [ ] 5.6 Добавить integration-тест `test_search_vector_hydrates_payload_from_cache`: 3 чанка в DuckDB-снапшоте, `search_vector` возвращает `SearchResult.content` и `SearchResult.row` корректно для каждого hit'а, при этом monkeypatch'нутый `_load_index_from_store` **не вызывается**. Verify: тест зелёный.
- [ ] 5.7 Добавить unit-тесты `test_health_orphan_from_storage`, `test_health_stale_from_loaded_items`: `compute_index_health` берёт `orphan` из DuckDB-снапшота, `stale` из `loaded_items[i]["signature_status"]`. Verify: тесты зелёные; старое поведение из `tests/test_preload_service.py::test_stale_when_signature_mismatch` (которое читало `metadata.signature` из store) переписано под новый источник.
- [ ] 5.8 Добавить тест cold-start cost: на синтетике 20к × 1024 замерить `time_ms` для `preload_indexes`. Verify: `assert time_ms < 5000`. (Базовое ограничение из спеки «Cold-start cost».)
- [ ] 5.9 Добавить тест `test_search_vector_cold_miss_raises`: первый `search_vector` для `index_name`, которого нет в `_index_cache`, возвращает `[]` и ставит понятный `_search_error` (НЕ собирает FAISS на лету). Verify: тест зелёный.

## 6. SQL

- [ ] 6.1 Создать миграцию `sql/migrations/<timestamp>_drop_signature_table.sql`: `DROP TABLE IF EXISTS "<signature_table>";` (имя резолвится из `gateway.vector.index.signature_table` оператором при деплое; по умолчанию — `public.agent_vector_index_store`). Verify: `python tools/migrate.py --apply` на тестовом стенде выполняет миграцию; `psql -c '\d <signature_table>'` возвращает `Did not find any relation`.
- [ ] 6.2 Пометить `sql/vectors/create_vector_index_store.sql` как DEPRECATED (комментарий-маркер сверху: «Этот DDL больше не применяется; индекс собирается в памяти из DuckDB-снапшота `audit_vectors`. См. `openspec/changes/remove-vector-index-store`.»). Verify: визуальная проверка файла.
- [ ] 6.3 Обновить `tests/test_config_resolver.py::test_resolve_profiles_table_is_readonly` (если затрагивается) — на текущий момент `oarb.audit_vectors` уже read-only (см. `docs/PROFILES.md:161`). Verify: тест зелёный.

## 7. Документация

- [ ] 7.1 Переписать `docs/VECTOR_INDEXES.md` — убрать упоминания `agent_vector_index_store` / `signature_table` / `default_root`. Единственный источник истины: `<storage_table>` (DuckDB-снапшот). Verify: `grep -n 'agent_vector_index_store\|signature_table\|default_root' docs/VECTOR_INDEXES.md` возвращает 0 матчей.
- [ ] 7.2 Обновить `docs/DATABASE.md` (§ vector-store) — убрать `gateway.vector.index.signature_table`; отметить `public.agent_vector_index_store` как удалённый этим релизом. Verify: `grep -n 'signature_table\|agent_vector_index_store' docs/DATABASE.md` возвращает 0 матчей.
- [ ] 7.3 Обновить `docs/ARCHITECTURE.md` § «Vector-инфраструктура» — диаграмма только с `<storage_table>` → DuckDB → `_index_cache`. Verify: визуальная проверка.
- [ ] 7.4 Обновить `AGENTS.md` § Configuration / Vector-инфраструктура — убрать упоминания `signature_table`, `default_root`; обновить описание. Verify: `grep -n 'signature_table\|default_root' AGENTS.md` возвращает 0 матчей.
- [ ] 7.5 Обновить `CHANGELOG.md` → `## [Unreleased]`: секция `Removed` (`public.agent_vector_index_store` table, `gateway.vector.index.signature_table` setting, `_save_index_to_store`/`_load_index_from_store`/`rebuild_and_store_index`/`VectorIndexBuildService.rebuild_and_store` methods), секция `Changed` (FAISS-индекс собирается в памяти из DuckDB-снапшота `<storage_table>` при `preload_indexes` и при первом `search_vector` для индекса вне кэша выбрасывается ошибка). Verify: файл отформатирован по Keep a Changelog, секции `Added`/`Changed`/`Deprecated`/`Removed`/`Fixed`/`Security` присутствуют.
- [ ] 7.6 Обновить `docs/skill-tool-inventory.md` — пометить `vector_index_service.py::VectorIndexBuildService.rebuild_and_store` и `cache_provider_impl.py::_save_index_to_store` как удалённые. Verify: визуальная проверка.
- [ ] 7.7 Обновить `docs/README.md` (навигация) — если добавляется новая секция в `VECTOR_INDEXES.md` / `DATABASE.md`, обновить индекс. Verify: визуальная проверка.

## 8. Конфигурация проекта

- [ ] 8.1 Убрать `gateway.vector.index.signature_table` из `project.json` (текущее значение `oarb.audit_vectors` — это `storage_table`, а не signature). Verify: `jq '.gateway.vector.index.signature_table' project.json` возвращает `null`.
- [ ] 8.2 Проверить, что в `project.json` нет ссылок на удалённое API. Verify: `python -c "from config import SETTINGS; print(SETTINGS.gateway.vector.index.signature_table)"` падает с понятной ошибкой `AttributeError` или `ValidationError` (новое поведение).

## 9. Финальная приёмка

- [ ] 9.1 `openspec.cmd validate remove-vector-index-store` — passed. Verify: вывод содержит `"passed": 1, "failed": 0`.
- [ ] 9.2 `pytest tests/` — все тесты зелёные. Verify: `pytest -q` возвращает 0 failed. (Текущий ориентир — **1480 passed, 22 skipped** + новые тесты из § 5.)
- [ ] 9.3 Smoke-прогон `python tools/build_vectors.py --check` (или эквивалентный read-only) — не падает на реальном PG; не обращается к `<signature_table>`. Verify: ручной прогон.
- [ ] 9.4 Smoke-прогон `python gateway.py` в test-профиле — `READY` сигнализируется; в stderr видна русская сводка `объявлено/загружено/не найдено/сироты/устаревшие`. Verify: визуальная проверка терминала и runtime-health.
