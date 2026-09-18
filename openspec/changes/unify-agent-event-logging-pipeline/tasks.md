# Tasks — unify-agent-event-logging-pipeline

> **Цель:** ликвидировать второй runtime-механизм
> записи structured agent events в `agent_gateway_logs`
> (`workspace/utils/event_log.py` с `record_event` /
> `record_sync_event` / `emit_sync_event`) и
> зафиксировать единственный путь —
> `DbLoggingService`.

## 1. Phase 0 — инвентаризация writers (Phase 0 design)

- [ ] 1.1 Создать отчёт `docs/architecture/EVENT_LOGGING_INVENTORY.md` со списком **всех** мест в runtime-коде, где происходит запись в `agent_gateway_logs`. Покрыть минимум: `lib/services/db_logging_service.py` (канон), `workspace/utils/event_log.py` (весь файл: `record_event` / `record_sync_event` / `emit_sync_event`), `lib/services/context_compaction.py` (`_record_event_log`), `lib/services/pg_duckdb_sync_service.py` (`_log_sync_event`), `lib/services/duckdb_cache_store.py` (`_emit_sync_event`), `lib/services/preload_service.py` (`_emit_health_event`), `lib/core/application_context.py` (`_record_sync_skipped`). Каждая строка: writer / текущий путь / целевой путь / риск (fallback yes/no). **Верификация:** `grep -rn 'agent_gateway_logs' lib/ workspace/ tools/ | sort -u > /tmp/baseline.txt` совпадает со строками из отчёта; ни одного необъяснённого вхождения в baseline-списке.

- [ ] 1.2 Зафиксировать Phase 0 как выполненный в `tasks.md §1.1-1.2` — на момент старта реализации список writers считается полным и **расширение запрещено** до завершения change.

## 2. Phase 1 — DI-инфраструктура (design D1, D8)

- [ ] 2.1 В `RuntimePatcher.apply_all` (`lib/services/runtime_patcher.py:440-493`) после успешного `_record` для каждого patch'а, использующего `db_logging_service`, добавить `if db_logging_service is not None: agent._db_logging_service = db_logging_service`. Выставить `agent._db_logging_service = None` явно перед `apply_all` (если ещё не выставлен), чтобы тесты с агентом без сервиса получали `None`. **Верификация:** `python -c "from lib.services.runtime_patcher import RuntimePatcher; from unittest.mock import MagicMock; p = RuntimePatcher(); r = MagicMock(); p.apply_all(MagicMock(), MagicMock(), '/tmp', r); assert hasattr(r, '_db_logging_service')"` — атрибут присутствует после `apply_all`.

- [ ] 2.2 В `ContextCompactionService.__init__` (`lib/services/context_compaction.py:65-69`) добавить keyword-параметр `db_logging_service: Any = None`; присвоить `self._db_logging_service = db_logging_service or getattr(agent, "_db_logging_service", None)`. **Верификация:** существующие 5 call-site'ов (`compact_command.py:48`, `console_loop.py:149`, `compact_context.py:128`, `runtime_patcher.py:1879`, тесты) **не изменены** и продолжают работать; новый kwarg опциональный с дефолтом `None`.

- [ ] 2.3 В `runtime_patcher.patch_compaction_tracking` (`lib/services/runtime_patcher.py:1848-1888`) добавить `db_logging_service` kwarg-передачу в `ContextCompactionService(...)` (через явный параметр, не только через `agent._db_logging_service` — fallback path). Аналогично в `patch_compact_command` (`lib/services/runtime_patcher.py:1890+`), если он тоже создаёт `ContextCompactionService`. **Верификация:** `pytest tests/test_compact_command.py -k tracking` зелёный; `tests/test_compact_command.py::TestDbLoggingServicePropagation::test_svc_receives_db_logging_service` — pass.

- [ ] 2.4 Покрыть тестом `tests/test_application_context_logging.py` (или новым `tests/test_unified_event_logging_pipeline.py`): runtime-сценарий `apply_all` с реальным `db_logging_service` моком — `agent._db_logging_service` совпадает с переданным. **Верификация:** `pytest tests/test_unified_event_logging_pipeline.py -k di_propagation` зелёный.

## 3. Phase 2 — `ContextCompactionService._notify` decoupling (design D2, D3)

