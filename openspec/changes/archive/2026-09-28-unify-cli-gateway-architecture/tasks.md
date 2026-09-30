## 1. SQL schema: новая таблица `agent_cache_ownership`

- [ ] 1.1 Создать миграцию `sql/migrations/<NN>_create_agent_cache_ownership.sql` через `tools/migrate.py --apply`:
  ```sql
  CREATE TABLE agent_cache_ownership (
      resource_key VARCHAR PRIMARY KEY,           -- фиксированное значение: 'local_cache'
      owner_id VARCHAR NOT NULL,                    -- worker_id текущего владельца
      generation BIGINT NOT NULL DEFAULT 1,         -- fencing token (монотонно растёт при takeover)
      acquired_at TIMESTAMP NOT NULL DEFAULT NOW(),
      last_heartbeat_at TIMESTAMP NOT NULL DEFAULT NOW(),
      expires_at TIMESTAMP NOT NULL
  );
  ```
  Verify: `python tools/migrate.py --status` показывает применённую миграцию; `psql -c "\d agent_cache_ownership"` показывает таблицу.

## 2. CacheOwnershipCoordinator: новый модуль с generation/fencing

- [ ] 2.1 Создать `lib/services/cache_ownership.py`:
  - `class CacheAccessMode(enum.Enum)`: `READ_WRITE`, `READ_ONLY`.
  - `class ClaimResult`: `acquired: bool`, `generation: int`, `owner_id: str`, `current_owner_id: str | None`, `current_generation: int | None`.
  - `class CacheOwnershipCoordinator`:
    - `__init__(worker_id: str, dsn: str, resource_key: str = "local_cache", ttl_seconds: int = 60)`.
    - `def try_claim(self) -> ClaimResult` — atomic через PG-транзакцию. Implementation MAY использовать `INSERT ... ON CONFLICT (resource_key) DO UPDATE SET generation = COALESCE(agent_cache_ownership.generation, 0) + 1, ... WHERE agent_cache_ownership.expires_at < NOW() RETURNING ...`. Возвращает:
      - `ClaimResult(acquired=True, generation=N, owner_id=self.worker_id)` при успешном claim.
      - `ClaimResult(acquired=False, current_owner_id=X, current_generation=N)` при неудачном claim.
    - `def heartbeat(self) -> bool`:
      ```sql
      UPDATE agent_cache_ownership
      SET last_heartbeat_at = NOW(), expires_at = NOW() + INTERVAL '60 seconds'
      WHERE resource_key = $1 AND owner_id = $2 AND generation = $3;
      ```
      Returns True если успешно, False если WHERE не match.
    - `def release(self) -> bool`:
      ```sql
      DELETE FROM agent_cache_ownership
      WHERE resource_key = $1 AND owner_id = $2 AND generation = $3
      RETURNING resource_key;
      ```
      Returns True если успешно, False при несовпадении (caller логирует WARNING).
    - `def acquire_write_fence(self) -> ContextManager[None]`:
      Внутри context manager:
      ```sql
      BEGIN PG;
        SELECT pg_advisory_xact_lock(hashtext($resource_key));
        -- (caller validates generation + executes DuckDB write)
      COMMIT;
      ```
      Mutual exclusion с ownership takeover branch в `try_claim()`.
  Verify: `python -c "from lib.services.cache_ownership import CacheOwnershipCoordinator, CacheAccessMode, ClaimResult; print('ok')"` работает.

## 3. DuckDbCacheStore: API с явным mode + реальный DuckDB read-only connection

- [ ] 3.1 `DuckDbCacheStore.open(path: str, mode: CacheAccessMode) -> DuckDbCacheStore`:
  - `mode=READ_ONLY` → `duckdb.connect(path, read_only=True)` (первый уровень защиты — DuckDB connection НЕ позволит мутации).
  - `mode=READ_WRITE` → `duckdb.connect(path, read_only=False)`.
  Verify: `tests/test_cache_provider_mode.py::TestReadOnlyConnectionBlocksMutations` зелёный (проверяет реальный уровень DuckDB, не только assertion).
