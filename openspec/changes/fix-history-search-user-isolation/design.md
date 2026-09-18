# Design — fix history_search user isolation

## Context

См. `proposal.md` (раздел Why) — `history_search` с
`session_scope="all"` сейчас возвращает **глобальный** набор событий
из-за отсутствия фильтра по `user_id`. Корень: в
`agent_gateway_logs` нет колонки `user_id`, а текущая SQL-сборка в
`workspace/tools/history_search_tool.py:290-292` снимает фильтр
`session_id` через `(%s OR session_id = %s)` при `allow_all=True`.

Существующие точки опоры (используются, не меняются):

- `nanobot.agent.tools.context.RequestContext` уже несёт
  `sender_id` (= `user_id`), `session_key` (= `channel:chat_id`),
  `chat_id`, `channel`, `message_id`, `turn_id`. Источник правды —
  канал, выставивший контекст через `bind_request_context` перед
  обработкой сообщения.
- `agent_question_runs.user_id` уже документирован как
  «ID пользователя (sender_id)» (`sql/logs/create_public_agent_question_runs.sql:40`)
  и заполняется из `sender_id` входящего сообщения. Связь с
  `agent_gateway_logs` — через `request_id`.
- `DbLoggingService.register_request` уже сохраняет `user_id` в
  `_QuestionRunRecord` и индексирует `session_key → request_id`
  (`lib/services/db_logging_service.py:165-172, 254-256`).
- `DbLoggingService._insert_batch` — единственная точка INSERT в
  `agent_gateway_logs` (см. capability `logging-db`,
  `openspec/changes/unify-agent-event-logging-pipeline`).

Ограничения:

- Python ≥ 3.14, `from __future__ import annotations` обязателен.
- nanobot 0.3.0 API (`tool_parameters`, `RequestContext`,
  `bind_request_context`, `current_request_context`) — менять нельзя,
  только читать.
- БД — PostgreSQL/Greenplum 6.5, без `ON CONFLICT`, без
  `make_interval`. Все SQL — `%s`-параметризованные. Распределение
  таблицы — `DISTRIBUTED BY (request_id)`; новый индекс должен быть
  совместим с этой раскладкой (Greenplum 6.5: индексы могут быть
  локальными по дистрибуции).
- `improve-history-search-pagination-and-logging` уже ввёл
  пагинацию, детерминированную сортировку, раздельные truncation-флаги.
  Эта change НЕ пересматривает эти решения, а только переписывает
  `WHERE`-часть SQL.

## Goals / Non-Goals

**Goals:**

- Закрыть cross-user leakage в `history_search(session_scope="all")`
  фильтром по `user_id`.
- Сделать так, чтобы любое событие request-потока (tool_call,
  tool_result, llm_call, run_finished, subagent_run_finished,
  outbound_final/delta, context_compacted, error) автоматически
  получало `user_id` без необходимости пробрасывать его явно из
  каждого producer'а.
- Сохранить `user_id` как internal security attribute: tool API
  остаётся прежним, агент не может подменить `user_id` параметром.
- Сохранить обратную совместимость поведения `session_scope="current"`.
- Обеспечить миграцию существующих событий (backfill по `request_id`)
  и безопасное исключение событий без `user_id` из `session_scope="all"`.

**Non-Goals:**

- Менять схему `agent_question_runs` (поле `user_id` уже есть).
- Пересматривать контракт `RequestContext` (sender_id уже есть).
- Менять JSON-формат ответа `history_search` (он зафиксирован в
  capability `tools-history-search`).
- Вводить cursor-пагинацию, новые параметры, новые
  `event_type`-категории — это не пересекается с этой change.
- Переносить логику фильтрации в SQL view / materialized view —
  чрезмерное усложнение для текущего объёма данных.

## Decisions

### D1. `session_scope` принимает ровно две взаимоисключающие ветви SQL

Текущая сборка WHERE-предикатов в `history_search_tool.py:286-329`
содержит одну универсальную форму

```python
clauses.append("(%s OR session_id = %s)")
params.append(allow_all)
params.append(session_id or "")
```

— она снимает фильтр `session_id` при `allow_all=True`. Это и есть
источник утечки. Решение: две явные ветви, одна из которых
**всегда присутствует**:

```python
if session_scope == "current":
    session_id = _current_session_key()
    if not session_id:
        return self._error("missing_session_identity", "...")
    clauses.append("session_id = %s")
    params.append(session_id)
elif session_scope == "all":
    user_id = _current_user_id()
    if not user_id:
        return self._error(
            "missing_user_identity",
            "history_search(session_scope='all') требует sender_id; "
            "запрос вне request context не поддерживается",
        )
    clauses.append("user_id = %s")
    params.append(user_id)
else:
    return self._error("invalid_session_scope", f"unknown scope: {session_scope!r}")
```

