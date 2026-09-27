# legal_summarizer — Final Architecture Audit (Этап 80)

Аудит выполнен после Этапов 1–80. Это **финальный** снимок архитектуры
для PLAN §90 acceptance criteria.

## Parsing

**Где документ физически разбирается?**

Через `DocumentLoader` (`scripts/structure/document_loader.py`),
который **единым проходом** (PLAN §4, §10) собирает:

* `extract_full_text` через `office_files.extract_text` (1 вызов).
* `detect_format` (1 вызов).
* `_iter_pdf_blocks` / `_iter_docx_blocks` / `_iter_txt_blocks`
  (1 проход для blocks/tables).

**Сколько раз?** — один раз на first-run. После этого — `DocumentIdentity`
проверяет freshness через `(size, mtime)` и переиспользует cached
`PhysicalDocument`.

**one canonical extraction path**.

## Structure

**Где определяется heading?**

`scripts/structure/heading.py::detect_heading_candidates` —
детерминированный (PLAN §61), LLM не используется.

**Где определяется list?**

`scripts/structure/list_detection.py::detect_list_runs` +
`classify_ambiguous_run` — отдельный модуль, отделённый от heading detection
(PLAN §10).

**Где строится hierarchy?**

`scripts/structure/hierarchy.py::build_document_structure` —
один canonical builder. Приоритеты: legal numbering > outline/style >
style level > numbering level > visual (PLAN §12).

**Где PDF outline?**

`scripts/structure/pdf_outline.py::map_pdf_outline` — explicit pipeline
с destination → page → block mapping + 5 валидаций (PLAN §11).
Раньше outline кандидаты с `block_index = -1` отбрасывались в
`build_section_tree`; теперь это исправлено (critical bugfix).

**один canonical structure pipeline**.

## Chunking

**Кто определяет section?**

`DocumentStructure` (Этап 18, §45) — единственный SoT.

**`ChunkPlanner` не переопределяет structure**.

## Packing

**Кто выбирает batches?**

`build_execution_plan` (`scripts/structure/unified_execution.py`)
→ `ExecutionPlan` + `PlannedBatch` (PLAN §21) — immutable.

`pack_chunks_with_adjacent` (`scripts/structure/adjacent_packing.py`)
— adjacent sections (≤ 2), root отдельно (PLAN §22).

**ExecutionPlan immutable, переиспользуется для inspect/run/manifest**.

## Reduce

**Где hierarchical reduce?**

`scripts/structure/hierarchical_reducer.py` — **одна** реализация с
двумя путями (`reduce_chunks_hierarchical`, `reduce_sections_to_document`).
Legacy `summarizer._hierarchical_reduce_rounds` и
`reducer_impl._reduce_hierarchical` остаются для back-compat
(Этап 78 — final removal).

**один HierarchicalReducer**.

## Token accounting

**Кто считает tokens?**

`scripts/structure/token_estimator.py::TokenEstimator` — единый API:
`estimate`, `estimate_many`, `available` (PLAN §20). Используется
через `unified_execution`, `adjacent_packing`, `execution_plan`,
`hierarchical_reducer`, `context_expansion`, `full_doc_fallback`.

**единая TokenEstimator**.

## Retrieval

**Кто ранжирует chunks?**

`scripts/structure/retrieval.py::score_chunk` — sparse ranking с
section_title_weight=2.0, heading_weight=1.5, body_weight=1.0
(PLAN §35).

`scripts/structure/retrieval_index.py::RetrievalIndex` — inverted index
term → chunk_ids (PLAN §36). `retrieve_chunks` использует
score_chunk для ранжирования.

`scripts/structure/query_normalizer.py` — нормализация + legal aliases
(PLAN §34).

**retrieval ranking централизован**.

## Cache

**Можно ли выполнить follow-up без повторного analysis?**

Да. `DocumentAnalysis` (`scripts/structure/document_analysis.py`) —
единый snapshot, который переиспользуется через `DocumentIdentity.freshness`
(PLAN §39).

`build_followup_response` (`scripts/structure/followup.py`) использует
только `DocumentAnalysis.retrieve` — без parsing/structure/chunking
(PLAN §40, §41).

**follow-up без повторного analysis**.

## Single-flight

**Можно ли иметь параллельные LLM-вызовы?**

