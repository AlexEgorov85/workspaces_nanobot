## 1. Baseline и контрактные тесты

- [ ] 1.1 Зафиксировать baseline прогонов:
      `pytest tests/test_pg_session_manager.py tests/test_application_context.py tests/test_utils_db.py -q` —
      сохранить вывод в CI-артефакт (НЕ коммитить).
- [ ] 1.2 Создать `tests/contract/test_session_manager_api.py`:
      - `test_session_manager_default_store_is_jsonl`
        (проверить через **поведение**, не через
        `inspect.getsource`: создать `SessionManager(workspace=tmp_path)`,
        вызвать `list_sessions()`, проверить, что
        сессии пишутся в `tmp_path/sessions/...` —
        это надёжнее, чем парсинг исходников upstream);
      - `test_session_manager_has_get_or_create`
        (`inspect.signature(SessionManager.get_or_create)`);
      - `test_session_manager_has_save`
        (`inspect.signature(SessionManager.save)`);
      - `test_session_manager_has_get_cached`;
      - `test_session_manager_has_list_sessions`
        (`inspect.signature(SessionManager.list_sessions)`);
      - `test_session_manager_has_read_session_metadata`;
      - `test_session_manager_has_read_session_file`;
      - `test_session_manager_has_update_session_metadata`;
      - `test_session_manager_has_set_delete_observer`;
      - `test_session_manager_has_save_runtime_checkpoint`;
      - `test_session_manager_has_restore_sessions_to_workspace`;
      - `test_session_manager_has_fork_session_before_user_index`;
      - `test_session_manager_has_rename_model_preset`;
      - `test_session_manager_jsonl_store_persists_session`
        (round-trip через tmp_path: `get_or_create` →
        `save` → `read_session_metadata`).
      Проверить: `pytest tests/contract/test_session_manager_api.py -q` → pass.
- [ ] 1.3 Создать `tests/contract/test_usage_store_api.py`:
      - `test_usage_store_init_signature`
        (`(self, path: Path) -> None`);
      - `test_usage_store_has_record_method`
        (`inspect.signature(LLMUsageStore.record)`);
      - `test_usage_store_has_record_many_method`;
      - `test_usage_store_has_recent_calls_method`;
      - `test_usage_store_has_usage_payload_method`;
      - `test_usage_store_has_count_method`;
      - `test_usage_store_has_close_method`;
      - `test_llm_call_record_is_frozen_dataclass`
        (поля согласно upstream-контракту);
      - `test_usage_store_round_trip_records`
        (round-trip через tmp SQLite).
      Проверить: `pytest tests/contract/test_usage_store_api.py -q` → pass.
- [ ] 1.4 Создать `tests/contract/test_llm_observer_api.py`:
      - `test_llm_provider_has_set_llm_call_observer`
        (проверить наличие публичного метода через
        `inspect.signature(LLMProvider.set_llm_call_observer)`);
      - `test_llm_call_observer_type_alias`
        (проверить, что `nanobot.providers.base.LLMCallObserver`
        это `Callable[[LLMCallRecord], None]`);
      - `test_record_llm_call_is_fail_open` (вызвать
        `record_llm_call` на замоканном `LLMUsageStore`,
        бросающем исключение; проверить, что callback не
        пробрасывает исключение наружу — fail-open
        семантика upstream);
      - `test_provider_snapshot_loader_callback_signature`
        (проверить, что `build_provider_snapshot(config)`
        возвращает `ProviderSnapshot` с полем `provider`).
      Проверить: `pytest tests/contract/test_llm_observer_api.py -q` → pass.

## 2. Этап 1: SessionColdSyncService (cold-storage mirror)

- [ ] 2.1 Создать `tools/dump_legacy_session.py`:
      - CLI-утилита `python tools/dump_legacy_session.py --key <session_key>` —
        читает `PGSessionLegacyReader.read(key)` и печатает
        метаданные + сообщения в формате, похожем на upstream
        `Session`;
      - используется ТОЛЬКО для admin-операций; не импортируется
        в hot path;
      - никаких записей в PG.
      Проверить: запуск на существующей legacy-сессии
      → выводит ожидаемую структуру.
