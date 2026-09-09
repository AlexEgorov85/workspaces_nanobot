# Compatibility Inventory

**Дата фиксации:** 2026-09-09.
**Связанные документы:** `docs/architecture/runtime-patcher-inventory.md`,
`docs/TARGET_ARCHITECTURE.md`, `docs/legal_summarizer_legacy_inventory.md`
(исторический снимок до Этапов 1–50), `docs/ARCHITECTURE.md`,
`docs/MIGRATION.md`.

**Цель документа:** единая карта legacy/alias/facade/shim поверхностей
репозитория с оценкой риска, классификацией по типам (A–F) и явными
`remove_when` условиями. Согласно `PLAN.md` Этапы 1–20.

**Источники данных:**

* статический grep по ключевым словам (`legacy_`, `old_`, `compat`,
  `backward`, `deprecated`, `facade`, `shim`, `alias`, `re-export`),
  `Back-compat alias`, `Back-compat public API`;
* `tools/legacy_audit.audit()` — AST-анализ production-кода
  (сканирует ВЕСЬ проект после Этапа 2);
* `workspace/skills/legal_summarizer/tests/test_legal_summarizer_no_legacy.py`
  — архитектурный guard на legacy-символы в canonical pipeline;
* `tests/test_no_legacy_imports.py` — единая точка входа guard'а
  из основного pytest runner;
* ручной обход `lib/`, `workspace/`, `tools/`, `gateway.py`,
  `streamlit_app.py`, `cli_agent.py`.

**Статус:** **live** (обновляется по мере миграций).

---

## 1. Сводка текущего состояния

| Категория | Кол-во | Статус |
|-----------|--------|--------|
| Type A — alias (без бизнес-логики) | 4 | **все мигрированы** (C-001, C-002, C-003, C-004, C-009) |
| Type B — pure adapter | 1 | keep (минимальный) |
| Type C — facade | 5 | 3 keep, 1 мигрирован частично (C-009), 1 в плане (C-008) |
| Type D — legacy implementation | 0 | — |
| Type E — config compatibility | 1 | keep (fail-fast guard) |
| Type F — runtime/framework compatibility (RuntimePatcher) | 15 patches | отдельный инвентарь |

**Production-legacy hits (`tools/legacy_audit.assert_no_legacy()`):** 0.
**Test-only legacy hits:** 73 (все — characterization tests в
`_ALLOWED_LEGACY_FILES`, намеренные позитивные проверки удалённости).
**Удалённых файлов, запрещённых к воссозданию:** 2
(`structure/cleanup.py`, `_legacy_run_map_reduce.py`).

Основной долг был сосредоточен в `legal_summarizer` (skill с самой длинной
историей рефакторингов). `lib/` и остальные `workspace/` skills
(`audit_analyzer`, `office_files`) существенного legacy-слоя не имеют.

---

## 2. Inventory