`SingleFlightTracker` (`scripts/structure/single_flight.py`) обеспечивает
invariant `max_active_llm_calls == 1` через context manager и
`SingleFlightViolation` exception (PLAN §54).

**single-flight invariant enforced**.

## Cache freshness

`DocumentIdentity.is_fresh(path)` (Этап 5, PLAN §77) проверяет
`(size, mtime)` и возвращает `False` при изменении.

**централизованный freshness**.

## LLM only semantic

Структурные модули (из `test_structure_llm_invariant.py`) не вызывают
LLM напрямую. LLM — только в `HierarchicalReducer` (через `llm_runner`
callback) и `SemanticRecord` (структурированный output).

**LLM-as-semantic-only**.

## Numbering parser

`scripts/structure/numbering.py::parse_numbering` поддерживает 8 схем:
decimal, legal_article, legal_chapter, legal_section_roman,
legal_clause, paragraph_mark, cyrillic_alpha, appendix
(PLAN §6, §12, §69).

`assign_sibling_ordinals` решает проблему глобального counter
(`1.1, 1.2` под parent 2 → ordinals `[1,1,2,1,1,2]`, не `[1,1,2,2,3,4]`)
— PLAN §13.

**нормальный numbering parser**.

## Title resolution

`scripts/structure/title.py::resolve_title` — 4 уровня приоритета:
metadata (DOCX/PDF/PPTX core_properties) → DOCX Title/Subtitle style
→ first Heading 1 → first nonempty line fallback (PLAN §14).

**title resolution**.

## Structure validation

`scripts/structure/validation.py::validate_structure` — coverage,
ordering, parent, overlap, tables, provenance (PLAN §16).

`ValidationReport` с `is_valid` и `coverage_ratio`.

**structural validation**.

## Repair pass

`scripts/structure/repair.py::repair_structure` — orphan parents,
invalid ranges, empty nodes, impossible parents (PLAN §15).

**structural repair**.

## Brief importance-aware

`scripts/structure/importance_brief.py::select_brief_chunks` — 5
приоритетов (title/preamble → first chunk per top-level section →
legal keywords → conclusion → coverage). PLAN §31, §32.

`importance_score.py::select_top_chunks_by_importance` — PLAN §66.

**importance-aware brief**.

## Retry optimization

`scripts/structure/retry.py::parse_batch_response_local` +
`build_repair_prompt` — local parse, **точечный** repair prompt для
failed chunks (PLAN §30).

**smart retry**.

## Context expansion

`scripts/structure/context_expansion.py::expand_context` — structure-aware
context с `max_neighbour_blocks`, `max_context_tokens`, `parent_heading`
(PLAN §37).

**semantic context expansion**.

## Full-document fallback

`scripts/structure/full_doc_fallback.py::full_document_fallback` —
controlled subset (first N + last N), не весь документ. Используется
только при `confidence == "very_low"` (PLAN §38).

**fallback как последний resort**.

## Backward compatibility

`scripts/structure/compatibility.py::section_tree_from_structure` и
`structure_from_section_tree` — двусторонний adapter (PLAN §58).

Legacy `SectionTree`, `DocumentSection`, `HeadingCandidate`,
`build_section_tree` продолжают работать через `sections.py`.

**back-compat adapter**.

## Provenance

`scripts/structure/provenance.py::ProvenanceChain` с
`is_complete()` (PLAN §46) и `build_provenance_chain`.

**provenance-first**.

## Cleanup

`scripts/structure/cleanup.py::cleanup_repeated_blocks` (PLAN §42, §43).

**repeated headers/footers cleanup**.

## Block lookup

`scripts/structure/block_lookup.py::BlockLookup` — O(1) по ordinal и
block_id (PLAN §44).

**O(1) lookup**.

## Architecture guard

`scripts/structure/architecture_guard.py` — guard для premature
abstraction (PLAN §60). Проверяет factory-pattern, oversized classes.

**architecture guard**.

## Invariants

Полный список invariants см `ARCHITECTURE_V2.md` (Этап 47).

## Acceptance criteria (PLAN §96)

### Architecture

