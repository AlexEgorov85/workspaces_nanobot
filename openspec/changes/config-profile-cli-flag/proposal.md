## Why

`gateway.py --profile=prod` сейчас молча запускается в test-режиме, потому что `config.py:517-518` фиксируют `SETTINGS` на module-level import по `os.environ.get("NANOBOT_PROFILE")`. CLI-флаг парсится позже импорта `config` и попадает только в банер и `ApplicationContext.create(profile=...)`, но глобальный `SETTINGS` уже подменён `_test`-суффиксами. Результат: банер врёт («profile=prod»), runtime-инструменты (включая `history_search`) получают несуществующие таблицы (`agent_gateway_logs_test`), отказывают работать.

Текущее «решение» — `$env:NANOBOT_PROFILE=prod` перед запуском — это обходной путь, который дублирует источник истины. Если убрать env-переменную как источник и оставить только CLI-флаг `--profile`, баг закрывается архитектурно: `--profile` парсится **до** импорта модулей, читающих `SETTINGS`, и единственный источник истины ясен из argv.

## What Changes

- **Удалить** module-level `_ACTIVE_PROFILE` и `SETTINGS` из `config.py:517-518`. `_resolve_mode()` больше **НЕ** читает `os.environ["NANOBOT_PROFILE"]`.
- **Добавить** lazy-init: `config.SETTINGS` становится proxy-объектом, требующим явного `_initialize_settings(profile=...)` от entrypoint перед первым обращением.
- **Entrypoints** (`gateway.py`, `cli_agent.py`, `streamlit_app.py`): парсить `--profile` **до** импорта модулей, читающих конфиг; передавать явное значение в `config._initialize_settings(profile=...)` первой строкой `main()`. `argparse` разбирается синтаксисом `parse_known_args()` на самом верху модуля, до `from loguru import logger` и прочего.
- **Entrypoints** без `--profile` падают с `ConfigurationError("--profile обязателен")` на старте. Никаких default'ов: фикс принципиально убирает дублирование источника.
- **Entrypoints** не выставляют `NANOBOT_PROFILE` в `os.environ` (текущая логика `setdefault` в `config.py:240,291` удаляется полностью).
- **Entrypoints** не должны передавать `--profile` через `os.environ` в дочерние процессы (`exec`-тул, subprocess-наследуемые). Это требование к документации `tools.exec`.
- **Удалить** упоминания `NANOBOT_PROFILE` из: `docs/PROFILES.md`, корневого `AGENTS.md`, `.github/workflows/*.yml` (если есть), `workspace/AGENTS.md`. Перенести инструкцию «как запускать прод» на форму «`python gateway.py --profile=prod`».
- **BREAKING**: все существующие деплои, использующие `NANOBOT_PROFILE=prod` в `docker-compose`/`k8s`/`systemd`, должны быть переписаны на передачу аргумента `command: python gateway.py --profile=prod`. Это требование фиксируется в `CHANGELOG.md` под релизом.

## Capabilities

### New Capabilities

Нет. Существующая спека `configuration/profiles` точно описывает область; вводить параллельную capability-path — дублирование.

### Modified Capabilities

- `configuration/profiles`: меняется требование к **источнику** профиля. Текущее `### Requirement: Resolution happens before runtime initialization` остаётся, но должно быть дополнено требованием `Profile SHALL be resolved from CLI argument exclusively (no environment fallback)`. Существующий `### Requirement: Profile surfaced for infrastructure use` остаётся. Это потребует delta-файла `specs/configuration/profiles/spec.md` с секцией `## MODIFIED Requirements`.

## Impact

- `config.py`: ~30–40 строк удаления/правки, в т.ч.:
  - удаление `_resolve_mode`'s `os.environ` чтения;
  - удаление module-level `_ACTIVE_PROFILE`/`SETTINGS`;
  - добавление `_initialize_settings(profile)` и ленивого proxy;
  - удаление `setdefault("NANOBOT_PROFILE", ...)` в `_export_secrets_to_env` (если таковое имеется).
- `gateway.py`, `cli_agent.py`, `streamlit_app.py`: по ~5–15 строк перестановок — `argparse`-блок вверх файла, единый helper `_resolve_and_init_profile()` до любых импортов.
- `tests/test_config_resolver.py`, `tests/test_profile_integration.py`, `tests/test_config.py`: обновление ассертов (некоторые тесты должны теперь получать `ConfigurationError` без init). По тестам с `patch("config.SETTINGS", ...)` — совместимость сохраняется через proxy-патч.
- `tools/build_vectors.py`, `workspace/skills/*/scripts/cli.py` (любые, импортирующие `SETTINGS` без entrypoint): добавить вызов `_initialize_settings(profile)` при их standalone-запуске. Прямой entrypoint flow (через `gateway.py`) уже включает это.
- `docs/PROFILES.md`: переписать секции «Запуск», «Cron», «Миграция существующих деплоев»; удалить все `NANOBOT_PROFILE`-упоминания. Добавить секцию «Что изменилось» — единый источник профиля = CLI-флаг.
- `AGENTS.md`: убрать упоминание `NANOBOT_PROFILE=prod`; обновить `docs/PROFILES.md`-ссылку.
- `tests/test_application_context.py:110-127` (mock `_resolve_mode` / `resolve_application_config` через monkeypatch): потребует переделки моков под новую сигнатуру `_initialize_settings`.
- `workspace/tools/exec`-параметризация: подтвердить, что `os.environ` НЕ наследуется в дочерние процессы (документация `tools.exec.pathPrepend`).

## Open Questions

Переносятся в `design.md` (не блокируют proposal, но требуют решения до tasks):
- **Healthcheck**: при удалении env-сигнала банер `profile=prod` становится единственным визуальным признаком корректного профиля. Стоит ли поднимать это до readiness-gate (HTTP `/health` отвечает 503 при `profile != expected`)? В текущей задаче это вне scope, но фиксируем как candidate-следствие.
- **Cron-процессы** в репо отсутствуют (`_make_cron_service` инициализируется только при `enable_cron=True`, который не активен ни в одном entrypoint). Если cron появится — потребует отдельного решения про источник профиля. На текущий момент этого делать не нужно.
- **Тесты с `patch("config.SETTINGS", _settings_with(...))`**: продолжают работать через подмену proxy-объекта, но нужен явный smoke-тест в `tests/test_config_resolver.py`. Зафиксировать в tasks.
