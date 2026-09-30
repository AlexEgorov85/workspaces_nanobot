# Work brief `09-skill-audit-analyzer`

Product files: **12**, LOC: **2035**

## `workspace/skills/audit_analyzer/scripts/cli.py` — 604 LOC (code 519)
- module: `workspace.skills.audit_analyzer.scripts.cli`
- docstring: Точка входа: CLI с разбором аргументов и маршрутизацией по режимам. CLI — единая точка вызова навыка из shell/runtime/тестов. Он НЕ содержит business-логики режимов: делегирует в ``predefined.run``, ``generated_sql_mode.
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: `tests/test_application_context.py`, `tests/test_application_context_logging.py`, `tests/test_application_context_single_application_point.py`, `tests/test_audit_analyzer_cli.py`, `tests/test_cli_agent.py`, `tests/test_gateway.py`, `tests/test_project_settings.py`, `workspace/skills/legal_summarizer/tests/test_skill_legal_summarizer.py`
- classes: 0, module functions: 12

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_resolve_known_index` | 70-105 | `(index_name: str) -> tuple[bool | None, str]` | 5 | 2 | Проверить, что ``index_name`` зарегистрирован в runtime-реестре. Использует публичный ``cache_provider_impl.re |
| `_parse_params` | 108-125 | `(raw: str) -> dict[str, Any]` | 6 | 2 | Распарсить ``--params`` в dict. Поддерживает JSON и key=value. |
| `_ensure_registered` | 128-160 | `() -> None` | 6 | 2 | Standalone-CLI регистрирует skill в ``TableRegistry`` перед работой. В обычном runtime это делает ``Applicatio |
| `_build_parser` | 163-237 | `() -> argparse.ArgumentParser` | 2 | 4 | Argparse: --mode, --script, --query, --params, --index-name, --top-k, --threshold, --context. |
| `_open_db` | 240-255 | `()` | 1 | 1 | Получить провайдера кэша через интерфейс. Провайдер открыт и настроен самой точкой создания (``open_cache_prov |
| `_list_scripts` | 258-305 | `(db: Any) -> dict` | 4 | 1 | Полный каталог predefined-скриптов из ``public.agent_predefined_scripts``. Включает name, description, paramet |
| `_list_indexes` | 308-388 | `(db: Any) -> dict` | 16 | 1 | Каталог runtime-индексов из локального кэша (``<storage_table>``). После change ``remove-vector-index-store``  |
| `_run_predefined` | 391-423 | `(script: str, db: Any, params: dict[str, Any] | None) -> dict` | 3 | 1 | Запустить predefined capability через ``predefined.run()``. Скрипты читаются из PG-снимка ``public.agent_prede |
| `_run_generated_sql` | 426-440 | `(query: str, db: Any, context: list[dict] | None) -> dict` | 2 | 1 | Запустить generated_sql capability через ``generated_sql_mode.run()``. |
| `_run_vector` | 443-532 | `(query: str, db: Any, index_name: str | None, top_k: int | None, threshold: float | None) ` | 14 | 3 | Запустить vector capability через ``CacheProvider.search_vector()``. CLI использует **прямой** CacheProvider A |
| `_run` | 535-557 | `(args: argparse.Namespace) -> dict` | 7 | 7 | Маршрутизация выполнения по ``args.mode``. |
| `main` | 560-600 | `() -> None` | 7 | 32 | Entry point: argparse → _run → JSON в stdout. |

## `workspace/skills/audit_analyzer/scripts/generated_sql_mode.py` — 276 LOC (code 210)
- module: `workspace.skills.audit_analyzer.scripts.generated_sql_mode`
- docstring: Режим: generated_sql — LLM генерирует SELECT по описанию на естественном языке. Pipeline с ретраями: 1. Получить схему БД (information_schema) 2. LLM генерирует SQL по схеме + запросу пользователя 3. Валидация безопаснос
- static importers (1): `workspace/skills/audit_analyzer/scripts/cli.py`
- string/dynamic refs: 0
- test files touching it: `workspace/skills/audit_analyzer/tests/test_audit_analyzer_edge_cases.py`
- classes: 0, module functions: 5

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_normalize` | 40-45 | `(text: str) -> set[str]` | 3 | 2 | Токенизация для keyword-overlap: lower + split по небуквенным. Длина токена ≥ 3 (отсекает «и», «по», «в», «на» |
| `_select_few_shot` | 48-78 | `(query: str, scripts: dict[str, ScriptDefinition], limit: int=2) -> str` | 9 | 1 | Выбрать top-N скриптов из реестра по keyword-overlap с запросом. Скоринг = \|tokens(name+description) ∩ tokens( |
| `is_no_match` | 84-97 | `(sql: str) -> bool` | 2 | 3 | Распознать явный отказ LLM от генерации SQL. LLM обучен system prompt'ом возвращать ``<NO_MATCH>`` (без markdo |
| `sanitize_sql_response` | 100-132 | `(text: str) -> str` | 4 | 2 | Извлечь SQL из ответа LLM (CoT + markdown-обёртки). Для reasoning-моделей ответ часто выглядит так:: <think>.. |
| `run` | 135-276 | `(query: str, db, context: list[dict] | None=None) -> dict` | 21 | 100 | Сгенерировать SQL через LLM, проверить, выполнить (с retry-циклом). Если LLM вернула некорректный SQL (не прош |

## `workspace/skills/audit_analyzer/scripts/predefined/builder.py` — 229 LOC (code 181)
- module: `workspace.skills.audit_analyzer.scripts.predefined.builder`
- docstring: ``DynamicQueryBuilder`` — сборка SQL из шаблона скрипта. Перенесено из ``workspace/skills/audit_analyzer/scripts/scripts_registry.py`` (эталон a606fe0). Семантика и pipeline те же: 1. Значения по умолчанию для отсутствую
- static importers (2): `workspace/skills/audit_analyzer/scripts/predefined/__init__.py`, `workspace/skills/audit_analyzer/scripts/predefined/mode.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 2, module functions: 1

### class `BuildError` — lines 69-74 (6 LOC), 1 methods
- bases: Exception
- decorators: —
- docstring: Ошибка сборки SQL (например, обязательный параметр отсутствует).
- name referenced in 2 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 72-74 | `(self, message: str) -> None` | 1 | 0 | 6 | — |

### class `DynamicQueryBuilder` — lines 77-229 (153 LOC), 3 methods
- bases: object
- decorators: —
- docstring: Сборка SQL из ``ScriptDefinition.sql_template`` + параметров.
- name referenced in 3 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `_render_template` | 81-115 | `(sql_template: str, params: dict[str, Any]) -> str` | 10 | 1 | 1 | Удалить ``{% if param %}...{% endif %}`` блоки для пустых параметров. Условия считаются «пустыми» если param о |
| `_convert_to_positional` | 118-148 | `(sql: str, params: dict[str, Any]) -> tuple[str, list[Any]]` | 4 | 1 | 2 | Конвертация ``:param_name`` → ``?`` по диалекту кэша. Negative lookbehind защищает ``::type_cast`` (двойное дв |
| `build` | 151-229 | `(cls, script: ScriptDefinition, params: dict[str, Any]) -> tuple[str, list[Any]]` | 24 | 0 | 14 | Полный цикл сборки SQL из шаблона. Returns: ``(positional_sql, values_in_order)`` — готов к ``CacheProvider.qu |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_param_usage_count` | 35-63 | `(sql: str, ordered_names: list[str]) -> list[tuple[str, int]]` | 4 | 1 | Подсчитать вхождения каждого ``:name`` в SQL в порядке первого появления. Возвращает список ``[(name, count),  |

## `workspace/skills/audit_analyzer/scripts/predefined/validator.py` — 212 LOC (code 176)
- module: `workspace.skills.audit_analyzer.scripts.predefined.validator`
- docstring: ``ParameterValidator`` — проверка пользовательских параметров. Перенесено из ``workspace/skills/audit_analyzer/scripts/predefined_mode.py`` (эталон a606fe0). Логика и сообщения об ошибках сохранены. Critical rules: * Val
- static importers (2): `workspace/skills/audit_analyzer/scripts/predefined/__init__.py`, `workspace/skills/audit_analyzer/scripts/predefined/mode.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 2, module functions: 1

### class `ValidationError` — lines 49-60 (12 LOC), 1 methods
- bases: Exception
- decorators: —
- docstring: Ошибка валидации параметров (для тестов и программных вызовов). Attributes: message: Человекочитаемое описание ошибки. script_name: Имя скрипта, в котором произошла ошибка.
- name referenced in 3 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 57-60 | `(self, message: str, *, script_name: str='') -> None` | 1 | 0 | 6 | — |

### class `ParameterValidator` — lines 63-212 (150 LOC), 5 methods
- bases: object
- decorators: —
- docstring: Валидирует и нормализует параметры для ``ScriptDefinition``. Делает то же самое, что встроенная логика ``predefined_mode.run()`` эталона a606fe0: 1. drop пустых (``None`` / ``""``) и неизвестных ключей; 2. возврат ``unkn
- name referenced in 3 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `merge` | 77-97 | `(script: ScriptDefinition, params: dict[str, Any] | None) -> tuple[dict[str, Any], list[st` | 6 | 1 | 1 | Оставить только параметры, объявленные в ``script.parameters``. Пропускает ``None`` и пустые строки (как было  |
| `check_required` | 100-117 | `(script: ScriptDefinition, merged: dict[str, Any]) -> str | None` | 4 | 1 | 1 | Вернуть сообщение об ошибке, если какого-то обязательного параметра нет. Returns: ``None`` если всё ок, иначе  |
| `coerce_types` | 120-161 | `(script: ScriptDefinition, merged: dict[str, Any]) -> str | None` | 15 | 1 | 1 | Проверить/преобразовать типы значений по ``ParamDefinition.type``. Соответствует поведению ``predefined_mode.r |
| `validate` | 164-200 | `(cls, script: ScriptDefinition, params: dict[str, Any] | None) -> tuple[dict[str, Any], st` | 9 | 1 | 5 | Полный цикл: merge + required + coerce. Returns: ``(validated_params, error_message)``. ``error_message`` None |
| `validate_or_raise` | 203-212 | `(cls, script: ScriptDefinition, params: dict[str, Any] | None) -> dict[str, Any]` | 2 | 0 | 0 | Как ``validate``, но бросает ``ValidationError`` при ошибке. |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_is_valid_iso_date` | 28-43 | `(value: Any) -> bool` | 4 | 1 | ISO-дата формата ``YYYY-MM-DD`` с валидным месяцем/днём. Проверяем через ``datetime.strptime`` (ISO strict), ч |

## `workspace/skills/audit_analyzer/scripts/predefined/mode.py` — 199 LOC (code 169)
- module: `workspace.skills.audit_analyzer.scripts.predefined.mode`
- docstring: ``predefined.run()`` — выполнение predefined SQL-скрипта через generic core. Канонический pipeline: public.agent_predefined_scripts (PostgreSQL, source of truth) ↓ seed_predefined_scripts.sql локальный кэш (снимок на мом
- static importers (1): `workspace/skills/audit_analyzer/scripts/predefined/__init__.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 4

### class `CacheQueryService` — lines 50-64 (15 LOC), 1 methods
- bases: Protocol
- decorators: —
- docstring: Минимальный интерфейс, нужный режиму predefined. Структурно — срез ``CacheProvider`` (см. ``lib.services.cache_provider``): нужен только ``query_sql``. Конкретная реализация кэша skill'у неизвестна. SQL-безопасность конт
- name referenced in 2 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `query_sql` | 60-64 | `(self, sql: str, params: list[Any] | None=None) -> dict[str, Any]` | 1 | 0 | 9 | — |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `list_available` | 67-69 | `(db: DBScriptProvider, predefined_table: str) -> str` | 2 | 2 | Список имён скриптов через запятую (для CLI/error messages). |
| `list_scripts` | 72-83 | `(db: DBScriptProvider, predefined_table: str) -> list[dict[str, str]]` | 3 | 1 | Метаданные всех скриптов для UI/CLI/документации. |
| `_resolve_script` | 86-96 | `(script_name: str, db: CacheQueryService | DBScriptProvider, predefined_table: str)` | 1 | 1 | DB-only lookup. Скрипт читается только из ``public.agent_predefined_scripts``. Никакого fallback на Python ``R |
| `run` | 99-199 | `(script_name: str, db: CacheQueryService, params: dict[str, Any] | None=None, *, predefine` | 14 | 100 | Выполнить predefined SQL-скрипт. Args: script_name: имя скрипта (DB-only lookup, см. ``db_loader``). db: ``Cac |

## `workspace/skills/audit_analyzer/scripts/predefined/db_loader.py` — 154 LOC (code 124)
- module: `workspace.skills.audit_analyzer.scripts.predefined.db_loader`
- docstring: Loader: ``public.agent_predefined_scripts`` (DB) → ``ScriptDefinition``. Единая точка входа для runtime-чтения predefined-скриптов из PostgreSQL. Резолв таблицы идёт через ``lib.core.skill_config.get_predefined_scripts_t
- static importers (3): `workspace/skills/audit_analyzer/scripts/generated_sql_mode.py`, `workspace/skills/audit_analyzer/scripts/predefined/__init__.py`, `workspace/skills/audit_analyzer/scripts/predefined/mode.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 4

### class `DBScriptProvider` — lines 34-46 (13 LOC), 1 methods
- bases: Protocol
- decorators: —
- docstring: Минимальный интерфейс для чтения скриптов из локального кэша. Структурно это срез ``CacheProvider``: нужен только ``query_sql``. Конкретная реализация кэша skill'у неизвестна, и подмена хранилища не должна требовать изме
- name referenced in 3 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `query_sql` | 42-46 | `(self, sql: str, params: list[Any] | None=None) -> dict[str, Any]` | 1 | 0 | 9 | — |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_row_to_script` | 49-87 | `(row: dict[str, Any]) -> ScriptDefinition` | 16 | 1 | PG-row → ``ScriptDefinition``. Ожидаемые колонки (см. ``sql/audit_analyzer/create_public_agent_predefined_scri |
| `_qualified_table` | 90-95 | `(table: str) -> tuple[str, str]` | 2 | 1 | ``"schema.table"`` → ``(schema, table)`` для SQL f-string. |
| `load_all` | 98-129 | `(provider: DBScriptProvider, table: str) -> dict[str, ScriptDefinition]` | 9 | 6 | Загрузить все скрипты из PG-реестра через локальный кэш. Args: provider: ``CacheProvider`` (роль только для чт |
| `load_script` | 132-154 | `(provider: DBScriptProvider, table: str, name: str) -> ScriptDefinition | None` | 9 | 3 | Загрузить один скрипт по имени. ``None`` если не найден. |

## `workspace/skills/audit_analyzer/scripts/output.py` — 88 LOC (code 63)
- module: `workspace.skills.audit_analyzer.scripts.output`
- docstring: Форматирование результатов для вывода в stdout (JSON). Приводит вложенные dict-результаты от режимов к плоскому единообразному формату для сериализации в JSON. Выходной JSON всегда содержит: - mode: режим работы ("predef
- static importers (1): `workspace/skills/audit_analyzer/scripts/cli.py`
- string/dynamic refs: 0
- test files touching it: `tests/test_benchmarks_runner.py`
- classes: 0, module functions: 2

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `prepare_output` | 27-83 | `(result: dict, mode: str) -> dict` | 17 | 6 | Привести вложенный результат режима к плоскому формату для вывода. Для predefined и sql: {"mode", "status", "r |
| `sanitize_output` | 86-88 | `(out: dict[str, Any]) -> dict[str, Any]` | 1 | 1 | Рекурсивно санировать значения в плоском dict для JSON. |

## `workspace/skills/audit_analyzer/scripts/skill_config.py` — 82 LOC (code 54)
- module: `workspace.skills.audit_analyzer.scripts.skill_config`
- docstring: Обёртка над ``lib.core.skill_config`` для текущего skill'а (audit_analyzer). Все функции параметризованы в ``lib.core.skill_config`` по ``skill_name``. Здесь — только реально используемые skill'ом обёртки, чтобы внутренн
- static importers (3): `workspace/skills/audit_analyzer/scripts/cli.py`, `workspace/skills/audit_analyzer/scripts/generated_sql_mode.py`, `workspace/skills/audit_analyzer/scripts/llm.py`
- string/dynamic refs: 0
- test files touching it: `tests/test_audit_analyzer_generated_sql.py`, `tests/test_skill_config_api.py`, `workspace/skills/legal_summarizer/tests/test_skill_legal_summarizer.py`
- classes: 0, module functions: 7

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `get_db_tables` | 45-46 | `() -> list[str]` | 1 | 4 | — |
| `get_db_schema` | 49-50 | `() -> str` | 1 | 4 | — |
| `get_predefined_scripts_table` | 53-54 | `() -> str` | 1 | 4 | — |
| `get_llm_config` | 57-58 | `() -> dict[str, Any]` | 1 | 4 | — |
| `get_cli_config` | 61-62 | `() -> dict[str, Any]` | 1 | 7 | — |
| `get_max_retries` | 65-66 | `() -> int` | 1 | 3 | — |
| `build_cache_provider` | 69-82 | `() -> 'CacheProvider'` | 1 | 2 | Провайдер кэша — тот же, что у runtime. Тонкий делегат в единую точку создания. Skill не выбирает реализацию и |

## `workspace/skills/audit_analyzer/scripts/predefined/models.py` — 77 LOC (code 62)
- module: `workspace.skills.audit_analyzer.scripts.predefined.models`
- docstring: Типизированные модели для predefined SQL-скриптов. Перенесено из ``workspace/skills/audit_analyzer/scripts/scripts_registry.py`` (эталон a606fe0). Типизация и семантика сохранены — то же поведение ``ParamDefinition``/``S
- static importers (5): `workspace/skills/audit_analyzer/scripts/generated_sql_mode.py`, `workspace/skills/audit_analyzer/scripts/predefined/__init__.py`, `workspace/skills/audit_analyzer/scripts/predefined/builder.py`, `workspace/skills/audit_analyzer/scripts/predefined/db_loader.py`, `workspace/skills/audit_analyzer/scripts/predefined/validator.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 2, module functions: 0

### class `ParamDefinition` — lines 23-46 (24 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Определение одного параметра скрипта. Attributes: type: Тип параметра. Управляет форматированием значения перед подстановкой в SQL: - ``like`` → оборачивает в %value% (ILIKE поиск) - ``exact`` → точное значение (без изме
- name referenced in 5 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `ScriptDefinition` — lines 50-70 (21 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Полное описание предопределённого SQL-скрипта. Attributes: name: Уникальное имя скрипта. description: Краткое описание для меню. sql_template: SQL-шаблон с ``:param_name`` плейсхолдерами и ``{% if param %}...{% endif %}`
- name referenced in 5 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

## `workspace/skills/audit_analyzer/scripts/predefined/__init__.py` — 60 LOC (code 53)
- module: `workspace.skills.audit_analyzer.scripts.predefined`
- docstring: Predefined SQL-режим навыка ``audit_analyzer``. Public API: * :func:`predefined.run` — выполнить скрипт через ``CacheQueryService``. * :func:`predefined.list_scripts` — метаданные всех скриптов из DB. * :func:`predefined
- static importers (1): `workspace/skills/audit_analyzer/scripts/cli.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 0

## `workspace/skills/audit_analyzer/scripts/llm.py` — 47 LOC (code 37)
- module: `workspace.skills.audit_analyzer.scripts.llm`
- docstring: LLM-клиент (OpenAI-compatible HTTP API) — тонкая обёртка над общим клиентом. Раньше здесь жил собственный httpx-POST с ретраями; теперь единая реализация — ``lib/services/llm_client.py`` (та же, что использует бенчмарк).
- static importers (1): `workspace/skills/audit_analyzer/scripts/generated_sql_mode.py`
- string/dynamic refs: 0
- test files touching it: `tests/integration/test_worker_pool_real_bot.py`, `tests/test_project_settings.py`, `workspace/skills/legal_summarizer/tests/test_confirmation_context.py`, `workspace/skills/legal_summarizer/tests/test_e2e_600_page.py`, `workspace/skills/legal_summarizer/tests/test_estimate_confirmation.py`, `workspace/skills/legal_summarizer/tests/test_execution_context_snapshot.py`, `workspace/skills/legal_summarizer/tests/test_final_invariants.py`, `workspace/skills/legal_summarizer/tests/test_recovered_invariants.py`, `workspace/skills/legal_summarizer/tests/test_single_flight_concurrent_safety.py`
- classes: 0, module functions: 1

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `chat` | 16-47 | `(messages: list[dict], *, context: list[dict] | None=None, **kwargs) -> str` | 6 | 4 | Отправить сообщения в LLM и получить текстовый ответ. Поддерживает опциональный context — историю чата, котора |

## `workspace/skills/audit_analyzer/scripts/__init__.py` — 7 LOC (code 6)
- module: `workspace.skills.audit_analyzer.scripts`
- docstring: CLI-обвязка навыка ``audit_analyzer``. Точка входа: ``python scripts/cli.py --mode ...``. Реализует три режима (predefined / sql / vector) поверх generic core services (CacheProvider + LLM-клиент). Конкретная СУБД кэша s
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: `tests/contract/test_channels_base.py`, `tests/contract/test_compaction_api.py`, `tests/contract/test_hook_subclass_contract.py`, `tests/contract/test_usage_store_api.py`, `workspace/skills/legal_summarizer/tests/test_structure_split_modules.py`
- classes: 0, module functions: 0