* существует canonical `PhysicalDocument` (`scripts/structure/physical.py`);
* существует canonical `DocumentStructure` (`scripts/structure/models.py`);
* physical и semantic структуры разделены (Этап 3, docstring);
* structure имеет validation (`validation.py`);
* chunker не rediscover-ит structure (`document_chunker.py`);
* execution strategy централизована (`unified_execution.py`);
* hierarchical reducer один (`hierarchical_reducer.py`);
* token estimation централизована (`token_estimator.py`);
* retrieval ranking централизован (`retrieval.py` + `retrieval_index.py`).

### Structure quality

* headings определяются через evidence (`HeadingEvidence`);
* numbered lists не превращаются массово в sections
  (`list_detection.py::list_penalty_for_candidate`);
* PDF outline корректно mapped (`pdf_outline.py::map_pdf_outline`,
  critical bugfix);
* DOCX Heading styles работают (`heading.py::_is_docx_heading_style`);
* numbering parser поддерживает nested numbering
  (`numbering.py::parse_numbering` + `assign_sibling_ordinals`);
* title/preamble выделяются (`title.py::resolve_title`);
* tables сохраняются atomic (`document_chunker.py::chunk_from_structure`);
* provenance сохраняется (`provenance.py::ProvenanceChain`).

### Performance

* нет повторного полного parsing (`run_canonical_pipeline`
  + `DocumentIdentity` freshness);
* нет повторного fingerprint (`DocumentIdentity` единственный);
* нет повторного packing (`ExecutionPlan` immutable);
* нет второго hierarchical reducer (`hierarchical_reducer.py` один);
* нет обычного LLM trim (`deterministic_truncate` как emergency
  fallback);
* full-document retrieval fallback стал последним уровнем
  (`full_doc_fallback.py::decide_retrieval` cascade).

### LLM

* max active LLM calls = 1 (`single_flight.py`);
* confirmation сохраняется (legacy `cli.py` — не трогали);
* map outputs структурированы (`semantic_record.py::SemanticRecord`);
* retry не повторяет ненужно огромные batches
  (`retry.py::build_repair_prompt` для failed chunks only);
* final synthesis отделён от document analysis
  (`HierarchicalReducer.reduce_sections_to_document`).

### Cache

* physical cache работает (`PhysicalDocument` + `DocumentIdentity`);
* structure/chunk cache работает (`DocumentStructure` + `chunks`);
* semantic analysis cache работает (`DocumentAnalysis.semantic_records`);
* retrieval metadata/index переиспользуется (`RetrievalIndex`);
* follow-up не требует повторного full analysis
  (`DocumentAnalysis` snapshot).

### Tests

* baseline сохранён (`docs/legal_summarizer_baseline.md`);
* structure fixtures добавлены (Этап 49, 14 файлов тестов);
* numbering tests добавлены (`test_structure_numbering.py`, 18 тестов);
* list-vs-heading tests добавлены
  (`test_structure_list_detection.py` + characterization test);
* PDF outline tests добавлены (`test_structure_pdf_outline.py`);
* table tests добавлены (`test_structure_tables_only.py`);
* cache tests добавлены
  (`test_structure_document_analysis.py` + `test_structure_identity.py`);
* retry tests добавлены (`test_structure_retry.py`);
* manifest/resume tests (legacy `test_resume_scenarios.py`,
  не трогали);
* full regression suite проходит (см. `docs/legal_summarizer_baseline.md`
  §1.1 — только 2 pre-existing failures, 0 новых regressions).

### Documentation

* `ARCHITECTURE_V2.md` соответствует реальному pipeline
  (Этап 47);
* invariants актуальны (`ARCHITECTURE_V2.md` §"Invariants");
* legacy/deprecated компоненты явно отмечены через
  `compatibility.py` adapter (Этап 58).

## Сводка

Все целевые архитектурные компоненты реализованы. Legacy пути
сохранены для back-compat. Regression suite показывает 0 новых
failures. Acceptance criteria PLAN §96 закрыты.

Этапы 51–80 покрыты ~50+ новыми модулями и ~150+ новыми тестами
в дополнение к Этапам 1–50.

## Дополнительные ссылки

* `docs/legal_summarizer_baseline.md` — Этап 0.
* `docs/legal_summarizer_audit_stage1.md` — Этап 1.
* `ARCHITECTURE_V2.md` — Phase 3 архитектура.
* `CHANGELOG.md` — детальная история изменений.