- [ ] 3.1 В `ContextCompactionService._notify` (`lib/services/context_compaction.py:299-314`) разделить два concerns: (a) **structured event** — `await self._record_event_log(...)` ВСЕГДА (без условия `notify_in_history`); (b) UI-history-notice — `if self.notify_in_history: await self._write_history_notice(...)`; (c) terminal output — `if self.print_to_terminal: ...`. **Верификация:** `pytest tests/test_context_compaction.py::TestNotifyRecordsEventLog::test_notify_calls_both_when_notify_enabled` — pass; новый тест `test_notify_still_records_event_log_when_notify_disabled` — pass; старый `test_notify_skips_event_log_when_notify_disabled` **переписан** под новое поведение (см. 3.2).

- [ ] 3.2 В `tests/test_context_compaction.py:837-854` заменить `test_notify_skips_event_log_when_notify_disabled` на `test_notify_still_records_event_log_when_notify_disabled`: при `notify_in_history=False` ожидается, что `_write_history_notice` НЕ вызван, а `_record_event_log` ВЫЗВАН. **Верификация:** `pytest tests/test_context_compaction.py::TestNotifyRecordsEventLog` — все 4 теста зелёные.

- [ ] 3.3 В `ContextCompactionService.record_external_compaction` (`lib/services/context_compaction.py:362-402`) удалить ранний return `if not self.notify_in_history: return` — он гасит observability-trail при авто-сжатии с отключённым UI-уведомлением. Доверить решение `_notify`, который теперь сам разделяет concerns. **Верификация:** новый тест `test_record_external_compaction_logs_event_when_notify_disabled` — pass; старые тесты `record_external_compaction` (3+) — pass без изменений.

- [ ] 3.4 Переписать `_record_event_log` (`lib/services/context_compaction.py:316-360`): убрать импорт `from workspace.utils.event_log import record_event`; заменить `await _asyncio.to_thread(record_event, ...)` на проверку `self._db_logging_service` (None или `not is_running()` → silent no-op + loguru `DEBUG`) и затем `self._db_logging_service.log_event(LogEvent(event_type="context_compacted", level="INFO", session_id=session_key, channel="system", actor="system", name="consolidator", summary=summary, payload=payload))`. **Верификация:** `pytest tests/test_context_compaction.py::TestNotifyRecordsEventLog::test_record_event_log_handles_db_logging_service_unavailable` — pass (silent no-op, никакого импорта удалённого модуля); `pytest tests/test_context_compaction.py::TestNotifyRecordsEventLog::test_record_event_log_uses_log_event` — pass (проверка `db_logging_service.log_event.assert_called_once_with(LogEvent(event_type="context_compacted", ...))`).

## 4. Phase 3 — sync-события через `DbLoggingService` (design D4, D5)

- [ ] 4.1 В `lib/services/pg_duckdb_sync_service.py:155-197` (`_log_sync_event`) заменить `emit_sync_event(...)` на прямой вызов: `if self._db_logging_service is not None and self._db_logging_service.is_running(): self._db_logging_service.log_sync_event(event_type=..., summary=..., payload=..., level=..., name=...)`; иначе `logger.debug("sync event dropped: {} (no DbLoggingService)", event_type)`. Никаких fallback'ов. **Верификация:** новый тест `tests/test_pg_duckdb_sync_service.py::TestLogSyncEvent::test_uses_db_logging_service_when_running` — pass; `test_silent_noop_when_db_logging_service_none` — pass; `test_silent_noop_when_db_logging_service_not_running` — pass.

- [ ] 4.2 В `lib/services/duckdb_cache_store.py:46-74` удалить helper `_emit_sync_event` (внутренняя обёртка) целиком. Все 6+ call-site'ов внутри `duckdb_cache_store.py` (по `grep "emit_sync_event\|record_sync_event" lib/services/duckdb_cache_store.py`) переписать на прямой вызов `self._db_logging_service.log_sync_event(...)` через тот же pattern (silent no-op если `None`/`not is_running()`). **Верификация:** `grep -n "_emit_sync_event\|record_sync_event\|emit_sync_event" lib/services/duckdb_cache_store.py` — пусто; `pytest tests/test_duckdb_cache_store.py` зелёный.