Конструкция `WHERE (%s OR session_id = %s)` удаляется из кода
полностью. Это закрывает failure mode вида «забыли явный параметр —
фильтр снялся».

**Альтернативы:**

- Универсальная форма с явным `params.append(allow_all)` и
  фиксированной проверкой «`allow_all=True` ⇒ подставить
  `current_user_id` или отказать» — добавляет runtime-ветвление
  в SQL-builder и оставляет риск регрессии при добавлении
  параметров.
- Передавать `user_id`/`session_id` явно из callers — нарушает
  контракт «tool получает identity из request context» и создаёт
  второй источник правды.

Выбрано: две ветви в коде tool'а, никакого unscoped fallback.

### D2. Источник `user_id` для `history_search` — только `RequestContext.sender_id`

`_current_user_id()` читает
`nanobot.agent.tools.context.current_request_context().sender_id`:

```python
def _current_user_id() -> str | None:
    try:
        from nanobot.agent.tools.context import current_request_context
        ctx = current_request_context()
    except Exception:
        return None
    return getattr(ctx, "sender_id", None) if ctx is not None else None
```

Если `current_request_context()` бросает (например, тест без
контекста) или возвращает `None` / `ctx.sender_id is None` —
`_current_user_id()` возвращает `None`. Это **единственный** путь
получения `user_id` в `history_search`. Никаких парсингов
`session_id`, `chat_id`, `payload.sender_id`, `actor` или
`name`-имён producer'ов. Это закрывает класс утечек вида
«извлекли `chat_id` из `session_id` и наивно посчитали его
пользователем».

**Альтернативы:**

- Передавать `user_id` через `ctx._settings_ref.tools.history_search.user_id`
  — создаёт security-чувствительный параметр в конфиге и
  требует ручной настройки на каждом инстансе.
- Использовать контекст `DbLoggingService._request_index` —
  два разных механизма context lookup, расходятся при изменениях.

Выбрано: `RequestContext.sender_id`, как уже сделано для
`session_key` в `_current_session_key()` (та же логика, тот же
nanobot API).

### D3. Колонка `user_id` в `agent_gateway_logs` + индекс

DDL:

```sql
ALTER TABLE public.agent_gateway_logs
    ADD COLUMN IF NOT EXISTS user_id VARCHAR(256);

CREATE INDEX IF NOT EXISTS agent_gateway_logs_user_id_timestamp_idx
    ON public.agent_gateway_logs (user_id, "timestamp" DESC);
```

Колонка рядом с `request_id` (а не с payload/metadata) — она
второй идентификатор контекста request'а и должна быть в одной
группе для grep'абельности. Комментарий:

```
ID пользователя, которому принадлежит событие.
Источник — user_id из request context (RequestContext.sender_id,
равен agent_question_runs.user_id для соответствующего request_id).
```

Индекс — `(user_id, "timestamp" DESC)` под `WHERE user_id = %s
ORDER BY "timestamp" DESC, "id" DESC LIMIT %s OFFSET %s`. На
Greenplum 6.5 индекс по `user_id` может быть некластеризованным,
но локальным по дистрибуции `(request_id)`; для типичных объёмов
(десятки тысяч строк на пользователя) планировщик использует
bitmap-сканирование по индексу. Точный план зависит от
статистики, и это предмет отдельной оптимизации (не блокер этой
change).

Backfill в той же миграции:

```sql
UPDATE public.agent_gateway_logs l
SET user_id = r.user_id
FROM public.agent_question_runs r
WHERE l.request_id = r.request_id
  AND l.user_id IS NULL;
```

Строки без `request_id` или без соответствующей записи в
`agent_question_runs` остаются с `user_id IS NULL` и не
участвуют в `session_scope="all"`. Это безопасный дефолт: лучше
недопоказать, чем показать чужое.

**Альтернативы:**

- `CREATE INDEX CONCURRENTLY` — Greenplum 6.5 поддерживает
  `CONCURRENTLY`, но требует прав на vacuum; для первичного
  развёртывания (когда таблица ещё маленькая) `CONCURRENTLY`
  избыточен. Идемпотентность через `IF NOT EXISTS` компенсирует
  повторный запуск миграции.
- Backfill через batched UPDATE по id-диапазонам — для текущих
  объёмов (десятки-сотни тысяч строк) одиночный UPDATE
  достаточно.
- Партиционирование по `user_id` — преждевременная оптимизация,
  не обоснована текущими объёмами.

Выбрано: простой ADD COLUMN + backfill UPDATE + INDEX.

### D4. `LogEvent.user_id` подтягивается из request-индекса

Расширение `LogEvent`:

```python
@dataclass
class LogEvent:
    event_type: str
    level: str = "INFO"
    session_id: str | None = None
    user_id: str | None = None  # NEW
    channel: str | None = None
    actor: str | None = None
    ...
```

