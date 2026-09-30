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
гонок, который устранён в `cache-architecture-alignment` и
`fix-cache-process-boundary`.

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

`AgentLoop` MUST взаимодействовать с внешним миром **только** через
in-memory `MessageBus`: `bus.publish_inbound(InboundMessage(...))` для
входящих сообщений и `bus.publish_outbound(OutboundMessage(...))` для
исходящих. Никаких прямых обращений к каналам, транспортам, файловым
дескрипторам или CLI-объектам.

Формулировка MUST оставаться проверяемой. В upstream `nanobot` присутствуют
ветвления по литеральным именам каналов внутри `AgentLoop`
(`nanobot/agent/loop.py:928,1297,1331,1342,1814,1822,2340` — строки, `"cli"`,
`"system"`, `"websocket"`). Это перечень известных исключений, а не
разрешённая зависимость:

- проект MUST NOT добавлять новых ветвлений по именам каналов;
- проект MUST NOT добавлять транспортные зависимости в `AgentLoop`;
- проект MUST NOT патчить `AgentLoop` для добавления транспортной логики —
  транспортные решения принимаются в channel- и client-слоях.

Смысл требования — инвариант «runtime-ядро не знает, откуда пришло сообщение
и куда уйдёт ответ», а не запрет на существующий upstream-код.

#### Scenario: AgentLoop получает одно и то же сообщение независимо от источника

- **WHEN** пользователь вводит сообщение в CLI REPL
- **THEN** CLI отправляет его в Gateway по wire-протоколу, а Gateway вызывает `bus.publish_inbound(InboundMessage(channel="websocket", chat_id=..., content=...))` — AgentLoop обрабатывает через bus
- **WHEN** сообщение приходит в Gateway через `PostgresChannel`
- **THEN** `PostgresChannel` вызывает `bus.publish_inbound(InboundMessage(channel="postgres", chat_id=..., content=...))` — тот же AgentLoop обрабатывает через bus
- **AND** AgentLoop MUST вести себя идентично в обоих случаях

#### Scenario: проект не расширяет список исключений

- **WHEN** выполняется поиск литералов имён каналов (`"cli"`, `"system"`, `"websocket"`, `"postgres"`, ...) в патчах проекта
- **THEN** совпадений в `RuntimePatcher` MUST NOT быть
- **AND** новая транспортная логика MUST добавляться в channel/client-слой

### Requirement: Cron = gateway-only

`CronService` MUST создаваться ТОЛЬКО процессом Gateway, по конфигурации
Gateway, и MUST подниматься вместе с ним. CLI MUST NOT создавать
`CronService`.

Cron MUST NOT быть переключателем composition. Флаг способа запуска
(`enable_cron` в роли entrypoint-аргумента) MUST быть удалён: он не должен
определять наличие `CronService` и MUST NOT определять `return_file_manager`
(`SessionStorageService.create` — сессионное решение, а не cron-решение).

Если одновременно работают CLI и Gateway, cron fires ТОЛЬКО из Gateway — нет
дублирования `jobs.json`. Это BREAKING для пользователей, у которых сейчас
cron работал в CLI.

#### Scenario: Cron в gateway

- **WHEN** запускается `gateway.py` с `gateway.enable_cron=True`
- **THEN** `CronService` MUST быть создан и подключён к `AgentLoop`
- **AND** scheduled jobs MUST выполняться при наступлении cron-тайминга

#### Scenario: Cron НЕ в CLI

- **WHEN** запускается `cli_agent.py`
- **THEN** `CronService` MUST NOT создаваться
- **AND** scheduled jobs MUST NOT выполняться из CLI-процесса

#### Scenario: cron-флаг не управляет storage

- **WHEN** создаётся `SessionStorageService`
- **THEN** выбор file-storage MUST определяться режимом хранилища
- **AND** значение `enable_cron` MUST NOT участвовать в этом выборе

### Requirement: CLI имеет фиксированный профиль test

`cli_agent.py` MUST NOT принимать `--profile` CLI-аргумент и MUST NOT читать
профиль из env. CLI MUST использовать фиксированный профиль `test` при вызове
`config._initialize_settings(profile="test")`. Gateway MAY принимать
`--profile`.

