# Tasks — remove-profile-environment-selection

## 0. Уже выполнено параллельным агентом (не дублировать, не откатывать)

- [x] 0.1 `cli_agent.py` — удалён `profile=CLI_FIXED_PROFILE` из smoke-пути `ApplicationContext.create()`; verify: в `cli_agent.py` ни один вызов `create()` не содержит `profile`
- [x] 0.2 `tests/test_application_context_role.py` — `"profile"` добавлен в guard-список запрещённых kwargs в `TestProductionCallersDoNotUseDeprecatedKwargs`; verify: тест проходит
- [x] 0.3 `tests/test_cli_agent.py` — добавлен `TestRunVanillaForwardsStorageAndSession` c assert `"profile" not in captured["kwargs"]`; verify: тест проходит
- [x] 0.4 `tests/test_cli_agent_profile.py` — вакуумный `assert ... or True` заменён реальной проверкой; verify: тест проходит (но всё ещё содержит литерал исторического имени — см. 3.3)

## 1. Runtime: composition root не принимает profile

- [x] 1.1 Удалить блок `if "profile" in kwargs:` из `ApplicationContext.create()` в `lib/core/application_context.py` (строки 207-219); verify: grep `if "profile" in kwargs` не находит совпадений
- [x] 1.2 Добавить reject неизвестных kwargs в `_resolve_enable_kwargs` (после цикла по `DEPRECATED_ENABLE_KWARGS`), чтобы `profile=...` приводил к `TypeError`; verify: `ApplicationContext.create(..., profile="test")` поднимает `TypeError`
- [x] 1.3 Обновить module docstring `lib/core/application_context.py` (строки 14-20) — убрать `profile` из перечня deprecated kwargs; verify: docstring соответствует фактическому поведению
- [x] 1.4 Обновить docstring `create()` (строки 185-195) — убрать `* ``profile`` (str | None);`; verify: docstring соответствует сигнатуре
- [x] 1.5 Проверить, что `resolved_profile = ctx_settings["profile"]` и `ctx.profile = resolved_profile` сохранены; verify: `ctx.profile` доступен и равен `SETTINGS["profile"]`, обращение к неинициализированному `SETTINGS` по-прежнему даёт `ConfigurationError`

## 2. Тесты: устранить противоречие CLI-контракта

- [x] 2.1 Удалить `test_cli_agent_no_profile_exits_2` из `tests/test_profile_lifecycle.py` (строки 278-286); verify: grep `test_cli_agent_no_profile_exits_2` не находит совпадений
- [x] 2.2 Добавить `test_cli_agent_starts_without_profile_flag` — `python cli_agent.py --smoke` завершается 0 и печатает `profile=test`; verify: тест проходит
- [x] 2.3 Добавить `test_cli_agent_rejects_profile_flag` — `python cli_agent.py --profile=test` завершается 2 со строкой про `--profile` в stderr; verify: тест проходит
- [x] 2.4 Обновить docstring-мэппинг покрытия в шапке `tests/test_profile_lifecycle.py` (секция D.2) под фактический состав тестов; verify: перечисленные имена совпадают с определёнными функциями
- [x] 2.5 Обновить `test_resolve_with_unknown_kwarg_silently_keeps_it` в `tests/test_application_context_role.py` под новое поведение reject; verify: тест проходит и проверяет `TypeError`

## 3. Тесты: name-agnostic guard вместо grep по историческому литералу

- [x] 3.1 Удалить `tests/test_nanobot_profile_env_removed.py`; verify: файл отсутствует
- [x] 3.2 Добавить static guard в `tests/test_profile_lifecycle.py`: функции `config._initialize_settings`, `config.resolve_application_config`, `config._merge_profile_overlay` не обращаются к `os.environ` / `os.getenv` / `environ`; verify: тест проходит; verify: искусственно вставленное обращение к `os.environ` в `_merge_profile_overlay` валит тест
- [x] 3.3 Перевести env-проверку в `tests/test_cli_agent_profile.py` с исторического имени на произвольные profile-like переменные (`FOO_PROFILE`, `APP_PROFILE`, `PROFILE`); verify: тест проходит; verify: grep `NANOBOT_PROFILE tests/` не находит совпадений

## 4. Активные спеки

> **ВНИМАНИЕ (архивирование):** дельты из `specs/**` этого change УЖЕ
> применены вручную к активным спекам (шаги 4.1–4.2). Поэтому
> `openspec archive` выдаст `ADDED failed for header ... already exists`.
> Перед архивированием нужно либо (а) удалить применённые
> `## ADDED Requirements` блоки из дельт этого change, либо (б) откатить
> ручное применение и выполнить архивирование штатно. Вариант (б)
> предпочтителен — он сохраняет канонический путь OpenSpec.

- [x] 4.1 Применить дельту `configuration/profiles` к `openspec/specs/configuration/profiles/spec.md` — name-agnostic invariant, раздельные контракты gateway/CLI, удаление требований «Application entrypoint requires --profile» и «All application entrypoints share identical lifecycle contract»; verify: `openspec validate remove-profile-environment-selection` зелёный
- [x] 4.2 Применить дельту `runtime/entrypoints` к `openspec/specs/runtime/entrypoints/spec.md` — требование `ApplicationContext.create` MUST NOT принимать `profile`, guard для production entrypoints, name-agnostic сценарий CLI; verify: grep `NANOBOT_PROFILE openspec/specs/` не находит совпадений

