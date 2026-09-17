## Context

`config.py:517-518` фиксирует `_ACTIVE_PROFILE` и `SETTINGS` на module-level
import через `_resolve_mode()` (читает `NANOBOT_PROFILE` из `os.environ`) и
`resolve_application_config(profile=_ACTIVE_PROFILE)`. Это происходит ДО
того, как `gateway.py` / `cli_agent.py` / `streamlit_app.py` успевают
распарсить `--profile=prod` через `argparse`. Банер и `ctx.profile`
получают правильное значение, но `from config import SETTINGS` отдают
test-версию: runtime-таблицы с `_test`-суффиксами. Инструменты
(`history_search`, `event_log`, `DbLoggingService`) ломаются по
`relation does not exist`.

`_resolve_mode()` уже принимает аргумент `profile_arg` (строки 232–247)
— CLI-флаг доходит до него через `gateway.py:67-70`, но **после** того,
как module-level сборка прошла. Сам по себе `_resolve_mode` имеет
корректный приоритет «CLI > env > default=test» (строки 238–241) и не
создаёт конкуренции источников — но применяется дважды: первый раз
на module-level (config.py:517, без аргумента), второй раз в
`main()` (gateway.py:70, с `args.profile`). Первый вызов фиксирует
`SETTINGS` слишком рано, до того, как `argparse` распарсил `--profile`;
второй — корректный, но приходит поздно и потому не влияет на уже
зафиксированный `SETTINGS` (влияет только на банер и `ctx.profile`).
Реальная проблема не в приоритете источников, а в моменте их
применения.

`ApplicationContext.create()` (`lib/core/application_context.py:121-126`)
уже содержит логику «если `profile != _ACTIVE_PROFILE`, пересобрать
`ctx.settings` через `resolve_application_config`» — это страховка
на случай расхождения, но она работает только для контекста, не для
глобального `config.SETTINGS`, на который ссылаются 182 места в коде.

Зафиксированный в `docs/PROFILES.md:97-110` принцип «профиль последний
→ runtime-таблицы immutable» формально продолжает выполняться, но
**сам профиль определяется неправильно** из-за module-level race.

## Goals / Non-Goals

**Goals:**

- Источник профиля один и только один — CLI-флаг `--profile`, без env-fallback.
- `SETTINGS` строится ленивым инициализатором под явный профиль, не
  на module-level import.
- Все существующие `from config import SETTINGS` продолжают работать
  без изменений (proxy-объект поддерживает тот же интерфейс).
- Entrypoints парсят `--profile` первой строкой файла, до любых
  импортов, читающих конфиг.
