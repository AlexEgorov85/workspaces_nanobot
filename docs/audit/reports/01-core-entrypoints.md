# Аудит: 01 — Ядро и точки входа

## Сводка группы

Файлов: 12 · LOC: 5680 · классов: 38 · методов: 28 · функций: 103 · разобрано символов: 188

Файлы: `lib/core/application_context.py`, `lib/core/project_settings.py`, `lib/core/skill_config.py`,
`lib/core/agent_factory.py`, `lib/core/bus_factory.py`, `lib/core/skill_registration.py`,
`lib/core/infra_registration.py`, `lib/core/__init__.py`, `config.py`, `gateway.py`,
`cli_agent.py`, `streamlit_app.py`.

**Ключевые находки**

- `cli_agent.py:191` — `asyncio.create_task(_run_patched_repl(ctx, args))` вызывает **синхронную**
  функцию вне event loop. Проверено: `RuntimeError: no running event loop`. Флаг `--patched`/`-P`
  (дефолт CLI-агента по смыслу) падает на 4-й строке после полной сборки `ApplicationContext`,
  контекст не `start()`-ится и не `stop()`-ится — утечка кэша и пула. ВЕРДИКТ: Упростить.
- `config.py:63` — `env_file = Path(path or _ENV_FILE)`, а константы `_ENV_FILE` в модуле нет
  (удалена ранее, см. `CHANGELOG.md:3522`). Проверено: `load_env()` → `NameError`. Ветка default
  недостижима: единственный prod-вызов `_load_secrets_override` всегда передаёт путь явно,
  все тесты (`tests/test_config.py:172-244`) тоже. ВЕРДИКТ: Упростить.
- `project.json::gateway.print_tools = false` — ключ присутствует в конфиге, но **ни один модуль
  его не читает** (grep по всему `*.py`: только комментарии `agent_factory.py:15` и
  `lib/hooks/terminal_tool_print_hook.py:18`). Документированный выключатель `TerminalToolPrintHook`
  не существует. ВЕРДИКТ: Упростить.
- `config.py:662` `get_active_profile()` — 0 ссылок. Дублирует `SETTINGS["profile"]`, который
  читается напрямую в `lib/services/subprocess_manager.py:88` и
  `lib/core/application_context.py:246`. ВЕРДИКТ: Удалить.
- `lib/core/skill_config.py` — 7 функций без единого вызова (`load_db_config:104`,
  `get_tool_config:114`, `get_in_memory_cache_path:205`, `get_vector_index_path:234`,
  `get_vector_indexes:303`, `get_embedding_config:312`, `get_embedding_model:324`) + 1
  транзитивно мёртвая (`_vector_indexes_list:47`). Проверено обе тонкие обёртки
  (`workspace/skills/audit_analyzer/scripts/skill_config.py` — 7 экспортов,
  `workspace/skills/legal_summarizer/scripts/llm/config.py` — 8): ни одна из мёртвых в них
  не делегирует. ВЕРДИКТ: Удалить.
- `lib/core/application_context.py:761-995` — три почти идентичных рендерера баннеров
  (`_emit_hook_inventory_banner`, `_emit_patch_inventory_banner`,
  `_emit_project_tools_inventory_banner`), ~150 LOC с одинаковой структурой
  (diff → gate → lazy `Console`/`Panel` → `style.replace("bold ","")` → fallback в `sys.stderr`).
  ВЕРДИКТ: Слить в один `_render_inventory_banner`.
- `lib/core/application_context.py:1372-1563` — три ~24-строчных блока «skip» (реестр пуст / нет
  DSN / нет имён таблиц), каждый со своим локальным `from lib.services.db_logging_service import
  LogEvent, try_log_event` и ручной сборкой `LogEvent`. При этом хелпер `_record_sync_skipped:1566`
  существует, помечен `DEPRECATED` («на данный момент ни одного нет») — и это ровно тот хелпер,
  который нужен этим трём блокам. ВЕРДИКТ: Слить (96 → ~15 строк).
- `lib/core/application_context.py:1036-1042` — `check_postgres` (вложена в
  `_register_readiness_checks`) лезет в приватное `utils.db._get_manager()` / `utils.db._Job` и
  делает `sys.path.insert` на каждый вызов `/health`. ВЕРДИКТ: Перенести (публичный API в
  `workspace/utils/db.py`).
- `lib/core/project_settings.py` — 32 модели, но `ctx.project_settings` читается ровно
  **в одном месте**: `lib/core/application_context.py:1195`
  (`logging.db.flush_interval_sec`). Остальные ~120 полей валидируются и не используются —
  потребители берут raw `SETTINGS` через `ConfigService.settings_section()`. Два параллельных
  пути доступа к конфигу. ВЕРДИКТ: Упростить.
- `lib/core/project_settings.py:183-185` `HeartbeatSettings` — потребителя нет ни одного,
  секции `gateway.heartbeat` нет и в `project.json`. ВЕРДИКТ: Удалить.
- `lib/core/project_settings.py:585-590` — docstring `SkillSettings` обещает
  `ConfigDict(extra="forbid")` = «fail-fast на опечатках», но вложенные модели
  (`SkillCliSettings`, `SkillChunkingSettings`) — `_StrictOptional` с `extra="allow"`.
  `skills.legal_summarizer.cli.default_length` и `.chunking.brief_input_ratio` реально
  читаются кодом (`legal_summarizer/scripts/llm/config.py:66`,
  `lib/core/skill_config.py:135-167`) и при этом **не валидируются** (опечатка пройдёт). Строка лжёт.
  ВЕРДИКТ: Упростить.
- `lib/core/bus_factory.py:104` `build_logging_bus` — 0 вызовов, собственный docstring называет
  его «legacy-хелпером». ВЕРДИКТ: Удалить.
- «Висячие» поля `ApplicationContext`: `subprocess_manager:180` (никогда не присваивается),
  `cache_loader:352` (только чтение в тесте `assert ctx.cache_loader is None`),
  `project_tools_result:502` (сразу передаётся в баннер и больше не читается). ВЕРДИКТ: Удалить.
- `lib/core/application_context.py:1615` — `_INFRA_KEY_VECTOR_STORAGE = "vector_index.storage"`
  дублирует `lib/core/infra_registration.py:18` `INFRA_KEY_VECTOR_STORAGE = "vector.storage"`,
  но с **другим значением** (старое, до переименования по `CHANGELOG.md:1858`). Реальная
  регистрация идёт через `infra_registration.register_vector_storage` → `register_infra("vector.storage", …)`.
  Константа в `application_context.py` не используется, но если её оживить, получится второй
  невидимый namespace. ВЕРДИКТ: Удалить (и синхронизировать пример в
  `lib/services/table_registry.py:149` и тест-данные `tests/test_table_registry.py:379,433` —
  это зона владельца `lib/services/table_registry.py`).
- `lib/core/application_context.py:617-618` — `stop()` начинается с `if not self._started: return`.
  `create()` `_started` не выставляет, поэтому `cfg.stop()` в smoke-пути `cli_agent.py:136`
  — no-op, а `gateway.py:143` его вообще не вызывает: `--smoke` закрывает кэш-провайдер
  только финальным `os._exit`. ВЕРДИКТ: Упростить (гард должен быть на «уже останавливали»,
  а не на «стартовали»).
- `lib/core/application_context.py:509` и `:524` — `runtime_health.mark_started()` вызывается
  дважды: в `create()` и в `start()`. В `create()` он помечает health «started» до того, как
  сервисы реально поднялись. ВЕРДИКТ: Упростить.
- `lib/core/application_context.py:1690` — docstring `_make_cron_service` говорит «только для
  CLI-режима (только там он нужен)», а код на `:390` вызывает его **только** при
  `role == "gateway"`. Строка лжёт. ВЕРДИКТ: Оставить + исправить docstring.
- `lib/core/application_context.py:1285-1288` — docstring `resolve_cache_path` перечисляет
  «явный путь, default, workspace-local fallback», но в коде две ветки: fallback-ветки нет.
  ВЕРДИКТ: Упростить (docstring).
- `gateway.py:498-518` / `cli_agent.py:263-290` — `main()` ловит только `ConfigurationError`,
  но docstring обещает «единый boundary». `CacheLoadError` из `CacheLoadService`
  (`application_context.py:352`) и `SystemExit(1)` из `_check_websocket_port_available`
  (`gateway.py:434`) в него не попадают → сырой трейсбек вместо `FATAL:` + exit 2. ВЕРДИКТ: Упростить.
- `gateway.py:313` `_gateway_print_llm_calls` — 0 вызовов; `gateway.py:34` `_WINDOWS_COLOR_WARNING`
  присваивается и никогда не печатается (в `cli_agent.py:112-113` — печатается). ВЕРДИКТ: Удалить.
