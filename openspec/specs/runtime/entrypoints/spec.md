# runtime/entrypoints Specification

## Purpose
Определяет контракт application entrypoint'ов (`cli_agent.py`, `gateway.py`) поверх единого composition root (`lib/core/application_context.py::ApplicationContext`). Оба entrypoint'а вызывают `ApplicationContext.create(role=...)` с одной и той же typed signature; различие только в `role` и опциональных CLI-runtime-флагах. AgentLoop остаётся transport-agnostic: transport (CLI = in-memory bus / gateway = `PostgresChannel`) живёт ниже AgentLoop, не в нём.

Спекой НЕ описывается снятая подсистема. Локальный кэш агента (снимок `cache.duckdb`), его загрузчик, реестр ресурсов, векторный индекс в агенте и слой владения ими удалены; владельцем снимка и всех ресурсов стала capability `data`/`vectors` платформы. Ни одно требование этой спеки не ссылается на них — см. § «Снятые требования» в конце файла, где зафиксировано, чем каждое из них заменено.

## Scope
`agent` — точки входа и lifecycle агента
Реализация: `gateway.py`, `cli_agent.py`, `lib/lifecycle/`

## Requirements

### Requirement: Единая typed signature ApplicationContext.create с role

`ApplicationContext.create(...)` MUST иметь typed signature (`lib/core/application_context.py::ApplicationContext.create`):

```python
def create(
    script_dir: Path,
    workspace_dir: Path,
    *,
    role: Literal["gateway", "cli"],
    storage_override: str | None = None,
    session_override: str | None = None,
    **kwargs,  # deprecated: enable_db_logging, enable_audit, enable_cron, print_llm_calls
) -> ApplicationContext: ...
```

`profile` MUST NOT присутствовать как параметр в `def create(..., *, ...):` — профиль MUST быть разрешён ДО `create()` через `config._initialize_settings(profile=...)`. `profile` MUST NOT также проходить через `**kwargs`: его передача SHALL приводить к `TypeError`.

Параметры `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` MUST NOT присутствовать как именованные параметры в typed signature. Они MAY приниматься ТОЛЬКО через `**kwargs` для backward compat с существующими вызовами.

CLI и gateway MUST вызывать `ApplicationContext.create(...)` с **одной и той же сигнатурой**; различие только в `role` и runtime-флагах (`storage_override`, `session_override` — только из CLI, `cli_agent.py:189-194`; gateway передаёт только `role`/`script_dir`/`workspace_dir`, `gateway.py::_entrypoint_main`).

#### Scenario: CLI и gateway используют одну typed signature

- **WHEN** `cli_agent.py` и `gateway.py` инициализируют runtime
- **THEN** оба entrypoint'а вызывают `ApplicationContext.create(script_dir=..., workspace_dir=..., role="cli" | "gateway", ...)`
- **AND** оба НЕ передают `profile` как параметр
- **AND** оба не передают `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` как именованные параметры
- **AND** поведение каждого сервиса определяется только конфигом `gateway.*` + `role`

#### Scenario: Profile resolved ДО ApplicationContext.create()

- **WHEN** `cli_agent.py` или `gateway.py` стартует
- **THEN** он MUST вызвать `config._initialize_settings(profile=...)` ДО `ApplicationContext.create(...)`
  (`gateway.py::_parse_args`, `cli_agent.py:119`)
- **AND** `ApplicationContext.create(...)` MUST NOT принимать `profile` как параметр
- **AND** `ApplicationContext.create(..., profile=...)` MUST приводить к `TypeError`

#### Scenario: Defaults для enable_* берутся из конфига

- **WHEN** в `config.json` отсутствуют ключи `gateway.enable_db_logging`, `gateway.enable_audit`, `gateway.enable_cron`, `gateway.print_llm_calls`
- **THEN** `ApplicationContext.create()` MUST использовать значения: `enable_db_logging=True`, `enable_audit=True`, `enable_cron=False`, `print_llm_calls=False`
  (дефолты в `lib/core/application_context.py::_resolve_enable_kwargs`)

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

### Requirement: ApplicationContext.create MUST NOT принимать profile

`ApplicationContext.create()` MUST NOT принимать `profile` ни как
именованный параметр, ни через `**kwargs`. Профиль MUST быть разрешён
ДО вызова `create()` через lifecycle-gate `_initialize_settings(profile=...)`,
после чего `ApplicationContext` читает его только из
`SETTINGS["profile"]`.

`profile` MUST NOT входить в перечень deprecated compatibility kwargs.
Deprecated compatibility boundary (`DEPRECATED_ENABLE_KWARGS`) предназначена
исключительно для `enable_db_logging`, `enable_audit`, `enable_cron`,
`print_llm_calls` — параметров с определённым migration path в
`config.json` (`gateway.*`). У `profile` такого migration path нет: он не
является deprecated API, а уже не является API `ApplicationContext` вовсе.

`ApplicationContext` MUST NOT знать, каким способом был выбран профиль.
Единственный канал получения профиля — `SETTINGS["profile"]`.

#### Scenario: Передача profile в create() приводит к TypeError

- **WHEN** код вызывает `ApplicationContext.create(..., profile="test")`
- **THEN** вызов SHALL привести к `TypeError`
- **AND** `ApplicationContext` SHALL NOT быть создан

#### Scenario: profile отсутствует в сигнатуре create()

- **WHEN** проверяется `inspect.signature(ApplicationContext.create)`
- **THEN** `profile` MUST NOT присутствовать в `parameters`
- **AND** `profile` MUST NOT входить в перечень deprecated kwargs

#### Scenario: ApplicationContext читает профиль из SETTINGS

- **WHEN** `ApplicationContext.create()` выполняется после успешной
  `_initialize_settings(profile=...)`
- **THEN** активный профиль SHALL быть прочитан из `SETTINGS["profile"]`
  (`lib/core/application_context.py::ApplicationContext.create`)
- **AND** значение SHALL совпадать с профилем, переданным в
  `_initialize_settings`

#### Scenario: ApplicationContext не влияет на выбор профиля

- **WHEN** `ApplicationContext.create()` вызывается
- **THEN** он MUST NOT изменять, пересобирать или переключать
  разрешённый профиль
- **AND** он MUST NOT выполнять повторное разрешение профиля

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

### Requirement: role определяет composition инфраструктуры, не AgentLoop

`role` MUST определять, какие инфраструктурные сервисы создаются внутри
`ApplicationContext.create()` и `ApplicationContext.start()`. `role` MUST NOT
представлять environment, profile, deployment mode, storage ownership или
runtime behavior.

| Сервис | Где создаётся | `role="gateway"` | `role="cli"` |
|---|---|---|---|
| `AgentLoop` (hooks, runtime patches, skills, tools, memory) | `lib/core/application_context.py::ApplicationContext.create` | ✅ | ✅ |
| `DbLoggingService` | `lib/core/application_context.py::ApplicationContext.create` / `::start` | ✅ | ✅ |
| `SessionManager` (поверх `SanitizingSessionStore`) | `lib/core/application_context.py::ApplicationContext.create` | ✅ | ✅ |
| `RuntimeEventsSubscriber` | `lib/core/application_context.py::ApplicationContext.start` | ✅ | ✅ |
| `RuntimePatcher.apply_all()` | `lib/core/application_context.py::ApplicationContext.create` (до сборки агента) | ✅ | ✅ |
| `CronService` (если `gateway.enable_cron=True`) | `lib/core/application_context.py::_make_cron_service` | ✅ | ❌ |
| `PostgresChannel` | `lib/services/channel_factory.py::ChannelFactory._add_postgres`, из `gateway._run` | ✅ | ❌ |
| WebSocket port check (вызывается из entrypoint) | `gateway.py::_entrypoint_main` | ✅ | ❌ |
| Console I/O (in-memory bus) | `lib/cli/console_loop.py` | ❌ | ✅ |

`role` MUST NOT влиять на `AgentLoop` (и его hooks, runtime patches, skills,
tools, memory). `role` MAY влиять только на transport-инфраструктуру и
entrypoint-specific services.

**Границы таблицы, важные для её чтения.** `PostgresChannel` создаётся НЕ
`ApplicationContext`, а `ChannelFactory.create_all()` внутри `gateway._run`
(`gateway.py::_run`), и дополнительно требует `channels.postgres.enabled=True`
и непустого DSN (`lib/services/channel_factory.py::ChannelFactory._add_postgres`) —
то есть решается конфигом, а не только `role`. `RuntimeEventsSubscriber`
создаётся в `start()`, а не в `create()`. `DbLoggingService` требует
`logging.db.enabled=True` и DSN
(`lib/core/application_context.py::_make_db_logging`) поверх `enable_db_logging`.

Снятые строки прежней версии этой таблицы (кэш-кластер) удалены вместе с ним;
перечень и замены — в § «Снятые требования».

#### Scenario: role="gateway" создаёт PostgresChannel и CronService

- **WHEN** `gateway.py` вызывает `ApplicationContext.create(role="gateway", ...)`
- **THEN** `CronService` MUST быть создан если `gateway.enable_cron=True`
  (`lib/core/application_context.py::_make_cron_service`, условие в `::create`)
- **AND** `PostgresChannel` MUST быть создан `ChannelFactory.create_all()`
  в `gateway._run`, если `channels.postgres.enabled=True` и задан DSN
  (`gateway.py::_run`, `lib/services/channel_factory.py::ChannelFactory._add_postgres`)
- **AND** gateway-specific check `WebSocket port availability` MUST быть вызван
  ДО входа в `GatewayRunner.run_forever(...)` (`gateway.py::_entrypoint_main`)

#### Scenario: role="cli" НЕ создаёт PostgresChannel и CronService

- **WHEN** `cli_agent.py` вызывает `ApplicationContext.create(role="cli", ...)`
- **THEN** `ApplicationContext` MUST NOT создавать `CronService` (cron = gateway-only)
  (`lib/core/application_context.py::ApplicationContext.create` — условие требует `role == "gateway"`)
- **AND** CLI MUST NOT создавать `PostgresChannel`: фабрика каналов в CLI не
  вызывается вовсе
- **AND** CLI REPL MUST публиковать сообщения через `bus.publish_inbound(InboundMessage(channel="cli", ...))` напрямую
  (`lib/cli/console_loop.py:395-403`)
- **AND** CLI REPL MUST читать outbound через `bus.consume_outbound()`
  (`lib/cli/console_loop.py:266-270`)

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

### Requirement: WebSocket port check остаётся server-only

`gateway._check_websocket_port_available()` MUST оставаться в `gateway.py`.
CLI MUST NOT выполнять её.

**Уточнение границы объекта.** «Server-only» относится к **порту канала
WebSocket** — к тому, что биндит сам gateway. Проверка занятости порта
**платформы** — другой объект и другое требование: её CLI выполняет, потому
что CLI поднимает собственный процесс платформы. Смешивать эти две проверки
запрещено: у них разный объект, разное место вызова и разный смысл отказа.

