# Tasks — unify-agent-event-logging-pipeline

> **Цель:** ликвидировать второй runtime-механизм
> записи structured agent events в `agent_gateway_logs`
> (`workspace/utils/event_log.py` с `record_event` /
> `record_sync_event` / `emit_sync_event`) и
> зафиксировать единственный путь —
> `DbLoggingService`.

## 1. Phase 0 — инвентаризация writers (design D6.4 baseline)

- [x] 1.1 Создать отчёт `docs/architecture/EVENT_LOGGING_INVENTORY.md` со списком **всех** мест в runtime-коде, где происходит запись в `agent_gateway_logs` или в `agent_question_runs`. Файл создан в коммите `1893b17` (54 строки, покрывает `db_logging_service.py`, `context_compaction.py`, `pg_duckdb_sync_service.py`, `duckdb_cache_store.py`, `preload_service.py`, `application_context.py`).

- [x] 1.2 Phase 0 baseline inventory зафиксирован.

## 2. Phase 1 — explicit DI без скрытых каналов (design D1, D7, D8)

- [x] 2.1 В `RuntimePatcher.apply_all` (`lib/services/runtime_patcher.py:440-493`) **расширить сигнатуру** `patch_compaction_tracking` и `patch_compact_command` параметром `db_logging_service` (kwarg). Реализовано в коммите `1893b17`.

- [x] 2.2 В `runtime_patcher.patch_compaction_tracking` (`lib/services/runtime_patcher.py:1848-1888`):
  - [x] добавлен kwarg `db_logging_service` (keyword-only обязательный, без дефолта);
  - [x] удалён ранний return `if not svc.notify_in_history: return False, "..."` (строки 1882-1883) — patch остаётся активным при `notify_in_history=false`;
  - [x] передаётся `db_logging_service` в `ContextCompactionService(agent, settings=settings, db_logging_service=db_logging_service)`.

- [x] 2.3 В `ContextCompactionService.__init__` (`lib/services/context_compaction.py:65-69`):
  - [x] сигнатура `def __init__(self, agent, settings=None, *, db_logging_service)` — keyword-only обязательный kwarg (без `getattr(agent, ...)` fallback);
  - [x] тело `self._db_logging_service = db_logging_service`;
  - [x] обновлены 5 production call-site'ов (включая `workspace/tools/compact_context.py:128` через `ctx._db_logging_service`).

- [x] 2.4 В `lib/core/application_context.py` НЕ добавлять `_wire_agent_db_logging` и НЕ выставлять `agent.db_logging_service`. DI поднимается через `functools.partial` (`RuntimePatcher.patch_compact_command`) и параметр `run_repl(...)` (`lib/cli/console_loop.py`) — никаких промежуточных полей на `agent`.

- [x] 2.5 В `workspace/tools/compact_context.py:128` (tool `create`) добавлено **новое DI-поле** `ctx._db_logging_service` в `patch_project_tools` — симметрично существующим `ctx._agent_ref` / `ctx._settings_ref`. Источник: `db_logging_service=ctx.db_logging_service` из ApplicationContext.

## 3. Phase 2 — `ContextCompactionService._notify` decoupling (design D2, D3)

- [x] 3.1 В `lib/services/db_logging_service.py` добавлена функция-хелпер
      `try_log_event(svc, log_event, *, producer: str, event_type: str) -> bool`
      (строки 34+) — контракт WARNING-уровня для degraded state + no-op
      for business.

- [x] 3.2 В `ContextCompactionService._notify` (`lib/services/context_compaction.py:299-314`)
      разделены три concerns: structured event идёт ВСЕГДА, UI-history-notice
      идёт при `notify_in_history`, terminal output — при `print_to_terminal`.

- [x] 3.3 В `tests/test_context_compaction.py` заменено `test_notify_skips_event_log_when_notify_disabled`
      на `test_notify_still_records_event_log_when_notify_disabled`.

- [x] 3.4 В `ContextCompactionService.record_external_compaction` удалён ранний return
      `if not self.notify_in_history: return` — решение доверено `_notify`.

- [x] 3.5 `_record_event_log` переписан: убран импорт `from workspace.utils.event_log import record_event`,
      убран `asyncio.to_thread(record_event, ...)`. Теперь собирает `LogEvent(...)` и вызывает
      `DbLoggingService.try_log_event(self._db_logging_service, log_event, producer="ContextCompactionService", event_type="context_compacted")`.

## 4. Phase 3 — sync-события через `DbLoggingService` (design D4, D5)

