# §2 — Инвентаризация `summarizer.py` и таблица legacy → canonical

Файл: `workspace/skills/legal_summarizer/scripts/summarizer.py` (1746 строк).
Размер — большой, потому что это orchestration layer поверх legacy-pipeline.

## Baseline audit (legacy references)

```
Production legacy references: 73
Test-only legacy references:   5
```

Основные точки legacy в `summarizer.py`:

| Где | Что |
|---|---|
| L59–63 | `from .packing import ContextBatch, TokenBudget, pack_chunks` |
| L69–72 | `from .document_stats import DocumentStats, compute_document_stats` |
| L73–77 | `from .structure.chunks import Chunk, ChunkConfig, StructureAwareChunker` |
| L83–86 | `from .document_cleanup import CleanupConfig, cleanup_blocks` |
| L87–95 | `from .structure.sections import (ROOT_SECTION_ID, DocumentSection, SectionTree, count_meaningful_sections, detect_sections, extract_local_structure_label, merge_short_sections)` |
| L202–206 | `from .document_cache import doc_cache_dir, load_doc_cache, save_doc_cache` |
| L207–210 | `from .fingerprint import resolve_document_id, resolve_session_key` |
| L211–213 | `from .token_budget import count_tokens` |
| L443 | `tree: SectionTree | None` (dataclass field) |
| L499, L792 | `load_physical_document(...)` (legacy signature, без `workspace_root` для второго?) |
| L527, L589 | `cleanup_blocks(...)`, `StructureAwareChunker().chunk(...)` |
| L537–546 | `detect_sections(...)`, `merge_short_sections(...)` |
| L589–593 | chunker + `pack_chunks(chunks, budget)` |
| L759–617 | весь `inspect()` — legacy path |
| L773–774 | `_resolve_document_id(...)`, `_resolve_session_key(...)` (fingerprint) |
| L1361–1369 | построение legacy `DocumentStats(...)` для reduce-strategy |
| L1489–1491 | canonical-only: `HierarchicalReducer` (это OK — single canonical reducer) |

## Группа A — оставить (orchestration API)

Внешний контракт `summarizer.py`, который **нельзя** менять без явной просьбы:

- `run(...)` — главный entry point (lines 736–1708)
- `inspect(...)` — inspection (lines 459–617)
- `Inspection` (lines 438–446)
- `Estimate` (lines 620–627)
- `estimate(insp)` (lines 630–642)
- `needs_confirmation(est)` (lines 645–646)
- `quick_estimate(path)` (lines 659–733)
- `summarize(...)` — legacy shim (lines 1716–1746) — deprecated, удалить
- `load_text(...)` (lines 219–247) — supported extensions + brief-mode helper
- `load_structure(...)` — старый legacy Phase-2 entry (lines 275–304) — удалить
- `make_operation_id(...)` (lines 307–312) — op_id helper, оставить
- `_fit_input(text, budget)` (lines 168–189) — truncation helper, нужен canonical pipeline
- `_chunk_structure_label`, `_format_chunk_block` (lines 102–121) — labels, можно сохранить
- `_iter_text_blocks`, `_make_text_block` (lines 385–403) — PhysicalDocument helpers, уйдут в canonical
- `_compute_chunk_size_chars`, `_build_token_budget`, `_make_chunk_config` — связаны с legacy `TokenBudget`, уйдут
- `_resolve_max_chunks()` (lines 192–199) — конфиг helper, оставить

## Группа B — заменить на canonical API

| Legacy (summarizer.py) | Canonical replacement |
|---|---|
| `pack_chunks` (L59, L592, L969) | `pack_chunks_with_adjacent` (structure.adjacent_packing) |
| `TokenBudget` dataclass (L59, L375) | `TokenEstimator` + `TokenEstimatorConfig` (structure.token_estimator) |
| `ContextBatch` (L59, L1208) | `ExecutionPlan.batches` (structure.execution_plan) |
| `StructureAwareChunker` (L589–590) | `ChunkPlanner` через `run_canonical_pipeline` |
| `Chunk`, `ChunkConfig` (L73) | canonical `Chunk` + `ChunkPlanner` (нужно разделить legacy Chunk на структурах/chunks.py и canonical) |
| `compute_document_stats` + `DocumentStats` (L69, L548) | `DocumentAnalysis` (structure.document_analysis) — `stats` |
| `detect_sections` + `merge_short_sections` (L87, L537, L542) | `run_canonical_pipeline` → `analysis.structure` (`DocumentStructure`) |
| `SectionTree`, `DocumentSection`, `ROOT_SECTION_ID` (L87, L443, L1090) | `DocumentStructure`, `StructureNode` (structure.hierarchy) |
| `count_meaningful_sections` (L604, L1644) | `DocumentAnalysis` / `ExecutionPlan` stats |
| `extract_local_structure_label` (L113) | chunk metadata из canonical pipeline |
| `cleanup_blocks` + `CleanupConfig` (L83, L527) | canonical cleanup (`structure.cleanup`) |
| `count_tokens` (L211) | `TokenEstimator` |
| `resolve_document_id`, `resolve_session_key` (L207) | `DocumentIdentity` (structure.identity) |
| `load_doc_cache`/`save_doc_cache` (L202) | `DocumentAnalysis` snapshot + persistence layer |
| `load_physical_document` (L499, L792) | `DocumentLoader` (structure.document_loader) — single canonical path |
| `execution_strategy_for_legacy` adapter (L556, L1373) | `unified_execution.select_strategy` (single source) |
| `reduce_strategy_for_legacy` adapter (L596, L1371) | `unified_execution.select_strategy` / `select_strategy` |

