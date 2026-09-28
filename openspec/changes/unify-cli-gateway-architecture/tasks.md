## 1. SQL schema: новая таблица `agent_cache_ownership`

- [ ] 1.1 Создать миграцию `sql/migrations/<NN>_create_agent_cache_ownership.sql` через `tools/migrate.py --apply`:
  ```sql
  CREATE TABLE agent_cache_ownership (
      owner_id VARCHAR PRIMARY KEY,
      acquired_at TIMESTAMP NOT NULL DEFAULT NOW(),
      last_heartbeat_at TIMESTAMP NOT NULL DEFAULT NOW(),
      expires_at TIMESTAMP NOT NULL
  );
  CREATE INDEX idx_agent_cache_ownership_expires_at ON agent_cache_ownership(expires_at);
  ```
  Verify: `python tools/migrate.py --status` показывает применённую миграцию; `psql -c "\d agent_cache_ownership"` показывает таблицу и индекс.

## 2. CacheOwnershipCoordinator: новый модуль

- [ ] 2.1 Создать `lib/services/cache_ownership.py`:
  - `class CacheAccessMode(enum.Enum)`: `READ_WRITE`, `READ_ONLY`.
  - `class CacheOwnershipCoordinator`:
    - `__init__(worker_id: str, dsn: str, ttl_seconds: int = 60)`.
    - `def try_claim(self) -> CacheAccessMode`:
      1. `SELECT * FROM agent_cache_ownership WHERE expires_at > NOW()`;
      2. Если active claim с нашим `owner_id` → return `READ_WRITE` (already owner);
      3. Если active claim с чужим `owner_id` → return `READ_ONLY`;
      4. Если нет active claim → `INSERT INTO agent_cache_ownership (owner_id, expires_at) VALUES ($1, NOW() + INTERVAL '60 seconds')` ON CONFLICT DO NOTHING RETURNING owner_id; если RETURNING даёт наш id → return `READ_WRITE`; иначе → `READ_ONLY`.
    - `def heartbeat(self) -> None` — `UPDATE last_heartbeat_at=NOW(), expires_at=NOW() + INTERVAL '60 seconds' WHERE owner_id=$1`. Вызывается каждые 30 сек.
    - `def release(self) -> None` — `DELETE FROM agent_cache_ownership WHERE owner_id=$1`.
  Verify: `python -c "from lib.services.cache_ownership import CacheOwnershipCoordinator, CacheAccessMode; print('ok')"` работает.

## 3. DuckDbCacheStore: API с явным mode

- [ ] 3.1 `DuckDbCacheStore.open(path: str, mode: CacheAccessMode) -> DuckDbCacheStore`:
  - `mode=READ_ONLY` → `duckdb.connect(path, read_only=True)`.
  - `mode=READ_WRITE` → `duckdb.connect(path, read_only=False)`.
  - При попытке INSERT/UPDATE/DELETE в `READ_ONLY` режиме — raise `ReadOnlyAssertionError`.
  Verify: `tests/test_cache_provider_mode.py::TestReadOnlyBlocksMutations` зелёный.
- [ ] 3.2 `duckdb.connect` после `kill -9`: DuckDB ATTACH auto-recovery. Если fails — логирование ERROR + raise. Verify: integration test с corrupted `cache.duckdb`.

## 4. PgDuckDbSyncService: создаётся только при OWNER

- [ ] 4.1 `PgDuckDbSyncService.__init__()` — принимает опциональный `heartbeat_callback: Callable[[], None]` и `release_callback: Callable[[], None]`.
- [ ] 4.2 В `ApplicationContext.create()`:
  ```python
  coord = CacheOwnershipCoordinator(worker_id=f"{role}_{os.getpid()}", dsn=...)
  mode = coord.try_claim()
  ctx.cache_store = DuckDbCacheStore.open(path=resolve_publish_path(...), mode=mode)
  if mode == CacheAccessMode.READ_WRITE:
      ctx.sync_service = PgDuckDbSyncService(
          ...,
          heartbeat_callback=coord.heartbeat,
          release_callback=coord.release,
      )
      ctx.sync_service.start(initial_load=True)
  else:
      ctx.sync_service = None
  ```
  Verify: `tests/test_application_context_role.py::TestRoleComposition::test_gateway_owner_creates_sync_service` и `test_cli_reader_does_not_create_sync_service` зелёные.

## 5. ApplicationContext: typed signature + role

