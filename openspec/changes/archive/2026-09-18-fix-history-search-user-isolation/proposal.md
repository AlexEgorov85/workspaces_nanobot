# Fix history_search user isolation

## Why

`history_search` с `session_scope="all"` сейчас возвращает
**глобальный набор событий**, потому что в
`workspace/tools/history_search_tool.py:279-292` фильтр построен
как

```python
clauses.append("(%s OR session_id = %s)")
params.append(allow_all)
params.append(session_id or "")
```

При `allow_all=True` первая скобка всегда истинна — фильтр по
`session_id` снимается, а фильтра по пользователю в таблице
просто нет. Это **cross-user leakage**: запрос пользователя A
возвращает события пользователей B, C, … из чужих чатов и
каналов. Дополнительно это делает `session_scope="all"`
непригодным как observability-tool для recovery после
`context_compacted`.

Корень — отсутствие идентификатора пользователя в
`agent_gateway_logs`. В `agent_question_runs` колонка `user_id`
уже есть (документирована как «ID пользователя (sender_id)»,
см. `sql/logs/create_public_agent_question_runs.sql:40`) и
связана с `agent_gateway_logs` через `request_id`. Решение —
добавить `user_id` в `agent_gateway_logs`, пробросить его через
`LogEvent` и `DbLoggingService` (единственный writer), и
фильтровать `session_scope="all"` напрямую по `user_id`.

## What Changes

- DDL: добавить `user_id VARCHAR(256)` в
  `agent_gateway_logs` рядом с `request_id`/`session_id`/`channel`/
  `actor`/`name`; добавить индекс под `user_id + timestamp`.
- Миграция `V004__agent_gateway_logs_user_id.sql`: идемпотентный
  ADD COLUMN + backfill UPDATE через `request_id → agent_question_runs.user_id`
  только когда `agent_question_runs.user_id IS NOT NULL` +
  CREATE INDEX. Старые строки без `request_id` или с
  `agent_question_runs.user_id IS NULL` остаются с
  `gateway_logs.user_id IS NULL` и не попадают в `scope="all"`.
- `LogEvent.user_id: str | None = None`. **Это намеренная
  денормализация**, исключение из прежнего правила «identity
  только в `agent_question_runs`»: `user_id` стал security
  boundary для чтения событий, и без него `session_scope="all"`
  требует JOIN на каждый поиск. Consistency между
  `agent_question_runs.user_id` (первичный source of truth) и
  `agent_gateway_logs.user_id` (security boundary) обеспечивается
  двумя механизмами: **(a)** single-writer invariant — все
  runtime-события пишутся только через `DbLoggingService`, и
  `user_id` подтягивается либо явно producer'ом, либо через
  request_id matching в `_enqueue`; **(b)** идемпотентная
  миграция `V004` с backfill через
  `request_id → agent_question_runs.user_id IS NOT NULL`,
  которая переносит identity только из осмысленных
  question_runs и оставляет `NULL` для неопределимых
  исторических событий.
- `DbLoggingService._request_index` хранит парную запись
  `{request_id, user_id}` (а не только `request_id`). Атомарное
  обновление обеих полей под одним lock'ом в `register_request`.
- `_enqueue` автозаполняет `event.user_id` из индекса, если
  producer не задал явно. Явное значение имеет приоритет.
  **Индекс остаётся внутренним механизмом `_enqueue`** —
  публичный `get_request_user_id()` НЕ вводится.
- `history_search_tool.py`: две взаимоисключающие ветви SQL —
  `current → session_id`, `all → user_id`. Конструкция
  `(%s OR session_id = %s)` удаляется полностью. Источник
  `user_id` — identity-store текущего request (в nanobot 0.3.0
  это `RequestContext.sender_id`; спека фиксирует **роль**
  identity-store, не имя поля).
- При `session_scope="all"` и отсутствии identity-store —
  запрос НЕ выполняется, возвращается
  `{"status": "error", "error_type": "missing_user_identity"}`.
  Симметричный кейс для `"current"` без `session_key` —
  `missing_session_identity`.
- Tool API остаётся неизменным: `user_id` не становится
  параметром tool'а, не попадает в payload ответа.
- Тесты, guard'ы, документация — см. `tasks.md` и `specs/tools-history-search/spec.md`.

**Это breaking change по поведению**: `session_scope="all"`
больше не возвращает глобальный набор. Существующие агенты
начнут получать либо события только своего пользователя (норма),
либо `missing_user_identity` (если request context не дошёл).
Обратной совместимости нет и не должно быть.

`improve-history-search-pagination-and-logging` остаётся
отдельной change со своим контрактом (пагинация, truncation,
observability); она ещё не архивирована в `openspec/specs/`, и
её задачи помечены как выполненные без архивирования — это
отдельная проблема, не блокирующая эту change.

## Capabilities

### New Capabilities

- `tools-history-search`: контракт кастомного tool `history_search` —
  параметры, фильтрация по `user_id` для `session_scope="all"`,
  безопасный отказ при отсутствии identity, поведение logging
  pipeline для `user_id`. Capability не объединяется с
  `improve-history-search-pagination-and-logging` потому что та
  change не дошла до архивирования; при архивировании обеих
  change'ей спек сольётся.

### Modified Capabilities

Нет. Существующие capabilities (`runtime/context`, `data/cache`,
`data/vector-indexes`, `architecture/skill-tool-boundary`,
`configuration/profiles`) требований по этой теме не меняют.
`logging-db` упомянут как related capability (single-writer
invariant остаётся в силе), но новых требований не добавляется.

## Impact

- **Код:**
  - `sql/logs/create_public_agent_gateway_logs.sql` — добавить
    `user_id` + индекс.
  - `sql/migrations/V004__agent_gateway_logs_user_id.sql` — новая
    миграция (DDL + backfill + INDEX).
  - `lib/services/db_logging_service.py`: `LogEvent.user_id`,
    расширение `_request_index` (парная запись), атомарный
    `register_request`, автозаполнение в `_enqueue`.
  - `lib/hooks/database_logging_hook.py:178-200` — `_factory`
    передаёт `user_id` в `register_request` (из request context
    или fallback).
  - `lib/services/runtime_patcher.py:_SubagentLoggingHook` —
    subagent явно прокидывает `user_id` родителя.
  - `lib/services/context_compaction.py:_record_event_log` —
    `context_compacted` через `LogEvent.user_id`.
  - `workspace/tools/history_search_tool.py`: две ветви SQL,
    `_current_user_id()` через identity-store, отказ при
    отсутствии identity. Tool description обновляется.
- **Тесты**: `TestUserIsolation`, регрессии на
  `register_request` атомарность, subagent parent-child
  user_id, backfill с NULL user_id, guard на сгенерированный
  SQL/параметры (не на исходник), fixture-обновления.
- **Документация**: `workspace/TOOLS.md` (семантика
  `current`/`all`/`missing_user_identity`),
  `docs/architecture/HISTORY_SEARCH_ANALYSIS.md` (закрыть gap),
  `CHANGELOG.md` (`Security` + `Changed`).
- **Без новых зависимостей.** Источник правды: `RequestContext`
  + `agent_question_runs` + `LogEvent → DbLoggingService`.
