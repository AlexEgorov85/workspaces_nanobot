## Phase A — Configuration Core

### A.1 Удалить module-level construction

Удалить строки `_ACTIVE_PROFILE = _resolve_mode()` и
`SETTINGS = resolve_application_config(...)` из `config.py:517-518`.
Поведенческий критерий: после запуска `python -c "import config;
print('ok')"` никакие merge-шаги (`project.json`, profile overlay,
secrets) не выполняются — это видно по отсутствию побочных эффектов
(нет печати в лог, нет обращения к БД, нет `os.environ`-чтения).

### A.2 Удалить `_resolve_mode()` полностью

После удаления env-чтения она сводится к whitelist-валидации,
которая встраивается в `_initialize_settings` (defensive re-validation).
Функция `_resolve_mode()` удаляется из `config.py` без замены.
`_ACTIVE_PROFILE` global, ссылающийся на неё, тоже удаляется.

### A.3 Реализовать `_initialize_settings(profile)` и `_LazySettings`

- `_initialize_settings(profile)`:
  - проверяет `profile in {"prod", "test"}` → иначе `ConfigurationError`;
  - проверяет, что `_inner_dict` ещё не заполнен → иначе
    `ConfigurationError("SETTINGS already initialized")`;
  - вызывает `resolve_application_config(profile=profile)` и
    сохраняет результат в `_LazySettings._inner_dict`.
- `_LazySettings`:
  - UNINITIALIZED state (initial): `__getitem__`, `__getattr__`,
    `.get()` бросают `ConfigurationError("SETTINGS not initialized:
    call _initialize_settings(profile) from the entrypoint")`;
  - INITIALIZED state: `__getitem__`/`__getattr__`/`.get()` —
    passthrough к `_inner_dict`;
  - **никакого** auto-init при чтении.
- Mapping access — canonical: `SETTINGS["profile"]`. `__getattr__`
  оставлен только для backward-compat с существующим кодом
  вроде `streamlit_app.py:33` (`getattr(SETTINGS, "channels", {})`).

Поведенческий критерий:

```python
import config                                  # → no env reads, no init
config.SETTINGS["profile"]                     # → ConfigurationError
config._initialize_settings("prod")
config.SETTINGS["logging"]["db"]["table_name"] # → "agent_gateway_logs"
config.SETTINGS["profile"]                     # → "prod"
config._initialize_settings("test")            # → ConfigurationError "already initialized"
config._initialize_settings("dev")             # → ConfigurationError "not supported"
```

### A.4 Удалить избыточную ctx-пересборку в `ApplicationContext.create()`

`lib/core/application_context.py:121-126` после фикса не нужна —
`_ACTIVE_PROFILE` больше нет. Заменяется одной строкой:
`ctx.settings = SETTINGS` после `_initialize_settings` уже
выполненного entrypoint'ом.

Поведенческий критерий: после `python gateway.py --profile=prod`,
`ctx.profile == "prod"` И `ctx.settings["profile"] == "prod"`
(одно значение, не два).

### A.5 Удалить любое чтение/запись профильных env vars в `config.py`

Убрать любые `os.environ.get(...)`, `os.environ.setdefault(...)`,
`os.environ[...] = ...` вызовы для получения или установки профиля
(включая устаревшие env var, упоминавшиеся в deployment descriptors
как `NANOBOT_PROFILE`). После Phase A поведенческий критерий:
subprocess-вызов `config` с любой env var, содержащей профильное имя,
не влияет на `_initialize_settings` — `_initialize_settings("prod")`
даёт `SETTINGS["profile"]=="prod"` независимо от env.

## Phase B — Application Entrypoints

### B.1 `gateway.py`

Перенести парсинг `--profile` на module-level (или в
`if __name__ == "__main__":`, выполняемый до импортов), до
`from lib.core.application_context import ...`. Использовать
`argparse.ArgumentParser(add_help=False)` + `.parse_known_args()`.
Whitelist и required-валидация — через прямые проверки
(`profile in {"prod", "test"}`), затем — вызов
`config._initialize_settings(profile=...)`.

`main()` оборачивается в `try/except ConfigurationError`, который
в catch'е делает `sys.stderr.write(...)` + `sys.exit(2)`.

