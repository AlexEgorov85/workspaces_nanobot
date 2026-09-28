## MODIFIED Requirements

### Requirement: Совместимость с upstream nanobot

ИЗМЕНЕНО нормативное место вызова `RuntimePatcher.apply_all()` —
фактически это composition-фаза `ApplicationContext.create()`, а не
фоновый lifecycle `ApplicationContext.start()`. Требование ниже
полностью заменяет ранее существовавшее требование «Совместимость
с upstream nanobot» в `openspec/specs/runtime/context/spec.md`.

`ApplicationContext.create()` MUST вызывать `RuntimePatcher.apply_all`
после инициализации сервисов и до того, как `ApplicationContext`
отдаёт `ctx.agent` внешним потребителям (gateway, CLI, streamlit);
если `apply_all` оставляет непустой `report.failed`, система MUST
логировать warning, но MUST NOT прерывать старт (каждый `failed`-патч
явно помечен `required=False` в `PatchSpec` или fail-сценарий не
критичен для прод). `ApplicationContext.start()` SHALL отвечать
исключительно за фоновое lifecycle-оборудование
(`_start_db_pool()`, `_validate_runtime_schema()`, старт
`db_logging_service`, `sync_service`, `session_cold_sync_service`,
`RuntimeEventsSubscriber`), и SHALL NOT повторно вызывать
`RuntimePatcher.apply_all()`.

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