- `gateway.py:176` `_project_version()` — ленивая обёртка, мотивация которой («отложить импорт
  до первой печати») уже неактуальна: `_entrypoint_main:132` импортирует `project_version`
  безусловно в той же функции. ВЕРДИКТ: Упростить (инлайн).
- `streamlit_app.py:120` `_MAX_WAIT = SETTINGS.streamlit.get("max_wait", 600)` — цикл опроса
  `:557-558` намеренно бесконечен («ждём бесконечно, без таймаута»), константа не используется
  нигде и держится наедине ассертом `tests/test_streamlit_app.py:356`. ВЕРДИКТ: Удалить.
- `streamlit_app.py:11` `from typing import Any` и `:109` `serialize as _media_serialize` —
  неиспользуемые импорты. `:665` `st.session_state.pop("_pending_uploads", None)` — мёртвый
  повторный `pop`: буфер уже снят на `:631`. ВЕРДИКТ: Удалить.
- `config.py:277-285` `_export_secrets_to_env(cfg)` экспортирует в `os.environ` **всё** слитое
  `cfg` (включая project.json), а не только секреты: `CHANNELS_POSTGRES_MAX_CONN=10` и т.п.
  утекают в окружение любого подапроцесса (`SubprocessManager.spawn_streamlit`).
  ВЕРДИКТ: Упростить (экспортировать только оверлей `.secrets.env`).
- `config.py:706-707` — `except ConfigurationError` в `get_setting` недостижим: `SETTINGS._inner_dict`
  — прямой слот-доступ, а если `SETTINGS` не `_LazySettings`, то и `try` не нужен. ВЕРДИКТ: Упростить.

**Вердикты (по первичному вердикту каждого символа, 188 символов):**
Оставить 127 · Упростить 27 · Удалить 29 · Слить 4 · Перенести 1 · **НЕ РАЗОБРАНО 0**

Расшифровка «Удалить» (29): `skill_config.py` 8 (`_vector_indexes_list`, `load_db_config`,
`get_tool_config`, `get_in_memory_cache_path`, `get_vector_index_path`, `get_vector_indexes`,
`get_embedding_config`, `get_embedding_model`) · `config.py` 7 (`AttrDict.__setattr__`,
`_LazySettings.{__iter__,__len__,items,keys,values}`, `get_active_profile`) ·
`application_context.py` 5 (поля `subprocess_manager`/`cache_loader`/`project_tools_result`
+ импорты `os`/`compute_overall_status`) · `streamlit_app.py` 3 (`_MAX_WAIT`, `Any`,
`_media_serialize`) · `gateway.py` 2 (`_gateway_print_llm_calls`, `_WINDOWS_COLOR_WARNING`) ·
`project_settings.py` 1 (`HeartbeatSettings`) · `cli_agent.py` 1 (`__get_cron`) ·
`bus_factory.py` 1 (`build_logging_bus`) · `skill_registration.py` 1 (импорт `Any`).

«Слить» (4): три рендерера баннеров + три блока skip-логирования в
`application_context.py`, `_run_patched` с `_run_vanilla` в `cli_agent.py`.
«Перенести» (1): `check_postgres` → публичный API `workspace/utils/db.py`.

---

## `lib/core/application_context.py` — 1816 LOC

**Назначение.** Composition root: единственное место, где собираются все сервисы рантайма
(конфиг → схема → БД-пул → логирование → таблицы → skill-регистрация → кэш → каналы →
хуки → agent → запуск/остановка).
**Что делает.** `create()` (~315 строк тела) конструирует ~40 сервисов и 12 баннеров инвентаризации;
`_init_cache_runtime` синхронно грузит кэш из PostgreSQL в DuckDB-файл; `start()` поднимает
пулы/воркеры/крон; `stop()` гасит их в обратном порядке. `create()` fail-fast валидирует
merged `project.json` через `lib.core.project_settings` и legacy-секции через `_reject_legacy_renamed_sections`.
Побочные эффекты: запись в `agent_gateway_logs`, чтение PG, создание DuckDB-файла кэша,
`sys.path.insert`, monkey-patch-приложение (`RuntimePatcher`), авто-скан `workspace/hooks|tools`.
**Зачем нужен.** Без него нет точки сборки: `gateway.py:139` и `cli_agent.py:151,178` — единственные
вызывающие; всё остальное (`lib/services/*`, skills) получает сервисы отсюда. Удаление = переписать
composition root в 4 места.
**Вердикт.** Упростить
**Обоснование.** Скелет нужен, но файл на 1816 строк содержит: 3 дублирующихся рендерера баннеров
(§), 3 дублирующихся блока логирования skip (§), 4 мёртвых поля dataclass (§), 5 мёртвых локальных
импортов, двойной `mark_started()` и docstring, противоречащий коду в двух местах. Плюс нарушение
слоёв в `check_postgres`. Всё это убирается без изменения контракта.
**Доказательства.** Вызывается из `gateway.py:139`, `cli_agent.py:151,178`, 13 файлов тестов
(`test_application_context*.py`, `test_cache_readiness_and_skill_role.py`,
`test_storage_hybridization_lifecycle.py`, `test_runtime_health.py`, …). `resolve_cache_path` —
публичный контракт для `lib/services/cache_provider.py` и `lib/core/skill_config.py:274`.

#### class `ApplicationContext` (строки 125–730, 4 метода, 606 LOC)

Назначение — dataclass-контейнер всех сервисов; `create()` — единственный сборщик, `start()/stop()` —
жизненный цикл. Зачем нужен — DI-контейнер без фреймворк-зависимостей; удаление ломает
21 файл (15 по `create`, 21 по `start`, 16 по `stop` по брифу). Вердикт — **Оставить**
(структура оправдана; чистить нужно содержимое, а не класс).

Атрибуты-поля (одной строкой): `config, script_dir, workspace_dir, role, storage_override,
settings, project_settings, db_logging_service, preload_service, transcription_service,
subprocess_manager, session_manager, usage_store, session_cold_sync_service, cache_provider,
cache_loader, vector_index_service, skill_contexts, skill_cli_providers, table_registry,
config_service, runtime_health, runtime_readiness, session_storage_service, hook_factories,
hooks, tool_audit_hook, runtime_patcher, runtime_patch_report, project_tools_result,
cron_service, bus, agent, agent_id, _started`.

