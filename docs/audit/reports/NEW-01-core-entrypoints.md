# Аудит: 01 — Ядро и точки входа (NEW, пересмотр)

> Пересмотр существующего `01-core-entrypoints.md`. Тот отчёт писался по коду
> ДО сноса кластера локального кэша (фаза 5) и миграции на `mcp-platform`
> (фаза 7+, 11). На момент этого аудита `application_context.py` — 1680 строк
> (в старом отчёте фигурировали 1816/1502), `lib/services/duckdb_cache_store.py`
> и ещё 18 модулей отсутствуют в дереве. Все выводы ниже — по коду на
> 2026-10-03; расхождения с `AGENTS.md` и `docs/ARCHITECTURE.md` помечены
> как находки уровня D.

## Сводка группы

Файлов: 10 · LOC: 5374 · классов: 39 · методов: 39 · функций: 85
(из них 7 вложенных: `application_context.py:841,1180,1186,1214`,
`agent_factory.py:200,223,290`). Индивидуальный вердикт проставлен
**145 символам** — по строке на каждый класс, метод и функцию.

Файлы: `config.py` (1040), `gateway.py` (699), `cli_agent.py` (487),
`lib/core/__init__.py` (1), `lib/core/application_context.py` (1680),
`lib/core/agent_factory.py` (419), `lib/core/project_settings.py` (814),
`lib/lifecycle/__init__.py` (1), `lib/lifecycle/gateway_runner.py` (113),
`lib/lifecycle/shutdown_coordinator.py` (120).

**Ключевые находки**

- `cli_agent.py:238` — CLI **делает** рукопожатие с `enterprise-mcp` внутри живого
  loop, до REPL. `AGENTS.md` утверждает обратное («Путь `cli_agent.py`
  рукопожатия **не делает** — там сессия ленивая»). Коду соответствует
  `openspec/specs/runtime/entrypoints/spec.md:306,636`. Расходится именно
  `AGENTS.md`. **D — расхождение канона с кодом.**
- `lib/core/application_context.py:480` — клиент `enterprise_mcp` создаётся
  **безусловно**, без гейта по `role`. `CronService` при этом гейтится
  (`:370`). Вместе с предыдущей находкой это даёт: при работающем Gateway
  любой запуск CLI поднимает **второй процесс** `enterprise-mcp`, то есть
  второй держатель пула PostgreSQL — ровно то, что `AGENTS.md` запрещает.
  `openspec/changes/unify-runtime-channels/proposal.md:33-48` это уже
  зафиксировал (с устаревшей привязкой `:502`, фактически `:480`).
- `lib/core/project_settings.py` — 814 LOC, 31 модель; **единственное
  production-чтение** — `application_context.py:1340`
  (`project_settings.logging.db.flush_interval_sec`). Остальное — валидация без
  потребителя. **B — переизбыточная подсистема.**
- `lib/core/project_settings.py:55-60` — `_StrictOptional` = `extra="allow"`.
  Проверено исполнением: из 31 модели строгих (`extra="forbid"`) **5**
  (`ProjectMetadataSettings`, `SkillSettings`, `TableEntry`, `VectorIndexConfig`,
  `VectorIndexEntry`), остальные 26 принимают произвольные ключи. Заявленная в
  модуле «типизированная валидация» не выполняется; опечатка в
  `logging.db.table_name` проходит молча. **C.**
- `lib/core/project_settings.py:548-551` и `:566-576` — докстринги ссылаются на
  `lib.services.text_splitter.split_text` и
  `lib.core.skill_config.get_brief_context_config` + skill `legal_summarizer`.
  Проверено `Test-Path`: **все три отсутствуют** в дереве. **D — строки, которые
  лгут** (то, что протокол просит искать в первую очередь).
- `lib/core/project_settings.py:141-183` (`VectorIndexSettings`) — докстринг сам
  признаёт: «Секция валидируется, но никем не читается». Дублирует
  `mcp-platform/platform.json → vectors.indexes`. **B — кандидат на удаление.**
- `gateway.py:506-517` `_gateway_print_llm_calls()` — **0 вызывающих**. Флаг
  при этом рабочий: его читает `application_context.py:99` → `:403` →
  `agent_factory.py:210` → `database_logging_hook.py:753`. То есть в
  `config.json:705` стоит `"print_llm_calls": true`, и он действует, но через
  другую цепочку. Мёртв именно helper в entrypoint'е. **B — удалить.**
- `cli_agent.py:251-254` `__get_cron()` — **0 вызывающих** во всём репо. Имя с
  двойным подчёркиванием в модульном scope — след неудачного «приватного»
  соглашения. **B — удалить.**
- `lib/lifecycle/gateway_runner.py:110-113` `reset_backoff()` — **0 вызывающих**,
  включая тесты; докстринг `:111-112` утверждает «Используется в тестах» —
  неправда. **B — удалить.**
- `lib/core/agent_factory.py:78` `framework_hooks` — параметр не передаётся ни
  одним вызывающим; докстринг `:181-182` сам пишет «Сейчас список пуст».
  **C — удалить параметр.**
- `lib/core/application_context.py:164` `enable_audit` — пишется в `:260`, не
  читается **нигде**. Остаток снятого audit-сервиса. Закреплён тестом
  `tests/test_application_context_cache_lifecycle.py:55-57`. **B — удалить
  вместе с тестом.**
- `gateway.py:472-477` — докстринг `script_dir_for_runtime` обещает, что
  `import gateway` «остаётся чистым от side-effects». На деле module-level
  побочные эффекты есть: `gateway.py:36` (`ensure_console_colors()`) и
  `gateway.py:91-99` (`os.environ.setdefault` × 4). **C — докстринг лжёт.**
- `gateway.py:659` `console = Console()` — определён **после** всех функций, его
  использующих (`:139, :149, :214, …`). Работает только потому, что module-level
  код выполняется целиком до `main()`. Хрупко. **C — перенести наверх.**
- `cli_agent.py:257-268` и `gateway.py:485-503` — две копии `_configure_logging`,
  причём **расходящиеся**: CLI читает `cli.log_level` (дефолт `WARNING`) и
  передаёт `env_var="NANOBOT_LOG_LEVEL"`; gateway читает `gateway.log_level`
  (дефолт `INFO`) и параметр `settings` **вообще игнорирует**, перечитывая
  конфиг через `ConfigService`. **C — слить.**
- `cli_agent.py:427` и `gateway.py:472` — две копии `script_dir_for_runtime` с
  разными module-level `_SCRIPT_DIR`. **C — слить.**
- `cli_agent.py:287-340` и `gateway.py:191-246` — две копии
  `_connect_enterprise_mcp` (различаются подачей отказа и наличием
  `_verify_platform_tables`). **C — слить в один helper с параметром подачи.**
- `lib/core/application_context.py:1413-1414` — докстринг `_make_cron_service`
  говорит «для CLI-режима (только там он нужен)», но вызывается он при
  `role == "gateway"` (`:370`). Докстринг инвертирован. **D.**
- `lib/lifecycle/shutdown_coordinator.py:11` — в докстринге порядка регистрации
  числится `sync_service` (удалён `pg_duckdb_sync_service`); `:5-6` упоминает
  «аудит-сервисы» (тоже удалены). `lib/lifecycle/gateway_runner.py:17` —
  «БД (Postgres, Redis)», а `redis_channel` удалён. **D — остатки снятых
  подсистем в докстрингах.**
- `config.py:116-119` против `config.py:166-167` — **внутреннее противоречие в
  одном докстринге**: строки 116-119 говорят, что source-таблица векторного
  индекса хранится в `public.agent_vector_index_config`, строки 166-167 — что
  этот PG-реестр «больше НЕ читается кодом». Таблица удалена. **D.**
- `gateway.py:154-158` и `application_context.py:688-691` — комментарии
  утверждают, что «кэш уже загружен в `_init_cache_runtime` (composition root)».
  Метода `_init_cache_runtime` в дереве **нет** (проверено `Select-String` по
  всему репо: 0 определений, остались только эти комментарии и
  `docs/DATABASE.md:263`). **D — комментарии описывают удалённое поведение.**
- `lib/core/application_context.py:157-158` — комментарий «dataclass без
  `__slots__` допускает произвольные атрибуты экземпляра», но класс **не**
  `@dataclass` (декоратора в файле нет, `class ApplicationContext:` на
  `:117`). **C.**
