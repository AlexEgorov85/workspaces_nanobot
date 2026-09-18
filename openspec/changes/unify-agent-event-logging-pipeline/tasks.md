# Tasks — unify-agent-event-logging-pipeline

> **Цель:** ликвидировать второй runtime-механизм
> записи structured agent events в `agent_gateway_logs`
> (`workspace/utils/event_log.py` с `record_event` /
> `record_sync_event` / `emit_sync_event`) и
> зафиксировать единственный путь —
> `DbLoggingService`.

## 1. Phase 0 — инвентаризация writers (design D6.4 baseline)

- [ ] 1.1 Создать отчёт `docs/architecture/EVENT_LOGGING_INVENTORY.md` со списком **всех** мест в runtime-коде, где происходит запись в `agent_gateway_logs` или в `agent_question_runs`. Покрыть минимум: `lib/services/db_logging_service.py` (канон, два пути: `LogEvent` queue и `_QuestionRunRecord` upsert), `workspace/utils/event_log.py` (весь файл: `record_event` / `record_sync_event` / `emit_sync_event`), `lib/services/context_compaction.py` (`_record_event_log`), `lib/services/pg_duckdb_sync_service.py` (`_log_sync_event`), `lib/services/duckdb_cache_store.py` (`_emit_sync_event`), `lib/services/preload_service.py` (`_emit_health_event`), `lib/core/application_context.py` (`_record_sync_skipped`). Каждая строка: writer / текущий путь / целевой путь / risk (fallback yes/no). **Верификация:** `grep -rn 'agent_gateway_logs\|agent_question_runs' lib/ workspace/ tools/ | sort -u > /tmp/baseline.txt` совпадает со строками отчёта; ни одного необъяснённого вхождения в baseline-списке.

- [ ] 1.2 Phase 0 устанавливает **baseline inventory** writers
  `agent_gateway_logs` и `agent_question_runs`. Если
  во время реализации обнаруживается новый writer,
  разработчик MUST добавить его в inventory
  (`docs/architecture/EVENT_LOGGING_INVENTORY.md`)
  **до** продолжения работы; baseline обновляется
  в рамках change (это **не** нарушение плана).
  Цель Phase 0 — зафиксировать **известные** writers
  на момент старта; расширение inventory во время
  реализации ожидаемо и приветствуется.

## 2. Phase 1 — explicit DI без скрытых каналов (design D1, D7, D8)

- [ ] 2.1 В `RuntimePatcher.apply_all` (`lib/services/runtime_patcher.py:440-493`) **расширить сигнатуру** `patch_compaction_tracking` и `patch_compact_command` параметром `db_logging_service` (kwarg). Текущий вызов `self.patch_compaction_tracking(agent, settings)` заменить на `self.patch_compaction_tracking(agent, settings, db_logging_service=db_logging_service)`. `db_logging_service` уже передаётся в `apply_all` через `db_logging_service=ctx.db_logging_service` (см. `application_context.py:325`). **Верификация:** `git grep -n "patch_compaction_tracking\|patch_compact_command" lib/services/runtime_patcher.py` — каждый вызов имеет `db_logging_service=`.

- [ ] 2.2 В `runtime_patcher.patch_compaction_tracking` (`lib/services/runtime_patcher.py:1848-1888`):
  - добавить kwarg `db_logging_service` (keyword-only **обязательный**, без дефолта — патч вызывается только из `apply_all`, который всегда передаёт явно через `db_logging_service=ctx.db_logging_service`);
  - **удалить** ранний return `if not svc.notify_in_history: return False, "..."` (строки 1882-1883) — patch остаётся активным при `notify_in_history=false`; решение о UI-стороне принимается внутри `_notify`;
  - передавать `db_logging_service` в `ContextCompactionService(agent, settings=settings, db_logging_service=db_logging_service)`.
  **Верификация:** новый тест `tests/test_runtime_patcher.py::TestPatchCompactionTracking::test_patch_active_when_notify_in_history_false` — pass (patch не отключается, structured event пишется при auto-compaction); `test_patch_disabled_when_enabled_false` — pass (старое поведение сохраняется); `git grep -n "patch_compaction_tracking" lib/services/runtime_patcher.py` — сигнатура всегда `*, db_logging_service` без дефолта.