- [ ] 2.2 Создать `lib/services/session_cold_sync_service.py`:
      - класс `SessionColdSyncService` с конструктором
        `(session_manager: SessionManager, pg_dsn: str,
        schema: str, meta_table: str, messages_table: str,
        sync_interval_sec: float = 30.0, batch_size: int = 50)`;
      - **пул — через DI**: использует `utils.db.transaction()` /
        `utils.db.run()`; никаких `psycopg2.pool.SimpleConnectionPool`,
        `connect(`, `create_pool` внутри модуля (гард —
        `test_no_new_pool_created`);
      - метод `start()` — запускает `daemon=True` поток
        (`threading.Thread(target=self._worker, name=
        "session-cold-sync", daemon=True)`), по образцу
        `PgDuckDbSyncService.start()`. Sync-код НЕ
        вызывается из event loop напрямую — `threading.Lock`
        блокирует event loop;
      - метод `stop(timeout_sec=30.0)` — корректно
        останавливает поток через `_stop_event.set()` +
        `thread.join(timeout)`. Перед полным teardown —
        `state_lock.acquire(timeout)` и финальный
        `_sync_cycle()` (для D21 shutdown order);
      - метод `_sync_cycle()` — читает upstream JSONL через
        `session_manager.list_sessions()` и `read_session_metadata(...)`,
        сравнивает `updated_at` в PG,
        batch-insert через `ON CONFLICT DO UPDATE WHERE
        EXCLUDED.updated_at > existing.updated_at`
        (без `version` — используется только `updated_at`,
        DDL не меняется);
      - **advisory lock — `pg_try_advisory_xact_lock`** (D-Pool.2);
        внутри одной транзакции, без долгоживущего соединения;
      - **батчи с сортировкой по `session_key`** (D-Pool.4) —
        исключает ABBA-deadlock с DbLoggingService;
      - single-flight защита: внутренний `threading.Lock`
        вокруг `_sync_cycle()` — следующий цикл НЕ
        запускается, пока текущий не завершён;
      - leader-election через `pg_try_advisory_lock`:
        в начале `_sync_cycle()` вызывается
        `SELECT pg_try_advisory_lock(hashtext(
        'session_cold_sync'))` через `utils.db` connection;
        если lock не получен — `cycles_skipped_lock_busy`
        инкрементируется, цикл пропускается. После
        завершения sync — `pg_advisory_unlock` в `finally`;
      - конфиг `gateway.session_cold_sync.enabled`
        (default `true`) — если `false`, `_sync_loop`
        не запускается вообще (escape hatch для политики);
      - экспоненциальный backoff при ошибках PG: если
        предыдущий цикл упал, задержка до следующего =
        `min(sync_interval_sec, base * 2^consecutive_failures)`
        (cap = `sync_interval_sec`);
      - метод `get_stats() -> dict` для health-check
        (cycles_total, cycles_failed_total,
        cycles_skipped_lock_busy, cycles_skipped_pool_busy,
        consecutive_failures, last_success_ts,
        last_success_lag_seconds, pool_size, pool_available,
        pool_wait_seconds, rows_synced_total,
        messages_synced_total, upstream_session_count,
        pg_session_count);
      - ошибки PG логируются через модульную функцию
        `try_log_event(svc, log_event, *, producer, event_type)`
        (импорт из `lib.services.db_logging_service`):
        `try_log_event(self.db_logging_service, LogEvent(...), producer="SessionColdSyncService", event_type="session_cold_sync_failed")`
        согласно спеке `logging-db` (requirement «try_log_event contract»).
        Никаких прямых SQL `INSERT INTO agent_gateway_logs`.
      Проверить: `pytest tests/test_session_cold_sync_service.py -q`
      (новый) → pass.
