## 1. Baseline и инструментарий

- [ ] 1.1 Зафиксировать baseline прогонов: `pytest tests/contract -q` (9 failed) + `pytest tests/test_runtime_patcher.py tests/test_tools_project_loader.py tests/test_runtime_patcher_e2e.py -q` (59 failed) — сохранить вывод в `openspec/changes/nanobot-035-upgrade/baseline.txt` как артефакт изменения
- [ ] 1.2 Создать тестовый helper `tests/contract/helpers_runtime.py::introspect_target(nanobot_path, version)` (использует `inspect.signature`) — убедиться, что helper импортируется и возвращает `inspect.Signature` для заданного nanobot-символа (`python -c "from tests.contract.helpers_runtime import introspect_target; import inspect; print(introspect_target('nanobot.agent.loop.AgentLoop._assemble_outbound'))"`)

## 2. Этап 1: HIGH — сломанные патчи

- [ ] 2.1 Переписать `patch_assemble_outbound` в `lib/services/runtime_patcher.py` на сигнатуру `(msg, final_content, stop_reason, streamed_content, *, log_content, turn_latency_ms)` — убрать `all_msgs`/`had_injections`, добавить чтение `_tool_audit` и recent-files через `session.metadata` после `_build_turn`; проверить: `pytest tests/contract/test_agent_loop_api.py::test_assemble_outbound_signature -q` → pass
- [ ] 2.2 Переписать `patch_project_tools` в `lib/services/runtime_patcher.py`: убрать kwargs `file_state_store`/`runtime_events` из `ToolContext(...)`, добавить `runtime_control=agent._runtime_control`, оставить `setattr` для DI — проверить: `pytest tests/test_tools_project_loader.py -q` → 0 failed
- [ ] 2.3 Перецепить `patch_document_text_threshold` в `lib/services/runtime_patcher.py` на `nanobot.utils.document.reference_non_image_attachments` с bounded-extraction до `channels.document_text_threshold` символов — проверить: `pytest tests/test_runtime_patcher.py::TestPatchDocumentTextThreshold -q` → 0 failed; smoke через `gateway.py --profile=test` отправляет вложение >20k символов, проверяет наличие `[text omitted (len=…) > threshold=…)]` в логах
- [ ] 2.4 Обновить `tests/contract/test_agent_loop_api.py::test_save_turn_signature` — `kwonly=["turn_latency_ms", "summary_checkpoint", "input_persisted_early"]`; проверить: `pytest tests/contract/test_agent_loop_api.py::test_save_turn_signature -q` → pass
- [ ] 2.5 Smoke `pytest tests/contract -q` после правок 2.1–2.4 → 0 failed

## 3. Этап 2: MEDIUM — миграция на upstream-механизмы

- [ ] 3.1 Фильтр `ContextCompactionEvent` в `postgres_channel`: `lib/channels/postgres_channel.py` — при чтении `OutboundMessage` из `bus.outbound` проверять `isinstance(msg.event, ContextCompactionEvent)`; для фазы `succeeded` вызывать `ContextCompactionService._notify(session_key, report)`; для всех фаз — `DbLoggingService.try_log_event(event_type="context_compacted", ...)` — проверить: `pytest tests/test_context_compaction.py -q` (если есть) → 0 failed; новый `tests/contract/test_compaction_api.py::test_context_compaction_event_emitted` (фиксирует: `event_type == "context_compacted"`, фазы `started/succeeded/failed/cancelled` идут через `EventSink.emit` → `bus.publish_event`) → pass
- [ ] 3.2 Удалить `lib/commands/compact_command.py` и `patch_compact_command` в `lib/services/runtime_patcher.py`; `_notify` остаётся и вызывается из фильтра `postgres_channel` (см. 3.1) — проверить: `pytest tests/test_runtime_patcher.py::TestPatchCompactCommand -q` → 0 failed; ручной smoke `gateway.py --profile=test` + `/compact` → работает, history-notice в `agent_conversation_messages` появляется, event `context_compacted` в `agent_gateway_logs`
- [ ] 3.3 Удалить `patch_auto_compact_idle_guard` в `lib/services/runtime_patcher.py`; проверить: `pytest tests/test_runtime_patcher.py -q` → 0 failed; smoke: `idleCompactAfterMinutes: 0` в `config.json`, никаких фоновых задач по `list_sessions`
- [ ] 3.4 Перенести `patch_context_bridge_seed` на подписку `RuntimeEventPublisher.turn_runtime_admitted` (или обёртку `_build_turn`) — проверить: `pytest tests/test_runtime_patcher.py -q` → 0 failed; Streamlit UI рисует `st.progress(pct)` с актуальным `context_window`
- [ ] 3.5 Smoke `pytest tests/contract tests/test_runtime_patcher.py tests/test_tools_project_loader.py -q` после правок 3.1–3.4 → 0 failed

