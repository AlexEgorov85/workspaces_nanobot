# legal_summarizer — Legacy Inventory (Этап 1)

Карта legacy-символов, файлов и runtime-imports по всему репозиторию.
Цель — определить, **что реально используется в production**, а что осталось
как compatibility layer / back-compat facade и подлежит удалению согласно
`PLAN.md` (Этапы 1–39).

Дата фиксации: 2026-09-04.
Связанные документы: `docs/legal_summarizer_baseline.md` (Этап 0),
`docs/legal_summarizer_audit_stage1.md` (Этап 1), `docs/legal_summarizer_final_audit.md`.

---

## 1. Резюме текущего состояния

После Этапов 1–50 (см. `final_audit.md`) **canonical pipeline создан** в
`scripts/structure/` (`run_canonical_pipeline`, `DocumentAnalysis`,
`DocumentStructure`, `HierarchicalReducer`, `ChunkPlanner`, `ExecutionPlan`,
`DocumentIdentity`, и т.д.).

Однако `summarizer.py` — главный production-орчестратор (1773 строки) —
**остался на legacy-пути**. В частности, `summarizer.py` импортирует
и вызывает:

- `structure/sections.py::detect_sections`, `merge_short_sections`,
  `SectionTree`, `DocumentSection`, `ROOT_SECTION_ID`,
  `count_meaningful_sections`, `extract_local_structure_label`
- `structure/chunks.py::StructureAwareChunker`
- `structure/physical.py::load_physical_document`
- `fingerprint.py::document_id_for`, `resolve_document_id`, `resolve_session_key`
- `document_cache.py::doc_cache_dir`, `load_doc_cache`, `save_doc_cache`
- `document_cleanup.py::cleanup_blocks`, `CleanupConfig`
- `execution_strategy.py::select_execution_strategy`, `ExecutionStrategy`,
  `StrategyConfig`
- `reducer_strategy.py::select_reduce_strategy`
- `reducer.py::reduce_results`, `ReduceConfig`
- `packing.py::pack_chunks`, `ContextBatch`, `TokenBudget`
- `token_budget.py::count_tokens`

Дополнительные legacy helpers внутри самого `summarizer.py`:

- `_hierarchical_reduce_rounds` (recursive rounds для hierarchical reduce)
- `_fit_input` (head+tail truncate strategy)

`run_canonical_pipeline` и `DocumentAnalysis` **не используются**
в production потоке `summarizer.py`. Они применяются только в
тестах/benchmarks нового pipeline.

---

## 2. Классификация legacy-компонентов

Обозначения:

- **Production** — используется в `summarizer.py` или в CLI/follow-up
  runtime-пути.
- **Back-compat facade** — модуль только re-экспортирует функции,
  импортируется из тестов и/или через facade.
- **Test only** — импортируется только тестами.
- **Никто** — нет consumers, можно удалять без миграции.

