## Purpose

Фиксирует нормативный контракт общего runtime-защитника от деградирующих
циклов одинаковых tool-вызовов внутри одного оборота агента: режимы
работы, фаза срабатывания, журналирование и границы ответственности
относительно upstream-механизмов `max_tool_iterations` и
`repeated_*_error`. Цель — дать проекту настраиваемый safety-net
для общего случая (не только web-fetch/web-search и workspace-bypass),
который сейчас зависает до исчерпания 200 итераций, и при этом
не сваливать оператора log-storm'ом при зациклившихся оборотах.

## ADDED Requirements

### Requirement: Режимы работы `off`, `warn`, `block`

Система SHALL предоставлять три режима работы защитника, выбираемых
конфигурацией. В режиме `off` защитник SHALL NOT выполнять никаких
действий: ни сравнения, ни журналирования, ни прерывания вызовов.
В режиме `warn` защитник SHALL enqueueing ровно одно событие
`tool_repeat_warned` через `DbLoggingService.try_log_event` в момент
превышения порога и НЕ прерывать tool-вызов. В режиме `block`
защитник SHALL заменять повторный вызов синтетическим tool-результатом
с понятным сообщением об ошибке и enqueueing ровно одно событие
`tool_repeat_blocked` через тот же producer'а. Последующие повторы
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
- **THEN** ровно одно событие `tool_repeat_warned` SHALL быть
  enqueued через `DbLoggingService.try_log_event` ровно в этот момент
- **AND THEN** любые последующие вызовы той же пары до сброса state
  SHALL NOT порождать новых событий

#### Scenario: `block` подменяет вызов ошибкой ровно один раз

- **WHEN** в режиме `block` превышен порог
- **THEN** tool-вызов SHALL быть прерван до выполнения реального
  инструмента, модель получает результат, начинающийся с `Error:` и
  содержащий понятное сообщение (без раскрытия внутренних деталей)
- **AND THEN** ровно одно событие `tool_repeat_blocked` SHALL быть
  enqueued через `DbLoggingService.try_log_event` (на момент
  crossing'а; не на каждый последующий повтор)

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
коллизий). Опциональный 16-char hex-digest (`blake2b` или аналог)
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
- `event_type` — `tool_repeat_blocked` или `tool_repeat_warned`;
- `actor` = `"RepeatGuardHook"`;
- `name` = `"agent"`;
- `session_id` = `context.session_key` (как у всех других hooks);
- `channel`, `chat_id`, `agent_id` — из контекста если доступны;
- `summary` — человекочитаемое сообщение (например,
  `"repeat-guard: 3 identical read_file calls in last 5 iterations"`);
- `payload` — JSONB-совместимый dict с полями `tool`,
  `fingerprint_hash` (16-char hex-truncation для observability, не для
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
  событие `tool_repeat_blocked` / `tool_repeat_warned` через
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

### Requirement: Наблюдаемость для оператора через `agent_gateway_logs`

Система SHALL предоставлять оператору возможность находить все
срабатывания защитника через `event_type IN ('tool_repeat_blocked',
'tool_repeat_warned')` в `agent_gateway_logs` (после того, как
worker `DbLoggingService` обработает очередь — eventual
consistency, не строгий момент публикации). Прямого stdout/stderr
вывода для срабатываний SHALL NOT быть; диагностика — через
существующий `tools/diagnose_startup.py` и SQL-запросы к
`agent_gateway_logs`.

#### Scenario: Поиск срабатываний через event_type

- **WHEN** оператор выполняет
  `SELECT * FROM agent_gateway_logs WHERE event_type IN ('tool_repeat_blocked', 'tool_repeat_warned') ORDER BY created_at DESC LIMIT 50`
- **THEN** возвращаются записи срабатываний с указанием инструмента,
  режима, attempt и параметров окна
