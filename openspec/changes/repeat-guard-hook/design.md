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

### Decision 1: поднимать `RepeatGuardBlocked` и перехватывать его патчем

> **Это решение заменяет первоначальную редакцию.** Оно опиралось на
> допущение, что исключение из `before_execute_tool` штатно
> превращается в синтетический `Error: …`. Допущение опровергнуто на
> живом коде nanobot 0.3.5, и ниже это зафиксировано, чтобы следующий
> читатель не проверял его заново.

Защитник вызывает `RepeatGuardBlocked(RuntimeError)` из
`RepeatGuardHook.before_execute_tool(...)` при превышении порога
в режиме `block`. Исключение перехватывает
`RuntimePatcher.patch_repeat_guard_block`, который подменяет
`_execute_tool_call` в `nanobot.agent.tools.execution` и возвращает
`("Error: RepeatGuardBlocked: repeat-guard: …", {name, status: "error",
detail})` — результат того же вида, что и штатная ошибка инструмента,
плюс вызывает `on_execute_tool_error` с исходным контекстом вызова.

**Почему штатный путь непригоден.** Проверено на исходниках 0.3.5:

1. `HookRegistry._for_each_hook_safe` (`nanobot/agent/hook.py:174-183`)
   глотает исключение каждого хука и логирует его. Без
   `reraise=True` режим `block` — **молчаливый no-op**: состояние
   обновилось бы, а вызов всё равно выполнился. Защитник, который
   «вроде блокирует», но не блокирует, хуже отсутствия защитника: он
   даёт ложную гарантию.
2. С `reraise=True` исключение пробрасывается, но
   `before_execute_tool` вызывается в `tools/execution.py:165` —
   **вне** `try`, который начинается строкой 166. Синтетический
   `"Error: …"` не образуется: исключение уходит из
   `execute_tool_calls` наверх, весь оборот падает с
   `stop_reason="error"`, а в режиме `concurrent`
   `asyncio.gather` без `return_exceptions=True` отменяет ещё и
   соседние вызовы батча.
3. Подменить вызов из `ctx.tool_calls` нельзя: `runner.py:483`
   присваивает `context.tool_calls = list(response.tool_calls")` —
   копию; мутация из хука ничего не отменила бы.

Остаётся ровно одна точка, где результат ещё можно подменить, — сама
`_execute_tool_call`.

**Альтернативы:**

- **Полагаться на штатный `except` в `execution.py:177-194`** —
  отвергнуто: `before_execute_tool` вне `try` (см. п. 2).
- **Патчить `execute_tool_calls` вместо `_execute_tool_call`** —
  шире поверхность патча и не даёт доступа к `hook`/`context` в той
  форме, в какой их передаёт upstream (позиционно). Отвергнуто.
- **Блокировать на уровне `before_execute_tools`, снимая вызов из
  `ctx.tool_calls`** — отвергнуто: поле уже копия (п. 3), и снятие
  из копии не изменит ни факт выполнения, ни порядок результатов.
- **Глобальный reset в `before_run`** вместо `before_iteration` —
  отвергнуто, см. Decision 3.

**Цена решения:** седьмой runtime-патч и его каталог в
`docs/architecture/runtime-patcher-inventory.md`. Это осознанное
расширение scope относительно первоначального design, который требовал
«нулевого нового патча». Альтернатива с нулевым патчем — оставить
режим `block` непригодным (молчаливый no-op либо обрыв оборота), что
хуже: оператор включил бы режим и получил бы либо отсутствие защиты,
либо падение каждого зациклившегося оборота вместо короткого
tool-ошибки. Патч ловит **только** `RepeatGuardBlocked`, поэтому
остальные ошибки хуков остаются видимыми.

### Decision 1a: сброс state в `before_iteration`, а не в `before_run`

> Тоже уточнение первоначальной редакции (см. Decision 3).

`AgentRunHookContext` в nanobot 0.3.5 **не содержит** `session_key` —
его раннер создаёт как `AgentRunHookContext(messages=...)`
(`runner.py:311`). Сброс по ключу там физически невозможен, а
глобальный сброс на каждом `before_run` стирал бы буферы параллельных
оборотов. У `AgentHookContext` ключ есть (`runner.py:436-440`), и
`iteration == 0` однозначно означает первый вызов нового оборота.
Сброс перенесён туда.

### Decision 1b: ограничение числа отслеживаемых сессий вместо чистки в `after_run`

