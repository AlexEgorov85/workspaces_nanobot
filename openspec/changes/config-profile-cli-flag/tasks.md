## Phase A — Configuration Core (`config.py`)

- [ ] A.1 Удалить module-level `_ACTIVE_PROFILE = _resolve_mode()` и `SETTINGS = resolve_application_config(...)` из `config.py:517-518`. Проверка: `grep -n '^_ACTIVE_PROFILE\|^SETTINGS = ' config.py` возвращает 0 строк.
- [ ] A.2 Удалить `_resolve_mode`'s чтение `os.environ.get("NANOBOT_PROFILE", "")` (config.py:240). Теперь функция принимает только `profile_arg`, причём только из whitelist `{"prod", "test"}`. Проверка: новый тест `tests/test_config_resolver.py::test_resolve_mode_no_env` — при `os.environ.pop("NANOBOT_PROFILE", None)` и `profile_arg=None` падает `ConfigurationError("--profile is required")`; при `profile_arg="prod"` возвращается `"prod"`; при `profile_arg="dev"` падает `ConfigurationError("profile='dev' is not supported")`; env-переменная не влияет.
- [ ] A.3 Удалить `os.environ.setdefault("NANOBOT_PROFILE", ...)` и любые другие места выставления этой env-переменной из `config.py` (`_export_secrets_to_env` и др.). Проверка: `grep -n 'NANOBOT_PROFILE' config.py` возвращает 0 строк.
- [ ] A.4 Добавить функцию `_initialize_settings(profile: str)` в `config.py`: вызывает `resolve_application_config(profile=profile)` с whitelist-проверкой `profile in {"prod", "test"}`, сохраняет результат в `_LazySettings._inner_dict`. **Идемпотентна**: повторный вызов бросает `ConfigurationError("SETTINGS already initialized")`. Проверка: тесты `tests/test_config_resolver.py::test_initialize_once` (двойной init → ConfigurationError) и `test_initialize_invalid_profile` (`_initialize_settings("dev")` → ConfigurationError).
- [ ] A.5 Добавить класс `_LazySettings` в `config.py` — proxy-объект в **UNINITIALIZED/INITIALIZED** состояниях. Поддерживает `__getitem__`, `__getattr__`, `.get()` через `_inner_dict`; при чтении в UNINITIALIZED бросает `ConfigurationError("SETTINGS not initialized: call _initialize_settings(profile=...) from the application entrypoint")`. **Никакой auto-init при чтении**. Проверка: тест `tests/test_config_resolver.py::test_settings_lazy_init` — любое чтение `SETTINGS["x"]` без `_initialize_settings` бросает ошибку.
- [ ] A.6 Удалить избыточную логику пересборки `ctx.settings` в `ApplicationContext.create()` (`lib/core/application_context.py:121-126`, ветка `if resolved_profile == _ACTIVE_PROFILE else resolve_application_config(...)`). После фикса `_ACTIVE_PROFILE` не существует, и пересборка не нужна — `ctx.settings = SETTINGS` всегда. Проверка: `grep -n '_ACTIVE_PROFILE\|resolve_application_config' lib/core/application_context.py` возвращает 0 строк.

## Phase B — Application Entrypoints (`gateway.py`, `cli_agent.py`, `streamlit_app.py`)

### B.1 `gateway.py`

- [ ] B.1.1 Перенести парсинг `--profile` в module-level область (или в `if __name__ == "__main__":` блок, выполняемый **до** `import config`), до любых `from lib.* / nanobot.* / loguru / rich.console`. Парсинг через `argparse.ArgumentParser(add_help=False).add_argument("--profile", type=str, default=None).parse_known_args()`. **Без** `--profile` → `sys.stderr.write("FATAL: --profile is required\n")` + `sys.exit(2)`. **Не** из whitelist → `sys.exit(2)` с `FATAL: --profile=<value> is not supported`.
- [ ] B.1.2 Сразу после успешного парсинга: `import config as _cfg; _cfg._initialize_settings(profile=args.profile)`. **До** всех прочих импортов, читающих SETTINGS.
- [ ] B.1.3 Проверка: `tests/test_config_resolver.py::test_gateway_argv_flow` (subprocess-вызов `python gateway.py --profile=prod` с минимальным CLI-тестом): баннер показывает `profile=prod` И `SETTINGS["logging"]["db"]["table_name"] == "agent_gateway_logs"` (не только баннер, но и реальный runtime config). `--profile` отсутствует → exit code 2 + `ConfigurationError("--profile is required")`.

