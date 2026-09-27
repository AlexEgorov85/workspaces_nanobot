# Tasks — fix-error-fallback-double-outbound

## 1. Переписать `_wrap_fail` в `lib/services/runtime_patcher.py`

- [x] 1.1 Добавить в файл импорт `import sys` (рядом с `import asyncio`).
  Использован существующий `import sys as _sys` (`_sys.exception()`).
- [x] 1.2 Добавить в файл приватный класс `_OutboundSilencer` (с `__slots__=("_inner",)`, `__init__`, `__getattr__`, `async def publish_outbound(self, msg)` возвращающий `None`).
- [x] 1.3 В `_wrap_fail` сразу после определения (первая исполняемая строка) добавить `exc = _sys.exception()`.
- [x] 1.4 Заменить источник `session_key`: `getattr(self, "session_key", None)` вместо `getattr(lifecycle, "session_key", ...)`.
- [x] 1.5 Заменить источник идентификатора пользователя: `getattr(lifecycle, "sender_id", None)` вместо `getattr(lifecycle, "user_id", ...)`.
- [x] 1.6 Обернуть вызов оригинального `fail()` в подмену `self.bus` через `_OutboundSilencer` с восстановлением в `finally`.
- [x] 1.7 Расширить payload `LogEvent` полями: `exception_type`, `exception_message`, `exception_available`, `sender_id`, `agent_id`, `chat_id`. `session_id` берётся из `self.session_key`. `user_id` заполняется из `sender_id` (LogEvent-уровневое поле).
- [x] 1.8 Проверить: `python -c "from lib.services.runtime_patcher import RuntimePatcher; print(RuntimePatcher.patch_turn_delivery_fail.__doc__[:80])"` не падает.

## 2. Передать `agent_id` из `apply_all`

- [x] 2.1 В `apply_all` (`lib/services/runtime_patcher.py:573-574`) изменить вызов на `self._record(report, "turn_delivery_fail", self.patch_turn_delivery_fail(settings, db_logging_service, agent_id=_resolve_agent_id(config, agent)))`.
- [x] 2.2 Добавить helper `_resolve_agent_id(config, agent)`: приоритеты `config.agents.defaults.name` → `config.default_agent` → `agent.name` → `None`.
- [x] 2.3 Fallback на `agent.name` и `None` при отсутствии других источников.
- [x] 2.4 Импорт и sanity-check: `python -c "from lib.services.runtime_patcher import _resolve_agent_id, RuntimePatcher; print(_resolve_agent_id(None, None))"` работает.

## 3. Переписать `tests/test_runtime_patcher.py::TestPatchTurnDeliveryFail`

- [x] 3.1 Заменить stub `bus` на `_StubBus` с настоящим async `publish_outbound` (а не `MagicMock`-side-effect, обходящий bus).
- [x] 3.2 Удалить assertions, проверявшие два outbound'а. Новые: `len(published) == 1`.
- [x] 3.3 Добавить test `test_upstream_literal_not_published`: ни один outbound НЕ содержит `"Sorry, I encountered an error."`.
- [x] 3.4 Добавить `test_turn_completed_not_published_when_completion_false`: `turn_completed` НЕ вызван при `publish_completion=False`.
- [x] 3.5 Добавить `test_exception_available_inside_except_block`: payload содержит `exception_type="ValueError"`, `exception_message="boom-12345"`, `exception_available=True`.
- [x] 3.6 Добавить `test_exception_unavailable_degrades_gracefully`: `exception_available=False` при прямом вызове.
- [x] 3.7 Добавить `test_session_key_from_turn_delivery_instance`: `lifecycle_message.session_key="WRONG_LIFECYCLE"` (должен быть `None`, ставим «неправильное»), `self.session_key="real_session_key"` → в payload `"real_session_key"`.
- [x] 3.8 Добавить `test_sender_id_from_lifecycle_message`: `lifecycle_message.sender_id="u-42"` → в payload `sender_id="u-42"`.
- [x] 3.9 Добавить `test_agent_id_passed_through`: `agent_id="agent_main"` через kwarg → в payload.
- [x] 3.10 `pytest tests/test_runtime_patcher.py -k PatchTurnDeliveryFail -v` → 19 passed.

## 4. Документация

- [x] 4.1 В `docs/architecture/runtime-patcher-inventory.md` обновить purpose для `turn_delivery_fail` (добавить описание bus-proxy и sys.exception()). `risk: MEDIUM` уже стоял.
- [x] 4.2 В `CHANGELOG.md` добавлена секция `### Fixed` под `[Unreleased]` с тремя пунктами.

## 5. Валидация

- [x] 5.1 `openspec.cmd validate fix-error-fallback-double-outbound` → `Change 'fix-error-fallback-double-outbound' is valid`.
- [x] 5.2 `pytest tests/test_project_settings.py -q` → 62 passed.
- [x] 5.3 `pytest tests/test_runtime_patcher.py -k PatchTurnDeliveryFail -v` → 19 passed.

  > **Предсуществующие падения:** в `TestPatchSubagentLogging` (4 теста) — не относятся к этому change'у, подтверждены через `git stash` (на чистом master тоже падают).

## 6. Архивирование и коммит

- [ ] 6.1 `openspec.cmd archive fix-error-fallback-double-outbound --yes` → спека переезжает в `openspec/changes/archive/`, дельта применяется к `openspec/specs/runtime/error-fallback/spec.md`.
- [ ] 6.2 Проверить: `openspec.cmd show runtime/error-fallback --type spec --no-scenarios` показывает обновлённые требования.
- [ ] 6.3 `git add` + коммит `fix(runtime): устранить двойной outbound и заполнить диагностику в patch_turn_delivery_fail`.
- [ ] 6.4 `git push origin master`.

## Замечания

- Все untracked файлы из предыдущих сессий (`tools/smoke_post_cleanup.py`,
  чужие `openspec/changes/*`) НЕ включаются в коммит — только файлы этого
  change'а.
- Существующие 4 предсуществующих падения в `TestPatchSubagentLogging` —
  НЕ являются целью этого change'а и фиксятся отдельным change'ом.

