# legal_summarizer — Аудит кода (Этап 1)

Карта модулей `workspace/skills/legal_summarizer/` по плану §7:
«что отвечает за parsing / structure / chunking / packing / reduce / retrieval».
Фиксируется как часть baseline-серии.

Дата: 2026-09-04.
Связанные документы: `docs/legal_summarizer_baseline.md` (Этап 0), `/PLAN.md`.

---

## 1. Ответственности модулей (текущее состояние)

### 1.1. Parsing / Physical extraction

| Модуль | Строк | Что делает |
|---|---:|---|
| `scripts/structure/physical.py` | 509 | `PhysicalDocument` + `load_physical_document(path)`. Adapter над `workspace.utils.office_files`. **Сейчас PDF разбирается минимум 2 раза**: `pypdf.PdfReader` для текста и `pdfplumber.open` для таблиц. Также DOCX title читается третьим проходом через `docx.Document` внутри `_pick_title_from_text` (строки 158–167). |
| `scripts/manifest.py::manifest_root` | 340 | Корень кэшей (`physical/` и т.п.). |

**Проблемы**:
- (P1) PDF: 2 прохода (text + tables) без необходимости — см. PLAN §4, §10.
- (P2) DOCX title читается из `docx.Document` отдельно — третий низкоуровневый доступ.
- (P3) DOCX page mapping сейчас **отключён** (fake pagination удалён, см. комментарий `physical.py:398-402`). `page_index = None` для DOCX → chunks теряют page-информацию.

### 1.2. Document Identity / Fingerprint

| Модуль | Строк | Что делает |
|---|---:|---|
| `scripts/fingerprint.py` | 107 | `compute_fingerprint(path)` — sha256 от `(path, size, mtime)`. |
| `scripts/structure/physical.py::_physical_cache_key` | — | Параллельная реализация: тоже sha256 от `(resolved, size, mtime)`. |

**Проблемы**:
- (F1) **Дублирование**: `fingerprint.py` и `physical.py::_physical_cache_key` реализуют одну и ту же логику независимо.
- (F2) Fingerprint не передаётся как объект — каждый компонент (`document_cache`, `manifest`, `retrieval`) пересчитывает по-своему. PLAN §5, §77.

### 1.3. Structure detection

| Модуль | Строк | Что делает |
|---|---:|---|
| `scripts/structure/heading.py` | 513 | `HeadingCandidate`, `detect_heading_candidates`, evidence scoring, `_extract_pdf_outline` (с `block_index=-1` — см. ниже). |
| `scripts/structure/list_detection.py` | 201 | `detect_list_runs`, `list_penalty_for_candidate`. **Уже отдельно** от heading detection (хорошо). |
| `scripts/structure/sections.py` | 222 | Facade: `detect_sections`, `merge_short_sections`, `count_meaningful_sections`, `extract_local_structure_label`. |
| `scripts/structure/tree.py` | 231 | `DocumentSection`, `SectionTree`, `build_section_tree`. **Проблема: sibling numbering через глобальный `path_counter_by_level`** (PLAN §13, §19). |