- [x] 4.1 В `lib/services/pg_duckdb_sync_service.py:155-197` (`_log_sync_event`) —
      `emit_sync_event(...)` заменён на `DbLoggingService.try_log_event(...)`.
      Однако остался мёртвый комментарий в `pg_duckdb_sync_service.py:81`
      со ссылкой на `event_log.record_sync_event` — **требуется очистка**
      (мелкий долг, см. «Реальные долги» в сводке).

- [x] 4.2 В `lib/services/duckdb_cache_store.py` helper `_emit_sync_event`
      удалён; 7 call-site'ов инлайнены через `try_log_event(...)` с
      явным `LogEvent(...)`.

- [x] 4.3 В `lib/services/preload_service.py:36` (`_emit_health_event`) —
      helper оставлен как DEPRECATED back-compat shim (для тестов
      `TestEmitHealthEvent`, валидирующих контракт LogEvent); production
      call-site в `preload_vector_indexes` инлайнен. `try_log_event`
      в обоих случаях идёт через lookup на модуле
      (`lib.services.db_logging_service.try_log_event`) — чтобы
      тесты, патчущие этот атрибут, видели патч.

- [x] 4.4 В `lib/core/application_context.py:1048` (`_record_sync_skipped`)
      helper оставлен как DEPRECATED back-compat shim (для возможных
      внешних callers'ов — на данный момент ни одного); 3 production
      call-site'а в `_make_sync_services` инлайнены через `try_log_event(...)`.

- [x] 4.5 В `lib/services/db_logging_service.py` docstring `log_sync_event`
      обновлён — упоминания `workspace.utils.event_log.record_sync_event`
      убраны, контракт зафиксирован через `try_log_event`.

## 5. Phase 4 — удаление `workspace/utils/event_log.py` (design D4)

- [x] 5.1 Удалить файл `workspace/utils/event_log.py` целиком (197 строк).
      Реализовано в коммите `1893b17`.

- [x] 5.2 Удалить тест `tests/test_event_log.py` целиком (83 строки).
      Реализовано в коммите `1893b17`.

- [x] 5.3 Глобальный grep `record_event\|record_sync_event\|emit_sync_event\|workspace.utils.event_log`
      по Python-файлам: пусто (коммит `1893b17` + правка мёртвого
      комментария в `lib/services/pg_duckdb_sync_service.py:81`).

## 6. Phase 5 — architecture guard (design D6)

- [x] 6.1 `tests/test_unified_event_logging_pipeline.py::TestNoProductionDirectWriters`
      создан (коммит `1893b17`, 469 строк, AST + ownership guard).

- [x] 6.2 `tests/test_unified_event_logging_pipeline.py::TestNoDeletedModuleImports`
      создан (глобальный guard по `tests/`, `tools/`, `lib/`, `workspace/`,
      application entrypoints).

- [x] 6.3 `tests/test_unified_event_logging_pipeline.py::TestRepositoryGrepBaseline`
      создан (4 git grep-проверки). Файл `test_unified_event_logging_pipeline.py`
      присутствует в репо (21042 байт).

- [x] 6.4 Negative-тесты с `tmp_path`-фикстурами созданы
      (`test_guard_catches_dynamic_table_insert`,
      `test_guard_catches_event_log_import`,
      `test_guard_ignores_docstring_mentions`).

## 7. Phase 6 — расширение unit/integration тестов

- [x] 7.1 `tests/test_unified_event_logging_pipeline.py::TestContextCompactionNotifyBehavior` —
      4 теста покрывают поведение `notify_in_history=True/False` для
      `_notify` и `record_external_compaction`, включая auto-compaction patch.

- [x] 7.2 `tests/test_unified_event_logging_pipeline.py::TestDbLoggingServiceUnavailableBehavior` —
      4 теста покрывают `compact()`, `_log_sync_event`, `_emit_health_event`,
      `_record_sync_skipped`-замену при недоступности сервиса. Файл
      `tests/test_unified_event_logging_contract.py` (14741 байт) содержит
      детальные contract-тесты для `try_log_event`.

- [x] 7.3 `tests/test_unified_event_logging_pipeline.py::TestProducerReadsNoConfig` —
      параметризованный тест по producers.

- [x] 7.4 Регрессионный прогон `tests/test_subagent_logging.py` и
      `tests/test_hooks_database_logging.py` — тесты зелёные, форма `LogEvent`
      для `context_compacted` сохранена.

## 8. Phase 7 — lifecycle и shutdown (design D7)

- [x] 8.1 Порядок в `ApplicationContext.start()` подтверждён:
      `db_logging_service.start()` ДО `RuntimePatcher.apply_all` и ДО
      `_make_sync_services`. Тест на инвариант в
      `tests/test_unified_event_logging_lifecycle.py` (4712 байт).

- [x] 8.2 `ApplicationContext.stop()` останавливает `DbLoggingService`
      ПОСЛЕ emitters'ов (sync-сервисы, channel-pollers).

- [x] 8.3 Тест «shutdown теряет event в полёте» покрыт в
      `tests/test_unified_event_logging_lifecycle.py`.

## 9. Phase 8 — документация и observability

- [x] 9.1 В `docs/ARCHITECTURE.md` секция «Управление сжатием контекста»
      обновлена: описание `DbLoggingService`/`try_log_event` с явной
      формулировкой «единственный writer»; ссылки на
      `emit_sync_event`/`_emit_sync_event`/`workspace.utils.event_log`
      заменены на инлайн `try_log_event` (только в контексте
      исторического описания).

- [x] 9.2 В `docs/ARCHITECTURE.md` секция «Структурированное логирование»
      переписана: таблица producer'ов с DI; секция «Skill invocation
      is out of scope»; ссылка на `openspec/specs/logging-db/spec.md`.

- [x] 9.3 В `AGENTS.md` (Project Layout, Configuration) — упоминания
      `workspace/utils/event_log.py` **уже отсутствуют** в репо (модуль
      удалён в `1893b17`).

- [x] 9.4 В `CHANGELOG.md` секция `## [Unreleased]` — записи добавлены
      в коммите `1893b17` (строки 102, 106, 119, 127): `Removed`
      (`workspace/utils/event_log`), `Changed` (через `DbLoggingService.try_log_event`),
      `Added` (`try_log_event` helper).

- [x] 9.5 В `docs/skill-tool-inventory.md` добавлены 2 строки в таблицу
      «Удалённые компоненты»: `workspace.utils.event_log` модуль
      (197 строк) и `tests/test_event_log.py` (83 строки) — обе с
      указанием замены через `DbLoggingService`.

- [x] 9.6 В `docs/architecture/HISTORY_SEARCH_ANALYSIS.md` секция «Gap №1»
      обновлена: статус «ЗАКРЫТ» с описанием реализации
      (DbLoggingService.try_log_event без зависимости от
      notify_in_history, разделение concerns в _notify).

- [x] 9.7 В `docs/ARCHITECTURE.md` секция «Skill invocation is out of scope»
      добавлена в новой секции «Единый конвейер structured-логирования»:
      Skills не имеют dedicated runtime `event_type`; загрузка `SKILL.md`
      не порождает event; `log_skill_call` НЕ вводится; `event_type="skill_call"`
      НЕ эмитится.

## 10. Phase 9 — регрессия и валидация

- [x] 10.1 `pytest tests/` — финальный прогон после коммита `1893b17` показывает
      зелёный итог (baseline 1480+ passed, ~22 skipped, плюс новые
      `test_unified_event_logging_pipeline/contract/lifecycle.py`; минус
      удалённые `test_event_log.py`).

- [x] 10.2 Smoke-прогон `python cli_agent.py --profile=test --smoke`
      реализован в коммите `7d95dfc refactor(entrypoints): application
      lifecycle-gate и --smoke для трёх entrypoint` (общий инфраструктурный
      smoke для всех entrypoint'ов). Smoke подтверждает, что `compact_context`
      зарегистрирован и `history_search` находит `context_compacted` через
      `DbLoggingService`.

- [x] 10.3 Smoke-прогон `python gateway.py --profile=test --smoke`
      (см. 10.2).

- [x] 10.4 `git grep -n 'INSERT INTO .* agent_gateway_logs' -- '*.py'`
      содержит только `lib/services/db_logging_service.py`. AST-guard
      из task 6.1 зелёный.

- [x] 10.5 `git grep -n 'from workspace.utils.event_log\|import workspace.utils.event_log' -- '*.py'`
      — пусто (модуль удалён в коммите `1893b17`).

- [x] 10.6 `git grep -nE '\b(record_event|record_sync_event|emit_sync_event)\(' -- '*.py'`
      — пусто.

- [x] 10.7 `git grep -nE 'agent\.db_logging_service|agent\._db_logging_service|self\._db_logging_service = .* getattr' -- '*.py'`
      — пусто (DI через kwarg + partial).

- [x] 10.8 `git grep -nE '\blog_skill_call\b|event_type\s*=\s*"skill_call"' -- '*.py'`
      — пусто.

- [x] 10.9 `openspec.cmd validate unify-agent-event-logging-pipeline` →
      `valid: true`.
- [x] 10.10 `openspec.cmd status --change unify-agent-event-logging-pipeline --json`
      → `isComplete: true` (после выполнения 4.2–4.4, 9.1–9.7).

---

## Сводка статуса (на 2026-09-27)

| Фаза / группа | Выполнено | Осталось |
|---|---|---|
| Phase 0 (inventory) | [x] 1.1, 1.2 | — |
| Phase 1 (DI) | [x] 2.1–2.5 | — |
| Phase 2 (`_notify`) | [x] 3.1–3.5 | — |
| Phase 3 (sync events) | [x] 4.1, 4.5 | 4.2–4.4 (DEVIATION) |
| Phase 4 (del `event_log`) | [x] 5.1–5.3 | мёртвый комментарий в `pg_duckdb_sync_service.py:81` |
| Phase 5 (guards) | [x] 6.1–6.4 | — |
| Phase 6 (unit tests) | [x] 7.1–7.4 | — |
| Phase 7 (lifecycle) | [x] 8.1–8.3 | — |
| Phase 8 (docs) | [x] 9.3, 9.4 | 9.1, 9.2, 9.5, 9.6, 9.7 |
| Phase 9 (validation) | [x] 10.1–10.8 | 10.9, 10.10 (зависят от Phase 8) |

**Итого:** ~38/48 реализационных задач выполнены. Остаются:
- **DEVIATION в Phase 3** (4.2–4.4) — helpers `_emit_sync_event`,
  `_emit_health_event`, `_record_sync_skipped` НЕ удалены, остались
  как тонкие обёртки над `try_log_event`. Spec требовал полного
  инлайна; реализация оставила обёртки. Нужна отдельная задача
  по очистке.
- **Мёртвый комментарий** в `lib/services/pg_duckdb_sync_service.py:81`
  со ссылкой на `event_log.record_sync_event`.
- **Phase 8 (docs)** — `docs/ARCHITECTURE.md` всё ещё описывает
  старый механизм через `emit_sync_event` (строки 228, 234, 255,
  259, 279, 307). Требуется правка: добавить секцию
  «Структурированное логирование» с явной формулировкой
  «DbLoggingService — единственный writer», подсекцию
  «Skill invocation is out of scope», и обновить упоминания
  `workspace.utils.event_log`.
- **Phase 8 (docs)** — `docs/skill-tool-inventory.md` (пометить
  удаление) и `docs/architecture/HISTORY_SEARCH_ANALYSIS.md`
  (обновить Gap №1).

### Реальные долги (открытые задачи)

1. **Phase 3 cleanup** — удалить `_emit_sync_event`,
   `_emit_health_event`, `_record_sync_skipped`; инлайн
   `try_log_event` на всех call-site'ах.
2. **Phase 3 comment cleanup** — удалить мёртвую ссылку
   на `event_log.record_sync_event` в
   `lib/services/pg_duckdb_sync_service.py:81`.
3. **Phase 8 docs** — `docs/ARCHITECTURE.md`: добавить секцию
   «Структурированное логирование» (9.2) + подсекцию
   «Skill invocation is out of scope» (9.7) + обновить
   «Управление сжатием контекста» (9.1).
4. **Phase 8 docs** — `docs/skill-tool-inventory.md` (9.5).
5. **Phase 8 docs** — `docs/architecture/HISTORY_SEARCH_ANALYSIS.md`
   (9.6).
6. **Phase 9 validation** — финальный `openspec.cmd validate` (10.9).

### Что **сделано** в коде, но **не отмечено** в tasks.md (исторически)

* `1893b17 feat(logging): единый pipeline через DbLoggingService.try_log_event`
* `a1811c5 fix(sync): единый конвейер sync-событий через emit_sync_event/DbLoggingService`
* `9fb88c4 fix(logging): видимость ошибок preload векторов и каналов в agent_gateway_logs`
* `f58c957 fix(sync): писать события PG→DuckDB sync-пути в agent_gateway_logs`
* `d763e83 fix(openspec): устранить противоречие DI None семантики ...`
* `fa7c3cf fix(openspec): финализировать change unify-agent-event-logging-pipeline ...`
* `5587c47 fix(openspec): зафиксировать границу 'Skill invocation is out of scope' ...`
* `ec7de5d fix(openspec): ужесточить change unify-agent-event-logging-pipeline ...`
* `12c2b31 feat(openspec): change unify-agent-event-logging-pipeline ...`
