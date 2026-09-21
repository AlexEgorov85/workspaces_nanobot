# Design — fix history_search user isolation

См. `proposal.md` (Why) и `specs/tools-history-search/spec.md`
(контракт). Этот документ фиксирует только архитектурные решения,
которые не выражены в спеке явно.

## Identity model

Существующие точки опоры (используются, не меняются):

- `nanobot.agent.tools.context.RequestContext` несёт поля
  `channel`, `chat_id`, `message_id`, `session_key`,
  `original_user_text`, `runtime`, `metadata`, `sender_id`,
  `turn_id`, `workspace`. Контрактный тест
  `tests/contract/test_tools_and_context.py:30-35` фиксирует
  лишь подмножество `(channel, chat_id, message_id, session_key,
  runtime)`. То есть `sender_id` в nanobot 0.3.0 присутствует,
  но **не закреплён** в contract subset (отмечен как `ORANGE`
  в `docs/architecture/nanobot-inventory.md:86`). Поэтому спека
  говорит об identity-store как о роли, а не о конкретном поле.
- Каналы (`postgres_channel.py:875, 1328`, `redis_channel.py:244-283`)
  уже пробрасывают `sender_id` в `_handle_message` и далее.
- `agent_question_runs.user_id` уже документирован как «ID
  пользователя (sender_id)» и заполняется из входящего
  сообщения.

Спека не привязывается к имени поля `sender_id`: если в будущей
версии nanobot поле будет переименовано, изменение API-поверхности
делается отдельным change.

## Logging propagation

Решение D4 фиксируется в трёх ветвях `_enqueue`:

```
session_key (RequestContext.session_key)
        │
        ▼
register_request(session_key, request_id, user_id=…)
        │   (atomic update под _request_index_lock)
        ▼
_request_index[session_key] = {"request_id": ..., "user_id": ...}
        │
        │  invisible from outside — no public getter
        ▼
_enqueue(event):
    # 1. Explicit value wins
    if event.user_id is not None:
        pass
    # 2. Match by request_id (NOT by session_id alone)
    elif (
        event.request_id is not None
        and event.session_id is not None
    ):
        entry = self._request_index.get(event.session_id)
        if entry and entry["request_id"] == event.request_id:
            event.user_id = entry["user_id"]
    # 3. Otherwise: event.user_id remains None (no inference)
        │
        ▼
    INSERT into agent_gateway_logs (…, user_id, …)
```

**Почему matching именно по `request_id`, а не по `session_id`.**

Альтернативная формулировка «подставить `user_id` индекса при
`event.session_id in index`» создаёт security-окно: между
созданием `LogEvent` и его `_enqueue` может произойти
`register_request` для следующего request в той же `session_key`.
Без сверки по `request_id` отложенное событие `req-A` увидит
индекс, уже перезаписанный под `req-B`, и запишется с
`user_id="bob"` — то есть чужой identity. Это ровно та утечка,
которую спека закрывает в `history_search`. Поэтому matching
**обязан** идти через `event.request_id == entry["request_id"]`.

**Событие без `request_id` не получает `user_id` из индекса.**

Если `event.request_id is None`, ни одна из трёх ветвей не
срабатывает: ни explicit, ни matching. Такой event пишется с
`user_id IS NULL` и невидим для `session_scope="all"`. Это
защищает от «события-сироты», которое иначе могло бы получить
identity просто потому, что принадлежит той же сессии.

**Публичный API не расширяется.** Ранний proposal предлагал
`get_request_user_id(session_key)` — от него отказались: единственный
потребитель — это `_enqueue`; вводить публичный метод ради одного
вызова внутри сервиса — лишняя поверхность, которой могут начать
пользоваться компоненты, не имеющие отношения к безопасности.

**Атомарность `register_request`.** Парная запись `{request_id,
user_id}` обновляется одним вызовом под `_request_index_lock`.
Сам по себе lock защищает только согласованность пары внутри
индекса. Корректность выбора `user_id` для конкретного события
обеспечивается request_id matching в `_enqueue`. Это **два
независимых механизма**: lock для consistency пары, request_id
matching для security выбора.

**Явный `user_id` от producer'а приоритетнее индекса.** Это
закрывает случай subagent'а: `_SubagentLoggingHook` пишет
`LogEvent` с `user_id=parent_sender_id` явно, и автозаполнение
не подменяет его.

## history_search query model

Две взаимоисключающие ветви:

```
session_scope="current":
    session_id = _current_session_key()   (from identity-store.session_key)
    → "session_id = %s"

session_scope="all":
    user_id = _current_user_id()           (from identity-store.sender_id)
    if user_id is None: → error("missing_user_identity")
    → "user_id = %s"
```

Никаких `(%s OR session_id = %s)`, `WHERE TRUE`, `LIKE ... session_id`.
SQL собирается через список clauses с одним `WHERE` в начале и
`AND`-соединением. Helper `_current_user_id()` — приватная
функция в `history_search_tool.py`, читающая identity-store
текущего request; **никаких** обращений к `session_id`,
`chat_id`, `actor`, `payload`, `name`, и **никакого** unscoped
fallback.

## Backfill semantics

Backfill в миграции V004:

```sql
UPDATE public.agent_gateway_logs l
SET user_id = r.user_id
FROM public.agent_question_runs r
WHERE l.request_id = r.request_id
  AND l.user_id IS NULL
  AND r.user_id IS NOT NULL;
```

