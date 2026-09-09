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

## 1. Сводка текущего состояния (на 2026-09-09)

Inventory содержит **19 entries** (C-001..C-019). Каждый ID имеет ровно
один status; summary ниже вычисляется из таблицы:

| Категория | Кол-во | IDs | Status |
|-----------|--------|-----|--------|
| **removed** | 6 | C-001, C-002, C-003, C-004, C-009, C-015 | мигрированы, в production не встречаются |
| **keep (canonical)** | 4 | C-007, C-008, C-010, C-011 | текущие canonical entry points (не legacy) |
| **keep (intentional shim)** | 4 | C-005, C-006, C-012, C-018 | документированные тонкие shims/adapters |
| **keep (guard)** | 3 | C-016, C-017, C-019 | regression protection, не production code |
| **broken characterization** | 2 | C-013, C-014 | тесты, импортирующие удалённые модули; требуют ремонта |
| **Всего** | 19 | | |

**Production-legacy hits (`tools/legacy_audit.assert_no_legacy()`):** 0.
**Test-only legacy hits:** см. секцию §6 «Broken characterization tests».
**Удалённых файлов, запрещённых к воссозданию:** 2
(`structure/cleanup.py`, `_legacy_run_map_reduce.py`).

### 1.1 Важная поправка к предыдущему состоянию документа

До 2026-09-09 inventory содержал **три противоречия**:

1. **C-008** одновременно помечен и `migrate`, и `keep (canonical)`
   (после проверки в Этапе 5 он оказался canonical, не facade).
2. **Type A count = 4** в summary, но в таблице 5 ID помечены Type A
   (C-001..C-005), из них 4 удалены и 1 keep — summary был неверным.
3. **`_ALLOWED_LEGACY_FILES` в `tools/legacy_audit.py`** слишком широкий:
   allow-list на уровне всего файла, а не на уровне тестовой функции.
   Это позволяло characterization-тестам импортировать удалённые модули
   без блокировки guard'ом.

Все три исправлены ниже.

---

## 2. Inventory

| ID | Legacy surface | Type | Canonical replacement | Risk | Status |
|----|----------------|------|-----------------------|------|--------|
| C-001 | `workspace.skills.legal_summarizer.scripts.chunking.block_ownership` | A | `workspace.skills.legal_summarizer.scripts.document.structure.{block_to_node, owner_for_block, build_block_ownership}` | low | **removed** |
| C-002 | `llm.calls._CHAT_LOCK` | A | `llm.single_flight.LLM_FLIGHT_LOCK` | low | **removed** |
| C-003 | `audit_analyzer.scripts.output._sanitize_value` (re-export alias) | A | `lib.utils.text_utils.sanitize_value` | low | **removed** |
| C-004 | `legal_summarizer.scripts.output.presenter._sanitize_value` (re-export alias) | A | `lib.utils.text_utils.sanitize_value` | low | **removed** |
| C-005 | `lib.services.vector_index_service.get_embedding` (re-export) | A | `lib.services.cache_provider_impl.get_embedding` | low | **keep** (intentional shim) |
| C-006 | `lib.core.bus_factory.sync_shim` (`build_logging_bus`) | B | `lib.services.db_logging_bus.publish_outbound` | low | **keep** (intentional shim) |
| C-007 | `workspace.skills.legal_summarizer.scripts.application.service` | C | (canonical entry point) | medium | **keep (canonical)** |
| C-008 | `workspace.skills.legal_summarizer.scripts.application.execution_orchestration` | C | (canonical orchestration, не facade) | low | **keep (canonical)** |
| C-009 | `application.pipeline_structure._is_complete` / `_read` (lazy aliases) | C | `cache.manifest.{is_document_cache_complete, read_document_snapshot}` | low | **removed** |
| C-010 | `llm.calls.chat_locked` (back-compat public API) | C | `llm.single_flight.guarded_chat` | low | **keep (canonical)** |
| C-011 | `lib.services.preload_service` | C | (canonical async-bridge service) | low | **keep (canonical)** |
| C-012 | `_LEGACY_GATEWAY_KEYS` в `lib/core/project_settings.py` | E | fail-fast через `_LegacyGatewaySectionsError` → `ConfigurationError` | medium | **keep** (intentional shim) |
| C-013 | `tests/test_skill_legal_summarizer_characterization.py` — tests, импортирующие удалённые модули | test-only | canonical pipeline | low | **broken characterization** (требует ремонта) |
| C-014 | `tests/benchmarks/test_acceptance_matrix.py::REQUIRED_MODULES` | test-only | canonical `legal_summarizer.*` paths | medium | **broken characterization** (требует ремонта) |
| C-015 | `tools/repoint_legal_summarizer_tests.py` | A | (one-shot, выполнил задачу) | low | **removed** |
| C-016 | `_FORBIDDEN_*` в `tools/legacy_audit.py` | E | (regression guard registry) | n/a | **keep (guard)** |
| C-017 | `_LEGACY_*` в `workspace/skills/legal_summarizer/tests/test_legal_summarizer_no_legacy.py` | E | (regression guard registry) | n/a | **keep (guard)** |
| C-018 | `nanobot` upstream API compatibility (RuntimePatcher) | F | monkey-patches | varied | **keep (intentional shim)** — отдельный инвентарь |
| C-019 | `_ALLOWED_LEGACY_TESTS` в `tools/legacy_audit.py` | E | (test-level allow-list) | n/a | **keep (guard)** |