- [ ] 2.3 В `ContextCompactionService.__init__` (`lib/services/context_compaction.py:65-69`):
  - сигнатура: `def __init__(self, agent, settings=None, *, db_logging_service)` — `db_logging_service` теперь **keyword-only обязательный** kwarg (без `getattr(agent, ...)` fallback);
  - тело: `self._db_logging_service = db_logging_service`;
  - обновить 5 production call-site'ов, **каждый передаёт явно через kwarg без промежуточных полей на agent**:
    - `lib/services/runtime_patcher.py:1879` (теперь через 2.2) → `db_logging_service=db_logging_service`;
    - `lib/commands/compact_command.py:48` → `db_logging_service=db_logging_service` через `functools.partial` (как уже сделано с `settings` в `RuntimePatcher.patch_compact_command`); если `RuntimePatcher.patch_compact_command` теперь принимает `db_logging_service`, он пробрасывает через partial;
    - `lib/cli/console_loop.py:149` (`_run_cli_compact`) → `db_logging_service` параметр `run_repl(...)` (composition root поднимает dependency до entrypoint'а);
    - `workspace/tools/compact_context.py:128` (tool `create`) → `db_logging_service=getattr(ctx, "_db_logging_service", None)` (`ToolContext` симметрично `ctx._agent_ref` / `ctx._settings_ref`); `patch_project_tools` выставляет `ctx._db_logging_service = db_logging_service`;
  - тесты `tests/test_context_compaction.py:130-141` и далее — все вызовы `ContextCompactionService(agent, settings=...)` явно получают `db_logging_service=mock_or_None`.
  **Верификация:** `git grep -nE 'ContextCompactionService\([^)]*\)' lib/ workspace/` — каждый вызов содержит `db_logging_service=`; `pytest tests/test_context_compaction.py` — все тесты зелёные после явной передачи; `git grep -nE 'agent\.db_logging_service|agent\._db_logging_service' lib/ workspace/` — пусто.

- [ ] 2.4 В `lib/core/application_context.py` НЕ добавлять `_wire_agent_db_logging` и НЕ выставлять `agent.db_logging_service`. DI поднимается до entrypoint'ов через `functools.partial` (`RuntimePatcher.patch_compact_command`) и параметр `run_repl(...)` (`lib/cli/console_loop.py`) — никаких промежуточных полей на `agent`. **Верификация:** `git grep -nE 'agent\.db_logging_service|agent\._db_logging_service' lib/ workspace/` — пусто; `git grep -nE 'self\.agent\.db_logging_service' lib/` — пусто; `git grep -nE 'getattr\(agent, .db_logging_service' lib/ workspace/` — пусто (нет fallback-lookup'ов).

- [ ] 2.5 В `workspace/tools/compact_context.py:128` (tool `create`) добавить **новое DI-поле** `ctx._db_logging_service` в `patch_project_tools` (`runtime_patcher.py:1617-1700`) — симметрично существующим `ctx._agent_ref` / `ctx._settings_ref`. Источник: `db_logging_service=ctx.db_logging_service` из ApplicationContext. **Верификация:** `pytest tests/test_compact_context.py::TestToolCreate::test_creates_service_with_db_logging` — pass.

## 3. Phase 2 — `ContextCompactionService._notify` decoupling (design D2, D3)

- [ ] 3.1 В `lib/services/db_logging_service.py` добавить **новую** функцию-хелпер `try_log_event(svc, log_event, *, producer: str, event_type: str) -> bool` (D3) рядом с `LogEvent`. Контракт: `svc is None` или `not svc.is_running()` → `logger.warning(...)` на уровне **WARNING** (НЕ DEBUG, НЕ INFO) и возврат `False`; иначе `svc.log_event(log_event)` и возврат `bool(result)`. Уровень WARNING — единый для всех producer'ов (никаких per-producer уровней). **Верификация:** новый тест `tests/test_db_logging_service.py::TestTryLogEvent::test_noop_when_service_none` (WARNING + False), `test_noop_when_service_not_running` (WARNING + False), `test_success_when_running` (False-warning + True). Уровень WARNING проверяется через `caplog`-инспекцию.

- [ ] 3.2 В `ContextCompactionService._notify` (`lib/services/context_compaction.py:299-314`) разделить три concerns: (a) **structured event** — `await self._record_event_log(...)` ВСЕГДА (без условия `notify_in_history`); (b) UI-history-notice — `if self.notify_in_history: await self._write_history_notice(...)`; (c) terminal output — `if self.print_to_terminal: ...`. **Верификация:** `pytest tests/test_context_compaction.py::TestNotifyRecordsEventLog::test_notify_calls_both_when_notify_enabled` — pass; новый тест `test_notify_still_records_event_log_when_notify_disabled` — pass.

- [ ] 3.3 В `tests/test_context_compaction.py:837-854` заменить `test_notify_skips_event_log_when_notify_disabled` на `test_notify_still_records_event_log_when_notify_disabled`: при `notify_in_history=False` ожидается, что `_write_history_notice` НЕ вызван, а `_record_event_log` ВЫЗВАН. **Верификация:** `pytest tests/test_context_compaction.py::TestNotifyRecordsEventLog` — все 4 теста зелёные.

- [ ] 3.4 В `ContextCompactionService.record_external_compaction` (`lib/services/context_compaction.py:362-402`) удалить ранний return `if not self.notify_in_history: return` — он гасит observability-trail при авто-сжатии с отключённым UI-уведомлением. Доверить решение `_notify`, который теперь сам разделяет concerns. **Верификация:** новый тест `test_record_external_compaction_logs_event_when_notify_disabled` — pass; старые тесты `record_external_compaction` (3+) — pass без изменений.

- [ ] 3.5 Переписать `_record_event_log` (`lib/services/context_compaction.py:316-360`):
  - убрать импорт `from workspace.utils.event_log import record_event`;
  - убрать `asyncio.to_thread(record_event, ...)` (sync INSERT);
  - новый код: собрать `LogEvent(event_type="context_compacted", level="INFO", session_id=session_key, channel="system", actor="system", name="consolidator", summary=summary, payload=payload)`; вызвать `DbLoggingService.try_log_event(self._db_logging_service, log_event, producer="ContextCompactionService", event_type="context_compacted")` (sync вызов — `log_event` неблокирующий, ставит в queue).
  **Верификация:** `pytest tests/test_context_compaction.py::TestNotifyRecordsEventLog::test_record_event_log_uses_log_event` — pass (проверка `db_logging_service.log_event.assert_called_once_with(LogEvent(event_type="context_compacted", ...))` через `try_log_event`-обёртку); `test_record_event_log_handles_db_logging_service_unavailable` — pass (no-op for business; operational WARNING emitted через `try_log_event`).

## 4. Phase 3 — sync-события через `DbLoggingService` (design D4, D5)

- [ ] 4.1 В `lib/services/pg_duckdb_sync_service.py:155-197` (`_log_sync_event`) заменить `emit_sync_event(...)` на `DbLoggingService.try_log_event(self._db_logging_service, log_event, producer="PgDuckDbSyncService", event_type=event_type)`. Конкретный шаг: собрать `LogEvent(event_type=event_type, level=level, session_id="gateway:sync", channel=None, actor="sync", name=name or event_type, summary=summary, payload=payload)` и зовём `try_log_event`. Никаких fallback'ов, никаких прямых INSERT. **Верификация:** новые тесты `tests/test_pg_duckdb_sync_service.py::TestLogSyncEvent::test_uses_db_logging_service_when_running` — pass; `test_noop_with_warning_when_service_none` — pass (caplog фиксирует WARNING); `test_noop_with_warning_when_service_not_running` — pass.

- [ ] 4.2 В `lib/services/duckdb_cache_store.py:46-74` удалить helper `_emit_sync_event` (внутренняя обёртка) целиком. Все 6+ call-site'ов внутри `duckdb_cache_store.py` (по `git grep "emit_sync_event\|record_sync_event" lib/services/duckdb_cache_store.py`) переписать на прямой вызов `DbLoggingService.try_log_event(self._db_logging_service, log_event, producer="DuckDbCacheStore", event_type=event_type)`. **Верификация:** `git grep -n "_emit_sync_event\|record_sync_event\|emit_sync_event" lib/services/duckdb_cache_store.py` — пусто; `pytest tests/test_duckdb_cache_store.py` зелёный.

- [ ] 4.3 В `lib/services/preload_service.py:36-61` (`_emit_health_event`) заменить `emit_sync_event(...)` на `DbLoggingService.try_log_event(self._db_logging_service, log_event, producer="PreloadService", event_type="vector_index_preload_health")`. Параметр `service` остаётся в сигнатуре как `self._db_logging_service`, но семантика меняется: business no-op + WARNING при `None` / `not is_running()`. **Верификация:** `pytest tests/test_preload_service.py::TestEmitHealthEvent` — pass (no-op + WARNING сценарии + happy path через `try_log_event`).

- [ ] 4.4 В `lib/core/application_context.py:950-967` **удалить** `_record_sync_skipped` целиком. Все 3+ call-site'а в `_make_sync_services` переписаны на **единый контракт через `try_log_event`** (НЕ «или loguru-warning»):
  - Если `ctx.db_logging_service` доступен — `DbLoggingService.try_log_event(ctx.db_logging_service, log_event, producer="ApplicationContext", event_type=event_type)`;
  - Если недоступен — внутри `try_log_event` уже зафиксирован WARNING (см. 3.1), **никакого дополнительного `logger.warning` снаружи**.
  **Верификация:** `git grep -n "_record_sync_skipped" lib/` — пусто; `pytest tests/test_application_context.py` — зелёный; новый unit-тест `tests/test_application_context.py::TestSyncSkipped::test_uses_try_log_event_when_service_available` (passes `log_event`-mock через `try_log_event`) — pass; `test_uses_try_log_event_warning_when_service_none` (caplog WARNING) — pass.

- [ ] 4.5 В `lib/services/db_logging_service.py` обновить docstring `log_sync_event` (строки 492-499): убрать упоминание `workspace.utils.event_log.record_sync_event` и фразу «Если сервис недоступен — caller должен упасть в `event_log.record_sync_event`» (этот fallback ликвидируется change'ом). Новый docstring: «Записать событие из PG→DuckDB sync-пути. Используется из worker-потока `PgDuckDbSyncService` (и аналогичных). При отсутствии сервиса caller должен использовать `DbLoggingService.try_log_event(...)` (D3), который даёт no-op for business + operational WARNING, без fallback INSERT». **Верификация:** `git grep -n "record_sync_event\|record_event\|emit_sync_event" lib/services/db_logging_service.py` — пусто (никаких ссылок на удалённый модуль в каноническом сервисе).

## 5. Phase 4 — удаление `workspace/utils/event_log.py` (design D4)

- [ ] 5.1 Удалить файл `workspace/utils/event_log.py` целиком (197 строк). **Верификация:** `git rm workspace/utils/event_log.py` или удаление через `Remove-Item`; файл отсутствует в `git status` (только в untracked + потом staged для commit).

- [ ] 5.2 Удалить тест `tests/test_event_log.py` целиком (83 строки, тестировал прямой INSERT bypass'а). **Верификация:** `git rm tests/test_event_log.py`; `pytest tests/ --collect-only -q | grep test_event_log` — пусто.

- [ ] 5.3 Глобальный grep `record_event\|record_sync_event\|emit_sync_event\|workspace.utils.event_log` по всему репозиторию (включая тесты, документацию, `CHANGELOG.md`). Все вхождения в Python-файлах удалены. **Верификация:** `git grep -n "record_event\|record_sync_event\|emit_sync_event\|workspace.utils.event_log" -- '*.py'` — пусто; в `.md`-файлах допустимы только исторические упоминания в `CHANGELOG.md` (секции уже выпущенных релизов) и `docs/architecture/HISTORY_SEARCH_ANALYSIS.md` (snapshot старого поведения).

## 6. Phase 5 — architecture guard (design D6)

- [ ] 6.1 Создать `tests/test_unified_event_logging_pipeline.py::TestNoProductionDirectWriters` (production-allowlist, design D6.1). Обход production runtime-путей: `lib/**/*.py`, `workspace/**/*.py`, `tools/*.py`, `cli_agent.py`, `gateway.py`, `streamlit_app.py`. **Исключаются** `tests/` (тесты могут содержать SQL fixture). Allowlist `LOGGING_OWNERS = {"lib/services/db_logging_service.py"}`. Guard проверяет: (a) AST-парсинг `ast.Call(func=ast.Attribute(attr='execute'), args=[...])` где SQL-литерал содержит имя logging-таблицы; (b) regex-fallback для f-string с динамической подстановкой `table_name` из `settings.logging.db.table_name` (отдельный negative-тест D6.4); (c) доступ к `logging.db.table_name` / `logging.db.schema` вне `LOGGING_OWNERS`. **Верификация:** `pytest tests/test_unified_event_logging_pipeline.py::TestNoProductionDirectWriters` — все параметризованные кейсы зелёные на baseline (после выполнения 1–5).

- [ ] 6.2 Создать `tests/test_unified_event_logging_pipeline.py::TestNoDeletedModuleImports` (global guard, design D6.2). Обход **всего** Python-кода включая `tests/`, `tools/`, `lib/`, `workspace/`, application entrypoints. AST-парсинг: (a) `ast.Import` / `ast.ImportFrom` с `module='workspace.utils.event_log'`; (b) `ast.Call(func=ast.Name(id='record_event'|'record_sync_event'|'emit_sync_event'))`. **Верификация:** `pytest tests/test_unified_event_logging_pipeline.py::TestNoDeletedModuleImports` — все кейсы зелёные.

- [ ] 6.3 Создать `tests/test_unified_event_logging_pipeline.py::TestRepositoryGrepBaseline` (CI-проверка, design D6.4). Прогоняет 4 `git grep`-проверки: (a) `git grep -n 'INSERT INTO .* agent_gateway_logs' -- '*.py'` — только `lib/services/db_logging_service.py`; (b) `git grep -nE '\b(record_event|record_sync_event|emit_sync_event)\(' -- '*.py'` — пусто; (c) `git grep -n 'from workspace.utils.event_log\|import workspace.utils.event_log' -- '*.py'` — пусто; (d) `git grep -n 'logging\.db\.table_name\|logging\["db"\]\["table_name"\]' -- '*.py'` вне `lib/services/db_logging_service.py` — пусто. **Верификация:** тест зелёный после выполнения change; если кто-то добавит новый INSERT/import/lookup — тест падает с указанием файла и строки.

- [ ] 6.4 Создать negative-тесты с `tmp_path`-фикстурами (страховка регрессии самого guard'а): (a) `test_guard_catches_dynamic_table_insert` — создать `tmp_path/fixture.py` с `cursor.execute(f'INSERT INTO "{schema}"."{table}" ...')` где `table = settings.logging.db.table_name` — guard ловит через ownership-проверку + AST dynamic-name detection; (b) `test_guard_catches_event_log_import` — `tmp_path/fixture.py` с `from workspace.utils.event_log import record_event` — guard ловит; (c) `test_guard_ignores_docstring_mentions` — фикстура с docstring, упоминающим `record_event` как историческое имя — guard НЕ падает (AST не заглядывает в `Expr(value=Constant(...))`). **Верификация:** ручной прогон каждого negative-теста с `tmp_path`-фикстурой.

## 7. Phase 6 — расширение unit/integration тестов

- [ ] 7.1 Добавить `tests/test_unified_event_logging_pipeline.py::TestContextCompactionNotifyBehavior`: (a) `notify_in_history=True` → оба эффекта (`_write_history_notice` + `_record_event_log`); (b) `notify_in_history=False` → только `_record_event_log` (нет `_write_history_notice`); (c) `notify_in_history=False` для `record_external_compaction` → тоже только event log; (d) `notify_in_history=False` для auto-compaction patch (`patch_compaction_tracking` остаётся активным) — `_record_event_log` ВСЕ ЕЩЁ вызывается (закрывает gap из design D8). **Верификация:** 4 теста зелёные.

- [ ] 7.2 Добавить `tests/test_unified_event_logging_pipeline.py::TestDbLoggingServiceUnavailableBehavior`: (a) `compact()` при `db_logging_service=None` — compaction успешен, `context_compacted` не записан (no-op for business + operational WARNING); (b) `_log_sync_event` при `db_logging_service=None` — no-op for business + WARNING; (c) `_emit_health_event` (preload_service) при `db_logging_service=None` — no-op for business + WARNING; (d) `_record_sync_skipped`-замена (внутри `_make_sync_services`) при недоступности — WARNING через `try_log_event`. Все 4 проверяют **уровень WARNING** через `caplog.records`. **Верификация:** 4+ теста зелёные.

- [ ] 7.3 Добавить `tests/test_unified_event_logging_pipeline.py::TestProducerReadsNoConfig`: параметризованный тест по списку producers (`ContextCompactionService`, `PgDuckDbSyncService`, `DuckDbCacheStore`, `PreloadService`); проверка, что в исходниках этих модулей (после рефакторинга) нет `SETTINGS.get("logging"...)`, `SETTINGS.get("channels", {}).get("postgres", {}).get("dsn"...)`, `from config import SETTINGS` для целей INSERT в `agent_gateway_logs` (только для чтения собственных доменных настроек). **Верификация:** `pytest tests/test_unified_event_logging_pipeline.py -k no_config` — pass.

- [ ] 7.4 Прогнать regression-check на `tests/test_subagent_logging.py` и `tests/test_hooks_database_logging.py` — после рефакторинга `ContextCompactionService` форма `LogEvent` для `context_compacted` остаётся прежней (payload `{mode, archived_msgs, kept_msgs, tokens_before, tokens_after, summary, raw_dump}`); тесты не должны требовать изменений. **Верификация:** `pytest tests/test_subagent_logging.py tests/test_hooks_database_logging.py` зелёный.

## 8. Phase 7 — lifecycle и shutdown (design D7)

- [ ] 8.1 Подтвердить порядок в `ApplicationContext.start()` (`lib/core/application_context.py:267-346`): `db_logging_service.start()` происходит ДО `RuntimePatcher.apply_all` и ДО `_make_sync_services`. Если не подтверждается — изменить порядок и добавить unit-тест на инвариант. **Верификация:** `tests/test_application_context.py::TestStartupOrdering::test_db_logging_starts_before_runtime_patcher` — pass (явный тест инварианта D7).

- [ ] 8.2 Подтвердить `ApplicationContext.stop()` останавливает `DbLoggingService` ПОСЛЕ остановки emitters'ов (sync-сервисы, channel-pollers). **Верификация:** `tests/test_application_context.py::TestShutdownOrdering` — pass (если теста нет, добавить).

- [ ] 8.3 Покрыть тестом сценарий «shutdown теряет event в полёте»: `compact()` mid-flight + `ctx.stop()` без `_record_event_log` exception (no-op for business через `try_log_event` корректно отрабатывает после `stop()`). **Верификация:** новый тест `tests/test_unified_event_logging_pipeline.py::TestShutdownMidFlight` — pass.

## 9. Phase 8 — документация и observability

- [ ] 9.1 В `docs/ARCHITECTURE.md` секция «Управление сжатием контекста» (`ContextCompactionService`) — обновить описание `_notify`: явно зафиксировать, что `_record_event_log` идёт через `DbLoggingService.try_log_event` всегда при `enabled=True`, независимо от `notify_in_history`. Зафиксировать, что `record_external_compaction` тоже проходит через `_notify` (ранний return при `notify_in_history=false` удалён). **Верификация:** `grep -n "_record_event_log\|notify_in_history" docs/ARCHITECTURE.md` — упоминания согласованы с новым поведением.

- [ ] 9.2 В `docs/ARCHITECTURE.md` добавить секцию «Структурированное логирование» (или расширить существующую) с явным фиксированием: «`DbLoggingService` — единственный runtime writer `agent_gateway_logs` и `agent_question_runs`. Любой structured event передаётся через `db_logging_service.log_event(LogEvent(...))` или `DbLoggingService.try_log_event(...)`. Прямой SQL INSERT в журнал запрещён. Уровень логирования для «сервис недоступен» — WARNING». Сослаться на `openspec/specs/logging-db/spec.md`. **Верификация:** новая секция присутствует; содержит явную формулировку «единственный writer».

- [ ] 9.3 В `AGENTS.md` (Project Layout, Configuration) — убрать упоминания `workspace/utils/event_log.py` (модуль удалён). В Configuration-секции добавить абзац про «Единый logging pipeline: `DbLoggingService` — единственный writer, прямой INSERT запрещён». **Верификация:** `git grep -n "event_log" AGENTS.md` — только в changelog-ссылках или вообще отсутствует.

- [ ] 9.4 В `CHANGELOG.md` секция `## [Unreleased]` добавить категории:
  - `Removed`: `workspace/utils/event_log` module (record_event, record_sync_event, emit_sync_event).
  - `Changed`: `context_compacted` event теперь записывается через `DbLoggingService` всегда при `gateway.compact.enabled=true`, независимо от `notify_in_history` (gap №1 из `docs/architecture/HISTORY_SEARCH_ANALYSIS.md` теперь закрыт).
  - `Changed`: `PgDuckDbSyncService`, `DuckDbCacheStore`, `PreloadService` — sync-события идут через `DbLoggingService.try_log_event` без fallback INSERT (единый WARNING-уровень при недоступности сервиса).
  - `Changed`: `RuntimePatcher.patch_compaction_tracking` остаётся активным при `notify_in_history=false` (раньше отключался целиком); разделение concerns в `_notify` идёт через `notify_in_history` только для UI-стороны.
  - `Changed`: DI producer'ов — explicit kwarg во всех 5 production call-site'ах `ContextCompactionService(db_logging_service=...)` (обязательный keyword-only параметр); DI поднимается через `functools.partial` (`RuntimePatcher.patch_compact_command`) и параметр `run_repl(...)` (`lib/cli/console_loop.py`); никаких DI-полей на `agent` (ни `_db_logging_service`, ни `db_logging_service`).
  - `Added`: `tests/test_unified_event_logging_pipeline.py` — два независимых guard'а (production-direct-writers, deleted-module-imports) + git grep baseline.
  - `Added`: `DbLoggingService.try_log_event(...)` — единый helper для producer'ов (no-op for business + operational WARNING при недоступности).
  **Верификация:** `git grep -n "workspace/utils/event_log\|unified-event-logging\|notify_in_history" CHANGELOG.md` — записи в `[Unreleased]` присутствуют.

- [ ] 9.5 В `docs/skill-tool-inventory.md` пометить `workspace.utils.event_log` как «удалён в release vX.Y — заменён `DbLoggingService.log_event(LogEvent(...))` / `DbLoggingService.try_log_event(...)`». **Верификация:** упоминание в истории удалённых модулей есть.

- [ ] 9.6 В `docs/architecture/HISTORY_SEARCH_ANALYSIS.md` секция «Gap №1» пометить как «закрыт в release vX.Y — `ContextCompactionService._record_event_log` через `DbLoggingService.try_log_event`, не зависит от `notify_in_history`. `history_search(event_type="context_compacted")` теперь возвращает событие при любой настройке `notify_in_history`». **Верификация:** текст gap-раздела обновлён.

- [ ] 9.7 В `docs/ARCHITECTURE.md` (секция «Структурированное логирование», добавленная в 9.2) добавить подсекцию «Skill invocation is out of scope» с явной формулировкой: Skills не имеют dedicated runtime `event_type`; загрузка `SKILL.md` в context не порождает event; вызов Skill-скриптов через `tools.exec` логируется как штатная пара `tool_call`/`tool_result`; `DbLoggingService.log_skill_call` НЕ вводится. Сослаться на `openspec/specs/logging-db/spec.md` requirement «Skill invocation is out of scope». **Верификация:** подсекция присутствует; явно упоминает, что `event_type="skill_call"` НЕ эмитится и что `log_skill_call` НЕ существует.

## 10. Phase 9 — регрессия и валидация

- [ ] 10.1 `pytest tests/` — все тесты зелёные. Целевой baseline: `1480+ passed, ~22 skipped` (как baseline `CHANGELOG.md`); учёт удалённых тестов `test_event_log.py` (4) и новых `test_unified_event_logging_pipeline.py` (~30). **Верификация:** финальный прогон; `pytest tests/ -q 2>&1 | tail -5` показывает зелёный итог.

- [ ] 10.2 `python cli_agent.py --profile=test --smoke` → `OK_SMOKE_COMPLETE`. **Верификация:** smoke-прогон показывает, что `compact_context` зарегистрирован и `history_search` находит `context_compacted` через `DbLoggingService`.

- [ ] 10.3 `python gateway.py --profile=test --smoke` (если есть) → аналогичный smoke-прогон с проверкой, что sync-события идут через `DbLoggingService` (`get_stats()["written_by_type"]` после smoke содержит `sync_service_started`, `sync_initial_load_started`, и т.п.). **Верификация:** smoke + проверка stats.

- [ ] 10.4 `git grep -n 'INSERT INTO .* agent_gateway_logs' -- '*.py'` — только `lib/services/db_logging_service.py`. Это **не** основной архитектурный критерий (см. requirement «Single writer invariant»: «sole owner permitted to persist», а не «одна строка в репозитории»), а quick smoke-guard. Основная проверка — AST + ownership из task 6.1. **Верификация:** `git grep` находит ровно одно совпадение (или ноль, если logging-таблица задаётся через f-string в `db_logging_service.py`); AST-guard из 6.1 зелёный.

- [ ] 10.5 `git grep -n 'from workspace.utils.event_log\|import workspace.utils.event_log' -- '*.py'` — пусто. **Верификация:** `git grep` пустой.

- [ ] 10.6 `git grep -nE '\b(record_event|record_sync_event|emit_sync_event)\(' -- '*.py'` — пусто (AST-уровневая проверка вызовов функций; regex-precise через `\b` и `\(`). **Верификация:** `git grep` пустой.

- [ ] 10.7 `git grep -nE 'agent\.db_logging_service|agent\._db_logging_service|self\._db_logging_service = .* getattr' -- '*.py'` — пусто (нет скрытого и нет «публичного» DI-канала на `agent`; нет fallback на `getattr`). **Верификация:** `git grep` пустой (допустимы только `self._db_logging_service = db_logging_service` в конструкторах producer'ов — но без `getattr(agent, ...)`).

- [ ] 10.8 `git grep -nE '\blog_skill_call\b|event_type\s*=\s*"skill_call"' -- '*.py'` — пусто (страховка от регрессии: skill_call event_type и `log_skill_call` метод НЕ должны появиться). Это подтверждает requirement «Skill invocation is out of scope» — нет dedicated Skill runtime event. **Верификация:** `git grep` пустой.

- [ ] 10.9 `openspec.cmd validate unify-agent-event-logging-pipeline` → `passed`, 0 issues. **Верификация:** финальный прогон валидатора.

- [ ] 10.10 `openspec.cmd status --change unify-agent-event-logging-pipeline --json` → `isComplete: true`, все артефакты `done`. **Верификация:** `applyRequires: []` (только `tasks` в `applyRequires`, который становится `[tasks ✓]`).
