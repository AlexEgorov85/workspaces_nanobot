# Аудит: workspace-плагины и утилиты

## Сводка группы
Файлов: 18 · LOC: 4240 · классов: 20 · методов: 139 · функций модуля: 68

**Ключевые находки**

- `workspace/utils/session_file_store.py:147-148` + `lib/channels/postgres_channel.py:209-211` — **баг в проде**: `SessionFileStore.__init__` безусловно дописывает `cache/sessions` к `base_dir`, а `_resolve_sfs_base()` возвращает `media_cache_dir.parent` = `data_store/cache`. В сумме `self.base` = `workspace/data_store/cache/**cache**/sessions/`. Внедряемый store в проде не существует (`"_file_store"` встречается только в `tests/test_application_context.py:262`- vicinity и `tests/test_postgres_channel.py`), поэтому `project.json:74` `"media_cache_dir": "data_store/cache/sessions"` даёт **двойной `cache`**. Вложения, приходящие из Postgres-канала, ложатся в каталог, который не смотрит ни `SessionFileRedirectHook._media_entry_exists` (`workspace/hooks/session_file_redirect_hook.py:223-239`, fallback `workspace/data_store/cache`), ни `streamlit_app.py:130-134`. **Вердикт: Слить/Упростить** (см. файл) — это единственная подтверждённая потеря данных в группе.
- `workspace/hooks/debug_stream_diag.py` — **отладочный хук активен в проде**. Он не в blacklist: `lib/cli/hook_loader.py:129` перечисляет `debug_stream_diag` в allowlist, модуль импортируется без ошибки (`nanobot/agent/hook.py` существует в 0.3.5), `scan_and_register` вызывается из `lib/core/application_context.py:407-409`, у класса нет `enabled`-флага и единственный выключатель — удаление имени из `_allowed_hook_names()`. `after_iteration`/`finalize_content` (`:49-70`) вызываются на каждой итерации и пишут **полный plaintext контента и reasoning** в `workspace/data_store/debug_stream.log` (append на каждый вызов, без ротации и без лимита размера). При этом `wants_streaming()` у всех хуков проекта `False` (контрактный тест `tests/contract/test_agent_hook_api.py`), поэтому `on_stream`/`emit_reasoning*`/`on_stream_end` никогда не вызываются. `runtime_inventory.py` помечает хук как «REMOVED в post-0.3.5-hook-migration», `required=False` — то есть и сам файл, и инвентарь расходятся. **Вердикт: Удалить файл целиком.**
- `workspace/utils/structure_cache.py:7` — **ImportError**: `from workspace.utils.office_files import extract_structure` — функции в `office_files.py` нет (проверено AST: 19 функций, из них `extract_text`/`extract_tables`/`read_xlsx_sheet`/`summarize`/`detect_format`; `extract_structure` отсутствует, `legal_summarizer/scripts/document/physical.py:214` прямо пишет «Аналог ранее существовавшего `office_files.extract_structure`»). 0 импортёров, 0 вызовов, 0 тестов. **Вердикт: Удалить файл целиком.** Побочно: `tools/extract_office_structure.py:10` импортирует ту же несуществующую функцию и тоже нерабочий (вне моей подсистемы).
- `workspace/utils/session_file_store.py:27-29` против `workspace/utils/session_key.py:29-37` — **две несовместимые реализации `safe_session_key`**, при том что `AGENTS.md` и docstring обоих модулей объявляют конвенцию «согласованной с `SessionFileRedirectHook` и `SessionFileStore`». `session_key` санитайзит всё, кроме `[A-Za-z0-9._-]`, и отдаёт `__nosession__` на пустом результате; `session_file_store` заменяет только `[\\/:*?"<>|]` и **не имеет fallback** (пробелы, кириллица, не-ASCII сохраняются; `key="///"` → `"_"`, `key="  "` → `"  "`). Следствие: файлы, которые хук положил в `sessions/__nosession__/`, `SessionFileStore` ищет в `sessions/_/`. **Вердикт: Слить с `workspace/utils/session_key.py`.**
- `workspace/tools/legal_summarizer_query.py:265-283` — **блокирующий `subprocess.run` внутри `async def execute`**. На всё время вызова CLI (дефолт `timeout_sec = 60`, `config.json:724`) event loop рантайма/шлюза стоит. Нужен `asyncio.create_subprocess_exec`. **Вердикт: Оставить (с обязательным исправлением).**
- `workspace/tools/example.py` — `ExampleTool` **не попадает** в список инструментов модели, но не потому, что шаблон, а только благодаря `config.json:719-720` `"example": {"enable": false}`. Проверено: `enabled()` (`:99-101`) читает `settings.tools.example` → `config.py` отдаёт `AttrDict` (dict-подкласс), а не pydantic-`Settings`, поэтому `getattr(section, "example")` работает и возвращает `{"enable": False}` → `False`. Сканер `lib/services/project_tool_loader.py::_discover` не имеет blacklist, `config_key="example"` не исключён. Стоит ли инвентарь: `lib/services/runtime_inventory.py` содержит spec с `name="ExampleTool"`, тогда как зарегистрированное имя — `example_tool` (регистрируется `cls.name.lower()`), то есть при включении флага `diff_project_tools` выдаст `unexpected: [example_tool]` и баннер «CRITICAL» в стартовом логе. **Вердикт: Оставить** (осознанный шаблон), с обязательным уточнением инвентаря.
- `workspace/tools/history_search_tool.py:101-115` — enum `EVENT_TYPES` неполон относительно того, что реально пишет рантайм: отсутствуют `error` (`lib/services/db_logging_service.py:574`), `turn_failed`, а также `cache_load_*`/`sync_skipped_*`/`channel_*_error`/`vector_*` (те пишутся с `session_id="gateway:sync"`, т.е. вне пользовательской сессии — их отсутствие оправдано, а `error` — нет). `session_scope="all"` при этом работает корректно: `user_id` денормализуется в `agent_gateway_logs` через request-индекс в `_enqueue` (`lib/services/db_logging_service.py`), backfill — миграция V004.
- `workspace/tools/history_search_tool.py` — конфиг читается **правильно**: `_read_settings_section` (`:207-229`) идёт через `ctx._settings_ref.tools.<config_key>`, не через `ctx.config`. То же верно для `compact_context` (`:111` → `settings.gateway.compact`, историческая секция) и `legal_summarizer_query` (`:180`). **Ни один из четырёх tool'ов не читает настройки через `ctx.config`** — ловушка из `workspace/tools/__init__.py:14-19` не сработала нигде. Но `lib/services/runtime_inventory.py:136` указывает `config_key="tools.compact_context.enable"`, а фактические ключи — `gateway.compact.enabled`; строки в инвентаре врут.
- `workspace/hooks/session_file_redirect_hook.py:66-86` — whitelist чисто лексический, без проверки ФС, и смешивает два корня: `agent.workspace` = `~/.nanobot/workspace` (`config.json:4`), т.е. `workspace/`, а список содержит и repo-relative (`lib/`, `sql/`, `tools/`, `tests/`, `benchmarks/`, `.git/`, `.opencode/`), и workspace-prefixed (`workspace/hooks/`, `workspace/skills/`, `workspace/cron/`) префиксы. Практический эффект: запись в `lib/foo.py` разрешена (редиректа нет), но уходит в `workspace/lib/foo.py`; запись в `workspace/skills/x.md` разрешена и уходит в `workspace/workspace/skills/x.md`. Три записи `workspace/*` недостижимы по назначению.
- `workspace/hooks/session_file_redirect_hook.py:390-401` — `_safe_leaf` использует `Path(name).stem.partition(".")`, поэтому dotfile теряет имя: `.gitignore` → `""` → вызывающий подставляет `untitled.txt`.
- `workspace/utils/db.py` — собственный пул на psycopg2, **дублирования пула с `lib/services/db_logging_service.py` нет**: `DbLoggingService` ходит в БД только через `utils.db.run` (`lib/services/db_logging_service.py:804-807`). Дублируется только **имя** `get_stats` (счётчики журнала против метрик пула) и `resolve_dsn` (вторая копия — `tools/migrate.py`, dev-слой). Реальная проблема не в дублировании, а в **глобальном мутабельном состоянии**: `configure()` (`:882-888`) переписывает `_manager._dsn` процесса, и он зовётся из 9 мест (`pg_session_manager.py:75`, `session_cold_sync_service.py:139`, `cache_load_service.py:151,448`, `context_compaction.py:548`, `session_storage.py:100`, `db_logging_service.py:804`, `scripts/backfill_media_aw.py:37`, `streamlit_app.py:105`, `benchmarks/db.py:33`) — «агентских» и «сервисных» подключений в проекте **нет**, есть один пул на процесс.
- `workspace/utils/db.py:467-513` `_take_job` — `deque.remove()` внутри `for job in self._queue` выглядит как `RuntimeError: deque mutated during iteration`; **проверено экспериментально** (`python -c` на 3.14) — исключение возникает только при *продолжении* итерации, а здесь сразу `return`, поэтому код безопасен. Ложная тревога, вердикт `Оставить`.
- `workspace/utils/jsonb.py` — обе функции **живые**, а не мёртвые: `lib/channels/postgres_channel.py:47` и `streamlit_app.py:107-108` импортируют `utils.jsonb`, а `dead_symbols.md` ищет по префиксу `workspace.utils` — это ложное срабатывание. Тот же дефект статики касается **всех** `workspace/utils/*`: продуктовый импорт идёт через алиас `utils.*`, потому что `gateway.py:510-511` и `cli_agent.py:282-283` кладут `<repo>/workspace` в `sys.path[0]`.
- `workspace/utils/office_files.py` — бриф показывает «тесты: —», но покрытие есть: `tests/test_office_files.py` (73-194) тестирует `detect_format`/`extract_tables`/`summarize`/`read_xlsx_sheet`. `summarize` и `read_xlsx_sheet` — публичный контракт скилла `workspace/skills/office_files/SKILL.md:46-150`. Все 19 функций живые, вердикт `Оставить` целиком.
- `workspace/utils/office_files.py` — `workspace/skills/legal_summarizer/SKILL.md:3,11,14,309-310` в четырёх местах предписывает агенту **не вызывать** `office_files.extract_metadata()`; такой функции в модуле нет (есть `summarize`). Строки лгут; `tests/test_skill_legal_summarizer.py:240` проверяет наличие подстроки в тексте и потому проходит.

**Вердикты:** Оставить 221 · Упростить 19 · Удалить 13 · Слить 1 · Перенести 0

Счётчики по символам (классы, методы, функции модуля, константы уровня модуля и `ClassVar`; всего 254 позиции — агрегат приведённой ниже таблицы «Сводка вердиктов по файлам»): Оставить 221 · Упростить 19 · Удалить 13 · Слить 1 · НЕ РАЗОБРАНО 0.

---

## `workspace/tools/compact_context.py` — 177 LOC

**Назначение.** Tool-обёртка над `ContextCompactionService`: ручное сжатие контекста сессии по команде модели.

**Что делает.** `CompactContextTool.create` достаёт `ctx.compaction_service` и кэширует его в `self._service` (`create`, `:124-141`). `execute` (`:160-177`) вызывает `await self._service.compact(session_key=..., idle=idle, force=force)`; локальный ключ сессии берётся из `context.session_key` и санитайнится **вручную** (`:162-165`, inline-регулярка `_re.sub`), а не через `workspace.utils.session_key` — единственное место в группе, где это продублировано. Схема объявлена с `force: bool = True` (`:168-172`), то есть ручной путь жёсткий, как и требует `AGENTS.md`; `idle` оставлен legacy-алиасом. Результат `res.get("ok")` мапится в `ToolResult.error`, остальное — в текст.

**Зачем нужен.** Дать модели явный способ сжать контекст в текущей сессии (пятый вход в `_notify` после AGENTS.md, § «Управление сжатием контекста»). Без него ручное сжатие доступно только через CLI `/compact`.

**Вердикт.** `Оставить`
**Обоснование.** Tool зарегистрирован (`lib/services/project_tool_loader.py::_discover`), канонизирован в `lib/services/runtime_inventory.py:131-137`, покрыт `tests/`. Настройки читаются корректно (через `ctx._settings_ref.gateway.compact`). Требуются только косметические правки — см. ниже.

**Доказательства.** Импортёров нет (сканирование каталога). Потребители: `ApplicationContext.create` → `project_tool_loader`; конфиг `project.json:383-386` (`gateway.compact.enabled=true`); тесты `tests/test_runtime_inventory.py`, `tests/contract/test_compaction_api.py`.

#### class `CompactToolConfig` (строки 43–54, 4 поля)
Классический pydantic-конфиг tool'а; читается в `create` (`:129-140`) через `cls.config_cls()(**section)`. Поля зеркалят `gateway.compact` из `project.json:383-386`.
Вердикт: `Оставить`.

