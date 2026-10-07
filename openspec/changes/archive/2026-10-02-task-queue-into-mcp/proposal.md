# Очередь задач и все данные — в `enterprise-mcp` (отмена решения 2.18)

## Why

Канал агента сегодня ходит в PostgreSQL **напрямую**: собственный пул
(`workspace/utils/db.py`, 1144 строки), собственный SQL в
`lib/channels/postgres_channel.py` (1895 строк, 33 SQL-глагола, 14
`execute`), плюс прямые записи в `lib/services/db_logging_service.py`
(1284), `lib/services/session_cold_sync_service.py` (672),
`lib/services/schema_validation.py` (344) и
`lib/services/context_compaction.py`. Итого ~5900 строк прямого доступа к БД
внутри агента и **второй владелец пула** рядом с платформой.

Это ровно то, что миграция `enterprise-mcp-platform` устраняла: в agent
оставалась одна точка входа — `DataService` платформы. Решение 2.18
(`tasks.md:262`) оставило очередь задач в агенте, потому что операции
`claim_task`/`update_task_status` **объявляли наружу имя таблицы, выбранное
вызывающей стороной**. Тот довод снимается тем же приёмом, которым платформа
уже решила задачу для журнала: имя таблицы объявляется **в `platform.json`**
(как `log_table` и `question_runs_table`), а не приходит от вызывающего.

Владелец 2026-10-02: **канал не подключается к БД напрямую, всё — через
MCP.**

## What Changes

- **Отменяется 2.18.** Операции очереди возвращаются в capability `data` —
  уже с другой схемой: имя таблицы берётся из настройки платформы, а не из
  тела вызова.
- Появляются операции capability `data` (по файлу на операцию, в
  `servers/enterprise/capabilities/data/tools/`):
  - `claim_task` — атомарный захват одного `pending`-сообщения
    (`UPDATE … RETURNING`) с фильтром по priority-командам и исключением
    чатов, где уже есть незавершённый assistant-оборот. Возвращает строку
    задачи целиком.
  - `update_task_status` — смена статуса задачи (в т.ч. `processing` →
    `error`/`failed`/`pending`).
  - `append_assistant_message` — вставка assistant-плейсхолдера
    (`role='assistant'`, `status='processing'`).
  - `delete_assistant_message` — удаление плейсхолдера (откат при ошибке).
  - `patch_message_metadata` — read-modify-write `metadata` (reasoning-delta,
    `context_window`).
  - `unstick_tasks` — возврат зависших `processing` в `pending`, с ретраями и
    пометкой сирот.
- `platform.json::data` получает `task_table` (и недостающие имена колонок),
  чтобы имя таблицы не приходило от вызывающей стороны.
- **Агент** перестаёт владеть пулом: `workspace/utils/db.py` уходит, все
  `execute` в `lib/` заменяются вызовами операций. Прямой INSERT в журнал
  как LOCAL-FALLBACK исчезает — писатель теперь один.
- Следствие для `schema_validation.py`: логика переезжает на существующую
  операцию `schema_check`.

## Порядок работ (обязателен)

1. Платформа: операции + `platform.json::data.task_table` + тесты.
2. Переключение канала на вызовы операций (feature-флаг, старый SQL — как
   fallback на время перехода).
3. Удаление `workspace/utils/db.py` и прямого SQL.
4. Удаление tombstone `_claim_task.py` / `_update_task_status.py` на платформе
   — они больше не описывают отсутствующие операции.

Шаг 1 обязателен до шага 3: пока операций нет, канал обязан уметь работать
через прямой SQL, иначе теряется очередь.

## Capabilities

### New Capabilities

- `data/task-queue`: контракт операций очереди задач — захват, смена статуса,
  вставка/удаление assistant-сообщения, патч метаданных, возврат зависших.

### Modified Capabilities

- `enterprise-mcp-platform` (пункт 2.18): решение заменено на противоположное.
  Обоснование прежнее («вход в данные агента шёл мимо его конфигурации»)
  снимается переносом имени таблицы в `platform.json`.

## Impact

- **Платформа:** `capabilities/data/service/main.py` (методы очереди),
  `capabilities/data/tools/*.py` (операции), `platform.json` (имя таблицы),
  `platform.json::data` в реестре настроек, удаление двух tombstone-файлов.
- **Агент:** `lib/channels/postgres_channel.py` (1895 → тонкий MCP-клиент),
  `lib/services/db_logging_service.py` (прямой INSERT уходит),
  `lib/services/schema_validation.py`, `lib/services/session_cold_sync_service.py`,
  `lib/services/context_compaction.py`, `workspace/utils/db.py` (удаляется),
  `lib/core/application_context.py` (пул в `create()`).
- **Тесты агента:** `test_utils_db.py` (902), `test_postgres_channel.py` (1393),
  `test_db_logging_service.py` (921), `test_session_cold_sync_service.py` (685),
  `test_schema_validation.py` (456), `test_single_mode_audit.py` (404),
  `test_storage_hybridization*.py` — все подменяют `lib.utils.db`, все переписываются
  на моки MCP-клиента.
- **Наблюдаемость:** круговые обороты по stdio добавляются на каждый poll и на
  каждое обновление статуса. Требуется батчинг: `claim_task` должен уметь
  вернуть пачку кандидатов, иначе поллинг станет дороже, чем прямой SQL.
