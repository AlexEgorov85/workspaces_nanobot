# Tasks — improve history_search pagination and logging observability

> **Статус:** все задачи выполнены. Реализация закрыта коммитами
> `f2d92f6` (основная) и последующим follow-up (дефолт `flush_interval_sec`
> в `LoggingDbSettings`, runtime-тест передачи настройки через
> `ApplicationContext`, исправление `_render()` для учёта
> `payload_truncated` в финальном JSON, wiring-тест `_SubagentLoggingHook`).

## 1. Диагностика DbLoggingService (счётчики и метрики)

- [x] 1.1 Добавить поле `queued_at: float | None = None` в dataclass `LogEvent` (`lib/services/db_logging_service.py`); инициализировать счётчик `self._stats["written_by_type"]: dict[str, int] = {}` в `DbLoggingService.__init__`; **верификация**: `python -c "from lib.services.db_logging_service import DbLoggingService, LogEvent; le = LogEvent(event_type='x'); assert le.queued_at is None; s = DbLoggingService(table_name='x', question_runs_table='y', dsn=''); assert s.get_stats()['written_by_type'] == {}"` не падает.

- [x] 1.2 В `_enqueue` (`lib/services/db_logging_service.py`) сохранять `event.queued_at = time.time()` **до** постановки в очередь; счётчик `written_by_type` НЕ инкрементировать; **верификация**: `tests/test_db_logging_service.py::TestWrittenByType::test_enqueue_does_not_increment_written_by_type` — после `log_event(...)` счётчик `written_by_type` пуст, `le.queued_at ≈ time.time()`.

- [x] 1.3 В `_flush_batch` после успешного `self._db_run(_work)` собрать `Counter(e.event_type for e in batch)` через module-level helper `_count_by_type` и под `_state_lock` прибавить к `self._stats["written_by_type"]`; при исключении — НЕ инкрементировать; **верификация**: тесты `test_written_by_type_grows_after_flush` (5 `tool_call` + 3 `run_finished` → `{"tool_call": 5, "run_finished": 3}`), `test_written_by_type_does_not_grow_on_flush_failure`.

- [x] 1.4 Реализовать `oldest_queued_age_sec` в `get_stats()`: вычислить `max(time.time() - event.queued_at for event in self._queue.queue if isinstance(event, LogEvent) and event.queued_at is not None)`; если `LogEvent` в очереди нет — `None`. Спека и design фиксируют `max` (самый старый = самый большой возраст); исходный текст task ошибочно упоминает `min`, реализация следует design D6; **верификация**: тесты `TestOldestQueuedAge::test_oldest_queued_age_returns_max_age`, `test_oldest_queued_age_only_counts_log_events`, `test_oldest_queued_age_none_when_empty`, `test_oldest_queued_age_ignores_records_without_queued_at`.

- [x] 1.5 Покрыть `tests/test_db_logging_service.py` сценариями: «written_by_type пуст на старте», «written_by_type растёт после flush'а», «written_by_type не растёт при ошибке flush'а», «written_by_type НЕ сбрасывается при повторном start()», «oldest_queued_age_sec учитывает только LogEvent», «oldest_queued_age_sec=None на пустой очереди»; **верификация**: `pytest tests/test_db_logging_service.py -k "written_by_type or oldest_queued_age"` — все 9 сценариев зелёные.

## 2. Конфигурация flush_interval_sec

- [x] 2.1 В `lib/core/project_settings.py` добавить поле `flush_interval_sec` в `LoggingDbSettings` (диапазон `0.5 ≤ value ≤ 60.0`); канонический дефолт `5.0` подставляется через `model_validator(mode="after")._default_flush_interval_sec` — pydantic `default=None` оставляет поле опциональным по JSON, а валидатор приводит к `5.0` после валидации диапазона. Это позволяет типизированной конфигурации быть источником default-value без хардкода в `ApplicationContext`; **верификация**: `LoggingDbSettings(flush_interval_sec=0.1)` бросает `pydantic.ValidationError`; `LoggingDbSettings().flush_interval_sec == 5.0`; `LoggingDbSettings(flush_interval_sec=2.0).flush_interval_sec == 2.0`.

