## Scope

Операции дельт, нацеленные на требования, уже лежащие в каноне, приведены
к тексту канона: пять требований под `MODIFIED` взяты из
`openspec/specs/runtime/entrypoints/spec.md` целиком, вместе со сценариями.
Пять требований, объявленных `REMOVED`, из дельты сняты: они живут в
каноне, а работа change'а не сделана — архив удалил бы живое требование, в
том числе описывающее `role`, который код реализует
(`lib/core/application_context.py:186`, `:427`). Пять требований под
`ADDED` в каноне отсутствуют, поэтому оставлены как работа change'а — их
имена перечислены ниже, в самой дельте. Причина — работа change'а не
сделана; незавершённое остаётся в `tasks.md`.

## ADDED Requirements

### Requirement: ApplicationContext.create без параметра role

`ApplicationContext.create(...)` MUST иметь typed signature **без** параметра
способа запуска:

```python
def create(
    script_dir: Path,
    workspace_dir: Path,
    *,
    storage_override: str | None = None,
    session_override: str | None = None,
    **kwargs,  # deprecated: enable_db_logging, enable_audit, print_llm_calls
) -> ApplicationContext: ...
```

`role` MUST NOT присутствовать ни как именованный параметр, ни как поле
`ApplicationContext`. Передача `role=` MUST приводить к `TypeError`.
`profile` MUST быть разрешён ДО `create()` через
`config._initialize_settings(profile=...)` и MUST NOT проходить через
`**kwargs`.

`ApplicationContext.create()` MUST вызываться **только** `gateway.py`.
`cli_agent.py` MUST NOT вызывать `create()` вовсе: CLI не создаёт `AgentLoop`,
`MessageBus`, patch'и, session manager и cache runtime.

`storage_override` MUST быть удалён вместе с ролью клиента: локальный выбор
storage mode невозможен, когда хранилищем владеет Gateway. `session_override`
MAY сохраняться как идентификатор сессии на стороне клиента.

#### Scenario: у create() нет параметра role

- **WHEN** проверяется `inspect.signature(ApplicationContext.create)`
- **THEN** `role` MUST NOT присутствовать в `parameters`
- **AND** у экземпляра `ApplicationContext` MUST NOT быть поля `role`

#### Scenario: передача role приводит к TypeError

- **WHEN** код вызывает `ApplicationContext.create(..., role="cli")`
- **THEN** MUST быть поднят `TypeError`
- **AND** `ApplicationContext` MUST NOT быть создан

#### Scenario: только Gateway вызывает composition root

- **WHEN** выполняется инвентаризация вызовов `ApplicationContext.create(...)`
  в production-файлах верхнего уровня
- **THEN** вызов MUST присутствовать только в `gateway.py`
- **AND** `cli_agent.py` MUST NOT содержать вызова `create()`

#### Scenario: profile по-прежнему разрешается до create()

- **WHEN** запускается `gateway.py`
- **THEN** он MUST вызвать `config._initialize_settings(profile=...)` ДО
  `ApplicationContext.create(...)`
- **AND** `create(...)` MUST NOT принимать `profile`

### Requirement: composition принадлежит Gateway

`ApplicationContext` MUST быть единственным composition root Agent Runtime и
MUST создаваться только в процессе Gateway. Перечень создаваемых
инфраструктурных компонентов MUST определяться конфигурацией
(`gateway.*`, `channels.*`) и фактом запуска Gateway, а НЕ параметром
способа запуска.

| Компонент | Кто создаёт | Где |
|---|---|---|
| `AgentLoop` (+ hooks, runtime patches, skills, tools, memory) | Gateway | `ApplicationContext` |
| `DbLoggingService` | Gateway | `ApplicationContext` |
| `SessionManager` / `PGSessionManager` | Gateway | `ApplicationContext` |
| `RuntimeEventsSubscriber` | Gateway | `ApplicationContext` |
| `CacheProvider` / `CacheOwnershipCoordinator` | Gateway | `ApplicationContext` |
| `CronService` | Gateway | `ApplicationContext` |
| `PostgresChannel` / `WebSocketChannel` | Gateway | `ChannelFactory` (`gateway.py`), **вне** `ApplicationContext` |
| WebSocket port availability check | Gateway | `gateway.py`, **вне** `ApplicationContext` |
| CLI-редактор ввода-вывода | CLI-процесс | `lib/cli/console_loop.py` |

Проектирование каналов и pre-startup-проверок портов MUST оставаться в
`gateway.py`/`ChannelFactory`. `ApplicationContext` MUST NOT создавать
каналы.

