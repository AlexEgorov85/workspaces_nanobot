# Tasks — history_search observability improvements

## 1. Диагностика DbLoggingService

- [ ] 1.1 Добавить поле `_queued_at: float` в `LogEvent` (timestamp постановки в очередь) и счётчик `_written_by_type: dict[str, int]` в `DbLoggingService.__init__`; инициализировать в `self._stats["written_by_type"] = {}`; верификация: `python -c "from lib.services.db_logging_service import DbLoggingService; s = DbLoggingService(table_name='x', question_runs_table='y', dsn=''); assert s.get_stats()['written_by_type'] == {}"` не падает.

- [ ] 1.2 В `_enqueue` сохранять `event._queued_at = time.time()` и инкрементировать `self._stats["written_by_type"].setdefault(event.event_type, 0)` НЕЛЬЗЯ (событие ещё не записано); инкремент только после успешного `_flush_batch`; верификация: тест `tests/test_db_logging_service.py` проверяет, что счётчик растёт только после `_flush_batch`.

- [ ] 1.3 В `_flush_batch` после успешного `self._db_run(_work)` собрать `Counter` по `event_type` батча и прибавить к `self._stats["written_by_type"]` под `_state_lock`; верификация: тест с моком пула, в котором 5 `tool_call` и 3 `run_finished` прошли успешно → `get_stats()["written_by_type"] == {"tool_call": 5, "run_finished": 3}`.

- [ ] 1.4 Реализовать `oldest_queued_age_sec` в `get_stats()`: если `self._queue` — обычный `queue.Queue`, использовать `self._queue.queue[0]` для head; иначе вернуть `None`; верификация: тест ставит событие в очередь, ждёт 0.3 сек, проверяет что `0.25 <= get_stats()["oldest_queued_age_sec"] <= 0.5`.

- [ ] 1.5 Расширить `tests/test_db_logging_service.py` сценариями: «счётчик пуст на старте», «счётчик растёт после flush'а», «age равен None на пустой очереди», «age растёт со временем»; верификация: `pytest tests/test_db_logging_service.py -k "written_by_type or oldest_queued_age"` — все сценарии зелёные.

## 2. Конфиг flush_interval_sec

- [ ] 2.1 Добавить поле `flush_interval_sec: float = Field(default=5.0, ge=0.5, le=60.0)` в Pydantic-модель `LoggingDbSettings` (или эквивалент, в `lib/core/project_settings.py`); верификация: `python -c "from lib.core.project_settings import LoggingDbSettings; LoggingDbSettings(flush_interval_sec=0.1)"` бросает ValidationError.

- [ ] 2.2 В `lib/core/application_context.py` (или где собирается `DbLoggingService`) прокинуть `flush_interval_sec` из parsed settings в конструктор `DbLoggingService`; верификация: grep по коду показывает, что `flush_interval=` берётся из settings, а не хардкод `5.0`.

- [ ] 2.3 Добавить ключ `flush_interval_sec` в `tests/test_config_keys.py::OPTIONAL_KEYS` (или эквивалентный список опциональных ключей `logging.db`); верификация: `pytest tests/test_config_keys.py` — тест на дефолт и валидацию диапазона зелёный.

- [ ] 2.4 В `docs/CONFIGURATION.md` (или `AGENTS.md` секция Configuration) добавить описание `logging.db.flush_interval_sec` с дефолтом и границами; верификация: grep `flush_interval_sec` находит описание в документации.

## 3. history_search: offset и truncation-флаги

- [ ] 3.1 Добавить параметр `offset` в JSON-schema `history_search` (в `tool_parameters({...})`, рядом с `limit`) с типом `integer`, `minimum=0`, дефолт `0`; верификация: `python -c "from workspace.tools.history_search_tool import HistorySearchTool; ..."` (или юнит-тест) подтверждает наличие параметра в схеме.

- [ ] 3.2 В `execute()` добавить `offset: int | None = None`, в SQL заменить `LIMIT %s` на `LIMIT %s OFFSET %s`, передавать `int(offset or 0)`; верификация: юнит-тест с моком `utils.db.fetch` проверяет, что SQL содержит `OFFSET 10` при `offset=10`.