Ключевое — `r.user_id IS NOT NULL`. Без этого предиката
`agent_question_runs.user_id IS NULL` «пробрасывается» в
`agent_gateway_logs`, и такие строки начинают считаться
принадлежащими NULL-пользователю. NULL-пользователь не совпадает
ни с одним `sender_id`, поэтому практической утечки нет, но
**семантически** запись `user_id=NULL` в `agent_gateway_logs`
становится «backfilled из источника без identity», а не
«источника без identity вообще». Предикат `IS NOT NULL`
фиксирует, что backfill переносит только осмысленные identity.

Строки без `request_id` (NULL или несуществующий в
`agent_question_runs`) остаются `user_id IS NULL` — это
безопасное поведение, описанное в спеке.

## Security invariants

Что проверяется на уровне guard-тестов:

1. **Сгенерированный SQL и параметры** — primary guard.
   `tests/test_history_search_tool.py` через mock на `utils.db.fetch`
   фиксирует:
   - `scope="current"` → SQL содержит `session_id = %s` с
     параметром `session_key`;
   - `scope="all"` → SQL содержит `user_id = %s` с параметром
     `sender_id`;
   - `scope="all"` без identity → `fetch()` НЕ вызван, ответ
     содержит `error_type="missing_user_identity"`.
2. **Architecture guard (supplementary)**. Grep по исходнику
   `workspace/tools/history_search_tool.py` на запрещённые
   паттерны (`OR session_id = %s`, `WHERE TRUE`, `OR TRUE`,
   `IS NULL OR user_id`, `LIKE %session_id%`) — это
   **дополнительная** страховка от случайного возврата
   unscoped-формы после рефакторинга. Не заменяет проверку
   сгенерированного SQL.
3. **Stale event does not inherit next request's user_id** —
   primary logging-pipeline guard. Тест в
   `tests/test_db_logging_service.py`:
   ```
   register_request(session_key, request_id="A", user_id="alice")
   event = LogEvent(request_id="A", user_id=None)
   register_request(session_key, request_id="B", user_id="bob")
   _enqueue(event)  # event всё ещё ссылается на A
   → INSERT содержит user_id IS NULL
   → INSERT НЕ содержит "bob"
   ```
   Это **главный** тест на logging security, без него change
   принимать нельзя.
4. **Cross-user isolation** в фикстурах: alice и bob с
   разными `session_id`, проверка, что ответ alice не содержит
   ни одного event_id bob'а.
5. **Атомарность `register_request`** под одним lock'ом —
   регрессионный тест на сценарий «смена пользователя в той же
   сессии»; проверяет, что параллельный поток не видит
   смешанное состояние пары `(request_id, user_id)`.
6. **Subagent user_id** — parent alice → subagent alice (без
   утечки); previous user_id не «протекает» в next request.
7. **Event without request_id does not inherit user_id** —
   тест, что `LogEvent(request_id=None, session_id=...)` пишется
   с `user_id IS NULL` даже при наличии индекса для сессии.
8. **RequestContext identity field present** — собственный
   contract-тест в нашей зоне (см. requirement «RequestContext
   exposes user identity» в спеке), не правящий существующий
   `tests/contract/test_tools_and_context.py`.

## Non-Goals

- Менять схему `agent_question_runs` (поле `user_id` уже есть).
- Пересматривать контракт `RequestContext` (frozen dataclass).
- Менять JSON-формат ответа `history_search`.
- Вводить cursor-пагинацию, новые параметры,
  `event_type`-категории.
- Партиционирование `agent_gateway_logs` по `user_id` —
  преждевременная оптимизация.
- Заменять single-writer invariant через прямые INSERT.

## Migration Plan

**Применение (на существующем deployment'е):**

1. Merge change.
2. `python tools/migrate.py --apply` —
   `V004__agent_gateway_logs_user_id.sql`:
   ADD COLUMN (IF NOT EXISTS) + backfill UPDATE
   (с `r.user_id IS NOT NULL`) + CREATE INDEX. Идемпотентна.
3. Restart gateway. Новые события пишутся с `user_id`.
4. `history_search` возвращает события только текущего
   пользователя. Tool description обновлён.

**Rollback:** Эта change **не предоставляет обратной совместимости**
старого кода со схемой без `user_id`: новый `_insert_batch`
содержит `user_id` в списке колонок INSERT, и при отсутствии
колонки в таблице вставка упадёт. Корректный откат:

1. Сначала откатить код (revert PR с правкой `LogEvent`,
   `_insert_batch`, `_enqueue`, `register_request`,
   `_request_index`).
2. Только после отката кода — удалить колонку и индекс
   (`DROP INDEX` + `ALTER TABLE DROP COLUMN`).

Обратный порядок (DDL сначала, код потом) даёт период
неработоспособности `DbLoggingService`. Альтернатива — оставить
колонку `user_id` в БД «сиротой» (NOT NULL DEFAULT не нужен,
NULL допустим) до следующего релиза; новые события пишутся с
`user_id IS NULL`, пока старый код не пишет `user_id` вообще.

**Совместимость существующих данных:**

- Старые события с `request_id` и `agent_question_runs.user_id`
  заполняются backfill'ом.
- Старые события с `request_id` и `agent_question_runs.user_id IS NULL`
  остаются с `gateway_logs.user_id IS NULL`.
- Старые события без `request_id` остаются `user_id IS NULL`.

Все три группы корректно исключаются из `session_scope="all"`.

## Open Questions

Нет.
