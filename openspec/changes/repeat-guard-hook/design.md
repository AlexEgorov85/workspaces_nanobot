## Context

В проекте единственный runtime-guard уровня агента — upstream
`max_tool_iterations = 200` (`config.json:13`,
`nanobot/agent/runner.py:435`) — и узкие hardcoded throttles для
`web_fetch`/`web_search` (`nanobot/utils/runtime.py:109`,
`_MAX_REPEAT_EXTERNAL_LOOKUPS = 2`) и workspace-bypass
(`nanobot/utils/runtime.py:173`,
`_MAX_REPEAT_WORKSPACE_VIOLATIONS = 2`). Эти throttles модульно-константные
и не расширяются на остальные tool'ы (`read_file`, `exec`,
`history_search` и др.). Все runtime-события проходят через
долговечный журнал `agent_gateway_logs` посредством
`DbLoggingService.try_log_event` (`lib/services/db_logging_service.py::try_log_event`
и примеры в `lib/services/runtime_events_subscriber.py:268,316`).
Hooks наследуют `nanobot.agent.hook.AgentHook`
(`nanobot/agent/hook.py:66-152`) и регистрируются через
`AgentFactory.create` (`lib/core/agent_factory.py:117-150`).
Канонические списки хуков — в `lib/services/runtime_inventory.py`
(`canonical_framework_hooks()`, строки 68-85) для прогона
`tools/diagnose_startup.py`. Спека контракта всех hooks —
`openspec/specs/runtime/agent-hooks/spec.md` (включая требования
к `AgentHook`-наследованию и `aclose`-lifecycle).

## Goals / Non-Goals

**Goals:**

- Дать runtime-защитнику от повторных tool-вызовов (вне web-fetch
  и workspace-bypass) настраиваемое поведение через
  `gateway.repeat_guard.*` с дефолтом `off` (полная обратная
  совместимость с существующими деплоями).
- Использовать канонические подсистемы: `AgentHook` базовый класс,
  `DbLoggingService.try_log_event` для журналирования, канонический
  `LogEvent` для структурированных payload'ов,
  `canonical_framework_hooks()` для инвентаря.
- Детектирование через **глобальный скользящий буфер** (cross-tool)
  с нормализованным представлением `(tool_name, canonical_args)` и
  **точным равенством** для детекции (НЕ через криптографические
  хеши).
- Журналирование ровно один раз на момент превышения порога —
  защита от log-storm'а при зациклившемся агенте в режиме `warn`.
- Fail-soft семантика: внутренние ошибки защитника никогда не
  прерывают оборот и не валят tool-вызов.
- Изоляция per-turn по `session_key`: конкурентные сессии не делят
  state.

**Non-Goals:**

- Модификация upstream `max_tool_iterations` или
  `repeated_external_lookup_error` /
  `repeated_workspace_violation_error`. Это fallback верхнего
  уровня.
- Детектирование по результату tool'а (result-equality). Требует
  хранения хешей результатов в окне и отдельного решения про
  размер/стоимость, выходит за scope.
- Глобальное (cross-turn) детектирование. Требует persistent state
  в БД и более сложной модели рисков (легитимное повторение похожего
  запроса в новом чате).
- UI-представление срабатываний (Streamlit/CLI-бейдж).
  Наблюдаемость — через `agent_gateway_logs`, как у
  `turn_completed`/`turn_failed`.
- Зашивание в дефолт имён конкретных инструментов проекта
  (`compact_context`, `history_search`, и т.п.). Оператор сам
  решает, что exempt'ить.

## Decisions

### Decision 1: поднимать `RuntimeError` в `before_execute_tool`

Защитник вызывает `RuntimeError(...)` из
`RepeatGuardHook.before_execute_tool(...)` при превышении порога
в режиме `block`. Этот путь уже поддержан upstream: исключение
ловится в `nanobot/agent/tools/execution.py:177-194`, вызывается
`on_execute_tool_error`, и возвращается синтетический
`"Error: <type>: <msg>"` как tool-результат, который модель видит
как обычный tool-error.

**Альтернативы:**