- [ ] 4.3 В `lib/services/preload_service.py:36-61` (`_emit_health_event`) заменить `emit_sync_event(...)` на прямой `self._db_logging_service.log_sync_event(event_type="vector_index_preload_health", summary=..., payload=..., level=...)`. Параметр `service` (сейчас передаётся как `self._db_logging_service`) остаётся, но семантика меняется: silent no-op при `None` / `not is_running()`, без `emit_sync_event` fallback. **Верификация:** `pytest tests/test_preload_service.py::TestEmitHealthEvent` — pass (silent no-op сценарии + happy path).

- [ ] 4.4 В `lib/core/application_context.py:950-967` удалить `_record_sync_skipped` целиком. Все 3+ call-site'а в `_make_sync_services` переписать на `logger.warning("sync skipped: {} — {}", reason, detail)` (если observability не критична) или `ctx.db_logging_service.log_error(...)` (если оператору важно видеть в `agent_gateway_logs`). **Верификация:** `grep -n "_record_sync_skipped" lib/` — пусто; `pytest tests/test_application_context.py` — зелёный.

## 5. Phase 4 — удаление `workspace/utils/event_log.py` (design D4)

- [ ] 5.1 Удалить файл `workspace/utils/event_log.py` целиком (197 строк). **Верификация:** `git rm workspace/utils/event_log.py` или удаление через `Remove-Item`; файл отсутствует в `git status` (только в untracked + потом staged для commit).