| Поле | Строки | Назначение | Зачем нужен | Кто читает | Вердикт |
|---|---|---|---|---|---|
| `subprocess_manager` | 180 | хендл `SubprocessManager` | — | никто: `gateway.py:196` создаёт локально | **Удалить** |
| `cache_loader` | 352 | хендл `CacheLoadService` | — | только `tests/test_application_context.py:244` (`assert … is None`) | **Удалить** |
| `project_tools_result` | 502 | отчёт `ProjectToolsLoadResult` | — | только `:503` (передача в баннер) | **Удалить** |
| `hook_factories` | 183-186 | фабрики per-turn хуков | логирование оборотов | `_log_connected_hooks:751`, `_emit_hook_inventory_banner:786`, `agent_factory.py:193` | Оставить |
| `runtime_patcher` | 466 | хендл `RuntimePatcher` | применение 12 патчей | `:467` (`apply_all`); ассерт `tests/test_application_context.py:240`; AST-guard `tests/test_application_context_single_application_point.py:35` | Оставить |
| `_started` | 178 | флаг «сервисы подняты» | гард `stop()` | `stop():617` | Оставить (но см. §) |
| остальные 26 | 133-177 | сервисы рантайма | — | start/stop + gateway/CLI | Оставить |

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `create` | 195-510 | classmethod: полная сборка всех сервисов, ~315 строк тела | единственная точка композиции | `gateway.py:139`, `cli_agent.py:151,178`, 13 тестовых файлов | **Оставить** |
| `start` | 516-614 | подъём фоновых сервисов (пул, логирование, preload, session cold-sync, FAISS, bus) | post-startup инициализация | `gateway.py:225`, `cli_agent.py:202`, 21 файл | **Оставить** |
| `stop` | 616-676 | shutdown в обратном порядке + `os._exit(0)` при mmap-ошибке | graceful shutdown, снятие блокировок | `gateway.py:242`, `cli_agent.py:160,190,202` | **Упростить** — гард `if not self._started: return` (`:617-618`) делает `stop()` no-op после `create()`-без-`start()`; из-за этого `cli_agent.py:136` (smoke) и `gateway.py:143` (smoke) не закрывают `cache_provider`. Заменить на «уже останавливали»-счётчик |
| `_validate_runtime_schema` | 678-730 | pre-startup проверка 6 runtime-таблиц через `information_schema` | fail-fast на рассинхроне схемы | `start():524` | **Упростить** — `try: settings = self.settings or {} except Exception:` (`:700-702`) недостижим: `self.settings` — уже инициализированный proxy к моменту вызова |

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_resolve_enable_kwargs` | 66-122 | нормализация legacy `enable_*`/`print_*` → `**kwargs` | обратная совместимость конфига | `create():218` | Оставить |
| `_log_connected_hooks` | 738-758 | лог подключённых хуков | стартовая диагностика | `create():441` | Оставить |
| `_emit_hook_inventory_banner` | 761-832 | баннер diff хуков vs `runtime_inventory` | диагностика startup | `create():447` | **Слить** (см. §1) |
| `_emit_patch_inventory_banner` | 835-900 | баннер diff runtime-патчей | диагностика startup | `create():475` | **Слить** |
| `_emit_project_tools_inventory_banner` | 903-995 | баннер diff project tools | диагностика startup | `create():503` | **Слить**; + мёртвая локальная `duplicate` (`:926`) — вычисляется, нигде не печатается → **Удалить** |
| `_register_readiness_checks` | 998-1102 | регистрация 3 readiness-проверок (`postgres`, `duckdb_cache`, `vector_search`) | `/health` | `create():451` | **Перенести** часть: `check_postgres` (`:1015-1093`) лезет в `utils.db._get_manager()`/`utils.db._Job` и делает `sys.path.insert` на каждый вызов `/health` → нужен публичный `workspace.utils.db.probe_pool()`. Остальное Оставить |
| `_make_config_service` | 1105-1126 | ленивый конструктор `ConfigService` | обход цикла импорта `config`↔`lib.core` | `create():196` | Оставить |
| `_resolve_agent_id` | 1129-1140 | `config.agents.defaults.model` → `agent_id` | имя агента для БД-логов | `create():227,418` | Оставить |
| `_make_db_logging` | 1143-1213 | `DbLoggingService` из `logging.db` | долговечный журнал | `create():333` | **Упростить** — `:1156` `from lib.services.config_service import ConfigService  # noqa: F401` не используется (проверка импортируемости, покрытой `:1120`) |
| `_default_local_cache_dir` | 1216-1232 | `~/.cache/nanobot/duckdb` | путь вне NFS | `resolve_cache_path():1268` | Оставить |
| `resolve_cache_path` | 1235-1307 | **публичная** функция вычисления пути `cache.duckdb` | единая точка для gateway + skill (иначе skill читал устаревший снимок — баг v2.5.1) | `lib/services/cache_provider.py`, `lib/core/skill_config.py:274`, `tools/build_vectors.py`, сам `_init_cache_runtime:1385` | Оставить; **Упростить** docstring `:1285-1288` — перечислена workspace-local fallback-ветка, которой в коде нет (веток две: явный путь / default) |
| `_warn_if_cache_path_on_nfs` | 1310-1369 | предупреждение о кэше на NFS | профилактика «PID 0» | `resolve_cache_path():1302` | Оставить; `:1290` `from pathlib import Path` избыточен (импорт на `:44`), `:1322` `import logging` / `:1324` `import sys` локальны и избыточны |
| `_init_cache_runtime` | 1372-1563 | загрузка кэша из PG + создание FAISS-сервиса; возвращает `(provider, loader)` | единственный writer кэша | `create():347-353` | **Слить**: три ~24-строчных «skip»-блока (реестр пуст / нет DSN / нет table_names) дублируют локальный импорт `LogEvent`+`try_log_event` и ручную сборку `LogEvent`. Заменить вызовами существующего `_record_sync_skipped` → ~96 строк экономии. Также: возвращаемый `loader` наружу не нужен (см. `cache_loader`) — возвращать только `provider` |
| `_record_sync_skipped` | 1566-1595 | логирование «sync пропущен» | — | 0 вызовов; docstring сам помечает DEPRECATED | **Упростить** — не удалять: это и есть нужный хелпер для 3 дублей выше. Сменить docstring с DEPRECATED на «используется `_init_cache_runtime`» |
| `_auto_register_skills` | 1600-1612 | регистрация skills из `project.json` | без `register.py` | `create():333`→`:443` | Оставить |
| `_register_infra_resources` | 1618-1633 | `register_infra("vector.storage", …)` | инфра-таблица эмбеддингов в кэше | `create():349` | Оставить; `:1615` `_INFRA_KEY_VECTOR_STORAGE` — **Удалить** (см. кросс-находку №8) |
| `_make_transcription` | 1636-1645 | `TranscriptionService` | аудио-транскрипция | `create():262` | Оставить |
| `_make_preload` | 1648-1663 | `PreloadService` (gateway — прогрев, CLI — no-op) | разница ролей | `create():271` | Оставить |
| `_make_cron_service` | 1666-1675 | `CronService` | cron-задачи | `create():390` | Оставить + **исправить docstring**: написано «только для CLI-режима (только там он нужен)», а `:390` держит `role == "gateway"` — текст инвертирован |
| `_make_session_cold_sync_service` | 1678-1750 | `SessionColdSyncService` | cold-зеркало сессий в PG | `create():344` | Оставить; `:1720-1724` используют `get_setting`/`require_setting`, импортированные внутри `try` выше — работает, но читается как «импорт в try, использование после» |
| `_make_usage_store` | 1753-1769 | upstream `LLMUsageStore` | учёт токенов | `create():339` | Оставить |
| `_configure_db_pool` | 1777-1796 | проброс `channels.postgres.pool` в `utils.db` | размер пула | `start():519` | Оставить |
| `_start_db_pool` | 1799-1806 | прогрев/старт пула | инфра-инвариант | `start():517` | Оставить |
| `_stop_db_pool` | 1809-1816 | остановка пула | shutdown | `stop():668` | Оставить |

**Мёртвые импорты модуля:** `import os` (`:44`) — не используется (единственное упоминание — комментарий `:665` про `os._exit(0)`, вызывается в другом файле). `compute_overall_status` (`:1011-1013`) — импортирован, ни разу не вызван. Вердикт на оба — **Удалить**.

---

## `lib/core/project_settings.py` — 759 LOC (code 571)

**Назначение.** Pydantic-модели для fail-fast валидации merged `project.json` на старте.
**Что делает.** `validate_project_settings()` (`:722`) прогоняет весь SETTINGS через
`ProjectSettings.model_validate`; при ошибке собирает все проблемы в один `ConfigurationError`.
Вложенные `model_validator(mode="before")` ловят legacy-имена секций (`gateway.vector_index`,
`gateway.duckdb`, `gateway.sync`, `channels.duckdb`) и не-virtualenv skill-секции.
Побочный эффект — `ctx.project_settings` становится «типизированной проекцией».
**Зачем нужен.** Ловит опечатки/мусор в конфиге до того, как рантайм упадёт. `SkillSettings` —
единственный `extra="forbid"`-контур (fail-fast на опечатках в `skills.*`).
**Вердикт.** Упростить
**Обоснование.** Валидация нужна, но типизированная проекция используется в 1 месте из ~120 полей
— это второй, параллельный путь чтения конфигу. Плюс 3 группы полей без потребителя,
одна модель целиком без потребителя и docstring, обещающий `extra="forbid"` там, где
вложенные модели `extra="allow"`.
**Доказательства.** `validate_project_settings` — `application_context.py:236` (1 вызов);
тесты: `tests/test_project_settings.py`, `tests/test_config_keys.py`,
`tests/test_application_context_logging.py:413-430`, `tests/test_auto_register_skills.py:179-182`,
`tests/test_application_context_schema_validation.py:25-158`. Поля профилей:
`tests/test_shared_cache_path_across_profiles.py:33-113`.

#### Базовые / gateway-секции