Профиль `test` у CLI определяет **только локальное разрешение конфигурации
клиента** (адрес Gateway, флаги подключения, отображение настроек). Он НЕ
означает, что CLI имеет собственный Agent Runtime: Agent Runtime, skills,
tools, vector search, memory, logging, prompts и runtime patches принадлежат
Gateway и доступны CLI через wire-протокол.

#### Scenario: CLI не принимает --profile

- **WHEN** пользователь запускает `python cli_agent.py --profile=test`
- **THEN** CLI MUST отклонить флаг с `ConfigurationError` и завершиться с кодом 2
- **AND** процесс MUST NOT подключаться к Gateway

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

#### Scenario: профиль CLI не создаёт локальный runtime

- **WHEN** CLI разрешил конфигурацию с профилем `test`
- **THEN** он MUST NOT создавать `AgentLoop` или `ApplicationContext`
- **AND** профиль MUST влиять только на локальное разрешение конфигурации клиента

### Requirement: Production entrypoints MUST NOT передавать profile в composition root

Production application entrypoints MUST NOT передавать `profile` в
`ApplicationContext.create()`. Профиль определяется entrypoint'ом и
публикуется через `_initialize_settings(profile=...)` до вызова `create()`;
после этого профиль доступен исключительно через `SETTINGS["profile"]`.

CLI после перехода в клиентскую модель вообще не вызывает `create()`: его
`_initialize_settings(profile="test")` разрешает конфигурацию клиента, а не
состав runtime.

#### Scenario: Entrypoints не передают profile

- **WHEN** проверяются production-файлы `gateway.py` и `cli_agent.py`
- **THEN** вызовы `ApplicationContext.create()` в них MUST NOT содержать
  аргумент `profile`

#### Scenario: Профиль CLI определён до composition

- **WHEN** `cli_agent.py` стартует
- **THEN** он MUST вызвать `_initialize_settings` с фиксированным
  профилем `test` ДО любого обращения к `SETTINGS`
- **AND** вызовов `ApplicationContext.create()` в `cli_agent.py` SHALL NOT быть

### Requirement: Deprecated kwargs с явной compatibility boundary

Deprecated kwargs являются временной compatibility boundary. Они MUST
приниматься только через `**kwargs` до выполнения отдельного change
`remove-deprecated-enable-kwargs`. До этого change production code MUST NOT
использовать эти kwargs. После применения `remove-deprecated-enable-kwargs`:

- `enable_db_logging`
- `enable_audit`
- `print_llm_calls`

MUST NOT приниматься `ApplicationContext.create()`; их передача MUST
приводить к `TypeError`.

`enable_cron` УДАЛЁН из перечня: cron перестаёт быть параметром composition
(см. требование «Cron = gateway-only»), поэтому принимать его через `**kwargs`
значило бы принимать молчаливо игнорируемый аргумент. Передача
`enable_cron` MUST приводить к `TypeError` сразу после этого change.

`profile` MUST NOT входить в перечень: у него нет migration path в
`project.json`, и он не является deprecated API.

#### Scenario: Deprecated kwargs через **kwargs продолжают работать

- **WHEN** существующий тест вызывает `ApplicationContext.create(..., enable_audit=False)` через `**kwargs`
- **THEN** система MUST использовать переданное значение `enable_audit=False`, игнорируя конфиг `gateway.enable_audit`
- **AND** система MUST логировать `DeprecationWarning` с указанием на новый путь конфигурации

#### Scenario: После remove-deprecated-enable-kwargs — TypeError на deprecated kwargs

- **WHEN** change `remove-deprecated-enable-kwargs` реализован
- **AND** код вызывает `ApplicationContext.create(..., enable_audit=False)` через `**kwargs`
- **THEN** MUST быть поднят `TypeError`

#### Scenario: profile не входит в перечень deprecated kwargs

- **WHEN** проверяется перечень deprecated compatibility kwargs
- **THEN** `profile` MUST NOT входить в него
- **AND** профиль MUST NOT обрабатываться через `**kwargs` с
  `DeprecationWarning`

#### Scenario: enable_cron не принимается composition-ом

