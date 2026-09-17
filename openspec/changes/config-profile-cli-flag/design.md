## Context

`config.py:517-518` фиксирует `_ACTIVE_PROFILE` и `SETTINGS` на module-level
import через `_resolve_mode()` (читает legacy env var из `os.environ`)
и `resolve_application_config(profile=_ACTIVE_PROFILE)`. Это происходит ДО
того, как `gateway.py` / `cli_agent.py` / `streamlit_app.py` успевают
распарсить `--profile=prod` через `argparse`. Банер и `ctx.profile`
получают правильное значение, но `from config import SETTINGS` отдают
test-версию: runtime-таблицы с `_test`-суффиксами. Инструменты
(`history_search`, `event_log`, `DbLoggingService`) ломаются по
`relation does not exist`.

Сам по себе `_resolve_mode` имеет корректный приоритет
«CLI > env > default=test» (config.py:238-241). Реальная проблема не в
приоритете источников, а в моменте их применения: profile-резолв
случается дважды — первый раз на module-level (config.py:517, без
аргумента), второй раз в `main()` (gateway.py:70, с `args.profile`).
Первый вызов фиксирует `SETTINGS` слишком рано, до того как `argparse`
распарсил `--profile`; второй корректный, но поздний и потому не влияет
на уже зафиксированный `SETTINGS`.

`ApplicationContext.create()` (`lib/core/application_context.py:121-126`)
уже содержит логику «если `profile != _ACTIVE_PROFILE`, пересобрать
`ctx.settings`» — страховка для ctx-level, но не для глобального
`config.SETTINGS`, на который ссылаются 182 места в коде.

## Goals / Non-Goals

**Goals:**

- Один lifecycle профиля: `--profile` → `_initialize_settings` → `SETTINGS` → runtime imports.
- Один источник профиля: argv `--profile` в application entrypoint.
- Закрытое множество профилей: только `prod` и `test`. Другие значения — `ConfigurationError` на старте.
- Импортабельный `SETTINGS` через proxy-объект (compatibility для 182 импортов).
- Без silent default, без env fallback, без auto-init при чтении.
- Legacy env var (упоминавшаяся в старых deployment descriptors) полностью
  удаляется из кода и документации.

**Non-Goals:**

- Полный отказ от module-level `SETTINGS` (отложен в отдельный, более крупный change).
- Healthcheck / readiness-gate по профилю.
- Cron-процессы (отсутствуют в репо).
- Рефакторинг `ApplicationContext.create()` за пределы удаления избыточной пересборки.
- Схема БД, profile-specific business logic, третий профиль (`dev`/`staging`).

## Lifecycle Архитектура

```text
process start
    ↓
application entrypoint (gateway.py / cli_agent.py / streamlit_app.py) parses --profile
    ↓
config._initialize_settings(profile)
    ↓
ConfigurationResolver builds SETTINGS (resolve_application_config)
    ↓
runtime imports / ApplicationContext
    ↓
channels / services / agent
```

**Инвариант 1**: `SETTINGS` SHALL NOT be constructed or resolved during `import config`. Доступ к `SETTINGS` без предварительного `_initialize_settings(...)` — `ConfigurationError`.

**Инвариант 2**: Никаких default-значений профиля. Нет `--profile` → `ConfigurationError("--profile is required")` на старте.

**Инвариант 3**: Поддерживаемый whitelist профилей — только `prod` и `test`. Любое другое значение → `ConfigurationError` на старте, **до** какого-либо конструирования runtime.

**Инвариант 4**: Никаких auto-init при чтении `SETTINGS`, никакого environment fallback, никакого runtime-resolve профиля.

## Subprocess Contract

```text
Application subprocess (запускает gateway.py / cli_agent.py / streamlit_app.py):
    --profile обязателен через command
    --profile отсутствует → fail-fast с ConfigurationError

Application subprocess получил --profile=<v> явно. Никакая env var
не используется для передачи профиля; любые unknown env vars
(включая устаревшие deployment-имена) — irrelevant для application
runtime и игнорируются.

Standalone utility (или low-level subprocess типа git/pip):
    --profile НЕ обязателен
    utility определяет свой собственный контракт если использует SETTINGS
```

## Definitions

### Application entrypoint

Исполняемый entrypoint, который создаёт runtime приложения и использует
`ApplicationContext` / `SETTINGS`. В текущем change: **`gateway.py`**,
**`cli_agent.py`**, **`streamlit_app.py`**. Только эти три файла
требуют обязательного `--profile`.

