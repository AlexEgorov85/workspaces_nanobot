# Задачи

## 1. Код

- [x] 1.1 `lib/services/mcp_provider.py`: `build_mcp_provider` (создаёт
  провайдера на переданном реестре, `None` если серверы не объявлены),
  `connect_mcp_provider` (подъём + сверка объявленных с соединёнными +
  `McpProviderUnavailable` с именем и статусом), `close_mcp_provider`
- [x] 1.2 `lib/core/agent_factory.py`: параметр `tool_registry`; переданный
  реестр используется как есть, свой создаётся только когда его не дали
- [x] 1.3 `lib/core/application_context.py`: реестр и провайдер собираются
  ДО `AgentFactory.create`, поле `tool_registry` уходит в фабрику,
  `mcp_provider` лежит на контексте
- [x] 1.4 `lib/core/application_context.py::stop()`: закрытие соединений
  тем же приёмом, что у `MessageBus.drain()` — задачей в живой loop или
  дожиганием на месте
- [x] 1.5 `gateway.py::_connect_mcp_provider`: вердиктная строка, отказ
  печатается ДО подъёма наружу
- [x] 1.6 `cli_agent.py::_connect_mcp_provider`: то же в подаче CLI, отказ
  поднимает `CliStartupError` до REPL
- [x] 1.7 Вызов `_connect_mcp_provider` после рукопожатия платформы и до
  транспорта журнала в обоих входах

## 2. Тесты

- [x] 2.1 `tests/test_mcp_provider_wiring.py`: сборка, громкий отказ, частичное
  соединение, закрытие, порядок шагов в обоих входах, общий реестр
- [x] 2.2 Каждый страж проверен ломанием, не «должен сработать»:
  фабрика создаёт свой реестр → 1 падение; контекст не отдаёт общий →
  2 падения; `connect` не поднимает отказ → 4 падения; порядок в gateway
  нарушен → 5 падений. Откат возвращал зелёный
- [x] 2.3 `tests/test_agent_factory.py`: фикстура объявляла
  `nanobot.agent` обычным модулем без `__path__`, из-за чего файл проходил
  только рядом с импортом настоящего нанобота. Подменены
  `nanobot.agent.tools` и `nanobot.agent.tools.registry`; 7 passed в
  одиночку и 63 в пачке — зависимости от порядка больше нет

## 3. Проверка на живой платформе

- [x] 3.1 `_initialize_settings("test")` поднимает профиль и выставляет
  `NANOBOT_ENTERPRISE_MCP_PROFILE=test`,
  `NANOBOT_ENTERPRISE_MCP_AGENT_SETTINGS=<workspace>/data_store/agent-settings.json`
- [x] 3.2 Провайдер поднялся: соединение 2.8 с (первый запуск 12.8 с),
  в реестр пришло 7 операций — ровно `enabled_tools`
- [x] 3.3 Вызов без личности → `identity_missing` с перечнем трёх ключей
  (граница работает)
- [x] 3.4 Вызов с личностью плоскими ключами в `arguments` — ровно то, что
  делает `McpIdentityHook` → **прошёл**, вернул 6 скриптов аудита
- [x] 3.5 `platform.json → execution.require_call_meta = false` подтверждён
  пробой: переходный режим работает, хук в текущем виде корректен

## 4. Не сделано и почему

- Проверка схемы за рукопожатие (B2) — не в этом change: она меняет слой,
  который поднимает `SchemaValidationError`, и это отдельное решение
  владельца
- `utils.db` и пул в агенте — снос заблокирован каноном
  `runtime/startup-schema-validation`; живёт в
  `2026-10-04-utils-db-pool-removal`
- Удаление `workspace/utils/db.py` и его 52 тестов — политика рантайма
  запрещает удаление, нужна ручная работа владельца

## 5. Прогоны

- `tests/test_mcp_provider_wiring.py` — 17 passed
- `tests/test_mcp_provider_wiring.py tests/test_agent_factory.py` — 24 passed
- Полный набор агента и платформы — см. сообщение коммита
