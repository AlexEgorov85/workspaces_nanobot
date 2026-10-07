# runtime/anti-loop Specification

## Purpose
Фиксирует нормативный контракт общего runtime-защитника от деградирующих
циклов одинаковых tool-вызовов внутри одного оборота агента: режимы
работы, фаза срабатывания, журналирование и границы ответственности
относительно upstream-механизмов `max_tool_iterations` и
`repeated_*_error`. Цель — дать проекту настраиваемый safety-net
для общего случая (не только web-fetch/web-search и workspace-bypass),
который сейчас зависает до исчерпания 200 итераций, и при этом
не сваливать оператора log-storm'ом при зациклившихся оборотах.

## Scope

`agent` — защитник от вырожденных циклов живёт в агенте
Реализация: `lib/hooks/repeat_guard_hook.py`, патч `repeat_guard_block`

## Requirements

### Requirement: Режимы работы `off`, `warn`, `block`

Система SHALL предоставлять три режима работы защитника, выбираемых
конфигурацией. В режиме `off` защитник SHALL NOT выполнять никаких
действий: ни сравнения, ни журналирования, ни прерывания вызовов.
В обоих активных режимах защитник SHALL ставить в очередь ровно одно
событие `tool.suppressed` через `DbLoggingService.try_log_event` в момент
превышения порога; режим различается полем `payload["mode"]`
(`"warn"` или `"block"`), а не именем события. Имя `tool.suppressed`
каноническое — `TOOL_SUPPRESSED` в
`mcp-platform/libs/enterprise_common/eventing/types.py:56`. В режиме
`warn` tool-вызов SHALL NOT прерываться. В режиме `block` защитник SHALL
заменять повторный вызов синтетическим tool-результатом с понятным
сообщением об ошибке. Последующие повторы
сверх порога (до сброса state) SHALL NOT генерировать новых событий —
ровно одно на момент crossing'а. Дефолт SHALL быть `off` (NO-OP),
чтобы существующие деплои без правок конфигурации работали как раньше.

#### Scenario: Режим `off` — никаких побочных эффектов

- **WHEN** конфигурация указывает режим `off` (или ключ отсутствует)
- **THEN** ни один tool-вызов не отличается по поведению от варианта
  без защитника: защитник SHALL пропускать вызовы без вычислений
- **AND THEN** в журнале нет записей от защитника

#### Scenario: `warn` enqueue ровно один раз на момент crossing'а

- **WHEN** в режиме `warn` состояние scокользящего буфера пересекает
  порог `max_repeats_in_window` для данной пары `(tool_name,
  canonical_args)`
- **THEN** ровно одно событие `tool.suppressed` с `payload["mode"]="warn"`
  SHALL быть enqueued через `DbLoggingService.try_log_event` ровно в этот момент
- **AND THEN** любые последующие вызовы той же пары до сброса state
  SHALL NOT порождать новых событий

#### Scenario: `block` подменяет вызов ошибкой ровно один раз

- **WHEN** в режиме `block` превышен порог
- **THEN** tool-вызов SHALL быть прерван до выполнения реального
  инструмента, модель получает результат, начинающийся с `Error:` и
  содержащий понятное сообщение (без раскрытия внутренних деталей)
- **AND THEN** ровно одно событие `tool.suppressed` с
  `payload["mode"]="block"` SHALL быть enqueued через
  `DbLoggingService.try_log_event` (на момент crossing'а; не на каждый
  последующий повтор)

### Requirement: Детектирование по нормализованному представлению аргументов

Система SHALL детектировать повтор по стабильному представлению,
которое вычисляется как пара `(tool_name, canonical(arguments))`.
`canonical(arguments)` SHALL приводить аргументы к каноническому
JSON-представлению через `json.dumps(arguments, sort_keys=True,
ensure_ascii=False, separators=(",", ":"), allow_nan=False,
default=_stable_fallback)`, где рекурсивная упорядоченность ключей
применяется на всех уровнях вложенности (включая dict внутри list).

