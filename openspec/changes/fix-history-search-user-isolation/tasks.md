# Tasks — fix history_search user isolation

## 1. DDL и миграция

- [ ] 1.1 В `sql/logs/create_public_agent_gateway_logs.sql` добавить
      колонку `user_id VARCHAR(256)` рядом с `request_id`/`session_id`/
      `channel`/`actor`/`name`. Комментарий:
      «ID пользователя, которому принадлежит событие. Источник —
      user_id из request context (RequestContext.sender_id, равен
      agent_question_runs.user_id для соответствующего request_id).»
      Добавить `CREATE INDEX ... ON public.agent_gateway_logs
      (user_id, "timestamp" DESC)` с явным комментарием
      «обслуживает history_search(session_scope="all")».

- [ ] 1.2 Создать `sql/migrations/V004__agent_gateway_logs_user_id.sql`:
      - `ALTER TABLE public.agent_gateway_logs ADD COLUMN IF NOT
        EXISTS user_id VARCHAR(256);`
      - `COMMENT ON COLUMN public.agent_gateway_logs.user_id IS
        'ID пользователя, которому принадлежит событие. Источник —
        user_id из request context (RequestContext.sender_id,
        равен agent_question_runs.user_id для соответствующего
        request_id).';`
      - `UPDATE public.agent_gateway_logs l SET user_id = r.user_id
        FROM public.agent_question_runs r WHERE l.request_id =
        r.request_id AND l.user_id IS NULL;`
      - `CREATE INDEX IF NOT EXISTS
        agent_gateway_logs_user_id_timestamp_idx ON
        public.agent_gateway_logs (user_id, "timestamp" DESC);`
      - `COMMENT ON INDEX
        agent_gateway_logs_user_id_timestamp_idx IS 'Обслуживает
        history_search(session_scope=''all'') — фильтр по user_id
        с обратной сортировкой по timestamp.';`
      Все шаги идемпотентны (`IF NOT EXISTS` / `IS NULL`). Применить
      локально через `python tools/migrate.py --apply`.

## 2. LogEvent и DbLoggingService

- [ ] 2.1 В `lib/services/db_logging_service.py` добавить в dataclass
      `LogEvent` поле `user_id: str | None = None` сразу после
      `session_id`. Обновить docstring: «Контекст пользователя
      (`user_id`) подтягивается из request index при `_enqueue`,
      если producer не задал его явно».

- [ ] 2.2 Расширить `DbLoggingService._request_index` с
      `dict[str, str | None]` на `dict[str, dict[str, str | None]]`
      со значениями `{"request_id": ..., "user_id": ...}`. Обновить
      `register_request` так, чтобы индекс хранил обе записи, и
      добавить публичный метод `get_request_user_id(session_key)
      -> str | None`. Сохранить обратную совместимость
      `get_request_id` (читает `entry["request_id"]`).

- [ ] 2.3 В `DbLoggingService._enqueue` перед `put_nowait` подтянуть
      `user_id` из `_request_index`, если `event.user_id is None`
      и `event.session_id` присутствует в индексе:
      ```python
      if event.user_id is None and event.session_id:
          entry = self._request_index.get(event.session_id)
          if entry:
              event.user_id = entry.get("user_id")
      ```
      Явное значение producer'а имеет приоритет (не перезаписывается).

- [ ] 2.4 Расширить `DbLoggingService._insert_batch` INSERT —
      добавить `user_id` в список колонок и в список параметров
      (порядок: id, level, event_type, user_id, session_id, channel,
      actor, summary, payload, metadata, request_id, name). Файл
      `lib/services/db_logging_service.py:749-754`.

- [ ] 2.5 Покрыть `tests/test_db_logging_service.py` сценариями:
      - `LogEvent(user_id="alice")` доходит до INSERT (мок SQL capture).
      - пустой `LogEvent.user_id` подтягивается из request index
        при `_enqueue`.
      - явный `LogEvent.user_id` имеет приоритет над индексом.
      - `register_request` сохраняет `user_id` в индексе и
        `get_request_user_id` его возвращает.

## 3. history_search: фильтрация по user_id и жёсткий отказ