Предпосылка «CLI поднимает платформу» держится **до** `unify-runtime-channels`
(D13): после него её не станет, и это требование SHALL быть снято тем change,
а не остаться требованием к коду, которого нет.

Функция MUST читать хост и порт из `ctx.config.channels.websocket`, а при
отсутствии конфигурации использовать дефолты `127.0.0.1` / `8765`
(`gateway.py::_check_websocket_port_available`). Проверка MUST выполняться до входа в
`GatewayRunner.run_forever(...)`: после неё перезапуск уже невозможен, потому
что подъём цикла биндит порт.

#### Scenario: Gateway проверяет занятость WebSocket-порта

- **WHEN** запускается `gateway.py` и порт занят
- **THEN** gateway MUST завершиться с кодом `1` (`SystemExit(1)`, `gateway.py::_check_websocket_port_available`)
- **AND** вывод MUST содержать PID процесса-владельца и подсказку по освобождению
  (`gateway.py::_check_websocket_port_available`)
- **AND** отказ MUST произойти ДО входа в `GatewayRunner.run_forever(...)`
  (`gateway.py::_entrypoint_main`)

#### Scenario: Конфигурация WebSocket-порта читается, а не зашита

- **WHEN** в `ctx.config.channels.websocket` заданы `host` и `port`
- **THEN** проверка MUST использовать именно их, а не дефолтные
  `127.0.0.1:8765` (`gateway.py::_check_websocket_port_available`)

#### Scenario: Проверки портов не смешиваются

- **WHEN** запускается CLI при транспорте `http` и порт платформы занят
- **THEN** CLI MUST проверить порт платформы и MUST NOT выполнять
  `_check_websocket_port_available()`
- **AND** gateway MUST выполнить обе проверки, если обе применимы
  (проверяется `tests/test_gateway_enterprise_mcp_startup.py::TestCliOrdering::test_cli_checks_platform_port_not_websocket_port`)

> Пометка D13. Сценарий держится на предпосылке «CLI поднимает платформу» и
> живёт **до** `unify-runtime-channels`: после него этой предпосылки не станет,
> и сценарий SHALL быть снят тем change, а не остаться проверкой кода, который
> тогда уже не вызывается.

### Requirement: CLI-специфичные runtime-параметры

CLI entrypoint MUST принимать runtime-флаги: `--storage`/`-S` (выбор
`storage_mode`: `auto`/`file`/`postgres`, `cli_agent.py:66-67`) и `--session`/`-s`
(имя сессии для `chat_id`, `cli_agent.py:68`).

CLI также принимает `--patched`/`-P` (холодное зеркало сессий через
`background_task_factory`) и `--smoke` (печать баннера и runtime-таблицы без
подъёма REPL). Оба флага MUST NOT менять порядок старта: рукопожатие и
`attach_log_transport()` MUST выполняться одинаково в обеих ветвях
(`cli_agent.py:152-178` — обе ветви зовут общий `_run_cli_repl`).

#### Scenario: --storage=file в CLI

- **WHEN** пользователь запускает `cli_agent.py --storage=file`
- **THEN** `storage_override="file"` MUST передаваться в `ApplicationContext.create(storage_override="file", ...)`
  (`cli_agent.py:193`)
- **AND** `SessionStorageService` MUST использовать file-storage
  (`lib/core/application_context.py::ApplicationContext.create`)

#### Scenario: Ветви --patched и обычная держат один порядок старта

- **WHEN** запускается CLI в любой из ветвей
- **THEN** обе MUST идти через общий `_run_cli_repl` с одинаковым порядком:
  рукопожатие → `attach_log_transport()` → REPL (`cli_agent.py:163-178,229-242`)

### Requirement: Slash-команда /compact в CLI остаётся локальной

CLI MUST обрабатывать `/compact` как локальный shortcut: вызов `ContextCompactionService.compact(...)` напрямую, минуя шину (`lib/cli/console_loop.py:371-372,132-150`).

CLI MUST передавать `force=True`; признак `idle` SHALL определяться самим текстом
команды (`/compact idle`, `/compact --idle`, `/compact -i`) и по умолчанию быть
`False` (`lib/cli/console_loop.py:141-145`).

**Граница записи в журнал.** CLI конструирует `ContextCompactionService` без
`db_logging_service` и без `settings` (`lib/cli/console_loop.py:143`), поэтому на
CLI-пути событие `agent.compacted` в `agent_gateway_logs` НЕ пишется:
`try_log_event(None, ...)` возвращает `False` и печатает WARNING
(`lib/services/context_compaction.py::ContextCompactionService._record_event_log`,
`lib/services/db_logging_service.py::try_log_event`). Заметка в
`agent_conversation_messages` на CLI-пути тоже не пишется: `_write_history_notice`
возвращается рано для любого префикса, кроме `postgres`
(`lib/services/context_compaction.py::ContextCompactionService._write_history_notice`). Это осознанная граница CLI-пути,
а не поведение, которое следует «исправить» в спеке: tool `compact_context`
(`workspace/tools/compact_context.py:136-139`) передаёт `db_logging_service`, и
запись там работает.

#### Scenario: /compact сжимает сессию локально и без шины

- **WHEN** пользователь в CLI вводит `/compact`
- **THEN** CLI MUST вызвать `ContextCompactionService.compact(session_key=f"{cli_channel}:{chat_id}", idle=<из текста команды>, force=True)` напрямую (`lib/cli/console_loop.py:141-145`)
- **AND** `force` MUST быть `True` независимо от наличия `idle` во вводе

#### Scenario: Запись в журнал на CLI-пути — известная граница, а не требование

- **WHEN** CLI выполняет `/compact`
- **THEN** событие `agent.compacted` в `agent_gateway_logs` SHALL NOT появиться:
  клиент журнала на этом пути не передан, и `try_log_event` возвращает `False`
  с WARNING
- **AND** заметка в `agent_conversation_messages` SHALL NOT появиться: префикс
  ключа сессии не `postgres`
- **WHEN** то же сжатие выполняет tool `compact_context`
- **THEN** `agent_gateway_logs` MUST получить `event_type="agent.compacted"`
  (`lib/services/context_compaction.py::ContextCompactionService._record_event_log`)

### Requirement: Запрет Streamlit в runtime-коде

`gateway.py`, `cli_agent.py` и весь runtime-код MUST NOT импортировать, спавнить или каким-либо образом инициализировать `streamlit_app` или `streamlit` модуль.

`streamlit_app.py` и `lib/services/subprocess_manager.py` уже удалены — на диске их
нет. Имена `SubprocessManager` и `spawn_streamlit` упоминаются здесь только как
предмет запрета: в `lib/` и `workspace/tools/` упоминаний `streamlit` нет.
Требование защищает достигнутую цель от возврата, а не ставит задачу.

#### Scenario: runtime-код не импортирует streamlit

- **WHEN** выполняется `grep -r "import streamlit\|from streamlit" lib/ workspace/tools/`
- **THEN** НЕ ДОЛЖНО быть результатов в runtime-коде

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

### Requirement: Невозможность поднятия обязательной зависимости обнаруживается на старте

Процесс `enterprise-mcp` — обязательная зависимость агента, а не опциональный
сервис: со стороны платформы он владеет пулом PostgreSQL и даёт модели входы к
данным. Поэтому `enterprise-mcp` MUST быть опрошен **синхронно и блокирующе на
старте**, ДО того как агент примет хоть один оборот. Отказ подъёма MUST NOT
откладываться до момента, когда зависимость понадобится: «платформа лежит»
обнаруживается на старте, а не посреди оборота пользователя.

Операция рукопожатия — `list_operations()`: она поднимает сессию и делает
discovery. Второго, отдельного механизма рукопожатия SHALL NOT появляться: если
сервер поднялся, но нужные операции не отдал, подниматься больше нечего, и такой
отказ MUST приводить к тому же отказу запуска, а не проходить дальше.

Рукопожатие MUST выполняться внутри живого event loop, которому сессия
принадлежит. Синхронный подъём ДО `asyncio.run` SHALL NOT использоваться:
loop там ещё не существует.

**Уточнение про транспорт.** Ранее требование называло сессию stdio и
утверждало, что она обязана быть поднята внутри живого event loop, к которому
привязана через stdin. С переходом на `streamable-http` (change
`2026-10-04-enterprise-mcp-http-transport`) **основание меняется, а требование —
нет**: при `http` сессия больше не порождает процесс, но подъём по-прежнему
на том же loop, который затем владеет сессией и её переподключением, и подъём до
`asyncio.run` создал бы сессию без владельца. Требование сформулировано о
владении loop'ом, а не о транспорте, поэтому следующая смена транспорта не
потребует его правки.

Ленивое создание сессии внутри клиента MAY оставаться, но ONLY как путь
восстановления оборвавшейся сессии — как нормальный путь подъёма оно SHALL NOT
использоваться. Пункт транспортонезависим. На практике этот путь берут три
вызывающих: `list_operations()`, `call()` и проба наблюдателя
(`lib/services/enterprise_mcp_client.py::_probe_once`); последняя лечит и меряет,
и нормальным путём подъёма не является.

#### Scenario: Платформа не отвечает — отказ на старте, а не на первом вопросе

- **WHEN** поднимается любой из двух entrypoint'ов, а `enterprise-mcp` не отвечает
- **THEN** рукопожатие MUST дать отказ ДО того, как агент станет способен принять
  оборот (каналы в gateway, REPL в CLI)
- **AND** подъём MUST быть блокирующим: продолжение старта без результата
  рукопожатия SHALL NOT происходить
- **AND** отказ MUST NOT откладываться до первого вызова tool'а, которому
  платформа нужна (проверяется поведенчески:
  `tests/test_gateway_enterprise_mcp_startup.py::TestRuntimeOrdering::test_handshake_precedes_channels_start_and_agent`
  и `::TestCliOrdering::test_handshake_runs_before_repl_and_transport`)

#### Scenario: Рукопожатие выполняется внутри живого event loop

- **WHEN** проверяется место вызова рукопожатия в обоих entrypoint'ах
- **THEN** вызов MUST находиться внутри корутины, исполняемой `asyncio.run(...)`
  (`gateway.py::_run` вызывается как `lambda: asyncio.run(_run(ctx))`, `gateway.py::_entrypoint_main`;
  CLI — `asyncio.run(body())`, `cli_agent.py:246`)
- **AND** подъём сессии ДО `asyncio.run` SHALL NOT использоваться

#### Scenario: Сервер поднялся, но операций не отдал

- **WHEN** `list_operations()` завершается пустым набором операций
- **THEN** это SHALL считаться тем же отказу запуска, что и неотвечающий сервер
- **AND** старт SHALL NOT продолжаться «на всякий случай»

### Requirement: Отказ подъёма — это отказ запуска, а не тихая деградация

Отказ рукопожатия MUST NOT деградировать в частичную работу. Ни каналы, ни
агент, ни REPL SHALL NOT стартовать после отказа, а сам отказ MUST NOT быть
проглочен. Тихая деградация здесь была бы единственно неправильным вариантом:
задачи начали бы забираться из очереди, а tool'ы отвечали бы ошибкой — и
«платформа лежит» выглядело бы снаружи как «агент работает».