### B.2 `cli_agent.py`

- [ ] B.2.1 Тот же паттерн: парсинг `--profile` на module-level / `__main__`-блоке до любых импортов, читающих конфиг; whitelist-проверка; `_initialize_settings(profile=...)`.
- [ ] B.2.2 Проверка: subprocess-тест `tests/test_cli_agent_argv.py` (новый) — `--profile=prod` обязателен, без него exit 2.

### B.3 `streamlit_app.py`

- [ ] B.3.1 Поддерживаемая форма запуска (фиксированная в design): `streamlit run streamlit_app.py -- --profile=prod`. Парсинг аргументов после `--` в `sys.argv` — `argv[argv.index("--") + 1:]` — внутри блока, выполняемого до любого импорта runtime.
- [ ] B.3.2 Whitelist-проверка на старте; `_initialize_settings(profile=...)` до `from lib.core.* / nanobot.*`.
- [ ] B.3.3 Проверка: `tests/test_streamlit_argv.py` (новый, subprocess):
  - `streamlit run streamlit_app.py -- --profile=prod` → exit 0 или успешный streamlit-режим с правильным SETTINGS;
  - `streamlit run streamlit_app.py -- --profile=test` → аналогично test;
  - `streamlit run streamlit_app.py` (без `--profile`) → `ConfigurationError("--profile is required")` до инициализации Streamlit;
  - `streamlit run streamlit_app.py -- --profile=dev` → `ConfigurationError("--profile=dev is not supported")`.

## Phase C — Subprocess / environment inheritance

- [ ] C.1 Удалить упоминания `NANOBOT_PROFILE` в `docs/INTERNAL_API.md` секции «Конфигурация `tools.exec`». Добавить explicit statement: «profile-related env vars (notably `NANOBOT_PROFILE`) НИКОГДА не наследуются subprocess'ами; application entrypoints получают профиль через явный `--profile=<value>` в `command`». Проверка: `grep -n 'NANOBOT_PROFILE' docs/INTERNAL_API.md` возвращает 0 строк.
- [ ] C.2 Проверить `workspace/tools/exec`-параметризацию: если код формирует `env=` для subprocess вручную (в обход `nanobot.exec`), убедиться что `NANOBOT_PROFILE` отфильтрован. Если такой код не существует — зафиксировать как «по умолчанию subprocess.env не содержит таких var».
- [ ] C.3 Проверка: тест `tests/test_subprocess_env.py` (новый): subprocess-вызов через `tools.exec`-обёртку — в child env нет `NANOBOT_PROFILE` даже если parent его имеет.

## Phase D — Tests

### D.1 Configuration core unit tests (`tests/test_config_resolver.py`)

- [ ] D.1.1 `test_resolve_mode_no_env`: `profile_arg=None` без env → `ConfigurationError`; `profile_arg="prod"` без env → `"prod"`; env-переменная не влияет.
- [ ] D.1.2 `test_resolve_mode_whitelist`: `profile_arg="dev"` или `"staging"` → `ConfigurationError("profile='<value>' is not supported")`.
- [ ] D.1.3 `test_settings_lazy_init`: `import config; SETTINGS["x"]` без init → `ConfigurationError("SETTINGS not initialized")`. **Никакой** auto-init.
- [ ] D.1.4 `test_initialize_once`: `_initialize_settings("prod")` затем `_initialize_settings("test")` → второй вызов бросает `ConfigurationError("SETTINGS already initialized")`.
- [ ] D.1.5 `test_initialize_invalid_profile`: `_initialize_settings("dev")` → `ConfigurationError`.
- [ ] D.1.6 `test_subprocess_no_env_fallback`: `python -c "from config import SETTINGS; print(SETTINGS['logging'])"` без `_initialize_settings` → exit 1. С `os.environ['NANOBOT_PROFILE']='prod'` — то же самое (env-fallback не работает).

### D.2 Standalone utility smoke (`tests/test_standalone_failfast.py`)