- **Monkey-patch `_execute_tool_call`** через `RuntimePatcher` —
  расширяет patch-surface, требует обновления
  `runtime-patcher-inventory.md`. Отвергнуто как избыточное.
- **`after_execute_tool`/`on_finally` для остановки оборота** —
  не соответствует upstream-семантике: точку решения нужно
  ставить ДО выполнения tool'а, не после.
- **`finalize_content` + event-flag на stop** — чище теоретически,
  но требует monkey-patch на `_assemble_outbound` /
  `TurnDelivery`. Расширяет patch-surface ещё больше.

**Почему A выигрывает:** нулевое изменение upstream-кода, нулевой
новый patch, нулевая модификация runner'а. Защитник остаётся
чисто-проектной сущностью в `lib/hooks/`.

### Decision 2: точное равенство canonical-представления для детекции, hex-truncation — только для observability

Детектирование в state'е использует **точное равенство**
нормализованного представления. Canonical-представление
формируется как:

```python
canonical_args = json.dumps(
    arguments,
    sort_keys=True,
    ensure_ascii=False,
    separators=(",", ":"),
    allow_nan=False,
    default=stable_fallback,
)
fingerprint = (tool_name, canonical_args)
```

Где `stable_fallback` — детерминированная функция для
несериализуемых типов:
- `Path` → `str(value)` (POSIX-путь);
- `datetime` → ISO-формат через `.isoformat()`;
- `bytes` → `"<bytes:n=...>"` repr с явной длиной;
- произвольный объект → `str(value)` если результат детерминирован,
  иначе → «пропуск вызова» (без занесения в буфер).

Никаких nondeterministic значений (PID, адреса объектов,
`__repr__` со случайными id) в fingerprint не попадает.

Для **журнала** (поле `payload.fingerprint_hash`) дополнительно
используется `blake2b(f"{tool_name}\x00{canonical_args}".encode(),
digest_size=4).hexdigest()` — 8-char строка для observability.
Этот хэш НЕ участвует в логике детекции; collisions даже в случае
их возникновения (что технически возможно для 32-бит digest'а,
но крайне маловероятно для типичных args) не приводят к ложным
срабатываниям или пропускам — детекция идёт по точному равенству
canonical-представления.

**Альтернативы:**