| Атрибут | Строки | Назначение | Зачем нужен | Кто читает | Вердикт |
|---|---|---|---|---|---|
| `enabled` | 51 | гейт регистрации/работы | выключает tool и сам сервис | `CompactContextTool.enabled` `:115`; дублируется `getattr(service,"enabled",True)` `:167` | Упростить |
| `notify_in_history` | 52 | писать заметку в `agent_conversation_messages` | видимость в UI | `create` `:135` | Оставить |
| `print_to_terminal` | 53 | дубль в терминал | дебаг | `create` `:136` | Оставить |
| `keep_recent_messages` | 54 | хвост последних сообщений | контекст при сжатии | `create` `:137` | Оставить |

`enabled` в конфиге и `service.enabled` — одна и та же настройка, прочитанная дважды; вторая проверка (`getattr(self._service, "enabled", True)`) избыточна, потому что `ContextCompactionService.compact` сам начинается с `if not self.enabled` (`lib/services/context_compaction.py:114-115`).

#### class `CompactContextTool` (строки 91–177, 7 методов)
Единственный tool этого файла; `name = "compact_context"` (`:97`), `config_key = "compact_context"` (`:101`).

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `config_cls` | 105-106 | `@classmethod` → `CompactToolConfig` | контракт загрузчика | `project_tool_loader._build_tool_context` | Оставить |
| `enabled` | 109-121 | `settings.gateway.compact` → bool | гейт регистрации | `project_tool_loader._discover` | Оставить |
| `create` | 124-141 | достаёт сервис из ctx, строит конфиг | DI-конструктор | `project_tool_loader` | Оставить |
| `__init__` | 143-144 | хранит `_service`/`_config` | состояние | `create` | Оставить |
| `name` | 147-148 | `"compact_context"` | имя в списке tool'ов | загрузчик | Оставить |
| `description` | 151-158 | текст для модели | выбор tool'а | LLM-промпт | Упростить |
| `execute` | 160-177 | вызов сервиса, маппинг результата | действие | LLM | Оставить |

- `description` (`:151-158`) перечисляет только `idle` и **не упоминает `force`**, хотя `parameters` (`:160-177`) объявляет `force` с дефолтом `true` и пояснением «игнорировать порог токенов». Модель читает `description`, а не схему → из документации не видно, что `force=false` — единственный нежёсткий путь. Правка: перечислить `force` первым, `idle` назвать legacy-алиасом.
- `execute` (`:162-165`) дублирует санитизацию ключа сессии inline-регуляркой. Правильный вызов — `resolve_session_key(context)` из `workspace/utils/session_key.py:40`, тот же, что использует `SessionFileRedirectHook._session_key` (`:307-314`). Иначе расходится с остальной системой именования каталогов.
- `_plugin_discoverable: ClassVar[bool] = False` (`:102`) — **мёртвый атрибут**. Единственный его «пользователь» — комментарий в docstring (`:30-32`): «auto-loader nanobot пропускает». Проверено: `lib/services/project_tool_loader.py::_discover` не читает этот флаг (только `issubclass`, `cls is not _T`, `__abstractmethods__`), и grep по всему репозиторию на `_plugin_discoverable` даёт ровно два попадания — объявление и этот комментарий. Удаление атрибута ничего не сломает; tool продолжит регистрироваться (что и нужно — он в `canonical_project_tools`).

---

## `workspace/tools/history_search_tool.py` — 602 LOC

**Назначение.** Generic-поиск по долговечному журналу `agent_gateway_logs` — дать модели возможность вспомнить, что происходило в прошлых/текущих сессиях, в том числе факты сжатия контекста, недоступные в промпте.

**Что делает.** `execute` (`:293-541`) строит параметризованный `SELECT` из whitelist-колонок `agent_gateway_logs` (таблица/схема резолвятся из `SETTINGS.logging.db`, `_log_table` `:591-602`; `schema` в конфиге действительно есть — `project.json:513`), фильтрует по `event_type`/`session_id`/`tool_name`/`user_id`/временному окну/`ILIKE` по `payload::text`, затем три уровня усечения: лимит строк (`max_rows`), обрезка одного жирного payload до `per_event_cap` символов и, наконец, общий бюджет ответа `max_result_chars`. Флаг `results_truncated` + `next_offset` возвращаются в JSON-подвале. Доступ к БД — `from utils.db import fetch` внутри `execute` (`:394`), то есть через общий пул.

**Зачем нужен.** После сжатия контекста агент теряет историю; `AGENTS.md` явно ссылается на этот tool как на единственный способ «вспомнить» факт `context_compacted`. Без него события журнала недоступны модели вообще.

**Вердикт.** `Оставить`
**Обоснование.** Канонизирован (`runtime_inventory.py:138-144`), конфигурируется через `config.json` (в `project.json` секции `tools.history_search` нет → все параметры берутся из дефолтов модели: `max_rows=50`, `max_result_chars=12000`), покрыт `tests/test_history_search_tool.py`. Найденные дефекты — в покрытии enum и в пагинации, а не в жизнеспособности.

**Доказательства.** Импортёров нет (сканирование). Потребители: `project_tool_loader`; ссылка как на рабочий механизм — `AGENTS.md` § `ContextCompactionService`; тесты `tests/test_history_search_tool.py`.

#### class `HistorySearchToolConfig` (строки 78–83, 3 поля)
Секция `tools.history_search.*`. В `project.json`/`config.json` такой секции нет, поэтому в рантайме всегда работают дефолты — сам факт не баг, но означает, что поднять `max_result_chars` без правки `config.json` нельзя.

| Атрибут | Строки | Назначение | Зачем нужен | Кто читает | Вердикт |
|---|---|---|---|---|---|
| `enable` | 81 | гейт | выключает tool | `enabled` `:233` | Оставить |
| `max_rows` | 82 | потолок строк на вызов | защита от больших выборок | `execute` `:415` | Оставить |
| `max_result_chars` | 83 | бюджет ответа | защита контекста модели | `execute` `:474` | Оставить |

#### class `HistorySearchTool` (строки 194–547, 9 методов)

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `config_cls` | 203-204 | `@classmethod` → `HistorySearchToolConfig` | контракт загрузчика | `project_tool_loader` | Оставить |
| `_read_settings_section` | 207-229 | чтение `ctx._settings_ref.tools.history_search` | конфиг без `ctx.config` | `enabled`/`create` | Упростить |
| `enabled` | 232-234 | `enable` из секции | гейт | `_discover` | Оставить |
| `create` | 237-243 | `cls.config_cls()(**section)` | DI | `project_tool_loader` | Оставить |
| `__init__` | 199-200 | `_config` | состояние | `create` | Оставить |
| `name` | 246-247 | `"history_search"` | имя в списке | загрузчик | Оставить |
| `description` | 250-291 | 42 строки контракта для модели (фильтры, пагинация, формат ответа) | модель должна знать про `next_offset`/`event_type` | LLM-промпт | Оставить |
| `execute` | 293-541 | SQL + три уровня усечения + JSON-подвал | действие | LLM | Оставить |
| `_error` | 543-547 | единый текст ошибки | непротиворечивость | `execute` `:307,332` | Оставить |

- `_read_settings_section` (`:207-229`) корректно идёт через `ctx._settings_ref` и корректно обрабатывает `AttrDict`/`dict`/`None`; но это **третья копия** одного и того же хелпера в группе (`example.py:68-96` — 29 строк, `legal_summarizer_query.py:176-204` — 29 строк, здесь 23). Кандидат на вынос в общий миксин; выигрыш — единая точка, где придётся чинить `ctx.config`, если платформа поменяет тип `_settings_ref`.
- `EVENT_TYPES` (`:101-115`) — docstring обещает «реально пишущиеся в журнал», но отсутствуют как минимум `error` (`lib/services/db_logging_service.py:574`) и `turn_failed`. Для `error`-событий (самые diagnostчески ценные) модель не может построить фильтр; единственный выход — `event_type=null`. Правка: добавить `error`, `turn_failed` и явно оговорить, что `gateway:*`-события исключены намеренно (у них `session_id="gateway:sync"`).
- Пагинация (`:520-524`): `next_offset = original_offset + len(events)`, где `events` уже усечён. При `results_truncated=true` следующая страница начинается с позиции, занятой уже показанными событиями → модель получает **дубликаты**, а не пропуски. `description` (`:250-291`) честно предупреждает «NOT offset+limit when results_truncated=true», но не говорит, что страницы перекрываются. Правка: считать `next_offset` от числа реально прочитанных строк, а не от числа возвращённых, и дописать в `description`, что страницы перекрываются.
- `effective_limit = min(int(limit or self.config.max_rows), self.config.max_rows)` (`:415`) — нет нижней границы: `limit=-5` даёт `LIMIT -5`, PostgreSQL вернёт ошибку, модель получит `_error`. Дешёвая правка `max(1, ...)`.
- `per_event_cap = 4000` (`:437`) — зашитый магнит, единственный параметр усечения, не вынесенный в `HistorySearchToolConfig`, при том что два других вынесены.
- Двойное декорирование `payload_truncated` — в `_render` (`:387-392`, для учёта размера) и в финальном ответе (`:536`). Это не дублирование: первое нужно для расчёта бюджета, docstring `:381-386` это объясняет. Оставляю как есть.

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_current_session_key` | 550-556 | достать `session_key` из `context`/`ctx` | скоупинг поиска по сессии | `execute` `:302` | Оставить |
| `_current_user_id` | 559-588 | резолв `user_id` (session_key → sessions → ctx) | реализует `session_scope="all"` | `execute` `:311-330` | Оставить |
| `_log_table` | 591-602 | `schema`/`table` из `SETTINGS.logging.db` | не хардкодить имя таблицы | `execute` `:340` | Оставить |

`_current_session_key` и `_current_user_id` помечены в `duplicates.md:87-88,113-114` как дубли `ContextCompactionService._current_session_key` / `_current_request_sender_id`. Совпадение по форме реальное, но обе реализации в этом tool'е читают **другой источник** (`AgentHookContext` + `ctx`), и `ContextCompactionService` — приватный метод сервиса. Объединение возможно только через вынос в `workspace/utils/session_key.py`; цена рефакторинга выше выигрыша. Вердикт `Оставить`, отметить в `duplicates.md` как ложное срабатывание по существу.

---

## `workspace/tools/legal_summarizer_query.py` — 360 LOC

**Назначение.** Мост между моделью и skill'ом `legal_summarizer`: запуск `python workspace/skills/legal_summarizer/scripts/cli.py` в subprocess и возврат stdout агенту.

**Что делает.** `execute` (`:236-315`) собирает argv (`--mode cli_query --operation-id … --field …` + либо `--file`, либо `--question`), ищет `cli.py` в `workspace_root` (по умолчанию — два уровня вверх от файла tool'а, `_resolve_workspace_root` `:104-114`), запускает `subprocess.run` с таймаутом, перехватывает stdout/stderr и разбирает exit code. Поля `operation_id`/`field` ограничены enum'ами в `parameters`, но **внутри tool'а не валидируются** — дальше они попадают в argv (без shell, поэтому инъекции нет) и в пути, которые строит уже skill.

**Зачем нужен.** `AGENTS.md` фиксирует: skill-доступ к `audit_analyzer` идёт только через CLI, Agent-tools для `legal_summarizer` возвращены не были — этот tool заменяет их одним контролируемым subprocess-вызовом.

**Вердикт.** `Оставить`
**Обоснование.** Зарегистрирован и канонизирован (`runtime_inventory.py:145-151`), явно включён (`config.json:723-725` `enable: true, timeout_sec: 60`), тесты есть. Требует одного функционального исправления (блокирующий subprocess) и косметики в docstring.

**Доказательства.** Импортёров нет (сканирование). Потребители: `project_tool_loader`; `AGENTS.md` § `workspace/tools/`; `config.json:723-725`; `runtime_inventory.py:145`.

#### class `LegalSummarizerQueryToolConfig` (строки 96–101, 3 поля)

| Атрибут | Строки | Назначение | Зачем нужен | Кто читает | Вердикт |
|---|---|---|---|---|---|
| `enable` | 99 | гейт | выключает tool | `enabled` `:208` | Оставить |
| `timeout_sec` | 100 | таймаут subprocess | не висеть вечно | `execute` `:288` | Оставить |
| `workspace_root` | 101 | корень репо | найти `cli.py` | `create` `:216` | Оставить |

Ключи совпадают с `config.json:723-725` (в отличие от `example.py`, где конфиг camelCase'ом и не читается — см. ниже).

#### class `LegalSummarizerQueryTool` (строки 163–357, 9 методов)

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `config_cls` | 172-173 | `@classmethod` | контракт | загрузчик | Оставить |
| `_read_settings_section` | 176-204 | чтение `ctx._settings_ref.tools.legal_summarizer_query` | конфиг без `ctx.config` | `enabled`/`create` | Упростить |
| `enabled` | 207-209 | `enable` | гейт | `_discover` | Оставить |
| `create` | 212-218 | конфиг + `workspace_root` | DI | загрузчик | Оставить |
| `__init__` | 168-169 | `_config` | состояние | `create` | Оставить |
| `name` | 221-222 | `"legal_summarizer_query"` | имя | загрузчик | Оставить |
| `description` | 225-234 | 10 строк для модели | выбор tool'а | LLM | Оставить |
| `execute` | 236-315 | сборка argv, запуск, разбор вывода | действие | LLM | Оставить |
| `_handle_nonzero_exit` | 317-349 | разбор stderr по exit code (в т.ч. confirmation_required) | не терять семантику ошибок | `execute` `:297-306` | Оставить |
| `_error` | 351-357 | единый текст | — | `execute` | Оставить |

- **`execute` (`:265-283`) — функциональный баг:** `subprocess.run(...)` вызывается синхронно внутри `async def`. На время работы CLI (дефолт 60 с) event loop шлюза/CLI-агента заблокирован: не отдаются heartbeat'ы, не обслуживаются другие сессии воркеров. Замена на `asyncio.create_subprocess_exec` + `asyncio.wait_for` сохраняет весь остальной контракт.
- `env = os.environ.copy()` (`:284`) копируется впустую: `subprocess.run` при `env=None` наследует окружение сам, а `env` нигде не мутируется. Убрать.
- `operation_id` и `field` (`:243-268`) не валидируются на соответствие enum'ам из `parameters` — защита только декларативная. Для `operation_id` это значит, что в skill уходит произвольная строка, которая там участвует в построении пути кэша (`workspace/skills/legal_summarizer/scripts/cache/manifest.py`, вне моей подсистемы). Минимальная правка: сравнение с множеством допустимых значений перед запуском.
- Docstring модуля (`:26-27`) утверждает, что путь строится «через `parents[3]` от самого файла tool'а», а код `_resolve_workspace_root` (`:104-114`) использует `parents[2]` и дописывает `workspace/skills/legal_summarizer/scripts` явно. Комментарий на `:113` верный, модульный docstring — нет. Строка лжёт.

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_resolve_workspace_root` | 104-114 | корень репо из `__file__` | опция `workspace_root` в конфиге | `create` `:215-216` | Упростить |
| `_resolve_cli_path` | 117-126 | путь к `cli.py` с проверкой существования | понятная ошибка вместо `FileNotFoundError` | `create` `:217` | Оставить |

