# Дублирующиеся тесты `tests/legal_summarizer/` — проверено мутационно, НЕ удалено

**Статус:** открытое решение закрыто. Мутационная проверка выполнена по всем
4 парам-кандидатам и файлу-кандидату. **Ни один кандидат не доказанно
избыточен — ничего не удалено.** Каталог: 768 тестов (до и после).

**Дата:** 2026-10-02
**Каталог:** `mcp-platform/tests/legal_summarizer/` — 118 файлов, 768 тестов.

---

## 1. Итоговая таблица: кто ловит регрессию

Колонка «падает» — фактический результат прогона pytest с внедрённой
мутацией (см. §4 протокол).

| # | Пара-кандидат | Инвариант | Мутация | Кто упал | Вердикт |
|---|---|---|---|---|---|
| 1 | `test_execution_context_snapshot.py::…_for_map_run`<br>`test_plan_build_count.py::test_plan_built_for_map_run` | map-run: `build_execution_context` == 1 **и** `build_execution_plan` == 1 | `P1.ctx_twice` — `run()` строит контекст дважды | **оба** | оба нужны |
| 1 | та же пара | тот же | `P1.plan_twice` — план строится дважды внутри одного контекста | **только plan-count** | plan-count ловит регресс, которого не ловит ctx-count |
| 1 | та же пара | тот же | `P1.ctx_twice_plan_once` — контекст пересоздаётся дважды, план мемоизирован | **только ctx-count** | ctx-count ловит регресс, которого не ловит plan-count |
| 2 | `test_canonical_retrieval.py::test_answer_followup_with_no_match_triggers_fallback`<br>`test_structure_followup.py::test_followup_question_no_hits_uses_fallback` | запрос без match → `confidence == "very_low"` + `used_full_doc_fallback is True` | `P2.fallback_confidence` — fallback сообщает `confidence="low"` | **оба** | оба нужны |
| 2 | та же пара | тот же | `P2.fallback_flag` — fallback перестаёт сообщать флаг | **оба** | оба нужны |
| 2 | та же пара | тот же | `P2.wrapper_drops_query` — `answer_followup` не передаёт query | **никто** | мутация неразличима на этой фикстуре (см. §5) |
| 3 | `test_single_flight.py::test_concurrent_llm_calls_counter_at_most_one`<br>`test_final_integration_suite.py::test_scenario_single_flight` | пик одновременных `llm_batch` == 1 за `run()` | `P3.parallel` — `Semaphore(1)→(8)` **и** `LLM_FLIGHT_LOCK` отключён | **оба** (`expected peak==1, got peak=3`) | оба нужны |
| 4 | `test_structure_document_analysis.py::test_retrieve_uses_index`<br>`test_structure_retrieval_index.py::test_retrieve_uses_inverted_index` | `retrieve("оплата")` возвращает chunk `001` | `P4.index_empty` — `RetrievalIndex.retrieve` возвращает `[]` | **оба** | оба нужны |
| 4 | та же пара | тот же | `P4.analysis_empty` — `DocumentAnalysis.retrieve` возвращает `[]` | **только analysis** | analysis-тест ловит регресс обёртки, index-тест — нет |
| 4 | та же пара | тот же | `P4.build_no_index` — `DocumentAnalysis.build` не строит индекс | **никто** | проверка индекса мертва (см. §5) |

**Файл-кандидат `test_single_flight.py` (1 тест, 71 строка) — НЕ удалён.**
Его единственный тест падает на `P3.parallel` наравне с
`test_scenario_single_flight`, т. е. регрессию он ловит. По заданному
критерию («удалять только доказанно не ловящие регрессию») он не является
кандидатом на удаление.

---

## 2. Главный вывод по паре 1: исходное предположение опровергнуто

Прежняя редакция этого файла называла `test_plan_built_for_map_run`
«единственным подтверждённым кандидатом» на удаление, а пару
`..._for_direct_run` / `..._for_map_run` — эшелонированием по стратегиям.

Мутация показала обратное. Эти два теста **считают разные единицы**:

* `test_plan_build_count.py` шпионит за `planning.strategy.build_execution_plan`
  (`context_builder.py:64`);
* `test_execution_context_snapshot.py` шпионит за
  `application.context_builder.build_execution_context` (`service.py:394`).

Счётчики расходятся. Регресс «план строится дважды, контекст один» ловит
**только** первый; регресс «контекст пересоздаётся, план переиспользуется»
ловит **только** второй. Ни один не покрывает другой — **оба нужны**.

---

## 3. Что НЕ является дублированием (сохранено из прежней редакции)

### 3.1. Unit-уровень трекера против acceptance-уровня конвейера
`test_structure_single_flight.py` (трекер изолированно),
`test_single_flight_concurrent_safety.py` (реальные `threading.Thread` +
`threading.Barrier`) и acceptance-сценарий `test_scenario_single_flight` —
разные уровни. Не удалять.

### 3.2. direct vs map внутри `test_execution_context_snapshot.py`
Оба теста ловят свою мутацию (см. §2). Не удалять.

