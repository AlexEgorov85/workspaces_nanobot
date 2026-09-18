# AgentFactory

## Назначение

Предоставляет единую точку создания `AgentLoop` с консистентно настроенными хуками. Гарантирует, что все агенты в системе создаются с одинаковым набором обязательных и опциональных хуков, обеспечивая аудируемость вызовов инструментов, логирование и терминальный вывод.

## Ответственность

Компонент отвечает за:
- создание экземпляров `AgentLoop` с полным набором хуков;
- подключение обязательного `ToolAuditHook` для сбора метаданных вызовов инструментов;
- опциональное подключение `TerminalToolPrintHook` для терминального вывода;
- предоставление фабрики `DatabaseLoggingHook` для per-turn логирования;
- интеграцию project-specific хуков из `workspace/hooks/`.

## Граница

Владеет:
- логикой сборки списка хуков для `AgentLoop`;
- порядком подключения хуков (project hooks → ToolAuditHook → TerminalToolPrintHook).

Может зависеть от:
- `MessageBus` — шина событий;
- конфигурации runtime;
- `SessionManager` — менеджер сессий;
- `CronService` — сервис cron-задач;
- `DbLoggingService` — сервис логирования в БД;
- модулей хуков (`lib/hooks/`).

Не должен зависеть от:
- конкретных реализаций Skills;
- бизнес-логики предметной области;
- сессионных данных.

## Публичный контракт

- `AgentFactory.create(config, bus, session_manager=None, cron_service=None, db_logging_service=None, agent_id=None, project_hooks=None, print_llm_calls=False)` — создать `AgentLoop` с хуками.

Возвращает кортеж `(agent, hooks, hook_factories)`:
- `agent` — созданный `AgentLoop`;
- `hooks` — общие хуки для всех оборотов агента;
- `hook_factories` — фабрики per-turn хуков (для `DatabaseLoggingHook`).

## Входы

- `config` — runtime-конфигурация nanobot;
- `bus` — экземпляр `MessageBus`;
- `session_manager` — менеджер сессий (опционально);
- `cron_service` — сервис cron (опционально);
- `db_logging_service` — сервис логирования в БД (опционально);
- `agent_id` — идентификатор агента для логирования;
- `project_hooks` — список хуков из `workspace/hooks/`;
- `print_llm_calls` — флаг логирования вызовов LLM.

## Выходы

- Экземпляр `AgentLoop` с подключёнными хуками;
- Список общих хуков;
- Список фабрик per-turn хуков.

## Состояние

Компонент не хранит состояния (stateless factory).

## Зависимости

- `nanobot.agent.loop.AgentLoop` — основной агентский цикл;
- `lib.hooks.tool_audit_hook.ToolAuditHook` — обязательный хук аудита;
- `lib.hooks.terminal_tool_print_hook.TerminalToolPrintHook` — опциональный хук терминального вывода;
- `lib.hooks.database_logging_hook.make_db_logging_hook_factory` — фабрика хука логирования.

## Конфигурация

Не читает конфигурацию напрямую. Получает конфигурацию через параметр `config` метода `create()`.

## Жизненный цикл

Компонент не имеет явного жизненного цикла (stateless factory). Создаётся по мере необходимости.

## Владение данными

Не владеет данными. Передаёт ссылки на хуки и фабрики в создаваемый `AgentLoop`.

## Поведение при ошибке

- Если модуль `TerminalToolPrintHook` недоступен — хук пропускается без ошибки;
- Если модуль `DatabaseLoggingHook` недоступен — фабрика не создаётся;
- Ошибки импорта хуков обрабатываются gracefully через try/except.

## Инварианты

- `ToolAuditHook` всегда присутствует в списке хуков;
- Project hooks подключаются ПЕРЕД `ToolAuditHook` (чтобы их правки параметров были видны в аудите);
- `TerminalToolPrintHook` подключается ПОСЛЕ `ToolAuditHook`;
- `DatabaseLoggingHook` НЕ попадает в общий список хуков — только в `hook_factories`;
- `AgentLoop` создаётся ровно один раз на вызов `create()`.

## Запрещённое поведение

Компонент НЕ ДОЛЖЕН:
- создавать `AgentLoop` без `ToolAuditHook`;
- подключать `DatabaseLoggingHook` как общий хук (только через фабрику);
- выполнять monkey-patch на `AgentLoop`;
- хранить состояние между вызовами `create()`;
- зависеть от конкретных реализаций Skills;
- создавать параллельные фабрики агентов.

## Потребители

- `ApplicationContext` — основной потребитель, создающий фабрику при сборке runtime;
- Тесты — для создания изолированных агентов.

## Реализация

Основная реализация:
- `lib/core/agent_factory.py:AgentFactory`

Связанные компоненты:
- `lib/core/application_context.py:ApplicationContext`
- `lib/channels/message_exchange.py:MessageBus`
- `lib/session/pg_session_manager.py:PGSessionManager`
- `lib/services/db_logging_service.py:DbLoggingService`
- `lib/hooks/tool_audit_hook.py:ToolAuditHook`
- `lib/hooks/terminal_tool_print_hook.py:TerminalToolPrintHook`
- `lib/hooks/database_logging_hook.py:make_db_logging_hook_factory`

## Проверка

- Архитектурные тесты: проверка наличия `ToolAuditHook` в созданном агенте;
- Контрактные тесты: проверка порядка подключения хуков;
- Code review: проверка отсутствия дублирования `AgentFactory`.
