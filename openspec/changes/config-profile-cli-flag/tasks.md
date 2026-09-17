## 1. Перевод деплоев на `--profile` (этап M1 из design.md, без кодовых изменений)

- [ ] 1.1 Обновить `docs/PROFILES.md` секцию «Запуск»: убрать блоки про `NANOBOT_PROFILE` env, оставить только CLI-флаг. Проверка: `grep -n 'NANOBOT_PROFILE' docs/PROFILES.md` возвращает 0 строк.
- [ ] 1.2 Обновить `docs/PROFILES.md` секцию «Миграция существующих деплоев»: переформулировать инструкцию «заменить `NANOBOT_PROFILE=prod` на аргумент `command: python gateway.py --profile=prod`» для `docker-compose.yml`, `k8s` manifests, `systemd` units. Проверка: в разделе нет инструкций вида `export NANOBOT_PROFILE=...`.
- [ ] 1.3 Обновить корневой `AGENTS.md` секцию «Configuration»: убрать упоминание `NANOBOT_PROFILE=prod` в инструкции по деплою. Проверка: `grep -n 'NANOBOT_PROFILE' AGENTS.md` возвращает 0 строк (или только в исторической справке/сноске, явно помеченной как deprecated).
- [ ] 1.4 Проверить `.github/workflows/*.yml` на наличие `NANOBOT_PROFILE` env и заменить на run-command с `--profile`. Проверка: `grep -rn 'NANOBOT_PROFILE' .github/` возвращает 0 строк.

## 2. config.py: удаление module-level и lazy-init

- [ ] 2.1 Удалить `_resolve_mode`'s чтение `os.environ.get("NANOBOT_PROFILE", "")` (config.py:240). Теперь функция принимает только `profile_arg`. Проверка: новый тест `tests/test_config_resolver.py::test_resolve_mode_no_env` — при `os.environ.pop("NANOBOT_PROFILE", None)` и `profile_arg=None` падает `ConfigurationError("--profile is required")`; при `profile_arg="prod"` возвращается `"prod"`; переменная окружения не влияет.
- [ ] 2.2 Удалить module-level `_ACTIVE_PROFILE = _resolve_mode()` и `SETTINGS = resolve_application_config(...)` (config.py:517-518). Проверка: `grep -n '^_ACTIVE_PROFILE\|^SETTINGS = ' config.py` возвращает 0 строк.
- [ ] 2.3 Добавить класс `_LazySettings` в `config.py`: поддерживает `__getitem__`, `__getattr__`, `.get()` (через `_inner_dict`); внутренний dict заполняется через `_initialize_settings(profile)`. Проверка: новый тест `tests/test_config_resolver.py::test_settings_lazy_init` — без вызова `_initialize_settings` любое чтение `SETTINGS["x"]` бросает `ConfigurationError("SETTINGS not initialized")`.
- [ ] 2.4 Добавить функцию `_initialize_settings(profile: str)` в `config.py`: вызывает `resolve_application_config(profile=profile)`, сохраняет в `_LazySettings._inner_dict`, идемпотентна (повторный вызов бросает `ConfigurationError("SETTINGS already initialized")`). Проверка: тест `tests/test_config_resolver.py::test_initialize_once` — двойной вызов бросает.
- [ ] 2.5 Удалить `os.environ.setdefault("NANOBOT_PROFILE", ...)` и любые другие места выставления этой env-переменной из `config.py` (`_export_secrets_to_env` и др.). Проверка: `grep -n 'NANOBOT_PROFILE' config.py` возвращает 0 строк.

## 3. Entrypoints: argparse на module-level и явная инициализация

- [ ] 3.1 `gateway.py`: перенести `_parse_args()` (или эквивалентный `argparse`) в module-level область файла, до любых импортов `lib.*` / `nanobot.*` / `loguru`. `_initialize_settings(profile=args.profile)` вызывается **до** `from lib.core.application_context import ...`. Проверка: новый тест `tests/test_config_resolver.py::test_gateway_argv_flow` через subprocess — `python gateway.py --profile=prod` (с минимальным CLI-тестом) приходит до `ApplicationContext.create()` с правильным профилем; `--profile` отсутствует → exit code 2 с `ConfigurationError`.
- [ ] 3.2 `cli_agent.py`: тот же паттерн. Проверка: subprocess-тест `tests/test_cli_agent_argv.py` (новый) — `--profile=...` обязателен, иначе exit 2.
- [ ] 3.3 `streamlit_app.py`: тот же паттерн. Streamlit запускается через `streamlit run`, argv обрабатывается особым образом; проверить, как `sys.argv` выглядит внутри streamlit-процесса, и адаптировать парсинг `--profile` соответственно (вероятно, через `st.session_state` или явный CLI-аргумент `-- --profile=prod`). Проверка: subprocess-тест `tests/test_streamlit_argv.py` (новый) — запуск без `--profile` → ConfigurationError до инициализации Streamlit.

