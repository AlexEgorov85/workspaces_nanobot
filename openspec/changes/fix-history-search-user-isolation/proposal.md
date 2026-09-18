# Fix history_search user isolation

## Why

Текущий `history_search` с `session_scope="all"` возвращает **все события из
всех пользователей и сессий**, потому что в
`workspace/tools/history_search_tool.py:290-292` фильтр построен как

```python
clauses.append("(%s OR session_id = %s)")
params.append(allow_all)
params.append(session_id or "")
```

При `allow_all=True` первая скобка всегда истинна — фильтр по
`session_id` снимается полностью, а фильтра по пользователю вообще нет.
Это нарушение изоляции данных: запрос пользователя A возвращает события
пользователей B, C, … из чужих чатов и каналов. Та же проблема делает
`session_scope=all` непригодным как observability-tool для recovery после
`context_compacted`: агент видит чужую историю и не может отличить свою
от посторонней.

Корень проблемы — отсутствие идентификатора пользователя в
`agent_gateway_logs`. В таблице уже есть `request_id` (FK-логически на
`agent_question_runs.request_id`), а в `agent_question_runs` уже есть
`user_id` (документирован как «ID пользователя (sender_id)»; см.
`sql/logs/create_public_agent_question_runs.sql:40`). Поэтому
`user_id` для события однозначно определяется через существующую связь
`request_id → agent_question_runs.user_id`. Источник `user_id` для
агентского запроса — `nanobot.agent.tools.context.RequestContext.sender_id`,
которое уже выставляется `bind_request_context` при обработке
входящего сообщения.

Решение: добавить колонку `user_id` в `agent_gateway_logs`,
пробрасывать её через `LogEvent` и `DbLoggingService` (единственный
writer), и фильтровать `session_scope=all` по `user_id` напрямую.
Никаких `LIKE`/`session_id`-эвристик, никакого глобального поиска.

## What Changes

- **DDL**: в `sql/logs/create_public_agent_gateway_logs.sql` добавить
  колонку `user_id VARCHAR(256)` рядом с `request_id`/`session_id`/
  `channel`/`actor`/`name`; добавить индекс
  `(user_id, "timestamp" DESC)` под `session_scope=all`.
- **Миграция**: `sql/migrations/V004__agent_gateway_logs_user_id.sql`
  — идемпотентный `ALTER TABLE ... ADD COLUMN IF NOT EXISTS user_id
  VARCHAR(256)` плюс backfill из `agent_question_runs` через
  `request_id`, плюс индекс. Старые события без `request_id` остаются
  с `user_id IS NULL` и не участвуют в `session_scope=all`
  (безопасное поведение).
- **`LogEvent`**: в `lib/services/db_logging_service.py` добавить поле
  `user_id: str | None = None`. Единственный writer
  (`DbLoggingService._insert_batch`) расширяет INSERT колонкой
  `user_id`.
- **`DbLoggingService.register_request`**: контракт расширяется
  хранением `user_id` в индексе `session_key → {request_id, user_id}`
  (а не только `request_id`). События, эмиттируемые в рамках того же
  `session_key` (tool_call, llm_call, run_finished, outbound),
  подтягивают `user_id` из этого индекса в `_enqueue` и кладут его в
  `LogEvent`. Поле `LogEvent.user_id`, заданное явно producer'ом,
  имеет приоритет над индексом (явное перекрытие неявного).
- **`history_search_tool.py`**:
  - `session_scope="current"` → `WHERE session_id = :current_session_id`;
  - `session_scope="all"` → `WHERE user_id = :current_user_id`;
  - жёсткое требование: при `session_scope="all"` и
    `current_user_id is None` запрос НЕ выполняется, возвращается
    JSON `{"status": "error", "error_type": "missing_user_identity", ...}`
    (отсутствие identity = отсутствие разрешения на cross-session search);
  - вспомогательная функция `_current_user_id()` читает
    `nanobot.agent.tools.context.current_request_context().sender_id`.
    Если контекст отсутствует (тесты, standalone) — `None`, и
    `session_scope="all"` отказывает. Это безопасный дефолт, а не
    fallback на unscoped query.
- **Удалить** из SQL-сборки `history_search` форму
  `(%s OR session_id = %s)` и любые конструкции, допускающие
  `WHERE TRUE` для пользовательского фильтра.
- **Тесты**:
  - новый класс `TestUserIsolation` в `tests/test_history_search_tool.py`:
    cross-user isolation (alice видит только свои сессии; bob — только
    свои); отсутствие identity при `session_scope=all` → error;
    сохранение существующих сценариев поиска.
  - `tests/test_db_logging_service.py`: `LogEvent(user_id=...)` доходит
    до INSERT (мок SQL capture).
  - `tests/fixtures/history_search/gateway_logs.jsonl`: добавить
    пары сессий `alice/session_a1`, `alice/session_a2`,
    `bob/session_b1`, `bob/session_b2` с разными `user_id`.
  - `tests/fixtures/history_search/scenarios.json`: добавить сценарии
    `scope_current_user_a`, `scope_all_user_a`,
    `scope_all_cross_user_isolation`, `scope_all_missing_user`.
    Существующие сценарии остаются (с `scope_all` без `user_id`
    контекста они теперь вернут `missing_user_identity` —
    тест явно проверяет это поведение как failure-сценарий, чтобы
    регрессия не прошла незамеченной).
- **Архитектурный guard**: тест, который грепит исходники
  `workspace/tools/history_search_tool.py` на отсутствие конструкций
  `OR session_id = %s` и `LIKE %session_id%`. Запускается в pytest.
