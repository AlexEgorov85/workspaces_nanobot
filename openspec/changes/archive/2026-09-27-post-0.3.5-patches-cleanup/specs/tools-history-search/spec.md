## ADDED Requirements

### Requirement: event_type accepts turn_completed

Tool `history_search` SHALL принимать значение `"turn_completed"` в параметре `event_type` и фильтровать `agent_gateway_logs` по строкам, у которых `event_type="turn_completed"`. Эти строки пишутся через подписку на `TurnCompleted` (`nanobot/bus/runtime_events.py:60-74`) в `RuntimeEventsSubscriber._handle_turn_completed`. Контракт `run_finished` и `subagent_run_finished` НЕ меняется.

#### Scenario: turn_completed найден через event_type filter
- WHEN tool вызывается с `event_type="turn_completed"` и `session_scope="current"`
- THEN результирующий SQL SHALL содержать предикат `event_type = %s` с параметром `"turn_completed"`
- AND SHALL вернуть только строки, у которых `event_type="turn_completed"` (никаких `run_finished` / `subagent_run_finished`)

#### Scenario: payload turn_completed содержит метрики
- WHEN tool возвращает событие `event_type="turn_completed"`
- THEN `payload` SHALL содержать `latency_ms`, `outcome`, `failure_kind`, `failure_error_kind`, `failure_attempts`, `usage_tokens`, `runtime_model`
- AND `payload` SHALL NOT содержать `final_content`, `tools_used`, `stop_reason`, `had_injections` (эти поля остаются у `run_finished`)

#### Scenario: turn_completed отфильтрован по user_id при all scope
- GIVEN событие `event_type="turn_completed"` с `user_id="alice"`
- WHEN alice вызывает `history_search(event_type="turn_completed", session_scope="all")`
- THEN событие SHALL быть возвращено
- AND события с `user_id="bob"` (даже того же типа) SHALL NOT быть возвращены