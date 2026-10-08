> **Состояние на 2026-10-08 (сверено с деревом, не с планом).**
>
> Этот change **не начинался в заявленном виде**. Значительная часть
> перечисленного ниже уже сделана, но ДРУГИМ change'ом —
> `unify-cli-gateway-architecture` (в архиве от 2026-09-28, блок
> `[Unreleased]` в CHANGELOG, Stage C/D/B/E/F/7). Поэтому `tasks.md`
> до этой правки показывал 0/40, хотя часть инфраструктуры в коде есть,
> и обратное — часть пунктов плана не покрыта этим change'ом вовсе.
>
> Чекбоксы ниже проставлены **по факту наличия в дереве**, а не по
> первоначальному плану. Каждая строка статуса содержит проверяемый
> адрес (`файл:строка`), чтобы расхождение можно было перепроверить.
>
> **Перед архивом** обязательно сравнить тела требований дельты
> (`specs/data/cache-provider/spec.md`) с каноном: в этом репозитории уже
> было, когда канон оказывался новее дельты, и перенос откатывал бы его.
> Архивная команда — `openspec archive cache-architecture-alignment`
> (в репозитории нет `openspec.cmd`, CLI глобальный через npm).

## 1. Cache lifecycle decoupling (Decision 1, пункт 1)

- [ ] 1.1 Split `_make_sync_services()` into `_make_cache_runtime()` + `_make_sync_service()` in `lib/core/application_context.py`, verify both methods are idempotent when called twice.
      **Статус: не сделано** — `_make_cache_runtime` в `application_context.py` не встречается ни разу, `_make_sync_services` остаётся единым методом.
- [ ] 1.2 Change gate logic so `_make_cache_runtime()` runs whenever `gateway.cache.local_path` is set, regardless of `enable_audit`, verify `python cli_agent.py --smoke` exits 0 with `enable_audit=false` in project.json.
      **Статус: не сделано** — смена гейта не производилась, следствие 1.1.
