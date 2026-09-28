## 1. SQL schema: расширение `agent_worker_claims` для cache ownership

- [ ] 1.1 Создать миграцию `sql/migrations/<NN>_add_claim_type_to_agent_worker_claims.sql` (через `tools/migrate.py --apply`). Добавить колонку `claim_type VARCHAR NOT NULL DEFAULT 'task'` в таблицу `agent_worker_claims`. Создать unique constraint `(claim_type, status)` где `status='active'`. Verify: `python tools/migrate.py --status` показывает применённую миграцию; `psql -c "\d agent_worker_claims"` показывает новую колонку.

## 2. cache_ownership: новый модуль

- [ ] 2.1 Создать `lib/services/cache_ownership.py::try_claim_cache_ownership(worker_id: str, ...) -> bool`. Использует PG-транзакцию с `agent_worker_claims` (расширенной колонкой `claim_type`). Логика:
  - `SELECT * FROM agent_worker_claims WHERE claim_type='cache' AND status='active' AND last_heartbeat_at > NOW() - INTERVAL '60 seconds'`;
  - Если active claim с нашим worker_id — return `True` (already owner);
  - Если active claim с чужим worker_id — return `False` (consumer);
  - Если нет active claim — `INSERT ... ON CONFLICT DO NOTHING RETURNING worker_id`; если RETURNING даёт наш id — return `True`; иначе `False`.
  Verify: `python -c "from lib.services.cache_ownership import try_claim_cache_ownership; print('ok')"` работает.
- [ ] 2.2 Реализовать `release_cache_ownership(worker_id)` — вызывается из `PgDuckDbSyncService.stop()` или при clean shutdown. Делает `UPDATE ... SET status='released'` или `DELETE`. Verify: `python -c "from lib.services.cache_ownership import release_cache_ownership; print('ok')"` работает.
- [ ] 2.3 Реализовать heartbeat в `lib/services/cache_ownership.py::heartbeat_cache_ownership(worker_id)` — `UPDATE last_heartbeat_at = NOW()` каждые 30 сек. Verify: модуль-тест проверяет, что heartbeat вызывается с правильным интервалом.

## 3. PgDuckDbSyncService: интеграция с cache ownership

- [ ] 3.1 В `lib/services/pg_duckdb_sync_service.py::start()` — первым делом вызвать `try_claim_cache_ownership(worker_id="cli_<pid>"|"gateway_<pid>")`. Если `False` — запустить сервис в consumer-mode (открыть cache read-only, НЕ запускать background sync). Verify: unit test мокирует `try_claim_cache_ownership` и проверяет, что sync не запускается при `False`.
- [ ] 3.2 Если claim выдан — запустить background sync (текущее поведение) и heartbeat-поток, обновляющий claim каждые 30 сек. Verify: integration test: producer живёт > 30 сек, claim `last_heartbeat_at` обновляется.
- [ ] 3.3 В `PgDuckDbSyncService.stop()` — вызвать `release_cache_ownership(worker_id)` для clean shutdown. Verify: модуль-тест мокирует `release_cache_ownership` и проверяет вызов.

## 4. ApplicationContext: единая сигнатура + role

- [ ] 4.1 Добавить обязательный kwarg `role: Literal["gateway","cli","utility"] = "gateway"` в `ApplicationContext.create()` (`lib/core/application_context.py:77`). Обновить docstring. Verify: `python -c "import inspect; sig = inspect.signature(ApplicationContext.create); assert 'role' in sig.parameters"`.
- [ ] 4.2 Реализовать чтение `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` из `SETTINGS["gateway"].*` если kwargs не передан. Если передан — kwargs + `warnings.warn(..., DeprecationWarning, stacklevel=2)`. Verify: `tests/test_application_context_role.py::TestEnableFlagsFromConfig` зелёный.
- [ ] 4.3 Реализовать composition matrix (D2 в design.md):
  - `role="gateway"`: создать `PostgresChannel`, `DuckDbCacheStore`, `PgDuckDbSyncService` (через cache ownership claim), `CronService` (если `gateway.enable_cron=True`).
  - `role="cli"`: НЕ создавать `PostgresChannel`; создать `DuckDbCacheStore`, `PgDuckDbSyncService` (через cache ownership claim), `CronService` (если `gateway.enable_cron=True`).
  - `role="utility"`: НЕ создавать `PostgresChannel`, `DuckDbCacheStore`, `PgDuckDbSyncService`, `CronService`.
  Verify: `tests/test_application_context_role.py::TestRoleComposition` зелёный.