#### Scenario: каналы создаются вне composition root

- **WHEN** `gateway.py` поднимает runtime
- **THEN** `PostgresChannel` и `WebSocketChannel` MUST создаваться
  `ChannelFactory` через `ChannelManager`
- **AND** `ApplicationContext` MUST NOT создавать каналы

#### Scenario: CLI не создаёт ни одного runtime-компонента

- **WHEN** запускается `cli_agent.py`
- **THEN** процесс MUST NOT создавать `AgentLoop`, `MessageBus`, session
  manager, `DbLoggingService`, `CacheProvider` и `CronService`

### Requirement: CLI-клиент принимает параметры подключения

CLI entrypoint MUST принимать параметры подключения к Gateway, а не
параметры локального runtime:

- `--session` / `-s` — идентификатор сессии (`chat_id`) на Gateway; сохраняется;
- `--gateway` / `-g` — адрес Gateway; переопределяет `cli.gateway_url`.

CLI MUST NOT принимать `--storage` (выбор локального `storage_mode`) и
`--patched` (vanilla/patched режим AgentLoop): локального runtime у клиента
нет, поэтому оба флага не имеют адресата. Их передача MUST приводить к
`ConfigurationError` и exit 2 — молчаливое игнорирование запрещено.

Адрес Gateway по умолчанию MUST вычисляться из `channels.websocket.host` и
`channels.websocket.port` — тех же ключей, по которым Gateway проверяет
занятость порта. Отдельный порт или хост для клиента MUST NOT вводиться.

#### Scenario: --session задаёт chat_id

- **WHEN** пользователь запускает `python cli_agent.py --session=42`
- **THEN** клиент MUST использовать `chat_id` этой сессии в исходящих
  сообщениях к Gateway

#### Scenario: --gateway переопределяет конфиг

- **WHEN** пользователь запускает `python cli_agent.py --gateway=ws://host:9000`
- **THEN** клиент MUST подключаться к указанному адресу
- **AND** значение `cli.gateway_url` из конфигурации MUST быть переопределено
  только на время этого запуска

#### Scenario: --storage отклоняется

- **WHEN** пользователь запускает `python cli_agent.py --storage=file`
- **THEN** CLI MUST отклонить флаг с `ConfigurationError`
- **AND** процесс MUST завершиться с кодом 2
- **AND** подключение к Gateway MUST NOT выполняться

#### Scenario: адрес по умолчанию совпадает с портом Gateway

- **WHEN** `cli.gateway_url` не задан
- **THEN** клиент MUST использовать `ws://<channels.websocket.host>:<channels.websocket.port>`

### Requirement: Slash-команда /compact в CLI делегируется Gateway

CLI MUST отправлять `/compact` как обычное сообщение (`content="/compact"`) и
MUST NOT вызывать `ContextCompactionService.compact(...)` локально.

У CLI после перехода в клиентскую модель нет ни `ApplicationContext`, ни
сервиса сжатия, ни доступа к `agent_conversation_messages`. Локальное
исполнение означало бы сжатие сессии вторым процессом — ровно тот класс
гонок, который устранён в архивных `2026-10-02-drop-local-cache-read-from-pg`
и `2026-10-02-fix-cache-process-boundary`.

Gateway MUST обработать команду в своём command router
(`nanobot/agent/loop.py:1342` — команды обрабатываются для любого канала, кроме
`"system"`), включая пропатченный `cmd_compact`
(`RuntimePatcher.patch_compact_command`). Событие `context_compacted` MUST
быть записано Gateway в `agent_gateway_logs`; клиент получает уведомление
в потоке ответа.

#### Scenario: /compact обрабатывается на Gateway

- **WHEN** пользователь в CLI вводит `/compact`
- **THEN** CLI MUST отправить сообщение с `content="/compact"` в Gateway
- **AND** CLI MUST NOT вызывать `ContextCompactionService` локально
- **AND** обработка команды и запись события `context_compacted` MUST
  выполняться в процессе Gateway

#### Scenario: CLI не содержит локального compaction-вызова

- **WHEN** выполняется поиск `ContextCompactionService` в `cli_agent.py` и
  `lib/cli/console_loop.py`
- **THEN** совпадений MUST NOT быть

### Requirement: WebSocket port check принадлежит Gateway, а тот же порт — адрес клиента

`gateway._check_websocket_port_available()` MUST оставаться в `gateway.py`.
CLI MUST NOT выполнять её: проверка занятости порта — ответственность
сервера.