- [ ] 3.1 В `workspace/tools/history_search_tool.py` заменить
      SQL-сборку на две явные взаимоисключающие ветви
      (`session_scope="current"` → `session_id`, `"all"` →
      `user_id`). Удалить конструкцию
      `clauses.append("(%s OR session_id = %s)")` целиком
      (включая параметры `allow_all`, `session_id or ""`). Файл
      `workspace/tools/history_search_tool.py:279-292`.

- [ ] 3.2 Добавить helper `_current_user_id() -> str | None`,
      читающий `nanobot.agent.tools.context.current_request_context
      ().sender_id`. Если контекст недоступен или `sender_id is None`
      — вернуть `None`. Не извлекать `user_id` из `session_id`,
      `chat_id`, `actor`, `payload`.

- [ ] 3.3 При `session_scope="all"` и `_current_user_id() is None`
      возвращать JSON
      `{"status": "error", "error_type": "missing_user_identity",
      "message": "history_search(session_scope='all') требует
      идентификатора пользователя (RequestContext.sender_id)."}`.
      SQL-запрос НЕ выполняется.

- [ ] 3.4 При `session_scope="current"` и `_current_session_key()
      is None` возвращать JSON
      `{"status": "error", "error_type": "missing_session_identity",
      "message": "history_search(session_scope='current') требует
      активной сессии (RequestContext.session_key)."}`.
      SQL-запрос НЕ выполняется. Это закрывает симметричный кейс
      (тесты/standalone без контекста).

- [ ] 3.5 Обновить tool description (`description` property
      `HistorySearchTool`): явно сказать
      «`session_scope="all"` — все сессии текущего пользователя
      (НЕ глобально). При отсутствии идентификатора пользователя
      возвращается ошибка `missing_user_identity`.»

- [ ] 3.6 Покрыть `tests/test_history_search_tool.py` классом
      `TestUserIsolation`:
      - `test_all_scope_filters_by_user_id`: alice вызывает
        `scope="all"` → только события с `user_id="alice"`.
      - `test_all_scope_excludes_other_users`: bob вызывает
        `scope="all"` → нет событий alice.
      - `test_all_scope_missing_user_returns_error`: вызов без
        `RequestContext` → JSON с `status="error"`,
        `error_type="missing_user_identity"`, SQL НЕ выполнен.
      - `test_all_scope_no_like_session_id`: проверить, что
        сгенерированный SQL НЕ содержит `LIKE ... session_id`,
        `OR session_id = %s`, `WHERE TRUE`.
      - `test_current_scope_filters_by_session_id` (регрессия):
        поведение `scope="current"` сохранено.
      - `test_response_does_not_leak_user_id`: payload события
        не содержит ключа `user_id`.

## 4. Producer'ы: user_id в каждом LogEvent

- [ ] 4.1 `lib/hooks/database_logging_hook.py:402-419` — убедиться,
      что `LogEvent` для `run_finished` получает `user_id`: явно
      из request context (через `current_request_context().sender_id`)
      либо положиться на `_enqueue`-автозаполнение. Покрыть
      регрессионным тестом `TestUserIdPropagatedToRunFinished`.

- [ ] 4.2 `lib/services/runtime_patcher.py:1370-1398` — для
      `subagent_run_finished`: subagent пишет `LogEvent` с
      `session_id` родительской сессии и `user_id` родителя
      (через явный `LogEvent.user_id=parent_sender_id`), чтобы
      автозаполнение в `_enqueue` не подтянуло чужой `user_id`.
      Покрыть регрессионным тестом
      `test_subagent_logging_inherits_parent_user_id`.

- [ ] 4.3 `lib/services/context_compaction.py:_record_event_log` —
      `context_compacted` пишется через `LogEvent` с явным
      `user_id` из request context (`current_request_context
      ().sender_id`). Если контекста нет — `user_id=None`, событие
      остаётся в БД, но невидимо для `session_scope="all"`.
      Покрыть регрессионным тестом
      `test_context_compacted_event_carries_user_id`.

- [ ] 4.4 `lib/services/db_logging_service.py:log_inbound` —
      подтянуть `user_id` из request index по `session_id`
      (или оставить `None`, если index пуст). Producer уже
      передаёт `sender_id`, но сохранять `user_id` именно в
      колонке важно для cross-channel поиска.

