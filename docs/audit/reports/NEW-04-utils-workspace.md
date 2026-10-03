# Аудит: lib/utils + workspace/{tools,hooks,utils,skills}

**Аудитор:** Worker-04 · **Ветка:** `refactor/mcp-platform` (HEAD `563bf94`)
**Дата:** 2026-10-03

## Сводка группы

Файлов: **26** · LOC: **5306** · классов: **22** · методов: **154** · функций модульного уровня: **65** (+ 5 вложенных в `db.py`)

Подсистема: утилиты рантайма (`lib/utils`), авто-сканируемые tool'ы (`workspace/tools`), авто-сканируемые plugin-хуки (`workspace/hooks`), общие утилиты (`workspace/utils`), документация скилла (`workspace/skills/audit_analyzer/SKILL.md`).

**Ключевые находки** (каждая с путём и строкой):

1. **`debug_stream_diag.py` в рантайме НЕ включён, но его докстринг утверждает обратное** — `lib/cli/hook_loader.py:63,68` (hard-skip по allowlist) против `workspace/hooks/debug_stream_diag.py:13` («Регистрируется через `workspace/hooks/` auto-scan»). Файл — мёртвый отладочный писатель полного контента модели и reasoning на диск без флага `enabled` и без ротации. **Удалить.** Историческая находка по безопасности *частично* закрыта: allowlist (`lib/cli/hook_loader.py:_allowed_hook_names`) теперь настоящий барьер, но сам код с утечкой лежит в дереве и в каноне инвентаря.

2. **Путь вложений Postgres-канала удваивается — расхождение боевого (не латентного)** — `workspace/utils/session_file_store.py:147-148` (`base_dir / "cache" / "sessions"`) × `lib/channels/postgres_channel.py:67-80` (`_resolve_sfs_base("data_store/cache/sessions")` срезает только один уровень и возвращает `…/data_store/cache`) ⇒ реальный путь `…/data_store/cache/cache/sessions/<key>/`. При этом `workspace/hooks/session_file_redirect_hook.py:107` пишет в `…/data_store/cache/sessions/<key>/`. **Хук и хранилище работают в разных каталогах**, и media-редирект хука (`:16-18`) ищет файл там, куда его никто не положил. Баг A-2 из брифа **не исправлен**, а лишь стал незаметен.

3. **`request_id_source` всегда `None` в проде у двух tool'ов — потеряна связь с `agent_question_runs`** — `workspace/tools/audit_analyzer_query.py:207` и `workspace/tools/legal_summarizer_query.py:164` читают `getattr(ctx, "db_logging_service", None)`, а `lib/services/project_tool_loader.py` проставляет `ctx._db_logging_service` (**с подчёркиванием**). Сравните `workspace/tools/compact_context.py:129`, где атрибут назван верно. Тесты (`tests/test_audit_analyzer_query_tool.py`, `tests/test_legal_summarizer_query_tool.py`) подставляют `request_id_source=` прямо в конструктор и `create()` не проверяют, поэтому дефект зелёный.

4. **Двойная обёртка обрезки текста в трёх местах + нарушенный контракт `truncate_middle`** — `lib/utils/text_utils.py:23-47` объявляет `max_chars` «жёстким потолком» (`:32`), но возвращает `max_chars + len(маркер)`; из-за этого в `workspace/tools/audit_analyzer_query.py:53-71` и `workspace/tools/legal_summarizer_query.py:44-56` живут две **функционально идентичные** копии `_cap`. `tests/test_text_utils.py:25` проверяет потолок условием `len(out) <= 200` при `max_chars=20` — **условие пустое** (200 > длины входа), поэтому нарушение контракта не ловится. **Слить в `text_utils.truncate_middle`.**

5. **`session_file_store` — половина мёртвая после снятия `patch_save_turn`** — `save()` (`:291`), `cleanup()` (`:363`), `archive_session()` (`:420`), `prepare_content()` (`:62`) не имеют ни одного продуктового вызывающего: grep по `lib/`, `workspace/`, `gateway.py`, `cli_agent.py` даёт только внутреннюю цепочку `save → cleanup` и тесты. `cleanup` достижим лишь из мёртвого `save`. Живы только `save_attachment` (через `workspace/utils/media.py`) и `deserialize`.

6. **Async-слой `workspace/utils/db.py` — порт платформы с нулём потребителей в агенте** — `async_execute`/`async_fetch`/`async_fetchone`/`async_fetchval`/`async_transaction` (`:1110-1140`) и `_AsyncConnectionWrapper` (`:854-874`) не вызываются нигде в агенте (все каналы синхронные, обёрнутые в потоки). Тесты, которые их «покрывают» (`mcp-platform/tests/test_enterprise_data_db.py:27-31`), импортируют **платформенный** `libs.enterprise_data.db`, а не этот модуль. Это остаток переноса фазы 2.

7. **`is_stream_delta` мёртв, но `AGENTS.md` объявляет его используемым каналами** — `lib/utils/outbound_meta.py:48-57`, 0 вызывающих во всём репозитории; `AGENTS.md:55` перечисляет его среди «используется каналами». Расхождение с каноном. **Удалить** (+ править `AGENTS.md`).

8. **Два дефекта приватности/безопасности в `_ALLOWED_FILES` редирект-хука** — `workspace/hooks/session_file_redirect_hook.py:63`: в белый список записи агента входят `config.json` (боевой конфиг: DSN-индирекция, профиль enterprise-MCP, имена таблиц каналов) и `project.json` — **файла, которого в репозитории уже нет** (`Test-Path project.json` → `False`). Первое — реальная поверхность: модель, направленная записать файл, может переписать маршрутизацию и цель БД. Второе — остаток.

9. **Докстринг редирект-хука описывает несуществующий whitelist** — `workspace/hooks/session_file_redirect_hook.py:28` обещает `**/*.py` в белом списке; в коде (`:60-81`) такого правила нет. Плюс `:3` указывает на `workspace/hooks/scan_and_register`, тогда как функция живёт в `lib.cli.hook_loader`. Плюс перечень префиксов (`:27-29`) расходится с реальным (`:66-81`).

10. **`SKILL.md` audit_analyzer: историческая находка закрыта, но осталась неточность** — таблица операций (`:124-141`) корректно описывает 4 операции, соответствующие `_ROUTED_OPERATIONS` (`workspace/tools/audit_analyzer_query.py:77`), а несуществующие 4 скрипта и команда `audit_analyze` убраны. Но `:154-155` предписывает читать событие `cache_load_done`, которое **не пишет агент** — его публикует платформенный загрузчик (`mcp-platform/libs/enterprise_data/loader.py`). Не ошибка, но вводят в заблуждение: скилл не может это событие ни найти, ни повлиять на него.

11. **`_plugin_discoverable` — мёртвый атрибут в двух tool'ах** — `workspace/tools/compact_context.py:102` (в докстринге `:30-32` — «auto-loader nanobot пропускает») и `workspace/tools/document_read.py:110`. `lib/services/project_tool_loader.py::_discover` этот флаг не читает (только `issubclass` / `cls is not _T` / `__abstractmethods__`). Grep по всему репо даёт только объявления и комментарии.

12. **Двойной путь импорта `workspace/utils/*` даёт ложные нули в брифе** — `lib/channels/postgres_channel.py:48` импортирует `from utils.session_file_store import SessionFileStore`, а `:38` — `from utils.jsonb import decode_jsonb`. То есть `session_file_store.py` и `jsonb.py` **живые**, хотя бриф показал 0 импортёров (искал `workspace.utils.*`). Побочный эффект — два независимых объекта модуля в `sys.modules` (у `db.py` это `_manager`/`_dsn` = **два пула соединений** в одном процессе).

13. **Два лишних git-worktree с полной копией дорефакторингового дерева** — `git worktree list`: `.worktrees/fork-f328176d27cd-…` (ветка `fork/f328176d…`, содержит `workspace/skills/legal_summarizer/scripts/cli.py`, `office_files.py` и `lib/utils/sql_safety.py` — всё удалённое) и `AppData/Local/Temp/nb-v5` (detached). Ложные срабатывания при любом grep по репозиторию.

14. **`StreamDiagnosisHook` остался в каноне `runtime_inventory`, хотя файл вне allowlist** — `lib/services/runtime_inventory.py` `canonical_plugin_hooks()` помечает `StreamDiagnosisHook` как `required=False` с комментарием «REMOVED», а `tests/test_runtime_inventory.py:81-86` **требует** его наличия (`assert diag`). То есть гард-тест закрепляет существование мёртвого хука.

**Вердикты (файлы):** Оставить **17** · Упростить **3** · Удалить **3** · Слить **2** (входят в «Упростить» по числу) · НЕ РАЗОБРАНО **0**

**Вердикты (символы, 241 разобран):** Оставить **198** · Упростить **19** · Слить **3** · Удалить **21**

---

## `lib/utils/__init__.py` — 0 LOC

**Назначение.** Пакет-маркер для `lib.utils`.
**Что делает.** Ничего; файл пуст.
**Зачем нужен.** Делает `lib.utils` импортируемым пакетом.
**Вердикт.** `Оставить`.
**Обоснование.** Импорты `lib.utils.*` идут в `gateway.py:31,134`, `cli_agent.py:44,135`, `lib/channels/postgres_channel.py:52`, `lib/cli/console_loop.py:47`, `lib/core/agent_factory.py:23`. Удаление ломает 8+ точек импорта.

---

## `lib/utils/text_utils.py` — 47 LOC

**Назначение.** Обрезка длинного текста с сохранением головы и хвоста.
**Что делает.** Одна функция `truncate_middle`; побочных эффектов нет (чистая функция).
**Зачем нужен.** Upstream режет хвост (`nanobot/utils/helpers.py:371`), а хвост JSON/CSV несёт данные. Единственный потребитель — `workspace/tools/history_search_tool.py:84`.
**Вердикт.** `Упростить`.
**Обоснование.** Докстринг `:32` объявляет `max_chars` «жёстким потолком», но `:41-47` возвращает `half + len(маркер) + half` = `max_chars + len(маркер)`. Из-за этого появились две копии корректной логики в tool'ах (находка 4). Кроме того, `__all__` (`:20`) и 12 строк докстринга (`:5-10`) описывают удалённую `sanitize_value` — исторический след, сам по себе безвредный, но занимает четверть модуля.
**Доказательства.** `history_search_tool.py:84,442,497`; `tests/test_text_utils.py` — 5 тестов, все зелёные, **но ни один не проверяет потолок**: `test_truncation_marker:25` требует `len(out) <= 200` при `max_chars=20` и входе длиной 200, то есть условие истинно всегда; `test_preserves_head_and_tail:27-31` требует лишь `startswith("HEAD")`/`endswith("TAIL")` при `max_chars=40`, что **удовлетворяется и текущей, и исправленной** реализацией. То есть контракт из `:32` не защищён тестом — это дыра в покрытии, а не «тест закрепляет неверное поведение».

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `truncate_middle` | 23–47 | обрезка с сохранением головы/хвоста | единственный хелпер text-слоя | `workspace/tools/history_search_tool.py:442,497` | `Упростить` — перенести в него корректную логику из `_cap` (см. ниже), сократив докстринг `:5-10` об удалённой `sanitize_value`; **побочно** потребует правки `tests/test_text_utils.py` |