## Группа C — удалить

| Legacy | Почему |
|---|---|
| `summarize(...)` (L1716–1746) | Legacy Phase-2 entry, deprecated уже давно, никем не используется |
| `load_structure(...)` (L275–304) | Phase-2 совместимость, `structure.title` идёт из canonical |
| `_relaxed_lexical_fallback` (L406–435) | Legacy fallback в `run()`, нужен только если question не нашёл через cache; после миграции canonical retrieval избыточен |
| `_build_token_budget` (L355–382) | использует legacy `TokenBudget`, не нужен после перехода на `TokenEstimator` |
| `_compute_chunk_size_chars` (L336–352) | оставлено для legacy chunk config — после перехода не нужно |
| `_make_chunk_config` (L448–456) | legacy `ChunkConfig` |
| `from .document_stats import ...` (L69–72) | legacy `DocumentStats` уйдёт (§15) |
| `from .document_cache import ...` (L202–206) | legacy `document_cache.py` (§29) |
| `from .fingerprint import ...` (L207–210) | legacy `fingerprint.py` (§11) |
| `from .token_budget import ...` (L211–213) | legacy `token_budget.py` (§19) |
| `from .document_cleanup import ...` (L83–86) | legacy `document_cleanup.py` (§17) |
| `from .structure.sections import ...` (L87–95) | `sections.py` (§22) |
| `from .structure.chunks import StructureAwareChunker` | `StructureAwareChunker` (§21) |
| `from .packing import ...` (L59–63) | legacy `packing.py` + `_impl.py` + `_models.py` (§20) |
| `_legacy adapters` в `summarizer_canonical.py` (L234–286) | `execution_strategy_for_legacy` + `reduce_strategy_for_legacy` (§14) |

## Таблица legacy → canonical (по всему skill)

| Legacy | Canonical replacement |
|---|---|
| `StructureAwareChunker` | `ChunkPlanner` (в `run_canonical_pipeline`) |
| `SectionTree` / `DocumentSection` / `sections.py` / `tree.py` | `DocumentStructure` / `StructureNode` (structure.hierarchy) |
| `packing.py` / `packing_impl.py` / `packing_models.py` / `pack_chunks` | `adjacent_packing.pack_chunks_with_adjacent` + `ExecutionPlan` |
| `token_budget.py` / `count_tokens` / `TokenBudget` | `structure/token_estimator.TokenEstimator` |
| `fingerprint.py` / `resolve_document_id` / `resolve_session_key` | `structure/identity.DocumentIdentity` |
| `document_cleanup.py` / `cleanup_blocks` | `structure/cleanup.py` |
| `reducer.py` / `reducer_impl.py` / `reducer_models.py` / `reducer_strategy.py` | `structure/hierarchical_reducer.HierarchicalReducer` |
| старый execution selector (`select_execution_strategy`) | `structure/unified_execution.select_strategy` |
| `document_cache.py` / `load_doc_cache` / `save_doc_cache` | `DocumentAnalysis` snapshot (structure.document_analysis) |
| `document_stats.py` / `compute_document_stats` / `DocumentStats` | `DocumentAnalysis` + `ExecutionPlan` метрики |
| `brief_representation.py` / `brief_strategy.py` | `structure/brief_budget.py` (новое, §16) + canonical `select_brief_chunks` |
| `execution_strategy.py` / `reducer_strategy.py` / `select_*_strategy` | `unified_execution.select_strategy` (single source) |

Все replacement'ы уже существуют в canonical коде — задача только в переносе
summarizer.py на них и удалении legacy файлов.