- [ ] 1.3 Add test `tests/test_application_context_cache_lifecycle.py::TestCacheRuntimeIndependentOfEnableAudit` asserting cache runtime created when `enable_audit=False`, verify all 3 cases pass (OWNER + READ_WRITE, READER + READ_ONLY, READER + READ_ONLY even with audit disabled).
      **Статус: не сделано** — файл `tests/test_application_context_cache_lifecycle.py` существует (создан другим change'ом), но класса `TestCacheRuntimeIndependentOfEnableAudit` в нём нет.
- [ ] 1.4 Sync service still gated by `enable_audit=True` AND `acquired=True`, verify by smoke test that no `CacheSyncService.start()` call when audit is disabled.
      **Статус: не проверено** — гейт не менялся вместе с 1.1; отдельный smoke-прогон не выполнялся.

## 2. DuckDbCacheStore extends CacheProvider (Decision 2, пункт 2)

- [ ] 2.1 Make `DuckDbCacheStore` inherit from `CacheProvider` ABC in `lib/services/duckdb_cache_store.py:308`, verify `isinstance(DuckDbCacheStore.open(path, mode), CacheProvider)` returns True via smoke test.
      **Статус: не сделано** — `duckdb_cache_store.py:308` объявляет `class DuckDbCacheStore:` без базового класса. Наследование `CacheProvider` есть только у `PostgresDuckDbProvider` (`cache_provider_impl.py:800`).
- [ ] 2.2 Audit all abstract methods on `CacheProvider` (lib/services/cache_provider.py) and ensure `DuckDbCacheStore` implements each, verify by extending `tests/test_cache_provider_mode.py::TestIsCacheProvider`.
      **Статус: не сделано** — следствие 2.1: без наследования isinstance-проверка не может стать зелёной.
- [ ] 2.3 Remove `instance method open(self)` (line 381) leaving only `@classmethod open(cls, path, mode)`, verify no callers depend on the instance-method signature.
      **Статус: не сделано** — instance-метод `open(self)` на месте (`duckdb_cache_store.py:381`) и делегирует в `connect()` (`:393`). Существует как back-compat alias для `gateway.py` и `benchmarks/runner.py`, которые вызывают `cache_store.open()`; удаление связано с 7.1–7.2.
- [ ] 2.4 Update `tests/test_cache_provider_mode.py:81` to assert `isinstance(instance, CacheProvider)` not `DuckDbCacheStore`, verify all existing tests still pass.
      **Статус: не сделано** — `tests/test_cache_provider_mode.py:81` по-прежнему содержит `assert isinstance(instance, DuckDbCacheStore)`.

## 3. Unified query_sql enforcement (Decision 2 cont., пункты 3, 10)

- [ ] 3.1 Audit `PostgresDuckDbProvider.query_sql` vs `DuckDbCacheStore.query_sql` for divergent enforcement, document both paths in design notes.
      **Статус: частично** — результат аудита описан в `CHANGELOG.md` `[Unreleased]` (блок Stage D), но в `design.md` этого change'а не зафиксирован.
- [ ] 3.2 Decide: either (a) consolidate `PostgresDuckDbProvider` into `DuckDbCacheStore` or (b) extract shared `_assert_query_sql_allowed_locked` into `CacheProvider` base class as a private helper, verify by code review and runtime test.
      **Статус: решение не зафиксировано** — фактически сделано (b)-наоборот: `_assert_query_sql_allowed_locked` остался приватным методом `DuckDbCacheStore` (`duckdb_cache_store.py:1274`), а `PostgresDuckDbProvider` остался отдельным классом (`cache_provider_impl.py:800`). Нужно осознанно выбрать (a) или (b) и привести код в соответствие.
- [ ] 3.3 Whichever path chosen, add test verifying both providers reject DDL (`CREATE`, `ALTER`, `DROP`, `TRUNCATE`) in any mode and reject INSERT/UPDATE/DELETE in READ_ONLY mode.
      **Статус: не сделано** — поведенческие тесты DDL/DML есть только для `DuckDbCacheStore` (`test_ddl_in_read_write_raises_unsupported_sql`, `test_insert/update/delete_in_read_only_raises_read_only_assertion`); у `PostgresDuckDbProvider` покрытие ограничено проверкой существования классов исключений (`test_unsupported_sql_in_cache_provider`, `test_read_only_assertion_in_cache_provider`).

## 4. Producer fencing scope (Decision 3, пункты 4, 5)

- [ ] 4.1 Add `_with_fence(work: Callable[[], None], label: str)` helper to `PgDuckDbSyncService` in `lib/services/pg_duckdb_sync_service.py`, verify by inspection that fence is acquired and generation rechecked before work.
      **Статус: не сделано** — хелпера `_with_fence` нет; есть только `_sync_cycle_with_fence()` (`pg_duckdb_sync_service.py:316`).
- [ ] 4.2 Wrap `_do_initial_load()` in `_with_fence`, verify by adding test `test_initial_load_under_fence` that mocks ownership takeover mid-load and asserts `OwnershipLostError`.
      **Статус: не сделано** — `_do_initial_load()` (`:481`) вызывается из `:289` вне fence-обёртки; теста `test_initial_load_under_fence` в дереве нет.
- [ ] 4.3 Wrap `_fire_sync_callback()` in `_with_fence`, verify by test `test_sync_callback_under_fence` with takeover during callback.
      **Статус: частично, иначе** — `_fire_sync_callback()` вызывается из `:304`, то есть уже внутри `_sync_cycle_with_fence()` (`:316`), поэтому цель достигнута другой структурой. Теста на takeover во время callback нет.
- [ ] 4.4 Refactor existing `_poll_changes()` to use `_with_fence` (instead of inline `with acquire_write_fence()`), verify by running existing `TestSyncServiceFencingBehavior` suite.
      **Статус: функционально эквивалентно, буквально не сделано** — `_poll_changes()` обёрнут в `_sync_cycle_with_fence()` (`pg_duckdb_sync_service.py:316`), а не в `_with_fence`. Набор `TestSyncServiceFencingBehavior` существует (`tests/test_application_context_cache_lifecycle.py:264`).
- [ ] 4.5 Run full sync test suite, verify no regression in polling-fence behavior.
      **Статус: не прогонялось.**

## 5. Heartbeat lifecycle (Decision 4, пункт 6)

- [ ] 5.1 Add `start_heartbeat()`, `stop_heartbeat()`, and internal `_heartbeat_loop()` to `CacheOwnershipCoordinator` in `lib/services/cache_ownership.py`, verify threading.Event for stop signal.
      **Статус: не сделано** — есть только синхронный метод `heartbeat()` (`cache_ownership.py:245`); `start_heartbeat`, `stop_heartbeat` и `_heartbeat_loop` отсутствуют.
- [ ] 5.2 Auto-start heartbeat on `try_claim().acquired=True`, verify by integration test that heartbeat thread is alive after successful claim and dead after `release()`.
      **Статус: не сделано** — автозапуска нет. Более того: **`heartbeat()` не вызывается нигде в runtime** — единственные вызовы в `tests/test_cache_ownership_claim.py:210,230,237`. При TTL 60 секунд (`sql/migrations/V005__create_agent_cache_ownership.sql`) claim в проде никем не продлевается; это отдельный риск, не только невыполненная задача.
- [ ] 5.3 Auto-stop heartbeat on `release()` and on detection of takeover (current claim generation != ours), verify by mock test.
      **Статус: не сделано.**
- [ ] 5.4 Add new test file `tests/test_cache_ownership_lifecycle.py::TestHeartbeatWorker`, verify all 4 scenarios from spec: heartbeat updates `expires_at`, takeover allowed after TTL, READER doesn't heartbeat, graceful shutdown.
      **Статус: не сделано** — файла `tests/test_cache_ownership_lifecycle.py` в дереве нет.
- [ ] 5.5 Wire heartbeat into `ApplicationContext.stop()` for graceful shutdown, verify by integration test.
      **Статус: частично** — `ownership_coordinator.release()` уже вызывается в `ApplicationContext.stop()` (`application_context.py:687`), то есть корректное освобождение ownership есть. Останавливать heartbeat нечего: потока нет (см. 5.1). Остаётся интеграционный тест.

## 6. NFS detection cross-platform (Decision 5, пункт 9)

- [ ] 6.1 Create `lib/utils/filesystem_prober.py` with `detect_network_filesystem(path) -> bool`, verify by unit tests covering Linux/macOS/Windows via mocked `platform.system()` and `subprocess.run()`.
      **Статус: не сделано** — `lib/utils/filesystem_prober.py` не создан. Проверка реализована inline-функцией `_reject_unsupported_filesystem` (`duckdb_cache_store.py:144`) и работает **только на Linux** (чтение `/proc/mounts`); на Windows и macOS функция выходит сразу по `platform.system()`. Кроссплатформенность из задачи не реализована.
- [x] 6.2 Wire network-filesystem detection into `DuckDbCacheStore.open()` before opening storage, verify local-FS path opens and network-FS path fails with `UnsupportedFilesystemError`.
      **Статус: сделано** — `_reject_unsupported_filesystem(path)` вызывается в `open()` (`duckdb_cache_store.py:456`) до открытия storage. Имя функции отличается от планового `detect_network_filesystem`: проверка inline, см. 6.1.
- [ ] 6.3 Add test `tests/test_duckdb_cache_store.py::TestNfsCheck::test_macos_nfs_rejected` mocking `platform.system() == "Darwin"` and `subprocess.run` returning NFS mount, verify fail-fast.
      **Статус: не сделано** — `tests/test_duckdb_cache_store.py` не содержит класса `TestNfsCheck`.
- [ ] 6.4 Add test `test_windows_smb_rejected` mocking Windows + `ctypes` response `DRIVE_REMOTE`, verify fail-fast.
      **Статус: не сделано** — теста нет.

## 7. cache_store → cache_provider rename (Decision 6, пункт 8)

- [ ] 7.1 Rename all `cache_store` occurrences in `gateway.py`, verify by grep that no `cache_store` references remain.
      **Статус: не начато** — `gateway.py`: 14 вхождений `cache_store`, 0 вхождений `cache_provider`. План указывал 13 вхождений и конкретные строки — числа устарели.
- [ ] 7.2 Rename all `cache_store` occurrences in `benchmarks/runner.py`, verify by smoke test of benchmark runner.
      **Статус: не начато** — 4 вхождения.
- [ ] 7.3 Rename all `cache_store` occurrences in `lib/services/project_tool_loader.py`, verify by full test suite pass.
      **Статус: не начато** — 11 вхождений (план указывал 8).
- [ ] 7.4 Update `ApplicationContext.__init__` alias to set `cache_store` to None (no longer alias), verify by integration test that runtime consumers can use either for one release.
      **Статус: не начато** — alias жив (14 вхождений в `application_context.py`).
- [ ] 7.5 Add AST-grep test `tests/test_cache_provider_layering.py::TestNoCacheStoreAliasInRuntime` asserting no `cache_store` reference outside `application_context.py` (alias), verify by full test pass.
      **Статус: не сделано** — файла `tests/test_cache_provider_layering.py` в дереве нет.
- [ ] 7.6 Document in CHANGELOG "Deprecated" section that `cache_store` alias will be removed in next MINOR release.
      **Статус: не сделано** — `CHANGELOG.md` не упоминает ни `cache-architecture-alignment`, ни депрекейшен alias `cache_store`.

## 8. Test layering guard (Decision 7, пункт 11)

- [ ] 8.1 Create `tests/test_cache_provider_layering.py::TestCacheProviderInheritance` with AST-grep asserting no runtime import of `DuckDbCacheStore` outside composition root (`lib/services/duckdb_cache_store.py`, `lib/services/cache_provider_impl.py`, `lib/core/application_context.py`), verify by running the test.
      **Статус: не сделано** — файла нет.
- [ ] 8.2 Create test `TestRuntimeConsumersDependOnCacheProvider` asserting `AgentLoop`, Skills, Tools, `CacheSyncService`, `CacheOwnershipCoordinator` import from `lib.services.cache_provider` not `lib.services.duckdb_cache_store`, verify by AST scan.
      **Статус: не сделано** — файла нет.
- [ ] 8.3 Update existing `tests/test_cache_provider_mode.py` to use `CacheProvider` (not concrete class) in isinstance checks throughout, verify all 21 tests still pass.
      **Статус: не сделано** — `tests/test_cache_provider_mode.py:81` всё ещё проверяет `DuckDbCacheStore`; в файле 21 тест, но с concrete-классом.

## 9. Final integration and archive

- [ ] 9.1 Run full test suite (`python -m pytest tests/ -q`), verify only the known pre-existing `test_finds_workspace_hooks_without_hooks_dir_in_syspath` failure remains.
      **Статус: не прогонялось.** Для ориентира: на 2026-10-08 `pytest tests/ -q --collect-only` собирает 3812 тестов без import-ошибок.
- [ ] 9.2 Run `python tools/architecture_guard.py`, verify no new violations.
      **Статус: не прогонялось.**
- [ ] 9.3 Run `python cli_agent.py --smoke` and `python gateway.py --profile=test --smoke`, verify both exit 0.
      **Статус: не выполнялось** — требует подключения к БД.
- [ ] 9.4 Update CHANGELOG `[Unreleased]` block with the 11-point list mapping to commits 1-8.
      **Статус: не сделано** — `CHANGELOG.md` не упоминает этот change.
- [ ] 9.5 Run `openspec archive cache-architecture-alignment` to merge spec deltas into canonical `data/cache-provider/spec.md`, verify archive validates.
      **Статус: не выполнено.** Перед запуском — сверьте тела требований дельты с каноном `openspec/specs/data/cache-provider/spec.md` (см. предупреждение в шапке).
- [x] 9.6 Run `openspec validate cache-architecture-alignment` post-archive, verify clean.
      **Статус: проверено 2026-10-08** — `openspec validate cache-architecture-alignment --strict` возвращает `Change 'cache-architecture-alignment' is valid`, exit 0. Предупреждений о конфликте с каноном нет, в отличие от тех change'ов, что закрывались сегодня.