# Tasks — fix history_search user isolation

Каждый пункт — конкретное изменение в файле + верификация. Детали
контракта — в `specs/tools-history-search/spec.md`; мотивация и
non-goals — в `proposal.md`; архитектурные решения — в `design.md`.

## 1. DDL и миграция

- [ ] 1.1 `sql/logs/create_public_agent_gateway_logs.sql`: добавить
      `user_id VARCHAR(256)` рядом с `request_id`/`session_id`/
      `channel`/`actor`/`name`; `COMMENT ON COLUMN` с явным
      указанием источника; `CREATE INDEX ... (user_id,
      "timestamp" DESC)` + `COMMENT ON INDEX`.
- [ ] 1.2 `sql/migrations/V004__agent_gateway_logs_user_id.sql`:
      - `ALTER TABLE ... ADD COLUMN IF NOT EXISTS user_id VARCHAR(256);`
      - `COMMENT ON COLUMN ... user_id IS '…';`
      - `UPDATE … FROM agent_question_runs r WHERE l.request_id = r.request_id AND l.user_id IS NULL AND r.user_id IS NOT NULL;`
      - `CREATE INDEX IF NOT EXISTS agent_gateway_logs_user_id_timestamp_idx ON public.agent_gateway_logs (user_id, "timestamp" DESC);`
      - `COMMENT ON INDEX ... IS '…';`
      Применить локально `python tools/migrate.py --apply`. Все
      шаги идемпотентны.

## 2. LogEvent и DbLoggingService

- [ ] 2.1 `lib/services/db_logging_service.py`: добавить
      `user_id: str | None = None` в `LogEvent` после `session_id`.
      Обновить docstring: «`user_id` хранится в
      `agent_gateway_logs` явно как security boundary для
      `history_search(session_scope="all")` — это
      намеренное исключение из правила "identity только в
      `agent_question_runs`"».
- [ ] 2.2 Тот же файл: расширить `_request_index` с
      `dict[str, str]` (только request_id) на
      `dict[str, dict[str, str | None]]` с
      `{"request_id": ..., "user_id": ...}`. `get_request_id`
      читает `entry["request_id"]`. **Публичный
      `get_request_user_id()` НЕ вводится** — индекс остаётся
      internal.
- [ ] 2.3 Тот же файл, `register_request`: атомарно (под
      `_request_index_lock`) обновлять обе записи индекса.
      Никакого публичного API для чтения user_id — только
      `_enqueue` имеет право заглядывать в индекс.
- [ ] 2.4 Тот же файл, `_enqueue`: реализовать три ветви
      (см. design.md «Logging propagation»):
      ```python
      if event.user_id is not None:
          pass  # Explicit value wins
      elif event.request_id is not None and event.session_id is not None:
          entry = self._request_index.get(event.session_id)
          if entry and entry["request_id"] == event.request_id:
              event.user_id = entry["user_id"]
      # else: event.user_id остаётся None
      ```
      Matching идёт **только по `event.request_id == entry["request_id"]`**,
      НЕ по `session_id` alone. Явное значение producer'а имеет
      приоритет.
- [ ] 2.5 Тот же файл, `_insert_batch`: расширить INSERT —
      добавить `user_id` в список колонок (порядок: id, level,
      event_type, user_id, session_id, channel, actor, summary,
      payload, metadata, request_id, name) и в список параметров.
- [ ] 2.6 `tests/test_db_logging_service.py`:
      - `test_log_event_user_id_reaches_insert` —
        мок `utils.db.run`/`execute_batch` фиксирует, что
        `LogEvent(user_id="alice")` доходит до INSERT.
      - `test_enqueue_fills_user_id_when_request_id_matches`:
        register_request → enqueue пустого `user_id` с тем же
        `request_id` → INSERT содержит `user_id`.
      - `test_explicit_user_id_overrides_index`: register
        alice, enqueue с `user_id="bob"` → INSERT = "bob".
      - `test_stale_event_does_not_inherit_next_request_user_id`:
        register A/alice, создать LogEvent(request_id=A,
        user_id=None) **до повторного register_request**;
        register B/bob; затем _enqueue того же события
        → INSERT содержит `user_id IS NULL`, НЕ "bob".
        **Это primary logging-security тест, без него change
        не считается готовой.**
      - `test_event_without_request_id_does_not_inherit_user_id`:
        register alice → enqueue `LogEvent(request_id=None,
        session_id=telegram:123, user_id=None)` → INSERT
        содержит `user_id IS NULL`.
      - `test_register_request_updates_pair_atomically`:
        mock с thread: параллельный reader индекса во время
        `register_request` B/bob видит либо полностью A/alice,
        либо полностью B/bob — не смесь.
      - `test_no_public_get_request_user_id`: проверить, что у
        `DbLoggingService` нет публичного метода
        `get_request_user_id` (или эквивалента вроде
        `lookup_user_id`/`resolve_user_id`).