`_stable_fallback` SHALL быть детерминированной строковой функцией
для несериализуемых типов (`Path`, `datetime`, `bytes`,
произвольные объекты): SHALL использоваться `str(value)` без адресов
памяти или `repr(value)` в зависимости от того, что даёт детерминизм
для конкретного типа; никаких nondeterministic значений (PIDs,
адреса объектов, таймстемпы без round-trip) в fingerprint попадать
НЕ ДОЛЖНО.

Защитник SHALL использовать точное равенство нормализованных
представлений для определения повторов внутри state'а (НЕ криптографические
хэши — они НЕ используются для детекции во избежание теоретических
коллизий). Опциональный 8-символьный hex (`blake2b` или аналог)
МОЖЕТ использоваться только как компактный идентификатор для записей
в журнале `agent_gateway_logs`.

#### Scenario: Перестановка ключей на любом уровне вложенности — одно представление

- **WHEN** модель вызывает инструмент сначала с
  `{"b": 2, "a": 1}`, затем с `{"a": 1, "b": 2}`, и затем с
  `{"query": {"b": 2, "a": 1}}` против `{"query": {"a": 1, "b": 2}}`
- **THEN** все эти вызовы дают идентичное нормализованное
  представление
- **AND THEN** при `max_repeats_in_window = 2` второй вызов SHALL
  считаться повтором

#### Scenario: Различие значений — разные представления

- **WHEN** модель вызывает инструмент сначала с `{"q": "alpha"}`,
  затем с `{"q": "beta"}`
- **THEN** нормализованные представления различны
- **AND THEN** ни один из вызовов не считается повтором

#### Scenario: Разные инструменты с одинаковыми аргументами — разные повторы

- **WHEN** модель вызывает `tool_a({"x": 1})` и затем
  `tool_b({"x": 1})`
- **THEN** нормализованные представления содержат разные `tool_name`
- **AND THEN** ни один из вызовов не считается повтором

#### Scenario: Несериализуемые значения — стабильный fallback без падения

- **WHEN** аргументы содержат `Path`, `datetime`, `bytes` или
  произвольный объект
- **THEN** защитник SHALL использовать детерминированное строковое
  представление (например, `str(value)`)
- **AND THEN** любые nondeterministic значения (PID, адрес объекта)
  SHALL NOT попадать в fingerprint — если их избежать нельзя,
  защитник SHALL пропустить такой вызов из проверки
- **AND THEN** `allow_nan=False` SHALL использоваться для отклонения
  NaN/Inf как недопустимых канонических значений

### Requirement: Глобальное скользящее окно последних вызовов

Система SHALL поддерживать в state'е один скользящий буфер последних
`window_size` tool-вызовов текущего оборота, без разделения по имени
инструмента. Буфер SHALL содержать записи
`(tool_name, canonical_args, iteration)` для ВСЕХ вызовов подряд,
вне зависимости от того, какие инструменты вызывались между ними.
Это — глобальная (cross-tool) семантика окна, а НЕ «consecutive»
(т.е. не требуется физическая непрерывность идентичных вызовов).

Защитник SHALL срабатывать, когда в этом глобальном буфере
присутствует `max_repeats_in_window` (по умолчанию `3`) записей
с одинаковой парой `(tool_name, canonical_args)`. Порог SHALL
считать **текущий** вызов включительно: с `max_repeats_in_window = 3`
срабатывание происходит на третьем идентичном вызове (первые два
проходят). Записи старше окна SHALL быть удалены из буфера
автоматически (например, через `collections.deque(maxlen=window_size)`).

#### Scenario: Перемежающиеся вызовы одного инструмента в общем окне

- **WHEN** `window_size = 5`, `max_repeats_in_window = 3`
- **AND** модель делает серию вызовов: `read(A)`, `exec(X)`,
  `read(A)`, `history(Y)`, `read(A)`
- **THEN** в скользящем буфере на момент 5-го вызова 3 записи с
  `tool_name="read"` и `canonical_args=canonical(A)`
- **AND THEN** защитник срабатывает на 5-м вызове (mode `block` →
  подмена ошибкой; mode `warn` → ровно одна запись в журнале)

#### Scenario: Старые записи выпадают из окна

- **WHEN** `window_size = 5`, и до окна было 7 одинаковых вызовов подряд,
  но последние 5 в буфере — все разные