### Standalone utility

Скрипт, который может выполняться вне application entrypoint и не
использует `ApplicationContext` / resolved `SETTINGS`. Примеры:
`tools/build_vectors.py`, `tools/check_worker_pool_integrity.py`,
`workspace/skills/*/scripts/cli.py` (в режиме exec-из-агента —
через entrypoint, в режиме standalone — отдельная задача). Не
требует обязательного `--profile`.

## Decisions

### Decision 0: Ownership разделение

```text
ConfigurationResolver
    отвечает: КАК построить configuration
              (project.json + profile overlay + secrets + validation)

_LazySettings
    отвечает: КОГДА configuration разрешено публиковать
              (UNINITIALIZED / INITIALIZED, защита от double-init)

_initialize_settings
    отвечает: gate между "resolved <profile>" и "constructed SETTINGS"

SETTINGS
    compatibility access point к constructed configuration;
    mapping semantics; SETTINGS["profile"] — canonical profile access
```

Это **строгое разделение ответственности**. `_LazySettings` НЕ
содержит логики построения, merge или валидации — только lifecycle.
`ConfigurationResolver` (`resolve_application_config`) НЕ знает про
`_initialize_settings` или proxy — он просто строит dict.
`_initialize_settings` — единственная точка, где они встречаются.

### Decision 1: `_LazySettings` — compatibility boundary (не архитектура)

**Выбор:** Ввести `_LazySettings` proxy-объект, у которого только два
состояния — UNINITIALIZED и INITIALIZED. SETTINGS access в UNINITIALIZED
состоянии бросает `ConfigurationError` (никакой авто-инициализации).
Прокси сохраняет существующие 182 `from config import SETTINGS`
импорты без изменений.

Это **compatibility mechanism**, не новая модель конфигурации.
Полный отказ от module-level `SETTINGS` — отдельный, более крупный
рефакторинг dependency injection архитектуры.

Альтернативы рассмотрены:

- **Полное удаление `from config import SETTINGS`** (требует
  переделки 182 мест с явной инжекцией через `ApplicationContext`).
  Слишком дорого для одного change, отложено.
- **`_LazySettings` с auto-инициализацией** при первом read через
  ленивый `_resolve_mode(argv/env)`. **Отвергнуто**: нарушает
  инвариант 1 (неявная инициализация — то же самое module-level
  поведение под другим именем).
- **`threading.local`-привязанный proxy** — не решает проблему,
  проблема в моменте инициализации, а не в многопоточности.

**Обоснование:** proxy исключительно как compatibility shim. Никакой
дополнительной логики сверх «бросить `ConfigurationError` если
не инициализирован / вернуть dict если инициализирован».

**Контракт proxy** (только три операции):

```text
UNINITIALIZED
    ↓
_initialize_settings(profile)
    ↓
INITIALIZED
```

- Повторный `_initialize_settings("test")` после
  `_initialize_settings("prod")` → `ConfigurationError`.
- `SETTINGS["k"]` до `_initialize_settings(...)` →
  `ConfigurationError`.
- `SETTINGS["k"]` после → dict access (mapping semantics).
- `SETTINGS.profile` (attribute access) — **НЕ** часть canonical
  contract; может быть оставлен как backward-compat shim для
  существующего кода (`streamlit_app.py:33` использует
  `getattr(SETTINGS, "channels", {})`), но новый код
  `SETTINGS["profile"]`.

**Запрещено:**

```text
auto initialization
profile switching
environment fallback
default profile
```

### Decision 1a: `_resolve_mode()` удаляется полностью

После удаления env-чтения из `_resolve_mode` функция превращается
в:

```python
def _resolve_mode(profile_arg):
    if profile_arg not in {"prod", "test"}:
        raise ConfigurationError(...)
    return profile_arg
```

Это уже **не resolution** (нет источника профиля, нет приоритетов,
нет env-чтения), а **whitelist-валидация**. Сохранять её как
отдельную функцию с именем `_resolve_mode` — лишняя сущность.

**Решение:** Удалить `_resolve_mode()` из `config.py` полностью.
Whitelist-валидация встраивается в `_initialize_settings(profile)`
как defensive re-validation. Двойная проверка (entrypoint CLI +
`_initialize_settings`) остаётся, как и было заявлено в спецификации,
но без отдельной функции `_resolve_mode`.

