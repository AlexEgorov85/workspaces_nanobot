## MODIFIED Requirements

### Requirement: Совместимость с upstream nanobot

ИЗМЕНЕНО нормативное место вызова `RuntimePatcher.apply_all()` —
фактически это composition-фаза `ApplicationContext.create()`, а не
фоновый lifecycle `ApplicationContext.start()`. Также ИЗМЕНЕНО
утверждение про failed-патчи: `required=True` — это metadata для
diagnostics, а НЕ триггер startup-abort. Требование ниже полностью
заменяет ранее существовавшее требование «Совместимость с upstream
nanobot» в `openspec/specs/runtime/context/spec.md`.

`ApplicationContext.create()` MUST вызывать `RuntimePatcher.apply_all`
после инициализации сервисов и до того, как `ApplicationContext`
отдаёт `ctx.agent` внешним потребителям (gateway, CLI, streamlit).

`ApplicationContext.start()` SHALL отвечать исключительно за
фоновое lifecycle-оборудование (`_start_db_pool()`,
`_validate_runtime_schema()`, старт `db_logging_service`,
`sync_service`, `session_cold_sync_service`,
`RuntimeEventsSubscriber`), и SHALL NOT вызывать
`RuntimePatcher.apply_all()` или отдельные `patch_*` методы
`RuntimePatcher`, входящие в `apply_all`.

**Семантика failed-патчей и `PatchSpec.required`:**

`PatchSpec.required: bool` — это metadata для diagnostics
(startup-баннер, `diff_runtime_patches()`, `diagnose_startup.py`),
а **НЕ** триггер прерывания startup. Если `apply_all` оставляет
непустой `report.failed`, система MUST логировать warning со
всеми именами failed-патчей (включая те, у которых
`PatchSpec.required=True`, — для оператора), и MUST NOT
прерывать startup. Это поведение уже реализовано в
`lib/core/application_context.py:352-357` (только
`logger.warning(... %d runtime patch(es) failed ...)`).

Утверждение «каждый failed-патч явно помечен DEPRECATED и не
критичен для прод» из старой версии спеки — НЕВЕРНО: failed-патч
может иметь `required=True` (например, если upstream-метод изменился
и сломалась приватная обёртка), и это должно быть явно видно
оператору через warning-лог, но не должно прерывать startup.

#### Scenario: Апгрейд upstream-nanobot без регрессии

- **WHEN** версия `nanobot-ai` в `requirements.txt` меняется
- **THEN** `pytest tests/contract/` MUST запускаться первым; если
  есть падения, они MUST быть исправлены или явно помечены `xfail`
  до merge upgrade-изменения.

#### Scenario: `apply_all` вызывается ровно один раз и только в `create()`

- **WHEN** `ApplicationContext.create()` завершается успешно
- **THEN** `RuntimePatcher.apply_all` MUST быть вызван ровно один
  раз, и `ctx.agent._assemble_outbound` MUST содержать ровно один
  project wrapper layer.
- **AND** `ApplicationContext.start()` MUST NOT вызывать
  `RuntimePatcher.apply_all` ни прямо, ни через отдельные
  `patch_*` методы `RuntimePatcher`.
- **AND** внешние entrypoint'ы (`cli_agent.py`, `gateway.py`,
  `streamlit_app.py`) MUST NOT вызывать `RuntimePatcher.apply_all`
  или отдельные `patch_*` методы, входящие в `apply_all`, после
  возврата из `create()`.

#### Scenario: Failed-патч с `required=True` логируется, но не прерывает startup

- **WHEN** `apply_all` оставляет `report.failed` и один из failed-патчей
  имеет `PatchSpec.required=True` (например, `assemble_outbound`
  сломался из-за изменения сигнатуры upstream-метода)
- **THEN** система MUST логировать `logger.warning(...)` с именем
  этого патча и общим списком failed-патчей (для оператора).
- **AND** система MUST NOT выбрасывать исключение, MUST NOT
  прерывать startup, MUST NOT вызывать `sys.exit`.
- **AND** тот факт, что `required=True`-патч fail'нул, остаётся
  видимым через warning-лог и через startup-баннер
  `PatchReport.render(...)`.

#### Scenario: `required=True` НЕ означает startup-abort

- **WHEN** разработчик читает `PatchSpec.required` и пытается
  добавить raise/abort на failed required-патче
- **THEN** тест `tests/test_application_context.py`
  (или эквивалентный) должен явно проверять, что
  `ApplicationContext.create()` НЕ выбрасывает исключение при
  failed `required=True`-патче, и startup продолжается с
  warning-логом.