- `lib/core/application_context.py:257, 454, 494` — `profile`,
  `runtime_patch_report`, `project_tools_result` присваиваются динамически и
  **не объявлены** в теле класса (`:121-193`). Работает только потому, что
  класс — plain object. Скрытый контракт: `getattr(ctx, "profile", ...)` у
  потребителей. **C — объявить явно.**
- `AGENTS.md` документирует контракт
  `register_project_tools(agent, workspace_dir, *, settings, cache_store, db_logging_service)`,
  но фактический вызов `application_context.py:487-493` передаёт
  `enterprise_mcp=` и **не** передаёт `cache_store`. **D — расхождение
  `AGENTS.md` с кодом (вне моей группы, отмечено для владельца `AGENTS.md`).**
- Гарды-тесты покрывают `lib/` и `workspace/tools/`
  (`tests/test_dependency_direction.py`, `test_platform_import_boundary.py`),
  но **не** `config.py`, `gateway.py`, `cli_agent.py`. Поэтому остатки старой
  архитектуры в корневых файлах никем не ловятся — в частности,
  `test_no_cache_api_remains.py` не срабатывает на докстринги (guard явно
  исключает строковые литералы). **Наблюдение для гардов.**

**Вердикты (145 символов, по строкам таблиц):** Оставить 108 · Упростить 18 ·
Слить 6 · Удалить 13

Из 13 «Удалить» 9 — это классы мёртвой ветки `VectorIndex*`/`Heartbeat*`/
`Skill*` в `project_settings.py`, остальные 4 — `_gateway_print_llm_calls`,
`__get_cron`, `reset_backoff`, параметр `framework_hooks` (+ поле
`enable_audit`, см. сводный список).

---

## `config.py` — 1040 LOC

**Назначение.** Единственная точка формирования `SETTINGS`: merge пяти слоёв
конфигурации, резолв `${VAR}`, валидация профиля и публикация через
lifecycle-gate.

**Что делает.** Модуль собирает конфиг из `config.json` → `session_manager.json`
→ `.secrets.env` → `profiles/<mode>.jsonc` (`resolve_application_config:746-798`).
Побочные эффекты, которые не видны из сигнатуры:
1. `_export_secrets_to_env:576-584` **пишет в `os.environ`** через `setdefault` —
   все «плоские» ключи конфига становятся переменными окружения процесса;
2. `_export_runtime_env:587-600` пишет `NANOBOT_PYTHON` и `NANOBOT_PROJECT_ROOT`;
3. `_initialize_settings:966` дополнительно делает `os.environ.setdefault("LLM_API_KEY", ...)`;
4. `_lift_agent_sections:396-431` **мутирует** загруженный dict, выдёргивая
   `gateway.agent.*` в корень, и бросает `ConfigurationError` на неизвестную
   секцию или на двойное объявление;
5. `validate_runtime_isolation:665-696` — hard-fail, сверяющий 5 runtime-таблиц
   с профилем.

**Зачем нужен.** Это фундамент lifecycle-гейта: `SETTINGS` недоступен до явного
`_initialize_settings(profile)` из entrypoint'а (`_LazySettings:815-905`), что
исключает чтение конфига на уровне модуля. Без него любой потребитель
настроек падал бы в недетерминированный момент.

**Вердикт.** Оставить (с точечным упрощением — см. `get_active_profile`).

**Обоснование.** Функционально востребован: 4 из 5 слоёв merge и оба
hard-fail'а — живой контракт, покрытый `tests/test_profile_lifecycle.py` и
`tests/test_config_keys.py`. Проблемы — не в существовании, а в объёме
сопутствующей документации (`:1-200` — 200 строк модульного докстринга, часть
которой противоречит сама себе, см. ниже) и в двух мёртвых функциях.

**Доказательства.** Импортируется: `gateway.py:29`, `cli_agent.py:24`,
`lib/lifecycle/gateway_runner.py:37`, `lib/core/project_settings.py`,
десятки потребителей через `from config import SETTINGS, get_setting`.
Тесты: `tests/test_profile_lifecycle.py`, `tests/test_config_keys.py`,
`tests/test_standalone_failfast.py`.

### Строка, которая лжёт (внутри одного файла)

`config.py:116-119` утверждает, что source-таблица векторного индекса хранится
в `public.agent_vector_index_config`; `config.py:166-167` в том же докстринге
утверждает, что этот PG-реестр «больше НЕ читается кодом». Второе верно
(таблица снята вместе с `infra_registration`), первое — нет. Обе строки
проходят гард `tests/test_no_hardcoded_table_names.py`, потому что guard
исключает докстринги. **D.**

### class `AttrDict` (строки 249-258, 2 метода)

