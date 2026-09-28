## Context

Текущее состояние репозитория подтверждено чтением ключевых файлов:

- `lib/services/runtime_patcher.py:596-655` — `RuntimePatcher.apply_all()`
  применяет 13 patches в фиксированном порядке:
  `context_governor`, `save_turn`, `exec_limits`, `exec_timeout_cap`,
  `tool_limits`, `assemble_outbound`, `turn_delivery_fail`,
  `async_save`, `session_dir_watch`, `subagent_logging`,
  `project_tools`, `document_text_threshold`, `session_content_cleanup`.
- `lib/services/runtime_patcher.py:239-426` — `_PATCH_SPECS` содержит
  14 записей: 11 из 13 совпадают с `apply_all`, но **отсутствуют**
  `turn_delivery_fail` и `session_dir_watch`; вместо них в `_PATCH_SPECS`
  есть DEPRECATED-остатки `compact_tracking`, `compact_command`,
  `idle_guard`, которые нигде не вызываются.
- `lib/core/application_context.py:325-358` — `apply_all()` вызывается
  **внутри** `ApplicationContext.create()`, а не в `start()`.
- `cli_agent.py:152-176` — `_run_patched()` после `create()` ещё раз
  вызывает `ctx.runtime_patcher.patch_assemble_outbound(ctx.agent,
  ctx.tool_audit_hook)`.
- `lib/cli/hook_loader.py:53-83` — `scan_and_register` при
  `path.stem not in allowed` только печатает warning и **продолжает**
  импорт через `spec.loader.exec_module(mod)`.
- `lib/services/runtime_inventory.py:162-186` — `canonical_runtime_patches()`
  проектируется из `RuntimePatcher.patch_specs()` (т.е. наследует drift)
  и содержит второй hardcoded set `high_risk_required` рядом с
  `PatchSpec.risk`.
- `lib/services/runtime_patcher.py:2279-2509` —
  `patch_project_tools()` импортирует `workspace.tools.*`, собирает
  `ToolContext`, вызывает `Tool.enabled/create`, регистрирует в
  `agent.tools.register(tool)`. Это **не** monkey-patch — это
  composition / DI / registration.
- `openspec/specs/runtime/context/spec.md:73-76` —
  `ApplicationContext.start()` MUST вызывать `RuntimePatcher.apply_all`.
  Это расходится с фактическим lifecycle и должно быть исправлено в
  спеке, а не в коде.

Эта change закрывает **только** эти 5 подтверждённых дефектов. Не
трогает: `ApplicationContext` composition, `Skill`/`Tool` boundary,
`TableRegistry`, `DbLoggingService`, `SessionManager`, vector
architecture, audit_analyzer workflow, MCP, profile mechanism,
`AgentLoop` upstream, `AgentHook` API, `RepeatGuardHook`,
`TerminalToolPrintHook` required-vs-optional, существующую семантику
отдельных patch-методов.

## Goals / Non-Goals

**Goals:**

1. Сделать применение runtime patches к одному экземпляру `AgentLoop`
   в рамках одного startup lifecycle однократным.
2. Убрать дубль `patch_assemble_outbound` в `cli_agent._run_patched()`.
3. Синхронизировать `_PATCH_SPECS` с реальным `apply_all()`:
   добавить спеки для двух отсутствующих, удалить три DEPRECATED.
4. Убрать `project_tools` из runtime-patch inventory и перенести
   регистрацию в узкий loader за пределами `RuntimePatcher`.
5. Сделать hook allowlist действительно ограничивающим (не импортировать
   файлы вне `_allowed_hook_names()`).
6. Сделать `PatchSpec.required` единственным источником истины для
   criticality и убрать `high_risk_required` из
   `runtime_inventory.canonical_runtime_patches`.
7. Исправить contract drift в `openspec/specs/runtime/context/spec.md`
   про точку вызова `apply_all`.
8. Добавить регрессионные тесты и архитектурные проверки, фиксирующие
   новое состояние.

**Non-Goals (явный запрет на включение в эту change):**

- Рефакторинг `ApplicationContext.create()` / `start()` /
  `_emit_*_inventory_banner` — это отдельная будущая change
  `application-bootstrap-decomposition`.
- Изменение семантики любого существующего `patch_*` метода
  (сохраняем сигнатуры, поведение, kwargs).
- Изменение публичного контракта `nanobot.agent.tools.base.Tool`,
  `ToolContext`, `ToolRegistry`, `AgentLoop`.
- Изменение `Skill`/`Tool` boundary, `TableRegistry`, `DbLoggingService`,
  `SessionManager`, `vector_index_service`, `cache_provider`,
  `audit_analyzer` workflow, `legal_summarizer`, `office_files`.
