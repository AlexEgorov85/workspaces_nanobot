# Задачи: инварианты границы вызова

## 1. Инвариант таймаутов на всех ногах

- [ ] 1.1 В `tests/test_mcp_platform_declaration.py` расширить
  `test_call_timeout_does_not_expire_before_the_platform_stops`: сравнивать
  `tools.mcpServers.enterprise.tool_timeout` и
  `gateway.agent.enterprise_mcp.tool_timeout_sec` с одной величиной —
  `platform.json → execution.execution_timeout_sec`
- [ ] 1.2 Сообщение об отказе SHALL называть обе стороны и разницу в секундах;
  строки выбирать по имени настройки, а не по порядку в списке
- [ ] 1.3 Отсутствие `tools.mcpServers.enterprise` SHALL считаться «нога не
  объявлена» и не ронять тест; проверяться SHALL только то, что объявлено
- [ ] 1.4 Поднять `config.json → tools.mcpServers.enterprise.tool_timeout` до
  значения, покрывающего `execution.execution_timeout_sec` с запасом
- [ ] 1.5 Дополнить `_about`-пояснение в `platform.json → execution`: значение —
  верхняя граница для обоих клиентов агента
- [ ] 1.6 Проверить, что `tests/test_config_keys.py` не фиксирует старое
  `tool_timeout` как ожидаемое (там же ассерты по `enterprise_mcp.tool_timeout_sec`)
- [ ] 1.7 Прогнать `tests/test_mcp_platform_declaration.py` и
  `tests/test_config_keys.py` пофайлово

## 2. Сверка категории с каталогом capability

- [ ] 2.1 В `libs/enterprise_common/loader.py` передавать имя capability из
  каталога в проверку объявления
- [ ] 2.2 Расхождение SHALL давать `ToolLoadError` с сообщением, называющим
  capability из пути и объявленную категорию
- [ ] 2.3 Проверку непустоты `registry.py:322-323` НЕ заменять сверкой с
  каталогом: пустая категория и чужая категория — разные отказы
- [ ] 2.4 При фильтрации `--capabilities` имя для сверки брать из пути, чтобы
  расхождение не проходило как «файл вне фильтра»
- [ ] 2.5 В `mcp-platform/tests/test_tool_loader.py` добавить: операция с
  совпадающей категорией загружается; операция с чужой — отвергается, и
  сообщение содержит обе стороны

## 3. Проверка

- [ ] 3.1 `mcp-platform/tests/test_tool_loader.py` — отдельно
- [ ] 3.2 `tests/test_mcp_platform_declaration.py` — отдельно
- [ ] 3.3 `tests/test_config_keys.py` — отдельно
- [ ] 3.4 `mcp-platform/tests/test_operation_registry.py` — отдельно (реестр и
  группировка по категории не должны разойтись после переименования поля)
- [ ] 3.5 `py_compile` по изменённым файлам платформы

## 4. Документация

- [ ] 4.1 В `mcp-platform/docs/MCP-CONTRACTS.md` рядом с открытым вопросом про
  `permissions` указать, что сегодняшняя граница доступа модели —
  `tools.mcpServers.enterprise.enabled_tools`, и она работает
- [ ] 4.2 В `mcp-platform/libs/enterprise_common/registry.py` в докстринге
  `category` указать, что это имя capability, обязательное и сверяемое с
  каталогом

## Вне объёма

- Массовое переименование (`ToolDefinition` → `OperationDefinition`,
  `McpCallContext` → `CallIdentity`, `raw`/`parsed`/`body`) — обосновано в
  `proposal.md`; по одному пункту, только с доказанным дефектом
- `runtime/call-contract` — принадлежит активному change
  `2026-10-03-mcp-native-tools`
- Enforcement для `permissions` — решение открыто в `MCP-CONTRACTS.md:1515`
- `identity_source`, `metadata`, `executor=max_workers` и прочая гигиена из
  второго разбора — оформляется после пунктов 1–2, отдельным решением
