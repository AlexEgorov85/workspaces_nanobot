## ADDED Requirements

### Requirement: CLI entrypoint имеет фиксированный профиль test

`cli_agent.py` MUST NOT принимать `--profile` CLI-аргумент и MUST NOT читать профиль из переменных окружения. CLI MUST hardcode `profile="test"` при вызове `config._initialize_settings(profile="test")`.

Это требование вытекает из принципа: CLI — локальный test/dev entrypoint, а не production deployment interface. CLI не должен создавать ложную универсальность через `--profile`/`--profile=prod`/`--profile=staging`. Это уменьшает поверхность конфигурации и количество комбинаций для тестирования.

Gateway entrypoint `gateway.py` MAY принимать `--profile` (текущее поведение); это требование CLI не распространяется на gateway.

"test" в контексте CLI НЕ означает урезанный runtime: CLI MUST иметь тот же AgentLoop, Skills, Tools, DuckDB, Vector search, Memory, Logging, Prompts, Runtime patches, что и gateway. Различие только в:
- profile (CLI == "test", gateway == "test" или "prod" в зависимости от `--profile`);
- transport (CLI == in-memory bus, gateway == PostgresChannel).

После `config._initialize_settings(profile="test")` runtime-компоненты НЕ ДОЛЖНЫ ветвиться по `profile == "test"` — выбор профиля происходит только на этапе resolution, не в runtime-коде.

#### Scenario: CLI не принимает --profile

- **WHEN** пользователь запускает `python cli_agent.py --profile=test`
- **THEN** argparse MUST exit с ошибкой `unrecognized arguments: --profile=test`
- **AND** процесс MUST NOT запускать ApplicationContext

#### Scenario: CLI использует profile="test" по умолчанию

- **WHEN** пользователь запускает `python cli_agent.py` (без `--profile`)
- **THEN** CLI MUST вызвать `config._initialize_settings(profile="test")` (hardcoded)
- **AND** `SETTINGS["profile"]` MUST быть `"test"`

#### Scenario: CLI не читает профиль из переменных окружения

- **WHEN** пользователь запускает `python cli_agent.py` с `NANOBOT_PROFILE=prod` в env
- **THEN** CLI MUST игнорировать переменную окружения
- **AND** `SETTINGS["profile"]` MUST быть `"test"`, не `"prod"`

#### Scenario: Runtime не ветвится по profile

- **WHEN** runtime-компонент (например, `lib/services/db_logging_service.py`) выполняет код
- **THEN** компонент НЕ ДОЛЖЕН содержать `if profile == "test"` или аналогичных branch'ей
- **AND** компонент получает уже resolved `SETTINGS` и работает с конфигурацией напрямую

#### Scenario: Gateway сохраняет --profile механизм

- **WHEN** пользователь запускает `python gateway.py --profile=prod` или `python gateway.py --profile=test`
- **THEN** gateway MUST принять `--profile` (текущее поведение сохраняется)
- **AND** `SETTINGS["profile"]` MUST соответствовать переданному значению