- [ ] D.2.1 Subprocess-запуск каждого из 6 файлов без предварительной инициализации: `tools/build_vectors.py`, `tools/check_worker_pool_integrity.py`, `workspace/skills/audit_analyzer/scripts/cli.py`, `workspace/skills/legal_summarizer/scripts/cli.py`, `workspace/skills/legal_summarizer/scripts/llm/config.py`, `workspace/skills/legal_summarizer/scripts/application/pipeline_structure.py`, `workspace/skills/legal_summarizer/scripts/application/brief_context.py`. Должен упасть с exit 1 и сообщением `ConfigurationError: SETTINGS not initialized` при попытке обращения. **Никаких правок в их коде.**
- [ ] D.2.2 Дополнительно: убедиться что в коде нет module-level `import config SETTINGS` (вне функций). Проверка: `grep -rln '^from config import SETTINGS\|^import config$' tools/ workspace/skills/`. Все импорты должны быть внутри `def` или `try`/`if __name__ == '__main__':`.

### D.3 Application entrypoint integration (`tests/test_cli_no_profile.py` и др.)

- [ ] D.3.1 `test_gateway_no_profile`: `python gateway.py` (без `--profile`) → exit 2 + stderr содержит `FATAL: --profile is required`.
- [ ] D.3.2 `test_gateway_invalid_profile`: `python gateway.py --profile=dev` → exit 2 + stderr содержит `FATAL: --profile=dev is not supported (allowed: prod, test)`.
- [ ] D.3.3 `test_gateway_profile_prod`: `python gateway.py --profile=prod` (с минимальным smoke-CLI) → `SETTINGS["logging"]["db"]["table_name"] == "agent_gateway_logs"` И баннер показывает `profile=prod`. **Integration test проверяет runtime configuration, не только баннер.**
- [ ] D.3.4 `test_gateway_env_ignored`: `NANOBOT_PROFILE=prod python gateway.py --profile=test` → `SETTINGS["logging"]["db"]["table_name"] == "agent_gateway_logs_test"` (CLI wins). См. сценарий «env var does not influence table selection» в spec.
- [ ] D.3.5 Аналогичные 4 теста для `cli_agent.py`.

### D.4 Streamlit invocation tests (`tests/test_streamlit_argv.py`)

- [ ] D.4.1 4 кейса из Phase B.3.3.

### D.5 Mock-переделка (`tests/test_application_context.py:110-127`)

- [ ] D.5.1 Заменить mock'и `_resolve_mode` и `resolve_application_config` на mock `_initialize_settings`. Каждый мок — stub возвращает `cfg` для дальнейшего использования в `ApplicationContext.create()`. Diff моков ≤ 30 строк.
- [ ] D.5.2 Проверка: `pytest tests/test_application_context.py` зелёный.

### D.6 Полный прогон

- [ ] D.6.1 `pytest -q`: зелёный, ≥1480 passed (baseline из AGENTS.md), без новых skipped/xfail.
- [ ] D.6.2 Без autouse-fixture для `_initialize_settings`. Lifecycle-ошибки **должны** всплывать, а не маскироваться.

## Phase E — Documentation / deployment

### E.1 `docs/PROFILES.md`

- [ ] E.1.1 Переработать секцию «Запуск» под CLI-флаг `python gateway.py --profile=prod` / `--profile=test`. Whitelist явно указан.
- [ ] E.1.2 Переработать секцию «Миграция существующих деплоев» как таблицу `NANOBOT_PROFILE=...` → `command: python gateway.py --profile=...` для docker-compose/k8s/systemd/GitHub Actions. Только whitelist-значения.
- [ ] E.1.3 Добавить секцию «Что изменилось в этом релизе» — ссылка на OpenSpec change, фикс CLI-флага как единственный источник профиля; whitelisting.
- [ ] E.1.4 Проверка: `grep -n 'NANOBOT_PROFILE' docs/PROFILES.md` возвращает 0 строк (кроме CHANGELOG-style раздела «Что изменилось», если упомянуто в историческом контексте явно как deprecated).

### E.2 Корневой `AGENTS.md`

- [ ] E.2.1 Убрать упоминание `NANOBOT_PROFILE=prod` в секции «Configuration». Заменить на `command: python gateway.py --profile=prod`.
- [ ] E.2.2 Проверка: `grep -n 'NANOBOT_PROFILE' AGENTS.md` возвращает 0 строк.

### E.3 `docs/INTERNAL_API.md`

- [ ] E.3.1 Секция «tools.exec»: явно указать, что env-переменные профиля НЕ наследуются в дочерний процесс; если дочернему нужен `--profile`, его передают через `command`. См. Phase C.
- [ ] E.3.2 Добавить секцию «streamlit invocation» с supported pattern `streamlit run streamlit_app.py -- --profile=prod`.