- [ ] 4.5 Регрессия в `tests/test_database_logging_bridge.py` и
      `tests/test_hooks_database_logging.py`: подтвердить, что
      `LogEvent.user_id` доходит до INSERT при работе через
      реальный `DbLoggingService`.

## 5. Архитектурный guard

- [ ] 5.1 В `tests/test_architecture_guards.py` (или существующем
      файле архитектурных guard-тестов) добавить тест
      `TestHistorySearchSqlGuard`:
      ```python
      SOURCE = Path("workspace/tools/history_search_tool.py").read_text(...)
      FORBIDDEN = (
          "OR session_id = %s",
          "LIKE %session_id%",
          "session_id LIKE",
          "OR TRUE",
          "WHERE TRUE",
          "IS NULL OR user_id",  # unscoped fallback
      )
      for pat in FORBIDDEN:
          assert pat not in SOURCE, f"history_search: forbidden {pat!r}"
      ```
      Тест запускается в pytest и валит CI, если кто-то добавит
      unscoped-fallback.

- [ ] 5.2 В том же файле — тест
      `TestHistorySearchIsReadOnly`: SQL, генерируемый
      `HistorySearchTool.execute(...)`, содержит только `SELECT`
      и `FROM "..."."agent_gateway_logs"`, без `INSERT`/`UPDATE`/
      `DELETE`/`TRUNCATE`.

- [ ] 5.3 Существующий guard из
      `openspec/changes/unify-agent-event-logging-pipeline` уже
      проверяет, что `INSERT INTO ... agent_gateway_logs` есть
      только в `lib/services/db_logging_service.py`. Подтвердить,
      что guard остаётся зелёным после этой change (нет
      регрессии в logging pipeline).

## 6. Фикстуры и сценарии

- [ ] 6.1 В `tests/fixtures/history_search/gateway_logs.jsonl`
      добавить пары сессий:
      - `user_a/session_a1` (например, `sender_id="alice"`,
        `session_id="telegram:alice_1"`, несколько событий с
        `event_type` из существующего набора);
      - `user_a/session_a2` (`session_id="telegram:alice_2"`,
        `sender_id="alice"`);
      - `user_b/session_b1` (`session_id="telegram:bob_1"`,
        `sender_id="bob"`);
      - `user_b/session_b2` (`session_id="telegram:bob_2"`,
        `sender_id="bob"`).
      Все события должны содержать поле `user_id` в JSONL
      (Alice — `"alice"`, Bob — `"bob"`). Существующие события
      фикстуры остаются как есть (для них проверяется поведение
      «без `user_id` → нет в `scope=all`»).

- [ ] 6.2 В `tests/fixtures/history_search/scenarios.json`
      добавить новые сценарии:
      - `scope_current_user_a` (`session_scope="current"`,
        `current_user_id="alice"` / `current_session_key="telegram:alice_1"`
        — только события alice/session_a1).
      - `scope_all_user_a` (`session_scope="all"`, `alice` —
        события alice/session_a1 + alice/session_a2; **исключая**
        bob/session_b1 и bob/session_b2).
      - `scope_all_user_b` (`session_scope="all"`, `bob` —
        только события bob).
      - `scope_all_cross_user_isolation` — alice запрашивает
        события bob через косвенный текст (например, общий query
        «contract»); возвращаются только её события.
      - `scope_all_missing_user` — без request context →
        ожидаемый `error_type="missing_user_identity"` (тест
        проверяет как failure-сценарий: `expected_status="error"`,
        `expected_error_type="missing_user_identity"`).
      Существующие сценарии с `session_scope="all"` (без
      request context) переводятся в failure-сценарии с
      `expected_error_type="missing_user_identity"` — иначе они
      начнут падать после смены семантики.

## 7. Документация

- [ ] 7.1 В `workspace/TOOLS.md` секция `history_search` —
      обновить описание `session_scope`:
      - `current` — только текущая сессия (по умолчанию);
      - `all` — все сессии **текущего пользователя**, не глобально;
      - явно отметить: «`history_search` не выполняет глобальный
        поиск по всем пользователям. Запрос `session_scope="all"`
        без идентификатора пользователя возвращает ошибку
        `missing_user_identity`.»

