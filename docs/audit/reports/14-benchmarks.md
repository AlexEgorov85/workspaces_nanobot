# Аудит: `benchmarks/`

## Сводка группы

Файлов: **9** (+ `items/*.yaml`, `results/`, `README.md`) · LOC: **2671** · классов: **11** · методов: **20** · функций: **51**

**Ключевые находки**

1. **`benchmarks/db.py:52-57` — подсистема не запускается вообще.** `ValueError` на
   import-time: `config.SETTINGS` не инициализирован, а `runner.py` **никогда** не
   вызывает `config._initialize_settings(...)`. Проверено:
   `python benchmarks/runner.py --dry-run` → `ValueError: benchmark.runs_table и
   benchmark.results_table обязательны`. Даже `--dry-run` и `--compare` (не нуждаются
   в БД) падают, т.к. `runner.py:75` импортирует `benchmarks.db` на верхнем уровне.
   *Вердикт: Упростить* (см. Ф9).
2. **`benchmarks/hooks.py:24` — `BenchmarkHook` не наследует `nanobot.agent.hook.AgentHook`
   и не имеет `wants_streaming()`.** `build_agent_turn_hook()` кладёт per-run хуки в
   `CompositeHook`, чей `wants_streaming()` = `any(h.wants_streaming() for h in chain)`.
   Проверено воспроизведением сборки цепочки: `hook.wants_streaming() →
   AttributeError: 'BenchmarkHook' object has no attribute 'wants_streaming'`.
   Вызов происходит в `nanobot/agent/runner.py:889` (`_request_model`) — **до** первого
   обращения к провайдеру, вне `try`. Любой прогон падает. *Вердикт: Оставить (с
   обязательным фиксом)*.
3. **`benchmarks/hooks.py:47` — `dict(context.usage)` всегда бросает `TypeError`.**
   `context.usage` — dataclass `LLMUsage` (`__dataclass_fields__` есть, `__iter__` нет);
   проверено: `dict(u) → TypeError: 'LLMUsage' object is not iterable`. Поле `self.usage`
   **никогда** не заполняется и **нигде не читается**. *Вердикт: Удалить*.
4. **`benchmarks/runner.py:414, 443, 525` — `await` на синхронном `delete_session() -> bool`**
   (подтверждённая находка другого аудитора, проверена). Проверено: `await f()` с
   `f() -> bool` → `TypeError: 'bool' object can't be awaited`, но **вызов уже успел
   выполниться** — файл сессии удалён, а `invalidate()` (no-op в
   `PGSessionManager.invalidate`, `lib/session/pg_session_manager.py:112-114`) оставил
   запись в `self._cache`; последующий `flush_all()` в `runner.py:703` **воскрешает**
   удалённый JSONL. Тип возврата при этом теряется, а `TypeError` глушится
   `except Exception: pass`. Изоляция **между заданиями не ломается** (ключ уникален
   на item+run_id), ломается только заявленная очистка. *Вердикт: Упростить* (убрать `await`).
5. **`benchmarks/runner.py:695` — `ctx.agent.close_mcp()` не существует** в nanobot 0.3.5
   (проверено: `hasattr(AgentLoop, "close_mcp") == False`, есть `aclose`). `AttributeError`
   глушится; MCP-провайдеры не закрываются. *Вердикт: Упростить*.
6. **`benchmarks/db.py:154` — `save_run()` гарантированно падает** при `--db`:
   `suite_result.config` — обычный `dict`, psycopg2 его не адаптирует
   (проверено: `ProgrammingError: can't adapt type 'dict'`). Соседний `suite_tags` обёрнут
   в `Json()` — т.е. автор знал про адаптер и забыл про `config`. Ошибка глушится
   в `runner.py:973`. *Вердикт: Упростить*.
7. **`benchmarks/db.py:189-194` — колонки/значения в `INSERT` разъехались.** `difficulty`
   получает `r.total_score`, `item_name` и `category` получают `""`. *Вердикт: Упростить*.
8. **56 из 57 YAML-заданий ожидают несуществующие tool'ы**
   (`run_predefined_script`, `vector_search`, `nl_sql_generate`, `duckdb_query`).
   Ни одного из них нет ни в `workspace/tools/`, ни в `nanobot/agent/tools/`. Так как
   `tools` — **критическая** проверка (`evaluator.py:315-320`), 56/57 заданий
   гарантированно `passed=False` даже после фиксов 1–2. *Вердикт: Упростить* (переписать набор).
9. **Двойной счёт метрик с расхождением: `evaluator.py:298-309` (`_aggregate_score`,
   среднее) против `scorer.py:182-201` (`_weighted_score`, взвешенное).**
   `EvalResult.total_score` (среднее) используется для решения `passed`
   (`evaluator.py:54`), а `BenchResult.total_score` (взвешенное) — для отчётов. Один и
   тот же прогон получает два разных балла. `EvalResult.total_score` иначе нигде не
   читается. *Вердикт: Упростить*.
10. **Мёртвый слой внутри подсистемы:** `db.py` дублирует `sys.path`-хак
    (`db.py:31`, `hooks.py:14-20`, `runner.py:60-68` — три копии); `db.py` **не создаёт
    третий пул** — переиспользует `workspace/utils/db.py` (это ответ на вопрос №2 брифа);
    `get_history`/`compare_runs`/`_compare_runs_inner`/`_is_greenplum` (86 LOC) не имеют
    ни одного production-вызова; `score_item`, `_detect_run_id`, `hook.skills`,
    `BenchItem.new_session/context_files/timeout/max_iterations`, `args.config`,
    `args.no_audit` — мёртвые. *Вердикт: Удалить*.
11. **Хуки `benchmarks/hooks.py` функционально не дублируют `lib/hooks/`** (в ответе на
    вопрос №5): `ToolAuditHook` пишет в PG, `DatabaseLoggingHook` — turn-метрики в
    `agent_gateway_logs`; `BenchmarkHook` — in-process счётчики для отчёта. Дублируется
    только `self.usage` (и он сломан). Дешевле чинить хук, чем удалять.
