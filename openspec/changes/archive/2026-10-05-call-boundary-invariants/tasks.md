# Задачи: инварианты границы вызова

## 1. Инвариант таймаутов на всех ногах

Дельта лежит в capability `runtime/call-timeout`, а не в `runtime/platform-settings`:
блок `--agent-settings-file` не владеет таймаутами (в `AGENT_BLOCK_PATHS` их нет),
а требование сопоставляет `config.json` агента с `platform.json` платформы.

- [x] 1.1 В `tests/test_mcp_platform_declaration.py` расширить
  `test_call_timeout_does_not_expire_before_the_platform_stops`: сравнивать
  `tools.mcpServers.enterprise.tool_timeout` и
  `gateway.agent.enterprise_mcp.tool_timeout_sec` с одной величиной —
  `platform.json → execution.execution_timeout_sec`
- [x] 1.2 Сравнение SHALL быть строгим (`>`), а не `>=`: равенство бюджетов —
  это отказ ровно в момент завершения платформы. Отдельным случаем проверить,
  что равные бюджеты дают красный тест, и что сообщение говорит «равны», а не
  «короче»
- [x] 1.3 Сообщение об отказе SHALL называть обе стороны и разницу в секундах;
  строки выбирать по имени настройки, а не по порядку в списке
- [x] 1.4 Отсутствие `tools.mcpServers.enterprise` SHALL считаться «нога не
  объявлена» и не ронять тест; проверяться SHALL только то, что объявлено
- [x] 1.5 Поднять `config.json → tools.mcpServers.enterprise.tool_timeout` выше
  `execution.execution_timeout_sec`, с объявленным запасом. Запас не выдавать за
  измеренный: замеров, логов и артефактов прогона в дереве нет, единственный
  источник числа — цитата в докстринге
  `tests/test_mcp_platform_declaration.py:106-108`, поэтому величина остаётся
  открытым решением, а не фактом
- [x] 1.6 Поднять `config.json → gateway.agent.enterprise_mcp.tool_timeout_sec`
  выше `120.0`: равенство платформенному бюджету страж больше не пропускает, и
  одной модельной ноги недостаточно
- [x] 1.7 Синхронно с 1.6 обновить пин `tests/test_config_keys.py:187` —
  `("enterprise_mcp.tool_timeout_sec", 120.0)` закреплён числом, и без этой
  правки тест красный. Задачи 1.9/1.10 этот пин не охватывают
- [x] 1.8 Дополнить `_about`-пояснение в `platform.json → execution`: значение —
  верхняя граница для обоих клиентов агента
- [x] 1.9 Проверить, что `tests/test_config_keys.py` не фиксирует старое
  `tool_timeout` модели как ожидаемое
- [x] 1.10 Прогнать `tests/test_mcp_platform_declaration.py` и
  `tests/test_config_keys.py` пофайлово

## 2. Сверка категории с каталогом capability

- [x] 2.1 В `libs/enterprise_common/loader.py` передавать имя capability из
  каталога в проверку объявления
- [x] 2.2 Расхождение SHALL давать `ToolLoadError` с сообщением, называющим
  capability из пути и объявленную категорию
- [x] 2.3 Проверку непустоты `registry.py:322-323` НЕ заменять сверкой с
  каталогом: пустая категория и чужая категория — разные отказы
- [x] 2.4 При фильтрации `--capabilities` имя для сверки брать из пути, чтобы
  расхождение не проходило как «файл вне фильтра»
- [x] 2.5 В `mcp-platform/tests/test_tool_loader.py` добавить: операция с
  совпадающей категорией загружается; операция с чужой — отвергается, и
  сообщение содержит обе стороны

## 3. Проверка

- [x] 3.1 `mcp-platform/tests/test_tool_loader.py` — отдельно
- [x] 3.2 `tests/test_mcp_platform_declaration.py` — отдельно
- [x] 3.3 `tests/test_config_keys.py` — отдельно
- [x] 3.4 `mcp-platform/tests/test_tool_registry.py` — отдельно (регрессия сверки
  `category` с каталогом: операция с чужой категорией не регистрируется, группировка
  `by_category` не должна разойтись с тем, что загрузилось; переименований полей
  в этом change нет, и прогон защищает сверку, а не словарь)
- [x] 3.5 `py_compile` по изменённым файлам платформы

## 4. Документация

- [x] 4.1 В `mcp-platform/docs/MCP-CONTRACTS.md` рядом с открытым вопросом про
  `permissions` указать, что сегодняшняя граница доступа модели —
  `tools.mcpServers.enterprise.enabled_tools`, и она работает
- [x] 4.2 В `mcp-platform/libs/enterprise_common/registry.py` в докстринге
  `category` указать, что это имя capability, обязательное и сверяемое с
  каталогом

## 5. Переопределение при архивации umbrella-change

- [x] 5.1 Переопределение по `category` **уже доведено до канона иным путём**, не
  через дельту: `enterprise-mcp-platform` архивирован 2026-10-08, и правки в его
  дельте не потребовалось. Фактическое состояние
  `openspec/specs/runtime/tool-registry/spec.md`: поле `category` запрещено как
  носитель принадлежности к capability (`:462`, Forbidden Behavior), слово выведено
  из употребления (`:43`) с указанием причины (`:497-498`), а обязательной осталась
  сверка `capability` со списком, объявленным сервером (`:496`,
  `mcp-platform/libs/enterprise_common/registry.py:446`). Требование «допустимы,
  но на загрузку не влиять» в этой форме в каноне отсутствует. Перечитано после
  архива, а не до: порядок был нарушен, и это записано здесь, чтобы правка не
  выглядела сделанной заранее.
- [x] 5.2 Ложный контраст в канон не уехал: перечисление на `:287` гласит «В
  отличие от `version`, `enabled`, `tags` и `permissions`, поле `quality_policy`
  SHALL проверяться при загрузке» — `category` в перечне уже нет, то есть правка,
  которую требовала задача, присутствует. Формулировка требования и его сценарии
  (`:290-310`) не менялись.
- [x] 5.3 Третьего упоминания, которое правка сделала бы ложным, нет:
  принадлежность к capability в каноне фиксируется полем `capability` с
  обязательной проверкой (`:496`), а не полем `category`. Закрыто проверкой, а не
  пропуском.

## Вне объёма

- Массовое переименование (`ToolDefinition` → `OperationDefinition`,
  `McpCallContext` → `CallIdentity`, `raw`/`parsed`/`body`) — обосновано в
  `proposal.md`; по одному пункту, только с доказанным дефектом
- `runtime/call-contract` — принадлежит активному change
  `2026-10-03-mcp-native-tools`
- Enforcement для `permissions` — решение открыто в `MCP-CONTRACTS.md:1515`
- `identity_source`, `metadata`, `executor=max_workers` и прочая гигиена из
  второго разбора — оформляется после пунктов 1–2, отдельным решением