`_request_index` хранит не только `request_id`, но и `user_id`:

```python
self._request_index: dict[str, dict[str, str | None]] = {}
# ключ = session_key, значение = {"request_id": ..., "user_id": ...}
```

`register_request` обновляет обе записи:

```python
self._request_index[session_key] = {
    "request_id": request_id,
    "user_id": user_id,
}
```

В `_enqueue` (перед `put_nowait`):

```python
event.queued_at = time.time()
if event.user_id is None and event.session_id:
    entry = self._request_index.get(event.session_id)
    if entry:
        event.user_id = entry.get("user_id")
self._queue.put_nowait(event)
```

`event.user_id`, заданный явно producer'ом, имеет приоритет
(явное > выведенное). Это покрывает сценарий «subagent logging
передаёт `user_id` родительского request'а» — он пишет
`LogEvent.user_id` явно в `LogEvent.__init__`, и `_enqueue` его
не перезаписывает.

**Альтернативы:**

- Прокидывать `user_id` явно из каждого producer'а (`log_tool_call`,
  `log_inbound`, ...) — N мест правок, риск пропустить.
- Хранить `user_id` в `RequestContext` напрямую через mutation —
  `RequestContext` помечен `frozen=True`, мутация невозможна.
- Расширить `RequestContext` новым полем — менять контракт
  nanobot API, что запрещено.

Выбрано: индекс `_request_index` + автозаполнение в `_enqueue`.

### D5. Единственный writer для `agent_gateway_logs`

`DbLoggingService._insert_batch` остаётся единственным INSERT'ом.
Расширение SQL:

```sql
INSERT INTO "<schema>"."<table>"
    (id, level, event_type, user_id, session_id, channel,
     actor, summary, payload, metadata, request_id, name)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
```

`user_id` ставится рядом с `session_id` в списке колонок — это
позиционно соответствует порядку в DDL-комментарии «расположение
рядом с идентификаторами контекста».

