## MODIFIED Requirements

### Requirement: Единый корень общей инфраструктуры

Система ДОЛЖНА предоставлять `ApplicationContext` как единственную точку сборки runtime-сервисов.

`ApplicationContext.create()` MUST читать флаги `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` из `SETTINGS["gateway"].*` (`gateway.enable_db_logging`, `gateway.enable_audit`, `gateway.enable_cron`, `gateway.print_llm_calls`) внутри фабрики, а НЕ принимать их через kwargs публичной сигнатуры. CLI и gateway вызывают `ApplicationContext.create(...)` с одинаковой сигнатурой; различие только в runtime-флагах вроде `role`, `storage_override`, `session_override`.

Deprecated kwargs `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` MAY приниматься для обратной совместимости с существующим кодом (тестами, интеграционными скриптами) и MUST быть удалены в MINOR релизе после раскрытия этого change. См. `runtime/entrypoints` для полного контракта.

#### Scenario: Сервисы подключаются через ApplicationContext

- **КОГДА** требуется runtime-сервис
- **ТОГДА** он ДОЛЖЕН быть получен через `ApplicationContext` или его документированный аксессор, а не создан ad hoc

#### Scenario: CLI и gateway используют одну сигнатуру create()

- **КОГДА** `cli_agent.py` и `gateway.py` инициализируют runtime
- **ТОГДА** оба entrypoint'а MUST вызывать `ApplicationContext.create(...)` без `enable_*`-kwargs
- **И ДОЛЖНЫ** передавать только `role`, `storage_override`, `session_override` (последние два — только из CLI)
- **И НЕ ДОЛЖНЫ** зависеть от того, передал ли caller `enable_*`-флаги вручную