- **Хеш-в-state (любой ширины)** — теоретически быстрее на
  длинных args, но вводит класс collision-багов и
  усложняет отладку (нельзя посмотреть «что было» в fingerprint'е).
- **Сырая строка `tool_name + JSON`** — проще, но 4-10KB JSON на
  вызов × 20 в окне — десятки KB на типичном обороте. Избыточно.
- **`str(arguments)` для всего** — не работает для вложенных
  структур и даёт нестабильное представление для dict'ов.

**Почему A выигрывает:** нулевые коллизии в state (точное
сравнение), компактный observability-id в журнале, полная
читаемость canonical-представления для отладки.

### Decision 3: глобальный скользящий буфер на сессию

State защитника — `dict[session_key, deque[tuple[tool_name,
fingerprint_tuple, iteration]]]` с `maxlen=window_size` на каждый
`session_key`. Это даёт O(1) push/pop и естественную эвикцию
старых записей. Сброс — через `before_run` для текущего
`session_key` (или глобально для всех, если session_key ещё не
известен).

**Альтернативы:**

- **`dict[tool_name, deque[...]]`** (per-tool окна) — даёт
  некорректную семантику: между идентичными вызовами могут быть
  другие инструменты; per-tool окно считало бы их повтором.
  Семантически отличается от контракта скользящего окна.
  Отвергнуто.
- **Глобальный state без reset'а** — ломает изоляцию между
  оборотами. Отвергнуто.
- **`deque[int]` + `set` для membership'а** — усложняет эвикцию
  без выгоды. Отвергнуто.

**Почему A выигрывает:** ровно одна deque на сессию → простое
вычисление «сколько раз в окне встречается (tool, canonical)»
через обычный счётчик проходом по буферу. Подходит для типичного
`window_size = 20` (одна проходка по 20 элементам — negligible
стоимость).

### Decision 4: настройки в `GatewaySettings`, дефолт `mode = "off"`

Добавляем подмодель `GatewayRepeatGuardSettings` в
`lib/core/project_settings.py:218-264+`, подключаем как
опциональное поле `repeat_guard: GatewayRepeatGuardSettings | None = None`
в `GatewaySettings`.

Поля:

- `mode: Literal["off", "warn", "block"] = "off"`.
- `window_size: int = 20` (`ge=1, le=1000` — не больше 1000, чтобы
  ограничить память).
- `max_repeats_in_window: int = 3` (`ge=2, le=100`).
- `exempt_tools: list[str] = []` + `field_validator` на отсутствие
  glob/regex-метасимволов.

Поведение режимов:

- `off` → `before_execute_tool` немедленно `return` после проверки
  флага (zero-cost);
- `warn` → вычислить fingerprint, добавить в буфер, проверить
  crossing; при crossing → enqueue `tool_repeat_warned` через
  `DbLoggingService.try_log_event`; НЕ прерывать вызов;
- `block` → то же + поднять `RuntimeError` с сообщением
  `"repeat-guard: 3 identical read_file calls in last 5 iterations;
  use existing result or vary arguments"`.

Семантика подсчёта: `max_repeats_in_window = N` означает
"срабатывание на N-ом ИДЕНТИЧНОМ вызове" (текущий включается).
Первые `N-1` пропускаются, N-ый — блокируется / warnится.

**Альтернативы:**

- **Дефолт `block`** — сломал бы существующие деплои. Отвергнуто.
- **Зашитые exempt-tools по именам** — нарушает архитектурный
  принцип "framework/runtime — generic, Skill/tool — domain-specific".
  Отвергнуто.
- **Consecutive (`count_consecutive_same`) semantics** —
  пропустит повторы между которыми были другие инструменты; не
  подходит для общей защиты от зацикливания. Отвергнуто.

### Decision 5: ровно одно событие на момент crossing'а

В режимах `warn` и `block`: ровно одна публикация события через
`DbLoggingService.try_log_event` в момент, когда в буфере
оказывается ≥ `max_repeats_in_window` записей с данным
fingerprint'ом. Последующие повторы сверх порога до сброса state
(новый оборот, `session_key` сменился) НЕ публикуют новых
событий.

Реализуется через флаг `_last_published_fingerprint: set` в state
сессии: при каждом crossing'е fingerprint фиксируется; при
последующих вызовах того же fingerprint'а, пока он уже в set'е,
публикация пропускается. При эвикции fingerprint'а из буфера
(выпал из `deque(maxlen=window_size)`) — соответствующая запись
удаляется из `_last_published_fingerprint`.

**Альтернативы:**

- **Событие на каждый повтор сверх порога** — log-storm при
  зациклившемся агенте (200 итераций × 1 событие = до 200 DB
  записей на типичную петлю). Отвергнуто.
- **Событие только один раз за оборот** — теряет information о
  повторных срабатываниях на разные fingerprints в одном обороте.
  Отвергнуто.

### Decision 6: подключение после `ToolAuditHook`

В `lib/core/agent_factory.py::create` (после строки 121 с
`hooks.append(tool_audit_hook)`) добавляется чтение
`ctx._settings_ref.gateway.repeat_guard` и условный
`hooks.append(RepeatGuardHook(settings=..., db_logging_service=...))`.
В режиме `off` хук всё равно добавляется (zero-cost early-return
по флагу) — это упрощает каноническую регистрацию в
`canonical_framework_hooks()` для `diagnose_startup.py`.

**Альтернативы:**

- **Регистрация только при `mode != off`** — усложняет inventory
  и диагностику. Отвергнуто.

### Decision 7: журнал через `DbLoggingService.try_log_event`

Срабатывания публикуются через канонический helper `try_log_event`
(`lib/services/db_logging_service.py:34-101`) с `producer =
"RepeatGuardHook"` и `event_type ∈ {"tool_repeat_blocked",
"tool_repeat_warned"}`. `try_log_event` сам проверяет `svc is
None` / `not svc.is_running()`, ловит исключения и возвращает
`False` — это fail-soft-семантика producer'а, которая идеально
подходит нашему случаю.

Прямой SQL INSERT, `INSERT INTO agent_gateway_logs`, модуль
`event_log.py` (удалён) или собственный pipeline НЕ используются.

**Альтернативы:** см. предыдущие — все отвергнуты по архитектурным
инвариантам.

### Decision 8: canonical inventory + `diagnose_startup.py`

Добавляем новую запись `HookSpec(name="RepeatGuardHook",
kind="framework", required=False, description="защитник от
повторных tool-вызовов (mode off/warn/block)",
source="lib/hooks/repeat_guard_hook.py")` в
`canonical_framework_hooks()` (`lib/services/runtime_inventory.py:68-85`).
`required=False` соответствует факту: хук опциональный
(отключаемый через `mode = off`), и его отсутствие не считается
критичным drift'ом — `tools/diagnose_startup.py` получит эту
запись автоматически без дополнительных правок.

Альтернативная семантика `required=True, enabled=False` потребовала
бы расширения модели `HookSpec` (новое поле `enabled`) и
`tools/diagnose_startup.py` (умение различать «отсутствует» и
«отключён»), что выходит за scope этого change.

## Risks / Trade-offs

- **[Risk] Деградация latency из-за каждого вызова** — на каждый
  tool-вызов идёт JSON-нормализация. → **Mitigation:** в режиме
  `off` early-return без вычислений; JSON-кодирование типичных
  args (<4KB) — единицы микросекунд; счётчик по буферу длиной
  ≤1000 — negligible стоимость.
- **[Risk] Маскировка легитимных повторов** (polling статуса,
  идемпотентный retry, ручные вызовы пользователя).
  → **Mitigation:** `exempt_tools` (по умолчанию пустой) +
  детальное логирование в `agent_gateway_logs` для
  расследования инцидентов; оператор сам выбирает tradeoff
  между безопасностью и удобством через `mode = off|warn|block`.
- **[Risk] Утечка памяти при долгом обороте (200 итераций × N
  вызовов).** → **Mitigation:** `deque(maxlen=window_size)`
  per-session; суммарно ≤ `window_size × 1` записей
  (типично ≤20); `_last_published_fingerprint` тоже ограничен
  размером буфера.
- **[Risk] `RuntimeError` от `before_execute_tool` приводит к
  side-эффекту в `on_execute_tool_error` других хуков.**
  → **Mitigation:** `RuntimeError` — стандартный upstream-путь,
  никаких новых side-эффектов; другие хуки видят `event_type=
  "error"`, как и для любого другого исключения в tool.
- **[Risk] Unbounded `_last_published_fingerprint` set.**
  → **Mitigation:** удаление записей при эвикции fingerprint'а
  из основного буфера (те же `append`/`popleft` синхронизированно).
- **[Risk] Несовместимость с upstream-изменениями в
  `_execute_tool_call`.** → **Mitigation:** поведение
  `before_execute_tool` + exception-handling в
  `tools/execution.py:177-194` — публичный стабильный контракт
  с версии 0.3.5; контракт покрыт `tests/contract/`.
- **[Risk] Log-storm в `warn`/`block` режимах (отменён).**
  → **Mitigation:** ровно одно событие на момент crossing'а
  (см. Decision 5); `_last_published_fingerprint` синхронизирован
  с эвикцией из буфера.

## Migration Plan

- **Deploy:** change не требует миграций БД, новых фич или
  поэтапного rollout. Конфиг `gateway.repeat_guard.mode = "warn"`
  или `"block"` включается оператором одной строкой в `project.json`
  после обновления. Дефолт `off` гарантирует, что любое обновление
  без правок `project.json` ведёт себя как раньше.
- **Rollback:** `mode = off` (или удаление секции
  `gateway.repeat_guard`) возвращает поведение в исходное
  состояние. Никаких persistent эффектов.
- **CHANGELOG:** отдельная запись в `CHANGELOG.md` под
  `### Added` секции текущего `[Unreleased]`.

## Open Questions

Нет. Все решения в этой стадии финальны; дополнительные вопросы
(UI-индикация в Streamlit/CLI, cross-turn детекция,
result-equality) явно вынесены в Non-Goals и описаны в
`proposal.md` как потенциальные follow-up изменения.