`ContextCompactionService._record_event_log` (после завершения
`unify-agent-event-logging-pipeline`) пишет `context_compacted`
через `db_logging_service.log_event(LogEvent(...))`. `LogEvent`
содержит `user_id`, если `_record_event_log` подтянул его из
request context (например, через `current_request_context()` или
через явное `event.user_id` от caller'а). Если compaction вне
request'а (теоретический кейс) — `user_id=None`, событие остаётся
в БД, но невидимо для `session_scope="all"`.

Никаких вторых writers (прямых INSERT в обход `DbLoggingService`)
эта change не вводит. Существующее ограничение из
`unify-agent-event-logging-pipeline` остаётся в силе.

**Альтернативы:**

- Второй путь через прямой INSERT — нарушает single-writer invariant.
- Денормализация `user_id` в JSONB-payload события — теряется
  индекс, нельзя строить `(user_id, timestamp)`.

Выбрано: колонка + единый INSERT.

### D6. Tool API остаётся неизменным

`history_search` не получает новых параметров. `user_id` —
внутренний security attribute, а не инструментальный параметр.
JSON-schema (`tool_parameters`), `tool_description` и runtime
параметры `execute(...)` остаются прежними. Это значит:

- Агент не может вызвать `history_search(user_id="bob")` —
  параметра нет в schema.
- Агент не может «обойти» изоляцию через `session_scope="all"` —
  фильтр по `user_id` всегда установлен (или запрос отвергнут).
- Агент видит `session_scope` в ответе (`"current"` / `"all"`),
  но не `user_id`.

Это закрывает класс атак вида «LLM prompt-injection просит
передать `user_id` другого пользователя».

**Альтернативы:**

- Добавить `user_id` как явный параметр с пометкой «internal» —
  увеличивает поверхность атаки и усложняет schema.
- Использовать `name`-фильтр на стороне БД — `name` это
  `tool_name`/`model`/`task_id`, не пользователь.

Выбрано: `user_id` остаётся внутренним, tool API не меняется.

### D7. Архитектурный guard на «LIKE session_id» / unscoped WHERE

`tests/test_architecture_guards.py` (или существующий файл с
аналогичной ролью — проверить наличие) грепит исходники:

```python
SOURCE = Path("workspace/tools/history_search_tool.py").read_text(encoding="utf-8")
forbidden_patterns = [
    "OR session_id = %s",
    "LIKE %session_id%",
    "session_id LIKE",
    "OR TRUE",
]
for pat in forbidden_patterns:
    assert pat not in SOURCE, f"history_search: forbidden pattern {pat!r} вернулся в код"
```

Тест запускается в pytest как часть `tests/test_architecture_guards.py`.
Если кто-то в будущем добавит «удобный» unscoped fallback — тест
упадёт.

Дополнительный guard на архитектуру logging-пайплайна уже есть в
`unify-agent-event-logging-pipeline` (нет прямого INSERT в
`agent_gateway_logs` вне `DbLoggingService`); этот guard
дополняется проверкой «нет INSERT/UPDATE/DELETE в `history_search`».

**Альтернативы:**

- Полагаться на code review — не работает для архитектурных
  инвариантов, которые забываются.
- Запрет через pre-commit hook — полезно, но требует отдельного
  tooling; тест в pytest — это contract test, который ловит
  регрессию при CI без pre-commit.

Выбрано: pytest test, грепающий исходник.

## Risks / Trade-offs

- **R1**: Backfill UPDATE может занять время на больших таблицах.
  → **Mitigation**: миграция идемпотентна (`WHERE l.user_id IS NULL`);
  для прод-развёртываний оператор может выполнить миграцию вне
  пиковой нагрузки. Дефолт Greenplum-блокировки достаточно для
  наших объёмов; если будет deadlock — добавляется batched UPDATE,
  но это не блокирует текущую change.
- **R2**: Индекс `(user_id, "timestamp" DESC)` увеличивает размер
  таблицы и замедляет INSERT.
  → **Mitigation**: 1024-байтный `VARCHAR(256)` × число строк на
  пользователя; для текущих объёмов (десятки тысяч строк) размер
  индекса в пределах мегабайт. INSERT замедление — линейное по
  размеру индекса, но INSERT в этой таблице уже идёт батчами
  через `execute_batch`.
- **R3**: После смены семантики `session_scope="all"` существующие
  агенты, рассчитывавшие на глобальный набор, начнут получать
  «не то» или `missing_user_identity`.
  → **Mitigation**: это намеренное поведенческое изменение
  (security-фикс); никакого режима совместимости не предусмотрено.
  Tool description и `workspace/TOOLS.md` явно фиксируют новую
  семантику.
- **R4**: Если `RequestContext.sender_id` не заполняется каким-то
  каналом, `session_scope="all"` всегда возвращает ошибку в этом
  канале.
  → **Mitigation**: каналы `postgres_channel.py` и `redis_channel.py`
  выставляют `sender_id` через `RequestContext` (это уже сделано
  для других tool'ов, см. `active_files_hook.py`); для CLI-режима
  `sender_id="cli"` или фактический OS-user. Проверка каналов —
  отдельная регрессия в `tests/test_request_context_propagation.py`
  (задача § 5.x).
- **R5**: Tool description обновляется — расход с JSON-schema на
  одну фразу больше.
  → **Mitigation**: описание уже сейчас описывает `session_scope`,
  новая формулировка лишь уточняет «all = все сессии текущего
  пользователя».
- **R6**: Если устаревшие события остались без `request_id` (например,
  до введения `_QuestionRunRecord`), backfill их не покроет, и
  `session_scope="all"` их не покажет.
  → **Mitigation**: документировано как ожидаемое поведение
  («events without identity are excluded»); в реальности таких
  событий нет после введения `agent_question_runs`. Если окажется,
  что есть — это отдельная data-quality задача.

## Migration Plan

**Применение (на существующем deployment'е):**

1. Merge change.
2. Применить `tools/migrate.py --apply` — выполнится
   `V004__agent_gateway_logs_user_id.sql`: ADD COLUMN + backfill UPDATE
   + CREATE INDEX. Миграция идемпотентна (`IF NOT EXISTS` /
   `IS NULL`).
3. Перезапустить gateway. Новые события начинают писаться с
   `user_id`.
4. `history_search` теперь возвращает события только текущего
   пользователя. Tool description обновлён.

**Rollback:**

- `DROP INDEX agent_gateway_logs_user_id_timestamp_idx;`
- `ALTER TABLE public.agent_gateway_logs DROP COLUMN user_id;`
- Revert `DbLoggingService` (`LogEvent.user_id`, `_request_index`
  storage) — старый INSERT работает с любой схемой, у которой
  нет колонки `user_id`, потому что новый код пишет её
  опционально (`%s` с `None` как параметр).
- Revert `history_search_tool.py` — две ветви возвращаются в
  одну (`OR session_id`), `_current_user_id()` удаляется.

Время отката — минуты (один revert PR + два DDL).

**Совместимость существующих данных:**

- Старые события с `request_id` заполняются backfill'ом — видимы
  в `session_scope="all"` у соответствующего пользователя.
- Старые события без `request_id` остаются с `user_id IS NULL`
  и невидимы в `session_scope="all"`.
- Это безопаснее, чем показывать их в чужой выборке.

## Open Questions

Нет. Все архитектурные развилки разрешены в D1–D7. Технические
детали реализации (например, точные имена индексов, размер
`per_event_cap`, формат `error_type`-строк) не меняют контракт
specs и уточняются в `tasks.md`.