**Проблемы**:
- (S1) **PDF outline bug**: `heading.py:140` ставит `block_index=-1`, потом `tree.py:94` фильтрует `c.block_index >= 0` — outline **не участвует в дереве**. PLAN §11.
- (S2) **Sibling numbering глобальный**: `tree.py:101` объявляет `path_counter_by_level: dict[int, int]`, инкремент в строке 132 — без сброса при заходе в нового parent. Соседи под родителем 1 получают `1.1, 1.2, 1.3`, а соседи под родителем 2 — `1.4, 1.5` вместо `2.1, 2.2`. PLAN §13.
- (S3) **Numbering detection** жёстко зашита в regex'ы `_RE_NUMBERED_LEVEL_*` внутри `heading.py:36-38` — нет модуля `structure/numbering.py`, нет `_numbering_consistency_with_neighbors` поддержки nested numbering (используется только первый numeric component, `heading.py:367-379`). PLAN §6, §12.
- (S4) **DOCX Heading styles** ловятся только если `_is_docx_heading_style` (prefix `heading `/`heading_`/`заголовок`) — нет поддержки `Title`, `Subtitle`, нестандартных имён. PLAN §8.
- (S5) **`merge_short_sections`** сливается с соседями того же level — это **не safety net** вокруг нормального heading detector, а основной механизм исправления плохих микро-секций (PLAN §17). При хорошем heading detection он не нужен.
- (S6) **`heading.py::_extract_pdf_outline`** не возвращает level — он ставит `level=level` из walk-depth outline, но **не валидирует** destination → page → block (PLAN §11, валидации нет).
- (S7) **Нет hierarchy builder как отдельного компонента** — иерархия собирается прямо в `build_section_tree` с неявным приоритетом (PLAN §12).
- (S8) **Нет title/preamble distinction** — title берётся из `_pick_title_from_text` (первая непустая строка), нет `DocumentTitle(value, source, confidence)` модели (PLAN §14).

### 1.4. Chunking

| Модуль | Строк | Что делает |
|---|---:|---|
| `scripts/structure/chunks.py` | 555 | `Chunk`, `ChunkConfig`, `StructureAwareChunker`, `_split_block_with_offsets`. **Сейчас SectionTree передаётся, и chunker смотрит `tree.block_to_section`** — но если SectionTree пустой (heading detection ничего не нашёл), chunker всё равно использует только root и работает корректно. Проблема: **chunker всё-таки независимо считает section_id из `block_to_section`** (т.е. реконструирует section info из tree), хотя это не «повторное определение структуры» в строгом смысле. Это OK для текущего уровня, но Этап 18 должен формализовать «ChunkPlanner не переопределяет structure». |

**Проблемы**:
- (C1) Нет единого `ChunkPlanner` API; есть только `StructureAwareChunker.chunk`. PLAN §19.
- (C2) `chunk_overlap=0` — дефолт skill'а, но нет `tokens_per_chunk` политики (PLAN §20).

### 1.5. Packing

| Модуль | Строк | Что делает |
|---|---:|---|
| `scripts/packing.py` | 50 | Public API: `ContextBatch`, `TokenBudget`, `pack_chunks`. |
| `scripts/packing_impl.py` | 213 | Реализация greedy section-locality packing. |
| `scripts/packing_models.py` | 84 | Модели. |
| `scripts/token_budget.py` | 139 | Token budget. |

**Проблемы**:
- (K1) **Strict section-locality** без adjacent sections — для больших документов даёт `map_calls == chunks_total` (PLAN §22, см. F3 в baseline).
- (K2) **Packing пересчитывается** в inspect-path и run-path — нет единого `ExecutionPlan` (PLAN §21).
- (K3) **Дублирование token estimation**: `token_budget.py` отдельно, `chunks.py::ChunkConfig.chars_per_token=3.5`, `document_stats.py` отдельно. PLAN §20.

### 1.6. Execution strategy

| Модуль | Строк | Что делает |
|---|---:|---|
| `scripts/execution_strategy.py` | 81 | `ExecutionStrategy`, `StrategyConfig`, `select_execution_strategy`. |
| `scripts/reducer_strategy.py` | 103 | `should_use_hierarchical_reduce`, `select_reduce_strategy`. |
| `scripts/reducer.py` | 59 | Public API reducer. |
| `scripts/reducer_impl.py` | 284 | `_reduce_flat`, `_reduce_hierarchical`. |
| `scripts/reducer_models.py` | 94 | `ReduceConfig`, `ReduceStats`, `LLMRunner`. |
| `scripts/summarizer.py::_hierarchical_reduce_rounds` | ~80 | **Вторая реализация hierarchical reduce** внутри summarizer.py (строки 144–196 по baseline-чтению). PLAN §24, §30. |
| `scripts/summarizer.py::_llm_document_reduce` | — | LLM-runner, специфичный для document-level. |