- [ ] 2.3 Зарегистрировать `SessionColdSyncService` в
      `ApplicationContext._make_session_cold_sync_service()`;
      добавить в `start()` после старта `DbLoggingService`
      и `SessionManager`; добавить в `stop()` перед их
      остановкой.
      Проверить: `pytest tests/test_application_context.py -q` → 0 failed.
- [ ] 2.4 Smoke: `python gateway.py --profile=test` стартует,
      `curl /health` → `READY`; через 60 секунд проверить
      `agent_session_meta` / `agent_session_messages` —
      появляются mirror-строки для активных сессий.
- [ ] 2.5 В `lib/services/runtime_health.py` (`RuntimeHealth`):
      - добавить секцию `session_cold_sync` в
        `RuntimeHealth.get_stats()` со следующими полями:
        - `enabled: bool` (из конфига);
        - `last_success_ts: float | None`;
        - `last_success_lag_seconds: float | None`;
        - `consecutive_failures: int`;
        - `cycles_total: int`;
        - `cycles_failed_total: int`;
        - `upstream_session_count: int`;
        - `pg_session_count: int`.
      - добавить секцию `llm_observer`:
        - `attached: bool` (0 или 1);
        - `storage_path: str` (абсолютный путь к SQLite).
      Проверить: `curl /health` (при `gateway.py --profile=test`)
      → JSON содержит обе секции.

## 3. Этап 2: PGSessionManager как cold-storage mirror (не hot-path writer)

- [ ] 3.0 Создать `lib/session/pg_session_legacy_reader.py`:
      - класс `PGSessionLegacyReader` с методами
        `read(key) -> dict | None`,
        `list_all() -> list[dict]`,
        `count() -> int`;
      - читает напрямую из PG через `utils.db.run(...)`;
      - использует те же `_quote` и `_validate_ident`
        хелперы, что и `PGSessionManager`;
      - импортируется только в admin-утилитах
        (`tools/dump_legacy_session.py`, новый), НЕ в hot path.
      Проверить: `pytest tests/test_pg_session_legacy_reader.py -q`
      → 0 failed.
- [ ] 3.1 Обновить docstring в файлах, ссылающихся на
      `PGSessionManager` как hot-path writer:
      - `lib/session/pg_session_manager.py` — явно указать
        «cold-storage mirror поверх upstream `SessionManager`»,
        ссылка на спеку `storage/session-hybridization`;
      - `lib/services/context_compaction.py` (строки 33, 439) —
        заменить утверждение «`PGSessionManager`
        (`agent_session_messages`)» на «upstream JSONL-стор
        `SessionManager` (mirror в PG через
        `SessionColdSyncService`)»;
      - `tools/generate_comments_sql.py` (строка 132) —
        заменить «Управляется PGSessionManager» на
        «Управляется upstream `SessionManager`;
        mirror через `SessionColdSyncService`»;
      - `benchmarks/runner.py` (строка 87) — аналогичная
        правка в комментарии.
      Проверить: `grep -rn "Управляется PGSessionManager\|PGSessionManager (\`agent_session_messages\`)" lib/ tools/ benchmarks/`
      → 0 совпадений (или только в историческом контексте
      комментариев).
- [ ] 3.2 Убрать прямые `INSERT/UPDATE` в hot-path методах
      `PGSessionManager.get_or_create` / `.save` /
      `.list_sessions` / `.read_session_metadata` —
      заменить на `super().get_or_create(...)` /
      `super().save(...)` / `super().list_sessions()` /
      `super().read_session_metadata(...)`.
      Проверить: `pytest tests/test_pg_session_manager.py -q`
      → 0 failed (обновлённые тесты под mirror-only роль).