При этом порт WebSocket-транспорта (`channels.websocket.host`/`port`) является
**адресом клиента** по умолчанию. Проверка (server-side) и подключение
(client-side) MUST ссылаться на один и тот же источник конфигурации, чтобы
клиент не подключался к порту, который Gateway проверяет как «свой другой».

#### Scenario: Gateway проверяет занятость WebSocket-порта

- **WHEN** запускается `gateway.py` и порт из `channels.websocket` занят
- **THEN** gateway MUST завершиться с ненулевым кодом до подъёма runtime

#### Scenario: клиент использует тот же порт

- **WHEN** клиент подключается без `--gateway`
- **THEN** он MUST использовать порт из `channels.websocket.port`
- **AND** CLI MUST NOT выполнять pre-startup проверку занятости этого порта

## MODIFIED Requirements

### Requirement: AgentLoop MUST быть transport-agnostic

`AgentLoop` MUST NOT знать о CLI, PostgreSQL, HTTP, WebSocket, Telegram, terminal. `AgentLoop` взаимодействует только с in-memory `MessageBus` через `bus.publish_inbound(InboundMessage(...))` и `bus.publish_outbound(OutboundMessage(...))`.

#### Scenario: AgentLoop получает одно и то же сообщение независимо от источника

- **WHEN** пользователь вводит сообщение в CLI REPL
- **THEN** CLI вызывает `bus.publish_inbound(InboundMessage(channel="cli", chat_id=<chat_id>, content=...))`, где `chat_id` — имя сессии из `--session` либо `"direct"`, а ключ сессии получается как `f"{channel}:{chat_id}"` (`lib/cli/console_loop.py:241-243,395-403`) — AgentLoop обрабатывает через bus
- **WHEN** HTTP-запрос приходит в gateway через `PostgresChannel`
- **THEN** `PostgresChannel` вызывает `bus.publish_inbound(InboundMessage(channel="postgres", chat_id=..., content=...))` — тот же AgentLoop обрабатывает через bus
- **AND** AgentLoop MUST вести себя идентично в обоих случаях

### Requirement: Cron = gateway-only

`CronService` MUST создаваться ТОЛЬКО при `role="gateway"` (если `gateway.enable_cron=True`). CLI MUST NOT создавать `CronService`. Если одновременно работают CLI и gateway, cron fires ТОЛЬКО из gateway — нет дублирования `jobs.json`. Это BREAKING для пользователей, у которых сейчас cron работал в CLI.

#### Scenario: Cron в gateway

- **WHEN** `gateway.py` запущен с `gateway.enable_cron=True`
- **THEN** `CronService` MUST быть подключен к `AgentLoop`
- **AND** scheduled jobs MUST выполняться при наступлении cron-тайминга

#### Scenario: Cron НЕ в CLI

- **WHEN** `cli_agent.py` запущен
- **THEN** `CronService` MUST NOT создаваться
- **AND** scheduled jobs MUST NOT выполняться из CLI-процесса

### Requirement: CLI имеет фиксированный профиль test

`cli_agent.py` MUST NOT принимать `--profile` CLI-аргумент и MUST NOT читать профиль из env. CLI MUST использовать фиксированный профиль `test` при вызове `config._initialize_settings(profile="test")` (`cli_agent.py:117-119`, `CLI_FIXED_PROFILE = "test"`). Отклоняются `--profile`, `-profile` и `-p` (`CLI_REJECTED_FLAGS`, `cli_agent.py`), каждая передача — `ConfigurationError`. Gateway MUST принимать `--profile` из whitelist'а `("prod", "test")` (`gateway.py:_SUPPORTED_PROFILES`) и MUST отклонять иной профиль.

"test" в контексте CLI НЕ означает урезанный runtime: CLI MUST иметь тот же AgentLoop, Skills, Tools, Memory, Logging, Prompts, Runtime patches, что и gateway. Различие только в profile (CLI == "test" fixed) и transport (CLI == in-memory bus).

#### Scenario: CLI не принимает --profile

- **WHEN** пользователь запускает `python cli_agent.py --profile=test`
- **THEN** CLI MUST отклонить флаг с `ConfigurationError` и завершиться с кодом 2 (`cli_agent.py:458-462`)
- **AND** процесс MUST NOT запускать `ApplicationContext`

#### Scenario: CLI hardcodes profile="test"

- **WHEN** пользователь запускает `python cli_agent.py` (без `--profile`)
- **THEN** CLI MUST вызвать `config._initialize_settings(profile="test")` (fixed)
- **AND** `SETTINGS["profile"]` MUST быть `"test"`
- **AND** процесс MUST NOT требовать `--profile` для старта