**Проблемы**:
- (E1) **Дублирование hierarchical reduce**: `reducer_impl._reduce_hierarchical` + `summarizer._hierarchical_reduce_rounds`. Они делают разные вещи (одна — секция→документ, другая — rounds of groups) — но **похожи по API**, что затрудняет единый SoT.
- (E2) **`should_use_hierarchical_reduce`** + **`select_reduce_strategy`** — два разных критерия в разных местах (legacy criterion vs token-budget first). PLAN §25.
- (E3) **`LLM-trim` в `reducer_impl._reduce_hierarchical`** (строки 232–241 по baseline-чтению): `llm_runner(..., trim=True, ...)` — это **обычный этап pipeline**, а не safety net. PLAN §26.
- (E4) **`head + tail` в `summarizer._fit_input`** — нормальный reduce output budget overflow handling, без предупреждения о потере секций. PLAN §27.

### 1.7. Retrieval / Cached Retrieval

| Модуль | Строк | Что делает |
|---|---:|---|
| `scripts/cached_retrieval.py` | 208 | `select_relevant_chunks(question, chunks, max_k=8)` — substring/key-word match. |
| `scripts/cache_followup.py` | 217 | Follow-up cache lookup. |
| `scripts/context_expansion.py` | 275 | Расширение контекста выбранного chunk'а. |
| `scripts/document_cache.py` | 200 | Document cache (chunk-level summary cache). |
| `scripts/provenance_reconstruction.py` | 123 | Восстановление provenance для follow-up tool. |

**Проблемы**:
- (R1) `select_relevant_chunks` — substring match без ranking, без нормализации. PLAN §33, §35.
- (R2) **Full-document fallback** прямо сейчас в `cached_retrieval` при keyword-miss (PLAN §38 — сделать последним шагом).
- (R3) **Нет retrieval cascade** (PLAN §41) — только substring + fallback.
- (R4) **`context_expansion.py`**: использует `doc.blocks.index(target)` — линейный lookup (PLAN §44).

### 1.8. Document cleanup (headers/footers)

| Модуль | Строк | Что делает |
|---|---:|---|
| `scripts/document_cleanup.py` | 191 | `CleanupConfig`, `cleanup_blocks` — маркирует `is_repeated`, `repeated_role`, `repeated_count`. |

**Проблемы**:
- (U1) Маркировка есть, но **сами данные downstream не используются** (per baseline warning в `document_cleanup.py:16` SyntaxWarning на `\s`; см. PLAN §42).
- (U2) Не исключает high-confidence repeated headers из semantic map (PLAN §42, вариант A).

### 1.9. Document stats / LLM / Manifest / Prompts / Output

| Модуль | Строк | Что делает |
|---|---:|---|
| `scripts/document_stats.py` | 127 | `compute_document_stats`, `DocumentStats`. |
| `scripts/llm.py` | 46 | LLM-client wrapper. |
| `scripts/llm_calls.py` | 148 | Учёт LLM-вызов. |
| `scripts/manifest.py` | 340 | Manifest v2 + chunk_states + completion. |
| `scripts/prompts.py` | 211 | Промпты + JSON-парсер. |
| `scripts/prompts_runtime.py` | 68 | Runtime prompt loader. |
| `scripts/output.py` | 161 | Output formatting. |
| `scripts/pipeline.py` | 169 | Async pipeline runner (`process_context_batch`, `run_one_batch_async`, `load_cached_partials`). |
| `scripts/brief_strategy.py` | 160 | Brief selection (round-robin coverage, см. F5 baseline). |
| `scripts/brief_representation.py` | 180 | Brief formatting. |
| `scripts/provenance_reconstruction.py` | 123 | Provenance rebuilder для tool. |
| `scripts/sanitize.py` | 85 | Sanitize, `extract_subject`. |
| `scripts/skill_config.py` | 62 | Конфиг. |
| `scripts/cli.py` | 405 | CLI entry-point. |
| `scripts/cli_query.py` | 251 | Follow-up tool. |

