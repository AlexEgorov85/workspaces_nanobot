## 1. Cache lifecycle decoupling (Decision 1, пункт 1)

- [ ] 1.1 Split `_make_sync_services()` into `_make_cache_runtime()` + `_make_sync_service()` in `lib/core/application_context.py`, verify both methods are idempotent when called twice
- [ ] 1.2 Change gate logic so `_make_cache_runtime()` runs whenever `gateway.cache.local_path` is set, regardless of `enable_audit`, verify `python cli_agent.py --smoke` exits 0 with `enable_audit=false` in project.json
- [ ] 1.3 Add test `tests/test_application_context_cache_lifecycle.py::TestCacheRuntimeIndependentOfEnableAudit` asserting cache runtime created when `enable_audit=False`, verify all 3 cases pass (OWNER + READ_WRITE, READER + READ_ONLY, READER + READ_ONLY even with audit disabled)
- [ ] 1.4 Sync service still gated by `enable_audit=True` AND `acquired=True`, verify by smoke test that no `CacheSyncService.start()` call when audit is disabled

## 2. DuckDbCacheStore extends CacheProvider (Decision 2, пункт 2)

- [ ] 2.1 Make `DuckDbCacheStore` inherit from `CacheProvider` ABC in `lib/services/duckdb_cache_store.py:308`, verify `isinstance(DuckDbCacheStore.open(path, mode), CacheProvider)` returns True via smoke test
- [ ] 2.2 Audit all abstract methods on `CacheProvider` (lib/services/cache_provider.py) and ensure `DuckDbCacheStore` implements each, verify by extending `tests/test_cache_provider_mode.py::TestIsCacheProvider`
- [ ] 2.3 Remove `instance method open(self)` (line 381) leaving only `@classmethod open(cls, path, mode)`, verify no callers depend on the instance-method signature
- [ ] 2.4 Update `tests/test_cache_provider_mode.py:81` to assert `isinstance(instance, CacheProvider)` not `DuckDbCacheStore`, verify all existing tests still pass

## 3. Unified query_sql enforcement (Decision 2 cont., пункты 3, 10)

- [ ] 3.1 Audit `PostgresDuckDbProvider.query_sql` vs `DuckDbCacheStore.query_sql` for divergent enforcement, document both paths in design notes
- [ ] 3.2 Decide: either (a) consolidate `PostgresDuckDbProvider` into `DuckDbCacheStore` or (b) extract shared `_assert_query_sql_allowed_locked` into `CacheProvider` base class as a private helper, verify by code review and runtime test
- [ ] 3.3 Whichever path chosen, add test verifying both providers reject DDL (`CREATE`, `ALTER`, `DROP`, `TRUNCATE`) in any mode and reject INSERT/UPDATE/DELETE in READ_ONLY mode

## 4. Producer fencing scope (Decision 3, пункты 4, 5)

- [ ] 4.1 Add `_with_fence(work: Callable[[], None], label: str)` helper to `PgDuckDbSyncService` in `lib/services/pg_duckdb_sync_service.py`, verify by inspection that fence is acquired and generation rechecked before work
- [ ] 4.2 Wrap `_do_initial_load()` in `_with_fence`, verify by adding test `test_initial_load_under_fence` that mocks ownership takeover mid-load and asserts `OwnershipLostError`
- [ ] 4.3 Wrap `_fire_sync_callback()` in `_with_fence`, verify by test `test_sync_callback_under_fence` with takeover during callback
- [ ] 4.4 Refactor existing `_poll_changes()` to use `_with_fence` (instead of inline `with acquire_write_fence()`), verify by running existing `TestSyncServiceFencingBehavior` suite
- [ ] 4.5 Run full sync test suite, verify no regression in polling-fence behavior

## 5. Heartbeat lifecycle (Decision 4, пункт 6)