| Legacy модуль / символ | Production consumer(s) | Canonical replacement | Категория | Можно удалить |
|---|---|---|---|---|
| `structure/sections.py::SectionTree`, `DocumentSection`, `ROOT_SECTION_ID` | `summarizer.py`, `structure/chunks.py`, `structure/compatibility.py`, `reducer_impl.py`, `reducer_strategy.py`, `document_stats.py` | `structure/models.py::DocumentStructure`, `StructureNode` | Production + back-compat | Нет (Этап 9) |
| `structure/sections.py::detect_sections` | `summarizer.py` | `structure/hierarchy.py::build_document_structure` через `run_canonical_pipeline` | Production | Нет (Этап 4) |
| `structure/sections.py::merge_short_sections` | `summarizer.py` | `structure/safety_merge.py::safety_merge` или `ChunkPlanner` напрямую | Production | Нет (Этап 4) |
| `structure/sections.py::count_meaningful_sections` | `summarizer.py` | `structure/unified_execution.py::_count_meaningful_sections` (canonical copy) | Production (дубль) | Нет (Этап 4) |
| `structure/sections.py::extract_local_structure_label` | `summarizer.py` (`_chunk_structure_label`), `reducer_impl.py` | Из `DocumentStructure` + `SemanticRecord` (PLAN §9) | Production | Нет (Этап 9) |
| `structure/tree.py::DocumentSection`, `SectionTree`, `build_section_tree`, `ROOT_SECTION_ID` | `structure/sections.py` (через `from .tree import …`) | `structure/models.py::DocumentStructure`, `StructureNode` | Production + back-compat | Нет (Этап 9) |
| `structure/chunks.py::StructureAwareChunker` | `summarizer.py`, `packing_impl.py`, `packing_models.py`, `document_stats.py`, `brief_representation.py`, `provenance_reconstruction.py`, `cache_followup.py` + **все canonical `structure/*.py` модули** | `structure/document_chunker.py::ChunkPlanner` (но низкоуровневые `Chunk`/`ChunkConfig` остаются shared) | Mixed | Нет (Этап 6) |
| `structure/chunks.py::Chunk`, `ChunkConfig` | Все потребители выше | Shared low-level model | Shared | Нет — общий контракт |
| `structure/compatibility.py::section_tree_from_structure`, `structure_from_section_tree` | `tests/test_structure_compatibility.py` (test only) | — (обёртка отменяется) | Back-compat facade | Нет (Этап 20) |
| `fingerprint.py::fingerprint_file`, `document_id_for`, `resolve_session_key`, `resolve_document_id` | `summarizer.py`, `document_cache.py` | `structure/identity.py::DocumentIdentity` | Production | Нет (Этап 14) |
| `document_cache.py::doc_cache_dir`, `load_doc_cache`, `save_doc_cache`, `cache_is_fresh`, `load_doc_cache_meta` | `summarizer.py`, `cache_followup.py`, `provenance_reconstruction.py` | `DocumentAnalysis` + `DocumentIdentity.freshness` | Production | Нет (Этап 15) |
| `cached_retrieval.py::select_cached_candidates`, `select_relevant_chunks`, `CachedCandidate`, `is_confident` | `cache_followup.py`, `provenance_reconstruction.py` (через `select_cached_candidates`) | `structure/retrieval.py::retrieve_chunks` + `structure/retrieval_index.py::RetrievalIndex` | Production (но не в summarizer) | Нет (Этап 16) |
| `document_cleanup.py::cleanup_blocks`, `CleanupConfig`, `CleanupResult`, `normalize_whitespace` | `summarizer.py` | `structure/cleanup.py::cleanup_repeated_blocks` | Production | Нет (Этап 19) |
| `context_expansion.py::expand_followup_context`, `ExpandedContext`, `expanded_context_to_dict` | `cache_followup.py` | `structure/context_expansion.py::expand_context` (уже есть) | Production (но не в summarizer) | Нет (Этап 17) |
| `execution_strategy.py::ExecutionStrategy`, `StrategyConfig`, `select_execution_strategy` | `summarizer.py` | `structure/unified_execution.py::select_strategy` + `ExecutionPlan` | Production | Нет (Этап 7) |
| `reducer.py::reduce_results`, `ReduceConfig` | `summarizer.py` | `structure/hierarchical_reducer.py::reduce_chunks_hierarchical` + `reduce_sections_to_document` | Production | Нет (Этап 9) |
| `reducer_impl.py::_reduce_flat`, `_reduce_hierarchical`, `reduce_results`, `_llm_section_trim` | `reducer.py` (facade) | `structure/hierarchical_reducer.py` | Back-compat facade | Нет (Этап 9) |
| `reducer_strategy.py::should_use_hierarchical_reduce`, `select_reduce_strategy` | `summarizer.py`, `reducer.py`, `reducer_impl.py` | `ExecutionPlan.strategy` (одна точка решения) | Production | Нет (Этап 8) |
| `reducer_models.py::ReduceConfig`, `ReduceStats`, `ReduceResult`, `ReduceStrategy`, `LLMRunner` | `reducer.py`, `reducer_impl.py`, `reducer_strategy.py` | `structure/hierarchical_reducer.py::HierarchicalReducerConfig` + `HierarchicalReducerResult` | Production (через facade) | Нет (Этап 9) |
| `packing.py::pack_chunks`, `ContextBatch`, `TokenBudget` | `summarizer.py`, `llm_calls.py`, `prompts.py`, `pipeline.py`, `packing_impl.py` | `structure/adjacent_packing.py::pack_chunks_with_adjacent` + `ExecutionPlan` | Production | Нет (Этап 13) |
| `packing_impl.py::pack_chunks` | `packing.py` (через facade) | `structure/adjacent_packing.py` | Back-compat facade | Нет (Этап 13) |
| `packing_models.py::ContextBatch`, `PackingConfig` | `packing.py`, `packing_impl.py` | `structure/execution_plan.py::PlannedBatch`, `ExecutionPlan` | Back-compat facade | Нет (Этап 13) |
| `token_budget.py::TokenBudget`, `count_tokens` | `summarizer.py`, `packing.py`, `packing_impl.py` | `structure/token_estimator.py::TokenEstimator` | Production | Нет (Этап 12) |
| `brief_strategy.py::select_brief_chunks`, `select_brief_chunks_structured`, `select_relevant_chunks` | `summarizer.py` (через local `import` в режиме brief/question) | `structure/importance_brief.py::select_brief_chunks` (через `DocumentAnalysis`); `structure/followup.py::build_followup_response` для question | Production | Нет (Этап 18) |
| `brief_representation.py::allocate_brief_budget`, `apply_brief_text_budget`, `total_input_chars` | `summarizer.py` | Нужен перенос minimal reusable logic в canonical (PLAN §23) | Production | Нет (Этап 18) |
| `cache_followup.py::retrieve_followup_context_via_cache` | `summarizer.py` (через local `import`) | `structure/followup.py::build_followup_response` (уже есть, но не используется в summarizer) | Production | Нет (Этап 15–17) |
| `provenance_reconstruction.py::reconstruct_candidate_source`, `reconstruct_candidates_sources` | `cache_followup.py` | `structure/provenance.py::build_provenance_chain` (через `DocumentAnalysis`) | Production | Нет (Этап 15–16) |
| `_hierarchical_reduce_rounds` (внутри `summarizer.py`) | `summarizer.py` (self) | `structure/hierarchical_reducer.py::reduce_chunks_hierarchical` | Production | Нет (Этап 10) |
| `_fit_input` (head+tail truncate, в `summarizer.py`) | `summarizer.py` (self) | `structure/hierarchical_reducer.py::deterministic_truncate` + `_fit_input` (deterministic truncate) | Production | Нет (Этап 11) |
| `llm_section_trim` (в `llm_calls.py`) | `llm_calls.py::llm_section_trim` (legacy path) | `structure/hierarchical_reducer.py::deterministic_truncate` (no-LLM) | Production | Нет (Этап 11) |

