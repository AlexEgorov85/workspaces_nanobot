## Why

`gateway.py --profile=prod` сейчас молча запускается в test-режиме, потому что `config.py:517-518` фиксируют `SETTINGS` на module-level import по `os.environ.get("NANOBOT_PROFILE")`. CLI-флаг парсится позже импорта `config` и попадает только в банер и `ApplicationContext.create(profile=...)`, но глобальный `SETTINGS` уже подменён `_test`-суффиксами. Результат: банер врёт («profile=prod»), runtime-инструменты (включая `history_search`) получают несуществующие таблицы (`agent_gateway_logs_test`) и отказывают работать.

Этот change закрепляет единственный источник профиля — **аргумент `--profile`**, переданный в argv application entrypoint. Env-переменная `NANOBOT_PROFILE` **полностью удаляется** как источник истины; ни runtime fallback, ни documentation hint, ни deploy-обвязка (docker-compose / k8s / systemd / GitHub Actions). Это устраняет дублирование источника и закрывает архитектурный баг.

## What Changes

- **Lifecycle профиля** становится строго односторонним и явно заданным:

  ```text
  process start
      ↓
  application entrypoint parses --profile
      ↓
  config._initialize_settings(profile)
      ↓
  ConfigurationResolver builds SETTINGS
      ↓
  runtime imports / ApplicationContext
      ↓
  channels / services / agent
  ```

  Инвариант: **`SETTINGS` SHALL NOT be constructed or resolved during `import config`**. Доступ к `SETTINGS` без предварительного `_initialize_settings(...)` — `ConfigurationError`.

- **Источник профиля** — только argv application entrypoint. Цепочка строго:

  ```text
  argv
      ↓
  --profile
      ↓
  application entrypoint
      ↓
  config._initialize_settings(profile)
  ```

  `_initialize_settings(profile)` принимает уже выбранный профиль
  как явный аргумент. Он **не** ищет профиль в `os.environ`,
  `project.json`, `config.json`, или где-либо ещё. Configuration'ом
  приложения (project.json, profile overlay, secrets, validation)
  занимается отдельный слой — `ConfigurationResolver`
  (`resolve_application_config`), который получает профиль от
  `_initialize_settings` уже как решённое значение и использует
  его ТОЛЬКО для выбора profile overlay.

  `_resolve_mode()` (старая функция) удаляется полностью: после
  отказа от env-чтения и default'а она сводится к whitelist-валидации,
  которая встраивается в `_initialize_settings`. Сохранение
  отдельной функции с именем `_resolve_mode` порождало бы лишнюю
  сущность без собственной ответственности.

- **Устаревшая env var (исторически именовавшаяся как `NANOBOT_PROFILE`)
  полностью удаляется** как механизм передачи профиля:
  - profile resolution;
  - configuration initialization;
  - runtime fallback;
  - subprocess propagation;
  - docs, deploy descriptors, CI, кода.

  Дополнительный negative scenario: если эта env var каким-то образом
  присутствует в env (например, оставлена от старого деплоя), она
  **не оказывает влияния**. Только argv `--profile`.

  **Никакой runtime sanitization child environment не вводится.**
  Приложение просто не работает с устаревшими env var'ами; их
  игнорирование — это отсутствие кода, который их читает, а не
  активный механизм вычистки.

- **Поддерживаемые профили** — закрытое множество `{"prod", "test"}`. Любое другое значение (`--profile=dev`, `--profile=staging`, `--profile=foo`) — `ConfigurationError` на старте, до какого-либо конструирования runtime. Это консистентно с Negative Requirements существующей спеки (`introduce a third profile without an explicit OpenSpec change`).

- **Application entrypoint** — определённый термин этого change:

  > **Application entrypoint** — исполняемый entrypoint, который создаёт runtime приложения и использует `ApplicationContext` / `SETTINGS`. В текущем change: `gateway.py`, `cli_agent.py`, `streamlit_app.py`.

  Только эти три файла требуют `--profile` обязательно. Standalone utilities НЕ обязаны знать о профиле, если они не используют `ApplicationContext` / resolved `SETTINGS`.

- **Subprocess requirement** ужесточён:
  - Application subprocess (запускающий `gateway.py`, `cli_agent.py` или `streamlit_app.py`) **должен** получить `--profile` через `command`;
  - Application subprocess без `--profile` — fail-fast с `ConfigurationError`;
  - Обычный utility subprocess **не обязан** иметь `--profile`, если он не является application entrypoint;
  - Никакой runtime sanitization child environment не вводится: устаревшие env vars просто игнорируются runtime-кодом;
  - `ConfigurationResolver` **не** занимается subprocess environment — это отдельная concern на application layer.

- **Import-order contract** ужесточён: ни один модуль, импортированный application entrypoint до `_initialize_settings()`, не может обращаться к resolved `SETTINGS`. Это формальное требование спеки (а не только impl-детали): новый scenario «ранний SETTINGS access» защищает от повторения текущего бага.

- **Streamlit invocation** теперь фиксируется явно (один supported invocation, не implementation-uncertainty). Текущий контракт — `streamlit run streamlit_app.py -- --profile=prod` (или эквивалент, проверяется через subprocess-тест в design/tasks).

- **Compatibility layer** вводится как **временный boundary**: `_LazySettings` proxy сохраняет существующие `from config import SETTINGS` (182 импорта) без изменений. Это **не** новая архитектура, а способ пройти этот change с минимальной диффузией по коду. Полный отказ от module-level `SETTINGS` остаётся долгосрочной целью, но в **другой** OpenSpec change.