Назначение: `dict` с доступом через точку, рекурсивно оборачивающий вложенные
dict'ы. Нужен как единственный тип, которым манипулируют потребители `SETTINGS`
(`SETTINGS.gateway.agent...`). Вердикт: **Оставить**.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__getattr__` | 250-255 | `self[name]` с рекурсивной обёрткой dict→`AttrDict` | точечный доступ в коде настроек | сотни мест, `SETTINGS.<section>` | Оставить |
| `__setattr__` | 257-258 | присваивание как `self[name] = v` | симметрия `__getattr__` | `config.py` internals | Оставить |

### class `ConfigurationError` (строки 529-540, 0 методов)

Назначение: наследник `ValueError`, единый тип ошибок конфигурации; объявлён
**до** `_initialize_settings`, чтобы module-level импорт не давал `NameError`
(докстринг `:537-539`). Вердикт: **Оставить** — это публичный контракт, на
котором построен error-lifecycle boundary обоих entrypoint'ов
(`gateway.py:692-694`, `cli_agent.py:475-477`).

### class `_LazySettings` (строки 815-905, 14 методов)

Назначение: proxy `SETTINGS` с двумя состояниями (UNINITIALIZED/INITIALIZED).
Любое чтение до инициализации бросает `ConfigurationError`, кроме `__contains__`
(`:870-873`), который возвращает `False`. Вердикт: **Оставить** — несущая
конструкция lifecycle-гейта, покрыта `tests/test_profile_lifecycle.py`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 832-833 | `_inner_dict = None` | состояние UNINITIALIZED | `config.py:907` | Оставить |
| `_ensure_initialized` | 835-841 | гейт, бросающий `ConfigurationError` | единая точка fail-fast | `__getitem__`, `__getattr__`, `get`, `items`/`keys`/`values`, `__iter__`, `__len__` | Оставить |
| `__getitem__` | 843-844 | mapping-доступ | канонический API | весь runtime | Оставить |
| `__setitem__` | 846-853 | мутация прокси | «legacy-тесты, мутирующие proxy in-place» | только тесты | Упростить — если мутирующих тестов нет, метод можно убрать; **проверено частично**, полный перебор тестов не проводился |
| `__delitem__` | 855-857 | удаление ключа | симметрия `__setitem__` | 0 найденных вызывающих | Упростить — кандидат на удаление вместе с `__setitem__`; требует grep по тестам на `del SETTINGS` |
| `__getattr__` | 859-868 | attribute-доступ с маппингом `KeyError`→`AttributeError` | backward-compat | `SETTINGS.profile` и др. | Оставить |
| `__contains__` | 870-873 | `in` без инициализации | удобство `if "x" in SETTINGS` | runtime | Оставить |
| `__iter__` | 875-876 | итерация | совместимость с dict-API | runtime | Оставить |
| `__len__` | 878-879 | длина | совместимость | runtime | Оставить |
| `__repr__` | 881-884 | отладка с профилем | диагностика | логи | Оставить |
| `get` | 886-895 | `.get` **с** fail-fast (в отличие от `get_setting`) | «не маскировать lifecycle-ошибку» | `application_context.py:291`, `lib/lifecycle/gateway_runner.py:69` и др. | Оставить |
| `items` | 897-898 | `.items()` | совместимость | runtime | Оставить |
| `keys` | 900-901 | `.keys()` | совместимость | runtime | Оставить |
| `values` | 903-904 | `.values()` | совместимость | runtime | Оставить |

### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_parse_value` | 261-286 | типизация значения из `.secrets.env` (bool/int/float/JSON/CSV) | разбор секретов | `load_env:320` | Оставить |
| `_header_to_prefix` | 289-291 | `# ns: sub:` → префикс вложенности | парсер секций `.secrets.env` | `load_env:311` | Оставить |
| `load_env` | 294-322 | чтение `.secrets.env` в дерево | слой merge №3 | `_load_secrets_override:572`, тесты | Оставить |
| `_strip_jsonc_comments` | 325-370 | удаление `//` и `/* */` без порчи строк | `profiles/*.jsonc` | `load_config_json:388` | Оставить |
| `load_config_json` | 373-393 | загрузка JSON/JSONC; битый файл → пустой `AttrDict` | слой merge №1 и оверлей | `resolve_application_config:769`, `_merge_profile_overlay:617` | Оставить. Замечание: «битый → пустой» глушит опечатку в `config.json`; для `config.json` это потенциально опасно, но изменение поведения — не в моём scope |
| `_lift_agent_sections` | 396-431 | подъём `gateway.agent.*` в корень + hard-fail на неизвестное | обход ограничения схемы nanobot | `resolve_application_config:775` | Оставить |
| `_deep_merge` | 434-439 | рекурсивный merge | шаги merge | `resolve_application_config` ×3, `_merge_profile_overlay:621` | Оставить |
| `runtime_table` | 499-526 | имя runtime-таблицы по роли и профилю | единственное место вместо литералов | `tools/apply_test_profile_tables.py:33,52`, тесты | Оставить |
| `_load_session_manager_override` | 543-553 | слой merge №2 | per-deploy override pool/timeout | `resolve_application_config:777` | Оставить |
| `_load_secrets_override` | 556-573 | слой merge №3 | секреты в `cfg` | `resolve_application_config:782` | Оставить |
| `_export_secrets_to_env` | 576-584 | экспорт плоских ключей в `os.environ` | чтобы `${VAR}` резолвился | `resolve_application_config:786` | Оставить (побочный эффект задокументирован в докстринге `:576-584`) |
| `_export_runtime_env` | 587-600 | `NANOBOT_PYTHON`, `NANOBOT_PROJECT_ROOT` | пути MCP-сервера без хардкода | `resolve_application_config:790` | Оставить. Проверено: `ENTERPRISE_*` здесь **не** экспортируются — канон соблюдён |
| `_merge_profile_overlay` | 603-621 | оверлей профиля последним шагом | изоляция prod/test | `resolve_application_config:792` | Оставить |
| `validate_profile_overlay` | 624-662 | симметричная проверка оверлея (ровно 5 ключей) | ловит невалидный файл оверлея | `_merge_profile_overlay:620` | Оставить |
| `validate_runtime_isolation` | 665-696 | сверка 5 таблиц с профилем, hard-fail | не дать тестовому контуру писать в боевые таблицы | `resolve_application_config:796` | Оставить |
| `_resolve_env_refs` | 702-716 | рекурсивный резолв `${VAR}`; неизвестная → литерал | «импорт не должен падать без секрета» | `resolve_application_config:794` | Оставить |
| `_flatten_env` | 719-738 | рекурсивное «расплющивание» для env | `_export_secrets_to_env` | `:582` | Оставить |
| `_` | 741-743 | санация имени env-переменной | пробелы/дефисы → `_` | `_flatten_env:737` | Оставить. Замечание: односимвольное имя на уровне модуля — антипаттерн, но переименование не в scope |
| `resolve_application_config` | 746-798 | весь merge, 6 шагов | единственная точка формирования конфига | `_initialize_settings:954` | Оставить |
| `_initialize_settings` | 910-966 | lifecycle-ate; двойной вызов → ошибка | «профиль инициализируется entrypoint'ом» | `gateway.py:118`, `cli_agent.py` (`_parse_args`) | Оставить |
| `is_settings_initialized` | 969-976 | признак инициализации | fail-fast в standalone-скриптах и тестах | `tests/conftest.py:67`, `tests/test_profile_lifecycle.py:95,181,529,544`; спека `openspec/specs/configuration/profiles/spec.md:194,219,226` | Оставить (production-вызывающих нет, но это осознанный диагностический API из спеки) |
| `get_active_profile` | 979-986 | `SETTINGS["profile"]` | замена старого `_ACTIVE_PROFILE` | **0 production-вызывающих**; только `CHANGELOG.md:687` и упоминания в отчётах | **Удалить** (см. сводный список) |
| `get_setting` | 989-1022 | безопасный доступ с `default`; **не** бросает при UNINITIALIZED | backward-compat для module-level чтений | `lib/lifecycle/gateway_runner.py:69,74`, `lib/services/*` | Оставить. Осознанное отличие от `_LazySettings.get` задокументировано в `:999-1011` |
| `require_setting` | 1025-1040 | строгий доступ, без fallback | «отсутствие настройки — ошибка» | runtime | Оставить |

---

## `gateway.py` — 699 LOC

**Назначение.** Точка входа демона gateway: argparse → lifecycle-ate →
`ApplicationContext.create(role="gateway")` → рукопожатие с платформой →
`_run` с restart-циклом.

**Что делает.** Побочные эффекты на уровне модуля: `gateway.py:36`
`ensure_console_colors()` (может выставить `NO_COLOR` и ANSI-фильтр) и
`gateway.py:91-99` — четыре `os.environ.setdefault` для кодировок и локали.
`gateway.py:29` импортирует `ConfigurationError` из `config` **на уровне
модуля**, до `sys.path`-настройки в `main` (`:685-688`) — работает только
потому, что `config.py` лежит рядом.

Handshake-путь: `_connect_enterprise_mcp:191-246` поднимает stdio-сессию
(сетевых вызовов нет, но поднимается **процесс**), затем
`_report_enterprise_mcp_health:249-295` делает по одной дешёвой операции на
capability (`vectors`→`list_indexes`, `data`→`schema_check`, `audit`→`list_scripts`),
затем `_verify_platform_table_alignment:297-368` сверяет имена таблиц и бросает
`ConfigurationError` при расхождении. `_report_db_pool_startup:535-570`
выполняет **реальный ping пула БД**. `_check_websocket_port_available:572-624`
запускает **подпроцесс `netstat`** (`:636-643`) и ищет PID слушателя.

**Зачем нужен.** Единственная production-точка входа для daemon-режима;
реализует Error Lifecycle Contract (`ConfigurationError` → `stderr` + код 2) и
ordered startup (спека `openspec/specs/runtime/entrypoints/spec.md`).