Правильный порядок сам по себе этого не гарантирует: порядок соблюдён, а
исключение проглочено — и деградация наступила. Отказ MUST доходить до
владельца процесса (`GatewayRunner` в gateway — он логирует и перезапускает с
backoff; `main()` в CLI — он печатает в `stderr` и возвращает ненулевой код).

#### Scenario: Каналы не стартуют после отказа

- **WHEN** `_connect_enterprise_mcp(ctx)` в `gateway._run` падает
- **THEN** `channels.start_all()` SHALL NOT быть вызван
- **AND** `ctx.agent.run()` SHALL NOT быть вызван
- **AND** исключение SHALL дойти до вызывающего кода
  (проверяется `TestHandshakeFailureRefusesStartup::test_channels_never_start_when_handshake_fails`)

#### Scenario: REPL не поднимается после отказа

- **WHEN** рукопожатие в CLI падает
- **THEN** REPL SHALL NOT подняться
- **AND** `ctx.attach_log_transport()` SHALL NOT быть вызван
  (проверяется `TestCliOrdering::test_repl_does_not_start_when_handshake_fails`)

### Requirement: Причина отказа фиксируется ДО отказа наружу

Причина MUST быть названа словами **до** того, как исключение уйдёт наружу. На
старте MUST быть выведена читаемая строка вердикта, называющая упавшую
зависимость и саму причину, и отдельная строка подсказки — что именно
проверять. Для CLI причина MUST дополнительно попасть в журнал запуска
(`logger.error`): строка в консоли не заменяет запись в журнал, из которого
инцидент потом и разбирают.

**Уточнение формулировки.** Раньше это обосновывалось так: «`GatewayRunner`
сообщает лишь `Gateway exited unexpectedly, restarting in 1.0s`, и причина не
читается ни в одном логе». Это неточно: `GatewayRunner` дописывает к уведомлению
само исключение и его трейс (`lib/lifecycle/gateway_runner.py:103-106`). Что верно и
что остаётся причиной требования: уведомление называет **перезапуск**, а не
**упавшую зависимость**, и не даёт подсказки, что проверять. Строка вердикта —
единственное место, где это сказано.

#### Scenario: Вердикт напечатан до отказа

- **WHEN** рукопожатие в gateway падает
- **THEN** ДО `raise` в вывод SHALL уйти строка, содержащая признак отказа
  (`НЕ ПОДНЯЛСЯ`) и текст исходной причины
  (проверяется `TestHandshakeFailureRefusesStartup::test_reason_is_printed_before_the_refusal`)

#### Scenario: Причина CLI попадает и в консоль, и в журнал

- **WHEN** рукопожатие в CLI падает
- **THEN** в вывод SHALL уйти строка с признаком отказа, исходной причиной и
  подсказкой, что проверять
- **AND** `logger.error` SHALL быть вызван с той же причиной (`cli_agent.py:362-363`)
  (проверяется `TestCliHandshake::test_reason_is_readable_and_stack_trace_free`
  и `::TestCliHandshake::test_reason_reaches_the_log`)

### Requirement: Живость платформы наблюдается непрерывно, а не только на старте

Рукопожатие подтверждает **подъём** процесса и ничего не говорит о его дальнейшей
жизни. Признак «сессия жива» для этого непригоден: объект stdio-сессии переживает
смерть процесса и остаётся не-`None` до первого неудачного вызова. Поэтому после
подъёма MUST идти наблюдение (`lib/gateway/mcp_health.py`), и обрыв платформы MUST
обнаруживаться **без чьего-либо обращения** к ней.

Наблюдение MUST отличать три вещи, которые неразличимы при вглядывании в флаг:

- **молчание как норма** — процесс жив, проба проходит;
- **молчание как отказ** — процесс мёртв, отказ фиксируется и виден;
- **молчание как отсутствие наблюдения** — петля не идёт или зависла; это отказ
  самого наблюдения, и он MUST NOT выглядеть как «платформа жива».

Готовность процесса (`runtime_readiness`) MUST знать о компоненте `enterprise_mcp`.
Required-ness выводится из конфигурации по тому же правилу, что и у `postgres`:
через платформу идут **все** выходы к данным — журнал (`data.log_events`), очередь
(`data.claim_task`), зеркало, — поэтому её недоступность останавливает работу, а не
ухудшает её. Проверка синхронна и MUST быть быстрой, поэтому ходить в процесс ей
SHALL NOT: она читает снимок последней пробы и показывает его возраст.

#### Scenario: Процесс остановлен — обрыв виден без обращения к платформе

- **WHEN** процесс `enterprise-mcp` остановлен, а к платформе никто не обращается
- **THEN** наблюдение MUST зафиксировать недоступность и опубликовать
  `agent.degraded` в журнале
- **AND** строки лога MUST называть причину (имя класса исключения, если текст
  пуст) — «факт без причины» неотличим от недописанной строки
- **AND** компонент `enterprise_mcp` в `runtime_readiness` MUST стать `DOWN`

#### Scenario: Платформа возвращается без чужого оборота

- **WHEN** процесс отвечает снова
- **THEN** ближайшая проба MUST сама поднять сессию
- **AND** восстановление MUST быть отражено в журнале и в состоянии готовности
- **AND** ожидание чужого оборота для восстановления SHALL NOT требоваться
  (`tests/test_mcp_health_monitor.py::TestLoopSurvivesAndRecovers`)

### Requirement: Проба платформы не имеет побочных эффектов и не нагружает базу

Признак живости MUST определяться протокольным `ping` (`send_ping`), а не
бизнес-операцией. Обоснование не в аккуратности, а в стоимости наблюдения: проба
идёт постоянно, поэтому MUST NOT трогать состояние.

- **Ни одного SQL.** Проба MUST NOT доходить до capability и операции.
- **Ни одного слота рабочего пула.** Пул платформы обслуживает операции; ping
  отвечается протоколом раньше маршрутизации.
- **Ни одной строки в журнале.** Проба MUST NOT писать событие сама: событие
  пишется только на **смену состояния**, иначе недоступность превращается в
  шум, а шум не читают.

Проверяемо: за прогон сервер принимает `PingRequest` и ноль `CallToolRequest` от
наблюдателя (`logs/enterprise-mcp.log`).

#### Scenario: Проба идёт по протоколу и не пишет событие сама

- **WHEN** наблюдатель опрашивает платформу (`McpHealthMonitor.check_once`)
- **THEN** разговор MUST ограничиваться подъёмом сессии и `session.send_ping()`, а
  бизнес-операция (`client.call(...)`) MUST NOT вызываться: тело `_probe_once()` —
  это ровно две строки, `_ensure_session()` и `send_ping()`
  (`lib/services/enterprise_mcp_client.py:905-913`)
- **AND** вердикт MUST выводиться из ответа протокола, а НЕ из `is_connected`:
  объект сессии переживает смерть процесса и остаётся не-`None` до первого
  неудачного вызова, поэтому проба, доверяющая ему, объявляла бы «платформа жива»
  у мёртвого процесса
  (`lib/services/enterprise_mcp_client.py:854-858`;
  `tests/test_mcp_health_wiring.py::TestProbeIsHonest::test_client_probe_pings_the_protocol`
  требует `send_ping` в `_probe_once` и запрещает `is_connected`)
- **AND** успешная проба при неизменном состоянии MUST обновить снимок и НЕ
  публиковать событие: `_emit` вызывается только из двух веток смены состояния —
  деградации и восстановления (`lib/gateway/mcp_health.py:262-271`, `:290-298`),
  а обычная ветка прошедшей пробы молчит (`:301-308`);
  первая успешная проба — это молчание как норма, а не выход из отказа
  (`tests/test_mcp_health_monitor.py::TestEventsOnlyOnChange::test_first_success_is_not_a_recovery`)

### Requirement: Ни проба, ни сброс сессии не имеют права зависнуть

Предел MUST покрывать **всю** пробу, включая подъём сессии с `initialize()`, а не
только обмен по протоколу. Платформа, которая стартует, но не отвечает, иначе
оставляет наблюдение подвешенным навсегда: цикл не доходит до записи отказа и
перестаёт замечать что-либо вообще. Смерть процесса при этом выглядит как
«тишина», а не как отказ, то есть хуже всего — неотличимо от нормы.

Сброс оборванной сессии (`_reset`) MUST иметь собственный предел: он зовётся на
каждый отказ, и зависшее закрытие процесса останавливало бы и вызов, который
пришёл **из-за** отказа.

#### Scenario: Платформа поднялась, но не отвечает

- **WHEN** процесс запущен, но на `initialize()` не отвечает
- **THEN** проба MUST завершиться отказом по своему пределу
- **AND** наблюдение MUST продолжить опрос в следующем цикле, а не зависнуть
  (`tests/test_mcp_probe_timeouts.py::TestProbeCannotHang`)

#### Scenario: Закрытие оборванного процесса не возвращается

- **WHEN** закрытие стека сессии не завершается
- **THEN** сброс MUST уложиться в собственный предел и освободить клиента
  (`::test_reset_does_not_wait_for_a_stuck_process_forever`)

### Requirement: Обнаружение ограничено интервалом, переподключение — отдельным

Интервал опроса и пауза между попытками **поднять** платформу MUST быть разными
числами. Часто спрашивать дёшево (проба ничего не меняет), часто поднимать
процесс — нет: подъём с рукопожатием занимает секунды.

- Интервал опроса по умолчанию — **10 с** (`gateway.agent.enterprise_mcp.health_interval_sec`).
  Обоснование: обнаружение MUST укладываться в предел, за которым «всё хорошо»
  уже нельзя принять за исправность. Полминуты тишины при остановке платформы
  выглядят как нормальная работа.
- Пауза перед следующей попыткой подъёма по умолчанию — **60 с**
  (`gateway.agent.enterprise_mcp.reconnect_interval_sec`).

Числа MUST объявляться в проекте настроек и быть единственными: второе место с
другим значением — это интервал, который поменяют в одном и забудут про другое.

#### Scenario: Недоступная платформа опрашивается реже, чем поднимается

- **WHEN** последняя проба платформы провалилась
- **THEN** цикл MUST ждать `max(interval_sec, reconnect_interval_sec)`, а не
  интервал опроса: подъём с рукопожатием занимает секунды, и повторять его на
  каждой пробе значит греть процесс зря (`lib/gateway/mcp_health.py:223-226`);
  после прошедшей пробы пауза равна именно интервалу опроса (`:221-222`)
- **AND** конструктор MUST NOT позволить переподключению стать чаще опроса и
  MUST поднять интервал до нижней границы в 1 с, даже если конфигурация этого
  просит (`lib/gateway/mcp_health.py:154-155`)