### 3.3. Чистый `inspect()` → `map_plan_to_chunk_batches()` против реального `run()`
Инвариант формы проверяется изолированно и дополнительно сквозным прогоном.
Эшелонирование, не копия. Не удалять.

### 3.4. Публичный `retrieve()` против реализации индекса
`P4.analysis_empty` ловится только тестом `DocumentAnalysis.retrieve`.
Уровни разные, оба оставлены.

### 3.5. Canonical retrieval против turn-level followup
`answer_followup` — тонкая обёртка над `build_followup_response`
(`retrieval/canonical.py:36`), но fixture'ы разные: реальный документ через
`build_pipeline_result` против собранных вручную чанков. Оба оставлены.

---

## 4. Протокол мутационной проверки

Мутации вносятся **без правки production-кода**: временный pytest-плагин
вне репозитория подменяет атрибут модуля через авто-фикстуру. Файлы:

```
%TEMP%\nanobot_mutprobe\mutprobe.py    # мутации (10 шт.)
%TEMP%\nanobot_mutprobe\run_probe.py    # драйвер: печать per-test PASSED/FAILED
```

Запуск:

```
$env:MUTPROBE = "<id мутации>"
$env:PYTHONPATH = "$env:TEMP\nanobot_mutprobe"
python -m pytest <node-ids> -v --tb=no -p mutprobe -p no:cacheprovider
```

**Важная методологическая оговорка.** Первая версия плагина подменяла
`build_execution_context` и `build_execution_plan` напрямую. Такой прогон
**недействителен**: оба кандидата сами делают `monkeypatch.setattr` на те же
атрибуты и затирают мутацию (тест на «счётчик вызовов» — это spy на тот же
самый атрибут). Наивный прогон дал бы ложный вердикт. Рабочая схема —
подменить
**ссылку вызывающего модуля на модуль-поставщик** (`service._ctx_builder_mod`,
`context_builder._planning_strategy_mod`) прокси-объектом: мутация применяется
ниже spy'а теста и потому не затирается. Первые два прогона пары 1 были
исправлены именно после этого.

---

## 5. Побочные наблюдения (не исправлены, вне области задачи)

1. **`test_retrieve_uses_index` не проверяет использование индекса.**
   Мутация `P4.build_no_index` (дефолт `include_retrieval_index=False`)
   оставила тест зелёным: `DocumentAnalysis.retrieve` при `index is None`
   уходит в линейный `retrieve_chunks` и находит `001` так же успешно
   (`document/analysis.py:163-171`). Имя теста и docstring вводят в
   заблуждение.
2. **Оба теста пары 4 вырождены по составу фикстуры.** В обеих фикстурах
   ровно один чанк содержит «оплата», поэтому `assert hits[0].chunk_id == "001"`
   выполнен и для функции, возвращающей единственный произвольный hit.
   `assert len(hits) >= 1` при этом ничего не добавляет.
3. **`P2.wrapper_drops_query` неразличим на этой фикстуре.** Обе фикстуры
   построены как «запрос без совпадений», поэтому потеря query в обёртке
   `answer_followup` не меняет результат. Проверить обёртку можно только
   тестом с **совпадающим** запросом (например, на
   `test_answer_followup_returns_followup_result`).
4. **`test_plan_build_count.py:74`** подменяет `summarizer.build_execution_plan`
   (импорт в `service.py:74`), который в `run()` **не вызывается** — вызов
   идёт из `context_builder` через `_planning_strategy_mod`. Подмена
   безвредна, но создаёт иллюзию второго пути вызова.
5. **Мёртвый `assert`** в `test_canonical_retrieval.py::test_answer_followup_returns_followup_result`:
   `assert len(result.target_chunks) >= 0` истинно всегда.

---

## 6. Что уже сделано ранее по этому вопросу

Два теста с пустым телом `pass` удалены без мутационной проверки — удаление
не удаляло покрытие:

* `test_final_invariants.py::test_invariant_h_two_concurrent_max_one_llm`
  → покрыт `test_single_flight_concurrent_safety.py::test_concurrent_runs_peak_is_one`.
* `test_final_invariants.py::test_invariant_l_no_legacy_files`
  → покрыт `test_legacy_audit.py::test_forbidden_files_do_not_exist`
  и `::test_forbidden_runtime_dirs_do_not_exist`.

Итого по каталогу: 770 → 768 тестов.

---

## 7. Остаток неатрибутированным (сохранено без изменений)

Из 26 тестов, оценённых проверяющим независимо, строгий пересчёт по полным
наборам ассертов подтвердил **4 пары** и **1 файл-кандидат**; остальные ~19
тестов и 2 файла дублированием не являются. Расхождение с оценкой «26» не
разрешено: критерий матчинга проверяющего неизвестен. Мутационная проверка
это расхождение не разрешает и не усугубляет — она отвечает на другой вопрос
(ловит ли тест регрессию), и по всем пяти кандидатам ответ — «да».
Число 26 остаётся **неподтверждённым и не опровергнутым**.