- [ ] 5.2 Удалить тест `tests/test_event_log.py` целиком (83 строки, тестировал прямой INSERT bypass'а). **Верификация:** `git rm tests/test_event_log.py`; `pytest tests/ --collect-only -q | grep test_event_log` — пусто.

- [ ] 5.3 Глобальный grep `record_event\|record_sync_event\|emit_sync_event\|workspace.utils.event_log` по всему репозиторию (включая тесты, документацию, `CHANGELOG.md`). Все вхождения в Python-файлах удалены. **Верификация:** `git grep -n "record_event\|record_sync_event\|emit_sync_event\|workspace.utils.event_log" -- '*.py'` — пусто; в `.md`-файлах допустимы только исторические упоминания в `CHANGELOG.md` (секции уже выпущенных релизов) и `docs/architecture/HISTORY_SEARCH_ANALYSIS.md` (snapshot старого поведения).

## 6. Phase 5 — architecture guard (design D6)

- [ ] 6.1 Создать `tests/test_unified_event_logging_pipeline.py` с классом `TestNoDirectWriters` (параметризованный по списку путей: `lib/**/*.py`, `workspace/**/*.py`, `tools/*.py`, `cli_agent.py`, `gateway.py`, `streamlit_app.py`, `tests/**/*.py`). Исключение: `lib/services/db_logging_service.py` (единственное место с легитимным INSERT в `agent_gateway_logs`). Guard проверяет: (a) regex `INSERT\s+INTO\s+["\']?(?:[a-zA-Z_]\w*\.)?["\']?["\']?agent_gateway_logs["\']?`; (b) regex `from\s+workspace\.utils\.event_log\b|import\s+workspace\.utils\.event_log\b`; (c) `ast`-парсинг с поиском `ast.Call(func=ast.Name(id="record_event"|"record_sync_event"|"emit_sync_event"))`. **Верификация:** `pytest tests/test_unified_event_logging_pipeline.py::TestNoDirectWriters` — все 100+ параметризованных кейсов зелёные на baseline (после выполнения 1–5).

- [ ] 6.2 Добавить negative-тест `test_guard_catches_direct_insert`: временно создать `tmp_path/fixture.py` с `cursor.execute('INSERT INTO public.agent_gateway_logs ...')` — guard падает с указанием файла/строки; после удаления fixture — guard снова зелёный. **Верификация:** ручной прогон теста с fixture и без (через `tmp_path`).

- [ ] 6.3 Добавить negative-тест `test_guard_catches_event_log_import`: временно создать `tmp_path/fixture.py` с `from workspace.utils.event_log import record_event` — guard падает; удалить — guard зелёный. **Верификация:** аналогично 6.2.

- [ ] 6.4 Добавить negative-тест `test_guard_ignores_docstring_mentions`: создать фикстуру с docstring, упоминающим `record_event` как имя исторической функции — guard НЕ падает (AST-парсинг не заглядывает в `Expr(value=Constant(...))` узлы docstring'ов). **Верификация:** ручной прогон с фикстурой.

## 7. Phase 6 — расширение unit/integration тестов

- [ ] 7.1 Добавить `tests/test_unified_event_logging_pipeline.py::TestContextCompactionNotifyBehavior`: (a) `notify_in_history=True` → оба эффекта (`_write_history_notice` + `_record_event_log`); (b) `notify_in_history=False` → только `_record_event_log` (нет `_write_history_notice`); (c) `notify_in_history=False` для `record_external_compaction` → тоже только event log. **Верификация:** 3 теста зелёные.

- [ ] 7.2 Добавить `tests/test_unified_event_logging_pipeline.py::TestDbLoggingServiceUnavailableBehavior`: (a) `compact()` при `db_logging_service=None` — compaction успешен, `context_compacted` не записан (silent no-op); (b) `_log_sync_event` при `db_logging_service=None` — silent no-op; (c) `_emit_health_event` при `db_logging_service=None` — silent no-op; (d) `_record_sync_skipped`-замена (`logger.warning`) при недоступности. **Верификация:** 4+ теста зелёные.

- [ ] 7.3 Добавить `tests/test_unified_event_logging_pipeline.py::TestProducerReadsNoConfig`: параметризованный тест по списку producers (`ContextCompactionService`, `PgDuckDbSyncService`, `DuckDbCacheStore`, `PreloadService`); проверка, что в исходниках этих модулей (после рефакторинга) нет `SETTINGS.get("logging"...)` или `SETTINGS.get("channels", {}).get("postgres", {}).get("dsn"...)`. **Верификация:** `pytest tests/test_unified_event_logging_pipeline.py -k no_config` — pass.

- [ ] 7.4 Обновить `tests/test_subagent_logging.py` и `tests/test_hooks_database_logging.py` (если требуется) — после рефакторинга `ContextCompactionService` форма `LogEvent` для `context_compacted` остаётся прежней (payload `{mode, archived_msgs, kept_msgs, tokens_before, tokens_after, summary, raw_dump}`); тесты не должны требовать изменений, прогнать как regression-check. **Верификация:** `pytest tests/test_subagent_logging.py tests/test_hooks_database_logging.py` зелёный.

## 8. Phase 7 — lifecycle и shutdown (design D7)

- [ ] 8.1 Подтвердить порядок в `ApplicationContext.start()` (`lib/core/application_context.py:267-346`): `db_logging_service.start()` происходит ДО `RuntimePatcher.apply_all` и ДО `_make_sync_services`. Если не подтверждается — изменить порядок и добавить unit-тест на инвариант. **Верификация:** `tests/test_application_context.py::TestStartupOrdering::test_db_logging_starts_before_runtime_patcher` — pass (явный тест инварианта D7).

- [ ] 8.2 Подтвердить `ApplicationContext.stop()` останавливает `DbLoggingService` ПОСЛЕ остановки emitters'ов (sync-сервисы, channel-pollers). **Верификация:** `tests/test_application_context.py::TestShutdownOrdering` — pass (если теста нет, добавить).

- [ ] 8.3 Покрыть тестом сценарий «shutdown теряет event в полёте»: `compact()` mid-flight + `ctx.stop()` без `_record_event_log` exception (silent no-op корректно). **Верификация:** новый тест `tests/test_unified_event_logging_pipeline.py::TestShutdownMidFlight` — pass.

## 9. Phase 8 — документация и observability

- [ ] 9.1 В `docs/ARCHITECTURE.md` секция «Управление сжатием контекста» (`ContextCompactionService`) — обновить описание `_notify`: явно зафиксировать, что `_record_event_log` идёт через `DbLoggingService` всегда при `enabled=True`, независимо от `notify_in_history`. **Верификация:** `grep -n "_record_event_log\|notify_in_history" docs/ARCHITECTURE.md` — упоминания согласованы с новым поведением.

- [ ] 9.2 В `docs/ARCHITECTURE.md` добавить секцию «Структурированное логирование» (или расширить существующую) с явным фиксированием: «`DbLoggingService` — единственный runtime writer `agent_gateway_logs`. Любой structured event передаётся через `db_logging_service.log_event(LogEvent(...))`. Прямой SQL INSERT в журнал запрещён». Сослаться на `openspec/specs/logging-db/spec.md`. **Верификация:** новая секция присутствует; содержит явную формулировку «единственный writer».

- [ ] 9.3 В `AGENTS.md` (Project Layout, Configuration) — убрать упоминания `workspace/utils/event_log.py` (модуль удалён). В Configuration-секции добавить абзац про «Единый logging pipeline: `DbLoggingService` — единственный writer». **Верификация:** `grep -n "event_log" AGENTS.md` — только в секции с явным указанием «удалён в release vX.Y» (если есть changelog-ссылка) или вообще отсутствует.

- [ ] 9.4 В `CHANGELOG.md` секция `## [Unreleased]` добавить категории:
  - `Removed`: `workspace/utils/event_log` module (record_event, record_sync_event, emit_sync_event).
  - `Changed`: `context_compacted` event теперь записывается через `DbLoggingService` всегда при `gateway.compact.enabled=true`, независимо от `notify_in_history` (closes gap №1 из `docs/architecture/HISTORY_SEARCH_ANALYSIS.md`).
  - `Changed`: `PgDuckDbSyncService`, `DuckDbCacheStore`, `PreloadService` — sync-события идут через `DbLoggingService` без fallback INSERT.
  - `Added`: `tests/test_unified_event_logging_pipeline.py` — architecture guard против прямых INSERT и импорта `workspace.utils.event_log`.
  **Верификация:** `grep -n "workspace/utils/event_log\|unified-event-logging\|notify_in_history" CHANGELOG.md` — записи в `[Unreleased]` присутствуют.

- [ ] 9.5 В `docs/skill-tool-inventory.md` пометить `workspace.utils.event_log` как «удалён в release vX.Y — заменён `DbLoggingService.log_event(LogEvent(...))`». **Верификация:** упоминание в истории удалённых модулей есть.

- [ ] 9.6 В `docs/architecture/HISTORY_SEARCH_ANALYSIS.md` секция «Gap №1» пометить как «закрыт в release vX.Y — `ContextCompactionService._record_event_log` через `DbLoggingService.log_event`, не зависит от `notify_in_history`». **Верификация:** текст gap-раздела обновлён.

## 10. Phase 9 — регрессия и валидация

- [ ] 10.1 `pytest tests/` — все тесты зелёные. Целевой baseline: `1480+ passed, ~22 skipped` (как baseline `CHANGELOG.md`); учёт удалённых тестов `test_event_log.py` (4) и новых `test_unified_event_logging_pipeline.py` (~20). **Верификация:** финальный прогон; `pytest tests/ -q 2>&1 | tail -5` показывает зелёный итог.

- [ ] 10.2 `python cli_agent.py --profile=test --smoke` → `OK_SMOKE_COMPLETE`. **Верификация:** smoke-прогон показывает, что `compact_context` зарегистрирован и `history_search` находит `context_compacted` через `DbLoggingService`.

- [ ] 10.3 `python gateway.py --profile=test --smoke` (если есть) → аналогичный smoke-прогон с проверкой, что sync-события идут через `DbLoggingService` (`get_stats()["written_by_type"]` после smoke содержит `sync_service_started`, `sync_initial_load_started`, и т.п.). **Верификация:** smoke + проверка stats.

- [ ] 10.4 `git grep -n 'INSERT INTO .* agent_gateway_logs' -- '*.py'` — только `lib/services/db_logging_service.py`. **Верификация:** `git grep` находит ровно одно совпадение.

- [ ] 10.5 `git grep -n 'from workspace.utils.event_log\|import workspace.utils.event_log' -- '*.py'` — пусто. **Верификация:** `git grep` пустой.

- [ ] 10.6 `git grep -nE '\b(record_event|record_sync_event|emit_sync_event)\(' -- '*.py'` — пусто (AST-уровневая проверка вызовов функций; regex-precise через `\b` и `\(`). **Верификация:** `git grep` пустой.

- [ ] 10.7 `openspec.cmd validate unify-agent-event-logging-pipeline` → `passed`, 0 issues. **Верификация:** финальный прогон валидатора.

- [ ] 10.8 `openspec.cmd status --change unify-agent-event-logging-pipeline --json` → `isComplete: true`, все артефакты `done`. **Верификация:** `applyRequires: []` (только `tasks` в `applyRequires`, который становится `[tasks ✓]`).