- **THEN** срабатывания нет (повтор вне окна не учитывается)

#### Scenario: Счётчик включает текущий вызов

- **WHEN** `max_repeats_in_window = 3`
- **AND** модель вызывает один и тот же `(tool, canonical_args)` 1-й,
  2-й и 3-й раз в пределах окна
- **THEN** 1-й и 2-й вызовы SHALL пропускаться без срабатывания
- **AND THEN** 3-й вызов SHALL сработать (mode `block` → подмена
  ошибкой; mode `warn` → запись в журнал)

### Requirement: Изоляция state'а между оборотами по `session_key`

State защитника SHALL быть идентифицирован по `session_key`
(`nanobot.agent.hook.AgentHookContext.session_key`,
`nanobot/agent/hook.py:33`). Каждый оборот SHALL иметь собственный
независимый state (буфер + множества exempt), полностью сбрасываемый
при старте нового оборота до первого tool-вызова. Конкурентные
обороты с разными `session_key` SHALL NOT делить состояние.

#### Scenario: Параллельные обороты полностью изолированы

- **WHEN** два независимых оборота с разными `session_key` запущены
  конкурентно
- **THEN** повтор того же `(tool, canonical_args)` в обороте A SHALL
  NOT влиять на счётчик в обороте B
- **AND THEN** state защитника SHALL быть независимым для каждого
  `session_key`

#### Scenario: Повторное обращение в новом обороте

- **WHEN** оборот завершился штатно (или с `max_iterations`)
- **THEN** при следующем вызове в новом обороте state SHALL начинаться
  с пустого буфера для соответствующего `session_key`

### Requirement: Список инструментов-исключений

Система SHALL поддерживать конфигурируемый список `exempt_tools`
(по умолчанию `[]`) — имён инструментов, которые SHALL NEVER
проверяться защитником. Сопоставление SHALL выполняться точным
равенством имени инструмента (НЕ glob / НЕ regex).

Защитник SHALL NOT зашивать в дефолт имена каких-либо конкретных
инструментов проекта — оператор самостоятельно решает, какие
инструменты исключить. Документация (вне этого спека) может
приводить примеры таких инструментов (например, инструменты с
ручным вызовом пользователя или идемпотентным опросом состояния),
но в дефолте они НЕ появляются.

При наличии glob/regex метасимволов (`*`, `?`, `[`, `]`, якоря
`^`/`$`) в любой записи `exempt_tools` система SHALL выдать
`ConfigurationError` при инициализации настроек (fail-fast).

#### Scenario: exempt_tools обходит защитника

- **WHEN** инструмент с произвольным именем указан в `exempt_tools`
- **AND** модель вызывает его 10 раз подряд с одинаковыми аргументами
- **THEN** ни один вызов НЕ подменяется ошибкой и НЕ порождает
  записей в журнале

#### Scenario: Шаблоны имён отвергаются при инициализации

- **WHEN** `exempt_tools = ["read_*"]`
- **THEN** старт `ApplicationContext` SHALL упасть с
  `ConfigurationError`
- **AND THEN** сообщение SHALL содержать имя записи и проблемный
  метасимвол

#### Scenario: Подмножество совпадений по точному равенству

- **WHEN** `exempt_tools = ["my_tool"]`
- **AND** модель вызывает сначала `read_file` (не в exempt), затем
  `my_tool` (в exempt) с одинаковыми аргументами
- **THEN** вызовы `read_file` SHALL проверяться, а вызовы
  `my_tool` SHALL полностью пропускаться

### Requirement: Отказоустойчивость и отсутствие побочных эффектов при сбоях

Система SHALL работать fail-soft: любые исключения внутри самого
защитника (ошибки нормализации, ошибки enqueue в журнал,
недоступность `DbLoggingService`) SHALL быть проглочены и
залогированы на уровне WARNING через `loguru.logger`, но НЕ
подниматься наружу и НЕ прерывать выполнение tool-вызова.

Если `DbLoggingService` недоступен (`None`), защитник SHALL NOT
падать — enqueue SHALL быть пропущен, а хук продолжит работу
в режиме in-memory.