## 3. history_search: фильтрация и жёсткий отказ

- [ ] 3.1 `workspace/tools/history_search_tool.py:279-292`:
      удалить конструкцию
      `clauses.append("(%s OR session_id = %s)")` целиком
      (включая параметры `allow_all`, `session_id or ""`).
- [ ] 3.2 Тот же файл: добавить helper `_current_user_id() -> str | None`,
      читающий identity-store текущего request через
      `nanobot.agent.tools.context.current_request_context()`. В
      nanobot 0.3.0 это `ctx.sender_id`; при недоступности
      контекста или `sender_id is None` — вернуть `None`.
      Конкретное имя поля инкапсулируется в одной функции;
      никаких внешних обращений к `sender_id`/`session_id`/
      `chat_id`/`actor`/`payload`.
- [ ] 3.3 Тот же файл, `execute()`: две взаимоисключающие ветви
      SQL:
      - `session_scope="current"`: предикат
        `session_id = %s` с параметром из
        `_current_session_key()`; при отсутствии —
        `error_type="missing_session_identity"` (SQL не выполняется).
      - `session_scope="all"`: предикат `user_id = %s` с
        параметром из `_current_user_id()`; при отсутствии —
        `error_type="missing_user_identity"` (SQL не выполняется).
      - Невалидный `session_scope` →
        `error_type="invalid_session_scope"`.
- [ ] 3.4 Тот же файл, `description` property: явно сказать
      «`session_scope="all"` — все сессии текущего пользователя
      (не глобально). При отсутствии identity —
      `missing_user_identity`».
- [ ] 3.5 `tests/test_history_search_tool.py`,
      класс `TestUserIsolation`:
      - `test_all_scope_filters_by_user_id`: alice →
        только её события.
      - `test_all_scope_excludes_other_users`: bob →
        только его события; ни одного event_id alice.
      - `test_all_scope_missing_user_returns_error`: без
        `RequestContext` → `status="error"`,
        `error_type="missing_user_identity"`; mock `fetch` НЕ
        вызван.
      - `test_current_scope_filters_by_session_id` (регрессия):
        поведение `scope="current"` сохранено.
      - `test_response_does_not_leak_user_id`: ни на одном
        уровне JSON нет ключа `user_id`.
      - `test_current_scope_does_not_compare_user_id`:
        событие с `user_id="stale"` в той же сессии —
        возвращается через `scope="current"`.
- [ ] 3.6 Тот же файл, класс `TestGeneratedSqlGuard` (primary
      guard):
      - mock `utils.db.fetch` перехватывает SQL и params;
      - `test_scope_current_uses_session_id_predicate`:
        SQL содержит `session_id = %s`, параметр = session_key;
      - `test_scope_all_uses_user_id_predicate`: SQL содержит
        `user_id = %s`, параметр = sender_id;
      - `test_scope_all_missing_user_does_not_call_fetch`:
        `fetch()` НЕ вызван, ответ содержит `missing_user_identity`;
      - `test_scope_current_missing_session_does_not_call_fetch`:
        симметрично.

## 4. Producer'ы: user_id в каждом LogEvent

- [ ] 4.1 `lib/hooks/database_logging_hook.py:178-200`,
      `_factory`: при создании fallback `register_request`
      пробрасывать `user_id` из identity-store текущего request
      (`current_request_context().sender_id`), при отсутствии —
      `None` (событие остаётся `user_id IS NULL` и не попадает
      в `scope="all"`).