- **AND** оба числа MUST приходить из того же раздела
  `gateway.agent.enterprise_mcp`, который объявляет сам процесс
  (`health_interval_sec`, `reconnect_interval_sec`;
  `lib/core/project_settings.py:338-345`), а дефолты MUST импортироваться у
  самого механизма и НЕ переписываться вторым числом в фабрике
  (`lib/core/application_context.py:1786-1798`)
- **AND** опрос при этом MUST продолжаться — следующая проба приходит сама, а не
  по чужому обороту
  (`tests/test_mcp_health_monitor.py::TestLoopSurvivesAndRecovers::test_loop_keeps_probing_while_platform_is_down`)

### Requirement: Подсистемы шлюза, которым нужен клиент платформы, собираются после него

Зеркало сессий, наблюдатель и любые будущие подсистемы шлюза MUST собираться
ПОСЛЕ создания клиента `enterprise-mcp`. Обратный порядок даёт подсистему с
`enterprise_mcp=None`, которая выключается навсегда с текстом «платформа
недоступна» — при платформе живой и только что отчитавшейся рукопожатием.

Порядок MUST проверяться машинно: по входу в тест на глаз он не виден, а
последствие — подсистема, которая годами не пишет ничего и выглядит работающей
(`tests/test_mcp_health_wiring.py::TestWiring::test_mirror_and_monitor_are_built_after_the_client`).

#### Scenario: Подсистемы собираются после клиента, а не вместо него

- **WHEN** `ApplicationContext.create()` собирает подсистемы шлюза
- **THEN** `ctx.enterprise_mcp` MUST присваиваться раньше `ctx.session_mirror` и
  `ctx.mcp_health_monitor` (`lib/core/application_context.py:555`, `:569`, `:570`)
- **AND** наблюдатель MUST собираться только при непустом клиенте: без клиента
  фабрика возвращает `None`, а не следит за `None`
  (`lib/core/application_context.py:1774-1776`)
- **AND** обратный порядок MUST быть виден машинно, а не только на глаз: страж
  разбирает исходник, находит номера строк трёх фабрик и требует
  `client_line < mirror_line` и `client_line < monitor_line` — иначе зеркало снова
  собирается до клиента и выключается с текстом «платформа недоступна» при живой
  платформе (`::TestWiring::test_mirror_and_monitor_are_built_after_the_client`)
- **AND** подсистема, выключенная по отсутствию клиента, MUST называть причину
  (`lib/gateway/mirror/mirror_poller.py:178-183`) — иначе молчащая подсистема
  неотличима от исправной

### Requirement: Отказ конфигурации отличается от отказа доступности

Два класса отказа MUST NOT смешиваться, потому что лечатся они по-разному и
значат разное.

- **Отказ конфигурации** — `ConfigurationError`. Типичный случай: оверлей
  профиля объявлен в ДВУХ файлах (`profiles/<режим>.jsonc` агента и
  `mcp-platform/platform.json → profiles.<имя>`), и расхождение приводит к тому,
  что агент ждёт одни имена таблиц, а платформа пишет в другие. Повтор и
  рестарт такое НЕ исправят. Тип MUST сохраняться: ошибка конфигурации MUST NOT
  заворачиваться в отказ доступности, иначе «опечатка в профиле» выдаст себя за
  «сервер не отвечает».
- **Отказ доступности** — процесс `enterprise-mcp` не поднялся, упал или не
  ответил вовремя. В CLI MUST подниматься `CliStartupError`, уносящий исходную
  причину (и в тексте, и в `__cause__`); в gateway `EnterpriseMcpUnavailable`
  MUST уходить наверх на своём месте.

В CLI код выхода MUST различать эти случаи: `2` — ошибка конфигурации,
`1` — не поднялась зависимость (`cli_agent.py:475-482`). Смешанные коды ломают
разбор: по одному числу нельзя понять, чинить ли профиль или поднимать процесс.

#### Scenario: Расхождение профиля сохраняет тип конфигурации

- **WHEN** рукопожатие (или сверка имён таблиц) даёт `ConfigurationError`
- **THEN** тип MUST сохраниться, без заворачивания в `CliStartupError` (`cli_agent.py:343-352`)
- **AND** в вывод SHALL уйти строка с признаком `КОНФИГУРАЦИЯ`
  (проверяется `TestCliHandshake::test_profile_mismatch_keeps_configuration_error_type`
  и `::TestHandshakeFailureRefusesStartup::test_profile_mismatch_also_refuses_to_start_channels`)

#### Scenario: Отказ доступности сохраняет исходную причину

- **WHEN** рукопожатие падает с `EnterpriseMcpUnavailable`
- **THEN** поднятый `CliStartupError` SHALL нести исходный текст причины (`cli_agent.py:364`)
- **AND** `__cause__` SHALL остаться `EnterpriseMcpUnavailable`
  (проверяется `TestCliHandshake::test_failure_keeps_the_original_cause`)

#### Scenario: Коды выхода CLI различают два класса отказа

- **WHEN** отказ конфигурации доходит до `cli_agent.main()`
- **THEN** код выхода SHALL быть `2`
- **WHEN** отказ доступности доходит до `cli_agent.main()`
- **THEN** код выхода SHALL быть `1` и `stderr` SHALL содержать причину
  (проверяется `TestCliStartupBoundary::test_configuration_error_still_exits_two`,
  `::TestCliStartupBoundary::test_configuration_error_from_handshake_still_exits_two`
  и `::TestCliStartupBoundary::test_startup_failure_exits_nonzero_with_reason`)

### Requirement: Порядок касается старта каналов, а не их конструирования

Инвариант порядка — рукопожатие раньше **запуска** — MUST NOT распространяться на
**конструирование**. Разница существенна и легко спутывается: `create_all()`
только конструирует каналы и задач не создаёт, а `start_all()` — это запуск.
Поэтому `create_all` MAY выполняться до рукопожатия, а `start_all` MUST NOT.

Различение MUST быть зафиксировано явно, иначе правка инварианта превратится в
ложное требование «каналы создаются только после рукопожатия» — то есть в
запрет несуществующей проблемы ценой реального усложнения composition.

Между рукопожатием и `start_all` находится `ctx.attach_log_transport()` —
построение writer'а журнала поверх живой MCP-сессии. Оно MUST оставаться после
рукопожатия: writer строится на живой сессии, которой до рукопожатия нет
(`gateway.py::_run`).

#### Scenario: Порядок в исходнике

- **WHEN** в `gateway.py` вызов рукопожатия расположен в тексте ПОСЛЕ вызова
  `channels.start_all()`
- **THEN** проверка MUST это увидеть и провалиться — это отдать первую задачу в
  никуда
  (проверяется `TestOrdering::test_handshake_precedes_channels`, статическая
  сверка порядка в исходнике)

#### Scenario: Фактический порядок вызовов

- **WHEN** выполняется `gateway._run` на заглушках
- **THEN** рукопожатие SHALL наблюдаться раньше `channels.start_all()` и раньше
  `agent.run()`
  (проверяется `TestRuntimeOrdering::test_handshake_precedes_channels_start_and_agent`;
  статическая сверка выше дёшева, но хрупка — она не ловит «рукопожатие вызвано
  правильно, но не оттуда»)

#### Scenario: Конструирование до рукопожатия — не нарушение

- **WHEN** выполняется `gateway._run` на заглушках
- **THEN** `create_all` SHALL наблюдаться раньше рукопожатия, и это SHALL быть
  признано корректным
- **AND** `create_all` SHALL наблюдаться раньше `start_all`
  (проверяется `TestRuntimeOrdering::test_constructing_channels_is_not_starting_them`)

### Requirement: CLI следует тому же контракту с поправкой на интерактивность вывода

Контракт запуска применим к CLI в полном объёме: то же обязательное
рукопожатие, тот же отказ вместо деградации, то же различение классов отказа.
Поправка касается только способа подъёма и подачи:

1. Рукопожатие MUST выполняться внутри живого event loop до старта REPL —
   stdio-сессия привязана к loop, и поднять её синхронно до `asyncio.run`
   нельзя.
2. Причина MUST печататься одной читаемой строкой **без стек-трейса**, а
   подсказка — отдельной строкой. CLI интерактивен: пользователю нужен вердикт
   «не поднялась платформа», а не дамп.
3. Стек-трейс MUST печататься только по явному запросу через
   `NANOBOT_CLI_TRACEBACK=1`. По умолчанию он SHALL NOT выводиться.

**Сверка профильных имён таблиц на CLI.** В gateway рукопожатие дополнено
сводкой по capability и сверкой имён таблиц с платформой. CLI выполняет
**сверку имён таблиц** (паритет): он жёстко прибит к профилю `test`, оверлей
объявлен в двух файлах, и расхождение — ровно тот дефект, который сверка ловит
(агент ждёт `*_test`, платформа пишет в боевые таблицы). Правило сверки
импортируется у владельца контракта (`cli_agent.py:392` импортирует
`gateway._verify_platform_table_alignment`), а не копируется: вторая копия
разошлась бы с первой при первой же правке. Расхождение MUST оставаться
`ConfigurationError` (код выхода `2`), а не `CliStartupError` (`1`), — иначе
опечатка в оверлее выглядит как упавшая платформа.

Чего CLI по-прежнему НЕ делает — сводку по всем capability. Интерактивная
консоль выигрывает от краткости, а список таблиц отдаёт та же проба
`data.schema_check`, ради которой сверка и делается.

**Граница контракта (что CLI НЕ делает).** Сводка по capability (`vectors` /
`data` / `audit`) на CLI-пути не выполняется намеренно. Это НЕ часть контракта
запуска и не должно выдаваться за него.

#### Scenario: Рукопожатие CLI идёт первым в живом loop

- **WHEN** запускается CLI
- **THEN** рукопожатие SHALL наблюдаться раньше REPL и раньше
  `attach_log_transport()` (`cli_agent.py:238-240`)
  (проверяется `TestCliOrdering::test_handshake_runs_before_repl_and_transport`
  и `TestCliPatchedBranch::test_patched_branch_reaches_the_repl` — обе ветви
  CLI: `--patched` обязана держать тот же контракт)

#### Scenario: Расхождение имён таблиц на CLI отказывает в запуске кодом 2

- **WHEN** CLI запускается, а имена таблиц агента и платформы расходятся
- **THEN** SHALL подниматься `ConfigurationError`, а не `CliStartupError` (`cli_agent.py:415-424`)
- **AND** код выхода SHALL быть `2`, а не `1`
- **AND** REPL SHALL NOT подниматься ни в обычной ветви, ни в `--patched`
  (проверяется `TestCliTableAlignment::test_profile_mismatch_exits_two_and_is_not_masked_as_one`
  и `::TestCliTableAlignment::test_profile_mismatch_refuses_startup_as_configuration_error`,
  обе ветви параметризованы)

#### Scenario: Трейс только по явному запросу