| Класс | Строки | Назначение | Зачем нужен | Вердикт |
|---|---|---|---|---|
| `_StrictOptional` | 48-51 | `BaseModel` c `extra="allow"`, все поля `| None` | forward-compat секций | Оставить |
| `PostgresChannelSettings` | 54-62 | 8 из 18 ключей `channels.postgres` | типы/range для `worker_id`, `lease_interval`, `processing_timeout`… | **Упростить** — 10 ключей присутствуют в `project.json`, но не типизированы (`max_concurrent`, `max_stuck_retries`, `allow_from`, `pool`, `msg_ctx_max_size`, `media_cache_dir`, `dsn`, `schema`, `enabled`, `flush_interval`) |
| `CompactSettings` | 65-68 | `gateway.compact` | порог авто-сжатия | Оставить |
| `StartupSchemaValidationSettings` | 71-91 | `gateway.startup.schema_validation` | гейт `_validate_runtime_schema` | Оставить |
| `StartupSettings` | 94-97 | контейнер `gateway.startup` | — | Оставить |
| `ErrorMessagesSettings` | 100-127 | `gateway.error_messages` | тексты для `patch_turn_delivery_fail` | Оставить |
| `VectorIndexSettings` | 134-167 | `gateway.vector.index` | FAISS-инфраструктура | **Упростить** — `storage_table` и `indexes` используются (`infra_registration.py:41`, `skill_config.py:264`, `cache_provider_impl.py:174`); `default_root` читается только мёртвой `skill_config.get_vector_index_path:249`; `enable` и `backend` не читаются **никем** (в `project.json` присутствуют) |
| `VectorInfrastructureSettings` | 170-180 | контейнер `gateway.vector.index` | — | Оставить |
| `HeartbeatSettings` | 183-185 | `gateway.heartbeat.{enabled,intervalS}` | — | **Удалить** — 0 потребителей в `*.py`, секции нет и в `project.json`; `tools/legacy_audit.py` её не перечисляет |
| `UsageStoreSettings` | 188-199 | `gateway.usage_store` | `_make_usage_store:1763` | Оставить |
| `SessionColdSyncSettings` | 202-214 | `gateway.session_cold_sync` | `_make_session_cold_sync_service:1705` | Оставить |
| `GatewaySettings` | 217-262 | контейнер `gateway` | — | Оставить |
| `GatewaySettings._reject_legacy_renamed_sections` | 235-262 | fail-fast на legacy-именах секций | миграция без бэкомпата | Оставить |
| `CacheSettings` | 265-299 | единственный ключ `local_path` | путь DuckDB-кэша | Оставить |
| `CliSettings` | 302-304 | 2 из 7 ключей `cli` | `max_iterations` (range), `show_context_window` | **Упростить** — `project.json` содержит 11 ключей `cli.*`, модель типизирует 2; `llm_timeout`/`exec_timeout`/`log_level` числовые, но без валидации |
| `StreamlitSettings` | 307-309 | `enabled`, `error_window_sec` | UI | **Упростить** — `files_dir`, `max_wait`, `poll_interval` не типизированы; `max_wait` вдобавок мёртв (§) |
| `ChannelsSettings` | 312-314 | `postgres`, `document_text_threshold` | — | Оставить |
| `LoggingDbSettings` | 317-337 | `enabled`, `flush_interval_sec` (0.5…60.0) | **единственное** поле, реально читаемое из проекции | Оставить |
| `LoggingDbSettings._default_flush_interval_sec` | 322-337 | `None → 5.0` | канонический дефолт из спеки | Оставить |
| `LoggingSettings` | 340-341 | контейнер `logging.db` | — | Оставить |

#### skills-секции

| Класс | Строки | Назначение | Зачем нужен | Вердикт |
|---|---|---|---|---|
| `TableEntry` | 352-386 | элемент `skills.*.tables[]` (`str` или объект) | разбор списка таблиц | Оставить |
| `VectorIndexEntry` | 389-418 | элемент `skills.*.vector_indexes[]` | min-контракт индекса | Оставить |
| `VectorIndexConfig` | 421-463 | `gateway.vector.index.indexes.<name>` | единственный источник для `build_vectors.py`/`cache_provider_impl` | Оставить |
| `SkillCliSettings` | 466-476 | `default_mode`, `default_format`, `max_retries`, `timeout_sec` | параметры CLI skill'а | **Упростить** — `project.json` содержит `default_length`, читается `legal_summarizer/scripts/llm/config.py:66`, но **не типизирован** (модель — `_StrictOptional`, `extra="allow"`) |
| `SkillLlmSettings` | 479-488 | `max_tokens`, `temperature` | LLM-политика | Оставить |
| `SkillChunkingSettings` | 491-509 | `chunk_size`, `chunk_overlap`, `single_call_threshold`, `chunk_size_input_ratio` | map-reduce | **Упростить** — `brief_input_ratio` есть в `project.json` и читается `skill_config.get_brief_context_config:170`, но не типизирован |
| `SkillBriefContextSettings` | 512-526 | параметры `BriefContextBuilder` | — | Оставить |
| `SkillExecutionContextBatchingSettings` | 529-543 | батчинг контекста | — | Оставить |
| `SkillExecutionSettings` | 546-558 | confirmation / safety net | — | Оставить |
| `SkillSettings` | 561-604 | секция `skills.<name>`; **единственный** `extra="forbid"` | fail-fast на опечатках в skill-конфиге | Оставить; **Упростить** docstring `:585-590` — «fail-fast на опечатках» не действует на вложенные секции (`cli`, `chunking`, `llm` — `extra="allow"`) |
| `SkillsSettings` | 607-663 | контейнер `skills.*` | forward-compat имён skill'ов | Оставить |
| `SkillsSettings._validate_skill_sections` | 627-663 | проверка, что все секции `skills.*` резолвятся в `SkillSettings` | fail-fast на «skill без валидной формы» | Оставить |
| `ProjectMetadataSettings` | 666-685 | `project.version` и т.п. | баннер версии | Оставить |
| `ProjectSettings` | 688-699 | корень | — | Оставить |
| `_LegacyGatewaySectionsError` | 702-708 | маркер для «обёртки» pydantic | pydantic не оборачивает произвольный `Exception` в `mode="before"` | Оставить |

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `validate_project_settings` | 722-759 | валидация merged SETTINGS + сборка всех ошибок в один `ConfigurationError` | fail-fast на старте | `application_context.py:236` | **Упростить** — единственный read-side потребитель проекции — `application_context.py:1195`. Либо взять проекцию как единственный источник (большой рефакторинг `ConfigService.settings_section`), либо честно объявить модуль validate-only и не возвращать «типизированную проекцию» в рекламе docstring |

---

## `config.py` — 725 LOC (code 546)

**Назначение.** Единая точка формирования и жизненного цикла конфигурации (`project.json` →
`session_manager.json` → `config.json` → `profiles/<mode>.jsonc` → `${VAR}` из `.secrets.env`).
**Что делает.** `resolve_application_config(profile)` (`:432`) собирает `AttrDict`,
`_merge_profile_overlay` накладывает профиль, `_resolve_env_refs` подставляет `${VAR}`,
`validate_runtime_isolation` — hard-fail на несовпадении имён runtime-таблиц. Публикация
`SETTINGS` — только через `_initialize_settings(profile)`; до неё любой доступ к `SETTINGS`
бросает `ConfigurationError` (`_LazySettings`). Побочные эффекты: `_export_secrets_to_env:283`
пишет во весь `os.environ`.
**Зачем нужен.** Гейт конфигурации: без него падение конфига всплывает как `NameError`/`KeyError`
внутри `AgentLoop`, а не как `FATAL:` + exit 2 до старта сети.
**Вердикт.** Упростить
**Обоснование.** Механика живая и нужная, но после удаления env-выбора профиля
(`0d7368c`) остались хвосты: `get_active_profile` (0 ссылок), `load_env` с несуществующим
`_ENV_FILE` в default-ветке, недостижимый `except ConfigurationError` в `get_setting`,
`_SECRETS_FILE is None` (константа `Path`, не `None`), 5 неиспользуемых методов `_LazySettings`,
неиспользуемый `AttrDict.__setattr__`. Плюс `_export_secrets_to_env` экспортирует не секреты,
а весь конфиг.
**Доказательства.** `_initialize_settings` — `gateway.py:116`, `cli_agent.py:117`,
`streamlit_app.py:68`, `tests/conftest.py:66`, `tools/*.py`; `is_settings_initialized` — 7 файлов;
`require_setting` — `tests/test_config_keys.py:210-218` + 1 прод-файл; `load_env` —
`config.py:273` + `tests/test_config.py:172-244`; `PROFILE_OWNED_RUNTIME_KEYS` —
`config.py:320` + `tests/test_shared_cache_path_across_profiles.py:38-113`.

#### class `AttrDict` (строки 17–26, 2 метода)