- [ ] 5.1 Обновить `lib/core/application_context.py`:
  - Убрать `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` из typed signature.
  - Добавить обязательный kwarg `role: Literal["gateway","cli"]`.
  - Добавить `**kwargs` для backward compat.
  - Обновить docstring.
  Verify: `python -c "import inspect; print(list(inspect.signature(ApplicationContext.create).parameters.keys()))"` показывает только `script_dir`, `workspace_dir`, `role`, `storage_override`, `session_override`, `kwargs`.
- [ ] 5.2 Реализовать обработку `**kwargs`:
  ```python
  DEPRECATED_KWARGS = ("enable_db_logging", "enable_audit", "enable_cron", "print_llm_calls")
  for key in DEPRECATED_KWARGS:
      if key in kwargs:
          warnings.warn(f"{key}=... is deprecated; configure via SETTINGS['gateway'].{key} instead", DeprecationWarning, stacklevel=2)
          # use kwargs[key]
      else:
          # read from SETTINGS
          kwargs[key] = SETTINGS["gateway"].get(...)
  ```
  Verify: `tests/test_application_context_role.py::TestDeprecatedKwargs::test_kwargs_trigger_warning` зелёный.
- [ ] 5.3 Реализовать composition matrix (D2 в design.md):
  - `role="gateway"`: создать `PostgresChannel`, `DuckDbCacheStore` (через CacheOwnershipCoordinator), `PgDuckDbSyncService` (если OWNER), `CronService` (если `gateway.enable_cron=True`).
  - `role="cli"`: НЕ создавать `PostgresChannel`, `CronService`; создать `DuckDbCacheStore` (через CacheOwnershipCoordinator), `PgDuckDbSyncService` (если OWNER).
  Verify: `tests/test_application_context_role.py::TestRoleComposition` зелёный.

## 6. CLI/gateway call site updates

- [ ] 6.1 Обновить `cli_agent.py::_parse_args` — УДАЛИТЬ `--profile` argparse argument полностью. CLI MUST hardcode `profile="test"` при `_cfg._initialize_settings(profile="test")`. Verify: `python cli_agent.py --profile=test` exit != 0 (argparse error); `python cli_agent.py --smoke` exit 0.
- [ ] 6.2 Обновить `cli_agent.py::_entrypoint_main` — убрать kwargs `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls`. Передавать `role="cli"`, `session_override=args.session`, `storage_override=args.storage`. Verify: `python cli_agent.py --smoke` exit 0.
- [ ] 6.3 Обновить `gateway.py::_entrypoint_main` — убрать те же kwargs. Передавать `role="gateway"`. `--profile` остаётся. Verify: `python gateway.py --profile=test --smoke` exit 0.
- [ ] 6.4 CLI `lib/cli/console_loop.py::run_repl` остаётся без изменений.
- [ ] 6.5 Обновить существующие тесты/скрипты, использующие `cli_agent.py --profile=...` — убрать `--profile` для CLI. Verify: `grep -rn "cli_agent.*--profile" tests/ scripts/` → 0 (или только в negative-сценариях `test_cli_no_profile`).

## 7. resolve_publish_path: единый путь

- [ ] 7.1 `lib/services/cache_provider_impl.py::resolve_publish_path(role)` — `role="cli"` и `role="gateway"` возвращают **`<local_path>/cache.duckdb`**. Verify: `tests/test_cache_provider_role_paths.py` зелёный.

## 8. Тесты: новые

- [ ] 8.1 `tests/test_application_context_role.py::TestRoleComposition` — composition matrix. Verify: `pytest -v` зелёный.
- [ ] 8.2 `tests/test_cli_uses_in_memory_bus.py`:
  - `test_cli_does_not_create_postgres_channel`
  - `test_cli_repl_uses_bus_publish_inbound`
  - `test_agent_loop_subscribed_to_bus_in_cli_role`
  Verify: `pytest -v` зелёный.
- [ ] 8.3 `tests/test_cache_ownership_claim.py`:
  - `test_first_process_becomes_owner`
  - `test_second_process_becomes_reader`
  - `test_already_owner_returns_read_write`
  - `test_stale_claim_takeover`
  - `test_heartbeat_updates_claim`
  - `test_release_deletes_claim`
  - `test_two_owners_simultaneously_impossible`
  Verify: `pytest -v` зелёный (требует PG).
- [ ] 8.4 `tests/test_cache_provider_mode.py`:
  - `test_read_only_blocks_insert`
  - `test_read_only_blocks_update`
  - `test_read_only_blocks_delete`
  - `test_read_only_allows_select`
  - `test_read_write_allows_mutations`
  Verify: `pytest -v` зелёный.