- **WHEN** `NANOBOT_CLI_TRACEBACK` не установлен
- **THEN** `stderr` SHALL NOT содержать `Traceback` (`cli_agent.py:478-482`)
- **WHEN** `NANOBOT_CLI_TRACEBACK=1`
- **THEN** полный трейс SHALL печататься, код выхода SHALL остаться `1`
  (проверяется `TestCliStartupBoundary::test_startup_failure_exits_nonzero_with_reason`
  и `::TestCliStartupBoundary::test_traceback_only_when_explicitly_requested`)

### Requirement: Контракт применим к обоим входам в систему

У системы два application entrypoint'а — `gateway.py` и `cli_agent.py` — и оба
MUST выполнять этот контракт. Отличие между ними — способ подъёма и подача
отказа, а не сам факт отказа. Требование, написанное для одного входа, SHALL NOT
считаться выполненным, пока его не держит второй.

Выключенный раздел `gateway.agent.enterprise_mcp` — единственный случай, когда
рукопожатия не происходит: клиент не создан (`None`) по решению оператора. Это
MUST NOT считаться отказом. Выход SHALL быть явным и печатаемым («не объявлен»
+ предупреждение, что инструменты данных ответят структурной ошибкой), а не
молчаливым, иначе следующий разбер не поймёт, был ли сервер выключен или сломан.

#### Scenario: Выключенный раздел — не отказ

- **WHEN** раздел `gateway.agent.enterprise_mcp` не объявлен
- **THEN** рукопожатие MUST завершиться без исключения в обоих entrypoint'ах
- **AND** в вывод SHALL уйти явная строка о том, что сервер не объявлен
  (проверяется `TestHandshake::test_disabled_section_is_not_an_error` и
  `::TestHandshake::test_disabled_section_is_reported_not_silent`)

#### Scenario: Второй вход не отстаёт от первого

- **WHEN** контракт запуска изменяется
- **THEN** правка MUST применена к обоим entrypoint'ам
- **AND** отсутствие рукопожатия на одном из путей SHALL считаться нарушением
  контракта, а не допустимым упрощением этого пути

#### Scenario: Отказ проб capability не равен отказу запуска

- **WHEN** рукопожатие прошло, но отдельная проба capability не ответила или
  вернула ошибку
- **THEN** старт MUST NOT от этого рушиться: платформа отвечает, а неполнота
  одного capability разбирается отдельно и не должна выглядеть как «шлюз не
  поднялся»
- **AND** при этом расхождение имён таблиц MUST по-прежнему ронять старт
  (проверяется `TestHealthSummary::test_probe_failure_does_not_fail_startup`
  и `::TestHealthSummary::test_misaligned_profile_refuses_to_start`)

### Requirement: Объявленные операции платформы доходят до модели

Система MUST создавать `MCPProvider` поверх того же реестра инструментов,
который передаётся в `AgentLoop`, и MUST поднимать объявленные в
`config.json → tools.mcpServers` серверы до начала работы агента. Объявление
операции само по себе MUST NOT считаться доставкой: настройка, которая
объявлена и не действует, — тот же класс дефекта, что и удалённый снимок
кэша, объявленный живым.

#### Scenario: Объявленные операции появляются в реестре модели

- **WHEN** агент поднимается при объявленном `tools.mcpServers.enterprise`
- **THEN** в реестре инструментов, который видит модель, MUST появиться
  операции с префиксом `mcp_enterprise_`
- **AND** их количество MUST совпадать с `enabled_tools` объявления
- **AND** операция MUST быть вызываема: реальный вызов возвращает ответ
  платформы, а не `identity_missing` при подставленной хуком личности

#### Scenario: Провайдер и цикл получают один и тот же реестр

- **WHEN** собирается `ApplicationContext`
- **THEN** реестр MUST быть создан один и передан и провайдеру, и
  `AgentFactory`
- **AND** `AgentFactory` MUST NOT заменять переданный реестр своим

### Requirement: Отказ соединения с модельной поверхностью останавливает запуск

`MCPProvider.connect()` отказ не бросает: он пишет предупреждение и пробует
позже. Проверка MUST выполняться вызывающей стороной — сверкой объявленных
серверов с соединёнными. Несовпадение MUST подниматься наружу, а не
оставлять агента без инструментов при зелёном старте.

#### Scenario: Объявленный сервер не поднялся

- **WHEN** сервер объявлен, но `connect()` не дал соединения
- **THEN** подъём MUST поднять ошибку с именем сервера и его статусом
- **AND** причина MUST быть напечатана ДО подъёма ошибки наружу
- **AND** в gateway ошибка MUST уйти в `GatewayRunner`, в CLI — в отказ
  запуска до REPL

#### Scenario: Серверы не объявлены

- **WHEN** `tools.mcpServers` пуст
- **THEN** провайдер MUST NOT создаваться, подъём MUST NOT падать
- **AND** причина MUST быть произнесена явно: модель не получает операции
  платформы по решению оператора, а не по ошибке

### Requirement: Соединения модельной поверхности закрываются при остановке

Дочерний процесс платформы, поднятый провайдером, MUST закрываться при
остановке агента. Отказ закрытия MUST NOT ронять остановку.

#### Scenario: Остановка агента

- **WHEN** `ApplicationContext.stop()` вызывается при поднятом провайдере
- **THEN** соединения MUST быть закрыты (`aclose`)
- **AND** отказ закрытия MUST быть записан в лог и MUST NOT прерывать
  остановку

### Requirement: stderr процесса платформы виден отдельно от stderr агента

stderr процесса `enterprise-mcp` MUST NOT попадать в консоль вперемешку с
журналом агента, если оператор это объявил: `errlog`, передаваемый в
`mcp.client.stdio.stdio_client`, MUST быть файлом-приёмником из
`gateway.agent.enterprise_mcp.stderr_log`
(`lib/services/enterprise_mcp_client.py::EnterpriseMcpClient._errlog`).

Путь журнала MUST NOT попадать в argv процесса: это транспорт на стороне
агента, а не настройка платформы, и объявление настроек платформы из агента
запрещено.

Баннер рукопожатия MUST называть, куда ушёл stderr платформы, независимо от
исхода: имя файла и результат попытки открыть окно. Молчание о режиме
наблюдения запрещено — «окно не открылось» и «смотреть не на что» MUST
различаться в выводе.

Отказ открыть файл или окно MUST NOT поднимать исключение и MUST NOT
останавливать старт: процесс продолжит писать в stderr агента, а баннер
скажет, куда именно.

#### Scenario: объявлен файл — stderr уходит в файл, а не в stderr агента

- **WHEN** в `gateway.agent.enterprise_mcp` объявлен `stderr_log`
- **THEN** `stdio_client` MUST получать `errlog` открытым файлом
  (`_errlog()`, проверяется `TestClientWiring::test_declared_path_becomes_errlog`)
- **AND** путь MUST NOT встречаться в `describe()["args"]`
  (`::test_path_never_reaches_process_arguments`)

#### Scenario: файл не объявлен — поведение прежнее

- **WHEN** ключ `stderr_log` отсутствует или пуст
- **THEN** `errlog` MUST быть `None`, то есть значением по умолчанию
  `stdio_client` — stderr агента
  (`TestClientWiring::test_without_declaration_errlog_stays_none`,
  `::test_blank_declaration_is_no_declaration`)

#### Scenario: баннер называет назначение stderr

- **WHEN** рукопожатие прошло (`gateway._connect_enterprise_mcp`)
- **THEN** в выводе MUST быть строка `stderr_report()`
  (`TestHandshake::test_stderr_destination_is_reported`)

#### Scenario: невозможность открыть файл не останавливает старт

- **WHEN** по объявленному пути открыть файл нельзя (например, на пути
  лежит каталог)
- **THEN** `open_redirect` MUST вернуть `None` без исключения, клиент
  MUST продолжить работу в stderr агента
  (`TestRedirectFile::test_unopenable_file_is_not_fatal`)

### Requirement: stderr процесса платформы доступен для чтения в отдельном окне

Файл-приёмник MUST показываться отдельной программой-зрителем: на Windows —
в новой консоли (`CREATE_NEW_CONSOLE`) с чтением файла с ожиданием и
UTF-8, на Linux — в терминале с `tail`, следующим за пересозданием файла.

Создание процесса платформы MUST остаться у SDK: перенаправляется `errlog`,
а не подменяется протокольная обвязка `stdio_client`.

Стандартные потоки зрителя MUST NOT перенаправляться: зритель пишет в
`stdout`, и перенаправление уводит вывод мимо его окна. Окно при этом
продолжает открываться, а заголовок задаёт сама оболочка, поэтому
неполное окно выглядит живым.

Файл MUST NOT быть пуст в момент подъёма зрителя: первая строка журнала
(заголовок прогона с временем и путём) MUST быть записана ДО подъёма
зрителя, иначе пустое окно неотличимо от «смотреть не на что».

На машине без графики (`DISPLAY` и `WAYLAND_DISPLAY` пусты) окно MUST NOT
искаться, а отчёт MUST называть файл.

#### Scenario: окно на Windows создаётся с правильной кодировкой

- **WHEN** зритель поднимается на Windows
- **THEN** команда MUST содержать `Get-Content ... -Wait` и принудительную
  UTF-8 (`chcp 65001`, `OutputEncoding`), иначе русские сообщения в окне
  были бы нечитаемы
  (`TestWindowsViewer::test_command_waits_for_the_file`,
  `::test_command_forces_utf8`)

#### Scenario: потоки зрителя не уходят в NUL

- **WHEN** поднимается зритель на любой платформе
- **THEN** `stdin`, `stdout` и `stderr` зрителя MUST остаться
  неперенаправленными
  (`TestViewerWritesToItsOwnConsole::test_windows_viewer_keeps_the_new_console`,
  `::test_linux_viewer_keeps_the_window`)

#### Scenario: окно не открывается пустым

- **WHEN** зритель поднимается, а платформа ещё ничего не напечатала
- **THEN** в файле MUST уже быть заголовок прогона
  (`TestRedirectFile::test_window_is_never_empty`)

#### Scenario: headless Linux называет файл вместо окна

- **WHEN** `DISPLAY` и `WAYLAND_DISPLAY` пусты
- **THEN** отчёт MUST содержать путь к файлу и упоминать `DISPLAY`
  (`TestPlatformDispatch::test_headless_linux_reports_the_file`)

#### Scenario: кавычки в пути не ломают команду зрителя

- **WHEN** объявленный путь содержит одинарную кавычку
- **THEN** литерал MUST быть экранирован удвоением
  (`TestWindowsViewer::test_quote_in_path_is_escaped`)

### Requirement: Журнал stderr платформы относится к прогону и не рвётся на обрыве

Файл MUST обнуляться при первом открытии в процессе агента, а
переподключение после обрыва сессии MUST дописывать в тот же файл. Иначе
зритель показывал бы вывод прошлого прогона как текущий, а обрыв сессии
обрывал бы журнал ровно в том месте, ради которого его читают.

#### Scenario: прошлый прогон не виден как текущий

- **WHEN** файл уже содержит вывод прошлого прогона
- **THEN** `open_redirect` MUST открыть его на запись с обнулением
  (`TestRedirectFile::test_log_is_truncated_not_appended`)