**Структурный паттерн** (не literal код):

```python
# 1. На самом верху (module-level):
import sys
if __name__ == "__main__":
    # 2. Парсинг argv:
    profile = _parse_profile_arg()  # argparse
    # 3. Whitelist и required проверка ДО import config / _initialize_settings:
    if profile not in {"prod", "test"}:
        ...  # ConfigurationError + sys.exit(2)
    # 4. Импорт config и явная инициализация:
    import config as _cfg
    _cfg._initialize_settings(profile=profile)
    # 5. Только теперь runtime-импорты:
    from lib.core.application_context import ApplicationContext
    ...
    # 6. main() в try/except ConfigurationError.
```

### B.2 `cli_agent.py`

`cli_agent.py` использует **тот же** startup lifecycle и **тот же**
contract `ConfigurationError` → exit code 2, что и `gateway.py`
(см. B.1). Никакой отдельной exception policy для `cli_agent.py`
нет: отсутствие `--profile` или unsupported profile — это
`ConfigurationError` + exit code 2 так же, как в `gateway.py`.

### B.3 `streamlit_app.py`

Файл уже выполняет `from config import SETTINGS` на module level
(строка 31). Перенести инициализацию профиля **выше** этой строки:

- Парсить `--profile=<v>` из `sys.argv` (после `--`) на самом
  верху файла.
- Whitelist и required проверки.
- `import config as _cfg; _cfg._initialize_settings(profile=<v>)`.
- Дальше — текущий код без изменений.

Принцип: **module-level блок streamlit'а выполняется ОДИН раз**
при первом `streamlit run`; `st.rerun()` re-executes скрипт
**без переимпорта модуля**, поэтому инициализация должна быть на
module-level (а не в `if __name__ == "__main__":` — для streamlit-run
это условие не сработает как для CLI).

### B.4 Application subprocess получает `--profile` через argv

`lib/services/subprocess_manager.py:83-89` сейчас спавнит Streamlit
UI через `subprocess.Popen(...)` без явного `--profile=<v>`. После
фикса entrypoint'ов это **обязательное** изменение — иначе
spawned Streamlit упадёт с `ConfigurationError("--profile is required")`
на module-level и не стартует вовсе.

Конкретно:
- `argv` child включает `--profile=<value>` из `SETTINGS["profile"]`
  родителя;