- [ ] 4.2 `lib/services/runtime_patcher.py:_SubagentLoggingHook`:
      subagent эмиттирует `subagent_run_finished` через
      `LogEvent` с **явным** `user_id=parent_sender_id`.
      Покрыть `tests/test_subagent_logging.py`:
      - `test_subagent_inherits_parent_user_id`:
        parent alice → subagent → INSERT содержит "alice";
      - `test_previous_request_user_does_not_leak_to_subagent`:
        previous request = alice, current request = bob,
        subagent текущего request → INSERT содержит "bob".
- [ ] 4.3 `lib/services/context_compaction.py:_record_event_log`:
      `context_compacted` пишется через `LogEvent.user_id`
      из identity-store текущего request. При отсутствии —
      `user_id=None`. Покрыть
      `tests/test_context_compaction.py`:
      `test_context_compacted_event_carries_user_id`.
- [ ] 4.4 Регрессия в `tests/test_hooks_database_logging.py`:
      тест на `run_finished` с `user_id` доходит до INSERT.

## 5. Архитектурный guard

- [ ] 5.1 `tests/test_architecture_guards.py` (или существующий
      файл): primary guard — **сгенерированный** SQL и параметры
      проверяются в `TestGeneratedSqlGuard` (см. 3.6). Это
      primary security check, не grep.
- [ ] 5.2 Тот же файл: **supplementary** guard
      `TestHistorySearchSourceGuard` — грепит исходник
      `workspace/tools/history_search_tool.py` на запрещённые
      паттерны (`OR session_id = %s`, `LIKE %session_id%`,
      `session_id LIKE`, `OR TRUE`, `WHERE TRUE`,
      `IS NULL OR user_id`). Это страховка от регрессии после
      рефакторинга, не primary check.
- [ ] 5.3 Существующий guard из
      `unify-agent-event-logging-pipeline` (нет прямого INSERT в
      `agent_gateway_logs` вне `DbLoggingService`): остаётся
      зелёным — никаких изменений в single-writer invariant.
- [ ] 5.4 Новый contract-тест
      `tests/contract/test_history_search_identity_contract.py`:
      импортирует `nanobot.agent.tools.context.RequestContext`,
      проверяет наличие поля `sender_id` с типом `str | None`
      через `dataclasses.fields()`. Тест НЕ правит существующий
      `tests/contract/test_tools_and_context.py` — это наш
      dependency contract, фиксирующий использование
      `RequestContext.sender_id` как identity-store. Если в
      будущей версии nanobot поле будет переименовано —
      адаптация делается через alias в `_current_user_id()`,
      а этот тест переписывается в том же change, который
      обновляет nanobot-зависимость.

## 6. Фикстуры и сценарии

- [ ] 6.1 `tests/fixtures/history_search/gateway_logs.jsonl`:
      добавить сессии с единым naming:
      - `telegram:alice_1` (`user_id="alice"`), несколько
        событий разных типов;
      - `telegram:alice_2` (`user_id="alice"`);
      - `telegram:bob_1` (`user_id="bob"`);
      - `telegram:bob_2` (`user_id="bob"`).
      Существующие события остаются (для них проверяется
      «без `user_id` → нет в `scope=all`»).
- [ ] 6.2 `tests/fixtures/history_search/scenarios.json`:
      добавить:
      - `scope_current_user_a`: `session_scope="current"`,
        `session_key="telegram:alice_1"` → только события
        alice/session_1.
      - `scope_all_user_a`: `session_scope="all"`, identity =
        alice → alice/session_1 + alice/session_2; **исключая**
        bob/session_1 и bob/session_2.
      - `scope_all_user_b`: identity = bob → только bob.
      - `scope_all_cross_user_isolation`: alice запрашивает
        общий текст (например, "contract"); возвращаются только
        её события.
      - `scope_all_missing_user`: без request context →
        `expected_status="error"`,
        `expected_error_type="missing_user_identity"`.
      Существующие сценарии с `scope="all"` без context
      переводятся в failure-сценарии
      (`expected_error_type="missing_user_identity"`) — иначе
      они начнут падать после смены семантики.

## 7. Документация

