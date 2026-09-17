# Tasks — improve history_search pagination and logging observability

## 1. Диагностика DbLoggingService (счётчики и метрики)

- [ ] 1.1 Добавить поле `queued_at: float | None = None` в dataclass `LogEvent` (`lib/services/db_logging_service.py`); инициализировать счётчик `self._stats["written_by_type"]: dict[str, int] = {}` в `DbLoggingService.__init__`; **верификация**: `python -c "from lib.services.db_logging_service import DbLoggingService, LogEvent; le = LogEvent(event_type='x'); assert le.queued_at is None; s = DbLoggingService(table_name='x', question_runs_table='y', dsn=''); assert s.get_stats()['written_by_type'] == {}"` не падает.

- [ ] 1.2 В `_enqueue` (`lib/services/db_logging_service.py`) сохранять `event.queued_at = time.time()` **до** постановки в очередь; счётчик `written_by_type` НЕ инкрементировать; **верификация**: `tests/test_db_logging_service.py::test_enqueue_sets_queued_at_does_not_increment_written_by_type` — после `log_event(...)` счётчик `written_by_type` пуст, `le.queued_at ≈ time.time()`.

- [ ] 1.3 В `_flush_batch` после успешного `self._db_run(_work)` собрать `Counter(e.event_type for e in batch)` и под `_state_lock` прибавить к `self._stats["written_by_type"]`; при исключении — НЕ инкрементировать; **верификация**: тест с моком пула, в котором 5 `tool_call` и 3 `run_finished` прошли успешно → `get_stats()["written_by_type"] == {"tool_call": 5, "run_finished": 3}`; отдельный тест с падением flush'а → `written_by_type` не изменился, `failed` инкрементирован.

- [ ] 1.4 Реализовать `oldest_queued_age_sec` в `get_stats()`: вычислить `min(time.time() - event.queued_at for event in self._queue.queue if isinstance(event, LogEvent) and event.queued_at is not None)`; если `LogEvent` в очереди нет — `None`; **верификация**: тест ставит 2 `LogEvent` с разным возрастом (0.3s и 0.1s) + 1 `_QuestionRunRecord`; `get_stats()["oldest_queued_age_sec"] ≈ 0.3` (минимум, не первый); тест с очередью только из `_QuestionRunRecord` → `None`; тест с пустой очередью → `None`.

- [ ] 1.5 Покрыть `tests/test_db_logging_service.py` сценариями: «written_by_type пуст на старте», «written_by_type растёт после flush'а», «written_by_type не растёт при ошибке flush'а», «written_by_type НЕ сбрасывается при повторном start()», «oldest_queued_age_sec учитывает только LogEvent», «oldest_queued_age_sec=None на пустой очереди»; **верификация**: `pytest tests/test_db_logging_service.py -k "written_by_type or oldest_queued_age"` — все сценарии зелёные.

## 2. Конфигурация flush_interval_sec

- [ ] 2.1 В `lib/core/project_settings.py` добавить поле `flush_interval_sec: float = Field(default=5.0, ge=0.5, le=60.0)` в `LoggingDbSettings`; **верификация**: `python -c "from lib.core.project_settings import LoggingDbSettings; LoggingDbSettings(flush_interval_sec=0.1)"` бросает `pydantic.ValidationError`; `LoggingDbSettings(flush_interval_sec=1.0).flush_interval_sec == 1.0`; `LoggingDbSettings().flush_interval_sec == 5.0`.

- [ ] 2.2 В `lib/core/application_context.py` (или эквивалентном месте сборки `DbLoggingService`) добавить передачу `flush_interval_sec=logging_db.flush_interval_sec` в конструктор; **верификация**: runtime-тест `tests/test_application_context_logging.py::test_db_logging_service_receives_flush_interval_from_settings` собирает `ApplicationContext` с `SETTINGS["logging"]["db"]["flush_interval_sec"] = 2.0` и проверяет `assert ctx.db_logging_service._flush_interval == 2.0`.

- [ ] 2.3 Добавить ключ `flush_interval_sec` в `tests/test_config_keys.py` (OPTIONAL_KEYS или эквивалентный реестр опциональных ключей `logging.db`); покрыть тестами валидацию диапазона (0.5–60.0) и дефолт; **верификация**: `pytest tests/test_config_keys.py -k flush_interval` зелёный.

- [ ] 2.4 В `AGENTS.md` (секция Configuration) добавить описание `logging.db.flush_interval_sec` с дефолтом `5.0`, диапазоном и ссылкой на `lib/core/project_settings.py`; **верификация**: `grep -n "flush_interval_sec" AGENTS.md` находит строку с диапазоном и дефолтом.

