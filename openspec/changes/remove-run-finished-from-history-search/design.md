# Design — history_search observability improvements

## Context

См. `proposal.md` (раздел Why) — четыре связанные проблемы
с durability и observability журнала `agent_gateway_logs`,
проявляющиеся при использовании `history_search` для recovery
после `context_compacted`.

Текущее состояние кода (без правок):

- `lib/services/db_logging_service.py:209-212` — `log_event` принимает
  `LogEvent` и кладёт в `queue.Queue` без диагностики по типам;
- `lib/services/db_logging_service.py:548-606` — worker-loop
  flush'ит батч по `flush_interval_sec` (хардкод `5.0`) или
  `batch_size`;
- `lib/hooks/database_logging_hook.py:402-419` —
  `DatabaseLoggingHook.after_run` пишет `run_finished`;
- `lib/services/runtime_patcher.py:1370-1398` — subagent logging
  patch пишет `subagent_run_finished`;
- `workspace/tools/history_search_tool.py:50-139` — параметры
  tool (нет `offset`); `:325-362` — truncation-логика с одним
  булевым `truncated`; `:338-341` — `per_event_cap = 4000`.

Ограничения:

- Python ≥ 3.14, `from __future__ import annotations` обязателен.
- `nanobot 0.3.0` API (`tool_parameters`, `ctx._settings_ref`,
  `current_request_session_key`) — менять нельзя, только читать.
- БД — PostgreSQL/Greenplum 6.5, без `make_interval`, без
  `ON CONFLICT`. Все SQL — `%s`-параметризованные.
- Профиль конфигурации (`--profile`) — `logging.db.flush_interval_sec`
  не входит в profile-owned runtime-ключи (см. AGENTS.md), наследуется
  из `project.json`.

## Goals / Non-Goals

**Goals:**

- Дать наблюдаемость «пишется ли `run_finished` вообще» через
  счётчики `written_by_type` и `oldest_queued_age_sec` в `get_stats()`.
- Дать управление flush-латентностью через `config.json`
  без правки кода.
- Дать агенту рабочую пагинацию (`offset`) и честную семантику
  truncation-флагов.
- Задокументировать схему `payload` для каждого `event_type`,
  чтобы агенту не приходилось угадывать структуру.

**Non-Goals:**

- Менять схему таблицы `agent_gateway_logs` (JSONB остаётся
  JSONB, миграций нет).
