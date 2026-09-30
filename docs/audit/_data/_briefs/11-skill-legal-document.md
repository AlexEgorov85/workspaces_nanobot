# Work brief `11-skill-legal-document`

Product files: **18**, LOC: **4505**

## `workspace/skills/legal_summarizer/scripts/document/heading.py` — 660 LOC (code 540)
- module: `workspace.skills.legal_summarizer.scripts.document.heading`
- docstring: Heading detection (candidates + scoring) — ``structure/heading.py``. Поведение НЕ меняется. Содержит: * ``CONFIDENCE_THRESHOLD`` — порог score для принятия кандидата. * ``HeadingCandidate`` — dataclass-кандидат на headin
- static importers (4): `workspace/skills/legal_summarizer/scripts/application/pipeline_structure.py`, `workspace/skills/legal_summarizer/scripts/document/hierarchy.py`, `workspace/skills/legal_summarizer/scripts/document/pdf_outline.py`, `workspace/skills/legal_summarizer/scripts/retrieval/candidate_aggregator.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 2, module functions: 16

### class `HeadingCandidate` — lines 60-68 (9 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Кандидат на heading с confidence score.
- name referenced in 12 file(s); tests: 8

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `HeadingEvidence` — lines 332-394 (63 LOC), 2 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Контекстные признаки, влияющие на heading-score. Все поля — дельты относительно ``source_score`` (см. ``HeadingCandidate.score``). Итоговый score = source + bonuses - penalties. Это намеренно **детерминированная** эврист
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `total_delta` | 378-389 | `(self) -> float` | 1 | 0 | 1 | Итоговая дельта score (прибавить к source_score). |
| `final_score` | 392-394 | `(self) -> float` | 3 | 0 | 0 | Итоговый score после всех bonuses/penalties. |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_is_docx_heading_style` | 71-75 | `(style_name: str) -> bool` | 4 | 1 | — |
| `_is_docx_title_style` | 78-88 | `(style_name: str) -> bool` | 4 | 1 | True если DOCX style — это Title / Subtitle. Title-стили дают очень высокую уверенность, что параграф — это do |
| `_looks_like_heading` | 91-93 | `(text: str) -> bool` | 1 | 0 | Грубая проверка формата heading в тексте. |
| `_classify_regex` | 96-140 | `(text: str) -> tuple[int, float, str, str | None] | None` | 9 | 1 | Классифицировать текст по regex'ам. Вернуть (level, score, source, number). Шкала base scores: * **Явные legal |
| `_extract_pdf_outline` | 143-194 | `(path: str) -> list[HeadingCandidate]` | 11 | 1 | Прочитать PDF outline → list[HeadingCandidate]. Не возвращаем уровень/номер — outline сам даёт структуру, а на |
| `detect_heading_candidates` | 197-289 | `(blocks: tuple[DocumentBlock, ...], pdf_path: str | None, *, physical_doc: Optional[Physic` | 21 | 8 | Найти всех кандидатов в heading'и (DOCX style + regex + PDF outline). Public API: было приватной ``_detect_can |
| `apply_confidence_penalties` | 292-323 | `(candidates: list[HeadingCandidate], blocks: tuple[DocumentBlock, ...]) -> list[HeadingCan` | 15 | 5 | Снизить score для heading'ов, после которых идёт другой heading или пустой block (anti-false-positive). |
| `_is_short` | 402-403 | `(text: str, max_len: int=_SHORT_TEXT_MAX) -> bool` | 1 | 1 | — |
| `_looks_like_heading_typography` | 406-421 | `(text: str) -> bool` | 7 | 1 | ``True`` если текст выглядит как heading по типографике. Простая эвристика: либо «короткий и title-cased», либ |
| `_is_substantial_body` | 424-425 | `(text: str) -> bool` | 1 | 1 | — |
| `_collect_previous_heading_text` | 428-440 | `(candidate: HeadingCandidate, candidates: list[HeadingCandidate]) -> str | None` | 5 | 1 | Текст предыдущего кандидата в heading'и (по block_index), если есть. |
| `_numbering_consistency_with_neighbors` | 443-473 | `(candidate: HeadingCandidate, candidates: list[HeadingCandidate]) -> bool` | 11 | 1 | ``True`` если кандидат участвует в монотонной последовательности. Эвристика: есть предыдущий кандидат того же  |
| `compute_evidence` | 476-541 | `(candidate: HeadingCandidate, blocks: tuple[DocumentBlock, ...], all_candidates: list[Head` | 14 | 2 | Вычислить контекстные evidence для одного кандидата. Не зависит от того, прошёл ли кандидат threshold — это чи |
| `_looks_like_explicit_legal_marker` | 550-559 | `(text: str) -> bool` | 5 | 1 | ``True`` если текст содержит явный юридический маркер. |
| `apply_evidence_scoring` | 562-637 | `(candidates: list[HeadingCandidate], blocks: tuple[DocumentBlock, ...]) -> list[HeadingCan` | 11 | 6 | Пересчитать score на основе контекстных evidence. Применяется ПОСЛЕ ``apply_confidence_penalties``. Итог: ``fi |
| `filter_above_threshold` | 640-643 | `(candidates: list[HeadingCandidate]) -> list[HeadingCandidate]` | 2 | 5 | — |