### E.4 CI / deploy descriptors

- [ ] E.4.1 `grep -rn 'NANOBOT_PROFILE' .github/` — 0 результатов; заменить на `command: ... --profile=...`.
- [ ] E.4.2 В deployment docs (`docs/PROFILES.md` или отдельном разделе) — указать миграционный path для docker-compose/k8s/systemd.

### E.5 `CHANGELOG.md`

- [ ] E.5.1 Секция `[Unreleased]`, категория `Changed`: «Профиль конфигурации теперь определяется только CLI-флагом `--profile` (whitelist: `prod`, `test`); env-переменная `NANOBOT_PROFILE` больше не читается и не передаётся subprocess'ам; application entrypoints (`gateway.py`, `cli_agent.py`, `streamlit_app.py`) без `--profile` падают с `ConfigurationError`. BREAKING для деплоев, использующих `NANOBOT_PROFILE=prod` — требуется миграция на `command: python gateway.py --profile=prod`.»

## Phase F — Smoke test (real DB scenario)

- [ ] F.1 `python gateway.py --profile=prod` без env: баннер `profile=prod` И `SETTINGS["logging"]["db"]["table_name"] == "agent_gateway_logs"`. `history_search` возвращает реальные события, не `db_error`.
- [ ] F.2 `python gateway.py --profile=test` без env: баннер `profile=test` И `SETTINGS["logging"]["db"]["table_name"] == "agent_gateway_logs_test"`.
- [ ] F.3 `NANOBOT_PROFILE=test python gateway.py --profile=prod`: баннер `profile=prod` И `SETTINGS["logging"]["db"]["table_name"] == "agent_gateway_logs"` (prod, не test — env проигнорирован).
- [ ] F.4 `python gateway.py` без `--profile`: exit 2 + `ConfigurationError: --profile is required`, до какого-либо runtime-импорта.

## Phase G — OpenSpec validation & archive

- [ ] G.1 `openspec.cmd validate config-profile-cli-flag` — зелёный. После переделки spec/design — проверить ещё раз.
- [ ] G.2 По завершении всех task'ов — `openspec.cmd archive config-profile-cli-flag --yes`. `openspec.cmd show "configuration/profiles" --type spec` показывает новые и изменённые требования.

## Архитектурный acceptance checklist

После завершения всех task'ов change считается реализованным, **когда выполнены все следующие пункты**:

- [ ] `config.py` не содержит module-level `SETTINGS` construction (`grep -n '^SETTINGS = ' config.py` → 0).
- [ ] `config.py` не читает `NANOBOT_PROFILE` ни в каком виде (`grep -n 'NANOBOT_PROFILE' config.py` → 0).
- [ ] Нет default-профиля: `python gateway.py` без `--profile` → fail-fast с exit 2.
- [ ] Только `prod` и `test` принимаются (`--profile=dev` / `--profile=staging` / `--profile=foo` → fail-fast).
- [ ] `SETTINGS` не может быть прочитан до `_initialize_settings(...)` (тест D.1.3).
- [ ] `SETTINGS` не может быть инициализирован дважды (тест D.1.4).
- [ ] Профиль не может быть изменён после инициализации (профильное поведение зафиксировано на всю жизнь процесса).
- [ ] Application entrypoints (`gateway.py`, `cli_agent.py`, `streamlit_app.py`) требуют `--profile`.
- [ ] Профиль инициализируется до любых runtime-импортов (Phase B + тест D.6.1).
- [ ] Subprocesses не получают профиль через environment (тест C.3).
- [ ] Application subprocesses получают `--profile` через `command` явно (контракт зафиксирован в design Decision 7 и spec Requirement «Profile does not inherit through subprocess environment»).
- [ ] Streamlit invocation явно определён: `streamlit run streamlit_app.py -- --profile=prod`.
- [ ] Integration-тест проверяет runtime configuration (имя runtime-таблицы), не только баннер (тесты D.3.3, D.3.4, Phase F).
- [ ] Никакой profile-ветки в business logic не добавлено: `grep -rn 'profile.*==.*"prod"\|profile.*==.*"test"' lib/ workspace/ tools/` → 0 строк (проверяется против Negative Requirements существующей спеки).
- [ ] `autouse-fixture для _initialize_settings` НЕ добавлен в `tests/conftest.py` (lifecycle-ошибки должны всплывать).
