## Why

Механизм выбора профиля из environment удалён из runtime-кода, но его следы
остались в трёх местах, и одно из них — actively broken.

1. **Runtime-остаток**: `ApplicationContext.create()` всё ещё принимает
   `profile` через `**kwargs` и валидирует его против `SETTINGS["profile"]`
   (`lib/core/application_context.py:207-219`). Из-за этого контракт
   «профиль выбирается только на границе запуска приложения» формально
   не enforced на уровне composition root: любой caller может передать
   `profile=` и пройти через compatibility-ветку.

2. **Противоречие в тестах**: `tests/test_profile_lifecycle.py::test_cli_agent_no_profile_exits_2`
   ожидает, что `python cli_agent.py` без `--profile` завершится с
   `exit 2` и сообщением `--profile is required`. При этом
   `tests/test_cli_agent_profile.py` и `cli_agent.py:74-81` фиксируют
   обратный контракт: CLI имеет фиксированный профиль `test` и
   `--profile` не принимает вообще. Два теста в одном репозитории
   описывают две несовместимые модели.

3. **Хрупкие ссылки на историческое имя переменной**: активные спеки
   (`openspec/specs/configuration/profiles/spec.md`,
   `openspec/specs/runtime/entrypoints/spec.md`) содержат сценарии,
   привязанные к конкретному историческому имени env-переменной, хотя
   контракт, который они защищают, — name-agnostic («environment не
   участвует в выборе профиля»). Плюс `tests/test_nanobot_profile_env_removed.py`
   закрепляет grep по конкретному литералу вместо архитектурного
   инварианта.

Сейчас репозиторий близок к целевой модели, но остатки старого контракта
означают, что возврат env-fallback не будет замечен архитектурными
тестами.

## What Changes

- **BREAKING**: `ApplicationContext.create()` больше не принимает `profile`
  ни как named-параметр, ни через `**kwargs`. Передача `profile=` приводит
  к `TypeError`. `profile` исключается из списка deprecated compatibility
  kwargs (`DEPRECATED_ENABLE_KWARGS`).
- `ApplicationContext` читает профиль исключительно через `SETTINGS["profile"]`
  и не знает, каким способом профиль был выбран.
- Активные спеки: name-agnostic invariant «environment variables SHALL NOT
  participate in profile resolution» вместо сценариев с историческим именем
  переменной. Фиксируются отдельные сценарии для gateway (`--profile` из argv)
  и CLI (фиксированный `test`).
- Тесты: `tests/test_profile_lifecycle.py` переработан — удалён
  противоречащий контракту `test_cli_agent_no_profile_exits_2`; CLI-часть
  переведена на фиксированный-профиль. `tests/test_cli_agent_profile.py`
  переведён с конкретного имени env-переменной на произвольные
  profile-like переменные.
- `tests/test_nanobot_profile_env_removed.py` удалён, заменён name-agnostic
  static guard: функции пути profile resolution
  (`config._initialize_settings`, `config.resolve_application_config`,
  `config._merge_profile_overlay`) не должны читать `os.environ`.
- Добавлен архитектурный guard: production entrypoints не передают `profile`
  в `ApplicationContext.create()`.
- Документация синхронизирована с фактической архитектурой: `docs/PROFILES.md`,
  `docs/MIGRATION.md`, `docs/INTERNAL_API.md`, `docs/README.md`, `README.md`,
  `AGENTS.md`. Устаревшие примеры (`python cli_agent.py --profile=prod`,
  `-P --profile=prod`) удалены.

Non-goals (явно не входит в scope):

- `streamlit_app.py` не меняется. Он получает профиль через argv
  (`_resolve_profile_from_argv()`), а не через environment, поэтому
  уже соответствует целевой модели. Его удаление — отдельный change
  `remove-streamlit-runtime`.
- Исторические артефакты (`CHANGELOG.md`, `openspec/changes/archive/`)
  не редактируются — это записи о прошлых решениях.

## Capabilities

### New Capabilities

- Нет. Новых capability не вводится: cleanup уже существующих контрактов.

### Modified Capabilities

- `configuration/profiles`: name-agnostic invariant вместо сценариев с
  историческим именем env-переменной; явные сценарии для gateway
  (`--profile` из argv) и CLI (фиксированный `test`).
- `runtime/entrypoints`: `ApplicationContext.create()` MUST NOT принимать
  `profile` ни через named-параметр, ни через `**kwargs`.

## Impact

**Runtime-код:**

- `lib/core/application_context.py` — удаление compatibility-ветки для
  `profile` (строки 207-219) из `create()`; обновление module docstring
  (строки 14-20) и docstring метода (строки 185-195).

**Тесты:**

- `tests/test_profile_lifecycle.py` — удаление
  `test_cli_agent_no_profile_exits_2`; обновление docstring-мэппинга (покрытие
  D.2); переработка CLI-сценариев.
- `tests/test_cli_agent_profile.py` — замена env-проверки с конкретным именем
  на name-agnostic.
- Удаление `tests/test_nanobot_profile_env_removed.py`.
- Новый guard-тест: entrypoints не передают `profile` в `create()`.

**Документация:**

- `docs/PROFILES.md`, `docs/MIGRATION.md`, `docs/INTERNAL_API.md`,
  `docs/README.md`, `README.md`, `AGENTS.md`.

**Спеки:**

- `openspec/specs/configuration/profiles/spec.md`
- `openspec/specs/runtime/entrypoints/spec.md`

**Не затронуто:** `config.py` (механизм уже соответствует целевой модели),
`gateway.py`, `cli_agent.py` (runtime-логика уже корректна),
`streamlit_app.py`.