**Проблемы**:
- (B1) `brief_strategy.py` использует **coverage ratio = 0.2** при тесте на 0.5 (F5 baseline) — не **importance-aware** (PLAN §31, §32, §66).

---

## 2. Карта дублирований и legacy paths

| Дубль / legacy | Где 1 | Где 2 | Что делать |
|---|---|---|---|
| Fingerprint | `scripts/fingerprint.py` | `scripts/structure/physical.py::_physical_cache_key` | Унифицировать через `DocumentIdentity` (Этап 5) |
| Hierarchical reduce | `scripts/reducer_impl.py::_reduce_hierarchical` | `scripts/summarizer.py::_hierarchical_reduce_rounds` | Один `HierarchicalReducer` (Этап 24) |
| Numbering regex | `scripts/structure/heading.py::_RE_NUMBERED_LEVEL_*` | `scripts/structure/list_detection.py::_RE_NUMBERED_LEVEL_*` | Вынести в `scripts/structure/numbering.py` (Этап 6) |
| Token estimation | `chunks.py::ChunkConfig.chars_per_token` | `token_budget.py` | Один `TokenEstimator` (Этап 20) |
| Hierarchical criterion | `reducer_strategy.py::should_use_hierarchical_reduce` | `select_reduce_strategy` | Один execution policy (Этап 25) |
| `_fit_input` (head+tail) | `summarizer.py::_fit_input` | — | Оставить только как emergency fallback (Этап 27) |
| `cleanup_blocks` маркировка без use | `document_cleanup.py` | — | Подключить к chunking (Этап 42) |
| `select_relevant_chunks` substring | `cached_retrieval.py` | — | Retrieval cascade + ranking (Этап 33–35) |
| `index(target)` lookup | `context_expansion.py` | — | `blocks_by_ord` уже есть в `PhysicalDocument` (Этап 44) |

---

## 3. Сводка для следующих этапов

| Этап PLAN | Что реализуем | Точки модификации |
|---|---|---|
| 2 | `DocumentStructure` контракт | новый `scripts/structure/models.py` |
| 3 | Physical vs Semantic split | типизация в `physical.py`, новый `DocumentStructure` в `models.py` |
| 4 | `DocumentLoader` (one-pass) | новый `scripts/structure/document_loader.py`; PDF: единый проход |
| 5 | `DocumentIdentity` | новый `scripts/structure/identity.py`; заменить оба fingerprint-расчёта |
| 6 | Numbering parser | новый `scripts/structure/numbering.py`; заменить regex'ы |
| 7 | Heading candidate detector | уже есть (`heading.py`); рефакторинг naming, не алгоритма |
| 8 | Evidence sources | расширение `HeadingEvidence` + `DOCX Title/Subtitle` |
| 9 | Объединение кандидатов | новый `scripts/structure/candidate_aggregator.py` |
| 10 | Heading vs list item | новый `scripts/structure/list_detection.py` уже есть; расширение |

---

## 4. Что НЕ трогаем на этих этапах (по PLAN §1)

* CLI (`scripts/cli.py`, `scripts/cli_query.py`) — внешний контракт.
* `scripts/summarizer.py` — **не переписываем**. Только инкрементальные правки в местах, где новый код предоставляет более чистый API.
* `scripts/manifest.py` — manifest schema стабильна.
* Существующие тесты — поведение не меняем (только добавляем новые).

---

## 5. Критерий готовности Этапа 1

* [x] Карта parsing / structure / chunking / packing / reduce / retrieval составлена.
* [x] Список дублирований (9 пунктов) зафиксирован.
* [x] Для каждого этапа 2–10 определены файлы-модификации.
* [x] Legacy paths помечены явно.

Следующий этап — **Этап 2: контракт `DocumentStructure`**.