---

## 3. Сводка: классификация в файлах

### 3.1. `scripts/summarizer.py` — главный оркестратор

**Production imports** (runtime, на горячем пути `run()` / `inspect()`):

- `structure/sections.py::detect_sections`, `merge_short_sections`,
  `SectionTree`, `DocumentSection`, `ROOT_SECTION_ID`,
  `count_meaningful_sections`, `extract_local_structure_label`
- `structure/chunks.py::Chunk`, `ChunkConfig`, `StructureAwareChunker`
- `structure/physical.py::DocumentBlock`, `PhysicalDocument`,
  `load_physical_document`
- `fingerprint.py::document_id_for`, `resolve_document_id`, `resolve_session_key`
- `document_cache.py::doc_cache_dir`, `load_doc_cache`, `save_doc_cache`
- `document_cleanup.py::CleanupConfig`, `cleanup_blocks`
- `document_stats.py::DocumentStats`, `compute_document_stats`
- `execution_strategy.py::ExecutionStrategy`, `StrategyConfig`,
  `select_execution_strategy`
- `reducer_strategy.py::select_reduce_strategy`
- `reducer.py::ReduceConfig`, `reduce_results`
- `packing.py::ContextBatch`, `TokenBudget`, `pack_chunks`
- `pipeline.py::process_context_batch`, `run_one_batch_async`,
  `load_cached_partials`, `now_iso`, `MAX_BATCH_PARSE_RETRIES`
- `llm_calls.py::doc_context`, `llm_batch`, `llm_section_reduce`,
  `llm_document_reduce`
- `manifest.py::NormalizedManifest`, `load_manifest`, `manifest_path`,
  `read_result`, `result_path`, `save_manifest`, `write_result`
- `prompts.py::ChunkResultParseError`, `build_batch_user_message`,
  `parse_batch_response`
- `prompts_runtime.py::LENGTH_INSTRUCTIONS`, `QUESTION_INSTRUCTION_TEMPLATE`,
  `load_prompt`, `system_instruction`
- `sanitize.py::extract_subject`, `strip_think_blocks`, `_THINK_BLOCK_RE`,
  `_THINK_OPEN`, `_THOUGHT_CLOSE`
- `token_budget.py::count_tokens`

**Local imports (lazy)** в режимах question/brief:

- `cache_followup.retrieve_followup_context_via_cache`
- `brief_strategy.select_relevant_chunks`
- `brief_strategy.select_brief_chunks_structured`
- `brief_representation.allocate_brief_budget`
- `brief_representation.apply_brief_text_budget`

**Internal legacy functions** (без `from … import`):