- [ ] 3.2 `CacheProvider` MUST иметь assertion guard (второй уровень защиты): при попытке INSERT/UPDATE/DELETE через `CacheProvider.query_sql(...)` с `mode=READ_ONLY` raise `ReadOnlyAssertionError`. Verify: `tests/test_cache_provider_mode.py::TestReadOnlyAssertionGuard` зелёный.
- [ ] 3.3 `CacheProvider` MUST reject путь на NFS ДО открытия DuckDB. Verify: `tests/test_cache_provider_mode.py::TestRejectsNFSPath` зелёный.
- [ ] 3.4 `CacheProvider` (ABC) MUST иметь classmethod/staticmethod `open(path: str, mode: CacheAccessMode) -> CacheProvider`. `DuckDbCacheStore.open(...)` — concrete factory, возвращающий `CacheProvider` instance. Verify: `tests/test_cache_provider_layering.py` зелёный.

## 4. CacheSyncService: создаётся только при OWNER + generation fencing

- [ ] 4.1 `CacheSyncService.__init__()` принимает: `cache_provider: CacheProvider`, `my_generation: int`, `heartbeat_callback`, `release_callback`, `fence_callback` (context manager).
- [ ] 4.2 Sync-поток MUST использовать `coord.acquire_write_fence()` (PG advisory lock) для mutual exclusion с takeover:
  ```python
  def _write_with_fence(self, sql, params):
      with self.coord.acquire_write_fence() as lock:
          # внутри lock: проверка generation + DuckDB write
          current_gen = tx.execute(
              "SELECT generation FROM agent_cache_ownership WHERE resource_key = 'local_cache' AND expires_at > NOW()"
          ).scalar()
          if current_gen != self.my_generation:
              raise OwnershipLostError(f"generation mismatch: have {self.my_generation}, db has {current_gen}")
          self.cache_provider.execute_sql(sql, params)
  ```
  Verify: integration test `test_fencing_prevents_write_after_takeover` (требует PG).
- [ ] 4.3 В `ApplicationContext.create()`:
  ```python
  coord = CacheOwnershipCoordinator(
      worker_id=f"{role}_{os.getpid()}",
      dsn=...,
      resource_key="local_cache",
      ttl_seconds=60,
  )
  result = coord.try_claim()  # atomic, returns CacheOwnershipResult
  ctx.cache_store = DuckDbCacheStore.open(path=resolve_publish_path(...), mode=result.mode)
  if result.mode == CacheAccessMode.READ_WRITE:
      ctx.sync_service = CacheSyncService(
          ...,
          my_generation=result.my_generation,
          heartbeat_callback=coord.heartbeat,
          release_callback=lambda: coord.release(),
          fence_callback=coord.is_still_owner,
      )
      ctx.sync_service.start(initial_load=True)
  else:
      ctx.sync_service = None
  ```
  Verify: `tests/test_application_context_role.py::TestRoleComposition::test_gateway_owner_creates_sync_service_with_generation` и `test_cli_reader_does_not_create_sync_service` зелёные.

## 5. ApplicationContext: typed signature без profile + role (staged implementation)

- [ ] **Stage A**: Убрать из typed signature `profile`, `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls`. Добавить `role: Literal["gateway","cli"]`. Добавить `**kwargs` для backward compat. Verify: `python -c "import inspect; params = list(inspect.signature(ApplicationContext.create).parameters.keys()); assert 'profile' not in params; assert 'enable_db_logging' not in params; assert 'role' in params"`.
- [ ] **Stage B**: Реализовать composition matrix в `ApplicationContext.create()`:
  - `role="gateway"`: создать `PostgresChannel`, `CronService` (если `gateway.enable_cron=True`).
  - `role="cli"`: НЕ создавать `PostgresChannel`, `CronService`.
  Verify: `tests/test_application_context_role.py::TestRoleComposition` зелёный.
- [ ] **Stage C** (в этом change): `CacheOwnershipCoordinator` + `agent_cache_ownership` + atomic claim + generation/fencing (см. задачи 1-4).
- [ ] **Stage D** (в этом change): `CacheProvider.open(mode=...)` + `DuckDbCacheStore.open(mode=...)` с реальным DuckDB read-only connection (см. задачу 3).
- [ ] **Stage E** (в этом change): `CacheSyncService` integration с generation fencing (см. задачу 4).
- [ ] **Stage F** (в этом change): Удалить `--profile` из `cli_agent.py` (см. задачу 6.1).
- [ ] **Stage G** (отдельный change): удаление deprecated `enable_*` kwargs из `**kwargs`-обработки.