**Вердикт.** Оставить; **Упростить** — убрать мёртвый `_gateway_print_llm_calls`
и починить порядок `console`; **Слить** с `cli_agent.py` (4 общих helper'а).

**Обоснование.** Файл несёт уникальную логику (проверка порта websocket,
сверка таблиц, restart-цикл), но 4 из 18 его функций — дословные или
почти дословные копии `cli_agent.py`, а одна (`_gateway_print_llm_calls`)
вообще никем не вызывается.

**Доказательства.** Точка входа: `python gateway.py`, `main:662`,
`if __name__ == "__main__":698`. Тесты: `tests/test_gateway.py`,
`tests/test_gateway_enterprise_mcp_startup.py` (33 ссылки на
`_verify_platform_table_alignment`), `tests/test_logging_bridge.py`.

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_parse_args` | 39-76 | argparse без `parser.error` (иначе минует `ConfigurationError`-boundary) | Error Lifecycle Contract | `main:674` | Оставить |
| `_entrypoint_main` | 105-176 | startup + тело; `--smoke` печатает баннер и выходит 0 | smoke для интеграционных тестов | `main:691` | Оставить |
| `_project_version` | 178-188 | ленивое чтение `project.version` из `SETTINGS` | баннер `gateway.py:150` | `gateway.py:150`; `tests/test_config_keys.py:278`, `tests/test_project_settings.py:576` | Оставить |
| `_connect_enterprise_mcp` | 191-246 | подъём сессии + health-сводка; отказ печатается и **пробрасывается** | «платформа не отвечает» видно на старте | `gateway.py:433` | **Слить с `cli_agent.py:287`** |
| `_report_enterprise_mcp_health` | 249-295 | по одной операции на capability; отказ capability не роняет старт | частично нерабочая плаформа видна | `_connect_enterprise_mcp:227` | Оставить |
| `_verify_platform_table_alignment` | 297-368 | сверка имён таблиц платформы со своими | оверлей объявлен в двух файлах | `gateway.py:294`; `cli_agent.py:392` (импорт); 33 ссылки в тестах | Оставить (уже переиспользуется CLI) |
| `_indexes_line` | 370-385 | форматирование строки индексов capability | сводка при старте | `_report_enterprise_mcp_health` | Оставить |
| `_schema_line` | 387-396 | форматирование строки схемы | сводка при старте | `_report_enterprise_mcp_health`; `cli_agent.py:392` | Оставить |
| `_scripts_line` | 398-407 | форматирование строки реестра скриптов | сводка при старте | `_report_enterprise_mcp_health` | Оставить |
| `_run` | 409-468 | handshake → `attach_log_transport` → `channels.start_all` → `await stop_event` | тело демона | `GatewayRunner.run_forever` (`gateway.py:167`) | Оставить |
| `script_dir_for_runtime` | 472-482 | ленивый кэш абсолютного пути | избежать module-level `Path` | `gateway.py:679` | **Слить с `cli_agent.py:427`**. Докстринг `:475-477` лжёт про side-effects |
| `_configure_logging` | 485-503 | настройка loguru через общий мост stdlib→loguru | единый уровень логов | `gateway.py:147` | **Слить с `cli_agent.py:257`**. Параметр `settings` **не используется** — мёртвый |
| `_gateway_print_llm_calls` | 506-517 | чтение `gateway.print_llm_calls` | — | **0 вызывающих** | **Удалить.** Флаг рабочий через `application_context.py:99→403` |
| `_gateway_print_worker_activity` | 520-532 | чтение `gateway.print_worker_activity` | вывод активности пула | `gateway.py:414` → `ChannelFactory` → `lib/channels/postgres_channel.py:174,346,437,558,678` | Оставить |
| `_report_db_pool_startup` | 535-570 | прогрев пула реальными подключениями | видно, сколько воркеров не смогло подключиться | `gateway.py:447` | Оставить (побочный эффект — сетевые подключения, задокументирован) |
| `_check_websocket_port_available` | 572-624 | проверка, что порт свободен; выводит PID, если занят | диагностика при старте | `gateway.py:164`; `tests/test_gateway.py:262` | Оставить. Докстринг `:592` упоминает «прогрев кэша DuckDB» — **остаток снятой подсистемы**, поправить |
| `_find_listener_pid` | 626-656 | PID слушателя через `netstat` (только Windows) | часть проверки порта | `_check_websocket_port_available:613` | Оставить с оговоркой: платформенно-зависимо, на Linux вернёт `None` (тихо) |
| `main` | 662-695 | boundary: parse → sys.path → `_entrypoint_main`; `ConfigurationError` → код 2 | единый error-lifecycle | `if __name__`:698; `benchmarks`, CI | Оставить |

---

## `cli_agent.py` — 487 LOC

**Назначение.** Точка входа интерактивного CLI-агента (REPL), профилем всегда
`test` (`:60-62`).

**Что делает.** Побочные эффекты на уровне модуля: `cli_agent.py:103-104` —
`os.environ.setdefault` для `PYTHONUTF8`/`PYTHONIOENCODING` (та же логика, что
`gateway.py:91-92`, продублирована). `_migrate_cron_store:271-281` **переносит
файл** `cron/jobs.json` через `shutil.move` — файловый побочный эффект на
диске, обёрнутый в голый `except Exception: pass`, который глотает в том числе
`ImportError`. `_run_cli_repl:207-248` поднимает живой `asyncio` loop, в
`body()` выполняет рукопожатие с `enterprise-mcp` (`:238`), подключает
транспорт журнала (`:239`) и только затем запускает REPL.

**Зачем нужен.** Единственный интерактивный вход. Обязан поднимать
собственный `ApplicationContext` (открытый change
`unify-runtime-channels` предлагает это убрать).

**Вердикт.** Оставить; **Упростить** (убрать мёртвые импорты и `__get_cron`);
**Слить** с `gateway.py` (3 helper'а).

**Обоснование.** Функционально востребован, но несёт дубли и один
неиспользуемый импорт; дополнительно делает рукопожатие, которое `AGENTS.md`
объявляет отсутствующим.

**Доказательства.** Точка входа: `python cli_agent.py`, `main:442`,
`if __name__":486`. Тесты: `tests/test_cli_agent.py` (в т.ч. `_migrate_cron_store`
на `:493-531`), `tests/test_log_transport_wiring.py`
(`test_handshake_runs_before_repl_and_transport`).

#### class `CliStartupError` (строки 32-51, 0 методов)

Назначение: `RuntimeError` для отказа не-конфигурационного типа; `main:478-482`
превращает его в `stderr` + код 1 (в отличие от `ConfigurationError` → 2).
Вердикт: **Оставить** — различение кодов выхода задокументировано в
`:449-456` и закреплено тестами.

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_parse_args` | 54-107 | argparse; профиль прибит к `test`, если не задан | CLI-контракт спеки | `main:459` | Оставить |
| `_entrypoint_main` | 110-156 | выбор ветки: smoke / vanilla / patched | единая точка ветвления | `main:474` | **Упростить**: `:121-122` импортируют `run_repl` и `DisplayConfig`, которые в этой функции **не используются** (ветка smoke идёт в `ApplicationContext.create`, а `_run_vanilla`/`_run_patched` → `_run_cli_repl:222-223` импортируют их заново) |
| `_run_vanilla` | 158-161 | тонкая обёртка над `_create_cli_context` | симметрия с `_run_patched` | `_entrypoint_main` | Оставить |
| `_run_patched` | 163-178 | то же, но печатает признак патчей | симметрия ветвей (спека `:302-306`) | `_entrypoint_main` | Оставить |
| `_create_cli_context` | 181-204 | `ApplicationContext.create(role="cli", ...)` + sys.path | сборка контекста | `_run_vanilla`, `_run_patched` | Оставить |
| `_run_cli_repl` | 207-248 | живой loop: handshake → transport → REPL | «asyncio.run блокирует, всё обязательное внутри body()» | `_run_vanilla`, `_run_patched`; `tests/test_log_transport_wiring.py:344-381` | Оставить. **Здесь `:238` противоречит `AGENTS.md`** |
| `__get_cron` | 251-254 | заглушка, всегда `None` | — | **0 вызывающих** | **Удалить** (двойное подчёркивание в модульном scope — след неудачного «приватного» соглашения) |
| `_configure_logging` | 257-268 | loguru из `cli.log_level` | — | `cli_agent.py:195` | **Слить с `gateway.py:485`**; расходится с ним по ключу, дефолту и `env_var` |
| `_migrate_cron_store` | 271-281 | перенос `jobs.json` в workspace | миграция старого расположения | `cli_agent.py:196`; `tests/test_cli_agent.py:493,516` | Оставить. Замечания: голый `except Exception: pass` глотает всё; `get_cron_dir` в установленном `nanobot` существует (`hasattr` → `True`), так что функция не no-op |
| `_connect_enterprise_mcp` | 287-340 | подъём сессии; отказ → `CliStartupError` | «REPL не поднимается ни в одной ветви» | `cli_agent.py:238` | **Слить с `gateway.py:191`** |
| `_verify_platform_tables` | 367-425 | сверка имён таблиц; импортирует правило **из `gateway`** | CLI прибит к `test` — оверлей тут вероятнее всего и ломается | `cli_agent.py` (в `_connect_enterprise_mcp`) | Оставить. Побочный эффект: `cli_agent.py:392` импортирует `gateway`, а тот на импорте выполняет `os.environ.setdefault` (`gateway.py:91-99`) и `ensure_console_colors()` (`:36`) — то есть **импорт одного entrypoint'а из другого тянет побочные эффекты** |
| `script_dir_for_runtime` | 427-439 | ленивый кэш пути | тот же смысл, что в gateway | `cli_agent.py:139,190,464` | **Слить с `gateway.py:472`** |
| `main` | 442-483 | boundary; `ConfigurationError`→2, `CliStartupError`→1 | Error Lifecycle Contract | `if __name__":486` | Оставить |

---

## `lib/core/__init__.py` — 1 LOC

**Назначение.** Маркер пакета `lib.core` с однострочным докстрингом
(«единая точка создания и связывания сервисов приложения»).

