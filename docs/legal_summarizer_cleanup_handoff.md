# Legal Summarizer Cleanup — Handoff

Эта сессия завершила **16/50 этапов PLAN.md**. Документ — handoff для
следующей сессии.

## Текущее состояние

- **HEAD:** `9d97396 refactor(summarizer): brief budget в canonical structure/brief_budget (§16)`
- **Branch:** `master` (clean working tree)
- **Тесты:** **431 passed, 4 skipped, 0 failed** (было 369 → +62)
- **Legacy файлы удалены:** `document_stats.py`, `brief_representation.py`
- **Canonical модули полностью покрыты планом §3-§12**

## Commits этой сессии (в порядке)

```
d501c7c docs(legal_summarizer): baseline для финального cleanup-плана
cdebed7 docs(legal_summarizer): §2 инвентаризация summarizer.py
ad92bf3 test(structure): формальные инвариант-тесты для DocumentStructure hierarchy (§3)
2f9e7f4 fix(structure): валидация разделяет parent-child overlap (valid) от sibling/cross-branch (invalid) (§4)
b964a07 fix(structure): repair — one-block section survives, sync children, deterministic (§5)
199d8cb feat(structure): owner_for_block helper + root fallback для preamble (§6)
c5ded32 refactor(structure): chunk_from_structure группирует blocks в document order (§7)
00573d4 test(structure): уникальность document-level table_id (§8)
e8ce5d6 refactor(structure): adjacent_packing явные правила table/section/budget/order (§9)
3a65d89 fix(structure): context expansion — neighbours по target_idx ± k (§10)
16db209 refactor(structure): PhysicalDocument использует DocumentIdentity для fingerprint (§11)
4cf4b34 refactor(structure): DocumentLoader single-pass loading (§12)
6425703 refactor(summarizer): удалить legacy adapters reduce/execution_strategy_for_legacy (§14)
189724f refactor(summarizer): удалить document_stats.py (§15)
9d97396 refactor(summarizer): brief budget в canonical structure/brief_budget (§16)
```

## Что сделано (детально)

### §3. DocumentStructure hierarchy invariants
- Добавлены 15 формальных инвариант-тестов (`test_structure_hierarchy_invariants.py`):
  parent_id, level, reachability, ranges inside document, byte-for-byte
  determinism (3 прогона), siblings monotonic, root covers full document.
- **Правок кода не потребовалось** — текущая реализация уже nested.

### §4. Validation parent-child/sibling/cross-branch
- `validate_structure` переписан:
  - parent-child overlap → **разрешён** (это часть nested-семантики)
  - siblings → новая issue kind `sibling_overlap`
  - cross-branch → новая issue kind `cross_branch_overlap`
  - добавлены: `cycle`, `duplicate_child`, `range_out_of_bounds`,
    `root_does_not_cover_document`, `root_not_at_start`,
    `non_root_without_parent`
- 9 новых тестов в `test_structure_validation_v4.py`
- 3 старых теста обновлены (root end_block должен соответствовать
  total_blocks-1)

### §5. Repair one-block survives, sync children
- `repair_structure` полностью переписан:
  - one-block section (`start_block == end_block`) **не удаляется** (§5.1)
  - при изменении `parent_id` синхронно пересобирается `children` обоих
    parent'ов через `_rebuild_children` (§5.2)
  - при drop invalid-range node дети привязываются к repaired parent (§5.3)
  - iteration по `sorted(current_nodes.keys())` — детерминированно (§5.4)
  - финальная пересборка children для всех nodes (consistency между
    `parent_id` и `children`-tuple)
- `RepairReport.empty_nodes_collapsed` удалён
- 7 новых тестов: `one_block_section_survives`, `removed_node_absent_from_children`,
  `child_of_repaired_parent_becomes_valid`, `idempotent`,
  `drops_invalid_child_of_dropped_node`, `dropped_node_removed_from_sibling_children`,
  `parent_changed_synchronously_rebuilds_children`

### §6. owner_for_block helper
- Добавлен `owner_for_block(struct, ordinal, ownership=None)`:
  - возвращает deepest section node_id если block принадлежит section
  - возвращает `struct.root_id` если block не принадлежи ни одной секции (preamble)
  - возвращает `None` если `ordinal` вне `[0, total_blocks)`
  - поддерживает ленивое построение ownership