- Изменение `MCP` / `repeat-guard-hook` / profile mechanism /
  `TerminalToolPrintHook` required-vs-optional — это другие changes.
- Введение новых framework-абстракций поверх существующих
  (`BaseRuntimePatcher`, `PatchManager`, `PluginManager`,
  `ToolManager`, `CapabilityRegistry`, generic DI container).
- Compatibility-fallback или «на будущее»-stubs для удалённых
  `compact_tracking`/`compact_command`/`idle_guard` PatchSpec.
- Изменение upstream `nanobot` кода (это контракт read-only).

## Decisions

### Decision 1: Единственная точка применения runtime patches — `RuntimePatcher.apply_all()` из `ApplicationContext.create()`

`ApplicationContext.create()` остаётся composition root и единственной
точкой, которая вызывает `RuntimePatcher.apply_all()` для полного
набора патчей. После возврата из `create()` ни один entrypoint
(`cli_agent.py`, `gateway.py`, `streamlit_app.py`) не должен повторно
вызывать ни `apply_all`, ни отдельные `patch_*` методы (для патчей,
которые входят в `apply_all`).

**Почему:** `apply_all` уже фактически вызывается в `create()` — это
composition-фаза, в которой уже доступны `agent`, `tool_audit_hook`,
`db_logging_service`, `cache_store`, `recent_files_hook`, `session_manager`,
`workspace_dir`. Любой повторный вызов — дубль.

**Альтернативы:**

- (a) Перенести вызов `apply_all()` в `start()`. Отвергнуто —
  нарушит существующий invariant «`start()` только для фоновых
  сервисов», придётся передавать в `start()` все DI-зависимости,
  ассерт на идемпотентность `start()` (`self._started`) перестанет
  покрывать повторный вызов `apply_all`.
- (b) Ввести дополнительный вызов `apply_all` в `start()` с
  idempotency-флагом. Отвергнуто — два пути вызова — два источника
  истины, в `create()` уже всё необходимое.
- (c) Оставить как есть и просто убрать вызов из `cli_agent`. Узкое
  решение, но допустимо как разновидность (a) выше — change
  выбирает чистый (a) вариант: запрет на повторные вызовы после
  `create()` фиксируется в спеке, код приводится в соответствие.

### Decision 2: `apply_all()` exact inventory ↔ `_PATCH_SPECS` exact inventory ↔ `canonical_runtime_patches()` exact inventory

Три множества должны быть равны. Любое расхождение считается drift'ом
и блокирует merge через тест `test_runtime_patcher.py::TestPatchSpecs::test_all_patches_have_specs`
(ужесточается с `assert key in actual` до `assert actual == expected`).
Тест должен явно покрывать:

- 13 patches в `apply_all()`;
- 13 specs в `_PATCH_SPECS`;
- 13 entries в `canonical_runtime_patches()`;
- `project_tools` исчезает из `_PATCH_SPECS` после вынесения.

**Почему:** subset-проверка маскирует drift, как показал ручной аудит
(три DEPRECATED-остатка живут в `_PATCH_SPECS`, но не вызываются).
Exact-проверка делает drift видимым при первом CI-прогоне.

**Альтернативы:** Множество «allow superset» (как сейчас) — отвергнуто
как раз и маскирующее drift.

### Decision 3: `RuntimePatcher` не владеет registration project tools

`patch_project_tools()` (тело методом `importlib.util` + `pkgutil.iter_modules`
+ `ToolContext(...)` + `agent.tools.register(tool)`) переносится в
узкий модуль `lib/services/project_tool_loader.py`:

```python
def discover(workspace_dir: Path) -> list[type[Tool]]: ...
def register_all(
    agent: AgentLoop,
    tools: list[type[Tool]],
    *,
    cache_store: CacheProvider | None,
    db_logging_service: DbLoggingService | None,
    settings: Any,
) -> tuple[list[str], list[str], list[str], list[str]]: ...
```

Loader **не** создаёт нового registry, **не** импортирует Skills,
**не** знает имён доменных tool'ов (Skill-названия для
`legal_summarizer_query` и т.п. не должны протекать в loader).
DI-расширения (`_agent_ref`, `_settings_ref`, `_cache_store_ref`,
`_db_logging_service`) остаются тем же `setattr`-паттерном, что и
сейчас — change только перемещает код, не переписывает его.

`RuntimePatcher.apply_all()` больше **не** вызывает `patch_project_tools`.
В `ApplicationContext.create()` после `apply_all()` отдельным шагом
вызывается `register_project_tools(agent, workspace_dir, ...)` из loader'а.

