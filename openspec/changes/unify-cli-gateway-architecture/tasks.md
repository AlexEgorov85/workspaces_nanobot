## 1. ApplicationContext: единая сигнатура

- [ ] 1.1 Добавить kwarg `role: Literal["gateway", "cli", "utility"] = "gateway"` в `ApplicationContext.create()` (`lib/core/application_context.py:77`). Обновить docstring с явным указанием роли и её эффектов. Verify: `python -c "import inspect; sig = inspect.signature(ApplicationContext.create); assert 'role' in sig.parameters"`.
- [ ] 1.2 Реализовать чтение `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` из `SETTINGS["gateway"].*` внутри `create()` когда соответствующий kwarg не передан (если передан — использовать kwarg, log `DeprecationWarning` через `warnings.warn(..., DeprecationWarning, stacklevel=2)`). Verify: новый test `tests/test_application_context.py::TestEnableFlagsFromConfig::test_enable_audit_read_from_config_when_kwarg_omitted` зелёный.
- [ ] 1.3 Применить `role` для всех `_make_sync_services()` и `resolve_publish_path(role=role)` вызовов внутри `create()`. Verify: `python -c "from lib.core.application_context import ApplicationContext; import inspect; src = inspect.getsource(ApplicationContext.create); assert 'role=' in src"`.
- [ ] 1.4 Передать `role` в `AgentFactory.create(...)` для логирования «process role» в startup-баннере. Verify: manual run `cli_agent.py --profile=test --smoke` показывает `role=cli` в баннере.

## 2. resolve_publish_path: role-based snapshot paths

- [ ] 2.1 Добавить параметр `role: str = "gateway"` в `resolve_publish_path(workspace_path, cache_cfg, *, role)` (`lib/services/cache_provider_impl.py` или `lib/core/application_context.py` — где находится сейчас; см. `application_context.py:1071`). Verify: `python -c "import inspect; from lib.core.application_context import resolve_publish_path; sig = inspect.signature(resolve_publish_path); assert 'role' in sig.parameters"`.
- [ ] 2.2 Реализовать `role="cli"` → `<local_path>/cli.duckdb`. Для остальных ролей сохранить текущий путь `<local_path>/cache.duckdb`. Verify: новый test `tests/test_cache_provider_role_paths.py::TestResolvePublishPath::test_cli_returns_cli_duckdb` и `test_gateway_returns_cache_duckdb` зелёные.
- [ ] 2.3 Обновить `PostgresDuckDbProvider` (или эквивалентный класс) — принимать `role` через конструктор, открывать соответствующий snapshot-файл. Verify: `tests/test_cache_provider_role_paths.py::TestProviderRoleOpens::test_cli_provider_opens_cli_duckdb` зелёный.
- [ ] 2.4 Обновить Skill-ридеры (`audit_analyzer/scripts/...`, `legal_summarizer/scripts/...`, `workspace/utils/...` если есть) — передавать `role="cli"` при создании `PostgresDuckDbProvider`. Verify: ручной grep `workspace/skills/*/scripts/*.py` показывает `role="cli"` в каждом reader; `tests/test_skills_read_cli_snapshot.py` (новый) зелёный.

## 3. CLI публикует через PostgresChannel (тот же transport)

- [ ] 3.1 В `cli_agent.py::_run_vanilla` (и `_run_pasted`) — создать `PostgresChannel` через `ChannelFactory` (или прямой инстанс) с тем же конфигом `channels.postgres.*`, что использует gateway, и `worker_id="cli_<pid>"`. Verify: `python cli_agent.py --profile=test --storage=postgres` логирует `PostgresChannel started, worker_id=cli_<pid>`.
- [ ] 3.2 Переписать `lib/cli/console_loop.py::run_repl` — заменить `bus.publish_inbound(InboundMessage(channel="cli", ...))` на `cli_channel.publish_inbound_or_similar(...)` или эквивалентный INSERT в `agent_messages` через `utils.db.execute(...)`. Сохранить параметры: `channel="cli"`, `chat_id="cli:<session>"`, `sender_id="user"`, `content=user_input`, `status="pending"`. Verify: ручной smoke — ввод в REPL создаёт строку в `agent_conversation_messages` со `status='pending'`.
- [ ] 3.3 Убедиться, что REPL продолжает читать outbound из `bus.consume_outbound()` (terminal rendering требует reasoning/delta/tool events metadata). Verify: ручной smoke — REPL показывает typewriter и reasoning в dim italic.
- [ ] 3.4 Проверить, что `--storage=file` режим CLI (offline) НЕ создаёт `PostgresChannel` (только session-file-storage). Verify: ручной smoke `cli_agent.py --profile=test --storage=file` не пишет в `agent_messages`, работает с локальным session-файлом.