**Сигнатура `_initialize_settings(profile)`:** функция НЕ возвращает
значение (`-> None`). Результат initialization доступен **только**
через `SETTINGS` после успешного вызова. Это явный выбор архитектуры:
«функция публикует состояние» (side-effect на `_LazySettings._inner_dict`),
а не «функция возвращает configuration в caller». Caller'у не нужно
проверять return value; успех = _initialize_settings не бросил
`ConfigurationError`; ошибка = бросил. Это контракт, который
используется всеми entrypoint'ами и тестами. Mock-стратегия для
тестов — `mock.patch("config._initialize_settings")` БЕЗ
`return_value`; после мока проверяется что `config.SETTINGS`
стал доступен через lazy proxy.

Соответственно, `_ACTIVE_PROFILE` module-level global тоже
удаляется (он привязан к `_resolve_mode`).

### Decision 2: Startup parsing в executable entrypoint path

**Выбор:** В `gateway.py` и `cli_agent.py` парсинг `--profile` и
вызов `_initialize_settings(profile)` выполняются
**в executable entrypoint path** — внутри `if __name__ == "__main__":`
(с последующим `sys.exit(2)` на ошибках), **до** любых импортов,
читающих конфиг. Это удерживает entrypoint от случайных
import-side-effects: `import gateway` (из других модулей, тестов,
или REPL) НЕ запускает приложение.

Для `streamlit_app.py` (особый случай — `streamlit run` не выставляет
`__name__ == "__main__"` на rerun) startup-блок расположен
**на module-level, выше** существующих импортов; см. Decision 4 для
деталей этого исключения.

Альтернативы рассмотрены:

- **Парсинг argv на module-level через post-import hook** — не решает:
  всё равно происходит **после** любых side-effect imports, а
  этого нельзя допустить (нарушает инвариант «import config не
  конструирует SETTINGS»).
- **Pre-fork процесс с явной передачей через stdin/файл** —
  избыточно.
- **argparse как глобальный module-level statement (без `if __name__`)** —
  отвергнуто. Превращает `import gateway` в side-effect, нарушает
  ожидаемый контракт импорта модуля. Слабый coding-agent может
  буквально интерпретировать «argparse на module-level» именно
  так; это та ловушка, которую явная формулировка «executable
  entrypoint path» предотвращает.

**Обоснование:** Python выполняет `if __name__ == "__main__":` блок
**только** при executable invocation (`python gateway.py`); при
обычном `import gateway` блок пропускается. Если entrypoint
имеет конструкцию `if __name__ == "__main__": _resolve_and_init_profile()`,
и все конфиг-читающие импорты находятся ниже этого блока
или внутри `main()`, то `_initialize_settings` гарантированно
происходит до import-цепочки, **но только** при executable run.

**Конкретный паттерн:**

```python
# gateway.py
import sys

if __name__ == "__main__":
    import argparse
    _parser = argparse.ArgumentParser(add_help=False)
    _parser.add_argument("--profile", type=str, default=None)
    _args, _ = _parser.parse_known_args()
    if not _args.profile:
        sys.stderr.write("FATAL: --profile is required\n")
        sys.exit(2)
    if _args.profile not in {"prod", "test"}:
        sys.stderr.write(f"FATAL: --profile={_args.profile!r} is not supported (allowed: prod, test)\n")
        sys.exit(2)
    import config as _cfg
    _cfg._initialize_settings(profile=_args.profile)
    # дальше все runtime-импорты
```

Аналогично для `cli_agent.py`. Для `streamlit_app.py` см. Decision 4.

**Контракт импорта как модуля:** `import gateway`,
`python -c "from cli_agent import ..."`, `from gateway import something`
НЕ запускают приложение и НЕ инициализируют SETTINGS. Только
**executable invocation** (как `python <file>.py` или
`streamlit run <file>.py`) запускает startup lifecycle. Это
контракт **application entrypoint**, не контракт `import`-statement.

Для `streamlit_app.py` блок `if __name__ == "__main__":` НЕ работает
(Streamlit-run сам решает, что ре-выполнять, и `__name__` не равен
`"__main__"` при rerun). Поэтому в `streamlit_app.py` логика инициализации
профиля выполняется **на module-level, выше** существующих импортов,
без `if __name__ == "__main__":` обёртки — это согласуется с тем,
что Streamlit кеширует модуль в `sys.modules` и не переимпортирует
его на `st.rerun()`.