- Entrypoints без `--profile` падают с `ConfigurationError` на старте
  (нет silent default'а).
- `NANOBOT_PROFILE` env-переменная физически удаляется из кода и
  документации.

**Non-Goals:**

- Healthcheck / readiness-gate по профилю — отдельный тикет, в этом
  change не делается (только фиксируется как Open Question).
- Изменения в cron-процессах — их в репо нет, `_make_cron_service`
  инициализируется только при `enable_cron=True`, что не используется.
- Рефакторинг `ApplicationContext.create()` за пределы удаления
  избыточной логики пересборки `ctx.settings` (после фикса она больше
  не нужна).
- Поддержка третьего профиля (`dev`, `staging`).
- Изменение схем `agent_gateway_logs` или других runtime-таблиц.
- Изменение `validate_profile_overlay` / `validate_runtime_isolation` —
  они остаются без изменений.

## Decisions

### Decision 1: SETTINGS как lazy-init proxy-объект (а не удаление)

**Выбор:** Заменить module-level `SETTINGS = resolve_application_config(...)`
на `SETTINGS = _LazySettings()` с методом
`_initialize_settings(profile)`. При первом чтении (`__getitem__` /
`__getattr__`) proxy-объект проверяет, инициализирован ли внутренний
dict, и:
  - если да — возвращает значение;
  - если нет — бросает `ConfigurationError("SETTINGS not initialized")`.

Альтернативы рассмотрены:
- **Полное удаление `from config import SETTINGS`** — потребует переделки 182
  мест с явной инжекцией через `ApplicationContext`. Слишком дорого для
  одного фикса, отложено в «гораздо более крупный рефакторинг».
- **`SETTINGS` как `threading.local`-привязанный proxy** — не решает
  проблему, т.к. проблема в моменте инициализации, а не в
  многопоточности.
- **`SETTINGS` как proxy с автоинициализацией при первом обращении по
  module-level `_resolve_mode()` (lazy resolve из argv или env)** —
  именно то, что предлагалось в варианте Б плана, отвергнут: контракт
  хрупкий (любой `import config; SETTINGS["x"]` до entrypoint триггерит
  resolve не вовремя), прозрачность для отладки плохая.

**Обоснование:** proxy-объект максимально сохраняет обратную совместимость
с 182 импортами, при этом формально разделяет «когда SETTINGS готов»
и «что в нём лежит». Тесты с `patch("config.SETTINGS", _settings_with(...))`
продолжают работать (mock подменяет proxy, внутренний `_inner` тоже
прокидывается через атрибут).

### Decision 2: argparse на самом верху entrypoint'а

**Выбор:** В `gateway.py`, `cli_agent.py`, `streamlit_app.py` `_parse_args()`
переезжает в модульную область (на module-level), чтобы выполняться
**до** любых импортов конфиг-читающих модулей. Сейчас argparse вызывается
внутри `main()` после `from loguru import logger` и `from rich.console`
(`gateway.py:35-50`).

Альтернативы рассмотрены:
- **Pre-fork процесс с явной передачей через stdin/файл** — избыточно,
  argparse и так работает на argv.
- **Lazy-import все модули, читающие `SETTINGS`** — слишком дорогая
  переделка `nanobot`'s и `lib.core`, требует реверс-инжиниринга
  того, что именно дёргается из `init_app_context`.

**Обоснование:** argparse на module level выполняется **до любых**
`from ... import ...`, потому что import-time в Python выполняется
при первом обращении к модулю. Сейчас в `gateway.py:11-50` все импорты
верхнего уровня (`argparse`, `os`, `sys`, `traceback`, `pathlib`,
`loguru`, `rich.console`, `lib.core.application_context`) — из них
`application_context` транзитивно импортирует `config`, и отсюда
начинается гонка. Если перенести парсинг argv выше `from loguru import
logger`, то argparse запустится раньше и `_resolve_mode(args.profile)` уже
сможет направить entrypoint в правильную ветку. Это сложнее текущего
кода (импорты под условием), но прямой и понятный фикс.

**Конкретный паттерн:**

```python
# gateway.py (module-level, до любых импортов)
import sys

if __name__ == "__main__":
    import argparse
    _parser = argparse.ArgumentParser(add_help=False)
    _parser.add_argument("--profile", type=str, default=None)
    _args, _ = _parser.parse_known_args()
    if not _args.profile:
        sys.stderr.write("FATAL: --profile is required\n")
        sys.exit(2)
    import config as _cfg
    _cfg._initialize_settings(profile=_args.profile)
    # дальше все импорты
```

Аналогичные блоки в `cli_agent.py` и `streamlit_app.py`.
Streamlit особенный — он стартует через `streamlit run`, argv может
передаваться иначе; для него отдельная подзадача в tasks (проверить
через `sys.argv` после `bootstrap`).

### Decision 3: Передача профиля через явный `_initialize_settings`, не атрибут

**Выбор:** Функция `config._initialize_settings(profile: str)` вызывается
**ровно один раз** в entrypoint, **синхронно**, **до** первого
обращения к `SETTINGS`. Повторный вызов бросает `ConfigurationError`
(защита от двойной инициализации).

Альтернатива: `_initialize_settings()` без аргумента + `_resolve_mode()`
внутри, читающая argv напрямую через `sys.argv`. **Отвергнута**: нарушает
принцип «источник профиля один, передан явно». Тестам тоже нужна явная
передача.

**Обоснование:** явность на стороне вызова упрощает отладку
(traceback показывает, кто инициализировал) и тесты (патч `sys.argv` не
нужен).

### Decision 4: subprocess-инструменты (`tools.exec`) не наследуют профиль

**Выбор:** В `workspace/tools/exec` (через `nanobot.exec`-обёртку) при
формировании `env` для дочернего процесса **не** передавать ни
`NANOBOT_PROFILE`, ни любые другие profile-related env vars. Дочерний
процесс либо получает `--profile=...` явно как часть `command`,
либо упадёт с `ConfigurationError` — это **требуемое** поведение,
а не баг (см. сценарий в specs «Subprocess inheritance does not leak profile»).

Альтернатива: явный проброс `NANOBOT_PROFILE=...` в env, дочерний
читает env. **Отвергнута**: ровно та дупликация, от которой уходим.

**Обоснование:** отражено в `docs/INTERNAL_API.md` § «Конфигурация
`tools.exec`» (обновится в рамках tasks).

### Decision 5: `tests/test_application_context.py:110-127` мокируется через новую сигнатуру

**Выбор:** Существующие mock'и `_resolve_mode` / `resolve_application_config`
переписываются под mock `_initialize_settings`. Если в тестах был сценарий
«вызвать ApplicationContext с произвольным profile, проверить
`_resolve_mode == profile`» — он становится «вызвать
`config._initialize_settings(profile)` до `ApplicationContext.create()`,
проверить `ctx.profile == profile`».

Альтернатива: подмена на уровне `os.environ` через env-переменную.
**Отвергнута**: та же дупликация источника.

## Risks / Trade-offs

- **Lazy-init proxy добавляет точку сбоя в каждый тест, который раньше
  работал по module-level** — даже тривиальный `from config import
  SETTINGS; print(SETTINGS)` теперь требует вызова
  `_initialize_settings(profile="...")` перед `print`. → Mitigation:
  в `tests/conftest.py` добавить autouse-fixture, который вызывает
  `_initialize_settings(profile="test")` для каждого теста по умолчанию.
  Тесты, которым нужен prod-профиль, делают явный
  `_initialize_settings(profile="prod")` первым делом.

- **Coverage `tests/test_application_context.py:110-127` подразумевает
  monkey-patch `_resolve_mode` и `resolve_application_config` как
  функции** — после фикса единственная точка инициализации это
  `_initialize_settings`, и mock'и должны ставиться туда. Это
  нетривиальный refactor — 1–2 часа ручной работы. → Mitigation:
  отдельная task в плане с явным diff'ом (моки `_resolve_mode` заменяются
  на `_initialize_settings` + `resolve_application_config`).