## 4. Удаление Streamlit

- [ ] 4.1 Удалить файл `streamlit_app.py` (полностью). Verify: `Test-Path "streamlit_app.py"` возвращает `False`.
- [ ] 4.2 Удалить `SubprocessManager.spawn_streamlit` метод. Если в `lib/services/subprocess_manager.py` нет других методов — удалить модуль целиком. Verify: `grep -r "spawn_streamlit" lib/ workspace/ tools/` возвращает 0 результатов.
- [ ] 4.3 Удалить `_streamlit_enabled()` функцию и блок `if _streamlit_enabled() and subprocess_manager.spawn_streamlit(...)` из `gateway.py::_run`. Удалить импорт `SubprocessManager`. Verify: `pytest -k test_gateway_no_streamlit` (новый) зелёный.
- [ ] 4.4 Удалить упоминания Streamlit из `AGENTS.md` (раздел Project Layout), `README.md`, `docs/INTERNAL_API.md`, `docs/ARCHITECTURE.md`. Verify: `grep -ri "streamlit" --include="*.md" AGENTS.md README.md docs/` показывает только ссылки на удалённый функционал (например, в CHANGELOG.md), не runtime-описания.
- [ ] 4.5 Удалить или обновить тесты, ссылающиеся на `streamlit_app.py` или `SubprocessManager.spawn_streamlit`. Verify: `pytest tests/` зелёный без xfail.

## 5. CLI/gateway call site updates

- [ ] 5.1 Обновить `cli_agent.py::_entrypoint_main` — убрать kwargs `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` из вызова `ApplicationContext.create(...)`. Добавить `role="cli"`. Verify: `python cli_agent.py --profile=test --smoke` exit 0.
- [ ] 5.2 Обновить `gateway.py::_entrypoint_main` — убрать те же kwargs. Добавить `role="gateway"`. Verify: `python gateway.py --profile=test --smoke` exit 0.
- [ ] 5.3 Обновить `tools/build_vectors.py`, `tools/check_worker_pool_integrity.py`, `tools/scan_nanobot_inventory.py`, `tools/architecture_guard.py` — все вызовы `ApplicationContext.create(...)` должны передавать `role="utility"` (или полагаться на default `role="gateway"`, если утилита только читает snapshot). Verify: `grep -rn "ApplicationContext.create" tools/` показывает `role="utility"` или `role="gateway"` в каждом вызове.
- [ ] 5.4 В `cli_agent.py::_run_vanilla` — убрать `enable_db_logging=True, enable_audit=False, enable_cron=True` (всё через конфиг теперь). Verify: `git diff cli_agent.py` показывает удаление kwargs.

## 6. Тесты: новые и обновлённые

- [ ] 6.1 Новый `tests/test_application_context_role.py::TestApplicationContextRole`:
  - `test_cli_and_gateway_create_identical_services` — оба `create(role=...)` создают один и тот же набор сервисов (за исключением `role`-зависимых).
  - `test_role_passed_to_resolve_publish_path` — моки `resolve_publish_path` проверяют, что `role` корректно пробрасывается.
  - `test_deprecated_kwargs_log_warning` — вызов с `enable_audit=True` логирует `DeprecationWarning`.
  Verify: `pytest tests/test_application_context_role.py -v` зелёный.
- [ ] 6.2 Новый `tests/test_cli_over_postgres_channel.py::TestCLIOverPostgresChannel`:
  - `test_cli_writes_to_agent_messages` — REPL INSERT создаёт строку со `status='pending'`.
  - `test_cli_worker_picks_up_own_message` — PostgresChannel в CLI claim'ит задачу из `agent_worker_claims`.
  - `test_cli_reads_outbound_from_bus` — после обработки агентом, REPL читает outbound из `bus.consume_outbound()`.
  Verify: `pytest tests/test_cli_over_postgres_channel.py -v` зелёный (требует `PG` для полного flow; может использовать моки).