12. **Бенчмарк не может измерять расход токенов/стоимость** (вопрос №8). `RunResult.usage`
    (`LLMUsage`) заполняется наном-агентом, но `runner.py` его не читает; единственный
    собственный канал `hook.usage` сломан и не используется. Мёртвый
    `llm_observer.py`+`llm_usage_store_factory.py` **не является причиной** — данные
    доступны без него. *Вердикт: Упростить* (добавить чтение `result.usage`).
13. **Тесты зелёные (225 passed), продукт мёртв** — все 8 test-файлов мокают ровно те
    места, где ломается прод. `benchmarks/results/runs/` содержит 20 каталогов, созданных
    **тестами** (`suite=test-suite`, без `summary.json`) — реальных прогонов не было ни разу.
14. **Подсистема не подключена ни к CI, ни к процедуре** (вопрос а): `grep -i benchmark .github/`
    → 0 совпадений. `ruff` в CI гоняется на `lib workspace tests tools` — `benchmarks/`
    не линтится; локально `ruff check benchmarks` даёт **31 ошибку**.
15. **`_run_item` не изолирует упавшее задание** (вопрос в): `try/finally` без `except`
    (`runner.py:376`), а `evaluate`/`score_single` лежат вне `try` внутри `_run_single`
    (`runner.py:427-441`). Любое исключение оценки/скоринга убивает весь прогон
    и приводит к `UnboundLocalError` на `runner.py:957`.

**Вердикты:** Оставить 34 · Упростить 25 · Удалить 12 · Слить/Перенести 2 · НЕ РАЗОБРАНО 0

---

## `benchmarks/runner.py` — 996 LOC

**Назначение.** CLI-точка входа подсистемы: разбор аргументов, подъём
`ApplicationContext`, прогон набора YAML-заданий через SDK-обёртку `Nanobot`,
агрегация и запись отчётов.
**Что делает.** Поднимает `ApplicationContext(role='gateway')` один раз на прогон
(`runner.py:596`), прогревает FAISS (`runner.py:621-634`), гоняет задания
(`runner.py:656-670`), в `finally` гасит контекст (`runner.py:692-708`). Побочные
эффекты: **временно переписывает `config.json`** ради `--model` (`runner.py:566-589`)
и восстанавливает его в `finally`; удаляет файлы агента в `_cleanup_item`; удаляет
старые каталоги прогонов (`cleanup_old_runs`); пишет `progress.log`.
**Зачем нужен.** Единственная точка входа; без него остальные 8 модулей недостижимы.
**Вердикт.** `Оставить` — но требует 4 обязательных фикса (Ф1, Ф2, Ф4, Ф5) иначе
не запускается в принципе.
**Обоснование.** Функциональность нужна и уникальна; проблема не в ней, а в том, что
она за 8 месяцев не была ни разу выполнена в проде, и это не поймал ни один тест.
**Доказательства.** 0 production-импортёров (ожидаемо для CLI). Тесты:
`tests/test_benchmarks_runner.py` (29.6 KB). 0 из 17 функций вызываются из
`tests/` вне моков; `main` — единственная точка входа.