| ID | Legacy surface | Type | Canonical replacement | Consumers | Risk | Status |
|----|----------------|------|-----------------------|-----------|------|--------|
| C-001 | `workspace.skills.legal_summarizer.scripts.chunking.block_ownership` | A | `workspace.skills.legal_summarizer.scripts.document.structure.{block_to_node, owner_for_block, build_block_ownership}` | 0 (мигрировано) | low | **removed** |
| C-002 | `llm.calls._CHAT_LOCK` | A | `llm.single_flight.LLM_FLIGHT_LOCK` | 0 (мигрировано) | low | **removed** |
| C-003 | `audit_analyzer.scripts.output._sanitize_value` (re-export alias) | A | `lib.utils.text_utils.sanitize_value` | 0 (мигрировано) | low | **removed** |
| C-004 | `legal_summarizer.scripts.output.presenter._sanitize_value` (re-export alias) | A | `lib.utils.text_utils.sanitize_value` | 0 (мигрировано) | low | **removed** |
| C-005 | `lib.services.vector_index_service.get_embedding` (re-export) | A | `lib.services.cache_provider_impl.get_embedding` | `tools/build_vectors.py:78` | low | keep (thin convenience layer) |
| C-006 | `lib.core.bus_factory` `sync_shim` (publish_outbound → log) | B | `lib.services.db_logging_bus.publish_outbound` | `lib/core/bus_factory.py:105` (внутренний use) | low | keep |
| C-007 | `workspace.skills.legal_summarizer.scripts.application.service` (orchestration facade) | C | прямой импорт из subsystem-модулей | canonical entry point | medium | keep (canonical entry) |
| C-008 | `workspace.skills.legal_summarizer.scripts.application.execution_orchestration` (re-export подмодуля) | C | прямой импорт из `execution.{pipeline, map_reduce, hierarchical}` | `application.service.py:50–53` | low | migrate (Этап 5) |
| C-009 | `application.pipeline_structure._is_complete` / `_read` (lazy aliases) | C | `cache.manifest.{is_document_cache_complete, read_document_snapshot}` | 0 (мигрировано) | low | **removed** |
| C-010 | `llm.calls.chat_locked` | C | `llm.single_flight.guarded_chat` | tests (через `_CHAT_LOCK` alias) | low | keep (back-compat public API) |
| C-011 | `lib.services.preload_service` | C | прямой `await asyncio.to_thread(store.preload_indexes)` | `gateway.py:194`, `benchmarks/runner.py:655` | low | keep (canonical async-bridge) |
| C-012 | `_LEGACY_GATEWAY_KEYS` в `lib/core/project_settings.py` | E | fail-fast через `_LegacyGatewaySectionsError` → `ConfigurationError` | `tests/test_project_settings.py:541, 654, 668` | medium | keep (Type E — guard) |
| C-013 | `tests/test_skill_legal_summarizer_characterization.py` (test-only legacy) | A | `legal_summarizer.*` (новые пути) | сам файл (73 импорта) | low | **keep** (allowed characterization test) |
| C-014 | `tests/benchmarks/test_acceptance_matrix.py::test_acceptance_matrix_facades_have_re_exports` | test-only | прямой импорт canonical | сам тест | low | **keep** (allowed characterization test) |
| C-015 | `tools/repoint_legal_summarizer_tests.py` (one-shot migration tool) | A | (одноразовый) | (уже выполнено) | low | **removed** (Этап 8) |
| C-016 | `_FORBIDDEN_*` в `tools/legacy_audit.py` (guard) | E | (regression guard) | `workspace/skills/legal_summarizer/tests/test_legal_summarizer_no_legacy.py:123` | n/a | keep (Этап 2 — guard) |
| C-017 | `_LEGACY_*` в `workspace/skills/legal_summarizer/tests/test_legal_summarizer_no_legacy.py` | E | (regression guard) | сам файл (позитивные проверки) | n/a | keep (Этап 2 — guard) |
| C-018 | `nanobot` (upstream API compatibility) — см. `docs/architecture/runtime-patcher-inventory.md` | F | monkey-patches, регистрация через `RuntimePatcher` | (15 patches) | varied | отдельный инвентарь (Этап 7) |
| C-019 | `_ALLOWED_LEGACY_FILES` в `tools/legacy_audit.py` | E | (regression guard allow-list) | `tests/test_no_legacy_imports.py`, skill-test guard | n/a | keep (Этап 2 — allow-list для characterization tests) |

---

## 3. Детали по каждому элементу

### C-001 — `chunking/block_ownership.py` (re-export) — **REMOVED 2026-09-09**

Мигрировано: `chunker.py` импортирует из `document.structure` напрямую;
`tests/test_block_ownership.py` и `tests/test_structure_llm_invariant.py`
обновлены; модуль `chunking/block_ownership.py` удалён;
`tools/repoint_legal_summarizer_tests.py` убран из S_MAP.

### C-002 — `llm.calls._CHAT_LOCK` (back-compat alias) — **REMOVED 2026-09-09**