---

## 3. Детали по каждому элементу

### C-001 — `chunking/block_ownership.py` (re-export) — **REMOVED 2026-09-09**

Мигрировано: `chunker.py` импортирует из `document.structure` напрямую;
`tests/test_block_ownership.py` и `tests/test_structure_llm_invariant.py`
обновлены; модуль `chunking/block_ownership.py` удалён.

### C-002 — `llm.calls._CHAT_LOCK` (back-compat alias) — **REMOVED 2026-09-09**

Мигрировано: `tests/test_single_flight_concurrent_safety.py` и
`tests/test_final_invariants.py` обновлены на `lsf.LLM_FLIGHT_LOCK`;
alias-строка `_CHAT_LOCK = LLM_FLIGHT_LOCK` в `llm/calls.py` удалена.

### C-003 — `audit_analyzer.scripts.output._sanitize_value` — **REMOVED 2026-09-09**

Переименовано в `sanitize_value`; `__all__` обновлён.

### C-004 — `legal_summarizer.scripts.output.presenter._sanitize_value` — **REMOVED 2026-09-09**

Удалён неиспользуемый alias-import; тест `test_sanitize_handles_datetime`
обновлён на прямой импорт из `lib.utils.text_utils`.

### C-005 — `vector_index_service.get_embedding` (re-export) — **keep (intentional shim)**

* **Canonical replacement:** `lib.services.cache_provider_impl.get_embedding`.
* **Production consumers:** `tools/build_vectors.py:78` (один use).
* **Why kept:** `vector_index_service` — это build-слой для FAISS-индексов
  (см. `lib.services.vector_index_service.VectorIndexBuildService`).
  Re-export `get_embedding` позволяет вызывающему импортировать всё из
  одного модуля. **Это convenience layer, не strict alias.**
* **Removal condition:** production references = 0 после миграции
  `tools/build_vectors.py` на прямой импорт из
  `lib.services.cache_provider_impl.get_embedding`.
* **Status:** keep (intentional shim; возможен следующий cleanup).

### C-006 — `lib.core.bus_factory.sync_shim` (`build_logging_bus`) — **keep (intentional shim)**

* **Canonical replacement:** `lib.services.db_logging_bus.publish_outbound`
  (асинхронный).
* **Why kept:** `build_logging_bus(...)` в `lib/core/bus_factory.py:105`
  — legacy compatibility helper для сценариев, где у вызывающего есть
  **синхронный sink** (smoke/integration tests, отсутствие event loop).
  Это **thin adapter**, не Type C facade: ровно одна функция, нет
  собственной бизнес-логики, только синхронный bridge к async API.
* **Removal condition:** zero production references + zero test references.
* **Status:** keep (intentional shim).

### C-007 — `application.service` (orchestration facade) — **keep (canonical)**

* `service.py` — единственная публичная entry point для запуска
  `summarizer.run()`. Docstring явно говорит: «не содержит back-compat
  aliases». Subsystem-модули импортируются через него, не наоборот.
* **Status:** keep (canonical).

### C-008 — `application.execution_orchestration` — **keep (canonical)**

* После проверки (Этап 5): модуль содержит **4 функции с собственной
  логикой**: `map_plan_to_chunk_batches`, `run_direct`, `run_map_reduce`,
  `_persist_final_manifest`. Это orchestration module, **не facade** и
  **не compatibility layer**.
* **Status:** keep (canonical).
* **Предыдущая ошибка в этом документе:** C-008 был ошибочно помечен
  `migrate` в Этапе 5 до проверки содержимого файла. После проверки —
  статус окончательный `keep (canonical)`.

### C-009 — `application.pipeline_structure._is_complete` / `_read` — **REMOVED 2026-09-09**

Мигрировано: обёртки удалены, инлайновый импорт в callsite.

