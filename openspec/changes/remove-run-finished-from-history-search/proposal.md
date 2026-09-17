# Remove run-finished from history search / Improve observability of durable event log

## Why

Пользовательская оценка инструмента `history_search` (см. внутренний
отчёт «Честная оценка `history_search`») выявила четыре связанные проблемы
в durability и observability журнала `agent_gateway_logs`, через который
агент «вспоминает» детали, выпавшие из контекста после `context_compacted`:

1. **Расхождение документации и наблюдаемого поведения.** Enum
   `event_type` в tool `history_search` объявляет `run_finished` и
   `subagent_run_finished`, но в живой истории конкретного пользователя
   эти типы возвращают `count: 0`. При этом код записи **существует**
   (`DatabaseLoggingHook.after_run` в `lib/hooks/database_logging_hook.py:402-419`,
   `_make_run_event` в `:422-453`; `runtime_patcher.patch_subagent_logging`
   в `lib/services/runtime_patcher.py:1370-1398`). Гипотеза «кода нет»
   не подтвердилась — значит, события теряются по дороге
   (отключён хук/patch, не передан `db_logging_service`, падение в
   `except` без логирования), и без диагностики это не восстановить.
2. **Семантика `truncated` смешивает два разных truncation-механизма.**
   В текущем `history_search_tool.py:343-362` один булев флаг `truncated`
   покрывает и «итоговый JSON обрезан по символам», и «выпали целые
   события из выборки». Это дезинформирует агента: `truncated: true` не
   означает «есть ещё результаты в БД».
3. **Нет `offset`/cursor при пагинации.** `history_search_tool.py:50-139`
   не предоставляет параметра смещения, поэтому при превышении
   `max_result_chars` или `limit` пользователь не может получить остальные
   события, кроме как сужением `since/until`/`query`.
4. **Flush-латентность `DbLoggingService` не настраивается и не
   наблюдается.** Дефолтный `flush_interval_sec=5.0`
   (`db_logging_service.py:117`) даёт задержку 5–15 секунд между событием
   и его видимостью через `history_search`. Это by design (батчевый
   writer), но:
   - не вынесено в `config.json` (нельзя ускорить без правки кода);
   - в `get_stats()` нет метрики возраста самого старого не-flushed
     события, поэтому дебаг задержки невозможен.

Дополнительно (пункт 5) — отсутствие явной схемы `payload` для каждого
`event_type` в `workspace/TOOLS.md` заставляет агента парсить разную
структуру под каждый тип (для `tool_result` — двойной JSON-string,
для `llm_call` — массив ролей, для `run_finished` — `final_content`).
Это документируемая проблема, не кодовая — и решается отдельной секцией
в TOOLS.md без миграций.

## What Changes

- Добавить **диагностику** в `DbLoggingService`: счётчики `written_by_type:
  dict[str, int]` и `oldest_queued_age_sec: float | None` в `get_stats()`,
  чтобы до любых правок записи `run_finished`/`subagent_run_finished`
  было видно, доходят ли события до `_enqueue` и как долго висят в очереди.
- Добавить **опциональный параметр `offset`** в `history_search` и
  **разделить флаг `truncated`** на `payload_truncated` (на каждом
  событии: «обрезан payload конкретного события») и `results_truncated`
  (на ответе: «выпали целые события из выборки»). Старый `truncated`
  остаётся как deprecated алиас для `results_truncated` в течение одного
  релиза, потом удаляется.
- Вынести `flush_interval_sec` в `config.json` → `logging.db.flush_interval_sec`
  с дефолтом `5.0` и валидацией `0.5 ≤ value ≤ 60.0`. Это позволяет
  ускорить видимость событий без правки кода.
- **Задокументировать** в `workspace/TOOLS.md` схему `payload` для
  каждого `event_type` (с примером JSON) — `tool_call`, `tool_result`,
  `llm_call`, `run_finished`, `subagent_run_finished`, `inbound`,
  `context_compacted`. Это не меняет код, но устраняет необходимость
  для агента угадывать структуру.
- Добавить в `requirements.txt`-тесты (`tests/test_config_keys.py`)
  новые ключи `logging.db.flush_interval_sec`. Существующий
  `tests/test_history_search_tool.py` расширить сценариями для `offset`,
  `payload_truncated`, `results_truncated`.

**BREAKING** для `history_search` API: переименование `truncated` →
`results_truncated` + `payload_truncated`. Старый `truncated`
сохраняется как deprecated алиас ровно один релиз (см. задачи).

## Capabilities

### New Capabilities

- `tools/history-search`: контракт кастомного tool `history_search` —
  параметры, фильтры, формат ответа, диагностика truncation, пагинация.
  Ранее этот контракт был размазан между `workspace/tools/history_search_tool.py`,
  `workspace/TOOLS.md` и `docs/architecture/HISTORY_SEARCH_ANALYSIS.md`,
  без единого нормативного источника.
- `logging/db`: контракт `DbLoggingService` — диагностика
  (`get_stats().written_by_type`, `oldest_queued_age_sec`), настройка
  `flush_interval_sec` через `logging.db.*`, поведение при недоступности
  БД (батч выбрасывается, `failed++`), формат retention/purge.

### Modified Capabilities

- `runtime/context`: модифицируется требование про
  `ctx.start()`/`ctx.stop()` lifecycle — добавляется явное «start MUST
  NOT проглатывать ошибки подключения хуков логирования без
  логирования». Это защищает от сценария «`DatabaseLoggingHook`
  зарегистрирован, но `db_logging_service is None` — события
  молча теряются».

## Impact

- Код: `lib/services/db_logging_service.py`, `lib/hooks/database_logging_hook.py`,
  `workspace/tools/history_search_tool.py`, `lib/utils/text_utils.py`
  (без изменений, но потребитель результата),
  `lib/services/runtime_patcher.py` (без изменений, диагностируется).
- Конфиг: новая опциональная секция `logging.db.flush_interval_sec`
  в `config.json`; не входит в profile-owned runtime-ключи
  (см. AGENTS.md «Профили конфигурации»), наследуется из `project.json`.
- Тесты: `tests/test_config_keys.py` (`REQUIRED_KEYS` +
  `OPTIONAL_KEYS`), `tests/test_history_search_tool.py`
  (новые сценарии), `tests/test_db_logging_service.py` (счётчики
  по типам, age в очереди), `tests/test_hooks_database_logging.py`
  (regression на потерю `run_finished`).
- Документация: `workspace/TOOLS.md` (секция `history_search` —
  добавить схему payload), `docs/ARCHITECTURE.md` (если есть секция
  про `history_search`), `CHANGELOG.md` (запись под `[Unreleased]`).
- **Без миграций БД.** Изменения чисто runtime-/tool-уровня.
- **Без новых зависимостей.**