**Что делает.** Ничего: ни импортов, ни реэкспортов, ни кода.

**Зачем нужен.** Делает `lib.core` импортируемым пакетом; без него
`from lib.core.application_context import ApplicationContext`
(`gateway.py:120`) не разрешился бы (каталог без `__init__.py` работает только
как namespace-package, что зависит от версии интерпретатора и способа запуска).

**Вердикт.** Оставить.

**Обоснование.** Инфраструктурно необходим; альтернатива (namespace-package)
меняет модель импорта без выигрыша. Удаление сломает
`gateway.py:120`, `cli_agent.py:189` и 4 теста, патчащих `lib.core.*` в `sys.modules`.

**Доказательства.** `gateway.py:120`, `cli_agent.py:189`;
`tests/test_application_context.py`, `tests/test_application_context_logging.py:205`
(`import lib.core.project_settings`), `tests/test_no_legacy_imports.py:53`.

---

## `lib/core/application_context.py` — 1680 LOC

**Назначение.** Composition root: единственное место, где собираются все
сервисы, применяются патчи, регистрируются хуки и tool'ы.

**Что делает.** `create()` (`:196-517`) — 320-строчный метод, выполняющий
~15 шагов в фиксированном порядке: `RuntimePatcher.apply_all()` (monkey-patch
6 методов `AgentLoop`), `AgentFactory.create()`, баннеры инвентаря,
`register_project_tools`, `CompactionEventSubscriber`, `RuntimeEventsSubscriber`.
Побочные эффекты: запись в `os.environ` не производит, но **создаёт процесс**
`enterprise-mcp` лениво (`:480` — клиент, процесс поднимается при первом
`list_operations`), пишет `data_store/logs/gateway-events-fallback.jsonl`
(`:551-558`), поднимает пул PostgreSQL (`:1663-1671`), регистрирует таймер
readiness-проверок (`:1143-1243`, в т.ч. **реальный ping БД** в `:1214-1243`),
и применяет 6 monkey-patch'ей к upstream `nanobot` (риск при апгрейде).

**Зачем нужен.** Без него нет ни одного сервиса. `AGENTS.md` и
`openspec/specs/runtime/startup-schema-validation` делают его единственной
допустимой точкой сборки.

**Вердикт.** Оставить. Метод `create()` — кандидат на разбиение (не в моём
scope), но не на удаление.

**Обоснование.** Живой и покрытый тестами. Проблемы — в остатках: неиспользуемое
поле `enable_audit`, неиспользуемый параметр, динамические атрибуты, инвертированный
докстринг `_make_cron_service` и комментарии про удалённый `_init_cache_runtime`.

**Доказательства.** Вызывается из `gateway.py:123`, `cli_agent.py:184`
и ~15 тестов (`tests/test_application_context.py`,
`test_application_context_role.py`, `test_application_context_logging.py`,
`test_log_transport_wiring.py`, `test_turn_observability_events.py`).
Спека: `openspec/specs/runtime/startup-schema-validation`.

#### class `ApplicationContext` (строки 117-788, 5 методов, ~30 полей)

**Класс — не `@dataclass`**, хотя комментарий `:157-158` говорит «dataclass без
`__slots__`». Поля объявлены аннотациями с дефолтами; часть атрибутов
(`profile` `:257`, `runtime_patch_report` `:454`, `project_tools_result` `:494`)
**присваивается динамически и не объявлена** — потребители вынуждены использовать
`getattr(..., default)`. Вердикт: **Оставить**.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `create` | 196-517 | вся сборка; `role` keyword-only, `profile` — **не** параметр | composition root | `gateway.py:123`, `cli_agent.py:184`, тесты | Оставить. Замечание: 320 строк в одном методе — кандидат на декомпозицию |
| `attach_log_transport` | 519-615 | перевод журнала на `log_events` платформы; зовётся **дважды** (из `start()` и из живого loop) | журнал идёт операцией платформы, а не прямой записью | `application_context.py:686`, `gateway.py:439`, `cli_agent.py:239` | Оставить (двойной вызов — задокументированный контракт, не дублирование) |
| `start` | 617-727 | lifecycle start: schema-validation, `channels.start_all`, cron, подписчики, лог health | подъём runtime | `gateway.py:451` (`_run`), `cli_agent.py:244` | Оставить |
| `stop` | 729-788 | graceful shutdown: `shutdown_all()` → close MCP → `bus.drain()` → usage_store → `_stop_db_pool()` | корректная остановка | `gateway.py:455` (fallback), `cli_agent.py:248` | Оставить |
| `_validate_runtime_schema` | 790-854 | один SELECT к `information_schema.tables` за 5 таблицами | fail-fast до старта агента | `start` | Оставить. Вложенный `_bounded_fetch:841` — защита от бесконечного `fetchmany`; вложен в метод |

