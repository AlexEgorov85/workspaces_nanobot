## 1. Конфигурация и скелет модуля

- [ ] 1.1 Добавить `GatewayRepeatGuardSettings` (pydantic) с полями
  `mode: Literal["off", "warn", "block"] = "off"`,
  `window_size: int = 20` (с `ge=1, le=1000`),
  `max_repeats_in_window: int = 3` (с `ge=2, le=100`),
  `exempt_tools: list[str] = []` + `field_validator` отвергающий
  glob/regex-метасимволы `* ? [ ] ^ $`.
  **Verify:** `python -c "from lib.core.project_settings import
  GatewayRepeatGuardSettings; print(GatewayRepeatGuardSettings().model_dump())"`
  печатает дефолты без `ValidationError`.

- [ ] 1.2 Подключить `repeat_guard: GatewayRepeatGuardSettings | None = None`
  в `GatewaySettings` (`lib/core/project_settings.py:218-264`).
  **Verify:** `python -c "from lib.core.project_settings import
  GatewaySettings; print(GatewaySettings().repeat_guard)"` печатает
  `None` (новые ключи опциональны).

- [ ] 1.3 Создать `lib/hooks/repeat_guard_hook.py` с хелперами
  `_canonical_arguments(arguments)` (через `json.dumps(..., sort_keys=
  True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
  default=_stable_fallback)`) и
  `_stable_fallback(value)` (детерминированные правила для
  `Path`/`datetime`/`bytes`/произвольных объектов; nondeterministic →
  явный sentinel для skip'а). **Verify:** `pytest
  tests/test_repeat_guard_hook.py::test_canonical -v` зелёный.

- [ ] 1.4 Реализовать класс `RepeatGuardHook(AgentHook)` в том же
  файле: `__init__(self, settings, db_logging_service=None)`,
  per-session state `_state: dict[str, dict]` с полями `deque`
  буфера и `_last_published_fingerprint: set`, методы
  `before_run` (reset для соответствующего `session_key`),
  `before_execute_tool` (детект + enqueue/raise), `on_execute_tool_error`
  (no-op), `after_run` (опциональная чистка). Детектирование
  через точное равенство `(tool_name, canonical_args)`.
  **Verify:** `python -c "from lib.hooks.repeat_guard_hook import
  RepeatGuardHook; from nanobot.agent.hook import AgentHook;
  print(issubclass(RepeatGuardHook, AgentHook))"` печатает `True`.

## 2. Интеграция и inventory

- [ ] 2.1 В `lib/core/agent_factory.py::create` (после строки 121 с
  `hooks.append(tool_audit_hook)`) добавить чтение
  `ctx._settings_ref.gateway.repeat_guard` (прямое чтение из
  конфига, без pydantic-модели в kwargs) и условный
  `hooks.append(RepeatGuardHook(settings=...,
  db_logging_service=db_logging_service))`. **Verify:** новый
  интеграционный тест 3.6 зелёный.

- [ ] 2.2 Добавить запись `HookSpec(name="RepeatGuardHook",
  kind="framework", required=False, description="защитник от
  повторных tool-вызовов (mode off/warn/block)",
  source="lib/hooks/repeat_guard_hook.py")` в
  `canonical_framework_hooks()` (`lib/services/runtime_inventory.py:68-85`).
  **Verify:** `python -c "from lib.services.runtime_inventory
  import canonical_framework_hooks; print(any(h.name ==
  'RepeatGuardHook' for h in canonical_framework_hooks()))"`
  печатает `True`.

- [ ] 2.3 В `project.json` (JSONC) добавить закомментированный
  пример блока `gateway.repeat_guard` под отдельной секцией
  `// === Repeat guard ===`. **Verify:** `python -c "import re;
  data = open('project.json', encoding='utf-8').read();
  assert 'gateway.repeat_guard' not in data or '// === Repeat
  guard ===' in data; print('ok')"` печатает `ok`.

## 3. Тесты

- [ ] 3.1 Юнит-тесты для `_canonical_arguments`:
  перестановка ключей верхнего уровня → совпадает;
  перестановка во вложенных `dict` → совпадает;
  разные значения → разные; `Path("/tmp/x")` vs `Path("/tmp/x")` →
  совпадает; `datetime(...)` детерминированно; NaN/Inf →
  `ValueError` (`allow_nan=False`); `Path("/tmp/")` +
  `Path("/tmp")` — нормализация через `str` сохраняет
  различие (или комментарий в спеке / docs о детерминизме).
  **Verify:** `pytest tests/test_repeat_guard_hook.py::test_canonical -v` зелёный.

- [ ] 3.2 Юнит-тесты для скользящего окна (глобальное):
  при `window_size=5`, `max_repeats=3` серия
  `read(A)`, `exec(X)`, `read(A)`, `history(Y)`, `read(A)` →
  срабатывание на 5-м вызове (mode `block`/`warn`).
  **Verify:** `pytest tests/test_repeat_guard_hook.py::test_global_window -v` зелёный.

- [ ] 3.3 Юнит-тесты на eviction: при `window_size=3` серия
  `A`, `A`, `B`, `C`, `A` → последний `A` НЕ считается третьим
  повтором (буфер `A`, `B`, `C` после эвикции), даже если
  по факту вызовов `A` было 3 за пределами окна.
  **Verify:** `pytest
  tests/test_repeat_guard_hook.py::test_window_eviction -v` зелёный.

- [ ] 3.4 Юнит-тесты для режимов: `mode=off` → no-op и нет
  записей; `mode=warn` → при crossing enqueue + ровно одно
  событие на crossing (последующие повторы НЕ публикуют новое);
  `mode=block` → `RuntimeError` с префиксом `repeat-guard`.
  **Verify:** `pytest tests/test_repeat_guard_hook.py::test_modes -v` зелёный.

- [ ] 3.5 Юнит-тесты для разных `(tool, canonical_args)` —
  `tool_a({"x":1})` и `tool_b({"x":1})` не считаются повтором.
  **Verify:** `pytest
  tests/test_repeat_guard_hook.py::test_distinct_tools -v` зелёный.

- [ ] 3.6 Юнит-тесты для `exempt_tools`: точное совпадение
  имени исключает вызов из проверки; вызов `read_file` при
  `exempt_tools=["read_*"]` → `ConfigurationError` на старте;
  `exempt_tools=["my_tool"]`, вызовы `read_file` продолжают
  проверяться. **Verify:** `pytest
  tests/test_repeat_guard_hook.py::test_exempt_tools -v` зелёный.

- [ ] 3.7 Интеграционный тест concurrent sessions: два
  независимых `session_key`, каждый со своим `AgentHookContext`;
  повторы в session A не должны влиять на счётчик в session B;
  сброс state при `before_run` (новый оборот того же `session_key`
  → пустой буфер). **Verify:** `pytest
  tests/test_repeat_guard_hook.py::test_concurrent_sessions -v` зелёный.

- [ ] 3.8 Интеграционный тест на сбой enqueue: `DbLoggingService`
  со stub'ом, бросающим исключение из `log_event`; проверка,
  что защитник логирует WARNING через loguru и НЕ падает;
  в режиме `block` вызов всё равно подменяется ошибкой.
  **Verify:** `pytest
  tests/test_repeat_guard_hook.py::test_logging_failure -v` зелёный.

- [ ] 3.9 Расширить `tests/test_runtime_inventory.py`: добавить
  `RepeatGuardHook` в ожидаемый список framework-хуков.
  **Verify:** `pytest tests/test_runtime_inventory.py -v` зелёный.

- [ ] 3.10 Контрактный тест
  `tests/contract/test_repeat_guard_hook_contract.py`:
  проверить `isinstance(hook, AgentHook)`, вызвать подряд
  lifecycle-методы (`before_run`, `before_execute_tools`,
  `before_execute_tool`, `on_execute_tool_error`,
  `after_execute_tool`, `after_run`) с фейковым
  `AgentHookContext`-стабом и убедиться, что они не падают
  на пустом state (особенно `on_execute_tool_error`, который
  пройдёт через синтетический `RuntimeError` в режиме `block`).
  **Verify:** `pytest
  tests/contract/test_repeat_guard_hook_contract.py -v` зелёный.

## 4. Документация и CHANGELOG

- [ ] 4.1 В `docs/ARCHITECTURE.md` рядом с `gateway.tool_result_limits.*`
  добавить абзац про `gateway.repeat_guard.*` с тремя режимами
  и ссылкой на OpenSpec change. Упомянуть пример `exempt_tools`
  как иллюстративный (без `must`-семантики в дефолте).
  **Verify:** `rg "repeat_guard" docs/ARCHITECTURE.md` находит 2+ упоминания.

- [ ] 4.2 В корневом `AGENTS.md` (секция «Configuration») добавить
  строку: `- Режим защитника циклов \`gateway.repeat_guard.*\`
  (\`off\` по умолчанию; \`mode\`, \`window_size\`,
  \`max_repeats_in_window\`, \`exempt_tools\`; см.
  openspec/specs/runtime/anti-loop/spec.md)`.
  **Verify:** `rg "gateway.repeat_guard" AGENTS.md` печатает 1+ строку.

- [ ] 4.3 Добавить запись в `CHANGELOG.md` под `### Added` секции
  текущего `[Unreleased]`: `RepeatGuardHook: framework hook
  anti-loop для общего случая повторных tool-вызовов,
  конфигурация через gateway.repeat_guard.* (дефолт off);
  см. openspec/changes/repeat-guard-hook/`.
  **Verify:** `rg "repeat-guard-hook" CHANGELOG.md` печатает 1+ строку.

## 5. Финализация

- [ ] 5.1 `openspec.cmd validate repeat-guard-hook` — exit 0,
  в выводе нет `FAIL`/`ERROR`. **Verify:** команда возвращает 0.

- [ ] 5.2 `pytest tests/test_repeat_guard_hook.py
  tests/test_runtime_inventory.py
  tests/contract/test_repeat_guard_hook_contract.py -v` —
  весь список зелёный. **Verify:** команда возвращает 0.

- [ ] 5.3 Сборка тестов всего проекта:
  `python -m pytest tests/ -q --collect-only | grep -c "::"` —
  число собранных тестов ≥ 3603, нет import-циклов.
  **Verify:** число ≥ 3603, exit 0.