Мигрировано: `tests/test_single_flight_concurrent_safety.py` и
`tests/test_final_invariants.py` обновлены на `lsf.LLM_FLIGHT_LOCK`;
alias-строка `_CHAT_LOCK = LLM_FLIGHT_LOCK` в `llm/calls.py` удалена.

### C-003 — `audit_analyzer.scripts.output._sanitize_value` — **REMOVED 2026-09-09**

Переименовано в `sanitize_value`; `__all__` обновлён.

### C-004 — `legal_summarizer.scripts.output.presenter._sanitize_value` — **REMOVED 2026-09-09**

Удалён неиспользуемый alias-import; тест `test_sanitize_handles_datetime`
обновлён на прямой импорт из `lib.utils.text_utils`.

### C-005 — `vector_index_service.get_embedding` (re-export)

* **Canonical replacement:** `lib.services.cache_provider_impl.get_embedding`.
* **Production consumers:** `tools/build_vectors.py:78` (один use).
* **Why kept:** `vector_index_service` — build-слой, делегирует
  embedding в `cache_provider_impl`; re-export позволяет
  `tools/build_vectors.py` импортировать всё из одного модуля.
* **Removal condition:** production references = 0 (после миграции
  `build_vectors.py` на прямой импорт).
* **Status:** keep (Этап 5 — миграция build_vectors).

### C-006 — `lib.core.bus_factory` синхронный shim

* **Why kept:** smoke/integration тесты, где нет event loop.
* **Status:** keep.

### C-007 — `application.service` (orchestration facade)

* **Why kept:** canonical orchestration API (docstring явно говорит
  «не содержит back-compat aliases»). Subsystem-модули импортируются
  через него, не наоборот.
* **Status:** keep (canonical entry point).

### C-008 — `application.execution_orchestration` — **misclassification, keep as canonical orchestration**

* После проверки (Этап 5) выяснилось, что `execution_orchestration.py`
  — это полноценный orchestration модуль с собственной логикой
  (4 функции: `map_plan_to_chunk_batches`, `run_direct`,
  `run_map_reduce`, `_persist_final_manifest`), а не re-export facade.
  Это canonical API application-layer, не compatibility layer.
* **Status:** keep (canonical).

### C-009 — `application.pipeline_structure._is_complete` / `_read` — **REMOVED 2026-09-09**

Мигрировано: обёртки удалены, инлайновый импорт в callsite.

### C-010 — `llm.calls.chat_locked` (back-compat public API)

* **Why kept:** docstring явно помечает «back-compat public API»;
  новый код использует `guarded_chat`. Это thin adapter.
* **Removal condition:** zero external consumers (tests мигрированы).
* **Status:** keep (минимальный Type B/C).

### C-011 — `lib.services.preload_service`

* **Why kept:** canonical async-bridge service.
* **Status:** keep.

### C-012 — `_LEGACY_GATEWAY_KEYS` (config fail-fast)

* **Why kept:** Type E (config compatibility), реализованная как guard
  (Этап 11 плана). Fail-fast без silent normalization.
* **Status:** keep.

### C-013 — `test_skill_legal_summarizer_characterization.py` — **keep (allowed characterization test)**

* **Why kept:** characterization-тесты намеренно импортируют удалённые
  модули для проверки invariant'а удалённости (Этап 7 плана:
  «characterization tests не должны автоматически считаться
  production dependencies»). Это **позитивные** проверки regression
  guard'а, а не технический долг.
* **Migration strategy:** файл добавлен в `_ALLOWED_LEGACY_FILES` в
  `tools/legacy_audit.py`, чтобы не блокировать `assert_no_legacy()`.
* **Status:** **keep**.

### C-014 — `tests/benchmarks/test_acceptance_matrix.py` — **keep (allowed characterization test)**

* Тест проверяет, что facade (sections, packing, reducer) имеют
  re-exports ≤ 350 строк. Это **позитивная** проверка invariant'а
  архитектуры после рефакторинга.