## 4. Этап 3: LOW — гигиена и deprecation

- [ ] 4.1 Удалить `lib/hooks/base_tool_tracking_hook.py`; в `lib/hooks/tool_audit_hook.py`, `lib/hooks/database_logging_hook.py`, `lib/hooks/terminal_tool_print_hook.py` заменить `self._iter_tool_calls(...)` на прямую работу с `ctx.tool_calls` — проверить: `pytest tests/test_*hook*.py -q` → 0 failed
- [ ] 4.2 Обернуть ссылку на `exec_session.WriteStdinTool` в `patch_exec_limits` (`lib/services/runtime_patcher.py`) в `try/except AttributeError` — проверить: `pytest tests/test_runtime_patcher_e2e.py::TestExecToolE2E -q` → 0 failed
- [ ] 4.3 Удалить мёртвую ссылку на `workspace/utils/event_log.py` в `lib/services/context_compaction.py:323` (docstring/комментарий) — проверить: `grep -r "event_log" lib/` → 0 совпадений в docstring
- [ ] 4.4 Пометить `xfail(strict=True)` тесты вне upgrade-скоупа: `tests/test_profile_lifecycle.py` (4 теста), `tests/test_history_search_tool.py::TestUserIsolation/TestGeneratedSqlGuard/TestSnapshotConsistency` (8 тестов), `tests/test_pg_session_manager.py::test_init_sets_framework_contract` — каждый `xfail` имеет `reason` с TODO-ссылкой; проверить: `pytest tests/test_profile_lifecycle.py -q` → `xfailed`

## 5. Документация и инвентарь

- [ ] 5.1 Обновить `docs/architecture/runtime-patcher-inventory.md`: каждая запись патча — target, status (`OK`/`BROKEN_SIG`/`DEPRECATED`/`MOVED`), версия nanobot, тесты; DEPRECATED-записи (для удалённых патчей) сохраняются с ссылкой на заменивший upstream-символ — проверить: `grep -c "DEPRECATED" docs/architecture/runtime-patcher-inventory.md` ≥ 3 (минимум 3 удалённых патча)
- [ ] 5.2 Обновить `openspec/specs/upgrade-compatibility/spec.md` (после `openspec apply`) — финальный текст спеки с актуальными ссылками на реализацию
- [ ] 5.3 Обновить `openspec/specs/runtime/context/spec.md` (после `openspec apply`) — добавить требование `ContextCompactionService через upstream EventSink`
- [ ] 5.4 Добавить запись в `CHANGELOG.md` под `[Unreleased]`: категории `Changed` (HIGH/MEDIUM правки), `Removed` (удалённые патчи/файлы), `Fixed` (исправленные сигнатуры); проверить: `head -50 CHANGELOG.md` содержит блок `## [Unreleased]`

## 6. Валидация и smoke

- [ ] 6.1 `openspec.cmd validate nanobot-035-upgrade --strict` → 0 ошибок
- [ ] 6.2 `pytest tests/contract -q` → 0 failed
- [ ] 6.3 `pytest tests/test_runtime_patcher.py tests/test_tools_project_loader.py tests/test_runtime_patcher_e2e.py -q` → 0 failed
- [ ] 6.4 `pytest tests/ -q --ignore=tests/integration` → 0 failed (или только xfailed, помеченные в 4.4)
- [ ] 6.5 `python gateway.py --profile=test` стартует; `curl /health` → `READY`; отправка тестового сообщения через postgres-channel → ответ с актуальным `metadata.context_window`
- [ ] 6.6 `python cli_agent.py --profile=test` стартует; `/compact` работает; compaction-notice виден в REPL
- [ ] 6.7 `python benchmarks/runner.py --tags simple` → smoke зелёный

## 7. Commit и PR

- [ ] 7.1 `git status` чистый; `git diff --stat` показывает изменения в `lib/services/runtime_patcher.py`, `lib/services/context_compaction.py`, `lib/commands/compact_command.py` (deleted), `lib/hooks/base_tool_tracking_hook.py` (deleted), `tests/contract/`, `tests/test_runtime_patcher*.py`, `tests/test_tools_project_loader.py`, `docs/architecture/runtime-patcher-inventory.md`, `CHANGELOG.md`, `requirements.txt` — без непреднамеренных файлов
- [ ] 7.2 `git commit -m "feat(nanobot): upgrade runtime_patcher to nanobot-ai 0.3.5"` (один коммит, Conventional Commits на русском по AGENTS.md)
- [ ] 7.3 `gh pr create --base master --title "feat(nanobot): upgrade to nanobot-ai 0.3.5" --body "Closes OpenSpec change nanobot-035-upgrade"`; в PR-описании — ссылка на `openspec/changes/nanobot-035-upgrade/`