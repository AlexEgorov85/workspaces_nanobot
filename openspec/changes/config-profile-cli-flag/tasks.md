## Phase A — Configuration Core

### A.1 Удалить module-level construction

Удалить строки `_ACTIVE_PROFILE = _resolve_mode()` и
`SETTINGS = resolve_application_config(...)` из `config.py:517-518`.
Поведенческий критерий: после запуска `python -c "import config;
print('ok')"` никакие merge-шаги (`project.json`, profile overlay,
profile-related secrets) для profile resolution не выполняются.

Это НЕ запрещает `os.environ`-чтение для **других** legitimate
целей (resolve `SECRETS_FILE_PATH`, `LOG_LEVEL` или похожих
не-profile-related переменных). Change ограничивается именно
**profile resolution** — другие env-переменные могут
использоваться компонентами ниже слоя config как обычно.

### A.2 Удалить `_resolve_mode()` полностью

После удаления env-чтения она сводится к whitelist-валидации,
которая встраивается в `_initialize_settings` (defensive re-validation).
Функция `_resolve_mode()` удаляется из `config.py` без замены.
`_ACTIVE_PROFILE` global, ссылающийся на неё, тоже удаляется.

**Зачем whitelist проверяется дважды** (CLI-entrypoint + `_initialize_settings`):
если в будущем кто-то добавит новый application entrypoint или
internal caller, который вызывает `_initialize_settings` напрямую
(минуя CLI-парсинг), defensive whitelist на уровне
`_initialize_settings` ловит невалидный profile ещё до того,
как ConfigurationResolver выполнит merge. CLI-валидация и
`_initialize_settings`-валидация — это **два независимых boundary'а**
с одной responsibility каждый (CLI error UX vs invariant protection).

### A.3 Реализовать `_initialize_settings(profile)` и `_LazySettings`

- `_initialize_settings(profile: str) -> None`:
  - **первая проверка — lifecycle state:** если `_inner_dict` уже
    заполнен → `ConfigurationError("SETTINGS already initialized")`
    (это вызывается при ЛЮБОМ втором вызове, независимо от значения
    profile, согласно spec «second call with any value»);
  - **вторая проверка — whitelist:** если `profile not in {"prod",
    "test"}` → `ConfigurationError("profile='<value>' is not
    supported (allowed: prod, test)")`;
  - только после обеих проверок успешно — вызвать
    `resolve_application_config(profile=profile)` и сохранить
    результат в `_LazySettings._inner_dict`;
  - **не возвращает значение** (`-> None`). Результат доступен только
    через `SETTINGS` после успешного вызова (side-effect publishing).
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
config._initialize_settings("test")            # → "already initialized" (lifecycle wins)
config._initialize_settings("prod")            # → "already initialized" (any value)
config._initialize_settings("dev")             # → "already initialized" (any value, even wrong whitelist)