- [ ] 3.3 Обновить `tests/test_pg_session_manager.py`:
      - удалить тесты прямого SQL `INSERT` в hot path
        (тесты `TestPGSessionManagerSave::test_*`,
        `TestPGSessionManagerGetOrCreate::test_*` —
        переписать под mirror-операции или удалить);
      - добавить тесты `TestPGSessionManagerAsMirror`:
        - `test_super_get_or_create_delegates_to_upstream`;
        - `test_super_save_delegates_to_upstream`;
        - `test_hot_path_does_not_perform_direct_sql`;
        - `test_mirror_methods_have_unchanged_signature`.
      Проверить: `pytest tests/test_pg_session_manager.py -q` → 0 failed.
- [ ] 3.4 Smoke: `python cli_agent.py --profile=test` стартует,
      отправка тестового сообщения → сессия создаётся в
      upstream JSONL (`<runtime_dir>/sessions/<workspace_id>/
      <session_key>.jsonl`); mirror в PG появляется через
      30 секунд.
- [ ] 3.5 Создать `tests/test_storage_hybridization.py`
      (архитектурный гард, упомянутый в design R6):
      - `test_no_direct_sql_to_session_tables_in_hot_path` —
        ДВА прохода по всем `.py` файлам в `lib/`, исключая
        `lib/services/session_cold_sync_service.py`,
        `lib/session/pg_session_legacy_reader.py`,
        `tools/dump_legacy_session.py`, `tests/`:
        (a) AST-обход через `ast.parse`:
        - для каждого `ast.Call` с `func.id in {"execute",
          "executemany", "execute_values"}` собираем
          `ast.unparse(node)` и проверяем regex
          `\b(INSERT|UPDATE|DELETE)\s+(INTO|FROM)?\s*agent_session_(meta|messages)\b`;
        (b) String-scan: для каждого `ast.Constant` (str)
          и каждого `ast.JoinedStr` (f-string) проверяем
          тот же regex по строковому представлению.
        Любое совпадение вне whitelist-файлов
        → `pytest.fail` с указанием файла/строки.
      - `test_pg_session_manager_docstring_says_compatibility_layer` —
        проверяет, что docstring `PGSessionManager` содержит
        фразу «compatibility layer».
      - `test_no_db_logging_service_llm_usage_event` —
        `grep -rn "event_type=[\"']llm_usage[\"']"` по `lib/`,
        исключая `tests/` и `lib/services/llm_observer.py`;
        проверяет, что нет параллельной записи LLM usage
        в `DbLoggingService` (запрещено спекой `usage-store`).
      Проверить: `pytest tests/test_storage_hybridization.py -q`
      → 0 failed.
      Smoke: добавить временный файл `lib/services/_bad_example.py`
      с `cursor.execute("INSERT INTO agent_session_meta ...")`
      и убедиться, что `test_no_direct_sql_to_session_tables_in_hot_path`
      падает с понятным сообщением.

## 4. Этап 3: LLMUsageStore через observer-pipeline

- [ ] 4.1 Создать `lib/services/llm_usage_store_factory.py`:
      - класс `LLMUsageStoreFactory` с методом
        `create(usage_config: dict | None) -> LLMUsageStore | None`;
      - если `usage_config is None` или
        `usage_config.get("enabled", True) is False` →
        возвращает `None`;
      - иначе — создаёт
        `LLMUsageStore(Path(sqlite_path).expanduser())`,
        родительский каталог через `ensure_dir`;
      - возвращает store.
      Проверить: `pytest tests/test_llm_usage_store_factory.py -q`
      → pass.
- [ ] 4.2 Создать `lib/services/llm_observer.py`:
      - функция `attach_llm_observer(provider, store)` —
        вызывает `provider.set_llm_call_observer(store.record)`;
      - функция `wrap_provider_snapshot_loader(base_loader, store)` —
        оборачивает `provider_snapshot_loader` callback,
        вызывая `attach_llm_observer` на каждом загруженном
        `ProviderSnapshot`;
      - для `FallbackProvider` дополнительно вызывается
        `set_fallback_model_observer(...)` (по образцу
        `nanobot.cli.gateway_runtime._observe_provider`).
      Проверить: `pytest tests/test_llm_observer.py -q`
      → 0 failed.
