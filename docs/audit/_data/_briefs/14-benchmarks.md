# Work brief `14-benchmarks`

Product files: **9**, LOC: **2671**

## `benchmarks/runner.py` — 996 LOC (code 814)
- module: `benchmarks.runner`
- docstring: Запуск бенчмарков для агента nanobot. Примеры: python benchmarks/runner.py python benchmarks/runner.py --tags simple python benchmarks/runner.py --items benchmarks/items/simple.yaml python benchmarks/runner.py --db postg
- static importers (1): `tests/test_benchmarks_runner.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 17

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_emit` | 52-73 | `(msg: str='', *, end: str='\n') -> None` | 9 | 3 | Напечатать ``msg`` в stdout и (если открыт) в лог-файл. Замена ``print()`` в runner: вывод идёт в оба места ср |
| `_detect_run_id` | 94-100 | `() -> str` | 1 | 1 | Генерация уникального идентификатора прогона на основе текущей метки времени. Returns: Строка вида "2026-06-16 |
| `_generate_run_id` | 103-113 | `() -> str` | 1 | 2 | Генерация единого UUID-идентификатора прогона. Один и тот же id используется для каталога файловых отчётов (re |
| `_parse_args` | 116-158 | `(argv: list[str] | None=None) -> argparse.Namespace` | 1 | 6 | Парсинг аргументов командной строки. Args: argv: Список аргументов (по умолчанию sys.argv). Returns: Пространс |
| `_filter_items` | 161-215 | `(suite: BenchSuite, tags: list[str] | None, category: list[str] | None, difficulty: str | ` | 20 | 2 | Фильтрация заданий по тегам, категории, сложности и типу. Args: suite: Исходный набор тестов. tags: Список тег |
| `_format_checks_failures` | 218-230 | `(checks: list[CheckResult]) -> str` | 5 | 2 | Форматирование списка заваленных проверок в читаемую строку. Args: checks: Список результатов проверок. Return |
| `_print_summary` | 233-264 | `(suite_result: SuiteResult) -> None` | 9 | 2 | Вывод итоговой сводки по прогону в консоль. Args: suite_result: Результаты прогона набора. |
| `cleanup_old_runs` | 267-294 | `(keep_last: int=20, runs_dir: str | Path | None=None) -> int` | 8 | 2 | Удаление старейших каталогов прогонов, кроме последних N. Args: keep_last: Сколько последних прогонов сохранят |
| `_cleanup_item` | 297-351 | `(item: BenchItem, bot: Any) -> None` | 31 | 2 | Удаление файлов, созданных агентом во время выполнения задания. Удаляет файлы, заявленные в ``expect.check_fil |
| `_run_item` | 354-377 | `(item: BenchItem, run_id: str, bot: Any, verbose: bool) -> BenchResult` | 2 | 2 | Запуск одного задания бенчмарка (single или multi_step). Args: item: Задание бенчмарка. run_id: Идентификатор  |
| `_run_single` | 380-447 | `(item: BenchItem, run_id: str, bot: Any, verbose: bool) -> BenchResult` | 8 | 2 | Запуск одношагового задания. Выполняет задание через агента, оценивает ответ и возвращает результат. Args: ite |
| `_run_multi_step` | 450-529 | `(item: BenchItem, run_id: str, bot: Any, verbose: bool) -> BenchResult` | 9 | 2 | Запуск многошагового задания. Последовательно выполняет каждый шаг в рамках одной сессии, оценивает каждый шаг |
| `_run_suite` | 532-708 | `(suite: BenchSuite, run_id: str, args: argparse.Namespace) -> SuiteResult` | 26 | 1 | Запустить все задания набора бенчмарков через ApplicationContext. По аналогии с gateway: поднимаем единый конт |
| `_do_compare` | 711-757 | `(args: argparse.Namespace) -> None` | 13 | 2 | Сравнение двух предыдущих прогонов по JSON-отчётам. Args: args: Аргументы командной строки (содержит compare — |
| `_validate_items` | 760-789 | `(suite: BenchSuite) -> list[str]` | 15 | 2 | Проверка всех заданий на очевидные проблемы перед запуском. Args: suite: Набор тестов для проверки. Returns: С |
| `main_async` | 792-980 | `(argv: list[str] | None=None) -> int` | 36 | 2 | Асинхронный входной точка запуска бенчмарков. Args: argv: Список аргументов командной строки. Returns: Код воз |
| `main` | 983-992 | `(argv: list[str] | None=None) -> int` | 1 | 32 | Синхронная точка входа для запуска бенчмарков. Args: argv: Список аргументов командной строки. Returns: Код во |

## `benchmarks/db.py` — 340 LOC (code 290)
- module: `benchmarks.db`
- docstring: Хранилище результатов бенчмарков в PostgreSQL. Использует общепроектный коннектор к БД из utils.db. Пример: from benchmarks.db import BenchmarkDB db = BenchmarkDB(dsn="...") db.ensure_tables() db.save_run(suite_result) h
- static importers (2): `benchmarks/runner.py`, `tests/test_benchmarks_db.py`
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 1, module functions: 1

### class `BenchmarkDB` — lines 75-340 (266 LOC), 8 methods
- bases: object
- decorators: —
- docstring: Управление сохранением и загрузкой результатов бенчмарков в PostgreSQL.
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 78-91 | `(self, dsn: str='', schema: str=SCHEMA) -> None` | 3 | 0 | 6 | Инициализация подключения к БД. Args: dsn: Строка подключения к PostgreSQL. schema: Схема базы данных (по умол |
| `ensure_tables` | 93-112 | `(self) -> None` | 4 | 0 | 2 | Создание таблиц для хранения прогонов, если они ещё не существуют. Автоматически выбирает между PG 9.4 и GP 6. |
| `save_run` | 114-127 | `(self, suite_result: SuiteResult) -> str | None` | 3 | 0 | 2 | Сохранение результатов прогона набора тестов. Args: suite_result: Результаты прогона набора. Returns: Идентифи |
| `_save_run_inner` | 129-207 | `(self, conn, suite_result: SuiteResult) -> str` | 9 | 1 | 1 | Вставка записи прогона и связанных результатов в БД. Использует единый ``suite_result.run_id`` (если задан) ка |
| `_result_details` | 210-241 | `(r: BenchResult) -> dict[str, Any]` | 6 | 1 | 1 | Собрать JSONB-details результата: checks и шаги multi_step. Args: r: Результат выполнения задания. Returns: Сл |
| `get_history` | 243-264 | `(self, suite_name: str, limit: int=10) -> list[dict[str, Any]]` | 2 | 0 | 2 | Получение истории прогонов для указанного набора тестов. Args: suite_name: Имя набора тестов. limit: Максималь |
| `compare_runs` | 266-280 | `(self, run_id_1: str, run_id_2: str) -> dict[str, Any] | None` | 3 | 0 | 1 | Сравнение двух прогонов по идентификаторам. Args: run_id_1: Идентификатор первого прогона. run_id_2: Идентифик |
| `_compare_runs_inner` | 282-340 | `(self, conn, run_id_1: str, run_id_2: str) -> dict[str, Any] | None` | 16 | 1 | 1 | Внутренняя логика сравнения двух прогонов. Args: conn: Активное соединение с БД (внутри транзакции). run_id_1: |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_is_greenplum` | 64-72 | `() -> bool` | 4 | 1 | Определить, работаем ли мы с Greenplum. |

## `benchmarks/evaluator.py` — 322 LOC (code 253)
- module: `benchmarks.evaluator`
- docstring: Модуль оценки выполнения заданий бенчмарка. Содержит функции проверки инструментов, навыков, ключевых слов, файлов и LLM-судьи, а также агрегации итоговых оценок.
- static importers (2): `benchmarks/runner.py`, `tests/test_benchmarks_evaluator.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 13

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `evaluate` | 20-56 | `(expect: BenchExpect, response: str | None, hook: BenchmarkHook, workspace: str | Path | N` | 7 | 2 | Полная оценка ответа агента по указанным ожиданиям. Args: expect: Ожидаемые критерии оценки. response: Текстов |
| `_check_tools` | 59-74 | `(expected: list[str], actual: list[str]) -> CheckResult` | 4 | 2 | Проверка, что агент использовал все ожидаемые инструменты. Args: expected: Список ожидаемых инструментов. actu |
| `_check_skills` | 77-92 | `(expected: list[str], actual: set[str]) -> CheckResult` | 4 | 2 | Проверка, что агент активировал все ожидаемые навыки. Args: expected: Список ожидаемых навыков. actual: Множес |
| `_check_iterations` | 95-112 | `(max_iterations: int, actual: int) -> CheckResult` | 6 | 2 | Проверка, что агент уложился в лимит итераций. Args: max_iterations: Максимально допустимое число итераций. ac |
| `_check_keywords_include` | 115-132 | `(keywords: list[str], response: str | None) -> CheckResult` | 5 | 2 | Проверка наличия обязательных ключевых слов в ответе. Args: keywords: Список обязательных ключевых слов. respo |
| `_check_keywords_exclude` | 135-152 | `(keywords: list[str], response: str | None) -> CheckResult` | 5 | 2 | Проверка отсутствия запрещённых ключевых слов в ответе. Args: keywords: Список запрещённых ключевых слов. resp |
| `_check_file_exists` | 155-168 | `(file_path: str, workspace: str | Path | None) -> CheckResult` | 2 | 2 | Проверка существования файла в рабочей области. Args: file_path: Путь к файлу (относительный или абсолютный).  |
| `_check_file_content` | 171-188 | `(file_path: str, expected_content: str, workspace: str | Path | None) -> CheckResult` | 3 | 2 | Проверка наличия ожидаемого содержимого в файле. Args: file_path: Путь к файлу. expected_content: Ожидаемое со |
| `_check_llm_judge` | 191-249 | `(expect: BenchExpect, response: str | None, hook: BenchmarkHook) -> CheckResult` | 14 | 2 | Оценка ответа LLM-судьёй через тот же провайдер, что использует агент. Строит промпт с целью и ответом, запраш |
| `_call_llm_json` | 252-277 | `(prompt: str) -> dict[str, Any] | None` | 1 | 2 | Вызов LLM через общий конфиг агента и парсинг JSON-ответа. Делегирует единому клиенту ``lib.services.llm_clien |
| `_resolve_path` | 280-295 | `(file_path: str, workspace: str | Path | None) -> Path` | 3 | 2 | Преобразование пути в абсолютный с учётом рабочей директории. Args: file_path: Исходный путь к файлу. workspac |
| `_aggregate_score` | 298-309 | `(checks: list[CheckResult]) -> float` | 4 | 2 | Усреднение баллов по всем проверкам. Args: checks: Список результатов проверок. Returns: Средний балл (0.0, ес |
| `_critical_checks` | 312-322 | `(checks: list[CheckResult]) -> list[CheckResult]` | 2 | 2 | Фильтрация критических проверок (инструменты, ключевые слова, файлы, LLM-судья). Args: checks: Полный список п |

## `benchmarks/reporter.py` — 260 LOC (code 211)
- module: `benchmarks.reporter`
- docstring: Формирование отчётов о результатах бенчмарков. Поддерживает сохранение в JSON и Markdown форматах, группировку по сложности и форматирование оценок.
- static importers (2): `benchmarks/runner.py`, `tests/test_benchmarks_reporter.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 8

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_score_label` | 17-33 | `(score: float) -> str` | 4 | 2 | Текстовое обозначение уровня оценки. Args: score: Числовой балл (0.0–1.0). Returns: "EXCELLENT" при score >= 0 |
| `_difficulty_label` | 36-49 | `(d: int) -> str` | 3 | 2 | Текстовое обозначение уровня сложности. Args: d: Числовой уровень сложности (1–10). Returns: "simple" при d <= |
| `save_json_report` | 52-80 | `(suite_result: SuiteResult, output_dir: str | Path) -> Path` | 4 | 2 | Сохранение результатов в JSON-формате. Создаёт summary.json и отдельные файлы detail/<item_id>.json для каждог |
| `save_markdown_report` | 83-164 | `(suite_result: SuiteResult, output_dir: str | Path) -> Path` | 24 | 2 | Сохранение результатов в Markdown-формате. Создаёт summary.md с таблицами сводки, группировки по сложности и д |
| `_suite_to_dict` | 167-188 | `(suite_result: SuiteResult) -> dict[str, Any]` | 2 | 2 | Преобразование SuiteResult в словарь для JSON-сериализации. Args: suite_result: Результаты прогона набора. Ret |
| `_result_to_dict` | 191-228 | `(r: BenchResult) -> dict[str, Any]` | 4 | 2 | Преобразование BenchResult в словарь для JSON-сериализации. Args: r: Результат выполнения задания. Returns: Сл |
| `_group_by_difficulty` | 231-244 | `(results: list[BenchResult]) -> dict[str, list[BenchResult]]` | 2 | 2 | Группировка результатов по уровню сложности. Args: results: Список результатов заданий. Returns: Словарь {метк |
| `_pct` | 247-260 | `(value: float) -> str` | 1 | 2 | Форматирование числа как процента с одним знаком после запятой. Args: value: Дробное число (0.0–1.0). Returns: |

## `benchmarks/models.py` — 234 LOC (code 202)
- module: `benchmarks.models`
- docstring: Модели данных для бенчмарков nanobot. Содержит dataclasses для описания заданий, шагов, ожиданий, результатов проверок и прогонов.
- static importers (14): `benchmarks/db.py`, `benchmarks/evaluator.py`, `benchmarks/loader.py`, `benchmarks/reporter.py`, `benchmarks/runner.py`, `benchmarks/scorer.py`, `tests/conftest.py`, `tests/test_benchmarks_db.py`, `tests/test_benchmarks_evaluator.py`, `tests/test_benchmarks_loader.py`, `tests/test_benchmarks_models.py`, `tests/test_benchmarks_reporter.py`, `tests/test_benchmarks_runner.py`, `tests/test_benchmarks_scorer.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 9, module functions: 0

### class `BenchExpect` — lines 14-36 (23 LOC), 0 methods
- bases: object
- decorators: dataclass
- docstring: Ожидаемые критерии оценки ответа агента. Attributes: tools: Список инструментов, которые должен использовать агент. skills: Список навыков, которые должен активировать агент. keywords_include: Обязательные ключевые слова
- name referenced in 9 file(s); tests: 5

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `BenchStep` — lines 40-52 (13 LOC), 0 methods
- bases: object
- decorators: dataclass
- docstring: Один шаг многошагового задания. Attributes: step: Номер шага. question: Формулировка вопроса для данного шага. weight: Вес шага в итоговой оценке. expect: Ожидаемые критерии для данного шага.
- name referenced in 5 file(s); tests: 3

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `BenchItem` — lines 56-94 (39 LOC), 2 methods
- bases: object
- decorators: dataclass
- docstring: Одно задание бенчмарка (простое или многошаговое). Attributes: id: Уникальный идентификатор задания. name: Человекочитаемое название. difficulty: Уровень сложности (1–10). category: Категория задания. type: Тип задания (
- name referenced in 9 file(s); tests: 5

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__hash__` | 88-89 | `(self) -> int` | 1 | 0 | 0 | — |
| `__eq__` | 91-94 | `(self, other: object) -> bool` | 2 | 0 | 0 | — |

### class `BenchSuite` — lines 98-108 (11 LOC), 0 methods
- bases: object
- decorators: dataclass
- docstring: Набор тестовых заданий бенчмарка. Attributes: name: Имя набора. items: Список заданий. tags: Теги для фильтрации (simple, medium, hard и т.д.).
- name referenced in 4 file(s); tests: 2

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `CheckResult` — lines 112-124 (13 LOC), 0 methods
- bases: object
- decorators: dataclass
- docstring: Результат одной проверки. Attributes: check: Имя проверки (tools, keywords_include, file_exists и т.д.). passed: Флаг успешности проверки. score: Числовой балл (0.0–1.0). detail: Текстовое описание результата.
- name referenced in 10 file(s); tests: 6

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `EvalResult` — lines 128-138 (11 LOC), 0 methods
- bases: object
- decorators: dataclass
- docstring: Результат оценки ответа агента по всем проверкам. Attributes: passed: Флаг, пройдено ли задание в целом. total_score: Итоговый средний балл. checks: Список результатов отдельных проверок.
- name referenced in 5 file(s); tests: 3

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `StepResult` — lines 142-166 (25 LOC), 0 methods
- bases: object
- decorators: dataclass
- docstring: Результат выполнения одного шага многошагового задания. Attributes: step: Номер шага. weight: Вес шага. passed: Флаг успешности. score: Балл за шаг. response: Ответ агента на шаге. tools_used: Использованные инструменты.
- name referenced in 6 file(s); tests: 3

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `BenchResult` — lines 170-204 (35 LOC), 0 methods
- bases: object
- decorators: dataclass
- docstring: Результат выполнения одного задания бенчмарка. Attributes: item_id: Идентификатор задания. item_name: Название задания. difficulty: Сложность задания. passed: Флаг успешности. total_score: Итоговый балл. response: Текст 
- name referenced in 9 file(s); tests: 4

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `SuiteResult` — lines 208-234 (27 LOC), 0 methods
- bases: object
- decorators: dataclass
- docstring: Результат прогона целого набора тестов. Attributes: suite_name: Имя набора. timestamp: Метка времени прогона. total_items: Общее количество заданий. passed_items: Количество пройденных заданий. total_score: Суммарный бал
- name referenced in 6 file(s); tests: 3

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

## `benchmarks/scorer.py` — 217 LOC (code 177)
- module: `benchmarks.scorer`
- docstring: Подсчёт взвешенных оценок для результатов бенчмарков. Определяет веса для каждого типа проверки и функции расчёта итоговых баллов для одношаговых, многошаговых заданий и отдельных шагов.
- static importers (2): `benchmarks/runner.py`, `tests/test_benchmarks_scorer.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 6

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `score_item` | 27-45 | `(item: BenchItem, eval_result: EvalResult) -> BenchResult` | 1 | 1 | Расчёт результата задания на основе оценки (без дополнительных метаданных). Args: item: Задание бенчмарка. eva |
| `score_single` | 48-87 | `(item: BenchItem, eval_result: EvalResult, response: str | None=None, tools_used: list[str` | 3 | 2 | Расчёт полного результата одношагового задания с метаданными выполнения. Args: item: Задание бенчмарка. eval_r |
| `score_step` | 90-124 | `(step_index: int, weight: float, eval_result: EvalResult, response: str | None=None, tools` | 3 | 2 | Расчёт результата одного шага многошагового задания. Args: step_index: Номер шага. weight: Вес шага в итоговой |
| `score_multi_step` | 127-179 | `(item: BenchItem, step_results: list[StepResult]) -> BenchResult` | 12 | 2 | Агрегация результатов всех шагов в итоговый результат многошагового задания. Итоговый балл = 80% взвешенная су |
| `_weighted_score` | 182-201 | `(checks: list[CheckResult]) -> float` | 5 | 2 | Расчёт взвешенного среднего балла по всем проверкам. Args: checks: Список результатов проверок. Returns: Взвеш |
| `_find_check_score` | 204-217 | `(checks: list[CheckResult], name: str) -> float | None` | 3 | 2 | Поиск балла конкретной проверки по её имени. Args: checks: Список результатов проверок. name: Имя проверки (на |

## `benchmarks/loader.py` — 173 LOC (code 130)
- module: `benchmarks.loader`
- docstring: Загрузка конфигураций бенчмарков из YAML-файлов. Поддерживает загрузку из отдельных файлов и директорий, парсинг элементов, шагов и ожидаемых критериев оценки.
- static importers (2): `benchmarks/runner.py`, `tests/test_benchmarks_loader.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 6

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `load_benchmark` | 17-33 | `(path: str | Path) -> BenchSuite` | 2 | 2 | Загрузка набора бенчмарков из файла или директории. Args: path: Путь к YAML-файлу или директории с файлами. Re |
| `_load_directory` | 36-61 | `(dir_path: Path) -> BenchSuite` | 4 | 2 | Загрузка всех YAML-файлов из директории, исключая файлы, начинающиеся с '_'. Args: dir_path: Путь к директории |
| `_load_file` | 64-96 | `(file_path: Path) -> BenchSuite` | 11 | 2 | Загрузка одного YAML-файла с бенчмарками. Args: file_path: Путь к YAML-файлу. Returns: Набор тестов из файла.  |
| `_parse_item` | 99-133 | `(data: dict[str, Any]) -> BenchItem` | 16 | 2 | Парсинг одного задания бенчмарка из словаря YAML. Args: data: Словарь с данными задания. Returns: Объект Bench |
| `_parse_expect` | 136-155 | `(data: dict[str, Any]) -> BenchExpect` | 10 | 2 | Парсинг ожидаемых критериев оценки из YAML-словаря. Args: data: Словарь с ожиданиями (tools, keywords_include, |
| `_parse_step` | 158-173 | `(data: dict[str, Any], default_step: int) -> BenchStep` | 4 | 2 | Парсинг одного шага многошагового задания. Args: data: Словарь с данными шага. default_step: Номер шага по умо |

## `benchmarks/hooks.py` — 125 LOC (code 98)
- module: `benchmarks.hooks`
- docstring: Хук для сбора метрик выполнения агента во время прогона бенчмарка. Собирает информацию о вызовах инструментов, итерациях, навыках и времени выполнения.
- static importers (4): `benchmarks/evaluator.py`, `benchmarks/runner.py`, `tests/test_benchmarks_evaluator.py`, `tests/test_benchmarks_hooks.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 0

### class `BenchmarkHook` — lines 24-125 (102 LOC), 10 methods
- bases: object
- decorators: —
- docstring: Перехватывает метрики во время выполнения агента в бенчмарке.
- name referenced in 4 file(s); tests: 2

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 27-35 | `(self) -> None` | 1 | 0 | 6 | Инициализация хука с пустыми счётчиками. |
| `before_iteration` | 37-47 | `(self, context: AgentHookContext) -> None` | 3 | 0 | 4 | Действие перед каждой итерацией агента: засекает время старта и увеличивает счётчик. Args: context: Контекст и |
| `_iter_tool_calls` | 50-51 | `(context: AgentHookContext) -> list` | 2 | 2 | 1 | — |
| `_tool_call_name` | 54-55 | `(call: Any) -> str` | 1 | 2 | 1 | — |
| `_tool_call_arguments` | 58-59 | `(call: Any) -> Any` | 1 | 1 | 1 | — |
| `before_execute_tools` | 61-72 | `(self, context: AgentHookContext) -> None` | 2 | 0 | 3 | Снимок имён и аргументов инструментов перед их выполнением. Сохраняет имена инструментов в ``tools_used`` неза |
| `after_iteration` | 74-99 | `(self, context: AgentHookContext) -> None` | 8 | 0 | 6 | Действие после каждой итерации: собирает данные о вызовах инструментов. Для каждого вызова фиксирует имя, пара |
| `finalize_content` | 101-112 | `(self, context: AgentHookContext, content: str | None) -> str | None` | 1 | 0 | 3 | Финализация контента: фиксирует время окончания. Args: context: Контекст после завершения всех итераций. conte |
| `tools_used` | 115-117 | `(self) -> list[str]` | 1 | 0 | 12 | Список инструментов, использованных агентом (отсортированный). |
| `duration_sec` | 120-125 | `(self) -> float` | 3 | 0 | 9 | Общая длительность выполнения агента в секундах. |

## `benchmarks/__init__.py` — 4 LOC (code 2)
- module: `benchmarks`
- docstring: Набор бенчмарков nanobot — автоматическая оценка качества агента.
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 0