## 3. history_search: пагинация, has_more, truncation-флаги

- [ ] 3.1 В JSON-schema `history_search` (в `tool_parameters({...})`, рядом с `limit`) добавить параметр `offset` с типом `integer`, `minimum=0`, дефолт `0`; **верификация**: тест `tests/test_history_search_tool.py::test_schema_includes_offset_with_default_zero` проверяет наличие параметра и дефолт.

- [ ] 3.2 В `execute()` добавить `offset: int | None = None`; заменить SQL на `ORDER BY "timestamp" DESC, "id" DESC LIMIT %s OFFSET %s`, где первый параметр = `effective_limit + 1`, второй = `int(offset or 0)`; **верификация**: тест с моком `utils.db.fetch` проверяет, что SQL содержит `ORDER BY "timestamp" DESC, "id" DESC`, `LIMIT %s OFFSET %s`, и что в вызов передаются `(effective_limit + 1, offset)`.

- [ ] 3.3 В `execute()` реализовать двухфазное вычисление `has_more`: **фаза 1 (до truncation)** — `db_has_more = (len(rows_from_db) > effective_limit)`; если `db_has_more`, отбросить лишнюю строку (`rows_from_db = rows_from_db[:effective_limit]`); **фаза 2 (после всех truncation-проходов)** — `has_more = db_has_more OR results_truncated`; **ЗАПРЕЩЕНО** вычислять финальный `has_more` до truncation (это даёт `has_more=false` при `results_truncated=true` и приводит к потере событий); добавить `has_more` в JSON-ответ; **верификация**: тест с 25 событиями в БД, `limit=10, offset=0` → `has_more=true, count=10`; с `limit=10, offset=20` → `has_more=false, count=5`; регрессионный тест с 10 событиями в БД, `limit=10`, `max_result_chars` сокращает до 4 → `has_more=true, count=4` (см. task 3.7).

- [ ] 3.4 В JSON-ответе добавить поле `results_truncated: bool` (на ответе) — текущий `truncated`; добавить deprecated алиас `truncated: bool` со значением `results_truncated`; **верификация**: тест `tests/test_history_search_tool.py::test_results_truncated_and_deprecated_alias` — при превышении `max_result_chars` оба поля равны `true`; при отсутствии обрезки оба равны `false`.

- [ ] 3.5 В цикле truncation (включая **оба** прохода: первичный по `per_event_cap` и вторичный при срабатывании `max_result_chars` через `cap //= 2`) для каждого события, чей payload был обрезан хотя бы раз, устанавливать `event["payload_truncated"] = True`; для остальных `event["payload_truncated"] = False`; **ЗАПРЕЩЕНО** устанавливать `results_truncated=true` только потому, что payload был ужат (это отдельный механизм); **верификация**: тест с payload > `per_event_cap` и влезающим в `max_result_chars` → `results_truncated=false`, `payload_truncated=true` на этом событии, `payload_truncated=false` на остальных; **отдельный регрессионный тест** `test_payload_truncated_via_max_result_chars` — одно событие с payload > `per_event_cap`, `max_result_chars` настолько мал, что срабатывает второй проход (`cap //= 2`) → `payload_truncated=true`, `results_truncated=false`, событие осталось в ответе (не выброшено).

- [ ] 3.6 В JSON-ответе добавить поле `next_offset: int` (≥ 0) со значением `int(original_offset) + count` после всех truncation-проходов; **верификация**: тест `test_next_offset_after_truncation` — `offset=0, limit=10, max_result_chars` мал, `count=4` после truncation → `next_offset=4`; тест `test_next_offset_no_truncation` — `offset=0, limit=10`, `count=10` → `next_offset=10`; тест `test_next_offset_with_offset_arg` — `offset=10, count=4` → `next_offset=14`.

- [ ] 3.7 Покрыть `tests/test_history_search_tool.py` сценариями: «offset=10 пропускает 10 строк», «offset=0 равен текущему поведению», «has_more=true при наличии следующей страницы», «has_more=false на последней странице», «LIMIT N+1 запрашивается на 1 строку больше effective_limit», «next_offset = offset + count без truncation», «next_offset = offset + count при results_truncated=true (продолжение после отброшенных событий)», «has_more=true при results_truncated=true даже когда db_has_more=false (регрессионный сценарий: ровно `effective_limit` событий в БД, truncation выбросил часть — агент НЕ должен получить `has_more=false`)», «payload_truncated=true при обрезке payload», «results_truncated=true при выбросе события», «truncated (deprecated) равен results_truncated», «ORDER BY содержит id DESC», «пустой результат возвращает has_more=false и next_offset=offset» (см. scenarios в спеке); **верификация**: `pytest tests/test_history_search_tool.py` — все новые и существующие сценарии зелёные, включая регрессионный тест на композитную формулу `has_more = db_has_more OR results_truncated`.