- никакая env-переменная не используется для передачи профиля
  (это и есть **отсутствие** sanitization в этом change — приложение
  просто **не работает** с legacy env var'ами).

Это **не отдельная фаза**: это просто приложение B.1-B.3 к
runtime-коду, который спавнит application entrypoint. Никаких
дополнительных helper'ов, никакого `_build_application_child_env()`,
никакого специального boundary для env var — потому что
приложение вообще **не читает** устаревшие env vars (после Phase A).

## Phase D — Tests

### D.1 Configuration core behavior tests

В `tests/test_config_resolver.py` — один тест-класс, проверяющий
три сценария через **импорт поведения**, не через `grep`:

- **`test_settings_lazy_init_requires_init`**: `import config;
  config.SETTINGS["x"]` → `ConfigurationError("SETTINGS not initialized")`;
  затем `config._initialize_settings("prod")`; затем
  `config.SETTINGS["logging"]` доступен.
- **`test_double_init_fails`**: `config._initialize_settings("prod")`
  → `config._initialize_settings("test")` → `ConfigurationError("already
  initialized")`.
- **`test_invalid_profile_rejected`**: `config._initialize_settings("dev")`
  → `ConfigurationError("profile='dev' is not supported")`. Никакая
  env-переменная не влияет на результат.
- **`test_import_has_no_env_side_effects`**: `import config` в
  subprocess с произвольной env var, содержащей профильное имя;
  проверяем, что env остаётся неизменным (config не дёргает
  `os.environ`) и `config.SETTINGS` остаётся uninitialized.

### D.2 Application entrypoint CLI tests

Subprocess-вызовы entrypoints, проверяющие реальное поведение,
а не содержимое stderr:

- `test_gateway_no_profile_exits_2`: `python gateway.py` → process
  exits with code 2; imports in `gateway.py` НЕ выполнились
  (например, `nanobot` модуль не загружен в child sys.modules).
- `test_gateway_invalid_profile_exits_2`: `python gateway.py
  --profile=dev` → exit 2, `nanobot` не импортирован.
- `test_gateway_prod_sets_agw_table`:
  `python gateway.py --profile=prod` (с минимальным smoke-CLI в
  gateway — отдельный sub-task D.2.5) →
  `SETTINGS["logging"]["db"]["table_name"] == "agent_gateway_logs"`
  И `SETTINGS["profile"] == "prod"` (это **integration test** —
  не только баннер).
- `test_gateway_unknown_env_var_ignored`:
  произвольная env var с профильным именем (например, исторически
  упоминавшаяся как `NANOBOT_PROFILE=test`) +
  `python gateway.py --profile=prod` →
  `SETTINGS["logging"]["db"]["table_name"] == "agent_gateway_logs"`
  (prod). И `SETTINGS["profile"] == "prod"`. Баннер тоже
  говорит `prod`.
- `test_cli_agent_*`: аналогичные 4 кейса для `cli_agent.py`.

### D.3 Streamlit invocation tests

В subprocess-запуске с реальным `streamlit run`:

- `test_streamlit_no_profile_exits_2`:
  `streamlit run streamlit_app.py` → exit 2 до того, как streamlit
  начинает отдавать страницу (например, проверяем, что в stdout/stderr
  есть `ConfigurationError("--profile is required")`).
- `test_streamlit_profile_prod_starts`:
  `streamlit run streamlit_app.py -- --profile=prod` → streamlit
  начинает стартовать (можно проверить по наличию "You can now view"
  в stderr или по открытому порту через короткий timeout); SETTINGS
  при первом обращении из `streamlit_app.py` (можно через `exec`-тест
  подменить `streamlit run` минимальным harness'ом) имеет
  `["profile"] == "prod"` И runtime-таблицу prod.

### D.4 Application subprocess получает `--profile` через argv

Минимальный integration test: parent entrypoint с
`SETTINGS["profile"]="prod"` спавнит Streamlit UI (через
`SubprocessManager` или `streamlit run` напрямую); subprocess
получает `--profile=prod` в `argv`. Чисто runtime-observation:
никакие env-переменные не проверяются — тест проверяет, что
**argv** правильный, не env.

- `test_streamlit_subprocess_argv_contains_profile`: parent
  инициализирует `config._initialize_settings("prod")` →
  вызывает `SubprocessManager.spawn_streamlit(script)`;
  child `streamlit_app.py` стартует, печатает `sys.argv`
  в лог → parent читает лог и проверяет, что
  `--profile=prod` присутствует.

### D.5 Mock-переделка `test_application_context.py:110-127`

Переписать mocks: вместо `_resolve_mode` / `resolve_application_config`
— mock `_initialize_settings`. Каждый mock возвращает stub `cfg`,
который используется в `ApplicationContext.create()`. Diff ≤ 30 строк.

### D.6 Полный прогон

`pytest -q` зелёный; ≥ 1480 passed (baseline AGENTS.md); без
новых skipped/xfail. `autouse-fixture` для `_initialize_settings`
**не** добавляется.

### D.7 Detect unsupported early SETTINGS access в standalone utilities

Только для файлов, которые **реально** импортируют `SETTINGS`
на module level (через простой grep с `-l`); для каждого —
subprocess-вызов **без предварительной инициализации** и проверка,
что процесс завершается с `ConfigurationError("SETTINGS not
initialized")`. Это negative test: фиксирует, что lazy proxy
корректно ловит случайный standalone-запуск. Не список из 7
файлов ради списка — только те, что реально используют.

## Phase E — Documentation

### E.1 `docs/PROFILES.md`

- Переработать «Запуск» под CLI-флаг (whitelist, обязательность).
- Переработать «Миграция существующих деплоев» как таблицу
  «устаревшая env var» → `command: python gateway.py --profile=...`.
- Удалить секцию «Cron» (env-инструкция).
- Добавить секцию «Что изменилось в этом релизе».
- Убрать упоминания устаревших env vars в тексте.

### E.2 Корневой `AGENTS.md`

- Убрать упоминание устаревшей env var в «Configuration» секции.
- Заменить на `python gateway.py --profile=prod`.

### E.3 `docs/INTERNAL_API.md`

- Секция «tools.exec»: явно зафиксировать, что env-переменные
  НЕ используются для передачи профиля; application subprocess
  получает `--profile` через `command` явно.
- Добавить секцию «streamlit invocation» с supported pattern
  `streamlit run streamlit_app.py -- --profile=prod`.

### E.4 `.github/workflows/*.yml`

- Заменить env-based профиль на `command: python gateway.py
  --profile=...` (или эквивалент).

### E.5 `CHANGELOG.md`

Секция `[Unreleased]`, категория `Changed`:
«Профиль конфигурации теперь определяется только CLI-флагом `--profile`
(whitelist: `prod`, `test`); env-переменные для передачи профиля более
не используются; application entrypoints (`gateway.py`, `cli_agent.py`,
`streamlit_app.py`) требуют обязательный `--profile` и без него падают
с `ConfigurationError` (exit 2). BREAKING для деплоев, использующих
env для передачи профиля — требуется миграция на
`command: python gateway.py --profile=prod`.»

## Phase F — Real-DB Smoke Test

- `python gateway.py --profile=prod` без env → баннер
  `profile=prod` И `SETTINGS["logging"]["db"]["table_name"] == "agent_gateway_logs"`.
  `history_search` отрабатывает на реальной таблице prod.
- `python gateway.py --profile=test` без env → test-таблица, без `db_error`.
- Произвольная устаревшая env var в окружении + `python gateway.py --profile=prod` →
  prod-таблица (env проигнорирован), баннер + runtime согласованы.
- `python gateway.py` → exit 2 + `ConfigurationError`, без runtime.

## Architectural Acceptance Checklist

`Change 'config-profile-cli-flag' is valid` остаётся зелёным после каждого
изменения артефактов. Реализация считается завершённой, когда:

### Lifecycle и ownership

- [ ] `config.py` не содержит module-level construction (`config.py`
      импортируется без побочных эффектов: ни merge, ни env-чтения,
      ни `_ACTIVE_PROFILE`, ни module-level `SETTINGS =`).
- [ ] `_resolve_mode()` удалён (поведенческая проверка: `import config;
      hasattr(config, "_resolve_mode")` → False).
- [ ] Whitelist профилей: `{"prod", "test"}`. `--profile=dev`,
      `--profile=staging`, `--profile=foo` → `ConfigurationError` +
      exit 2.
- [ ] `SETTINGS["k"]` до `_initialize_settings(...)` →
      `ConfigurationError("SETTINGS not initialized")`.
- [ ] Второй `_initialize_settings(...)` →
      `ConfigurationError("SETTINGS already initialized")`.
- [ ] Профиль иммутабелен после инициализации (нет смены профиля).
- [ ] `SETTINGS["profile"]` доступен как canonical API.

### Application entrypoints — единый contract

- [ ] `gateway.py`, `cli_agent.py`, `streamlit_app.py` требуют
      `--profile` и без него падают с `ConfigurationError` + exit 2.
- [ ] Все три entrypoint'а используют **одинаковый** lifecycle:
      parse → validate → `_initialize_settings` → runtime imports.
- [ ] Все три entrypoint'а переводят startup `ConfigurationError`
      в exit code 2 (нет «cli_agent без try/except»).
- [ ] Профиль инициализируется до любых runtime-импортов
      в application entrypoint'е.

### Legacy env var — полное удаление из runtime

- [ ] `config.py` не читает и не пишет **никакую** env var с
      профильным именем (ни `os.environ.get`, ни `setdefault`, ни
      прямое обращение).
- [ ] Runtime-код в `lib/` не передаёт профиль через env
      subprocess'ам (test fixtures, специально проверяющие
      отсутствие эффекта, — исключение).
- [ ] Документация, CI/deploy descriptors не используют env-based
      передачу профиля (после Phase E).
- [ ] Repository-wide search для любой профильной env var
      возвращает только:
  - Negative-test fixtures, специально проверяющие игнорирование;
  - REMOVED-секция OpenSpec (как описание удалённого контракта).
  - Никаких упоминаний в runtime-коде или в документации как
    действующей концепции.

### Application subprocess получает `--profile` через argv

- [ ] `SubprocessManager.spawn_streamlit` (или эквивалентный
      runtime call) передаёт `--profile=<v>` явно в argv
      child subprocess.
- [ ] Источник profile в argv — `SETTINGS["profile"]`
      родителя, не повторный resolve.

### Integration и observability

- [ ] Integration test (D.2) проверяет имя runtime-таблицы
      (`agent_gateway_logs` vs `agent_gateway_logs_test`), а не
      только баннер.
- [ ] Streamlit invocation зафиксирован:
      `streamlit run streamlit_app.py -- --profile=<v>`.
- [ ] Любая устаревшая env var в окружении + `gateway.py --profile=prod` →
      `SETTINGS["logging"]["db"]["table_name"] == "agent_gateway_logs"`.

### Anti-patterns

- [ ] `tests/conftest.py` autouse-fixture для `_initialize_settings`
      НЕ добавлен.
- [ ] Никакой profile-ветки в business logic (`grep -rn
      'profile.*==.*"prod"\|profile.*==.*"test"' lib/ workspace/
      tools/` → 0).
- [ ] Никакой `_resolve_mode()`-обвязки вокруг `_initialize_settings`
      (т.е. helper не вызывает «resolve» сам профиль — он принимает
      профиль как явный аргумент и валидирует whitelist).

## Definition of Done

OpenSpec считается готовой к реализации **только** когда одновременно
выполнены все 12 условий:

1. Единственный источник profile — CLI `--profile` (whitelist
   `prod`/`test`).
2. Единственная точка публикации `SETTINGS` —
   `_initialize_settings(profile)`.
3. Единственная внутренняя ошибка lifecycle/config —
   `ConfigurationError`.
4. Все application entrypoints имеют одинаковый startup/error
   contract (нет «cli_agent без try/except»).
5. Никакая профильная env var не используется для configuration
   resolution (ни в коде, ни в deploy descriptors, ни в доках).
6. Никакая профильная env var не существует как runtime-механизм
   (не вводится никакой application subprocess boundary для env
   sanitization — приложение просто не работает с такими vars).
7. Application subprocess получает profile через `--profile` в
   `argv`, не через env (acceptance test D.4
   `test_streamlit_subprocess_argv_contains_profile`).
8. Runtime integration tests проверяют фактическую configuration
   (имя runtime-таблицы, не баннер).
9. Нет fallback-механизма (no default profile, no env reading,
   no auto-init at SETTINGS access).
10. Нет autouse fixture, скрывающего новый lifecycle.
11. Repository-wide search для любой профильной env var
    возвращает только:
    - Negative-test fixtures (тесты, специально проверяющие
      игнорирование неизвестной env var);
    - REMOVED-секция OpenSpec (описание удалённого контракта).
    Никаких упоминаний в runtime-коде или в документации как
    действующей концепции.
12. `proposal.md`, `spec.md`, `design.md` и `tasks.md` описывают
    один и тот же механизм без взаимоисключающих требований
    (конфликтующих формулировок нет; cross-references согласованы).

Проверка №12 выполняется через `openspec.cmd validate config-profile-cli-flag --strict`
(зелёный) плюс ручное чтение критических путей:
«`cli_agent` имеет другой error lifecycle?»,
«существует ли runtime-санитизация env var, сохраняющая старую
концепцию под новым именем?»,
«standalone utilities обязаны принимать `--profile`?»,
«`_resolve_mode` ещё фигурирует как рабочий механизм?»,
«SETTINGS может создаваться при import?»,
«profile может быть получен из environment/default?»,
«entrypoint может создавать runtime до `_initialize_settings`?»,
«профильная env var где-то упоминается как механизм передачи
профиля (а не только как deprecated-историческая ссылка)?».

Если хотя бы один из этих вопросов имеет ответ «да» в текущем
состоянии артефактов — change возвращается на доработку до старта
implementation.