Сам факт срабатывания SHALL NOT зависеть от успешности
журналирования: детектор работает в `mode="block"` и подменяет
вызовы ошибкой независимо от того, удалось ли enqueue событие в БД.

#### Scenario: Сбой enqueue в журнал не валит вызов

- **WHEN** `DbLoggingService.try_log_event` бросает исключение
- **THEN** защитник логирует WARNING через `loguru.logger` и
  продолжает работу (вызов либо подменяется ошибкой, либо
  пропускается — в зависимости от режима)
- **AND THEN** исключение НЕ поднимается в вызывающий код

#### Scenario: Отсутствие DB-логирования не отключает детектор

- **WHEN** `db_logging_service = None` (например, в тестах)
- **THEN** защитник продолжает работу в режиме in-memory:
  детектирует повторы и (в режиме `block`) подменяет вызовы
  ошибкой; enqueue в журнал просто пропускается

### Requirement: Журналирование ровно один раз на момент crossing'а через `DbLoggingService.try_log_event`

Система SHALL публиковать срабатывания ТОЛЬКО через канонический
helper `DbLoggingService.try_log_event` (`lib/services/db_logging_service.py`).
Прямой INSERT в `agent_gateway_logs`, прямой SQL, собственные
event pipeline или модуль `event_log` (удалён) SHALL NOT использоваться.