Это **by design**: для `streamlit_app.py` не гарантируется, что
`import streamlit_app` (из других модулей, тестов, или REPL) НЕ
триггерит module-level side effects, потому что Streamlit-run
— единственный корректный entrypoint для этого файла.
`gateway` и `cli_agent` эту гарантию предоставляют через `if __name__ == "__main__":`,
`streamlit_app` — нет, потому что Streamlit этого не позволяет.

Контракт приложения таков: `streamlit_app.py` запускается
**только** через `streamlit run streamlit_app.py -- --profile=<v>`;
другие формы invocation не являются частью application contract.
Любое использование `streamlit_app.py` вне `streamlit run` —
unsupported scenario, и наличие module-level initialization
в этом случае семантически не определено. Если в будущем
потребуется импортировать `streamlit_app` из тестов или
других модулей без side effects, это отдельная задача
(введение helper-модуля, вынос initialization в factory и т.п.) —
НЕ часть данного OpenSpec change.

### Decision 3: Whitelist профилей и валидация на старте

**Выбор:** Разрешённые профили — только `prod` и `test`. Проверка
выполняется **до** вызова `_initialize_settings`, в аргументах
entrypoint, чтобы fail-fast был на старте, до `import config`.

Альтернативы рассмотрены:

- **Whitelist внутри `_initialize_settings`** — работает, но не
  защищает от попыток `import config; _initialize_settings(profile="dev")`
  из других мест; валидация в entrypoint — defense-in-depth.
- **Regex `[a-z0-9_-]+`** — текущее поведение в `_resolve_mode`
  (config.py:242-247). Позволяет фактически произвольные имена.
  Не соответствует Negative Requirements существующей спеки
  (запрет третьего профиля без OpenSpec change).

**Обоснование:** whitelist — единственный надёжный способ
соблюсти Negative Requirements. Проверка в entrypoint защищает
от ошибок в самом `config._initialize_settings`.

### Decision 4: Streamlit invocation — один supported pattern

**Выбор:** Поддерживаемая форма запуска:

```bash
streamlit run streamlit_app.py -- --profile=prod
```

Альтернатива (через `streamlit_app.py main()` сам читает `sys.argv`):
не используется, т.к. Streamlit преобразует argv особым образом,
и parsing для `--profile` в самом верху `main()` достаточно.

**Конкретное поведение Streamlit:**
1. В модульном блоке `streamlit_app.py` — функция
   `_resolve_profile_from_argv()`, читающая `sys.argv` после `--`
   (переданные streamlit-run аргументы).
2. Если `--profile=...` отсутствует → `ConfigurationError`.
3. Если `--profile=...` не из whitelist → `ConfigurationError`.
4. Вызов `config._initialize_settings(profile=...)` **до** любых
   импортов, читающих SETTINGS (т.е. до `from lib.core.*`,
   `from nanobot.*`).

**Acceptance test:** `pytest tests/test_streamlit_argv.py` —
subprocess-вызов с правильным и неправильным `--profile`.

**Обоснование:** Streamlit-процесс стартует через
`streamlit run`, после чего `sys.argv` имеет особый формат. Чёткая
фиксация supported invocation устраняет implementation-uncertainty.

### Decision 5: Standalone utilities не знают про профиль

**Выбор:** `tools/build_vectors.py`, `tools/check_worker_pool_integrity.py`,
`workspace/skills/audit_analyzer/scripts/cli.py`, `workspace/skills/legal_summarizer/scripts/cli.py`
**никак не изменяются**. Их прямой standalone-запуск — неподдерживаемый
сценарий; если они случайно стартовали без предварительной
инициализации, `_LazySettings` proxy обеспечивает `ConfigurationError`
на первом обращении к `SETTINGS`.

Альтернативы рассмотрены:

- **Каждый standalone-utility должен парсить `--profile`** —
  размазывает знание о профиле по всей кодовой базе, нарушает
  инвариант «источник профиля один — application entrypoint».
- **Централизованная инициализация в `from config import SETTINGS`
  в каждом standalone** — то же самое через module-level, что
  и привело к текущему багу.

**Обоснование:** автотест `tests/test_standalone_failfast.py`
подтверждает, что 6 standalone-скриптов падают с правильным
сообщением без специальных правок в их коде. Если в будущем
понадобится standalone-режим для одного из них — это отдельный
тикет с явной точкой инициализации.

### Decision 6: tests/conftest.py autouse-fixture НЕ добавляется

