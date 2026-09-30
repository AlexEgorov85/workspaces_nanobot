# Work brief `10-skill-legal-a`

Product files: **31**, LOC: **7172**

## `workspace/skills/legal_summarizer/scripts/execution/map_reduce.py` — 738 LOC (code 651)
- module: `workspace.skills.legal_summarizer.scripts.execution.map_reduce`
- docstring: Map-reduce execution strategy: batching + LLM calls + reduce. Фактическая реализация ``map_reduce`` strategy, извлечённая из ``application.execution_orchestration``. Архитектурный контракт: * ``execution`` НЕ импортирует
- static importers (2): `workspace/skills/legal_summarizer/scripts/application/execution_orchestration.py`, `workspace/skills/legal_summarizer/scripts/application/service.py`
- string/dynamic refs: 3
- test files touching it: — none —
- classes: 0, module functions: 7

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_assert_invariants` | 70-90 | `(plan: ExecutionPlan, chunks: list[Chunk], final_batches: list[list[Chunk]]) -> None` | 6 | 1 | Проверить инварианты Phase 2B между plan и chunks. |
| `_queued_batches` | 93-114 | `(final_batches: list[list[Chunk]], chunk_states: dict[str, dict[str, Any]]) -> list[tuple[` | 6 | 1 | Список ``(batch_id, pending_chunks, total_chunks_in_batch)``. |
| `_run_all_batches` | 117-147 | `(queued: list[tuple[str, list[Chunk], int]], *, chunks_total: int, struct: DocumentStructu` | 2 | 1 | Запустить все queued батчи через ``run_one_batch_async`` (concurrency=1). |
| `_persist_batch_results` | 150-255 | `(queued: list[tuple[str, list[Chunk], int]], gather_results: list[tuple[str, dict | None, ` | 13 | 1 | Cache writes per-chunk + собрать stats. ``write_chunk_result`` инжектируется из application — это соблюдение ` |
| `_reduce_phase` | 258-433 | `(*, strategy: str, struct: DocumentStructure | None, section_ids: list[str], section_headi` | 19 | 1 | Reduce phase: hierarchical или flat. Возвращает ``(final_summary, section_reduce_calls, document_reduce_calls, |
| `_build_initial_partials_from_cache` | 436-453 | `(chunk_states: dict[str, dict[str, Any]], cached_partials: dict[str, str]) -> dict[str, di` | 14 | 1 | Build chunk_states entries для уже-cached chunks. |
| `run_map_reduce_execution` | 456-732 | `(chunks: list, *, plan: ExecutionPlan | None, strategy: str, length: str, focus: str | Non` | 35 | 2 | Фактическая реализация map-reduce execution. Pure execution: возвращает dict в shape ``application.service.run |

## `workspace/skills/legal_summarizer/scripts/chunking/chunker.py` — 623 LOC (code 525)
- module: `workspace.skills.legal_summarizer.scripts.chunking.chunker`
- docstring: DocumentStructure-aware chunker. Chunker, который использует ``DocumentStructure`` как единственный источник section info. Ключевые правила (STRUCTURAL_PACKING_PLAN v3): * ``DocumentStructure`` — единственный источник se
- static importers (2): `workspace/skills/legal_summarizer/scripts/application/brief_context.py`, `workspace/skills/legal_summarizer/scripts/application/pipeline_structure.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 3, module functions: 9

### class `DocumentStructureChunkerConfig` — lines 48-54 (7 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Параметры chunker'а, работающего с DocumentStructure.
- name referenced in 7 file(s); tests: 5

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `ChunkingDiagnostics` — lines 560-595 (36 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Диагностика chunking'а (STRUCTURAL_PACKING_PLAN §4). Attributes: physical_blocks: суммарное число physical blocks в chunks. sections: число section nodes в DocumentStructure. chunks: всего chunks создано. special_blocks:
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `ChunkPlanner` — lines 609-623 (15 LOC), 2 methods
- bases: object
- decorators: —
- docstring: ChunkPlanner использует ``DocumentStructure`` как SoT. Не переопределяет structure.
- name referenced in 4 file(s); tests: 2

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 615-616 | `(self, *, config: DocumentStructureChunkerConfig | None=None) -> None` | 2 | 0 | 6 | — |
| `plan` | 618-623 | `(self, doc: PhysicalDocument, struct: DocumentStructure) -> list[Chunk]` | 1 | 0 | 20 | — |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `build_chunk_config_from_runtime` | 57-121 | `(context_window_tokens: int | None=None) -> ChunkConfig` | 13 | 1 | Построить ``ChunkConfig`` из runtime-конфига. Приоритет источников для ``max_chunk_chars``: 1. Если передан `` |
| `_make_chunk_id` | 124-125 | `(idx: int) -> str` | 1 | 1 | — |
| `_section_path_for` | 128-141 | `(node_id: str, struct: DocumentStructure) -> str` | 10 | 1 | — |
| `_ancestor_chain_titles` | 144-171 | `(node_id: str, struct: DocumentStructure) -> list[str]` | 10 | 1 | Ancestor chain от root до node_id в формате ['Title1', 'Title2', ...]. Каждый элемент — это title родительског |
| `_build_context_preamble` | 174-206 | `(unit: 'PackableUnit', struct: DocumentStructure, cache: dict[str, str]) -> str` | 5 | 1 | Строит короткую преамбулу для chunk'а в формате: [Контекст: Doc Title > Parent1 > Parent2] Добавляется в начал |
| `_is_strong_boundary` | 209-226 | `(prev_owner_id: str, nxt_owner_id: str, struct: DocumentStructure) -> bool` | 9 | 0 | DEPRECATED: оставлен для back-compat, используйте ``structural_packing._is_strong_boundary_units``. |
| `chunk_from_structure` | 229-480 | `(doc: PhysicalDocument, struct: DocumentStructure, *, config: DocumentStructureChunkerConf` | 39 | 8 | Создать ``Chunk``-и из ``PhysicalDocument`` + ``DocumentStructure``. Алгоритм (hierarchical structural packing |
| `_collect_owner_section_ids` | 483-496 | `(block_indices: tuple[int, ...], ownership: dict[int, str], root_id: str) -> tuple[str, ..` | 5 | 1 | Уникальные owner'ы blocks в document order, исключая root_id. |
| `chunk_from_structure_with_diagnostics` | 499-556 | `(doc: PhysicalDocument, struct: DocumentStructure, *, config: DocumentStructureChunkerConf` | 27 | 1 | Создать chunks + вернуть ``ChunkingDiagnostics``. Diagnostics собирается по результату chunking'а и не влияет  |

## `workspace/skills/legal_summarizer/scripts/application/service.py` — 514 LOC (code 446)
- module: `workspace.skills.legal_summarizer.scripts.application.service`
- docstring: Orchestration facade для legal_summarizer. Public entry points: * ``run`` — canonical execution (idempotency → inspect → context → confirmation → execute). * ``inspect`` — document-level snapshot через ``run_canonical_pi
- static importers (1): `workspace/skills/legal_summarizer/scripts/cli.py`
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 0, module functions: 2

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_try_question_via_document_cache` | 78-291 | `(*, question: str, text: str, document_path: str, workspace_root: Path | str, operation_id` | 24 | 1 | Shortcut для ``--question`` через document-level cache. При успехе возвращает ``dict`` в shape ``service.run() |
| `run` | 294-491 | `(text: str, *, length: str='brief', focus: str | None=None, question: str | None=None, con` | 28 | 100 | Canonical execution path. Порядок: 1. ``resolve operation_id`` (детерминированно из text+length+path+question) |

## `workspace/skills/legal_summarizer/scripts/application/brief_context.py` — 509 LOC (code 419)
- module: `workspace.skills.legal_summarizer.scripts.application.brief_context`
- docstring: BriefContextBuilder: один структурный Chunk для всего документа. BRIEF CONTRACT: один документ → ровно один Chunk. Brief не является выборкой canonical chunks. Brief является компактным структурным представлением всего д
- static importers (1): `workspace/skills/legal_summarizer/scripts/application/chunk_selection.py`
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 1, module functions: 11

### class `BriefContextConfig` — lines 90-110 (21 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Параметры BriefContextBuilder. Attributes: max_chars_fallback: жёсткий потолок ``chunk.text``, используемый только если контекстное окно модели недоступно (см. ``resolve_max_chars``). input_ratio: доля контекстного окна 
- name referenced in 3 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_resolve_context_window_tokens` | 113-138 | `() -> int | None` | 10 | 1 | Прочитать ``contextWindowTokens`` из SETTINGS. Источник: ``config.json::agents.defaults.contextWindowTokens``  |
| `resolve_max_chars` | 141-158 | `(config: BriefContextConfig) -> int` | 8 | 2 | Динамически рассчитать ``max_chars`` (п.18). Формула: ``max_chars = contextWindowTokens * input_ratio * chars_ |
| `_node_label` | 161-165 | `(node: StructureNode) -> str` | 2 | 1 | Человекочитаемый label структурного узла для outline. |
| `_collect_subtree_ordinals` | 168-195 | `(node: StructureNode, struct: DocumentStructure) -> list[int]` | 6 | 1 | Собрать physical ordinals всех блоков в subtree ``node``. Обход в pre-order по ``children``. Для каждого узла  |
| `_render_outline` | 198-252 | `(struct: DocumentStructure, *, max_chars: int) -> str` | 21 | 1 | DOCUMENT STRUCTURE: рекурсивный outline всех значимых узлов. Не ограничивает глубину — структура должна быть м |
| `_trim_structure` | 255-264 | `(text: str, *, max_chars: int) -> str` | 4 | 1 | Обрезать outline до ``max_chars`` по newline-boundary. |
| `_select_meaningful_level` | 267-306 | `(struct: DocumentStructure) -> list[StructureNode]` | 13 | 1 | Определить верхний содержательный уровень для brief. Алгоритм (п.6): 1. Непосредственные дети root, имеющие `` |
| `_preamble_ordinals` | 309-323 | `(struct: DocumentStructure, top_level_nodes: list[StructureNode]) -> list[int]` | 5 | 1 | Physical ordinals, попадающие в preamble (до первого top-level узла). Preamble = всё, что между ``0`` и ``min( |
| `_render_section_content` | 326-346 | `(node: StructureNode, struct: DocumentStructure, blocks_by_ord: dict[int, DocumentBlock]) ` | 6 | 1 | Собрать весь текст subtree ``node`` в document order. Таблицы (block_type == 'table') включаются атомарно (п.1 |
| `build_brief_chunk` | 349-467 | `(analysis: DocumentAnalysis, *, config: BriefContextConfig | None=None) -> Chunk` | 27 | 2 | Построить ровно один ``Chunk`` — компактное структурное представление всего документа. Args: analysis: ``Docum |
| `_validate_blocks` | 470-502 | `(blocks_by_ord: dict[int, DocumentBlock], *, expected_total: int) -> None` | 7 | 1 | П.29: ordinal существует, не повторяется, идёт в document order. Здесь проверяем: каждый ordinal в ``[0, expec |

## `workspace/skills/legal_summarizer/scripts/application/execution_orchestration.py` — 489 LOC (code 418)
- module: `workspace.skills.legal_summarizer.scripts.application.execution_orchestration`
- docstring: Execution orchestration: координатор ``run_direct`` / ``run_map_reduce``. Тонкая прослойка application layer: * выбирает стратегию (``direct`` vs ``map_reduce``); * делегирует фактическую работу в ``execution/map_reduce`
- static importers (1): `workspace/skills/legal_summarizer/scripts/application/service.py`
- string/dynamic refs: 2
- test files touching it: — none —
- classes: 0, module functions: 4

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `map_plan_to_chunk_batches` | 55-83 | `(plan: ExecutionPlan, chunks: list) -> list[list[Chunk]]` | 7 | 7 | Преобразовать ``ExecutionPlan`` в список батчей chunks. Единая точка маппинта plan→chunks для ``run_map_reduce |
| `run_direct` | 86-222 | `(chunks: list, *, length: str, focus: str | None, question: str | None, structure: Documen` | 18 | 1 | Canonical direct execution: single llm_document_reduce call. |
| `run_map_reduce` | 225-380 | `(chunks: list, *, plan: ExecutionPlan | None, strategy: str, length: str, focus: str | Non` | 18 | 3 | Canonical map_reduce: coordinator. Вычисляет ``section_index`` / ``sections_payload`` (document-level helpers) |
| `_persist_final_manifest` | 383-482 | `(*, payload: dict, document_path: str | None, structure: DocumentStructure | None, analysi` | 15 | 1 | Сохранить финальный manifest на диск. Извлекает ``_internal`` из ``payload.stats`` и сохраняет ``NormalizedMan |

## `workspace/skills/legal_summarizer/scripts/cache/document_cache.py` — 484 LOC (code 370)
- module: `workspace.skills.legal_summarizer.scripts.cache.document_cache`
- docstring: DocumentCache — единственный владелец document-level cache protocol. Ответственность (жёсткая граница, см. ``docs/TARGET_ARCHITECTURE.md``): * хранение snapshot'а (``physical.json``, ``analysis.json``, ``retrieval_index.
- static importers (4): `workspace/skills/legal_summarizer/scripts/application/execution_orchestration.py`, `workspace/skills/legal_summarizer/scripts/application/pipeline_structure.py`, `workspace/skills/legal_summarizer/scripts/application/question_context.py`, `workspace/skills/legal_summarizer/scripts/application/service.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 4

### class `DocumentCache` — lines 110-484 (375 LOC), 16 methods
- bases: object
- decorators: —
- docstring: Canonical API document-level cache. Instance инкапсулирует общий cache configuration (``workspace_root``, ``session_key``) — caller создаёт ``DocumentCache`` один раз в начале запроса и переиспользует для всех операций. 
- name referenced in 13 file(s); tests: 9

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 122-128 | `(self, workspace_root: Path | str | None, session_key: str='default') -> None` | 2 | 0 | 6 | — |
| `_document_dir` | 134-136 | `(self, document_id: str) -> Path` | 1 | 6 | 6 | Корневая папка document cache. **Private** — не для production. |
| `_marker_path` | 138-140 | `(self, document_id: str) -> Path` | 1 | 1 | 2 | Путь к ``_complete.marker``. **Private** — не для production. |
| `is_complete` | 146-152 | `(self, document_id: str) -> bool` | 1 | 2 | 9 | ``True`` если snapshot существует и marker на месте. Дешёвая проверка (stat по marker'у). Не читает payload —  |
| `write_snapshot` | 158-265 | `(self, *, document_id: str, physical_data: dict[str, Any], analysis_data: dict[str, Any], ` | 8 | 0 | 3 | Атомарная запись document-level snapshot'а. Concurrency contract: * если ``target_dir`` уже **complete** (mark |
| `_evict_orphan_siblings` | 267-318 | `(self, target_dir: Path, physical_data: dict[str, Any]) -> None` | 13 | 1 | 1 | Удалить snapshot'ы, соответствующие старым версиям того же файла. Orphan sibling = каталог в ``documents_root` |
| `read_snapshot` | 320-343 | `(self, document_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, ` | 2 | 0 | 4 | Прочитать document-level snapshot. Returns: ``(physical, analysis, retrieval_meta)`` или ``None`` если snapsho |
| `invalidate` | 345-358 | `(self, document_id: str) -> None` | 2 | 0 | 3 | Удалить document-level snapshot целиком. Безопасно вызывать на несуществующем каталоге (no-op). Используется п |
| `_chunk_path` | 364-365 | `(self, document_id: str, chunk_id: str) -> Path` | 1 | 2 | 1 | — |
| `write_chunk_summary` | 367-405 | `(self, *, document_id: str, chunk_id: str, summary: str, section_id: str | None=None, sect` | 5 | 0 | 3 | Записать per-chunk LLM summary в document-level cache. document-level cache хранит ТОЛЬКО question-independent |
| `_read_chunk_summary` | 407-412 | `(self, document_id: str, chunk_id: str) -> dict[str, Any] | None` | 1 | 1 | 1 | — |
| `load_chunk_summaries` | 414-429 | `(self, document_id: str, expected_chunk_ids: list[str]) -> dict[str, str]` | 5 | 0 | 4 | Загрузить per-chunk summaries из document-level cache. Cross-operation lookup — не привязан к ``operation_id`` |
| `_section_path` | 435-436 | `(self, document_id: str, section_id: str) -> Path` | 1 | 2 | 1 | — |
| `write_section_summary` | 438-464 | `(self, *, document_id: str, section_id: str, summary: str, question: str | None=None) -> N` | 5 | 0 | 5 | Записать per-section LLM summary в document-level cache. document-level cache хранит ТОЛЬКО question-independe |
| `_read_section_summary` | 466-471 | `(self, document_id: str, section_id: str) -> dict[str, Any] | None` | 1 | 1 | 1 | — |
| `load_section_summaries` | 473-484 | `(self, document_id: str, expected_section_ids: list[str]) -> dict[str, str]` | 5 | 0 | 4 | Загрузить per-section summaries из document-level cache. |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_atomic_write_json` | 66-74 | `(path: Path, payload: dict[str, Any]) -> None` | 1 | 3 | Атомарная запись JSON: tmp + replace. |
| `_read_json` | 77-84 | `(path: Path) -> dict[str, Any] | None` | 3 | 5 | Прочитать JSON; вернуть ``None`` если файла нет или он битый. |
| `_skill_repo_root` | 87-93 | `() -> Path` | 1 | 1 | Стабильный абсолютный корень репо, выведенный из расположения этого файла. ``<repo>/workspace/skills/legal_sum |
| `_cache_root` | 96-107 | `(workspace_root: Path | str | None, session_key: str) -> Path` | 3 | 2 | Корень document-level cache для конкретной сессии. ``<repo>/workspace/data_store/cache/sessions/<safe_session_ |

## `workspace/skills/legal_summarizer/scripts/chunking/structural_packing.py` — 471 LOC (code 399)
- module: `workspace.skills.legal_summarizer.scripts.chunking.structural_packing`
- docstring: Hierarchical structural packing для legal_summarizer. Алгоритм (Phase 2): 1. Tables → atomic chunks (каждый table block — свой chunk). 2. Oversized blocks → split через ``_split_block_with_offsets``. 3. Recursive descent
- static importers (1): `workspace/skills/legal_summarizer/scripts/chunking/chunker.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 10

### class `PackableUnit` — lines 41-65 (25 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Внутренняя единица packing'а. Не экспортируется в public API. Attributes: kind: "structural" | "table" | "oversized_part" | "root". block_indices: конкретные ordinals блоков (отсортированы). section_ids: все section node
- name referenced in 2 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_node_subtree_range` | 68-85 | `(node_id: str, struct: DocumentStructure) -> tuple[int, int]` | 4 | 1 | Subtree physical range для node'а. Учитывает direct range самого node + диапазоны всех descendants. |
| `_node_has_specials` | 88-106 | `(node_id: str, struct: DocumentStructure, by_ord: dict[int, DocumentBlock], max_chunk_char` | 5 | 0 | True, если subtree содержит oversized block (>max_chunk_chars). Tables — обычные блоки (atomic, но не special) |
| `_direct_blocks_for_node` | 109-138 | `(node_id: str, struct: DocumentStructure, by_ord: dict[int, DocumentBlock], max_chunk_char` | 9 | 1 | Direct blocks node'а, не покрытые ни одним descendant. Это blocks, у которых owner == node_id, но которые не в |
| `_collect_section_ids_for_range` | 141-154 | `(block_indices: tuple[int, ...], ownership: dict[int, str], root_id: str) -> tuple[str, ..` | 5 | 1 | Уникальные owner'ы blocks в document order, исключая root_id. |
| `_build_units_for_node` | 157-310 | `(node_id: str, struct: DocumentStructure, by_ord: dict[int, DocumentBlock], ownership: dic` | 34 | 1 | Recursive descent: subtree целиком или раскрытие на children. Tables — обычные блоки (atomic по построению: од |
| `_is_strong_boundary_units` | 313-331 | `(prev: PackableUnit, nxt: PackableUnit, struct: DocumentStructure) -> bool` | 9 | 1 | True, если prev→nxt — major→major переход между разными primary section. |
| `_merge_units` | 334-350 | `(prev: PackableUnit, nxt: PackableUnit) -> PackableUnit` | 1 | 1 | Объединить два соседних unit'а (structural или table inline). primary_section_id остаётся от prev (back-compat |
| `_is_consecutive` | 353-369 | `(prev: PackableUnit, nxt: PackableUnit) -> bool` | 3 | 0 | True, если nxt находится ВНУТРИ диапазона prev или сразу после. Используется для small_table inline: таблица i |
| `greedy_pack_units` | 372-444 | `(units: list[PackableUnit], *, target_chunk_chars: int, max_chunk_chars: int, preferred_mi` | 13 | 1 | Greedy packing unit'ов с плотным заполнением chunks. Алгоритм: 1. **Oversized_part**: всегда atomic. 2. **Разн |
| `build_packable_units` | 447-471 | `(doc: PhysicalDocument, struct: DocumentStructure, ownership: dict[int, str], *, target_ch` | 2 | 1 | Public entry point: построить упорядоченные packable units. Использует hierarchical descent по DocumentStructu |

## `workspace/skills/legal_summarizer/scripts/chunking/chunks.py` — 394 LOC (code 349)
- module: `workspace.skills.legal_summarizer.scripts.chunking.chunks`
- docstring: Structure-Aware Chunker для legal_summarizer. Преобразует ``DocumentBlock[]`` + ``SectionTree`` в ``Chunk[]`` с сохранением: * ``section_id`` / ``section_path`` / ``section_heading`` * ``page_start`` / ``page_end`` * ``b
- static importers (26): `workspace/skills/legal_summarizer/scripts/application/brief_context.py`, `workspace/skills/legal_summarizer/scripts/application/context_builder.py`, `workspace/skills/legal_summarizer/scripts/application/estimation.py`, `workspace/skills/legal_summarizer/scripts/application/execution_orchestration.py`, `workspace/skills/legal_summarizer/scripts/application/pipeline_structure.py`, `workspace/skills/legal_summarizer/scripts/application/question_context.py`, `workspace/skills/legal_summarizer/scripts/application/service.py`, `workspace/skills/legal_summarizer/scripts/chunking/_text_helpers.py`, `workspace/skills/legal_summarizer/scripts/chunking/chunker.py`, `workspace/skills/legal_summarizer/scripts/chunking/importance_score.py`, `workspace/skills/legal_summarizer/scripts/chunking/order.py`, `workspace/skills/legal_summarizer/scripts/chunking/packing.py`, `workspace/skills/legal_summarizer/scripts/chunking/structural_packing.py`, `workspace/skills/legal_summarizer/scripts/document/analysis.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 2, module functions: 4

### class `Chunk` — lines 90-270 (181 LOC), 2 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Результат structure-aware chunker'а. Attributes: chunk_id: ``"001"``, ``"002"``, ... (zero-padded width=3). index: 0..N-1, document order. text: содержимое. char_count: ``len(text)``. token_estimate: ``ceil(char_count / 
- name referenced in 50 file(s); tests: 22

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `to_dict` | 147-182 | `(self) -> dict[str, Any]` | 3 | 0 | 24 | — |
| `from_dict` | 185-270 | `(cls, data: dict[str, Any]) -> 'Chunk'` | 41 | 0 | 6 | Обратная сериализация для ``to_dict``. Используется при восстановлении ``DocumentAnalysis`` из document-level  |

### class `ChunkConfig` — lines 274-284 (11 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Параметры chunker'а.
- name referenced in 6 file(s); tests: 5

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_split_block_with_offsets` | 32-86 | `(text: str, *, chunk_size: int, chunk_overlap: int) -> list[tuple[str, int, int]]` | 13 | 1 | Разбить текст block'а на подчасти и сохранить абсолютные offsets. Возвращает список ``(part_text, char_start,  |
| `_make_table_chunk_text` | 287-289 | `(rows: list[str], row_start: int, row_end: int) -> str` | 2 | 0 | — |
| `_split_table_into_chunks` | 292-318 | `(table_text: str, rows_total: int, threshold_chars: int) -> list[tuple[str, int, int]]` | 8 | 0 | Split a table by rows into multiple chunks, each ≤ threshold. Returns: list of (text, row_start, row_end). All |
| `reconstruct_source_fragment` | 322-387 | `(chunk: Chunk, *, doc: PhysicalDocument | None=None, blocks: tuple[DocumentBlock, ...] | N` | 20 | 2 | Восстановить точный исходный текст чанка из PhysicalDocument. Контракт: * ``block_indices`` — отсортированные  |

## `workspace/skills/legal_summarizer/scripts/cache/manifest.py` — 388 LOC (code 321)
- module: `workspace.skills.legal_summarizer.scripts.cache.manifest`
- docstring: Manifest v2 для legal_summarizer Phase 2B. Manifest хранит source of truth для resume (invariant #12, #13, #14): * ``chunk_states`` — per-chunk state (status, section_id, page range, ...) * ``context_batches`` — list of 
- static importers (5): `workspace/skills/legal_summarizer/scripts/application/execution_orchestration.py`, `workspace/skills/legal_summarizer/scripts/application/manifest_builder.py`, `workspace/skills/legal_summarizer/scripts/application/service.py`, `workspace/skills/legal_summarizer/scripts/cli_query.py`, `workspace/skills/legal_summarizer/scripts/document/physical.py`
- string/dynamic refs: 2
- test files touching it: — none —
- classes: 1, module functions: 20

### class `NormalizedManifest` — lines 142-194 (53 LOC), 1 methods
- bases: object
- decorators: dataclass
- docstring: Унифицированное представление manifest'а в формате v2.
- name referenced in 5 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `to_dict` | 169-194 | `(self) -> dict[str, Any]` | 2 | 0 | 24 | — |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_atomic_write_json` | 32-39 | `(path: Path, payload: dict[str, Any]) -> None` | 1 | 3 | — |
| `_read_json` | 42-48 | `(path: Path) -> dict[str, Any] | None` | 3 | 5 | — |
| `_read_json_strict` | 51-71 | `(path: Path) -> tuple[dict[str, Any] | None, bool]` | 4 | 1 | Прочитать JSON и отличить «файла нет» / «битый JSON» / «успех». Возвращает ``(data, is_file_present)``: * ``(N |
| `_coerce_version_int` | 74-90 | `(raw: dict[str, Any]) -> int | None` | 4 | 1 | Прочитать ``raw["version"]`` как int, если это возможно. Возвращает ``None`` если поля нет или оно не coerce'и |
| `diagnose_manifest` | 93-138 | `(operation_id: str, workspace_root: Path | str | None=None) -> dict[str, Any]` | 4 | 2 | Диагностика manifest для IPC-слоя ``cli_query.py``. Возвращает dict с полями: * ``reason``: один из ``"ok"`` / |
| `skill_repo_root` | 197-203 | `() -> Path` | 1 | 1 | Корень репозитория, выведенный из расположения этого скрипта. Модуль лежит по пути ``<repo>/workspace/skills/l |
| `manifest_root` | 206-215 | `(workspace_root: Path | str | None) -> Path` | 2 | 3 | Корень для manifest'ов/chunks/result skill'а (operation-level). ``workspace_root`` — корень РЕПО (не workspace |
| `manifest_path` | 218-219 | `(operation_id: str, workspace_root: Path | str | None=None) -> Path` | 1 | 8 | — |
| `chunks_dir` | 222-223 | `(operation_id: str, workspace_root: Path | str | None=None) -> Path` | 1 | 4 | — |
| `chunk_result_path` | 226-231 | `(operation_id: str, chunk_id: str, workspace_root: Path | str | None=None) -> Path` | 1 | 3 | — |
| `result_path` | 234-235 | `(operation_id: str, workspace_root: Path | str | None=None) -> Path` | 1 | 1 | — |
| `_detect_version` | 238-250 | `(raw: dict[str, Any]) -> int | None` | 6 | 1 | Вернуть ``MANIFEST_VERSION_V2`` только для v2 manifest, иначе ``None``. Legacy v1 manifest не поддерживается ( |
| `_normalize_v2` | 253-278 | `(raw: dict[str, Any]) -> NormalizedManifest` | 33 | 1 | — |
| `load_manifest` | 281-292 | `(operation_id: str, workspace_root: Path | str | None=None) -> NormalizedManifest | None` | 3 | 9 | Прочитать manifest.json и нормализовать к формату v2 in-memory. |
| `save_manifest` | 295-302 | `(normalized: NormalizedManifest, workspace_root: Path | str | None=None) -> None` | 1 | 3 | Записать manifest в формате v2 на диск. |
| `write_chunk_result` | 305-329 | `(operation_id: str, chunk_id: str, summary: str, *, context_batch_id: str | None, section_` | 1 | 3 | Сохранить per-chunk partial на диск (operation-level). |
| `read_chunk_result` | 332-338 | `(operation_id: str, chunk_id: str, workspace_root: Path | str | None=None) -> dict[str, An` | 1 | 3 | Прочитать per-chunk partial. None если файла нет. |
| `write_result` | 341-347 | `(operation_id: str, result: dict[str, Any], workspace_root: Path | str | None=None) -> Non` | 1 | 3 | Сохранить финальный result.json. |
| `read_result` | 350-354 | `(operation_id: str, workspace_root: Path | str | None=None) -> dict[str, Any] | None` | 1 | 2 | — |
| `load_cached_partials` | 357-368 | `(operation_id: str, expected_chunk_ids: list[str], workspace_root: Path | str | None) -> d` | 5 | 1 | Загрузить per-chunk summary из disk-манифеста (operation/chunks/*.json). |

## `workspace/skills/legal_summarizer/scripts/application/pipeline_structure.py` — 352 LOC (code 298)
- module: `workspace.skills.legal_summarizer.scripts.application.pipeline_structure`
- docstring: DocumentStructure как SoT для всех downstream'ов. Этот модуль — **точка сборки** canonical pipeline: file → DocumentLoader → DocumentIdentity → DocumentStructure → repair → validate → ChunkPlanner → DocumentAnalysis → Ex
- static importers (3): `workspace/skills/legal_summarizer/scripts/application/canonical.py`, `workspace/skills/legal_summarizer/scripts/application/inspection.py`, `workspace/skills/legal_summarizer/scripts/application/service.py`
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 1, module functions: 4

### class `PipelineResult` — lines 100-105 (6 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Полный результат canonical pipeline.
- name referenced in 2 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_read_context_window_tokens` | 69-96 | `() -> int | None` | 10 | 1 | Прочитать ``contextWindowTokens`` из SETTINGS. Источник: ``config.json::agents.defaults.contextWindowTokens``  |
| `_try_load_cached_pipeline_result` | 108-187 | `(*, path: str | Path, workspace_root: Path | str | None, session_key: str='default', inclu` | 11 | 1 | Попробовать загрузить cached ``PipelineResult`` из document-level cache. Условия cache hit: * ``workspace_root |
| `_write_document_snapshot_after_pipeline` | 190-235 | `(*, path: str | Path, workspace_root: Path | str | None, physical: PhysicalDocument, ident` | 5 | 1 | Сохранить document-level snapshot после успешного canonical pipeline. Используется только при cache miss. При  |
| `run_canonical_pipeline` | 238-349 | `(path: str | Path, *, text: str | None=None, apply_repair: bool=True, include_retrieval_in` | 5 | 13 | Запустить canonical pipeline. При наличии document-level cache — попытка cache hit: если файл не менялся (mtim |

## `workspace/skills/legal_summarizer/scripts/execution/hierarchical.py` — 294 LOC (code 250)
- module: `workspace.skills.legal_summarizer.scripts.execution.hierarchical`
- docstring: All reduce functions (canonical copy in execution layer).
- static importers (1): `workspace/skills/legal_summarizer/scripts/execution/map_reduce.py`
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 1, module functions: 4

### class `HierarchicalReducerResult` — lines 39-45 (7 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Результат HierarchicalReducer.reduce.
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `deterministic_truncate` | 11-30 | `(text: str, max_chars: int) -> str` | 4 | 2 | Deterministic head + tail truncate с omission marker. Используется как **emergency fallback** — нормальный пут |
| `_fit_input` | 33-35 | `(text: str, budget: int) -> str` | 1 | 1 | Обёртка над deterministic_truncate. |
| `reduce_sections_to_document` | 51-170 | `(section_summaries: list[tuple[str, str]], *, config: HierarchicalReducerConfig | None=Non` | 23 | 5 | Hierarchical reduce: section-level → rounds → final. Используется, когда section_summaries уже есть (follow-up |
| `reduce_chunks_hierarchical` | 173-293 | `(chunks: list, chunk_summaries: dict[str, str], *, section_ids: list[str], section_heading` | 27 | 4 | Hierarchical reduce: section-level + document-level. Используется когда chunks ещё не просуммированы. Диагност |

## `workspace/skills/legal_summarizer/scripts/application/question_context.py` — 258 LOC (code 209)
- module: `workspace.skills.legal_summarizer.scripts.application.question_context`
- docstring: Question synthesis context builder. Собирает LLM-вход для ``llm_document_reduce(question=...)`` из document-level cache без повторного map-вызова LLM. Три уровня (от cheap к deep): 1. **section_summary** — per-section LL
- static importers (1): `workspace/skills/legal_summarizer/scripts/application/service.py`
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 1, module functions: 3

### class `_FakeChunk` — lines 91-96 (6 LOC), 1 methods
- bases: object
- decorators: —
- docstring: — NONE —
- name referenced in 3 file(s); tests: 2

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 94-96 | `(self, *, chunk_id: str, section_heading: str) -> None` | 1 | 0 | 6 | — |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_format_section_summary_block` | 33-71 | `(*, chunk_id: str, section_id: str, section_path: str, section_heading: str, page_start: i` | 9 | 1 | Один chunk-блок для LLM-входа. Блок **всегда** содержит метаданные (section heading, / page range). Содержимое |
| `_summaries_block_text` | 74-88 | `(chunk: 'Chunk', *, section_summary: str | None, chunk_summary: str | None) -> str` | 4 | 1 | Возвращает текст summaries блока (без metadata header и source text). |
| `build_question_context` | 99-255 | `(selected_chunks: list[Chunk], *, document_id: str, workspace_root: str | None, budget_cha` | 35 | 2 | Собрать LLM-вход для question synthesis. Загружает section summaries (level 1) и chunk summaries (level 2) из  |

## `workspace/skills/legal_summarizer/scripts/application/canonical.py` — 244 LOC (code 205)
- module: `workspace.skills.legal_summarizer.scripts.application.canonical`
- docstring: Canonical run pipeline. Этот модуль — **production-flow**, использующий только canonical pipeline (``run_canonical_pipeline`` → ``DocumentAnalysis`` → ``HierarchicalReducer``). **Не содержит legacy adapters**. ``executio
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 7

### class `CanonicalInspection` — lines 108-125 (18 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Результат canonical inspection (аналог summarizer.Inspection). Attributes: chars_in: длина входного текста. chunks: список ``Chunk`` из ``ChunkPlanner``. structure: ``DocumentStructure`` (canonical semantic structure). s
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `build_pipeline_result` | 52-72 | `(*, document_path: str | Path, text: str | None=None, workspace_root: Path | str | None=No` | 1 | 5 | Запустить canonical pipeline и вернуть ``PipelineResult``. Параметр ``text`` — fallback для title resolution ( |
| `strategy_from_pipeline` | 75-89 | `(result: PipelineResult, *, policy: ExecutionPolicy | None=None) -> str` | 1 | 2 | Определить стратегию выполнения по ``PipelineResult``. Использует canonical ``select_strategy`` (один источник |
| `build_plan_from_pipeline` | 92-104 | `(result: PipelineResult, *, document_id: str, policy: ExecutionPolicy | None=None) -> Exec` | 1 | 2 | Построить ``ExecutionPlan`` из ``PipelineResult``. |
| `inspect_canonical` | 128-184 | `(text: str, document_path: str | Path | None=None, *, workspace_root: Path | str | None=No` | 5 | 4 | Canonical осмотр документа. Использует только canonical pipeline (без legacy ``StructureAwareChunker``, ``Sect |
| `estimate_canonical` | 187-202 | `(document_path: str | Path) -> dict[str, Any]` | 1 | 1 | Canonical оценка документа без полного parsing. Возвращает dict с chars_in, estimated_llm_calls, strategy. Не  |
| `estimate_chunks_canonical` | 205-214 | `(chunks: list) -> int` | 2 | 1 | Canonical оценка суммарных токенов для списка Chunk. Использует ``TokenEstimator``. Возвращает общее число ток |
| `pack_batches_canonical` | 217-232 | `(chunks: list, *, max_sections_per_batch: int=2, per_batch_token_budget: int=6000) -> list` | 1 | 1 | Canonical batch packing через adjacent-section policy. Использует ``pack_chunks_with_adjacent``. Возвращает сп |

## `workspace/skills/legal_summarizer/scripts/application/estimation.py` — 200 LOC (code 172)
- module: `workspace.skills.legal_summarizer.scripts.application.estimation`
- docstring: Estimation: верхняя граница LLM-вызовов и времени для конкретного запуска. Использует те же константы, что и ``execution/hierarchical.py`` (``MID_REDUCE_GROUP_SIZE``, ``MAX_REDUCE_ROUNDS``), чтобы ``actual_llm_calls <= e
- static importers (2): `workspace/skills/legal_summarizer/scripts/application/service.py`, `workspace/skills/legal_summarizer/scripts/cli.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 5

### class `Estimate` — lines 31-37 (7 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: — NONE —
- name referenced in 2 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `needs_confirmation` | 40-41 | `(est: Estimate) -> bool` | 1 | 2 | — |
| `count_execution_calls` | 44-67 | `(plan: ExecutionPlan | None, chunks: tuple[Chunk, ...], *, strategy: str, structure: Docum` | 5 | 1 | Верхняя граница (upper bound) числа LLM-вызовов для запуска. * ``direct`` / ``plan is None`` → 1 (один прямой  |
| `_hierarchical_reduce_calls` | 70-100 | `(sections: int) -> int` | 5 | 2 | Верхняя граница числа document-level LLM-вызовов в ``reduce_chunks_hierarchical`` (reduce-фаза после section r |
| `estimate_for_run` | 103-129 | `(insp, ctx) -> Estimate` | 2 | 7 | Estimate для конкретного run-context (run-level). ``estimated_llm_calls`` — верхняя граница (upper bound) факт |
| `quick_estimate` | 132-191 | `(path: Path | str) -> dict[str, Any]` | 19 | 3 | Быстрая оценка размера документа БЕЗ полного извлечения текста. |

## `workspace/skills/legal_summarizer/scripts/application/brief_compression.py` — 168 LOC (code 137)
- module: `workspace.skills.legal_summarizer.scripts.application.brief_compression`
- docstring: Детерминированная компрессия brief-секций. BRIEF CONTRACT: один документ → ровно один Chunk. Brief не выбирает canonical chunks. Brief собирает одно структурное представление всего документа и при нехватке места **сокращ
- static importers (1): `workspace/skills/legal_summarizer/scripts/application/brief_context.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 3

### class `BriefSection` — lines 34-47 (14 LOC), 1 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Один раздел документа для финального brief-context. Attributes: heading: заголовок раздела (используется как heading и как fallback marker, если раздел пришлось сильно урезать). text: полный текст раздела (до компрессии)
- name referenced in 3 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `char_count` | 46-47 | `(self) -> int` | 1 | 0 | 14 | — |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_truncate_text_safely` | 54-73 | `(text: str, max_chars: int) -> str` | 6 | 1 | Обрезать ``text`` до ``max_chars`` по безопасной границе. П.15: paragraph → newline → sentence → word → hard c |
| `allocate_budget` | 76-120 | `(sections: list[BriefSection], *, available_chars: int, min_section_chars: int=80) -> list` | 15 | 2 | Распределить ``available_chars`` между секциями weighted-allocation. Returns: список кортежей ``(section, fina |
| `render_sections` | 123-160 | `(sections: list[BriefSection], *, available_chars: int, min_section_chars: int=80) -> str` | 6 | 2 | Сжать и отрендерить секции в один текст с маркерами truncation. Формат каждой секции:: <heading> <text> П.13:  |

## `workspace/skills/legal_summarizer/scripts/chunking/packing.py` — 164 LOC (code 128)
- module: `workspace.skills.legal_summarizer.scripts.chunking.packing`
- docstring: Controlled adjacent-section packing. Сейчас ``packing_impl.pack_chunks`` строго section-locality greedy — что безопасно, но для 600-страничного документа даёт ``map_calls == chunks_total`` (см. baseline F3). Целевая поли
- static importers (2): `workspace/skills/legal_summarizer/scripts/application/canonical.py`, `workspace/skills/legal_summarizer/scripts/planning/strategy.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 3

### class `AdjacentPackingConfig` — lines 34-49 (16 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Параметры adjacent-section packing. Attributes: max_sections_per_batch: максимум distinct ``section_id`` в batch. per_batch_token_budget: token budget на batch. chars_per_token: коэффициент для TokenEstimator. allow_tabl
- name referenced in 5 file(s); tests: 2

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_section_id_for_chunk` | 52-53 | `(chunk: Chunk) -> str` | 2 | 1 | — |
| `_is_root_chunk` | 56-64 | `(chunk: Chunk) -> bool` | 2 | 1 | ``chunk.section_id`` пустой или имеет root-marker. Root-marker: пустая строка (``""``) или ``"s_root"``. Испол |
| `pack_chunks_with_adjacent` | 67-158 | `(chunks: tuple[Chunk, ...], *, config: AdjacentPackingConfig | None=None) -> list[tuple[st` | 21 | 4 | Сгруппировать ``Chunk`` в batches по правилам. Возвращает список tuple chunk_ids — порядок execution. Алгоритм |

## `workspace/skills/legal_summarizer/scripts/execution/pipeline.py` — 157 LOC (code 136)
- module: `workspace.skills.legal_summarizer.scripts.execution.pipeline`
- docstring: Pipeline execution: один LLM batch + retry (без side-effects на cache). Содержит: * ``process_context_batch`` — sync: один LLM call → parse → return batch_meta + chunk_results * ``run_one_batch_async`` — async: retry-цик
- static importers (1): `workspace/skills/legal_summarizer/scripts/application/execution_orchestration.py`
- string/dynamic refs: 2
- test files touching it: — none —
- classes: 0, module functions: 3

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `now_iso` | 41-43 | `() -> str` | 1 | 3 | Текущее время в ISO 8601 (UTC). |
| `process_context_batch` | 46-90 | `(chunks: Sequence[Chunk], *, chunks_total: int, structure: DocumentStructure | None, lengt` | 3 | 1 | Один LLM call → parse → return (batch_meta, chunk_results). Pure execution: возвращает ``dict[chunk_id, summar |
| `run_one_batch_async` | 93-149 | `(chunks: Sequence[Chunk], *, chunks_total: int, structure: DocumentStructure | None, opera` | 5 | 1 | Один батч с retry-циклом (parse-error), под семафором concurrency. ``operation_id``/``workspace_root`` — обяза |

## `workspace/skills/legal_summarizer/scripts/application/chunk_selection.py` — 122 LOC (code 108)
- module: `workspace.skills.legal_summarizer.scripts.application.chunk_selection`
- docstring: Chunk selection policy для application layer. Единая точка ``select_chunks_for_mode(insp, length, question)``, которая выбирает chunks для запуска по режиму (brief/detailed/question): * ``question`` → retrieval (canonica
- static importers (2): `workspace/skills/legal_summarizer/scripts/application/context_builder.py`, `workspace/skills/legal_summarizer/scripts/application/service.py`
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 0, module functions: 3

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_resolve_max_chunks` | 22-29 | `() -> int` | 4 | 1 | Максимум chunks для brief/question режимов из конфига. |
| `relaxed_lexical_fallback` | 32-52 | `(question: str, chunks: list, *, max_chunks: int) -> list | None` | 12 | 3 | Управляемый fallback для question: расслабленный lexical match. |
| `select_chunks_for_mode` | 55-116 | `(insp, *, question: str | None, length: str) -> list` | 17 | 2 | Select chunks for the given run mode (brief/detailed/question). |

## `workspace/skills/legal_summarizer/scripts/chunking/importance_score.py` — 108 LOC (code 83)
- module: `workspace.skills.legal_summarizer.scripts.chunking.importance_score`
- docstring: Importance score для chunk. Deterministic score на основе: * is_title (короткий chunk с section_heading). * is_heading (короткий, body < 200). * section_level (низкий level = важнее). * legal importance (есть legal keywo
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 2

### class `ImportanceScore` — lines 32-53 (22 LOC), 1 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Score компоненты для chunk.
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `total` | 44-53 | `(self) -> float` | 1 | 0 | 2 | — |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `compute_importance` | 56-87 | `(chunk: Chunk, *, section_chunk_count: int=0, section_index: int=0, section_level: int=1) ` | 12 | 2 | Вычислить importance score для chunk. |
| `select_top_chunks_by_importance` | 90-105 | `(chunks: Iterable[Chunk], *, top_k: int=8) -> list[Chunk]` | 3 | 1 | Выбрать top-K chunks по importance score. |

## `workspace/skills/legal_summarizer/scripts/chunking/_text_helpers.py` — 84 LOC (code 65)
- module: `workspace.skills.legal_summarizer.scripts.chunking._text_helpers`
- docstring: Text-helper утилиты для chunk layer. Маленькие чистые функции для разметки chunk-блоков и truncate. Без зависимостей от downstream subsystems. Раньше жили в ``application/_text_helpers.py`` — переехали сюда, потому что р
- static importers (5): `workspace/skills/legal_summarizer/scripts/application/chunk_selection.py`, `workspace/skills/legal_summarizer/scripts/application/execution_orchestration.py`, `workspace/skills/legal_summarizer/scripts/application/service.py`, `workspace/skills/legal_summarizer/scripts/cli.py`, `workspace/skills/legal_summarizer/scripts/execution/map_reduce.py`
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 0, module functions: 5

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `local_structure_label` | 25-35 | `(text: str) -> str` | 3 | 1 | Extract first structural heading from text chunk (inline replacement). Находит первую строку, начинающуюся с l |
| `chunk_structure_label` | 38-43 | `(chunk: 'Chunk') -> str` | 4 | 1 | Структурная метка чанка: global heading, иначе локальная из текста. |
| `format_chunk_block` | 46-51 | `(chunk: 'Chunk', summary: str) -> str` | 2 | 1 | Подписать блок чанка его структурной меткой при сборке ответа. |
| `fit_input` | 54-69 | `(text: str, budget: int) -> str` | 4 | 2 | Урезать text до budget символов стратегией head + tail. |
| `progress` | 72-75 | `(msg: str) -> None` | 1 | 7 | Прогресс ТОЛЬКО в stderr. |

## `workspace/skills/legal_summarizer/scripts/application/context_builder.py` — 76 LOC (code 61)
- module: `workspace.skills.legal_summarizer.scripts.application.context_builder`
- docstring: Execution context: run-level snapshot (selected chunks + strategy + plan).
- static importers (2): `workspace/skills/legal_summarizer/scripts/application/service.py`, `workspace/skills/legal_summarizer/scripts/cli.py`
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 1, module functions: 1

### class `ExecutionContext` — lines 15-25 (11 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Контекст конкретного запуска (run-level). Собирается через ``build_execution_context`` из ``Inspection`` (document-level). ``chunks`` — выбранная для запуска выборка; ``strategy`` и ``plan`` построены именно для неё.
- name referenced in 2 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `build_execution_context` | 28-73 | `(insp: Inspection, length: str | None=None, question: str | None=None, selected_chunks: li` | 5 | 22 | Собрать контекст конкретного запуска из Inspection (document-level). Единая точка, которая выбирает chunks, вы |

## `workspace/skills/legal_summarizer/scripts/application/inspection.py` — 73 LOC (code 59)
- module: `workspace.skills.legal_summarizer.scripts.application.inspection`
- docstring: Inspection: document-level снимок анализа документа. Один canonical pipeline на запуск: ``inspect()`` возвращает ``Inspection`` со structure + analysis + chunks. Выбор strategy / batch'ей / plan строится на уровне запуск
- static importers (2): `workspace/skills/legal_summarizer/scripts/application/context_builder.py`, `workspace/skills/legal_summarizer/scripts/application/service.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 1

### class `Inspection` — lines 20-31 (12 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Снимок анализа документа (document-level). ``Inspection`` описывает **документ**: structure + analysis + chunks. Это НЕ план конкретного запуска. Для конкретного запуска строить ``ExecutionContext`` через ``build_executi
- name referenced in 4 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `inspect` | 34-70 | `(text: str, document_path: str | None=None, *, workspace_root: Path | str | None=None, ses` | 4 | 49 | Canonical inspection (document-level). Возвращает ``Inspection`` со structure + analysis + chunks. Выбор strat |

## `workspace/skills/legal_summarizer/scripts/application/manifest_builder.py` — 66 LOC (code 58)
- module: `workspace.skills.legal_summarizer.scripts.application.manifest_builder`
- docstring: Manifest builder: построение NormalizedManifest для cache/state.
- static importers (1): `workspace/skills/legal_summarizer/scripts/application/execution_orchestration.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 1

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `build_manifest` | 12-63 | `(*, operation_id: str, document_path: str | None, structure: DocumentStructure | None, ana` | 6 | 1 | Собрать ``NormalizedManifest`` для запуска (начальное состояние). ``structure`` — back-compat параметр публичн |

## `workspace/skills/legal_summarizer/scripts/application/document_io.py` — 62 LOC (code 48)
- module: `workspace.skills.legal_summarizer.scripts.application.document_io`
- docstring: Document IO: извлечение plain text из файла документа. Тонкая обёртка над ``workspace.utils.office_files.extract_text`` с поддержкой brief-режима для PDF (первые 100 стр. + до 300К символов через pypdf).
- static importers (1): `workspace/skills/legal_summarizer/scripts/application/service.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 2

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `load_text` | 18-38 | `(path, *, mode: str='full') -> str` | 6 | 5 | Извлечь plain text из файла через office_files. ``mode='brief'`` для PDF: первые 100 стр. + до 300К символов ч |
| `_extract_pdf_head` | 41-59 | `(path: Path, *, max_pages: int, max_chars: int) -> str` | 8 | 1 | Извлечь первые ``max_pages`` страниц PDF через pypdf. |

## `workspace/skills/legal_summarizer/scripts/application/operation_id.py` — 44 LOC (code 34)
- module: `workspace.skills.legal_summarizer.scripts.application.operation_id`
- docstring: Детерминированный operation_id (canonical id для manifest).
- static importers (1): `workspace/skills/legal_summarizer/scripts/application/service.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 1

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `make_operation_id` | 8-41 | `(text: str, length: str, *, document_path: str | None=None, question: str | None=None) -> ` | 3 | 3 | Стабильный operation_id. Детерминированно зависит от: * полного текста (sha256 hex); * ``length`` (brief / det |

## `workspace/skills/legal_summarizer/scripts/chunking/order.py` — 44 LOC (code 32)
- module: `workspace.skills.legal_summarizer.scripts.chunking.order`
- docstring: Order-preserving utilities. Даже при ranking/retrieval порядок документа не должен уничтожаться. После ranking `restore_document_order`. Этот модуль — minimal helpers.
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 2

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `restore_document_order` | 16-24 | `(chunks: Iterable[Chunk], *, key=None) -> list[Chunk]` | 2 | 1 | Восстановить document order (по ``chunk.index``). |
| `ensure_order_preserved` | 27-41 | `(chunks: Iterable[Chunk], original_order_ids: list[str]) -> list[Chunk]` | 5 | 1 | Если chunks уже в document order, вернуть как есть. Иначе — отсортировать по позиции в ``original_order_ids``. |

## `workspace/skills/legal_summarizer/scripts/execution/config.py` — 42 LOC (code 29)
- module: `workspace.skills.legal_summarizer.scripts.execution.config`
- docstring: Execution-level конфигурация для HierarchicalReducer. Эти параметры описывают **execution policy** (как LLM-вызовы группируются и объединяются при reduce-фазе), а не **доменную модель документа**. Поэтому они живут в ``e
- static importers (3): `workspace/skills/legal_summarizer/scripts/application/estimation.py`, `workspace/skills/legal_summarizer/scripts/execution/hierarchical.py`, `workspace/skills/legal_summarizer/scripts/execution/map_reduce.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 0

### class `HierarchicalReducerConfig` — lines 29-35 (7 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Параметры HierarchicalReducer'а.
- name referenced in 7 file(s); tests: 5

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

## `workspace/skills/legal_summarizer/scripts/application/__init__.py` — 1 LOC (code 0)
- module: `workspace.skills.legal_summarizer.scripts.application`
- docstring: — NONE —
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: `workspace/skills/legal_summarizer/tests/test_execution_context_snapshot.py`, `workspace/skills/legal_summarizer/tests/test_idempotency_no_reexecution.py`, `workspace/skills/legal_summarizer/tests/test_question_integration.py`
- classes: 0, module functions: 0

## `workspace/skills/legal_summarizer/scripts/cache/__init__.py` — 1 LOC (code 0)
- module: `workspace.skills.legal_summarizer.scripts.cache`
- docstring: — NONE —
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: `workspace/skills/legal_summarizer/tests/test_corrective_guards.py`, `workspace/skills/legal_summarizer/tests/test_demo_workflow.py`, `workspace/skills/legal_summarizer/tests/test_document_cache_hit_miss.py`, `workspace/skills/legal_summarizer/tests/test_document_chunk_callback.py`, `workspace/skills/legal_summarizer/tests/test_document_snapshot.py`, `workspace/skills/legal_summarizer/tests/test_question_context.py`, `workspace/skills/legal_summarizer/tests/test_question_via_document_cache.py`, `workspace/skills/legal_summarizer/tests/test_section_summaries_persist.py`
- classes: 0, module functions: 0

## `workspace/skills/legal_summarizer/scripts/chunking/__init__.py` — 1 LOC (code 0)
- module: `workspace.skills.legal_summarizer.scripts.chunking`
- docstring: — NONE —
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 0

## `workspace/skills/legal_summarizer/scripts/execution/__init__.py` — 1 LOC (code 0)
- module: `workspace.skills.legal_summarizer.scripts.execution`
- docstring: — NONE —
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 0