- `_hierarchical_reduce_rounds` (recursive grouping rounds)
- `_fit_input` (head+tail truncate)
- `_format_chunk_block` (через `_chunk_structure_label`)
- `_chunk_structure_label` (через `extract_local_structure_label`)

**Canonical imports** — **отсутствуют**. `run_canonical_pipeline`,
`DocumentAnalysis`, `DocumentStructure`, `ChunkPlanner`, `HierarchicalReducer`,
`ExecutionPlan`, `DocumentIdentity` — НЕ импортируются.

### 3.2. `scripts/cli.py`

Только entry-points: `_PROJECT_ROOT`, `_DONE_SENTINEL`, `main`. Внутренние
imports к `summarizer.py`/`manifest.py` подтверждены через прямой запуск
(`python cli.py --help`). Внутренние legacy-импорты — отсутствуют
(`from workspace...` секций в AST не зафиксировано — likely через relative
imports).

### 3.3. `scripts/cli_query.py`

Только `main`. Никаких дополнительных imports не выявлено.

### 3.4. `scripts/cache_followup.py`

Импортирует ВСЕ legacy: `cached_retrieval`, `context_expansion`,
`document_cache`, `provenance_reconstruction`, плюс canonical
`structure/physical.py`. Этот модуль — отдельный production runtime path
для follow-up retrieval. Не используется через `summarizer.py`, но
используется локально при `--question` (lazy import в `summarizer.py:836`).

### 3.5. `scripts/provenance_reconstruction.py`

Использует `cached_retrieval.CachedCandidate` и
`document_cache.cache_is_fresh`. Зависит от legacy, но не импортируется
напрямую из `summarizer.py` — только через `cache_followup.py`.

### 3.6. `scripts/document_cleanup.py`

Legacy cleanup (mark repeated headers/footers). Используется
`summarizer.py` напрямую в `inspect()` строке 585. Canonical counterpart —
`structure/cleanup.py::cleanup_repeated_blocks` (уже есть, но не
подключён к `summarizer.py`).

### 3.7. `scripts/llm_calls.py::llm_section_trim`

Отдельный LLM-call для trim. Сейчас legacy-путь в `summarizer.py` —
truncate (не LLM). Сам `llm_section_trim` остаётся в `llm_calls.py`,
но **не вызывается** production кодом (поиск показывает только
определение). Безопасный кандидат на удаление (Этап 11).

### 3.8. `scripts/structure/compatibility.py`

Back-compat adapter: `section_tree_from_structure` /
`structure_from_section_tree`. Используется **только тестами**
(`tests/test_structure_compatibility.py`). Нет production consumers.
Удалить после миграции теста (Этап 20).

### 3.9. `scripts/structure/chunks.py::StructureAwareChunker`

Самая широкая зависимость: импортируется **всеми** legacy- и
canonical-модулями. Точка критической миграции (Этап 6):
низкоуровневые `Chunk`/`ChunkConfig` остаются shared, но
`StructureAwareChunker` как алгоритм нужно заменить на
`document_chunker.ChunkPlanner`.

---

## 4. Точки удаления (по этапам PLAN.md)

Краткий путь от текущего состояния к финальной архитектуре (один
production pipeline):