**Выбор:** Тесты явно вызывают `_initialize_settings(profile=...)`
в setup. Никаких autouse-fixture, которые бы скрывали lifecycle-ошибки.

Альтернатива: `autouse-fixture → _initialize_settings("test")` —
отвергнута. Скрывает тесты, которые забывают инициализировать
профиль, превращая ошибки в silent state.

**Обоснование:** каждая ошибка «забыл инициализировать» должна
всплыть как `ConfigurationError` в тесте, а не маскироваться через
`autouse`. Тесты, которым нужен resolved `SETTINGS`, явно
инициализируют профиль в setup.

### Decision 7: Application subprocess получает profile через argv, не через env

**Выбор:** При spawn'е дочернего процесса, являющегося
**application entrypoint** (`gateway.py`, `cli_agent.py`,
`streamlit_app.py`), parent передаёт активный профиль **через
argv** (`--profile=<v>`), источник — `SETTINGS["profile"]`.
Env-переменная для передачи профиля **не используется ни при каких
условиях**, потому что единственный source of truth —
`--profile=<v>`. Любая другая env var (включая устаревшие
deployment-имена) — unknown external variable и игнорируется.

Альтернатива: env-based profile propagation (явная передача через
`os.environ`). Отвергнута — это именно та дупликация источника,
от которой уходим.

**Обоснование:** отражено в `docs/INTERNAL_API.md` § «Конфигурация
`tools.exec`» и в spec requirement «Profile is passed to application
subprocesses only through --profile». Этот change **не вводит**
механизм sanitization child environment: приложение просто не
работает с устаревшими env var'ами — они нерелевантны для его
runtime-контракта. Никакая sanitization в runtime не нужна, потому
что никакого runtime-чтения этих env vars не существует.

### Decision 8: `tests/test_application_context.py:110-127` переписывается под новую сигнатуру

**Выбор:** Существующие mock'и `_resolve_mode` / `resolve_application_config`
переписываются под mock `_initialize_settings`.

Альтернатива: подмена на уровне `os.environ` через env-переменную.
Отвергнута.

**Обоснование:** единственная точка инициализации после change —
`_initialize_settings`, и mock'и должны ставиться туда.

### Decision 10: OpenSpec lifecycle финален перед реализацией

Перед началом реализации `openspec.cmd validate config-profile-cli-flag --strict`
проходит без ошибок; все 12 пунктов Definition of Done в `tasks.md`
выполнимы существующими acceptance-критериями; ни одно из проверенных
противоречий не остаётся в артефактах. Только после этого `tasks.md`
phase переводится из «planned» в «ready for implementation», и
отдельный тикет начинает coding.

## Risks / Trade-offs

- **Compatibility shim остаётся надолго** — 182 импорта `from config import SETTINGS`
  работают через proxy, но это не устраняет базу: module-level доступ к
  `config` остаётся доступен до `_initialize_settings`. Полный отказ — отдельный
  change. → Mitigation: этот change фиксирует минимально достаточную границу.
- **Whitelist профилей — потенциальный breaking change** для внутренних
  экспериментов с `dev`/`staging`. → Mitigation: открыть отдельные OpenSpec
  change'ы для каждого нового профиля, как требует Negative Requirements.
- **Streamlit-инвокация — единственный supported pattern**; другие формы
  (например, передача через переменную окружения `STREAMLIT_PROFILE`)
  не поддерживаются. → Mitigation: документировать явно в
  `docs/INTERNAL_API.md`.
- **`tests/test_application_context.py:110-127` mocks** — нетривиальный
  refactor. → Mitigation: в design Phase B tasks есть отдельная
  работающая инструкция с явным diff.
- **`healthcheck / readiness` не покрыт этим change** — профиль фиксируется
  на старте, runtime-проверки не имеют смысла. → Mitigation: Open Question,
  отдельный тикет.

## Streamlit — конкретный supported invocation

```bash
streamlit run streamlit_app.py -- --profile=prod
```

Реальный текущий `streamlit_app.py:31` уже выполняет
`from config import SETTINGS` на module level (до любых streamlit-API
вызовов). Модуль импортируется ОДИН раз при первом запуске `streamlit
run`; последующие `st.rerun()` re-execute скрипт **без переимпорта**
(`streamlit_app.py` уже в `sys.modules`). Это означает, что
`_initialize_settings()` обязан выполниться в module-level блоке
(а не в `if __name__ == "__main__":` — это условие для streamlit-run
не сработает как для CLI).