**Поля без потребителя:** `enable_audit` (`:164`) — пишется в `:260`, не
читается нигде в репо. Остаток снятого audit-сервиса; закреплён тестом
`tests/test_application_context_cache_lifecycle.py:55-57`. **Удалить.**

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_resolve_enable_kwargs` | 58-114 | извлечь 4 deprecated-флага из `**kwargs`; принятые → `DeprecationWarning` | compatibility-граница | `create:250` | Оставить до change `remove-deprecated-enable-kwargs`. При удалении: убрать `enable_audit` из `:54` и из `create` |
| `_print_startup_block` | 856-871 | печать баннера конфигурации | диагностика старта | `create`, `start` | Оставить |
| `_log_connected_hooks` | 873-898 | лог подключённых хуков | старт-инвентарь | `create` | Оставить |
| `_emit_hook_inventory_banner` | 900-972 | prominent-баннер missing/unexpected по `runtime_inventory` | соответствие `runtime_inventory.py` | `create` | Оставить |
| `_emit_patch_inventory_banner` | 974-1040 | баннер патчей | то же | `create:471` | Оставить |
| `_emit_project_tools_inventory_banner` | 1042-1141 | баннер project tools | то же | `create:495` | Оставить |
| `_register_readiness_checks` | 1143-1243 | регистрация readiness-проверок, вкл. **реальный ping БД** | `ctx.runtime_readiness` | `start` | Оставить. Вложенные `_detail:1180` и `check_postgres:1186` (внутри — `_ping:1214`) отмечены как вложенные |
| `_make_config_service` | 1245-1267 | сборка `ConfigService` | доступ к конфигу | `create:264` | Оставить |
| `_resolve_agent_id` | 1269-1281 | имя агента | идентификация | `create` | Оставить |
| `_make_db_logging` | 1283-1386 | `DbLoggingService` по `logging.db.*` | журнал | `create:326` | Оставить |
| `_make_enterprise_mcp` | 1388-1410 | клиент платформы; ничего, кроме раздела конфига, не передаёт | единственный путь к данным | `create:480` | Оставить, **но** вызов безусловен — см. находку выше |
| `_make_cron_service` | 1413-1422 | `CronService` | задачи по расписанию | `create:371` (только `role="gateway"`) | Упростить: докстринг `:1414` говорит «для CLI-режима» — инвертирован. Плюс `enable_cron` в `config.json` **не задан** → в production `CronService` не создаётся никогда (проверено: ключ отсутствует в `config.json`) |
| `_make_session_cold_sync_service` | 1425-1498 | cold-mirror JSONL→PG | гибридное хранилище | `create` | Оставить |
| `_make_compaction_service` | 1500-1529 | `ContextCompactionService` | единая точка записи факта сжатия | `create` | Оставить |
| `_make_compaction_subscriber` | 1531-1552 | подписка на `ContextCompactionEvent` | путь сжатия из upstream | `create` | Оставить |
| `_wrap_bus_publish` | 1554-1571 | обёртка `publish_inbound`/`publish_outbound` для логов | наблюдаемость | `_create_bus` | Оставить (вместо отдельного `bus_factory.py`) |
| `_create_bus` | 1573-1592 | сборка `MessageBus` | DI | `create` | Оставить |
| `_make_usage_store` | 1594-1639 | `LLMUsageStore` (SQLite WAL) | usage-метрики | `create` | Оставить |
| `_configure_db_pool` | 1641-1661 | настройка пула | пул | `create` | Оставить |
| `_start_db_pool` | 1663-1671 | запуск пула | пул | `start` | Оставить |
| `_stop_db_pool` | 1673-1680 | остановка пула | пул | `stop:785` | Оставить |

---

## `lib/core/agent_factory.py` — 419 LOC

**Назначение.** Сборка `AgentLoop` с каноническим порядком хуков и per-turn
`DatabaseLoggingHook`-инстансами.

**Что делает.** `create()` (`:68-257`) — единая точка, где склеиваются
`ToolAuditHook` → `TerminalToolPrintHook` → `RepeatGuardHook` (последний
**всегда**, включая `mode="off"`, чтобы канон `runtime_inventory` совпадал с
фактом) → per-turn хуки из `hook_factories`. Побочные эффекты:
`_wrap_provider_snapshot_loader` (`:260-316`) подменяет приватный метод
provider'а (`_load_snapshot`?) — **monkey-patch приватного API upstream
`nanobot`**, риск при апгрейде; вложенный `_wrapped:290` — замыкание.

**Зачем нужен.** Гарантирует, что порядок хуков одинаков везде, и что
`AgentLoop` создаётся **ровно один раз** (`:127-130`).

**Вердикт.** Оставить; **Упростить** — убрать неиспользуемый параметр
`framework_hooks`.

**Обоснование.** Ядро конструирования агента, покрыто
`tests/test_runtime_inventory.py` и `tests/test_agent_factory*.py`. Замечания —
в одном мёртвом параметре и в устаревшем комментарии.

**Доказательства.** `application_context.py` (вызов `create`),
`tests/test_runtime_inventory.py`, `tests/test_repeat_guard_hook.py:339`
(`GatewaySettings`).

#### class `AgentFactory` (строки 51-419, 7 методов)

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `create` | 68-257 | сборка `AgentLoop` + порядок хуков + per-turn фабрики | единственная точка | `application_context.py` | Оставить. Вложенные `get_model:200` и `_populate_box:223` отмечены как вложенные |
| `_wrap_provider_snapshot_loader` | 260-316 | подмена приватного метода provider'а | usage-обёртка на snapshot | `create` | Оставить с оговоркой: **приватный API upstream**, вложенный `_wrapped:290` |
| `_read_repeat_guard_settings` | 319-332 | чтение `gateway.agent.repeat_guard.*` | конфиг защитника | `create` | Оставить |
| `_import_tool_audit_hook` | 335-345 | ленивый импорт | порядок хуков | `create` | Оставить |
| `_import_terminal_tool_print_hook` | 348-364 | ленивый импорт | порядок хуков | `create` | Оставить |
| `_import_repeat_guard_hook` | 367-377 | ленивый импорт | хук подключается **всегда** | `create` | Оставить |
| `_build_database_logging_factory` | 380-419 | фабрика per-turn хуков журнала | `print_llm_calls` и батчи | `create:210` | Оставить |

**Параметр `framework_hooks` (`:78`) — не передаётся ни одним вызывающим.**
Докстринг `:181-182` сам признаёт: «Сейчас список пуст». **Упростить** —
удалить параметр и три строки, которые его обрабатывают.

---

## `lib/core/project_settings.py` — 814 LOC

**Назначение.** Типизированная (по замыслу) валидация секций `config.json`
через pydantic.

**Что делает.** `validate_project_settings:777-814` прогоняет merged `SETTINGS`
через `ProjectSettings` и превращает `ValidationError` в `ConfigurationError` с
человекочитаемым списком проблем. `SkillsSettings._validate_skill_sections`
(`:680-718`) спускается в каждую секцию `skills.<name>` через `SkillSettings`
(`extra="forbid"`) — единственное место в модуле, где опечатка действительно
ловится.

**Зачем нужен.** Роль fail-fast'а реальна, но **фактически реализована в
5 моделях из 31** (проверено исполнением: `ProjectMetadataSettings`,
`SkillSettings`, `TableEntry`, `VectorIndexConfig`, `VectorIndexEntry`; остальные
26 — `extra="allow"` через `_StrictOptional:55-60`). Единственное
production-чтение результата — `application_context.py:1340`
(`project_settings.logging.db.flush_interval_sec`); всё остальное
производится для валидации и сразу забывается.

**Вердикт.** **Упростить** (радикально).

**Обоснование.** 814 LOC модели, из которых читается одно поле. Ветки
`VectorIndexSettings`/`VectorIndexEntry`/`VectorIndexConfig` (~150 LOC)
дублируют `mcp-platform/platform.json → vectors.indexes` — это ровно тот
«второй источник правды», который `AGENTS.md` запрещает; ветки
`Skill*Settings` (~100 LOC) обслуживают настройки, которые читает платформа,
а не агент. Оставлять 814 LOC ради одного `flush_interval_sec` — избыточно.
**Что именно убрать (по убыванию уверенности):**
1. `VectorIndexSettings:141-183`, `VectorIndexEntry:437-467`,
   `VectorIndexConfig:469-513` — дублируют объявление платформы; перед
   удалением — снять `vector.index.*` из `config.json` и проверить
   `tests/test_project_settings.py:401-425`;
2. `SkillChunkingSettings:545-564`, `SkillBriefContextSettings:566-581`,
   `SkillExecutionContextBatchingSettings:583-598` — настройки удалённых
   потребителей (`text_splitter`, `skill_config`, skill `legal_summarizer`);
3. либо, если оставлять, **ужесточить** `_StrictOptional` до `extra="forbid"`
   — тогда модуль начнёт соответствовать своему описанию, но потребует
   чистки `config.json` (сейчас в `cli.*` 9 ключей вне модели — см. ниже).

**Доказательства.** Импорт: `application_context.py:271` (единственный
production-импорт) и ~10 тестовых файлов. Чтение: только
`application_context.py:1340`.

#### Наблюдение: модель «строгая» только на 5/31

`_StrictOptional:55-60` объявляет `ConfigDict(extra="allow")` с формулировкой
«неизвестные ключи разрешены, известные — типизированы». Проверено исполнением
на живом `config.json`: `validate_project_settings` проходит, а
`ps.cli` содержит 11 полей, из которых модель `CliSettings:341-343` объявляет
**два** (`show_context_window`, `max_iterations`). Остальные 9
(`show_reasoning`, `show_tool_calls`, `show_tool_results`, `show_tool_params`,
`show_progress`, `typewriter_speed`, `llm_timeout`, `exec_timeout`, `log_level`,
`repl_idle_timeout_sec`) проходят только потому, что `extra="allow"`.
**C.**

#### Классы

| Класс | Строки | Назначение | Зачем нужен | Кто читает | Вердикт |
|---|---|---|---|---|---|
| `_StrictOptional` | 55-59 | база секций, `extra="allow"` | forward-compat | все секции | Упростить (см. выше) |
| `PostgresChannelSettings` | 61-66 | pool/timeout канала | конфиг канала | `lib/channels` через `ConfigService`, **не** через pydantic | Упростить — модель не читается, конфиг идёт мимо |
| `CompactSettings` | 69-72 | `gateway.compact.*` | сжатие | `lib/services/context_compaction.py` читает `SETTINGS` напрямую | Упростить |
| `StartupSchemaValidationSettings` | 75-99 | pre-startup проверка 5 таблиц | fail-fast | `application_context._validate_runtime_schema` читает `SETTINGS` напрямую | Упростить |
| `StartupSettings` | 101-105 | контейнер `gateway.startup.*` | — | косвенно | Упростить |
| `ErrorMessagesSettings` | 107-139 | `internal_error`, `log_to_db` | fallback-ответ | `lib/services/turn_delivery_factory.py` через `SETTINGS` | Упростить |
| `VectorIndexSettings` | 141-183 | `gateway.vector.*` | — | **никто**; докстринг `:155-162` признаёт | **Удалить** — дублирует `platform.json` |
| `VectorInfrastructureSettings` | 186-197 | `gateway.vector.index.*` | — | **никто** | **Удалить** вместе с предыдущим |
| `HeartbeatSettings` | 199-202 | `gateway.heartbeat.*` | — | **никто** (heartbeat снят с `cache_ownership`) | **Удалить** |
| `UsageStoreSettings` | 204-216 | `gateway.usage_store.*` | usage-метрики | `application_context._make_usage_store` через `SETTINGS` | Упростить |
| `SessionColdSyncSettings` | 218-231 | `gateway.session_cold_sync.*` | cold-mirror | `_make_session_cold_sync_service` через `SETTINGS` | Упростить |
| `GatewaySettings` | 233-279 | контейнер `gateway.*`; `print_llm_calls` | — | `application_context.py:99` читает **сырой dict**, не модель | Упростить. Метод `_reject_legacy_renamed_sections:251-278` — живой (использует `_LEGACY_GATEWAY_KEYS`) |
| `GatewayRepeatGuardSettings` | 281-338 | `gateway.repeat_guard.*` | защитник от циклов | `agent_factory._read_repeat_guard_settings`; `tests/test_repeat_guard_hook.py:26`, `tests/contract/test_repeat_guard_hook_contract.py:38` | Оставить. Метод `_reject_patterns:318-338` — живой guard |
| `CliSettings` | 341-343 | `cli.show_context_window`, `cli.max_iterations` | — | `application_context.py:280` читает `SETTINGS` напрямую | Упростить (9 из 11 ключей вне модели) |
| `ChannelsSettings` | 346-348 | `channels.postgres`, `document_text_threshold` | — | через `ConfigService` | Упростить |
| `LoggingDbSettings` | 351-371 | `logging.db.*` | **единственное реально читаемое поле** | `application_context.py:1340` | Оставить. Метод `_default_flush_interval_sec:356-371` — источник канонического дефолта `5.0`, живой |
| `LoggingSettings` | 374-375 | `logging.db` | — | `application_context.py:1340` | Оставить |
| `EnterpriseMcpSettings` | 378-403 | `enterprise_mcp.*` | путь к серверу, профиль | `client_from_settings` читает `SETTINGS` напрямую | Оставить (тесты `test_enterprise_mcp_config.py:80,126`) |
| `TableEntry` | 406-434 | элемент `tables` skill'а | разбор объявления платформой | платформа; в агенте только валидация | Упростить |
| `VectorIndexEntry` | 437-467 | элемент `vector_indexes` | — | **никто** | **Удалить** |
| `VectorIndexConfig` | 469-513 | `storage_table`, `default_root`, `signature_table` | — | **никто**; `signature_table` уже удалён по change `remove-vector-index-store` | **Удалить** |
| `SkillCliSettings` | 515-526 | `skills.*.cli.*` | параметры CLI навыка | навык читает `SETTINGS` напрямую | Упростить |
| `SkillLlmSettings` | 528-543 | `skills.*.llm.*` (execution policy) | budget-прогона | навык | Упростить |
| `SkillChunkingSettings` | 545-564 | `skills.*.chunking.*` | — | **никто**; докстринг `:548-551` ссылается на удалённый `lib.services.text_splitter.split_text` | **Удалить** (D: строка лжёт) |
| `SkillBriefContextSettings` | 566-581 | `skills.*.brief_context.*` | — | **никой**; докстринг `:566-576` ссылается на удалённые `lib.core.skill_config.get_brief_context_config` и skill `legal_summarizer` | **Удалить** (D: строка лжёт) |
| `SkillExecutionContextBatchingSettings` | 583-598 | батчинг контекста | — | **никто** | **Удалить** |
| `SkillExecutionSettings` | 600-612 | `skills.*.execution.*` | — | **никто** | **Удалить** |
| `SkillSettings` | 615-658 | секция skill'а, `extra="forbid"` | ловит опечатки | `SkillsSettings._validate_skill_sections` | Оставить — **единственная реально работающая строгость** модуля |
| `SkillsSettings` | 662-718 | контейнер `skills.*` | прогоняет каждую секцию | `ProjectSettings.skills` | Оставить. Метод `_validate_skill_sections:682-718` — живой |
| `ProjectMetadataSettings` | 721-740 | `project.version` | баннер | `gateway._project_version:178-188` | Оставить (`extra="forbid"`) |
| `ProjectSettings` | 743-754 | корневая проекция | точка входа валидации | `validate_project_settings:791` | Оставить |
| `_LegacyGatewaySectionsError` | 757-763 | маркер legacy-секции | unwrap внутри `ValidationError` | `validate_project_settings:792,804` | Оставить |
| `validate_project_settings` | 777-814 | fail-fast со списком проблем | единственная точка входа | `application_context.py:273`; ~40 мест в тестах | Оставить |

**Модульные константы.** `__all__:35-52` — **неполон**: объявляет 17 имён,
тогда как модуль экспортирует 31 модель. Отсутствуют, в частности,
`ErrorMessagesSettings`, `ChannelsSettings`, `CliSettings`, `CompactSettings`,
`EnterpriseMcpSettings`, `GatewayRepeatGuardSettings`, `HeartbeatSettings`,
`LoggingDbSettings`, `LoggingSettings`, `PostgresChannelSettings`,
`ProjectMetadataSettings`, `SkillChunkingSettings`,
`SkillExecutionContextBatchingSettings`, `SkillExecutionSettings`,
`VectorIndexSettings`. При этом `tests/test_project_settings.py:201-202`
проверяет экспорт через **прямой импорт**, а не `__all__`, поэтому расхождение
не ловится. **C — привести `__all__` в соответствие или удалить.**

`_LEGACY_GATEWAY_KEYS:769-774` — мапа legacy-имён; живой, используется
`_reject_legacy_renamed_sections:251-278`. **Оставить.**

---

## `lib/lifecycle/__init__.py` — 1 LOC

**Назначение.** Маркер пакета `lib.lifecycle`.

**Что делает.** Ничего — одна строка докстринга.

**Зачем нужен.** Делает `lib.lifecycle` импортируемым; без него
`from lib.lifecycle.gateway_runner import GatewayRunner` (`gateway.py:121`)
не разрешался бы надёжно.

**Вердикт.** Оставить.

**Обоснование.** Инфраструктурно необходим, ноль стоимости.

**Доказательства.** `gateway.py:121`; `tests/test_gateway_runner.py`,
`tests/test_shutdown_coordinator.py`.

---

## `lib/lifecycle/gateway_runner.py` — 113 LOC

**Назначение.** Цикл перезапуска gateway с экспоненциальным backoff.

**Что делает.** `run_forever` (`:78-108`) вызывает `run_once()`; исключение →
лог с traceback → `sleep(delay)` → `delay = min(delay*2, max_delay)`;
нормальный возврат → выход; `KeyboardInterrupt` → тихий выход. Побочных
эффектов нет, кроме логирования и сна. Параметры по умолчанию читаются из
`get_setting("gateway", "restart_*_delay_sec")` (`:69,74`) — через
`get_setting`, который **не** бросает при неинициализированных `SETTINGS`,
поэтому объект конструируется и до lifecycle-гейта.

**Зачем нужен.** Единственная точка, где решается «падает ли процесс» против
«успешно завершился». `GatewayRunner` ловит `EnterpriseMcpUnavailable` и тем
самым даёт retry с backoff вместо тихой деградации.

**Вердикт.** Оставить; **Удалить** `reset_backoff`.

**Обоснование.** Механика backoff — живой контракт. `reset_backoff` не имеет
ни одного вызывающего ни в runtime, ни в тестах, а его докстринг утверждает
обратное.

**Доказательства.** `gateway.py:121,167`; `tests/test_gateway_runner.py`
(7 тестов, ни один не вызывает `reset_backoff`).

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 51-76 | задержки из конфига; `sleep` инъецируется | тестируемость без реального сна | `gateway.py:167` | Оставить |
| `run_forever` | 78-108 | сам цикл | retry-политика | `gateway.py:167-168` | Оставить |
| `reset_backoff` | 110-113 | вернуть начальную задержку | — | **0 вызывающих** | **Удалить** |

**Строка, которая лжёт.** `:17` — «БД (Postgres, Redis) не успевают
восстановиться». `redis_channel` удалён, каналов один — PostgreSQL. **D.**

---

## `lib/lifecycle/shutdown_coordinator.py` — 120 LOC

**Назначение.** Упорядоченная (LIFO) остановка сервисов с поглощением
исключений.

**Что делает.** `register` (`:49-65`) резолвит stop-функцию через
`_resolve_stop_fn` (`:86-120`) — порядок поиска `close` → `stop` → `shutdown` →
`terminate`; функция без методов вызывается напрямую; подходит
`inspect.isfunction`/`isbuiltin` (намеренно **не** `callable`, иначе
`MagicMock().close()` не вызвался бы — задокументировано в `:102-106`).
`shutdown_all` (`:67-79`) обходит `reversed()` и глотает исключения каждого
компонента. Находится в `ApplicationContext.start`/`stop` как
`_shutdown: ShutdownCoordinator`.

**Зачем нужен.** Гарантирует, что зависимости остановятся после потребителей,
и что один битый сервис не оставит процесс висящим.

**Вердикт.** Оставить.

**Обоснование.** Механика живая и покрыта `tests/test_shutdown_coordinator.py`.
Замечание — только в докстринге: он перечисляет `sync_service`
(`:11`, удалённый `pg_duckdb_sync_service`) и «аудит-сервисы» (`:5-6`, тоже
удалены) в «типичном порядке регистрации». **D — поправить докстринг;** сам
код корректен.

**Доказательства.** `application_context.py:621,623`;
`tests/test_shutdown_coordinator.py` (в т.ч. `clear()` на `:30`).

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 46-47 | пустой реестр | состояние | `application_context.py:621` | Оставить |
| `register` | 49-65 | добавить компонент | сборка stop-списка | `application_context.py` (в `start`) | Оставить |
| `shutdown_all` | 67-79 | LIFO-остановка, исключения глотаются | гарантия завершения | `application_context.py:734` | Оставить |
| `clear` | 81-83 | очистить реестр | изоляция тестов | `tests/test_shutdown_coordinator.py:30` | Оставить (осознанный тестовый хелпер) |
| `_resolve_stop_fn` (модульная функция) | 86-120 | найти stop-метод | единая семантика | `register:65` | Оставить |

---

## НЕ РАЗОБРАНО

- `docs/audit/reports/01-core-entrypoints.md` (старый отчёт, 82 KB) — не
  разбирался построчно; использован только как признак расхождения строк
  (1816/1502 против текущих 1680). Его выводы **не** переиспользовались.
- Динамические атрибуты `ApplicationContext` (`profile`, `runtime_patch_report`,
  `project_tools_result`) — полный перебор всех потребителей через
  `getattr(ctx, "profile")` не проводился; установлено лишь, что они не
  объявлены в теле класса.
- `_flatten_env` / `_parse_value` — проверены наличие потребителей, но не
  поведение на «экзотических» значениях `.secrets.env` (нужен прогон).
- Ни один символ моей группы **не остался неразобранным**: все 10 файлов,
  39 классов, 87 методов и 66 функций разобраны. Перечисленное выше — не
  символы без вердикта, а аспекты, требующие отдельной проверки.

---

## Удалить — сводный список

| Файл | Символ | LOC | Почему | Что нужно сделать перед удалением | Что сломается |
|---|---|---|---|---|---|
| `gateway.py` | `_gateway_print_llm_calls` | 12 | **0 вызывающих** во всём репо (проверено `Select-String` по `*.py`, `tests/`, `tools/`, `docs/`). Флаг `gateway.print_llm_calls` работоспособен — его читает `application_context.py:99` → `:403` → `agent_factory.py:210` → `database_logging_hook.py:753`; helper в entrypoint'е остался от старого пути | Удалить функцию. Опционально — обновить докстринг `:507`, где сказано «включается в `project.json`» (файла уже нет) | Ничего: `config.json:705` `"print_llm_calls": true` продолжает действовать через другую цепочку. Проверить `tests/test_gateway_enterprise_mcp_startup.py` — прямых ссылок на символ нет |
| `cli_agent.py` | `__get_cron` | 4 | **0 вызывающих**; двойное подчёркивание в модульном scope — след неудачного «приватного» соглашения; возвращает константу `None` | Удалить функцию | Ничего runtime. В `tests/test_cli_agent.py` ссылок нет (проверено grep по `__get_cron`) |
| `lib/lifecycle/gateway_runner.py` | `reset_backoff` | 4 | **0 вызывающих**, включая тесты; докстринг `:111-112` утверждает «Используется в тестах» — неправда | Удалить метод; поправить докстринг | Ничего. `tests/test_gateway_runner.py` (7 тестов) этот метод не зовёт |
| `lib/core/agent_factory.py` | параметр `framework_hooks` | ~6 (`:78`, `:181-182`, ветка `:186-190`) | Не передаётся ни одним вызывающим; докстринг сам признаёт «Сейчас список пуст» | Удалить параметр из сигнатуры `create` и ветку, которая его обрабатывает; поправить докстринг | Ничего: единственный вызывающий — `application_context.py` — его не передаёт |
| `lib/core/application_context.py` | поле `enable_audit` | 2 (`:164`, присваивание `:260`) | Пишется и **не читается нигде** — остаток снятого audit-сервиса | Удалить поле, присваивание и ключ в `_resolve_enable_kwargs:54`/`_DEPRECATED`; **обновить `tests/test_application_context_cache_lifecycle.py:55-57`** (`test_enable_audit_field_exists`) и `tests/test_application_context_role.py:71,97,171,253` (списки deprecated-kwargs) | `tests/test_application_context_cache_lifecycle.py::test_enable_audit_field_exists` упадёт; списки в `test_application_context_role.py` станут неполными. Связано с change `remove-deprecated-enable-kwargs` — удалять **вместе** с остальными тремя флагами |
| `lib/core/project_settings.py` | ветка `VectorIndex*` (`VectorIndexSettings:141-183`, `VectorIndexEntry:437-467`, `VectorIndexConfig:469-513`, `VectorInfrastructureSettings:186-197`) | ~150 | Объявляет индексы/эмбеддинги, которые теперь принадлежат `mcp-platform/platform.json → vectors.indexes`; в агенте **никем не читается** (докстринг `:155-162` признаёт сам). `config.py:176-177` при этом объявляет `gateway.vector.index.*` каноническим путём — второй источник правды | Снять секцию `gateway.vector.*` из `config.json`; удалить 4 класса и ссылки в `GatewaySettings`/`SkillSettings`; поправить `tests/test_project_settings.py:401-425` и `__all__:35-52`; поправить докстринги `config.py:86-88,165-177` | `tests/test_project_settings.py` (4 теста на `VectorIndexEntry`/`VectorIndexConfig`) и `tests/test_config_keys.py` могут потребовать правки. Runtime-эффекта нет: агент индексы не строит. **Проверить `mcp-platform/platform.json` — объявления должны быть полными ДО удаления** |

### Отдельно: кандидаты в «Упростить», требующие подготовки (не «Удалить»)

| Файл | Символ | LOC | Почему не «Удалить» прямо сейчас |
|---|---|---|---|
| `lib/core/application_context.py` | комментарии про `_init_cache_runtime` (`:688-691`), `gateway.py:154-158` | ~14 | Метод удалён; **комментарии лгут**. Это правка текста, а не кода — поправить вместе с `docs/DATABASE.md:263` |
| `lib/core/application_context.py` | докстринг `_make_cron_service:1414` | 1 | Инвертирован («для CLI-режима» при вызове только для `role="gateway"`). Плюс `enable_cron` не задан в `config.json` → сервис в production не создаётся; решить, нужен ли cron вообще |
| `lib/core/application_context.py` | комментарий `:157-158` про dataclass | 2 | Класс не `@dataclass`; поправить текст или добавить декоратор (последнее меняет поведение `__init__`) |
| `lib/lifecycle/shutdown_coordinator.py` | докстринг `:5-6,11` | 4 | Перечисляет удалённые `sync_service` и «аудит-сервисы» |
| `lib/lifecycle/gateway_runner.py` | докстринг `:17` | 1 | Упоминает Redis |
| `config.py` | `:116-119` против `:166-167` | 4 | Внутреннее противоречие про `public.agent_vector_index_config` |
| `gateway.py` | `console = Console()` на `:659` | 1 | Определён после всех использований; перенести к импортам (`:102`) |
| `gateway.py` / `cli_agent.py` | `_configure_logging`, `script_dir_for_runtime`, `_connect_enterprise_mcp` | ~90 суммарно | Три дублирующихся helper'а; слить в один (см. находки) |
| `cli_agent.py` | неиспользуемые импорты `:121-122` | 2 | `run_repl`/`DisplayConfig` импортируются в `_entrypoint_main`, но не используются |
| `lib/core/project_settings.py` | `__all__:35-52` | 18 | Объявляет 17 из 31 экспортируемых имён; никем не проверяется |