---

## `lib/utils/project_version.py` — 67 LOC

**Назначение.** Версия проекта для стартового баннера.
**Что делает.** Читает `config.json → project.version`; при отсутствии — `git describe` через subprocess. Побочный эффект: может породить `git` subprocess на старте (только при отсутствии ключа в конфиге; в боевом `config.json` ключ есть, поэтому на практике не срабатывает). `git rev-parse` вызывается с `cwd=repo`, timeout ограничен — **не проверено** фактическое поведение при отсутствии `git` (см. `:55-67`, там `try/except`).
**Зачем нужен.** Баннер версии в `gateway.py:141,150` и `cli_agent.py:144`.
**Вердикт.** `Оставить`.
**Обоснование.** Живой: `gateway.py:134,187`, `cli_agent.py:135`, плюс `tests/test_config_keys.py:278-283` сверяет его вывод с ключом конфига. Удаление ломает баннер и тест.
**Доказательства.** `gateway.py:178-188` (ленивая обёртка `_project_version`), `cli_agent.py:135,144`, `lib/core/project_settings.py:726` (упоминание в докстринге).

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `project_version` | 26–33 | публичная точка | баннер + тест | `gateway.py:187`, `cli_agent.py:135` | Оставить |
| `_config_version` | 35–53 | `config.json → project.version` | основной источник | `project_version:31` | Оставить |
| `_git_version` | 55–67 | fallback через `git describe` | версия при отсутствии ключа | `project_version:32` | Оставить |

Классовая переменная `_PROJECT_ROOT` (`:23`) — `parents[2]` от модуля. Используется обоими fallback'ами; `project_version(root=...)` позволяет переопределить. Оставить.

---

## `lib/utils/node_access.py` — 49 LOC

**Назначение.** Безопасный доступ к вложенным секциям pydantic-конфига.
**Что делает.** Чистые функции, `getattr`-цепочка по pydantic-узлам с фолбэком на dict-ключи. Побочных эффектов нет.
**Зачем нужен.** `ctx.config` отбрасывает неизвестные секции, поэтому чтение настроек идёт от merged `SETTINGS` через эти хелперы.
**Вердикт.** `Оставить`.
**Обоснование.** Живой: `lib/services/config_service.py:23,80-82`, `lib/services/channel_factory.py:24`, `lib/services/runtime_patcher.py:61`.
**Доказательства.** `config_service.py:23` (`_get`), `:82`; `channel_factory.py:24` (`_section`); `runtime_patcher.py:61` (`_get`).

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `get_path` | 13–33 | `getattr`-цепочка + dict-фолбэк | единый доступ к конфигу | `config_service.py:23`, `channel_factory.py:24`, `runtime_patcher.py:61` | Оставить |
| `get_settings_section` | 36–49 | сахар над `get_path` для секций | меньше шума в вызывающих | `config_service.py:82`, `channel_factory.py:24` | Оставить |

---

## `lib/utils/outbound_meta.py` — 126 LOC

**Назначение.** Единый контракт классификации outbound-сообщений (шум / финал / отброшено).
**Что делает.** Чистые предикаты по `msg.metadata` и `msg.event`. Побочных эффектов нет. **Единственный источник истины** для трёх разных потребителей, что делает его местом, где расхождение опасно.
**Зачем нужен.** Каналы решают, что показывать; `db_logging_bus` решает, что писать в `agent_gateway_logs`.
**Вердикт.** `Упростить` (удалить один символ).
**Обоснование.** `is_stream_delta` (`:48-57`) не имеет ни одного вызывающего во всём репозитории, при этом `AGENTS.md:55` утверждает обратное. Обоснование «legacy-проверка» не подтверждается: стриминг идёт через типизированный `StreamDeltaEvent` в `msg.event`, а `_stream_delta` не входит в `OUTBOUND_DROPPED_KEYS`, так что на фильтрацию функция не влияет.
**Доказательства.** Живые: `postgres_channel.py:52` (`FINAL_TURN_KEY`, `is_dropped:1255`), `db_logging_bus.py:49,81,185,187` (`msg_session_key`, `is_outbound_noise`, `is_outbound_final`), `runtime_patcher.py:778` (`FINAL_TURN_KEY`). Мёртв: `is_stream_delta`. `tests/test_outbound_meta.py` покрывает `_typed_event` (`:15,24,39,60`), не `is_stream_delta`.

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `is_dropped` | 36–46 | `metadata` помечен как отброшенный | канал не отправляет | `postgres_channel.py:1255`, `is_outbound_noise:103` | Оставить |
| `is_stream_delta` | 48–57 | legacy-проверка `_stream_delta` | — | **0 вызывающих** | **`Удалить`** + править `AGENTS.md:55` |
| `_typed_event` | 60–71 | достать типизированное событие | основа предикатов | `is_outbound_final:80`, `is_outbound_noise:98`; `tests/test_outbound_meta.py` | Оставить |
| `is_outbound_final` | 73–92 | «это финальный ответ» | что писать в БД | `db_logging_bus.py:49,187` | Оставить |
| `is_outbound_noise` | 94–116 | «это шум стрима» | что не писать | `db_logging_bus.py:49,185` | Оставить |
| `msg_session_key` | 118–126 | достать ключ сессии из `msg` | бакет журнала | `db_logging_bus.py:49,81` | Оставить |

Классовая константа `OUTBOUND_DROPPED_KEYS` (`:18-25`) — Оставить (потребитель `is_dropped`). `FINAL_TURN_KEY` — Оставить (три потребителя).

---

## `lib/utils/windows_terminal.py` — 210 LOC