## 6. CLI/gateway call site updates

- [ ] 6.1 Обновить `cli_agent.py::_parse_args` — УДАЛИТЬ `--profile` argparse argument. CLI MUST hardcode `profile="test"`. Verify: `python cli_agent.py --profile=test` exit != 0.
- [ ] 6.2 Обновить `cli_agent.py::_entrypoint_main` — убрать `enable_*` kwargs. Передавать `role="cli"`. НЕ передавать `profile`. Verify: `python cli_agent.py --smoke` exit 0.
- [ ] 6.3 Обновить `gateway.py::_entrypoint_main` — убрать `enable_*` kwargs. Передавать `role="gateway"`. НЕ передавать `profile`. `--profile` остаётся. Verify: `python gateway.py --profile=test --smoke` exit 0.
- [ ] 6.4 CLI `lib/cli/console_loop.py::run_repl` остаётся без изменений.
- [ ] 6.5 Обновить существующие тесты/скрипты, использующие `cli_agent.py --profile=...` — убрать `--profile`.

## 7. ConfigurationResolver: gateway.cache.local_path MUST быть shared

- [ ] 7.1 ConfigurationResolver MUST reject per-profile override `gateway.cache.local_path`. Если `profiles/test.jsonc` или `profiles/prod.jsonc` задают `gateway.cache.local_path` — ConfigurationError на старте.
- [ ] 7.2 `resolve_publish_path(role)` — `role="cli"` и `role="gateway"` возвращают один и тот же `<local_path>/cache.duckdb`. Verify: `tests/test_cache_provider_role_paths.py` зелёный.
- [ ] 7.3 Verify: `tests/test_shared_cache_path_across_profiles.py` — при `profile="test"` и `profile="prod"` оба резолвят один и тот же физический путь.

## 8. Тесты: новые и обновлённые

- [ ] 8.1 `tests/test_application_context_role.py::TestRoleComposition` — composition matrix.
- [ ] 8.2 `tests/test_application_context_role.py::TestNoProfileInSignature` — `inspect.signature` НЕ содержит `profile`.
- [ ] 8.3 `tests/test_application_context_role.py::TestDuckDBLifecycleOrdering` — verify sequence: `resolve_publish_path` → `try_claim` → `DuckDbCacheStore.open(mode=...)` → (OWNER) `CacheSyncService.start()`.
- [ ] 8.4 `tests/test_cli_uses_in_memory_bus.py` — REPL использует bus, `PostgresChannel` НЕ создаётся.
- [ ] 8.5 `tests/test_cache_ownership_claim.py`:
  - `test_first_process_becomes_owner` → `ClaimResult(acquired=True, generation=1)`.
  - `test_second_process_becomes_reader` → `ClaimResult(acquired=False, current_owner_id=...)`.
  - `test_concurrent_claim_exactly_one_owner` — два процесса параллельно → ровно один OWNER.
  - `test_stale_claim_takeover` — claim expired → следующий процесс → generation incremented.
  - `test_heartbeat_updates_claim`.
  - `test_heartbeat_fails_after_generation_change` — после takeover heartbeat returns False → sync останавливается.
  - `test_release_deletes_claim_for_matching_generation` — release с правильным generation → success.
  - `test_release_is_noop_for_mismatched_generation` — release с чужим generation → False + WARNING.
  - `test_old_owner_fencing_with_advisory_lock` — B захватывает lock + takeover; A blocked внутри lock; A получает `OwnershipLostError` при попытке write.
  - `test_ownership_key_fixed` — только один ряд в `agent_cache_ownership`.
  Verify: `pytest -v` зелёный (требует PG).
- [ ] 8.6 `tests/test_cache_provider_mode.py`:
  - `test_read_only_blocks_insert_via_duckdb_connection` — реальный DuckDB `read_only=True` connection.
  - `test_read_only_blocks_insert_via_assertion` — assertion guard.
  - `test_read_only_blocks_update` / `test_read_only_blocks_delete` / `test_read_only_allows_select`.
  - `test_read_write_allows_mutations`.
  - `test_query_sql_accepts_select_and_dml_in_read_write` — SELECT/INSERT/UPDATE/DELETE работают в READ_WRITE.
  - `test_rejects_nfs_path`.
  Verify: `pytest -v` зелёный.