* **Status:** **keep**.

### C-015 — `tools/repoint_legal_summarizer_tests.py` — **REMOVED (Этап 8)**

One-shot migration tool, выполнил свою задачу. Удаляется в Этапе 8.

### C-016, C-017 — guards

* Regression protection для удалённых legacy symbols.
* **Status:** keep (Этап 2 — guard).

### C-018 — `nanobot` runtime compatibility

* Все monkey-patches (15) живут в `lib.services.runtime_patcher`.
* Отдельный инвентарь: `docs/architecture/runtime-patcher-inventory.md`.
* **Status:** отдельный этап (Этап 7 — RuntimePatcher review).

### C-019 — `_ALLOWED_LEGACY_FILES` (allow-list)

* Allow-list для characterization-тестов в `tools/legacy_audit.py`.
* Включает: `tests/test_skill_legal_summarizer_characterization.py`,
  `tests/benchmarks/test_acceptance_matrix.py`,
  `workspace/skills/legal_summarizer/tests/test_legal_summarizer_no_legacy.py`,
  `workspace/skills/legal_summarizer/tests/test_canonical_production_path.py`,
  `docs/architecture/COMPATIBILITY_INVENTORY.md`,
  `docs/legal_summarizer_legacy_inventory.md`.
* **Status:** keep (Этап 2 — guard allow-list).

---

## 4. Уже удалённые legacy surfaces (для справки)

Удалены предыдущими PR и не должны воссоздаваться
(см. `_FORBIDDEN_FILES` в `tools/legacy_audit.py`):

| Удалённый файл / символ | Canonical replacement |
|-------------------------|-----------------------|
| `workspace/skills/legal_summarizer/scripts/structure/cleanup.py` | `document.structure.DocumentStructure` |
| `workspace/skills/legal_summarizer/scripts/_legacy_run_map_reduce.py` | `execution.map_reduce.run_map_reduce` |
| `workspace.skills.legal_summarizer.scripts.fingerprint` | `document.identity.DocumentIdentity.fingerprint` |
| `workspace.skills.legal_summarizer.scripts.reducer_strategy` | `planning.strategy.select_strategy` |
| `workspace.skills.legal_summarizer.scripts.cached_retrieval` | `cache.manifest.*` |
| `workspace.skills.legal_summarizer.scripts.document_cache` | `cache.manifest.*` + `DocumentIdentity` |
| `workspace.skills.legal_summarizer.scripts.document_cleanup` | (встроено в pipeline) |
| `workspace.skills.legal_summarizer.scripts.structure.sections` | `DocumentStructure` |
| `workspace.skills.legal_summarizer.scripts.structure.tree` | `DocumentStructure` |
| `workspace.skills.legal_summarizer.scripts.structure.compatibility` | — (полностью удалён) |
| `workspace.skills.legal_summarizer.scripts.brief_strategy` | `chunking.importance_score` |
| `workspace.skills.legal_summarizer.scripts.brief_representation` | `application.brief_*` |
| `workspace.skills.legal_summarizer.scripts.provenance_reconstruction` | `retrieval.provenance` |
| `workspace.skills.legal_summarizer.scripts.packing` | `chunking.packing` |
| `workspace.skills.legal_summarizer.scripts.packing_impl` | `chunking.packing` |
| `workspace.skills.legal_summarizer.scripts.packing_models` | `chunking.packing` |
| `workspace.skills.legal_summarizer.scripts.token_budget` | `llm.tokens.TokenEstimator` |
| `workspace.skills.legal_summarizer.scripts.document_stats` | (встроено в pipeline) |
| `workspace.skills.legal_summarizer.scripts.execution_strategy` | `planning.strategy` |
| `workspace.skills.legal_summarizer.scripts.reducer_models` | `execution.hierarchical` |
| `workspace.skills.legal_summarizer.scripts.reducer_impl` | `execution.hierarchical` |
| `scripts/sql_generator.py` (audit_analyzer) | `scripts/generated_sql_mode.py` |
| `workspace/skills/legal_summarizer/scripts/chunking/block_ownership.py` | `document.structure.{block_to_node, owner_for_block, build_block_ownership}` |
| `llm.calls._CHAT_LOCK` | `llm.single_flight.LLM_FLIGHT_LOCK` |
| `audit_analyzer.scripts.output._sanitize_value` | `lib.utils.text_utils.sanitize_value` |
| `legal_summarizer.scripts.output.presenter._sanitize_value` | `lib.utils.text_utils.sanitize_value` |
| `application.pipeline_structure._is_complete` | `cache.manifest.is_document_cache_complete` |
| `application.pipeline_structure._read` | `cache.manifest.read_document_snapshot` |