#### Scenario: CLI не читает профиль из env

- **WHEN** `cli_agent.py` запускается в окружении, содержащем
  произвольные переменные, чьи имена или значения выглядят как выбор
  профиля
- **THEN** CLI MUST игнорировать эти переменные
- **AND** `SETTINGS["profile"]` MUST быть `"test"`
- **AND** этот сценарий ограничивает ТОЛЬКО выбор профиля; секреты и
  `${VAR}` substitution MAY legitimately читать environment

#### Scenario: Gateway сохраняет --profile механизм

- **WHEN** пользователь запускает `python gateway.py --profile=prod` или `--profile=test`
- **THEN** gateway MUST принять `--profile`
- **AND** `SETTINGS["profile"]` MUST соответствовать переданному значению

#### Scenario: Профиль вне whitelist'а отклоняется

- **WHEN** пользователь запускает `python gateway.py --profile=staging`
- **THEN** gateway MUST поднять `ConfigurationError` и завершиться с кодом 2 (`gateway.py::_parse_args`)

### Requirement: Production entrypoints MUST NOT передавать profile в composition root

Production application entrypoints MUST NOT передавать `profile` в
`ApplicationContext.create()`. Профиль определяется entrypoint'ом и
публикуется через `_initialize_settings(profile=...)` до вызова
`create()`; после этого профиль доступен исключительно через
`SETTINGS["profile"]`.

#### Scenario: Entrypoints не передают profile

- **WHEN** проверяются production-файлы `gateway.py` и `cli_agent.py`
- **THEN** вызовы `ApplicationContext.create()` в них MUST NOT содержать
  аргумент `profile`

#### Scenario: Профиль CLI определён до composition

- **WHEN** `cli_agent.py` стартует
- **THEN** он MUST вызвать `_initialize_settings` с фиксированным
  профилем `test` ДО вызова `ApplicationContext.create()` (`cli_agent.py:119`)
- **AND** вызов `ApplicationContext.create()` SHALL NOT содержать `profile`

### Requirement: Deprecated kwargs с явной compatibility boundary

Deprecated kwargs являются временной compatibility boundary. Они MUST приниматься только через `**kwargs` до выполнения отдельного change `remove-deprecated-enable-kwargs`. До этого change production code MUST NOT использовать эти kwargs. После применения `remove-deprecated-enable-kwargs`:

- `enable_db_logging`
- `enable_audit`
- `enable_cron`
- `print_llm_calls`

MUST NOT приниматься `ApplicationContext.create()`; их передача MUST приводить к `TypeError`.

Перечень deprecated kwargs SHALL состоять ровно из этих четырёх
параметров. `profile` MUST NOT входить в этот перечень: у него нет
migration path в `config.json`, и он не является deprecated API.

`DEPRECATED_ENABLE_KWARGS` (`lib/core/application_context.py::DEPRECATED_ENABLE_KWARGS`) MUST оставаться
allowlist'ом, а не «мягкой» обработкой: любой ключ вне перечня MUST отвергаться
`TypeError`. Это делает опечатку (`enable_aduit=`) явной ошибкой, а не молчаливым
игнорированием.

#### Scenario: Deprecated kwargs через **kwargs продолжают работать

- **WHEN** существующий тест вызывает `ApplicationContext.create(..., enable_audit=False)` через `**kwargs`
- **THEN** система MUST использовать переданное значение `enable_audit=False`, игнорируя конфиг `gateway.enable_audit`
- **AND** система MUST логировать `DeprecationWarning` с указанием на новый путь конфигурации
  (`lib/core/application_context.py::_resolve_enable_kwargs`)

#### Scenario: После remove-deprecated-enable-kwargs — TypeError на deprecated kwargs

- **WHEN** change `remove-deprecated-enable-kwargs` реализован
- **AND** код вызывает `ApplicationContext.create(..., enable_audit=False)` через `**kwargs`
- **THEN** MUST быть поднят `TypeError`

#### Scenario: Ключ вне allowlist'а — TypeError, а не молчание

- **WHEN** код вызывает `ApplicationContext.create(..., enable_aduit=False)`
- **THEN** MUST быть поднят `TypeError`, называющий принятые имена
  (`lib/core/application_context.py::_resolve_enable_kwargs`)

#### Scenario: profile не входит в перечень deprecated kwargs

- **WHEN** проверяется перечень deprecated compatibility kwargs
- **THEN** `profile` MUST NOT входить в него
- **AND** профиль MUST NOT обрабатываться через `**kwargs` с
  `DeprecationWarning`
