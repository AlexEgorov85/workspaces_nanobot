## Purpose

`role` нормативен не только в `runtime/entrypoints`. Каноническая capability
`runtime/context` закрепляет его в требовании «Единый корень общей
инфраструктуры» (`openspec/specs/runtime/context/spec.md:61,83` —
«различие только в обязательном kwarg `role`», «MUST передавать только `role`,
`storage_override`, `session_override`»).

Снос параметра в коде без дельты здесь оставил бы висящее требование к
несуществующему kwarg'у, и канон разошёлся бы с кодом в тот самый момент,
когда change объявляет себя выполненным. Поэтому решение Р1: `runtime/context`
несёт `MODIFIED` того же требования.

Дельта касается одного требования. Все четыре канонических заголовка
сценариев сохранены; скорректированы тела.

## Scope

Операции дельт, нацеленные на требования, уже лежащие в каноне, приведены
к тексту канона: единственное требование под `MODIFIED` взято из
`openspec/specs/runtime/context/spec.md` целиком, вместе со сценариями.
Требований `ADDED` и `REMOVED` в этой дельте нет. Причина — работа change'а
не сделана; незавершённое остаётся в `tasks.md`.

## MODIFIED Requirements

### Requirement: Единый корень общей инфраструктуры

Система ДОЛЖНА предоставлять `ApplicationContext` как единственную точку сборки runtime-сервисов.

`ApplicationContext.create()` MUST иметь typed signature **БЕЗ** параметров `profile`, `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls`. Эти параметры НЕ ДОЛЖНЫ быть в `def create(..., *, ...):` typed signature.

**`profile` MUST быть разрешён ДО `ApplicationContext.create()`** через `config._initialize_settings(profile=...)`. `ApplicationContext.create()` MUST NOT принимать `profile` как параметр — профиль — это configuration-resolution concern, не runtime concern. После `_initialize_settings(profile=...)` runtime получает уже resolved `SETTINGS` через `SETTINGS["profile"]`.

Параметры `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` MAY приниматься через `**kwargs` для обратной совместимости. Если kwarg передан — используется + `warnings.warn(..., DeprecationWarning, stacklevel=2)`. Если не передан — читается из `SETTINGS["gateway"].*`.

CLI и gateway вызывают `ApplicationContext.create(...)` с одинаковой typed signature; различие только в обязательном kwarg `role` и runtime-флагах `storage_override`, `session_override` (последние два — только из CLI).

Deprecated kwargs через `**kwargs` MUST быть удалены в MINOR релизе после раскрытия этого change (см. отдельный change `remove-deprecated-enable-kwargs`). См. `runtime/entrypoints` для полного контракта.

#### Scenario: Сервисы подключаются через ApplicationContext

- **КОГДА** требуется runtime-сервис
- **ТОГДА** он ДОЛЖЕН быть получен через `ApplicationContext` или его документированный аксессор, а не создан ad hoc

#### Scenario: profile resolved ДО ApplicationContext.create()

- **КОГДА** application entrypoint стартует
- **ТОГДА** он MUST вызвать `config._initialize_settings(profile=...)` ПЕРЕД `ApplicationContext.create(...)`
- **И ДОЛЖЕН** полагаться на global `SETTINGS`, опубликованный `_initialize_settings()`
- **И НЕ ДОЛЖЕН** передавать `profile` как параметр в `ApplicationContext.create(...)`
- **AND** `ApplicationContext.create()` MUST потреблять (consume) уже-resolved global `SETTINGS` через `import config as _config; ctx_settings = _config.SETTINGS`
- **AND** `ApplicationContext.create()` MUST NOT resolve profile самостоятельно и MUST NOT принимать `settings` как параметр

#### Scenario: CLI и gateway используют одну typed signature create()

- **КОГДА** `cli_agent.py` и `gateway.py` инициализируют runtime
- **ТОГДА** оба entrypoint'а MUST вызывать `ApplicationContext.create(...)` БЕЗ `profile`-параметра
- **И ДОЛЖНЫ** передавать только `role`, `storage_override`, `session_override` (последние два — только из CLI)
- **И НЕ ДОЛЖНЫ** зависеть от того, передал ли caller `enable_*`-флаги через `**kwargs`

#### Scenario: Утилиты и тесты могут использовать **kwargs для backward compat

- **КОГДА** `ApplicationContext.create(..., enable_audit=False)` вызывается через `**kwargs`
- **ТОГДА** система MUST принять этот kwarg, использовать значение и залогировать `DeprecationWarning`
- **И НЕ ДОЛЖНА** падать с `TypeError: unexpected keyword argument`