Момент публикации: ровно в момент превышения порога. Последующие
повторы сверх порога до сброса state SHALL NOT генерировать новых
событий (это и есть защита от log-storm'а при зациклившемся агенте).

Поля `LogEvent` (контракт producer'а):
- `event_type` — `tool.suppressed` (один тип на оба активных режима);
  режим читается из `payload["mode"]`;
- `actor` = `"RepeatGuardHook"`;
- `name` = `"agent"`;
- `session_id` = `context.session_key` (как у всех других hooks);
- `channel`, `chat_id`, `agent_id` — из контекста если доступны;
- `summary` — человекочитаемое сообщение (например,
  `"repeat-guard: 3 identical read_file calls in last 5 iterations"`);
- `payload` — JSONB-совместимый dict с полями `tool`,
  `fingerprint_hash` (8-символьный hex для observability, не для
  детекции), `attempt`, `window_size`, `max_repeats_in_window`,
  `mode`.

Нормативный контракт SHALL описывать поведение **producer'а**
(enqueue + return code), а не eventual availability данных в
`agent_gateway_logs` — в конечном счёте событие появится там
через worker `DbLoggingService`, но это НЕ контрактная обязанность
защитника.

#### Scenario: Ровно одно событие на момент crossing'а

- **WHEN** в режиме `block` или `warn` состояние пересекает порог
- **AND** модель продолжает делать тот же вызов ещё 5 раз сверх порога
- **THEN** ровно ОДНО событие SHALL быть enqueued
  через `DbLoggingService.try_log_event` на момент превышения
- **AND THEN** 5 последующих повторов сверх порога до сброса state
  SHALL NOT генерировать дополнительных событий

#### Scenario: Публикация через try_log_event, не прямой INSERT

- **WHEN** срабатывание
- **THEN** защитник вызывает `DbLoggingService.try_log_event` с
  `producer="RepeatGuardHook"` и `event_type` из списка выше
- **AND THEN** НЕ происходит прямого INSERT/UPDATE в БД, прямого
  SQL, открытия собственного pipeline'а или `event_log.py` (удалён)

### Requirement: Независимость от upstream-механизмов завершения

Система SHALL сосуществовать с upstream `max_tool_iterations`
(`config.json:13` → `nanobot/agent/runner.py:435`) и
`repeated_external_lookup_error` /
`repeated_workspace_violation_error`
(`nanobot/utils/runtime.py:109,173`): они SHALL NOT быть
модифицированы и SHALL оставаться fallback'ом верхнего уровня.
Защитник SHALL сосуществовать с ними как дополнительный слой, не
как замена.

#### Scenario: Web-fetch срабатывает и у защитника, и у upstream

- **WHEN** модель вызывает `web_fetch` с тем же URL 5 раз подряд
- **THEN** сначала срабатывает upstream
  `repeated_external_lookup_error` (на 3-м вызове при
  `_MAX_REPEAT_EXTERNAL_LOOKUPS = 2`)
- **AND THEN** если включён защитник, дополнительно enqueueing
  событие `tool.suppressed` (с `payload["mode"]`) через
  `DbLoggingService.try_log_event` (взаимодополняющие сигналы)

#### Scenario: Достижение upstream `max_iterations` поверх защитника

- **WHEN** модель превышает upstream 200 итераций
- **THEN** штатное завершение `stop_reason = "max_iterations"`
  происходит НЕЗАВИСИМО от срабатываний защитника

### Requirement: Подключение через `AgentFactory`, регистрация в canonical inventory, наследование `AgentHook`

Защитник SHALL быть framework-хуком (`lib/hooks/`), подключаемым
через `AgentFactory.create` (`lib/core/agent_factory.py`) после
`ToolAuditHook`, чтобы `ToolAuditHook.before_execute_tools`
(который фиксирует стартовые записи вызовов) отработал раньше, чем
защитник принимает решение по каждому конкретному вызову в
`before_execute_tool`. Защитник SHALL наследовать
`nanobot.agent.hook.AgentHook` (`nanobot/agent/hook.py:66-152`),
чтобы `CompositeHook._for_each_hook_safe` корректно диспатчил
жизненный цикл без `AttributeError` — это контрактное требование
`openspec/specs/runtime/agent-hooks/spec.md`.

Защитник SHALL быть зарегистрирован в `canonical_framework_hooks()`
(`lib/services/runtime_inventory.py`) с `required = False`
(опциональный, отключаемый через `mode = off`), чтобы
`tools/diagnose_startup.py` отображал его в expected-vs-actual
без считания его отсутствия критичным drift'ом.

#### Scenario: Наследование AgentHook и порядок подключения

- **WHEN** защитник зарегистрирован в `ctx.hooks`
- **THEN** он SHALL наследовать `nanobot.agent.hook.AgentHook`
- **AND THEN** `ToolAuditHook.before_execute_tools` SHALL наблюдать
  tool-вызовы раньше, чем защитник их оценивает в
  `before_execute_tool` (порядок подключения гарантирует это)

#### Scenario: Режим `off` регистрируется, но не активен

- **WHEN** `gateway.repeat_guard.mode = off`
- **THEN** защитник присутствует в списке `ctx.hooks`
- **AND THEN** `canonical_framework_hooks()` отображает его как
  `required = False`
- **AND THEN** `tools/diagnose_startup.py` не считает его отсутствие
  критичным drift'ом

### Requirement: Механизм перехвата отказа в режиме `block`

*(Раздел добавлен при реализации: первоначальная редакция спеки и
`design.md` Decision 1 опирались на неверное допущение об устройстве
upstream. Ниже зафиксирован фактический, проверенный на живом коде
nanobot 0.3.5 механизм.)*

В режиме `block` защитник SHALL поднимать из `before_execute_tool`
собственное исключение `RepeatGuardBlocked`, а синтетический
tool-результат SHALL формировать перехватчик этого исключения,
установленный в `nanobot.agent.tools.execution._execute_tool_call`.

Причина в том, что штатный путь обработки недостижим для хуков:

* `HookRegistry._for_each_hook_safe` (`nanobot/agent/hook.py`)
  глотает исключение каждого хука при `AgentHook(reraise=False)`.
  Без `reraise=True` режим `block` был бы **молчаливым no-op**:
  состояние обновилось бы, а вызов всё равно выполнился;
* с `reraise=True` исключение пробрасывается, но
  `before_execute_tool` вызывается в `_execute_tool_call` **вне**
  `try`-блока, который ловит ошибки инструмента. Поэтому исключение
  уходит из `execute_tool_calls` наверх, а в параллельном режиме
  `asyncio.gather` без `return_exceptions=True` отменяет весь батч
  соседних вызовов;
* `ctx.tool_calls` для этого непригоден: раннер присваивает
  `context.tool_calls = list(response.tool_calls)`, то есть копию,
  и мутация из хука ничего не отменила бы.

Следствия, которые система SHALL обеспечивать:

* защитник SHALL быть создан с `reraise=True`;
* перехватчик SHALL ловить **только** `RepeatGuardBlocked` — любая
  другая ошибка хука SHALL оставаться видимой и SHALL NOT маскироваться
  под отказ защитника;
* перехватчик SHALL вызвать `on_execute_tool_error` с исходным
  `context`, `tool_call`, `tool` и `params`, чтобы остальные хуки
  увидели отказ так же, как увидели бы ошибку инструмента;
* перехватчик SHALL вернуть результат того же вида, что и штатная
  ошибка инструмента: строку, начинающуюся с `Error:`, и event-словарь
  со `status = "error"`;
* аргументы `hook` и `context` SHALL извлекаться по сигнатуре
  оригинала, а не из `kwargs`: upstream вызывает `_execute_tool_call`
  **позиционно**, и обращение к `kwargs["hook"]` всегда дало бы
  `None`;
* если перехватчик не применился (API изменился), режимы `off` и
  `warn` SHALL продолжать работать, а `block` SHALL деградировать до
  обрыва оборота — это громче отказа, но не тише.

#### Scenario: Повтор отклонён, соседние вызовы батча не отменены

- **WHEN** в режиме `block` в одном батче `concurrent` идут вызов
  с повторяющимися аргументами и вызов с другими аргументами
- **THEN** повтор SHALL получить синтетический результат с
  `status = "error"`
- **AND THEN** соседний вызов SHALL быть выполнен нормально
- **AND THEN** исключение SHALL NOT выйти из `execute_tool_calls`

#### Scenario: Чужая ошибка хука не маскируется

- **WHEN** другой хук бросает исключение в `before_execute_tool`
- **THEN** оно SHALL пройти через `_execute_tool_call` без
  подмены на синтетический результат защитника

### Requirement: Наблюдаемость для оператора через `agent_gateway_logs`

Система SHALL предоставлять оператору возможность находить все
срабатывания защитника через `event_type = 'tool.suppressed'` в
`agent_gateway_logs` (после того, как
worker `DbLoggingService` обработает очередь — eventual
consistency, не строгий момент публикации). Прямого stdout/stderr
вывода для срабатываний SHALL NOT быть; диагностика — через
существующий `tools/diagnose_startup.py` и SQL-запросы к
`agent_gateway_logs`.

#### Scenario: Поиск срабатываний через event_type

- **WHEN** оператор выполняет
  `SELECT * FROM agent_gateway_logs WHERE event_type = 'tool.suppressed' ORDER BY created_at DESC LIMIT 50`
- **THEN** возвращаются записи срабатываний с указанием инструмента,
  режима, attempt и параметров окна

## Responsibility

Детект вырожденного цикла «модель зовёт один и тот же инструмент с одними и
теми же аргументами» внутри одного оборота и реакция по режиму. Владелец
поведения — `RepeatGuardHook` (`lib/hooks/repeat_guard_hook.py:178`);
владелец способа отказать в вызове — патч
`RuntimePatcher.patch_repeat_guard_block` (`lib/services/runtime_patcher.py:1211`),
потому что hook-API upstream не умеет «мягко» отклонить вызов.

## Boundary

- **Внутри:** окно последних вызовов оборота, канонизация аргументов,
  подсчёт повторов, одно событие на crossing, режимы `warn`/`block`.
- **Снаружи:** лимиты итераций upstream (`max_tool_iterations`,
  `repeated_*_error`) — детектор их не заменяет и о них не знает; сбор и
  подключение хука — `runtime/agent-hooks`; подстановка синтетического
  результата — патч в `lib/services/runtime_patcher.py`.

## Public Contract

- `RepeatGuardHook(settings, db_logging_service=None)` — общий инстанс на
  все обороты; состояние изолировано по `session_key`
  (`lib/hooks/repeat_guard_hook.py:185-210`). `settings=None` трактуется как
  `mode="off"`; `db_logging_service=None` отключает журнал, но не детект
  (`:196`).
- `RepeatGuardHook(reraise=True)` — обязательный режим: без него
  `HookRegistry._for_each_hook_safe` проглотит отказ и режим `block`
  станет no-op, который при этом выглядит работающим (`:198-201`).
- `RepeatGuardBlocked(RuntimeError)` (`:154`) — единственный тип, который
  перехватывает патч; несёт `context`, `tool_call`, `tool`, `params`.
- Lifecycle-точки: `before_iteration` (сброс), `before_execute_tool`
  (детект), `on_execute_tool_error` (no-op), `after_run` (чистка
  fingerprints).

## Inputs

- `GatewayRepeatGuardSettings` — `mode`, `window_size`,
  `max_repeats_in_window`, `exempt_tools`
  (`lib/core/project_settings.py:253-256`), прочитанные в конструкторе
  (`lib/hooks/repeat_guard_hook.py:202-208`).
- Контекст вызова: `session_key` и `iteration` (`_bucket_key` `:215-217`,
  `before_iteration` `:315`).
- Вызов: `tool_call.name` и `tool_call.arguments`; при `arguments is None`
  берётся `params` (`:333-335`).
- `_MAX_TRACKED_SESSIONS = 512` — потолок одновременно отслеживаемых
  сессий (`:72`).

## Outputs

- Ровно одно событие на crossing с `event_type="tool.suppressed"`
  (`:384`), `level="WARN"` (`:278`), `actor="RepeatGuardHook"`, и payload
  `{tool, fingerprint_hash, attempt, window_size, max_repeats_in_window, mode}`
  (`:283-290`).
- Строка `repeat-guard: N identical <tool> calls in last <W> iterations…`
  в loguru уровнем WARNING (`:246-251`, `:389`).
- В режиме `block` — `RepeatGuardBlocked` вместо выполнения вызова
  (`:391-398`), перехватываемый патчем.

## State

`self._state: dict[session_key, bucket]` (`lib/hooks/repeat_guard_hook.py:210`),
где bucket — `deque(maxlen=window_size)` последних пар `(tool_name, canonical)`,
множество `published` fingerprint'ов и отметка `touched`
(`:222-226`). Состояние персистентно между вызовами и обслуживает
конкурентные обороты изолированно; потолок — 512 сессий с вытеснением по
времени последнего касания (`:232-244`). Сброс окна — в
`before_iteration` при `iteration == 0` (`:317`), то есть в начале нового
оборота, а не нового `run`.

## Dependencies

- `nanobot.agent.AgentHook` — базовый класс (`lib/hooks/repeat_guard_hook.py:59`);
- `lib.services.db_logging_service.try_log_event` + `LogEvent` — журнал,
  импортируется лениво внутри `_publish` (`:270`);
- `lib/services/runtime_patcher.py:1211` (`patch_repeat_guard_block`) — приём отказа;
- `lib/core/agent_factory.py` — подключение инстанса (`:174-182`);
- stdlib: `json`, `hashlib`, `deque`, `math`, `PurePath`.

## Configuration

Блок `gateway.repeat_guard` (`lib/core/project_settings.py:223-256`):

- `mode`: `off` | `warn` | `block`, дефолт `off` (`:253`);
- `window_size`: `ge=1, le=1000`, дефолт `20` (`:254`) — потолок памяти
  на сессию;
- `max_repeats_in_window`: `ge=2, le=100`, дефолт `3` (`:255`) — срабатывание
  на N-м идентичном вызове, текущий считается;
- `exempt_tools`: список имён; сопоставление точным равенством, шаблоны
  запрещены валидатором (`:258-280`).

В `config.json` блок по умолчанию не объявлен — деплой без правок ведёт себя
как раньше.

## Lifecycle

1. Инстанс создаётся `AgentFactory` всегда, включая `mode="off"`
   (`lib/core/agent_factory.py:174-182`), чтобы канонический список
   `runtime_inventory` совпадал с фактом.
2. На каждом обороте первая итерация (`iteration == 0`) сбрасывает bucket
   сессии (`lib/hooks/repeat_guard_hook.py:305-317`).
3. Каждый `before_execute_tool` дописывает пару в окно и считает вхождения
   (`:350-355`).
4. `after_run` вычищает из `published` fingerprints, выпавшие из окна
   (`:411-421`) — состояние сессии при этом не удаляется, ключа в этом
   контексте нет.

## Data Ownership

Детектор не владеет ничем долговременным: его буфер — оперативное состояние
одного инстанса, освобождаемое вытеснением. Аргументы tool-вызовов не
копируются в хранилище — в журнал уходит только 8-символьный
`fingerprint_hash` (`:141-151`, `:285`).

## Error Behavior

- Отказ записи в журнал глотается с WARNING и не влияет на детект: контракт
  producer'а — «enqueue + результат», а не гарантия появления в БД
  (`lib/hooks/repeat_guard_hook.py:263-267`, `:295-301`).
- Аргументы, которые не удалось привести к детерминированному виду
  (`_SKIP`), вызов пропускают с DEBUG-записью, а не считают «не
  повтором» (`:337-345`).
- `db_logging_service=None` — штатный режим: детект работает, записи нет
  (`:196`).
- Патч `patch_repeat_guard_block` при изменившемся API upstream возвращает
  `(False, причина)` и не применяется: режимы `off`/`warn` продолжают
  работать, `block` деградирует до обрыва оборота — это громче отказа, но
  не тише (`lib/services/runtime_patcher.py:1231-1235`).

## Invariants

- `mode == "off"` → выход до любых вычислений: ни сравнения, ни журнала
  (`lib/hooks/repeat_guard_hook.py:313`, `:327`).
- Один fingerprint публикует не более одного события за окно; повторный
  `block` всё равно бросает исключение, но молча (`:360-371`).
- Счётчик `count` уже включает текущий вызов, поэтому порог срабатывает
  ровно на N-м совпадении — лишний `+1` сдвинул бы его на
  `max_repeats - 1` (`:352-355`).
- `after_run` не удаляет bucket сессии: `AgentRunHookContext` не несёт
  `session_key`, адресная чистка там физически невозможна (`:411-418`).
- Патч ловит только `RepeatGuardBlocked`; любая другая ошибка хука остаётся
  видимой и не маскируется под отказ защитника
  (`lib/services/runtime_patcher.py:1228-1229`).

## Forbidden Behavior

- Строить инстанс с `reraise=False` — блокировка станет молчаливым no-op.
- Сбрасывать состояние в `before_run`: глобальный сброс стёр бы буфер
  параллельных оборотов (`lib/hooks/repeat_guard_hook.py:37-43`).
- Сравнивать аргументы как есть, без `sort_keys`: `{"b":2,"a":1}` и
  `{"a":1,"b":2}` — это один вызов, а не два (`:119-122`).
- Считать недетерминированный вызов «не повтором» — это спрятало бы
  настоящий цикл (`:338-339`).
- Публиковать новое событие на каждый последующий повтор сверх порога —
  зациклившийся агент зальёт журнал (`:361-362`).
- Ловить в патче исключение шире `RepeatGuardBlocked` (`:1228`).
- Поддерживать glob/regex в `exempt_tools` — сопоставление только точное,
  шаблоны отвергаются на старте (`lib/core/project_settings.py:261-279`).

## Consumers

- `lib/core/agent_factory.py:174-182` — создание и подключение.
- `lib/services/runtime_patcher.py:1211` — приём отказа; патч заявлен в
  `_PATCH_SPECS` как `repeat_guard_block`
  (`lib/services/runtime_patcher.py:315`).
- `lib/services/runtime_inventory.py:85-86` — `RepeatGuardHook` в
  каноническом списке фреймворковых хуков.
- Оператор — по событию `tool.suppressed` и строке WARNING в журнале.

## Implementation

Существующие на диске пути:

- `lib/hooks/repeat_guard_hook.py` — хук, канонизация, детект;
- `lib/services/runtime_patcher.py` — приём отказа
  (`patch_repeat_guard_block`);
- `lib/core/agent_factory.py` — подключение инстанса;
- `lib/core/project_settings.py` — `GatewayRepeatGuardSettings`;
- `lib/services/db_logging_service.py` — `try_log_event`, `LogEvent`;
- `lib/services/runtime_inventory.py` — канонический список хуков.

## Verification

- `tests/test_repeat_guard_hook.py` — режимы, окно, канонизация,
  исключение на блокировку;
- `tests/test_runtime_patcher.py` — наложение патча `repeat_guard_block`,
  синтетический результат при отказе;
- `tests/test_runtime_inventory.py` — наличие хука в каноническом списке.
