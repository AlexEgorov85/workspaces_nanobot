## 1. Конфигурация и скелет модуля

- [x] 1.1 Добавить `GatewayRepeatGuardSettings` (pydantic) с полями
  `mode: Literal["off", "warn", "block"] = "off"`,
  `window_size: int = 20` (с `ge=1, le=1000`),
  `max_repeats_in_window: int = 3` (с `ge=2, le=100`),
  `exempt_tools: list[str] = []` + `field_validator` отвергающий
  glob/regex-метасимволы `* ? [ ] ^ $`.
  **Verify:** `python -c "from lib.core.project_settings import
  GatewayRepeatGuardSettings; print(GatewayRepeatGuardSettings().model_dump())"`
  печатает дефолты без `ValidationError`.
  **Выполнено.** Поля, границы и валидатор — в точности по плану.
  `mode` вынесен в `Literal`, `exempt_tools` — `default_factory=list`
  (не общий мутабельный список).

- [x] 1.2 Подключить `repeat_guard: GatewayRepeatGuardSettings | None = None`
  в `GatewaySettings` (`lib/core/project_settings.py:218-264`).
  **Verify:** `python -c "from lib.core.project_settings import
  GatewaySettings; print(GatewaySettings().repeat_guard)"` печатает
  `None` (новые ключи опциональны).
  **Выполнено, с инцидентом.** Первая вставка класса в середину
  `GatewaySettings` обрезала родительский класс и утянула в новый класс
  его валидатор `_reject_legacy_renamed_sections` — legacy-секции
  `gateway.*` перестали падать, а тест
  `test_legacy_vector_index_top_level_rejected` это поймал
  (`DID NOT RAISE`). Класс перенесён ниже `GatewaySettings`, в его
  docstring записано почему именно так (в Python вложенная модель
  разрешена и раньше, но одна правка порядка классов тут уже стоила
  тихой поломки fail-fast'а).

- [x] 1.3 Создать `lib/hooks/repeat_guard_hook.py` с хелперами
  `_canonical_arguments(arguments)` (через `json.dumps(..., sort_keys=
  True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
  default=_stable_fallback)`) и
  `_stable_fallback(value)` (детерминированные правила для
  `Path`/`datetime`/`bytes`/произвольных объектов; nondeterministic →
  явный sentinel для skip'а). **Verify:** `pytest
  tests/test_repeat_guard_hook.py::test_canonical -v` зелёный.
  **Выполнено.** Sentinel `__non_deterministic__` + исключение
  `_non_deterministic()`, которое `before_execute_tool` ловит и
  **пропускает вызов** (молчаливый пропуск скрыл бы настоящий цикл).

- [x] 1.4 Реализовать класс `RepeatGuardHook(AgentHook)` в том же
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
  **Выполнено с отклонением.** Сброс перенесён из `before_run` в
  `before_iteration` (`iteration == 0`): у `AgentRunHookContext` в
  nanobot 0.3.5 **нет поля `session_key`** — раннер создаёт его как
  `AgentRunHookContext(messages=...)` (`runner.py:311`). Адресный сброс
  там невозможен, а глобальный стирал бы буферы параллельных оборотов.
  `before_iteration` получает `AgentHookContext` с `session_key`
  (`runner.py:436-440`). Вместо чистки в `after_run` (тоже без ключа
  сессии) введён потолок `_MAX_TRACKED_SESSIONS = 512` с LRU по
  `time.monotonic()`; `after_run` подчищает множество `published`.
  Подробности — `design.md` Decision 1a и 1b.

## 2. Интеграция и inventory

- [x] 2.1 В `lib/core/agent_factory.py::create` (после строки 121 с
  `hooks.append(tool_audit_hook)`) добавить чтение
  `ctx._settings_ref.gateway.repeat_guard` (прямое чтение из
  конфига, без pydantic-модели в kwargs) и условный
  `hooks.append(RepeatGuardHook(settings=...,
  db_logging_service=db_logging_service))`. **Verify:** новый
  интеграционный тест 3.6 зелёный.
  **Выполнено с двумя отклонениями.** (а) Хук добавляется **после
  `TerminalToolPrintHook`** и **безусловно**, включая `mode="off"`:
  иначе канон `canonical_framework_hooks()` разошёлся бы с фактом и
  `diagnose_startup.py` кричал бы о ложном drift'е. В режиме `off`
  хук — no-op с ранним выходом. (б) Настройка читается через
  `_read_repeat_guard_settings(settings)`, а не через
  `ctx._settings_ref`: `create()` принимает `settings` явно, а
  завязка на приватное поле контекста сделала бы фабрику зависимой
  от его внутреннего состояния.

- [x] 2.2 Добавить запись `HookSpec(name="RepeatGuardHook",
  kind="framework", required=False, description="защитник от
  повторных tool-вызовов (mode off/warn/block)",
  source="lib/hooks/repeat_guard_hook.py")` в
  `canonical_framework_hooks()` (`lib/services/runtime_inventory.py:68-85`).
  **Verify:** `python -c "from lib.services.runtime_inventory
  import canonical_framework_hooks; print(any(h.name ==
  'RepeatGuardHook' for h in canonical_framework_hooks()))"`
  печатает `True`.
  **Выполнено.** `required=False` обоснован, а не просто разрешён:
  `tests/test_runtime_inventory.py` проверяет, что `before_execute_tool`
  действительно гейтится `mode == "off"`. Без этой проверки флаг
  «обязателен, если фича включена» был бы декларацией без механизма —
  ровно тот дефект, который был найден в `startup-schema-validation`.

- [x] 2.3 В `project.json` (JSONC) добавить закомментированный
  пример блока `gateway.repeat_guard` под отдельной секцией
  `// === Repeat guard ===`. **Verify:** `python -c "import re;
  data = open('project.json', encoding='utf-8').read();
  assert 'gateway.repeat_guard' not in data or '// === Repeat
  guard ===' in data; print('ok')"` печатает `ok`.
  **Выполнено.** Блок вставлен в `gateway`, рядом с
  `gateway.tool_result_limits`, с пояснением, почему `block`
  требует патча.

## 3. Тесты

- [x] 3.1 Юнит-тесты для `_canonical_arguments`:
  перестановка ключей верхнего уровня → совпадает;
  перестановка во вложенных `dict` → совпадает;
  разные значения → разные; `Path("/tmp/x")` vs `Path("/tmp/x")` →
  совпадает; `datetime(...)` детерминированно; NaN/Inf →
  `ValueError` (`allow_nan=False`); `Path("/tmp/")` +
  `Path("/tmp")` — нормализация через `str` сохраняет
  различие (или комментарий в спеке / docs о детерминизме).
  **Verify:** `pytest tests/test_repeat_guard_hook.py::test_canonical -v` зелёный.
  **Выполнено.** Различие `Path("/tmp/")` vs `Path("/tmp")`
  сохраняется и зафиксировано тестом: нормализация через `str` не
  должна склеивать пути, иначе два разных вызова выглядели бы
  одинаковыми.

- [x] 3.2 Юнит-тесты для скользящего окна (глобальное):
  при `window_size=5`, `max_repeats=3` серия
  `read(A)`, `exec(X)`, `read(A)`, `history(Y)`, `read(A)` →
  срабатывание на 5-м вызове (mode `block`/`warn`).
  **Verify:** `pytest tests/test_repeat_guard_hook.py::test_global_window -v` зелёный.
  **Выполнено.** Тест вскрыл off-by-one в счётчике: текущий вызов уже
  лежит в `deque`, но `count = 1 + sum(...)` считал его дважды, и
  порог срабатывал на `max_repeats - 1` — то есть блокировался бы
  второй вызов вместо третьего. Исправлено на `count = sum(...)`,
  семантика закреплена тестом
  `test_counter_includes_current_call`.

- [x] 3.3 Юнит-тесты на eviction: при `window_size=3` серия
  `A`, `A`, `B`, `C`, `A` → последний `A` НЕ считается третьим
  повтором (буфер `A`, `B`, `C` после эвикции), даже если
  по факту вызовов `A` было 3 за пределами окна.
  **Verify:** `pytest
  tests/test_repeat_guard_hook.py::test_window_eviction -v` зелёный.
  **Выполнено, план пересобран.** Исходный план не проверял
  эвикцию: серия из 5 вызовов срабатывала бы и с `maxlen=3`, и без
  него. Новая серия `A, A, B, C, A, A, A` (окно 3, порог 3) даёт
  блокировку только на 7-м вызове, а 5-й — контрольный: без
  эвикции порог был бы пройден на нём. Промежуточная правка теста
  ошибалась в арифметике `deque(maxlen=3)` — `append` вытесняет и
  самый левый A, поэтому в окне `[B, C, A]` ровно один A, а не два.

- [x] 3.4 Юнит-тесты для режимов: `mode=off` → no-op и нет
  записей; `mode=warn` → при crossing enqueue + ровно одно
  событие на crossing (последующие повторы НЕ публикуют новое);
  `mode=block` → `RuntimeError` с префиксом `repeat-guard`.
  **Verify:** `pytest tests/test_repeat_guard_hook.py::test_modes -v` зелёный.
  **Выполнено, тип уточнён.** Поднимается не `RuntimeError`, а
  собственный `RepeatGuardBlocked(RuntimeError)` — патч обязан отличать
  «отказ защитника» от любой чужой ошибки хука, а `RuntimeError` от
  этого не отличает. Префикс `repeat-guard:` в сообщении сохранён.

- [x] 3.5 Юнит-тесты для разных `(tool, canonical_args)` —
  `tool_a({"x":1})` и `tool_b({"x":1})` не считаются повтором.
  **Verify:** `pytest
  tests/test_repeat_guard_hook.py::test_distinct_tools -v` зелёный.
  **Выполнено, план пересобран.** Исходный порог `max_repeats=2` при
  чередовании `tool_a`/`tool_b` в 6 вызовах давал по два вхождения
  каждого — и блокировал, т.е. тест доказывал обратное своему
  названию. Корректный сценарий: порог 3, чередование из 4 вызовов →
  ни одна пара порога не достигает.

- [x] 3.6 Юнит-тесты для `exempt_tools`: точное совпадение
  имени исключает вызов из проверки; вызов `read_file` при
  `exempt_tools=["read_*"]` → `ConfigurationError` на старте;
  `exempt_tools=["my_tool"]`, вызовы `read_file` продолжают
  проверяться. **Verify:** `pytest
  tests/test_repeat_guard_hook.py::test_exempt_tools -v` зелёный.
  **Выполнено.**

- [x] 3.7 Интеграционный тест concurrent sessions: два
  независимых `session_key`, каждый со своим `AgentHookContext`;
  повторы в session A не должны влиять на счётчик в session B;
  сброс state при `before_run` (новый оборот того же `session_key`
  → пустой буфер). **Verify:** `pytest
  tests/test_repeat_guard_hook.py::test_concurrent_sessions -v` зелёный.
  **Выполнено, с учётом 1.4.** Сброс проверяется при
  `before_iteration(iteration=0)`, а не в `before_run`: у
  `AgentRunHookContext` нет `session_key` (см. 1.4). Ключевая проверка
  изоляции — повторы в `s1` не двигают счётчик в `s2`, при том что
  оба контекста строятся на настоящем `AgentHookContext` со
  `slots=True`.

- [x] 3.8 Интеграционный тест на сбой enqueue: `DbLoggingService`
  со stub'ом, бросающим исключение из `log_event`; проверка,
  что защитник логирует WARNING через loguru и НЕ падает;
  в режиме `block` вызов всё равно подменяется ошибкой.
  **Verify:** `pytest
  tests/test_repeat_guard_hook.py::test_logging_failure -v` зелёный.
  **Выполнено, деталь уточнена.** `try_log_event` — модульная
  функция `lib.services.db_logging_service`, а не метод
  `DbLoggingService`: стаб обязан удовлетворять её контракту
  (`is_running()` + `log_event()`), иначе проверка уходит по ложному
  пути «сервиса нет». Также устранён двойной префикс в логuru-записи
  (`repeat-guard: repeat-guard: ...`).

- [x] 3.9 Расширить `tests/test_runtime_inventory.py`: добавить
  `RepeatGuardHook` в ожидаемый список framework-хуков.
  **Verify:** `pytest tests/test_runtime_inventory.py -v` зелёный.
  **Выполнено.** Попутно исправлен устаревший docstring'и ссылающийся
  на несуществующий `test_optional_framework_hook_is_config_gated`.

- [x] 3.10 Контрактный тест
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
  **Выполнено, объём расширен.** Стабы контекста заменены
  настоящими `AgentHookContext` / `AgentRunHookContext` /
  `ToolCallRequest` — стаб не увидел бы, что `_execute_tool_call`
  вызывается upstream **позиционно**, и `kwargs["hook"]` всегда был бы
  `None` (второй по счёту аргумент, а не первый). Исправлено на
  `signature(original).bind_partial(...).arguments[name]`.
  Добавлены сценарии, которых не было в плане: отказ превращается в
  синтетический результат, соседний вызов батча `concurrent` при этом
  исполняется (иначе `asyncio.gather` без `return_exceptions=True`
  отменил бы весь батч), и чужая ошибка хука не маскируется.
  `RepeatGuardHook` добавлен в `CUSTOM_HOOKS`
  (`tests/contract/test_hook_subclass_contract.py`), где инвариант
  `super().__init__()` расширен до `super().__init__(reraise=...)` и
  дополнен проверкой, что флаг **реально выставлен на инстансе** —
  проверка по исходнику пропускала бы вызов в неисполняемой ветке.

## 4. Документация и CHANGELOG

- [x] 4.1 В `docs/ARCHITECTURE.md` рядом с `gateway.tool_result_limits.*`
  добавить абзац про `gateway.repeat_guard.*` с тремя режимами
  и ссылкой на OpenSpec change. Упомянуть пример `exempt_tools`
  как иллюстративный (без `must`-семантики в дефолте).
  **Verify:** `rg "repeat_guard" docs/ARCHITECTURE.md` находит 2+ упоминания.
  **Выполнено.** Добавлен отдельный подраздел с разбором того, почему
  `block` требует патча — иначе читатель повторит допущение,
  опровергнутое в `design.md`.

- [x] 4.2 В корневом `AGENTS.md` (секция «Configuration») добавить
  строку: `- Режим защитника циклов \`gateway.repeat_guard.*\`
  (\`off\` по умолчанию; \`mode\`, \`window_size\`,
  \`max_repeats_in_window\`, \`exempt_tools\`; см.
  openspec/specs/runtime/anti-loop/spec.md)`.
  **Verify:** `rg "gateway.repeat_guard" AGENTS.md` печатает 1+ строку.
  **Выполнено.** Попутно: в перечне `lib/hooks/` добавлен
  `repeat_guard_hook.py` с порядком подключения, а счётчик патчей в
  описании `lib/services/runtime_patcher.py` исправлен с 6 на 7
  (иначе инструкция для нано-агента врала бы).

- [x] 4.3 Добавить запись в `CHANGELOG.md` под `### Added` секции
  текущего `[Unreleased]`: `RepeatGuardHook: framework hook
  anti-loop для общего случая повторных tool-вызовов,
  конфигурация через gateway.repeat_guard.* (дефолт off);
  см. openspec/changes/repeat-guard-hook/`.
  **Verify:** `rg "repeat-guard-hook" CHANGELOG.md` печатает 1+ строку.
  **Выполнено.** Запись явно называет седьмой патч расширением scope
  относительно исходного design и приводит причину.

## 5. Финализация

- [x] 5.1 `openspec.cmd validate repeat-guard-hook` — exit 0,
  в выводе нет `FAIL`/`ERROR`. **Verify:** команда возвращает 0.
  **Выполнено** (и в `--strict`): `Change 'repeat-guard-hook' is valid`,
  exit 0.

- [x] 5.2 `pytest tests/test_repeat_guard_hook.py
  tests/test_runtime_inventory.py
  tests/contract/test_repeat_guard_hook_contract.py -v` —
  весь список зелёный. **Verify:** команда возвращает 0.
  **Выполнено:** 31 + 8 зелёных.

- [x] 5.3 Сборка тестов всего проекта:
  `python -m pytest tests/ -q --collect-only | grep -c "::"` —
  число собранных тестов ≥ 3603, нет import-циклов.
  **Verify:** число ≥ 3603, exit 0.
  **Выполнено** (результат — в разделе 6).