### Модульные функции

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_emit` | 52-73 | Вывод в stdout + progress.log | Единая точка логирования прогона | `main_async` (7 вызовов), `_run_suite` | Оставить |
| `_detect_run_id` | 94-100 | ID прогона из таймстемпа | — | никто | **Удалить** |
| `_generate_run_id` | 103-113 | Единый UUID4-прогон (dir + DB + session_key) | Связывает отчёты, БД и ключи сессий | `main_async:926` | Оставить |
| `_parse_args` | 116-158 | argparse | CLI-контракт | `main_async:800` | Упростить — убрать `--config` (мёртвый) и `--no-audit` (не влияет на поведение) |
| `_filter_items` | 161-215 | Фильтры tags/category/difficulty/mode | Отбор подмножества | `main_async:814` | Оставить |
| `_format_checks_failures` | 218-230 | Список проваленных проверок в строку | Читаемый лог | `_print_summary:262` | Оставить |
| `_print_summary` | 233-264 | Сводка в stdout | Обратная связь | `main_async:978` | Оставить |
| `cleanup_old_runs` | 267-294 | Удаление старых каталогов прогонов | Ротация диска | `main_async:976` | Оставить (но см. Ф13 про вызов из тестов) |
| `_cleanup_item` | 297-351 | Удаление артефактов задания | Изоляция между заданиями | `runner.py:376` (finally) | Оставить |
| `_run_item` | 354-377 | Диспетчер single/multi + cleanup | — | `_run_suite:659` | **Упростить** — добавить `except` |
| `_run_single` | 380-447 | Одношаговое задание | Основной путь исполнения | `_run_item:373` | Оставить (фикс Ф4) |
| `_run_multi_step` | 450-529 | N шагов в одной сессии | Сценарии с контекстом | `_run_item:375` | Оставить (фикс Ф4) |
| `_run_suite` | 532-708 | Подъём контекста + цикл заданий | — | `main_async:946` | Оставить (фикс Ф5) |
| `_do_compare` | 711-757 | Diff двух прогонов по JSON | Регрессия между запусками | `main_async:804` | **Упростить** — фикс процентной вёрстки |
| `_validate_items` | 760-789 | Пре-фlight проверки YAML | Ранний отказ | `main_async:815` | Оставить |
| `main_async` | 792-980 | Асинхронный entrypoint | — | `main:987` | Упростить (см. Ф1) |
| `main` | 983-992 | `asyncio.run` обёртка | `python benchmarks/runner.py` | CLI | Оставить |
| `_get_llm_model` | — | — | — | — | Не существует (см. прим. к Ф8) |

**Функциональные дефекты, привязанные к строкам:**

- **`runner.py:52-73` — `_emit` теряет перевод строки.** `text = msg + ("" if end == "\n" else end)`.
  Для дефолтного `end="\n"` текст пишется **без** `\n`. Доказательство: все 20 файлов
  `benchmarks/results/runs/*/progress.log` содержат весь прогон одной строкой
  (`=== Лог прогона бенчмарка ===ID прогона: …`). Лог нечитаем.
- **`runner.py:594, 596-599, 636-640` — `--no-audit` не делает ничего.**
  `enable_audit` вычисляется, но **не передаётся** в `ApplicationContext.create(...)`
  (нет такого параметра), а используется только для печати предупреждения. Флаг
  `--no-audit` — чистый no-op; комментарий `runner.py:593` «enable_audit=False при
  --no-audit» **лжёт** (след удалённого API-параметра).
- **`runner.py:146-147, 566` — `--config` мёртв.** Парсится, но `args.config` не читается
  нигде; вместо него жёстко используется `BENCH_SCRIPT_DIR / "config.json"`.
- **`runner.py:594, 617, 636-640` — дубль логики.** `enable_audit` (из флага) и
  `audit_ready` (из `ctx.cache_store`) — два разных источника одного и того же вопроса;
  флаг мёртв, `audit_ready` жив.
- **`runner.py:587-589` — частичное применение `--model`.** При ошибке
  `original_config = None` выставляется **после** частичной записи, поэтому
  `finally`-восстановление (`runner.py:603`) пропускается и повреждённый `config.json`
  остаётся на диске. Комментарий `runner.py:601-602` («иначе следующие запуски будут
  гонять на чужой модели») прямо обещает обратное.
- **`runner.py:622-624` — hard dependency на приватный API.** `ctx.preload_service` —
  нет в контракте `ApplicationContext.create`; при `no_audit` атрибут может отсутствовать
  → `AttributeError` **вне try** (в `try` только `ctx.start()`, `runner.py:618`).
- **`runner.py:645-646` — `Nanobot(ctx.agent, config=ctx.config)`.** Проверено по
  `nanobot.py: run()`: `config` — keyword-only, позиционный первый аргумент — `loop`.
  Формально корректно, но это обход публичного контракта: `AgentLoop` уже умеет
  `process_direct(hooks=...)` сам.
- **`runner.py:692-708` — блок shutdown ничего не делает.** Из трёх `try`: `close_mcp`
  (Ф5) и `sessions.flush_all` (восстанавливает «удалённые» сессии, Ф4) — обе операции
  нооперационны; работает только `ctx.stop()`.
- **`runner.py:610-616` — комментарий описывает удалённое поведение.** Упоминает
  «коллэки синка», «ожидать первого sync нечего», change `drop-local-cache-read-from-pg`
  — четырёхстрочный комментарий-некролог поверх трёх строк кода. Также `suite_start`
  (`:651`) присваивается и не используется.
- **`runner.py:78` и `runner.py:526` — `await` на sync-API (Ф4).**
- **`runner.py:375-376` — `verbose` в `_run_multi_step` не используется** (в отличие от
  `_run_single`, где `runner.py:402` печатает). Мёртвый параметр.
- **`runner.py:264` — подсказка «run with --verbose» врёт:** `_print_summary` не печатает
  ответы агента в verbose-режиме; verbose влияет только на `log_level` (`:807`).
- **Нет таймаутов.** `BenchItem.timeout` парсится в `loader.py`, но `runner.py` его
  игнорирует — `asyncio.wait_for`/`TimeoutError` в подсистеме отсутствуют полностью.
  `max_iterations` — только валидационное предупреждение (`:783`), не enforcement.

---

## `benchmarks/db.py` — 340 LOC

**Назначение.** Запись результатов прогона в PostgreSQL.
**Что делает.** `__init__` **перенастраивает глобальный общий пул** вызовом
`configure(dsn)` (`db.py:88`) — это не новый пул, а подмена DSN у пула
`workspace/utils/db.py`. `ensure_tables` читает DDL из `sql/benchmarks/*.sql` с
`CREATE TABLE IF NOT EXISTS` (в PG нет `IF NOT EXISTS` — только в GP), т.е. таблицы
должны быть созданы заранее.
**Зачем нужен.** Опциональное долговременное хранилище для `--db`.
**Вердикт.** `Упростить` — файл на 60% состоит из мёртвого кода и содержит
два несовместимых с psycopg2 бага, которые делают основной метод нерабочим.
**Обоснование.** Канонический коннектор — `workspace/utils/db.py`; `db.py` его
переиспользует (третьего пула нет), но дублирует `sys.path`-хак, 86 LOC истории
прогонов без вызовов и логику выбора DDL, которой нет. `db_logging_service.py`
не дублируется: у него другая задача (fire-and-forget очередь в
`agent_gateway_logs`, свой воркер-пул) — пересечение только на уровне
`utils.db`.
**Доказательства.** Импортируется `runner.py:75` (на верхнем уровне) и
`tests/test_benchmarks_db.py`. 0 production-вызовов `get_history`/`compare_runs`.

| Символ | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_is_greenplum` | 64-72 | Определить Greenplum | — | никто (только тест) | **Удалить** |
| `class BenchmarkDB` | 75-340 | PG-хранилище результатов | `--db` | `runner.py:968` | Упростить |
| `BenchmarkDB.__init__` | 78-91 | Конфигурирует общий пул | — | `runner.py:968` | Оставить |
| `BenchmarkDB.ensure_tables` | 93-112 | DDL из `sql/benchmarks/` | Первичная инициализация | `runner.py:969` | **Упростить** — убрать обещание выбора DDL, которого нет |
| `BenchmarkDB.save_run` | 114-127 | Транзакция + `_save_run_inner` | — | `runner.py:970` | Оставить (после Ф6/Ф7) |
| `BenchmarkDB._save_run_inner` | 129-207 | INSERT прогонов + результатов | — | `save_run:126` | **Упростить** — Ф6, Ф7 |
| `BenchmarkDB._result_details` | 210-241 | JSONB: checks + steps | Полнота в БД | `_save_run_inner:189` | Оставить |
| `BenchmarkDB.get_history` | 243-264 | История прогонов набора | — | никто | **Удалить** |
| `BenchmarkDB.compare_runs` | 266-280 | Сравнение по ID | — | никто | **Удалить** |
| `BenchmarkDB._compare_runs_inner` | 282-340 | Внутренняя логика compare | — | `compare_runs:279` (только из мёртвого) | **Удалить** |

**Функциональные дефекты:**

- **`db.py:52-57` (Ф1) — `raise ValueError` на import-time при неинициализированных
  `SETTINGS`.** `except Exception` на `:46` глушит исходный `ConfigurationError`,
  подставляя пустые строки и превращая diagnosable ошибку в невнятную. Это
  блокирует **весь** CLI. Проверено: `python benchmarks/runner.py --dry-run` падает.
- **`db.py:88` — `configure(dsn)` перенастраивает глобальный пул.** Вызывается
  `BenchmarkDB(dsn=args.db)` уже **после** `ctx.stop()` (`runner.py:966-970` в
  `main_async`, а `_run_suite` уже закрыл контекст), поэтому не мешает агенту —
  но это неявный глобальный side-effect у конструктора.
- **`db.py:154` (Ф6) — `suite_result.config` (`dict`) не адаптируется psycopg2.**
  Проверено: `psycopg2.extensions.adapt({...}) → ProgrammingError: can't adapt type
  'dict'`. `suite_tags` на `:153` обёрнут в `Json()`. Ошибка глушится в
  `runner.py:973-974` → **`--db` молча не сохраняет ничего**.
- **`db.py:189-194` (Ф7) — рассогласование колонок и значений.** Порядок колонок
  `(run_id, item_id, item_name, difficulty, category, item_type, passed, score, …)`,
  порядок значений `(run_id, r.item_id, "", r.total_score, "", "single"/"multi_step",
  r.passed, r.total_score, …)`. Итог: `difficulty ← total_score` (число вместо
  уровня 1-10), `item_name` и `category` теряются. При этом `item_name TEXT NOT NULL`
  в DDL, а `BenchResult` **не имеет** поля `category` вообще — т.е. `item_type`
  наполовину дублирует его.
- **`db.py:93-112` — `ensure_tables` docstring обещает «автоматически выбирает между
  PG 9.4 и GP 6.25 DDL`, выбора нет** (нет вызова `_is_greenplum`). Плюс
  `CREATE TABLE IF NOT EXISTS` в DDL рассчитан на Greenplum, тогда как проект
  заявлен как PG/Greenplum 6.5.
- **`db.py:31-46` — дубль `sys.path`-хака** из `runner.py:60-68` и `hooks.py:14-20`
  (три копии в подсистеме, плюс четвёртая в `tools/`).

---

## `benchmarks/evaluator.py` — 322 LOC

**Назначение.** Проверка ответа агента по критериям из YAML.
**Что делает.** Строит список `CheckResult` (tools, skills, iterations, keywords,
files, опциональный LLM-as-judge) и агрегирует в `EvalResult`. LLM-судья —
**побочный сетевой вызов** через `lib.services.llm_client.call_llm_json` с
`max_retries=0`, `temperature=0.0`.
**Зачем нужен.** Единственная реализация семантических критериев.
**Вердикт.** `Оставить` с обязательным устранением двойного счёта (Ф9).
**Обоснование.** Ядро оценки не дублируется нигде; проблема — в границе с `scorer.py`.
**Доказательства.** Импортируется `runner.py:71`, `tests/test_benchmarks_evaluator.py`.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `evaluate` | 20-56 | Сбор checks + агрегация | Точка входа оценки | `runner.py:426, 517` | Упростить (Ф9) |
| `_check_tools` | 59-74 | Все ли tool'ы использованы | Критический критерий | `evaluate:31` | Оставить |
| `_check_skills` | 77-92 | Все ли навыки активированы | — | `evaluate:35` | **Упростить** — `hook.skills` всегда пуст, критерий всегда vacuous-pass |
| `_check_iterations` | 95-112 | Бюджет итераций | Метрика эффективности | `evaluate:39` | Оставить |
| `_check_keywords_include` | 115-132 | Обязательные подстроки | Критический критерий | `evaluate:43` | Оставить |
| `_check_keywords_exclude` | 135-152 | Запрещённые подстроки | Анти-паттерн | `evaluate:47` | Оставить |
| `_check_file_exists` | 155-168 | Наличие файла | Критический критерий | `evaluate:51` | Оставить |
| `_check_file_content` | 171-188 | Подстрока в файле | Критический критерий | `evaluate:51` | Оставить |
| `_check_llm_judge` | 191-249 | LLM-оценка по рубрике | Субъективные критерии | `evaluate:55` | Оставить |
| `_call_llm_json` | 252-277 | Обёртка над `llm_client` | Единый LLM-клиент | `_check_llm_judge:243` | Оставить |
| `_resolve_path` | 280-295 | Путь относительно workspace | Изоляция FS | `_check_file_exists:163, _check_file_content:180` | Оставить |
| `_aggregate_score` | 298-309 | **Среднее** по checks | — | `evaluate:53` | **Упростить** (Ф9) |
| `_critical_checks` | 312-322 | Фильтр критических | Гейт `passed` | `evaluate:54` | Оставить |

**Функциональные дефекты:**

- **Ф9 — двойной счёт.** `evaluate:53` считает `_aggregate_score` (среднее),
  `evaluate:54` на нём же решает `passed`. `scorer.py:59` считает `_weighted_score`
  (взвешенное) и кладёт его в `BenchResult.total_score`, который идёт в отчёты
  (`reporter.py:213`) и в БД (`db.py:194`). Итог: `Passed: True` при
  `Total Score: 42%` — два несовместимых определения качества в одном прогоне.
- **`evaluator.py:32-38` — `hook.skills` всегда пустое множество.** `BenchmarkHook`
  не пишет в `self.skills` нигде. `_check_skills` при непустом `expect.skills`
  всегда вернёт `passed=False`; при пустом (сейчас всегда) — вакуумный pass с
  весом 0.10. Мёртвый код + искажение весов.
- **`evaluator.py:35` — `expect.skills` недостижим из YAML-контракта:** поле
  есть в `BenchExpect` и `_template.yaml`, но ни в одном рабочем YAML не заполнено.
- **`evaluator.py:190-249` — LLM-судья в тестах не исполняется, но выполняет
  реальный сетевой вызов в проде** с `max_retries=0`: при таймауте/500 фича
  молча деградирует в `passed=True, score=0.5` (`:247`) — **провал не штрафуется**.
- **`evaluator.py:78-92` — `match_type` в `_check_skills` не проверяется**;
  `match_type: any` в YAML-контракте (`_template.yaml`) нигде не реализован.

---

## `benchmarks/reporter.py` — 260 LOC

**Назначение.** Сериализация результатов в `summary.json`, `summary.md`,
`detail/<id>.json`.
**Что делает.** Пишет 2 + N файлов в `output_dir`; Markdown — сводка +
группировка по сложности + таблицы проверок + секция multi-step.
**Зачем нужен.** Единственный формат вывода; без него результаты нечитаемы.
**Вердикт.** `Оставить` с одним обязательным исправлением (проценты от суммы).
**Обоснование.** Функция нужна и полна; недооценённых полей для решения о качестве
хватает с избытком, а вот поля для расхода токенов нет (Ф12) — это пробел, а не
избыточность.
**Доказательства.** `runner.py:959-960`, `tests/test_benchmarks_reporter.py`.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_score_label` | 17-33 | Текстовый уровень оценки | Читаемость | `save_markdown_report` (3 вызова) | Оставить |
| `_difficulty_label` | 36-49 | simple/medium/hard | — | `save_markdown_report`, `_group_by_difficulty` | Оставить |
| `save_json_report` | 52-80 | `summary.json` + `detail/*.json` | Машиночитаемость | `runner.py:959` | Оставить |
| `save_markdown_report` | 83-164 | `summary.md` | Читаемость | `runner.py:960` | Оставить |
| `_suite_to_dict` | 167-188 | Сериализация `SuiteResult` | — | `save_json_report:70` | Оставить |
| `_result_to_dict` | 191-228 | Сериализация `BenchResult` | — | `_suite_to_dict:181` | Оставить |
| `_group_by_difficulty` | 231-244 | Группировка по сложности | Навигация | `save_markdown_report:106` | Оставить |
| `_pct` | 247-260 | `value*100` с одним знаком | — | `save_markdown_report:110, 111, 213` | Оставить |

**Функциональные дефекты:**

- **`reporter.py:111` и `:213` — `total_score` форматируется как процент, но это
  СУММА, а не доля.** `SuiteResult.total_score = sum(r.total_score for r in results)`
  (`runner.py:675`). Для набора из 57 заданий строка `| Total Score | 4100.0% |`
  бессмысленна. То же в `_print_summary` (`runner.py:246`) и `_do_compare`
  (`runner.py:737, 741` — `{run1['total_score']:7.1%}`). У `SuiteResult` есть
  корректный `avg_score`, который не используется ни в одной агрегации репортера.
- **`reporter.py:111` vs `:213` — два разных показателя** в одном файле: в Summary
  это сумма, в Per-item Breakdown это доля одного задания. Визуально неотличимо.
- **Нет полей расхода токенов/стоимости** (Ф12) — при том что `RunResult.usage`
  доступен, а весь смысл бенчмарка в решении «стоит ли эта конфигурация».
- **Мёртвых веток формата нет** — `_pct`/`_score_label`/`_difficulty_label`
  покрывают все значения из реальных наборов. Пункт вопроса №7 закрыт чисто.

---

## `benchmarks/models.py` — 234 LOC

**Назначение.** dataclass-модели предметной области бенчмарка.
**Что делает.** Чистые структуры данных без логики; единственный файл подсистемы,
который импортируется из `tests/conftest.py` (общие фикстуры).
**Зачем нужен.** Единый контракт данных между loader/evaluator/scorer/reporter/db.
**Вердикт.** `Оставить`, с удалением 6 мёртвых полей.
**Обоснование.** Стабильный и корректный; мёртвые поля — следствие расхождения
YAML-контракта с рантаймом (Ф8) и двух слоёв скоринга (Ф9).
**Доказательства.** 14 импортёров (6 модулей подсистемы + 8 test-файлов) —
самый востребованный модуль; `tests/conftest.py:105-110`.

| Класс | Строки | Назначение | Зачем нужен | Вердикт |
|---|---|---|---|---|
| `BenchExpect` | 14-36 | Критерии оценки | Контракт YAML→оценщик | Упростить — `skills` мёртв (Ф9) |
| `BenchStep` | 40-52 | Шаг multi_step | Контракт YAML | Оставить |
| `BenchItem` | 56-94 | Задание | Контракт YAML | Упростить — 4 мёртвых поля |
| `BenchSuite` | 98-108 | Набор заданий | Контракт YAML | Оставить |
| `CheckResult` | 112-124 | Результат одной проверки | Ядро отчётов | Оставить |
| `EvalResult` | 128-138 | Итог оценки | Промежуточный слой | **Упростить** — `total_score` мёртв (Ф9) |
| `StepResult` | 142-166 | Результат шага | multi_step | Оставить |
| `BenchResult` | 170-204 | Результат задания | Единица отчёта | Оставить |
| `SuiteResult` | 208-234 | Результат прогона | Единица отчёта | Оставить |

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `BenchItem.__hash__` | 88-89 | Хэш по id | — | никто (нет `set[BenchItem]`) | **Удалить** |
| `BenchItem.__eq__` | 91-94 | Сравнение по id | — | никто | **Удалить** |

**Мёртвые поля (вопрос №6):**

- `BenchItem.new_session` — парсится (`loader.py:120`), но `runner.py` его не читает;
  изоляция обеспечивается уникальным `session_key`, а не флагом. В YAML: 3 задания
  с `new_session: false` — все multi_step, где флаг и так не имеет смысла.
- `BenchItem.context_files` — парсится (`loader.py:121`), не используется. В YAML: 0.
- `BenchItem.timeout` — парсится (`loader.py:124`), **не enforced**: в подсистеме нет
  `asyncio.wait_for`. В YAML присутствует почти везде — иллюзия контроля.
- `BenchItem.max_iterations` — только валидационное предупреждение
  (`runner.py:783`); enforcement происходит только через `expect.max_iterations`
  (это **другое поле**, у `BenchExpect`).
- `BenchExpect.skills` / `BenchResult.skills_activated` / `BenchmarkHook.skills` —
  тройка мёртвых полей (Ф9). `skills_activated` всегда `[]` в JSON, MD и БД.
- `BenchExpect.match_type: any` — заявлен в `_template.yaml`, не реализован ни в
  одном `_check_*`.
- `EvalResult.total_score` — вычисляется, но не читается: `scorer.py` использует
  только `eval_result.passed` и `.checks`.
- `StepResult.details` — заполняется только в ветке ошибки (`runner.py:496`) и **никогда
  не сериализуется**: ни `_result_to_dict`, ни `_result_details`, ни Markdown.

**Согласованность пути к сценариям (вопрос №6).** `loader.load_benchmark` принимает
путь; дефолт `ITEMS_DIR = <repo>/benchmarks/items` (`runner.py:57`). Это **отдельный
каталог**, а не `workspace/skills/legal_summarizer/...`. Связь со скиллом опосредованная
и только через имя в вопросе задания — `_template.yaml` даже предлагает `mode: vector`,
которого в `BenchItem` **нет** (silently игнорируется `loader.py:110`).

---

## `benchmarks/scorer.py` — 217 LOC

**Назначение.** Итоговые баллы: single / multi_step / отдельный шаг.
**Что делает.** Задаёт веса проверок (`CHECK_WEIGHTS`), считает взвешенное среднее
`_weighted_score`, агрегирует шаги multi_step как
`0.8 * weighted_step + 0.2 * (passed_steps / total_steps)`.
**Зачем нужен.** Единственное место, где балл превращается в вердикт.
**Вердикт.** `Оставить` — но разрешить конфликт с `evaluator.py` (Ф9).
**Обоснование.** Логика взвешивания осмысленна и покрыта тестами; проблема в том,
что она **перекрывает** агрегацию `evaluator.py`, а не заменяет её.
**Доказательства.** `runner.py:72`, `tests/test_benchmarks_scorer.py`.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `score_item` | 27-45 | Минимальный расчёт | — | никто | **Удалить** |
| `score_single` | 48-87 | Итог single | Основной путь | `runner.py:432` | Оставить |
| `score_step` | 90-124 | Итог шага | multi_step | `runner.py:512` | Оставить |
| `score_multi_step` | 127-179 | Агрегация шагов | multi_step | `runner.py:525` | Оставить |
| `_weighted_score` | 182-201 | Взвешенное среднее | Итоговый балл | `score_single:60`, `score_step:99`, `score_multi_step:160` | Оставить |
| `_find_check_score` | 204-217 | Балл проверки по имени | Извлечение | `score_single:70-76` | Оставить |

**Дефекты:**

- **Ф9 — `score_item` (без метаданных) мёртв**, а `score_single` — его копия с
  8 аргументами. Удаление `score_item` безопасно.
- **`scorer.py:9` — неиспользуемый импорт `BenchExpect`** (единственная из 31
  ruff-ошибки в подсистеме, попадающая в эту строку).
- **`scorer.py:52` — `passed` берётся из `EvalResult` (Ф9), `total_score` считается
  заново.** Явный признак двух независимых определений качества.
- **`scorer.py:139-158` — при `not step_results` возвращается `total_score=0.0`,
  `passed=False` без `error`.** Задание молча помечается проваленным; причина
  (0 шагов) диагностируется только чтением YAML.

---

## `benchmarks/loader.py` — 173 LOC

**Назначение.** Загрузка YAML-сценариев в `BenchSuite`.
**Что делает.** Принимает файл или директорию (`_`-файлы пропускаются), парсит
`items` → `BenchItem` → `BenchStep` + `BenchExpect`. Молча игнорирует неизвестные
ключи (нет схемы и нет `ProjectSettings`-валидации).
**Зачем нужен.** Единственный вход YAML-контракта.
**Вердикт.** `Оставить`.
**Обоснование.** Прост и корректен; проблема не в коде, а в данных (Ф8) и в
отсутствии fail-fast на неизвестных ключах.
**Доказательства.** `runner.py:70`, `tests/test_benchmarks_loader.py`.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `load_benchmark` | 17-33 | Точка входа | — | `runner.py:802` | Оставить |
| `_load_directory` | 36-61 | Все YAML из директории | — | `load_benchmark:26` | Оставить |
| `_load_file` | 64-96 | Один YAML | — | `load_benchmark:23`, `_load_directory:44` | Оставить |
| `_parse_item` | 99-133 | Словарь → `BenchItem` | — | `_load_file:85` | Упростить (валидация `id`/`difficulty` уже есть, но `steps` для `single` игнорируется молча) |
| `_parse_expect` | 136-155 | Словарь → `BenchExpect` | — | `_parse_item:126`, `_parse_step:170` | Оставить |
| `_parse_step` | 158-173 | Словарь → `BenchStep` | — | `_parse_item:131` | Оставить |

**Дефекты:**

- **`loader.py:36-61` — `tags` на уровне набора НЕ собираются из YAML.** Коллекция
  помечена `tags: list[str] = []` и ни разу не заполняется; на выходе `tags=[]`
  для всех 5 файлов. `--tags` фильтрует по `item.difficulty`, а не по этим тегам
  (`runner.py:191-197`) — то есть имя фильтра не соответствует семантике.
  Подтверждено: `SuiteResult.config["tags"]` всегда `[]`.
- **`loader.py:99-133` — нет валидации против `BenchItem`.** Неизвестные ключи
  (например, предлагаемый `_template.yaml: mode: vector`) молча теряются.
  `type` проверяется на `("single", "multi_step")` (`:114`), но `item.type` из YAML
  при отсутствии ключа дефолтится в `single` — опечатка в значении даст
  `_validate_items`-предупреждение, а опечатка в регистре — тихий `single`.
- **`loader.py:124` — `timeout` парсится, но не enforced** (см. Ф8/модели).

---

## `benchmarks/hooks.py` — 125 LOC

**Назначение.** In-process сбор метрик одного прогона (tool'ы, итерации, время).
**Что делает.** Реализует 4 хук-метода nanobot (`before_iteration`,
`before_execute_tools`, `after_iteration`, `finalize_content`) плюс 2 свойства.
**Зачем нужен.** Единственный источник `tools_used`/`total_iterations`/`duration_sec`
для отчётов; без него 4 колонки БД и 3 поля отчётов пусты.
**Вердикт.** `Оставить` — но **обязательный фикс Ф2** (наследование `AgentHook`),
иначе хук нерабочий; `usage` удалить.
**Обоснование.** Ответ на вопрос №5: хуки **функционально не дублируют**
`lib/hooks/`. `ToolAuditHook` — аудит вызовов в PG, `DatabaseLoggingHook` —
per-turn телеметрия в `agent_gateway_logs`; обе работают в другом процессе/БД-схеме
и не дают in-process объекта для бенчмарка. Переиспользовать `DatabaseLoggingHook`
нельзя: он per-turn-инстанс (`hook_factories`) и пишет в БД, а бенчмарку нужен
объект в памяти. **Дешевле починить, чем удалять.** Единственное реальное
дублирование — `self.usage` (и он сломан).

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 25-35 | Счётчики | — | `runner.py:398, 477` | Упростить (убрать `usage`) |
| `before_iteration` | 37-47 | Старт времени + счётчик итераций | Метрика итераций | nanobot | Упростить (убрать строки 46-47, Ф3) |
| `_iter_tool_calls` | 50-51 | Итерация вызовов | Совместимость версий | `before_execute_tools:64`, `after_iteration:79` | Оставить |
| `_tool_call_name` | 54-55 | Имя tool'а | Адаптер к API | `_iter_tool_calls:51` | Оставить |
| `_tool_call_arguments` | 58-59 | Аргументы tool'а | — | `after_iteration:82` | Оставить |
| `before_execute_tools` | 61-72 | Снимок имён до выполнения | Не потерять вызовы при ошибке | nanobot | Оставить |
| `after_iteration` | 74-99 | Полный tool_calls + `_tool_events` | Основной сбор | nanobot | Оставить |
| `finalize_content` | 101-112 | Время окончания | Метрика длительности | nanobot | Оставить |
| `tools_used` | 115-117 | Отсортированный список | Критерий `_check_tools` | `runner.py:435, 528`, `evaluator.py:31` | Оставить |
| `duration_sec` | 120-125 | Секунды | Метрика | `runner.py:438` | Оставить |

**Дефекты:**

- **`hooks.py:24` (Ф2) — класс не наследует `AgentHook` и не имеет `wants_streaming()`.**
  Проверено воспроизведением цепочки `build_agent_turn_hook()`:
  `CompositeHook([AgentProgressHook, AgentHook, BenchmarkHook]).wants_streaming()` →
  `AttributeError`. Вызов — `nanobot/agent/runner.py:889`, вне `try`, до первого
  обращения к провайдеру. Также нет `_reraise` — при поправке нужно учитывать, что
  `CompositeHook._for_each_hook_safe` читает `getattr(h, "_reraise", False)`.
  Фикс: `class BenchmarkHook(AgentHook): def __init__(self): super().__init__()`.
- **`hooks.py:46-47` (Ф3) — `dict(context.usage)` → `TypeError` каждый вызов.**
  `context.usage` — `LLMUsage` (dataclass, не итерируемый); проверено. Исключение
  глушится `CompositeHook._for_each_hook_safe`, поэтому поле `self.usage` **всегда**
  `{}` и **никогда** не читается ни в одном отчёте. Строка — мёртвая.
- **`hooks.py:12, 18` — дубликат `sys.path`-хака** из `runner.py:60-68` и `db.py:31`.
- **`hooks.py:74-99` — `_tool_events` собирается, но не используется**: ни
  `BenchResult`, ни `reporter`, ни `db` не несут tool-аргументы. 8 LOC + поле в
  хуке в пользу пустоты.
- **`hooks.py:35` — `self._tool_names: set[str]`** заполняется
  (`before_execute_tools:66`, `after_iteration:80`), но `tools_used` возвращает
  `sorted(self._tool_calls)` (`:116`) — множество мертвее. Дублирующее состояние.

---

## `benchmarks/__init__.py` — 4 LOC

**Назначение.** Маркер пакета.
**Что делает.** Ничего, кроме docstring и `__version__ = "0.1.0"`.
**Зачем нужен.** Без него `python benchmarks/runner.py` не находит соседние
модули (`from benchmarks.db import ...`).
**Вердикт.** `Оставить`.
**Обоснование.** Функционально необходим для запуска как из `python
benchmarks/runner.py`, так и из pytest. `__version__` не читается нигде в
репозитории (проверено grep), но это безвредно и типично для пакетного `__init__`.
**Доказательства.** 0 прямых импортов; работает как namespace-пакет.

---

## Ответы на ключевые вопросы брифа

**(а) Подключена ли к CI или процедуре.** Нет. `grep -i benchmark .github/` → 0
совпадений (`ci.yml` содержит только `pytest tests` и `ruff check lib workspace
tests tools`; `benchmarks/` в список линтера не входит). Локально
`ruff check benchmarks` → **31 ошибка**. В `AGENTS.md` упомянут одной строкой
(«`benchmarks/` — подсистема бенчмарков»), в `README.md` и `docs/` — как dev-утилита;
конкретных процедур запуска нет. Единственный «процесс» — ручной запуск, который
ни разу не завершился успешно.

**(б) Упомянута ли в документации.** `AGENTS.md:1 строка`; `project.json:326-332`
(секция `benchmark.*` — единственный «официальный» контракт: имена таблиц);
`benchmarks/README.md` (45.9 KB — крупнее любого модуля подсистемы в 40 раз).
**README.md содержит инструкции по удалённому поведению**: `benchmarks/README.md:44-47`
описывает `set_on_sync_callback(wrapped)` и `await asyncio.wait_for(first_sync_event,
30s)` — механизмы, удалённые change `drop-local-cache-read-from-pg` (см.
`runner.py:610-616`, где тот же некролог задокументирован в коде).

**(в) Работает ли вообще сейчас.** Нет, и не по одной причине, а по трём
независимым, каждая из которых фатальна:
1. `db.py:52-57` — `ValueError` на import (нет `_initialize_settings`).
2. `hooks.py` — `AttributeError` на первом вызове `wants_streaming()`.
3. `hooks.py:47` — `TypeError` в `before_iteration` (не фатален, но шумит лог).
Плюс два функциональных: 56/57 заданий ждут несуществующих tool'ов (Ф8), `--db`
молча не пишет (Ф6). Доказательство «не запускалось»: 20 каталогов в
`benchmarks/results/runs/` созданы тестами (`suite=test-suite`), `summary.json`
отсутствует во всех.

**(г) Какие модули реально задействованы в цепочке.** Все 9 связаны статически
(`runner` → `db`/`evaluator`/`scorer`/`loader`/`reporter`/`hooks` → `models`), т.е.
цикл замкнут. Но по факту исполнения: `loader`, `hooks`, `evaluator`, `scorer`,
`reporter`, `models` — в рабочей цепочке; **`db`** — только за импортом
(`--db` не передаётся ни в одном из 20 прогонов, судя по `progress.log: DSN: False`),
**плюс 86 LOC в нём мертвы даже при `--db`**; **`__init__`** — служебный.

---

## Кросс-подсистемные находки

1. **`lib/session/pg_session_manager.py:112-114` × `benchmarks/runner.py:414,443,525` —
   связанная пара дефектов.** `PGSessionManager.invalidate` — no-op, а upstream
   `SessionManager.delete_session` (`nanobot/session/manager.py:1870-1876`) начинает
   с `self.invalidate(key)`. Последствие в бенчмарке: файл сессии удаляется, запись
   в `self._cache` остаётся, и `flush_all()` (`runner.py:703`) **воскрешает** JSONL.
   **Изоляция между заданиями при этом НЕ ломается** — `session_key` уникален на
   (`item.id`, `run_id`), поэтому коллизий нет. Правится **в двух местах**:
   в `benchmarks/` убрать `await` (3 строки) и в `lib/` восстановить
   `invalidate` (или не полагаться на `delete_session` для очистки). Фикс в
   `benchmarks/` — обязателен сам по себе: он убирает и `TypeError`, и потерю
   возвращаемого `bool`; фикс в `lib/` — независимая задача.
2. **`lib/core/agent_factory.py:208` × `benchmarks/hooks.py:34` — мёртвый
   observer-слой не является причиной невозможности считать токены.** Подтверждаю
   находку другого аудитора: `getattr(config, "build_provider_snapshot", None)` на
   объекте `Config` даёт `None` (в `lib/` этот символ больше не встречается), значит
   `llm_observer.py` + `llm_usage_store_factory.py` — мёртвый слой. **Однако**
   бенчмарку он не нужен: `RunResult.usage` (`nanobot/sdk/types.py`) уже заполняется
   `SDKCaptureHook.after_run` и доступен в `runner.py` как `result.usage`. Вывод:
   чинить observer-пайплайн для бенчмарка **не нужно** — достаточно прочитать
   `result.usage` в `_run_single`/`_run_multi_step` и добавить поле в `BenchResult`
   + колонку (либо переиспользовать `agent_gateway_logs`: `DatabaseLoggingHook`
   уже пишет `usage` per-turn по `session_key` вида `bench:single:*`).
3. **`workspace/utils/db.py` — канонический коннектор; третьего пула нет.**
   `benchmarks/db.py:33` импортирует `configure/execute/transaction/fetchval/fetch`
   из него же, как и `lib/services/db_logging_service.py`. Пересечение с
   `db_logging_service` — только на уровне коннектора; сам `db_logging_service`
   (fire-and-forget очередь, свой воркер, purge по retention) задачу бенчмарков
   не решает и дублирования не создаёт. Реальная проблема `db.py` — не дублирование
   пула, а (Ф6/Ф7) несовместимость с psycopg2 и рассогласование колонок.
4. **4 копии `sys.path`-хака** (`runner.py:60-68`, `db.py:31-46`, `hooks.py:12-20`,
   плюс `tools/legal_benchmark.py`) — скрытый контракт: подсистема не запускается
   из произвольного CWD. Правильное решение — один пакетный bootstrap, а не
   копипаст.
5. **`nanobot 0.3.5` vs заявленный `0.3.0`.** `AGENTS.md` и все docstring'и
   подсистемы говорят о 0.3.0; фактически установлена **0.3.5**
   (`pip show nanobot`; коммит `b7f73c9 feat(nanobot): upgrade runtime_patcher to
   nanobot-ai 0.3.5`). Именно этим апгрейдом, вероятно, внесены
   `wants_streaming()` в `CompositeHook` и `close_mcp → aclose` — то есть
   подсистема не пережила апгрейд, и это **прямое следствие отсутствия CI-проверки**
   (Ф14).

## Рекомендация по подсистеме

`benchmarks/` нельзя удалить (это единственный инструмент оценки качества агента,
и `AGENTS.md` его объявляет), но в текущем виде он **не приносит пользы**: он
декоративен, при этом создаёт видимость контроля качества. Порядок возврата в
рабочее состояние (по стоимости):

1. **Ф1** (3 строки) — `config._initialize_settings(profile=...)` в `main_async`
   до импорта `benchmarks.db`; снять `raise` на import-time в `db.py`.
2. **Ф2** (2 строки) — `BenchmarkHook(AgentHook)` + `super().__init__()`.
   После этого хотя бы одно задание отработает.
3. **Ф3/Ф5/Ф4** — убрать `dict(context.usage)`, `close_mcp()`, три `await`.
4. **Ф8** — переписать `items/*.yaml` под реальный tool-оверлей
   (`write_file`/`exec`/`read_file`/`grep`/`legal_summarizer_query`); до этого
   любой отчёт бессмысленен.
5. **Ф9** — свести `_aggregate_score` и `_weighted_score` к одной формуле.
6. Подключить `ruff check benchmarks` и минимальный smoke-тест
   (`--dry-run` + один item) в CI — иначе следующий апгрейд снова сломает молча.

Пункт 4 — самый дорогой по содержанию, но без него пункты 1-3 дают отчёт,
в котором 56 из 57 строк «ПРОВАЛЕН» по заведомо невыполнимым критериям.