#### Scenario: переподключение не открывает файл заново

- **WHEN** `_errlog()` вызван повторно после обрыва сессии
- **THEN** MUST вернуться тот же открытый файл, а не новый
  (`TestClientWiring::test_file_is_opened_once_per_client`)

### Requirement: Транспорт платформы — streamable-http на порту от ОС

При выбранном транспорте `http` сессия к `enterprise-mcp` SHALL устанавливаться
по `streamable-http`; сам транспорт выбирается значением конфигурации, и дефолтом
остаётся `stdio` — он же путь отката. Порт
SHALL запрашиваться агентом в `config.json → gateway.agent.enterprise_mcp`
и SHALL быть уникальным между **одновременно живыми процессами**, а не между
контурами: он MUST NOT вычисляться из профиля, MUST NOT читаться из
`platform.json` и MUST NOT приходить через `os.environ`.

Запрос и факт — разные вещи. Агент передаёт платформе **запрос** (`0`
означает «выдай свободный»), а платформа сообщает **фактический** адрес
привязки одной машинно-читаемой строкой **в канале уведомления** —
выделенном унаследованном дескрипторе, который агент открывает ребёнку сам.

Канал уведомления — не stdout, и это не украшение. На stdout конвейера
MCP-сервера запрещена человеческая печать действующим стражем
(`mcp-platform/tests/test_journal_contract_visibility.py::TestServerNeverPrintsToStdout`:
обход AST по всем production-модулям платформы; из обхода исключены пять
модулей, у которых stdout и есть продукт, `servers/enterprise/server.py`
среди них нет, падение на
любом `print()` без `file=`). Запрет модульный, а не транспортный: «в режиме
`http` stdout свободен» верно про канал MCP и неверно про репозиторий. В список
модулей, исключённых из обхода, `server.py` добавлять нельзя — это ослабило бы
страж целиком, а не для одной строки.

Сервер SHALL слушать **только loopback**. Выход порта за пределы loopback
SHALL быть запрещён до появления потребителя платформы, который не является
дочерним процессом того же агента; условие зафиксировано, чтобы появление
второго потребителя не выглядело поводом молча расширить поверхность.

Ветка stdio SHALL сохраняться как путь отката, управляемый значением
конфигурации, и SHALL быть удалена отдельным change после арбитражного прогона,
а не вместе с переходом.

Формат строки рукопожатия SHALL быть единственным местом, где он записан, —
и записан он в `runtime/platform-settings` («Адрес приходит в канал
уведомления»): одна строка JSON с `host`, `port` и `pid`. Здесь он не
воспроизводится: продублированная в двух требованиях договорённость разъедется
при первой же правке одного из них, а вторая копия успела разойтись с кодом
раньше, чем её успели заметить.

Логи ASGI-сервера MUST идти в stderr, а access-log SHALL быть выключен.
Причина прежняя, чем при отменённом варианте со stdout, и потому её нельзя
переносить буквально: смешиваться больше нечему, уведомление идёт в свой
дескриптор. Причина новая — **пустота stdout**: на ней держится страж
`TestServerNeverPrintsToStdout`, и любой будущий вывод туда станет поводом
разбираться вместо того, чтобы просто работать. Устанавливаемый `uvicorn`
пишет access-лог именно в `sys.stdout` (`uvicorn.config.LOGGING_CONFIG`).

Отдельно MUST сохраняться право видеть stderr процесса платформы: пока процесс
дочерний, приёмник `errlog` и окно зрителя продолжают работать. Переход
транспорта SHALL NOT стоить оператору окна наблюдения — смена канала не должна
отнимать то, чем он пользуется при разборе.

#### Scenario: Порт запрашивает агент, фактический сообщает платформа

- **WHEN** поднимается процесс платформы
- **THEN** значение порта SHALL быть прочитано агентом из
  `gateway.agent.enterprise_mcp`
  (`lib/services/enterprise_mcp_client.py::client_from_settings`)
- **AND** оно SHALL NOT попадать в `os.environ` (проверяется
  `tests/test_enterprise_mcp_settings_contract.py`)
- **AND** фактический адрес SHALL быть прочитан агентом из строки в канале
  уведомления (проверяется
  `tests/test_enterprise_mcp_http_transport.py::TestPortUniqueness::test_endpoint_read_from_notify_channel`,
  `::test_endpoint_never_read_from_stdout`)

#### Scenario: Порт не объявлен по контурам

- **WHEN** собирается объявление платформы
- **THEN** порт SHALL приходить из `config.json`, а не из
  `profiles/<mode>.jsonc` (проверяется
  `tests/test_enterprise_mcp_http_transport.py::TestPortUniqueness::test_no_port_in_profile_overlay`,
  `tests/test_profile_integration.py::test_validate_profile_overlay_rejects_extra_keys`)

#### Scenario: Сервер слушает только loopback

- **WHEN** сервер платформы поднимается
- **THEN** адрес привязки SHALL быть `127.0.0.1` (или `::1`), а не `0.0.0.0`
  (проверяется `tests/test_enterprise_mcp_http_transport.py::TestLoopback::test_binds_loopback_only`,
  `::test_wildcard_bind_is_refused`)

#### Scenario: При откате транспорта stderr остаётся виден

- **WHEN** в конфигурации выбран `stdio`
- **THEN** приёмник `errlog` SHALL оставаться тем же файлом, что и до перехода,
  и окно зрителя SHALL открываться. `errlog` — параметр `stdio_client`
  (`enterprise_mcp_client.py:705`) и в http-ветке неприменим вовсе, поэтому
  агент открывает stderr ребёнку сам, при подъёме процесса, в тот же файл.
  Существующий страж покрывает только stdio-путь и упасть на http-ветке не
  может (проверяется
  `tests/test_enterprise_mcp_stderr.py::TestClientWiring::test_declared_path_becomes_errlog`,
  `tests/test_enterprise_mcp_http_transport.py::TestStderrOnHttpPath::test_stderr_lands_in_declared_file`)

### Requirement: Закреплённый порт проверяется до запуска, занятый — отказ запуска

Порядок с объявлением единственности gateway (change
`2026-10-04-queue-as-anchor-identity`) задан там: активный gateway → занятость
закреплённого порта платформы, оба до разбора очереди. Здесь этот порядок
не переопределяется, чтобы канон не зависел от того, какой change заархивирован
первым.

Требование держится **до** `unify-runtime-channels` (D13): после него CLI не
поднимает платформу, и проверки в CLI теряют предмет.

Проверка MUST выполняться в **обоих** входа в систему — в gateway и в CLI:
платформу поднимает и CLI, а значит он обязан знать, свободен ли порт.

Это MUST NOT читаться как противоречие требованию «WebSocket port check остаётся
server-only». То требование — о порте канала, который биндит сам gateway и
который CLI не поднимает; здесь — о порте **платформы**, который биндит её
дочерний процесс, а он поднимается в обоих входах. Проверки MUST остаться
разными по объекту и по месту вызова.

При порте `0` (назначение ОС) проверять нечего: два одновременно живых
процесса не могут получить один порт, и конфликт структурно невозможен.
Проверка MUST выполняться **только при закреплённом порте** (`port: N`,
нужен, когда к серверу должен обращаться внешний инструмент). Порт занят —
агент SHALL отказать в запуске, назвав PID держателя и различив живого
держателя от процесса, которого больше нет.

Проверка MUST выполняться **только при включённом транспорте `http`**. При
транспорте `stdio` порт процессом не биндится, и проверка дала бы ложный отказ
по адресу, который платформа не занимает. Это MUST NOT мешать откату: смена
значения `transport` на `stdio` обязана возвращать прежнее поведение без
правки кода.

Механизм SHALL быть тем же, что уже работает для порта websocket
(`gateway.py::_check_websocket_port_available`), и MUST NOT вводить второго
способа проверки занятости.

Проверка обязана работать на обеих поддерживаемых платформах. Действующая
`_find_listener_pid` использует `netstat -ano -p TCP` (`gateway.py:732-762`) и на
Linux не даёт ничего: оператор получает `PID ?` и команду `taskkill`, которая на
этой ОС неверна. Проверка SHALL получать ветку для Linux, а подсказка по
устранению SHALL соответствовать платформе.

Формулировка отказа SHALL описывать текущий случай: чаще порт держит **живой**
второй gateway в соседнем окне, и текст «вероятно, остался висеть предыдущий
процесс» предлагает оператору погасить работающую систему.

Следствие, которое обязано быть сказано: **после этого change два процесса
платформы одного контура больше не конфликтуют нигде** — их порты различаются
по определению. Раньше это было возможно, и молча: два stdio-процесса делили
пул и файл снимка. Проверка порта остаётся ровно для закреплённого случая и
обнаруживает не конкуренцию агентов, а конкуренцию с чужим процессом на
адресе, который оператор назвал сам.

#### Scenario: Закреплённый порт свободен — процесс поднимается

- **WHEN** закреплённый порт свободен
- **THEN** проверка SHALL пройти, процесс платформы SHALL быть запущен
  (проверяется
  `tests/test_enterprise_mcp_http_transport.py::TestPortExclusivity::test_free_port_starts_process`)

#### Scenario: Порт назначается ОС — проверки нет

- **WHEN** запрошен порт `0`
- **THEN** проверка занятости SHALL NOT выполняться, а фактический адрес
  SHALL прийти от платформы (проверяется
  `tests/test_enterprise_mcp_http_transport.py::TestPortExclusivity::test_ephemeral_port_skips_occupancy_check`)

#### Scenario: Закреплённый порт занят живым процессом — отказ запуска с PID

- **WHEN** закреплённый порт занят
- **THEN** запуск SHALL быть отменён ДО старта каналов и агента
- **AND** в вывод SHALL уйти строка с PID держателя
  (проверяется
  `tests/test_enterprise_mcp_http_transport.py::TestPortExclusivity::test_occupied_port_refuses_startup_with_pid`,
  `::test_occupied_port_never_spawns_a_second_process`)

#### Scenario: Отказ по занятому порту и в CLI

- **WHEN** запускается CLI при закреплённом занятом порте
- **THEN** CLI SHALL отказать до входа в REPL, назвав PID держателя
  (проверяется
  `tests/test_gateway_enterprise_mcp_startup.py::TestCliOrdering::test_cli_refuses_occupied_platform_port`)

#### Scenario: Порт занят, держателя нет — вывод различает два случая