| Этап PLAN | Действие | Затрагиваемые файлы |
|---|---|---|
| 2 | Исправить canonical hierarchy (nested) | `scripts/structure/hierarchy.py` |
| 3 | Ввести `block_owner(block_ordinal) -> node_id` | `scripts/structure/document_chunker.py` |
| 4 | Миграция `summarizer.py.inspect()` → canonical pipeline | `summarizer.py`, `scripts/structure/pipeline.py` |
| 5 | Удалить `legacy_document_structure_path` | n/a (нет такого файла) |
| 6 | `StructureAwareChunker` → `ChunkPlanner` (в production) | `summarizer.py`, всё, что импортирует `StructureAwareChunker` |
| 7 | `execution_strategy.py` → `ExecutionPlan.strategy` | `summarizer.py`, `scripts/structure/unified_execution.py` |
| 8 | `reducer_strategy.py` → одна точка решения | `summarizer.py`, `reducer.py` (facade) |
| 9 | `reducer.py`/`reducer_impl.py` → `HierarchicalReducer` | `summarizer.py`, `reducer*.py` |
| 10 | `_hierarchical_reduce_rounds` → `HierarchicalReducer.reduce_chunks_hierarchical` | `summarizer.py` |
| 11 | Удалить `llm_section_trim`, `_fit_input` → `deterministic_truncate` | `summarizer.py`, `llm_calls.py` |
| 12 | `token_budget.py` → `TokenEstimator` | `summarizer.py`, `packing*.py`, `token_budget.py` |
| 13 | `packing*.py` → `adjacent_packing.py` + `ExecutionPlan` | `summarizer.py`, `packing*.py` |
| 14 | `fingerprint.py` → `DocumentIdentity` | `summarizer.py`, `fingerprint.py`, `document_cache.py` |
| 15 | `document_cache.py` → `DocumentAnalysis` snapshot | `summarizer.py`, `document_cache.py` |
| 16 | `cached_retrieval.py` → `DocumentAnalysis.retrieve` | `cache_followup.py`, `cached_retrieval.py` |
| 17 | `context_expansion.py` → `structure/context_expansion.py` (canonical) | `cache_followup.py`, `context_expansion.py` |
| 18 | `brief_strategy.py`, `brief_representation.py` → `importance_brief.py` + canonical brief | `summarizer.py`, `brief_strategy.py`, `brief_representation.py` |
| 19 | `document_cleanup.py` → `structure/cleanup.py` | `summarizer.py`, `document_cleanup.py` |
| 20 | `structure/compatibility.py` — удалить | `structure/compatibility.py`, `tests/test_structure_compatibility.py` |
| 21 | Удалить legacy tests | `tests/test_structure_legacy_regression.py` и др. |
| 22 | Удалить facade из `sections.py` (если только legacy) | `structure/sections.py` |
| 23 | `summarizer.py` → только orchestration | `summarizer.py` |
| 24 | CLI verification | n/a (контракт сохранён) |
| 25 | Удалить legacy filenames | см. §5 ниже |
| 26 | Zero-reference audit | repo-wide grep |
| 27 | `tests/test_legal_summarizer_no_legacy.py` | новый файл |
| 28 | Production-path test | новый файл |
| 29 | Cache-path test | новый файл |
| 30 | Execution-path test | новый файл |
| 31–37 | Тесты структуры, ownership, determinism, regression | `tests/` |
| 38 | `pytest -q` final | n/a |
| 39 | lint/typecheck | n/a |

---

## 5. Файлы-кандидаты на удаление (после миграции)

Только после полной миграции consumers эти файлы можно удалить:

- `scripts/fingerprint.py`
- `scripts/document_cache.py`
- `scripts/cached_retrieval.py`
- `scripts/document_cleanup.py`
- `scripts/context_expansion.py`
- `scripts/execution_strategy.py`
- `scripts/packing.py`
- `scripts/packing_impl.py`
- `scripts/packing_models.py`
- `scripts/token_budget.py`
- `scripts/reducer.py`
- `scripts/reducer_impl.py`
- `scripts/reducer_strategy.py`
- `scripts/reducer_models.py`
- `scripts/brief_strategy.py`
- `scripts/brief_representation.py`
- `scripts/cache_followup.py`
- `scripts/provenance_reconstruction.py`
- `scripts/structure/sections.py`
- `scripts/structure/tree.py`
- `scripts/structure/compatibility.py`

`scripts/chunks.py` остаётся как shared low-level model (только `Chunk`,
`ChunkConfig`); `StructureAwareChunker` class удаляется из него
(после перевода всех consumers на `ChunkPlanner`).

`scripts/llm_calls.py::llm_section_trim` удаляется (Этап 11).

---

## 6. Что НЕ трогаем

- **CLI контракт** (`scripts/cli.py`, `scripts/cli_query.py`) — внешний
  контракт v0 (см. baseline §2).
- **Manifest schema** (`scripts/manifest.py`) — стабильная схема.
- **Тесты `test_resume_scenarios.py`, `test_legal_summarizer_pr2.py`,
  и др. с behavior coverage** — поведение не меняем, только
  внутренние вызовы.
- **`scripts/structure/*` canonical модули** — они уже существуют
  (Этапы 1–50 завершены); задача PLAN.md — перевести production
  consumers на них.
- **`scripts/document_stats.py`** — используется legacy, но его
  responsibility (cheap metrics) не дублируется в `structure/`.
  Требует отдельного решения (возможно — перенос в `structure/`).

---

## 7. Критерий готовности Этапа 1 (этот документ)

* [x] Каждый legacy-компонент в §2 классифицирован.
* [x] Production consumers для каждого legacy зафиксированы.
* [x] Canonical replacement указан.
* [x] Файлы-кандидаты на удаление перечислены (§5).
* [x] План удаления по этапам (§4) построен.

Следующий этап — **Этап 2: исправить canonical hierarchy
(`structure/hierarchy.py`) до nested** (PLAN.md §6).