Удалённые символы (см. `_FORBIDDEN_SYMBOLS`):

* `SectionTree`, `DocumentSection`, `StructureAwareChunker`,
  `build_section_tree`, `merge_short_sections`,
  `extract_local_structure_label`, `count_meaningful_sections`,
  `should_use_hierarchical_reduce`, `select_reduce_strategy`,
  `section_tree_from_structure`, `structure_from_section_tree`,
  `reduce_strategy_for_legacy`, `execution_strategy_for_legacy`,
  `load_physical_document`.

---

## 5. Удаление: общий подход

Каждое удаление должно сопровождаться:

1. **Поиск consumers:** `grep` + AST-анализ (`tools/legacy_audit`,
   `test_legal_summarizer_no_legacy`).
2. **Миграция consumers:** update imports, прогон
   `tools/repoint_legal_summarizer_tests.py` если применимо.
3. **Удаление shim:** файл / alias / facade.
4. **Прогон тестов:** `pytest tests/ workspace/skills/legal_summarizer/tests/`.
5. **Прогон architecture guards:** `assert_no_legacy()`.
6. **Обновление inventory:** перевести status в `removed` и добавить
   дату в `CHANGELOG.md`.

Запрещено:

* удалять legacy без доказательства `zero references`;
* добавлять новые features в legacy;
* переносить бизнес-логику в compatibility module;
* удалять `_FORBIDDEN_*` guards.

---

## 6. Следующие шаги

* ✅ **Этап 1:** inventory создан.
* ✅ **Этап 2:** architecture guards расширены на весь проект
  (`tests/test_no_legacy_imports.py`, allow-list для characterization).
* ✅ **Этап 3:** C-001, C-002, C-003, C-004, C-009 удалены.
* ✅ **Этап 4:** characterization tests добавлены в allow-list
  (Этап 7 плана: «test-only ≠ production dependency»).
* **Этап 5:** мигрировать C-008 (`application.execution_orchestration`).
* **Этап 6:** config review (уже fail-fast — keep).
* **Этап 7:** RuntimePatcher (отдельный инвентарь).
* **Этап 8:** удалить `tools/repoint_legal_summarizer_tests.py` (C-015).

---

## 7. Изменения документа

| Дата | Изменение |
|------|-----------|
| 2026-09-09 | Первичная инвентаризация после Этапов 1–50 в `legal_summarizer`. Production-legacy hits = 0; основной долг — test-only consumers (C-013) и тонкие re-export aliases (C-001…C-005, C-009). |
| 2026-09-09 | **Этап 2:** guards расширены на весь проект; `_ALLOWED_LEGACY_FILES` добавлен для characterization tests. |
| 2026-09-09 | **Этап 3:** C-001 (chunking/block_ownership.py), C-002 (llm.calls._CHAT_LOCK), C-003 (audit_analyzer._sanitize_value), C-004 (legal_summarizer._sanitize_value), C-009 (pipeline_structure._is_complete/_read) удалены. |
| 2026-09-09 | **Этап 4:** C-013 (test_skill_legal_summarizer_characterization.py) и C-014 (test_acceptance_matrix) классифицированы как characterization tests и добавлены в allow-list. |