- **WHEN** порт занят, но PID определить не удалось
- **THEN** вывод SHALL говорить, что порт занят, а держатель **не определён**
- **AND** он SHALL NOT утверждать, что «остался висеть предыдущий процесс»
  (проверяется tests/test_enterprise_mcp_http_transport.py::TestPortExclusivity::test_unknown_holder_is_stated_as_unknown`)

#### Scenario: Подсказка соответствует платформе

- **WHEN** отказ по занятому порту выводится на Linux
- **THEN** подсказка SHALL называть POSIX-средство завершения
- **AND** `taskkill` SHALL NOT упоминаться (проверяется
  `tests/test_enterprise_mcp_http_transport.py::TestPlatformPortCheck::test_linux_hint_does_not_mention_taskkill`,
  `tests/test_gateway.py::TestPortCheck::test_linux_finds_listener_pid`)

### Requirement: Ответ по адресу доказывает, что сервер наш

Успешное соединение по адресу MUST NOT само по себе считаться рукопожатием.
Сервер по этому адресу может оказаться чужим: порт занят иным процессом,
поднятым до нас, и MCP-рукопожатие на таком сервере завершится успешно.

Агент SHALL подтверждать принадлежность сервера до того, как считать
подключение состоявшимся: ответ SHALL содержать имя контура и идентификатор
процесса, и оба SHALL совпадать с объявленным контуром и с тем, кого агент
запустил.

Несовпадение контура MUST быть отказом конфигурации, а не предупреждением:
именно расхождение контуров приводит к тому, что журнал тестового прогона
попадает в боевые таблицы, и это обнаруживается только по содержимому боевого
журнала.

#### Scenario: По адресу отвечает запущенный нами сервер

- **WHEN** по объявленному адресу отвечает сервер того же контура
- **THEN** рукопожатие SHALL считаться состоявшимся
  (проверяется
  `tests/test_enterprise_mcp_http_transport.py::TestServerIdentity::test_matching_profile_is_accepted`)

#### Scenario: По адресу отвечает сервер другого контура

- **WHEN** по объявленному адресу отвечает сервер, назвавший другой контур
- **THEN** старт SHALL быть отменён как отказ **конфигурации**, а не доступности
- **AND** в выводе SHALL быть названы оба контура (проверяется
  `tests/test_enterprise_mcp_http_transport.py::TestServerIdentity::test_foreign_profile_is_refused_as_configuration_error`,
  `::test_refusal_reason_is_configuration_not_unavailable`)

#### Scenario: Сервер не называет свою личность

- **WHEN** сервер ответил, но не вернул имя контура
- **THEN** подключение SHALL считаться несостоявшимся
- **AND** агент SHALL NOT продолжать работу, полагаясь на догадку
  (проверяется tests/test_enterprise_mcp_http_transport.py::TestServerIdentity::test_missing_identity_is_not_accepted`)

### Requirement: Присутствие платформы объявляется отдельной строкой вердикта

Стартовый вывод SHALL содержать строку вердикта о платформе — поднята или нет, с
именем контура и идентификатором процесса, — **отдельно** от сводки по
capability.

Разделение обязательно: сводка отвечает на вопрос «какая дешёвая операция у
каждой capability получилась» и не может вместить вывод о процессе. Смешение
двух вопросов в одну строку даёт расхождение, которое читатель не заметит, пока
не станет искать его по журналу.

Деградация снимка, наоборот, SHALL оставаться в сводке capability: она является
свойством capability, а не процесса, и различение обеспечивается кодом причины в
ответе платформы, а не видом строки.

#### Scenario: Платформа поднята — вердикт называет контур и pid

- **WHEN** рукопожатие состоялось
- **THEN** в выводе SHALL быть строка вердикта с контуром и `pid`
  (проверяется
  `tests/test_gateway_enterprise_mcp_startup.py::TestHandshake::test_presence_verdict_names_profile_and_pid`)

#### Scenario: Сводка capability остаётся сводкой

- **WHEN** печатается сводка по capability
- **THEN** деградация снимка SHALL отражаться в ней как состояние capability
  с кодом причины, а не отдельной строкой о процессе
  (проверяется `tests/test_gateway_enterprise_mcp_startup.py::TestHealthSummary::test_snapshot_code_is_reported`,
  `::TestHealthSummary::test_verdict_is_not_merged_into_capability_lines`)

### Requirement: Вердикт старта — это факт консоли, а не новое событие журнала

Вердикт о присутствии платформы MUST печататься через
`operator_console.startup_fact(...)` — тем же форматом, что и остальные
вердиктные строки старта. Прямой `print`/`console.print` из кода проверки
запрещён: строка без полей «кто» и «задача» неотличима в потоке от соседней.

Строка MUST NOT вводить новый тип события журнала. Канонический словарь
имён живёт в платформе (`mcp-platform/libs/enterprise_common/eventing/types.py`),
агент его не импортирует, а страж `tests/test_operator_console_lines.py`
читает словарь разбором AST. Присутствие платформы — это состояние, а не
произошедшее событие: повторять его в журнал на каждом старте значит плодить
события без события.

#### Scenario: Вердикт напечатан фактом консоли

- **WHEN** печатается вердикт о присутствии платформы
- **THEN** он MUST быть создан через `startup_fact`
  (проверяется `tests/test_operator_console_lines.py::test_line_facts_bypass_rich_console`)

#### Scenario: Новый тип события не заведён

- **WHEN** добавлена строка вердикта
- **THEN** `eventing/types.py` MUST NOT пополниться новым именем ради неё
  (проверяется
  `tests/test_operator_console_lines.py::test_console_only_facts_are_not_dressed_as_journal_events`)

#### Scenario: Диагностика старта не сломана

- **WHEN** добавлена вердиктная строка
- **THEN** `tools/diagnose_startup.py` MUST продолжать разбирать лог старта
  (проверяется `tests/test_diagnose_startup.py`)

### Requirement: Канал уведомления имеет контракт отказа

Отсутствие, нечитаемость или повтор уведомления MUST приводить к отказу
запуска с названной причиной, а не к ожиданию. Наивное чтение построчно от
мёртвого ребёнка повесило бы старт, и это прямо нарушило бы требование «Отказ
подъёма — это отказ запуска».

Повтор уведомления MUST трактоваться как ошибка, а не как «второй адрес»: два
разных адреса от одного процесса означают, что адрес не тот, за кем его приняли.

#### Scenario: Уведомление не пришло

- **WHEN** процесс платформы завершился, не отправив уведомления
- **THEN** рукопожатие MUST дать отказ с причиной, а не ждать
  (проверяется
  `tests/test_enterprise_mcp_http_transport.py::TestNotifyContract::test_missing_notification_refuses_startup`)

#### Scenario: Уведомление пришло дважды

- **WHEN** процесс отправил два уведомления с разными адресами
- **THEN** старт SHALL быть отменён (проверяется
  `tests/test_enterprise_mcp_http_transport.py::TestNotifyContract::test_duplicate_notification_refuses_startup`)

### Requirement: Восстановительный переподъём платформы перечитывает адрес

Переподъём по пути восстановления (`_reset()` → `_ensure_session()`, которым
пользуются `call()`, `list_operations()` и проба наблюдателя) MUST заново
прочитать адрес из нового уведомления и, при закреплённом порте, заново его
проверить. Прежний адрес после переподъёма невалиден, а вывод «конфликт
структурно невозможен» этот случай не покрывает: он говорил о назначении ОС,
а не о повторном подъёме на известном номере.

При переподъёме MUST сверяться `pid` из ответа, и расхождение MUST быть
отказом — по той же причине, что и при первом рукопожатии.

#### Scenario: Восстановление читает новый адрес

- **WHEN** сессия переподнимается по пути восстановления
- **THEN** агент SHALL использовать адрес из нового уведомления, а не прежний
  (проверяется
  `tests/test_enterprise_mcp_http_transport.py::TestRecovery::test_recovery_rereads_endpoint`)

#### Scenario: Переподъём на занятый закреплённый порт

- **WHEN** при переподъёме закреплённый порт уже занят
- **THEN** подъём SHALL быть отменён с PID держателя
  (проверяется
  `tests/test_enterprise_mcp_http_transport.py::TestRecovery::test_recovery_refuses_taken_pinned_port`)

### Requirement: Признак «платформа объявлена» не зависит от способа запуска

Готовность (`runtime_health`) определяет, обязательна ли платформа, по наличию
объявления в `gateway.agent.enterprise_mcp`. Сегодня признак — наличие ключа
`command` (`lib/core/application_context.py:1371`), то есть по признаку команды
запуска.

Признак MUST определяться **наличием `transport`**, а не командой запуска.
Требовать все три ключа нельзя: `port` законно отсутствует (значит `0`), и такая
конфигурация была бы объявлена незаявленной — то есть ровно тот `NOT_READY`,
который это же требование запрещает. `port` и `bind` — необязательные.
Иначе переход на HTTP выключит платформу из готовности: она станет
`NOT_READY` там, где раньше была обязательной, и её отказ перестанет быть отказом
запуска — то есть молча вернётся ровно та деградация, которую требование «Отказ
подъёма — это отказ запуска» запрещает.

#### Scenario: Платформа объявлена транспортом, а не командой

- **WHEN** в `gateway.agent.enterprise_mcp` объявлен `transport` без `command`
  (проверяется
  `tests/test_mcp_health_wiring.py::TestDeclaration::test_declared_by_transport_without_command`)
- **THEN** признак «объявлена» SHALL быть истинным
- **AND** required-ness SHALL выводиться из конфигурации канала, как прежде

#### Scenario: Раздел выключен