### C-010 — `llm.calls.chat_locked` (back-compat public API) — **keep (canonical)**

* `chat_locked(messages, *, context=None)` — тонкая обёртка над
  `guarded_chat(llm.chat, messages, context=context)`. Docstring явно
  помечает её как «back-compat public API». Это Type B pure adapter.
* **Removal condition:** zero external consumers (после миграции тестов
  на `guarded_chat` напрямую).
* **Status:** keep (canonical — этот API экспортируется как часть
  публичной поверхности `llm.calls`).

### C-011 — `lib.services.preload_service` — **keep (canonical)**

* `PreloadService.preload_vector_indexes(store)` — async-bridge service:
  единая точка для прогрева FAISS-индексов в память с обработкой ошибок
  и идемпотентностью. Consumers: `gateway.py:194`, `benchmarks/runner.py:655`.
* **Status:** keep (canonical).

### C-012 — `_LEGACY_GATEWAY_KEYS` (config fail-fast) — **keep (intentional shim)**

* `_LEGACY_GATEWAY_KEYS` в `lib/core/project_settings.py:597` —
  Type E (config compatibility), реализованная как guard (Этап 11
  плана). Fail-fast через `_LegacyGatewaySectionsError` →
  `ConfigurationError` без silent normalization.
* Текущий список: `{"vector_index": "gateway.vector_index.* → gateway.vector.index.*"}`.
* **Status:** keep (intentional shim; это и есть правильная форма
  config compatibility — явный отказ от старого API).

### C-013 — `tests/test_skill_legal_summarizer_characterization.py` — **broken characterization**

* Содержит **73 импорта удалённых модулей** (document_cleanup,
  packing, document_stats, structure.sections/tree, load_physical_document).
* Эти тесты писались ДО финальной реструктуризации legal_summarizer и
  **никогда не были мигрированы** на canonical API. Сейчас они падают
  с `ModuleNotFoundError`.
* **Старая интерпретация (до 2026-09-09):** «characterization tests
  намеренно импортируют удалённые модули для проверки удалённости».
  Это было **ошибочной интерпретацией**: в файле есть только реальные
  `from X import Y`, нет ни одного `try: import X except ImportError: ...`.
* **Status:** broken — требует ремонта. См. секцию §6.

### C-014 — `tests/benchmarks/test_acceptance_matrix.py::REQUIRED_MODULES` — **broken characterization**

* `REQUIRED_MODULES` (строка 323–339) содержит **12 путей к удалённым
  legacy-модулям**: `workspace.skills.legal_summarizer.scripts.fingerprint`,
  `document_cache`, `token_budget`, `document_stats`, `document_cleanup`,
  `execution_strategy`, `reducer_models/strategy/impl`, `packing_models/impl`,
  и т.д. Тест `test_acceptance_matrix_required_modules_exist` **требует,
  чтобы эти удалённые модули существовали** — это **прямое
  противоречие** с архитектурным guard'ом.
* Также `test_acceptance_matrix_facades_have_re_exports` импортирует
  `workspace.skills.legal_summarizer.scripts.packing` (строка 358),
  который не отслеживается в git.
* **Status:** broken — требует ремонта. См. секцию §6.

### C-015 — `tools/repoint_legal_summarizer_tests.py` — **REMOVED 2026-09-09**

One-shot migration tool, выполнил свою задачу при предыдущих
рефакторингах legal_summarizer. Удалён.

### C-016, C-017 — guards — **keep (guard)**

* `_FORBIDDEN_MODULES`, `_FORBIDDEN_SYMBOLS`, `_FORBIDDEN_FILES` в
  `tools/legacy_audit.py` — единый реестр для regression guard'а.
* `_LEGACY_MODULES`, `_LEGACY_SYMBOLS`, `_LEGACY_FILES` в
  `workspace/skills/legal_summarizer/tests/test_legal_summarizer_no_legacy.py`
  — skill-level guard. См. секцию §7 о необходимости унификации.

### C-018 — `nanobot` runtime compatibility (RuntimePatcher) — **keep (intentional shim)**

* Все 15 monkey-patches живут в `lib.services.runtime_patcher`.
* Отдельный инвентарь: `docs/architecture/runtime-patcher-inventory.md`.
* Каждый patch имеет purpose, target, risk, тест.
* **Status:** keep (intentional shim, Type F — framework compatibility).

### C-019 — `_ALLOWED_LEGACY_TESTS` (test-level allow-list) — **keep (guard)**

* После ремонта (Этап C, см. ниже): `_ALLOWED_LEGACY_FILES` заменён
  на `_ALLOWED_LEGACY_TESTS` — словарь `{file.py::test_function_name: rationale}`.