## `workspace/skills/legal_summarizer/scripts/document/hierarchy.py` — 520 LOC (code 432)
- module: `workspace.skills.legal_summarizer.scripts.document.hierarchy`
- docstring: StructureTreeBuilder. Строит ``DocumentStructure`` из: * ``HeadingCandidate`` (из heading.py); * ``NumberingInfo`` (из numbering.py); * ``StructureEvidence``; * ``HeadingEvidence`` (через heuristics). Иерархия строится и
- static importers (1): `workspace/skills/legal_summarizer/scripts/application/pipeline_structure.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 10

### class `StructureTreeBuilderConfig` — lines 183-188 (6 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Параметры builder'а (минимальный набор).
- name referenced in 5 file(s); tests: 3

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_scheme_priority` | 62-75 | `(scheme: str | None) -> int` | 5 | 1 | Приоритет схемы для выбора parent. Меньше = глубже в иерархии (выше шанс стать дочерним). |
| `_level_from_numbering` | 78-88 | `(ni: NumberingInfo | None) -> int` | 5 | 1 | Вывести level из numbering. Для decimal — это ``len(components)`` (1 → 1.0 → 2, etc.). Для остальных — ``ni.le |
| `_effective_level` | 91-115 | `(c: HeadingCandidate, ni: NumberingInfo | None) -> int` | 7 | 0 | Вычислить ``effective level`` кандидата. Приоритет: 1. ``docx_style`` / ``pdf_outline`` — собственный ``c.leve |
| `_heading_rank` | 118-156 | `(c: HeadingCandidate, ni: NumberingInfo | None) -> int` | 13 | 1 | Монотонный ранг глубины для заголовка (больше = глубже). Используется stack-алгоритмом построения дерева из пл |
| `_build_parents_by_stack` | 159-179 | `(order: list[tuple[str, int]], root_id: str) -> dict[str, str]` | 4 | 1 | Построить ``parent_id`` для каждого section по stack-алгоритму. Вход: ``order`` — список ``(node_id, rank)`` в |
| `_evidence_from_candidate` | 191-209 | `(c: HeadingCandidate) -> tuple[StructureEvidence, ...]` | 4 | 1 | Преобразовать ``HeadingCandidate`` в tuple ``StructureEvidence``. Weight выбирается по ``source``: * ``docx_st |
| `_resolve_level` | 212-218 | `(c: HeadingCandidate) -> int` | 8 | 0 | Вычислить level кандидата с учётом source-приоритета. |
| `_resolve_semantic_type` | 221-240 | `(c: HeadingCandidate) -> str | None` | 5 | 1 | Извлечь ``semantic_type`` из source'а кандидата. Правила: * ``regex_statiya`` → ``"article"``. * ``regex_glava |
| `_log_structure_diagnostics` | 255-300 | `(accepted: list[HeadingCandidate], total_blocks: int, *, document_id: str) -> None` | 6 | 1 | Записать diagnostic counters и sanity check. Diagnostic counters (по source): * ``total_blocks`` — размер доку |
| `build_document_structure` | 303-514 | `(candidates: Iterable[HeadingCandidate], *, total_blocks: int, document_id: str | None=Non` | 26 | 12 | Построить ``DocumentStructure`` из ``HeadingCandidate``. Args: candidates: ``HeadingCandidate`` (например, из  |

## `workspace/skills/legal_summarizer/scripts/document/structure.py` — 506 LOC (code 413)
- module: `workspace.skills.legal_summarizer.scripts.document.structure`
- docstring: DocumentStructure — единый контракт семантической структуры документа. Это **canonical model** для семантической структуры, отдельная от ``PhysicalDocument`` (semantic vs physical границы). Архитектурные инварианты: * ``
- static importers (27): `workspace/skills/legal_summarizer/scripts/application/brief_context.py`, `workspace/skills/legal_summarizer/scripts/application/estimation.py`, `workspace/skills/legal_summarizer/scripts/application/execution_orchestration.py`, `workspace/skills/legal_summarizer/scripts/application/inspection.py`, `workspace/skills/legal_summarizer/scripts/application/manifest_builder.py`, `workspace/skills/legal_summarizer/scripts/application/pipeline_structure.py`, `workspace/skills/legal_summarizer/scripts/application/service.py`, `workspace/skills/legal_summarizer/scripts/chunking/chunker.py`, `workspace/skills/legal_summarizer/scripts/chunking/structural_packing.py`, `workspace/skills/legal_summarizer/scripts/document/analysis.py`, `workspace/skills/legal_summarizer/scripts/document/block_ownership.py`, `workspace/skills/legal_summarizer/scripts/document/heading.py`, `workspace/skills/legal_summarizer/scripts/document/hierarchy.py`, `workspace/skills/legal_summarizer/scripts/document/numbering.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 5, module functions: 5

### class `StructureEvidence` — lines 30-48 (19 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Один evidence для решения о structural node. Используется в ``StructureNode.evidence`` (пять уровней уверенности: very-high / high / medium / low). Attributes: source: имя источника (например, ``"docx_style"``, ``"legal_
- name referenced in 4 file(s); tests: 2

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `NumberingInfo` — lines 52-82 (31 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Парсер numbering для heading/list caption. Поддерживает минимум: * ``1.``, ``1.1``, ``1.1.1`` — decimal scheme. * ``Статья 12``, ``Статья 12.1`` — legal_article scheme. * ``Глава 3`` — legal_chapter scheme. * ``Раздел I`
- name referenced in 9 file(s); tests: 5

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `DocumentTitle` — lines 86-108 (23 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Title документа. Различает три источника: * ``source="metadata"`` — из DOCX/PDF/PPTX metadata. * ``source="visual"`` — первая визуально выделенная строка (typography heuristics). * ``source="inferred"`` — heading candida
- name referenced in 7 file(s); tests: 4

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `StructureNode` — lines 112-249 (138 LOC), 2 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Узел семантической структуры документа. Семантика полей: * ``node_id``: стабильный идентификатор вида ``"n_0001"``. * ``node_type``: ``"section"`` | ``"body"`` | ``"table"`` | ``"list"`` | ``"list_item"`` | ``"caption"``
- name referenced in 35 file(s); tests: 25

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `to_dict` | 163-191 | `(self) -> dict[str, Any]` | 3 | 0 | 24 | — |
| `from_dict` | 194-249 | `(cls, data: dict[str, Any]) -> 'StructureNode'` | 25 | 0 | 6 | Обратная сериализация для ``to_dict``. Используется при восстановлении ``DocumentStructure`` из document-level |

### class `DocumentStructure` — lines 253-394 (142 LOC), 7 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Canonical DocumentStructure. Attributes: document_id: идентификатор документа (``DocumentIdentity.document_id``). title: ``DocumentTitle`` или ``None``. nodes: dict ``node_id → StructureNode``. root_id: node_id корневого
- name referenced in 54 file(s); tests: 25

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `get_node` | 277-278 | `(self, node_id: str) -> StructureNode | None` | 2 | 0 | 1 | — |
| `iter_nodes` | 280-282 | `(self) -> list[StructureNode]` | 1 | 1 | 2 | Все ноды в document order (по ``start_block``). |
| `iter_sections` | 284-289 | `(self) -> list[StructureNode]` | 2 | 0 | 11 | Только section-узлы (исключая body / table / list_item). |
| `iter_children` | 291-295 | `(self, parent_id: str) -> list[StructureNode]` | 4 | 0 | 1 | — |
| `block_to_node` | 297-311 | `(self) -> dict[int, str]` | 3 | 0 | 2 | Mapping ``ordinal DocumentBlock`` → ``node_id``. Этот метод — единая точка для consumers, которым нужно ``bloc |
| `to_dict` | 313-341 | `(self) -> dict[str, Any]` | 4 | 0 | 24 | — |
| `from_dict` | 344-394 | `(cls, data: dict[str, Any]) -> 'DocumentStructure'` | 21 | 0 | 6 | Обратная сериализация для ``to_dict``. Используется при восстановлении ``DocumentAnalysis`` из document-level  |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_depth_of` | 418-425 | `(node_id: str, struct: DocumentStructure) -> int` | 5 | 2 | Глубина узла в дереве (root = 0). |
| `build_block_ownership` | 428-449 | `(struct: DocumentStructure) -> dict[int, str]` | 4 | 5 | Построить ``block_ordinal → owning_section_node_id``. Каждый block, попадающий в диапазон какого-либо section, |
| `owner_for_block` | 452-475 | `(struct: DocumentStructure, ordinal: int, ownership: dict[int, str] | None=None) -> str | ` | 5 | 2 | Получить owner для block ``ordinal``. Returns: - ``owning_section_node_id`` если block принадлежит section; -  |
| `block_to_node` | 478-489 | `(struct: DocumentStructure) -> dict[int, str]` | 3 | 2 | Mapping ``block_ordinal → node_id``. Этот метод — единая точка для consumers, которым нужно ``block → node_id` |
| `_make_node_id` | 505-506 | `(counter: int) -> str` | 1 | 2 | — |

## `workspace/skills/legal_summarizer/scripts/document/physical.py` — 456 LOC (code 374)
- module: `workspace.skills.legal_summarizer.scripts.document.physical`
- docstring: PhysicalDocument: нормализованный список блоков документа с координатами. Это **adapter** над ``workspace.utils.office_files``, не parser. Что берём из office_files: * ``extract_structure(path)`` → ``title``, ``begin``, 
- static importers (17): `workspace/skills/legal_summarizer/scripts/application/brief_context.py`, `workspace/skills/legal_summarizer/scripts/application/pipeline_structure.py`, `workspace/skills/legal_summarizer/scripts/application/service.py`, `workspace/skills/legal_summarizer/scripts/chunking/chunker.py`, `workspace/skills/legal_summarizer/scripts/chunking/chunks.py`, `workspace/skills/legal_summarizer/scripts/chunking/structural_packing.py`, `workspace/skills/legal_summarizer/scripts/document/analysis.py`, `workspace/skills/legal_summarizer/scripts/document/block_lookup.py`, `workspace/skills/legal_summarizer/scripts/document/heading.py`, `workspace/skills/legal_summarizer/scripts/document/list_detection.py`, `workspace/skills/legal_summarizer/scripts/document/loader.py`, `workspace/skills/legal_summarizer/scripts/document/pdf_outline.py`, `workspace/skills/legal_summarizer/scripts/document/safety_merge.py`, `workspace/skills/legal_summarizer/scripts/document/title.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 2, module functions: 8

### class `DocumentBlock` — lines 60-94 (35 LOC), 1 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Один физический блок документа. Attributes: block_id: стабильный идентификатор вида ``"b_001"``. block_type: ``"page"`` (PDF) | ``"paragraph"`` (DOCX) | ``"table"`` | ``"text"`` (TXT) | ``"slide"`` (PPTX, зарезервировано
- name referenced in 39 file(s); tests: 25

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `to_dict` | 93-94 | `(self) -> dict[str, Any]` | 1 | 0 | 24 | — |

### class `PhysicalDocument` — lines 98-160 (63 LOC), 3 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Нормализованная физическая модель документа. Attributes: path: исходный путь. format: расширение файла (``pdf`` / ``docx`` / ``txt``). title: из ``extract_structure``. size_bytes: размер файла. blocks: плоский список ``D
- name referenced in 41 file(s); tests: 25

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `blocks_by_ord` | 123-138 | `(self) -> dict[int, 'DocumentBlock']` | 3 | 0 | 4 | Lookup ``DocumentBlock`` по ``ordinal`` (identity, не position). Invariant в текущей реализации: ``blocks[i].o |
| `to_dict` | 140-148 | `(self) -> dict[str, Any]` | 2 | 0 | 24 | — |
| `from_dict` | 151-160 | `(cls, data: dict[str, Any]) -> 'PhysicalDocument'` | 3 | 0 | 6 | — |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_physical_cache_root` | 163-165 | `(workspace_root: Path | str | None) -> Path` | 1 | 0 | — |
| `_identity_for` | 168-170 | `(path: Path) -> DocumentIdentity` | 1 | 0 | ``DocumentIdentity`` для ``path``. |
| `_pick_title_from_text` | 173-207 | `(fmt: str, text: str, path: Path) -> str | None` | 15 | 2 | Получить title из метаданных файла или первой содержательной строки. |
| `_build_structure_dict` | 210-233 | `(path: Path, fmt: str) -> dict[str, Any]` | 3 | 0 | Построить минимальный dict с полями, нужными PhysicalDocument. Поля: ``title``, ``text``, ``begin``, ``end``,  |
| `_table_to_text` | 236-243 | `(table: list[list[str]]) -> str` | 8 | 1 | Склеить таблицу в текст с ``\|``-разделителем ячеек (как office_files). |
| `_iter_pdf_blocks` | 246-313 | `(path: Path) -> tuple[list[DocumentBlock], int]` | 17 | 1 | PDF → blocks (по страницам). Таблицы встроены между страницами. |
| `_iter_docx_blocks` | 316-412 | `(path: Path) -> tuple[list[DocumentBlock], int]` | 23 | 1 | DOCX → blocks (paragraphs + tables) в document order. Обход ``body.iterchildren()`` даёт честный document orde |
| `_iter_txt_blocks` | 422-444 | `(path: Path) -> tuple[list[DocumentBlock], int]` | 2 | 1 | TXT → один block. |

## `workspace/skills/legal_summarizer/scripts/document/pdf_outline.py` — 369 LOC (code 303)
- module: `workspace.skills.legal_summarizer.scripts.document.pdf_outline`
- docstring: PDF outline mapping. Критический bugfix: ``heading._extract_pdf_outline`` ставил ``block_index = -1`` для outline-кандидатов, после чего ``tree.build_section_tree`` отбрасывал их (filter ``c.block_index >= 0``). В резуль
- static importers (1): `workspace/skills/legal_summarizer/scripts/document/heading.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 2, module functions: 5

### class `StructureAnchor` — lines 60-73 (14 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Якорь, связывающий outline entry с конкретным ``DocumentBlock``. Attributes: block_ordinal: ordinal ``DocumentBlock``, на который указывает outline entry. page_index: 1-based страница из destination. char_offset: смещени
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `MappedOutlineCandidate` — lines 77-113 (37 LOC), 1 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Outline entry после успешного mapping на DocumentBlock. Attributes: block_index: ordinal DocumentBlock (**всегда >= 0** для mapped; ``-1`` только если mapping провалился и есть в diagnostics). text: текст outline. level:
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `to_heading_candidate` | 99-113 | `(self) -> HeadingCandidate | None` | 2 | 0 | 0 | Преобразовать в ``HeadingCandidate``. Возвращает ``None`` если mapping провалился (нет anchor). |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_resolve_destination_page` | 116-150 | `(reader: Any, page_ref: Any) -> int | None` | 12 | 1 | Получить 1-based номер страницы из outline destination. Поддерживает два формата destination: * ``list`` (``[' |
| `_find_nearest_block_on_page` | 153-165 | `(doc: PhysicalDocument, page_index: int) -> int | None` | 3 | 1 | Найти ближайший DocumentBlock на заданной странице. Возвращает ordinal первого block'а с ``page_index == page_ |
| `_walk_outline` | 180-213 | `(reader: Any, items: list[Any], level: int) -> list[tuple[int, str, Any]]` | 9 | 1 | Рекурсивно обойти outline, вернуть ``(level, title, page_ref)``. Page_ref — это destination target, который по |
| `map_pdf_outline` | 216-331 | `(pdf_path: str, doc: PhysicalDocument) -> list[MappedOutlineCandidate]` | 13 | 2 | Прочитать PDF outline и замапить каждую entry на ``DocumentBlock``. Возвращает список ``MappedOutlineCandidate |
| `mapped_to_heading_candidates` | 334-361 | `(mapped: list[MappedOutlineCandidate]) -> list['HeadingCandidate']` | 3 | 2 | Преобразовать успешно mapped кандидатов в ``HeadingCandidate``. Провалившие (с ``block_index = -1``) **отбрасы |

## `workspace/skills/legal_summarizer/scripts/document/list_detection.py` — 335 LOC (code 256)
- module: `workspace.skills.legal_summarizer.scripts.document.list_detection`
- docstring: Numbered-list detection — ``structure/list_detection.py``. Различение нумерованных **разделов** от **списков**. Сценарии: * Section sequence: 1. Общие положения <длинный body> 2. Обязанности сторон <длинный body> 3. Отве
- static importers (1): `workspace/skills/legal_summarizer/scripts/document/heading.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 2, module functions: 7

### class `ListDetectionConfig` — lines 49-63 (15 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Параметры list-detection. ``max_item_chars`` увеличен со старого 200 до 600: для юридических документов (НК РФ, ГК РФ) средняя длина нумерованного пункта статьи 1.5К–7К символов. Старый порог 200 → ``is_list=False`` для 
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `ListRun` — lines 67-74 (8 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Обнаруженная последовательность нумерованных блоков (list или section).
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_parse_number` | 77-90 | `(text: str) -> int | None` | 4 | 1 | Извлечь начальный номер из текста вида ``1. ...`` / ``1.2. ...``. Возвращает первую цифру из ведущего номера.  |
| `detect_list_runs` | 93-155 | `(blocks: tuple[DocumentBlock, ...], config: ListDetectionConfig | None=None) -> list[ListR` | 11 | 3 | Найти все list-like runs нумерованных блоков в документе. Возвращает список ListRun. Каждый ListRun — это посл |
| `_classify_run` | 158-185 | `(ordinals: tuple[int, ...], blocks: tuple[DocumentBlock, ...], cfg: ListDetectionConfig) -` | 9 | 1 | Определить, является ли последовательность list или section-цепочкой. |
| `_neighbor_numbered_count` | 191-215 | `(candidate_ordinal: int, list_runs: list[ListRun], *, window: int=_NEIGHBOR_WINDOW) -> int` | 5 | 1 | Сколько нумерованных блоков в окрестности ±window от кандидата. Используется для различения: * standalone head |
| `list_penalty_for_candidate` | 218-259 | `(candidate_ordinal: int, list_runs: list[ListRun]) -> float` | 7 | 2 | Штраф к heading-score за попадание кандидата в list-run. Возвращает: * ``0.0`` если run длины 1 (standalone he |
| `ambiguous_decimal_penalty` | 262-291 | `(candidate_ordinal: int, list_runs: list[ListRun]) -> float` | 5 | 1 | Доп. штраф для кандидатов с голой десятичной нумерацией в ambiguous run. Это **ещё одна** ступень защиты: даже |
| `classify_ambiguous_run` | 294-324 | `(ordinals: tuple[int, ...], blocks: tuple[DocumentBlock, ...], *, max_item_chars: int=200)` | 9 | 1 | Определить категорию для спорного numbered-run. Различать heading vs list item в спорных случаях. Возвращает о |

## `workspace/skills/legal_summarizer/scripts/document/validation.py` — 332 LOC (code 290)
- module: `workspace.skills.legal_summarizer.scripts.document.validation`
- docstring: StructureValidator. Проверяет ``DocumentStructure`` на: * **Coverage**: каждый значимый ``DocumentBlock`` покрыт (``start_block ≤ ordinal ≤ end_block`` хотя бы для одного node); * **Ordering**: ``start_block ≤ end_block`
- static importers (1): `workspace/skills/legal_summarizer/scripts/application/pipeline_structure.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 2, module functions: 3

### class `ValidationIssue` — lines 41-62 (22 LOC), 2 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Одна найденная проблема в структуре.
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `to_dict` | 48-49 | `(self) -> dict[str, Any]` | 1 | 0 | 24 | — |
| `from_dict` | 52-62 | `(cls, data: dict[str, Any]) -> 'ValidationIssue'` | 5 | 0 | 6 | Обратная сериализация для ``to_dict``. |

### class `ValidationReport` — lines 66-92 (27 LOC), 2 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Сводка валидации (для diagnostics и acceptance).
- name referenced in 5 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `to_dict` | 73-78 | `(self) -> dict[str, Any]` | 2 | 0 | 24 | — |
| `from_dict` | 81-92 | `(cls, data: dict[str, Any]) -> 'ValidationReport'` | 6 | 0 | 6 | Обратная сериализация для ``to_dict``. Используется при восстановлении ``PipelineResult.validation`` из docume |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_is_ancestor` | 95-113 | `(struct: DocumentStructure, ancestor_id: str, descendant_id: str) -> bool` | 7 | 1 | True если ``ancestor_id`` — предок ``descendant_id`` (или они равны). |
| `_ranges_overlap` | 116-118 | `(a_start: int, a_end: int, b_start: int, b_end: int) -> bool` | 2 | 1 | True если диапазоны перекрываются (включая границы). |
| `validate_structure` | 121-325 | `(struct: DocumentStructure, doc: PhysicalDocument) -> ValidationReport` | 48 | 3 | Валидировать ``DocumentStructure`` против ``PhysicalDocument``. Проверки: 1. ``total_blocks`` соответствует `` |

## `workspace/skills/legal_summarizer/scripts/document/numbering.py` — 237 LOC (code 199)
- module: `workspace.skills.legal_summarizer.scripts.document.numbering`
- docstring: Numbering parser для headings / list items / captions. Цель: **один модуль** для всего numbering detection. Сейчас regex'ы разбросаны между ``heading.py`` (private ``_RE_NUMBERED_LEVEL_*``) и ``list_detection.py`` (тоже 
- static importers (2): `workspace/skills/legal_summarizer/scripts/document/heading.py`, `workspace/skills/legal_summarizer/scripts/document/hierarchy.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 4

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_roman_to_int` | 52-69 | `(s: str) -> int | None` | 7 | 1 | Преобразовать римскую цифру (I..XXX) в int. None если не парсится. |
| `_parse_decimal_number` | 72-75 | `(raw: str) -> tuple[tuple[int, ...], int]` | 2 | 1 | ``"1.2.3"`` → ((1, 2, 3), 3). |
| `parse_numbering` | 78-175 | `(text: str) -> NumberingInfo | None` | 14 | 3 | Определить numbering scheme для ``text``. Возвращает ``NumberingInfo`` или ``None``, если текст не является ну |
| `assign_sibling_ordinals` | 178-231 | `(items: list[NumberingInfo | None]) -> list[int | None]` | 10 | 2 | Вычислить ``ordinal`` среди siblings одного ``scheme``/``level``. На вход — список ``NumberingInfo`` (или ``No |

## `workspace/skills/legal_summarizer/scripts/document/repair.py` — 200 LOC (code 166)
- module: `workspace.skills.legal_summarizer.scripts.document.repair`
- docstring: Structural repair pass. После построения иерархии запускается **repair** для исправления типичных проблем: * level jumps (level 1 → level 3 без level 2); * orphan nodes (parent_id не существует); * overlapping ranges (ес
- static importers (1): `workspace/skills/legal_summarizer/scripts/application/pipeline_structure.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 1

### class `RepairReport` — lines 48-54 (7 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Сводка repair-прохода (для diagnostics).
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `repair_structure` | 57-197 | `(struct: DocumentStructure) -> tuple[DocumentStructure, RepairReport]` | 34 | 4 | Запустить repair pass и вернуть (исправленная структура, отчёт). Repair идёт в фиксированном порядке: 1. orpha |

## `workspace/skills/legal_summarizer/scripts/document/analysis.py` — 166 LOC (code 138)
- module: `workspace.skills.legal_summarizer.scripts.document.analysis`
- docstring: DocumentAnalysis cache architecture. Единый ``DocumentAnalysis`` — это immutable snapshot всех результатов анализа документа, который переиспользуется для follow-up запросов: * identity (DocumentIdentity); * physical (Ph
- static importers (10): `workspace/skills/legal_summarizer/scripts/application/brief_context.py`, `workspace/skills/legal_summarizer/scripts/application/execution_orchestration.py`, `workspace/skills/legal_summarizer/scripts/application/inspection.py`, `workspace/skills/legal_summarizer/scripts/application/manifest_builder.py`, `workspace/skills/legal_summarizer/scripts/application/pipeline_structure.py`, `workspace/skills/legal_summarizer/scripts/application/service.py`, `workspace/skills/legal_summarizer/scripts/execution/map_reduce.py`, `workspace/skills/legal_summarizer/scripts/retrieval/canonical.py`, `workspace/skills/legal_summarizer/scripts/retrieval/followup.py`, `workspace/skills/legal_summarizer/scripts/retrieval/question.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 0

### class `DocumentAnalysis` — lines 39-163 (125 LOC), 5 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Единый cache для анализа документа. Attributes: identity: ``DocumentIdentity``. physical: ``PhysicalDocument``. structure: ``DocumentStructure`` (canonical semantic structure). chunks: tuple of ``Chunk`` (в document orde
- name referenced in 16 file(s); tests: 6

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `get_chunk` | 62-66 | `(self, chunk_id: str) -> Chunk | None` | 3 | 0 | 2 | — |
| `get_record` | 68-69 | `(self, chunk_id: str) -> SemanticRecord | None` | 2 | 0 | 1 | — |
| `to_dict` | 71-81 | `(self) -> dict[str, Any]` | 1 | 0 | 24 | — |
| `build` | 84-143 | `(cls, *, physical: PhysicalDocument, structure: DocumentStructure, chunks: tuple[Chunk, ..` | 5 | 0 | 14 | Построить DocumentAnalysis из ингредиентов. Это **canonical** сборка. ``DocumentAnalysis`` — единый слой immut |
| `retrieve` | 145-163 | `(self, query: str, *, config=None) -> list` | 2 | 0 | 4 | Удобный API: retrieval через ``RetrievalIndex``. Возвращает список ``RetrievalHit``. |

## `workspace/skills/legal_summarizer/scripts/document/title.py` — 154 LOC (code 125)
- module: `workspace.skills.legal_summarizer.scripts.document.title`
- docstring: Document title resolution. Источники title (по приоритету): 1. ``DOCX core_properties.title`` → ``source="metadata"``. 2. ``PDF metadata /Title`` → ``source="metadata"``. 3. ``PDF /Info Title`` → ``source="metadata"``. 4
- static importers (1): `workspace/skills/legal_summarizer/scripts/application/pipeline_structure.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 5

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_metadata_title` | 33-71 | `(path: Path) -> tuple[str | None, str]` | 17 | 1 | Прочитать title из метаданных формата (DOCX/PDF/ПPPTX). Возвращает ``(title, source)`` где ``source`` — ``"met |
| `_docx_title_style_block` | 74-87 | `(blocks: tuple[DocumentBlock, ...]) -> DocumentTitle | None` | 5 | 1 | Найти первый DOCX Title-style block → ``source="visual"``. |
| `_first_strong_candidate` | 90-103 | `(blocks: tuple[DocumentBlock, ...]) -> DocumentTitle | None` | 5 | 1 | Первая heading-кандидат с минимальным level → ``source="inferred"``. |
| `_first_nonempty_line_fallback` | 106-117 | `(text: str) -> DocumentTitle | None` | 4 | 1 | Fallback — первая непустая строка (для обратной совместимости). |
| `resolve_title` | 120-151 | `(doc: PhysicalDocument, *, text: str | None=None) -> DocumentTitle | None` | 5 | 2 | Извлечь title из ``PhysicalDocument``. Приоритет источников — как описано в docstring модуля. Args: doc: ``Phy |

## `workspace/skills/legal_summarizer/scripts/document/safety_merge.py` — 147 LOC (code 124)
- module: `workspace.skills.legal_summarizer.scripts.document.safety_merge`
- docstring: Safety net merge для микро-секций. После хорошего heading detection + repair остаются **крайние случаи** — например, микро-секции из одной строки heading + одной строки body (false micro-section из-за агрессивного headin
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 2

### class `SafetyMergeConfig` — lines 39-44 (6 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Параметры safety merge.
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_section_total_chars` | 47-55 | `(node: StructureNode, chars_by_ord: dict[int, int]) -> int` | 4 | 1 | — |
| `safety_merge` | 58-144 | `(struct: DocumentStructure, blocks: tuple[DocumentBlock, ...], *, config: SafetyMergeConfi` | 14 | 2 | Safety net: схлопнуть микро-секции в соседнюю секцию того же level. Алгоритм: 1. Для каждого level (1, 2) прой |

## `workspace/skills/legal_summarizer/scripts/document/identity.py` — 118 LOC (code 98)
- module: `workspace.skills.legal_summarizer.scripts.document.identity`
- docstring: DocumentIdentity — единый идентификатор документа. Заменяет **две параллельные** реализации fingerprint: * ``scripts/fingerprint.py::compute_fingerprint`` (sha256 от path/size/mtime). * ``scripts/structure/physical.py::_
- static importers (4): `workspace/skills/legal_summarizer/scripts/application/pipeline_structure.py`, `workspace/skills/legal_summarizer/scripts/application/service.py`, `workspace/skills/legal_summarizer/scripts/document/analysis.py`, `workspace/skills/legal_summarizer/scripts/document/physical.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 0

### class `DocumentIdentity` — lines 35-115 (81 LOC), 4 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Единый идентификатор документа. Attributes: document_id: короткий ID (первые 12 hex fingerprint'а) для логирования и manifest. fingerprint: полный sha256 hex от ``(resolved_path, size, mtime)``. physical_cache_key: == fi
- name referenced in 17 file(s); tests: 13

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `is_fresh` | 56-71 | `(self, path: str | Path) -> bool` | 4 | 0 | 2 | ``True`` если ``(size, mtime)`` совпадают с закэшированными. Используется при cache lookup: если ``is_fresh``  |
| `to_dict` | 73-81 | `(self) -> dict[str, str | int]` | 1 | 0 | 24 | — |
| `from_path` | 84-96 | `(cls, path: str | Path) -> 'DocumentIdentity'` | 1 | 0 | 15 | — |
| `from_path_with_mtime` | 99-115 | `(cls, path: str | Path, *, size_bytes: int, mtime_ns: int) -> 'DocumentIdentity'` | 1 | 0 | 3 | Создать identity по явно переданным ``size_bytes``/``mtime_ns``. Полезно для back-compat с ``_physical_cache_k |

## `workspace/skills/legal_summarizer/scripts/document/block_ownership.py` — 109 LOC (code 87)
- module: `workspace.skills.legal_summarizer.scripts.document.block_ownership`
- docstring: Canonical block ownership. Чистые функции на ``DocumentStructure``, которые определяют «какой section node владеет данным ``DocumentBlock``». Это **domain API**: использует только поля ``DocumentStructure`` (nodes, root_
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 4

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_depth_of` | 31-38 | `(node_id: str, struct: DocumentStructure) -> int` | 5 | 2 | Глубина узла в дереве (root = 0). |
| `build_block_ownership` | 41-62 | `(struct: DocumentStructure) -> dict[int, str]` | 4 | 5 | Построить ``block_ordinal → owning_section_node_id``. Каждый block, попадающий в диапазон какого-либо section, |
| `owner_for_block` | 65-88 | `(struct: DocumentStructure, ordinal: int, ownership: dict[int, str] | None=None) -> str | ` | 5 | 2 | Получить owner для block ``ordinal``. Returns: - ``owning_section_node_id`` если block принадлежит section; -  |
| `block_to_node` | 91-102 | `(struct: DocumentStructure) -> dict[int, str]` | 3 | 2 | Mapping ``block_ordinal → node_id``. Этот метод — единая точка для consumers, которым нужно ``block → node_id` |

## `workspace/skills/legal_summarizer/scripts/document/loader.py` — 81 LOC (code 64)
- module: `workspace.skills.legal_summarizer.scripts.document.loader`
- docstring: DocumentLoader — единственный canonical loader для legal_summarizer. Создаёт ``PhysicalDocument`` за **один проход** по файлу. ``DocumentLoader.load()`` — единая production загрузочная цепочка. Делает один проход для blo
- static importers (1): `workspace/skills/legal_summarizer/scripts/application/pipeline_structure.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 0

### class `DocumentLoader` — lines 28-78 (51 LOC), 1 methods
- bases: object
- decorators: —
- docstring: Canonical loader для ``PhysicalDocument``. Single-pass loading: ``_iter_*_blocks`` парсит файл один раз, и тот же blocks-iteration даёт текст для title resolution (через ``_pick_title_from_text``). Никакого второго вызов
- name referenced in 7 file(s); tests: 5

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `load` | 42-78 | `(self, path: str | Path, *, workspace_root: Path | str | None=None) -> PhysicalDocument` | 8 | 0 | 10 | — |

## `workspace/skills/legal_summarizer/scripts/document/section_helpers.py` — 62 LOC (code 49)
- module: `workspace.skills.legal_summarizer.scripts.document.section_helpers`
- docstring: Canonical section helpers для DocumentStructure. Чистые функции на ``DocumentStructure``: индекс секций (ids, headings, hierarchical paths), подсчёт meaningful sections, подсчёт sections. Раньше жили в ``application/sect
- static importers (2): `workspace/skills/legal_summarizer/scripts/application/execution_orchestration.py`, `workspace/skills/legal_summarizer/scripts/execution/map_reduce.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 3

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `section_index` | 16-40 | `(struct: DocumentStructure) -> tuple[list[str], dict[str, str], dict[str, str]]` | 9 | 3 | Build canonical section index: (ids, headings, paths) из DocumentStructure. ``paths`` — иерархические path вид |
| `count_meaningful_sections_canonical` | 43-48 | `(struct: DocumentStructure) -> int` | 4 | 2 | Meaningful sections: section nodes с non-empty title или >1 block. |
| `count_sections` | 51-55 | `(struct: DocumentStructure | None) -> int` | 2 | 2 | Число section-узлов в DocumentStructure (0 если None). |

## `workspace/skills/legal_summarizer/scripts/document/block_lookup.py` — 52 LOC (code 36)
- module: `workspace.skills.legal_summarizer.scripts.document.block_lookup`
- docstring: Lookup helpers — замена linear ``index()``. В legacy коде (``context_expansion.py``, ``cached_retrieval.py``) использовался ``doc.blocks.index(target)`` — O(N) на каждый chunk. Этот модуль предоставляет ``build_block_loo
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 1

### class `BlockLookup` — lines 24-34 (11 LOC), 2 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: O(1) lookup для ``DocumentBlock``.
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `get_by_ord` | 30-31 | `(self, ordinal: int) -> DocumentBlock | None` | 2 | 0 | 1 | — |
| `get_by_id` | 33-34 | `(self, block_id: str) -> DocumentBlock | None` | 2 | 0 | 1 | — |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `build_block_lookup` | 37-49 | `(doc: PhysicalDocument) -> BlockLookup` | 2 | 1 | Построить O(1) lookup для всех блоков документа. Для больших документов (10k+ blocks) это критично — позволяет |

## `workspace/skills/legal_summarizer/scripts/document/__init__.py` — 1 LOC (code 0)
- module: `workspace.skills.legal_summarizer.scripts.document`
- docstring: — NONE —
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 0