# Whitelist test must run in a fresh process (subprocess-based):
#   python -c "import config; config._initialize_settings('dev')"
#   → ConfigurationError("profile='dev' is not supported")
# Reason: in-process reset is not a valid operation for SETTINGS
# proxy (already-imported `from config import SETTINGS` references
# old proxy, NOT the reset one). Use subprocess isolation for
# whitelist-only test scenarios; do not invent a reset API.
```

### A.4 Удалить избыточную ctx-пересборку в `ApplicationContext.create()`

`lib/core/application_context.py:121-126` после фикса не нужна —
`_ACTIVE_PROFILE` больше нет. Заменяется одной строкой:
`ctx.settings = SETTINGS` после `_initialize_settings` уже
выполненного entrypoint'ом.

Поведенческий критерий: после `python gateway.py --profile=prod`,
`ctx.profile == "prod"` И `ctx.settings["profile"] == "prod"`
(одно значение, не два).

### A.5 Remove profile environment resolution

Profile SHALL be supplied only via explicit argument to
`_initialize_settings(profile)`. The change SHALL remove all
profile resolution through environment variables: any
`os.environ.get(...)`, `os.environ.setdefault(...)`,
`os.environ[...] = ...` call used for profile acquisition or
persistence SHALL be removed. Behavioral criterion: subprocess
invocation of `config` with arbitrary external environment
variables SHALL NOT influence `_initialize_settings` —
`_initialize_settings("prod")` yields `SETTINGS["profile"]=="prod"`
regardless of env.

## Phase B — Application Entrypoints

### B.1 `gateway.py`

Перенести парсинг `--profile` на module-level (или в
`if __name__ == "__main__":`, выполняемый до импортов), до
`from lib.core.application_context import ...`. Использовать
`argparse.ArgumentParser(add_help=False)` + `.parse_known_args()`.

**Контракт обработки ошибок** (см. также Error Lifecycle Contract
в spec.md):

- **Внутренний контракт** — exception: missing/invalid profile
  РАЗ `ConfigurationError(...)`. Не `sys.exit(2)` напрямую.
- **Внешний контракт** — process exit: entrypoint top-level
  перехватывает `ConfigurationError` через `try/except` и
  конвертирует в `sys.exit(2) + stderr message`.

Whitelist и required-валидация:

- `profile is None` (no `--profile`) →
  `raise ConfigurationError("--profile is required")`.
- `profile not in {"prod", "test"}` →
  `raise ConfigurationError(f"--profile={profile!r} is not supported (allowed: prod, test)")`.

Эти ошибки возникают **внутри** startup-блока; `try/except` стоит
на уровне `main()` или непосредственно вокруг startup-блока, и
в catch'е делает `sys.stderr.write(...)` + `sys.exit(2)`.

**Структурный паттерн** (не literal код):

```python
# 1. На самом верху (module-level):
import sys
import config as _cfg
from config import ConfigurationError  # re-exported


def _entrypoint_main():
    """Startup + application body, raises ConfigurationError on startup errors."""
    # 2. Парсинг argv:
    profile = _parse_profile_arg()  # argparse
    if profile is None:
        raise ConfigurationError("--profile is required")
    if profile not in {"prod", "test"}:
        raise ConfigurationError(
            f"--profile={profile!r} is not supported (allowed: prod, test)"
        )
    # 3. _initialize_settings (запускается lifecycle, не возвращает значение)
    _cfg._initialize_settings(profile=profile)
    # 4. Только теперь runtime-импорты и инициализация приложения:
    from lib.core.application_context import ApplicationContext
    ...
    return ApplicationContext.create(...)


if __name__ == "__main__":
    try:
        _entrypoint_main()
    except ConfigurationError as exc:
        sys.stderr.write(f"FATAL: {exc}\n")
        sys.exit(2)
```

Ключевой момент: **внутри** startup-блока — **исключения**,
а **exit code 2** — **только** в entrypoint boundary. Прямых
`sys.exit(2)` из validation-проверок НЕТ.

### B.2 `cli_agent.py`

`cli_agent.py` использует **тот же** startup lifecycle и **тот же**
contract `ConfigurationError` → exit code 2, что и `gateway.py`
(см. B.1). Никакой отдельной exception policy для `cli_agent.py`
нет: ни прямой `sys.exit(2)` из validation-проверок, ни
отдельный exit-code; всё проходит через один и тот же
`try/except ConfigurationError` boundary на верхнем уровне.

### B.3 `streamlit_app.py`

Файл уже выполняет `from config import SETTINGS` на module level
(строка 31). Streamlit имеет **особый lifecycle**, отличный от
нормальных entrypoint'ов: модуль импортируется ОДИН раз
при первом `streamlit run`, но `st.rerun()` **re-executes
скрипт повторно** через runpy (`streamlit.runtime.scriptrunner`).
Это значит, что module-level statements в `streamlit_app.py`
выполняются заново при каждом `st.rerun()`. Если просто
поместить `_initialize_settings(profile)` на module level,
**второй `st.rerun()` упадёт** на
`ConfigurationError("SETTINGS already initialized")` — что
нарушит spec scenario «second call with any value → already
initialized».

Решение: **guard вокруг вызова** на module level, не изменяя
внутренний lifecycle `_initialize_settings`:

```python
# streamlit_app.py, на самом верху (выше существующих импортов)
import sys as _streamlit_sys

_app_argv_profile = None
if "--profile=" in _streamlit_sys.argv:
    _idx = _streamlit_sys.argv.index("--profile=")
    _app_argv_profile = _streamlit_sys.argv[_idx + 1] if _idx + 1 < len(_streamlit_sys.argv) else None