- [ ] 5.1 Add `start_heartbeat()`, `stop_heartbeat()`, and internal `_heartbeat_loop()` to `CacheOwnershipCoordinator` in `lib/services/cache_ownership.py`, verify threading.Event for stop signal
- [ ] 5.2 Auto-start heartbeat on `try_claim().acquired=True`, verify by integration test that heartbeat thread is alive after successful claim and dead after `release()`
- [ ] 5.3 Auto-stop heartbeat on `release()` and on detection of takeover (current claim generation != ours), verify by mock test
- [ ] 5.4 Add new test file `tests/test_cache_ownership_lifecycle.py::TestHeartbeatWorker`, verify all 4 scenarios from spec: heartbeat updates `expires_at`, takeover allowed after TTL, READER doesn't heartbeat, graceful shutdown
- [ ] 5.5 Wire heartbeat into `ApplicationContext.stop()` for graceful shutdown, verify by integration test

## 6. NFS detection cross-platform (Decision 5, пункт 9)

- [ ] 6.1 Create `lib/utils/filesystem_prober.py` with `detect_network_filesystem(path) -> bool`, verify by unit tests covering Linux/macOS/Windows via mocked `platform.system()` and `subprocess.run()`
- [ ] 6.2 Wire `detect_network_filesystem` into `DuckDbCacheStore.__init__` (or `open()`) before opening storage, verify by smoke test that local-FS path opens and network-FS path fails with `UnsupportedFilesystemError`
- [ ] 6.3 Add test `tests/test_duckdb_cache_store.py::TestNfsCheck::test_macos_nfs_rejected` mocking `platform.system() == "Darwin"` and `subprocess.run` returning NFS mount, verify fail-fast
- [ ] 6.4 Add test `test_windows_smb_rejected` mocking Windows + `ctypes` response `DRIVE_REMOTE`, verify fail-fast

## 7. cache_store → cache_provider rename (Decision 6, пункт 8)

- [ ] 7.1 Rename all 13 occurrences in `gateway.py` (lines 155, 156, 160, 172, 182, 222, 224, 266, 268, 269, 272, 293, 295), verify by grep that no `cache_store` references remain
- [ ] 7.2 Rename all 4 occurrences in `benchmarks/runner.py`, verify by smoke test of benchmark runner
- [ ] 7.3 Rename all 8 occurrences in `lib/services/project_tool_loader.py` (lines 144, 156, 189, 201, 213, 267, 287, 295), verify by full test suite pass
- [ ] 7.4 Update `ApplicationContext.__init__` alias line 333 to set `cache_store` to None (no longer alias), verify by integration test that runtime consumers can use either for one release
- [ ] 7.5 Add AST-grep test `tests/test_cache_provider_layering.py::TestNoCacheStoreAliasInRuntime` asserting no `cache_store` reference outside `application_context.py` (alias), verify by full test pass
- [ ] 7.6 Document in CHANGELOG "Deprecated" section that `cache_store` alias will be removed in next MINOR release

## 8. Test layering guard (Decision 7, пункт 11)

- [ ] 8.1 Create `tests/test_cache_provider_layering.py::TestCacheProviderInheritance` with AST-grep asserting no runtime import of `DuckDbCacheStore` outside composition root (`lib/services/duckdb_cache_store.py`, `lib/services/cache_provider_impl.py`, `lib/core/application_context.py`), verify by running the test
- [ ] 8.2 Create test `TestRuntimeConsumersDependOnCacheProvider` asserting `AgentLoop`, Skills, Tools, `CacheSyncService`, `CacheOwnershipCoordinator` import from `lib.services.cache_provider` not `lib.services.duckdb_cache_store`, verify by AST scan
- [ ] 8.3 Update existing `tests/test_cache_provider_mode.py` to use `CacheProvider` (not concrete class) in isinstance checks throughout, verify all 21 tests still pass

## 9. Final integration and archive

- [ ] 9.1 Run full test suite (`python -m pytest tests/ -q`), verify only the known pre-existing `test_finds_workspace_hooks_without_hooks_dir_in_syspath` failure remains
- [ ] 9.2 Run `python tools/architecture_guard.py`, verify no new violations
- [ ] 9.3 Run `python cli_agent.py --smoke` and `python gateway.py --profile=test --smoke`, verify both exit 0
- [ ] 9.4 Update CHANGELOG `[Unreleased]` block with the 11-point list mapping to commits 1-8
- [ ] 9.5 Run `openspec.cmd archive cache-architecture-alignment` to merge spec deltas into canonical `data/cache-provider/spec.md`, verify archive validates
- [ ] 9.6 Run `openspec.cmd validate cache-architecture-alignment` post-archive, verify clean
