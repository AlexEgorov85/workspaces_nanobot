# Work brief `13-tools-cli`

Product files: **19**, LOC: **4666**

## `tools/build_vectors.py` — 1023 LOC (code 802)
- module: `tools.build_vectors`
- docstring: Инкрементальная сборка векторных индексов из исходных таблиц. Поддерживает чанкование длинных текстов: если колонка в embedding_columns помечена "chunk": true, её текст разбивается на перекрывающиеся сегменты, каждый сег
- static importers (1): `tests/test_build_vectors_cli.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 17

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `fetchone` | 97-100 | `(sql, *args)` | 2 | 16 | Вернуть первую строку как dict или None. |
| `_setup_logging` | 103-115 | `(verbose: bool) -> None` | 2 | 1 | Настроить логгер: без ANSI-цветов (удобно при redirect в файл), без stderr-дубликата. Уровень DEBUG при ``--ve |
| `_fmt_eta` | 118-127 | `(sec: float) -> str` | 3 | 2 | Человекочитаемый остаток времени: ``4м 32с`` / ``1ч 05м``. |
| `_interactive_stderr` | 130-140 | `() -> bool` | 2 | 2 | Живой терминал (TTY) или redirect/pipe? В TTY прогресс перезаписывается через ``\r`` (одна строка не плодит со |
| `_print_progress` | 143-158 | `(tag: str, idx: int, total: int, started: float, interactive: bool, batch_size: int) -> No` | 9 | 2 | Вывести прогресс эмбеддинга: для TTY — перезаписываемая строка с ETA, иначе — лог-строка каждые ``batch_size`` |
| `_validate_index_config` | 161-335 | `(index_name: str, index_cfg: dict, chunk_size: int, chunk_overlap: int, metric: str) -> di` | 67 | 2 | Pre-flight проверка конфига индекса перед сборкой. Проверяет ВСЁ до начала эмбеддинга: - existence и формат по |
| `_format_content` | 342-345 | `(row: dict, columns: list[str]) -> str` | 6 | 1 | Собрать content из указанных колонок. |
| `_norm_pk` | 352-364 | `(raw: Any) -> str` | 3 | 1 | Привести значение PK к канонической строке. ``pk_value`` в векторной таблице хранится как TEXT, а в исходной т |
| `_get_existing_entries` | 367-374 | `(source: str, source_table: str, db_table: str) -> dict[str, dict]` | 3 | 1 | Вернуть {str(pk_value): {synced_at, content_hash, chunk_count}} существующих в векторной таблице. |
| `_get_source_rows` | 377-384 | `(table: str, pk: str, order_column: Optional[str]=None) -> list[dict]` | 3 | 1 | Все строки из исходной таблицы (для сравнения). |
| `_build_search_text` | 387-396 | `(row: dict, embedding_cols: list[str]) -> str` | 8 | 1 | Собрать search_text с метками колонок. |
| `_content_hash` | 399-401 | `(text: str) -> str` | 1 | 1 | Хеш search_text — для обнаружения изменений без переэмбеддинга. |
| `_normalize_cols` | 404-422 | `(embedding_cols: list) -> list[str]` | 6 | 1 | Привести embedding_cols к списку строк-имён колонок. Конфигурация в ``gateway.vector.index.indexes`` (см. ``Ve |
| `_rebuild_faiss` | 425-467 | `(index_name: str, db_table: str, rebuilt_only_deletion: bool=False) -> None` | 8 | 1 | Прогреть FAISS-индекс в in-memory cache провайдера. После change ``remove-vector-index-store`` persisted FAISS |
| `build_index` | 474-755 | `(index_name: str, index_cfg: dict, db_table: str, batch_size: int, default_chunk_size: int` | 50 | 1 | Собрать/обновить векторы для одного индексного источника. Обрабатывает сценарии: - NEW: строки, которых нет в  |
| `_filter_unchanged` | 762-795 | `(enabled: dict, db_table: str) -> dict` | 7 | 1 | Оставить только те индексы, где данные изменились. Сравнивает сигнатуру (число УНИКАЛЬНЫХ строк + MAX track_co |
| `main` | 802-1019 | `()` | 63 | 32 | — |

## `tools/legacy_audit.py` — 484 LOC (code 407)
- module: `tools.legacy_audit`
- docstring: Zero-reference audit: regression guard для legacy symbols. Это **regression guard**, не просто print. Три режима: * ``audit()`` — возвращает structured result (dict с hits). Сканирует **весь проект** (не только ``legal_s
- static importers (2): `tests/test_no_legacy_imports.py`, `workspace/skills/legal_summarizer/tests/test_legal_summarizer_no_legacy.py`
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 0, module functions: 8

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_iter_python_files` | 203-207 | `(root: pathlib.Path) -> list[pathlib.Path]` | 2 | 3 | — |
| `_is_test_file` | 210-212 | `(rel: str) -> bool` | 3 | 1 | Является ли путь тестовым файлом (не production). |
| `_is_allowed_legacy_test` | 215-241 | `(rel: str, line_no: int | None=None) -> bool` | 5 | 1 | Является ли legacy reference в файле allow-listed для теста. Args: rel: относительный путь к .py файлу. line_n |
| `audit_legacy_in_module` | 244-284 | `(py_file: pathlib.Path, project_root: pathlib.Path) -> dict[str, list[str]]` | 14 | 1 | Найти legacy references в одном .py файле (через AST). |
| `audit` | 287-318 | `(skill_root: pathlib.Path | None=None) -> dict[str, list[str]]` | 6 | 5 | Запустить audit по всему проекту. Сканирует ВСЕ .py файлы репозитория (не только legal_summarizer); каталоги ` |
| `_is_production_file` | 321-322 | `(rel: str) -> bool` | 1 | 1 | — |
| `assert_no_legacy` | 325-411 | `() -> None` | 32 | 2 | Поднять AssertionError если production содержит legacy hits. Учитывает **test-level allow-list** (Этап C remed |
| `main` | 414-472 | `() -> None` | 21 | 32 | — |

## `tools/diagnose_startup.py` — 442 LOC (code 382)
- module: `tools.diagnose_startup`
- docstring: Diagnose startup-inventory: парсит лог gateway/CLI и сверяет с каноном. Зачем: после старта gateway/CLI оператор/агент должен иметь возможность по сохранённому логу проверить, что **все** ожидаемые хуки/tools/патчи зарег
- static importers (1): `tests/test_diagnose_startup.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 5

### class `StartupFacts` — lines 70-92 (23 LOC), 1 methods
- bases: object
- decorators: dataclass
- docstring: Распарсенные факты из startup-лога.
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `is_empty` | 86-92 | `(self) -> bool` | 4 | 0 | 2 | — |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `parse_startup_log` | 95-184 | `(text: str) -> StartupFacts` | 31 | 2 | Парсит текст startup-лога и возвращает структурированные факты. |
| `diff_against_canonical` | 187-217 | `(facts: StartupFacts) -> dict[str, Any]` | 1 | 1 | Сравнить распарсенные факты с каноническими списками. Returns: dict с ключами ``hooks``, ``project_tools``, `` |
| `_print_panel` | 220-237 | `(console: Any, title: str, body: str, style: str) -> None` | 3 | 0 | — |
| `render_report` | 240-347 | `(facts: StartupFacts, diffs: dict[str, Any], *, use_color: bool=True) -> int` | 45 | 1 | Распечатать отчёт. Возвращает exit code (0/1/2). |
| `main` | 350-437 | `(argv: list[str] | None=None) -> int` | 23 | 32 | — |

## `tools/generate_comments_sql.py` — 291 LOC (code 262)
- module: `tools.generate_comments_sql`
- docstring: Генератор apply_all_comments.sql из schema.json + extra описаний. Требует на входе ``workspace/skills/audit_analyzer/cache/schema.json`` — это внешний дамп схемы домена. Штатного runtime-производителя дампа в репозитории
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 2

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_sql_escape` | 16-17 | `(s: str) -> str` | 1 | 1 | — |
| `render_table` | 20-29 | `(name: str, full: str, comment: str, columns: dict) -> list[str]` | 6 | 1 | Рендер COMMENT для одной таблицы. |

## `tools/check_indexes.py` — 285 LOC (code 238)
- module: `tools.check_indexes`
- docstring: ``tools/check_indexes.py`` — declared-vs-runtime diff для vector-индексов. Сравнивает: * ``project.json::gateway.vector.index.indexes.*`` — декларация (что должно быть построено); * runtime-состояние, которое видит ``sea
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 6

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_diff` | 58-166 | `(declared: dict[str, Any] | Exception, runtime: list[dict[str, Any]] | Exception) -> dict[` | 34 | 2 | Сравнить декларацию (JSON) и runtime (DuckDB-кэш). Возвращает структурированный diff. Возвращает dict с ключам |
| `_load_declared` | 169-173 | `() -> dict[str, Any] | Exception` | 3 | 1 | — |
| `_open_provider` | 176-185 | `() -> Any` | 1 | 1 | Открыть DuckDB-кэш в read-only через единственную точку создания. Инструмент читает кэш, а не PG, и не создаёт |
| `_load_runtime` | 188-196 | `() -> list[dict[str, Any]] | Exception` | 3 | 1 | — |
| `_format_text` | 199-239 | `(result: dict[str, Any]) -> str` | 29 | 2 | Человеко-читаемый вывод для terminal/pre-commit. |
| `main` | 242-281 | `(argv: list[str] | None=None) -> int` | 6 | 32 | — |

## `tools/validate_component_specs.py` — 274 LOC (code 224)
- module: `tools.validate_component_specs`
- docstring: Валидация структуры компонентных спецификаций OpenSpec (component-spec-validation). Проверяет, что все ``openspec/specs/<domain>/<component>/spec.md`` соответствуют шаблону, зафиксированному в ``openspec/specs/architectu
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 2, module functions: 6

### class `SpecIssue` — lines 71-79 (9 LOC), 1 methods
- bases: object
- decorators: dataclass
- docstring: Одно нарушение, найденное при валидации spec.
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `render` | 78-79 | `(self) -> str` | 1 | 0 | 3 | — |

### class `ValidationReport` — lines 83-96 (14 LOC), 2 methods
- bases: object
- decorators: dataclass
- docstring: Сводный отчёт валидации всех spec.
- name referenced in 5 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `ok` | 92-93 | `(self) -> bool` | 3 | 0 | 1 | — |
| `add` | 95-96 | `(self, spec_path: Path, section: str, message: str) -> None` | 1 | 0 | 39 | — |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `iter_spec_files` | 99-101 | `(specs_dir: Path) -> Iterable[Path]` | 2 | 1 | — |
| `validate_spec_sections` | 104-155 | `(spec_path: Path, text: str, report: ValidationReport) -> None` | 15 | 1 | Проверка обязательных и опциональных разделов. |
| `parse_components_registry` | 158-184 | `(report: ValidationReport) -> dict[str, str]` | 6 | 1 | Парсит ``COMPONENTS.md`` и возвращает словарь ``component_name -> status``. |
| `validate_registry` | 187-205 | `(registry: dict[str, str], report: ValidationReport, specs_dir: Path) -> None` | 5 | 1 | Проверка существования spec.md для non-missing компонентов. |
| `_slug_to_kebab` | 208-211 | `(name: str) -> str` | 1 | 1 | CamelCase → kebab-case (используется как fallback при поиске spec). |
| `main` | 214-270 | `(argv: list[str] | None=None) -> int` | 13 | 32 | — |

## `tools/migrate.py` — 252 LOC (code 214)
- module: `tools.migrate`
- docstring: migrate.py — runner миграций схемы (sql/migrations/V*.sql). Порядок версий определяется номером в имени файла: ``V001__name.sql``. Каждая применённая версия записывается в ``public.schema_migrations`` с SHA256-контрольно
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 11

### class `Migration` — lines 43-48 (6 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: — NONE —
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `compute_checksum` | 51-56 | `(sql: str) -> str` | 4 | 2 | SHA256 нормализованного SQL (без комментариев-строк и хвостовых пробелов). |
| `discover` | 59-72 | `(directory: Path | None=None) -> list[Migration]` | 6 | 2 | Найти и отсортировать файлы миграций; дубликаты номеров — ошибка. |
| `resolve_dsn` | 75-84 | `() -> str` | 4 | 4 | DATABASE_URL из окружения или channels.postgres.dsn из конфига. |
| `ensure_tracking_table` | 87-100 | `(conn) -> None` | 2 | 1 | — |
| `fetch_applied` | 103-109 | `(conn) -> dict[str, tuple[str, str]]` | 3 | 1 | version → (name, checksum) уже применённых. |
| `apply_migration` | 112-134 | `(conn, mig: Migration, force: bool=False) -> bool` | 2 | 1 | Применить одну мигранцию в транзакции; True если применилась. |
| `stamp_migration` | 137-147 | `(conn, mig: Migration) -> None` | 1 | 1 | — |
| `cmd_status` | 150-165 | `(migs: list[Migration], applied: dict[str, tuple[str, str]]) -> int` | 10 | 2 | — |
| `cmd_apply` | 168-195 | `(conn, migs: list[Migration], applied: dict[str, tuple[str, str]], target: str | None, for` | 11 | 1 | — |
| `cmd_verify` | 198-205 | `(migs: list[Migration], applied: dict[str, tuple[str, str]]) -> int` | 5 | 1 | — |
| `main` | 208-248 | `(argv: list[str] | None=None) -> int` | 10 | 32 | — |

## `tools/check_worker_pool_integrity.py` — 201 LOC (code 152)
- module: `tools.check_worker_pool_integrity`
- docstring: Диагностика целостности мульти-машинного пула воркеров. Проверяет инвариант ``processing ⇔ claim`` между ``agent_conversation_messages`` и ``agent_worker_claims`` и выводит отчёт о рассинхронах. Ничего не меняет (read-on
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 4

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_connect` | 49-52 | `()` | 1 | 5 | — |
| `main` | 55-158 | `() -> int` | 22 | 32 | — |
| `_rows` | 161-166 | `(conn, sql: str) -> list` | 3 | 2 | — |
| `_fix` | 169-197 | `(conn, messages: str, claims: str) -> None` | 2 | 1 | Аналог ``_reclaim_and_heal`` канала: вернуть в пул истёкшие lease, вылечить processing-без-claim, убрать висяч |

## `tools/legal_benchmark.py` — 197 LOC (code 166)
- module: `tools.legal_benchmark`
- docstring: Benchmark suite для token/call efficiency (PLAN §51). Сравнивает разные размеры документов (small/medium/large/very_large) для unified execution planner'а. Метрики: * parse_count * structure_pass_count (всегда 1 для unif
- static importers (2): `workspace/skills/legal_summarizer/tests/test_structure_benchmark.py`, `workspace/skills/legal_summarizer/tests/test_structure_llm_invariant.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 2, module functions: 6

### class `BenchmarkMetrics` — lines 64-77 (14 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Метрики benchmark'а.
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `BenchmarkScenario` — lines 81-86 (6 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Один benchmark сценарий.
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_build_chunks_and_struct` | 89-111 | `(text: str, n_sections: int, tmp_path_factory=None) -> tuple[DocumentStructure, tuple[Chun` | 4 | 1 | — |
| `run_benchmark` | 114-139 | `(scenario: BenchmarkScenario) -> BenchmarkMetrics` | 3 | 1 | Запустить benchmark для одного сценария. |
| `small_scenario` | 142-147 | `() -> BenchmarkScenario` | 1 | 1 | — |
| `medium_scenario` | 150-160 | `() -> BenchmarkScenario` | 3 | 1 | — |
| `large_scenario` | 163-173 | `() -> BenchmarkScenario` | 3 | 1 | — |
| `very_large_scenario` | 176-186 | `() -> BenchmarkScenario` | 3 | 1 | — |

## `tools/audit_nanobot_contracts.py` — 194 LOC (code 169)
- module: `tools.audit_nanobot_contracts`
- docstring: Аудит контрактов nanobot: AST нашего кода vs реальные символы nanobot 0.3.5. Что делает: 1. AST-обход всех ``.py`` в ``lib/``, ``workspace/``, ``tools/``. 2. Собирает ``from nanobot.X import Y`` / ``import nanobot.X.Y`` 
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 5

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_iter_py_files` | 34-45 | `() -> list[Path]` | 7 | 1 | — |
| `_walk_dotted` | 48-69 | `(dotted: str) -> object | None` | 10 | 1 | Идёт ``nanobot → agent → loop → AgentLoop → from_config``. |
| `_kind` | 72-85 | `(obj) -> str` | 8 | 1 | — |
| `audit` | 88-156 | `() -> list[dict]` | 36 | 5 | — |
| `main` | 159-190 | `() -> None` | 11 | 32 | — |

## `tools/scan_nanobot_inventory.py` — 176 LOC (code 151)
- module: `tools.scan_nanobot_inventory`
- docstring: Одноразовый сканер nanobot-зависимостей: строит JSON-инвентарь. Запуск: python tools/scan_nanobot_inventory.py [--out docs/architecture/nanobot-inventory.json] Сканирует lib/, workspace/, gateway.py, cli_agent.py, stream
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 4

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `iter_py_files` | 31-42 | `() -> list[Path]` | 8 | 1 | — |
| `classify` | 45-59 | `(import_module: str, names: str) -> str` | 11 | 1 | — |
| `scan_file` | 62-122 | `(path: Path) -> dict` | 27 | 1 | — |
| `main` | 125-172 | `() -> None` | 13 | 32 | — |

## `tools/smoke_post_cleanup.py` — 176 LOC (code 140)
- module: `tools.smoke_post_cleanup`
- docstring: Smoke-test runtime для opencode change post-0.3.5-patches-cleanup. Запускает gateway в фоне, проверяет: * gateway запускается без ошибок (RuntimeEventsSubscriber подключён); * в runtime-логах НЕТ упоминаний ``_last_usage
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 2

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_extract_event_type` | 37-43 | `(payload: Any) -> str` | 4 | 0 | Psycopg2 может вернуть JSON как dict или str в зависимости от версии. |
| `main` | 46-172 | `() -> int` | 20 | 32 | — |

## `tools/release_v252.py` — 169 LOC (code 123)
- module: `tools.release_v252`
- docstring: Создать GitHub Release для тега v2.5.2 через gh CLI (fallback: curl). Режимы: --dry-run — печатает полный payload (tag/title/body) в stdout, ничего не создаёт и не пишет на диск. По умолчанию. --curl — печатает готовую c
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 5

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_payload` | 75-76 | `() -> dict` | 1 | 2 | — |
| `_print_payload` | 79-89 | `() -> None` | 3 | 1 | Печать полного payload в stdout (для --dry-run). Без записи на диск. |
| `_print_curl` | 92-122 | `() -> None` | 3 | 2 | Печать готовой curl-команды (нужен $GITHUB_TOKEN). Без записи на диск. |
| `_run_gh` | 125-144 | `() -> int` | 3 | 2 | — |
| `main` | 147-165 | `() -> int` | 3 | 32 | — |

## `tools/demo_internal_fallback.py` — 142 LOC (code 113)
- module: `tools.demo_internal_fallback`
- docstring: Демонстрация fallback-ответа при internal-ошибке AgentLoop. Запускать:: PYTHONIOENCODING=utf-8 python tools/demo_internal_fallback.py Что показывает: 1) Реальный OutboundMessage, который получит пользователь (текст, chan
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 3, module functions: 2

### class `_StubBus` — lines 29-34 (6 LOC), 2 methods
- bases: object
- decorators: —
- docstring: — NONE —
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 30-31 | `(self) -> None` | 1 | 0 | 6 | — |
| `publish_outbound` | 33-34 | `(self, msg: OutboundMessage) -> None` | 1 | 0 | 3 | — |

### class `_RuntimeEventPublisher` — lines 37-42 (6 LOC), 2 methods
- bases: object
- decorators: —
- docstring: — NONE —
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 38-39 | `(self) -> None` | 1 | 0 | 6 | — |
| `turn_completed` | 41-42 | `(self, **kw) -> None` | 1 | 0 | 0 | — |

### class `_StubTurnDelivery` — lines 45-72 (28 LOC), 2 methods
- bases: object
- decorators: —
- docstring: Поведение upstream ``nanobot/agent/turn_delivery.py:TurnDelivery.fail``.
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 48-53 | `(self) -> None` | 1 | 0 | 6 | — |
| `fail` | 55-72 | `(self, *, publish_completion: bool) -> None` | 3 | 0 | 9 | — |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_install_stub_module` | 75-78 | `() -> None` | 1 | 1 | — |
| `main` | 81-138 | `() -> None` | 9 | 32 | — |

## `tools/release_v251.py` — 120 LOC (code 90)
- module: `tools.release_v251`
- docstring: Создать GitHub Release для тега v2.5.1 через gh CLI (fallback: curl).
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 4

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_print_curl` | 56-71 | `(token: str) -> None` | 1 | 2 | — |
| `_payload_path` | 74-80 | `() -> Path` | 1 | 1 | — |
| `_run_gh` | 83-98 | `() -> int` | 3 | 2 | — |
| `main` | 101-116 | `() -> int` | 4 | 32 | — |

## `tools/apply_test_profile_tables.py` — 97 LOC (code 80)
- module: `tools.apply_test_profile_tables`
- docstring: apply_test_profile_tables.py — создать 6 runtime-таблиц профиля test. Эти таблицы перечислены в profiles/test.jsonc: channels.postgres.table_name = public.agent_conversation_messages_test channels.postgres.messages_table
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 2

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `split_statements` | 45-60 | `(sql: str) -> list[str]` | 10 | 1 | — |
| `main` | 63-93 | `() -> int` | 10 | 32 | — |

## `tools/architecture_guard.py` — 78 LOC (code 61)
- module: `tools.architecture_guard`
- docstring: Premature abstraction guard (PLAN §60). PLAN §60: > Не делать: > - ``BaseStructureFactory`` > - ``AbstractHeadingStrategyFactory`` > - ``GenericNodeResolverFactory`` > > без реального потребителя. > > Предпочитать: > - м
- static importers (2): `workspace/skills/legal_summarizer/tests/test_structure_architecture_guard.py`, `workspace/skills/legal_summarizer/tests/test_structure_llm_invariant.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 3

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `is_factory_pattern` | 43-47 | `(name: str) -> bool` | 4 | 1 | True если имя класса выглядит как фабрика/builder. |
| `count_abstract_classes` | 50-58 | `(module: Any) -> int` | 4 | 1 | Подсчёт абстрактных классов в модуле. |
| `has_oversized_class` | 61-71 | `(module: Any, max_lines: int=100) -> bool` | 4 | 1 | True если в модуле есть класс > max_lines строк. |

## `tools/extract_office_structure.py` — 64 LOC (code 51)
- module: `tools.extract_office_structure`
- docstring: — NONE —
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 2

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_iter_files` | 15-21 | `(target: Path)` | 5 | 1 | — |
| `main` | 24-60 | `(argv: list[str] | None=None) -> int` | 5 | 32 | — |

## `tools/__init__.py` — 1 LOC (code 1)
- module: `tools`
- docstring: Project-level dev tooling (not part of any skill package).
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: `tests/test_application_context.py`, `tests/test_application_context_logging.py`, `tests/test_application_context_single_application_point.py`, `tests/test_benchmarks_loader.py`, `tests/test_cli_agent.py`, `tests/test_config_service.py`, `tests/test_context_compaction.py`, `tests/test_gateway.py`, `tests/test_tools_project_loader.py`
- classes: 0, module functions: 0

