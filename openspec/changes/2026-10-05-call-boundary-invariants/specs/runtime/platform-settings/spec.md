# Дельта: бюджет ожидания каждого клиента платформы

## ADDED Requirements

### Requirement: Бюджет ожидания каждой ноги доступа покрывает бюджет платформы

Агент заходит к операциям платформы двумя разными ногами, и у каждой свой потолок
ожидания. Обе обязаны быть не меньше `platform.json → execution.execution_timeout_sec`.

Обрыв на стороне клиента раньше, чем платформа закончила, — это отказ, который
выглядит как результат: клиент возвращает модели ошибку, а операция продолжает
выполняться, потому что поток Python не убивается
(`mcp-platform/libs/enterprise_common/execution/pipeline.py:20-25`). Повтор, который
модель предложит по советам, запускает ту же работу заново, и платит за неё
источник данных.

| Нога | Путь в `config.json` | Кто применяет |
|---|---|---|
| Модельный вызов | `tools.mcpServers.enterprise.tool_timeout` | `nanobot/agent/tools/mcp.py:632-642` |
| Фоновый клиент | `gateway.agent.enterprise_mcp.tool_timeout_sec` | `lib/services/enterprise_mcp_client.py` |

Правило «клиент не короче платформы» уже сформулировано в
`tests/test_mcp_platform_declaration.py`, но проверяет только фоновую ногу.
Модельная нога — та, где отказ уходит в контекст модели и где пользователь видит
ложное «операция не удалась», — не проверяется ничем, и её потолок (`30`) меньше
платформенного (`120.0`).

#### Scenario: Обе ноги объявлены

- **WHEN** `config.json` объявляет сервер в `tools.mcpServers.enterprise`
- **THEN** `tool_timeout` SHALL быть не меньше
  `platform.json → execution.execution_timeout_sec`
- **AND** `tool_timeout_sec` в `gateway.agent.enterprise_mcp` SHALL быть не меньше
  того же значения
- **AND** правило SHALL проверяться для **каждой** объявленной ноги, а не для
  одной выбранной

#### Scenario: Срабатывание стража

- **WHEN** `tool_timeout` в `tools.mcpServers.enterprise` меньше
  `execution.execution_timeout_sec`
- **THEN** `tests/test_mcp_platform_declaration.py` SHALL падать
- **AND** сообщение SHALL называть обе стороны и разницу в секундах
- **AND** сообщение SHALL указывать, какая именно нога короче платформенного бюджета

#### Scenario: Отсутствующая нога

- **WHEN** у агента не объявлен `tools.mcpServers.enterprise`
- **THEN** страж SHALL проверять только фоновую ногу
- **AND** проверка SHALL НЕ требовать значения, которого в конфигурации нет, и
  SHALL НЕ падать на его отсутствии

#### Scenario: Рост платформенного бюджета

- **WHEN** `execution.execution_timeout_sec` в `platform.json` увеличено
- **THEN** страж SHALL требовать увеличить и `tool_timeout`, и `tool_timeout_sec`,
  иначе тест SHALL падать
- **AND** конкретные числа SHALL оставаться в конфигурации, а не в тесте: страж
  читает обе стороны и сравнивает, поэтому подъём бюджета платформы не требует
  правки теста, но требует правки конфигурации

#### Scenario: Секция объявляет себя верхней границей

- **WHEN** `platform.json → execution` содержит `execution_timeout_sec`
- **THEN** секция SHALL пояснять, что это верхняя граница для **обоих** клиентов
  агента, а не только для фонового