- [ ] 7.1 `workspace/TOOLS.md` секция `history_search`:
      `current` = текущая сессия;
      `all` = все сессии текущего пользователя;
      отсутствие identity → `missing_user_identity`;
      явное указание: `history_search` не выполняет глобальный
      поиск по всем пользователям.
- [ ] 7.2 `docs/architecture/HISTORY_SEARCH_ANALYSIS.md`:
      пометить cross-user leakage как **закрытый**, ссылка на
      эту change.
- [ ] 7.3 `CHANGELOG.md` `[Unreleased]`: категория `Security`
      (cross-user isolation fix) и `Changed` (новая семантика
      `scope="all"`, новая колонка, миграция V004).

## 8. Регрессия и валидация

- [ ] 8.1 `pytest tests/test_history_search_tool.py` — все
      тесты (старые + `TestUserIsolation` + `TestGeneratedSqlGuard`)
      зелёные.
- [ ] 8.2 `pytest tests/test_db_logging_service.py` — все тесты
      на `user_id` и атомарность зелёные.
- [ ] 8.3 `pytest tests/test_hooks_database_logging.py` —
      регрессия `run_finished` с `user_id` зелёная.
- [ ] 8.4 `pytest tests/test_subagent_logging.py` —
      subagent parent-child `user_id` зелёная.
- [ ] 8.5 `pytest tests/test_context_compaction.py` —
      `context_compacted` с `user_id` зелёная.
- [ ] 8.6 `pytest tests/test_architecture_guards.py` —
      primary + supplementary guards зелёные.
- [ ] 8.7 `pytest tests/` — никакой регрессии в смежных
      подсистемах.
- [ ] 8.7a `pytest tests/contract/test_history_search_identity_contract.py`
      — contract-тест на наличие `RequestContext.sender_id`
      зелёный.
- [ ] 8.8 `openspec.cmd validate fix-history-search-user-isolation`
      → «valid».
- [ ] 8.9 Smoke `python cli_agent.py --profile=test --smoke` →
      `OK_SMOKE_COMPLETE`; `history_search` присутствует в
      реестре.

## Definition of Done

Все пункты одновременно:

- [ ] `agent_gateway_logs.user_id` существует (DDL + V004).
- [ ] DDL содержит индекс под `user_id + timestamp`.
- [ ] `LogEvent.user_id` доходит до INSERT (single-writer).
- [ ] `_request_index` хранит пару `{request_id, user_id}`,
      обновляется атомарно.
- [ ] `_enqueue` автозаполняет `user_id` из индекса **только
      при совпадении `event.request_id == entry["request_id"]`**;
      explicit value побеждает; event без `request_id` не
      получает user_id из индекса.
- [ ] Регрессионный тест stale event
      (`test_stale_event_does_not_inherit_next_request_user_id`)
      зелёный — это primary logging-security acceptance.
- [ ] Contract-тест `RequestContext.sender_id` зелёный
      (наш dependency contract, не правящий чужой
      `test_tools_and_context.py`).
- [ ] Нет публичного `get_request_user_id()`.
- [ ] `session_scope="current"` фильтрует по `session_id`,
      `user_id` не участвует.
- [ ] `session_scope="all"` фильтрует по `user_id`.
- [ ] Нет unscoped-формы (`(%s OR session_id = %s)`,
      `WHERE TRUE`, `LIKE ... session_id`) — primary guard
      проверяет сгенерированный SQL.
- [ ] `all` без identity → `missing_user_identity`, fetch не
      вызван.
- [ ] Backfill переносит только `agent_question_runs.user_id IS
      NOT NULL`; `gateway_logs.user_id IS NULL` остаётся `NULL`.
- [ ] Регрессионный тест на смену пользователя в одной
      `session_key` зелёный.
- [ ] Регрессионный тест subagent: parent alice + subagent
      alice; previous alice + current bob + subagent → bob.
- [ ] Tool API не меняется: `user_id` не параметр tool.
- [ ] Payload ответа не утекает `user_id`.
- [ ] `workspace/TOOLS.md`, `HISTORY_SEARCH_ANALYSIS.md`,
      `CHANGELOG.md` обновлены.
- [ ] `openspec.cmd validate` зелёный.