`_resolve_workspace_root` (`:104-114`) — вердикт `Упростить`: функция нужна, но её docstring (`:108-109`) противоречит телу; приводить к одному виду, а не удалять.

---

## `workspace/tools/example.py` — 131 LOC

**Назначение.** Эталонный шаблон кастомного tool'а: минимальный, но полный набор `config_key`/`config_cls`/`enabled`/`create`/`name`/`description`/`parameters`/`execute`, на который ссылаются `AGENTS.md` и `workspace/tools/__init__.py:7,23`.

**Что делает.** Ничего в рантайме: `execute` возвращает `f"echo: {text}"`. Единственное реальное поведение — `enabled()` (`:99-101`), который читает `ctx._settings_ref.tools.example` и возвращает `False` при `config.json:719-720`.

**Зачем нужен.** Референс-контракт для новых tool'ов + живой пример того, как **не** надо читать настройки. Удаление сломает документацию (`AGENTS.md`, `workspace/tools/__init__.py`) и обучение новых авторов.

**Вердикт.** `Оставить` (осознанный шаблон, как предписывает § 4 протокола)
**Обоснование.** Шаблон не мёртв и не должен удаляться, но в текущем виде он содержит две ошибки, которые копируют читатели, и один риск, который не ограждён кодом — только одним ключом в конфиге.

**Доказательства.** `config.json:719-722` `"example": {"enable": false, "maxChars": 8000}`. Спец в `lib/services/runtime_inventory.py` (`required=False`, «шаблон с правильным паттерном (disabled by config — служебный)»). Не импортируется ничем. `dead_symbols.md` числит `ExampleTool` с 0 ссылок — ложное срабатывание, объясняется сканированием.

#### class `ExampleToolConfig` (строки 41–45, 2 поля)

| Атрибут | Строки | Назначение | Зачем нужен | Кто читает | Вердикт |
|---|---|---|---|---|---|
| `enable` | 44 | гейт | выключает шаблон | `enabled` `:100` | Оставить |
| `max_chars` | 45 | лимит длины ответа | демонстрация лимита | `create` `:107` | Оставить |

`max_chars` **не читается из конфига**: в `config.json:721` ключ называется `maxChars`, а pydantic-модель ждёт `max_chars` и по умолчанию игнорирует лишние поля → в рантайме всегда 8000 из дефолта. Правка: переименовать ключ в конфиге (или принять alias в модели), иначе шаблон учит неверному.

#### class `ExampleTool` (строки 58–131, 8 методов)

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `config_cls` | 64-65 | `@classmethod` | контракт | загрузчик | Оставить |
| `_read_settings_section` | 68-96 | чтение секции с фолбэком `config.<key>` | демонстрация правильного пути | `enabled`/`create` | Упростить |
| `enabled` | 99-101 | `enable` | гейт | `_discover` | Оставить |
| `create` | 104-110 | конфиг | DI | загрузчик | Оставить |
| `__init__` | 112-113 | `_config` | состояние | `create` | Оставить |
| `name` | 116-117 | `"example_tool"` | имя | загрузчик | Оставить |
| `description` | 120-124 | описание для модели | демонстрация | — | Оставить |
| `execute` | 126-131 | `echo: <text>` | демонстрация `parameters`/`execute` | — (tool выключен) | Оставить |

**Точный ответ на вопрос брифа о «мусоре в списке инструментов»:** сейчас мусора нет. `name` = `"example_tool"` (`:117`), `enabled()` = `False` → `project_tool_loader._discover` кладёт его в `skipped_disabled`, реестр инструментов модели не трогает. Но защита **одноключевая**: `lib/services/project_tool_loader.py::_discover` не имеет blacklist, а `lib/cli/hook_loader.py`-подобного исключения для tool'ов нет; достаточно дописать `"enable": true` — и `example_tool` окажется в промпте модели, плюс `runtime_inventory` выдаст `unexpected: [example_tool]` (имя в инвентаре — `"ExampleTool"`, а регистрируется `cls.name.lower()`), и стартовый баннер пометит запуск как CRITICAL. Рекомендация: (а) привести имя в `runtime_inventory` к фактическому `example_tool`; (б) добавить в `project_tool_loader._discover` явный skip для файла `example.py` (механизм уже есть в `_allowed_hook_names` для хуков) — тогда «шаблон» перестанет зависеть от ключа в конфиге.

- `_read_settings_section` (`:68-96`) содержит фолбэк `getattr(ctx.config, cls.config_key, {})` (`:78-82`) — ровно тот путь, от которого предостерегает `workspace/tools/__init__.py:14-19` (на pydantic-`ToolsConfig` он даёт `AttributeError`, пойманный широким `except`). Для шаблона это вредно: читатель скопирует «безопасный» фолбэк, который никогда не сработает. Правка: убрать фолбэк, оставив только `_settings_ref`, и явно прокомментировать почему.
- Docstring класса (`:20-25`) утверждает: «`ctx._settings_ref` — полный pydantic-объект `Settings`, который кладёт туда `RuntimePatcher.patch_project_tools`». Проверено по `config.py`: `SETTINGS` — это `AttrDict` (dict-подкласс), а инжектор — `lib/services/project_tool_loader.py::_build_tool_context`, а не `RuntimePatcher`. Код выживает оба варианта, но описание вводит в заблуждение.

---

## `workspace/hooks/session_file_redirect_hook.py` — 417 LOC

**Назначение.** Файловая политика: перенаправление результатов `write_file`/`edit_file` и входящих медиа в `workspace/data_store/cache/sessions/<safe_session_key>/`, чтобы агент не мусорил в репозитории, но сохранял allowlist-пути для редактирования существующих файлов проекта.

**Что делает.** `before_execute_tool` (`:114-128`) по имени tool'а из `_FILE_TOOLS`/`_MEDIA_TOOLS` и наличию пути в `_PATH_KEYS` либо: переписывает аргумент `path` (`:130-167`) на путь внутри папки сессии, с разрешением коллизий до 999 вариантов, либо нормализует media-запись (`:169-288`) — если файл уже лежит в ожидаемом месте, запись не меняется, иначе запись пересобирается в сторовую ссылку. Allowlist чисто лексический (`:316-330`).

**Зачем нужен.** Реализует «File Storage Policy» из `AGENTS.md`: без него результаты tool'ов сыпались бы в корень репозитория. Согласован с `workspace/utils/session_key.py` через `resolve_session_key` (`:307-314`).

**Вердикт.** `Оставить`
**Обоснование.** Регистрируется через `lib/cli/hook_loader.py::scan_and_register` (вызов из `application_context.py:407-409`), включён в `canonical_plugin_hooks` (`required=True`), покрыт `tests/test_session_file_redirect_hook.py`. Дефекты ниже — точечные, архитектура рабочая.

**Доказательства.** Сканеры: `lib/cli/hook_loader.py` (allowlist `:129` содержит имя), `ApplicationContext`. Тесты: `tests/test_session_file_redirect_hook.py`.

#### class `SessionFileRedirectHook` (строки 102–417, 14 методов)

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 105-108 | сохраняет `workspace_dir` | корень для нормализации | загрузчик | Оставить |
| `before_execute_tool` | 114-128 | точка входа хука | вызывается наномотом | nanobot | Оставить |
| `_redirect_write` | 130-167 | подмена `path` для file-tool'ов | политика хранения | `before_execute_tool` | Оставить |
| `_redirect_media` | 169-221 | нормализация media-записи | единый layout | `before_execute_tool` | Оставить |
| `_media_entry_exists` | 223-239 | проверка, что цель уже на месте | идемпотентность | `_redirect_media` | Оставить |
| `_resolve_media_entry` | 241-288 | нормализация одного элемента `media` | в т.ч. `..`-обход | `_redirect_media` | Оставить |
| `_tool_name` | 295-297 | достать имя tool'а из ctx | классификация | `before_execute_tool` | Оставить |
| `_extract_path` | 300-304 | достать путь из аргументов | классификация | `before_execute_tool` | Оставить |
| `_session_key` | 307-314 | делегирует `resolve_session_key` | единый резолвер | `_redirect_write`/`_redirect_media` | Оставить |
| `_is_allowed` | 316-330 | проверка allowlist'а | исключения из политики | `_redirect_write` | Оставить |
| `_normalize` | 332-350 | приведение пути к относительному POSIX-виду | единый формат | `_is_allowed` | Оставить |
| `_redirect` | 352-387 | собирает путь в папке сессии, разрешает коллизии | основная работа | `_redirect_write` | Оставить |
| `_safe_leaf` | 390-417 | безопасное имя файла | защита от path traversal | `_redirect` | Упростить |

Константы модуля (все живые, все — данные, а не логика):

| Константа | Строки | Назначение | Вердикт |
|---|---|---|---|
| `_PATH_KEYS` | 54 | ключи аргументов с путём | Оставить |
| `_FILE_TOOLS` | 55 | tool'ы, чей результат — файл | Оставить |
| `_MEDIA_TOOLS` | 58 | tool'ы, чей результат — media | Оставить |
| `_ALLOWED_FILES` | 60 | файлы верхнего уровня вне редиректа | Оставить |
| `_ALLOWED_PREFIXES` | 66 | каталоги вне редиректа | Упростить |
| `_INVALID_NAME_CHARS` | 88 | запрещённые символы имени | Оставить |
| `_WIN_RESERVED` | 91 | зарезервированные имена Windows | Оставить |
| `_INVALID_NAME_RE` | 97 | regex по имени | Оставить |
| `_TRAILING_DOTS_RE` | 99 | хвостовые точки | Оставить |