`after_run` получает `AgentRunHookContext` — тоже без `session_key`,
поэтому адресно удалить state сессии оттуда нельзя. Вместо этого
держится жёсткий потолок `_MAX_TRACKED_SESSIONS = 512` с LRU-вытеснением
по `time.monotonic()` на касание буфера. На долгоживущем gateway память
остаётся ограниченной без контракта на явную очистку. `after_run` при
этом подчищает только множество `published` — выкидывает из него
fingerprint'ы, уже выпавшие из окна, иначе вернувшийся в окно вызов не
опубликует событие, хотя для оператора это снова crossing.

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
fingerprint_tuple]]]` с `maxlen=window_size` на каждый `session_key`
плюс множество `published` (fingerprint'ы, для которых событие уже
опубликовано в этом окне). Это даёт O(1) push/pop и естественную
эвикцию старых записей. Сброс — в `before_iteration` при
`iteration == 0` для текущего `session_key`; см. Decision 1a, почему
не в `before_run`.

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
- `block` → то же + поднять `RepeatGuardBlocked` с сообщением
  `"repeat-guard: 3 identical read_file calls in last 5 iterations;
  use existing result or vary arguments"` (см. Decision 1).

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

Реализуется через множество `published` в state'е сессии: при
каждом crossing'е fingerprint фиксируется; при последующих вызовах
того же fingerprint'а, пока он уже в множестве, публикация
пропускается. Когда fingerprint выпадает из
`deque(maxlen=window_size)`, его запись удаляется из `published` —
иначе вернувшийся в окно вызов не опубликует событие, хотя для
оператора это снова crossing. Чистка выполняется в `after_run` по всем
сессиям: `deque` не сообщает, какой именно элемент вытеснился, а
держать обратный индекс ради этого дороже, чем один проход по буферам
на завершении оборота.

**Альтернативы:**

- **Событие на каждый повтор сверх порога** — log-storm при
  зациклившемся агенте (200 итераций × 1 событие = до 200 DB
  записей на типичную петлю). Отвергнуто.
- **Событие только один раз за оборот** — теряет information о
  повторных срабатываниях на разные fingerprints в одном обороте.
  Отвергнуто.

### Decision 6: подключение после `ToolAuditHook`

В `lib/core/agent_factory.py::create` добавляется чтение
`settings.gateway.repeat_guard` и безусловный
`hooks.append(RepeatGuardHook(settings=..., db_logging_service=...))`
сразу после `TerminalToolPrintHook`. В режиме `off` хук всё равно
добавляется (zero-cost early-return по флагу) — это упрощает
каноническую регистрацию в `canonical_framework_hooks()` для
`diagnose_startup.py`.

Чтение настройки идёт через `_read_repeat_guard_settings`, а не через
поле на `ApplicationContext`: `create()` принимает `settings` явно, и
завязка на приватный `ctx._settings_ref` сделала бы фабрику
зависимой от внутреннего состояния контекста.

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
  (типично ≤20); множество `published` ограничено тем же размером
  буфера, а число сессий — потолком `_MAX_TRACKED_SESSIONS`.
- **[Risk] `RepeatGuardBlocked` виден остальным хукам как
  side-эффект в `on_execute_tool_error`.**
  → **Mitigation:** это ровно тот же контракт, что и для любой другой
  ошибки инструмента: патч вызывает `on_execute_tool_error` с
  исходным `context`/`tool_call`/`tool`/`params`, и остальные хуки видят
  обычный отказ, а не экзотическое событие. Но защитник НЕ может
  навесить на этот вызов ничего своего — поэтому `on_execute_tool_error`
  у него no-op, а весь эффект сделан до вызова инструмента.
- **[Risk] Патч `_execute_tool_call` — седьмой runtime-патч, и
  расширение patch-surface против design.** → **Mitigation:** патч
  ловит только собственный тип `RepeatGuardBlocked`, идемпотентен
  (`_repeat_guard_patched`), и при несовместимом API возвращает
  `(False, причина)` вместо тихой подмены. Контракт закрыт
  `tests/contract/test_repeat_guard_hook_contract.py`, который гоняет
  патч на настоящем `_execute_tool_call` и настоящем
  `execute_tool_calls`. Без патча режим `block` непригоден вовсе —
  см. Decision 1.
- **[Risk] Unbound-ное множество `published`.**
  → **Mitigation:** из него вычищаются fingerprint'ы, уже выпавшие из
  окна (в `after_run`); сверх того число отслеживаемых сессий
  ограничено потолком `_MAX_TRACKED_SESSIONS` (Decision 1b).
- **[Risk] Несовместимость с upstream-изменениями в
  `_execute_tool_call`.** → **Mitigation:** патч проверяет наличие
  функции, запоминает её сигнатуру и извлекает `hook`/`context`
  биндингом по этой сигнатуре — перестановка или добавление
  параметров upstream его не сломают. Контракт покрыт `tests/contract/`.
- **[Risk] Log-storm в `warn`/`block` режимах (отменён).**
  → **Mitigation:** ровно одно событие на момент crossing'а
  (см. Decision 5); множество `published` чистится от выпавших из
  окна fingerprint'ов.

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