Поведение `streamlit_app.py` после фикса:

1. На самом верху файла (до существующих `import streamlit as st`
   и `from utils.db import ...`, до `from config import SETTINGS`):
   парсер `sys.argv`, ищущий `--profile=<value>` после `--`.
2. Если отсутствует → `ConfigurationError("--profile is required")`.
3. Если не из `{prod, test}` → `ConfigurationError(...)`.
4. `import config as _cfg; _cfg._initialize_settings(profile=<value>)`.
5. Дальше — текущий код `streamlit_app.py` без изменений (он
   потребляет `SETTINGS` через `getattr(SETTINGS, ...)` —
   backward-compat, см. Decision 1).

**Acceptance test** (subprocess):

- `streamlit run streamlit_app.py -- --profile=prod` →
  процесс стартует; `_initialize_settings("prod")` вызван;
  `SETTINGS["profile"] == "prod"`; runtime configuration prod.
- `streamlit run streamlit_app.py -- --profile=test` → test.
- `streamlit run streamlit_app.py` (без `--profile`) →
  module-level `ConfigurationError`, process exits before first
  streamlit run completes.
- `streamlit run streamlit_app.py -- --profile=dev` →
  `ConfigurationError("--profile=dev is not supported")`.

**Implementation assumption об архитектуре Streamlit:** текущая
форма `streamlit run <script> -- <args>` транслирует `<args>`
в `sys.argv` как позиционные элементы после `--`. Если в будущей
версии Streamlit это изменится, нужно будет обновить argparse-блок;
это **implementation detail**, не часть spec. Спекуфицируется только
поведение: `--profile=<v>` обязан прийти в `argv` после `--`,
а streamlit-процесс обязан упасть с exit code 2 + `ConfigurationError`
при его отсутствии.

## Архитектурный acceptance checklist

После завершения всех task'ов change считается реализованным, **когда**:

- [ ] `config.py` не содержит module-level `SETTINGS` construction.
- [ ] `config.py` не читает environment variables для resolution
      профиля ни в каком виде.
- [ ] Нет default-профиля: без `--profile` — fail-fast.
- [ ] Только `prod` и `test` принимаются; остальные — `ConfigurationError`.
- [ ] `SETTINGS` не может быть прочитан до `_initialize_settings(...)`.
- [ ] `SETTINGS` не может быть инициализирован дважды.
- [ ] Профиль не может быть изменён после инициализации.
- [ ] Application entrypoints (`gateway.py`, `cli_agent.py`, `streamlit_app.py`)
      требуют `--profile`.
- [ ] Профиль инициализируется до любых runtime-импортов.
- [ ] Application subprocesses получают `--profile` через `command`
      (никогда не через env).
- [ ] Streamlit invocation явно определён (`streamlit run streamlit_app.py -- --profile=prod`).
- [ ] Integration-тест проверяет runtime configuration (например, имя
      runtime-таблицы), не только банер.
- [ ] Никакой profile-ветки в business logic не добавлено.

## Migration Plan

### Этап M1 — shadow fix (только документация)

1. Деплои переводятся с устаревшей env var на
   `command: python gateway.py --profile=prod`.
2. `docs/PROFILES.md` перерабатывается: «Запуск» через CLI-флаг;
   «Миграция существующих деплоев» — таблица env → command.
3. `AGENTS.md` обновляется.
4. **Никаких** кодовых изменений в `config.py`.

### Этап M2 — code change (Phase A–E из tasks.md)

1. `config.py` — module-level удаление + lazy proxy + whitelist.
2. Entrypoints — argparse + `_initialize_settings`.
3. Standalone utilities — без изменений.
4. Tests — explicit init, новые сценарии, mock-переделка.
5. Documentation — `PROFILES.md`, `INTERNAL_API.md`, CHANGELOG.

### Rollback

Если после M2 обнаруживается критичный regression, revert одного merge
достаточно. Rollback не требует миграции БД — профиль-контракт не
затрагивает схему `agent_gateway_logs` или другие таблицы.

## Open Questions

1. **Healthcheck / readiness-gate** для прод-деплоя (вариант
   `project.json::runtime.expected_profile`) — отдельный OpenSpec,
   не блокирует.
2. **Cron-процессы** — отсутствуют в репо; при появлении потребуют
   явного решения про `--profile` в `command:` systemd/k8s/cron-job.