## 4. Standalone-утилиты: проверка fail-fast без entrypoint

Standalone-скрипты (`tools/build_vectors.py`, `tools/check_worker_pool_integrity.py`, `workspace/skills/audit_analyzer/scripts/cli.py`,
`workspace/skills/legal_summarizer/scripts/cli.py` и др.) **не должны
знать про профиль**: они либо запускаются из агента через `exec`-тул
(агент уже инициализировал `SETTINGS`), либо никогда не должны
стартовать standalone (это internal-утилиты). Попытка обязать каждый
standalone-скрипт парсить `--profile` — это размазывание знания о
профиле по всей кодовой базе, против чего и направлен этот change.

После фикса `SETTINGS` становится lazy proxy: первый доступ без
`_initialize_settings` бросает `ConfigurationError`. Это означает,
что **любой** standalone-запуск этих утилит без предварительной
инициализации упадёт с понятным сообщением — без специальных правок
в их коде.

- [ ] 4.1 Убедиться, что в коде нет явных обращений к `config.SETTINGS` на module-level (вне функций/методов). Module-level обращение после фикса упадёт на module import, что лучше, чем глубоко в рантайме, но всё равно непрозрачно. Проверка: `grep -rn '^from config import SETTINGS\|^import config$' tools/ workspace/skills/` — все импорты должны быть внутри `def` или `try`/условных блоков (как сейчас). Если найден module-level импорт — обернуть в функцию (это refactor существующего кода, не частью данного change).
- [ ] 4.2 Добавить тест `tests/test_standalone_failfast.py`: для каждого из 6 файлов (`tools/build_vectors.py`, `tools/check_worker_pool_integrity.py`, `workspace/skills/audit_analyzer/scripts/cli.py`, `workspace/skills/legal_summarizer/scripts/cli.py`, `workspace/skills/legal_summarizer/scripts/llm/config.py`, `workspace/skills/legal_summarizer/scripts/application/pipeline_structure.py`, `workspace/skills/legal_summarizer/scripts/application/brief_context.py`) запустить `python <path>` без предварительной инициализации и убедиться, что процесс завершается с exit-code 1 и сообщением `ConfigurationError: SETTINGS not initialized`. Проверка: тест зелёный, сообщение содержит подсказку «call _initialize_settings(profile) from entry point».

## 5. Тесты: conftest и mock'и

- [ ] 5.1 Добавить `tests/conftest.py` autouse-fixture: для каждого теста по умолчанию вызывать `config._initialize_settings(profile="test")` в setup. Тесты, которым нужен prod, делают явный `_initialize_settings(profile="prod")` первым делом (но autouse уже зарегистрировал test — нужно либо unset перед повторной инициализацией, либо mock-перехват). Проверка: `pytest tests/test_config.py -q` без отдельной настройки падает на первом же `from config import SETTINGS`, если autouse не сработал. С autouse — все существующие тесты `tests/test_config.py` зелёные.
- [ ] 5.2 Переписать mock'и в `tests/test_application_context.py:110-127`: вместо `monkeypatch` на `config._resolve_mode` и `config.resolve_application_config` — mock `_initialize_settings` и stub возвращает `cfg` для дальнейшего использования. Проверка: `pytest tests/test_application_context.py` зелёный; diff моков обозрим (≤30 строк).
- [ ] 5.3 Добавить тест `tests/test_config_resolver.py::test_subprocess_no_env_fallback`: запуск `python -c "from config import SETTINGS; print(SETTINGS['logging'])"` без `_initialize_settings` → exit 1 с `ConfigurationError("SETTINGS not initialized")`. С `os.environ['NANOBOT_PROFILE']='prod'` — то же самое (env-fallback не работает).
- [ ] 5.4 Прогнать `pytest` полностью. Проверка: `pytest -q` зелёный, ≥1480 passed (текущий baseline из AGENTS.md), без новых skipped/xfail.