- **Healthcheck / мониторинг prod** теряет env-сигнал. Банер
  `profile=prod` остаётся единственным визуальным индикатором.
  В случае неправильного `--profile` в прод-деплое — единственная
  защита это readiness-алерт на старте. → Mitigation: фиксируется как
  Open Question, не блокер. Если в проде случится инцидент — отдельный
  тикет через `RuntimeHealth`-механизм.

- **`from config import SETTINGS` в `lib/services/cache_provider_impl.py`,
  `lib/core/skill_config.py`, `skills/*` — все они в runtime не
  выполняются без предварительного `ApplicationContext.start()`**.
  То есть в «боевом» флоу проблем нет: сначала entrypoint инициализирует
  профиль, потом запускается ctx, потом весь остальной код.
  Standalone-утилиты (`tools/build_vectors.py`,
  `skills/audit_analyzer/scripts/cli.py` и др.) **не должны знать про
  профиль** — попытка обязать их парсить `--profile` размазывает
  знание о профиле по кодовой базе и противоречит этому change
  (источник профиля один — entrypoint). После фикса `SETTINGS` —
  lazy proxy: если standalone-скрипт случайно стартовал без
  инициализации, он упадёт на первом обращении к `SETTINGS[...]`
  с понятным `ConfigurationError("SETTINGS not initialized")`. Это
  требуемое поведение, не баг. → Mitigation: smoke-тест
  `tests/test_standalone_failfast.py` подтверждает, что все 6
  standalone-скриптов падают с этим сообщением без специальных правок
  в их коде. Если в будущем понадобится standalone-режим для одного
  из них — это отдельный тикет с явной точкой инициализации.

- **`tools/build_vectors.py` сейчас читает
  `project.json::gateway.vector.*` напрямую** (без профиля,
  потому что ему нужны продовые таблицы даже в test-режиме — там
  своя логика через `register_vector_storage`). После фикса
  утилита, запущенная через `exec`-тул из агента, продолжает
  работать: entrypoint уже инициализировал `SETTINGS`. При
  прямом CLI-запуске без предварительной инициализации — падает
  с `ConfigurationError`, как описано выше. → Mitigation: в
  документации `docs/INTERNAL_API.md` для `tools.build_vectors`
  явно указан контракт «запускается либо через `gateway.py --profile=...`,
  либо через `exec`-тул агента; прямой запуск без инициализации —
  не поддерживается».

## Migration Plan

### Этап M1 — shadow fix (для существующих деплоев, минимально инвазивно)

1. Деплои переводятся на `python gateway.py --profile=prod`
   (вместо `NANOBOT_PROFILE=prod` в env).
2. `docs/PROFILES.md` обновляется: секция «Миграция существующих деплоев»
   переписывается под «заменить env на CLI-флаг».