* Allow-list применяется только к конкретным тестовым функциям, а не
  ко всему файлу. Это позволяет characterization-тестам вроде
  `test_legacy_module_removed` (с `try: import X except ImportError: pass`)
  проходить guard, но блокирует любой новый код, который попытается
  импортировать удалённый модуль в production-логике внутри test-файла.

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
2. **Миграция consumers:** update imports, прогон тестов.
3. **Удаление shim:** файл / alias / facade.
4. **Прогон тестов:** `pytest tests/ workspace/skills/legal_summarizer/tests/`
   **полным набором**, не только подмножеством.
5. **Прогон architecture guards:** `assert_no_legacy()`.
6. **Обновление inventory:** перевести status в `removed` и добавить
   дату в `CHANGELOG.md`.

Запрещено:

* удалять legacy без доказательства `zero references`;
* добавлять новые features в legacy;
* переносить бизнес-логику в compatibility module;
* удалять `_FORBIDDEN_*` guards;
* объявлять compatibility cleanup завершённым только по `production
  legacy hits == 0` (нужны 4 независимых критерия, см. §8).

---

## 6. Broken characterization tests (C-013, C-014)

### 6.1 Состояние на 2026-09-09

После коммитов `9df24e3` и `afd3bac` **84 теста падают** в полном
pytest-запуске:

* `tests/test_skill_legal_summarizer_characterization.py` — 82 failures
  (импорт `document_cleanup`, `packing`, `document_stats`,
  `structure.sections/tree`, `load_physical_document`).
* `tests/benchmarks/test_acceptance_matrix.py` — 9 failures
  (требует существования удалённых модулей через `REQUIRED_MODULES`).

Эти тесты **никогда не проходили** после финальной реструктуризации
legal_summarizer (Этапы 1–50). Предыдущая оценка compatibility cleanup
как «завершённого» была **ошибочной** — основывалась на targeted
прогонах мигрированных тестов, а не на полном pytest.

### 6.2 План ремонта (Этапы D, E, F)

**Этап D — `tests/benchmarks/test_acceptance_matrix.py`:**

* Удалить из `REQUIRED_MODULES` все legacy-пути
  (`workspace.skills.legal_summarizer.scripts.*`).
* Оставить только canonical `legal_summarizer.*` пути.
* Обновить или удалить `test_acceptance_matrix_facades_have_re_exports`
  (он импортирует `workspace.skills.legal_summarizer.scripts.packing`,
  который не отслеживается).

**Этап E — `tests/test_skill_legal_summarizer_characterization.py`:**

* Удалить **все тесты, которые импортируют удалённые модули**:
  `test_cleanup_blocks_*`, `test_summarize_*` со ссылками на
  `document_cleanup`, `packing`, `document_stats`,
  `structure.sections/tree`, `load_physical_document`.
* Эти тесты потеряли смысл после реструктуризации (Этапы 1–50);
  они писались для старой реализации, которая больше не существует.
* Если какой-то тест проверяет полезное свойство — переписать его
  на canonical API, иначе удалить как orphaned.

**Этап F — `workspace/skills/legal_summarizer/tests/test_legal_summarizer_no_legacy.py`:**

* Унифицировать реестры `_LEGACY_*` с `tools/legacy_audit._FORBIDDEN_*`.
  Сейчас они **содержат разные наборы**: например,
  `test_legal_summarizer_no_legacy._LEGACY_MODULES` включает
  `fingerprint`, `cached_retrieval`, `document_cache`,
  `brief_strategy`, `brief_representation`, `provenance_reconstruction`,
  `structure.cleanup`, которых **нет** в
  `tools/legacy_audit._FORBIDDEN_MODULES`. Это означает, что код,
  импортирующий эти legacy-модули, **проходит** `legacy_audit` guard,
  но **падает** на `test_legal_summarizer_no_legacy`. Два guard'а дают
  противоречивые сигналы.
* Решение: импортировать `_FORBIDDEN_*` из `tools.legacy_audit` в
  `test_legal_summarizer_no_legacy.py`. Один source of truth.

---

## 7. Необходимость единого registry

Текущее состояние **противоречит самому принципу compatibility cleanup**:
два разных файла определяют, что такое legacy, и эти определения
различаются.

* `tools/legacy_audit._FORBIDDEN_MODULES`: 7 модулей (без fingerprint,
  document_cache, brief_strategy и т.д.)
* `workspace/skills/legal_summarizer/tests/test_legal_summarizer_no_legacy._LEGACY_MODULES`:
  13 модулей (включая fingerprint, document_cache, brief_strategy,
  provenance_reconstruction, structure.cleanup).
