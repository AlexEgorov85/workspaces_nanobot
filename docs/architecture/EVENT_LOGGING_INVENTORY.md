# EVENT_LOGGING_INVENTORY

Список **всех** мест в runtime-коде, где происходит запись в `agent_gateway_logs`
или `agent_question_runs` (baseline Phase 0 для change
`unify-agent-event-logging-pipeline`).

Каждая строка: writer / текущий путь / целевой путь / risk (fallback yes/no).

## Writers

| # | Writer | Текущий путь | Целевой путь | Fallback на `event_log`? |
|---|---|---|---|---|
| 1 | `lib/services/db_logging_service.py` (`DbLoggingService`) | прямой INSERT в `agent_gateway_logs` через `utils.db.run` | **остаётся каноном** | — |
| 2 | `lib/services/db_logging_service.py` (`DbLoggingService.upsert_question_run`) | прямой upsert в `agent_question_runs` | **остаётся каноном** | — |
| 3 | `workspace/utils/event_log.py` (`record_event`) | прямой INSERT в `agent_gateway_logs` через `utils.db.execute` | **УДАЛИТЬ** | сам и есть fallback |
| 4 | `workspace/utils/event_log.py` (`record_sync_event`) | тонкая обёртка над `record_event` для sync-пути | **УДАЛИТЬ** | — |
| 5 | `workspace/utils/event_log.py` (`emit_sync_event`) | dual-sink: `DbLoggingService` если есть, иначе `record_sync_event` | **УДАЛИТЬ** (заменить на `DbLoggingService.try_log_event`) | — |
| 6 | `lib/services/context_compaction.py` (`ContextCompactionService._record_event_log`) | `asyncio.to_thread(record_event, ...)` | `DbLoggingService.try_log_event(...)` (через `_notify`, всегда при `enabled`) | да |
| 7 | `lib/services/pg_duckdb_sync_service.py` (`_log_sync_event`) | `emit_sync_event(..., service=self._db_logging_service)` | `DbLoggingService.try_log_event(self._db_logging_service, log_event, producer="PgDuckDbSyncService", event_type=...)` | да |
| 8 | `lib/services/duckdb_cache_store.py` (`_emit_sync_event`) | `emit_sync_event(..., service=self._db_logging_service)` | `DbLoggingService.try_log_event(self._db_logging_service, log_event, producer="DuckDbCacheStore", event_type=...)`; helper `_emit_sync_event` УДАЛИТЬ | да |
| 9 | `lib/services/preload_service.py` (`_emit_health_event`) | `emit_sync_event(..., service=...)` | `DbLoggingService.try_log_event(self._db_logging_service, log_event, producer="PreloadService", event_type="vector_index_preload_health")` | да |
| 10 | `lib/core/application_context.py` (`_record_sync_skipped`) | `record_sync_event(...)` для трёх `sync_skipped_*` событий | удалить функцию; 3+ call-site'а переписать на `DbLoggingService.try_log_event(self.db_logging_service, log_event, producer="ApplicationContext", event_type=...)` | да |

## Проверка baseline

```bash
git grep -n 'agent_gateway_logs\|agent_question_runs' -- '*.py' | sort -u > /tmp/baseline.txt
```

Все 10 строк покрывают наблюдаемый baseline; необъяснённых мест в
runtime-коде (`lib/`, `workspace/`, `tools/`) нет. Упоминания в `tests/`
(фикстуры), `sql/`, `tools/generate_comments_sql.py`, `tools/release_v252.py`,
`workspace/tools/history_search_tool.py`, `config.py` — не writers, либо
read-only (schema/DDL/comments).

## Документы / changelog (не writers, упоминания)

* `tools/release_v252.py` — историческая фиксация прошлого релиза (`emit_sync_event`/`DbLoggingService`); оставить как есть.
* `tools/generate_comments_sql.py` — генератор SQL-комментариев; имена таблиц как строки в словарях; не writers.
* `workspace/tools/history_search_tool.py` — reader по `agent_gateway_logs` (не writer).
* `config.py` — имена таблиц по умолчанию; не writers.

## Целевой контракт (после change)

* **Единственный writer** в `agent_gateway_logs` / `agent_question_runs` —
  `lib/services/db_logging_service.py` (`DbLoggingService`).
* Все producer'ы передают события через:
  * `db_logging_service.log_event(LogEvent(...))` — happy path (sync producer, async caller);
  * `DbLoggingService.try_log_event(svc, log_event, *, producer, event_type) -> bool`
    — defensive helper: WARN-уровень при `svc is None` / `not svc.is_running()`,
    возврат `False` для бизнеса (no-op for business).
* `workspace/utils/event_log.py` удалён целиком (модуль-fallback ликвидирован).
* AST-guards в `tests/test_unified_event_logging_pipeline.py` ловят любые
  попытки прямого INSERT или import'а удалённого модуля.