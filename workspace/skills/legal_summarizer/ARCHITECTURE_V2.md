# Architecture — `legal_summarizer` Phase 3 (PLAN §47)

Это **дополнение** к `ARCHITECTURE.md` (Phase 2B invariants) для нового
pipeline, описанного в `/PLAN.md` (Этапы 1–80).

Существующий `ARCHITECTURE.md` остаётся source of truth для Phase 2B
invariants. Этот документ фиксирует Phase 3 pipeline.

## Pipeline

```
FILE
  ↓
DocumentLoader (single-pass)
  ↓
DocumentIdentity (fingerprint + cache_key)
  ↓
PhysicalDocument (canonical physical model)
  ↓
Heading detection → build_document_structure
  ↓
DocumentStructure (canonical semantic structure)
  ↓
repair_structure (orphans, invalid ranges, etc.)
  ↓
validate_structure (coverage, overlap, total_blocks)
  ↓
ChunkPlanner (chunk_from_structure) — DocumentStructure как SoT
  ↓
DocumentAnalysis (cache architecture)
  ↓
ExecutionPlan (direct / map_flat / map_hierarchical)
  ↓
Unified execution (one LLM at a time — single-flight)
  ↓
HierarchicalReducer (rounds of groups + section-level)
  ↓
Final answer + ProvenanceChain
```

## Новые компоненты (Phase 3)

* `structure/models.py` — `DocumentStructure`, `StructureNode`,
  `StructureEvidence`, `NumberingInfo`, `DocumentTitle`.
* `structure/identity.py` — `DocumentIdentity` (PLAN §5).
* `structure/numbering.py` — `parse_numbering` + `assign_sibling_ordinals`
  (PLAN §6, §13).
* `structure/pdf_outline.py` — `map_pdf_outline` + `mapped_to_heading_candidates`
  (PLAN §11, critical bugfix).
* `structure/candidate_aggregator.py` — `aggregate_by_block`
  (PLAN §9).
* `structure/hierarchy.py` — `build_document_structure`
  (PLAN §12).
* `structure/title.py` — `resolve_title` (PLAN §14).
* `structure/repair.py` — `repair_structure` + `RepairReport`
  (PLAN §15).
* `structure/validation.py` — `validate_structure` + `ValidationReport`
  (PLAN §16).
* `structure/safety_merge.py` — `safety_merge` (PLAN §17).
* `structure/document_chunker.py` — `ChunkPlanner` (PLAN §18, §19).
* `structure/token_estimator.py` — `TokenEstimator` (PLAN §20).
* `structure/execution_plan.py` — `ExecutionPlan` + `PlannedBatch`
  (PLAN §21).
* `structure/adjacent_packing.py` — `pack_chunks_with_adjacent`
  (PLAN §22).
* `structure/unified_execution.py` — `ExecutionPolicy` + `select_strategy`
  + `build_execution_plan` (PLAN §23, §25).
* `structure/hierarchical_reducer.py` — `reduce_chunks_hierarchical` +
  `reduce_sections_to_document` + `deterministic_truncate`
  (PLAN §24, §26, §27).
* `structure/importance_brief.py` — `select_brief_chunks` (PLAN §31, §32).
* `structure/semantic_record.py` — `SemanticRecord` + `Provenance`
  (PLAN §29).
* `structure/retry.py` — `parse_batch_response_local` + `build_repair_prompt`
  (PLAN §30).
* `structure/retrieval.py` — `retrieve_chunks` + `score_chunk` (PLAN §33, §35).
* `structure/query_normalizer.py` — `normalize_query` + `expand_with_aliases`
  (PLAN §34).
* `structure/retrieval_index.py` — `RetrievalIndex` (PLAN §36).
* `structure/context_expansion.py` — `expand_context` (PLAN §37).
* `structure/full_doc_fallback.py` — `full_document_fallback` + `decide_retrieval`
  (PLAN §38).
* `structure/document_analysis.py` — `DocumentAnalysis` (PLAN §39).
* `structure/followup.py` — `build_followup_response` (PLAN §40).
* `structure/cleanup.py` — `cleanup_repeated_blocks` (PLAN §42, §43).
* `structure/block_lookup.py` — `BlockLookup` (PLAN §44).
* `structure/pipeline.py` — `run_canonical_pipeline` (PLAN §45).
* `structure/provenance.py` — `ProvenanceChain` (PLAN §46).

## Invariants (Phase 3)

Phase 3 invariants **дополняют** Phase 2B. Полный список — в
`/PLAN.md §48`.

Ключевые новые invariants:

1. `DocumentStructure` — единственный source of truth для section info
   (ChunkPlanner, Chunking, Retrieval, Brief, Reducer используют его).
2. ChunkPlanner **не** переоткрывает headings.
3. TokenEstimator — единственная оценка токенов (используется везде).
4. ExecutionPlanner — единый селектор strategy.
5. HierarchicalReducer — одна реализация.
6. **max_active_llm_calls == 1** (single-flight).
7. Manifest — execution source of truth.
8. chunk_states — execution source of truth.
9. Cache freshness — детерминированная (через DocumentIdentity).
10. Cached analysis переиспользуется для follow-up.
11. Retrieval — ranked (не first-match).
12. Full-document fallback — последний resort (PLAN §38).

## Backward compatibility

Legacy API (`DocumentSection`, `SectionTree`, `HeadingCandidate`,
`build_section_tree`, `merge_short_sections`) остаётся в `sections.py`
для тестов и старых consumers. Новый canonical путь —
`DocumentStructure` через `run_canonical_pipeline` (`pipeline.py`).

Миграция consumers на новый pipeline — Этап 45 (через `run_canonical_pipeline`)
и Этап 58 (back-compat adapter).

## Acceptance matrix

* `small` doc: 1 LLM-вызов (DIRECT).
* `medium` doc: ≥ 2 LLM-вызовов (MAP_FLAT).
* `large` doc: MAP_HIERARCHICAL.
* Quality (small, mock): ≥ 80% facts.

## Тесты

* 14 новых файлов тестов в `workspace/skills/legal_summarizer/tests/`
  (test_structure_*.py).
* `+cyrillic_literals.py` — обход cp1251/cp866 в shell.
* Все новые тесты: `pytest workspace/skills/legal_summarizer/tests/test_structure_*.py`.

## Связанные документы

* `/PLAN.md` — главный план рефакторинга.
* `docs/legal_summarizer_baseline.md` — Этап 0.
* `docs/legal_summarizer_audit_stage1.md` — Этап 1 (аудит кода).
* `ARCHITECTURE.md` — Phase 2B invariants.