- **`_ALLOWED_PREFIXES` (`:66-86`) — недостижимые записи.** Проверено: `self._workspace = ctx.workspace_dir = <repo>/workspace` (`gateway.py:503`, `cli_agent.py:177`, `config.json:4` `agents.defaults.workspace`), то есть корень всех относительных путей tool'ов — `workspace/`. Записи `"workspace/hooks/"`, `"workspace/skills/"`, `"workspace/cron/"` (`:82-85`) не могут сработать: путь `workspace/skills/x.md` от tool'а разрешается в `<repo>/workspace/workspace/skills/x.md` — allowlist его пропустит, и запись уйдёт в несуществующее место. Их надо либо удалить, либо (правильнее) переименовать в `skills/`, `cron/`, `hooks/` — но только если политика действительно хочет разрешить запись туда; сейчас `memory/` разрешён, `skills/` — нет.
- Обратная половина списка (`lib/`, `sql/`, `tools/`, `tests/`, `benchmarks/`, `.git/`, `.opencode/` — `:67-81`) разрешает запись, но путь уходит в `workspace/lib/…` вместо `<repo>/lib/…`. Проверить не удалось без запуска (`не проверено`: создаёт ли нано-ботовский `write` промежуточные каталоги) — но уже сейчас allowlist не соответствует собственному комментарию `:67` «`lib/**` — редактирование существующих файлов проекта».
- **`_safe_leaf` (`:390-401`) — баг с dotfile'ами.** `stem, dot, suffix = Path(name).stem.partition(".")`: для `.gitignore` `stem` = `""` → возвращается `""` → вызывающий (`:379-385`) подставляет `"untitled.txt"`. Для `report.2024.pdf` `stem` = `"report"`, `suffix` = `"2024.pdf"` → `report_2024.pdf` (склеивается через `f"{stem}_{suffix}"`) — приемлемо. Правка: отдельная ветка для `name.startswith(".")`, сохраняющая имя после ведущей точки.
- `_redirect` (`:352-387`): при исчерпании 999 вариантов `candidate` остаётся на **существующем** файле → следующий вызов перезапишет чужой результат. Практически недостижимо, но дешёвая правка: после цикла вернуть ошибку.
- `_ALLOWED_FILES`/`_ALLOWED_PREFIXES` объявлены как `ClassVar[set[str]]`/`ClassVar[tuple[str, ...]]` на **уровне модуля** (`:60,66`) — аннотация бессмысленна вне тела класса (никакого mangling, никакой проверки). Косметика.
- Хук использует `logging.getLogger(__name__)` (напр. `:381-385`), тогда как остальной проект — loguru. Поведение при отладке зависит от конфигурации stdlib-логгера (не проверено: подключён ли он в `gateway.py`/`cli_agent.py`).

---

## `workspace/hooks/recent_files_hook.py` — 125 LOC

**Назначение.** Собирать пути файлов, созданных tool'ами в ходе сессии, чтобы `RuntimePatcher`-обёртка на `media_serialize` могла приложить их к исходящему сообщению.

**Что делает.** `after_execute_tool` (`:84-109`) вытаскивает `path` из аргументов file-tool'ов, отбрасывает вложения (`{attachments}`) и пути вне workspace, складывает абсолютные пути в `self._paths[bucket]` (dict на сессию). Публичный `drain(session_key)` (`:111-120`) атомарно забирает и очищает bucket — его зовёт `RuntimePatcher`-обёртка в момент сборки outbound.

**Зачем нужен.** Без него `OutboundMessage.media` был бы пустым: наномот не знает, что агент создал файл, а пути живут в аргументах вызовов.

**Вердикт.** `Оставить`
**Обоснование.** Единственный источник «что агент создал» для media-авто-прикрепления; читается `lib/services/runtime_patcher.py:727,819`-областью (патч `_wrap`), регистрируется сканером хуков, канонизирован как `required=True`, покрыт тестами.

**Доказательства.** Сканер `lib/cli/hook_loader.py`. Потребители: `runtime_inventory.py::canonical_plugin_hooks`; `drain`/`collected` — `RuntimePatcher`-обёртка вокруг `media_serialize`.

#### class `RecentFilesHook` (строки 51–125, 6 методов)

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 60-66 | `_paths = {}`, лимит на сессию | состояние | загрузчик | Оставить |
| `_bucket_key` | 69-72 | ключ бакета из `context.session_key` | изоляция сессий | `after_execute_tool`, `drain` | Оставить |
| `_extract_path` | 75-82 | достать путь из аргументов | классификация | `after_execute_tool` | Оставить |
| `after_execute_tool` | 84-109 | регистрация нового пути | сбор | nanobot | Оставить |
| `drain` | 111-120 | забрать+очистить bucket | отдача наружу | `RuntimePatcher`-обёртка | Оставить |
| `collected` | 122-125 | peek без очистки | диагностика | внешний код | Оставить |

Замечания:
- `drain(session_key=None)` (`:111-120`) при `key == ""` читает `self._paths.get("", [])`, а такой ключ никогда не появляется (метод выходит раньше на `if not key`, `:88-90`). Вызов без аргумента — тихий no-op, возвращающий `[]`. Либо задокументировать, либо вернуть `list(self._paths.get("", []))`-эквивалент осмысленно; сейчас поведение неочевидно.
- `_bucket_key` (`:69-72`) использует **сырой** `context.session_key`, без `safe_session_key`, тогда как `SessionFileRedirectHook` санитайзит. Для бакета в памяти это безопасно, но два хука оперируют разными строковыми представлениями одной сессии — источник будущих багов при склейке логов.
- `self._paths` не очищается для завершённых сессий без `drain` → медленная утечка по числу живых session_key (обычно десятки, не критично).
- `after_execute_tool` создаёт записи только если `context.session_key` непуст; для gateway-сессий (`gateway:sync`) путей не будет — ожидаемо.

---

## `workspace/hooks/debug_stream_diag.py` — 70 LOC

**Назначение.** Временный диагностический хук: логирует поток дельт, reasoning и итоговое содержимое итерации в плоский файл.

**Что делает.** `_write` (`:27-29`) открывает `workspace/data_store/debug_stream.log` в режиме `"a"` на каждый вызов и пишет одну строку с `repr`-экранированием. `after_iteration` (`:49-64`) пишет `[ITER_END n=… iter=… content=… reasoning=…]`, `finalize_content` (`:66-70`) — `[FINALIZE_CONTENT …]`.

**Зачем нужен.** Для отладки конкретного инцидента со стримингом — и для этого уже ничего не нужен: хук существует в коде спустя много итераций, его собственный docstring (`:12-13`) предписывает «УДАЛИТЬ после диагностики», а `runtime_inventory.py` помечает его как «REMOVED в post-0.3.5-hook-migration». Он не был удалён.

**Вердикт.** `Удалить` (файл целиком)
**Обоснование.** Все семь символов файла удаляются вместе с ним. Ничего, кроме файла лога, не ломается: хук не входит в пользовательский контур, его вывод нигде не читается (grep по репозиторию: единственные упоминания — сам файл, allowlist `lib/cli/hook_loader.py:129` и запись в `runtime_inventory.py`).
**Предварительная работа:** (1) удалить `"debug_stream_diag"` из `_allowed_hook_names()` в `lib/cli/hook_loader.py`; (2) удалить spec из `canonical_plugin_hooks()` в `lib/services/runtime_inventory.py`; (3) прогнать `tests/test_runtime_inventory.py` и `tools/diagnose_startup.py --log` для подтверждения `OK`; (4) если нужен исторический доступ к логу — он и так останется на диске, удалять вручную по `data_store/debug_stream.log`; (5) проверить `tests/contract/test_agent_hook_api.py` — он перечисляет хуки проекта.

**Доказательства. Разрешение противоречия «0 ссылок vs. документирован»:** 0 статических ссылок — это артефакт сканирования каталога, а не признак смерти. Хук **жив**:
- `lib/cli/hook_loader.py:129` — `"debug_stream_diag"` в allowlist (не в blacklist);
- `nanobot/agent/hook.py` существует в установленной 0.3.5, поэтому `from nanobot.agent.hook import AgentHook` (`:15`) импортируется без ошибки (в отличие от `from nanobot.agent import AgentHook` в соседних хуках — косметическое расхождение стиля);
- `lib/core/application_context.py:407-409` вызывает `scan_and_register(ctx.workspace_dir / "hooks", ctx.workspace_dir)` на каждом старте;
- сигнатуры `on_stream`/`emit_reasoning`/`emit_reasoning_end`/`on_stream_end`/`after_iteration`/`finalize_content` совпадают с `AgentHook` в 0.3.5 (проверено по `nanobot/agent/hook.py`).

**Ответ про `enabled` и риск в проде:** флага `enabled`/`config_key` у хуков в этом проекте нет вовсе (такого механизма нет и в `hook_loader`); единственный способ отключения — убрать имя из `_allowed_hook_names()` или удалить файл. Риск в проде реальный и двойной: (а) **конфиденциальность** — в `data_store/debug_stream.log` попадает полный текст ответов модели и reasoning в открытом виде, без маскирования; (б) **диск** — append без лимита и ротации, по одному `open()/close()` на чанк.