- **Документация**:
  - `workspace/TOOLS.md` секция `history_search`: явно описать
    семантику `current`/`all`, предупреждение, что `all` = все сессии
    текущего пользователя (не глобально), поведение при отсутствии
    identity.
  - `docs/architecture/HISTORY_SEARCH_ANALYSIS.md`: пометить
    cross-user leakage как **закрытый gap**, добавить ссылку на эту
    change.

**Это breaking change по поведению**: `session_scope="all"` больше не
возвращает глобальный набор событий. Существующие агенты, полагавшиеся
на эту семантику, начнут получать либо события только своего
пользователя (норма), либо `missing_user_identity` (если контекст не
дошёл). Это намеренное поведение, диктуемое требованиями изоляции
данных; обратной совместимости нет и не должно быть.

`improve-history-search-pagination-and-logging` остаётся отдельной
change со своим контрактом (пагинация, truncation, observability).
Эта change не модифицирует его требования; их архивирование — отдельная
задача.

## Capabilities

### New Capabilities

- `tools-history-search`: контракт кастомного tool `history_search` —
  фильтрация по `user_id` для `session_scope="all"`, безопасный
  отказ при отсутствии identity, единственный путь
  `LogEvent → DbLoggingService → agent_gateway_logs.user_id`.
  Закладывается как новый capability `tools/history-search/spec.md`.
  Capability не объединяется с
  `improve-history-search-pagination-and-logging` потому что эта
  change не дошла до архивирования (её задачи помечены `[x]`,
  но соответствующего `openspec/specs/tools-history-search/spec.md`
  в репо нет); новая спецификация собирается из этой change
  при её архивировании.

### Modified Capabilities

Нет. Существующие capabilities (`runtime/context`, `data/cache`,
`data/vector-indexes`, `architecture/skill-tool-boundary`,
`configuration/profiles`) требований по этой теме не меняют.
Внутреннее хранение `user_id` в индексе `DbLoggingService` —
implementation detail, не spec-level.

## Impact

- **Код:**
  - `sql/logs/create_public_agent_gateway_logs.sql` — добавить
    `user_id` и индекс `(user_id, "timestamp" DESC)`.
  - `sql/migrations/V004__agent_gateway_logs_user_id.sql` — новая
    миграция (DDL + backfill + CREATE INDEX IF NOT EXISTS).
  - `lib/services/db_logging_service.py`:
    - `LogEvent.user_id: str | None = None`;
    - `_insert_batch` — расширить список колонок INSERT;
    - `_request_index` хранит `dict[str, dict[str, str | None]]`
      со значениями `request_id` и `user_id`;
    - `register_request` принимает `user_id` и сохраняет его в индекс;
    - новый метод `get_request_user_id(session_key) -> str | None`;
    - в `_enqueue` (или в публичных `log_*` методах) при пустом
      `event.user_id` подтягивать `user_id` из `_request_index`
      по `session_key`, если он известен (это покрывает
      `log_inbound`/`log_outbound`/`log_tool_*`/`log_llm_call`,
      которые получают `session_id`, но не передают `user_id`).
  - `workspace/tools/history_search_tool.py`:
    - две взаимоисключающие ветви SQL;
    - жёсткий отказ при `allow_all and current_user_id is None`;
    - удалить `allow_all OR session_id` ветку;
    - `_current_user_id()` через `current_request_context().sender_id`;
    - tool_description обновить (отражать новую семантику).
- **Конфиг**: без изменений. `user_id` берётся из существующего
  `RequestContext.sender_id` (заполняется каналами в `bind_request_context`).
- **Тесты**:
  - `tests/test_history_search_tool.py` — новый класс
    `TestUserIsolation` (4+ сценария).
  - `tests/test_db_logging_service.py` — сценарии на
    `LogEvent.user_id` доходит до INSERT; индекс
    `session_key → {request_id, user_id}` обновляется в
    `register_request` и читается в `_enqueue`.
  - `tests/test_hooks_database_logging.py` — регрессия: `after_run`
    прокидывает `user_id` из request context в `LogEvent`.
  - `tests/test_subagent_logging.py` — регрессия: subagent logging
    тоже прокидывает `user_id` родительского request'а.
  - `tests/test_context_compaction.py` — регрессия:
    `ContextCompactionService._record_event_log` кладёт `user_id`
    в `LogEvent` из текущего request context.
  - `tests/test_architecture_guards.py` (или новый файл) —
    guard: в `history_search_tool.py` нет
    `OR session_id = %s`, `LIKE %session_id%`, `WHERE TRUE`.
- **Документация**: `workspace/TOOLS.md` (секция `history_search`),
  `docs/architecture/HISTORY_SEARCH_ANALYSIS.md` (закрыть gap),
  `CHANGELOG.md` `[Unreleased]` → категория `Security` + `Changed`.
- **Совместимость:** для deployment'ов без
  `RequestContext.sender_id` (теоретически: инструменты, вызванные
  вне request-цикла) `session_scope="all"` начнёт возвращать
  `missing_user_identity` вместо глобальной выборки. Это
  **наблюдаемое поведенческое изменение**, соответствующее требованиям
  изоляции, и не должно маскироваться fallback'ом.
- **Зависимости:** нет. `RequestContext.sender_id`, `user_id` поле
  `agent_question_runs`, и путь `LogEvent → DbLoggingService →
  agent_gateway_logs` уже существуют.