- 5 новых тестов: `owner_for_block_returns_deepest_section`,
  `returns_root_for_uncovered_block`, `returns_none_for_out_of_range`,
  `lazy_builds_ownership`, `zero_or_one_owner_per_block`

### §7. Document chunk order по physical order
- `chunk_from_structure` переписан:
  - итерация по physical blocks в ordinal order, **не** по sections
  - последовательные blocks с одним owner группируются в один chunk
    (если влезает в budget)
  - atomic tables
  - порядок строго по block.ordinal
- 3 новых теста: `chunks_in_physical_document_order`,
  `chunks_have_strictly_increasing_index`, `chunks_deterministic_across_runs`
- Обновлены старые тесты для новой семантики группировки
- **§8 bonus:** document-level table counter (глобальный по документу)

### §8. Уникальность table_id
- 2 новых теста: `table_ids_unique_across_sections` (10 sections × 2 tables
  → 20 уникальных), `table_ids_deterministic`

### §9. Adjacent packing правила
- `pack_chunks_with_adjacent` полностью переписан с явными правилами:
  - Rule 1: table + non-table → отдельные batches (в обе стороны)
  - Rule 2: table + table → отдельные по умолчанию
  - Rule 3: max_sections_per_batch (default 2)
  - Rule 4: document order
  - Rule 5: distant sections не объединяются
  - Rule 6: token budget
- 8 новых тестов для каждого правила
- `current_section_ids` через list (не set) для детерминизма

### §10. Context expansion neighbours по target index
- `expand_context` переписан:
  - поиск neighbours по `target_idx ± 1, ± 2, ...` (чередование left/right)
  - subsection restriction
  - budget check после каждого добавления
  - `total_tokens == tokens(target) + sum(tokens(neighbours))`
- 6 новых тестов: `neighbours_by_target_index_not_section_prefix`,
  `target_at_left_edge`, `target_at_right_edge`, `skip_other_section`,
  `total_tokens_equals_sum`, `max_neighbour_blocks_respected`

### §11. DocumentIdentity fingerprint owner
- `physical.py` теперь использует `DocumentIdentity`:
  - удалён `_physical_cache_key` (старый алгоритм с `st.st_mtime` в секундах)
  - единый canonical алгоритм — `DocumentIdentity.from_path` с `st.st_mtime_ns`
- 2 новых теста: `identity_fingerprint_equals_physical_cache_key`,
  `identity_uses_mtime_ns_not_mtime`

### §12. Single canonical PhysicalDocument loading
- `DocumentLoader` упрощён:
  - удалён промежуточный `_extract_full_text` (отдельный вызов `extract_text`)
  - `DocumentLoader.load()` теперь делает ОДИН проход через `_iter_*_blocks`
  - title resolution работает на тексте из blocks
- 6 новых spy-тестов: pypdf ≤ 2, docx ≤ 2, DocumentIdentity integration, etc.

### §14. Удалить legacy adapters
- `summarizer_canonical.py`: убраны `reduce_strategy_for_legacy` и
  `execution_strategy_for_legacy`
- `summarizer.py`: 3 callsite переписаны как inline logic
- Canonical `select_strategy` (в `unified_execution.py`) — единственный owner

### §15. DocumentStats → DocumentAnalysis
- `document_stats.py` удалён
- `DocumentStats` dataclass и `compute_document_stats` удалены
- В `summarizer.py` введена `_inline_stats(doc, tree, chars_per_token)` helper
- 3 callsite переведены на inline расчёт
- `DocumentStats -> SectionTree` references = 0

### §16. Brief budget → canonical
- Создан `structure/brief_budget.py` с canonical `allocate_brief_budget`
- Legacy `apply_brief_text_budget` **удалён**
- `brief_representation.py` удалён
- `summarizer.py` использует canonical импорт

## Что осталось сделать

### §13. summarizer.py на canonical pipeline (продолжение) — ГЛАВНЫЙ ЭТАП

`summarizer.py` всё ещё содержит legacy imports и использует legacy path:

| Legacy import (summarizer.py) | Статус |
|---|---|
| `fingerprint` | ❌ не удалён |
| `document_cache` | ❌ не удалён |
| `document_cleanup` | ❌ не удалён |
| `structure.sections` | ❌ не удалён |
| `StructureAwareChunker` | ❌ не удалён |
| `token_budget` | ❌ не удалён |
| `pack_chunks` | ❌ не удалён |
| `DocumentStats` / `compute_document_stats` | ✅ удалён (§15) |
| `allocate_brief_budget` from brief_representation | ✅ удалён (§16) |
| `execution_strategy_for_legacy` / `reduce_strategy_for_legacy` | ✅ удалён (§14) |

