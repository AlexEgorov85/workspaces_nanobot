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

Решение D4 в первоначальной версии спеки сводилось к
«автозаполнение в `_enqueue` из `_request_index`». Здесь это
фиксируется строже:

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
    if event.user_id is None and event.session_id:
        entry = _request_index.get(event.session_id)
        if entry:
            event.user_id = entry["user_id"]
        │
        ▼
    INSERT into agent_gateway_logs (…, user_id, …)
```

**Публичный API не расширяется.** Ранний proposal предлагал
`get_request_user_id(session_key)` — от него отказались: единственный
потребитель — это `_enqueue`; вводить публичный метод ради одного
вызова внутри сервиса — лишняя поверхность, которой могут начать
пользоваться компоненты, не имеющие отношения к безопасности.

**Атомарность `register_request`.** Парная запись `{request_id,
user_id}` обновляется одним вызовом под `_request_index_lock`.
Сценарий «новый request зарегистрирован в той же `session_key`,
старые события предыдущего request ещё в полёте» обрабатывается
так: события предыдущего request несут явный `LogEvent.user_id`,
заданный producer'ом при эмиссии (либо через автозаполнение по
`session_id` на момент `_enqueue`). Если `register_request` уже
перезаписал индекс к моменту `_enqueue` отложенного события — это
**security regression**, и регрессионный тест
`TestRegisterRequestAtomicUpdate` это ловит.

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
3. **Cross-user isolation** в фикстурах: alice и bob с
   разными `session_id`, проверка, что ответ alice не содержит
   ни одного event_id bob'а.
4. **Атомарность `register_request`** под одним lock'ом —
   регрессионный тест на сценарий «смена пользователя в той же
   сессии».
5. **Subagent user_id** — parent alice → subagent alice (без
   утечки); previous user_id не «протекает» в next request.

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

**Rollback:** DROP INDEX + ALTER TABLE DROP COLUMN + revert
`LogEvent.user_id` и `_request_index` storage. Старый INSERT
работает с любой схемой, у которой нет колонки `user_id`,
потому что новый код пишет её опционально.

**Совместимость существующих данных:**

- Старые события с `request_id` и `agent_question_runs.user_id`
  заполняются backfill'ом.
- Старые события с `request_id` и `agent_question_runs.user_id IS NULL`
  остаются с `gateway_logs.user_id IS NULL`.
- Старые события без `request_id` остаются `user_id IS NULL`.

Все три группы корректно исключаются из `session_scope="all"`.

## Open Questions

Нет.