`dict` с доступом через точку. Нужен как тип возврата `resolve_application_config` и для
`SETTINGS.<section>` в 149 местах. Вердикт — **Оставить**.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__getattr__` | 18-23 | `cfg.channels` → `cfg["channels"]` | 100+ мест в коде | атрибутный доступ по всему рантайму | Оставить |
| `__setattr__` | 25-26 | `cfg.x = v` → `self["x"] = v` | — | 0 вызовов (в тестах используется `SETTINGS["k"] = v` → `__setitem__`) | **Удалить** |

#### class `ConfigurationError` (строки 230–241)

Единый тип «конфиг/стартап невалиден». Ловится в `gateway.py:27,498,515`, `cli_agent.py:30,272,287`,
`streamlit_app.py:41,61,66`, `conftest.py:69`. Наследует `ValueError` — поэтому
`project_settings.validate_project_settings` может отлавливать его через pydantic. Вердикт — **Оставить**.

#### class `_LazySettings` (строки 498–587, 14 методов)

Прокси к конфигу: блокирует доступ до `_initialize_settings`, кэширует `_inner_dict`,
`__setitem__`/`__delitem__` разрешены только в INITIALIZED. Нужен как единственный публичный
доступ к конфигу (`SETTINGS.channels...` — 149 мест). Вердикт — **Оставить** (с чисткой 5 методов).

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 515-516 | `self._inner_dict = None` | ленивый proxy | конструктор `SETTINGS:590` | Оставить |
| `_ensure_initialized` | 518-524 | ленивый `_initialize_settings` c lock | единственная инициализация | 10 внутренних + `application_context` | Оставить |
| `__getitem__` | 526-527 | `SETTINGS["x"]` | основной API | по всему рантайму | Оставить |
| `__setitem__` | 529-536 | мутация в INITIALIZED (legacy-тесты) | — | `tests/test_config_keys.py:190`, `tests/test_profile_lifecycle.py` | Оставить |
| `__delitem__` | 538-540 | `del SETTINGS[k]` | — | `tests/test_config_keys.py:190` | Оставить (тест-контракт) |
| `__getattr__` | 542-551 | `SETTINGS.channels` | — | по всему рантайму | Оставить |
| `__contains__` | 553-556 | `in SETTINGS` | — | 1 файл (тест) | Оставить |
| `__iter__` | 558-559 | итерация по верхнему уровню | — | 0 вызовов | **Удалить** |
| `__len__` | 561-562 | `len(SETTINGS)` | — | 0 вызовов | **Удалить** |
| `__repr__` | 564-567 | дамп для отладки | — | dev-диагностика | Оставить |
| `get` | 569-578 | `SETTINGS.get(k, d)` с lazy-init | 149 мест | по всему рантайму | Оставить |
| `items` | 580-581 | `SETTINGS.items()` | — | 0 вызовов (49 вхождений `.items()` — у обычных dict'ов, не у `SETTINGS`) | **Удалить** |
| `keys` | 583-584 | `SETTINGS.keys()` | — | 0 вызовов | **Удалить** |
| `values` | 586-587 | `SETTINGS.values()` | — | 0 вызовов | **Удалить** |

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_parse_value` | 29-54 | типизация значения `.env` (`true/int/float/JSON/строка`) | секреты приходят строками | `load_env:88`; `tests/test_config.py:54-95` | Оставить |
| `_header_to_prefix` | 57-59 | `# a:b:c` → `["a","b","c"]` | вложенные секции `.env` | `load_env:79`; `tests/test_config.py:100-115` | Оставить |
| `load_env` | 62-90 | парсер `.env`-формата с `#`-секциями | чтение `.secrets.env` | `config.py:273` (всегда с явным путём); `tests/test_config.py:172-244` | **Упростить** — убрать `path or _ENV_FILE`: константы `_ENV_FILE` в модуле нет → default-ветка даёт `NameError` (проверено запуском) и недостижима |
| `_strip_jsonc_comments` | 93-138 | снятие `//`/`/* */` без порчи строк | `project.json`/`config.json` — JSONC | `load_config_json:154`; `tests/test_config.py:254-266`, `tests/test_config_keys.py:13` | Оставить |
| `load_config_json` | 141-159 | JSONC → `AttrDict`; битый файл → пустой | — | `resolve_application_config:453,461`, `_merge_profile_overlay:302` | Оставить |
| `_deep_merge` | 162-167 | рекурсивный merge с приоритетом override | 6-шаговый merge | `config.py:456,458,464,469,306` + `tests/test_config.py:121-141` | Оставить |
| `_load_session_manager_override` | 244-254 | per-deploy override (`session_manager.json`) | историческая роль пула | `resolve_application_config:458` | Оставить |
| `_load_secrets_override` | 257-274 | `.secrets.env` → dict | `${VAR}` | `resolve_application_config:469` | **Упростить** — `if _SECRETS_FILE is None or not _SECRETS_FILE.exists():` (`:271`): `_SECRETS_FILE` — module-level `Path(...)` (`:9`), он не может быть `None`; условие мёртвое |
| `_export_secrets_to_env` | 277-285 | плоский экспорт в `os.environ` (`setdefault`) | чтобы `${VAR}` в project.json нашли значения | `resolve_application_config:473` | **Упростить** — вызывается с **всем** слитым `cfg`, т.е. в окружение попадает весь project.json (`CHANNELS_POSTGRES_MAX_CONN=10` и т.п.), а не только секреты. Передавать результат `_load_secrets_override()` |
| `_merge_profile_overlay` | 288-306 | применить `profiles/<mode>.jsonc` (для `prod` — no-op) | изоляция runtime-таблиц | `resolve_application_config:475` | Оставить |
| `validate_profile_overlay` | 309-347 | hard-fail: только 6 profile-owned ключей | не дать профилю переопределить shared-ресурс | `_merge_profile_overlay:305`; `tests/test_profile_integration.py` | Оставить |
| `validate_runtime_isolation` | 350-382 | hard-fail: имена 6 runtime-таблиц == профилю | prod/test не смешивают таблицы | `resolve_application_config:479`; `tests/test_profile_lifecycle.py` | Оставить |
| `validate_profile_overlay._walk` | 322-331 | рекурсивный обход overlay | поиск лишних ключей | вложена в `validate_profile_overlay` | Оставить |
| `_resolve_env_refs` | 388-402 | рекурсивная подстановка `${VAR}` | секреты в конфиге | `resolve_application_config:477` | Оставить |
| `_flatten_env` | 405-424 | `{"a": {"b": 1}}` → `{"A_B": "1"}` | экспорт в env | `_export_secrets_to_env:283`; `tests/test_config.py:148-167` | Оставить |
| `_` | 427-429 | sanitize имени env-переменной | — | `_flatten_env` | **Упростить** — односимвольное имя `_` внутри `config.py` не читается; переименовать в `_env_key` |
| `resolve_application_config` | 432-481 | 6-шаговый merge + hard-fail | единственная сборка конфига | `_initialize_settings:645`; `tests/test_profile_lifecycle.py`, `tests/test_config_resolver.py` | Оставить |
| `_initialize_settings` | 593-649 | единственная точка публикации `SETTINGS` | lifecycle-gate | `gateway.py:116`, `cli_agent.py:117`, `streamlit_app.py:68`, `tests/conftest.py:66` | Оставить |
| `is_settings_initialized` | 652-659 | `True` после успешной инициализации | fail-fast в standalone-скриптах | `tools/check_indexes.py:260`, `tools/build_vectors.py:812`, `skills/*/scripts/cli.py`, `tests/conftest.py:66` | Оставить |
| `get_active_profile` | 662-669 | `SETTINGS["profile"]` | — | **0 вызовов** (только упоминание в docstring `tests/test_profile_lifecycle.py:4`). Дублирует `SETTINGS["profile"]`, который читается напрямую в `lib/services/subprocess_manager.py:88` и `application_context.py:246` | **Удалить** |
| `get_setting` | 672-707 | безопасный доступ по пути ключей | 6 файлов | `application_context.py:1720`, `tools/*`, skills | **Упростить** — `except ConfigurationError: return default` (`:706-707`) недостижим: `SETTINGS._inner_dict` — прямой слот-доступ, а в ветке «`SETTINGS` — не `_LazySettings`» (`else`) `try` не покрывает ничего |
| `require_setting` | 710-725 | строгий доступ по пути | 1 файл | `application_context.py:1721`; `tests/test_config_keys.py:210-218` | Оставить |

---

## `streamlit_app.py` — 669 LOC (code 476)