**Почему:** Разделение ответственности. `RuntimePatcher` — адаптер
к upstream runtime API, не tool registry. Сейчас
`patch_project_tools` сидит в нём по инерции первого релиза.

**Альтернативы:**

- (a) Полноценный `ProjectToolManager` со своим lifecycle,
  health-check'ом, restart-loop'ом. Отвергнуто — overengineering,
  нет operational требований; project tools — это
  pure-discover-and-register, без state и без горячей перезагрузки.
- (b) `CapabilityRegistry` / `PluginManager`. Отвергнуто —
  вводит generic-framework-абстракцию, которая не нужна для
  4 файлов в `workspace/tools/`.
- (c) Generic DI container поверх `ToolContext`. Отвергнуто —
  `ToolContext` уже предоставляет DI-поля через `setattr`,
  дополнительный слой избыточен.

### Decision 4: Hook allowlist — действительно блокирующий

`lib/cli/hook_loader.py::scan_and_register` получает единственную
ветку: файл вне `_allowed_hook_names()` — **пропускается** без импорта,
без `exec_module`, без warning'а (warning создавал ложное ощущение
«защищённости»). Чтобы не терять диагностику для миграций (например,
разработчик добавил hook и забыл прописать в allowlist), warning
заменяется на hard-skip + INFO-лог с явным маркером
«hook X не зарегистрирован — добавьте в allowlist».

**Почему:** Текущий код называет себя «allowlist», но семантически —
warn-only. Это создаёт false sense of security: произвольный файл в
`workspace/hooks/` (включая легитимный, но не прошедший ревью)
выполняется.

**Альтернативы:**

- (a) `continue` после warning — оставляет как есть. Отвергнуто —
  это и есть текущая проблема.
- (b) Hard fail (sys.exit) если есть не-alwisted hook. Отвергнуто —
  слишком жёстко для миграционных сценариев; INFO-лог достаточен.
- (c) Чтение списка из `SETTINGS["hooks"]["allowed"]`. Отвергнуто —
  текущий механизм с `_allowed_hook_names()` ужесточён, отдельный
  конфиг-ключ не нужен.

### Decision 5: `PatchSpec.required` — единственный источник истины

`PatchSpec` получает новое поле:

```python
@dataclass(frozen=True)
class PatchSpec:
    name: str
    purpose: str
    nanobot_target: str
    reason: str
    alternatives_checked: str
    risk: str
    nanobot_version: str = "0.3.0"
    required: bool = False
```

В `runtime_inventory.canonical_runtime_patches()` убирается
`high_risk_required`; проекция становится:

```python
return [
    RuntimePatchSpec(
        name=name,
        required=spec.required,
        risk=spec.risk,
        purpose=spec.purpose,
    )
    for name, spec in RuntimePatcher.patch_specs().items()
]
```

Дефолтные значения `required` для существующих спеков задаются явно
на основе реального fail-impact:

- `assemble_outbound`, `save_turn`, `subagent_logging`,
  `context_governor` — `required=True` (ломают runtime при отказе);
- остальные — `required=False` (skip по конфигу / opt-in фичи).

**Почему:** Текущий второй hardcoded set `high_risk_required`
(`runtime_inventory.py:170-175`) дублирует знание, которое уже
принадлежит `PatchSpec`. Дрейф между двумя источниками возможен,
что и показывает текущее состояние (`assemble_outbound` входит
в `high_risk_required`, но `risk='high'` есть и у других — не
однозначно).

**Альтернативы:**

