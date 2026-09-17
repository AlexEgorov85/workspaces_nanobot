# Improve history_search pagination and logging observability

## Why

Пользовательская оценка инструмента `history_search` (см. внутренний
отчёт «Честная оценка `history_search`») выявила три категории
проблем, делающих tool непригодным как полноценный observability
инструмент и ненадёжным для recovery после `context_compacted`:

1. **Неопределённая семантика `truncated`.** В текущем
   `history_search_tool.py:325-362` один булев флаг `truncated`
   используется одновременно для двух разных механизмов:
   обрезки payload'а конкретного события и выбрасывания целых
   событий из выборки. Агент не может различить эти случаи.
2. **Отсутствие пагинации.** Нет параметра `offset`, нет
   признака наличия следующей страницы. При превышении
   `max_result_chars` или `limit` пользователь не может получить
   остальные события.
3. **Ненаблюдаемая потеря событий.** `run_finished` /
   `subagent_run_finished` пишутся в журнал через
   `DatabaseLoggingHook.after_run`
   (`lib/hooks/database_logging_hook.py:402-419`) и
   `runtime_patcher.patch_subagent_logging`
   (`lib/services/runtime_patcher.py:1370-1398`), но без
   диагностики невозможно понять, доходят ли они до БД и где
   теряются (в отчёте пользователя `count: 0` для этих типов).
   `DbLoggingService.get_stats()` не публикует ни счётчиков
   по типам, ни возраста очереди.
4. **Нестабильная сортировка при пагинации.** Текущий SQL
   `ORDER BY "timestamp" DESC` без явного tie-breaker'а не
   гарантирует детерминированный порядок при равных timestamp'ах
   (важно для multi-row INSERT'ов в одном батче flush'а).
5. **Хардкод flush-интервала.** `flush_interval_sec=5.0`
   (`db_logging_service.py:117`) не настраивается через
   `config.json` и не наблюдается, что мешает как тонкой
   настройке latency, так и дебагу задержек.

Дополнительно (пункт 6) — отсутствие явной схемы `payload`
для каждого `event_type` в `workspace/TOOLS.md` заставляет
агента угадывать структуру. Это документируемая проблема,
не кодовая — решается отдельной секцией в TOOLS.md без миграций.

## What Changes

- Переименование change: первоначальное название
  `remove-run-finished-from-history-search` отражало
  ошибочную гипотезу «типы не пишутся, надо удалить из enum».
  После ревизии кода выяснилось, что код записи существует,
  поэтому change переименован в
  `improve-history-search-pagination-and-logging`.
- **Декомпозиция `truncated`**: в JSON-ответе `history_search`
  вводятся два независимых флага:
  - `results_truncated: bool` — `true`, если были выброшены
    целые события из выборки, чтобы влезть в `max_result_chars`.
    `MUST NOT` интерпретироваться как «есть ещё результаты в БД».
  - `payload_truncated: bool` (на каждом событии) — `true`,
    если payload конкретного события был обрезан до
    `per_event_cap` символов через `truncate_middle`.
  - Старое поле `truncated: bool` сохраняется как **deprecated
    алиас** `results_truncated` в течение одного MINOR-релиза.
    Удаление — отдельным follow-up change после релиза.
- **Пагинация с `has_more` и `next_offset`**:
  - Добавляется параметр `offset: int >= 0` (дефолт 0).
  - В ответ добавляются `has_more: bool` и
    `next_offset: int` (`= offset + count`).
  - SQL: `ORDER BY "timestamp" DESC, id DESC LIMIT %s OFFSET %s`,
    где `LIMIT = effective_limit + 1` — лишняя строка
    используется для определения `db_has_more` и не возвращается
    агенту. Это дешевле, чем отдельный `COUNT(*)`.
  - Финальный `has_more` вычисляется как
    `db_has_more OR results_truncated`: даже если
    `LIMIT N+1` не обнаружил следующей строки в БД,
    `results_truncated=true` означает, что текущая страница
    сокращена `max_result_chars` и часть отобранных событий
    не показана агенту — следующая страница обязательна.
  - `next_offset` позволяет продолжать пагинацию после
    `results_truncated`, не пропуская события, отброшенные
    из текущего ответа (агент SHALL использовать `next_offset`,
    а не `offset + limit`).
- **Детерминированная сортировка**: явный `ORDER BY "timestamp" DESC, id DESC`
  (UUID из `agent_gateway_logs.id`) — исключает повторы/пропуски
  событий на границе страниц при равных timestamp'ах.
