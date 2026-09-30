# Work brief `08-workspace-plugins`

Product files: **18**, LOC: **4240**

## `workspace/utils/db.py` — 1063 LOC (code 811)
- module: `workspace.utils.db`
- docstring: Единый коннектор к PostgreSQL / Greenplum через psycopg2. Архитектура — «одна очередь + пул соединений» (вместо connect-per-op): * все подсистемы (PostgresChannel, PGSessionManager, DbLoggingService, PgDuckDbSyncService,
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 8, module functions: 22

### class `_JobResult` — lines 121-142 (22 LOC), 4 methods
- bases: object
- decorators: —
- docstring: Однократный контейнер результата: воркер кладёт значение/ошибку.
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 124-127 | `(self) -> None` | 1 | 0 | 6 | — |
| `set_result` | 129-131 | `(self, value: Any) -> None` | 1 | 0 | 0 | — |
| `set_error` | 133-135 | `(self, exc: BaseException) -> None` | 1 | 0 | 0 | — |
| `get` | 137-142 | `(self, timeout: float | None=None) -> Any` | 3 | 0 | 149 | — |

### class `_Job` — lines 145-161 (17 LOC), 1 methods
- bases: object
- decorators: —
- docstring: Задача для воркера. ``lease_id != 0`` — задача эксклюзивной транзакции.
- name referenced in 2 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 150-161 | `(self, fn: Callable[[Any], Any], lease_id: int=0, result: _JobResult | None=None, tag: str` | 2 | 0 | 6 | — |

### class `PoolTimeoutError` — lines 189-190 (2 LOC), 0 methods
- bases: RuntimeError
- decorators: —
- docstring: Не удалось получить свободное соединение пула за pool_timeout.
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `_Worker` — lines 198-373 (176 LOC), 10 methods
- bases: threading.Thread
- decorators: —
- docstring: — NONE —
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 199-217 | `(self, manager: DBManager, index: int) -> None` | 1 | 0 | 6 | — |
| `_ensure_connected` | 221-239 | `(self) -> bool` | 6 | 1 | 1 | Подключиться (если нет) с backoff. True — соединение живо. При неудаче ``connect_max_retries`` попыток — сдаём |
| `_connect_with_backoff` | 241-273 | `(self) -> bool` | 7 | 1 | 1 | Цикл подключения с экспоненциальным backoff (вызывается под ``_conn_lock``). |
| `_drop_connection` | 275-283 | `(self) -> None` | 3 | 2 | 1 | — |
| `_open_cursor` | 287-291 | `(self, conn: Any, args: tuple, kwargs: dict) -> int` | 1 | 0 | 0 | — |
| `_cursor` | 293-297 | `(self, cid: int) -> Any` | 3 | 0 | 0 | — |
| `_close_cursor` | 299-305 | `(self, cid: int) -> None` | 3 | 0 | 0 | — |
| `run` | 309-320 | `(self) -> None` | 6 | 0 | 87 | — |
| `_activity_print` | 322-336 | `(self, line: str) -> None` | 4 | 2 | 3 | Напечатать строку активности db-worker, если флаг включён. |
| `_execute_job` | 338-373 | `(self, job: _Job) -> None` | 11 | 1 | 1 | — |

### class `DBManager` — lines 381-658 (278 LOC), 14 methods
- bases: object
- decorators: —
- docstring: — NONE —
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 382-419 | `(self, dsn: str='') -> None` | 11 | 0 | 6 | — |
| `start` | 423-434 | `(self) -> DBManager` | 5 | 1 | 21 | — |
| `shutdown` | 436-447 | `(self) -> None` | 6 | 0 | 4 | — |
| `_spawn_worker` | 449-453 | `(self) -> _Worker` | 1 | 3 | 1 | — |
| `_maybe_shrink` | 455-465 | `(self, worker: _Worker) -> bool` | 6 | 0 | 1 | — |
| `_take_job` | 467-513 | `(self, worker: _Worker) -> _Job | None` | 19 | 0 | 0 | Выбрать задачу, которую может выполнить этот воркер. Воркер в эксклюзивной транзакции берёт только задачи свое |
| `_requeue` | 515-518 | `(self, job: _Job) -> None` | 2 | 0 | 0 | — |
| `_submit` | 520-543 | `(self, job: _Job) -> _JobResult` | 9 | 2 | 1 | Положить задачу в общую очередь и дождаться результата. |
| `_ensure_started` | 545-547 | `(self) -> None` | 2 | 2 | 1 | — |
| `_acquire_lease` | 551-604 | `(self, tag: str='') -> int` | 15 | 0 | 1 | — |
| `_begin_tx` | 607-608 | `(conn: Any) -> None` | 1 | 1 | 1 | — |
| `_end_tx` | 611-616 | `(conn: Any, commit: bool) -> None` | 2 | 1 | 1 | — |
| `_release_lease` | 618-631 | `(self, lease_id: int, commit: bool, tag: str='') -> None` | 5 | 0 | 2 | — |
| `get_stats` | 633-658 | `(self) -> dict` | 10 | 0 | 7 | — |

### class `_CursorProxy` — lines 691-751 (61 LOC), 15 methods
- bases: object
- decorators: —
- docstring: Прокси psycopg2-курсора: каждая операция — job на соединение аренды.
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 694-696 | `(self, proxy: _ConnectionProxy, cid: int) -> None` | 1 | 0 | 6 | — |
| `_run` | 698-699 | `(self, fn: Callable[[Any], Any]) -> Any` | 1 | 14 | 1 | — |
| `__enter__` | 701-702 | `(self) -> _CursorProxy` | 1 | 0 | 4 | — |
| `__exit__` | 704-705 | `(self, *exc: Any) -> None` | 1 | 0 | 1 | — |
| `connection` | 708-709 | `(self) -> _ConnectionProxy` | 1 | 0 | 1 | — |
| `description` | 712-713 | `(self) -> Any` | 1 | 0 | 14 | — |
| `rowcount` | 716-717 | `(self) -> int` | 1 | 0 | 3 | — |
| `statusmessage` | 720-721 | `(self) -> str | None` | 1 | 0 | 1 | — |
| `execute` | 723-730 | `(self, sql: str, params: Any=None) -> None` | 1 | 0 | 43 | — |
| `mogrify` | 732-734 | `(self, sql: str, params: Any=None) -> bytes` | 1 | 0 | 1 | — |
| `__iter__` | 736-739 | `(self) -> Any` | 1 | 0 | 0 | — |
| `fetchone` | 741-742 | `(self) -> Any` | 1 | 0 | 12 | — |
| `fetchall` | 744-745 | `(self) -> list` | 1 | 1 | 21 | — |
| `fetchmany` | 747-748 | `(self, size: int=100) -> list` | 1 | 0 | 2 | — |
| `close` | 750-751 | `(self) -> None` | 1 | 1 | 30 | — |

### class `_ConnectionProxy` — lines 754-821 (68 LOC), 9 methods
- bases: object
- decorators: —
- docstring: Прокси соединения внутри транзакции (синхронный API).
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 757-760 | `(self, manager: DBManager, lease_id: int) -> None` | 1 | 0 | 6 | — |
| `_run` | 762-765 | `(self, fn: Callable[[Any], Any], tag: str | None=None) -> Any` | 3 | 14 | 1 | — |
| `encoding` | 768-769 | `(self) -> str` | 1 | 0 | 3 | — |
| `cursor` | 771-776 | `(self, *args: Any, **kwargs: Any) -> _CursorProxy` | 1 | 0 | 20 | — |
| `execute` | 778-786 | `(self, sql: str, *args: Any, _tag: str | None=None) -> Any` | 4 | 0 | 43 | — |
| `fetch` | 788-796 | `(self, sql: str, *args: Any, _tag: str | None=None) -> list` | 5 | 0 | 9 | — |
| `fetchrow` | 798-807 | `(self, sql: str, *args: Any, _tag: str | None=None) -> dict | None` | 5 | 1 | 8 | — |
| `fetchone` | 809-810 | `(self, sql: str, *args: Any, _tag: str | None=None) -> dict | None` | 1 | 0 | 12 | — |
| `fetchval` | 812-821 | `(self, sql: str, *args: Any, _tag: str | None=None) -> Any` | 5 | 0 | 3 | — |

### class `_AsyncConnectionWrapper` — lines 824-840 (17 LOC), 5 methods
- bases: object
- decorators: —
- docstring: Обёртка синхронного прокси для async-кода (через asyncio.to_thread).
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 827-828 | `(self, proxy: _ConnectionProxy) -> None` | 1 | 0 | 6 | — |
| `fetch` | 830-831 | `(self, sql: str, *args: Any) -> list` | 1 | 0 | 9 | — |
| `fetchrow` | 833-834 | `(self, sql: str, *args: Any) -> dict | None` | 1 | 1 | 8 | — |
| `execute` | 836-837 | `(self, sql: str, *args: Any) -> Any` | 1 | 0 | 43 | — |
| `fetchval` | 839-840 | `(self, sql: str, *args: Any) -> Any` | 1 | 0 | 3 | — |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `set_pool_config` | 101-113 | `(cfg: dict) -> None` | 5 | 4 | Переопределить параметры пула (min_conn/max_conn/pool_timeout/...). Применяется до первого вызова воркера; уже |
| `_caller_tag` | 164-186 | `(frames_back: int=2, chain: int=0) -> str` | 5 | 1 | Короткая метка вызывающей стороны job'а: ``базовое_имя_файла:строка``. Фрейм(0) — сама ``_caller_tag``, фрейм( |
| `_sanitize_param` | 666-678 | `(value: Any) -> Any` | 2 | 1 | Страховка: вычистить недопустимые для PostgreSQL управляющие символы. Единая глобальная точка санитизации всех |
| `_sanitize_params` | 681-688 | `(params: Any) -> Any` | 6 | 1 | — |
| `_get_manager` | 851-859 | `() -> DBManager` | 5 | 3 | — |
| `resolve_dsn` | 867-879 | `() -> str` | 8 | 4 | Вернуть DSN: явный через configure(), иначе channels.postgres.dsn. |
| `configure` | 882-888 | `(dsn: str) -> None` | 4 | 19 | Настроить DSN для подключения к БД (идемпотентно). |
| `start` | 891-893 | `() -> DBManager` | 1 | 49 | Запустить пул (воркеры подключаются лениво при первой задаче). |
| `shutdown` | 896-901 | `() -> None` | 3 | 6 | Остановить пул и закрыть все соединения. |
| `get_stats` | 904-905 | `() -> dict` | 1 | 8 | — |
| `probe_connections` | 908-937 | `(count: int | None=None, timeout: float | None=None) -> None` | 6 | 2 | Прогреть пул: заставить воркеров реально подключиться к БД. Воркеры пула подключаются **лениво** — при первой  |
| `run` | 940-947 | `(fn: Callable[[Any], Any]) -> Any` | 2 | 100 | Выполнить ``fn(conn)`` на свободном соединении пула (без транзакции). ``fn`` получает сырой psycopg2-conn в во |
| `execute` | 955-964 | `(sql: str, *args: Any, _tag: str | None=None) -> str | None` | 5 | 47 | Выполнить INSERT/UPDATE/DELETE, вернуть command tag. |
| `fetch` | 967-976 | `(sql: str, *args: Any, _tag: str | None=None) -> list` | 6 | 20 | Выполнить SELECT, вернуть список строк как dict. |
| `fetchone` | 979-989 | `(sql: str, *args: Any, _tag: str | None=None) -> dict | None` | 6 | 16 | Выполнить SELECT, вернуть одну строку как dict или None. |
| `fetchval` | 992-1002 | `(sql: str, *args: Any, _tag: str | None=None) -> Any` | 6 | 6 | Выполнить SELECT, вернуть первую колонку первой строки или None. |
| `transaction` | 1006-1022 | `()` | 2 | 6 | Синхронная транзакция: эксклюзивная аренда соединения пула. Внутри контекста возвращается прокси соединения; в |
| `async_execute` | 1030-1031 | `(sql: str, *args: Any) -> str | None` | 1 | 7 | — |
| `async_fetch` | 1034-1035 | `(sql: str, *args: Any) -> list` | 1 | 7 | — |
| `async_fetchone` | 1038-1039 | `(sql: str, *args: Any) -> dict | None` | 1 | 7 | — |
| `async_fetchval` | 1042-1043 | `(sql: str, *args: Any) -> Any` | 1 | 7 | — |
| `async_transaction` | 1047-1063 | `()` | 2 | 7 | Асинхронная транзакция (см. ``transaction``). Возвращает async-обёртку прокси: ``await conn.fetch(...)`` и т.п |

## `workspace/tools/history_search_tool.py` — 602 LOC (code 507)
- module: `workspace.tools.history_search_tool`
- docstring: ``history_search`` — generic-инструмент агента для поиска по истории. Реализует запрос пользователя «что было в старых сообщениях / что агент уже делал»: ищет события в долговечном журнале ``agent_gateway_logs`` (пишется
- static importers (2): `tests/test_history_search_benchmark.py`, `tests/test_history_search_tool.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 2, module functions: 3

### class `HistorySearchToolConfig` — lines 78-83 (6 LOC), 0 methods
- bases: BaseModel
- decorators: —
- docstring: Конфиг секции ``tools.history_search`` в ``config.json``.
- name referenced in 3 file(s); tests: 2

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `HistorySearchTool` — lines 194-547 (354 LOC), 9 methods
- bases: Tool
- decorators: tool_parameters({'type': 'object', 'properties': {'query': {'type': 'string', 'description': "Подстрока для поиска по истории (ILIKE, регистронезависимо). Ищется в summary события и в JSON-теле payload (включая пути файлов, doc_id, текст диалога). Можно оставить пустым, если нужна фильтрация только по event_type / времени. Примеры: query='договор' (найти упоминания договора), query='.pdf' (найти файлы по расширению), query='риски' (найти обсуждение рисков в llm_call)."}, 'event_type': {'type': 'string', 'enum': ['context_compacted', 'tool_call', 'tool_result', 'llm_call', 'run_finished', 'turn_completed', 'subagent_run_finished', 'inbound'], 'description': 'Тип события (опционально). Доступные типы, реально пишущиеся в журнал:\n  • context_compacted — факт сжатия контекста (что именно заархивировано, сколько токенов до/после).\n  • tool_call — вызов инструмента агентом, включая аргументы (пути файлов, переданные пользователем или агентом, лежат здесь).\n  • tool_result — результат инструмента (может содержать пути созданных файлов, doc_id и пр.).\n  • llm_call — полный промпт итерации LLM, включая вопросы пользователя (поиск по тексту диалога).\n  • run_finished — прошлый финальный ответ агента пользователю (содержит final_content, tools_used). Для пользовательского контента это основной тип.\n  • turn_completed — метрики оборота: latency_ms, outcome, usage_tokens, runtime_model. НЕ содержит final_content — только статистика. Для контента используйте run_finished.\n  • subagent_run_finished — ответ под-агента.\n  • inbound — входящее сообщение пользователя.\nЕсли не указан — ищутся все типы. Для поиска файлов используй tool_call/tool_result (аргументы и результаты tool-вызовов) и llm_call (текст диалога), а НЕ выдуманные типы file_* / document_summarized.'}, 'tool_name': {'type': 'string', 'description': "Имя инструмента для фильтрации (опционально). Применимо только при event_type='tool_call' или event_type='tool_result'. Удобно для поиска истории конкретного инструмента: tool_name='compact_context' найдёт все его вызовы и результаты. Соответствует колонке ``name`` в ``agent_gateway_logs``. Если не указан — фильтрация по имени инструмента не применяется (поиск по всем инструментам в рамках event_type)."}, 'since': {'type': 'string', 'description': 'Нижняя граница времени (ISO-8601), опционально.'}, 'until': {'type': 'string', 'description': 'Верхняя граница времени (ISO-8601), опционально.'}, 'session_scope': {'type': 'string', 'enum': ['current', 'all'], 'description': "Область поиска: 'current' — только текущая сессия (по умолчанию; используй, когда пользователь ссылается на 'тот файл из нашего разговора'), 'all' — по всем сессиям (кросс-чатовый поиск, когда неизвестно, в какой сессии было событие).", 'default': 'current'}, 'limit': {'type': 'integer', 'description': 'Максимум событий в ответе (по умолчанию из конфига).', 'minimum': 1}, 'offset': {'type': 'integer', 'description': 'Сколько первых событий пропустить в сортировке (пагинация). Дефолт 0 — первая страница. Продолжать страницы через поле next_offset из предыдущего ответа, НЕ через ``offset + limit`` (при results_truncated=true часть событий была отброшена, и арифметика offset+limit пропустит их).', 'minimum': 0, 'default': 0}}, 'required': []})
- docstring: Искать события в долговечном журнале агента (переживает compaction).
- name referenced in 2 file(s); tests: 2

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 199-200 | `(self, *, config: HistorySearchToolConfig) -> None` | 1 | 0 | 6 | — |
| `config_cls` | 203-204 | `(cls)` | 1 | 2 | 3 | — |
| `_read_settings_section` | 207-229 | `(cls, ctx: Any) -> dict[str, Any]` | 8 | 2 | 3 | Прочитать ``tools.history_search`` из ``ctx._settings_ref``. |
| `enabled` | 232-234 | `(cls, ctx: Any) -> bool` | 2 | 0 | 9 | — |
| `create` | 237-243 | `(cls, ctx: Any) -> Tool` | 2 | 0 | 15 | — |
| `name` | 246-247 | `(self) -> str` | 1 | 0 | 91 | — |
| `description` | 250-291 | `(self) -> str` | 1 | 0 | 14 | — |
| `execute` | 293-541 | `(self, *, query: str | None=None, event_type: str | None=None, tool_name: str | None=None,` | 47 | 0 | 43 | — |
| `_error` | 543-547 | `(self, error_type: str, message: str) -> str` | 1 | 5 | 3 | — |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_current_session_key` | 550-556 | `() -> str | None` | 2 | 3 | — |
| `_current_user_id` | 559-588 | `() -> str | None` | 6 | 2 | Получить идентификатор текущего пользователя из RequestContext. Единственная точка обращения к ``RequestContex |
| `_log_table` | 591-602 | `() -> tuple[str, str]` | 10 | 1 | — |

## `workspace/utils/session_file_store.py` — 431 LOC (code 366)
- module: `workspace.utils.session_file_store`
- docstring: — NONE —
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 5

### class `SessionFileStore` — lines 127-431 (305 LOC), 11 methods
- bases: object
- decorators: —
- docstring: — NONE —
- name referenced in 15 file(s); tests: 11

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 128-154 | `(self, base_dir: Path, max_files: int=0, max_age_hours: int=0, attachments_subdir: str='at` | 1 | 0 | 6 | Инициализирует хранилище сессий. Аргументы: base_dir: Базовая директория (внутри неё создаются cache/sessions  |
| `_get_session_dir` | 156-162 | `(self, session_key: str) -> Path` | 1 | 6 | 2 | Возвращает директорию сессии, создавая её при необходимости. |
| `_resolve_attachments_dir` | 164-174 | `(self, session_key: str) -> Path` | 1 | 1 | 1 | Возвращает каталог вложений сессии, создавая при необходимости. Лежит рядом с ``results/``: ``{base}/{safe_key |
| `_sanitize_filename` | 177-182 | `(name: str | None) -> str` | 2 | 2 | 1 | Очистить имя файла: оставить ``[\w.-]``, пробелы, остальное в ``_``. |
| `_guess_ext_from_mime` | 185-186 | `(mime_type: str) -> str` | 2 | 2 | 1 | — |
| `save_attachment` | 188-257 | `(self, session_key: str, data_url: str | None, *, filename: str | None=None) -> dict | Non` | 14 | 0 | 3 | Сохранить вложение (data URL или путь/сырые байты) в каталоге сессии. Принимает: * ``data_url`` вида ``data:<m |
| `_ensure_metadata` | 259-271 | `(self, session_key: str) -> None` | 2 | 2 | 2 | Создаёт metadata.json для сессии, если его ещё нет. |
| `_find_existing_for_hash` | 273-289 | `(self, session_key: str, content_hash: str, ext: str) -> str | None` | 5 | 1 | 1 | Вернуть путь уже сохранённого файла с таким хешем содержимого. Сканирует ``results/`` сессии в поисках файла с |
| `save` | 291-361 | `(self, session_key: str, content: str, source_tool: str, ext: str='.json', dedupe: bool=Tr` | 5 | 0 | 14 | Сохраняет содержимое как файл результата в сессии. Аргументы: session_key: Ключ сессии. content: Содержимое фа |
| `cleanup` | 363-418 | `(self, session_key: str) -> None` | 19 | 1 | 3 | Удаляет устаревшие файлы результатов согласно лимитам max_files / max_age_hours. |
| `archive_session` | 420-431 | `(self, session_key: str) -> bool` | 3 | 0 | 1 | Перемещает директорию сессии в архив. Возвращает True, если архивация выполнена, иначе False. |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `safe_session_key` | 27-29 | `(key: str) -> str` | 1 | 6 | Заменяет символы, небезопасные для имён директорий, на ``_``. |
| `guess_ext_from_mime` | 32-54 | `(mime_type: str, default_ext: str='.bin') -> str` | 5 | 2 | Единая точка: от MIME-типа к расширению файла. Эквивалент прежних ``SessionFileStore._guess_ext_from_mime`` и  |
| `_csv_val` | 57-59 | `(v)` | 2 | 1 | Возвращает пустую строку для None, иначе строковое представление значения. |
| `prepare_content` | 62-80 | `(content: str) -> tuple[str, str]` | 5 | 6 | Нормализует содержимое результата инструмента и выбирает расширение файла. Возвращает ``(content, ext)``, где  |
| `_try_convert_to_csv` | 83-124 | `(data) -> str | None` | 30 | 2 | Пытается преобразовать данные (list/dict) в CSV с BOM. Проверяет несколько распространённых структур: список с |

## `workspace/hooks/session_file_redirect_hook.py` — 417 LOC (code 326)
- module: `workspace.hooks.session_file_redirect_hook`
- docstring: SessionFileRedirectHook — AgentHook, перенаправляющий записи сессии. Подключается через ``workspace/hooks/scan_and_register`` (hook_loader.py). Срабатывает на инструментах ``write``/``edit``/``create_file``/``write_file`
- static importers (1): `tests/test_session_file_redirect_hook.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 0

### class `SessionFileRedirectHook` — lines 102-417 (316 LOC), 13 methods
- bases: AgentHook
- decorators: —
- docstring: Перенаправляет write/edit агентских файлов в data_store/cache/sessions/.
- name referenced in 1 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 105-108 | `(self, workspace_dir: str | None=None) -> None` | 2 | 0 | 6 | — |
| `before_execute_tool` | 114-128 | `(self, context: Any, tool_call: Any, tool: Any, params: Any) -> None` | 4 | 0 | 4 | — |
| `_redirect_write` | 130-167 | `(self, tool_call: Any, params: dict, context: Any) -> None` | 9 | 1 | 1 | Перенаправить целевой путь write-инструмента в session-папку. |
| `_redirect_media` | 169-221 | `(self, tool_call: Any, params: dict, context: Any) -> None` | 8 | 1 | 1 | Перенаправить пути в ``media`` тула ``message`` в session-папку. ``MessageTool`` в nanobot резолвит относитель |
| `_media_entry_exists` | 223-239 | `(self, entry: Any) -> bool` | 6 | 1 | 1 | Проверить, что media-элемент существует так, как его увидит ``MessageTool._resolve_media`` (относительный путь |
| `_resolve_media_entry` | 241-288 | `(self, entry: Any, session_sub: Path) -> str | None` | 11 | 1 | 1 | Найти реальный файл сессии для media-элемента. Возвращает абсолютный путь, если файл найден в одной из известн |
| `_tool_name` | 295-297 | `(tool_call: Any) -> str` | 3 | 1 | 1 | — |
| `_extract_path` | 300-304 | `(params: dict) -> str | None` | 5 | 1 | 2 | — |
| `_session_key` | 307-314 | `(context: Any) -> str` | 1 | 2 | 1 | Получить стабильный ключ сессии из контекста. Делегирует ``workspace.utils.session_key.resolve_session_key`` — |
| `_is_allowed` | 316-330 | `(self, target: str) -> bool` | 5 | 1 | 1 | Белый список: не перенаправляем служебные файлы и уже легитимные пути. |
| `_normalize` | 332-350 | `(self, target: str) -> str` | 5 | 1 | 1 | Привести путь к POSIX-виду относительно workspace. Поддерживает пути в стиле POSIX (/foo/bar) и Windows (C:\fo |
| `_redirect` | 352-387 | `(self, target: str, session_key: str) -> str | None` | 7 | 1 | 1 | Собрать новый путь в ``data_store/cache/sessions/<session_key>/``. Имя папки берётся из ``context.session_key` |
| `_safe_leaf` | 390-417 | `(cls, target: str) -> str` | 7 | 1 | 1 | Извлечь имя файла и сделать его кросс-платформенно валидным. - Убирает запрещённые символы (``<>:"/\\|?*\0``).  |

## `workspace/tools/legal_summarizer_query.py` — 360 LOC (code 300)
- module: `workspace.tools.legal_summarizer_query`
- docstring: ``legal_summarizer_query`` — follow-up tool по сохранённой operation_id. Регистрируется автоматически через ``RuntimePatcher.patch_project_tools`` (см. ``lib/services/runtime_patcher.py``). Зачем: без этого tool'а агент 
- static importers (2): `tests/test_legal_summarizer_query_ipc.py`, `tests/test_legal_summarizer_query_manifest_integration.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 2, module functions: 2

### class `LegalSummarizerQueryToolConfig` — lines 96-101 (6 LOC), 0 methods
- bases: BaseModel
- decorators: —
- docstring: Конфиг секции ``tools.legal_summarizer_query`` в ``config.json``.
- name referenced in 3 file(s); tests: 2

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `LegalSummarizerQueryTool` — lines 163-357 (195 LOC), 10 methods
- bases: Tool
- decorators: tool_parameters({'type': 'object', 'properties': {'operation_id': {'type': 'string', 'description': 'operation_id ранее выполненного summarize (поле result.operation_id из прошлого ответа).'}, 'field': {'type': 'string', 'enum': ['stats', 'articles', 'chunks', 'sections', 'tree', 'all'], 'description': 'Что вернуть: stats — ключевые метрики + article_count, articles — только article_count, chunks — список чанков с summary, sections — список section_path + heading, tree — иерархия секций, all — весь manifest.', 'default': 'stats'}, 'max_chunk_summary_chars': {'type': 'integer', 'minimum': 100, 'maximum': 10000, 'description': "Обрезка summary чанка для поля chunks (default 1500). Игнорируется для других field'ов.", 'default': 1500}}, 'required': ['operation_id']})
- docstring: Follow-up запросы по сохранённой operation_id навыка legal_summarizer.
- name referenced in 2 file(s); tests: 2

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 168-169 | `(self, *, config: LegalSummarizerQueryToolConfig) -> None` | 1 | 0 | 6 | — |
| `config_cls` | 172-173 | `(cls)` | 1 | 2 | 3 | — |
| `_read_settings_section` | 176-204 | `(cls, ctx: Any) -> dict[str, Any]` | 11 | 2 | 3 | Прочитать секцию ``tools.<config_key>`` из ``ctx._settings_ref``. |
| `enabled` | 207-209 | `(cls, ctx: Any) -> bool` | 2 | 0 | 9 | — |
| `create` | 212-218 | `(cls, ctx: Any) -> Tool` | 2 | 0 | 15 | — |
| `name` | 221-222 | `(self) -> str` | 1 | 0 | 91 | — |
| `description` | 225-234 | `(self) -> str` | 1 | 0 | 14 | — |
| `execute` | 236-315 | `(self, *, operation_id: str, field: str='stats', max_chunk_summary_chars: int=1500, **_kwa` | 9 | 0 | 43 | — |
| `_handle_nonzero_exit` | 317-349 | `(self, completed: subprocess.CompletedProcess) -> str` | 8 | 1 | 1 | Обработать non-zero exit cli_query.py: pass-through или cli_failed. При ``returncode != 0``: 1. Пытаемся распа |
| `_error` | 351-357 | `(self, error_type: str, message: str) -> str` | 1 | 6 | 3 | — |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_resolve_workspace_root` | 104-114 | `(arg: Optional[str]) -> Path` | 2 | 2 | Кросс-платформенный путь к корню репо. Приоритет: явно переданный ``workspace_root`` из конфига → корень из ра |
| `_resolve_cli_path` | 117-126 | `(workspace_root: Path) -> Path` | 1 | 1 | Абсолютный путь к ``cli_query.py``. |

## `workspace/utils/office_files.py` — 304 LOC (code 251)
- module: `workspace.utils.office_files`
- docstring: — NONE —
- static importers (5): `tools/extract_office_structure.py`, `workspace/skills/legal_summarizer/scripts/application/document_io.py`, `workspace/skills/legal_summarizer/scripts/document/loader.py`, `workspace/skills/legal_summarizer/scripts/document/physical.py`, `workspace/utils/structure_cache.py`
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 0, module functions: 19

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `detect_format` | 10-18 | `(path: str | Path) -> str` | 3 | 4 | — |
| `_read_text_auto` | 21-33 | `(path: Path) -> str` | 8 | 1 | — |
| `_extract_docx` | 36-49 | `(path: Path) -> str` | 10 | 1 | — |
| `_extract_pdf` | 52-64 | `(path: Path) -> str` | 6 | 1 | — |
| `_extract_pptx` | 67-82 | `(path: Path) -> str` | 11 | 1 | — |
| `_extract_xlsx` | 85-98 | `(path: Path) -> str` | 10 | 1 | — |
| `_extract_xls` | 101-113 | `(path: Path) -> str` | 9 | 1 | — |
| `_extract_csv` | 116-120 | `(path: Path) -> str` | 4 | 1 | — |
| `extract_text` | 123-142 | `(path: str | Path) -> str` | 9 | 6 | — |
| `_tables_docx` | 145-152 | `(path: Path) -> list[list[list[str]]]` | 4 | 1 | — |
| `_tables_pdf` | 155-163 | `(path: Path) -> list[list[list[str]]]` | 8 | 1 | — |
| `extract_tables` | 166-175 | `(path: str | Path) -> list[list[list[str]]]` | 4 | 3 | — |
| `read_xlsx_sheet` | 178-193 | `(path: str | Path, sheet_name: str | None=None) -> list[list[str]]` | 5 | 1 | — |
| `_summarize_docx` | 196-209 | `(path: Path) -> dict` | 4 | 1 | — |
| `_summarize_pdf` | 212-224 | `(path: Path) -> dict` | 9 | 1 | — |
| `_summarize_pptx` | 227-239 | `(path: Path) -> dict` | 4 | 1 | — |
| `_summarize_xlsx` | 242-252 | `(path: Path) -> dict` | 1 | 1 | — |
| `_summarize_xls` | 255-263 | `(path: Path) -> dict` | 1 | 1 | — |
| `summarize` | 266-304 | `(path: str | Path, *, preview_chars: int=500) -> dict` | 14 | 1 | — |

## `workspace/utils/media.py` — 259 LOC (code 224)
- module: `workspace.utils.media`
- docstring: Media-кодек — единая точка работы с вложениями сообщений (поле ``media``). Разные каналы и web-UI обмениваются вложениями, и чтобы схема жила в одном месте (а не копировалась в ``postgres_channel``, ``redis_channel`` и `
- static importers (2): `tests/test_diagnose_media_v22_vs_v23.py`, `tests/test_session_file_redirect_hook.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 8

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `data_url_info` | 40-57 | `(data_url: str) -> tuple[str, int] | None` | 6 | 2 | Достать ``(mime_type, file_size)`` из ``data:<mime>;base64,<payload>``. Возвращает ``None`` для не-``data:``-U |
| `_storage_entry` | 60-69 | `(data_url: str, filename: str, mime_type: str, file_size: int) -> dict[str, Any]` | 1 | 1 | Собрать один storage-элемент AW-схемы. |
| `entry_from_data_url` | 72-86 | `(data_url: str, filename: str | None=None) -> dict[str, Any]` | 8 | 2 | Собрать storage-элемент из готового data URL. Имя по умолчанию выводится из MIME-типа (``file.png``); при пере |
| `serialize` | 89-131 | `(media: list[str]) -> list[Any]` | 11 | 7 | Превратить runtime ``list[str]`` (пути/URL) в storage AW-дикты. Для локальных файлов читается содержимое и код |
| `deserialize` | 134-180 | `(media: list[Any], file_store: Any, session_key: str='default') -> list[Any]` | 19 | 1 | Перевести storage-медиа обратно в runtime-формат для агента. data URL (в ``file_id``/``data``/строке) сохраняю |
| `resolve_paths_and_hints` | 183-202 | `(media: list[Any]) -> tuple[list[str], list[str]]` | 12 | 1 | Из декодированных media (строки-пути или dict filename/path) извлечь пути для агента и подсказки «файл лежит т |
| `normalize_storage_entry` | 205-220 | `(entry: Any) -> Any` | 9 | 2 | Нормализовать один legacy-элемент media в AW-формат (для backfill). ``dict {"filename", "data": "data:..."}``  |
| `read_for_ui` | 223-247 | `(entry: Any) -> tuple[str, str, str]` | 18 | 1 | Толерантный читатель любого спорный shape для отрисовки. Возвращает ``(data_url, path, filename)``, пробуя ``f |

## `workspace/tools/compact_context.py` — 177 LOC (code 154)
- module: `workspace.tools.compact_context`
- docstring: CompactContextTool — tool ручного сжатия контекста диалога. Регистрируется автоматически через ``lib.services.project_tool_loader.register_project_tools`` при старте gateway/CLI (см. ``lib/services/project_tool_loader.py
- static importers (1): `tests/test_context_compaction.py`
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 2, module functions: 0

### class `CompactToolConfig` — lines 43-54 (12 LOC), 0 methods
- bases: BaseModel
- decorators: —
- docstring: Конфиг секции ``gateway.compact`` в ``project.json``. Поля совпадают с тем, что читает ``ContextCompactionService`` — см. ``lib/services/context_compaction.py``. Pydantic-валидация гарантирует типы при загрузке ``project
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `CompactContextTool` — lines 91-177 (87 LOC), 7 methods
- bases: Tool
- decorators: tool_parameters({'type': 'object', 'properties': {'session_key': {'type': 'string', 'description': 'Ключ сессии для сжатия. По умолчанию — текущая сессия (берётся из request context).'}, 'idle': {'type': 'boolean', 'default': False, 'description': 'Жёсткое idle-сжатие: оставить последние сообщения, остальное суммаризовать. Аналог ``force=true`` ниже; оставлено для обратной совместимости.'}, 'force': {'type': 'boolean', 'default': True, 'description': 'Ручной запуск — безоговорочно сжать сессию жёстко (``compact_idle_session``), игнорируя порог токенов. Дефолт ``true`` соответствует семантике команды ``/compact``: пользователь явно попросил сжать, а пустой вызов ``compact_context({})`` трактуется как ручной запрос. Передайте явно ``false``, чтобы вернуться к token-budget режиму (``maybe_consolidate_by_tokens``).'}}})
- docstring: Сжать контекст текущего (или указанного) диалога. По умолчанию сжимает жёстко (``force=true``): пользователь явно позвал tool — значит, нужно сжать сейчас, независимо от размера. ``idle`` — алиас ``force``. ``force=false
- name referenced in 1 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `config_cls` | 105-106 | `(cls)` | 1 | 0 | 3 | — |
| `enabled` | 109-121 | `(cls, ctx: Any) -> bool` | 6 | 0 | 9 | — |
| `create` | 124-141 | `(cls, ctx: Any) -> Tool` | 2 | 0 | 15 | — |
| `__init__` | 143-144 | `(self, *, service: Any) -> None` | 1 | 0 | 6 | — |
| `name` | 147-148 | `(self) -> str` | 1 | 0 | 91 | — |
| `description` | 151-158 | `(self) -> str` | 1 | 0 | 14 | — |
| `execute` | 160-177 | `(self, session_key: str | None=None, idle: bool=False, force: bool=True, **_kwargs: Any) -` | 3 | 0 | 43 | — |

## `workspace/tools/example.py` — 131 LOC (code 108)
- module: `workspace.tools.example`
- docstring: Шаблон кастомного tool'а проекта. Скопируйте файл, переименуйте класс и ``config_key``, допишите ``execute``. Шаблон следует конвенциям nanobot без дополнительных обёрток. Конфиг в ``config.json``:: { "tools": { "example
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 2, module functions: 0

### class `ExampleToolConfig` — lines 41-45 (5 LOC), 0 methods
- bases: BaseModel
- decorators: —
- docstring: Конфиг секции ``tools.example`` в ``config.json``.
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `ExampleTool` — lines 58-131 (74 LOC), 8 methods
- bases: Tool
- decorators: tool_parameters({'type': 'object', 'properties': {'text': {'type': 'string', 'description': 'Текст, длину которого нужно посчитать.'}}, 'required': ['text']})
- docstring: Возвращает длину переданного текста. Шаблон для копирования.
- name referenced in 0 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `config_cls` | 64-65 | `(cls)` | 1 | 2 | 3 | — |
| `_read_settings_section` | 68-96 | `(cls, ctx: Any) -> dict[str, Any]` | 8 | 2 | 3 | Прочитать секцию ``tools.<config_key>`` из ``_settings_ref``. ``ctx.config`` (``agent.tools_config``) — это py |
| `enabled` | 99-101 | `(cls, ctx: Any) -> bool` | 2 | 0 | 9 | — |
| `create` | 104-110 | `(cls, ctx: Any) -> Tool` | 2 | 0 | 15 | — |
| `__init__` | 112-113 | `(self, *, config: ExampleToolConfig) -> None` | 1 | 0 | 6 | — |
| `name` | 116-117 | `(self) -> str` | 1 | 0 | 91 | — |
| `description` | 120-124 | `(self) -> str` | 1 | 0 | 14 | — |
| `execute` | 126-131 | `(self, *, text: str, **_kwargs: Any) -> str` | 3 | 0 | 43 | — |

## `workspace/hooks/recent_files_hook.py` — 125 LOC (code 95)
- module: `workspace.hooks.recent_files_hook`
- docstring: RecentFilesHook — отслеживание файлов, которые агент создал за оборот. Задача: при формировании ``OutboundMessage.media`` автоматически прикладывать пути ко всем файлам, которые агент только что записал через ``write_fil
- static importers (2): `tests/test_recent_files_hook.py`, `tests/test_smoke_postgres_channel_media.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 0

### class `RecentFilesHook` — lines 51-125 (75 LOC), 6 methods
- bases: AgentHook
- decorators: —
- docstring: Собирает пути к файлам, которые агент создал/изменил за оборот. Изолирует состояние по ``session_key``: разные вопросы (конкурентные сессии) не «путают» файлы. Используется в ``RuntimePatcher._wrap`` после ``tool_audit_h
- name referenced in 2 file(s); tests: 2

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 60-66 | `(self, workspace_dir: str | None=None) -> None` | 1 | 0 | 6 | — |
| `_bucket_key` | 69-72 | `(ctx: Any) -> str` | 2 | 1 | 3 | Вернуть ``session_key`` из контекста (``""`` если его нет/не строка). |
| `_extract_path` | 75-82 | `(params: Any) -> str | None` | 6 | 1 | 2 | Извлечь путь из ``params`` (поддержка dict- и kwargs-вариантов). |
| `after_execute_tool` | 84-109 | `(self, context: Any, tool_call: Any, tool: Any, params: Any, result: Any) -> None` | 6 | 0 | 5 | Записать финальный путь файла, если инструмент файловый. ``params["path"]`` на этом этапе уже отредактирован ` |
| `drain` | 111-120 | `(self, session_key: str | None=None) -> list[str]` | 2 | 0 | 7 | Вернуть и обнулить список путей текущей сессии. Порядок путей сохраняется (в порядке их создания агентом). Дуб |
| `collected` | 122-125 | `(self, session_key: str | None=None) -> list[str]` | 3 | 0 | 1 | Снимок путей без обнуления (для тестов/диагностики). |

## `workspace/utils/session_key.py` — 119 LOC (code 96)
- module: `workspace.utils.session_key`
- docstring: Safe session_key для путей в data_store/cache/sessions/. Используется: - SessionFileRedirectHook (redirect write_file/media в session-папку). - legal_summarizer (document-cache для переиспользования chunks). Sanitize-лог
- static importers (4): `tests/test_session_key.py`, `workspace/hooks/session_file_redirect_hook.py`, `workspace/skills/legal_summarizer/scripts/cache/document_cache.py`, `workspace/skills/legal_summarizer/scripts/cli.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 4

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `safe_session_key` | 29-37 | `(key: str) -> str` | 2 | 6 | Sanitize session_key для имени директории (Windows + Linux). Совпадает с бывшим ``SessionFileRedirectHook._san |
| `resolve_session_key` | 40-65 | `(context: Any) -> str` | 6 | 1 | Получить стабильный ключ сессии из контекста (in-process). Зеркало бывшего ``SessionFileRedirectHook._session_ |
| `resolve_session_key_for_subprocess` | 68-106 | `(file_path: str | Path | None=None) -> str` | 6 | 1 | Получить стабильный ключ сессии для standalone CLI/skill-процесса. Зеркало ``resolve_session_key`` для случая, |
| `extract_session_key_from_path` | 109-119 | `(file_path: str) -> str | None` | 3 | 1 | Извлечь raw session_key из пути ``data_store/cache/sessions/<key>/...``. Поддерживает POSIX и Windows пути. `` |

## `workspace/hooks/debug_stream_diag.py` — 70 LOC (code 57)
- module: `workspace.hooks.debug_stream_diag`
- docstring: DEBUG-HOOK: StreamDiagnosisHook — временный диагностический хук. Регистрируется через ``workspace/hooks/`` auto-scan и подробно логирует каждый стрим-чанк в файл ``data_store/cache/debug_stream.log``. УДАЛИТЬ после диагн
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 0

### class `StreamDiagnosisHook` — lines 16-70 (55 LOC), 8 methods
- bases: AgentHook
- decorators: —
- docstring: — NONE —
- name referenced in 0 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 17-25 | `(self, workspace_dir: Path | str | None=None) -> None` | 3 | 0 | 6 | — |
| `_write` | 27-29 | `(self, msg: str) -> None` | 2 | 8 | 1 | — |
| `on_stream` | 31-35 | `(self, context: Any, delta: str) -> None` | 2 | 0 | 0 | — |
| `emit_reasoning` | 37-41 | `(self, reasoning_content: str | None) -> None` | 2 | 0 | 0 | — |
| `emit_reasoning_end` | 43-44 | `(self) -> None` | 1 | 0 | 0 | — |
| `on_stream_end` | 46-47 | `(self, context: Any, *, resuming: bool) -> None` | 1 | 0 | 0 | — |
| `after_iteration` | 49-64 | `(self, context: Any) -> None` | 4 | 0 | 6 | — |
| `finalize_content` | 66-70 | `(self, context: Any, content: str | None) -> str | None` | 1 | 0 | 3 | — |

## `workspace/utils/structure_cache.py` — 62 LOC (code 53)
- module: `workspace.utils.structure_cache`
- docstring: — NONE —
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 2

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_key` | 12-15 | `(path: Path) -> str` | 1 | 1 | — |
| `get_structure` | 18-62 | `(path: str | Path, *, begin_chars: int=800, end_chars: int=800, include_text: bool=True) -` | 7 | 0 | Получить структуру документа с кэшированием на диске. Повторные вызовы для того же файла (path+size+mtime не и |

## `workspace/utils/jsonb.py` — 52 LOC (code 42)
- module: `workspace.utils.jsonb`
- docstring: Безопасное декодирование JSONB-значений, приходящих из psycopg2/asyncpg. psycopg2 при ``register_json`` возвращает ``dict`` напрямую, но старые записи в БД (до перехода на JSONB-колонку) могут храниться как JSON-строка, 
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 2

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `decode_jsonb` | 16-31 | `(val: Any) -> dict` | 6 | 0 | Безопасно декодировать JSONB-значение из БД в ``dict``. Принимает: * ``None`` → ``{}`` * ``str`` (JSON) → парс |
| `decode_json_list` | 34-52 | `(val: Any) -> list` | 5 | 0 | Безопасно декодировать JSONB-список из БД в ``list``. Принимает: * ``None``/``""`` → ``[]`` * ``str`` (JSON) → |

## `workspace/utils/clean_text.py` — 44 LOC (code 34)
- module: `workspace.utils.clean_text`
- docstring: Каноническая санитизация текста сообщений/результатов инструментов. PostgreSQL не принимает настоящий NUL-байт (0x00) в text-литералах (``A string literal cannot contain NUL (0x00) characters.``), а psycopg2 трактует лит
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 1

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `clean_text` | 25-44 | `(value: Any) -> Any` | 12 | 3 | Рекурсивно вычистить NUL (0x00) и литеральные ``\u0000``..\u0003``. Проходит по строкам внутри строк/list/tupl |

## `workspace/tools/__init__.py` — 24 LOC (code 20)
- module: `workspace.tools`
- docstring: Кастомные tool'ы проекта (auto-discover). Модули в этой директории сканируются ``RuntimePatcher.patch_project_tools`` после старта ``AgentLoop``. Каждый модуль может экспортировать tool-классы — наследники ``nanobot.agen
- static importers (0): — NONE —
- string/dynamic refs: 5
- test files touching it: — none —
- classes: 0, module functions: 0

## `workspace/hooks/__init__.py` — 0 LOC (code 0)
- module: `workspace.hooks`
- docstring: — NONE —
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 0

## `workspace/utils/__init__.py` — 0 LOC (code 0)
- module: `workspace.utils`
- docstring: — NONE —
- static importers (1): `tests/test_office_files.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 0