**Назначение.** UI на Streamlit поверх PG-таблиц сообщений: чат, вложения, прогресс, окно контекста.
**Что делает.** Резолвит профиль из `sys.argv` (`:44`), читает историю из БД (`:144`),
по кругу сабмитит сообщение в `agent_question_runs`/канал и опрашивает ответ (`:514`, `:562`),
отдаёт вложения через `SessionFileStore`. Side effects: запись в PG, файлы в
`workspace/data_store/cache/sessions/…`, `import streamlit` на module level (`:98`).
**Зачем нужен.** Альтернативный интерфейс к тому же gateway; без него агент доступен только через
CLI/канал.
**Вердикт.** Упростить
**Обоснование.** Функциональность нужна, но файл содержит мёртвый `_MAX_WAIT` (цикл намеренно
бесконечен), два неиспользуемых импорта и мёртвый повторный `pop` в session_state; плюс
третья копия резолва профиля из `argv` (после `gateway._parse_args` и `cli_agent._parse_args`).
**Доказательства.** Точка входа (`streamlit run`), спавнится из `lib/services/subprocess_manager.py`;
тест `tests/test_streamlit_app.py`.

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_resolve_profile_from_argv` | 44-61 | `--profile=<v>` из `sys.argv` (streamlit пробрасывает после `--`) | профиль для UI | `streamlit_app.py:64` | **Упростить** — третья реализация разбора `--profile` (первые две: `gateway.py:60-79`, `cli_agent.py:75-84`). Кандидат на общий хелпер в `config.py` |
| `_load_chat_history` | 144-195 | история чата из БД → `st.session_state.messages` | контекст диалога | `:372` | Оставить |
| `_get_extension_from_mime` | 198-207 | расширение по MIME через `utils.session_file_store` | имя файла | `:427,465` | Оставить |
| `_save_file_from_data_url` | 210-225 | сохранение data-URL через `SessionFileStore` | вложения | `:429,471` | Оставить |
| `_check_response` | 228-249 | ответ assistant'а или `(None, None)` | основной цикл | `:514` | Оставить |
| `_get_processing_state` | 252-266 | промежуточное состояние `processing` | живой прогресс | `:562` | Оставить |
| `_render_context_window` | 269-297 | прогресс-бар занятости контекста | M1 UI | `:410,583` | Оставить |
| `_buffer_uploads` | 596-616 | on_change: переложить файлы в устойчивый буфер | переживание rerun | `:623` | Оставить; `:665` `st.session_state.pop("_pending_uploads", None)` — **Удалить** (буфер уже снят на `:631`) |

**Мёртвые символы уровня модуля** (не в брифе, найдено вручную):

| Символ | Строки | Назначение | Зачем нужен | Вердикт |
|---|---|---|---|---|
| `_MAX_WAIT` | 120 | `SETTINGS.streamlit.get("max_wait", 600)` | — | **Удалить** — цикл опроса `:557-558` намеренно бесконечен («ждём бесконечно, без таймаута»); константа не читается нигде, кроме ассерта `tests/test_streamlit_app.py:356` (тест удалить вместе) |
| `from typing import Any` | 11 | — | — | **Удалить** — не используется |
| `serialize as _media_serialize` | 109 | — | — | **Удалить** — импортирован, не вызван |

---

## `gateway.py` — 522 LOC (code 378)

**Назначение.** Точка входа HTTP-сервера: парс аргументов, инициализация конфига, сборка
`ApplicationContext`, старт каналов + агента + опционального Streamlit, graceful shutdown.
**Что делает.** `main()` (`:485`) — единственный error-lifecycle boundary (`ConfigurationError →
"FATAL:" + exit 2`). `_run` (`:189`) поднимает bus/каналы/agent, спавнит Streamlit
(`SubprocessManager`), крутит `asyncio.run` до отмены. Проверяет занятость WS-порта
(`_check_websocket_port_available`) и ищет PID слушателя через `netstat -ano` на Windows.
Side effects: сетевые bind'ы, запуск дочернего процесса streamlit, запись в лог.
**Зачем нужен.** Это prod-точка входа; без него нет сервера. Аналог `cli_agent.main`.
**Вердикт.** Упростить
**Обоснование.** Bootstrap функционален, но: одна мёртвая функция (`_gateway_print_llm_calls`),
одна мёртвая константа (`_WINDOWS_COLOR_WARNING`), избыточная ленивая обёртка
`_project_version`, `__import__("contextlib")` внутри функции, и `except ConfigurationError`
не покрывает `CacheLoadError`/`SystemExit`, хотя docstring обещает «единый boundary».
**Доказательства.** Точка входа (`python gateway.py`); 32 файла вызывают `main`/`_entrypoint_main`
по брифу; `tests/test_gateway.py`, `tests/test_gateway_entrypoint_schema_validation.py`,
`tests/test_gateway_live_media_e2e.py`, `tests/test_gateway_runner.py`.

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_parse_args` | 37-79 | разбор argv с ручным `--profile` (обязателен) | профиль — явный, не env | `main:487`, `tests/test_gateway.py` | Оставить |
| `_entrypoint_main` | 103-173 | startup + тело приложения; `--smoke` печатает баннер и выходит | smoke-режим для интеграционных тестов | `main:513` | Оставить; **Упростить** `:148` — вызов `_project_version()` рядом с безусловным импортом `project_version` на `:132` |
| `_project_version` | 176-186 | ленивая обёртка над `lib.utils.project_version` | «отложить импорт `lib.*` до первой печати» | `gateway.py:148` | **Упростить** — обёртка дублирует импорт на `:132`; вызывать `project_version()` напрямую |
| `_run` | 189-281 | рабочий цикл: каналы + agent + streamlit + shutdown | основной runtime | `main:516` | Оставить; **Упростить** `:270` `__import__("contextlib")` → обычный импорт верхнего уровня |
| `script_dir_for_runtime` | 287-297 | абсолютный путь к каталогу `gateway.py` | чтобы `import gateway` не тянул `lib.*` | `main:500,504` | Оставить |
| `_configure_logging` | 300-310 | loguru из `gateway.log_level` | — | `_entrypoint_main:122` | Оставить |
| `_gateway_print_llm_calls` | 313-324 | читает `gateway.print_llm_calls` | — | **0 вызовов** (в `_run` передаётся литерал из `ConfigService` напрямую) | **Удалить** |
| `_gateway_print_worker_activity` | 327-339 | читает `gateway.print_worker_activity` | флаг воркеров | `_run:195` | Оставить |
| `_streamlit_enabled` | 342-355 | читает `streamlit.enabled` | UI on/off | `_run:191` | Оставить |
| `_report_db_pool_startup` | 358-392 | прогрев пула + отчёт воркеров | стартовая видимость | `_run:236` | Оставить |
| `_check_websocket_port_available` | 395-446 | проверка порта WS до старта | понятная ошибка вместо трейсбека | `_run:207` | Оставить; **Упростить** `main:485-518` — `_check_websocket_port_available` поднимает `SystemExit(1)`, который `main` не ловит, т.е. выходит в обход заявленного boundary |
| `_find_listener_pid` | 449-479 | PID слушателя через `netstat -ano` (Windows) | подсказка в ошибке | `_check_websocket_port_available:434` | Оставить |
| `main` | 485-518 | boundary: `parse → validate → init → build → run`, `ConfigurationError → exit 2` | единая точка ошибок | entry point + 32 файла по брифу | **Упростить** — `except ConfigurationError` (`:498`, `:515`) не ловит `CacheLoadError` (из `ApplicationContext.create` → `_init_cache_runtime`) и `SystemExit`; либо расширить до явного tuple, либо честно сузить docstring |

**Мёртвые символы модуля:** `_WINDOWS_COLOR_WARNING` (`:34`) — присваивается, но не печатается
(в `cli_agent.py:112-113` тот же вызов печатается). Вердикт — **Удалить**.

---

## `lib/core/skill_config.py` — 325 LOC (code 247)