- **Диагностика `DbLoggingService`**:
  - `get_stats()["written_by_type"]: dict[str, int]` —
    счётчик по `event_type`, инкрементируется **только** после
    успешного `_flush_batch` (не в `_enqueue`).
  - `get_stats()["oldest_queued_age_sec"]: float | None` —
    возраст самого старого `LogEvent` в очереди (исключая
    `_QuestionRunRecord` и `_FlushSentinel`).
  - Срок жизни счётчиков = lifetime экземпляра
    `DbLoggingService`, **не** сбрасывается при повторном
    `start()` (диагностический сервис не должен терять историю
    на restart worker'а).
- **Вынос `flush_interval_sec` в `config.json`**:
  - Новое поле `LoggingDbSettings.flush_interval_sec: float`,
    диапазон `0.5 ≤ value ≤ 60.0`, дефолт `5.0`.
  - Значение передаётся из resolved `SETTINGS` через
    `ConfigurationResolver` → `ProjectSettings` →
    `ApplicationContext` → `DbLoggingService.__init__`.
    Сервис НЕ читает `config.json` напрямую.
- **Документация payload**: подсекция в `workspace/TOOLS.md`
  с явной схемой `payload` для каждого `event_type` —
  `tool_call`, `tool_result`, `llm_call`, `run_finished`,
  `subagent_run_finished`, `inbound`, `context_compacted`.
  Схема описывает **текущую** форму данных и явно отмечает
  поля, помеченные как JSON-string (например, `tool_result.result`),
  без превращения этих wrapping'ов в долгосрочный нормативный
  контракт.

**API extension, compatibility-preserving change** для
`history_search`: добавляются новые поля (`has_more`,
`next_offset`, `results_truncated`, `payload_truncated`,
параметр `offset`) и расширяется SQL-сортировка; **старый
потребитель, читающий только `truncated`, продолжает
работать без изменений** благодаря deprecated алиасу
`truncated` (= `results_truncated`). Алиас удаляется
отдельным follow-up change, который и будет формальным
breaking change. Этот change миграции со стороны
потребителя НЕ требует.

## Capabilities

### New Capabilities

- `tools-history-search`: контракт кастомного tool
  `history_search` — параметры, формат ответа, пагинация,
  диагностика truncation, честный empty-result.
- `logging-db`: контракт сервиса `DbLoggingService` в части
  наблюдаемости и настройки flush-интервала. **Только** то,
  что этот change реально меняет: `flush_interval_sec`,
  `written_by_type`, `oldest_queued_age_sec`. Существующее
  поведение (`retention`, `purge`, batching, question_runs)
  НЕ переспецифицируется.

### Modified Capabilities

Нет. Change не модифицирует существующие capabilities. Вопросы
вида «`ctx.start()` должен логировать ошибки подключения хуков»
или «`runtime_health.db_logging` должен помечаться `DEGRADED`»
являются **отдельными** потенциальными change и НЕ входят
в scope этого.

## Impact

- Код:
  - `lib/services/db_logging_service.py` — добавить
    `written_by_type`, `oldest_queued_age_sec`, `flush_interval_sec`
    в конструктор.
  - `workspace/tools/history_search_tool.py` — добавить `offset`,
    `has_more`, разделить `truncated` на `results_truncated` и
    `payload_truncated`, детерминированная сортировка.
  - `lib/core/project_settings.py` — поле
    `LoggingDbSettings.flush_interval_sec`.
  - `lib/core/application_context.py` (или место сборки) —
    прокинуть `flush_interval_sec` из settings в конструктор
    `DbLoggingService`.
- Конфиг: новое опциональное поле `logging.db.flush_interval_sec`
  в `config.json`. Не входит в profile-owned runtime-ключи
  (см. AGENTS.md «Профили конфигурации»), наследуется из
  `project.json` или задаётся явно в `config.json`.
- Тесты:
  - `tests/test_config_keys.py` — `OPTIONAL_KEYS` +
    валидация диапазона `flush_interval_sec`.
  - `tests/test_history_search_tool.py` — сценарии для
    `offset`, `has_more`, `results_truncated`, `payload_truncated`,
    детерминированной сортировки, deprecated alias.
  - `tests/test_db_logging_service.py` — счётчики по типам
    (растут только после flush'а), `oldest_queued_age_sec`
    (с учётом `_QuestionRunRecord`/`_FlushSentinel`).
  - `tests/test_hooks_database_logging.py` — регрессионный
    тест на запись `run_finished` через `after_run`.
  - `tests/test_runtime_patcher.py` или новый
    `tests/test_subagent_logging.py` — регрессионный тест
    на запись `subagent_run_finished`.
- Документация:
  - `workspace/TOOLS.md` — секция `history_search`: добавить
    подсекцию «Структура payload по event_type» с примерами
    JSON; пометить `truncated` как deprecated.
  - `CHANGELOG.md` — запись под `[Unreleased]` →
    категория `Changed`.
- **Без миграций БД.** Изменения runtime/tool-уровня.
- **Без новых зависимостей.**