- **WHEN** в `gateway.agent.enterprise_mcp` нет ни транспорта, ни команды
- **THEN** платформа SHALL считаться не объявленной, и выключенный раздел SHALL
  печататься явно, а не молча (проверяется tests/test_mcp_health_wiring.py::TestDeclaration::test_absent_declaration_is_not_declared`)

## Снятые требования

Раздел нормативной части НЕ содержит требований, и не должен. Ниже — запись о
том, какие требования сняты чисткой кэш-кластера, **чем они заменены** и где
живёт замена. Запись существует, чтобы читатель спеки не искал снятое в
`lib/services/` и не решил, что владение снимком ещё не описано.

| Снятое требование | Чем заменено | Где живёт замена |
|---|---|---|
| `CacheOwnershipCoordinator MUST определять режим cache ДО открытия` (claim → mode → open) | Слоя владения нет: в системе один gateway, writer снимка один и известен заранее. Режим доступа к снимку задаёт его владелец | `mcp-platform/libs/enterprise_data/snapshot/contracts.py::CacheAccessMode` |
| `Ownership contract — atomic claim + real fencing через advisory lock` (таблица `agent_cache_ownership`, `ClaimResult`, `release()`, heartbeat) | Таблицы `agent_cache_ownership` и миграции к ней не существуют; fencing не нужен, потому что takeover'а не бывает | — (контракт снят целиком, замена не требуется) |
| `CacheProvider API с явным mode + layered architecture` (`open_snapshot`/concrete factory, `ctx.cache_provider`, `ReadOnlyAssertionError`, `UnsupportedSqlError`, `query_sql` DDL-запрет, отвержение NFS) | Интерфейс и режимы живут на платформе. В агенте `CacheProvider`/`ctx.cache_provider` нет и не должно быть: агент не владеет снимком | `mcp-platform/libs/enterprise_data/snapshot/contracts.py::CacheProvider` / `::CacheStore`, реализация `snapshot/store.py::DuckDbSnapshotStore` |
| `CacheSyncService для синхронизации данных` (fenced write boundary, `OwnershipLostError`) | Синхронизацией занимается capability `data` платформы, у которой один writer | `mcp-platform/libs/enterprise_data/loader.py::SnapshotLoadService` |
| Строки `CacheProvider` / `CacheOwnershipCoordinator` / `CacheSyncService` / concrete cache factory в таблице composition | Сняты вместе с кэш-кластером; состав таблиц и индексов объявляет платформа | `mcp-platform/platform.json → audit.tables`, `→ vectors.indexes` |
| Путь снимка в `gateway.cache.local_path` как настройка агента | Путь объявляет платформа; в `config.json` секции `gateway.cache` больше нет | `mcp-platform/platform.json → data.snapshot_path`, резолвится в `snapshot/store.py::resolve_snapshot_path` |

Снятые требования про **вторую СУБД-канал** (`RedisChannel`) и **Streamlit**
(`SubprocessManager.spawn_streamlit`, `streamlit_app.py`) в таблице composition
не стояли, но упоминались в `Purpose` прежней редакции; запрет на Streamlit
сохранён как требование, каналов транспорта остался один — PostgreSQL.

## Responsibility

Контракт двух точек входа в систему — `gateway.py` и `cli_agent.py` — поверх
единого composition root `lib/core/application_context.py::ApplicationContext`.
Различие между входами сведено к `role` и опциональным CLI-флагам; всё
остальное собирается одним и тем же кодом.

## Boundary

- **Внутри:** разбор argv, вызов `ApplicationContext.create`, порядок
  подъёма обязательных зависимостей, код возврата, shutdown.
- **Снаружи:** сбор сервисов — `ApplicationContext`; поведение AgentLoop —
  `runtime/agent-loop`; логика каналов и cron; рендер REPL —
  `runtime/operator-console`. Entrypoint не знает про устройство capability
  платформы, кроме имён проб в сводке здоровья.

## Public Contract

`ApplicationContext.create(script_dir, workspace_dir, *, role: Literal["gateway", "cli"], storage_override=None, session_override=None, **kwargs)`
(`lib/core/application_context.py:248-257`).

- `role` — keyword-only и обязательный; им определяется composition
  инфраструктуры (`ctx.role = role`, `:310`), например `CronService` только
  при `role == "gateway"` (`:427`).
- Deprecated compatibility boundary в `**kwargs`: `enable_db_logging`,
  `enable_audit`, `enable_cron`, `print_llm_calls` — принимаются с
  `DeprecationWarning` и применяются как override над `SETTINGS["gateway"].*`
  (`:270-279`).
- `profile` **не** принимается: определён ДО вызова и читается из
  `SETTINGS["profile"]`; передача `profile=` даёт `TypeError` (`:281-283`).
- Gateway: `main` (`gateway.py:882`), `_entrypoint_main` (`:105`).
- CLI: `main` (`cli_agent.py:494`), `_entrypoint_main` (`:110`), ветви
  `_run_vanilla` (`:158`) и `_run_patched` (`:163`), `CliStartupError`
  (`:32`).
- Обязательная зависимость поднимается рукопожатием на старте:
  `_connect_enterprise_mcp(ctx)` (`gateway.py:210`, `cli_agent.py:324`),
  `_connect_mcp_provider(ctx)` (`gateway.py:275`, `cli_agent.py:286`).

## Inputs

- `argv` — разбирается `_parse_args` (`gateway.py:39`, `cli_agent.py:54`).
- `script_dir` и `workspace_dir` — корень проекта и корень workspace;
  `script_dir_for_runtime()` есть у обоих входов (`gateway.py:675`,
  `cli_agent.py:479`).
- `config.json` через `SETTINGS` — отсюда профиль, транспорт, параметры
  платформы.
- CLI-only: `storage_override`, `session_override`.

## Outputs

- Код возврата `main()` (`gateway.py:882`, `cli_agent.py:494`).
- Вердикт и сводка по capability на старте:
  `_report_enterprise_mcp_health` (`gateway.py:339`).
- CLI: строки REPL через `_run_cli_repl` (`cli_agent.py:207`).

## State

Entrypoint-специфичного состояния нет: оба только собирают контекст и
передают владение `ApplicationContext`. Локальные переменные живут в рамках
одного процесса; долговременное состояние принадлежит сервисам контекста
(`ctx.shutdown` — `ShutdownCoordinator`,
`lib/core/application_context.py:244`; `ctx.runtime_events_subscriber` —
`:245`).

## Dependencies

- `lib/core/application_context.py` — composition root;
- `lib/lifecycle/gateway_runner.py` — перезапуск gateway с backoff;
- `lib/lifecycle/shutdown_coordinator.py` — порядок остановки;
- `lib/services/enterprise_mcp_client.py` — stdio-сессия к платформе;
- `lib/services/mcp_provider.py` — модельная поверхность операций;
- `lib/services/operator_console.py` — печать вердиктов и глубина вывода;
- `lib/cli/console_loop.py` — REPL CLI.

## Configuration

- Профиль (`SETTINGS["profile"]`) — **не** аргумент `create`: он прочитан до
  вызова (`lib/core/application_context.py:281-283`). Production-входы не
  передают его в composition root.
- CLI-специфичные runtime-параметры: режим хранилища и имя сессии
  (`storage_override`, `session_override`).
- Блок платформы объявляет себя сам: клиент добавляет `--profile <имя>`
  только для не-`prod`; ни путь снимка, ни имена таблиц журнала в argv не
  едут.
- `gateway.error_messages` / `gateway.repeat_guard` — блоки, которые читает
  контекст (`lib/core/project_settings.py:183`, `:189`).

## Lifecycle

1. `main` разбирает argv и зовёт `_entrypoint_main`
   (`gateway.py:105`, `cli_agent.py:110`).
2. `_create_cli_context` (`cli_agent.py:181`) или аналог в gateway собирает
   контекст через `create(..., role=...)`.
3. Подъём обязательной платформы — рукопожатием, до старта каналов и до
   работы агента: `_connect_enterprise_mcp(ctx)`
   (`gateway.py:210`; в CLI вызывается первым шагом в живом loop —
   `cli_agent.py:324`). Причина неудачи печатается **до** `raise`.
4. Сводка по capability и сверка таблиц профиля:
   `_report_enterprise_mcp_health` (`gateway.py:339`),
   `_verify_platform_table_alignment` (`:426`), в CLI —
   `_verify_platform_tables` (`cli_agent.py:419`).
5. Подсистемы шлюза, которым нужен клиент платформы, собираются после
   него; `_run` (`gateway.py:564`) стартует каналы.
6. Проверка WebSocket-порта — server-only: `_check_websocket_port_available`
   вызывается только из gateway (`gateway.py:183`, определение `:775`).
7. Shutdown: `await ctx.agent.aclose()` (`gateway.py:639`),
   `await ctx.enterprise_mcp.aclose()` (`gateway.py:669`).

## Data Ownership

Entrypoint не владеет данными: ни таблиц, ни файлов, ни снимка. Всё долговременное
принадлежит сервисам, которые собрал `ApplicationContext`; локальный путь
конфигурации определяется при разборе argv и передаётся вниз, а не
записывается.

## Error Behavior

- Отказ подъёма обязательной зависимости — отказ запуска, а не тихая
  деградация: причина печатается до `raise`, иначе `GatewayRunner`
  сообщил бы только «Gateway exited unexpectedly, restarting in 1.0s», и
  причину в логе было бы не найти.
- Неудачная проба capability не роняет старт: платформа отвечает,
  неполнота одного capability разбирается отдельно.
- Расхождение имён таблиц профиля — `ConfigurationError`: оверлей объявлен
  в двух файлах, и заметить его можно только по содержимому
  (`gateway.py:426`).
- `CliStartupError` (`cli_agent.py:32`) — отказ интерактивного входа.
- Код возврата `main()` не равен нулю при отказе.

## Invariants

- Сигнатура `create` одна для обоих входов; различие только в `role` и
  опциональных CLI-флагах.
- `AgentLoop` transport-agnostic: транспорт (in-memory bus в CLI,
  `PostgresChannel` в gateway) живёт ниже AgentLoop, не в нём.
- `role` определяет composition инфраструктуры, а не поведение AgentLoop
  (`lib/core/application_context.py:263-267`).
- Порядок касается **старта** каналов, а не их конструирования: конструирование
  допустимо раньше, подъём — нет.
- Проба платформы не имеет побочных эффектов и не нагружает базу.
- Проверка WebSocket-порта не выполняется в CLI-ветке.

## Forbidden Behavior

- Передавать `profile=` в `ApplicationContext.create` — `TypeError`
  (`lib/core/application_context.py:281-283`); профиль читается из
  `SETTINGS["profile"]` до вызова.
- Тихо переживать неудачу подъёма платформы и продолжать работу.
- Запускать `AgentLoop` до рукопожатия с платформой.
- Тянуть transport в `AgentLoop`.
- Включать `CronService` при `role == "cli"` (`lib/core/application_context.py:427`).
- Вызывать проверку WebSocket-порта из CLI-ветки.
- Поднимать Streamlit (**снят**) или `RedisChannel` (**снят**) в runtime-коде.
- Вычислять путь снимка или имена таблиц журнала в агенте: это объявления
  платформы.

## Consumers

- `lib/core/application_context.py` — composition root, вызывается обоими
  входами.
- `lib/lifecycle/gateway_runner.py` — перезапуск процесса gateway с backoff
  и передача причины в лог.
- Оператор — по вердикту и сводке на старте (`gateway.py:339`), по строке
  профиля и по коду возврата.
- `tests/test_gateway_enterprise_mcp_startup.py` — отдельный страж порядка
  рукопожатия в CLI.

## Implementation

Существующие на диске пути:

- `./gateway.py` — вход шлюза: разбор argv, рукопожатие, сводка здоровья,
  сверка таблиц, старт каналов, shutdown;
- `./cli_agent.py` — вход CLI: те же стадии в живом loop, затем REPL;
- `lib/core/application_context.py` — `ApplicationContext.create`;
- `lib/lifecycle/gateway_runner.py` — перезапуск с backoff;
- `lib/lifecycle/shutdown_coordinator.py` — порядок остановки;
- `lib/services/enterprise_mcp_client.py` — stdio-сессия к платформе;
- `lib/services/mcp_provider.py` — модельная поверхность операций;
- `lib/cli/console_loop.py` — REPL;
- `lib/services/operator_console.py` — печать вердиктов.

## Verification

- `tests/test_gateway.py` — gateway-путь;
- `tests/test_gateway_runner.py` — перезапуск и причины;
- `tests/test_gateway_enterprise_mcp_startup.py` — рукопожатие на старте и
  порядок в CLI;
- `tests/test_cli_agent.py` — CLI-путь;
- `tests/test_cli_agent_profile.py` — профиль не передаётся в composition root;
- `tests/test_gateway_entrypoint_schema_validation.py` — валидация схемы
  на старте;
- `tests/test_application_context.py` — сборка контекста и роль.
