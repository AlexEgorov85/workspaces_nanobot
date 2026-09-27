## 1. Pydantic-модель `ErrorMessagesSettings`

- [x] 1.1 Добавить `ErrorMessagesSettings(_StrictOptional)` в `lib/core/project_settings.py` (поля `internal_error: str | None = None`, `log_to_db: bool | None = None`) и поле `error_messages: ErrorMessagesSettings | None = None` в `GatewaySettings`. Проверить: `python -c "from lib.core.project_settings import ProjectSettings; ProjectSettings.model_validate({'gateway': {'error_messages': {}}})"` не падает, `ProjectSettings(...).gateway.error_messages` — сконструированная модель.

- [x] 1.2 Добавить `tests/test_project_settings.py::TestErrorMessagesSettings` (default + custom + invalid types). Проверить: `pytest tests/test_project_settings.py -k ErrorMessagesSettings -v` зелёный.

## 2. Реализация `patch_turn_delivery_fail`

- [x] 2.1 Добавить модульные константы `_DEFAULT_INTERNAL_ERROR_TEXT` и `_DEFAULT_LOG_TO_DB = True` в `lib/services/runtime_patcher.py`. Проверить: `grep -n "_DEFAULT_INTERNAL_ERROR_TEXT" lib/services/runtime_patcher.py` показывает обе строки.

- [x] 2.2 Реализовать `RuntimePatcher.patch_turn_delivery_fail(settings, db_logging_service=None, agent_id=None) -> tuple[bool, str]`: резолв `internal_error` через `get_path` или `None`-fallback, резолв `log_to_db` через `get_path` или default `True`, импорт `TurnDelivery` через `_getloaded("nanobot.agent.turn_delivery")` (fail-fast `return False, "TurnDelivery module not loaded"` если отсутствует), обёртка `_wrap_fail` (читает `self.lifecycle_message.channel/chat_id/metadata`, формирует `OutboundMessage` с `content=internal_error` + `metadata._error_kind="internal"`, при `log_to_db=True` и `db_logging_service is not None` зовёт `try_log_event(..., producer="runtime_patcher", event_type="turn_failed", log_event=LogEvent(...))`, затем вызывает `original_fail(self, publish_completion=publish_completion)`). Проверить: `python -c "from lib.services.runtime_patcher import RuntimePatcher; print(RuntimePatcher.patch_turn_delivery_fail.__doc__[:80])"` не падает.

- [x] 2.3 Зарегистрировать в `RuntimePatcher.apply_all(...)`: `self._record(report, "turn_delivery_fail", self.patch_turn_delivery_fail(settings, db_logging_service, agent_id=agent_id))` ПОСЛЕ `_record(... "assemble_outbound" ...)` и ДО `_record(... "subagent_logging" ...)`. Проверить: `grep -n "turn_delivery_fail" lib/services/runtime_patcher.py` показывает ровно 2 вхождения (определение метода и `_record`).

## 3. Тесты для `patch_turn_delivery_fail`

- [x] 3.1 Добавить `tests/test_runtime_patcher.py::TestPatchTurnDeliveryFail` с тестами: default text, custom text, log_to_db=True (мок `try_log_event`), log_to_db=False (мок `try_log_event` НЕ вызван), db_logging_service=None (fail-open), отсутствие `TurnDelivery` модуля (graceful no-op). Проверить: `pytest tests/test_runtime_patcher.py -k PatchTurnDeliveryFail -v` зелёный.

## 4. Документация и конфиг

- [x] 4.1 Добавить закомментированный пример `"gateway": {"error_messages": {"internal_error": "...", "log_to_db": true}}` в `project.json` (после существующего `gateway.session_cold_sync`). Проверить: `python -c "import json; json.load(open('project.json'))"` не падает.

- [x] 4.2 Обновить `docs/TARGET_ARCHITECTURE.md` — добавить пункт про error fallback в раздел runtime contract (со ссылкой на спеке `runtime/error-fallback`). Проверить: `grep -n "error_messages" docs/TARGET_ARCHITECTURE.md` показывает ≥1 вхождение.

- [x] 4.3 Обновить `AGENTS.md` — добавить секцию «Error fallback messages» в раздел Configuration (после `gateway.cache`) с описанием `gateway.error_messages.{internal_error, log_to_db}` и default-значениями. Проверить: `grep -n "error_messages" AGENTS.md` показывает ≥1 вхождение.

- [x] 4.4 Обновить `docs/architecture/runtime-patcher-inventory.md` — добавить запись для `turn_delivery_fail` (target: `nanobot.agent.turn_delivery.TurnDelivery.fail`; risk: low; tests: `TestPatchTurnDeliveryFail`). Проверить: `grep -n "turn_delivery_fail" docs/architecture/runtime-patcher-inventory.md` показывает ≥1 вхождение.

## 5. Валидация change и регрессии

- [x] 5.1 `openspec.cmd validate error-fallback-messages` — зелёный. Проверить: exit code 0, output без `FAIL`.

- [x] 5.2 `pytest tests/test_project_settings.py tests/test_runtime_patcher.py -v` — зелёный. Проверить: `0 failed`.

- [x] 5.3 `pytest tests/ -q --no-header` — общая регрессия (без новых падений). Проверить: дельта `passed` относительно ориентира в `AGENTS.md § Release Process` (1480 passed) — либо равно, либо больше; никаких новых `failed`.

  > **Примечание:** в master присутствуют 23 предсуществующих падения (подтверждено через `git stash`: `test_history_search_tool.py`, `test_smoke_postgres_channel_media.py::test_patcher_auto_attach_end_to_end`, `test_build_vectors_cli.py::test_validate_only_flag_in_cli` — последний требует живой PG). Эти регрессии НЕ относятся к change `error-fallback-messages`; фиксятся отдельными change'ами. Целевые тесты фичи (17 из `TestErrorMessagesSettings` + `TestPatchTurnDeliveryFail`) — зелёные.