**Следующие шаги для §13:**

1. **Заменить `inspect()` (summarizer.py:459-617) на canonical
   `inspect_canonical`** через `run_canonical_pipeline`. Это уберёт
   legacy `SectionTree` и `StructureAwareChunker`.

2. **Заменить map-фазу `run()` (summarizer.py:~L1160-1310)** — перевести
   с `ContextBatch` + `pack_chunks` на `ExecutionPlan.batches`. Использовать
   `build_execution_plan` из `unified_execution`.

3. **Убрать legacy cache (doc_cache, fingerprint)** — перейти на
   `DocumentAnalysis` snapshot (уже есть через
   `run_canonical_pipeline`). Проверить что follow-up path использует
   `canonical_retrieval.answer_followup` через `DocumentAnalysis`.

4. **Убрать legacy cleanup (`cleanup_blocks`)** — перейти на
   `structure.cleanup`.

5. **Убрать legacy `_build_token_budget` + `count_tokens`** — перейти на
   `TokenEstimator`.

**После §13 production-path должен быть:**

```
run() →
  inspect_canonical (через run_canonical_pipeline) →
  DocumentAnalysis →
  ExecutionPlan →
  batch execution через canonical →
  HierarchicalReducer (final reduce)
```

### §17-§32. Удаление legacy файлов

После §13 можно безопасно удалять:

```
scripts/document_cleanup.py
scripts/fingerprint.py
scripts/document_cache.py
scripts/token_budget.py
scripts/packing.py
scripts/packing_impl.py
scripts/packing_models.py
scripts/execution_strategy.py
scripts/reducer_strategy.py
scripts/brief_strategy.py (если ещё есть)
structure/sections.py
structure/tree.py
structure/compatibility.py
```

Также удалить `StructureAwareChunker` из `structure/chunks.py` (сохранить
только `Chunk` dataclass и shared primitives).

### §33-§49. Тесты, guards, cleanup, audit, docs

- §34. `legacy_audit.py` → regression guard с `audit()` + `assert_no_legacy()`
- §35. zero-reference pytest через AST
- §36-§37. production-path + cache-path integration tests
- §38-§39. structure correctness suite + final determinism suite
- §40-§41. single-flight LLM invariant + error scenarios
- §42. documentation review
- §43. размер `summarizer.py` (orchestration only)
- §44. проверка отсутствия параллельных реализаций
- §45. final repository-wide audit
- §46-§47. финальный pytest + static validation
- §48-§49. финальная проверка архитектуры + acceptance criteria

## Полезные ссылки

- `docs/legal_summarizer_cleanup_baseline.md` — baseline (369+4 skipped,
  commit `2008e7d`)
- `docs/legal_summarizer_cleanup_inventory.md` — инвентаризация summarizer.py
  с группами A/B/C и таблицей legacy→canonical
- `workspace/skills/legal_summarizer/scripts/legacy_audit.py` — текущий audit
  (после §1-§16 показывает 73 production legacy references)
- `workspace/skills/legal_summarizer/scripts/summarizer_canonical.py` —
  canonical pipeline wrapper
- `workspace/skills/legal_summarizer/scripts/structure/pipeline.py` —
  `run_canonical_pipeline` (DocumentLoader → DocumentStructure →
  DocumentAnalysis)
- `workspace/skills/legal_summarizer/scripts/structure/unified_execution.py` —
  canonical `select_strategy` (заменяет все legacy selectors)
- `workspace/skills/legal_summarizer/scripts/canonical_retrieval.py` —
  `answer_followup` через `DocumentAnalysis`

## Текущий legacy audit

После §14-§16 (commit `9d97396`):

```
Production legacy references: ~50-60 (было 73)
Test-only legacy references:   5
```

Главные остатки:
- `summarizer.py` — использует `fingerprint`, `document_cache`,
  `document_cleanup`, `structure.sections`, `StructureAwareChunker`,
  `token_budget`, `pack_chunks`
- `structure/chunks.py` — содержит `StructureAwareChunker` (для §21)
- `structure/sections.py` — содержит legacy `SectionTree` и
  `DocumentSection` (для §22)
- `structure/tree.py` — содержит `build_section_tree` (для §22)

После §13 (когда summarizer.py переедет на canonical) можно
безопасно удалять эти legacy modules.