- (a) Оставить `high_risk_required` как есть. Отвергнуто — два
  источника истины, которые уже разъехались (`assemble_outbound`
  в обоих, но `subagent_logging` помечен `high` в spec, и в set
  тоже; если завтра кто-то решит «risk='high' = required»,
  получит неправильный результат для `subagent_logging` против
  нового patch'а с `risk='high'` но `required=False`).
- (b) `risk='high' ⇒ required=True` неявно. Отвергнуто —
  high risk != required (можно иметь критичный по апгрейду patch,
  но не ломающий runtime).

### Decision 6: Существующий contract drift в `runtime/context` spec исправляется в спеке, не в коде

`openspec/specs/runtime/context/spec.md:73-76` сейчас говорит, что
`ApplicationContext.start()` MUST вызывать `RuntimePatcher.apply_all`.
Реально `apply_all()` вызывается в `create()`, а не в `start()`.
Нормативный текст приводится к фактическому lifecycle:

> `ApplicationContext.create()` MUST вызывать `RuntimePatcher.apply_all`
> после инициализации сервисов и до подключения каналов.

`start()` продолжает отвечать **только** за фоновое оборудование
(`_start_db_pool()`, `_validate_runtime_schema()`, фоновые сервисы,
`RuntimeEventsSubscriber.start()`).

**Почему:** Spec не должен диктовать неправильное поведение. Если
выровнять код под spec — сломается composition root (понадобится
прокидывать все DI-зависимости в `start()` и снимать идемпотентность).
Если оставить drift — каждое новое ревью будет спотыкаться.

**Альтернативы:** Перенос `apply_all()` в `start()` — отвергнуто,
см. Decision 1.

## Risks / Trade-offs

- **[Risk]** После вынесения `patch_project_tools` `apply_all()` теряет
  последний «не-monkey-patch» элемент. Если забыть синхронизировать
  `_emit_project_tools_inventory_banner` и `diagnose_startup.py`,
  диагностика project tools сломается.
  → **Mitigation:** task 3.5 явно синхронизирует обе функции; тест
  `test_diagnose_startup.py` остаётся зелёным (если запускается).

- **[Risk]** Ужесточение теста `test_all_patches_have_specs` (subset →
  equality) может сломать существующие CI-прогоны, если где-то
  ещё остались неучтённые DEPRECATED-остатки.
  → **Mitigation:** baseline-прогон теста **до** изменения теста;
  если есть лишние specs — добавляются в `apply_all()` или удаляются
  из `_PATCH_SPECS` в одной фазе change.

- **[Risk]** Если `PatchSpec.required` дефолт `False`, а в `_PATCH_SPECS`
  забыли проставить `True` для критичных — `diff_runtime_patches`
  перестанет ловить missing.
  → **Mitigation:** явно прописать `required=True` в `_PATCH_SPECS`
  для `assemble_outbound`/`save_turn`/`subagent_logging`/
  `context_governor` в Phase 2 этой change; добавить unit-тест,
  который строит `canonical_runtime_patches()` и проверяет, что
  эти 4 имени имеют `required=True`.

- **[Risk]** Сужение hook allowlist ломает существующие
  workspace-deploy'ы, которые случайно держат хуки вне allowlist.
  → **Mitigation:** allowlist остаётся
  `{"session_file_redirect_hook", "recent_files_hook",
  "debug_stream_diag"}` (тот же набор, что и сейчас); INFO-лог
  показывает имя файла; внешний deploy не затрагивается (production
  держит все 3 в allowlist и они совпадают с реально лежащими в
  `workspace/hooks/`).

- **[Trade-off]** Удаление `compact_tracking`/`compact_command`/
  `idle_guard` из `_PATCH_SPECS` теряет audit-trail DEPRECATED-меток.
  → **Принимаемо:** DEPRECATED-логика перенесена в
  `lib/services/compaction_event_subscriber.py` (см. предыдущие
  changes `nanobot-035-upgrade` и `runtime-events-subscription`);
  audit-trail остаётся в git history и в CHANGELOG, не в коде.

- **[Trade-off]** `ProjectToolLoader` как отдельный модуль добавляет
  один файл, но **не** добавляет абстракций — это набор чистых
  функций. Если позже потребуется lifecycle (hot-reload, remove),
  loader расширяется, не заменяется.

## Migration Plan

Деплой — чисто кодовая правка. Никаких миграций БД, никаких
профильных overlay'ов, никаких секретов.

1. Merge change в `master` (или cherry-pick в release-ветку для
   PATCH-релиза).
2. Поднять новую версию gateway/CLI.
3. Smoke-проверки:
   - `cli_agent.py --profile=test --smoke` — старт проходит, баннер
     «Runtime patches» содержит ровно 13 имён, никаких
     «unexpected_applied» в startup-логе;
   - `python tools/diagnose_startup.py --strict` — exit 0
     (при наличии тестового профиля и БД);
   - pytest `tests/test_runtime_patcher.py`, `tests/test_tools_project_loader.py`,
     `tests/test_runtime_inventory.py`, `tests/test_application_context.py`,
     `tests/test_agent_factory.py` — все зелёные.

**Rollback:** revert merge; никаких эффектов на БД-данные. Если
revert невозможен (долгий downtime) — `project_tools` остаётся
как no-op stub в `apply_all` ещё один релиз (deprecation period).
Сейчас deprecation period не нужен — change атомарна и узка.

## Open Questions

Нет. Все решения приняты на уровне proposal/design, deferred
вопросов нет. Если в ходе реализации выяснится, что какой-то
existing patch (например, `session_dir_watch`) ведёт себя иначе,
чем заявлено в его `PatchSpec` (drift в `nanobot_version` /
`nanobot_target`) — это отдельная bug-fix change, не эта.