- [ ] 8.7 `tests/test_cache_provider_role_paths.py` — `role="cli" == role="gateway" == cache.duckdb`.
- [ ] 8.8 `tests/test_shared_cache_path_across_profiles.py` — `profile="test"` и `profile="prod"` оба резолвят один физический путь.
- [ ] 8.9 `tests/test_agent_loop_transport_agnostic.py` — AgentLoop работает только через bus.
- [ ] 8.10 `tests/test_streamlit_imports_removed.py` — AST-based проверка (не grep) отсутствия импортов Streamlit.
- [ ] 8.11 `tests/test_cli_no_profile.py` — CLI rejects `--profile`, hardcodes test, ignores env.
- [ ] 8.12 `tests/test_gateway_accepts_profile.py` — gateway принимает `--profile=prod`/`--profile=test`.
- [ ] 8.13 Integration test: параллельный запуск `cli_agent.py` и `gateway.py --profile=test` — один OWNER (generation=N), второй READER; `cache.duckdb` (один файл) валиден; fencing предотвращает write после takeover.
- [ ] 8.14 Новый `tests/test_cache_provider_layering.py`:
  - `test_application_context_uses_cache_provider_abc_not_duckdb` — `ctx.cache_provider` типизирован как `CacheProvider`, не `DuckDbCacheStore`.
  - `test_local_cache_store_open_returns_cache_provider_instance` — factory возвращает ABC instance.
- [ ] 8.15 Новый `tests/test_application_context_role.py::TestCacheLifecycleIndependentOfAudit`:
  - При `gateway.enable_audit=False` и `gateway.cache` настроен → `CacheProvider` MUST быть создан.
  - При `gateway.enable_audit=False` → `CacheSyncService` MUST NOT быть создан.
  - Skills (`audit_analyzer`) MUST иметь доступ к cache даже при `enable_audit=False`.

## 9. Документация

- [ ] 9.1 `AGENTS.md` — добавить `CacheOwnershipCoordinator`, `lib/services/cache_ownership.py`; `role="cli"|"gateway"` для `ApplicationContext.create(...)`; убрать Streamlit (ссылка на `remove-streamlit-runtime`); секция «Configuration» — CLI = fixed test profile.
- [ ] 9.2 `README.md` (корневой) — обновить описание архитектуры (см. change's README.md).
- [ ] 9.3 `docs/ARCHITECTURE.md` — секции «Composition Root», «CacheOwnershipCoordinator», «DuckDB ownership», «CLI profile», «Generation/fencing token».
- [ ] 9.4 `docs/INTERNAL_API.md` — секции «Role-based composition», «CacheOwnershipCoordinator», «CacheProvider API», «CLI = fixed test profile».
- [ ] 9.5 `CHANGELOG.md` — `Added` (composition unification, CacheOwnershipCoordinator, generation/fencing token, CacheProvider mode API); `Changed` (BREAKING: cron = gateway-only; CLI = fixed test profile; `gateway.cache.local_path` MUST быть shared); `Deprecated` (`enable_*` через `**kwargs`).

## 10. Валидация и smoke

- [ ] 10.1 `openspec.cmd validate unify-cli-gateway-architecture` exit 0.
- [ ] 10.2 `pytest tests/ -q` зелёный.
- [ ] 10.3 `python cli_agent.py --smoke` exit 0.
- [ ] 10.4 `python gateway.py --profile=test --smoke` exit 0.
- [ ] 10.5 `python cli_agent.py --profile=test` exit != 0.
- [ ] 10.6 Smoke: `python cli_agent.py` → REPL работает, ввод → typewriter.
- [ ] 10.7 Smoke: только CLI без gateway — CLI = OWNER (generation=1), sync запускается.
- [ ] 10.8 Smoke: параллельный запуск CLI и gateway — один OWNER, второй READER; `lsof cache.duckdb` показывает writer-процесс.
- [ ] 10.9 Smoke: `kill -9` producer → через 60 сек claim stale → новый owner (generation incremented) → старый producer fencing test.
- [ ] 10.10 `python tools/architecture_guard.py` exit 0.

## 11. Деактивация deprecated kwargs (следующий MINOR)

- [ ] 11.1 Создать отдельный OpenSpec change `remove-deprecated-enable-kwargs` для удаления deprecated `enable_*` kwargs из `**kwargs`-обработки в `ApplicationContext.create()`.

