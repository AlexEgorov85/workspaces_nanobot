# Аудит: `10-skill-legal-core` — ядро скилла `legal_summarizer` (application / cache / chunking / execution)

## Сводка группы

Файлов: **31** · LOC: **7172** · классов: **20** · методов: **24** · функций: **119**

Подсистема: `workspace/skills/legal_summarizer/scripts/{application,cache,chunking,execution}/`.
Точка входа продукта — `scripts/cli.py`; все внутренние импорты идут от корня `scripts/`,
поэтому «0 static importers» в брифе — не признак мёртвости. Все выводы ниже проверены
grep'ом по всему репозиторию (включая `tests/`, `docs/`, `.github/`).

### Ключевые находки

1. **`application/canonical.py` (244 LOC) — мёртвый модуль целиком, и он называется «production-flow».**
   Docstring `canonical.py:3` гласит «Этот модуль — **production-flow**», но
   `grep "from application.canonical"` по всему репо даёт **0 production-импортёров** —
   25 совпадений, все в `tests/`. Точка входа агента — `application/service.py:run`
   (единственный production-импортёр — `cli.py`), а он идёт
   `service.run → inspection.inspect → pipeline_structure.run_canonical_pipeline`.
   `canonical.py` — **второе «каноническое» имя** для пути, который уже канонический.
   Вердикт: **Удалить** файл (244 LOC, 8 символов, 1 класс). Что сломается: 6 тест-файлов
   (`test_canonical_cache_path.py`, `test_canonical_execution_path.py`,
   `test_canonical_retrieval.py`, `test_canonical_production_path.py`,
   `test_summarizer_canonical.py`, `test_legal_summarizer_no_legacy.py`) — удалить
   вместе с ними. Предварительная работа: переписать 4 из них на
   `service.run`/`pipeline_structure.run_canonical_pipeline`/`context_builder`,
   один (`test_legal_summarizer_no_legacy.py`) — переименовать, он проверяет отсутствие
   legacy, а не canonical.py.

