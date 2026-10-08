## ADDED Requirements

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
`project.json` (`gateway.*`). У `profile` такого migration path нет: он не
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

- **WHEN** `ApplicationContext.create()` выполняется после успешного
  `_initialize_settings(profile=...)`
- **THEN** активный профиль SHALL быть прочитан из `SETTINGS["profile"]`
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
  профилем `test` ДО вызова `ApplicationContext.create()`
- **AND** вызов `ApplicationContext.create()` SHALL NOT содержать `profile`

## MODIFIED Requirements

### Requirement: Единая typed signature ApplicationContext.create с role

`ApplicationContext.create(...)` MUST иметь typed signature:

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

`profile` MUST NOT присутствовать как параметр в `def create(..., *, ...):`
— профиль MUST быть разрешён ДО `create()` через
`config._initialize_settings(profile=...)`. `profile` MUST NOT также
проходить через `**kwargs`: его передача SHALL приводить к `TypeError`.

Параметры `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` MUST NOT присутствовать как именованные параметры в typed signature. Они MAY приниматься ТОЛЬКО через `**kwargs` для backward compat с существующими вызовами.

CLI и gateway MUST вызывать `ApplicationContext.create(...)` с **одной и той же сигнатурой**; различие только в `role` и runtime-флагах (`storage_override`, `session_override` — только из CLI).

#### Scenario: CLI и gateway используют одну typed signature

- **WHEN** `cli_agent.py` и `gateway.py` инициализируют runtime
- **THEN** оба entrypoint'а вызывают `ApplicationContext.create(script_dir=..., workspace_dir=..., role="cli" | "gateway", ...)`
- **AND** оба НЕ передают `profile` как параметр
- **AND** оба не передают `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` как именованные параметры
- **AND** поведение каждого сервиса определяется только конфигом `gateway.*` + `role`

#### Scenario: Profile resolved ДО ApplicationContext.create()

- **WHEN** `cli_agent.py` или `gateway.py` стартует
- **THEN** он MUST вызвать `config._initialize_settings(profile=...)` ДО `ApplicationContext.create(...)`
- **AND** `ApplicationContext.create(...)` MUST NOT принимать `profile` как параметр
- **AND** `ApplicationContext.create(..., profile=...)` MUST приводить к `TypeError`

#### Scenario: Defaults для enable_* берутся из конфига

- **WHEN** в `project.json` отсутствуют ключи `gateway.enable_db_logging`, `gateway.enable_audit`, `gateway.enable_cron`, `gateway.print_llm_calls`
- **THEN** `ApplicationContext.create()` MUST использовать значения: `enable_db_logging=True`, `enable_audit=True`, `enable_cron=True`, `print_llm_calls=False`

### Requirement: Deprecated kwargs с явной compatibility boundary

Deprecated kwargs являются временной compatibility boundary. Они MUST приниматься только через `**kwargs` до выполнения отдельного change `remove-deprecated-enable-kwargs`. До этого change production code MUST NOT использовать эти kwargs. После применения `remove-deprecated-enable-kwargs`:

- `enable_db_logging`
- `enable_audit`
- `enable_cron`
- `print_llm_calls`

MUST NOT приниматься `ApplicationContext.create()`; их передача MUST приводить к `TypeError`.

Перечень deprecated kwargs SHALL состоять ровно из этих четырёх
параметров. `profile` MUST NOT входить в этот перечень: у него нет
migration path в `project.json`, и он не является deprecated API.

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

### Requirement: CLI имеет фиксированный профиль test

`cli_agent.py` MUST NOT принимать `--profile` CLI-аргумент и MUST NOT читать профиль из env. CLI MUST использовать фиксированный профиль `test` при вызове `config._initialize_settings(profile="test")`. Gateway MAY принимать `--profile`.

"test" в контексте CLI НЕ означает урезанный runtime: CLI MUST иметь тот же AgentLoop, Skills, Tools, Vector search, Memory, Logging, Prompts, Runtime patches, что и gateway (плюс CacheProvider interface). Различие только в profile (CLI == "test" fixed) и transport (CLI == in-memory bus).

#### Scenario: CLI не принимает --profile

- **WHEN** пользователь запускает `python cli_agent.py --profile=test`
- **THEN** CLI MUST отклонить флаг с `ConfigurationError` и завершиться с кодом 2
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