- [ ] 4.4 `resolve_publish_path(role=role)` — `role="cli"` и `role="gateway"` возвращают один и тот же `<local_path>/cache.duckdb`. Verify: `tests/test_cache_provider_role_paths.py` зелёный.

## 5. CLI/gateway call site updates

- [ ] 5.1 Обновить `cli_agent.py::_entrypoint_main` — убрать kwargs `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls`. Передавать `role="cli"` и `session_override=args.session`, `storage_override=args.storage`. Verify: `python cli_agent.py --profile=test --smoke` exit 0.
- [ ] 5.2 Обновить `gateway.py::_entrypoint_main` — убрать те же kwargs. Передавать `role="gateway"`. Verify: `python gateway.py --profile=test --smoke` exit 0.
- [ ] 5.3 Обновить standalone utilities (`tools/build_vectors.py`, `tools/check_worker_pool_integrity.py`, `tools/scan_nanobot_inventory.py`, `tools/architecture_guard.py`) — все вызовы `ApplicationContext.create(...)` передают `role=...` явно. Verify: `grep -rn "ApplicationContext.create" tools/` показывает `role=` в каждом вызове.
- [ ] 5.5 CLI `lib/cli/console_loop.py::run_repl` остаётся без изменений (уже использует `bus.publish_inbound/consume_outbound`).

## 6. Удаление Streamlit

- [ ] 6.1 Удалить `streamlit_app.py`. Verify: `Test-Path "streamlit_app.py"` → `False`.
- [ ] 6.2 Удалить `lib/services/subprocess_manager.py::spawn_streamlit`. Если других методов нет — удалить модуль целиком. Verify: `grep -r "spawn_streamlit" lib/ workspace/ tools/` → 0 результатов.
- [ ] 6.3 Удалить `_streamlit_enabled()` и блок `if _streamlit_enabled() and subprocess_manager.spawn_streamlit(...)` из `gateway.py::_run`. Удалить импорт `SubprocessManager`. Verify: `pytest tests/test_streamlit_removed.py` зелёный.
- [ ] 6.4 Удалить упоминания Streamlit из `AGENTS.md`, `README.md`, `docs/INTERNAL_API.md`, `docs/ARCHITECTURE.md`. Verify: `grep -ri "streamlit" --include="*.md" AGENTS.md README.md docs/` → 0 (или только CHANGELOG context).
- [ ] 6.5 Удалить/обновить тесты, ссылающиеся на `streamlit_app.py` или `SubprocessManager.spawn_streamlit`. Verify: `pytest tests/` зелёный без xfail.

## 7. Тесты: новые и обновлённые

- [ ] 7.1 Новый `tests/test_application_context_role.py::TestRoleComposition` — composition matrix из D2. Verify: `pytest tests/test_application_context_role.py -v` зелёный.
- [ ] 7.2 Новый `tests/test_cli_uses_in_memory_bus.py`:
  - `test_cli_does_not_create_postgres_channel` — `ApplicationContext.create(role="cli")` не создаёт `PostgresChannel`.
  - `test_cli_repl_uses_bus_publish_inbound` — мок `MessageBus` → REPL вызывает `bus.publish_inbound`.
  - `test_agent_loop_subscribed_to_bus_in_cli_role`.
  Verify: `pytest tests/test_cli_uses_in_memory_bus.py -v` зелёный.
- [ ] 7.3 Новый `tests/test_cache_ownership_claim.py`:
  - `test_first_process_becomes_producer` — `try_claim_cache_ownership` для первого процесса возвращает `True`.
  - `test_second_process_becomes_consumer` — для второго процесса возвращает `False`.
  - `test_stale_claim_takeover` — producer kill'нут, claim stale > TTL, новый процесс получает `True`.
  - `test_heartbeat_updates_claim`.
  Verify: `pytest tests/test_cache_ownership_claim.py -v` зелёный (требует PG).