- [ ] 6.3 Новый `tests/test_cache_provider_role_paths.py` (см. 2.2-2.3).
- [ ] 6.4 Новый `tests/test_streamlit_removed.py::TestStreamlitRemoved`:
  - `test_streamlit_app_file_does_not_exist` — `Test-Path` False.
  - `test_no_imports_of_streamlit_app` — `grep` runtime-кода не находит импортов.
  - `test_subprocess_manager_no_spawn_streamlit` — метод удалён.
  Verify: `pytest tests/test_streamlit_removed.py -v` зелёный.
- [ ] 6.5 Обновить существующие тесты, использующие `ApplicationContext.create(enable_audit=True/False)` — добавить `role="utility"` где snapshot path важен; иначе оставить deprecated kwargs для compat. Verify: `pytest tests/` без xfail и skipped.

## 7. Документация

- [ ] 7.1 Обновить `AGENTS.md` (этот файл) — секция «Project Layout»: убрать упоминание `streamlit_app.py`, `SubprocessManager`. Добавить упоминание `role="cli" | "gateway" | "utility"` для `ApplicationContext.create(...)`. Verify: `grep -i streamlit AGENTS.md` показывает 0 результатов в Project Layout секции.
- [ ] 7.2 Обновить `README.md` — убрать упоминание Streamlit UI, deployment-секции с `streamlit run`. Verify: `grep -i streamlit README.md` показывает 0 результатов (или только в CHANGELOG context).
- [ ] 7.3 Обновить `docs/ARCHITECTURE.md` — секция «Запуск CLI / gateway» переписана под единый transport (PostgresChannel) + role-based snapshot paths. Verify: ручной review раздела.
- [ ] 7.4 Обновить `docs/INTERNAL_API.md` — удалить секцию про Streamlit; добавить секцию «Producer/Consumer для audit-sync snapshot (gateway vs gateway)». Verify: `grep -i streamlit docs/INTERNAL_API.md` возвращает 0.
- [ ] 7.5 Обновить `CHANGELOG.md` — категория `Added`: producer/consumer snapshot для CLI. Категория `Removed`: Streamlit UI, `SubprocessManager.spawn_streamlit`. Категория `Changed`: BREAKING `--storage=postgres` (CLI transport через PostgresChannel); `enable_*`-kwargs помечены deprecated. Verify: `git diff CHANGELOG.md` показывает новые секции.

## 8. Валидация и smoke

- [ ] 8.1 `openspec.cmd validate unify-cli-gateway-architecture` возвращает exit 0, 0 ошибок. Verify: `openspec.cmd validate --change unify-cli-gateway-architecture; if ($LASTEXITCODE -eq 0) { "OK" }`.
- [ ] 8.2 `pytest tests/ -q` зелёный. Verify: `python -m pytest tests/ -q 2>&1 | Select-String "passed|failed"`.
- [ ] 8.3 `python cli_agent.py --profile=test --smoke` exit 0, печатает `OK_SMOKE_COMPLETE`. Verify: `python cli_agent.py --profile=test --smoke; if ($LASTEXITCODE -eq 0) { "OK" }`.
- [ ] 8.4 `python gateway.py --profile=test --smoke` exit 0, печатает `OK_SMOKE_COMPLETE`. Verify: аналогично.
- [ ] 8.5 Smoke: `python cli_agent.py --profile=test --session=test_cli` → REPL стартует, ввод «привет» → ответ отрисован через typewriter → exit Ctrl+C чистый. Verify: manual run + check no traceback в stderr.
- [ ] 8.6 Smoke: `python gateway.py --profile=test` (foreground, no Streamlit) → стартует, PostgresChannel работает, нет ошибок про `streamlit_app.py`. Verify: manual run + `grep -i streamlit logs/` показывает 0 результатов.
- [ ] 8.7 Smoke: параллельный запуск `cli_agent.py --profile=test` и `gateway.py --profile=test` — оба работают, snapshot-файлы (`cli.duckdb` и `cache.duckdb`) созданы и валидны, DuckDB flock не конфликтует. Verify: manual run + `ls -la ~/.cache/nanobot/duckdb/`.
- [ ] 8.8 Архитектурный guard: `python tools/architecture_guard.py` exit 0 (нет регрессий в инвариантах). Verify: manual run.

## 9. Деактивация deprecated kwargs (следующий MINOR)

- [ ] 9.1 Создать отдельный OpenSpec change `remove-deprecated-enable-kwargs` для удаления `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` из `ApplicationContext.create()` сигнатуры. Задача перенесена в новый change, не входит в текущий. Verify: новый change создан в `openspec/changes/remove-deprecated-enable-kwargs/proposal.md`.