* `tools/legacy_audit._ALLOWED_LEGACY_FILES` (после ремонта → `_ALLOWED_LEGACY_TESTS`):
  broad whole-file allow-list.
* `_ALLOWED_LEGACY_FILES` **включает** тестовые файлы, которые сами
  содержат реестры legacy — это циклическая зависимость.

**Правильное решение (Этап B):**

* Сделать **один canonical registry** в `tools/legacy_audit.py`:
  единые `FORBIDDEN_MODULES`, `FORBIDDEN_SYMBOLS`, `FORBIDDEN_FILES`,
  `LEGACY_CONFIG_KEYS`.
* `workspace/skills/legal_summarizer/tests/test_legal_summarizer_no_legacy.py`
  импортирует эти реестры напрямую.
* `tests/test_no_legacy_imports.py` тоже использует тот же registry.
* Один source of truth = один ответ на вопрос «что такое legacy?».

---

## 8. Критерии завершения compatibility cleanup

`production legacy hits == 0` — **недостаточный** критерий.

Необходимы **четыре независимых проверки**:

1. **Canonical production path has no forbidden dependencies.**
   `tools/legacy_audit.assert_no_legacy()` returns 0 production hits.

2. **Forbidden legacy modules/files do not exist.**
   `_FORBIDDEN_FILES` не существуют на диске.

3. **Tests do not depend on removed implementations.**
   **Полный** `pytest tests/ workspace/skills/legal_summarizer/tests/`
   проходит **без ImportError на удалённые модули**. Это требование
   **нарушается** сейчас (84 failures в characterization/acceptance
   тестах) и должно быть исправлено до объявления cleanup завершённым.

4. **Inventory and code agree.**
   Каждый C-ID в `COMPATIBILITY_INVENTORY.md` имеет ровно один status,
   согласованный с реальным кодом. Status «keep (canonical)» означает,
   что модуль действительно является canonical API, а не facade.

Только когда **все четыре** критерия выполнены, можно объявить
compatibility stage завершённым.

---

## 9. Следующие шаги

* ✅ **Этап 1:** inventory создан.
* ✅ **Этап 2:** architecture guards расширены на весь проект
  (`tests/test_no_legacy_imports.py`).
* ✅ **Этап 3:** C-001, C-002, C-003, C-004, C-009 удалены.
* ✅ **Этап 8:** `tools/repoint_legal_summarizer_tests.py` удалён.
* **Этап B:** расширить `_FORBIDDEN_*` в `tools/legacy_audit.py`
  на полный набор из `test_legal_summarizer_no_legacy.py`.
* **Этап C:** сузить `_ALLOWED_LEGACY_FILES` до
  `_ALLOWED_LEGACY_TESTS` (test-level allow-list).
* **Этап D:** исправить `test_acceptance_matrix.py::REQUIRED_MODULES`.
* **Этап E:** удалить сломанные characterization тесты
  в `test_skill_legal_summarizer_characterization.py`.
* **Этап F:** унифицировать реестры
  `test_legal_summarizer_no_legacy.py` с `tools/legacy_audit.py`.
* **Этап 7 (отдельный):** RuntimePatcher review
  (см. `docs/architecture/runtime-patcher-inventory.md`).
* **Этап 5 (после F):** возможный cleanup C-005 (build_vectors
  импорт `get_embedding` напрямую из `cache_provider_impl`).

---

## 10. Изменения документа

| Дата | Изменение |
|------|-----------|
| 2026-09-09 | Первичная инвентаризация после Этапов 1–50 в `legal_summarizer`. Production-legacy hits = 0. |
| 2026-09-09 | **Этап 2:** guards расширены на весь проект; `_ALLOWED_LEGACY_FILES` добавлен для characterization tests. |
| 2026-09-09 | **Этап 3:** C-001, C-002, C-003, C-004, C-009 удалены. |
| 2026-09-09 | **Этап 4:** characterization тесты добавлены в `_ALLOWED_LEGACY_FILES`. |
| 2026-09-09 | **Этап 8:** `tools/repoint_legal_summarizer_tests.py` удалён (C-015). |
| 2026-09-09 | **Этап A (remediation):** исправлены три противоречия — (1) C-008 окончательно `keep (canonical)`, не `migrate`; (2) summary исправлен: 6 removed, 4 keep (canonical), 4 keep (intentional shim), 3 keep (guard), 2 broken characterization; (3) `_ALLOWED_LEGACY_FILES` помечен для ремонта до test-level. **Добавлена секция §6 «Broken characterization tests» с описанием 84 падающих тестов.** Добавлены секции §7 (единый registry) и §8 (4 критерия завершения). |