- [ ] 3.8 Добавить сценарий для snapshot-неконсистентности: при INSERT'е новых событий между запросами `offset`-пагинация может сдвинуться; **верификация**: тест `test_pagination_not_snapshot_consistent` имитирует INSERT нового события между двумя вызовами с `offset=0` и `offset=10`; тест фиксирует, что новые строки попадают в начало выборки (сценарий документирует ограничение, не assertion на конкретные значения).

## 4. Документация payload и deprecated alias

- [ ] 4.1 В `workspace/TOOLS.md` секцию `history_search` добавить подсекцию «Структура payload по event_type» с примерами JSON для `tool_call`, `tool_result`, `llm_call`, `run_finished`, `subagent_run_finished`, `inbound`, `context_compacted`; для `tool_result` явно отметить, что `payload.result` сериализуется как JSON-string; для `context_compacted` пометить как «snapshot текущей реализации `ContextCompactionService._notify`, изменение требует отдельного change»; **верификация**: `grep -c "event_type" workspace/TOOLS.md` находит каждый тип с блоком кода JSON.

- [ ] 4.2 В `workspace/TOOLS.md` пометить поле `truncated` как deprecated, указав, что нужно использовать `results_truncated`/`payload_truncated`, и что алиас будет удалён в отдельном follow-up change (без указания конкретного релиза); **верификация**: `grep -B1 -A1 "deprecated" workspace/TOOLS.md` находит упоминание `truncated`.

- [ ] 4.3 Сверить схемы `payload` в `workspace/TOOLS.md` с фактической реализацией: открыть `lib/services/db_logging_service.py` (методы `log_inbound`, `log_tool_call`, `log_tool_result`, `log_llm_call`), `lib/hooks/database_logging_hook.py` (`_make_run_event`), `lib/services/runtime_patcher.py` (`subagent_run_finished`), `lib/services/context_compaction.py` (`_notify`); **верификация**: для каждого типа из TOOLS.md есть source-of-truth строка в коде; расхождений нет; если найдено расхождение — правка идёт в том же изменении.

## 5. Регрессия и валидация

- [ ] 5.1 Добавить интеграционный тест в `tests/test_hooks_database_logging.py`: хук `DatabaseLoggingHook` запускается с фейковым контекстом `after_run` (с `final_content`, `tools_used`, `stop_reason`, `usage`); после `flush` проверить, что `db_logging_service.get_stats()["written_by_type"]["run_finished"] >= 1`; **верификация**: `pytest tests/test_hooks_database_logging.py -k run_finished` зелёный.

- [ ] 5.2 Добавить тест для subagent-логирования в `tests/test_runtime_patcher.py` или новый `tests/test_subagent_logging.py`: `_SubagentLoggingHook.after_run` финализируется; после `flush` проверить, что `written_by_type["subagent_run_finished"] >= 1`; **верификация**: тест зелёный.

- [ ] 5.3 В `CHANGELOG.md` секция `[Unreleased]` → категория `Changed`: запись «history_search: добавлены параметр offset, поля has_more и next_offset; поле truncated помечено deprecated в пользу results_truncated/payload_truncated; SQL ORDER BY теперь детерминирован (timestamp DESC, id DESC); db_logging_service: счётчики written_by_type и oldest_queued_age_sec в get_stats; logging.db.flush_interval_sec добавлен в типизированную конфигурацию»; **верификация**: `grep -n "offset\|has_more\|next_offset\|written_by_type\|flush_interval_sec" CHANGELOG.md` находит запись в секции Unreleased.

- [ ] 5.4 Прогнать `openspec.cmd validate improve-history-search-pagination-and-logging`; **верификация**: статус «passed», 0 issues.

- [ ] 5.5 Прогнать `pytest tests/`; **верификация**: все тесты зелёные (без регрессий).

- [ ] 5.6 Smoke-прогон `python cli_agent.py --profile=test --help` или эквивалентный способ убедиться, что gateway собирается без ошибок и `tools.history_search` присутствует в реестре tools; **верификация**: процесс не падает на старте, в логе нет traceback.