**Назначение.** Безопасный вывод ANSI в legacy Windows-консоли.
**Что делает.** `enable_vt` включает `ENABLE_VIRTUAL_TERMINAL_PROCESSING` через `ctypes` на STDOUT/STDERR; `install_ansi_stripper` **подменяет `sys.stdout` объектом-фильтром**, вырезающим escape-последовательности; `ensure_console_colors` — диспетчер: пробует VT, при неудаче ставит `NO_COLOR=1` + stripper. Побочные эффекты: **глобальная подмена `sys.stdout`** и запись переменной окружения `NO_COLOR`. Вне Windows — no-op.
**Зачем нужен.** Без него Rich-вывод превращается в мусор `?[1m` и загрязняет промпт (`cli_agent.py:47`).
**Вердикт.** `Оставить`.
**Обоснование.** Живой из трёх точек входа: `cli_agent.py:44,51`, `gateway.py:31,36`, `lib/cli/console_loop.py:47,230,362`. Регрессия против `nanobot` upgrade минимальна — модуль работает со stdio-handle'ами Win32, а не с приватным API nanobot. `tests/test_windows_terminal.py` покрывает posix-noop, piped-stdout и win32-tty (19 тестов при прогоне с `test_office_files.py`).
**Доказательства.** См. выше. Внутренние `_console_mode`/`is_vt_enabled`/`is_windows_console`/`install_ansi_stripper` вызываются только из этого модуля и из тестов.

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_console_mode` | 40–58 | чтение/запись `ConsoleMode` | низкоуровневый доступ | `is_vt_enabled:68`, `enable_vt:87` | Оставить |
| `is_vt_enabled` | 61–71 | VT уже включён? | не вызывать `SetConsoleMode` дважды | `ensure_console_colors:195`; `tests/test_windows_terminal.py:27` | Оставить |
| `enable_vt` | 74–102 | включить VT | предпочтительный путь | `ensure_console_colors:194`; `tests/:26,36,80` | Оставить |
| `is_windows_console` | 105–116 | Windows + настоящая консоль | guard для CI/pipe | `install_ansi_stripper:126`, `ensure_console_colors:174` | Оставить |
| `install_ansi_stripper` | 119–165 | подмена `sys.stdout` на фильтр | fallback без VT | `ensure_console_colors:203` | Оставить (побочный эффект задокументирован в `:119-127`) |
| `ensure_console_colors` | 171–210 | публичная точка входа | вызывается из 3 entrypoints | `cli_agent.py:51`, `gateway.py:36`, `console_loop.py:230,362` | Оставить |

Классовая константа `_WARNED` (`:168`) — флаг однократного предупреждения. Оставить.

---

## `lib/utils/logging_utils.py` — 181 LOC

**Назначение.** Единая настройка loguru + мост stdlib→loguru.
**Что делает.** `configure_loguru` ставит/переставляет sink, идемпотентна, читает уровень из переменной окружения; `configure_stdlib_bridge` вешает `logging.Handler` на root-logger, чтобы записи из `logging` (в т.ч. из плагинов) попадали в loguru. Побочные эффекты: **глобальная настройка root-логгера**, чтение env. Диспетчеризация по уровню (`DEBUG` → только loguru, иначе мост включён всегда) задокументирована.
**Зачем нужен.** Без моста записи из `workspace/hooks/*` (используют `logging.getLogger`) не попадали бы в журнал приложения.
**Вердикт.** `Оставить`.
**Обоснование.** Живой: `gateway.py:501-503`, `cli_agent.py:266-268`, `tests/conftest.py:20-22` (на весь прогон тестов). `tests/test_logging_bridge.py` — 25+ тестов, включая проверку идемпотентности и режимов (`INFO` → мост, `DEBUG` → без моста, `:232`).
**Доказательства.** См. выше.

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `configure_stdlib_bridge` | 109–139 | мост stdlib→loguru | записи плагинов в журнал | `configure_loguru:179`; `tests/test_logging_bridge.py` (много) | Оставить |
| `configure_loguru` | 141–179 | настроить loguru + мост | единая точка конфигурации | `gateway.py:503`, `cli_agent.py:268`, `conftest.py:22` | Оставить |

---

## `workspace/tools/__init__.py` — 26 LOC

**Назначение.** Документирует конвенции регистрации авто-сканируемых tool'ов.
**Что делает.** Ничего (только докстринг). Код в нём отсутствует.
**Зачем нужен.** Это единственное место, где зафиксированы четыре конвенции (`config_key` / `config_cls` / `enabled` / `create`, чтение через `ctx._settings_ref` и **не** через `ctx.config`, сигнатура `execute`).
**Вердикт.** `Упростить`.
**Обоснование.** Все четыре конвенции подтверждены кодом tool'ов и работают. Но `:3-4` называет загрузчиком `RuntimePatcher.patch_project_tools` — **такого метода не существует** с момента переноса регистрации в `lib/services/project_tool_loader.py` (`AGENTS.md` это фиксирует). Это та же устаревшая ссылка, что и в `compact_context.py:30-32`. Второй нюанс: `:11-12` говорит «`gateway.<name>.*`, если так сложилась история — см. `compact_context`» — фактически `compact_context` читает `gateway.compact.*`, т.е. имя секции не совпадает с `config_key`; оговорка верная, но её легко скопировать неправильно.
**Доказательства.** Потребителей нет (пакет-маркер); ценность — как документация. `lib/services/project_tool_loader.py::_discover` — фактический загрузчик.

---

## `workspace/tools/audit_analyzer_query.py` — 398 LOC

**Назначение.** Единственный вход агента к capability `audit` платформы.
**Что делает.** `execute` вызывает `EnterpriseMcpClient.call` с capability `audit` и одной из `_ROUTED_OPERATIONS` (`:77`); `_build_arguments` (`:305`) переводит аргументы модели в контракт платформы; `_cap` (`:53`) усекает ответ до `max_result_chars`; `_identity` (`:352`) строит `CallIdentity` из `ctx._agent_ref` для журналирования на стороне платформы. **Прямых SQL-запросов нет** — вся работа ушла платформенному `libs.audit`. Сеть: да, stdio-сессия к процессу `enterprise-mcp`.
**Зачем нужен.** Данные аудита принадлежат платформе; без этого tool'а агент не имеет доступа к ним. Заменяет skill-side `scripts/cli.py`.
**Вердикт.** `Оставить` (с двумя правками).
**Обоснование.** `required=True` в `lib/services/runtime_inventory.py:162-172`, включён в `config.json → tools.audit_analyzer_query.enable`. Гарды зелёные: `tests/test_audit_analyzer_query_tool.py`, `test_architecture_tool_domain_free.py` (домен-маркеры `oarb`/`audits_index` в докстрингах запрещены и отсутствуют).
**Доказательства.** `canonical_project_tools()` `runtime_inventory.py:162-172`; `lib/services/project_tool_loader.py::_discover`; `tests/test_audit_analyzer_query_tool.py` (инъектирует `request_id_source=` в конструктор, минуя `create()`).

#### class `AuditAnalyzerQueryToolConfig` (80–146)
Pydantic-модель секции `tools.audit_analyzer_query`. Нужна: `create()` (`:194`) валидирует секцию и отдаёт дефолты. **Оставить.**

#### class `AuditAnalyzerQueryTool` (148–398, 11 методов)
Класс-плагин авто-сканируемый, регистрируется в `AgentLoop`. Атрибуты: `config_key` (`tools.audit_analyzer_query`), `_ROUTED_OPERATIONS` (classmethod-константа), `_client` (инъецируется), `request_id_source` (инъецируется).

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 153–166 | приём `config`/`client`/`request_id_source` | DI-конструктор | `create:194` | Оставить |
| `config_cls` | 168–170 | вернуть pydantic-модель | контракт загрузчика | `create:200`, `project_tool_loader` | Оставить |
| `_read_settings_section` | 172–188 | defensive-чтение `ctx._settings_ref` | обход `ToolsConfig`-отбрасывания | `enabled:190`, `create:195` | Оставить |
| `enabled` | 190–192 | гейт регистрации | флаг `enable` | `project_tool_loader::_discover` | Оставить |
| `create` | 194–209 | собрать tool из ctx | DI | `project_tool_loader::_discover` | **Упростить** — см. находку 3: читать `ctx._db_logging_service`, а не `ctx.db_logging_service` (строка `:207`) |
| `name` | 211–213 | `"audit_analyzer_query"` | список tool'ов | фреймворк | Оставить |
| `description` | 215–236 | текст для модели | выбор tool'а | LLM-промпт | Оставить |
| `execute` | 238–303 | вызов capability `audit` | действие | LLM | Оставить |
| `_build_arguments` | 305–343 | маппинг аргументов в контракт платформы | схема вызова | `execute` | Оставить |
| `_error` | 345–350 | единый формат ошибки | читаемость для модели | `execute` | Оставить |
| `_identity` | 352–398 | `CallIdentity` из `ctx._agent_ref` | журналирование на платформе | `execute` | Оставить (следствие правки `create`) |

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_cap` | 53–71 | усечение до жёсткого `max_result_chars` | бюджет ответа | `execute` | **`Слить с lib/utils/text_utils.py`** — функционально идентична `workspace/tools/legal_summarizer_query.py:44-56`; перенести корректную логику в `truncate_middle`. **Строка, которая лжёт:** докстринг `:58-61` утверждает, что `tests/test_text_utils.py::TestTruncateMiddle` «закрепляет именно это поведение (на `max_chars=40` он требует сохранить 4 символа головы и 4 хвоста)» — тест (`:27-31`) требует лишь `startswith("HEAD")`/`endswith("TAIL")`, что выполняется и для исправленной реализации. Обоснование дублирования не подтверждается |

---

## `workspace/tools/legal_summarizer_query.py` — 293 LOC

**Назначение.** Вход агента к capability `legal_summarizer` платформы.
**Что делает.** `execute` вызывает `query_operation` (`:37`) с операцией `legal_summarizer`; фильтрует поля ответа по `_FIELDS` (`:41`); `_cap` усекает до `max_result_chars`; `_identity` строит `CallIdentity`. **Прямых SQL нет**, обращений к удалённым модулям нет — проверено: единственный импорт клиента это `lib.services.enterprise_mcp_client`.
**Зачем нужен.** Скилл `legal_summarizer` уехал на платформу (фаза 11), но агенту по-прежнему нужен вход к этому capability. Скилл-часть (Python) удалена целиком — в `workspace/skills/legal_summarizer/` больше нет ни `scripts/`, ни `__init__.py`.
**Вердикт.** `Оставить` (с той же правкой `create`, что и в audit-инструменте).
**Обоснование.** Живой: `required=True` (`runtime_inventory.py` ~:150-161), `config.json:947-949` (`enable: true`). Гард `tests/test_no_legal_imports_in_agent.py` проходит — значит, tool не тянет логику скилла. **Проверено:** после переезда скилла этот файл **не** остался вызывающим удалённое.
**Доказательства.** `canonical_project_tools()`; `lib/services/project_tool_loader.py::_discover`; `tests/test_legal_summarizer_query_tool.py`.

#### class `LegalSummarizerQueryToolConfig` (59–94)
Pydantic-модель `tools.legal_summarizer_query`. **Оставить.**

#### class `LegalSummarizerQueryTool` (96–293, 10 методов)

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 101–114 | приём `config`/`client`/`request_id_source` | DI | `create:147` | Оставить |
| `config_cls` | 116–118 | pydantic-модель | контракт загрузчика | `create:153` | Оставить |
| `_read_settings_section` | 120–141 | defensive-чтение секции | обход `ToolsConfig` | `enabled:143`, `create:148` | Оставить |
| `enabled` | 143–145 | гейт регистрации | флаг `enable` | `project_tool_loader` | Оставить |
| `create` | 147–166 | собрать tool из ctx | DI | `project_tool_loader` | **Упростить** — `:164` читает `ctx.db_logging_service` вместо `ctx._db_logging_service` (находка 3) |
| `name` | 168–170 | `"legal_summarizer_query"` | список tool'ов | фреймворк | Оставить |
| `description` | 172–183 | текст для модели | выбор tool'а | LLM-промпт | Оставить |
| `execute` | 185–240 | вызов `query_operation` | действие | LLM | Оставить |
| `_error` | 242–246 | единый формат ошибки | читаемость | `execute` | Оставить |
| `_identity` | 248–293 | `CallIdentity` | журналирование | `execute` | Оставить |

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_cap` | 44–56 | усечение до жёсткого потолка | бюджет ответа | `execute` | **`Слить с lib/utils/text_utils.py`** — идентична `audit_analyzer_query.py:53-71` |

---

## `workspace/tools/compact_context.py` — 177 LOC

**Назначение.** Tool-обёртка над `ContextCompactionService`.
**Что делает.** `execute` (`:160`) зовёт единственную публичную точку сжатия — `ContextCompactionService.notify_session_compacted()`. SQL нет, файлов нет, глобального состояния нет. Ключ сессии санитизируется **инлайн-регуляркой** (`:162-165`), а не через `resolve_session_key`.
**Зачем нужен.** Даёт модели явный рычаг сжатия контекста (`force=True` — жёсткий путь, `idle` — legacy-алиас).
**Вердикт.** `Оставить` (с мелкой правкой).
**Обоснование.** Живой: `canonical_project_tools()` (`runtime_inventory.py:141-152`, `required=True`), но `config_key` в спеке указан как `tools.compact_context.enable`, тогда как код читает `gateway.compact.enabled` (`:109-121`) — расхождение инвентаря с кодом (вне моей подсистемы, `runtime_inventory.py`). Внутри файла: `create` (`:124`) — **единственный, кто читает `ctx._db_logging_service` правильно** (`:129`); это эталон для находки 3. `_plugin_discoverable` (`:102`) — мёртвый атрибут.
**Доказательства.** `runtime_inventory.py:141-152`; `lib/services/context_compaction.py` (потребитель сервиса); `tests/test_architecture_tool_domain_free.py` — проходит.

#### class `CompactToolConfig` (43–89)
Pydantic-модель `gateway.compact`. **Оставить.**

#### class `CompactContextTool` (91–177, 7 методов)

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `config_cls` | 105–107 | pydantic-модель | контракт загрузчика | `create:124` | Оставить |
| `enabled` | 109–121 | читает `gateway.compact.enabled` | гейт регистрации | `project_tool_loader` | Оставить |
| `create` | 124–141 | собрать сервис из ctx | DI | `project_tool_loader` | Оставить (`:129` — корректное имя атрибута) |
| `__init__` | 143–145 | хранит `_service`/`_config` | состояние | `create:141` | Оставить |
| `name` | 147–149 | `"compact_context"` | список tool'ов | фреймворк | Оставить |
| `description` | 151–158 | текст для модели | выбор tool'а | LLM-промпт | `Упростить` — перечисляет только `idle`, не упоминая `force`, хотя `parameters` объявляет `force` с дефолтом `true`; модель читает `description`, а не схему |
| `execute` | 160–177 | вызов сервиса + санитизация ключа | действие | LLM | `Упростить` — заменить инлайн-регулярку `:162-165` на `resolve_session_key` из `workspace/utils/session_key.py:40` (тот же путь, что у хука `session_file_redirect_hook.py:306`) |

Классовые атрибуты: `config_key = "compact"` (`:102`-блок), `_plugin_discoverable: ClassVar[bool] = False` (`:102`) — **`Удалить`**: `project_tool_loader::_discover` флаг не читает; grep по репо даёт только объявление и комментарий `:30-32` «auto-loader nanobot пропускает», что неверно.

---

## `workspace/tools/document_read.py` — 299 LOC

**Назначение.** Извлечение текста из офисных документов — носитель порога длины текста.
**Что делает.** `execute` (`:189`) резолвит путь, проверяет расширение по `SUPPORTED_SUFFIXES`, лениво импортирует `libs.office.extract_text` (платформенный пакет) и отдаёт текст либо JSON-маркер `[text omitted ...]` с подсказкой дочитать через `offset`/`chunk_chars`. **Читает произвольные пути** — ограничение только по расширению (см. ниже). Сети нет.
**Зачем нужен.** Содержимое документа не вставляется в промпт заранее; tool'а — единственный способ его прочитать. Платформа владеет парсером, агент — порогом.
**Вердикт.** `Оставить` (с одной правкой атрибута).
**Обоснование.** `required=True` (`runtime_inventory.py:173-182`), `config.json → tools.document_read.enable`. Гард `tests/test_architecture_tool_domain_free.py` проходит; `tests/test_office_files.py` (19 тестов) проходит отдельно.
**Доказательства.** `canonical_project_tools()`; `lib/services/project_tool_loader.py::_discover`; `tests/test_office_files.py`; потребитель парсера — `mcp-platform/libs/office/`.

#### class `DocumentReadToolConfig` (74–104)
Pydantic-модель `tools.document_read`. **Оставить.**

#### class `DocumentReadTool` (106–299, 11 методов)

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 112–113 | хранить конфиг | состояние | `create:163` | Оставить |
| `config_cls` | 120–121 | pydantic-модель | контракт загрузчика | `create:158,162` | Оставить |
| `_read_settings_section` | 124–147 | defensive-чтение секции | обход `ToolsConfig` | `enabled:151`, `create:156` | Оставить |
| `enabled` | 150–152 | гейт регистрации | флаг `enable` | `project_tool_loader` | Оставить |
| `create` | 155–163 | собрать tool, пережив битый конфиг | DI + fail-soft | `project_tool_loader` | Оставить (`try/except:159-162` — осознанный fail-soft, задокументирован) |
| `name` | 166–168 | `"document_read"` | список tool'ов | фреймворк | Оставить |
| `description` | 170–178 | текст для модели | выбор tool'а | LLM-промпт | Оставить |
| `_max_chars` | 184–187 | приоритет override над конфигом | лимит ответа | `execute` | Оставить |
| `execute` | 189–266 | извлечение + окно + маркер | действие | LLM | Оставить |
| `_window_size` | 273–280 | приоритет `chunk_chars` над `max_chars` | окно чтения | `execute:212` | Оставить |
| `_resolve_path` | 283–299 | путь от cwd, затем абсолютный | поиск файла | `execute:199` | Оставить **с оговоркой безопасности** (ниже) |

Классовый атрибут `_plugin_discoverable: ClassVar[bool] = False` (`:110`) — **`Удалить`**, тот же мёртвый флаг, что и в `compact_context.py`.

**Риск приватности (умеренный).** `_resolve_path` (`:283-299`) не ограничивает чтение рабочим каталогом: `Path(path).expanduser()` и `candidate.is_file()` допускают любой абсолютный путь на хосте. Смягчает гейт `SUPPORTED_SUFFIXES` (`:205-210`) — прочитать `.env`, `config.json` или `id_rsa` нельзя, но любой `.txt`/`.csv`/`.pdf`/`.docx` в системе доступен модели, если она угадает путь. Стоит либо ограничить корень `workspace/data_store/cache/sessions/`, либо явно принять это как осознанный риск в каноне.

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_as_offset` | 60–70 | привести `offset` к неотрицательному int | защита от мусора от модели | `execute:213` | Оставить |

---

## `workspace/tools/history_search_tool.py` — 589 LOC

**Назначение.** Поиск по долговечному журналу `agent_gateway_logs` — память агента о прошлых оборотах.
**Что делает.** `execute` (`:316`) строит параметризованный `SELECT` из whitelist-колонок журнала; имя таблицы/схемы резолвится из merged SETTINGS (не зашито); фильтры — `event_type`, `session_id`, `tool_name`, `user_id`, временное окно, `ILIKE` по `payload::text`. Три уровня усечения: лимит строк → обрезка жирного payload до `per_event_cap` → общий бюджет `max_result_chars`. Возвращает `next_offset` и `results_truncated`. **SQL параметризован полностью** — инъекции нет (проверено: значения идут через `%s`, идентификаторы — из SETTINGS).
**Зачем нужен.** После сжатия контекста агент теряет историю; `AGENTS.md` называет этот tool единственным способом «вспомнить» факт `context_compacted`. Без него события журнала модели недоступны.
**Вердикт.** `Оставить`.
**Обоснование.** Самый проработанный файл группы: изоляция по user/session (есть гард `tests/test_history_search_user_isolation_guards.py`), корректный цикл до-усечения (`:432-442`, `:490-497`), accessor-слой `client_from_settings` для тестов. Гарды зелёные: `tests/test_history_search_tool.py` (16 пропусков — предсуществующие, помечены `ISSUE-NB035-4`, не связаны с этим аудитом), `test_skill_tool_independence.py`, `test_architecture_tool_domain_free.py`.
**Доказательства.** `canonical_project_tools()`; `lib/services/project_tool_loader.py::_discover`; `tests/test_history_search_tool.py`; `AGENTS.md` (ссылка как на механизм доступа к `context_compacted`).

#### class `HistorySearchToolConfig` (87–205)
Pydantic-модель секции конфига: лимиты строк, символов, окно времени, поля фильтров. **Оставить.**

#### class `HistorySearchTool` (207–549, 9 методов)

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 212–215 | приём `config`/клиента | DI + тестовый seam | `create:251` | Оставить |
| `config_cls` | 217–219 | pydantic-модель | контракт загрузчика | `create` | Оставить |
| `_read_settings_section` | 221–244 | defensive-чтение секции | обход `ToolsConfig` | `enabled:246`, `create:252` | Оставить |
| `enabled` | 246–249 | гейт регистрации | флаг `enable` | `project_tool_loader` | Оставить |
| `create` | 251–262 | собрать tool из ctx | DI | `project_tool_loader` | Оставить |
| `name` | 264–266 | `"history_search"` | список tool'ов | фреймворк | Оставить |
| `description` | 268–314 | текст для модели | выбор tool'а | LLM-промпт | Оставить |
| `execute` | 316–542 | параметризованный поиск + усечение | действие | LLM | Оставить |
| `_error` | 544–549 | единый формат ошибки | читаемость | `execute` | Оставить |

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_current_session_key` | 551–558 | ключ текущей сессии | фильтр по сессии | `execute` | Оставить |
| `_current_user_id` | 560–589 | id текущего пользователя | изоляция данных между пользователями | `execute` | Оставить |

Мелочь: `per_event_cap = 4000` (`:442`-блок) — **магическое число вне конфига**, тогда как `max_result_chars` конфигурируем. Низкий риск; кандидат на вынос в `HistorySearchToolConfig`.

---

## `workspace/hooks/__init__.py` — 0 LOC

**Назначение.** Пакет-маркер для `workspace.hooks`.
**Что делает.** Ничего; файл пуст.
**Зачем нужен.** Консистентность пакетов; сканер `lib/cli/hook_loader.py:66` пропускает файлы, начинающиеся с `_`, поэтому пустой `__init__.py` гарантированно не попадёт в allowlist-скан.
**Вердикт.** `Оставить`.

---

## `workspace/hooks/debug_stream_diag.py` — 70 LOC

**Назначение.** Отладочный хук, пишущий поток модели и reasoning в файл.
**Что делает.** `on_stream` (`:31`) пишет каждый дельта-чанк в `workspace/data_store/debug_stream.log`; `emit_reasoning` (`:37`) — reasoning-контент; `finalize_content` (`:66`) — итоговый ответ. Побочные эффекты: **запись полного контента модели и reasoning в plaintext-файл на диск, без флага `enabled`, без ротации, без ограничения размера**. Файл растёт монотонно; в нём потенциально персональные данные пользователя и внутренние рассуждения модели.
**Зачем нужен.** По замыслу — отладка стриминга. В текущей архитектуре не нужен: хуков-компонентов для наблюдаемости теперь достаточно (`lib/hooks/database_logging_hook.py`, `lib/hooks/tool_audit_hook.py`).
**Вердикт.** **`Удалить`.**
**Обоснование.** Файл **не подключён**: `lib/cli/hook_loader.py:63` строит `allowed = _allowed_hook_names()`, а `:68` делает hard-skip для всего, чего в allowlist нет, — «ни `spec_from_file_location`, ни `exec_module`, ни поиск `AgentHook`-подклассов не вызываются». В allowlist только `session_file_redirect_hook` и `recent_files_hook`. То есть находка по безопасности из брифа («нет флага `enabled`, пишет полный контент») **фактически нейтрализована барьером allowlist** — но сам код утечки лежит в дереве, а его докстринг (`:13`) продолжает утверждать: «Регистрируется через `workspace/hooks/` auto-scan», то есть **строка лжёт**. Достаточно одного `git rm`, чтобы проблема исчезла; оставлять «спящий» писатель секретов в репозитории — риск без выигрыша.
**Доказательства удаления.** Проверено: (1) allowlist `lib/cli/hook_loader.py::_allowed_hook_names` — 2 имени, `debug_stream_diag` среди них нет; (2) grep по `tests/`, `openspec/`, `docs/`, `config.json` — упоминаний нет; (3) `tests/test_hook_allowlist.py` не требует его наличия; (4) не точка входа, не шаблон, не `__all__`-экспорт. **Одна связь есть:** `lib/services/runtime_inventory.py::canonical_plugin_hooks()` перечисляет `StreamDiagnosisHook` как `required=False`, а `tests/test_runtime_inventory.py:81-86` требует наличия хука с «Diag» в имени. **Перед удалением** синхронизировать `runtime_inventory.py` (убрать спек) и поправить/удалить `test_runtime_inventory.py::test_plugin_hooks_have_optional_diag`. **Что сломается:** баннер `ApplicationContext` перестанет показывать `StreamDiagnosisHook` в списке плагинов; на работу не влияет, так как хук и так не регистрируется.

#### class `StreamDiagnosisHook` (16–70, 8 методов)

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 17–25 | вычислить путь лога | состояние | `scan_and_register` (**не вызывается**) | Удалить |
| `_write` | 27–29 | дописать в лог | побочный эффект | `on_stream`, `emit_reasoning`, … | Удалить |
| `on_stream` | 31–35 | записать дельта-чанк | отладка | фреймворк (**не зарегистрирован**) | Удалить |
| `emit_reasoning` | 37–41 | записать reasoning | отладка | фреймворк | Удалить |
| `emit_reasoning_end` | 43–44 | маркер конца reasoning | отладка | фреймворк | Удалить |
| `on_stream_end` | 46–47 | маркер конца стрима | отладка | фреймворк | Удалить |
| `after_iteration` | 49–64 | сводка за оборот | отладка | фреймворк | Удалить |
| `finalize_content` | 66–70 | записать итоговый контент | отладка | фреймворк | Удалить |

Дополнительно: `:13` импортирует `from nanobot.agent.hook import AgentHook`, тогда как рабочие хуки используют `from nanobot.agent import AgentHook` (`session_file_redirect_hook.py:48`). Если файл когда-нибудь вернут в allowlist, он может не импортироваться — ещё один аргумент удалить, а не чинить.

---

## `workspace/hooks/recent_files_hook.py` — 125 LOC

**Назначение.** Собирает пути файлов, созданных агентом за оборот, для автоприкладывания в `OutboundMessage.media`.
**Что делает.** `after_execute_tool` (`:84`) для файловых инструментов (`write`/`edit`/`create_file`/`write_file`) сохраняет `params["path"]` в бакет по `session_key`; `drain` (`:111`) отдаёт и обнуляет бакет. Побочные эффекты: рост в памяти, если `drain` не вызвать (в проде вызывается каждый оборот). SQL/файлы/сеть — нет.
**Зачем нужен.** Закрывает три реальных кейса (перечислены в `:8-16`): модель забыла приложить файл; приложила несуществующий; приложила абсолютный путь чужого workspace.
**Вердикт.** `Оставить`.
**Обоснование.** Живой: `required=True` в `canonical_plugin_hooks()`, в allowlist, и `drain` вызывается из `lib/services/runtime_patcher.py:508,822` внутри `patch_assemble_outbound`. Состояние изолировано по сессии (важно для конкурентных вопросов). Гард `tests/test_recent_files_hook.py` зелёный.
**Доказательства.** `runtime_patcher.py:474,486,508,745-750,821-822`; `agent_factory.py:31-35,118-119,170-173`; `application_context.py:376,387`.

#### class `RecentFilesHook` (51–125, 6 методов)

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 60–66 | создать бакеты | состояние | `scan_and_register` (единый контракт `cls(workspace_dir=…)`; параметр принимается для совместимости и не используется) | Оставить |
| `_bucket_key` | 68–72 | достать `session_key` | изоляция сессий | `after_execute_tool:106` | Оставить |
| `_extract_path` | 74–82 | достать путь из params | 4 синонима ключа | `after_execute_tool:103` | Оставить |
| `after_execute_tool` | 84–109 | записать финальный путь | сбор состояния | фреймворк | Оставить |
| `drain` | 111–120 | отдать и обнулить | потребление | `runtime_patcher.py:822` | Оставить |
| `collected` | 122–125 | снимок без обнуления | тесты/диагностика | `tests/test_recent_files_hook.py` | Оставить |

Классовые константы `_FILE_TOOLS` (`:41-43`) и `_PATH_KEYS` (`:46-48`) — Оставить. Замечание: `_PATH_KEYS` и `_FILE_TOOLS` **дублируют** одноимённые константы в `session_file_redirect_hook.py:54-55` — терпимо (модули независимы, хук загружается изолированно), но при правке одного нужно помнить о втором.

**Проверено и снято:** докстринги `:24` и `:94-96` утверждают требование «`SessionFileRedirectHook` зарегистрирован раньше `RecentFilesHook`». Фактически `lib/cli/hook_loader.py:65` итерирует `sorted(hooks_dir.iterdir())`, поэтому порядок алфавитный: `recent_files_hook` идёт **перед** `session_file_redirect_hook`, а `agent_factory.py:173` (`hooks = list(project_hooks) + hooks`) порядок не меняет. Однако это **не дефект**: `SessionFileRedirectHook.before_execute_tool` мутирует общий dict `params` (`:149-151`), а все `before_execute_tool` выполняются раньше всех `after_execute_tool`, поэтому `RecentFilesHook` действительно видит уже перенаправленный путь. Инвариант держится на разделении фаз, а не на порядке списка — это скрытый контракт, который стоит зафиксировать в докстринге явно, иначе следующий автор «чинил» бы несуществующий баг.

---

## `workspace/hooks/session_file_redirect_hook.py` — 416 LOC

**Назначение.** Перенаправляет записи агента в `data_store/cache/sessions/<session_key>/` и чинит `media` инструмента `message`.
**Что делает.** `before_execute_tool` (`:113`) для `write`/`edit`/`create_file`/`write_file` и для `media` у `message` переписывает пути: (1) мутирует `params` (`:149-151`), (2) дублирует в `tool_call.arguments` (`:156-157`) «на случай если инструмент читает оттуда». `_redirect_write` (`:129`) применяет whitelist; `_redirect_media` (`:168`) ищет реальный файл в папке сессии по относительному пути и по basename. Санитизация имени файла для Windows (`:83-98`). Побочные эффекты: запись файлов на диск, мутация аргументов tool-call.
**Зачем нужен.** Соблюдает политику «new files must be saved under data_store/cache/» и закрывает реальный баг: `MessageTool` резолвит относительные пути от корня workspace, а файлы агента лежат глубже в сессии — без хука `media.serialize` писал «Media file not found, keeping path» (`:15`).
**Вердикт.** `Оставить` (с обязательными правками докстринга и `config.json` из белого списка).
**Обоснование.** Живой: `required=True` в `canonical_plugin_hooks()`, в allowlist, регистрируется первым среди плагинов. `tests/test_session_file_redirect_hook.py` зелёный.
**Доказательства.** `lib/cli/hook_loader.py:63,68`; `agent_factory.py:170-173`; `application_context.py:376,387`; `workspace/utils/session_file_store.py` — **партнёр по контракту пути**, и именно там расходится (находка 2).

#### class `SessionFileRedirectHook` (101–416, 13 методов)

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 104–107 | вычислить `_sessions_root` | состояние | `scan_and_register` | Оставить |
| `before_execute_tool` | 113–127 | точка входа хука | перехват | фреймворк | Оставить |
| `_redirect_write` | 129–166 | whitelist + перенаправление пути | политика записи | `before_execute_tool` | Оставить |
| `_redirect_media` | 168–220 | починить `media` у `message` | работоспособность вложений | `before_execute_tool` | Оставить |
| `_media_entry_exists` | 222–238 | существует ли элемент «как увидит MessageTool» | не трогать валидное | `_redirect_media` | Оставить |
| `_resolve_media_entry` | 240–292 | найти файл в папке сессии (путь и basename) | мост между двумя layout'ами | `_redirect_media` | Оставить (именно здесь ломается из-за удвоения пути — находка 2) |
| `_tool_name` | 294–296 | имя инструмента | гейт | `before_execute_tool` | Оставить |
| `_extract_path` | 298–303 | достать путь | 4 синонима ключа | `_redirect_write` | Оставить |
| `_session_key` | 305–314 | ключ сессии из контекста | имя папки | `before_execute_tool` | Оставить |
| `_is_allowed` | 315–329 | попадает ли путь в whitelist | политика | `_redirect_write` | Оставить |
| `_normalize` | 331–349 | привести к workspace-relative | сравнение префиксов | `_is_allowed` | Оставить |
| `_redirect` | 351–387 | вычислить целевой путь в папке сессии | перенаправление | `_redirect_write` | Оставить |
| `_safe_leaf` (classmethod) | 389–416 | безопасное имя файла | Windows-санитизация | `_redirect` | Оставить |

**Расхождения докстринга и кода (правка обязательна):**

- `:28` обещает `**/*.py` в белом списке — в `_ALLOWED_FILES` (`:60-64`) и `_ALLOWED_PREFIXES` (`:66-81`) такого правила **нет**. Агент не может писать `.py` вне перечисленных префиксов, то есть докстринг обещает больше, чем код даёт.
- `:27-29` перечисляет 4 префикса и `workspace/data_store/**`; фактически в `_ALLOWED_PREFIXES` 13 префиксов, включая `data_store/` (без `workspace/`). Расхождение в обе стороны.
- `:3` указывает `workspace/hooks/scan_and_register` (hook_loader.py) — фактически `lib.cli.hook_loader.scan_and_register`.
- `:311` утверждает «CLI-процессы используют sister-функцию `resolve_session_key_for_subprocess`» — **единственный** её потребитель был `workspace/skills/legal_summarizer/scripts/cli.py`, который уехал на платформу. Строка лжёт (см. `session_key.py`).
- `AGENTS.md` (блок `session_file_store`) описывает путь как «единый `resolve_cache_path()` на платформе либо `~/.cache/nanobot/duckdb/`» — к этому хуку отношения не имеет; расхождение канона вне моего файла.

**Риск безопасности (существенный).** `_ALLOWED_FILES` (`:63`) включает `config.json` — боевой конфиг проекта. Он не перенаправляется, значит модель, которой хук не мешает, может его перезаписать: сменить `channels.postgres.*` (цель БД), `gateway.agent.enterprise_mcp` (профиль платформы), `logging.db.*` (имя таблицы журнала). `lib/services/config_service.py` резолвит `${VAR}`, так что секреты в файле нет — но маршрутизация и адресация меняются, а это P0-контур. Рекомендация: убрать `config.json` из белого списка либо ограничить его read-only-инструментами на уровне `SessionFileRedirectHook` (например, отдельным флагом «допустим только `read`, не `write`/`edit`»).

**Остаток:** `"project.json"` в `_ALLOWED_FILES` (`:63`) — **файла в репозитории нет** (`Test-Path project.json` → `False`; `AGENTS.md`: «Раньше источником объявлений был `project.json` — файла больше нет»). Мёртвая запись; удалить без последствий.

---

## `workspace/utils/__init__.py` — 0 LOC

**Назначение.** Пакет-маркер для `workspace.utils`.
**Что делает.** Ничего; файл пуст.
**Зачем нужен.** Импортируется как `workspace.utils.*` из тестов и из `session_file_redirect_hook.py:50`. Продовый код импортирует как `utils.*` (см. находку 12).
**Вердикт.** `Оставить**.

---

## `workspace/utils/db.py` — 1143 LOC

**Назначение.** Единственный пул PostgreSQL агента: пул воркеров, lease'ы, прокси курсоров, транзакции.
**Что делает.** Модуль поднимает N daemon-потоков (`_Worker:197`), держит очередь `_Job` (`:144`), выдаёт **эксклюзивные lease'ы** соединений (`_acquire_lease:579`) — то есть `transaction()`/`async_transaction()` получают соединение целиком. Каждое публичное действие (`execute`/`fetch`/`fetchone`/`fetchval`/`fetch_with_timeout`) ставит задачу в очередь и ждёт `_JobResult` с timeout'ом. `_sanitize_params` (`:696-719`) логирует/редактирует параметры для диагностики. `fetch_with_timeout` (`:1009`) дополнительно ставит `statement_timeout`. **Побочные эффекты:** фоновые потоки, сетевые соединения, `statement_timeout` в сессии, глобальный `_manager` (`:877`) под локом.
**Зачем нужен.** Единственная точка доступа агента к PostgreSQL: её используют `history_search_tool`, каналы, логирование, health-проверка. Свои пулы запрещены (`lib/services/session_cold_sync_service.py:69` — «никаких собственных psycopg2-пулов»).
**Вердикт.** `Оставить` (async-слой — удалить).
**Обоснование.** Модуль большой, но это инфраструктурное ядро; заменять его нечем, а пул с lease'ами — осознанное решение (иначе транзакция в одном потоке видела бы не committed данные другого). **SQL-инъекции нет:** grep по всем `SELECT`/`INSERT`/`UPDATE`/`DELETE` показывает полную параметризацию через `%s`; имена таблиц приходят из SETTINGS, а не из пользовательского ввода. **Асинхронный слой (`:854-874`, `:1110-1140`, 5 функций + 1 класс, ~62 LOC) — остаток переноса фазы 2** и не имеет в агенте ни одного потребителя.
**Доказательства.** `lib/core/application_context.py:1654-1658` (`set_pool_config`), `:827` (`fetch_with_timeout`); `lib/services/schema_validation.py:261,317`; `lib/services/db_logging_service.py`, `session_cold_sync_service.py:69`; `gateway.py:547-549` (`probe_connections`, `get_stats`); `lib/services/session_storage.py:188`; `lib/channels/postgres_channel.py:42-46`; `tests/test_utils_db.py`, `tests/test_db_logging_service.py:300,378`.

#### class `PoolTimeoutError` (188–195)
`RuntimeError`, бросается при исчерпании lease'а/таймауте. **Оставить** — контракт ошибки для `history_search_tool` и логирования.

#### class `_JobResult` (120–142, 4 метода) и `_Job` (144–161, 1 метод)
Внутренние примитивы очереди. `_JobResult` синхронизирован `threading.Event`; `get` (`:136`) ждёт с timeout и переводит `set_error` в исключение. **Оставить** (инфраструктура пула).

#### class `_Worker` (197–378, 10 методов)
Поток пула: держит своё соединение, переподключается с backoff (`:240`), открывает/переиспользует курсоры (`:286-306`), исполняет задачи (`:337`), печатает активность (`:321`). **Оставить** — ядро пула.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 198–218 | создать поток | пул | `_spawn_worker:459` | Оставить |
| `_ensure_connected` | 220–238 | живо ли соединение | устойчивость | `_execute_job:337` | Оставить |
| `_connect_with_backoff` | 240–272 | переподключение | устойчивость | `_ensure_connected` | Оставить |
| `_drop_connection` | 274–284 | сбросить соединение | recovery | `_connect_with_backoff` | Оставить |
| `_open_cursor` | 286–290 | открыть/переиспользовать курсор | ресурсы | `_cursor:292` | Оставить |
| `_cursor` | 292–296 | контекстный менеджер курсора | API | `_execute_job` | Оставить |
| `_close_cursor` | 298–306 | закрыть курсор | ресурсы | `_execute_job` | Оставить |
| `run` | 308–319 | цикл воркера | пул | `threading.Thread` | Оставить |
| `_activity_print` | 321–335 | диагностический вывод | отладка пула | `_execute_job` | Оставить |
| `_execute_job` | 337–378 | исполнить задачу | работа | `run:310` | Оставить |

#### class `DBManager` (380–694, 14 методов)
Владеет пулом, lease'ами, статистикой. **Оставить.**

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 381–421 | состояние менеджера | пул | `_get_manager:881` | Оставить |
| `start` | 423–441 | поднять воркеры | пул | `start:921`, `gateway.py` | Оставить |
| `shutdown` | 443–457 | остановить воркеры | lifecycle | `shutdown:926`, `ApplicationContext.stop` | Оставить |
| `_spawn_worker` | 459–479 | добавить воркер | пул | `start:423` | Оставить |
| `_maybe_shrink` | 481–491 | убрать простаивающий воркер | экономный пул | `_take_job:493` | Оставить |
| `_take_job` | 493–539 | взять задачу или lease | ядро | `_execute_job` | Оставить |
| `_requeue` | 541–544 | вернуть задачу | при lease-конфликте | `_take_job` | Оставить |
| `_submit` | 546–571 | поставить задачу | API | `execute`/`fetch`/… | Оставить |
| `_ensure_started` | 573–577 | ленивый старт | удобство | `_submit` | Оставить |
| `_acquire_lease` | 579–635 | эксклюзивная аренда соединения | изоляция транзакций | `transaction`, `fetch_with_timeout` | Оставить |
| `_begin_tx` | 637–639 | BEGIN | транзакция | `transaction:1086` | Оставить |
| `_end_tx` | 641–646 | COMMIT/ROLLBACK | транзакция | `transaction:1086` | Оставить |
| `_release_lease` | 648–661 | вернуть lease | пул | `transaction:1086` | Оставить |
| `get_stats` | 663–694 | метрики пула | health/диагностика | `get_stats:934`, `gateway.py:547` | Оставить |

#### class `_CursorProxy` (721–782, 15 методов) и `_ConnectionProxy` (784–852, 9 методов)
Прокси-обёртки, дающие воркеру «виртуальное» соединение: любой вызов ставится в очередь воркера, который уже держит реальный lease. Реализуют DBAPI-подмножество (`execute`/`fetchone`/`fetchall`/`fetchmany`/`mogrify`/`rowcount`/`statusmessage`/`__iter__`). **Оставить** — это и есть механизм lease.

#### class `_AsyncConnectionWrapper` (854–874, 5 методов) + 5 async-функций (1110–1140)
**`Удалить`.**

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 857–858 | обернуть прокси | — | `async_transaction:1127` | **`Удалить`** |
| `fetch` | 860–861 | async-обёртка | — | `async_fetch:1114` | **`Удалить`** |
| `fetchrow` | 863–864 | async-обёртка | — | — | **`Удалить`** |
| `execute` | 866–867 | async-обёртка | — | `async_execute:1110` | **`Удалить`** |
| `fetchval` | 869–870 | async-обёртка | — | `async_fetchval:1122` | **`Удалить`** |

**Доказательства удаления.** (1) grep по `lib/`, `workspace/`, `gateway.py`, `cli_agent.py` — единственные совпадения это определения в самом `db.py` (строки 1110, 1114, 1118, 1122, 1127) плюс упоминание в докстринге `:12`. (2) Все каналы агента синхронные и оборачивают блокирующий вызов в поток — потребности в async-обёртках нет. (3) Модуль-источник тестов **не этот**: `mcp-platform/tests/test_enterprise_data_db.py:27-31` импортирует `async_execute, async_fetch, async_fetchone, async_fetchval, async_transaction, set_pool_config` из платформенного `libs.enterprise_data.db` (проверено `:133-140` — патчится платформенный модуль). (4) Единственный мок — `tests/test_parallel_modes.py:77` (`db_mod.async_transaction.return_value = tx_cm`), он задаёт значение MagicMock'у и реальную функцию не выполняет. **Перед удалением** убрать упоминание в докстринге `db.py:12`. **Что сломается:** ничего в проде; в тестах — `tests/test_parallel_modes.py:77` (надо удалить или переписать на платформенный модуль).

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `set_pool_config` | 100–118 | применить конфиг пула | bootstrap | `application_context.py:1654-1658` | Оставить |
| `_caller_tag` | 163–186 | метка вызывающего для диагностики | логи | `_acquire_lease` | Оставить |
| `_sanitize_param` | 696–709 | редактирование параметра для лога | диагностика | `_sanitize_params:711` | Оставить |
| `_sanitize_params` | 711–719 | то же для структуры | диагностика | `_acquire_lease` | Оставить |
| `resolve_dsn` | 897–910 | `${DATABASE_URL}` → DSN | конфиг | `configure:912` | Оставить. **Замечание:** дублирует `tools/migrate.py` (dev-слой) — кросс-подсистемный кандидат на общий резолвер |
| `configure` | 912–919 | применить DSN | bootstrap | `application_context` | Оставить |
| `start` | 921–924 | поднять пул | lifecycle | `application_context` | Оставить |
| `shutdown` | 926–932 | остановить пул | lifecycle | `ApplicationContext.stop` | Оставить |
| `get_stats` | 934–936 | метрики пула | health | `gateway.py:547` | Оставить. **Замечание:** имя совпадает с `DbLoggingService.get_stats` — в разборе логов их путают |
| `probe_connections` | 938–968 | реальный пинг пула | health | `gateway.py:549` | Оставить |
| `run` | 970–983 | исполнить замыкание на lease | API | `db_logging_service._db_run` | Оставить |
| `execute` | 985–995 | DML/DDL, вернуть `statusmessage` | API | продовый код, `schema_validation` | Оставить |
| `fetch` | 997–1007 | вернуть список строк | API | `postgres_channel`, `session_storage` | Оставить |
| `fetch_with_timeout` | 1009–1057 | `SELECT` + `statement_timeout` | защита от висящих запросов | `application_context.py:827`, `schema_validation.py` | Оставить |
| `fetchone` | 1059–1070 | одна строка | API | продовый код | Оставить |
| `fetchval` | 1072–1084 | одно значение | API | `schema_validation` | Оставить |
| `transaction` | 1086–1108 | контекстный менеджер с lease | изоляция | продовый код | Оставить |
| `async_execute` | 1110–1112 | async-обёртка | — | **0 вызывающих** | **`Удалить`** |
| `async_fetch` | 1114–1116 | async-обёртка | — | **0 вызывающих** | **`Удалить`** |
| `async_fetchone` | 1118–1120 | async-обёртка | — | **0 вызывающих** | **`Удалить`** |
| `async_fetchval` | 1122–1124 | async-обёртка | — | **0 вызывающих** | **`Удалить`** |
| `async_transaction` | 1127–1140 | async-транзакция | — | **0 вызывающих** (только мок `tests/test_parallel_modes.py:77`) | **`Удалить`** |
| `_get_manager` | 881–895 | ленивая инициализация `_manager` | состояние | `configure`, `start`, все API-функции | Оставить |

**Вложенные функции:** `_work` внутри `execute:990`, `fetch:1002`, `fetch_with_timeout:1037`, `fetchone:1064`, `fetchval:1077` — замыкания, исполняемые воркером на lease. **Оставить** (уходят вместе с родителями).

---

## `workspace/utils/session_file_store.py` — 431 LOC

**Назначение.** Хранение вложений сессии на диске.
**Что делает.** `save_attachment` (`:188`) пишет файл в папку сессии, дедуплицирует по хэшу (`:273-289`), ведёт `metadata.json` (`:259-271`), угадывает расширение по MIME (`:185-186`). `deserialize`/`save` (`:291-361`) — сериализация произвольного объекта в `messages` внутри папки сессии. Побочные эффекты: **запись файлов и JSON на диск**, чтение существующих файлов. Сети нет.
**Зачем нужен.** Жив ради вложений: `workspace/utils/media.py` вызывает `save_attachment`/`deserialize`, а `lib/channels/postgres_channel.py:48` импортирует класс и `:198` использует инстанс. Остальное — от предрефакторингового слоя кэша сессий.
**Вердикт.** `Упростить` (удалить ~40% мёртвого кода) + обязательная правка контракта пути.
**Обоснование.** После снятия `patch_save_turn` (заменён на `nanobot/utils/helpers.py:580 maybe_persist_tool_result()`) и удаления локального кэша **единственный живой вход — `save_attachment`**. Всё остальное осиротело. Плюс **критический баг**: `__init__` (`:147-148`) безусловно дописывает `cache/sessions` к `base_dir`, а `lib/channels/postgres_channel.py:67-80` передаёт `data_store/cache/sessions` → срезает один уровень → `data_store/cache`; итог `data_store/cache/cache/sessions/<key>/`. Хук (`session_file_redirect_hook.py:107`) пишет в `data_store/cache/sessions/<key>/` — **каталоги не совпадают**.
**Доказательства.** Живое: `lib/channels/postgres_channel.py:48,198`; `workspace/utils/media.py` (`save_attachment`, `deserialize`). Мёртвое: `prepare_content`, `save`, `cleanup`, `archive_session` — grep по `lib/`, `workspace/`, `gateway.py`, `cli_agent.py` даёт только внутреннюю цепочку `save:352 → cleanup` и тесты. `tests/test_office_files.py` и `test_media.py` зелёные.

#### class `SessionFileStore` (127–431, 11 методов)

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 128–154 | вычислить `base = base_dir/"cache"/"sessions"` | корень хранения | `postgres_channel.py:198` | **Упростить** — источник удвоения пути (находка 2) |
| `_get_session_dir` | 156–162 | папка сессии | база | `save_attachment`, `save` | Оставить |
| `_resolve_attachments_dir` | 164–175 | папка вложений | база | `save_attachment` | Оставить |
| `_sanitize_filename` | 177–183 | безопасное имя | Windows-совместимость | `save_attachment` | Оставить |
| `_guess_ext_from_mime` | 185–186 | делегат на модульную функцию | — | `save_attachment:230,239` | `Упростить` — тривиальная обёртка; вызывать `guess_ext_from_mime` напрямую |
| `save_attachment` | 188–257 | записать вложение | **единственная живая функция** | `workspace/utils/media.py` | Оставить |
| `_ensure_metadata` | 259–271 | создать/прочитать `metadata.json` | дедуп и учёт | `save_attachment`, `_find_existing_for_hash` | Оставить |
| `_find_existing_for_hash` | 273–289 | найти дубль по хэшу | экономия места | `save_attachment` | Оставить |
| `save` | 291–361 | сериализация объекта в `messages` | — | **0 продуктовых вызывающих** (только `tests/`) | **`Удалить`** |
| `cleanup` | 363–418 | удалить старые сообщения | — | только `save:352` (тоже мёртв) | **`Удалить`** (транзитивно мёртв) |
| `archive_session` | 420–431 | архивировать папку сессии | — | **0 продуктовых вызывающих** (только `tests/`) | **`Удалить`** |

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `safe_session_key` | 27–30 | санитизация ключа сессии | имя папки | `session_file_store:158,331,357,425` | `Слить с workspace/utils/session_key.py` — **точная дублика** функции `session_key.py:29-37`; хук импортирует `session_key.safe_session_key` (`session_file_redirect_hook.py:50`), а этот модуль держит вторую копию. Две реализации одного правила разойдутся при правке |
| `guess_ext_from_mime` | 32–55 | MIME → расширение | имя файла | `_guess_ext_from_mime:186` | Оставить |
| `_csv_val` | 57–60 | экранирование значения CSV | `_try_convert_to_csv` | `_try_convert_to_csv` | `Удалить` вместе с `_try_convert_to_csv` |
| `prepare_content` | 62–81 | нормализация текста (CSV/JSON) | — | **0 вызывающих**, кроме `tests/` | **`Удалить`** |
| `_try_convert_to_csv` | 83–125 | привести структуру к CSV | — | только `prepare_content:62` | **`Удалить`** (вместе с `_csv_val`) |

---

## `workspace/utils/media.py` — 258 LOC

**Назначение.** Сериализация/десериализация вложений между путями, data-URL и storage-записями.
**Что делает.** `serialize` (`:88`) превращает список путей в mix `str`/`dict`; `deserialize` (`:133`) — обратно, **и именно он вызывает `SessionFileStore.save_attachment`**, то есть пишет файлы на диск; `data_url_info` (`:39`) разбирает `data:` URL; `read_for_ui` (`:222`) читает файл для Streamlit-UI (в UI-части этого отчёта не проверял — **не проверено**, есть ли потребитель). Побочные эффекты: запись файлов, декодирование base64 в память.
**Зачем нужен.** Канал должен уметь принять вложение от модели (путь) и отдать БД/UI (storage-entry), и наоборот.
**Вердикт.** `Оставить`.
**Обоснование.** Живой: `lib/channels/postgres_channel.py:40-46` (импорт и использование `serialize`/`deserialize`), `lib/services/session_storage.py:188`. `tests/test_media.py` зелёный.
**Доказательства.** `postgres_channel.py` (импорт `from utils.media import ...`), `media.py` внутри; `tests/test_media.py`.

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `data_url_info` | 39–57 | разобрать `data:` URL | входной формат | `entry_from_data_url` | Оставить |
| `_storage_entry` | 59–69 | собрать storage-dict | внутреннее | `entry_from_data_url`, `deserialize` | Оставить |
| `entry_from_data_url` | 71–86 | путь/data-URL → storage-entry | вход канала | `serialize:88` | Оставить |
| `serialize` | 88–131 | пути → outbound-представление | **публичный контракт канала** | `lib/channels/postgres_channel.py` | Оставить |
| `deserialize` | 133–180 | outbound → пути/entries, **пишет файлы** | приём от пользователя | `lib/channels/postgres_channel.py` | Оставить |
| `resolve_paths_and_hints` | 182–202 | пути + подписи | UI | каналы/UI | Оставить |
| `normalize_storage_entry` | 204–220 | привести entry к канону | совместимость записей | `deserialize` | Оставить |
| `read_for_ui` | 222–247 | прочитать файл для UI | отображение | **потребитель не проверен** | Оставить, но отметить: требует подтверждения вызывающим |

Классовая константа `_STORAGE_KEYS` (`:36`) — Оставить (whitelist полей storage-entry).

---

## `workspace/utils/session_key.py` — 119 LOC

**Назначение.** Единое правило получения и санитизации ключа сессии.
**Что делает.** Чистые функции: санитизация в безопасное имя папки, извлечение ключа из контекста, разбор ключа из пути, получение ключа для подпроцесса из `SESSION_KEY`/имени файла. Побочных эффектов нет, сети нет.
**Зачем нужен.** Имя папки сессии — это общий контракт между хуком редиректа, хранилищем вложений и любыми CLI-подпроцессами.
**Вердикт.** `Упростить` (удалить 2 из 4 функций; одну — слить).
**Обоснование.** После переезда `legal_summarizer` на платформу **единственный прежний потребитель `resolve_session_key_for_subprocess` — `workspace/skills/legal_summarizer/scripts/cli.py` — исчез**. Функция осталась, а докстринг `session_file_redirect_hook.py:311` продолжает писать, что «CLI-процессы используют sister-функцию». `extract_session_key_from_path` не имеет потребителей вовсе — только тесты.
**Доказательства.** Живое: `session_file_redirect_hook.py:50` (`resolve_session_key`, `safe_session_key`). Мёртвое: `resolve_session_key_for_subprocess` (0 вызовов; единственное упоминание вне себя — ложная ссылка в докстринге хука, `session_file_redirect_hook.py:311`), `extract_session_key_from_path` (только `tests/test_session_key.py`).

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `safe_session_key` | 29–37 | безопасное имя папки | имя каталога | `session_file_redirect_hook.py:50`; внутри модуля | `Слить с session_file_store.py:27-30` — точная дублика |
| `resolve_session_key` | 40–66 | ключ из `context` | имя каталога | `session_file_redirect_hook.py:50` | Оставить |
| `resolve_session_key_for_subprocess` | 68–107 | ключ для CLI-подпроцесса | — | **0 вызывающих** (потребитель уехал на платформу) | **`Удалить`** + поправить ложную ссылку `session_file_redirect_hook.py:311` |
| `extract_session_key_from_path` | 109–119 | разобрать ключ из пути к файлу | — | **0 вызывающих**, только `tests/test_session_key.py` | **`Удалить`** |

Классовая константа `__nosession__` (`:26`) — заглушка ключа для случая «сессии нет». Проверяется, что она используется функциями модуля; **Оставить**.

---

## `workspace/utils/clean_text.py` — 45 LOC

**Назначение.** Очистка строковых значений перед записью в БД/в логи.
**Что делает.** Чистая функция: обрезает пробелы, нормализует переводы строк, приводит «пустые» значения (`"null"`, `"none"`, `""`) к `None`/пустой строке. Побочных эффектов нет.
**Зачем нужен.** Не даёт мусорным значениям отравлять журнал и колонки.
**Вердикт.** `Оставить`.
**Обоснование.** Живой: `lib/session/pg_session_manager.py` (импорт для санитизации полей) и `workspace/utils/db.py:64` (`from utils.clean_text import clean_text`) — применимо к параметрам запросов. Модуль крошечный, дублирования нет.
**Доказательства.** `db.py:64`; `lib/session/pg_session_manager.py`; `tests/test_utils_db.py` (косвенно).

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `clean_text` | 26–44 | нормализация строки/«пустого» значения | гигиена данных | `workspace/utils/db.py:64`, `lib/session/pg_session_manager.py` | Оставить |

---

## `workspace/utils/jsonb.py` — 31 LOC

**Назначение.** Безопасное декодирование JSONB-значений из PostgreSQL.
**Что делает.** Чистая функция: `None → {}`, `str → json.loads` (пустая строка → `{}`), `dict → как есть`, иначе `dict(val)`.
**Зачем нужен.** Значения приходят и как `dict` (psycopg2 с `register_json`), и как `str` (старые записи до JSONB-колонки, другие драйверы). Одна точка декодирования вместо разбросанных `json.loads` по коду.
**Вердикт.** `Оставить`.
**Обоснование.** Живой: `lib/channels/postgres_channel.py:38` (`from utils.jsonb import decode_jsonb as _decode_jsonb`). **Обратить внимание аудитора каналов:** бриф показал 0 импортёров, потому что искал `workspace.utils.*`, а прод импортирует как `utils.*` (находка 12). Это ложный ноль, а не мёртвый код.
**Доказательства.** `postgres_channel.py:38`; `tests/test_utils_db.py`/тесты каналов.

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `decode_jsonb` | 16–31 | декодирование JSONB в `dict` | единая точка | `lib/channels/postgres_channel.py:38` | Оставить |

---

## `workspace/skills/__init__.py` — 0 LOC

**Назначение.** Пакет-маркер для `workspace.skills`.
**Что делает.** Ничего; файл пуст.
**Зачем нужен.** Импортируется `tests/test_skill_tool_independence.py::_all_py_files(REPO_ROOT / "workspace" / "skills")` для обхода дерева. Позволяет проверять, что скиллы не тянут tool'ы.
**Вердикт.** `Оставить**.

---

## `workspace/skills/audit_analyzer/SKILL.md` — 206 LOC

**Назначение.** Единственный оставшийся артефакт скилла `audit_analyzer` после переноса Python-части на платформу — документация для модели, как работать с capability `audit`.
**Что делает.** Ничего исполняемого: в `workspace/skills/audit_analyzer/` остались **только** `SKILL.md` — ни `scripts/`, ни `__init__.py`, ни `cli.py` (проверено обходом каталога). Документ не импортируется, а читается как prompt-инструкция.
**Зачем нужен.** Описывает модели 4 операции capability `audit` и их аргументы; без него модель не знает контракта, который реализует `AuditAnalyzerQueryTool`.
**Вердикт.** `Оставить` (с одной правкой).
**Обоснование.** **Историческая находка закрыта.** Документ корректно описывает 4 операции, совпадающие с `_ROUTED_OPERATIONS` в `workspace/tools/audit_analyzer_query.py:77` (`list_scripts`, `run_script`, `generate_sql`, `vector_search`); несуществующие скрипты и команда `audit_analyze` из текста убраны. Гард `tests/test_audit_analyzer_skill_doc.py` это фиксирует: он проверяет, что в документе есть строки всех четырёх операций и что подстроки `"audit_analyze "` в нём нет. Прошёл при прогоне.
**Доказательства.** `tests/test_audit_analyzer_skill_doc.py` (зелёный); соответствие операций — `audit_analyzer_query.py:77`; перечень таблиц/индексов в `:124-141` — сверяется с `mcp-platform/platform.json` (`audit.tables`, `vectors.indexes`), домен-маркеры допустимы, потому что это не tool-докстринг (гард `test_architecture_tool_domain_free.py` проверяет только `workspace/tools/*.py`).

**Остаточные неточности:**

- `:154-155` предписывает читать событие `cache_load_done` с полем `payload.loaded_at`. **Событие существует и попадает в `agent_gateway_logs`**, но публикует его **платформенный** загрузчик (`mcp-platform/libs/enterprise_data/loader.py`), а не агент (см. `docs/ARCHITECTURE.md:374`). Формулировка не ошибочна по сути, но создаёт у модели ложное впечатление, что скилл/агент на это влияет. Уточнить: «событие публикует загрузчик снимка платформы; агент только читает».
- Таблица операций и раздел «Источники данных» перечисляют доменные имена (`oarb.audits`, индексы). Это законно для SKILL.md, но при переезде скилла на платформу справочник продублирует `mcp-platform/platform.json` и со временем разойдётся. Стоит добавить в гард `test_audit_analyzer_skill_doc.py` проверку, что имена таблиц совпадают с `platform.json → audit.tables`, иначе расхождение будет обнаружено только в рантайме.

---

## Кросс-подсистемные находки

1. **Ложные нули из-за двойного пути импорта `workspace/utils/*`.** `lib/channels/postgres_channel.py:38,48` импортирует `utils.jsonb` и `utils.session_file_store`; `lib/core/application_context.py:1654` и `lib/services/session_storage.py:188` — тоже `utils.*`. Тесты при этом импортируют `workspace.utils.*`. Один файл = **два объекта в `sys.modules`** со своими копиями module-level состояния; для `db.py` это `_manager` и `_dsn` (`:877-878`), то есть в процессе, где встретились оба трека, возможны **два независимых пула соединений**. Рекомендация владельцу core: зафиксировать один трек (предпочтительно `workspace.utils.*`) и добавить гард-тест «все продовые импорты `workspace/utils/*` используют один префикс». **Это ложные нули в брифах 06/07 — при пересчёте инвентаря учесть.**

2. **`runtime_inventory` не соответствует фактическому состоянию (вне моей подсистемы).** `canonical_plugin_hooks()` перечисляет `StreamDiagnosisHook` (файл вне allowlist, т.е. не регистрируется), а `tests/test_runtime_inventory.py:81-86` требует его наличия — гард закрепляет мёртвый хук. `canonical_project_tools()` указывает `config_key="tools.compact_context.enable"` для `compact_context`, тогда как код читает `gateway.compact.enabled` (`compact_context.py:109-121`); для `legal_summarizer_query` указан `config_key=None` (по брифу), хотя ключ есть в `config.json:947-949`. Эти строки попадают прямо в startup-баннер и в вывод `tools/diagnose_startup.py`.

3. **`benchmarks/runner.py` не существует, но упомянут в `CHANGELOG.md:288` и `AGENTS.md`.** Проверено: путь отсутствует. Мёртвая ссылка в документации.

4. **Два лишних git-worktree с полной копией дорефакторингового дерева.** `git worktree list` показывает `.worktrees/fork-f328176d27cd-8591868d877a4aeba52b615ace806fff` (ветка `fork/f328176d…`, содержит `workspace/skills/legal_summarizer/scripts/cli.py`, `workspace/utils/office_files.py`, `lib/utils/sql_safety.py` — то есть всё, что удалено из основного дерева) и `AppData/Local/Temp/nb-v5` (detached `c62972b`). **Последствие для аудита:** любой grep по «репозиторию», выполненный рекурсивно от корня, даёт ложные срабатывания (именно так `resolve_session_key_for_subprocess` выглядела живой). Рекомендация: исключить `.worktrees/` из будущих прогонов инвентаризации или удалить устаревшие worktree.

5. **`lib/utils/sql_safety.py` — удалён чисто.** Проверено по всему репозиторию: живых ссылок в коде, тестах, `config.json`, `openspec/specs/` нет; все оставшиеся упоминания — исторические записи в `CHANGELOG.md`, `AGENTS.md:51` (помечен как удалённый, с корректным описанием переезда границы на платформу), `docs/DATABASE.md:503-525` и `openspec/changes/enterprise-mcp-platform/tasks.md` (отметки `[x]`). SQL-граница сейчас живёт в `mcp-platform/libs/audit/guard.py` + `libs/enterprise_data/snapshot/sql_guard.py` + `libs/enterprise_data/sql_safety.py`. Замечание к владельцу docs: `docs/architecture/nanobot-inventory.json:1065` всё ещё перечисляет `lib/utils/sql_safety.py` как существующий файл, а `docs/audit/README.md:104,123,131-133,247` и `reports/06-channels-utils.md` описывают его security-дефекты как актуальные — это относится к удалённому коду и дезориентирует читателя.

6. **`document_read` читает произвольные пути (`:283-299`).** Ограничение только по расширению (`:205-210`). См. разбор в секции файла; вопрос к владельцу `lib/hooks/` и канону политики путей: допустимо ли, что модель может прочитать любой `.txt`/`.csv`/`.pdf`/`.docx` на хосте.

7. **`config.json` в белом списке записи агента** (`session_file_redirect_hook.py:63`) — см. разбор в секции файла. Влияет на core (`config_service.py`) и на канон политики записи.

---

## Удалить — сводный список

| Файл | Символ | LOC | Почему | Что сделать перед удалением | Что сломается |
|---|---|---|---|---|---|
| `workspace/hooks/debug_stream_diag.py` | весь файл (класс `StreamDiagnosisHook`, 8 методов) | 70 | Не подключён: `lib/cli/hook_loader.py:63,68` — hard-skip по allowlist, имя отсутствует. Пишет полный контент модели и reasoning в plaintext-файл без `enabled` и без ротации. Докстринг `:13` лжёт о регистрации | Убрать спек `StreamDiagnosisHook` из `lib/services/runtime_inventory.py::canonical_plugin_hooks()`; удалить или переписать `tests/test_runtime_inventory.py::test_plugin_hooks_have_optional_diag` (`:81-86`); убрать упоминание из `docs/audit/*` как «находку» и перенести в «закрыто барьером allowlist» | Баннер `ApplicationContext` перестанет показывать `StreamDiagnosisHook`; на работу не влияет (хук и так не регистрируется) |
| `workspace/utils/session_key.py` | `resolve_session_key_for_subprocess` | ~40 (68–107) | Единственный потребитель — `workspace/skills/legal_summarizer/scripts/cli.py`, уехавший на платформу. В репозитории 0 вызовов; единственное упоминание вне функции — ложная ссылка в `session_file_redirect_hook.py:311` | Поправить докстринг `session_file_redirect_hook.py:311` (убрать «CLI-процессы используют sister-функцию»); удалить соответствующие тесты в `tests/test_session_key.py` | Ничего в проде. Тесты `tests/test_session_key.py` придётся почистить |
| `workspace/utils/session_key.py` | `extract_session_key_from_path` | ~11 (109–119) | 0 вызывающих во всём репозитории; покрыт только `tests/test_session_key.py` | Удалить тесты | Ничего в проде |
| `workspace/utils/session_file_store.py` | `prepare_content` | ~20 (62–81) | 0 продуктовых вызывающих; был нужен skill-side выводу, уехавшему на платформу | Удалить тесты (в `tests/test_office_files.py` и/или `tests/test_media.py` — **требует уточнения, какие именно**) | Ничего в проде |
| `workspace/utils/session_file_store.py` | `_try_convert_to_csv` + `_csv_val` | ~50 (57–60, 83–125) | Единственный вызывающий — мёртвый `prepare_content` | Удалить вместе с `prepare_content` | Ничего в проде |
| `workspace/utils/session_file_store.py` | `save` | ~71 (291–361) | 0 продуктовых вызывающих после снятия `patch_save_turn` (заменён на `nanobot/utils/helpers.py:580 maybe_persist_tool_result()`) | Убрать из тестов; убедиться, что `cleanup` удаляется следом | Ничего в проде |
| `workspace/utils/session_file_store.py` | `cleanup` | ~56 (363–418) | Достижим только из мёртвого `save:352` → транзитивно мёртв | Удалить вместе с `save` | Ничего в проде |
| `workspace/utils/session_file_store.py` | `archive_session` | ~12 (420–431) | 0 вызывающих; есть только в тестах как контракт | Убрать из `tests/` | Ничего в проде |
| `workspace/utils/db.py` | `async_execute`, `async_fetch`, `async_fetchone`, `async_fetchval`, `async_transaction`, `_AsyncConnectionWrapper` | ~62 (854–874, 1110–1140) | 0 вызовов в агенте; все каналы синхронные. Порт платформенного `libs/enterprise_data/db.py` — тесты async-API импортируют **платформенный** модуль, не этот | Убрать упоминание из докстринга `db.py:12`; удалить/переписать `tests/test_parallel_modes.py:77` (мок `db_mod.async_transaction`) | Ничего в проде. Тест `test_parallel_modes.py` требует правки |
| `lib/utils/outbound_meta.py` | `is_stream_delta` | 10 (48–57) | 0 вызывающих во всём репозитории; ключ `_stream_delta` не участвует в фильтрации (`OUTBOUND_DROPPED_KEYS` его не содержит) | Поправить `AGENTS.md:55`, где он числится среди «используется каналами»; убрать из `docs/audit/README.md:191` как актуальную находку | Ничего в проде |
| `workspace/tools/compact_context.py` | `_plugin_discoverable` | 1 (102) | Мёртвый атрибут: `project_tool_loader::_discover` флаг не читает; единственные упоминания — объявление и комментарий `:30-32`, который тоже неверен | Поправить докстринг `:24,30-32` (указать `lib/services/project_tool_loader.py` вместо `RuntimePatcher.patch_project_tools`) | Ничего — tool продолжит регистрироваться (он `required=True` в каноне) |
| `workspace/tools/document_read.py` | `_plugin_discoverable` | 1 (110) | То же | Ничего не нужно | Ничего |
| `lib/utils/text_utils.py` | — (не удаление, а **слияние**) `truncate_middle` поглощает корректную логику из двух копий `_cap` | 47 (файл) | Докстринг `:32` обещает жёсткий потолок, код его не держит; из-за этого в двух tool'ах живут идентичные копии обрезки (18 + 13 LOC) | Усилить `tests/test_text_utils.py:25` — заменить пустое `len(out) <= 200` на `len(out) <= 20`; удалить `_cap` из `audit_analyzer_query.py:53-71` и `legal_summarizer_query.py:44-56`; поправить ложную ссылку на тест в докстринге `audit_analyzer_query.py:56-63` | Поведение `history_search_tool` при `per_event_cap`/`max_result_chars` станет строже (соответствует заявленному контракту). Существующие тесты правки выдерживают — проверено чтением `tests/test_text_utils.py:14-37` |

**Итого к удалению: ~400 LOC** (из них ~330 — реально мёртвый код, ~70 — отладочный хук, никогда не включавшийся в текущей конфигурации).

---

## Счётчики

- Файлов разобрано: **26 / 26** · **НЕ РАЗОБРАНО: 0**
- Символов разобрано: **246 / 246** (22 класса, 154 метода, 65 функций модульного уровня, 5 вложенных) · **НЕ РАЗОБРАНО: 0**
- Вердикты по файлам: Оставить **17** · Упростить **3** · Удалить **3** · Слить **2** · НЕ РАЗОБРАНО **0**
- Вердикты по символам: Оставить **198** · Упростить **19** · Слить **3** · Удалить **21**
- Прогнано тестов: **440 passed, 16 skipped** (пропуски — предсуществующие, помечены `ISSUE-NB035-4` в `tests/test_history_search_tool.py`, к этой подсистеме не относятся)