- [ ] 7.2 В `docs/architecture/HISTORY_SEARCH_ANALYSIS.md` —
      пометить cross-user leakage как **закрытый**, добавить
      ссылку на эту change (по `task_id` или по имени).

- [ ] 7.3 В `CHANGELOG.md` секция `[Unreleased]` → категории
      `Security` (cross-user isolation fix) и `Changed`
      (новая семантика `session_scope="all"`):
      ```
      ### Security
      - history_search: `session_scope="all"` фильтрует события по
        user_id (закрыт cross-user leakage). Источник user_id —
        RequestContext.sender_id; при отсутствии возвращается
        ошибка `missing_user_identity`. См. OpenSpec change
        fix-history-search-user-isolation.

      ### Changed
      - history_search: добавлена колонка `agent_gateway_logs.user_id`,
        индекс `(user_id, "timestamp" DESC)`, миграция
        V004__agent_gateway_logs_user_id.
      ```

## 8. Регрессия и валидация

- [ ] 8.1 `pytest tests/test_history_search_tool.py` — все тесты
      (старые + новый класс `TestUserIsolation`) зелёные.

- [ ] 8.2 `pytest tests/test_db_logging_service.py` — все тесты
      на `user_id` (новые и старые) зелёные.

- [ ] 8.3 `pytest tests/test_hooks_database_logging.py` —
      регрессия `run_finished` с `user_id` зелёная.

- [ ] 8.4 `pytest tests/test_subagent_logging.py` — регрессия
      subagent `user_id` зелёная.

- [ ] 8.5 `pytest tests/test_context_compaction.py` — регрессия
      `context_compacted` с `user_id` зелёная.

- [ ] 8.6 `pytest tests/test_architecture_guards.py` —
      `TestHistorySearchSqlGuard` и `TestHistorySearchIsReadOnly`
      зелёные.

- [ ] 8.7 `pytest tests/` — все тесты зелёные (никакой регрессии
      в смежных подсистемах).

- [ ] 8.8 `openspec.cmd validate fix-history-search-user-isolation`
      → статус «passed», 0 issues.

- [ ] 8.9 Smoke-прогон `python cli_agent.py --profile=test --smoke`
      → `OK_SMOKE_COMPLETE`; `history_search` присутствует в реестре
      tools; `RequestContext.sender_id` доступен в
      `tools.history_search`.

## Definition of Done

Change считается выполненным, когда одновременно выполнены все пункты:

- [ ] `agent_gateway_logs.user_id` существует (DDL + миграция V004).
- [ ] `agent_gateway_logs` получает `user_id` через `LogEvent.user_id`.
- [ ] `DbLoggingService` — единственный writer (guard остаётся зелёным).
- [ ] `user_id` НЕ извлекается из `session_id`, `chat_id`,
      `actor`, `payload`, `name`.
- [ ] `session_scope="current"` фильтрует по `session_id`.
- [ ] `session_scope="all"` фильтрует по `user_id`.
- [ ] `all` никогда не превращается в unscoped query
      (`(%s OR session_id = %s)`, `WHERE TRUE`, `LIKE ... session_id`
      отсутствуют — guard-тест зелёный).
- [ ] Отсутствие `user_id` при `scope="all"` возвращает
      `missing_user_identity`, а не unscoped-выборку.
- [ ] Существующие события с `request_id` backfill'ятся через
      `agent_question_runs.user_id`.
- [ ] События без `request_id` (или без `user_id`) НЕ участвуют
      в `scope="all"`.
- [ ] Добавлен индекс `(user_id, "timestamp" DESC)`.
- [ ] Есть cross-user isolation tests (alice vs bob).
- [ ] Есть tests на отсутствие identity (`missing_user_identity`).
- [ ] Есть tests на сохранение `user_id` через logging pipeline
      (run_finished, subagent_run_finished, context_compacted,
      tool_call, llm_call, outbound).
- [ ] Tool API не меняется: `user_id` — внутренний security
      attribute, а не параметр tool.
- [ ] `workspace/TOOLS.md` обновлён.
- [ ] `docs/architecture/HISTORY_SEARCH_ANALYSIS.md` обновлён.
- [ ] `CHANGELOG.md` `[Unreleased]` содержит записи `Security` и
      `Changed`.
- [ ] `openspec.cmd validate` зелёный.