elif "--profile" in _streamlit_sys.argv:
    _idx = _streamlit_sys.argv.index("--profile")
    _app_argv_profile = _streamlit_sys.argv[_idx + 1] if _idx + 1 < len(_streamlit_sys.argv) and "=" not in _streamlit_sys.argv[_idx + 1] else None

if not _app_argv_profile:
    raise ConfigurationError("--profile is required")
if _app_argv_profile not in {"prod", "test"}:
    raise ConfigurationError(f"--profile={_app_argv_profile!r} is not supported (allowed: prod, test)")

import config as _streamlit_cfg

# Guard against st.rerun() re-execution: skip second call.
# _initialize_settings itself stays strict (second call with any
# value -> "already initialized"); this guard prevents the second
# CALL from happening, not the second response from the function.
# Use globals() rather than sys.modules["streamlit_app"].__dict__
# to set the flag: globals() refers to module's own namespace,
# which is the supported way to set per-module attributes from
# within module-level code.
_PROFILE_GUARD_ATTR = "_config_initialized"
if not globals().get(_PROFILE_GUARD_ATTR, False):
    _streamlit_cfg._initialize_settings(profile=_app_argv_profile)
    globals()[_PROFILE_GUARD_ATTR] = True
```

Контракт по-прежнему: entrypoint initializes SETTINGS **ровно один раз**
на startup, и любой второй вызов `_initialize_settings(...)`
**из любого контекста** остаётся ошибкой. Guard на стороне
streamlit_app — это **явная защита от re-execution**, а не
изменение lifecycle-gate'а. Это принципиально отличается от
«auto-init / profile switching»: lifecycle остаётся строгим,
guard просто не вызывает функцию повторно.

Behavioral acceptance: `st.rerun()` НЕ приводит к
`ConfigurationError("already initialized")` — guard видит
`_initialized = True` и пропускает. CLI-launch через
`streamlit run` (даже если он внутренне делает re-execution
при первом connect) проходит без второго вызова.

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
- **`test_import_has_no_profile_resolution_side_effects`**:
  `import config` не вызывает profile resolution, `config.SETTINGS`
  остаётся uninitialized; `_initialize_settings("prod")` затем
  даёт `SETTINGS["profile"]=="prod"` независимо от env. Тест
  проверяет архитектурный контракт («environment не участвует
  в profile resolution»), а не исторические переменные.

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
- `test_gateway_profile_comes_only_from_cli`:
  `python gateway.py --profile=prod` с произвольным набором env
  vars в parent (любые unrelated vars) →
  `SETTINGS["logging"]["db"]["table_name"] == "agent_gateway_logs"`
  (prod); `SETTINGS["profile"] == "prod"`; баннер говорит `prod`.
  Тест проверяет архитектурный контракт «environment не
  используется для передачи профиля», без ссылки на конкретные
  исторические имена.
- `test_cli_agent_happy_path`: `python cli_agent.py --profile=prod`
  → `SETTINGS["logging"]["db"]["table_name"] == "agent_gateway_logs"`
  (prod); `SETTINGS["profile"] == "prod"`. Один happy path
  подтверждает, что `cli_agent.py` использует тот же lifecycle
  и тот же resolved configuration, что и `gateway.py`; остальные
  edge cases (no-profile, invalid-profile, exit-2) одинаковы
  для всех entrypoint'ов благодаря общему контракту и
  покрываются на gateway.

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

### D.5 Тесты `ApplicationContext` без mock на `_initialize_settings`

Тесты `ApplicationContext.create()` НЕ мокают `_initialize_settings`.
`_initialize_settings` — это lifecycle-gate с side-effect публикацией
`SETTINGS`, а не функция с return value; mock на неё либо бесполезен
(mock не выполняет реальную функцию → `SETTINGS` остаётся uninitialized
и `ApplicationContext.create()` падает на `ConfigurationError`), либо
воспроизводит side-effect через `side_effect=...`, что эквивалентно
вызову реальной функции. Поэтому mock `_initialize_settings` — антипаттерн.

Правильная стратегия:

1. Тест **явно** вызывает `config._initialize_settings(profile)`
   перед `ApplicationContext.create()` (либо в `setup`-фикстуре,
   либо в самом теле теста). Это — публикация реального `SETTINGS`,
   а не подмена.
2. После явного вызова тест проверяет, что `ApplicationContext.create()`
   использует уже инициализированный `SETTINGS` без повторной
   сборки конфигурации. Никаких вызовов `_initialize_settings` со
   стороны `ApplicationContext` не ожидается и не должно быть
   (lifecycle-gate был вызван раньше, entrypoint'ом; см. design.md
   Decision 0/1).
3. Если ранее тест проверял конкретный configuration-input
   (например, какой именно dict отдаёт resolver), то мокать нужно
   сам resolver (`resolve_application_config`), а не
   `_initialize_settings`. Тесты на lifecycle и тесты на resolver —
   это **разные** уровни, и смешивать их через один mock — ошибка.
4. Старые mock'и (`_resolve_mode` / `resolve_application_config`
   на уровне ApplicationContext) удаляются; если нужен resolver mock,
   он делается точечно и явно.

Diff по сути сводится к **удалению** mock'ов на `_initialize_settings`
(≤ 30 строк negative-diff). Никаких новых mock'ов на lifecycle-gate
не требуется.

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

Smoke-test запускает **`gateway.py` через `--profile=<v>` с
**bounded harness'ом**, чтобы избежать полноценного infinite-loop
runtime (gateway стартует postgres channel polling, websocket
listener, streamlit subprocess и т.п., которые при smoke-тесте
должны быть остановлены сразу после проверки конфигурации).

Конкретно:

1. **Smoke-CLI mode в `gateway.py`** (флаг `--smoke` или
   внутренний `GATEWAY_SMOKE=1` env, или просто проверка через
   subprocess с timeout): при `--smoke` gateway НЕ стартует
   channels/services/event-loop. Только:
   - Парсит `--profile`;
   - Инициализирует `SETTINGS`;
   - Печатает в stdout баннер `profile=<v>` и
     `SETTINGS["logging"]["db"]["table_name"]`;
   - Печатает `OK_SMOKE_COMPLETE` и выходит с кодом 0.

   Эта функция специально добавлена для Phase F и для D.2
   integration test'а; production invocation **не** использует
   `--smoke`. Это позволяет тестам получить реальный
   `SETTINGS["logging"]["db"]["table_name"]` без поднятия
   postgres channel / websocket listener.

2. Сценарии Phase F запускаются как
   `subprocess.run(["python", "gateway.py", "--profile=prod", "--smoke"],
                   timeout=10, capture_output=True)`:
   - `python gateway.py --profile=prod --smoke` →
     stdout содержит `OK_SMOKE_COMPLETE`, exit code 0,
     `SETTINGS["logging"]["db"]["table_name"] == "agent_gateway_logs"`
     в выводе.
   - `python gateway.py --profile=test --smoke` → test-таблица,
     без `db_error` в выводе.
   - Произвольная устаревшая env var + `--profile=prod --smoke` →
     prod-таблица, env проигнорирован.
   - `python gateway.py --smoke` (без `--profile`) → exit 2 +
     stderr содержит `--profile is required`.
   - `python gateway.py --profile=dev --smoke` → exit 2 +
     `--profile='dev' is not supported`.

3. `history_search` end-to-end test (отдельный scenario, не в
   Phase F): он не требует `--smoke`, потому что это уже
   **отдельный subprocess, выполняющий tool** — бесконечный
   runtime gateway'а ему не нужен.

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
    возвращает только разрешённые категории:
    - Negative-test fixtures (тесты, специально проверяющие
      игнорирование неизвестной env var через произвольный
      набор env vars; тест НЕ создаёт/упоминает конкретное
      историческое имя);
    - REMOVED-секция OpenSpec spec.md (как описание удалённого
      контракта);
    - `proposal.md` разделы «Context» / «Why» / «Impact» в стиле
      «что мы убираем» (явно historic, не runtime-механизм);
    - `tasks.md` Phase E (deployment-миграция, historic).
    Запрещены в runtime-коде, в runtime-acceptance scenarios,
    в test names, в requirements, и в любых местах, где env var
    может выглядеть как действующий механизм (а не как
    описание удалённого).
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