**Мёртвые ветки внутри (независимо от вердикта по файлу):** `on_stream` (`:31-35`), `emit_reasoning` (`:37-41`), `emit_reasoning_end` (`:43-44`), `on_stream_end` (`:46-47`) и счётчики `_stream_count`/`_think_count` **не вызываются никогда**: `AgentHook.wants_streaming()` по умолчанию `False` (`nanobot/agent/hook.py:72-73`), а ни один хук проекта его не переопределяет (проверено grep'ом по всему репозиторию + контрактный тест). Фактически работает только `after_iteration`/`finalize_content`. Это ровно тот случай, когда «0 ссылок» внутри класса — честный сигнал.

#### class `StreamDiagnosisHook` (строки 16–70, 7 методов)

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 17-25 | фиксирует `workspace_dir` | путь к логу | загрузчик | Удалить |
| `_write` | 27-29 | append одной строки в лог | единственный побочный эффект | остальные методы | Удалить |
| `on_stream` | 31-35 | лог дельты контента | отладка стриминга | **никогда** (`wants_streaming()=False`) | Удалить |
| `emit_reasoning` | 37-41 | лог reasoning-дельты | отладка | **никогда** | Удалить |
| `emit_reasoning_end` | 43-44 | счётчик thinking-блоков | отладка | **никогда** | Удалить |
| `on_stream_end` | 46-47 | счётчик стримов | отладка | **никогда** | Удалить |
| `after_iteration` | 49-64 | лог `content`+`reasoning` за итерацию | отладка | nanobot, каждый ход | Удалить |
| `finalize_content` | 66-70 | лог финального содержимого | отладка | nanobot, каждый ход | Удалить |

Атрибуты: `name = "stream_diag"` (`:21`), `_log_file` (`:22-23`), `_stream_count`/`_think_count` (`:19-20`) — удаляются вместе с классом.

---

## `workspace/utils/db.py` — 1063 LOC

**Назначение.** Единственный коннектор всего проекта к PostgreSQL/Greenplum: собственный пул на psycopg2, синхронные и асинхронные хелперы, прокси-объекты для безопасного запуска callback'ов на «своём» соединении, экспортный пул для синглтонов (blocking и async).

**Что делает.** `DBManager` (`:381-658`) держит N воркеров-потоков и очередь задач. Ключевая идея: `transaction()` / `async_transaction()` выдают **эксклюзивную аренду** воркера (`:551-604`), поэтому callback, работающий с переданной `_ConnectionProxy`, гарантированно выполняется на том же соединении, где начата транзакция, — иначе BEGIN/COMMIT разъехались бы по соединениям. Прокси `_CursorProxy`/`_ConnectionProxy`/`_AsyncConnectionWrapper` (`:691-840`) реализуют «SQL-объект без реального соединения»: любой метод синхронизируется через `threading.Event` с выделенным воркером, а `_sanitize_param` (`:666-678`) на границе вычищает NUL через `utils.clean_text.clean_text`. Экспортные `run/execute/fetch*/transaction` (`:940-1022`) и `async_*` (`:1028-1063`) — тонкие обёртки над общим `_get_manager()` (`:851-859`).

**Зачем нужен.** Без него нет ни каналов, ни журнала, ни зеркала сессий, ни загрузки кэша. Это инфраструктурный корень подсистемы данных.

**Вердикт.** `Оставить`
**Обоснование.** Самый тщательно написанный модуль группы: инвариант аренды выдержан, `deque`-мутация в `_take_job` проверена и безопасна, `docs/DATABASE.md:70-71` фиксирует публичный API как контракт, покрыт `tests/test_utils_db.py` и `tests/integration/test_worker_pool_concurrency.py`. Найденные проблемы — в глобальном состоянии DSN и в двух неточных доктринах, а не в устройстве пула.

**Доказательства.** Импортёры (13+): `lib/channels/postgres_channel.py:42-46`, `lib/core/application_context.py:715,1042,1790,1802,1812`, `lib/session/pg_session_manager.py:75`, `lib/services/db_logging_service.py:804`, `lib/services/session_cold_sync_service.py:139,494,637`, `lib/services/cache_load_service.py:151,448`, `lib/services/context_compaction.py:548`, `lib/services/session_storage.py:100`, `workspace/tools/history_search_tool.py:394`, `streamlit_app.py:105`, `gateway.py:370`, `tools/build_vectors.py:79`, `tools/check_worker_pool_integrity.py:50`, `benchmarks/db.py:33`, `scripts/backfill_media_aw.py:37`. Тесты: `tests/test_utils_db.py`, `tests/integration/test_worker_pool_concurrency.py`.

#### class `_JobResult` (строки 121–142, 4 метода)
Ручной `Future`: воркер кладёт результат/ошибку, вызывающая сторона ждёт `threading.Event` с таймаутом. Обёртка `concurrent.futures.Future` здесь была бы лишней.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 124-127 | `event` + слоты | состояние | `_submit` | Оставить |
| `set_result` | 129-131 | успех | передача значения | `_Worker._execute_job` | Оставить |
| `set_error` | 133-135 | ошибка | передача исключения | `_Worker._execute_job` | Оставить |
| `get` | 137-142 | ожидание + таймаут → `PoolTimeoutError` | вызывающая сторона | `_Worker.run` | Оставить |

#### class `_Job` (строки 145–161, 1 метод)
Единица работы: функция, аргументы, метка вызывающего (`_caller_tag`), `lease_id` (0 = без аренды) и `result`. `lease_id` — тот самый механизм, который различает «свою» и «чужую» задачу в `_take_job`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 150-161 | заполняет поля | состояние | `_submit` | Оставить |

#### class `_Worker` (строки 198–373, 13 методов)
Поток пула: держит одно psycopg2-соединение, берёт задачи, держит `lease_id` для транзакционных callback'ов.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 199-217 | слоты состояния, дескриптор для отладки | состояние | `_spawn_worker` | Оставить |
| `_ensure_connected` | 221-239 | ленивое подключение | не поднимать пул целиком при первом касании | `run` | Оставить |
| `_connect_with_backoff` | 241-273 | `psycopg2.connect` с экспоненциальным backoff | устойчивость к падению БД | `_ensure_connected` | Оставить |
| `_drop_connection` | 275-283 | закрыть + забыть | самовосстановление после обрыва | `_execute_job` | Оставить |
| `_open_cursor` | 287-291 | новый курсор + регистрация | учёт открытых курсоров | `_cursor` | Оставить |
| `_cursor` | 293-297 | взять/переоткрыть | — | `run` | Оставить |
| `_close_cursor` | 299-305 | закрыть и снять с учёта | — | `run` | Оставить |
| `run` | 309-320 | цикл: взять задачу, выполнить, закрыть курсор | основной цикл | поток | Оставить |
| `_activity_print` | 322-336 | разовая диагностическая печать активности | отладка пула | `run` | Оставить |
| `_execute_job` | 338-373 | прогон callback'а, `set_result`/`set_error` | исполнение | `run` | Оставить |

- Утечка курсоров: `_open_cursor` (`:287-291`) регистрирует курсор в `worker._cursors`, но `_release_lease` (`:618-631`) реестр **не чистит** — курсоры, оставленные открытыми внутри транзакционного callback'а, живут до следующего `_drop_connection`. Ограничено, но при частых транзакциях без явного `close()` список растёт. Правка: в `_release_lease` закрывать и очищать непустой `_cursors`.

#### class `DBManager` (строки 381–658, 20 методов)

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 382-419 | конфиг, слоты, очередь, леас-воркеры | состояние | `_get_manager` | Оставить |
| `start` | 423-434 | ленивый старт под локом | потоки создаются по требованию | экспортный `start` | Оставить |
| `shutdown` | 436-447 | остановка потоков, дренирование очереди | graceful | экспортный `shutdown` | Упростить |
| `_spawn_worker` | 449-453 | создание потока | — | `start` | Оставить |
| `_maybe_shrink` | 455-465 | уменьшение пула при простое | экономия соединений | `get_stats` | Оставить |
| `_take_job` | 467-513 | достать задачу, приоритет lease'ов | ядро планировщика | `Worker.run` | Оставить |
| `_requeue` | 515-518 | вернуть задачу в очередь | при нехватке воркеров | `_submit` | Оставить |
| `_submit` | 520-543 | поставить задачу, дождаться воркера | — | экспортные хелперы | Оставить |
| `_ensure_started` | 545-547 | автостарт | удобство | `_acquire_lease` | Оставить |
| `_acquire_lease` | 551-604 | эксклюзивный воркер + BEGIN | инвариант транзакции | `transaction`/`async_transaction` | Оставить |
| `_begin_tx` | 607-608 | `BEGIN` | — | `_acquire_lease` | Оставить |
| `_end_tx` | 611-616 | `COMMIT`/`ROLLBACK` | — | `_release_lease` | Оставить |
| `_release_lease` | 618-631 | END + возврат воркера в свободные | — | прокси-`__exit__` | Оставить |
| `get_stats` | 633-658 | метрики (size/available/queued/in_use/reconnects) | `/health`, диагностика | экспортный `get_stats` | Оставить |

- **`_take_job` (`:467-513`) — проверено, ложная тревога.** Код делает `self._queue.remove(job)` внутри `for job in self._queue`, что формально должно дать `RuntimeError: deque mutated during iteration`. Экспериментально подтверждено (Python 3.14): исключение возникает при *продолжении* итерации после мутации, а здесь `return` происходит сразу же. Поведение безопасно; менять нечего, но стоит добавить комментарий, иначе следующий аудит потратит время на ту же проверку.
- `shutdown` (`:436-447`) не сбрасывает `_lease_workers` — после останова там остаются записи воркеров, помеченных как занятые. `_acquire_lease` после `shutdown` падает в `RuntimeError` (проверено по коду), так что практического вреда нет, но состояние нечистое. Правка: обнулить словарь.

#### class `_CursorProxy` (строки 691–751, 14 методов)
Синхронный прокси курсора: каждый метод пересылает выполнение на воркера, владеющего соединением, и возвращает результат вызывающему потоку.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 694-696 | привязка к manager/lease | состояние | `_ConnectionProxy.cursor` | Оставить |
| `_run` | 698-699 | общий «выполни на воркере» | — | все ниже | Оставить |
| `__enter__` | 701-702 | контекстный менеджер | — | `transaction()` | Оставить |
| `__exit__` | 704-705 | контекстный менеджер | — | `transaction()` | Оставить |
| `connection` | 708-709 | underlying connection | для `execute_values` | внешний код | Оставить |
| `description` | 712-713 | метаданные результата | ORM-совместимость | psycopg2 extras | Оставить |
| `rowcount` | 716-717 | затронуто строк | — | вызывающий | Оставить |
| `statusmessage` | 720-721 | текст команды | дебаг | вызывающий | Оставить |
| `execute` | 723-730 | выполнить + санитизация параметров | граница БД | `run`/вызывающий | Оставить |
| `mogrify` | 732-734 | сборка литерала | дебаг SQL | вызывающий | Оставить |
| `__iter__` | 736-739 | итерация по fetchmany | привычный API | вызывающий | Оставить |
| `fetchone` | 741-742 | одна строка | — | `fetchone` | Оставить |
| `fetchall` | 744-745 | все строки | — | `fetch` | Оставить |
| `fetchmany` | 747-748 | порция | — | `__iter__` | Оставить |
| `close` | 750-751 | закрыть | — | `run` | Оставить |

`executemany` в прокси **нет** — `psycopg2.extras.execute_values` на `_CursorProxy` упадёт `AttributeError`. Это не баг (вызывать его нельзя), но стоит отметить явно, потому что docstring `_sanitize_params` (`:681-688`) обещает санитизацию «в т.ч. `execute_values` для сессий», а фактически `_replace_messages` (`lib/services/session_cold_sync_service.py`) выполняет `execute_values` на **сыром** соединении внутри `run()`, мимо `_sanitize_params`. Доктрина неверна: санитизация там держится только на патче `Session.add_message` (`lib/services/runtime_patcher.py`).

#### class `_ConnectionProxy` (строки 754–821, 11 методов)
Аналогично, но для соединения; `execute`/`fetch`/`fetchrow`/`fetchone`/`fetchval` дают привычный API поверх арендованного воркера.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 757-760 | захват `_lease_workers[lease_id]` | привязка к воркеру | `_acquire_lease` | Оставить |
| `_run` | 762-765 | общий диспетчер | — | все ниже | Оставить |
| `encoding` | 768-769 | кодировка | — | вызывающий | Оставить |
| `cursor` | 771-776 | `with conn.cursor()` | API psycopg2 | `run`/вызывающий | Оставить |
| `execute` | 778-786 | DML | — | вызывающий | Оставить |
| `fetch` | 788-796 | все строки | — | `utils.db.fetch` | Оставить |
| `fetchrow` | 798-807 | первая строка | — | `utils.db.fetchone` | Оставить |
| `fetchone` | 809-810 | синоним `fetchrow` | совместимость | вызывающий | Оставить |
| `fetchval` | 812-821 | первое значение | — | `utils.db.fetchval` | Оставить |

#### class `_AsyncConnectionWrapper` (строки 824–840, 5 методов)
Async-зеркало `_ConnectionProxy`: каждый метод — `async def`, синхронно ждущий тот же результат. Используется `lib/channels/postgres_channel.py:42-46`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 827-828 | обёртка над прокси | — | `async_transaction` | Оставить |
| `fetch` | 830-831 | await-обёртка | — | `async_fetch` | Оставить |
| `fetchrow` | 833-834 | await-обёртка | — | `async_fetchone` | Оставить |
| `execute` | 836-837 | await-обёртка | — | `async_execute` | Оставить |
| `fetchval` | 839-840 | await-обёртка | — | `async_fetchval` | Оставить |

**Ограничение, о котором стоит знать:** «асинхронный» слой синхронно блокирует event-loop-поток на всё время SQL-запроса внутри `_run`. Для канала это приемлемо (запросы короткие), но имя `async_*` вводит в заблуждение: это не неблокирующий драйвер. Документировано ли это в `docs/DATABASE.md` — не проверено.

#### class `PoolTimeoutError` (строки 189–190)
Экспортируемое исключение «задача не выполнена за `pool_timeout`». Живёт как контракт: `catch PoolTimeoutError` в коде не встречается (grep), но он же `_JobResult.get` внутри `_Worker.run` ловит, чтобы не убить поток.

| — | — | — | — | — | — |
|---|---|---|---|---|---|
| `PoolTimeoutError` | 189-190 | таймаут на задачу | диагностика зависших запросов | `_JobResult.get` | Оставить |

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `set_pool_config` | 101-113 | применить `channels.postgres.pool` | конфигурируемость пула | `application_context.py:1790` | Оставить |
| `_caller_tag` | 164-186 | метка вызывающего из стектрейса | диагностика в `get_stats` | `_Job.__init__` | Оставить |
| `_sanitize_param` | 666-678 | `clean_text` на параметре | граница БД | `_sanitize_params` | Оставить |
| `_sanitize_params` | 681-688 | рекурсивно по параметрам | граница БД | `_CursorProxy.execute`, `_ConnectionProxy.execute` | Оставить |
| `_get_manager` | 851-859 | ленивый синглтон-менеджер | один пул на процесс | все экспортные хелперы | Оставить |
| `resolve_dsn` | 867-879 | `${DATABASE_URL}` → DSN | без хардкода | `tools/build_vectors.py:79`, `_get_manager` | Оставить |
| `configure` | 882-888 | установить DSN глобально | переключение на тестовую БД | 9 мест (см. выше) | Оставить |
| `start` | 891-893 | форс-старт пула | прогрев | `application_context.py:1802` | Оставить |
| `shutdown` | 896-901 | остановка пула | graceful | `application_context.py:1812` | Оставить |
| `get_stats` | 904-905 | метрики пула | `/health` | `gateway.py:370`, `session_cold_sync_service.py:637` | Оставить |
| `probe_connections` | 908-937 | прогреть N соединений | прогрев при старте | `gateway.py:370` | Упростить |
| `run` | 940-947 | произвольный callback на воркере | нижний примитив | `db_logging_service.py:805`, `cache_load_service.py:448`, `tools/check_worker_pool_integrity.py:50` | Оставить |
| `execute` | 955-964 | DML → statusmessage | — | 6 мест | Оставить |
| `fetch` | 967-976 | все строки | — | `history_search_tool.py:394`, `streamlit_app.py:105`, `application_context.py:715` | Оставить |
| `fetchone` | 979-989 | первая строка | — | `streamlit_app.py:105` | Оставить |
| `fetchval` | 812-821 (внутри) → 992-1002 | первое значение | — | `benchmarks/db.py:33` | Оставить |
| `transaction` | 1006-1022 | контекстный менеджер с арендой | транзакции | `session_cold_sync_service.py:494`, `context_compaction.py:548`, `db_logging_service.py:805` | Оставить |
| `async_execute` | 1030-1031 | await-обёртка | — | `postgres_channel.py:42` | Оставить |
| `async_fetch` | 1034-1035 | await-обёртка | — | `postgres_channel.py:43` | Оставить |
| `async_fetchone` | 1038-1039 | await-обёртка | — | `postgres_channel.py:44` | Оставить |
| `async_fetchval` | 1042-1043 | await-обёртка | — | `postgres_channel.py:45` | Оставить |
| `async_transaction` | 1047-1063 | async CM с арендой | транзакции канала | `postgres_channel.py:46` | Оставить |

- **`configure` (`:882-888`) — глобальное мутабельное состояние, ответ на вопрос брифа о разделении подключений.** Разделения нет: DSN — глобальная переменная модуля, менеджер — процессный синглтон. `configure()` переписывает `_manager._dsn` **без переподключения уже живых воркеров**, поэтому после `configure(new_dsn)` `resolve_dsn()` вернёт новый адрес, а уже открытые соединения продолжат работать со старым — рассинхрон на уровне процесса. В текущей конфигурации это безвредно (dsn один), но при тестовом профиле или при двух сервисах с разными базами — реальная ловушка. Правка (не в этой итерации): хранить dsn в `ApplicationContext` и передавать в конструктор менеджера, либо явно документировать «после `configure()` требуется `shutdown()`+`start()`».
- `probe_connections` (`:908-937`) — доктрина «каждый воркер инициирует подключение» не гарантирована: задачи без lease'а (`lease_id=0`) распределяются как попало, один свободный воркер может выполнить все `count` задач подряд. Для цели «прогреть пул» надо после прогрева сверять `stats["connected"]` и при недоборе повторять. Вердикт `Упростить` (доктрина/логика), не удалять — вызывается из `gateway.py:370`.
- Дублирования с `lib/services/db_logging_service.py` **нет**: там нет ни psycopg2, ни пула — только счётчики и вызовы `utils.db.run`. Совпадает лишь **имя** `get_stats` (у пула и у журнала разные поля), что стоит учитывать при чтении логов. Вторая реализация `resolve_dsn` — `tools/migrate.py` (dev-слой, не мой; кросс-подсистемная находка).
- Greenplum: код не использует ничего, что ломало бы GPDB 6 — только `mogrify`/параметризование вместо интерполяции, `%s`-плейсхолдеры и `ILIKE`. Таймауты задач реализованы на стороне клиента, что для GPDB корректно (отмена запроса на сервере не нужна). Нарушений не найдено.
- `run()` (`:940-947`) документирует риск тупика, если из callback'а вызвать `transaction()`; это верно и осознанно, вердикт `Оставить`.

---

## `workspace/utils/session_file_store.py` — 431 LOC

**Назначение.** Файловое хранилище артефактов сессии: результаты больших выводов tool'ов в `results/`, вложения пользователя в `attachments/`, `metadata.json` счётчиков, уборка по количеству/возрасту.

**Что делает.** `save` (`:291-361`) нормализует содержимое через `prepare_content` (json/csv/txt), дедуплицирует по sha1 в пределах сессии, пишет `{ts}_{tool}_{id}__{hash}{ext}` в `results/`, инкрементирует `metadata.json`, зовёт `cleanup` и возвращает относительный путь вида `cache/sessions/<key>/results/<file>`. `save_attachment` (`:188-257`) декодирует data-URL или копирует локальный файл в `attachments/` под `{uuid12}_{имя}` и возвращает **абсолютный** путь. `cleanup` (`:363-418`) сначала режет по возрасту (префикс имени `YYYYmmdd_HHMMSS`, сортировка по имени = по времени), затем по количеству, и пересчитывает `metadata.json`.

**Зачем нужен.** Патчи `lib/services/runtime_patcher.py:727,819` сохраняют через него крупные результаты tool'ов, а каналы — вложения. Без него результаты ехали бы в промпт целиком.

**Вердикт.** `Слить с `workspace/utils/session_key.py`` (в части `safe_session_key`) + `Упростить`
**Обоснование.** Сам класс нужен и работает, но у него три самостоятельных дефекта: несогласованный с хуком `safe_session_key` (ломает навигацию по папкам), несовместимый контракт `base_dir` с двумя из трёх вызывающих (см. ключевую находку) и back-compat-обёртка с нулём внешних ссылок. Остальное — косметика.

**Доказательства.** Импортёры: `lib/channels/postgres_channel.py:207,211`, `lib/channels/redis_channel.py:147-149`, `lib/services/runtime_patcher.py:727,819`, `streamlit_app.py` (через `utils.session_file_store`). Тесты: `tests/test_utils_session_file_store.py` (покрывает `prepare_content`, `archive_session`, `save`, `save_attachment`, `cleanup`). С `lib/session/` **не дублирует**: `lib/session/pg_session_manager.py` работает с JSONL/PG-метаданными, файловый кэш артефактов — только здесь.

#### class `SessionFileStore` (строки 127–431, 12 методов)

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 128-154 | `base_dir/cache/{sessions,archive}` | layout | `runtime_patcher`, каналы | Упростить |
| `_get_session_dir` | 156-162 | папка сессии + `results/` + `attachments/` | — | почти все | Оставить |
| `_resolve_attachments_dir` | 164-174 | каталог вложений | — | `save_attachment` | Оставить |
| `_sanitize_filename` | 177-182 | безопасное имя | — | `save_attachment` | Оставить |
| `_guess_ext_from_mime` | 185-186 | делегат модульной функции | — | `save_attachment` `:230,239` | Упростить |
| `save_attachment` | 188-257 | вложение → `attachments/` | приём медиа от пользователя | каналы | Оставить |
| `_ensure_metadata` | 259-271 | создать `metadata.json` | — | `save`, `save_attachment` | Оставить |
| `_find_existing_for_hash` | 273-289 | поиск дубля по `__<hash>` | дедуп | `save` | Оставить |
| `save` | 291-361 | результат tool'а → `results/` | выгрузка больших ответов | `runtime_patcher` | Оставить |
| `cleanup` | 363-418 | уборка по возрасту/количеству | ограничение диска | `save` | Оставить |
| `archive_session` | 420-431 | перенос папки в `cache/archive` | — | **только тесты** | Удалить |

- **`__init__` (`:147-151`) — источник бага из ключевых находок.** Контракт «`base_dir` + `cache/sessions`» задокументирован в docstring метода (`:138`), но вызывающие его поняли по-разному: `lib/services/runtime_patcher.py:732-733` передаёт `workspace_dir / "data_store"` → путь верный; `lib/channels/postgres_channel.py:210` и `lib/channels/redis_channel.py:148` передают `media_cache_dir.parent` = `data_store/cache` → путь `data_store/cache/**cache**/sessions`, неверный. Внедряемый store в проде не настроен (`"_file_store"` в `_get(...)` есть только в тестах), поэтому именно неверная ветка — боевая. Правка по уму: разрешить в конструкторе абсолютный/готовый путь (если `base_dir` уже заканчивается на `sessions` — не дописывать), тогда и `_resolve_sfs_base` в каналах станет не нужна.
- `_guess_ext_from_mime` (`:184-186`) — **мёртвая back-compat-обёртка.** Docstring `guess_ext_from_mime` (`:35-37`) прямо говорит, что метод «прежний» и заменён модульной функцией. Проверено: вне класса к статическому методу не обращается никто (два внутренних вызова на `:230,239` — единственные). Правка: удалить метод, в двух местах звать `guess_ext_from_mime` напрямую; `__all__`-контракт не пострадает.
- `archive_session` (`:420-431`) — **0 вызовов в проде**; единственные ссылки — `tests/test_utils_session_file_store.py:308-325`. `self.archive_dir` создаётся в `__init__:150-151` только ради него, поэтому каталог `cache/archive/` создаётся впустую. Вердикт `Удалить` вместе с тремя тестами и строкой `archive_dir`; либо, если архивация планировалась (закрытие сессии), сначала подключить её к месту закрытия сессии — сейчас такой точки нет.
- `save` (`:330`) — **несогласованная семантика `id`.** В ветке дедупа `id = existing.split("_")[-1].split(".")[0]` даёт `"__<hash12>"`, в обычной ветке (`:356`) — `entry_id` (8 hex). Один и тот же ключ ответа означает то «идентификатор записи», то «хеш». Правка: возвращать `entry_id` в обеих ветках (или ввести отдельное поле `content_hash`).
- `save` (`:339`) — `source_tool` подставляется в имя файла **без санитизации**; при `..`/`/` в имени tool'а путь вышел бы из `results/`. Сейчас имена tool'ов задаются кодом (`runtime_patcher`), поэтому эксплуатируемости нет, но дешёвая защита через уже существующий `_sanitize_filename` была бы правильной.
- `save` (`:331`, `:357`) возвращает путь вида `cache/sessions/...` — **относительный**, а `save_attachment` (`:254`) — абсолютный. Один тип возврата на два публичных метода одного класса; вызывающий обязан знать, что именно придёт. Правка: унифицировать.
- `save`/`save_attachment` (`:345-350`, `:245-251`) читают-пишут `metadata.json` без блокировки и без атомарной замены → при двух параллельных сохранениях в одну сессию счётчики теряются. Правка: `os.replace` временного файла.
- `UTC = UTC` (`:12`) — остаток миграции на `datetime.UTC`, **мёртвый оператор**; удалить одной строкой.
- Строки `:14-18` — текст «"""Модуль для хранения сессий…"""» стоит **после** импортов (`:1-12`) и потому не является docstring модуля. Перенести в начало файла.

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `safe_session_key` | 27-29 | санитизация ключа сессии | имя папки | весь класс | Слить с `workspace/utils/session_key.py` |
| `guess_ext_from_mime` | 32-54 | MIME → расширение | единая точка | `save_attachment`, `streamlit_app.py` | Оставить |
| `_csv_val` | 57-59 | `None` → `""` в CSV | — | вложена в `_try_convert_to_csv` | Оставить |
| `prepare_content` | 62-80 | контент → (контент, ext) | формат файла результата | `runtime_patcher.py:756,853` | Оставить |
| `_try_convert_to_csv` | 83-124 | json → CSV (с BOM) | читаемость таблиц | вложена в `prepare_content` | Оставить |

**Слияние `safe_session_key` (ключевая находка №4).** Реализации несовместимы:

| Вход | `session_key.py:36-37` | `session_file_store.py:29` |
|---|---|---|
| `"telegram:8281248569"` | `telegram_8281248569` | `telegram_8281248569` (совпадает) |
| `"my session"` | `my_session` | `"my session"` (пробел сохранён) |
| `"привет"` | `______` | `привет` |
| `"///"` | `__nosession__` | `"_"` |
| `"a:b:c"` | `a_b_c` | `a_b_c` (совпадает) |
| `""` | `__nosession__` | `""` → путь `sessions//` (то есть `sessions/`) |

`AGENTS.md` (File Storage Policy) утверждает, что конвенция согласована между `SessionFileRedirectHook` и `SessionFileStore` — это неверно; хук использует `session_key.py`, класс — собственный вариант. Правка: удалить `session_file_store.py:24,27-29` и импортировать `safe_session_key` из `workspace/utils/session_key.py`. Побочный выигрыш: перестаёт существовать риск, что папка, куда хук положил файл, не совпадёт с папкой, откуда `SessionFileStore` его перечитывает.

---

## `workspace/utils/office_files.py` — 304 LOC

**Назначение.** Извлечение текста, таблиц и метаданных из офисных документов (docx/pdf/pptx/xlsx/xls/csv/txt) — общая утилита для скиллов и документации скилла.

**Что делает.** `detect_format` (`:10-18`) определяет формат по расширению (в т.ч. `doc`→`docx`, `ppt`→`pptx`, `rtf`→`txt`). `extract_text` (`:123-142`) маршрутизирует по формату в один из шести приватных `_extract_*`, каждый из которых открывает свой движок (`python-docx`, `pdfplumber`, `python-pptx`, `openpyxl`, `xlrd`, CSV). `extract_tables` (`:166-175`) — два приватных бэкенда (`_tables_docx` — XML-обход `w:tbl`, `_tables_pdf` — `pdfplumber`), для xlsx/xls/pptx/txt возвращает `[]`. `summarize` (`:266-304`) собирает метаданные (страницы/листы/слайды/автор/свойства) пятью приватными `_summarize_*`. `read_xlsx_sheet` (`:178-193`) — точечный доступ к листу. Импорты движков ленивые, внутри функций, — модуль импортируется без необязательных зависимостей.

**Зачем нужен.** Это публичный API скилла `workspace/skills/office_files` (документация — `workspace/skills/office_files/SKILL.md:46-150`) и общий вход для `legal_summarizer` (`scripts/document/loader.py:25`, `document/physical.py:46-49`, `document_io.py:12`).

**Вердикт.** `Оставить`
**Обоснование.** Ни один символ не мёртв: все девятнадцать функций достижимы либо из публичного API, либо из приватного слоя под ним. Краткая брифа «тесты: —» — ложная: покрытие есть в `tests/test_office_files.py:73-194`.

**Доказательства.** `workspace/skills/legal_summarizer/scripts/document/physical.py:46-49` (`detect_format`, `extract_tables`), `scripts/document/loader.py:25` (`detect_format`), `scripts/document_io.py:12` (`extract_text`), `workspace/skills/office_files/SKILL.md:46,48,49,126,138,148` (`extract_text`, `extract_tables`, `summarize`, `read_xlsx_sheet`), `tests/test_office_files.py:73-194`.

#### Функции уровня модуля (19)

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `detect_format` | 10-18 | формат по расширению | маршрутизация и guard | `loader.py:25`, `physical.py:47`, `extract_text:127`, `extract_tables:170`, `summarize:270` | Оставить |
| `_read_text_auto` | 21-33 | txt с авто-детектовкой кодировки | — | вложена в `extract_text` | Оставить |
| `_extract_docx` | 36-49 | абзацы + таблицы docx | — | `extract_text` | Оставить |
| `_extract_pdf` | 52-64 | страницы pdf | — | `extract_text` | Оставить |
| `_extract_pptx` | 67-82 | текст слайдов | — | `extract_text` | Оставить |
| `_extract_xlsx` | 85-98 | листы xlsx | — | `extract_text` | Оставить |
| `_extract_xls` | 101-113 | листы xls (`xlrd`) | — | `extract_text` | Оставить |
| `_extract_csv` | 116-120 | csv как текст | — | `extract_text` | Оставить |
| `extract_text` | 123-142 | публичная точка | API скилла | `document_io.py:12`, `physical.py:219` | Оставить |
| `_tables_docx` | 145-152 | таблицы docx | — | `extract_tables` | Оставить |
| `_tables_pdf` | 155-163 | таблицы pdf | — | `extract_tables` | Оставить |
| `extract_tables` | 166-175 | публичная точка | API скилла | `physical.py:48` | Оставить |
| `read_xlsx_sheet` | 178-193 | один лист xlsx | — | `SKILL.md:138` | Оставить |
| `_summarize_docx` | 196-209 | метаданные docx | — | `summarize` | Оставить |
| `_summarize_pdf` | 212-224 | страницы/свойства pdf | — | `summarize` | Оставить |
| `_summarize_pptx` | 227-239 | слайды/свойства pptx | — | `summarize` | Оставить |
| `_summarize_xlsx` | 242-252 | листы/автор xlsx | — | `summarize` | Оставить |
| `_summarize_xls` | 255-263 | листы/автор xls | — | `summarize` | Оставить |
| `summarize` | 266-304 | публичная точка | API скилла | `SKILL.md:148`, `tests/test_office_files.py:94,110,130,164,194` | Оставить |

Замечания (не меняющие вердикт):
- `summarize` (`:266-304`) возвращает `{"pages": None, ...}` для docx/xlsx/xls — то есть формат ответа неоднороден по типам. `SKILL.md:65` обещает «dict: pages/sheets/slides/author/...», не оговаривая `None`. Дешёвая правка — задокументировать.
- `_extract_xls` (`:101-113`) и `_summarize_xls` (`:255-263`) держат импорт `xlrd`; в `xlrd>=2` модуль умеет только xls, и если в окружении окажется xlrd 3 — импорт падает, ломая весь `extract_text` для docx/pdf, а не только xls. Защита — try/import-локальный fallback (не проверено: какая версия xlrd стоит).
- Модуль не содержит `extract_structure` — см. ключевую находку №3.

---

## `workspace/utils/structure_cache.py` — 62 LOC

**Назначение.** (заявлено) файловый кэк «структуры» офисного документа, чтобы не пересчитывать её при каждом анализе.

**Что делает.** `_key` (`:12-15`) строит имя кэш-файла по пути+hash; `get_structure` (`:18-62`) вызывает `extract_structure` и складывает результат в `workspace/data_store/cache/structure/`.

**Зачем нужен.** Не нужен: он был написан под `office_files.extract_structure`, которого в проекте больше нет.

**Вердикт.** `Удалить` (файл целиком)
**Обоснование.** Три независимых доказательства смерти: (1) **0 импортёров** — проверено grep'ом по всему репозиторию, включая `tests/`, `tools/`, `benchmarks/`, `.github/`, `docs/`, `SKILL.md`; (2) **0 ссылок** на `get_structure` и `structure_cache` вне самого файла; (3) **модуль физически не импортируется** — строка `from workspace.utils.office_files import extract_structure` (`:7`) падает `ImportError`, потому что такой функции в `office_files.py` нет (проверено разбором AST: 19 дефов, `extract_structure` среди них нет; `legal_summarizer/scripts/document/physical.py:214` описывает её как «ранее существовавшую»). Мёртвый и одновременно сломанный код.
**Что удаляется.** Весь файл (62 LOC): `_CACHE_DIR` (`:9`), `_key` (`:12-15`), `get_structure` (`:18-62`).
**Что останется.** Ничего не сломается: файл никогда не импортировался. Побочно остаётся `tools/extract_office_structure.py:10` с тем же битым импортом — это dev-утилита вне моей подсистемы, её правку нужно сделать владельцу `tools/`.
**Предварительная работа.** Удалить файл; проверить `python -c "import workspace.utils"` и прогнать `tests/` (ожидаемо — без изменений). Каталог `workspace/data_store/cache/structure/`, если он остался от прошлых запусков, удалить вручную.

| Символ | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_CACHE_DIR` | 9 | путь кэша (cwd-relative!) | — | `get_structure` | Удалить |
| `_key` | 12-15 | имя кэш-файла | — | вложена в `get_structure` | Удалить |
| `get_structure` | 18-62 | кэш структуры документа | — | никто | Удалить |

Отдельно: `_CACHE_DIR = Path("workspace/data_store/cache/structure")` — **путь относительный к cwd**, а не к корню репо (ср. `session_file_store`, где `base_dir` всегда абсолютный). Даже будь модуль живым, он писал бы в каталог, зависящий от точки запуска. Ещё один аргумент за удаление.

---

## `workspace/utils/session_key.py` — 119 LOC

**Назначение.** Единая конвенция имени папки сессии и единый резолвер ключа сессии для хуков, скиллов и standalone-процессов.

**Что делает.** `safe_session_key` (`:29-37`) санитайзит всё, кроме `[A-Za-z0-9._-]`, снимает краевые `._-` и отдаёт `__nosession__` на пустом результате. `resolve_session_key` (`:40-65`) достаёт ключ из in-process контекста (`context.session_key`, затем `context.metadata.session_key`). `resolve_session_key_for_subprocess` (`:68-106`) — зеркало для skill-процессов: `SESSION_KEY` из окружения, иначе basename переданного файла. `extract_session_key_from_path` (`:109-119`) обратная операция — вытаскивает raw-ключ из пути `data_store/cache/sessions/<key>/…`.

**Зачем нужен.** Склеивает три мира (хук в процессе, скилл в subprocess, разбор пути), для которых иначе понадобились бы три разных алгоритма. `SessionFileRedirectHook._session_key` (`:307-314`) делегирует сюда — то есть это действительно тот модуль, который задаёт конвенцию, а не один из многих.

**Вердикт.** `Оставить`
**Обоснование.** Живой публичный контракт, документирован doctest-примерами (`:51-55,86-95`), покрыт `tests/` (doctest-стиль в docstring'ах). Единственная правка — косметическая переименование сентинела.

**Доказательства.** `workspace/hooks/session_file_redirect_hook.py:50` (`resolve_session_key`, `safe_session_key`), `legal_summarizer/scripts/…` (`resolve_session_key_for_subprocess`, `extract_session_key_from_path`), сам модуль. Внутри группы — потребитель хука; вне группы — скиллы.

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `safe_session_key` | 29-37 | каноническое имя папки | единая конвенция | `session_file_store.py` (нет — там своя копия!), `SessionFileRedirectHook._session_key`, `resolve_session_key*` | Оставить |
| `resolve_session_key` | 40-65 | ключ из `AgentHookContext` | хуки | `session_file_redirect_hook.py:307-314` | Оставить |
| `resolve_session_key_for_subprocess` | 68-106 | ключ из env/имени файла | standalone-скиллы | `workspace/skills/**` | Оставить |
| `extract_session_key_from_path` | 109-119 | raw-ключ из пути | обратный разбор | `workspace/skills/**` | Оставить |

Константы:

| Константа | Строки | Назначение | Вердикт |
|---|---|---|---|
| `_SAFE_RE` | 19 | regex «всё лишнее → `_`» | Оставить |
| `_SESSION_PATH_RE` | 20 | разбор `data_store/cache/sessions/<key>` | Оставить |
| `__nosession__` | 26 | сентинел «ключа нет» | Упростить |

- **`__nosession__` (`:26`) — плохое имя.** Двойное подчёркивание на уровне модуля не даёт ничего (mangling работает только внутри тела класса), но зато запрещает нормальный импорт из других модулей и провоцирует опечатки. Проверено: вне самого модуля не импортируется нигде. Правка: переименовать в `NO_SESSION` (или `_NOSESSION`, если оставить приватным) и обновить doctest'ы (`:55,94,106`).
- `resolve_session_key_for_subprocess` (`:68-106`) при отсутствии `SESSION_KEY` берёт **basename переданного файла** как ключ сессии. Для skill'а, обрабатывающего во втором вызове другой файл, это даст другой каталог кэша → переиспользование chunks перестаёт работать. Docstring (`:81-83`) это допускает («имена файлов не пересекаются»), но допущение неверно для повторной обработки разных файлов в одном пайплайне. Кросс-подсистемная находка для владельца `legal_summarizer`.
- `safe_session_key` оставляет точки внутри имени (`"foo.pdf"` → `"foo.pdf"`, см. doctest `:92`), тогда как `extract_session_key_from_path` их аккуратно выделяет. Не баг, но стоит знать при работе с именами, где точка значима.

---

## `workspace/utils/clean_text.py` — 44 LOC

**Назначение.** Каноническая вычистка NUL и литеральных `\u0000..\u0003` из любых данных, идущих в PostgreSQL.

**Что делает.** `clean_text` (`:25-44`) рекурсивно проходит строки, списки, кортежи и словари; байты и прочее не трогает. Применяется в двух местах: на входе (`Session.add_message` в `lib/services/runtime_patcher.py`) и на границе БД (`utils.db._sanitize_param`).

**Зачем нужен.** PostgreSQL отвергает NUL в text-литералах, а psycopg2 не может отправить литеральный `\u0000`..`\u0003`. Без этой функции произвольный бинарный вывод `exec` роняет запись сессии.

**Вердикт.** `Оставить`
**Обоснование.** 44 строки, один публичный символ, два потребителя, покрыт `tests/`. Упрощать нечего.

**Доказательства.** `workspace/utils/db.py:65` (`from utils.clean_text import clean_text` → `_sanitize_param`), `lib/services/runtime_patcher.py:1055`.

| Символ | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `CLEAN_ESCAPES` | 22 | список литеральных escape-последовательностей | — | `clean_text:32,34-35` | Оставить |
| `clean_text` | 25-44 | рекурсивная вычистка | граница БД | `utils/db.py:_sanitize_param`, `runtime_patcher.py:1055` | Оставить |

Замечание: ветка `tuple` (`:38-39`) возвращает `list`, то есть тип меняется; на текущих потребителях безвредно, но для `metadata` с кортежами это смена типа. Ключи словарей (`:41`) не чистятся — сознательно (они код, не данные).

---

## `workspace/utils/jsonb.py` — 52 LOC

**Назначение.** Декодирование значений jsonb/json из psycopg2 в питоновские структуры с защитой от битых типов.

**Что делает.** `decode_jsonb` (`:16-31`) и `decode_json_list` (`:34-52`) — функции, регистрируемые как `loads=` в `psycopg2.extras.register_json` (регистрация — на стороне вызывающих: `lib/channels/postgres_channel.py:47`, `streamlit_app.py:107-108`).

**Зачем нужен.** Без них psycopg2 отдаёт `jsonb` как Python-строку, и прикладной код получает строку вместо dict/list — тихая ошибка уровня данных.

**Вердикт.** `Оставить`
**Обоснование.** Обе функции живые; запись в `dead_symbols.md` — ложное срабатывание (см. ниже).

**Доказательства.** `lib/channels/postgres_channel.py:47` (`from utils.jsonb import decode_jsonb, decode_json_list`), `streamlit_app.py:107-108`.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `decode_jsonb` | 16-31 | dict/str → dict | `loads=` для jsonb | `postgres_channel.py`, `streamlit_app.py` | Оставить |
| `decode_json_list` | 34-52 | list/str → list | `loads=` для json-колонок | там же | Оставить |

Замечания (не меняющие вердикт):
- **`decode_jsonb` (`:16-31`) не соответствует своему docstring.** Заявлено «любое другое → `{}` (защита от битых типов)», но ветка «уже декодированный список» (`:24-26`) делает `dict(val)`, что для `[1, 2]` даёт `TypeError`, а для `5` — тоже `TypeError`. Ветка «невалидный JSON» (`:28-30`) не ловит `json.JSONDecodeError`. То есть функция, названная безопасной, может поднять исключение. Правка: обернуть обе ветки в `try/except (TypeError, ValueError)` → `{}`.
- `decode_json_list` (`:34-52`) возвращает `json.loads(val)` без проверки типа результата: колонка, содержащая `5`, вернёт `5` вопреки аннотации `-> list`. Проверка `isinstance(result, list)` отсутствует.
- Дублирования внутри `workspace/utils/db.py` нет — там jsonb не трогается.

**Почему `dead_symbols.md` ошибся (важно для других аудиторов).** Запись «0 ссылок» получена поиском по префиксу `workspace.utils`. Продуктовый код импортирует эти модули по алиасу `utils.*`, потому что `gateway.py:510-511` и `cli_agent.py:282-283` добавляют `<repo>/workspace` в `sys.path[0]`. Один и тот же файл доступен под двумя именами (`utils.db` и `workspace.utils.db`), причём это **два разных объекта модуля** в `sys.modules` со своими копиями module-level состояния. Это ложное срабатывание касается **всех** `workspace/utils/*` в общем отчёте статики, а не только этого файла; дедупликация имён — сквозная кросс-подсистемная находка.

---

## `workspace/utils/media.py` — 259 LOC

**Назначение.** Сериализация/десериализация поля `media` сообщений: data-URL ↔ файловая ссылка, нормализация для БД и подсказки для UI.

**Что делает.** `entry_from_data_url` (`:72-86`) разбирает data-URL в storage-запись. `serialize` (`:89-131`) — главный конвертер: http(s)-ссылки проходят насквозь, `data:`-URL декодируется в `file_id` (если совпадает с уже сохранённым) либо в `path`/`name`/`size`, а локальные файлы **читаются целиком** и кодируются в новый `data:`-URL. `deserialize` (`:134-180`) — обратное. `normalize_storage_entry` (`:205-220`) приводит legacy-формы к канону, `resolve_paths_and_hints` (`:183-202`) строит пути+подсказки для системного промпта, `read_for_ui` (`:223-247`) — превью для Streamlit.

**Зачем нужен.** `media` — единственный способ передать агенту файл; без `serialize` вложение не попадёт в запрос, без `deserialize` — в ответ.

**Вердикт.** `Оставить`
**Обоснование.** Все девять символов используются и покрыты `tests/test_utils_media.py`; бриф здесь совпадает с реальностью.

**Доказательства.** `lib/channels/postgres_channel.py`, `lib/channels/redis_channel.py`, `lib/services/db_logging_bus.py`, `lib/hooks/recent_files_hook.py`, `streamlit_app.py:105-110`, `tests/test_utils_media.py`.

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `data_url_info` | 40-57 | mime/size из data-URL | без декодирования | `serialize` | Оставить |
| `_storage_entry` | 60-69 | сборка словаря записи | — | `serialize`, `normalize_storage_entry` | Оставить |
| `entry_from_data_url` | 72-86 | data-URL → запись | — | `serialize` | Оставить |
| `serialize` | 89-131 | нормализация media для БД/запроса | запись вложений | каналы, хуки | Оставить |
| `deserialize` | 134-180 | нормализация media из БД | чтение | каналы, Streamlit | Оставить |
| `resolve_paths_and_hints` | 183-202 | пути + подсказки агенту | модель знает, что за файлы | хуки/каналы | Оставить |
| `normalize_storage_entry` | 205-220 | приведение legacy-форм | миграция формата | `serialize` | Оставить |
| `read_for_ui` | 223-247 | превью для Streamlit | UI | `streamlit_app.py` | Оставить |
| `_STORAGE_KEYS` | 37 | константа в `__all__` | — | — | Упростить |

Замечания:
- **`serialize` (`:89-131`) — неограниченное чтение файла в память.** Для не-`data:` пути он делает `p.read_bytes()` (`:126`) и затем base64-энкодит содержимое внутрь `data:`-URL, который уходит в `jsonb`-колонку БД. Файл в 100 МБ превращается в ~133 МБ строки в памяти и в jsonb-колонке. Ни в `media.py`, ни в `session_file_store.save_attachment` (который пишет на диск) нет лимита размера. Правка: порог (напр. 10 МБ), выше которого — сохранять на диск и отдавать `path`, а не `data:`.
- **`deserialize` (`:134-180`) — непоследовательный тип возврата.** При `dict` возвращает словарь, при `str` — голую строку `info["path"]` (`:172`). Одна функция, два типа на входе одного и того же типа данных; задокументировано (`:137-139`), но это ловушка для вызывающего. Правка: всегда возвращать dict.
- `entry_from_data_url` (`:72-86`) при неуспешном разборе всё равно формирует имя из mime (`:80-84`) и затем подставляет `mime=""`, `size=0` (`:84-85`) — имя остаётся осмысленным, но запись указывает на несуществующий файл с валидным расширением. Лучше возвращать `None`.
- `_STORAGE_KEYS` (`:37`) включён в `__all__` (приватное имя в публичном экспорте) — косметика.

---

## `workspace/utils/__init__.py`

Пустой пакет-маркер. Импортируется как `workspace.utils` (тестами) и как `utils` (продуктом). Вердикт `Оставить` — необходим для обоих путей импорта.

---

## Сводка вердиктов по файлам

| Файл | Оставить | Упростить | Удалить | Слить |
|---|---:|---:|---:|---:|
| `workspace/tools/compact_context.py` | 11 | 3 | 1 | 0 |
| `workspace/tools/history_search_tool.py` | 13 | 1 | 0 | 0 |
| `workspace/tools/legal_summarizer_query.py` | 13 | 3 | 0 | 0 |
| `workspace/tools/example.py` | 10 | 1 | 0 | 0 |
| `workspace/hooks/session_file_redirect_hook.py` | 22 | 2 | 0 | 0 |
| `workspace/hooks/recent_files_hook.py` | 6 | 0 | 0 | 0 |
| `workspace/hooks/debug_stream_diag.py` | 0 | 0 | 8 | 0 |
| `workspace/utils/session_file_store.py` | 15 | 4 | 1 | 1 |
| `workspace/utils/office_files.py` | 19 | 0 | 0 | 0 |
| `workspace/utils/structure_cache.py` | 0 | 0 | 3 | 0 |
| `workspace/utils/session_key.py` | 4 | 1 | 0 | 0 |
| `workspace/utils/clean_text.py` | 2 | 0 | 0 | 0 |
| `workspace/utils/jsonb.py` | 2 | 0 | 0 | 0 |
| `workspace/utils/media.py` | 8 | 1 | 0 | 0 |
| `workspace/utils/db.py` | 95 | 3 | 0 | 0 |
| `workspace/utils/__init__.py` | 1 | 0 | 0 | 0 |
| `workspace/tools/__init__.py` | 0 | 0 | 0 | 0 |

`workspace/tools/__init__.py` (24 строки) — модульный docstring, фиксирующий четыре конвенции регистрации (`config_key`/`config_cls`/`enabled`/`create`, чтение через `ctx._settings_ref` и не через `ctx.config`, сигнатуры `execute`). Все четыре подтверждены кодом tool'ов; вердикт `Оставить`. Единственная неточность: `:11-12` говорит, что секция живёт в `config.json` «или `gateway.<name>.*`» — фактически `compact_context` читает `gateway.compact.*` (не `gateway.compact_context.*`), то есть имя секции и имя класса не совпадают; это стоит оговорить, иначе третий автор скопирует неверный ключ.

---

## Кросс-подсистемные находки

1. **Двойной путь импорта `workspace/utils/*` → ложные «мёртвые» символы и риск дублей модулей.** `gateway.py:510-511` и `cli_agent.py:282-283` кладут `<repo>/workspace` в `sys.path[0]`, поэтому продукт импортирует `utils.db`, а тесты — `workspace.utils.db`. Один файл = два объекта в `sys.modules` со **своими копиями module-level состояния** (в `db.py` это `_manager`, `_dsn`; в `session_file_store.py` — константы; в `office_files.py` — nothing, но кэши импорта тоже разойдутся). Оба трека живы: `postgres_channel.py:42-46` (`utils.db`) и `tests/test_utils_db.py` (`utils.db`), против `tests/test_application_context.py` (`workspace.utils.session_file_store`). Пока в проекте живут оба трека, любой процесс, где смешаются оба импорта (например, тест, поднимающий `ApplicationContext` и импортирующий `workspace.utils.db`), получит **два независимых пула соединений**. Минимальная правка: зафиксировать один трек (рекомендую `workspace.utils.*`, он переживёт переезд репозитория) и переименовать алиас `utils.*` на один пакет-реэкспорт.
2. **Путь хранения вложений Postgres-канала не совпадает с путями, которые его читают.** Подробно в ключевой находке №1. Затрагивает `lib/channels/postgres_channel.py:67-80,209-211`, `lib/channels/redis_channel.py:104-110,146-149`, `streamlit_app.py:130-134`, `workspace/hooks/session_file_redirect_hook.py:223-239`. Redis-канал выключен (`project.json:83` `enabled: false`), поэтому там дефект латентный; Postgres-канал — боевой. Первопричина — в моём файле (`session_file_store.py:147-148`), но фикс требует правки обоих `_resolve_sfs_base`.
3. **Инвентарь runtime описывает несуществующие ключи конфига.** `lib/services/runtime_inventory.py:136` — `config_key="tools.compact_context.enable"`, фактически `gateway.compact.enabled`; `:151` — `config_key=None` для `legal_summarizer_query`, хотя ключ есть (`config.json:723`); имя спека `"ExampleTool"` (`:153`-блок) не совпадает с регистрируемым `"example_tool"`. Инструмент `tools/diagnose_startup.py` использует эти строки, поэтому расхождение попадает прямо в startup-баннер.
4. **`RuntimePatcher` vs `project_tool_loader` в документации.** `workspace/tools/compact_context.py:30-32` и `workspace/tools/example.py:20-25` называют `RuntimePatcher.patch_project_tools` тем, кто внедряет `ctx._settings_ref` и «пропускает» `_plugin_discoverable`. Фактически инжектор — `lib/services/project_tool_loader.py::_build_tool_context`, а `_discover` флаг не проверяет. Обе строки вводят в заблуждение.
5. **`workspace/skills/legal_summarizer/SKILL.md:3,11,14,309-310` предписывает вызов несуществующей `office_files.extract_metadata()`.** Функция переименована в `summarize`; тест `tests/test_skill_legal_summarizer.py:240` проверяет лишь наличие подстроки в тексте и потому зелёный. Владельцу скилла: заменить на `summarize()` и ужесточить тест до проверки существования символа.
6. **`tools/extract_office_structure.py:10` импортирует удалённую `office_files.extract_structure`** → dev-утилита нерабочая. Владельцу `tools/`: удалить утилиту либо переписать на существующий `extract_text`/`detect_format`.
7. **Дублирование `resolve_dsn`.** `workspace/utils/db.py:867-879` и `tools/migrate.py` (dev-слой). Пока оба читают `${DATABASE_URL}`, расхождение возможно; кандидат на один общий резолвер. Вне моей подсистемы.
8. **Совпадение имён `get_stats`** в `utils.db` (метрики пула) и `DbLoggingService.get_stats` (счётчики журнала) — при разборе логов их регулярно путают; в `/health` и в `session_cold_sync_service.py:637` используется именно пуловый.
9. **Утечка plaintext-контента в лог на диске** (`workspace/hooks/debug_stream_diag.py`, см. ключевую находку №2). Файл `workspace/data_store/debug_stream.log` не имеет аналога в политике хранения и не упоминается ни в `AGENTS.md`, ни в `docs/DATABASE.md`.
10. **`session_key.resolve_session_key_for_subprocess` как источник cache-isolation для скиллов** использует basename файла при отсутствии `SESSION_KEY` (`session_key.py:99-105`). Для `legal_summarizer`, обрабатывающего несколько документов подряд, это даёт разные каталоги кэша и отключает переиспользование chunks. Требует внимания владельца скилла: передавать ключ из окружения гарантированно.