- [ ] 7.4 Новый `tests/test_cache_provider_role_paths.py` — `resolve_publish_path(role="cli") == resolve_publish_path(role="gateway") == cache.duckdb`. Verify: `pytest` зелёный.
- [ ] 7.5 Новый `tests/test_agent_loop_transport_agnostic.py` — AgentLoop работает только через bus. Verify: `pytest` зелёный.
- [ ] 7.6 Новый `tests/test_streamlit_removed.py` — `grep` подтверждает отсутствие Streamlit. Verify: `pytest` зелёный.
- [ ] 7.7 Integration test: параллельный запуск `cli_agent.py --profile=test` и `gateway.py --profile=test` — оба работают, `cache.duckdb` (один файл) валиден, только один процесс — writer. Verify: integration test passes (требует PG).

## 8. Документация

- [ ] 8.1 Обновить `AGENTS.md` — секция «Project Layout»: убрать упоминание `streamlit_app.py`, `SubprocessManager`. Добавить упоминание `role="cli"|"gateway"|"utility"` для `ApplicationContext.create(...)`. Verify: `grep -i streamlit AGENTS.md` → 0 в Project Layout.
- [ ] 8.2 Обновить `README.md` — убрать Streamlit UI. Verify: `grep -i streamlit README.md` → 0.
- [ ] 8.3 Обновить `docs/ARCHITECTURE.md` — единый composition root, runtime-level cache coordination. Verify: ручной review.
- [ ] 8.4 Обновить `docs/INTERNAL_API.md` — секции «Role-based composition», «PG-level Cache Claim». Verify: ручной review.
- [ ] 8.5 Обновить `CHANGELOG.md` — `Added`/`Removed`/`Changed`. Verify: `git diff CHANGELOG.md` показывает новые секции.

## 9. Валидация и smoke

- [ ] 9.1 `openspec.cmd validate unify-cli-gateway-architecture` exit 0. Verify: `openspec.cmd validate --change unify-cli-gateway-architecture; if ($LASTEXITCODE -eq 0) { "OK" }`.
- [ ] 9.2 `pytest tests/ -q` зелёный. Verify: `python -m pytest tests/ -q 2>&1 | Select-String "passed|failed"`.
- [ ] 9.3 `python cli_agent.py --profile=test --smoke` exit 0. Verify: ручной run.
- [ ] 9.4 `python gateway.py --profile=test --smoke` exit 0. Verify: ручной run.
- [ ] 9.5 Smoke: `python cli_agent.py --profile=test --session=test_cli` → REPL стартует, ввод «привет» → ответ отрисован через typewriter → exit Ctrl+C чистый. Verify: manual run.
- [ ] 9.6 Smoke: `python gateway.py --profile=test` (foreground) → стартует, PostgresChannel работает. Verify: manual run.
- [ ] 9.7 Smoke: только CLI, без gateway — CLI должен самостоятельно поднять cache. Запустить `python cli_agent.py --profile=test`, ввести запрос к `audit_analyzer` (если данные есть в PG), получить ответ. Verify: manual run.
- [ ] 9.8 Smoke: параллельный запуск CLI и gateway — `cache.duckdb` (один файл) валиден, только один процесс — writer. Verify: `ls -la ~/.cache/nanobot/duckdb/`; `lsof ~/.cache/nanobot/duckdb/cache.duckdb` показывает writer-процесс.
- [ ] 9.9 `python tools/architecture_guard.py` exit 0. Verify: manual run.

## 10. Деактивация deprecated kwargs (следующий MINOR)

- [ ] 10.1 Создать отдельный OpenSpec change `remove-deprecated-enable-kwargs` для удаления deprecated `enable_*` kwargs из `ApplicationContext.create()` сигнатуры. Verify: новый change создан в `openspec/changes/remove-deprecated-enable-kwargs/proposal.md`.