- [ ] 8.5 `tests/test_cache_provider_role_paths.py` — `role="cli" == role="gateway"`. Verify: `pytest` зелёный.
- [ ] 8.6 `tests/test_agent_loop_transport_agnostic.py` — AgentLoop работает только через bus. Verify: `pytest` зелёный.
- [ ] 8.7 `tests/test_streamlit_imports_removed.py` — `grep` подтверждает отсутствие импортов Streamlit в runtime-коде. Verify: `pytest` зелёный.
- [ ] 8.8 `tests/test_cli_no_profile.py::TestCLINoProfile`:
  - `test_cli_rejects_profile_arg`
  - `test_cli_uses_test_profile_default`
  - `test_cli_ignores_env_profile`
  Verify: `pytest -v` зелёный.
- [ ] 8.9 `tests/test_gateway_accepts_profile.py::TestGatewayAcceptsProfile`:
  - `test_gateway_accepts_prod_profile`
  - `test_gateway_accepts_test_profile`
  Verify: `pytest -v` зелёный.
- [ ] 8.10 Integration test: параллельный запуск `cli_agent.py` и `gateway.py --profile=test` — один OWNER, второй READER; `cache.duckdb` (один файл) валиден. Verify: integration test passes.

## 9. Документация

- [ ] 9.1 Обновить `AGENTS.md` — секция «Project Layout»: добавить `CacheOwnershipCoordinator`, `lib/services/cache_ownership.py`; добавить `role="cli"|"gateway"` для `ApplicationContext.create(...)`; убрать упоминание Streamlit (ссылка на отдельный change `remove-streamlit-runtime`); секция «Configuration» — добавить «CLI = fixed test profile».
- [ ] 9.2 Обновить `README.md` — убрать Streamlit UI; добавить заметку про CLI=fixed-test.
- [ ] 9.3 Обновить `docs/ARCHITECTURE.md` — секции «Composition Root», «CacheOwnershipCoordinator», «DuckDB ownership», «CLI profile».
- [ ] 9.4 Обновить `docs/INTERNAL_API.md` — секции «Role-based composition», «CacheOwnershipCoordinator», «CacheProvider API», «CLI = fixed test profile».
- [ ] 9.5 Обновить `CHANGELOG.md` — `Added` (composition unification, CacheOwnershipCoordinator, CacheProvider mode API); `Changed` (BREAKING: cron = gateway-only; CLI = fixed test profile, не принимает --profile); `Deprecated` (`enable_*` через `**kwargs`).

## 10. Валидация и smoke

- [ ] 10.1 `openspec.cmd validate unify-cli-gateway-architecture` exit 0. Verify: `openspec.cmd validate --change unify-cli-gateway-architecture; if ($LASTEXITCODE -eq 0) { "OK" }`.
- [ ] 10.2 `pytest tests/ -q` зелёный. Verify: `python -m pytest tests/ -q 2>&1 | Select-String "passed|failed"`.
- [ ] 10.3 `python cli_agent.py --smoke` exit 0. Verify: ручной run.
- [ ] 10.4 `python gateway.py --profile=test --smoke` exit 0. Verify: ручной run.
- [ ] 10.5 `python cli_agent.py --profile=test` exit != 0. Verify: ручной run (negative).
- [ ] 10.6 Smoke: `python cli_agent.py` → REPL стартует, ввод → typewriter. Verify: manual run.
- [ ] 10.7 Smoke: только CLI без gateway — CLI = OWNER, sync запускается. Verify: `audit_analyzer` возвращает свежие данные.
- [ ] 10.8 Smoke: параллельный запуск CLI и gateway — один OWNER, второй READER. Verify: `lsof cache.duckdb` показывает writer-процесс.
- [ ] 10.9 Smoke: `kill -9` producer → через 60 сек claim stale → новый процесс перехватывает. Verify: integration test с уменьшенным TTL.
- [ ] 10.10 `python tools/architecture_guard.py` exit 0. Verify: manual run.

## 11. Деактивация deprecated kwargs (следующий MINOR)

- [ ] 11.1 Создать отдельный OpenSpec change `remove-deprecated-enable-kwargs` для удаления deprecated `enable_*` kwargs из `**kwargs`-обработки в `ApplicationContext.create()`. Verify: новый change создан в `openspec/changes/remove-deprecated-enable-kwargs/proposal.md`.