- [ ] 4.3 В `lib/core/application_context.py`:
      - добавить `_make_llm_usage_store()` через фабрику;
      - в `_make_agent_loop()` (или эквивалентном методе
        создания `AgentLoop`): передать
        `provider_snapshot_loader=wrap_provider_snapshot_loader(
        base_loader=build_provider_snapshot, store=self.usage_store)`;
      - в `stop()` вызвать `store.close()`.
      Проверить: `pytest tests/test_application_context.py -q` → 0 failed.
- [ ] 4.4 Smoke: `python gateway.py --profile=test` стартует;
      отправить 3 тестовых сообщения → проверить SQLite
      `<runtime_dir>/usage/usage.db` — появляются записи
      `LLMCallRecord` через observer-pipeline.
- [ ] 4.5 Проверить контракт: `DbLoggingService.log_event`
      НЕ получает параллельную запись
      `event_type="llm_usage"` с content-free payload
      (т.к. это нарушает спеку `storage/usage-store`).
      Проверить: `grep -n 'event_type="llm_usage"' lib/`
      → только metadata-only конвертер, не дубликат
      `LLMUsageStore`.

## 5. Этап 4: Конфигурация и валидация

- [ ] 5.1 Обновить `lib/core/project_settings.py`:
      добавить `UsageStoreSettings(sqlite_path: str | None = None,
      enabled: bool = True)` под `gateway.usage_store.*`.
      Проверить: `pytest tests/test_config_keys.py -q` → pass.
- [ ] 5.2 В `project.json` добавить секцию
      `gateway.usage_store` (опционально):
      ```jsonc
      "gateway": {
        "usage_store": {
          // sqlite_path: дефолт <get_runtime_subdir("usage")>/usage.db
          // enabled: дефолт true
        }
      }
      ```
      Проверить: `python -c "from config import SETTINGS; print(SETTINGS.gateway.usage_store)"`
      → возвращает `UsageStoreSettings(enabled=True)`.
- [ ] 5.3 Добавить `REQUIRED_KEYS` запись в
      `tests/test_config_keys.py` если `gateway.usage_store`
      обязателен (сейчас — опциональный).
      Проверить: `pytest tests/test_config_keys.py -q` → pass.

## 6. Этап 5: Документация

- [ ] 6.1 Создать `docs/architecture/storage-layers.md`:
      секция «Session storage: hot JSONL + cold PG mirror»
      с диаграммой компонентов, описанием
      `SessionColdSyncService` и конфликт-резолюции.
      Проверить: `head -30 docs/architecture/storage-layers.md`
      содержит заголовок «Session storage».
- [ ] 6.2 Создать `docs/architecture/usage-tracking.md`:
      секция «LLM usage: upstream LLMUsageStore +
      DbLoggingService» с описанием раздельных concerns,
      observer-pipeline, конфигурируемости.
      Проверить: `head -30 docs/architecture/usage-tracking.md`
      содержит заголовок «LLM usage».
- [ ] 6.3 Обновить `docs/MIGRATION.md`: добавить раздел
      «Storage migration (storage-hybridization)» с описанием
      перехода и rollback.
      Проверить: `grep -n "storage-hybridization" docs/MIGRATION.md`
      → найдено.
- [ ] 6.4 Обновить `CHANGELOG.md`: добавить запись под
      `## [Unreleased]`:
      - `Changed`: переход session hot-path на upstream
        `SessionManager` (JSONL); PG остаётся как
        cold-storage mirror.
      - `Added`: `SessionColdSyncService`,
        `LLMUsageStore` через observer-pipeline,
        контрактные тесты на upstream API,
        `gateway.usage_store.*` конфиг.
      - `Removed`: нет удалённых компонентов (только
        роль `PGSessionManager` обновлена).
      Проверить: `head -50 CHANGELOG.md` содержит блок
      `## [Unreleased]`.