- [ ] 3.3 В JSON-ответе (`_render(...)`) добавить поле `results_truncated: bool` (уже вычисляется как `truncated`); добавить поле `truncated: bool` (deprecated алиас `results_truncated`) с явным комментарием в коде; верификация: тест проверяет, что при превышении `max_result_chars` ответ содержит оба поля с одинаковым значением.

- [ ] 3.4 В цикле truncation при `cap` halving добавить `event["payload_truncated"] = True` для каждого события, чей payload был обрезан; по дефолту (без обрезки) `payload_truncated: False`; верификация: тест с payload > `per_event_cap` проверяет `payload_truncated == True` на этом событии.

- [ ] 3.5 Расширить `tests/test_history_search_tool.py` сценариями: «offset=10 пропускает 10 событий», «offset=0 равен текущему поведению», «payload_truncated=true при обрезке», «results_truncated=true при выбросе события», «truncated (deprecated) равен results_truncated»; верификация: `pytest tests/test_history_search_tool.py` — все новые сценарии зелёные.

## 4. Документация payload по event_type

- [ ] 4.1 В `workspace/TOOLS.md` секцию `history_search` добавить подсекцию «Структура payload по event_type» с примерами JSON для `tool_call`, `tool_result`, `llm_call`, `run_finished`, `subagent_run_finished`, `inbound`, `context_compacted`; верификация: grep по `event_type` находит каждый тип с блоком кода JSON.

- [ ] 4.2 В `workspace/TOOLS.md` пометить поле `truncated` как deprecated, указав, что нужно использовать `results_truncated`/`payload_truncated`, и что алиас будет удалён в следующем релизе; верификация: grep `deprecated` находит упоминание `truncated`.

- [ ] 4.3 Сверить `workspace/TOOLS.md` с фактической структурой payload'ов: открыть `lib/services/db_logging_service.py` (методы `log_inbound`, `log_tool_call`, `log_tool_result`, `log_llm_call`), `lib/hooks/database_logging_hook.py` (`_make_run_event`), `lib/services/context_compaction.py` (структура для `context_compacted`), `lib/services/runtime_patcher.py` (`subagent_run_finished`); верификация: каждый тип из TOOLS.md имеет источник в коде, расхождений нет.

- [ ] 4.4 В `docs/ARCHITECTURE.md` (если есть секция про `history_search`) обновить упоминания `truncated` на `results_truncated`; верификация: grep `truncated` в `docs/` находит только документированные ссылки на новые имена полей.

## 5. Регрессия и валидация

- [ ] 5.1 Добавить интеграционный тест в `tests/test_hooks_database_logging.py`: хук `DatabaseLoggingHook` запускается с фейковым контекстом `after_run`, проверяется что в `db_logging_service.get_stats()["written_by_type"]["run_finished"]` есть приращение; верификация: `pytest tests/test_hooks_database_logging.py -k run_finished` зелёный.

- [ ] 5.2 Добавить интеграционный тест для subagent-логирования (`tests/test_runtime_patcher.py` или новый `test_subagent_logging.py`): `_SubagentLoggingHook` финализируется, проверяется `written_by_type["subagent_run_finished"]` инкремент; верификация: тест зелёный.

- [ ] 5.3 В `CHANGELOG.md` секция `[Unreleased]` → категория `Changed`: запись «history_search: добавлен параметр offset; поле truncated помечено deprecated в пользу results_truncated/payload_truncated; db_logging_service: счётчики written_by_type и oldest_queued_age_sec в get_stats; logging.db.flush_interval_sec вынесен в config.json»; верификация: grep `offset` и `flush_interval_sec` находит запись в CHANGELOG.

- [ ] 5.4 Прогнать `openspec.cmd validate remove-run-finished-from-history-search`; верификация: статус «passed», без ошибок.

- [ ] 5.5 Прогнать `pytest tests/`; верификация: все тесты зелёные (без регрессий).

- [ ] 5.6 Smoke-прогон `python cli_agent.py --profile=test --help` (или эквивалент, проверить что gateway собирается без ошибок и `tools.history_search` присутствует в реестре); верификация: процесс не падает на старте, в логе нет traceback.