- [x] 2.2 В `lib/core/application_context.py` (`_make_db_logging`) пробрасывается `flush_interval_sec` из `ctx.project_settings.logging.db.flush_interval_sec` в конструктор `DbLoggingService`; **верификация**: runtime-тесты `tests/test_application_context_logging.py::TestFlushIntervalSecPropagation` — `test_db_logging_service_receives_flush_interval_from_settings` (значение `2.0` доходит до `ctx.db_logging_service._flush_interval`), `test_db_logging_service_uses_default_when_absent` (без ключа → `5.0`), `test_out_of_range_value_rejected_by_pydantic` (`0.1` → `ConfigurationError`).

- [x] 2.3 Ключ `flush_interval_sec` присутствует в `_required_keys()` `tests/test_config_keys.py` (подсекция `logging.db`) и валидация диапазона/дефолта покрыта в `TestLoggingDbFlushIntervalValidation` (`test_default_is_five`, `test_in_range`, `test_below_minimum_raises`, `test_above_maximum_raises`); **верификация**: `pytest tests/test_config_keys.py -k flush_interval` зелёный.

- [x] 2.4 В `AGENTS.md` (секция Configuration, после `logging.db.purge_interval_sec`) добавлено описание `logging.db.flush_interval_sec` с дефолтом `5.0`, диапазоном `0.5 ≤ value ≤ 60.0` и ссылкой на `lib/core/project_settings.py`; **верификация**: `grep -n "flush_interval_sec" AGENTS.md` находит строку 78.

## 3. history_search: пагинация, has_more, truncation-флаги

- [x] 3.1 В JSON-schema `history_search` (`tool_parameters({...})`) добавлен параметр `offset` с типом `integer`, `minimum=0`, дефолт `0`; **верификация**: `tests/test_history_search_tool.py::TestPagination::test_schema_includes_offset_with_default_zero`.

- [x] 3.2 В `execute()` добавлен `offset: int | None = None`; SQL: `ORDER BY "timestamp" DESC, "id" DESC LIMIT %s OFFSET %s`, где параметры = `(effective_limit + 1, int(offset or 0))`; **верификация**: `test_sql_uses_limit_offset_and_deterministic_order`, `test_offset_zero_equivalent_to_default_behavior`, `test_offset_with_offset_arg` (через `next_offset`).

- [x] 3.3 В `execute()` реализовано двухфазное вычисление `has_more`: фаза 1 до truncation — `db_has_more = (len(rows_from_db) > effective_limit)`, лишняя строка отбрасывается; фаза 2 после truncation — `has_more = db_has_more OR results_truncated`; **верификация**: `test_has_more_true_when_more_rows_in_db` (25 строк → `has_more=true`), `test_has_more_false_on_last_page` (последняя страница), `test_has_more_true_when_results_truncated_even_without_db_more` (регрессия: ровно 10 строк в БД, truncation выбросил 6 → `has_more=true`).

- [x] 3.4 В JSON-ответе добавлено поле `results_truncated: bool`; сохранён deprecated алиас `truncated: bool` со значением `results_truncated`; **верификация**: `test_results_truncated_and_deprecated_alias`, `test_truncated_alias_equals_results_truncated`.

- [x] 3.5 В цикле truncation (первичный по `per_event_cap` + вторичный через `cap //= 2` при срабатывании `max_result_chars`) для каждого события, чей payload был обрезан хотя бы раз, устанавливается `payload_truncated=True`; для остальных `False`; **ЗАПРЕЩЕНО** устанавливать `results_truncated=true` только потому, что payload был ужат; **верификация**: `test_payload_truncated_per_event`, `test_results_and_payload_independent`, `test_payload_truncated_via_max_result_chars_second_pass` (регрессия на второй проход `cap //= 2`).

- [x] 3.6 В JSON-ответе добавлено поле `next_offset: int` со значением `int(original_offset) + count` после всех truncation-проходов; **верификация**: `test_next_offset_no_truncation`, `test_next_offset_after_truncation`, `test_next_offset_with_offset_arg`.

- [x] 3.7 Покрыты сценарии из спеки: «offset пропускает строки», «offset=0 = текущее поведение», «has_more на наличии следующей страницы», «has_more на последней странице», «LIMIT N+1 запрашивает на 1 строку больше», «next_offset = offset + count без truncation», «next_offset = offset + count при results_truncated=true», «has_more=true при results_truncated=true даже когда db_has_more=false (регрессионный сценарий)», «payload_truncated при обрезке», «results_truncated при выбросе», «truncated равен results_truncated», «ORDER BY содержит id DESC», «пустой результат возвращает has_more=false и next_offset=offset» — классы `TestPagination` (10 тестов), `TestTruncationFlags` (6 тестов), `TestSnapshotConsistency` (1 тест). Дополнительно — `test_final_json_respects_max_result_chars` (регрессия на пункт 5 review: финальный JSON учитывает `payload_truncated`); **верификация**: `pytest tests/test_history_search_tool.py` — 25 тестов зелёные + 1 xpassed (pre-existing flake).