- [ ] 6.5 Обновить `docs/architecture/storage-layers.md`
      (или `AGENTS.md` секция «Project Layout»):
      явно указать «`PGSessionManager` — cold-storage
      mirror поверх upstream `SessionManager`».
      Проверить: `grep -n "cold-storage mirror" AGENTS.md`
      → найдено.

## 7. Валидация и smoke

- [ ] 7.1 `openspec.cmd validate storage-hybridization --strict`
      → 0 ошибок.
- [ ] 7.2 `pytest tests/contract -q` → 0 failed.
- [ ] 7.3 `pytest tests/test_session_cold_sync_service.py
      tests/test_pg_session_manager.py tests/test_application_context.py
      tests/test_utils_db.py tests/test_config_keys.py -q`
      → 0 failed.
- [ ] 7.4 `pytest tests/ -q --ignore=tests/integration`
      → 0 failed (или только `skip`, помеченные явно).
- [ ] 7.5 `python gateway.py --profile=test` стартует;
      `curl /health` → `READY`.
- [ ] 7.6 `python cli_agent.py --profile=test` стартует;
      отправка тестового сообщения → сессия создаётся в
      upstream JSONL; mirror появляется в PG через 30 сек.
- [ ] 7.7 Smoke LLM usage: `python gateway.py --profile=test`;
      отправить 5 тестовых сообщений → проверить
      `<runtime_dir>/usage/usage.db` через
      `python -c "from nanobot.llm_usage.store import LLMUsageStore; print(LLMUsageStore(...).count())"`
      → ≥ 5 записей.
- [ ] 7.8 `python benchmarks/runner.py --tags simple`
      → smoke зелёный.

## 8. Commit и PR

- [ ] 8.1 `git status` чистый; `git diff --stat` показывает
      изменения в `lib/session/pg_session_manager.py`,
      `lib/session/pg_session_legacy_reader.py` (added),
      `lib/services/session_cold_sync_service.py` (added),
      `lib/services/llm_usage_store_factory.py` (added),
      `lib/services/llm_observer.py` (added),
      `lib/core/application_context.py`,
      `lib/core/project_settings.py`,
      `tests/contract/test_session_manager_api.py` (added),
      `tests/contract/test_usage_store_api.py` (added),
      `tests/contract/test_llm_observer_api.py` (added),
      `tests/test_session_cold_sync_service.py` (added),
      `tests/test_llm_usage_store_factory.py` (added),
      `tests/test_llm_observer.py` (added),
      `tests/test_storage_hybridization.py` (added),
      `tests/test_pg_session_legacy_reader.py` (added),
      `tools/dump_legacy_session.py` (added),
      `tests/test_pg_session_manager.py`,
      `docs/architecture/storage-layers.md` (added),
      `docs/architecture/usage-tracking.md` (added),
      `docs/MIGRATION.md`, `CHANGELOG.md`, `project.json`,
      `AGENTS.md` — без непреднамеренных файлов.
      `lib/services/llm_usage_store_factory.py` (added),
      `lib/core/application_context.py`,
      `lib/core/project_settings.py`,
      `tests/contract/test_session_manager_api.py` (added),
      `tests/contract/test_usage_store_api.py` (added),
      `tests/contract/test_llm_observer_api.py` (added),
      `tests/test_session_cold_sync_service.py` (added),
      `tests/test_llm_usage_store_factory.py` (added),
      `tests/test_pg_session_manager.py`,
      `docs/architecture/storage-layers.md` (added),
      `docs/architecture/usage-tracking.md` (added),
      `docs/MIGRATION.md`, `CHANGELOG.md`, `project.json`,
      `AGENTS.md` — без непреднамеренных файлов.
- [ ] 8.2 `git commit -m "feat(storage): hybrid model with upstream SessionManager and LLMUsageStore"`
      (один коммит, Conventional Commits на русском по AGENTS.md).
- [ ] 8.3 `gh pr create --base master --title "feat(storage): hybrid storage model"
      --body "Closes OpenSpec change storage-hybridization"`;
      в PR-описании — ссылка на
      `openspec/changes/storage-hybridization/`.