3. `AGENTS.md` обновляется в той же части.
4. **Не** содержит кодовых изменений в `config.py`. Подготовка инфраструктуры.

### Этап M2 — code change (то, что в tasks)

1. `config.py`: удалить module-level `_ACTIVE_PROFILE` / `SETTINGS`;
   убрать чтение `os.environ["NANOBOT_PROFILE"]` в `_resolve_mode`;
   добавить `_initialize_settings(profile)` и `SETTINGS`-proxy.
2. Entrypoints: argparse на module-level, `_initialize_settings(profile)`
   первой строкой `main()` / run-блока.
3. Standalone-утилиты: **никаких изменений**. Их прямой запуск без
   entrypoint — неподдерживаемый сценарий; lazy proxy обеспечивает
   `ConfigurationError` на первом обращении к `SETTINGS`.
4. Тесты: обновление mock'ов, добавление autouse-fixture в conftest,
   smoke-тест `tests/test_standalone_failfast.py` (см. risks).
5. Документация: `docs/PROFILES.md` полная переработка «Запуск»,
   «Cron», «Миграция»; упоминание `NANOBOT_PROFILE` удаляется везде.
   `docs/INTERNAL_API.md` — для каждой standalone-утилиты явно
   указано «требует предварительной инициализации через gateway entrypoint».
6. Smoke: `python gateway.py --profile=prod` без env → банер `prod`,
   `SETTINGS["logging"]["db"]["table_name"] == "agent_gateway_logs"`,
   `history_search` отрабатывает реальные запросы.

### Rollback

Если после M2 обнаруживается критичный regression
(например, тесты в другой ветке зависят от старого
`from config import SETTINGS` без инициализации):

1. Revert коммита M2 (один merge).
2. Релиз отозван, `SETTINGS` снова module-level под env.
3. M1 (документация) остаётся — она не ломает рабочее поведение.

Полный rollback **не требует миграции БД** — профиль-контракт не
затрагивает схему `agent_gateway_logs` или любую другую таблицу.

## Open Questions

1. **Деплой-gate: ловля «оператор запустил gateway с `--profile=test`
   на прод-сервере».** Профиль фиксируется при запуске gateway и
   остаётся таким до перезапуска — runtime-проверка через `/health` во
   время жизни процесса не имеет смысла. Однако при деплое легко
   ошибиться флагом (`--profile=prod` vs `--profile=test`), и в
   текущей инфраструктуре единственный сигнал — банер + ручной
   осмотр логов.

   Возможные варианты (все — отдельный OpenSpec-черновик, не блокер):
   - **Startup-gate в `ApplicationContext.start()`**: читать
     `project.json::runtime.expected_profile` (например, `prod`) и
     `ConfigurationError`-нуть на старте, если `SETTINGS.profile !=
     expected_profile`. Это fail-fast при первом старте, до того как
     gateway примет первую задачу. Не затрагивает `/health` и
     runtime-проверки.
   - **Парсинг банера воркер-процессом мониторинга** (Prometheus
     log-based alert на строку `profile=`): сработает только если
     мониторинг реально настроен, что в этом проекте не так.
   - **Ничего**: текущий банер + осмотр логов при инцидентах.
     Минимальное изменение, наихудшая защита.

   Этот change **не реализует** ни одно из решений — фиксируется как
   candidate-следствие для отдельного тикета, потому что решение
   влияет на схему конфига и требует согласования deployment pipeline.

2. **Subprocess-наследование через `exec`-tool** — выбранное решение
   «не передавать профиль в env дочерним процессам» означает, что
   дочерние `python -m skill.cli --file ...` тоже должны стартовать
   с `--profile=...` или получать его через явную аргументацию.
   Документация `docs/INTERNAL_API.md` секции «tools.exec» должна
   явно это отражать, проверяется отдельной task'ой.

3. **Тесты в `tests/test_skill_config_api.py` и подобных** используют
   `with patch("config.SETTINGS", _settings_with({...}))`. После
   перехода на proxy-объект мок подменяет сам proxy-целиком (не
   внутренний `_inner`). Это должно работать (стандартный unittest.mock
   паттерн), но если тесты сломаются на тонкостях lazy-инициализации —
   отдельный refactor-test-suite задача. Не блокирует план.

4. **Cron-процессы** — в текущем репо не активны. Если появятся в
   будущем, потребуют явного `--profile=...` в `command:` (см. Decision 4).
   Не блокирует текущий change.
