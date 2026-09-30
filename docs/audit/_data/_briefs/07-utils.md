# Work brief `07-utils`

Product files: **11**, LOC: **1454**

## `lib/utils/sql_safety.py` — 425 LOC (code 347)
- module: `lib.utils.sql_safety`
- docstring: SQL safety guard для read-only инструментов и skill'ов, генерирующих SQL. Security boundary (TARGET_ARCHITECTURE §16): любой SQL, который мог быть сформирован LLM, проходит инфраструктурную policy validation непосредстве
- static importers (4): `tests/test_skill_tool_integration.py`, `tests/test_sql_safety.py`, `workspace/skills/audit_analyzer/scripts/generated_sql_mode.py`, `workspace/skills/audit_analyzer/tests/test_audit_analyzer_behavior.py`
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 2, module functions: 11

### class `SqlPolicy` — lines 129-134 (6 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Параметры SQL-политики read-only исполнения.
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `ValidationReport` — lines 141-157 (17 LOC), 1 methods
- bases: object
- decorators: dataclass
- docstring: Структурированный результат валидации (для audit trail вызывающей стороны).
- name referenced in 5 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `to_dict` | 150-157 | `(self) -> dict[str, Any]` | 1 | 0 | 24 | — |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_first_word` | 160-161 | `(stripped_upper: str) -> str` | 2 | 1 | — |
| `_get_exp` | 164-175 | `() -> Any` | 2 | 1 | Модуль выражений sqlglot (или None, если пакет недоступен). Подмодуль называется ``sqlglot.expressions`` и дос |
| `_parse_ast` | 178-192 | `(sql: str) -> list[Any] | None` | 3 | 1 | Разобрать SQL через sqlglot; список корней или None при недоступности. Ошибка парсинга не трактуется как наруш |
| `_func_name` | 195-212 | `(node: Any) -> str` | 8 | 1 | Имя функции из AST-узла (нижний регистр, без кавычек). ``Anonymous``-функции (неизвестные парсеру) хранят реал |
| `_walk_policy_issues` | 215-281 | `(ast_roots: list[Any], policy: SqlPolicy, depth: int=0) -> list[str]` | 24 | 1 | Обойти AST и собрать структурные нарушения политики. |
| `_regex_fallback_checks` | 284-295 | `(sql: str, policy: SqlPolicy) -> str | None` | 6 | 1 | Резервные проверки без AST (когда sqlglot недоступен). |
| `validate_sql_report` | 298-361 | `(sql: str, *, policy: SqlPolicy=DEFAULT_POLICY) -> ValidationReport` | 11 | 2 | Полная валидация со структурированным отчётом (для audit trail). Args: sql: исходный SQL. policy: политика (см |
| `validate_sql` | 364-376 | `(sql: str) -> str | None` | 1 | 4 | Проверить SQL на безопасность: только SELECT-подобные, один statement. Обратно совместимый контракт: ``None``  |
| `normalize_sql` | 379-386 | `(sql: str) -> str` | 2 | 2 | Нормализовать SQL для логирования/хеширования: схлопнуть пробелы. Строковые литералы НЕ вырезаются (риск невер |
| `query_hash` | 389-391 | `(normalized_sql: str) -> str` | 2 | 2 | SHA256-хеш нормализованного запроса (для audit trail и дедупликации). |
| `format_schema` | 394-425 | `(schema: dict) -> str` | 14 | 2 | Преобразовать схему БД в человекочитаемый формат для LLM-промпта. Структура ``schema``:: { "schema": "oarb", " |

## `lib/utils/duckdb_query.py` — 338 LOC (code 284)
- module: `lib.utils.duckdb_query`
- docstring: Общие примитивы для работы с DuckDB-кэшем: query/schema/explain и группировка чанков. Используется классами, которые держат собственное DuckDB-соединение (``PostgresDuckDbProvider``, ``DuckDbCacheStore``). DuckDB здесь н
- static importers (5): `lib/services/duckdb_cache_store.py`, `tests/integration/test_vector_build_e2e.py`, `tests/test_remove_vector_index_store_guards.py`, `tests/test_sql_safety.py`, `tests/test_vector_search_silent_failure.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 8

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `rewrite_duck_sql` | 26-29 | `(sql: str) -> str` | 1 | 1 | Адаптировать SQL к DuckDB: ``%s`` → ``?``, ``TO_CHAR(.., 'Month')`` → ``strftime``. |
| `run_query` | 32-56 | `(conn: Any, sql: str, params: list[Any] | None=None) -> dict[str, Any]` | 6 | 1 | Выполнить запрос на DuckDB-соединении, вернуть нормализованный результат. |
| `explain_query` | 59-68 | `(conn: Any, sql: str) -> dict[str, Any]` | 4 | 1 | EXPLAIN на DuckDB — синтаксическая проверка без выполнения. |
| `build_schema` | 71-142 | `(conn: Any, schema: str, tables: list[str] | None, meta_reader: Callable[[str], dict[tuple` | 16 | 2 | Собрать схему таблиц из ``information_schema`` DuckDB. ``meta_reader(schema)`` возвращает ``{(table, column):  |
| `build_raw_items` | 145-219 | `(meta_items: dict[str, Any], scores, ids, index_name: str, threshold: float | None, conn: ` | 23 | 1 | Собрать сырые чанки-строки из результатов поиска FAISS. После change ``remove-vector-index-store`` ``meta_item |
| `group_vector_hits` | 222-252 | `(raw: list[dict[str, Any]], top_k: int=5, threshold: float | None=None) -> list[dict[str, ` | 8 | 1 | Группировка чанков: один документ = одно место в top_k. Принимает сырые чанки (содержат ``source``, ``table``, |
| `build_faiss_index` | 255-301 | `(records: list[dict[str, Any]], metric: str | None=None) -> tuple[Any, dict[str, Any] | No` | 7 | 4 | Построить FAISS ``IndexFlatIP`` + metadata из списка записей. ``records`` — список словарей с ключами: ``sourc |
| `_as_vector` | 304-337 | `(raw: Any) -> list[float] | None` | 10 | 2 | Привести значение колонки ``embedding`` к списку чисел. В локальном снимке колонка ``embedding`` хранится как  |

## `lib/utils/windows_terminal.py` — 210 LOC (code 167)
- module: `lib.utils.windows_terminal`
- docstring: Поддержка Windows-консоли (legacy cmd/PowerShell без VT). Диагноз (проверен на 0.3.5 + prompt_toolkit 3.0.52): * кодировка ни при чём — ``cp1251`` кодирует U+001B как ``0x1B``; * Rich тоже ни при чём — он пишет корректны
- static importers (3): `cli_agent.py`, `gateway.py`, `lib/cli/console_loop.py`
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 0, module functions: 6

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_console_mode` | 40-58 | `(handle_id: int) -> int | None` | 6 | 1 | Текущий ``ConsoleMode`` для stdio-handle. ``None`` — handle не является консолью (пайп, файл, не-Windows) либо |
| `is_vt_enabled` | 61-71 | `(handle_id: int=_STDOUT_HANDLE) -> bool` | 2 | 2 | Включён ли VT на указанном stdio-handle. ``True`` также когда handle не консоль (нечего интерпретировать) или  |
| `enable_vt` | 74-102 | `() -> bool` | 8 | 2 | Включить ``ENABLE_VIRTUAL_TERMINAL_PROCESSING`` для STDOUT/STDERR. Returns: ``True`` если цвета безопасны — ли |
| `is_windows_console` | 105-116 | `() -> bool` | 3 | 2 | Запущены ли мы в TTY Windows-консоли (cmd/PowerShell/Windows Terminal)? Используется для условного включения V |
| `install_ansi_stripper` | 119-165 | `() -> bool` | 8 | 2 | Обёрнуть ``sys.stdout`` фильтром, вырезающим ANSI-последовательности. Нужен как последний рубеж: VT недоступен |
| `ensure_console_colors` | 171-210 | `() -> str | None` | 5 | 4 | Гарантировать, что ANSI-вывод не превратится в ``?[1m``-мусор. Порядок: включить VT → если не вышло, отключить |

## `lib/utils/outbound_meta.py` — 126 LOC (code 95)
- module: `lib.utils.outbound_meta`
- docstring: Служебные ключи в OutboundMessage.metadata, которые runner ставит в течение оборота. Сейчас это сигналы прогресса / рассуждений / потоковых чанков, которые НЕ интересуют пользователя и логирование. Канал и CLI должны дро
- static importers (5): `lib/channels/postgres_channel.py`, `lib/channels/redis_channel.py`, `lib/services/db_logging_bus.py`, `lib/services/runtime_patcher.py`, `tests/test_outbound_meta.py`
- string/dynamic refs: 2
- test files touching it: — none —
- classes: 0, module functions: 6

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `is_dropped` | 36-45 | `(metadata: Mapping[str, Any] | None) -> bool` | 4 | 3 | True, если в metadata есть любой из ``OUTBOUND_DROPPED_KEYS``. Каналы, CLI-цикл и DB-шина используют эту прове |
| `is_stream_delta` | 48-57 | `(metadata: Mapping[str, Any] | None) -> bool` | 3 | 0 | True, если metadata содержит ``_stream_delta`` (чанк стрима). Legacy-проверка: в nanobot 0.3.0 потоковые чанки |
| `_typed_event` | 60-70 | `(msg: Any) -> Any | None` | 1 | 2 | Вернуть типизированный outbound-ивент nanobot из ``msg`` (или None). Начиная с nanobot 0.3.5 событие приходит  |
| `is_outbound_final` | 73-91 | `(msg: Any) -> bool` | 4 | 1 | True, если сообщение — финальный ответ оборота (логируем как ``outbound_final``). Маркеры (единый контракт ``l |
| `is_outbound_noise` | 94-115 | `(msg: Any) -> bool` | 4 | 1 | True для служебных/потоковых событий БЕЗ аналитической ценности. Сюда относятся stream-delta (каждый токен), s |
| `msg_session_key` | 118-126 | `(msg: Any) -> str` | 2 | 1 | ``session_key`` с объекта msg/context (или ``""`` если нет/не строка). Исторически копировалось дословно в 5+  |

## `lib/utils/text_utils.py` — 96 LOC (code 82)
- module: `lib.utils.text_utils`
- docstring: Text-и JSON-safe value helpers shared by tools and skills. Перенесено из ``workspace/skills/audit_analyzer/scripts/output.py::_sanitize_value`` без изменения контракта. Дублирование заменено единой реализацией.
- static importers (4): `tests/test_text_utils.py`, `workspace/skills/audit_analyzer/scripts/output.py`, `workspace/skills/legal_summarizer/tests/test_skill_legal_summarizer.py`, `workspace/tools/history_search_tool.py`
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 0, module functions: 2

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `sanitize_value` | 18-69 | `(obj: Any) -> Any` | 19 | 4 | Рекурсивно привести объект к JSON-совместимому виду. Поддерживает: datetime / date / time → .isoformat() timed |
| `truncate_middle` | 72-96 | `(text: str, max_chars: int) -> str` | 3 | 2 | Обрезать ``text`` до ``max_chars`` символов, сохранив head и tail. Если длина ``text`` не превышает ``max_char |

## `lib/utils/retry.py` — 70 LOC (code 58)
- module: `lib.utils.retry`
- docstring: Универсальный retry с exponential backoff. Единая реализация повтора вызова при перечисленных исключениях — используется для БД-запросов, HTTP-вызовов и т.п., где раньше каждый модуль писал свой цикл ``for attempt ... ti
- static importers (2): `lib/services/cache_provider_impl.py`, `lib/services/llm_client.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 1

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `retry_on_exception` | 25-70 | `(fn: Callable[[], T], *, exceptions: tuple[type, ...], max_retries: int=3, base_delay: flo` | 7 | 2 | Повторить ``fn`` при ошибках из ``exceptions`` с exponential backoff. Args: fn: вызываемый объект без аргумент |

## `lib/utils/project_version.py` — 60 LOC (code 46)
- module: `lib.utils.project_version`
- docstring: project_version — версия текущего проекта. Версия проекта (в отличие от версии библиотеки nanobot ``__version__``) канонически хранится в ``project.json`` в секции ``project.version`` (актуальный релизный тег ``vX.Y.Z`` 
- static importers (2): `cli_agent.py`, `gateway.py`
- string/dynamic refs: 2
- test files touching it: — none —
- classes: 0, module functions: 3

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `project_version` | 24-30 | `(root: str | Path | None=None) -> str` | 3 | 2 | Версия проекта: ``project.version`` из ``project.json``, иначе git-тег, иначе ``"dev"``. |
| `_config_version` | 33-45 | `(root: str | Path | None) -> str | None` | 8 | 1 | — |
| `_git_version` | 48-60 | `(repo: str | Path) -> str` | 3 | 1 | — |

## `lib/utils/node_access.py` — 49 LOC (code 38)
- module: `lib.utils.node_access`
- docstring: Доступ к вложенным dict/AttrDict-структурам по цепочке пути. Много где в проекте повторялась одна и та же идея: достать значение из ``settings.channels.postgres.dsn`` через цепочку, где каждый уровень может быть dict или
- static importers (3): `lib/services/channel_factory.py`, `lib/services/config_service.py`, `lib/services/runtime_patcher.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 2

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `get_path` | 13-33 | `(node: Any, *path: str, default: Any=None) -> Any` | 6 | 1 | Безопасно пройти по цепочке ``path`` в ``node`` (dict или атрибуты). Возвращает ``default``, если на любом шаг |
| `get_settings_section` | 36-49 | `(settings: Any, name: str, default: Any=None) -> Any` | 3 | 1 | Достать секцию настроек (``settings.<name>``) с учётом dict/AttrDict. Сахар над ``get_path(settings, name, def |

## `lib/utils/logging_utils.py` — 44 LOC (code 30)
- module: `lib.utils.logging_utils`
- docstring: Общая настройка loguru для точек входа (gateway, cli_agent, runner). Раньше каждый модуль писал собственный ``_configure_logging`` с одинаковым ``logger.remove(); logger.add(sys.stderr, level=...)``. Единая точка здесь; 
- static importers (4): `benchmarks/runner.py`, `cli_agent.py`, `gateway.py`, `tests/conftest.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 1

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `configure_loguru` | 14-44 | `(level: str, *, env_var: str | None=None) -> None` | 5 | 4 | Настроить loguru на вывод в ``sys.stderr`` с указанным уровнем. Args: level: Уровень логирования (DEBUG/INFO/W |

## `lib/utils/table_utils.py` — 36 LOC (code 27)
- module: `lib.utils.table_utils`
- docstring: Утилиты для нормализации имён таблиц и схем (generic, не зависят от skill).
- static importers (1): `tests/test_table_utils.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 1

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `normalize_table_names` | 8-33 | `(value: Any) -> list[str]` | 14 | 1 | Привести список таблиц к плоскому списку ``"schema.table"`` строк. Допустимые форматы (для совместимости с ист |

## `lib/utils/__init__.py` — 0 LOC (code 0)
- module: `lib.utils`
- docstring: — NONE —
- static importers (1): `tests/test_windows_terminal.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 0