## 5. Документация

- [x] 5.1 `docs/PROFILES.md` — убрать упоминания исторического имени env-переменной, исправить устаревшие CLI-примеры (`python cli_agent.py --profile=prod`), зафиксировать схему «gateway: `--profile`; CLI: fixed `test`; environment: не участвует в выборе профиля»; verify: grep `NANOBOT_PROFILE docs/PROFILES.md` пусто
- [x] 5.2 `docs/MIGRATION.md` — привести раздел миграции к фактической архитектуре, убрать пример `python cli_agent.py -P --profile=<prod|test>`; verify: grep `NANOBOT_PROFILE docs/MIGRATION.md` пусто
- [x] 5.3 `docs/INTERNAL_API.md` — убрать историческое имя из таблицы деплой-обвязки; verify: grep `NANOBOT_PROFILE docs/INTERNAL_API.md` пусто
- [x] 5.4 `docs/README.md` — исправить пример запуска CLI с `--profile`; verify: grep `cli_agent.py.*--profile` пусто
- [x] 5.5 `README.md` — исправить примеры `python cli_agent.py -P --profile=prod` / `--profile=prod`; verify: grep `cli_agent.py.*--profile` пусто
- [x] 5.6 `AGENTS.md` — переформулировать строку про устаревшую env-переменную в name-agnostic вид; verify: grep `NANOBOT_PROFILE AGENTS.md` пусто
- [~] 5.7 ~~Добавить запись в `CHANGELOG.md`~~ — ОТМЕНЕНО по решению пользователя: scope ограничен активными кодами/спеками/документацией. `CHANGELOG.md` содержит исторические записи о release'ах, к которым этот change не относится; правка исторического changelog не входит в задачу (см. design D5). Запись о BREAKING-изменении `create(profile=...)` будет добавлена автором вручную при подготовке следующего релиза.

> **Дополнительно (сверх плана):** в `docs/PROFILES.md` исправлены две
> диаграммы lifecycle (стр. ~75 и ~363), которые утверждали, что ВСЕ
> entrypoint'ы парсят `--profile` как обязательный — это противоречило
> фиксированному профилю CLI.

## 6. Verification

- [x] 6.1 `python -m pytest tests/test_profile_lifecycle.py tests/test_cli_agent_profile.py tests/test_application_context_role.py tests/test_cli_agent.py -q` — зелёный прогон; verify: 0 failed
      → **110 passed, 5 skipped** (без `test_cli_agent.py`, см. 6.7); полный набор с ним — 128 passed + 1 преднастоящее падение (см. 6.7)
- [x] 6.2 `python -m pytest tests/ -q --collect-only` — количество собираемых тестов зафиксировано; verify: нет collection errors
      → **3960 tests collected**, 0 collection errors
- [x] 6.3 `openspec.cmd validate remove-profile-environment-selection` — зелёный; verify: exit 0
      → `Change 'remove-profile-environment-selection' is valid`
- [x] 6.4 `python tools/architecture_guard.py` — зелёный; verify: exit 0
      → exit 0, пустой вывод
- [x] 6.5 `python tools/validate_component_specs.py` — зелёный; verify: exit 0
      → ⚠️ **exit 1, но ПРЕДНАСТОЯЩИЙ (не регрессия этого change)**. Валидатор требует русские
      заголовки (`## Назначение`, `## Ответственность`, …), тогда как capability-спеки
      по конвенции проекта используют английские (`## Purpose`, `## Boundary`, …).
      Проверено через `git stash`: baseline (без правок этого change) даёт
      **идентичные 192 строки вывода и тот же exit 1**.
      Затронуты спеки, не относящиеся к change: `upgrade-compatibility`,
      `validation/component-spec-validation`. См. § Известные расхождения.
- [x] 6.6 Финальная проверка инварианта: `grep -r NANOBOT_PROFILE lib/ workspace/ tools/ config.py gateway.py cli_agent.py streamlit_app.py tests/ openspec/specs/ docs/ README.md AGENTS.md` — пусто; verify: 0 совпадений (CHANGELOG.md и `openspec/changes/archive/` исключены сознательно, см. design D5)
      → **0 совпадений** (exit 1 от grep = PASS)

## 7. Известные расхождения (НЕ в scope этого change)

1. **`tests/test_cli_agent.py::TestHookLoader::test_finds_workspace_hooks_without_hooks_dir_in_syspath`** —
   падает на master. Причина: `lib/cli/hook_loader.py` ввёл allowlist имён хуков,
   а тест регистрирует фиктивный `my_hook.py`, который в allowlist отсутствует.
   Проверено: `git diff HEAD -- lib/cli/hook_loader.py tests/test_cli_agent.py` пуст
   (ни я, ни параллельный агент эти файлы не меняли). Требует отдельной правки.

2. **`enable_cron` default в спеке vs коде** — `openspec/specs/runtime/entrypoints/spec.md`
   (стр. 47) утверждает `enable_cron=True`, но `lib/core/application_context.py::_resolve_enable_kwargs`
   возвращает `False` (дефолт `bool(gateways.get("enable_cron", False))`).
   Расхождение существовало ДО этого change и не относится к выбору профиля.
   Требует отдельного решения (либо правка спеки, либо правка кода).

3. **`validate_component_specs.py` требует русские заголовки компонентных спек** —
   конфликтует с англоязычными capability-спеками (см. 6.5). Глобальная
   несовместимость инструмента с конвенцией проекта.