## 6. Документация

- [ ] 6.1 `docs/PROFILES.md`: переработать секцию «Запуск» под CLI-флаг; удалить секцию «Cron» (env-инструкция); переписать «Миграция существующих деплоев» как таблицу `NANOBOT_PROFILE=...` → `command: ... --profile=...` для docker-compose/k8s/systemd. Проверка: `grep -n 'NANOBOT_PROFILE' docs/PROFILES.md` возвращает 0 строк.
- [ ] 6.2 `docs/PROFILES.md`: добавить секцию «Что изменилось в этом релизе» — ссылка на фикс CLI-флага как единственный источник профиля; отметить, что `NANOBOT_PROFILE` env больше не поддерживается.
- [ ] 6.3 `docs/INTERNAL_API.md` секция «tools.exec»: явно указать, что env-переменные профиля НЕ наследуются в дочерний процесс; если дочернему нужен `--profile`, его передают через `command`. Проверка: раздел не содержит «export NANOBOT_PROFILE=...» как рекомендованный способ.
- [ ] 6.4 Обновить `CHANGELOG.md` секцию `[Unreleased]`: категория `Changed` — «Профиль конфигурации теперь определяется только CLI-флагом `--profile`; env-переменная `NANOBOT_PROFILE` больше не читается; entrypoints без `--profile` падают с `ConfigurationError`. BREAKING для деплоев, использующих `NANOBOT_PROFILE=prod` — требуется миграция на `command: python gateway.py --profile=prod`.»

## 7. Smoke-тест: реальный сценарий пользователя

- [ ] 7.1 На машине с живой БД (которая содержит и `agent_gateway_logs`, и `agent_gateway_logs_test`): запустить `python gateway.py --profile=prod` без `NANOBOT_PROFILE` в env. Банер должен показать `profile=prod`. `agent.predefined_scripts` или equivalent external-инструмент запускает `history_search` с фильтром `event_type=tool_result`. Должен вернуть реальные события, не `db_error`. Проверка: `grep '"status": "error"' на ответе history_search` — 0 результатов; `grep '"status": "success"'` — ≥1 результат при наличии событий в журнале.
- [ ] 7.2 Запустить `python gateway.py --profile=test` без env. Банер `profile=test`. `history_search` показывает структуру `agent_gateway_logs_test` (может быть пустой, главное — отсутствие `db_error`). Проверка: `agent_gateway_logs_test` либо существует, и тогда `history_search` возвращает результат; либо не существует — в текущем тесте проваливался тест прежде всего из-за prod-теста, для test-режима требуется наличие тестовой таблицы (отдельная sub-task; в текущем change не требуется создавать её).
- [ ] 7.3 Запустить `python gateway.py` без `--profile`. Банер НЕ появляется, процесс завершается до `ApplicationContext.create()`, exit code 2, stdout содержит `ConfigurationError: --profile is required`. Проверка: subprocess-тест `tests/test_cli_no_profile.py` (новый) — три кейса для gateway/cli_agent/streamlit.
- [ ] 7.4 Убедиться, что ни один файл в `lib/`, `workspace/`, `tools/`, `tests/`, `docs/`, `.github/` не содержит `NANOBOT_PROFILE` (кроме исторических упоминаний в CHANGELOG/commits). Проверка: `grep -rn 'NANOBOT_PROFILE' --include='*.py' --include='*.md' --include='*.yml' --include='*.jsonc' .` — должно вернуть 0 строк (или только явно отмеченные как deprecated в CHANGELOG).

## 8. Validation через OpenSpec

- [ ] 8.1 `openspec.cmd validate config-profile-cli-flag` — зелёный. Проверка: вывод `"valid"` без errors/warnings. Если есть strict-mode замечания — исправить в соответствующем артефакте.
- [ ] 8.2 По завершении всех задач — `openspec.cmd archive config-profile-cli-flag --yes` для фиксации в main specs (`openspec/specs/configuration/profiles/spec.md` обновится через archive-flow). Проверка: `openspec.cmd show "configuration/profiles" --type spec` показывает новые требования из этого change.