**Назначение.** Единый параметризованный runtime-API для чтения конфигурации skill'ов
(таблицы, LLM, CLI, чанкинг, кэш) из `project.json` по имени skill'а.
**Что делает.** Тонкие геттеры поверх `SETTINGS.skills.<name>` + `TableRegistry`; `build_cache_provider`
делегирует в `lib.services.cache_provider.open_cache_provider` (единственная точка создания).
Побочных эффектов нет (только чтение).
**Зачем нужен.** Убирает копипаст `skill_config.py` в каждом skill'е. Оба skill'а используют
тонкие обёртки, делегирующие сюда с фиксированным `_SKILL_NAME`.
**Вердикт.** Упростить
**Обоснование.** 8 из 21 функции никем не вызываются. Проверено обе обёртки
(`workspace/skills/audit_analyzer/scripts/skill_config.py` — 7 экспортов;
`workspace/skills/legal_summarizer/scripts/llm/config.py` — 8 экспортов) — ни одна из мёртвых
в них не делегирует, так что «мёртвость» из `dead_symbols.md` **подтверждена**, а не опровергнута.
**Доказательства.** Живые функции вызываются из `workspace/skills/*/scripts/**` и
`tools/build_vectors.py`; тесты `tests/test_skill_config_api.py` (18 кейсов) покрывают
`get_db_tables`, `get_db_schema`, `get_predefined_scripts_table`, `get_vector_db_table`,
`get_cli_config`, `get_chunking_config`.

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_skills` | 27-31 | секция `skills.*` | база всех геттеров | `_skill_cfg:36` | Оставить |
| `_skill_cfg` | 34-38 | `skills.<name>` | — | 8 геттеров | Оставить |
| `_tables_list` | 41-44 | `skills.<name>.tables[]` | — | `get_db_tables`, `get_db_schema`, `get_vector_db_table` | Оставить |
| `_vector_indexes_list` | 47-50 | `skills.<name>.vector_indexes[]` | — | только `get_vector_indexes:307` (мёртвая) | **Удалить** |
| `get_db_tables` | 53-66 | имена таблиц без `label` | LLM-схема skill'а | `audit_analyzer/scripts/skill_config.py:45` | Оставить |
| `get_db_schema` | 69-82 | схема по первой таблице | LLM-схема | `audit_analyzer/scripts/skill_config.py:49` | Оставить |
| `get_predefined_scripts_table` | 85-101 | реестр SQL-через `TableRegistry` (`label='scripts_registry'`) | DB-first predefined | `audit_analyzer/scripts/skill_config.py:53` | Оставить |
| `load_db_config` | 104-105 | возвращает `{"tables": [...]}` | — | 0 вызовов; нет в `project.json` (`skills.*.tables` — list, не dict) | **Удалить** |
| `get_llm_config` | 108-111 | `skills.<name>.llm` | LLM-политика | оба skill'а | Оставить |
| `get_tool_config` | 114-115 | `skills.<name>.tools` | — | 0 вызовов; секции `tools` в `project.json` нет | **Удалить** |
| `get_cli_config` | 118-126 | `skills.<name>.cli` + дефолты | CLI skill'а | оба skill'а | Оставить |
| `get_max_retries` | 129-132 | `get_cli_config(...)["max_retries"]` | — | оба skill'а | **Упростить** — однострочная обёртка над `get_cli_config`; оставить как публичный API, но отметить дублирование |
| `get_chunking_config` | 135-167 | map-reduce чанкинг | legal_summarizer | `legal_summarizer/.../llm/config.py:42` | Оставить |
| `get_brief_context_config` | 170-202 | `BriefContextBuilder` | legal_summarizer | `legal_summarizer/.../llm/config.py:46` | Оставить |
| `get_in_memory_cache_path` | 205-231 | путь `cache.duckdb` через `resolve_cache_path` | — | 0 вызовов; заменён на `build_cache_provider` (упоминается только в `CHANGELOG.md:3522` и `tools/release_v252.py:46` как исторический факт) | **Удалить** |
| `get_vector_index_path` | 234-251 | `<default_root>/<index>` | — | 0 вызовов; единственное упоминание — `docs/SKILL_AUTHORING.md:526` | **Удалить** |
| `get_vector_db_table` | 254-271 | `gateway.vector.index.storage_table` | — | 0 прод-вызовов; только `tests/test_skill_config_api.py:99,111,118` | **Упростить** — покрыт тестом, но не нужен ни runtime'у, ни skill'ам; кандидат на удаление вместе с 3 тест-кейсами |
| `build_cache_provider` | 274-300 | делегат в `open_cache_provider` | единая точка создания кэша | `audit_analyzer/scripts/skill_config.py:69` | Оставить |
| `get_vector_indexes` | 303-309 | метаданные индексов | — | 0 вызовов | **Удалить** |
| `get_embedding_config` | 312-321 | конфиг эмбеддинга | — | 0 вызовов; дубль `cache_provider_impl.read_embedding_config` | **Удалить** |
| `get_embedding_model` | 324-325 | `get_embedding_config()["model"]` | — | 0 вызовов | **Удалить** |

---

## `cli_agent.py` — 294 LOC (code 211)

**Назначение.** Точка входа CLI-агента (REPL для человека). Фиксированный профиль `test`,
`--profile` запрещён.
**Что делает.** `main()` — error-lifecycle boundary; `_entrypoint_main` инициализирует
конфиг, консольные цвета, логирование, перенос cron-хранилища, потом либо `_run_vanilla`
(чистый `nanobot agent`) либо `_run_patched` (с `PGSessionManager` и workspace-хуками).
**Зачем нужен.** Единственный интерактивный вход. Патчи runtime'а в режиме без `--patched`
не применяются — это разделение намеренное (для чистого прогона upstream).
**Вердикт.** Упростить
**Обоснование.** Главный путь (`--patched`) **сломан**: `asyncio.create_task` вызывается на
синхронной функции вне event loop. Плюс мёртвые импорты в `_entrypoint_main`, мёртвый
argparse-флаг `--help`, мёртвый `__get_cron`, дубли `_run_vanilla`/`_run_patched`, и smoke-путь,
где `stop()` — no-op из-за гарда в `ApplicationContext.stop`.
**Доказательства.** Entry point (`python cli_agent.py`); `tests/test_cli_agent.py`,
`tests/test_cli_agent_profile.py`. Ключевое: `--patched` тестируется **только** на уровне
`_parse_args` (`tests/test_cli_agent.py:319-334`), сам `_run_patched`/`main` с этим флагом
не вызываются ни в одном тесте — поэтому дефект и не пойман.

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_parse_args` | 42-87 | разбор argv; `--profile`/`-p` → `ConfigurationError` | CLI жёстко на `test` | `main:266`; `tests/test_cli_agent.py`, `tests/test_cli_agent_profile.py` | **Упростить** — `:64` `parser.add_argument("--help", "-h", action="store_true")` мёртв: при `add_help=False` ветка `:67-69` печатает help и делает `sys.exit(0)` до парсинга, `args.help` не читается |
| `_entrypoint_main` | 97-142 | startup + выбор режима + smoke | — | `main:284` | **Упростить** — `:108-109` `from lib.cli.console_loop import run_repl` и `from lib.cli.display_config import DisplayConfig` не используются (реальный `run_repl` вызывается в `_run_patched_repl:199` с локальным импортом); тот же мёртвый дублирующий импорт продублирован в `:147-148` и `:171-172` |
| `_run_vanilla` | 145-166 | `nanobot agent` без доработок | контрольный режим | `_entrypoint_main:132` | Оставить |
| `_run_patched` | 169-191 | patched-режим | основной режим CLI | `_entrypoint_main:134` | **Слить** с `_run_vanilla` — тела совпадают до вызова REPL (единственное различие: `background_task_factory`/`print_llm_calls` в `create(...)`); **исправить баг** `:191` `asyncio.create_task(_run_patched_repl(ctx, args))` → прямой вызов (функция синхронная; проверено: `RuntimeError: no running event loop`; следствие — `ctx` не стартует и не останавливается) |
| `_run_patched_repl` | 194-209 | `ctx.start()` → `run_repl` → `finally ctx.stop()` | REPL | только из `_run_patched:191` (недостижимо) | **Упростить** — свести в `_run_patched` (вызывается ровно один раз, из одного места) |
| `__get_cron` | 212-215 | возвращает `None` | — | **0 вызовов** | **Удалить** |
| `_configure_logging` | 218-229 | loguru из `cli.log_level` | — | `_entrypoint_main:121` | Оставить |
| `_migrate_cron_store` | 232-242 | перенос cron-задач в workspace | миграция | `_entrypoint_main:124,152,180` | Оставить |
| `script_dir_for_runtime` | 248-257 | абсолютный путь к каталогу `cli_agent.py` | «чистый `import`» | `_entrypoint_main:107` | Оставить |
| `main` | 263-290 | boundary `ConfigurationError → exit 2` | единая точка ошибок | entry point | **Упростить** — тот же пробел, что и в `gateway.main`: `CacheLoadError`/`SystemExit` не ловятся |

**Мёртвый символ модуля:** `_WINDOWS_COLOR_WARNING` — **не** мёртв, печатается в `:112-113`.
(В отличие от `gateway.py:34`, где тот же вызов не печатается.)

---

## `lib/core/agent_factory.py` — 287 LOC (code 218)

**Назначение.** Сборка `AgentLoop` с явной (не auto-discovery) регистрацией хуков.
**Что делает.** `create()` строит `AgentLoop` ровно один раз (двухшаговая пересборка удалена —
`tests/test_application_context_single_application_point.py`), вручную вешает
`ToolAuditHook` → `TerminalToolPrintHook` → per-turn `DatabaseLoggingHook` из `hook_factories`,
оборачивает `provider_snapshot_loader` в LLM-observer (fail-soft), присоединяет `usage_store`.
Побочные эффекты: lazy-import модулей, `sys.modules` патчинг при ошибке импорта.
**Зачем нужен.** Порядок хуков и «всегда в `ctx.hooks`» — контракт из `AGENTS.md`;
`agent_factory.py:109-112` — единственное место, где этот контракт закреплён.
**Вердикт.** Оставить
**Обоснование.** Модуль компактный и соответствует контракту. Правки — косметические
(см. таблицу) плюс одна **ложная строка** в документации.
**Доказательства.** `ApplicationContext.create:420`; тесты `tests/test_hooks_database_logging.py`,
`tests/test_runtime_patcher.py`, `tests/test_application_context_single_application_point.py`,
`tests/test_llm_usage_*`.

| Символ | Строки | Назначение | Зачем нужен | Вердикт |
|---|---|---|---|---|
| `AgentFactory.create` | 66-195 | `AgentLoop` + 3 хука + observer | единственная сборка агента | **Оставить**; **Упростить** `:174-176` — присваивание `lambda` (flake8 E731) и no-op `else`-ветка `_populate_box = lambda built: None` только чтобы не поймать `NameError`; заменить на один `def` с default-аргументом |
| `AgentFactory._wrap_provider_snapshot_loader` | 198-213 | observer поверх `provider_snapshot_loader` | учёт токенов | `create:190` | Оставить |
| `AgentFactory._import_tool_audit_hook` | 216-226 | lazy-import `ToolAuditHook` | всегда в `ctx.hooks` | `create:158` | Оставить |
| `AgentFactory._import_terminal_tool_print_hook` | 229-245 | lazy-import `TerminalToolPrintHook` | «живой вывод результатов» | `create:172` | **Оставить**; **исправить docstring `:15`** — заявлено «Отключается через `gateway.print_tools=false`», но ни `create()`, ни `ApplicationContext` этот флаг не читают (см. §) |
| `AgentFactory._build_database_logging_factory` | 248-287 | per-turn фабрика `DatabaseLoggingHook` | запись оборотов в БД | `create:184` | Оставить |

