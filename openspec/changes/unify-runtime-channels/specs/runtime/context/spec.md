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

## MODIFIED Requirements

### Requirement: Единый корень общей инфраструктуры

Система ДОЛЖНА предоставлять `ApplicationContext` как единственную точку сборки runtime-сервисов.

`ApplicationContext.create()` MUST иметь typed signature **БЕЗ** параметров `profile`, `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls`. Эти параметры НЕ ДОЛЖНЫ быть в `def create(..., *, ...):` typed signature.

**`profile` MUST быть разрешён ДО `ApplicationContext.create()`** через `config._initialize_settings(profile=...)`. `ApplicationContext.create()` MUST NOT принимать `profile` как параметр — профиль — это configuration-resolution concern, не runtime concern. После `_initialize_settings(profile=...)` runtime получает уже resolved `SETTINGS` через `SETTINGS["profile"]`.

Параметры `enable_db_logging`, `enable_audit`, `print_llm_calls` MAY приниматься через `**kwargs` для обратной совместимости. Если kwarg передан — используется + `warnings.warn(..., DeprecationWarning, stacklevel=2)`. Если не передан — читается из `SETTINGS["gateway"].*`. `enable_cron` MUST NOT приниматься composition-ом вовсе: cron перестаёт быть параметром composition (см. «Cron = gateway-only» в `runtime/entrypoints`), и принимать его через `**kwargs` значило бы принимать молчаливо игнорируемый аргумент.

Единственный вызывающий `ApplicationContext.create(...)` — процесс Gateway. `role` MUST NOT быть ни параметром сигнатуры, ни полем `ApplicationContext`; различие между entrypoint'ами, определявшееся `role`, снято вместе с параметром. `storage_override` MUST быть удалён вместе с ролью клиента: локальный выбор storage mode невозможен, когда хранилищем владеет Gateway. `session_override` MAY сохраняться как идентификатор сессии на стороне клиента.

Change `remove-deprecated-enable-kwargs`, который должен был снять deprecated kwargs, в репозитории **отсутствует**, и замена кандидата не выполнена — deprecated kwargs остаются действующей compatibility boundary до отдельного change. См. `runtime/entrypoints` для полного контракта.

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

- **КОГДА** инициализируется Agent Runtime
- **ТОГДА** вызов `ApplicationContext.create(...)` MUST присутствовать только в `gateway.py`
- **И НЕ ДОЛЖЕН** передавать `role` — параметра MUST NOT быть ни в сигнатуре, ни среди полей `ApplicationContext`; передача `role=` MUST приводить к `TypeError`
- **И НЕ ДОЛЖЕН** полагаться на `storage_override` от CLI — локального выбора хранилища у клиента не остаётся
- **И НЕ ДОЛЖЕН** зависеть от того, передал ли caller `enable_*`-флаги через `**kwargs`

#### Scenario: Утилиты и тесты могут использовать **kwargs для backward compat

- **КОГДА** `tools/build_vectors.py` или тест вызывает `ApplicationContext.create(..., enable_audit=False)` через `**kwargs`
- **ТОГДА** система MUST принять этот kwarg, использовать значение и залогировать `DeprecationWarning`
- **И НЕ ДОЛЖНА** падать с `TypeError: unexpected keyword argument`

- **КОГДА** тот же вызов передаёт ключ вне `DEPRECATED_ENABLE_KWARGS` (например, опечатку `enable_aduit=`)
- **ТОГДА** система MUST поднять `TypeError`, называющий принятые имена
- **И НЕ ДОЛЖНА** молча игнорировать ключ