- [x] 3.8 Сценарий snapshot-неконсистентности: при INSERT'е новых событий между запросами `offset`-пагинация может сдвинуться — задокументировано в `tools/TOOLS.md` («`offset`-пагинация **не snapshot-consistent**») и покрыто тестом `test_pagination_not_snapshot_consistent`; **верификация**: тест фиксирует, что новые строки попадают в начало выборки.

## 4. Документация payload и deprecated alias

- [x] 4.1 В `workspace/TOOLS.md` секция `history_search` дополнена подсекцией «Структура payload по event_type» с примерами JSON для `tool_call`, `tool_result`, `llm_call`, `run_finished`, `subagent_run_finished`, `inbound`, `context_compacted`. Для `tool_result.result` явно отмечено «хранится как JSON-string (нужен `json.loads`)». Для `context_compacted` — пометка «snapshot текущей реализации `ContextCompactionService._notify`, изменение требует отдельного change»; **верификация**: каждый тип присутствует в файле.

- [x] 4.2 В `workspace/TOOLS.md` поле `truncated` помечено deprecated: «**deprecated** алиас `results_truncated`. Сохранён ради совместимости; удаляется в отдельном follow-up change»; **верификация**: `grep -B1 -A1 "deprecated" workspace/TOOLS.md` находит упоминание.

- [x] 4.3 Схемы `payload` в `workspace/TOOLS.md` сверены с фактической реализацией: `lib/services/db_logging_service.py:log_inbound`, `log_tool_call`, `log_tool_result`, `log_llm_call`; `lib/hooks/database_logging_hook.py:_make_run_event`; `lib/services/runtime_patcher.py:_SubagentLoggingHook._finalize` (subagent); `lib/services/context_compaction.py:_record_event_log` (context_compacted). Расхождений нет; `tool_result.payload.result` описан как JSON-string (сериализуется через `psycopg2.extras.Json`).

## 5. Регрессия и валидация

- [x] 5.1 Добавлен класс `TestRunFinishedEventShape` в `tests/test_hooks_database_logging.py`: `DatabaseLoggingHook.after_run` вызывается с фейковым `ctx` (`final_content`, `tools_used`, `stop_reason`, `had_injections`, `usage`) — проверяется, что эмиттированный `LogEvent` имеет `event_type="run_finished"` и payload содержит ожидаемые поля; **верификация**: `pytest tests/test_hooks_database_logging.py::TestRunFinishedEventShape` зелёный.

- [x] 5.2 Создан `tests/test_subagent_logging.py` с двумя слоями покрытия: (1) форма события `subagent_run_finished` (payload, session_id, channel); (2) wiring-тест через реальный патчер `RuntimePatcher.patch_subagent_logging` + `_SubagentLoggingHook.after_run(ctx)` — финализация эмиттирует `LogEvent` с правильным `event_type` и характерным payload (`task_id`, `task`, `parent_request_id`, `final_content`, `tools_used`, `stop_reason`). Также подтверждён рост `written_by_type["subagent_run_finished"]` после flush'а; **верификация**: 3 теста зелёные.

- [x] 5.3 В `CHANGELOG.md` секция `[Unreleased]` → категория `Changed`: запись о `offset`/`has_more`/`next_offset`, deprecated `truncated`, разделении `results_truncated`/`payload_truncated`, детерминированной сортировке, `written_by_type`/`oldest_queued_age_sec`, `logging.db.flush_interval_sec`; **верификация**: `grep -n "offset\|has_more\|next_offset\|written_by_type\|flush_interval_sec" CHANGELOG.md` находит записи в `[Unreleased]`.

- [x] 5.4 `openspec.cmd validate improve-history-search-pagination-and-logging` → статус «passed», 0 issues.

- [x] 5.5 `pytest tests/` — все тесты зелёные (2240 passed, 16 skipped, 1 xfailed — pre-existing flake в `test_history_search_tool.py::test_search_current_session_filters_by_session`, не связан с change).

- [x] 5.6 Smoke-прогон `python cli_agent.py --profile=test --smoke` → `OK_SMOKE_COMPLETE`, `tools.history_search` присутствует в реестре tools (`Registered 18 tools: ... history_search, ...`).