---

## `lib/core/bus_factory.py` — 130 LOC (code 101)

**Назначение.** Создание upstream `MessageBus` с опциональной обёрткой `publish_*` логгерами.
**Что делает.** `BusFactory.create` создаёт `MessageBus` и подменяет `publish_outbound`/`publish_inbound`
на async-обёртки `await logger(); await original()`. Мутирует объект bus (monkey-patch инстанса).
**Зачем нужен.** Единая точка, где логирование сообщений навешивается на шину.
**Вердикт.** Упростить
**Обоснование.** `BusFactory` жив и нужен; `build_logging_bus` — мёртвый legacy-хелпер
собственным признанием в устаревании.
**Доказательства.** `BusFactory` — `ApplicationContext.create:322` + 15 файлов;
`_wrap` — только `create:69,73`; `build_logging_bus` — 0 вызовов.

| Символ | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `BusFactory.__init__` | 54-60 | принимает inbound/outbound логгеры | DI | `application_context.py:322` | Оставить |
| `BusFactory.create` | 62-77 | `MessageBus` + обёртка publish | — | `application_context.py:322` | Оставить |
| `BusFactory._wrap` | 80-101 | подмена `bus.<method>` на async-обёртку | — | `create:69,73` | Оставить |
| `build_logging_bus` | 104-130 | синхронный shim `publish_outbound` → лог | — | **0 вызовов**; docstring сам: «предпочтительнее `BusFactory`… оставлена для обратной совместимости» | **Удалить** |

---

## `lib/core/skill_registration.py` — 98 LOC (code 78)

**Назначение.** Декларативная регистрация skill'а из `project.json::skills.<name>` в `TableRegistry`.
**Что делает.** `build_resources_for_skill` строит `TableResource`/`VectorResource` из `tables[]`
и `vector_indexes[]` с дедупликацией по `name` (конфликт → `ConfigurationError`);
`register_skill_from_config` фильтрует по `enabled` и возвращает `SkillRegistration` (или `None`).
Побочных эффектов нет.
**Зачем нужен.** Убирает per-skill `register.py`; `AGENTS.md` объявляет этот модуль
канонической точкой регистрации.
**Вердикт.** Оставить
**Обоснование.** Обе функции живые, дедупликация и fail-fast на конфликте — осмысленная логика;
форма минимальна, лишних абстракций нет. Один мёртвый импорт.
**Доказательства.** `ApplicationContext._auto_register_skills:1606`;
`tools/build_vectors.py`; тесты `tests/test_auto_register_skills.py`, `tests/test_table_registry*`.

| Символ | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `build_resources_for_skill` | 31-60 | список ресурсов skill'а с дедупликацией | единый вход регистрации | `register_skill_from_config:88` | Оставить |
| `register_skill_from_config` | 63-98 | регистрация в `TableRegistry` | — | `application_context.py:1606`, `tools/build_vectors.py` | Оставить |
| `from typing import Any` | 21 | — | — | **Удалить** — не используется |

---

## `lib/core/infra_registration.py` — 54 LOC (code 39)

**Назначение.** Регистрация инфраструктурных (не skill-овых) ресурсов рантайма в `TableRegistry`.
**Что делает.** `register_vector_storage()` читает `gateway.vector.index.storage_table` и
регистрирует её в namespace `vector.storage` (не в skills-namespace), чтобы
`CacheLoadService` загрузил таблицу эмбеддингов в DuckDB-кэш. Побочных эффектов нет.
**Зачем нужен.** Единственная точка, где runtime-инфраструктура попадает в кэш; дублировать
логику в `ApplicationContext` нельзя — `lib/core/skill_registration.py:11-13` явно это запрещает.
**Вердикт.** Оставить
**Обоснование.** Модуль корректно разделяет два namespace'а; единственная лишняя абстракция —
однострочная функция-обёртка.
**Доказательства.** `ApplicationContext._register_infra_resources:1625`; `tools/build_vectors.py`;
`tests/test_infra_registration.py`.

| Символ | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_settings` | 21-24 | функция-обёртка: lazy-import + возврат `SETTINGS` | — | `register_vector_storage:34` | **Упростить** — инлайнить (единственный вызов, обёртка не даёт ничего) |
| `register_vector_storage` | 27-54 | `register_infra("vector.storage", …)` | PG-таблица эмбеддингов в кэше | `application_context.py:1625`, `tools/build_vectors.py` | Оставить |

---

## `lib/core/__init__.py` — 1 LOC

**Назначение.** Пакетный docstring `lib/core`.
**Вердикт.** Оставить — тривиально, удалять нечего. Реэкспортов нет намеренно:
импорты вида `from lib.core.application_context import …` в проекте повсеместны,
добавление `__all__` создало бы второй публичный контракт того же пакета.

---

## Кросс-подсистемные находки

1. **`gateway.print_tools` — фантомный переключатель.** Ключ есть в `project.json`
   (`"print_tools": false`), но читателей нет ни в одном `*.py`. При этом
   `lib/core/agent_factory.py:15` и `lib/hooks/terminal_tool_print_hook.py:18` документируют
   его как способ отключения `TerminalToolPrintHook`. Риск для подсистемы хуков: оператор
   выставит `false`, а хук продолжит печатать. **Нужен владелец `lib/hooks/`:** либо реализовать
   флаг, либо убрать обе строки документации.
2. **`ApplicationContext.check_postgres` нарушает границу слоёв.** `lib/core/application_context.py:1036-1042`
   обращается к `utils.db._get_manager()` и `utils.db._Job` (приватное API `workspace/utils/db.py`)
   и делает `sys.path.insert` на каждый вызов `/health`. **Нужен владелец `workspace/utils/db.py`:**
   публичный `probe_pool()` вместо `getattr` по приватным именам.
3. **Двойной путь чтения конфигурации.** `lib/core/project_settings.py` валидирует ~120 полей,
   но `ctx.project_settings` читается только в `application_context.py:1195`; остальной код берёт
   raw `SETTINGS` через `ConfigService.settings_section()` / `skill_config`. Любая новая настройка
   может быть «валидируема, но не прочитана» (см. `default_length`, `brief_input_ratio`,
   `vector.index.enable|backend|default_root`). **Нужен владелец `lib/services/config_service.py`:**
   выбрать один путь.
4. **`_LazySettings` не поддерживает Mapping-протокол, но им пользуются как dict'ом.** `items()`/`keys()`/`values()`/`__iter__`/`__len__` не имеют вызывающих, при этом `dict(SETTINGS)`, `for k in SETTINGS`, `json.dumps(SETTINGS)` тихо не сработают (или сломаются неожиданно). Риск при миграции на `collections.abc.Mapping`.
5. **`tests/test_streamlit_app.py:356` закрепляет мёртвую константу `_MAX_WAIT`.** Тест-контракт на поведение, которого нет: цикл опроса намеренно бесконечен (`streamlit_app.py:557-558`). При удалении константы тест надо удалить вместе с ней, иначе CI упадёт.
6. **`--patched` не покрыт интеграционным тестом.** `tests/test_cli_agent.py:319-334` проверяет
   только `_parse_args`; `_run_patched` / `main` с этим флагом не вызываются. Именно поэтому
   `asyncio.create_task` на синхронной функции (`cli_agent.py:191`) дожил до релиза.
7. **Smoke-пути обоих entry point'ов не чистят ресурсы.** `gateway.py:143` и `cli_agent.py:136`
   завершаются без `stop()`; при `enable_audit` (дефолт `true`) это полная синхронная загрузка
   DuckDB-кэша из PG ради одной строки вывода. Плюс гард `stop():617-618` делает
   `cfg.stop()` no-op после `create()`-без-`start()`. Интеграционные тесты, использующие
   `--smoke` (Phase F), на каждый прогон поднимают полный cache load.
8. **Дубль ключа `TableRegistry` infra-namespace'а с разными значениями.**
   `lib/core/application_context.py:1615` = `"vector_index.storage"`,
   `lib/core/infra_registration.py:18` = `"vector.storage"`. Живой — второй. Старое имя ещё
   фигурирует в `lib/services/table_registry.py:149` (docstring-пример) и в тест-данных
   `tests/test_table_registry.py:379,433` — поэтому grep по старому ключу даёт ложное
   «используется». **Нужен владелец `lib/services/table_registry.py`:** синхронизировать
   пример и тестовые данные, чтобы следующий аудит не принял старый ключ за живой.
9. **`AGENTS.md:108` обещает «гейт `enable`» для `VectorIndexSettings.enable`, но код его
   не читает** — vector-слой включается/выключается только по факту наличия
   `storage_table`/`indexes`. Документация задаёт ложный переключатель.