2. **НЕканонической копии reduce-функций нет — гипотеза брифа опровергнута.**
   `execution/hierarchical.py:1` заявляет «All reduce functions (**canonical copy** in execution layer)».
   Фактически: `grep "def .*(reduce|merge|combine|consolidat|summariz)"` по `scripts/` даёт
   `execution/map_reduce._reduce_phase`, `execution/hierarchical.reduce_sections_to_document`,
   `execution/hierarchical.reduce_chunks_hierarchical`, `application/estimation._hierarchical_reduce_calls`
   и `llm/calls.llm_section_reduce`/`llm_document_reduce`. `reduce_sections_to_document`
   имеет **ровно одного** вызывающего — `reduce_chunks_hierarchical:262` (внутренний).
   Ни в `application/`, ни в `chunking/`, ни в `llm/` дубля нет. Единственная настоящая
   проблема — **сама формулировка docstring**: «canonical copy» подразумевает
   несуществующего конкурента. Вердикт: **Оставить** код, **Упростить** документирование;
   `reduce_sections_to_document` переименовать в приватный `_reduce_sections_to_document`
   (вне модуля у него нет ни одного consumer'а) либо оставить публичным, но убрать
   «canonical copy» из docstring и добавить отсутствующий `__all__`.

3. **Реальное дублирование: `deterministic_truncate` / `_fit_input` / `fit_input` — 3 имени, 1 алгоритм.**
   `execution/hierarchical.py:11-30` (публичное) и `chunking/_text_helpers.py:54-69`
   (`fit_input`) — **байт-идентичны** (совпадает `duplicates.md` → `0342da2b898f07ed`).
   Плюс `execution/hierarchical.py:33-35` `_fit_input` — чистая pass-through обёртка,
   вызывается 4 раза (110, 139, 230, 243). Канон — `_text_helpers.fit_input` (её импортируют
   `map_reduce.py:27` и `execution_orchestration.py:37`); `hierarchical` нарушает границу
   слоёв, дублируя leaf-утилиту. Вердикт: **Слить с `chunking/_text_helpers.py`** —
   удалить `deterministic_truncate` и `_fit_input`, импортировать `fit_input`.
   Выигрыш: −25 LOC, один алгоритм усечения вместо трёх имён.

4. **`chunking/packing.py` НЕ вытеснен `structural_packing.py` — гипотеза брифа опровергнута.**
   Это **разные уровни**: `structural_packing` = blocks→units→`Chunk[]` (chunk-level),
   `packing.py` = `Chunk[]`→batches (batch-level, импортируется `planning/strategy.py:130`
   и `application/canonical.py:30`). Оба живые и оба нужны. Мёртв только **docstring**
   `packing.py:3`, который описывает несуществующий модуль `packing_impl.pack_chunks`
   и подаёт себя как «целевая политика», хотя модуль и есть реализация.
   Вердикт: **Оставить** код, **Упростить** — переписать docstring на фактический контракт.
   Побочно: `packing.py:154` `current_is_table = is_table` выглядит как баг (`=` вместо `|=`),
   но при текущей последовательности flush'ов (129-137) эквивалентно `|=`; хрупко, не баг.

5. **Три слоя исполнения сводятся к бинарному развилке; `map_flat` и `map_hierarchical`
   неразличимы на исполнении.** `planning/strategy.select_strategy` возвращает все три
   (`strategy.py:104,108,110`), но `service.py` ветвится только по `ctx.strategy == "direct"`,
   а `map_flat` и `map_hierarchical` оба уходят в `run_map_reduce` с **одним и тем же**
   `_reduce_phase(strategy=...)`. Хуже: `context_builder.build_execution_context:62` дублирует
   то же правило (`len(chunks) <= 1 → "direct"`) и **перекрывает** `select_strategy` —
   для документа с 1 chunk'ом, чьи токены превышают `direct_threshold_tokens`,
   `select_strategy` скажет `map_flat`, а `build_execution_context` тихо вернёт `direct`.
   Два конкурирующих места принятия решения, второе молча выигрывает. Вердикт:
   **Упростить** — оставить одно место выбора; 3-значная стратегия избыточна.

6. **`application/service.py` НЕ тонкий facade — 210 из 514 LOC это параллельный pipeline.**
   `_try_question_via_document_cache` (78-291) самостоятельно делает: restore snapshot →
   `DocumentAnalysis.build` → `select_chunks_for_mode` → `build_question_context` →
   `llm_document_reduce` → инлайн-сборка 30-полевого `NormalizedManifest` → `write_result`.
   Это дублирует и `pipeline_structure._try_load_cached_pipeline_result` (108-187,
   тот же restore-снимок), и manifest/result-контракт из `manifest_builder.build_manifest`
   и `_persist_final_manifest`. Причём копия restore-логики **не самовосстанавливается**:
   на битом snapshot `_try_load_cached_pipeline_result:171` вызывает `cache.invalidate()`,
   а `service.py:133-135` просто `return None` — битый snapshot навсегда блокирует
   question-путь без самоисцеления. Вердикт: **Упростить** (вынести restore в
   `pipeline_structure` и переиспользовать; `manifest_builder.build_manifest` — единственный
   конструктор manifest'а).

7. **Функциональный баг (потеря данных при resume): `map_reduce.py:219-228`.**
   `chunk_states[...]["status"] = "completed"` присваивается **вне** `if c.chunk_id in chunk_results:`
   (194-218). Если LLM вернул не summary для всех chunks батча, такой chunk всё равно
   помечается `completed` и получает `result_path`, указывающий на **несуществующий** файл.
   При следующем resume `_queued_batches:101-105` отбрасывает chunks со `status == "completed"`,
   `load_cached_partials` файла не находит → chunk **навсегда выпадает** из reduce-входа,
   молча, без ошибки. Вердикт: **Упростить** (перенести присваивание внутрь `if`).

8. **Функциональный баг (ложная метрика): `execution_orchestration.py:118-126`.**
   `except Exception: retries += 1; reduce_calls = 0` — ретрая **не происходит** (комментарий
   «REDUCE_INPUT_EMPTY on non-retryable input error»), но `stats.retries` увеличивается
   на первой и единственной неудаче. Аналогично `map_reduce.stats.retries` не считает
   внутренние parse-retry из `execution/pipeline.MAX_BATCH_PARSE_RETRIES`.
   Вердикт: **Упростить** — `retries` должен быть 0, либо ретрай реализовать.

9. **Функциональный баг (потеря page provenance): `question_context.py:117-131`.**
   Полный путь строит head как `[CHUNK … | Section: …] + pages_str`, а путь budget-усечения
   (120-131) пересобирает head **без** `pages_str` — при срабатывании лимита бюджета
   page-диапазон chunk'а теряется из LLM-контекста. Тот же head форматируется в трёх
   местах с разным результатом. Вердикт: **Упростить** — один форматтер.

10. **Функциональный баг (manifest «залипает» в `running`): `execution_orchestration.py:191` и `:290`.**
    `save_manifest(initial_manifest)` пишет `status="running"`; `_persist_final_manifest` (и
    `run_direct`-ветка) вызываются **только** для `completed`/`partial`. На `status == "failed"`
    (`run_direct:150-158`, `run_map_reduce`) manifest навсегда остаётся `running` — resume
    и диагностика (`cli_query.py`) показывают неверное состояние. Вердикт: **Упростить** —
    писать terminal-статус во всех ветках.

11. **Пять (!) копий title-резолвинга.** Один и тот же 3-строчный приоритет
    `analysis.structure.title → structure.title.value → "Документ"`:
    `manifest_builder.py:34-38`, `execution_orchestration.py:140-144`,
    `execution_orchestration.py:422-426`, `execution/map_reduce.py:680-684`
    (+ вариант без `analysis` в `service.py:195-198` и 4-строчный nested-ternary
    дважды в `service.py:411-419` / `441-449` — 6 вариантов). Вердикт: **Слить** в одну
    функцию в `document/title.py`.

12. **Мёртвый модуль целиком: `chunking/importance_score.py` (108 LOC).**
    `compute_importance`, `select_top_chunks_by_importance`, `ImportanceScore.total` —
    **0 ссылок** в production и **0 в тестах**. Docstring:4 утверждает «Используется в brief,
    retrieval tie-break, packing, fallback selection» — ни одно из четырёх неправда.
    Вердикт: **Удалить** файл. Что сломается: ничего (0 ссылок).

13. **Мёртвый модуль целиком: `chunking/order.py` (44 LOC).**
    `restore_document_order` / `ensure_order_preserved` — 0 ссылок где-либо.
    Docstring:4 «После ranking `restore_document_order`» — ranking его не вызывает.
    Вердикт: **Удалить** файл.

14. **Дубль `_collect_owner_section_ids` / `_collect_section_ids_for_range` разрешён.**
    `chunker.py:483-496` и `structural_packing.py:141-154` — **байт-идентичны**.
    Оба живые: `structural_packing`-версия вызывается 5 раз (195, 215, 243, 262, 285),
    `chunker`-версия — 2 раза (446 в oversized-цикле, и 413 в **мёртвой** `_unit_for_block`).
    Канон — `structural_packing._collect_section_ids_for_range`; `chunker`-копию удалить
    вместе с `_unit_for_block`. Вердикт: **Слить**.

15. **Кросс-подсистемный дубль (вне моего скоупа, но виден отсюда):**
    `document/structure.py:428-…` и `document/block_ownership.py:41-…` определяют
    **байт-идентичные** `build_block_ownership`, `owner_for_block`, `_depth_of`.
    `chunker.py:33-37` импортирует и **ре-экспортирует** их в `__all__` (604-605) — пассивно,
    никто не импортирует оттуда. Передаю аудитору слоя `document/`.

16. **Второй кросс-подсистемный дубль:** `contextWindowTokens` читается двумя
    копиями одной функции — `pipeline_structure._read_context_window_tokens:69-96` и
    `brief_context._resolve_context_window_tokens:113-138` (`duplicates.md` → `c280600df15ce5dc`).
    Канон — `pipeline_structure`; `brief_context` должна импортировать. Плюс
    `brief_context.py:29` ссылается на `ChunkPlanner.build_chunk_config_from_runtime`,
    а такого метода у `ChunkPlanner` **нет** (это module-level функция `chunker.py:57`).

**Вердикты по функциям и методам (145 строк таблиц: 119 функций + 24 метода + 2 служебные строки):**
Оставить **94** · Упростить **28** · Удалить **20** · Слить с **3** · НЕ РАЗОБРАНО **0**

**Вердикты по классам (20):** Оставить **13** · Упростить **3** · Удалить **4**

**Итого по символам:** Оставить **107** · Упростить **31** · Удалить **24** · Слить с **3** · НЕ РАЗОБРАНО **0**
Из `Удалить` на уровне файла — **3** (`application/canonical.py`, `chunking/importance_score.py`,
`chunking/order.py`; суммарно 396 LOC, 0 production-ссылок, 6 тест-файлов на `canonical.py`).

**Вердикты по файлам (31):** Оставить **20** · Упростить **7** · Удалить **3** · Слить с **1**

**Функциональных багов найдено: 7** (см. § «Функциональные баги»).

---

## `application/service.py` — 514 LOC

**Назначение.** Единственная production-точка входа для агента: idempotency → inspect → build execution context → confirmation → execute; плюс собственный shortcut для `--question` через document-level cache.
**Что делает.** `run()` (294-491) вычисляет детерминированный `operation_id`, проверяет manifest на idempotency, вызывает `inspection.inspect` (пишет document snapshot на cache miss), `context_builder.build_execution_context`, при `requires_confirmation` возвращает ранний результат, иначе ветвится на `run_direct` / `run_map_reduce`. Пишет `NormalizedManifest` и `result.json` через `cache.manifest`.
**Зачем нужен.** Без него `cli.py` не имеет входа; здесь же держится контракт формы `result`-dict для CLI и IPC (`cli_query.py`).
**Вердикт.** **Упростить** — facade не тонкий: 41% LOC это второй pipeline (см. находку 6), плюс мёртвый `structure`-параметр в цепочке вызовов и 6 копий title-резолвинга.
**Обоснование.** `run()` делегирует `canonical.py`-уровень задачи инспекции/оценки, но при этом дублирует restore-снимка и manifest-сборку вместо переиспользования существующих `pipeline_structure` / `manifest_builder`.
**Доказательства.** Импортёры: `cli.py` (1, production). `build_execution_plan` в `__all__:513` — мёртвый re-export (никто не импортирует из `service`). Тесты: `tests/test_idempotency_no_reexecution.py`, `test_question_via_document_cache.py`, `test_question_integration.py` (через `application/__init__`-паттерн).

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_try_question_via_document_cache` | 78-291 | Shortcut `--question`: собрать контекст из document cache и сделать один `llm_document_reduce` | Экономит полный map-прогон при повторном вопросе по тому же документу | `service.py:367` | Упростить — 210 LOC параллельного pipeline; дублирует `pipeline_structure._try_load_cached_pipeline_result`; на битом snapshot не вызывает `cache.invalidate()` в отличие от канона; manifest собирается инлайн вместо `manifest_builder.build_manifest` |
| `run` | 294-491 | Canonical execution path | Единственная production-точка входа | `cli.py` | Упростить — `structure`-параметр мёртв по всей цепочке (`canonical`/`run_direct`/`run_map_reduce` используют его только как fallback title); `load_manifest` вызывается дважды (331, 401); два почти идентичных early-return блока для confirmation |
| `run.inspect_legacy_hook` (вложенный re-export блок) | 62-76 | Re-export `DocumentCache`, `load_manifest`, `read_result`, `build_execution_plan`, `map_plan_to_chunk_batches`, `run_direct`, `run_map_reduce` | Совместимость импортов | — | Упростить — `build_execution_plan` не импортируется из `service` никем; остальные используются `cli.py` |

---

## `application/canonical.py` — 244 LOC

**Назначение.** Заявлен как «production-flow» на каноническом пайплайне.
**Что делает.** Тонкие обёртки над `run_canonical_pipeline` (`build_pipeline_result`), `select_strategy` (`strategy_from_pipeline`), `build_execution_plan` (`build_plan_from_pipeline`), плюс `CanonicalInspection` и три评估-хелпера.
**Зачем нужен.** Не нужен: полностью вытеснен `service.run` + `context_builder` + `estimation`.
**Вердикт.** **Удалить** файл целиком (см. находку 1).
**Обоснование.** 0 production-импортёров; при этом внутри — мёртвый код: `total_tokens` (164-166) вычисляется и не используется, `_duration` (71) вычисляется и отбрасывается, `estimate_canonical` возвращает `chars_in`, **всегда равный 0** (193-196 передаёт `text=""`), `chars_per_token=3.5` захардкожен в двух местах вместо `llm.config.get_chunking_config()`.
**Доказательства.** `grep "application.canonical"` → 25 совпадений, все в 6 тест-файлах, production — 0.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `build_pipeline_result` | 52-72 | Обёртка над `run_canonical_pipeline` | Скрывает `apply_repair=True, include_retrieval_index=True` | `inspect_canonical:154`, тесты | Удалить |
| `strategy_from_pipeline` | 75-89 | Стратегия по `PipelineResult` | Тонкий делегат | `inspect_canonical:159`, тесты | Удалить |
| `build_plan_from_pipeline` | 92-104 | План по `PipelineResult` | Тонкий делегат | `inspect_canonical:171`, тесты | Удалить |
| `inspect_canonical` | 128-184 | Канонический осмотр | Параллель `inspection.inspect` | `estimate_canonical:193`, тесты | Удалить — `total_tokens` (164-166) мёртв; `chars_per_token=3.5` продублирован |
| `estimate_canonical` | 187-202 | Оценка без LLM | Публичный хелпер | тесты | Удалить — возвращает `chars_in == 0` всегда (баг) |
| `estimate_chunks_canonical` | 205-214 | Сумма токенов по chunks | Оценка | тесты | Удалить — `chars_per_token=3.5` захардкожен, расходится с `llm.config` |
| `pack_batches_canonical` | 217-232 | Batch packing | Тонкий делегат | тесты | Удалить — дефолты `max_sections_per_batch=2, per_batch_token_budget=6000` дублируют `planning.strategy.ExecutionPolicy` |

#### class `CanonicalInspection` (108-125, 0 методов)
DTO: `chars_in`, `chunks`, `structure`, `strategy`, `estimated_llm_calls`, `pipeline_result`.
**Удалить** — дубль `application.inspection.Inspection` (20-31) с добавленными полями; потребителей в production нет.

---

## `application/execution_orchestration.py` — 489 LOC

**Назначение.** Координатор `run_direct` / `run_map_reduce`: кэш-персистенс manifest'а и `result.json`, инъекция cache-callback'ов в `execution`-слой.
**Что делает.** `run_direct` склеивает все chunks в один вход и делает один `llm_document_reduce`. `run_map_reduce` готовит initial manifest, инжектит `write_chunk_result` / `load_cached_partials` / `write_document_chunk_summary` в `execution.map_reduce`, затем пишет финальный manifest + section summaries в `DocumentCache`.
**Зачем нужен.** Единственное место, где `execution` получает доступ к `cache` без нарушения layer-guard'а (`tests/architecture/test_layer_boundaries.py:86-88` запрещает `execution → cache`). **Это легитимный DI, а не theatre** — оставить.
**Вердикт.** **Оставить** с точечными упрощениями (баги 8, 10, копия title-резолва, function-level импорты).
**Обоснование.** Граница слоёв выдержана корректно; проблемы — в корректности состояний и дублировании, а не в архитектуре.
**Доказательства.** Импортёр: `service.py` (1). Функциональные импорты `from cache.manifest import …` внутри тел функций (292-296, 406-410) дублируют модульный импорт (30-34) — стиль, не нарушение слоя.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `map_plan_to_chunk_batches` | 55-83 | `ExecutionPlan` → `list[list[Chunk]]` | Единственный маппинг plan→chunks | `run_map_reduce:281` | Оставить |
| `run_direct` | 86-222 | Single-call исполнение | Быстрый путь для 1-батчевых документов | `service.py:453` | Упростить — `retries` инкрементируется без ретрая (118-126); `session_key` принимается и не используется (**dead-cache-пробел**, см. баг 7 в списке кросс-системных); формат входа `f"[Chunk {id}]\n{text}"` (121) обходит `format_chunk_block` и теряет section-метки, которые flat-reduce добавляет; на `failed` manifest остаётся `running` |
| `run_map_reduce` | 225-380 | Координатор map-reduce | Основной исполняющий путь | `service.py:466` | Упростить — `batches_done` (369) включает **и failed** батчи (`range(len(ctx_batches))`), противоречит `batches_failed`; на `failed` manifest остаётся `running` (290) |
| `_persist_final_manifest` | 383-482 | Финальный manifest + section summaries | Resume-контракт + document cache | `run_direct:201`, `run_map_reduce:377` | Упростить — 100 LOC; копия title-резолва (422-426); собирает `NormalizedManifest` инлайн вместо `manifest_builder.build_manifest` |

---

## `application/pipeline_structure.py` — 352 LOC

**Назначение.** Точка сборки канонического пайплайна: loader → identity → structure → repair → validate → `ChunkPlanner` → `DocumentAnalysis`, плюс document-level snapshot cache.
**Что делает.** `run_canonical_pipeline` (238-349) сначала пытается cache hit (`_try_load_cached_pipeline_result`), при miss строит всё с нуля и пишет snapshot. `DocumentStructure` конструируется здесь и **только** здесь.
**Зачем нужен.** Единственный источник `DocumentStructure` для chunking, retrieval и execution — **инвариант подтверждён** (см. ответ на вопрос 7).
**Вердикт.** **Оставить** с точечной упросткой.
**Обоснование.** Границы ответственности выдержаны: `ChunkPlanner.plan(physical, struct)` (322) и `DocumentAnalysis.build(structure=struct)` (324) оба получают структуру отсюда; ни один downstream не строит её сам. Единственная асимметрия — копия restore-логики в `service.py` (находка 6), и `_write_document_snapshot_after_pipeline` принимает неиспользуемый `path`.
**Доказательства.** Импортёры: `application/canonical.py` (мёртвый), `application/inspection.py`, `application/service.py`. Тестов прямых нет; покрыт транзитивно.

#### class `PipelineResult` (100-105, 0 методов)
DTO: `analysis`, `validation`, `chunks`. Нужен как возвращаемое значение канонического пайплайна. **Оставить.**

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_read_context_window_tokens` | 69-96 | Читает `config.json::agents.defaults.contextWindowTokens` | Привязывает размер chunk'а к реальному окну модели | `run_canonical_pipeline:315` | Оставить — **канон**; дублируется в `brief_context.py:113-138` (Слить) |
| `_try_load_cached_pipeline_result` | 108-187 | Restore `PipelineResult` из snapshot | Избегает повторного парсинга PDF/DOCX | `run_canonical_pipeline:269` | Оставить — самовосстанавливается через `cache.invalidate()` (171); **это образец** для `service._try_question_via_document_cache`, который не восстанавливается |
| `_write_document_snapshot_after_pipeline` | 190-235 | Атомарная запись snapshot | Заполняет document cache | `run_canonical_pipeline:333` | Упростить — `path` (199) принимается и **не используется**; module-level `from chunking.chunker import ChunkPlanner` (36-38) перекрыт локальным импортом (310-314) и **мёртв** |
| `run_canonical_pipeline` | 238-349 | Канонический пайплайн | Единственная точка сборки структуры | `inspection.py:56` | Оставить |

---

## `application/inspection.py` — 73 LOC

**Назначение.** Document-level снимок: `structure` + `analysis` + `chunks`.
**Что делает.** Тонкая обёртка над `run_canonical_pipeline`; при пустом `text` возвращает пустой `Inspection` **без ошибки**.
**Зачем нужен.** Отделяет document-level анализ от run-level решения (strategy/plan) — граница выдержана и осмысленна.
**Вердикт.** **Оставить** с оговоркой.
**Обоснование.** Дизайн чистый, но `text=""` + валидный `path` даёт «успешный» пустой результат → `service.run` сообщит «no chunks» вместо диагностики; `chars_in` меряет переданный вызывающим текст, а не распарсенный документ.
**Доказательства.** Импортёры: `application/context_builder.py`, `application/service.py`.

#### class `Inspection` (20-31, 0 методов)
Immutable DTO: `chars_in`, `chunks`, `structure`, `analysis`. Канонический контракт для `context_builder` и `service`. **Оставить.**

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `inspect` | 34-70 | Document-level snapshot | Единая точка входа в канонический пайплайн | `context_builder.py:56`, `service.py:378` | Оставить — но `text` используется только для title-fallback (62), а `if not text` (48) молча возвращает пустой результат |

---

## `application/context_builder.py` — 76 LOC

**Назначение.** Run-level снимок: выбранные chunks + стратегия + план.
**Что делает.** Вызывает `select_chunks_for_mode`, затем `select_strategy` и `build_execution_plan`.
**Зачем нужен.** Разделяет document-level и run-level решения.
**Вердикт.** **Упростить** — дублирует и правило выбора стратегии, и передачу `chunks` несогласованно.
**Обоснование.** `len(chunks) <= 1 → "direct"` (62) — второе место принятия решения, перекрывающее `planning.strategy.select_strategy` без записи об этом (находка 5). Кроме того `chunks` передаётся в `select_strategy` как `list`, а в `build_execution_plan` как `tuple` (59, 64) — мелочь, но признак копипасты.
**Доказательства.** Импортёры: `service.py`, `cli.py`; 22 ссылки в брифе (в основном тесты).

#### class `ExecutionContext` (15-25, 0 методов)
DTO: `chunks`, `strategy`, `plan`, `length`, `question`. Нужен как контракт `estimation.estimate_for_run` и `service.run`. **Оставить.**

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `build_execution_context` | 28-73 | Собрать run-контекст | Единая точка выбора chunks+strategy+plan | `service.py:390`, `cli.py`, тесты | Упростить — перекрывает `select_strategy`; `document_id=insp.analysis.identity.document_id if insp.analysis else ""` (58) использует truthiness dataclass'а вместо `is not None` |

---

## `application/chunk_selection.py` — 122 LOC

**Назначение.** Политика выбора chunks под режим (brief / detailed / question).
**Что делает.** Для `question` — retrieval → relaxed lexical → bounded top-of-document; для `brief` — ровно один структурный chunk; иначе все chunks.
**Зачем нужен.** Единая точка выбора; `service._try_question_via_document_cache` переиспользует её же.
**Вердикт.** **Оставить** с мелкими правками.
**Обоснование.** Логика трёхуровневого деградирования корректна. Мелочи: `_resolve_max_chunks()` (62) вызывается всегда, но используется только в `question`-ветке; `if chunk is None: raise RuntimeError` (107-110) недостижим — `build_brief_chunk` типизирован `-> Chunk` и не возвращает `None`.
**Доказательства.** Импортёры: `application/context_builder.py`, `application/service.py`.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_resolve_max_chunks` | 22-29 | `max_chunks_per_question` из конфига | Ограничить retrieval-выборку | `select_chunks_for_mode:62` | Оставить |
| `relaxed_lexical_fallback` | 32-52 | Поиск по 4-символьным префиксам | Деградация, когда retrieval пуст | `select_chunks_for_mode:79` | Оставить |
| `select_chunks_for_mode` | 55-116 | Выбор chunks под режим | Политика слоя application | `context_builder.py:48`, `service.py:158` | Упростить — мёртвая ветка `chunk is None` (107); `progress("question: retrieval пустой → …")` (76) выводится и когда retrieval непуст, но chunk_id'ы не сопоставились (72) |

---

## `application/brief_context.py` — 509 LOC

**Назначение.** BRIEF CONTRACT: один документ → ровно один структурный `Chunk`.
**Что делает.** Выбирает верхний содержательный уровень, рендерит outline + полный текст секций в preamble, сжимает через `brief_compression`, обрезает до `max_chars` и возвращает один `Chunk`.
**Зачем нужен.** Brief — не выборка canonical chunks, а компактное представление всего документа; иначе LLM не видит документ целиком.
**Вердикт.** **Оставить**; контракт **подтверждён**.
**Обоснование.** `build_brief_chunk` возвращает `Chunk` (не список) и вызывается из `select_chunks_for_mode:106`, который возвращает `[chunk]`; `build_execution_context:62` видит `len == 1` → стратегия принудительно `direct`. Контракт «один документ → один chunk» **выполняется**.
**Доказательства.** Импортёр: `application/chunk_selection.py` (1). Бриф говорит «2 ref files» — второй, вероятно, тест.

#### class `BriefContextConfig` (90-110, 0 методов)
Поля: `max_chars_fallback`, `input_ratio`, `chars_per_token`, `structure_max_chars`. Нужна как конфиг-контракт (собирается в `chunk_selection.py:96-105`). **Оставить.**

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_resolve_context_window_tokens` | 113-138 | То же, что `pipeline_structure._read_context_window_tokens` | Получить окно модели для расчёта `max_chars` | `resolve_max_chars:148` | Слить с `application/pipeline_structure.py` — **байт-идентичный дубль** (`duplicates.md` → `c280600df15ce5dc`) |
| `resolve_max_chars` | 141-158 | `max_chars = cwt * input_ratio * chars_per_token` | Потолок размера brief-входа | `build_brief_chunk` | Оставить |
| `_node_label` | 161-165 | Label узла для outline | Читаемость outline | `_render_outline` | Оставить |
| `_collect_subtree_ordinals` | 168-195 | Ordinals всех блоков в subtree | Сбор текста секции | `build_brief_chunk`, `_preamble_ordinals` | Оставить — но вызывается **дважды** на каждый top-level узел (407 и 413), O(2n) обход |
| `_render_outline` | 198-252 | Рекурсивный outline | Структурный каркас brief | `build_brief_chunk` | Упростить — ветка `root is None` (216) делает `"\n".join(lines[:max_chars])`, т.е. режет **список строк по символьному бюджету** и обходит `_trim_structure`; остальные три возврата её используют |
| `_trim_structure` | 255-264 | Обрезка по newline-boundary | Соблюдение `structure_max_chars` | `_render_outline` | Оставить |
| `_select_meaningful_level` | 267-306 | Выбор верхнего содержательного уровня | Не утапливать outline в тысячи строк | `build_brief_chunk` | Оставить |
| `_preamble_ordinals` | 309-323 | Блоки preamble (до первого top-level узла) | Не потерять вводную часть | `build_brief_chunk` | Оставить |
| `_render_section_content` | 326-346 | Текст subtree в document order | Основной контент brief | `build_brief_chunk` | Оставить |
| `build_brief_chunk` | 349-467 | Собрать единственный `Chunk` | Ядро brief-режима | `chunk_selection.py:106` | Упростить — docstring:29 ссылается на несуществующий `ChunkPlanner.build_chunk_config_from_runtime` (это module-level `chunker.py:57`); импортирует **приватный** `_make_chunk_id` из `chunking.chunker` (63) — межслойный приватный импорт |
| `_validate_blocks` | 470-502 | Инвариант ordinal'ов | Проверка п.29 | `build_brief_chunk` | Оставить |

---

## `application/brief_compression.py` — 168 LOC

**Назначение.** Детерминированная компрессия brief-секций (weighted allocation + безопасное усечение).
**Что делает.** `allocate_budget` распределяет доступные символы между секциями пропорционально весу, `render_sections` усекает по paragraph → newline → sentence → word → hard cut и склеивает с маркерами truncation.
**Зачем нужен.** Гарантирует, что brief влезет в окно модели без потери структуры.
**Вердикт.** **Оставить**; дублирования с `brief_context` **нет**.
**Обоснование.** Логика сжатия не дублируется: `brief_context` отвечает за *выбор и рендер* структуры, `brief_compression` — за *распределение бюджета и усечение текста*. Пересечение только в `_truncate_text_safely` (54-73) vs `brief_context._trim_structure` (255-264) — разные задачи (безопасная граница vs обрезка outline), допустимо.
**Доказательства.** Импортёр: `application/brief_context.py` (1).

#### class `BriefSection` (34-47, 1 метод)
`heading`, `text`, `weight`, `is_structural`. Нужен как DTO для аллокации. **Оставить** — носитель данных для `allocate_budget`/`render_sections`, удаление ломает обе.
| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `char_count` | 46-47 | `len(self.text)` | Вес для распределения бюджета | `allocate_budget`, `render_sections` | Оставить |

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_truncate_text_safely` | 54-73 | Усечение по безопасной границе (5 уровней) | Не разорвать предложение/слово | `allocate_budget`, `render_sections` | Оставить |
| `allocate_budget` | 76-120 | Weighted allocation символов | Справедливое распределение | `render_sections:132` | Оставить |
| `render_sections` | 123-160 | Финальный склей | Единственная точка рендера | `build_brief_chunk` | Упростить — docstring:2 утверждает «Использует существующий набор separators из `chunking._text_helpers`», но `_text_helpers` сепараторов не содержит, а `_SAFE_BOUNDARIES` определён локально (строка лжёт) |

---

## `application/question_context.py` — 258 LOC

**Назначение.** Сбор LLM-входа для question-synthesis из document-level cache (3 уровня: section summaries → chunk summaries → source text).
**Что делает.** `build_question_context` грузит summaries через `DocumentCache`, форматирует блоки, впихивает в `budget_chars` деградацией от самого длинного блока.
**Зачем нужен.** Позволяет ответить на вопрос без полного map-прогона.
**Вердикт.** **Упростить** — функциональный баг (находка 9) + мёртвые параметры и класс-обманка.
**Обоснование.** `_summaries_block_text(chunk, …)` (74-88) **вообще не использует** `chunk` в теле — из-за этого пришлось ввести `_FakeChunk` (91-96), чтобы вообще «иметь» объект. Это чистая конструкция ради несуществующей зависимости.
**Доказательства.** Импортёр: `application/service.py:165` (1, production). Тесты: `tests/test_question_context.py`.

#### class `_FakeChunk` (91-96, 1 метод)
Служебная заглушка, существующая только чтобы удовлетворить неиспользуемый параметр `chunk`.
| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 94-96 | Хранит `chunk_id`/`section_heading` | — | `build_question_context:187-193` | Удалить — вместе с параметром `chunk` у `_summaries_block_text` |

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_format_section_summary_block` | 33-71 | Один chunk-блок с метаданными | Метаданные + summary (+ source опционально) | `build_question_context` | Упростить — параметр `section_id` (36) не используется в теле |
| `_summaries_block_text` | 74-88 | Текст summaries без header'а | Сборка level-1/2 | `build_question_context` | Упростить — параметр `chunk` мёртв; из-за него же существует `_FakeChunk` |
| `build_question_context` | 99-255 | Сбор входа с бюджетом | Основная функция модуля | `service.py:165` | Упростить — **баг**: путь усечения (117-131) теряет `pages_str`; локаль `overhead` (221) не используется; комментарий 227-228 говорит «overhead включает `\nSOURCE TEXT:\n` (15 chars)» — литерал 14 символов и в `overhead` не входит; флаги `include_section_summary`/`include_chunk_summary` (101-102) всегда `True` в обоих вызовах |

---

## `application/estimation.py` — 200 LOC

**Назначение.** Верхняя граница числа LLM-вызовов и времени для конкретного запуска.
**Что делает.** `estimate_for_run` агрегирует map/reduce-вызовы по стратегии и плану; `quick_estimate` оценивает размер документа без полного парсинга.
**Зачем нужен.** Обеспечивает инвариант `actual_llm_calls <= estimated_llm_calls` (декларируется в docstring) для диагностики и UI.
**Вердикт.** **Оставить** с мелкими правками.
**Обоснование.** Формулы привязаны к `execution.config.MID_REDUCE_GROUP_SIZE` / `MAX_REDUCE_ROUNDS` — инвариант «оценка сверху» выдержан. Мелочи: `chunks` в `count_execution_calls` не используется; `_BATCH_OVERESTIMATE_RATIO = 1.0` превращает `int(x * 1.0)` в no-op; `insp`/`ctx` в `estimate_for_run` не типизированы.
**Доказательства.** Импортёры: `application/service.py`, `cli.py`.

#### class `Estimate` (31-37, 0 методов)
Поля `estimated_llm_calls`, `estimated_seconds`, `chunks`, `strategy`. Контракт для confirmation-гейта. **Оставить.**

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `needs_confirmation` | 40-41 | Порог подтверждения | Гейт перед дорогими прогонами | `service.py:396` | Оставить |
| `count_execution_calls` | 44-67 | Верхняя граница вызовов | Инвариант оценки | `estimate_for_run:115` | Упростить — параметр `chunks` не используется |
| `_hierarchical_reduce_calls` | 70-100 | Симуляция reduce-вызовов | Верхняя граница | `count_execution_calls` | Оставить |
| `estimate_for_run` | 103-129 | Estimate для run-контекста | Публичный API слоя | `service.py:399` | Упростить — `insp`/`ctx` без типов |
| `quick_estimate` | 132-191 | Оценка размера без парсинга | Быстрый CLI-путь | `cli.py` | Оставить |

---

## `application/manifest_builder.py` — 66 LOC

**Назначение.** Сборка начального `NormalizedManifest`.
**Что делает.** Заполняет 30 полей из `analysis`/`ctx`/`estimation` + `build_execution_plan` для `context_batches`.
**Зачем нужен.** Должен быть **единственным** конструктором manifest'а — сейчас это не так.
**Вердикт.** **Упростить** — расширить до единственного конструктора.
**Обоснование.** `NormalizedManifest` собирается в трёх местах: здесь (12-63), инлайн в `service._try_question_via_document_cache:224-272` и инлайн в `execution_orchestration._persist_final_manifest`. Канон должен быть здесь.
**Доказательства.** Импортёр: `application/execution_orchestration.py` (1).

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `build_manifest` | 12-63 | Начальный manifest | Resume-контракт | `execution_orchestration.py:270` | Упростить — сделать единственным конструктором; `now_iso` — нетипизированный инжектируемый callable без дефолта; копия title-резолва (34-38) |

---

## `application/document_io.py` — 62 LOC

**Назначение.** Извлечение plain text из файла документа (обёртка над `workspace.utils.office_files.extract_text`).
**Что делает.** `load_text` делегирует в `office_files`; для PDF в `mode="brief"` — первые N страниц через pypdf.
**Зачем нужен.** Единая точка чтения документа; используется `service.py` и `cli.py`.
**Вердикт.** **Оставить**.
**Обоснование.** Тонкая обёртка с реальной специализацией (PDF head), дублирования нет.
**Доказательства.** Импортёры: `application/service.py`, `cli.py`.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `load_text` | 18-38 | Plain text из файла | Вход для `operation_id` и `inspect` | `service.py:344`, `cli.py` | Оставить |
| `_extract_pdf_head` | 41-59 | Первые страницы PDF | Быстрый brief-режим | `load_text:33` | Оставить |

---

## `application/operation_id.py` — 44 LOC

**Назначение.** Детерминированный `operation_id` для manifest.
**Что делает.** sha256 полного текста (12 hex) + sha256 от `(path, length, question)` (8 hex) + суффикс `length`.
**Зачем нужен.** Idempotency manifest'а: два одинаковых прогона дают один id.
**Вердикт.** **Оставить**.
**Обоснование.** Корректная и осмысленная реализация; хеширование полного текста (а не префикса) — правильное решение, явно задокументированное в docstring.
**Доказательства.** Импортёр: `application/service.py`.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `make_operation_id` | 8-41 | Детерминированный id | Idempotency | `service.py:326` | Оставить |

---

## `application/__init__.py` — 1 LOC
Пустой пакетный маркер. **Оставить.**

---

## `cache/document_cache.py` — 484 LOC

**Назначение.** Единственный владелец document-level cache protocol.
**Что делает.** Хранит snapshot (`physical.json`, `analysis.json`, `retrieval_index.meta`, `_complete.marker`), per-chunk и per-section summaries; чинит битые снимки и вычищает orphan-siblings.
**Зачем нужен.** Инвариант «единственный владелец» **подтверждён** (см. вопрос 6) и **enforced тестом** `tests/architecture/test_document_cache_boundaries.py`, который явно штрафует `document_chunks_dir`, `document_chunk_result_path`, `document_section_result_path`, `_document_complete_marker_path` в любом другом модуле.
**Вердикт.** **Оставить**.
**Обоснование.** Границы выдержаны, тест-guard существует. Изменений не требуется.
**Доказательства.** Импортёры (4): `execution_orchestration.py`, `pipeline_structure.py`, `question_context.py`, `service.py`. Тесты: 9 файлов.

#### class `DocumentCache` (110-484, 16 методов)
Инкапсулирует `workspace_root` + `session_key`; инстанцируется заново в 6 местах (по одному на операцию) — вопреки собственному docstring «caller создаёт **один раз** в начале запроса и переиспользует».
| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 122-128 | Хранит root+session_key | Инкапсуляция конфигурации | 6 мест | Оставить |
| `_document_dir` | 134-136 | `<cache_root>/documents/<id>` | Единая раскладка | 6 self-call | Оставить |
| `_marker_path` | 138-140 | Путь к `_complete.marker` | Cheap cache-hit проверка | `is_complete`, `write_snapshot` | Оставить |
| `is_complete` | 146-152 | Snapshot complete? | Дешёвый cache hit | `pipeline_structure.py:152`, `service.py:110` | Оставить |
| `write_snapshot` | 158-265 | Атомарная запись snapshot | Заполнение кэша | `pipeline_structure.py:227` | Оставить |
| `_evict_orphan_siblings` | 267-318 | Удалить снапшоты старых версий файла | Гигиена кэша при смене mtime | `write_snapshot:233` | Оставить |
| `read_snapshot` | 320-343 | Прочитать snapshot | Восстановление | `pipeline_structure.py:155`, `service.py:116` | Оставить |
| `invalidate` | 345-358 | Удалить snapshot целиком | Самовосстановление | `pipeline_structure.py:171` | Оставить |
| `_chunk_path` | 364-365 | Путь к `chunks/<cid>.json` | Раскладка | `write_chunk_summary`, `_read_chunk_summary` | Оставить |
| `write_chunk_summary` | 367-405 | Запись per-chunk summary (question-independent only) | Semantic pollution guard | `execution_orchestration.py:319` | Оставить |
| `_read_chunk_summary` | 407-412 | Чтение | — | `load_chunk_summaries` | Оставить |
| `load_chunk_summaries` | 414-429 | Batch-чтение по списку id | Cross-operation lookup | `question_context.py` | Оставить |
| `_section_path` | 435-436 | Путь к `sections/<sid>.json` | Раскладка | `write_section_summary`, `_read_section_summary` | Оставить |
| `write_section_summary` | 438-464 | Запись per-section summary | Только question-independent | `execution_orchestration.py:477` | Оставить |
| `_read_section_summary` | 466-471 | Чтение | — | `load_section_summaries` | Оставить |
| `load_section_summaries` | 473-484 | Batch-чтение | Cross-operation lookup | `question_context.py` | Оставить |

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_atomic_write_json` | 66-74 | tmp + replace | Атомарность | 3 self-call | Оставить |
| `_read_json` | 77-84 | Чтение с tolerance к битому JSON | Устойчивость | 5 self-call | Оставить |
| `_skill_repo_root` | 87-93 | Абсолютный корень репо из `__file__` | Стабильный путь при `workspace_root=None` | `_cache_root:101` | Оставить |
| `_cache_root` | 96-107 | `<workspace>/data_store/cache/sessions/<safe_session_key>/documents` | Раскладка document cache | `__init__` | Оставить |

---

## `cache/manifest.py` — 388 LOC

**Назначение.** Operation-level manifest v2 (resume, idempotency, IPC-диагностика).
**Что делает.** Нормализует/сериализует `NormalizedManifest`, пишет per-chunk partial'ы и `result.json`, различает причины отсутствия манифеста для `cli_query.py`.
**Зачем нужен.** Resume-протокол и IPC-контракт. Явно отделён от document-level cache (docstring:17) — граница соблюдена.
**Вердикт.** **Оставить**.
**Обоснование.** Функционально необходимое. Единственная претензия — `_atomic_write_json`/`_read_json` (32-39, 42-48) дублируют одноимённые приватные функции из `document_cache.py` (66-74, 77-84). Модули в разных слоях, поэтому дублирование оправдано слоями; при слиянии не требуется.
**Доказательства.** Импортёры (5): `execution_orchestration.py`, `manifest_builder.py`, `service.py`, `cli_query.py`, `document/physical.py`. `tests/test_manifest_diagnose.py` — 8 тестов на `diagnose_manifest`.

#### class `NormalizedManifest` (142-194, 1 метод)
Единственный DTO манифеста; 30 полей, `to_dict` нормализует в v2.
| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `to_dict` | 169-194 | Сериализация в v2 | Формат на диске | `save_manifest:299` | Оставить |

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_atomic_write_json` | 32-39 | tmp + replace | Атомарность | 3 self-call | Оставить |
| `_read_json` | 42-48 | Чтение | Устойчивость | 5 self-call | Оставить |
| `_read_json_strict` | 51-71 | Чтение с флагом «файл есть» | Различение причин в диагностике | `diagnose_manifest:120` | Оставить |
| `_coerce_version_int` | 74-90 | `raw["version"]` как int | Диагностика v1/отсутствующего поля | `diagnose_manifest:127` | Оставить |
| `diagnose_manifest` | 93-138 | Диагностика для IPC | Structured error envelope | `cli_query.py:115` | Оставить |
| `skill_repo_root` | 197-203 | Корень репо из `__file__` | Стабильный путь | `manifest_root:214` | Оставить |
| `manifest_root` | 206-215 | Корень manifest'ов | Раскладка | 3 импортёра | Оставить |
| `manifest_path` | 218-219 | Путь к `manifest.json` | — | 8 ref-файлов | Оставить |
| `chunks_dir` | 222-223 | Путь к `chunks/` | — | `cli_query.py:193` | Оставить |
| `chunk_result_path` | 226-231 | Путь к partial'у | — | `write_chunk_result:329`, `read_chunk_result:338` | Оставить |
| `result_path` | 234-235 | Путь к `result.json` | — | `write_result`, `read_result` | Оставить |
| `_detect_version` | 238-250 | Только v2 | Отвергать legacy v1 | `load_manifest:289` | Оставить |
| `_normalize_v2` | 253-278 | raw dict → `NormalizedManifest` | Нормализация | `load_manifest:292` | Оставить |
| `load_manifest` | 281-292 | Чтение + нормализация | Resume/idempotency | `service.py:331,401`, `cli_query.py:132` | Оставить |
| `save_manifest` | 295-302 | Запись | — | 3 места | Оставить |
| `write_chunk_result` | 305-329 | Per-chunk partial | Resume | инжектится в `execution` | Оставить |
| `read_chunk_result` | 332-338 | Чтение partial | — | 3 ref-файла | Оставить |
| `write_result` | 341-347 | Финальный result | Idempotency + CLI | 3 места | Оставить |
| `read_result` | 350-354 | Чтение result | Idempotency fast path | `service.py:336` | Оставить |
| `load_cached_partials` | 357-368 | Batch-чтение partial'ов | Resume | инжектится в `execution` | Оставить |

---

## `cache/__init__.py` — 1 LOC
Пустой маркер. **Оставить.**

---

## `chunking/chunker.py` — 623 LOC

**Назначение.** Сборка `Chunk[]` из `PhysicalDocument` + `DocumentStructure` (оркестрация structural packing).
**Что делает.** `chunk_from_structure` (229-480) строит packable units, добавляет oversized-части, сортирует по physical order, зовёт `greedy_pack_units` и эмитит `Chunk[]` с section-преамбулой.
**Зачем нужен.** Единственная точка входа в chunking для `ChunkPlanner`.
**Вердикт.** **Упростить** — 6 мёртвых/дублирующих символов.
**Обоснование.** Границы с `chunks.py` (модель данных) и `structural_packing.py` (алгоритм) выдержаны корректно — дублирования работы между ними нет. Проблемы только в мёртвом коде и неверных docstring'ах.
**Доказательства.** Импортёры (2): `application/brief_context.py` (приватный `_make_chunk_id`), `application/pipeline_structure.py`.

#### class `DocumentStructureChunkerConfig` (48-54, 0 методов)
Обёртка над `ChunkConfig` с дефолтом `max_chunk_chars=100000, overlap=0`. Нужна как публичный контракт конфигурации. **Оставить.**

#### class `ChunkingDiagnostics` (560-595, 0 методов)
14 метрик chunking'а. **Удалить** — единственный потребитель — `chunk_from_structure_with_diagnostics`, а тот вызывается только из `tests/smoke_chunking_diagnostics.py` (не pytest-тест, а ручной smoke-скрипт). Поле `unassigned_blocks` (582) жёстко зашито в 0 (544) — метрика-константа.

#### class `ChunkPlanner` (609-623, 2 метода)
Единственный публичный вход: `ChunkPlanner(config).plan(physical, struct)` → `chunk_from_structure`.
| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 615-616 | Хранит конфиг | DI конфигурации | `pipeline_structure.py:321` | Оставить |
| `plan` | 618-623 | Тонкий делегат в `chunk_from_structure` | Публичный контракт | `pipeline_structure.py:322` | Оставить |

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `build_chunk_config_from_runtime` | 57-121 | `ChunkConfig` из runtime | Привязка к окну модели | `pipeline_structure.py:317` | Оставить — **канон**; `brief_context.py:29` ошибочно приписывает его `ChunkPlanner` |
| `_make_chunk_id` | 124-125 | `f"{idx:03}"` | Стабильный id | `chunk_from_structure:317`, `brief_context.py` (приватный импорт) | Оставить, но убрать межслойный приватный импорт из `brief_context` |
| `_section_path_for` | 128-141 | Ordinal-path до root | Section path в chunk'е | `_meta:289` | Оставить |
| `_ancestor_chain_titles` | 144-171 | Цепочка заголовков предков | Преамбула `[Контекст: …]` | `_build_context_preamble:199` | Оставить |
| `_build_context_preamble` | 174-206 | Преамбула с кэшем | Даёт LLM контекст размещения | `_emit_unit:370` | Оставить |
| `_is_strong_boundary` | 209-226 | DEPRECATED-обёртка | — | **0 вызовов** | Удалить — помечен `DEPRECATED`, вытеснен `structural_packing._is_strong_boundary_units`, никем не вызывается. После удаления `_MAJOR_SEMANTIC_TYPES` (44) станет мёртв |
| `chunk_from_structure` | 229-480 | Основной алгоритм | Единственный producer `Chunk[]` | `ChunkPlanner.plan:623` | Упростить — мёртвые `_unit_for_block` (410-422, никогда не вызывается) и `doc_oversized_parts` (431, 444 — пишется, не читается); docstring:16 «split by rows для oversize tables» **ложен** (row-split мёртв, см. `chunks.py`); docstring:9 «tables атомарны» противоречит фактическому поведению |
| `_collect_owner_section_ids` | 483-496 | Уникальные owner'ы блоков | Section ids unit'а | `chunk_from_structure:446` (+ мёртвая 413) | Слить с `chunking/structural_packing.py` — **байт-идентичен** `_collect_section_ids_for_range` (141-154) |
| `chunk_from_structure_with_diagnostics` | 499-556 | Chunks + метрики | — | только `tests/smoke_chunking_diagnostics.py` | Удалить |

---

## `chunking/structural_packing.py` — 471 LOC

**Назначение.** Иерархический structural packing: blocks → `PackableUnit[]` → greedy merge.
**Что делает.** Рекурсивный спуск по `DocumentStructure`: если subtree помещается в `target_chunk_chars` и не содержит oversized-блоков — один unit на всё subtree, иначе раскрытие на children + direct-блоки родителя.
**Зачем нужен.** Ядро качества chunking'а; 5–10x сокращение числа chunks на вложенных документах (docstring:17-18).
**Вердикт.** **Оставить** с точечной очисткой мёртвых членов.
**Обоснование.** Функционально необходимо. Мёртвое: `_node_has_specials` (88-106) — 0 вызовов, его логика инлайнится в `_build_units_for_node`; `_is_consecutive` (353-369) — 0 вызовов, docstring описывает удалённое поведение «small_table inline»; 2 из 4 значений `kind` не конструируются.
**Доказательства.** Импортёр: `chunking/chunker.py:256-260` (1). Тесты: `tests/test_structure_chunker_packing.py`.

#### class `PackableUnit` (41-65, 0 методов)
Поля: `kind`, `block_indices`, `section_ids`, `primary_section_id`, `char_count`, `table_id`, `source_block`, `source_char_start/end`.
**Упростить** — `kind: Literal["structural","table","oversized_part","root"]`, но конструируются только `"structural"` и `"oversized_part"`; `table_id` (62) **никогда не устанавливается**; `source_block` (63) пишется в `chunker.py:456` и **никогда не читается**. Мёртвые 2 enum-значения + 2 мёртвых поля.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_node_subtree_range` | 68-85 | Диапазон блоков subtree | Границы рекурсии | `_direct_blocks_for_node`, `_build_units_for_node` | Оставить |
| `_node_has_specials` | 88-106 | Есть ли oversized block в subtree | Ранний bail из рекурсии | **0 вызовов** | Удалить — логика инлайнится в `_build_units_for_node` |
| `_direct_blocks_for_node` | 109-138 | Блоки родителя, не покрытые детьми | «Gaps» между поддеревьями | `_build_units_for_node:279` | Оставить |
| `_collect_section_ids_for_range` | 141-154 | Уникальные owner'ы блоков | `section_ids` unit'а | 5 мест (195, 215, 243, 262, 285) | Оставить — **канон** дубля из `chunker.py:483` |
| `_build_units_for_node` | 157-310 | Рекурсивный спуск | Ядро алгоритма | `build_packable_units:465`, рекурсия 304 | Оставить |
| `_is_strong_boundary_units` | 313-331 | major→major переход | Граница chunk'а | `greedy_pack_units:434` | Оставить — канон мёртвого `chunker._is_strong_boundary` |
| `_merge_units` | 334-350 | Объединение соседних units | Greedy fill | `greedy_pack_units:420,441` | Оставить |
| `_is_consecutive` | 353-369 | Consecutiveness | — | **0 вызовов** | Удалить — docstring описывает «small_table inline», которого нет |
| `greedy_pack_units` | 372-444 | Greedy слияние units | Плотность chunks | `chunker.py:469` | Упростить — ветка `current.kind != "structural"` (409-412) недостижима (только 2 kind'а); `min_target = int(max_chunk_chars * 0.4)` (398) — магическое число рядом с конфигурируемым `preferred_min` |
| `build_packable_units` | 447-471 | Public entry point | Построить units | `chunker.py:424` | Упростить — docstring:458 «Tables обрабатываются как обычные блоки (atomic по построению)» противоречит docstring'ам `chunker.py:9` и `structural_packing.py:5` («tables атомарны»); фактически tables **не** атомарны и мержатся с соседним текстом |

---

## `chunking/chunks.py` — 394 LOC

**Назначение.** Модель данных чанка (`Chunk`, `ChunkConfig`) + утилиты разбиения/восстановления.
**Что делает.** `Chunk` — frozen dataclass с 20 полями и `to_dict`/`from_dict` для snapshot'а. `_split_block_with_offsets` режет oversized-блок по char-границам с overlap.
**Зачем нужен.** Границы с `chunker.py` **выдержаны**: `chunks.py` — данные, `chunker.py` — алгоритм. Дублирования работы между ними **нет**.
**Вердикт.** **Упростить** — 3 мёртвых символа.
**Обоснование.** `chunks.py:6-9` docstring утверждает «Body (paragraphs) chunk'ятся per-section через `lib.services.text_splitter` с overlap'ом» — такого импорта в файле нет, body chunk'ится в `structural_packing`; строка лжёт. `_make_table_chunk_text` и `_split_table_into_chunks` — row-splitting таблиц, объявленный в `chunker.py:16`, но никогда не вызываемый.
**Доказательства.** Импортёров: **26** — самый импортируемый модуль скилла.

#### class `Chunk` (90-270, 2 метода)
Модель чанка; 50 файлов ссылаются. `from_dict` терпим к отсутствию опциональных полей (back-compat для legacy-снапшотов).
| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `to_dict` | 147-182 | Сериализация | Запись в snapshot | `pipeline_structure.py:215` | Оставить |
| `from_dict` | 185-270 | Десериализация | Восстановление | `pipeline_structure.py:168`, `service.py:131` | Оставить |

#### class `ChunkConfig` (274-284, 0 методов)
`max_chunk_chars`, `chunk_overlap_chars`, `chars_per_token`, `table_chunk_threshold_chars`, `target_chunk_chars`, `preferred_min_before_strong_boundary`. **Оставить.**

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_split_block_with_offsets` | 32-86 | Разбить блок с сохранением offsets | Oversized-блоки → chunks | `chunker.py:439` | Оставить |
| `_make_table_chunk_text` | 287-289 | Текст части таблицы | — | **0 вызовов** | Удалить |
| `_split_table_into_chunks` | 292-318 | Row-split таблицы | — | **0 вызовов** | Удалить — реализация «split by rows», объявленного в `chunker.py:16`, отсутствует в проде |
| `reconstruct_source_fragment` | 322-387 | Точное восстановление исходного текста chunk'а | **Оракул для тестов** | `tests/test_structure_chunker_invariants.py:371` (проверяет `chunk.text == reconstruct_source_fragment(...)`) | Оставить — не «мёртвый», а load-bearing контракт; production-пути нет, но и удалять нельзя без потери верификации |

---

## `chunking/packing.py` — 164 LOC

**Назначение.** Controlled adjacent-section packing: `Chunk[]` → batch'и.
**Что делает.** `pack_chunks_with_adjacent` (67-158) склеивает соседние chunks в батчи, соблюдая `max_sections_per_batch`, `per_batch_token_budget`, table-изоляцию и root-изоляцию.
**Зачем нужен.** Живёт в batch-level, **не** конкурирует со `structural_packing` — это разные уровни (см. находку 4). Без него `map_calls == chunks_total` на больших документах.
**Вердикт.** **Оставить** код, **Упростить** документирование.
**Обоснование.** Модуль необходим (`planning/strategy.build_execution_plan:130` строит план именно им). Единственная проблема — docstring:3, описывающий несуществующий `packing_impl.pack_chunks` и подающий себя как «целевая политика» вместо реализации.
**Доказательства.** Импортёры (2): `planning/strategy.py` (production), `application/canonical.py` (мёртвый).

#### class `AdjacentPackingConfig` (34-49, 0 методов)
`max_sections_per_batch=2`, `per_batch_token_budget`, `chars_per_token=3.5`, `allow_table_table_batch`.
**Упростить** — дефолты `max_sections_per_batch=2` дублируются в `planning.strategy.ExecutionPolicy`; `build_execution_plan` всегда передаёт значения явно, поэтому дефолты используются только прямыми вызовами из тестов.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_section_id_for_chunk` | 52-53 | `chunk.section_id or ""` | Подсчёт distinct sections | `pack_chunks_with_adjacent` | Оставить |
| `_is_root_chunk` | 56-64 | Детект root-marker | Изоляция root-контента | `pack_chunks_with_adjacent:120` | Оставить |
| `pack_chunks_with_adjacent` | 67-158 | Greedy adjacent packing | Плотность батчей | `planning/strategy.py:133`, `canonical.py:232` (мёртвый) | Упростить — переписать docstring (находка 4); `current_is_table = is_table` (154) хрупко (эквивалентно `|=` только из-за flush'ов 129-137) |

---

## `chunking/_text_helpers.py` — 84 LOC

**Назначение.** Leaf-утилиты chunk-слоя: разметка блоков и усечение.
**Что делает.** `local_structure_label` находит первую heading-строку в тексте, `format_chunk_block` подписывает блок, `fit_input` усекает head+tail, `progress` пишет в stderr.
**Зачем нужен.** Канон для усечения (см. находку 3) и для progress-логов.
**Вердикт.** **Оставить**.
**Обоснование.** Правильно определённый leaf без зависимостей от downstream; именно сюда должна уйти логика из `hierarchical.deterministic_truncate`.
**Доказательства.** Импортёры (5): `chunk_selection.py`, `execution_orchestration.py`, `service.py`, `cli.py`, `map_reduce.py`.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `local_structure_label` | 25-35 | Первая heading-строка | Метка блока | `chunk_structure_label:43` | Оставить |
| `chunk_structure_label` | 38-43 | Глобальная или локальная метка | Метка блока | `format_chunk_block:48` | Оставить |
| `format_chunk_block` | 46-51 | Подпись блока | Читаемость reduce-входа | `map_reduce.py:385` | Оставить — `execution_orchestration.run_direct:121` его **обходит** и теряет метки |
| `fit_input` | 54-69 | Head+tail усечение | Бюджеты | `map_reduce.py:328,395`, `execution_orchestration.py:109` | Оставить — **канон**; дубль в `hierarchical.py:11` |
| `progress` | 72-75 | stderr-лог | UX CLI | 7 мест | Оставить |

---

## `chunking/importance_score.py` — 108 LOC

**Назначение.** Детерминированный importance score для chunk'а.
**Что делает.** `compute_importance` считает `ImportanceScore` по 5 признакам (is_title, is_heading, section_level, legal keywords, position); `select_top_chunks_by_importance` выбирает top-K.
**Зачем нужен.** **Не нужен** — 0 ссылок в production и 0 в тестах.
**Вердикт.** **Удалить** файл целиком.
**Обоснование.** Docstring:4 перечисляет 4 предполагаемых потребителя (brief, retrieval tie-break, packing, fallback selection) — ни один не импортирует модуль. Мёртвый код с ложными заявлениями о применении — худший вариант, т.к. провоцирует повторное «подключение».
**Доказательства.** `grep "compute_importance\|select_top_chunks_by_importance\|ImportanceScore"` по всему `legal_summarizer/` → 0 совпадений вне самого файла. Бриф: `static importers (0)`.

#### class `ImportanceScore` (32-53, 1 метод)
DTO весов importance. **Удалить** вместе с модулем — ни один символ модуля не имеет ссылок.
| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `total` | 44-53 | Взвешенная сумма компонент | Итоговый score | `compute_importance` (в мёртвом модуле) | Удалить |

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `compute_importance` | 56-87 | Score для chunk'а | — | **0 вызовов** | Удалить |
| `select_top_chunks_by_importance` | 90-105 | Top-K по score | — | **0 вызовов** | Удалить |

---

## `chunking/order.py` — 44 LOC

**Назначение.** Order-preserving утилиты после ranking'а.
**Что делает.** `restore_document_order` сортирует по `chunk.index`; `ensure_order_preserved` переупорядочивает по исходному списку id, сохраняя «extras» в конце.
**Зачем нужен.** **Не нужен** — 0 ссылок.
**Вердикт.** **Удалить** файл целиком.
**Обоснование.** Ни ranking, ни retrieval его не вызывают; порядок документов фактически сохраняется тем, что оба selection-пути (`select_chunks_for_mode`) обходят `insp.chunks` по порядку. `ensure_order_preserved` вдобавок содержит потенциальный баг: `chunks` перебирается дважды (35, 40), а если это генератор — второй проход даст пустой `extras`.
**Доказательства.** `grep "restore_document_order\|ensure_order_preserved"` → 0 совпадений вне самого файла.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `restore_document_order` | 16-24 | Сортировка по `index` | — | **0 вызовов** | Удалить |
| `ensure_order_preserved` | 27-41 | Переупорядочивание по исходным id | — | **0 вызовов** | Удалить |

---

## `chunking/__init__.py` — 1 LOC
Пустой маркер. **Оставить.**

---

## `execution/map_reduce.py` — 738 LOC

**Назначение.** Фактическая реализация map-reduce execution: батчинг → LLM-вызовы → reduce, без прямых записей в кэш.
**Что делает.** `run_map_reduce_execution` (456-732) проходит 6 фаз: invariants → cached partials → queued batches → `_run_all_batches` (последовательно, `Semaphore(1)`) → `_persist_batch_results` (через инжектированный callback) → `_reduce_phase` (hierarchical или flat). При частичных сбоях статус `partial`, reduce идёт по уцелевшим батчам.
**Зачем нужен.** Основной исполняющий слой для документов, не влезающих в direct-вызов. **Инвариант single_flight соблюдён** — `execution` не импортирует `llm.single_flight` напрямую, а `llm.calls` внутри `run_one_batch_async` применяет flight-lock; плюс `Semaphore(1)` как второй уровень сериализации.
**Вердикт.** **Оставить** с точечными правками.
**Обоснование.** Дизайн корректен: деградация при сбое отдельного батча реализована (`is_partial` → reduce по `all_partials`), DI соблюдает layer-guard. Дефекты — баг с `chunk_states` (находка 7), дублированный docstring, мёртвые параметры и поля статистики.
**Доказательства.** Импортёры (2): `application/execution_orchestration.py`, `application/service.py`. Тестов, задевающих файл, **нет** — при том, что это крупнейший файл скилла (пробел покрытия).

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_assert_invariants` | 70-90 | Проверка покрытия chunks батчами | Защита от потери данных при планировании | `run_map_reduce_execution:547` | Упростить — параметр `plan` (71) **не используется** в теле; docstring «инварианты Phase 2B между plan и chunks» лжёт (проверяется только покрытие `chunks` батчами) |
| `_queued_batches` | 93-114 | Pending chunks по батчам | Resume | `run_map_reduce_execution:572` | Оставить — источник бага «completed без файла» |
| `_run_all_batches` | 117-147 | Последовательный прогон батчей | Исполнение | `run_map_reduce_execution:575` | Упростить — `asyncio.gather` без `return_exceptions=True`: с реальным `run_one_batch_async` безопасно (тот ловит всё), но инъектированный mock, бросающий исключение, уронит весь прогон. `Semaphore(1)` + `gather` — избыточная concurrency-обвязка для последовательного цикла |
| `_persist_batch_results` | 150-255 | Запись partial'ов + stats | Resume + document cache | `run_map_reduce_execution:585` | Упростить — **баг**: `chunk_states[...]="completed"` (219-228) вне `if c.chunk_id in chunk_results`; `assert last_error is not None` (230) и `assert batch_meta is not None` (194) — asserts в проде, отключаются под `python -O`; `retries` не считает внутренние parse-retry из `pipeline.MAX_BATCH_PARSE_RETRIES` |
| `_reduce_phase` | 258-433 | Hierarchical или flat reduce | Финальный синтез | `run_map_reduce_execution:604` | Упростить — дублирует локальный `import os/sys` и `_mr_trace` (272-292), уже определённые в 502-518; `section_trim_calls: 0` в возвращаемой структуре — вестимая метрика |
| `_build_initial_partials_from_cache` | 436-453 | Chunk_states из кэша | Resume | `run_map_reduce_execution:557` | Оставить |
| `run_map_reduce_execution` | 456-732 | Основная функция | Оркестрация фаз | `execution_orchestration.run_map_reduce:296` | Упростить — **дублированный docstring-блок 528-544** как «сиротский» строковый литерал после кода (17 мёртвых строк); `stats["section_trim_calls"] = 0` (686) — константа; копия title-резолва (680-684); `batches_done` формируется в `execution_orchestration` (не здесь) |

---

## `execution/hierarchical.py` — 294 LOC

**Назначение.** Иерархический reduce: chunk summaries → section summaries → document summary.
**Что делает.** `reduce_chunks_hierarchical` (173-293) суммирует chunks по секциям, затем `reduce_sections_to_document` (51-170) схлопывает секции раундами по `group_size` с финальным добивочным round'ом, чтобы не терять данные.
**Зачем нужен.** Канонический reduce для `map_hierarchical`. Инвариант «данные не теряются» реализован честно (132-156).
**Вердикт.** **Слить с `chunking/_text_helpers.py`** (усечение) + **Упростить** (API).
**Обоснование.** Ключевая находка брифа разрешена: неканонической копии reduce-функций **нет**, но docstring «canonical copy» вводит в заблуждение. Реальные проблемы — дублирование `deterministic_truncate` (находка 3), отсутствие `__all__`, публичность функции без внешних потребителей, мёртвая ветка `llm_runner is None` в проде, и `structure` у `reduce_sections_to_document`, который никогда не передаётся.
**Доказательства.** Импортёр: `execution/map_reduce.py:42-45` (1). Бриф: `test files touching it: — none —`.

#### class `HierarchicalReducerResult` (39-45, 0 методов)
`final_summary`, `section_summaries`, `rounds_done`, `truncated`. Нужен как DTO reduce-фаз.
**Оставить** — 2 ref-файла, 1 тест.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `deterministic_truncate` | 11-30 | Head+tail усечение с маркером | Бюджеты reduce | `hierarchical.py:35` | Слить с `chunking/_text_helpers.py` — **байт-идентичен** `fit_input` (54-69) |
| `_fit_input` | 33-35 | Pass-through обёртка | — | `hierarchical.py:110,139,230,243` | Удалить вместе с `deterministic_truncate`; заменить импортом `fit_input` |
| `reduce_sections_to_document` | 51-170 | Раунды схлопывания секций | Финальный синтез | `hierarchical.py:262` (**единственный** вызов) | Упростить — сделать приватной (`_reduce_sections_to_document`): вне модуля потребителей нет; docstring «Используется, когда section_summaries уже есть (follow-up или pre-computed)» — **ложь** (follow-up-пути нет); параметр `structure` (58) в проде всегда `None`, т.к. `reduce_chunks_hierarchical:262-268` его не передаёт, а работает лишь за счёт closure-default в `map_reduce._llm_doc_runner:339`; ветка `llm_runner is None` (112, 141) в проде не достигается |
| `reduce_chunks_hierarchical` | 173-293 | Chunk→section→document | Reduce для map_hierarchical | `map_reduce.py:290` | Упростить — `focus=None` жёстко зашит в section-вызов (238) без объяснения; `sections_with_chunks`/`sections_skipped` (216-226) используются только в trace; если `section_items` пусто (283) → `final_summary=""` → `REDUCE_INPUT_EMPTY`, т.е. тихий отказ |

---

## `execution/pipeline.py` — 157 LOC

**Назначение.** Прогон одного LLM-батча с retry-циклом, без побочных эффектов на кэш.
**Что делает.** `process_context_batch` — один `llm_batch` + parse; `run_one_batch_async` — обёртка с `asyncio.to_thread`, семафором и до 3 попытками при parse-ошибке; исключения деградируются в кортеж `(code, exc)`, а не пробрасываются.
**Зачем нужен.** Чистое исполнение батча; отделяет LLM-логику от persistence.
**Вердикт.** **Оставить**.
**Обоснование.** Корректный и узкий модуль. Замечание: `_run_all_batches` в `map_reduce` обрачивается `run_one_batch_async` в `to_thread`, так что `Semaphore(1)` + `gather` — чистая избыточность.
**Доказательства.** Импортёр: `application/execution_orchestration.py:406-410` (функциональный импорт).

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `now_iso` | 41-43 | ISO 8601 UTC | Метки времени в метаданных | 3 места | Оставить |
| `process_context_batch` | 46-90 | Один LLM-вызов + parse | Ядро батча | `run_one_batch_async:120` | Оставить |
| `run_one_batch_async` | 93-149 | Async-обёртка + retry | Concurrency-слой | инжектится в `map_reduce` | Оставить — `MAX_BATCH_PARSE_RETRIES` здесь, но не учитывается в `stats.retries` |

---

## `execution/config.py` — 42 LOC

**Назначение.** Execution-policy конфигурация reducer'а.
**Что делает.** Объявляет `MID_REDUCE_GROUP_SIZE=3`, `MAX_REDUCE_ROUNDS=4` и `HierarchicalReducerConfig` с теми же значениями по умолчанию.
**Зачем нужен.** Единый источник параметров reduce для `execution` и `estimation`.
**Вердикт.** **Упростить** — модуль нарушает собственную декларацию.
**Обоснование.** Docstring:7-16 заявляет «Единый источник истины» и «если добавить ещё одного потребителя — он обязан импортировать отсюда, а не дублировать литералы», после чего **сам** дублирует литералы: `group_size: int = 3` (32) и `max_rounds: int = 4` (33) не ссылаются на `MID_REDUCE_GROUP_SIZE` / `MAX_REDUCE_ROUNDS`. Расхождение констант и дефолтов не проверяется ничем.
**Доказательства.** Импортёры (3): `application/estimation.py`, `execution/hierarchical.py`, `execution/map_reduce.py`.

#### class `HierarchicalReducerConfig` (29-35, 0 методов)
`group_size`, `max_rounds`, `input_budget_chars`, `section_summary_max_chars`. 7 ref-файлов, 5 тестов.
**Упростить** — дефолты `group_size`/`max_rounds` заменить ссылками на константы модуля.

---

## `execution/__init__.py` — 1 LOC
Пустой маркер. **Оставить.**

---

## Функциональные баги

| # | Место | Суть | Симптом |
|---|---|---|---|
| B1 | `execution/map_reduce.py:219-228` | `chunk_states[...]["status"]="completed"` присваивается вне `if c.chunk_id in chunk_results` | Chunk, для которого LLM не вернул summary, помечается `completed`; при resume `_queued_batches:101-105` его пропускает, а файла нет → chunk навсегда выпадает из reduce-входа, молча |
| B2 | `application/execution_orchestration.py:118-126` | `retries += 1` без ретрая | `stats.retries` показывает 1 при единственной неудаче — метрика врёт |
| B3 | `application/execution_orchestration.py:191,290` | initial manifest = `running`; terminal-статус пишется только для completed/partial | При `failed` manifest навсегда остаётся `running` → `cli_query.py` и resume показывают неверное состояние |
| B4 | `application/question_context.py:117-131` | Budget-усечение пересобирает head без `pages_str` | Потеря page provenance в LLM-контексте при срабатывании лимита |
| B5 | `application/canonical.py:193-196` | `estimate_canonical` вызывает `inspect_canonical(text="")` | Возвращаемый `chars_in` **всегда 0** |
| B6 | `application/brief_context.py:216` | `"\n".join(lines[:max_chars])` | Обрезка **списка строк** символьным бюджетом; `_trim_structure` обойдена — только для ветки `root is None` |
| B7 | `application/service.py:133-135` | Битый snapshot → `return None` без `cache.invalidate()` | В отличие от `pipeline_structure.py:171`, question-путь не самовосстанавливается: битый snapshot блокирует вопросы навсегда |

---

## Кросс-подсистемные находки

1. **`document/structure.py:428-…` и `document/block_ownership.py:41-…` — байт-идентичные
   `build_block_ownership` / `owner_for_block` / `_depth_of`.** `chunker.py:33-37` импортирует
   оба и ре-экспортирует в `__all__:604-605`, но никто не импортирует оттуда. → аудитору `document/`.
2. **`_try_question_via_document_cache` (`service.py:78-291`) — копия
   `pipeline_structure._try_load_cached_pipeline_result:108-187`.** Обе читают snapshot и
   пересобирают `DocumentAnalysis`; канон — в `pipeline_structure` (он чинит битый snapshot).
3. **`contextWindowTokens` читается двумя копиями** —
   `pipeline_structure._read_context_window_tokens` и `brief_context._resolve_context_window_tokens`.
4. **Document-level кэш никогда не наполняется в direct-режиме.** `write_chunk_summary`
   вызывается только из `run_map_reduce` (`execution_orchestration.py:319`),
   `write_section_summary` — только из `_persist_final_manifest:477`. `run_direct` не пишет
   **ничего**. А brief-режим всегда `direct` (1 chunk → `context_builder:62`), и
   `build_execution_context` форсирует `direct` для любого документа с ≤1 chunk'ом.
   Значит `question_context.build_question_context` (3 уровня кэша) для brief-документов
   **гарантированно промахивается**. Либо это баг, либо фича не доведена — требует
   подтверждения владельцем скилла.
5. **`execution_orchestration.run_direct:118-121` обходит `format_chunk_block`** — direct-режим
   отдаёт LLM вход без section-меток, которые flat-reduce добавляет. Разное качество
   контекста для одного и того же документа в зависимости от стратегии.
6. **Покрытие: у `execution/map_reduce.py` (738 LOC, крупнейший файл скилла) — 0 тестов**;
   у `execution/hierarchical.py` — 0; у `application/service.py` — 0 прямых.
   При этом `tests/architecture/test_layer_boundaries.py` и
   `test_document_cache_boundaries.py` архитектурные инварианты проверяют надёжно.
7. **Страница документации `references/architecture.md:213-215` и
   `docs/document_cache_refactoring_inventory.md:148-177` описывают удалённые символы**
   (`document_chunks_dir`, `document_chunk_result_path`, `document_section_result_path`,
   `_document_complete_marker_path`, `_load_manifest_or_none`) — актуальны только как
   история рефакторинга.

---

## Покрытие

Разобрано **165 из 165** символов из брифа `10-skill-legal-a` (20 классов, 24 метода,
119 функций + 2 служебные строки таблиц) + 3 вложенные функции `_run_all_batches._gather_all`,
`_mr_trace` (в трёх функциях) и `_persist_batch_results` (внутр. циклы).
**`НЕ РАЗОБРАНО`: 0.**

**Что НЕ проверено запуском:** все вердикты основаны на статическом анализе (AST-граф
+ grep по всему репозиторию, включая `tests/`, `docs/`, `.github/`, `project.json`,
`SKILL.md`). Динамического прогона (`python scripts/cli.py`) не выполнялось; утверждение
«brief всегда идёт в direct» выведено из кода (`chunk_selection.py:115` → `[chunk]` →
`context_builder.py:62` → `service.py` ветвление) и требует подтверждения запуском, если
будет использоваться как основание для правок.