- Менять batching-модель (worker-поток остаётся, синхронные
  INSERT'ы не вводятся — это потенциально x10 нагрузка на БД).
- Переписывать payload-сериализацию для разных `event_type`
  в единую Pydantic-схему (это отдельный большой change).
- Удалять `run_finished`/`subagent_run_finished` из enum
  (как обсуждалось в первом разборе) — теперь видно, что код
  пишет эти типы; задача — починить потерю, а не убирать типы.

## Decisions

### D1. Счётчик `written_by_type` инкрементируется после успешного INSERT

В `_flush_batch` (после `self._db_run(_work)` без исключения) —
считаем `Counter` по `event_type` батча и прибавляем к
`self._stats["written_by_type"]` под `_state_lock`.

Альтернативы:

- Считать в `_enqueue` (мгновенно) — врёт при ошибке flush'а.
- Считать через SQL `GROUP BY event_type` каждый раз — лишний
  запрос на чтение, не нужен.

Выбрано: после успешного `_flush_batch` (батч уже в БД).
Это даёт точную картину «сколько каждого типа реально записано».

### D2. `oldest_queued_age_sec` — ленивое вычисление

В `get_stats()` дополнительно читается `self._queue.queue[0].timestamp`
(атрибут `LogEvent` или поле `_queued_at: float`, добавляемое в
`_enqueue`). Если очередь пуста — `None`. Без отдельного потока.

Альтернативы:

- Background-thread, обновляющий `stats` раз в секунду — лишний
  поток и пробуждение, не нужно.

Выбрано: ленивое `O(1)` чтение head очереди при `get_stats()`.

### D3. `flush_interval_sec` читается из `config.json`, не из ENV

Парсинг — в `lib/core/project_settings.py` (Pydantic
`LoggingDbSettings`), значение передаётся в конструктор
`DbLoggingService` через `ApplicationContext.start()`.
Валидация `0.5 ≤ value ≤ 60.0` — на уровне Pydantic.

Альтернативы:

- ENV (`LOGGING_DB_FLUSH_INTERVAL_SEC`) — менее явно,
  расходится с конвенцией остальных настроек.
- `gateway.*` — это runtime/observability, не скилл;
  секция `logging.db` уже существует для retention/purge,
  логичное продолжение.

Выбрано: `config.json::logging.db.flush_interval_sec`.

### D4. `offset` как целочисленный параметр tool

Добавляется в JSON-schema `history_search` (см. spec
`tools-history-search`). В SQL:

```sql
ORDER BY "timestamp" DESC LIMIT %s OFFSET %s
```

Передаётся двумя `%s`-параметрами. Никаких cursor-токенов —
выбор простоты и предсказуемости для агента.

Альтернативы:

- Cursor на основе `event_id` последнего события —
  стабильнее при INSERT'ах между страницами, но усложняет
  контракт для агента.

Выбрано: простой `offset`. Для нашего объёма (десятки-сотни
событий на сессию) между страницами обычно 0 новых INSERT'ов,
так что offset даёт стабильную картину.

### D5. Разделение truncation-флагов без немедленного удаления `truncated`

JSON-ответ tool'а расширяется полями `results_truncated` (на ответе)
и `payload_truncated` (на каждом событии). Старое поле `truncated`
сохраняется как алиас `results_truncated` в течение одного релиза
(до следующего MINOR/PATCH). После этого — удаляется.

Альтернативы:

- Удалить `truncated` сразу — ломает существующих агентов,
  которые его читают.
- Сохранять `truncated` всегда — не намекает агентам на новый контракт.

Выбрано: deprecation-окно в один релиз.

### D6. Схема payload в TOOLS.md без миграций БД

В `workspace/TOOLS.md` (раздел `history_search`) добавляется
подсекция «Структура payload по event_type» с примерами JSON
для каждого типа. Без изменений в коде записи/чтения.

## Risks / Trade-offs

- **R1**: Счётчик `written_by_type` занимает память пропорционально
  числу уникальных `event_type`. → **Mitigation**: число типов
  фиксировано (8 enum-значений + возможные `error`, `outbound_delta`),
  счётчик — обычный `dict[str, int]`, десятки байт.

- **R2**: Снижение `flush_interval_sec` до 0.5 увеличивает
  нагрузку на БД (больше мелких INSERT'ов). → **Mitigation**:
  дефолт остаётся 5.0, нижняя граница 0.5 жёсткая,
  оператор снижает осознанно и под наблюдением.

- **R3**: Поле `truncated` как deprecated-алиас добавляет
  шум в JSON-ответ tool'а. → **Mitigation**: deprecated только
  один релиз, в TOOLS.md упомянуто явно.

- **R4**: `offset` без cursor'а нестабилен при INSERT между
  страницами. → **Mitigation**: в сценарии recovery после
  compaction новые события между страницами маловероятны
  (агент обычно долистывает быстро); если нужна строгая
  стабильность — добавим cursor в следующем change.

- **R5**: Документирование `payload`-схемы без миграций может
  разойтись с реальностью, если payload-структура изменится.
  → **Mitigation**: TOOLS.md синхронизируется в том же
  изменении, где меняется код; регрессионные тесты
  проверяют наличие всех типов в фикстурах.

## Migration Plan

Изменения runtime-уровня, миграций БД нет.

**Применение:**

1. Merge change через стандартный PR-флоу.
2. Новые опциональные ключи `logging.db.flush_interval_sec`
   и `tools.history_search.max_result_chars` имеют дефолты,
   существующие `config.json` работают без правок.
3. Поле `truncated` в ответе `history_search` deprecated —
   один релиз совместимости.

**Откат:**

- `flush_interval_sec`: поменять значение в `config.json`,
  перезапустить gateway. Никаких миграций.
- `offset` и `payload_truncated`/`results_truncated`:
  revert одного PR.
- Счётчики `written_by_type` / `oldest_queued_age_sec`:
  аддитивное изменение `get_stats()`, не ломает существующих
  потребителей.

## Open Questions

- **Q1**: Нужно ли включать `oldest_queued_age_sec` в `runtime_health`
  как сигнал `DEGRADED` при превышении порога (например, 30 секунд)?
  Сейчас это только в `get_stats()`. → Решаемо отдельно в
  следующем change, если будет нужно; не блокирует текущий.
- **Q2**: Должна ли быть возможность ручного `flush()` из
  админ-эндпоинта (например, при диагностике)? Сейчас только
  автоматический flush по интервалу. → Запрос вне scope текущего
  change; можно сделать отдельно, если появится use-case.