- **WHEN** код вызывает `ApplicationContext.create(..., enable_cron=True)`
- **THEN** MUST быть поднят `TypeError`
- **AND** значение MUST NOT интерпретироваться как «создать `CronService`»

## REMOVED Requirements

### Requirement: Единая typed signature ApplicationContext.create с role

**Reason**: Требование нормализовало `role` как обязательный элемент
composition API («CLI и gateway вызывают `create()` с одной и той же
сигнатурой; различие только в `role`»). После того как CLI перестаёт быть
владельцем Agent Runtime, у параметра остаётся одно допустимое значение и
ноль вызывающих из CLI. Требование закрепляло ровно ту модель, которую
change устраняет, и служило источником побочных эффектов
(`return_file_manager=not ctx.enable_cron`), при которых флаг cron
определял наличие session storage.

**Migration**: Контракт переносится в требование «`ApplicationContext.create`
без параметра `role`». Параметр `role` удаляется из сигнатуры и из полей
`ApplicationContext`; его чтение в cron-гейте
(`lib/core/application_context.py:385`) удаляется. Проверка «profile
разрешён ДО create()» и границы deprecated kwargs сохраняются без изменений.

### Requirement: role определяет composition инфраструктуры, не AgentLoop

**Reason**: Таблица «сервис × `role`» кодировала композицию как функцию от
параметра способа запуска и была частично неверна: `PostgresChannel`
создаётся `ChannelFactory._add_postgres`
(`lib/services/channel_factory.py:129-191`) из `gateway.py:249-258`, то есть
вне `ApplicationContext`. Кроме того, таблица утверждала, что cron — сервис,
определяемый `role` и `enable_cron`, что и породило связь cron → storage.

**Migration**: Контракт переносится в требование «composition принадлежит
Gateway»: перечень создаваемых компонентов фиксирован и разделён по
исполнителю (`ApplicationContext` — runtime-сервисы, `ChannelFactory` /
`gateway.py` — каналы и pre-startup проверки). Cron определяется
конфигурацией Gateway и не является параметром composition.

### Requirement: WebSocket port check остаётся server-only

**Reason**: Требование утверждало, что порт WebSocket интересует только
сервер. После перехода CLI в клиентскую модель тот же порт становится
адресом подключения клиента, и требование в этой формулировке запрещало бы
указать клиенту на порт, который Gateway проверяет как свой.

**Migration**: Контракт переносится в требование «WebSocket port check
принадлежит Gateway, а тот же порт — адрес клиента»: проверка остаётся
server-side в `gateway.py`, а источник адреса клиента и источник проверки —
один и тот же (`channels.websocket.host`/`port`). Второй порт для клиента не
вводится.

### Requirement: CLI-специфичные runtime-параметры

**Reason**: Требование закрепляло `--storage` (выбор локального
`storage_mode`) и `--session` как «runtime-флаги CLI». После перехода CLI не
владеет локальным хранилищем, поэтому `--storage` адресата не имеет:
`storage_override` передавался в `ApplicationContext.create()`, который CLI
больше не вызывает.

**Migration**: Контракт переносится в требование «CLI-клиент принимает
параметры подключения»: `--session` сохраняется как идентификатор сессии на
Gateway, `--storage` и `--patched` удаляются с `ConfigurationError` + exit 2,
добавляется `--gateway`.

### Requirement: Slash-команда /compact в CLI остаётся локальной

**Reason**: Требование предписывало локальный вызов
`ContextCompactionService.compact(...)` из CLI-процесса. После перехода CLI
не имеет ни `ApplicationContext`, ни сервиса сжатия, ни подключения к
`agent_conversation_messages`; локальное исполнение означало бы запись в
сессию вторым процессом.

**Migration**: Контракт переносится в требование «Slash-команда `/compact` в
CLI делегируется Gateway». Команда отправляется как `content="/compact"`,
разбирается command router'ом Gateway (включая
`RuntimePatcher.patch_compact_command`), событие `context_compacted` пишется
Gateway в `agent_gateway_logs`. Локальный вызов из `console_loop.py` и
миграция cron-хранилища из `cli_agent.py` удаляются.