- **BREAKING**: все существующие деплои, использующие env-based передачу профиля (исторически — `NANOBOT_PROFILE=prod`) в `docker-compose`/`k8s`/`systemd`/GitHub Actions, должны быть переведены на передачу `command: python gateway.py --profile=prod` (или эквивалент для `cli_agent.py`/`streamlit_app.py`).

## Capabilities

### New Capabilities

Нет. Существующая спека `configuration/profiles` точно описывает область; вводить параллельную capability-path — дублирование.

### Modified Capabilities

- `configuration/profiles`: точечные правки существующих требований + добавление требований для **замкнутого множества профилей** и **import-order contract**. Никаких новых capability-path.

## Impact

- `config.py`: точечные правки
  - удаление `_resolve_mode`'s `os.environ` чтения (строки 240-241);
  - удаление module-level `_ACTIVE_PROFILE = _resolve_mode()` и `SETTINGS = resolve_application_config(...)` (строки 517-518);
  - удаление `os.environ.setdefault("NANOBOT_PROFILE", ...)` если присутствует;
  - добавление `_initialize_settings(profile)` с защитой от двойной инициализации;
  - добавление `_LazySettings` proxy-объекта с UNINITIALIZED/INITIALIZED-состояниями;
  - whitelisting профилей: только `{"prod", "test"}` принимаются, остальные — `ConfigurationError`.

- `gateway.py`, `cli_agent.py`, `streamlit_app.py`:
  - парсинг `--profile` до любых `import`, читающих конфиг;
  - вызов `config._initialize_settings(profile=args.profile)` первой строкой `main()`;
  - без `--profile` — exit code 2 + `ConfigurationError("--profile is required")`;
  - `streamlit_app.py` — отдельная адаптация (см. streamlit-конкретное ниже).

- **Standalone utilities (`tools/build_vectors.py`, `tools/check_worker_pool_integrity.py`, `workspace/skills/audit_analyzer/scripts/cli.py`, `workspace/skills/legal_summarizer/scripts/cli.py` и др.)** — **никаких изменений**. Их прямой запуск без entrypoint — неподдерживаемый сценарий; lazy proxy обеспечивает `ConfigurationError` на первом обращении к `SETTINGS`, что и есть требуемое поведение.

- Тесты:
  - `tests/test_config_resolver.py` — новые сценарии (uninitialized access, double-init, invalid profile, env-ignored, import-order, application entrypoint integration, banner≠settings невозможность);
  - `tests/test_application_context.py:110-127` — моки `_resolve_mode`/`resolve_application_config` переписываются под новую сигнатуру `_initialize_settings`;
  - **autouse-fixture НЕ добавляется**: lifecycle-ошибки должны всплывать, а не маскироваться под тестовый bootstrap. Тесты явно вызывают `_initialize_settings(...)` в setup.

- Документация:
  - `docs/PROFILES.md` — переработка «Запуск» под CLI-флаг; «Миграция существующих деплоев» — таблица env → `command: python gateway.py --profile=prod` для docker-compose / k8s / systemd / GitHub Actions; секция «Что изменилось в этом релизе»;
  - `AGENTS.md` (корень) — убрать упоминания устаревших env vars;
  - `.github/workflows/*.yml` — заменить env на `command: ... --profile=...`;
  - `docs/INTERNAL_API.md` — секция «tools.exec»: env-переменные не используются для передачи профиля; application subprocess получает `--profile` через `command`;
  - `CHANGELOG.md` — категория `Changed`: BREAKING для деплоев, мигрирующих с env на CLI-флаг; whitelisting профилей; обязательность `--profile` для application entrypoints.

- `tests/conftest.py`: **autouse-fixture НЕ добавляется**. Тесты, которым нужен `SETTINGS`, делают явный `_initialize_settings(profile="...")` (см. `tests/test_standalone_failfast.py` как позитивный сценарий поведения proxy без init).

## Open Questions

Переносятся в `design.md` (не блокируют proposal, но требуют фиксации до tasks):

1. **Streamlit invocation pattern** — конкретная форма `streamlit run` с `--profile`. После фикса design должен зафиксировать ровно один supported invocation и один acceptance-тест. Не оставлять implementation-uncertainty в принятом change.

2. **Healthcheck / readiness-gate по профилю в проде** — отдельный тикет. Профиль фиксируется при старте и не «просрочивается»; runtime-проверки не имеют смысла, но при деплое легко ошибиться флагом. Возможные варианты (`project.json::runtime.expected_profile`, log-based alert, ничего) — в отдельном OpenSpec-черновике, не блокирует текущий change.

3. **Cron-процессы** — в репо отсутствуют. Не блокирует.

## Что принципиально НЕ делается в этом change

- 182 `from config import SETTINGS` остаются работающими (через `_LazySettings` proxy — compatibility boundary, не новая архитектура).
- Полный переход на dependency injection архитектуру для `SETTINGS` — отдельный, более крупный рефакторинг.
- `ApplicationContext` за пределами устранения дублирующей profile-resolution логики (`ctx_settings` при `profile != _ACTIVE_PROFILE`).
- Схема БД (`agent_gateway_logs` и т.п.).
- profile-specific business logic (запрещено Negative Requirements).
- Healthcheck / readiness-gate.
- Введение новых профилей (`dev`, `staging`).
- Рефакторинг standalone-утилит для проброса `--profile` — они не application entrypoints и не требуют этого.
