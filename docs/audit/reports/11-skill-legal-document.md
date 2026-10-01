# Аудит: 11-skill-legal-document (`document/` слой скилла `legal_summarizer`)

## Сводка группы

Файлов: 18 · LOC: 4200 · классов: 20 · методов: 44 · функций (модульного уровня): 32 · вложенных функций: 5

Путь: `workspace/skills/legal_summarizer/scripts/document/`

**Ключевые находки** (каждая — с путём и строкой):

1. **Подсистема scoring'а heading'ов мертва в проде.** `apply_confidence_penalties` (heading.py:292), `HeadingEvidence` (332), `compute_evidence` (476), `apply_evidence_scoring` (562), `filter_above_threshold` (640) вызываются **только из `tests/`**. `run_canonical_pipeline` (application/pipeline_structure.py:283) зовёт единственный `detect_heading_candidates`. Следствие: `CONFIDENCE_THRESHOLD = 0.60` (heading.py:40) в проде не применяется ни к одному кандидату — любой кандидат с `block_index >= 0` становится секцией. Это ~300 LOC `heading.py` + весь `list_detection.py` (335 LOC) — около **16% слоя** — недостижимы из production path.
2. **`document/block_ownership.py` (109 LOC) — мёртвый модуль-дубль, ноль импортёров.** Весь репозиторий (`scripts/` + `tests/`) импортирует `build_block_ownership`/`owner_for_block`/`block_to_node` **только** из `document.structure` (см. chunking/chunker.py:31-34 и все 6 импортов в tests/test_block_ownership.py:15,101,113,126,139). Docstring «canonical block ownership» — ложный: канон закрепился за `structure.py`, который объявлен SoT-слоем.
3. **`DocumentStructure.block_to_node` (structure.py:297, 15L) дублирует модульную функцию `block_to_node` (structure.py:478, 12L) с точностью до docstring'а.** Обе живут в одном файле, обе экспортируются в `__all__` (500). Тест test_structure_models.py:135 + test_block_ownership.py:113 сравнивает их друг с другом, т.е. дубль закреплён тестом. Метод не имеет ни одного production-вызова; модульная функция — тоже (см. п.1 п.9 отчёта: потребитель — только `chunking/chunker.py` импортирует `build_block_ownership`/`owner_for_block`, но **не** `block_to_node`).
4. **Этап `safety_merge` не вызывается из точки сборки.** `application/pipeline_structure.py` не импортирует `document.safety_merge` вообще. Модуль (147 LOC) + `SafetyMergeConfig` живут только ради `tests/test_structure_safety_merge.py` (6 тестов). Более того, «схлопывание» там — **no-op**: `end_block=max(target.end_block, cur.end_block)` (safety_merge.py:108) при контигентных диапазонах, выданных `build_document_structure`, всегда даёт `target.end_block` — ни один range не расширяется, меняется только `cur.confidence = 0.0`.
5. **`document/block_lookup.py` (52 LOC) — полумёртв.** Ноль production-импортёров; `PhysicalDocument.blocks_by_ord` (physical.py:122-138) уже даёт O(1) lookup по ordinal и используется в проде (brief_context.py:378, structural_packing.py:463, chunker.py:276). `BlockLookup.by_ord` — чистый дубль. Единственная «уникальная» ценность — `by_id`, но читатель `DocumentBlock.block_id` в проде ровно один: `block_lookup.py:48`, то есть поле `block_id` (physical.py:81) тоже оказывается мёртвым.
6. **Валидация вычисляется и выбрасывается.** `validate_structure` (validation.py:121) зовётся на каждом cache-miss прогоне (pipeline_structure.py:308), результат кладётся в `PipelineResult.validation` (345) и в snapshot (216) — но **ни один потребитель в `scripts/` не читает `result.validation`**. `retrieval/quality.py:112` принимает `structure_is_valid`, но никто его не передаёт. При этом проверки overlap — O(N²) по парам секций.
7. **`repair_structure` содержит баг: отчёт может claims-ить правку, которой не было.** `changes` заполняется только через `_put` (repair.py:80-82); `_drop` лишь `changes.pop(nid, None)` (84-86). Если единственная проблема — невалидный диапазон, `_drop` удалит узел из `current_nodes`, но `changes` останется пустым → ранний `return struct, report` (74-75) вернёт **исходный** `struct` с невалидным узлом, тогда как `report.invalid_ranges_dropped == 1`.
8. **Три параллельные реализации чтения DOCX/PDF/PPTX title** в слое: `physical._pick_title_from_text` (physical.py:173-207), `title._metadata_title` (title.py:33-71) и `DocumentIdentity`/`manifest` — нет, точнее: первые две почти идентичны, а `title.resolve_title` (title.py:137) **повторно открывает файл** через `_metadata_title`, хотя `loader.load` уже вычислил `physical.title` (loader.py:68). Это прямо противоречит docstring'ам `loader.py:3-7` и `loader.py:31-34` («никакого двойного парсинга») и признано в `tests/test_structure_document_loader.py:64-67`.
9. **~28 устаревших ссылок в docstring'ах на несуществующие пути/символы** (см. § «Stale-ссылки»): каталога `scripts/structure/` **не существует** (проверено), `structure/models.py` не существует, `scripts/fingerprint.py::compute_fingerprint` удалён, `physical.py::_physical_cache_key` удалён, `sections.py::merge_short_sections` удалён, `cached_retrieval.py` удалён, `office_files.extract_structure` удалён, `_detect_candidates`/`_apply_confidence_penalties`/`_filter_above_threshold` никогда не существовали под этими именами. Слои переименовывали `domain/structure.py` → `structure.py` → `document/` и не дочистили.
10. **`repair.py` docstring описывает 4 вида починки, из которых реализован 1.** Упомянуты `_repair_numbering` (несуществующая функция), «level jumps», «overlapping ranges», «duplicate headings (тот же start_block)» — реализовано только reparent/orphan + drop невалидных диапазонов. Поле `RepairReport.numbering_glued` (repair.py:52) **всегда 0** — единственный путь, который его заполнил бы, удалён.
11. **`_RE_NUMBERED_LEVEL_1/2` продублированы** в `heading.py:45-46` и `list_detection.py:44-45` — байт-в-байт. При этом docstring `numbering.py:3-6` объявляет `numbering.py` **единым** модулем регулярных выражений и прямо обещает «из нового единого модуля они импортируются отсюда» — обещание не выполнено.
12. **Функциональный баг: `page_count == 0` для любого непустого DOCX.** `_iter_docx_blocks` (physical.py:316) строит `para_to_page` (338-341), который **всегда** возвращает `None`; `page_idx` у всех блоков → `None`; итог `page_count = max((b.page_index or 0) ...)` (411-414) даёт `0` при непустых блоках и `1` при пустых. Docstring (108-109) обещает «оценочное max page_index». Влияет на `pdf_outline._resolve_destination_page` и на любые потребители `page_count`.
13. **Кросс-подсистемный баг (подтверждает находку другого аудитора):** `workspace/utils/structure_cache.py:7` импортирует `workspace.utils.office_files.extract_structure` — функции в `office_files.py` **больше нет** (в файле только `detect_format`, `extract_text`, `extract_tables`, `summarize`, …). Модуль падает с `ImportError` при первом же импорте. `physical.py:214` явно называет `office_files.extract_structure` «ранее существовавшим» — то есть удаление состоялось, а этот модуль остался.
14. **`MappedOutlineCandidate.to_heading_candidate` (pdf_outline.py:99-113) — мёртвый метод.** `mapped_to_heading_candidates` (334) инлайнит ту же конверсию (346-362) вместо вызова. Метод не покрыт ни одним тестом. Docstring модуля (pdf_outline.py:9-14) при этом утверждает, что реализованы дедупликация по destination и приоритет outline над конфликтующим heading — **ни того, ни другого в коде нет** (есть только запись строки в `diagnostics: list[str]`, который никто не читает).
15. **Мёртвый кластер в `hierarchy.py` (54 LOC, 4 функции).** `_effective_level` (91), `_resolve_level` (212) и их эксклюзивные помощники `_scheme_priority` (62), `_level_from_numbering` (78) не имеют ни одного вызова во всём репозитории. Реальную иерархию строит `_heading_rank` (118) + `_build_parents_by_stack` (159), а `level` всех секций перезаписывается `depth_by_id` (441-443) и сразу затирает `level=1` из (365). Поля `StructureTreeBuilderConfig.default_section_level` (187) и `include_body_nodes` (188) не читаются нигде.

**Вердикты:** Оставить 47 · Упростить 14 · Удалить 24 · Перенести 2 · Слить с `<файл>` 1

---

## `workspace/skills/legal_summarizer/scripts/document/__init__.py` — 1 LOC

**Назначение.** Пакетный маркер слоя `document/`.
**Что делает.** Ничего: файл содержит один перевод строки, ни импортов, ни `__all__`.
**Зачем нужен.** Каталог должен быть пакетом (skill запускается как `python scripts/cli.py`, корень в `sys.path` — `scripts/`, поэтому `document.*` резолвится).
**Вердикт.** `Оставить`
**Обоснование.** Функционально нужен, но **отсутствие re-export'ов** — отдельная находка: `__all__` пуст, поэтому ни `from document import DocumentStructure`, ни `from document import validate_structure` не работают; все 60+ импортов в скилле идут в глубь по полным путям. Если слою нужна точка входа — её нет.

---

## `workspace/skills/legal_summarizer/scripts/document/structure.py` — 506 LOC

**Назначение.** Единый контракт семантической структуры документа (SoT-модель) + хелперы владения блоками.
**Что делает.** Определяет 4 value-объекта (`StructureEvidence`, `NumberingInfo`, `DocumentTitle`, `StructureNode`) и контейнер `DocumentStructure` с сериализацией в/из dict; ниже — 4 функции построения карты владения блоками. Побочных эффектов нет (чистые структуры, frozen dataclasses).
**Зачем нужен.** Через `DocumentStructure` проходят **все** downstream'ы: chunker, retrieval, application/*, cache snapshot. Удаление ломает весь скилл.
**Вердикт.** `Упростить`
**Обоснование.** Модель SoT оправдана, но модуль тащит на себе 4 мёртвые/дублирующие функции (см. ниже) и содержит 2 ложных docstring'а (`domain/structure.py` в 398, `SectionTree/DocumentSection/HeadingCandidate` в 19).
**Доказательства.** Импортёры: `chunking/chunker.py`, `chunking/structural_packing.py`, `application/brief_context.py`, `application/execution_orchestration.py`, `application/pipeline_structure.py`, `application/inspection.py`, `retrieval/index.py`, `document/{heading,hierarchy,loader,title,repair,validation,safety_merge,section_helpers,analysis}.py` — 20+ файлов. Тесты: `test_structure_models.py`, `test_roundtrip_serialization.py`, `test_structure_hierarchy_invariants.py`, `test_block_ownership.py`, `test_chunk_ownership.py`.

#### class `StructureEvidence` (строки 30–49)
Назначение: один элемент доказательства присутствия заголовка (source + weight + detail).
Зачем нужен: сериализуется в `StructureNode.to_dict` / `from_dict` (163, 194) → часть snapshot-контракта на диске.
Вердикт: `Упростить`
Обоснование: **фактически всегда 1-элементный tuple**. Единственный производитель — `hierarchy._evidence_from_candidate` (191-209), возвращающий кортеж из одного элемента безусловно. Абстракция «список доказательств» не используется по назначению; поле `StructureNode.evidence` — всегда 1-tuple. Можно свернуть в три плоских поля на `StructureNode` (`evidence_source`, `evidence_weight`, `evidence_detail`) — минус ~25 LOC, минус реконструкция frozen-dataclass в трёх местах.

#### class `NumberingInfo` (строки 52–83)
Атрибуты одной строкой: `scheme: str`, `raw: str`, `level: int | None`, `components: tuple[int, ...] = ()`, `text: str = ""`, плюс `to_dict`/`from_dict` внутри (методы не объявлены как `def` верхнего уровня — присутствуют в брифе как методы 0-го уровня; см. примечание ниже).
Назначение: результат разбора номера заголовка («Статья 12. Права сторон» → scheme + level + components + текст).
Зачем нужен: `structure.numbering` сериализуется в snapshot; `hierarchy` строит из него `semantic_type` и `ordinals`.
Вердикт: `Оставить`
Обоснование:SoT-контракт, покрыт `tests/test_structure_numbering.py` (12 ассертов) и `test_roundtrip_serialization.py`.

#### class `DocumentTitle` (строки 86–109)
Атрибуты: `value: str`, `source: str = "unknown"`, `confidence: float = 0.0`, `block_ordinal: int | None = None`.
Назначение: типизированный результат резолва заголовка документа.
Зачем нужен: `DocumentStructure.title` (253+), `title.resolve_title` (title.py:120).
Вердикт: `Оставить`
Обоснование: единственный источник `source`/`confidence`/`block_ordinal`; удаление ломает `structure.to_dict` и 3 подписанта в `title.py`.

#### class `StructureNode` (строки 112–250)
Атрибуты: `node_id`, `node_type`, `semantic_type`, `level`, `title`, `number`, `parent_id`, `children`, `start_block`, `end_block`, `confidence`, `evidence`, `source_refs`.
Назначение: узел дерева секций.
Зачем нужен: центральная единица всей семантической модели; `children` + `parent_id` дублируют друг друга (см. `repair._rebuild_children`).
Вердикт: `Оставить`
Обоснование: незаменим. Замечание (не отдельный вердикт): **дублирование `children`/`parent_id`** — единственный источник рассинхрона, который `repair._rebuild_children` (88-106) обязан латать при каждом reparent.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `to_dict` | 163–192 | Сериализация узла | контракт snapshot на диске | `DocumentStructure.to_dict` (326), `execution_orchestration.py:272`, `test_roundtrip_serialization.py:168` | Оставить |
| `from_dict` | 194–250 | Десериализация узла | cache-hit path | `DocumentStructure.from_dict` (344), `pipeline_structure.py:164` | Оставить |

#### class `DocumentStructure` (строки 253–415)
Атрибуты: `document_id`, `title`, `nodes`, `root_id`, `preamble_node_id`, `numbering`, `total_blocks`, `coverage_ratio`.
Назначение: корень структуры документа.
Зачем нужен: SoT всего скилла.
Вердикт: `Упростить`
Обоснование: два поля мертвы, одно дублирует метод (см. таблицу).

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `get_node` | 277–278 | `self.nodes.get(node_id)` | — | **только** `tests/test_structure_models.py:102-103` | **Удалить** — 2-строчный accessor; `struct.nodes.get(id)` уже используется в проде (`section_helpers.py:38`, `validation.py:159`). Удаление ломает 1 тест, строки 74, 102-103 |
| `iter_nodes` | 280–282 | список всех узлов | — | **только** `test_structure_models.py:74` | **Удалить** — нет ни одного production-вызова во всём репозитории; заменяется `list(struct.nodes.values())` |
| `iter_sections` | 284–289 | `node_type == "section"` | публичный контракт обхода секций | `cli.py:373`, `estimation.py:65`, `service.py:208`, `execution_orchestration.py:271`, `section_helpers.py:26,46,55`, `test_chunk_ownership.py:86,114` | Оставить |
| `iter_children` | 291–295 | дети узла по `children` | упаковка чанков | `structural_packing.py:79,125,302` | Оставить |
| `block_to_node` | 297–311 | карта block→node_id | — | **только** `tests/test_structure_models.py:135` | **Удалить** — дубль модульной `block_to_node` (478). Точный порядок: сначала удалить метод (297-311) + его `__all__`-экспорт отсутствует, поправить `test_structure_models.py:123-135`; модульную функцию оставить каноном (см. § Про `block_to_node`) |
| `to_dict` | 313–342 | Сериализация структуры | snapshot | `pipeline_structure.py:214`, `test_roundtrip_serialization.py:225,265` | Оставить |
| `from_dict` | 344–415 | Десериализация | cache-hit | `pipeline_structure.py:164` | Оставить |

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_depth_of` | 418–426 | глубина узла по parent-chain | только для `build_block_ownership` | `build_block_ownership` (437); копия — `block_ownership.py:31-39` | **Слить с `block_ownership.py`** → нет, инверсия: см. ниже |
| `build_block_ownership` | 428–450 | map block→section node_id | канон | `chunking/chunker.py:32` (реэкспорт в `chunker.__all__` 604) → `tests/test_chunk_ownership.py`; копия — `block_ownership.py:41-63` | **Оставить** (канон) — удалить дубль `block_ownership.py:41-63` |
| `owner_for_block` | 452–476 | deepest-owner для блока | канон | `chunking/chunker.py:33`; копия — `block_ownership.py:65-89` | **Оставить** (канон) — удалить дубль `block_ownership.py:65-89` |
| `block_to_node` | 478–489 | плоская карта block→node | **0 production-вызовов** | только `tests/test_block_ownership.py:126`; копия — метод (297) и `block_ownership.py:91-102` | **Удалить** вместе с методом (297-311) и `block_ownership.py:91-102`; `test_block_ownership.py:126-137` удалить/переписать |
| `_make_node_id` | 505–506 | `f"n_{i:04d}"` | генерация id узлов | `hierarchy.py:54, 372` | Оставить (замечание: стоит **после** `__all__` (492) — стилистическая аномалия) |

**Про порядок удаления `block_ownership.py` (точная последовательность):**

1. `block_ownership.py` **не имеет ни одного импортёра** — ни в `scripts/`, ни в `tests/`. Подтверждено: `grep "build_block_ownership|owner_for_block|block_to_node"` по всему репозиторию даёт совпадения только в `document/structure.py`, `document/block_ownership.py`, `chunking/chunker.py` (строки 32-33, 604-605) и `tests/{test_chunk_ownership,test_block_ownership,test_structure_models}.py`; все 6 импортов в `test_block_ownership.py` (строки 15, 101, 113, 126, 139) идут из `document.structure`, **не** из `document.block_ownership`. Имя файла-теста вводит в заблуждение.
2. Канон — `document.structure`, потому что: (а) его собственный module docstring (structure.py:19-23) объявляет `DocumentStructure` SoT; (б) `chunking/chunker.py:31-34` импортирует `build_block_ownership`/`owner_for_block` оттуда и реэкспортирует их в `chunker.__all__` (604-605); (в) `structure.py:398` помечает секцию комментарием `# Block ownership (formerly domain/structure.py)` — то есть ownership **изначально** жил в этом файле, а `block_ownership.py` — отщеплённая копия, которая не «победила», вопреки своему docstring-у «canonical block ownership».
3. Шаги удаления (в этом порядке, каждый атомарен):
   - **(1)** Удалить `DocumentStructure.block_to_node` (297-311). Что останется: модульная `block_to_node` (478-489). Что сломается: `tests/test_structure_models.py:123-135`.
   - **(2)** Удалить модульную `block_to_node` (478-489) + строку `"block_to_node"` из `__all__` (500). Что сломается: `tests/test_block_ownership.py:126-137` (`test_block_to_node_*`).
   - **(3)** Удалить файл `document/block_ownership.py` целиком (109 LOC, 4 функции, 1 `__all__`). Что сломается: **ничего** — 0 импортёров.
   - **(4)** Поправить docstring'ы: `block_ownership.py:21` (`domain.structure.block_to_node`) исчезает вместе с файлом; в `structure.py:398` заменить на «canonical block ownership».
   - **Предварительная работа:** удалить/переписать ~15 тестовых функций в трёх файлах (`test_block_ownership.py` — 5 импортов, `test_chunk_ownership.py:104-171`, `test_structure_models.py:123-135`). Альтернатива, не требующая тестовых правок: оставить один-единственный `block_to_node` и **переименовать** его в метод — тогда меняется только `test_block_ownership.py:126-137` (импорт `from document.structure import block_to_node` → вызов метода).
4. **Побочная выгода:** `structure.py` перестаёт быть «SoT-моделью + 4 утилиты владения» и реально становится единым контрактом; исчезает второй источник правды при будущих правках алгоритма владения.

---

## `workspace/skills/legal_summarizer/scripts/document/block_ownership.py` — 109 LOC

**Назначение.** Заявлено как «canonical block ownership»: карта block→section, deepest-owner, плоская карта block→node.
**Что делает.** Четыре функции: `_depth_of` (31), `build_block_ownership` (41), `owner_for_block` (65), `block_to_node` (91). Первые две — **байт-в-байт идентичны** `structure.py:418-450` (это и есть зафиксированный в `duplicates.md` хэш `67c685f78aa53ecd`, 22L). `owner_for_block` и `block_to_node` — тоже копии (`structure.py:452-489`).
**Зачем нужен.** По факту — ничего: ни одного импорта за пределами самого файла.
**Вердикт.** `Удалить`
**Обоснование.** Модуль-дубль, чей собственный docstring («canonical») опровергается единственным production-импортёром функций — `chunking/chunker.py:31-34`, который берёт их из `document.structure`. Удаление безопасно: 0 импортёров в `scripts/`, 0 в `tests/`, 0 в `docs/`, 0 в CI; не точка входа; не сканируется каталогом.
**Доказательства.** `grep` по всему репозиторию: 0 совпадений `from document.block_ownership` / `import document.block_ownership`. Файл не входит ни в один `__all__` верхнего уровня. `tests/test_block_ownership.py` — одноимённый, но импортирует `document.structure` (строки 15, 101, 113, 126, 139).
**Что останется:** `structure.py:418-489` (4 функции) без изменений. **Что сломается:** ничего в проде. **Предварительная работа:** см. п. (1)-(4) в разделе «Про порядок удаления» выше (там же — вариант без тестовых правок).

---

## `workspace/skills/legal_summarizer/scripts/document/physical.py` — 456 LOC

**Назначение.** Нормализованный физический список блоков: adapter от PDF/DOCX/TXT к единому `DocumentBlock`-списку.
**Что делает.** Диспетчеризация по формату в `_iter_*_blocks` (246, 316, 422), конвертация таблиц в текст (`_table_to_text` 236), извлечение title (`_pick_title_from_text` 173), кэшируемый `blocks_by_ord` (122-138) с записью в frozen-объект через `object.__setattr__` (133-137). Побочный эффект: `Document(p)`/`PdfReader`/`pptx.Presentation` открываются по одному разу на формат внутри итераторов, плюс **повторно** в `_pick_title_from_text` (180, 199).
**Зачем нужен.** Первый шаг любого pipeline-прогона (`loader.py:58-63`).
**Вердикт.** `Упростить`
**Обоснование.** Модуль функционален, но содержит 3 мёртвые приватные функции, 2 мёртвых импорта и 1 мёртвый локал в горячем пути DOCX.

#### class `DocumentBlock` (строки 60–94)
Атрибуты: `block_id`, `block_type`, `content`, `char_count`, `page_index`, `page_start`, `page_end`, `paragraph_index`, `table_index`, `ordinal`, `block_metadata`.
Назначение: один блок документа.
Зачем нужен: единица chunking'а и вся `PhysicalDocument`.
Вердикт: `Упростить`
Обоснование: поля `block_id`, `page_start`, `page_end` не читаются ни одним production-кодом. `block_id` читается ровно в одном месте — `block_lookup.py:48`, в мёртвом модуле. `page_start`/`page_end` — docstring (72-73) обещает «всегда равны `page_index`», но для DOCX они `None` (385, 397), а для PDF `_iter_pdf_blocks` их не заполняет вовсе. Удаление трёх полей: ~10 LOC минус, ломает только `test_structure_document_loader.py`-фикстуры и `block_lookup.py:48`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `to_dict` | 93–94 | `asdict(self)` | snapshot | `PhysicalDocument.to_dict` (147) | Оставить |

#### class `PhysicalDocument` (строки 98–161)
Атрибуты: `path`, `format`, `title`, `size_bytes`, `blocks`, `page_count`, `_blocks_by_ord_cache` (private, `compare=False`).
Назначение: корень физической модели документа.
Зачем нужен: вход `ChunkPlanner.plan`, `RetrievalIndex.build`, snapshot.
Вердикт: `Оставить`
Обоснование: поле `title` заполняется (loader.py:74), но **не читается** в проде — `title.resolve_title` (title.py:137) заново открывает файл вместо использования `doc.title`. Это отдельная кросс-модульная находка (см. `title.py`).

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `blocks_by_ord` | 122–138 | ленивый O(1)-lookup по `ordinal` | горячий путь | `brief_context.py:378,395,440,443,485`, `structural_packing.py:463`, `chunking/chunker.py:276` | Оставить |
| `to_dict` | 140–149 | Сериализация | snapshot | `pipeline_structure.py:229` | Оставить |
| `from_dict` | 151–160 | Десериализация | cache-hit | `pipeline_structure.py:163` | Оставить |

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_physical_cache_root` | 163–165 | путь к каталогу кэша | — | **0 вызовов в репозитории** | **Удалить** (3 LOC) |
| `_identity_for` | 168–170 | `DocumentIdentity.from_path(p)` | — | **0 вызовов в репозитории** | **Удалить** (3 LOC) — pipeline делает то же самое явно (`pipeline_structure.py:281`) |
| `_pick_title_from_text` | 173–207 | title из метаданных + fallback на первую строку | заголовок для `PhysicalDocument.title` | `loader.py:23, 68`; тесты `test_structure_document_loader.py:9,64,67` | **Слить с `title.py`** — функция на 90% идентична `title._metadata_title` (title.py:33-71): обе читают `docx.core_properties.title`, `PdfReader.metadata['/Title']`, `pptx.core_properties.title`. Разница — только fallback на первую непустую строку текста (physical.py:203-207) vs `_first_nonempty_line_fallback` (title.py:106-117), которые сами почти идентичны. Выигрыш: один чтец метаданных вместо двух; побочный эффект — исчезает двойное открытие файла (см. `title.py`) |
| `_build_structure_dict` | 210–233 | legacy-словарь `title/begin/end/text` | — | **0 вызовов** (ни в `scripts/`, ни в `tests/`) | **Удалить** (24 LOC) — возвращает `{"begin": "", "end": "", "text": ""}` хардкодом для DOCX (228-232), т.е. это заглушка удалённого `office_files.extract_structure` |
| `_table_to_text` | 236–244 | склейка строк таблицы в текст | внутреннее | `_iter_pdf_blocks` (301), `_iter_docx_blocks` (397) | Оставить |
| `_iter_pdf_blocks` | 246–314 | PDF → blocks (pdfplumber) | internal | `loader.py:20, 59` | Оставить |
| `_iter_docx_blocks` | 316–420 | DOCX → blocks (python-docx) | internal | `loader.py:20, 61` | **Упростить** — 2 мёртвых локала + баг `page_count` (см. ниже) |
| `_iter_txt_blocks` | 422–456 | TXT → blocks | internal | `loader.py:22, 63` | **Упростить** — при `page_count = 1` (425) единственный блок имеет `page_index=None` (433) |

**Функциональные баги `physical.py`:**
- **DOCX `page_count` всегда 0.** (a) `para_to_page` (338-341) — словарь, который **никогда не заполняется** и всегда возвращает `None`; (b) `page_idx = para_to_page.get(p_idx)` (354) → `None` у всех paragraph-блоков; таблицы тоже получают `page_index=None` (382); (c) `page_count = max((b.page_index or 0) for b in blocks, default=0)` (411-414) → `0` при непустых блоках, `1` при пустых. Docstring (108-109) обещает «оценочное max page_index». Влияет на `pdf_outline._resolve_destination_page` (116) и на потребителей `page_count`.
- **Мёртвые локали в `_iter_docx_blocks`:** `p_idx_seen` (347, 358) и `t_idx_seen` (348, 385) вычисляются и не читаются. `para_to_page` — мёртвый словарь.
- **Мёртвые импорты модуля:** `json` (40) — ни одного использования; `extract_tables` (47-49 из `office_files`) — ни одного использования (все таблицы читаются напрямую через pdfplumber / python-docx); `detect_format` (46) — ни одного использования (формат определяется в `loader.py:51`).

---

## `workspace/skills/legal_summarizer/scripts/document/loader.py` — 81 LOC

**Назначение.** Единственный canonical loader: `path` → `PhysicalDocument`.
**Что делает.** Валидирует формат через `workspace.utils.office_files.detect_format`, диспетчеризует в `_iter_*_blocks`, склеивает `text` из блоков, вызывает `_pick_title_from_text`. Побочный эффект: `p.stat()` (69) и один полный проход по блокам ради склейки текста (67).
**Зачем нужен.** Единственная точка входа в pipeline (`pipeline_structure.py:279-280`).
**Вердикт.** `Упростить`
**Обоснование.** 81 LOC на 3 шага, при этом docstring'ы (3-7, 31-34) **лгут про single-pass**: `title` вычисляется из метаданных, а `_pick_title_from_text` открывает файл заново — признано в `tests/test_structure_document_loader.py:64-67` («Допускается ≤ 2, не строго 1»).

#### class `DocumentLoader` (строки 28–78)
Вердикт: `Оставить` (это правильная точка входа; проблемы — в сигнатуре и в приватных импортах)

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `load` | 42–78 | `path` → `PhysicalDocument` | вход pipeline | `pipeline_structure.py:280` | **Упростить** — 3 правки: (1) **удалить параметр `workspace_root`** (44) — принимается и **ни разу** не используется в теле; вызывающая сторона передаёт его (`pipeline_structure.py:280`); (2) **удалить мёртвую ветку** `else: raise ValueError(f"Unsupported format: {fmt}")` (64-65) — недостижима, т.к. 52-56 уже отфильтровал всё вне `SUPPORTED_FORMATS`; (3) заменить `from document.physical import (_iter_docx_blocks, _iter_pdf_blocks, _iter_txt_blocks, _pick_title_from_text)` (20-23) на **публичные** имена — кросс-модульный доступ к приватному API соседнего модуля (4 приватных символа). Опционально: убрать склейку `text` (67) — единственный её потребитель, `_pick_title_from_text` (68), использует `text` только как fallback |

---

## `workspace/skills/legal_summarizer/scripts/document/heading.py` — 660 LOC

**Назначение.** Обнаружение кандидатов в заголовки + (неиспользуемая) evidence-scoring подсистема.
**Что делает.** `detect_heading_candidates` (197) классифицирует блоки по regex/DOCX-стилю/PDF-outline и возвращает `list[HeadingCandidate]`. Вторая половина модуля (292-658) — расчёт evidence и фильтрация по порогу.
**Зачем нужен.** Шаг 2 pipeline. `detect_heading_candidates` — безусловно нужен. Вторая половина — нет.
**Вердикт.** `Упростить` (для `detect_heading_candidates`) + `Удалить` (для scoring-подсистемы)
**Обоснование.** Ключевая находка аудита: **scoring не вызывается из прода**. `grep "apply_confidence_penalties|apply_evidence_scoring|filter_above_threshold"` по `scripts/` даёт **ноль** совпадений вне самого `heading.py`; все вызовы — в 7 тестовых файлах. Следствие: `CONFIDENCE_THRESHOLD = 0.60` (40) не применяется в проде, `heading.py:105` утверждает, что score «0.55 (ниже `CONFIDENCE_THRESHOLD=0.60`). Чтобы пройти…» — в проде этот порог не существует.

#### class `HeadingCandidate` (строки 60–68)
Атрибуты: `block_index: int`, `text: str`, `score: float`, `source: str`, `level: int`, `raw_number: str | None = None`.
Назначение: плоская запись «здесь может быть заголовок».
Зачем нужен: контракт `detect_heading_candidates` → `build_document_structure`.
Вердикт: `Оставить`
Обоснование: незаменим; единственный носитель `source`, по которому `hierarchy._resolve_semantic_type` (221) и `_evidence_from_candidate` (191) выводят `semantic_type` и `weight`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| — | — | методов нет (frozen dataclass без методов) | — | — | — |

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_is_docx_heading_style` | 71–75 | стиль `Heading N` | классификация | `detect_heading_candidates` | Оставить |
| `_is_docx_title_style` | 78–88 | стиль `Title`/`Subtitle` | классификация | `compute_evidence` (521) — **только из мёртвой подсистемы** | **Удалить** вместе с `compute_evidence` |
| `_looks_like_heading` | 91–93 | эвристика «похоже на заголовок» | — | **0 вызовов** (только `__all__` 658) | **Удалить** (3 LOC) |
| `_classify_regex` | 96–140 | regex-классификация заголовка | ядро детектора | `detect_heading_candidates` (252) | Оставить (замечание: использует **копию** `_RE_NUMBERED_LEVEL_1/2` из `numbering.py`; см. дублирование ниже) |
| `_extract_pdf_outline` | 143–194 | PDF outline → кандидаты **без** маппинга на блоки | legacy-фолбэк | `detect_heading_candidates` (285) — **только** когда `physical_doc is None`; в проде `physical_doc` всегда передан (`pipeline_structure.py:285`) | **Удалить** (52 LOC) — см. ниже |
| `detect_heading_candidates` | 197–289 | публичный детектор | шаг 2 pipeline | `pipeline_structure.py:283` | **Упростить** — после удаления `_extract_pdf_outline` (285) сделать `physical_doc` **обязательным** параметром; тогда 3 `if physical_doc is not None` ветки (281, 285) схлопываются |
| `apply_confidence_penalties` | 292–329 | штрафы за «голые» заголовки | — | **только тесты** | **Удалить** (38 LOC) |
| `_is_short` | 402–403 | длина ≤ порога | — | `compute_evidence` | **Удалить** |
| `_looks_like_heading_typography` | 406–421 | типографская эвристика | — | `compute_evidence` | **Удалить** |
| `_is_substantial_body` | 424–425 | длина body-блока | — | `compute_evidence` | **Удалить** |
| `_collect_previous_heading_text` | 428–440 | текст предыдущего заголовка | — | `compute_evidence` | **Удалить** |
| `_numbering_consistency_with_neighbors` | 443–473 | согласованность нумерации | — | `compute_evidence` | **Удалить** |
| `compute_evidence` | 476–547 | расчёт 9-компонентного evidence | — | `apply_evidence_scoring` (597) | **Удалить** (72 LOC) |
| `_looks_like_explicit_legal_marker` | 550–559 | «Статья/Глава/Раздел» | — | `compute_evidence` (539) | **Удалить** |
| `apply_evidence_scoring` | 562–637 | evidence → score | — | **только тесты** | **Удалить** (76 LOC) |
| `filter_above_threshold` | 640–643 | фильтр по `CONFIDENCE_THRESHOLD` | — | **только тесты** | **Удалить** (4 LOC) |

| Вложенная функция | Строки | Родитель | Вердикт |
|---|---|---|---|
| `_walk` | 166–188 | `_extract_pdf_outline` | **Удалить** вместе с родителем |

#### class `HeadingEvidence` (строки 332–399)
Атрибуты: `short_text`, `docx_style`, `typography`, `body_below`, `numbering_consistency`, `explicit_legal_marker`, `list_penalty` + производные `total_delta` (378) и `final_score` (392).
Назначение: структурированное доказательство «это заголовок».
Зачем нужен: **только** внутри мёртвой scoring-подсистемы.
Вердикт: `Удалить` (68 LOC)
Обоснование: единственный потребитель — `apply_evidence_scoring` (597), который никогда не вызывается в проде.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `total_delta` | 378–389 | сумма 7 дельт | — | `final_score` (394) | **Удалить** |
| `final_score` | 392–399 | итоговый score | — | `apply_evidence_scoring` (599) | **Удалить** |

**Итого по `heading.py`:** удалить **367 LOC из 660** (55%): `_looks_like_heading` (3), `_is_docx_title_style` (11), `_extract_pdf_outline`+`_walk` (52), `apply_confidence_penalties` (38), `HeadingEvidence`+2 метода (68), `_is_short` (2), `_looks_like_heading_typography` (16), `_is_substantial_body` (2), `_collect_previous_heading_text` (13), `_numbering_consistency_with_neighbors` (31), `compute_evidence` (72), `_looks_like_explicit_legal_marker` (10), `apply_evidence_scoring` (76), `filter_above_threshold` (4), `CONFIDENCE_THRESHOLD` (40). Что останется: `HeadingCandidate`, `_is_docx_heading_style`, `_classify_regex`, `detect_heading_candidates` — ~290 LOC, полностью покрывающие production path. Что сломается: 7 тестовых файлов (`test_heading_legal_document.py`, `test_heading_nk_long_paragraphs.py`, `test_integration_nk_realistic.py`, `test_structure_chunking_nk_smoke.py`, `test_structure_no_empty_reduce.py`, `test_structure_low_quality_pdf.py`) — они проверяют **несуществующий в проде** pipeline; `CONFIDENCE_THRESHOLD` упоминается в `test_heading_nk_long_paragraphs.py:18` в тексте docstring-описания.

**Ответ на вопрос 3 брифа (багфикс `heading._extract_pdf_outline` → `pdf_outline.py`):**
Формально багфикс есть: `pdf_outline.map_pdf_outline` (216) реально маппит outline на реальные `block_index` через `_find_nearest_block_on_page` (153), тогда как legacy `_extract_pdf_outline` (143-194) жёстко ставит `block_index=-1` (180) — ровно тот баг, который описывает docstring `heading.py:216-221`. **Но** именно поэтому legacy-функция и мертва: `build_document_structure` отбрасывает всё с `block_index < 0` (`hierarchy.py:342` — `accepted = [c for c in candidates if c.block_index >= 0]`). То есть legacy-ветка (285) может **только** вернуть кандидатов, которые тут же будут отброшены. Мёртвый код от багфикса в `heading.py` **не вычищен** — это ровно искомая находка. Дополнительно внутри мёртвой функции: `ordinal_counter` (160, 167, 188) инкрементируется и **ни разу не читается**; вложенная `_walk` (166) дублирует `pdf_outline._walk_outline` (180) по логике.

**Дублирование, не попавшее в `duplicates.md`:** `_RE_NUMBERED_LEVEL_1 = r"^\s*(\d+)\.\s+(.{2,200})$"` и `_RE_NUMBERED_LEVEL_2 = r"^\s*(\d+)\.(\d+)\.?\s+(.{2,200})$"` определены **дважды** — `heading.py:45-46` и `list_detection.py:44-45` (идентичны посимвольно). При этом docstring `numbering.py:3-6` объявляет `numbering.py` единственным модулем и обещает импорт оттуда — обещание **не выполнено**. Второе расхождение по этой же теме: в `numbering.py:56-71` те же regex записаны с `(.*)$` вместо `(.{2,200})$`, т.е. без ограничения длины — два разных поведения на одном входе.

---

## `workspace/skills/legal_summarizer/scripts/document/hierarchy.py` — 520 LOC

**Назначение.** `StructureTreeBuilder`: плоский список кандидатов → `DocumentStructure`.
**Что делает.** Ранжирует заголовки (`_heading_rank` 118), строит parent-связи стеком (`_build_parents_by_stack` 159), конструирует узлы, логирует диагностику (`_log_structure_diagnostics` 255). Побочный эффект: `logger.info` + `logger.warning` на каждом прогоне (283, 293).
**Зачем нужен.** Шаг 3 pipeline, самый нагруженный по смыслу этап.
**Вердикт.** `Упростить`
**Обоснование.** Ядро нужно, но 4 мёртвые функции, 2 мёртвых поля конфига и 3 полных 14-полевых пересборки `StructureNode` (из-за `frozen=True` без `dataclasses.replace`).

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_scheme_priority` | 62–76 | приоритет legal-схемы (1..6) | — | **только** `_effective_level` (112) | **Удалить** (15 LOC) |
| `_level_from_numbering` | 78–88 | level из decimal-компонентов | — | **только** `_effective_level` (114) | **Удалить** (11 LOC) |
| `_effective_level` | 91–115 | «эффективный» level кандидата | — | **0 вызовов в репозитории** | **Удалить** (25 LOC) — заменена `_heading_rank` (118) + `_build_parents_by_stack` (159) |
| `_heading_rank` | 118–157 | монотонный ранг глубины | ядро иерархии | `build_document_structure` (352), `_build_parents_by_stack` | Оставить |
| `_build_parents_by_stack` | 159–180 | stack-алгоритм parent'ов | ядро иерархии | `build_document_structure` (354) | Оставить |
| `_evidence_from_candidate` | 191–209 | кандидат → `StructureEvidence` | заполнение поля | `build_document_structure` (409) | **Упростить** — всегда возвращает 1-элементный tuple; см. `StructureEvidence` |
| `_resolve_level` | 212–218 | level с учётом source | — | **0 вызовов** | **Удалить** (7 LOC) — `level` перезаписывается `depth_by_id` (441-443) |
| `_resolve_semantic_type` | 221–252 | source → semantic_type | публичное поле | `build_document_structure` (371) | Оставить |
| `_log_structure_diagnostics` | 255–300 | info+warning по плотности секций | диагностика | `build_document_structure` (345) | Оставить (побочный эффект: 2 записи в лог на каждый прогон; приемлемо) |
| `build_document_structure` | 303–514 | **публичная точка** сборки | шаг 3 pipeline | `pipeline_structure.py:287`; тесты `test_structure_hierarchy_invariants.py`, `test_chunk_ownership.py` | **Упростить** — см. ниже |

#### class `StructureTreeBuilderConfig` (строки 183–188)
Атрибуты: `document_id: str` (без default), `default_section_level: int = 1`, `include_body_nodes: bool = True`.
Назначение: параметры builder'а.
Зачем нужен: публичный контракт; создаётся в `pipeline_structure.py:290` и внутри самого builder'а (336), покрыт `test_structure_hierarchy_invariants.py:216`.
Вердикт: `Упростить`
Обоснование: **`default_section_level` (187) и `include_body_nodes` (188) не читаются нигде** — ни в `build_document_structure`, ни в тестах. Из трёх полей работает одно. Удаление двух полей ничего не ломает (все конструкторы используют только `document_id`).

**Упрощения `build_document_structure`:**
- **3 полных пересборки `StructureNode`** (410-428, 442-455, 469-482) по 14 полей — прямое следствие `frozen=True` без `dataclasses.replace`. Замена на `dataclasses.replace(node, level=..., parent_id=..., ...)` даёт −30 LOC и убирает риск забыть поле при добавлении нового (сейчас `StructureNode` имеет 14 полей, скопированных вручную в трёх местах — источник рассинхрона).
- **`preamble_node_id=root.node_id`** (510). Docstring (325-326) обещает «плюс `preamble` node для непокрытых blocks в начале» — такого node не создаётся. Поле `DocumentStructure.preamble_node_id` **всегда равно `root_id`**: читателей нет ни в одном модуле. Кандидат на удаление (осторожно: поле сериализуется в snapshot, нужен `from_dict`-default).
- **`level=1` в конструкторе узла (365) немедленно затирается** `depth_by_id` (441-443) — мёртвый промежуточный assignment.
- **`coverage_ratio = len(accepted) / max(1, total_blocks)`** (503) — «плотность кандидатов», а не покрытие блоков. Поле с именем `coverage` в `validation.py:312-320` считает принципиально другое (покрытие блоков узлами). Два разных `coverage` под одним именем в одном слое — источник неверных выводов при чтении snapshot'ов.

---

## `workspace/skills/legal_summarizer/scripts/document/pdf_outline.py` — 369 LOC

**Назначение.** Маппинг PDF outline (bookmarks) на реальные `DocumentBlock`.
**Что делает.** `map_pdf_outline` (216) обходит outline через `_walk_outline` (180), резолвит destination в страницу (`_resolve_destination_page` 116), ищет ближайший блок на странице (`_find_nearest_block_on_page` 153) → `MappedOutlineCandidate` со **сдвинутым** `block_index`. `mapped_to_heading_candidates` (334) конвертирует их в `HeadingCandidate`.
**Зачем нужен.** Единственный источник структуры для PDF без DOCX-стилей. В проде зовётся из `detect_heading_candidates` (282).
**Вердикт.** `Упростить`
**Обоснование.** Модуль функционален и это лучшее место в слое, но: мёртвый метод-дубль, неиспользуемый параметр, мёртвое поле `char_offset`, поле `diagnostics`, которое никто не читает, и docstring, описывающий несуществующую дедупликацию.

#### class `StructureAnchor` (строки 60–75)
Атрибуты: `block_ordinal: int | None`, `page_index: int | None = None`, `char_offset: int | None = None`.
Назначение: якорь outline-entry в физическом документе.
Зачем нужен: несёт смещение, найденное `_find_nearest_block_on_page`; конвертируется в `block_index` в `mapped_to_heading_candidates`.
Вердикт: `Упростить`
Обоснование: **`char_offset` всегда `None`** (318) — единственное место, где он заполняется, передаёт константу. Удаление поля убирает 1 LOC в классе + 1 в конструировании; ломает `test_structure_pdf_outline.py:159` (там он передаётся вручную, как неиспользуемый аргумент).

#### class `MappedOutlineCandidate` (строки 77–113)
Атрибуты: `title: str`, `level: int`, `block_index: int | None`, `anchor: StructureAnchor`, `diagnostics: tuple[str, ...] = ()`.
Назначение: outline-entry, привязанный к блоку.
Зачем нужен: транспорт между `map_pdf_outline` и `mapped_to_heading_candidates`.
Вердикт: `Упростить`
Обоснование: `diagnostics` заполняется (307, 320-322) реальными значениями `"out_of_document_order"` / `"duplicate_destination"`, но **читателей нет нигде в репозитории** — данные собираются и теряются. Либо раздать потребителю (логгер), либо удалить.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `to_heading_candidate` | 99–113 | `MappedOutlineCandidate` → `HeadingCandidate` | — | **0 вызовов** | **Удалить** (15 LOC) — `mapped_to_heading_candidates` (346-362) инлайнит **ту же** конверсию. Метод не покрыт тестами |

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_resolve_destination_page` | 116–151 | destination → 1-based страница | маппинг | `_find_nearest_block_on_page` | Оставить |
| `_find_nearest_block_on_page` | 153–178 | ближайший блок на странице | маппинг | `map_pdf_outline` | Оставить |
| `_walk_outline` | 180–214 | рекурсивный обход outline | внутреннее | `map_pdf_outline` | **Упростить** — параметр `reader` (180) **не используется** в теле; передаётся напрасно (3 вызова) |
| `map_pdf_outline` | 216–332 | публичная точка | шаг 2 pipeline | `heading.detect_heading_candidates` (282) | **Упростить** — удалить `diagnostics` либо начать его читать |
| `mapped_to_heading_candidates` | 334–367 | конверсия в `HeadingCandidate` | шаг 2 pipeline | `heading.detect_heading_candidates` (288) | **Упростить** — заменить инлайн 346-362 на вызов `to_heading_candidate` (либо наоборот, удалить метод; выигрыш — один источник конверсии) |

**Ложные утверждения docstring (pdf_outline.py:9-14):** заявлено, что реализованы (а) «duplicate destinations → один outline entry» и (б) «конфликт с существующим heading (тот же `block_index`) → outline считается более приоритетным». Ни того, ни другого в коде нет: дубликаты только **помечаются** строкой в `diagnostics` (320-322) и всё равно конвертируются в кандидаты; `detect_heading_candidates` (288) просто дописывает их в список, поэтому **два кандидата с одинаковым `block_index` возможны** — а `hierarchy.apply_confidence_penalties` (292) строит `by_index = {}` (305), который такой дубль схлопывает молча (и сам мёртв). Docstring на 342 утверждает то же самое про `build_section_tree`, которого больше нет.

**Прочие находки:** мёртвый импорт `field` (47, из `dataclasses`) — ни одного использования; мёртвый импорт `DocumentBlock` (51) — не используется. **Повреждённый текст:** строка 36 — «отсут конные destinations» (следы порчи/смешения языков в docstring).

---

## `workspace/skills/legal_summarizer/scripts/document/list_detection.py` — 335 LOC

**Назначение.** Детекция нумерованных списков и штраф за «heading, который на самом деле пункт списка».
**Что делает.** `detect_list_runs` (93) находит runs подряд идущих нумерованных блоков; `list_penalty_for_candidate` (218) и `ambiguous_decimal_penalty` (262) дают штрафы; `classify_ambiguous_run` (294) классифицирует run.
**Зачем нужен.** Только как поставщик evidence для `heading.apply_evidence_scoring` (589).
**Вердикт.** `Удалить` (весь модуль, 335 LOC)
**Обоснование.** Единственный production-путь в этот модуль — `heading.apply_evidence_scoring` (589), который не вызывается из прода (см. находку 1). Grep: `list_detection` импортируется ровно одним production-модулем (`heading.py:33-37`). Следствие: `ListDetectionConfig.body_threshold_chars` (52) и `classify_ambiguous_run` (294) — мёртвы.
**Доказательства.** `tests/test_structure_list_detection.py` — единственный тест, 3 ассерта на `classify_ambiguous_run`. `chunking/chunker.py` и `hierarchy.py` модуль не используют.
**Что останется:** ничего; `numbering.py` и `heading._classify_regex` продолжают работать без него. **Что сломается:** `tests/test_structure_list_detection.py` (удалить целиком), `heading.py:33-37` (удалить импорт), `heading.py:589` (удалить вызов). **Предварительная работа:** удалить 7 тестовых функций из `test_heading_*.py`/`test_integration_nk_realistic.py`, которые вызывают `apply_evidence_scoring` (см. `heading.py`).

| Символ | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| class `ListDetectionConfig` | 49–65 | параметры | — | `detect_list_runs` (94) | Удалить |
| ↳ поле `body_threshold_chars` | 52 | порог «substantial body» | — | **0 чтений** (комментарий 177-180 признаёт, что проверка тавтологична) | Удалить |
| class `ListRun` | 67–74 | диапазон нумерованных блоков | — | `list_penalty_for_candidate`, `classify_ambiguous_run` | Удалить |
| `_parse_number` | 77–90 | первый числовой компонент | — | `detect_list_runs` (112) | Удалить (дублирует `numbering._parse_decimal_number`) |
| `detect_list_runs` | 93–156 | поиск list-runs | — | `heading.apply_evidence_scoring` (589) | Удалить |
| ↳ `_flush` (вложенная) | 117–128 | `detect_list_runs` | — | `detect_list_runs` | Удалить |
| `_classify_run` | 158–189 | классификация run | — | `detect_list_runs` (135) | Удалить |
| `_neighbor_numbered_count` | 191–215 | соседи-нумерация | — | `_classify_run` (170) | Удалить |
| `list_penalty_for_candidate` | 218–259 | штраф в evidence | — | `heading.apply_evidence_scoring` (589) | Удалить |
| `ambiguous_decimal_penalty` | 262–291 | штраф за неоднозначный decimal | — | **0 вызовов** | Удалить |
| `classify_ambiguous_run` | 294–324 | list/section/ambiguous | — | **только** `tests/test_structure_list_detection.py:63,70,74` | Удалить |

**Ложный комментарий:** 177-180 «Проверка 2: между блоками нет substantial body» описывает проверку, которой нет; комментарий сам признаёт тавтологию («у нас уже contiguous run»). `ListDetectionConfig.body_threshold_chars` (52) — поле без единого чтения.

---

## `workspace/skills/legal_summarizer/scripts/document/numbering.py` — 237 LOC

**Назначение.** Разбор номеров заголовков и присвоение порядковых номеров siblings.
**Что делает.** `parse_numbering` (78) распознаёт 7 схем (legal_chapter/article/clause/section_roman, appendix, paragraph_mark, decimal) → `NumberingInfo`; `assign_sibling_ordinals` (178) проставляет `ordinal` внутри групп одинаковых parent+scheme+level. Побочных эффектов нет.
**Зачем нужен.** Шаг 3 pipeline: `hierarchy.py:373, 469` (даёт `semantic_type` и `ordinals`) и `structure.NumberingInfo`.
**Вердикт.** `Упростить`
**Обоснование.** Модуль нужен, но его docstring (3-6) объявляет его **единым** модулем регулярных выражений, и это обещание не выполнено (см. `heading.py`/`list_detection.py`); плюс 2 вызова `parse_numbering` на один кандидат (дублирующая работа).

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_roman_to_int` | 52–70 | римские цифры → int | — | `parse_numbering` (98) | **Упростить** — таблица (55-63) покрывает только `I..XXX` (до C=100), т.е. `L`(50), `D`, `M` не поддержаны, хотя docstring (54) обещает «I..XXX» без оговорок; `D`/`M` дадут `None` → `legal_section_roman` не распознается для `Раздел D` |
| `_parse_decimal_number` | 72–76 | компоненты decimal | — | `parse_numbering` (150) | Оставить |
| `parse_numbering` | 78–176 | публичный парсер | ядро | `hierarchy.py:373`; тесты `test_structure_numbering.py` (12 ассертов) | **Упростить** — вызывается **дважды** на один и тот же текст: `heading.detect_heading_candidates` (265) и `hierarchy.build_document_structure` (373). Второй вызов избыточен — кандидат уже несёт результат; перенос поля в `HeadingCandidate` убирает один проход регулярных выражений по каждому заголовку |
| `assign_sibling_ordinals` | 178–235 | ordinals внутри группы | семантика `numbering` | `hierarchy.py:469`; тесты `test_structure_numbering.py:104-140` | Оставить |
| ↳ `_flush_group` (вложенная) | 201–212 | `assign_sibling_ordinals` | — | `assign_sibling_ordinals` (214, 228) | Оставить |

**Неточности docstring:** 86 — «потом цирillic_alpha» (смешение латиницы и кириллицы в одном слове); 8-9 — ссылка на `StructureTreeBuilder`, которого больше нет (реальное имя — `hierarchy.build_document_structure`); 3-6 — обещание единого модуля regex, не выполненное.

---

## `workspace/skills/legal_summarizer/scripts/document/repair.py` — 200 LOC

**Назначение.** Починка структуры после детекции: reparent-сирот, снятие циклов, удаление невалидных диапазонов.
**Что делает.** `repair_structure` (57) в 3 прохода по узлам: (1) `parent_id` не существует → в root; (2) `end_block < start_block` → дроп; (3) `node.level <= parent.level` → reparent в root. Внутри — 4 вложенных хелпера (80, 84, 88, 108). Чистые, без побочных эффектов.
**Зачем нужен.** Шаг 4 pipeline (`pipeline_structure.py:305-306`), опциональный (`apply_repair=True` по умолчанию).
**Вердикт.** `Упростить` + **исправить баг**
**Обоснование.** Механика нужна, но docstring (1-46) обещает 4 вида починки, из которых реализован 1, ссылается на несуществующую `_repair_numbering`, а логика дропа содержит баг (см. ниже).

#### class `RepairReport` (строки 48–55)
Атрибуты: `nodes_before`, `nodes_after`, `orphans_reparented`, `impossible_parents_repaired`, `invalid_ranges_dropped`, `numbering_glued`, `changed: bool = False`.
Назначение: отчёт о починке.
Зачем нужен: возвращается в pipeline, но **в `pipeline_structure.py:306` результат отбрасывается** (`struct, _ = repair_structure(struct)`) и нигде не логируется.
Вердикт: `Упростить`
Обоснование: **`numbering_glued` (54) всегда 0** — единственный код, который его заполнил бы, удалён. `changed` (55) — единственное поле, которое реально читается (внутри 74). Остальные 5 счётчиков пишутся и не читаются. Рекомендация: либо начать логировать `RepairReport` в `pipeline_structure.py:306`, либо сократить до `changed`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| — | — | методов нет | — | — | — |

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `repair_structure` | 57–198 | публичная починка | шаг 4 pipeline | `pipeline_structure.py:306`; тесты `test_structure_repair.py` | **Упростить + исправить баг** — см. ниже |

| Вложенная | Строки | Родитель | Назначение | Вердикт |
|---|---|---|---|---|
| `_put` | 80–82 | `repair_structure` | записать узел в `current_nodes` | Оставить |
| `_drop` | 84–86 | `repair_structure` | удалить узел | **Исправить** — источник бага |
| `_rebuild_children` | 88–106 | `repair_structure` | пересобрать `children` после reparent | Оставить |
| `_reparent` | 108–119 | `repair_structure` | сменить parent | Оставить |

**Функциональный баг `repair_structure` (74-75 + 84-86):** `changes: dict` заполняется **только** через `_put` (80-82). `_drop` лишь делает `changes.pop(nid, None)` (85) — если `nid` в `changes` не было, `changes` остаётся путстым. Далее (74-75) идёт ранний выход `if not changes: return struct, report` — возвращается **исходный** `struct`, тогда как `report` уже содержит `invalid_ranges_dropped=1`. Воспроизводимый сценарий: структура, где **единственная** проблема — узел с `end_block < start_block` (никаких сирот, никаких невозможных parent'ов) → `current_nodes` теряет узел, `changes` пуст, ранний выход возвращает нетронутую структуру, а отчёт вращает. Правильная правка: `_drop` должен делать `changes[nid] = <sentinel-absent>` либо ранний выход должен проверять `len(current_nodes) != len(struct.nodes)`, а не `changes`.

**Ложный docstring (1-46):** заявлены repairs для «level jumps», «overlapping ranges (если две секции перекрываются)», «duplicate headings (тот же `start_block` у двух nodes)» и склейка нарушений numbering «см. `_repair_numbering`» — **ни одной из этих четырёх функций в модуле нет**, `_repair_numbering` не существует нигде в репозитории. Реализовано: reparent-сирот (1-й проход), reparent невозможных parent'ов (3-й), дроп невалидных диапазонов (2-й). Overlap и duplicate-heading **не проверяются** (это делает `validation.py:116-118, 250-280` — но только как отчёт, без починки).

---

## `workspace/skills/legal_summarizer/scripts/document/validation.py` — 332 LOC

**Назначение.** Инвариантная проверка `DocumentStructure` против `PhysicalDocument` с машинно-читаемым отчётом.
**Что делает.** `validate_structure` (121) выполняет 11 проверок (docstring 127-148) и возвращает `ValidationReport`; `ValidationIssue`/`ValidationReport` сериализуются в snapshot.
**Зачем нужен.** Шаг 5 pipeline (`pipeline_structure.py:308`) + персистентность отчёта в cache snapshot (216).
**Вердикт.** `Упростить`
**Обоснование.** Проверки корректны и дешевле большинства альтернатив, но **результат не потребляется никем**, а часть проверок — O(N²).

**Ответ на вопрос 8 брифа (вызывается ли в проде, и что при падении):**
- **Вызывается:** да, `pipeline_structure.py:308` — безусловно на каждом cache-miss прогоне. Отчёт кладётся в `PipelineResult.validation` (104, 345) и сериализуется в snapshot (216), откуда читается обратно при cache-hit (165-167).
- **Потребляется:** **нет**. `grep "result\.validation|\.validation\b|is_valid"` по `scripts/` даёт совпадения только в `document/validation.py` и `pipeline_structure.py` (создание). `retrieval/quality.py:112` принимает параметр `structure_is_valid`, но **ни один вызов** в `scripts/` его не передаёт, и поле `structure_correctness` (47) никогда не читается. То есть 332 LOC валидации + O(N²)-проверки overlap на каждом прогоне — **telemetry в никуда**.
- **Fail-soft или проглатывание:** **ни то, ни другое** — `validate_structure` **не имеет** fail-soft обёртки. `pipeline_structure.py:308` — голый вызов. Хотя сама функция чистая и исключений не бросает (все обращения к `struct.nodes` защищены `.get()`), риск падения есть: `_is_ancestor` (104) использует `struct.nodes[descendant_id]` — **прямой доступ по ключу**, который бросит `KeyError` на структуре с `parent_id`, указывающим на отсутствующий узел. Проверка №4 (docstring 132) «все `parent_id` существуют» выполняется, судя по коду, **после**/независимо от `_is_ancestor`, поэтому битая ссылка даст `KeyError`, а не `ValidationIssue`. Правильная правка: `struct.nodes.get(descendant_id)` + проверка порядка.

| Символ | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| class `ValidationIssue` | 41–63 | одно нарушение инварианта | формат отчёта | `validate_structure` (много), `to_dict`/`from_dict` | Оставить |
| ↳ `to_dict` | 48–49 | сериализация | snapshot | `ValidationReport.to_dict` (75) | Оставить |
| ↳ `from_dict` | 52–63 | десериализация | cache-hit | `ValidationReport.from_dict` (86) | Оставить |
| class `ValidationReport` | 66–92 | сводка валидации | формат отчёта | `validate_structure` (322-329) | Оставить |
| ↳ `to_dict` | 73–78 | сериализация | snapshot | `pipeline_structure.py:216` | Оставить |
| ↳ `from_dict` | 81–92 | десериализация | cache-hit | `pipeline_structure.py:165` | Оставить |
| ↳ поле `is_valid` | 71 | «нет issues» | — | **0 чтений** | Упростить (см. выше) |
| `_is_ancestor` | 95–113 | ancestor-descendant | проверки overlap | `validate_structure` (267, 285) | **Упростить** — `struct.nodes[descendant_id]` (104) бросит `KeyError` вместо issue |
| `_ranges_overlap` | 116–118 | пересечение диапазонов | проверки overlap | `validate_structure` (263, 279) | Оставить |
| `validate_structure` | 121–329 | публичная валидация | шаг 5 pipeline | `pipeline_structure.py:308`; 2 тестовых файла (17 вызовов) | **Упростить** — входные данные (проверки 1-11) корректны; выход потребляется нулём. Первоочередная рекомендация: **либо** начать потреблять `is_valid` (лог при `False` + передача в `retrieval/quality.py:112`), **либо** удалить модуль. Промежуточный вариант — оставить только дешёвые O(N) проверки (1-5, 10-11) и убрать O(N²) overlap (проверки 6-9) |

---

## `workspace/skills/legal_summarizer/scripts/document/safety_merge.py` — 147 LOC

**Назначение.** Заявлено как «safety net» для схлопывания микро-секций.
**Что делает.** `safety_merge` (58) для level 1-2 находит секции короче `min_section_chars` и помечает `confidence=0.0`, формально расширяя диапазон соседа через `max()`. Чистый, без побочных эффектов.
**Зачем нужен.** **Ни зачем в текущем виде** — см. ниже.
**Вердикт.** `Удалить` (147 LOC)
**Обоснование.** Модуль **не вызывается ни из одной production-точки**: `application/pipeline_structure.py` не импортирует `document.safety_merge`; grep по `scripts/` даёт 0 совпадений вне самого файла. Единственные потребители — 6 тестов в `tests/test_structure_safety_merge.py`. При этом заявленная функция схлопывания — **no-op**: диапазоны секций, выданные `build_document_structure`, контигентны (`end_block` следующей = `start_block` − 1), поэтому `new_end = cur.end_block` (98) < `target.end_block`, и `max(target.end_block, new_end)` (108) всегда возвращает `target.end_block` — **ни один диапазон не расширяется**. Меняется только `confidence` у `cur` (123). Модуль, который ничего не делает и не вызывается, — чистый кандидат на удаление.
**Доказательства.** 0 импортёров в `scripts/`; 0 в `docs/`; 0 в CI. Тесты: `test_structure_safety_merge.py` (5 тестов, 40-120).
**Что останется:** без изменений. **Что сломается:** `tests/test_structure_safety_merge.py` удаляется целиком; `tests/test_structure_llm_invariant.py:36,68` импортирует `document.safety_merge` как модуль в списке «структурных модулей» — одну строку поправить.
**Альтернатива удалению** (если команда хочет сохранить идею): перенести в `hierarchy.py` как часть builder'а и **сделать вызов в `pipeline_structure.py`** после `build_document_structure`; при этом переписать расчёт `end_block` так, чтобы он реально расширял диапазон (иначе эффекта нет).

| Символ | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| class `SafetyMergeConfig` | 39–44 | параметры | — | `safety_merge` (77) | Удалить |
| ↳ поле `dry_run` | 44 | режим «считать, не применять» | — | **0 чтений** | Удалить |
| `_section_total_chars` | 47–55 | сумма `char_count` по диапазону | — | `safety_merge` (94) | Удалить |
| `safety_merge` | 58–144 | схлопывание | — | **только тесты** | Удалить |

**Ложные утверждения docstring:** строка 10 — «срабатывает только если в структуре **много** коротких siblings» (плюс мусор `**`): никакой проверки «много» нет, триггер — одна короткая секция с соседом; строка 19 — «помечает `confidence = 0.0` и **parent = merged**»: `parent_id` **не меняется**; строки 15-18 — «Отличие от `merge_short_sections` в `sections.py`»: ни `sections.py`, ни `merge_short_sections` в репозитории не существуют.

---

## `workspace/skills/legal_summarizer/scripts/document/title.py` — 154 LOC

**Назначение.** Детерминированный (без LLM) резолв заголовка документа из 4 приоритетных источников.
**Что делает.** `resolve_title` (120) пробует: (1) метаданные файла, (2) DOCX Title-style блок, (3) первый `heading*`-стиль блока, (4) первую непустую строку `text`. Побочный эффект: `_metadata_title` (33) **открывает файл повторно** (`Document(str(path))` 44, `PdfReader(str(path))` 54, `Presentation(str(path))` 65).
**Зачем нужен.** Заполняет `DocumentStructure.title` (`pipeline_structure.py:292-303`).
**Вердикт.** `Упростить`
**Обоснование.** Модуль нужен, но дублирует чтение метаданных из `physical._pick_title_from_text` и ре-открывает файл, хотя `loader` уже всё распарсил.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_metadata_title` | 33–71 | title из DOCX/PDF/PPTX core props | источник 1 | `resolve_title` (137) | **Слить с `physical.py`** — ~30 из 35 строк совпадают с `physical._pick_title_from_text` (173-199) построчно (docx / pdf / pptx, три одинаковых `try/except Exception: pass`). Возвращает кортеж `(title, source)`, но `resolve_title` (137) отбрасывает `source` через `_` и **хардкодит** `"metadata"` (139) — второй элемент кортежа мёртв |
| `_docx_title_style_block` | 74–87 | первый `title`/`subtitle` блок | источник 2 | `resolve_title` (141) | Оставить |
| `_first_strong_candidate` | 90–103 | первый `heading*` блок | источник 3 | `resolve_title` (145) | **Упростить** — docstring модуля (9-10) обещает «DOCX Heading 1 **с маленьким уровнем и коротким текстом**»; код проверяет только `style.lower().startswith("heading")`, ни уровень, ни длину не смотрит |
| `_first_nonempty_line_fallback` | 106–117 | первая строка ≥ 3 символов | источник 4 | `resolve_title` (150) | Оставить |
| `resolve_title` | 120–151 | публичная точка | шаг 3.5 pipeline | `pipeline_structure.py:292` | **Упростить** — вызывается **один раз**; `_first_nonempty_line_fallback` (150) требует `text`, а `pipeline_structure.py:292` передаёт `text=text`, где `text` опционален (`run_canonical_pipeline(..., text=None)` по умолчанию) → на дефолтном пути fallback молча выключен |

**Дублирование внутри слоя (2-я и 3-я копии одной логики):** чтение core-title реализовано **дважды** (`physical.py:180-199` и `title.py:40-69`), и обе копии дополнительно дублируют fallback «первая непустая строка» (`physical.py:203-207` ≈ `title.py:106-117`). Плюс docstring `loader.py:3-7` и `31-34` («никакого двойного парсинга PDF/DOCX») противоречат фактическому поведению — что зафиксировано в `tests/test_structure_document_loader.py:64-67`.

**Мёртвый импорт:** `Any` (22) — ни одного использования. **Stale-ссылка:** 16 — `DocumentTitle` из `structure/models.py` (файла нет).

---

## `workspace/skills/legal_summarizer/scripts/document/identity.py` — 118 LOC

**Назначение.** Единый идентификатор документа: `document_id` (12 hex) + `fingerprint` (полный sha256) + freshness.
**Что делает.** `from_path` (84) считает `sha256(f"{resolved}|{size}|{mtime_ns}")`; `is_fresh` (56) перепроверяет `(size, mtime)`. Побочных эффектов нет.
**Зачем нужен.** `document_id` — ключ document-level cache (`pipeline_structure.py:146, 152, 155, 212`) и `DocumentStructure.document_id` (через `StructureTreeBuilderConfig`). Change detection: новый файл = новый `document_id` = cache miss.
**Вердикт.** `Упростить`
**Обоснование.** Модуль нужен (это **настоящая** консолидация — см. вопрос 7), но 2 метода мертвы, 1 поле мертво, 2 конструктора дублируют друг друга.

**Ответ на вопрос 7 брифа (удалены ли две старые реализации):** **да, обе удалены, и это единственная успешно завершённая консолидация в слое.** `grep "compute_fingerprint|_physical_cache_key"` по всему репозиторию даёт **только** упоминания в docstring'ах самого `identity.py` (5, 6, 43, 102) — ни `scripts/fingerprint.py`, ни функции `_physical_cache_key` в коде нет. `PhysicalDocument` получает `size_bytes` (loader.py:69) от `loader`, а не от identity; fingerprint считается **один раз** в `pipeline_structure.py:146` (cache-hit путь) и `281` (cache-miss путь) и передаётся вниз через `DocumentAnalysis.identity` — ровно как обещает docstring (9-13). **Остатки:** см. таблицу ниже; 4 устаревшие ссылки в docstring'ах на удалённое.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `is_fresh` | 56–71 | свежесть `(size, mtime)` | — | **только** `tests/test_operation_identity.py:107,112` и `tests/test_structure_identity.py:43,45,52`. В проде **не вызывается намеренно** — это зафиксировано в `docs/document_cache_refactoring_inventory.md:102-112` («использование в pipeline неправильное (НЕ баг метода)») | **Упростить** — метод корректен и осознанно не используется; оставить как публичный API, но удалить 4 мёртвые ссылки на него в docstring'ах (`identity.py:20`, `pipeline_structure.py:132`) либо пометить «не используется, change detection через `document_id`» |
| `to_dict` | 73–81 | сериализация | `DocumentAnalysis.to_dict` (73) | `analysis.py:73` — а сам `DocumentAnalysis.to_dict` в проде не вызывается (см. `analysis.py`) | **Упростить** — цепочка целиком мертва |
| `from_path` | 84–96 | identity по файлу | **основной путь** | `pipeline_structure.py:146, 281`; `analysis.py:103` | **Упростить** — 14 строк дублируют `from_path_with_mtime` (99-115) побайтово, различаясь только источником `stat`; `from_path` должен делегировать: `return cls.from_path_with_mtime(path, size_bytes=st.st_size, mtime_ns=st.st_mtime_ns)` |
| `from_path_with_mtime` | 99–115 | identity по явным `size/mtime` | back-compat | **только** `tests/test_structure_identity.py:55,66`, `test_structure_document_analysis.py:129`, `test_document_cache_hit_miss.py:158` | **Удалить** (17 LOC) — docstring (100-104) объясняет существование «back-compat с `_physical_cache_key`, который использовал `st.st_mtime` (секунды)», а **эта функция удалена**; сохранять API ради удалённой функции незачем. Правка тестов: во всех 4 местах заменить на `DocumentIdentity.from_path(tmp_path / ...)` |

**Мёртвое поле:** `physical_cache_key` (51) — заполняется в обоих конструкторах (92, 111) и сериализуется (77), но **читается только в тестах** (`test_structure_identity.py:18, 61`). Docstring (42-43) объясняет его «для обратной совместимости с `_physical_cache_key` из `physical.py`» — функции нет. В скриптах поле не читается ни разу; единственное упоминание в проде — комментарий `retrieval/index.py:11`. Кандидат на удаление (осторожно: попадает в `to_dict`, а значит потенциально в persisted-контракт — нужно проверить `DocumentAnalysis.to_dict` потребителей, см. `analysis.py`).

---

## `workspace/skills/legal_summarizer/scripts/document/analysis.py` — 166 LOC

**Назначение.** Immutable snapshot всех результатов анализа документа (единый объект для brief/question/follow-up).
**Что делает.** `build` (84) выравнивает `structure.document_id` под `identity.document_id` (111-121) и строит `RetrievalIndex` (122-131). Побочный эффект: `from retrieval.index import RetrievalIndex` — ленивый импорт внутри метода (123).
**Зачем нужен.** Единый объект, передаваемый в LLM/retrieval слои. `pipeline_structure.py:174, 324` + все downstream'ы.
**Вердикт.** `Упростить`
**Обоснование.** Нужен, но содержит дублированный импорт (баг), 2 метода без production-вызова и 2 мёртвых поля.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `get_chunk` | 62–66 | линейный поиск chunk по id | — | `retrieval/followup.py:124, 125, 136` | **Упростить** — линейный O(N) скан по `chunks` на каждый chunk в follow-up; в `followup.py:124-125` **один и тот же chunk ищется дважды** подряд (`[analysis.get_chunk(cid) for cid in target_ids]` + фильтр `if analysis.get_chunk(cid) is not None`) — удвоенный O(N) обход |
| `get_record` | 68–69 | `semantic_records.get(chunk_id)` | — | brief/semantic-record слой (1 файл) | Оставить |
| `to_dict` | 71–81 | сериализация сводки | — | **0 production-вызовов**. `pipeline_structure.py:211-217` собирает `analysis_payload` вручную из `structure.to_dict()` / `c.to_dict()` / `validation.to_dict()`; `DocumentAnalysis.to_dict` не участвует. Единственное упоминание — комментарий `cache/document_cache.py:186` | **Удалить** (11 LOC) — или, наоборот, использовать его в `_write_document_snapshot_after_pipeline` (устранив ручную сборку payload). Удаление ломает 1 тест (`test_structure_document_analysis.py:104`) |
| `build` | 84–143 | каноническая сборка | шаг 6 pipeline | `pipeline_structure.py:174, 324` | **Упростить** — блок выравнивания `document_id` (111-121) копирует 8 полей `DocumentStructure` вручную; `dataclasses.replace(structure, document_id=identity.document_id)` (1 строка) вместо 11 |
| `retrieve` | 145–163 | retrieval через `RetrievalIndex` | — | brief/question слои | **Исправить** — 156-161: `from retrieval.query import retrieve_chunks` **импортируется дважды подряд** (156-158 и 159-161), буквальный copy-paste |

**Мёртвые поля:** `created_at` (59) — дефолт `""`, `pipeline_structure.py:174, 324` **не передаёт** его, поэтому в проде всегда `""`; `version` (60) — объявлен «для migrations», но snapshot имеет собственный `"version": 1` (`pipeline_structure.py:212`), а метода `DocumentAnalysis.from_dict` **не существует** — сериализация односторонняя.

**Отсутствующие импорты:** `SemanticRecord` (57) и `RetrievalIndex` (58) используются в аннотациях, но **не импортированы** — работает только потому, что `from __future__ import annotations` (21) делает аннотации ленивыми. Хрупко: любой вызов `typing.get_type_hints(DocumentAnalysis)` или pydantic-валидация упадёт с `NameError`.

---

## `workspace/skills/legal_summarizer/scripts/document/section_helpers.py` — 62 LOC

**Назначение.** Чистые утилиты над `DocumentStructure`: индекс секций, подсчёты.
**Что делает.** `section_index` (16) строит `(ids, headings, paths)` с иерархическим путём `"1 > 2 > 3"`; `count_meaningful_sections_canonical` (43) и `count_sections` (51) — счётчики. Побочных эффектов нет.
**Зачем нужен.** Канонизация: раньше жили в `application/section_index.py`, перенесены сюда как чистые функции над моделью.
**Вердикт.** `Оставить`
**Обоснование.** Единственный модуль слоя без находок. Импортёры: `application/section_index.py` (или его эквивалент) + `application/*`; покрыт тестами. Минимальное замечание (не вердикт): `section_index` (29-39) обходит parent-chain вручную, дублируя идею `DocumentStructure.iter_children`; при `cur.node_id == struct.root_id` цикл прерывается, поэтому root в `parts` не попадает — поведение корректное.
**Стиль:** docstring (6-8) — единственная в слое **корректная** ссылка на историю переноса.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `section_index` | 16–40 | (ids, headings, paths) | LLM-контекст | `application/*` | Оставить |
| `count_meaningful_sections_canonical` | 43–48 | счётчик meaningful | метрики | `application/*` | Оставить |
| `count_sections` | 51–55 | счётчик секций | метрики + graceful `None` | `application/*` | Оставить |

---

## `workspace/skills/legal_summarizer/scripts/document/block_lookup.py` — 52 LOC

**Назначение.** Заявлено как замена линейного `index()` — O(1) lookup блока по `ordinal` и `block_id`.
**Что делает.** `build_block_lookup` (37) строит два dict'а за O(N); `BlockLookup.get_by_ord/get_by_id` (30, 33) — доступ. Чистый, без побочных эффектов.
**Зачем нужен.** **Ни зачем в текущем виде** — см. ниже.
**Вердикт.** `Удалить` (52 LOC)
**Обоснование.** Ответ на вопрос 5 брифа: **legacy-код не переведён, а модуль не нужен.** (1) `scripts/retrieval/context_expansion.py` существует, но `block_lookup` **не импортирует** и не использует — grep по `scripts/` даёт 0 совпадений вне самого файла; (2) `scripts/retrieval/cached_retrieval.py` **не существует вообще** (упоминание — только в docstring'ах `block_lookup.py:3, 42` и в списке `tests/test_canonical_production_path.py:58`); (3) сам docstring модуля (9-11) признаёт, что `PhysicalDocument.blocks_by_ord` **уже даёт** lookup по ordinal — то есть `BlockLookup.by_ord` (27) дублирует существующий кэшируемый property (`physical.py:122-138`), который и используется в проде (`brief_context.py:378,395,440,443,485`, `structural_packing.py:463`, `chunking/chunker.py:276`); (4) `by_id` (28) — единственная «уникальная» ценность, но читатель `DocumentBlock.block_id` в prod-коде ровно один: `block_lookup.py:48`, то есть удаление модуля делает поле `block_id` (physical.py:81) тоже мёртвым.
**Доказательства.** 0 импортёров в `scripts/`; единственный потребитель — `tests/test_structure_block_lookup.py` (4 теста, 6-69). `blocks_by_ord` покрыт prod-тестами (`test_application_brief_context.py`).
**Что останется:** без изменений; `blocks_by_ord` покрывает единственную реальную потребность. **Что сломается:** `tests/test_structure_block_lookup.py` удаляется целиком (4 теста). **Предварительная работа:** удалить 2 ссылки на `cached_retrieval.py` из docstring'ов модуля (3, 42) — они исчезают вместе с файлом; проверить, не осталось ли `blocks.index(...)` в `retrieval/context_expansion.py` (в текущем коде не найдено — O(N)-поиска там нет, то есть модуль изначально решал несуществующую проблему).

| Символ | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| class `BlockLookup` | 24–34 | контейнер двух dict'ов | — | `build_block_lookup` (49) | Удалить |
| ↳ `get_by_ord` | 30–31 | lookup по ordinal | — | **0 вызовов в prod** | Удалить (дубль `blocks_by_ord`) |
| ↳ `get_by_id` | 33–34 | lookup по `block_id` | — | **0 вызовов в prod** | Удалить |
| `build_block_lookup` | 37–49 | построение индекса | — | **только** `tests/test_structure_block_lookup.py:33,39,53,69` | Удалить |

---

## Stale-ссылки: масштаб (ответ на вопрос 2 брифа)

`structure.py` действительно SoT для **модели** (`DocumentStructure` — единственный контракт, 20+ импортёров). Но слои переименовывались минимум трижды (`domain/structure.py` → `structure.py` → `document/`), и **ни один из docstring'ов не был обновлён**. Каталога `workspace/skills/legal_summarizer/scripts/structure/` **не существует** (проверено), `structure/models.py` **не существует**.

**Полный перечень — 28 ссылок на несуществующие пути/символы в 8 файлах слоя:**

| Файл:строка | Ссылка | Реальность |
|---|---|---|
| `heading.py:1` | ``structure/heading.py`` | `document/heading.py` |
| `heading.py:10` | ``_detect_candidates`` | `detect_heading_candidates` (197) |
| `heading.py:11` | ``_apply_confidence_penalties`` | `apply_confidence_penalties` (292) |
| `heading.py:12` | ``_filter_above_threshold`` | `filter_above_threshold` (640) |
| `heading.py:14` | `SectionTree` / `DocumentSection` | удалены (см. `pipeline_structure.py:12-13`) |
| `heading.py:15` | ``structure/tree.py``, ``structure/sections.py`` | не существуют |
| `heading.py:205` | ``_detect_candidates`` в ``sections.py`` | не существует |
| `heading.py:209` | ``scripts/structure/numbering.py`` | `document/numbering.py` |
| `heading.py:216` | ``scripts/structure/pdf_outline.py`` | `document/pdf_outline.py` |
| `heading.py:219` | ``build_section_tree`` | `build_document_structure` |
| `list_detection.py:1` | ``structure/list_detection.py`` | `document/list_detection.py` |
| `list_detection.py:27` | ``structure/heading.py::apply_evidence_scoring`` | `document/heading.py` |
| `physical.py:6, 104, 214` | ``extract_structure`` | удалена из `office_files.py` (см. кросс-подсистемную находку) |
| `physical.py:27, 30` | ``structure/models.py`` | не существует |
| `physical.py:214` | ``office_files.extract_structure`` | удалена |
| `title.py:16` | ``structure/models.py`` | не существует |
| `identity.py:5` | ``scripts/fingerprint.py::compute_fingerprint`` | файла нет |
| `identity.py:6, 43, 102` | ``_physical_cache_key`` | функции нет |
| `identity.py:18, 20` | «Хранит `document_id`… Проверяет freshness» | описывает `is_fresh`, который в проде не вызывается |
| `block_ownership.py:6, 21` | ``domain.structure.block_to_node`` | `document.structure`; модуль удаляется |
| `block_ownership.py:1-25` | «canonical block ownership» | канон — `structure.py` |
| `structure.py:19` | `SectionTree` / `DocumentSection` / `HeadingCandidate` в «Legacy» | удалены |
| `structure.py:398` | ``domain/structure.py`` | `document/structure.py` |
| `pdf_outline.py:4, 342` | ``tree.build_section_tree`` | `build_document_structure` |
| `safety_merge.py:15, 17` | ``merge_short_sections`` в ``sections.py``, ``SectionTree`` | не существуют |
| `block_lookup.py:3, 42` | ``cached_retrieval.py`` | файла нет |
| `repair.py:23-27` | ``_repair_numbering`` | функции нет |
| `numbering.py:3-6` | «из нового единого модуля они импортируются отсюда» | regex дублируются в `heading.py` и `list_detection.py` |
| `numbering.py:8-9` | ``StructureTreeBuilder`` | `build_document_structure` |
| `analysis.py:105` | «Production pipeline (structure.pipeline)» | `application/pipeline_structure.py` |

Итог: **~28 stale-ссылок в 11 файлах из 18**. `section_helpers.py:6-8` — единственная корректная историческая ссылка в слое.

---

## Этапность pipeline (ответ на вопрос 6 брифа)

Точка сборки: `application/pipeline_structure.py:238-349` (`run_canonical_pipeline`). Фактическая последовательность:

| # | Этап | Вызов | Статус |
|---|---|---|---|
| 0 | cache hit | `_try_load_cached_pipeline_result` (269) | ✅ работает |
| 1 | **physical** | `loader.load` (280) → `_iter_*_blocks` | ✅ вызывается |
| 2 | **heading** | `detect_heading_candidates` (283) | ✅ вызывается |
| 2a | heading scoring | `apply_confidence_penalties` / `apply_evidence_scoring` / `filter_above_threshold` | ❌ **НЕ вызывается нигде в `scripts/`** (только тесты) |
| 2b | list detection | `list_detection.detect_list_runs` | ❌ **недостижим** (только через 2a) |
| 3 | **hierarchy** | `build_document_structure` (287) | ✅ вызывается |
| 3a | tree safety | `document.safety_merge.safety_merge` | ❌ **НЕ импортирован в точку сборки вообще** |
| 4 | **repair** | `repair_structure` (306) | ✅ вызывается (опционально, `apply_repair=True`) |
| 5 | **validation** | `validate_structure` (308) | ✅ вызывается, ❌ результат не потребляется |
| 6 | chunking | `planner.plan` (322) | ✅ вызывается |
| 7 | analysis | `DocumentAnalysis.build` (324) | ✅ вызывается |
| 8 | snapshot | `_write_document_snapshot_after_pipeline` (333) | ✅ вызывается |

**Выводы по вопросу 6:**
- **Ни одна фаза не вызывается дважды.** Двойного вызова нет ни для одного этапа.
- **Три этапа/подсистемы не вызываются вообще** — и это **кандидаты на удаление**: (1) `safety_merge` — целиком, мёртвый этап; (2) heading-scoring (`apply_confidence_penalties` → `compute_evidence` → `apply_evidence_scoring` → `filter_above_threshold`) — целиком; (3) `list_detection` — целиком, достижим только через (2).
- **Валидация вызывается, но результат выбрасывается** — либо чинить потребление, либо удалять.
- **Порядок в целом корректен** (physical → heading → hierarchy → repair → validation → chunking), единственное расхождение с ожидаемым: `title.resolve_title` (292) вызывается **после** `build_document_structure` и **пересобирает `DocumentStructure` целиком** (294-303) ради смены одного поля `title` — копируя 8 полей вручную; `dataclasses.replace(struct, title=title)` (1 строка) заменяет 10.
- **`StructureValidator` не fail-soft** (вопрос 8) — см. раздел `validation.py`.

---

## Функциональные баги (сводно)

| # | Файл:строка | Баг | Влияние | Предлагаемая правка |
|---|---|---|---|---|
| 1 | `repair.py:74-75, 84-86` | `_drop` не помечает `changes`, поэтому при единственной проблеме «невалидный диапазон» ранний выход возвращает исходную структуру, а `report.invalid_ranges_dropped == 1` | Отчёт врёт; невалидный узел остаётся в структуре | `_drop` должен заселять `changes` (или ранний выход сравнивать `len(current_nodes)`) |
| 2 | `physical.py:338-341, 354, 411-414` | `page_count == 0` для любого непустого DOCX (`para_to_page` всегда `None`) | `pdf_outline._resolve_destination_page`, любые потребители `page_count` получают 0 страниц | Либо реализовать оценку страниц (через `w:br type="page"` / `lastRenderedPageBreak`), либо честно задокументировать `page_count=0` для DOCX и убрать `para_to_page` |
| 3 | `analysis.py:156-161` | `from retrieval.query import retrieve_chunks` продублирован дважды подряд | Косметика, но признак copy-paste | Удалить дубль |
| 4 | `validation.py:104` | `struct.nodes[descendant_id]` — прямой доступ, бросит `KeyError` вместо `ValidationIssue` при битом `parent_id` | Падение pipeline (не fail-soft) | `struct.nodes.get(...)` + `None`-guard |
| 5 | `heading.py:40, 105` | `CONFIDENCE_THRESHOLD=0.60` не применяется в проде, но docstring утверждает, что применяется | Все кандидаты с `block_index >= 0` становятся секциями (нет фильтра) | Либо подключить scoring к `pipeline_structure.py:286`, либо удалить порог вместе с подсистемой |
| 6 | `numbering.py:55-63` | `_roman_to_int` не поддерживает `D`/`M` (только `I..XXX`), docstring обещает без оговорок | `Раздел D` / `Раздел M` не распознаются | Дополнить таблицу или ограничить docstring |
| 7 | `pdf_outline.py:9-14, 342` + `heading.py:288` | Заявленная дедупликация outline по destination и приоритет над конфликтующим heading **не реализованы**; возможны два кандидата с одинаковым `block_index` | Дубли секций в структуре | Либо реализовать дедупликацию в `mapped_to_heading_candidates`, либо исправить docstring |
| 8 | `physical.py:81` + `block_lookup.py:48` | `block_id` вычисляется для каждого блока, но читается только мёртвым модулем | Лишняя работа + мёртвое поле | Удалять вместе с `block_lookup.py` |
| 9 | `title.py:137` + `loader.py:68` | Файл открывается дважды (DOCX/PDF/PPTX), вопреки docstring'ам `loader.py:3-7, 31-34` | ×2 I/O на каждый документ | Использовать `physical.title` вместо повторного `_metadata_title` |

---

## Кросс-подсистемные находки

1. **`workspace/utils/structure_cache.py:7` — мёртвый модуль, падающий с `ImportError`.** Импортирует `workspace.utils.office_files.extract_structure`, которой в `office_files.py` **больше нет** (проверено: в файле только `detect_format`, `_read_text_auto`, `_extract_{docx,pdf,pptx,xlsx,xls,csv}`, `extract_text`, `_tables_{docx,pdf}`, `extract_tables`, `read_xlsx_sheet`, `_summarize_*`, `summarize`). Это **подтверждает находку другого аудитора** про `structure_cache.py`. Связь с этим аудитом: `physical.py:214` называет `office_files.extract_structure` «ранее существовавшим» — то есть удаление состоялось осознанно, а `structure_cache.py` остался. **Рекомендация:** удалить `workspace/utils/structure_cache.py` целиком — он не имеет импортёров (0 совпадений по `structure_cache` вне самого файла) и физически не импортируется.
2. **Третья реализация fingerprint.** `workspace/utils/structure_cache.py:12-15` считает `sha256(f"{resolved}|{size}|{st_mtime}")[:32]` — третий вариант того же алгоритма, что и `identity.DocumentIdentity` (`mtime_ns`, полный hexdigest, `document_id=[:12]`). `identity.py:3-8` заявляет, что заменил «две параллельные реализации» — фактически осталась **третья**, в другом пакете (`workspace/utils/`). Рекомендация: удалить вместе с `structure_cache.py`.
3. **Кросс-подсистемная приватность API.** `loader.py:20-23` импортирует **4 приватных** символа соседнего модуля (`_iter_docx_blocks`, `_iter_pdf_blocks`, `_iter_txt_blocks`, `_pick_title_from_text`). Это делает `physical.py` фактически не-инкапсулированным: любая перестановка внутренних имён `_iter_*` ломает `loader`. Рекомендация: переименовать в `iter_*_blocks` (публичные) либо инкапсулировать фабрику `load_physical_blocks(fmt, path)`.
4. **`retrieval/quality.py` — висячий параметр.** `structure_is_valid` (112) → `structure_correctness` (47) — единственный канал, через который `ValidationReport.is_valid` **мог бы** потребляться. Ни один вызов в `scripts/` его не заполняет. Либо замкнуть цепочку `pipeline_structure.py:308 → PipelineResult.validation → quality`, либо удалить `quality.py`-часть (вне моего скоупа — передать владельцу `retrieval/`).
5. **`DocumentStructure.to_dict` — «только serialization».** `references/architecture.md:168` явно ограничивает `to_dict()` сериализацией. `hierarchy.py` и `analysis.py` при этом **пересобирают** `DocumentStructure`/`StructureNode` вручную (копируя поля) — то есть обходят это правило конструкторами, а не `dataclasses.replace`. Рекомендация для владельца документации: разрешить `dataclasses.replace` как канонический способ частичного обновления immutable-моделей.
6. **Загрязнённый текст в docstring.** `pdf_outline.py:36` — «отсут конные destinations» (порча/смешение языков). `numbering.py:86` — «цирillic_alpha» (латиница `ill` внутри кириллического слова). `heading.py:86`-уровень — см. `hierarchy._evidence_from_candidate:197` («regex_glзава» — кириллица `за` внутри латинского идентификатора схемы). Мелочь, но признак того, что docstring'ы правились вручную и не вычитывались.

---

## Счётчики вердиктов

| Вердикт | Кол-во | Ключевые позиции |
|---|---|---|
| **Оставить** | 47 | `DocumentStructure` + 4 value-класса, `iter_sections`/`iter_children`/`to_dict`/`from_dict`, `PhysicalDocument` + `blocks_by_ord`, `DocumentLoader.load`, `detect_heading_candidates`, `_heading_rank`, `_build_parents_by_stack`, `parse_numbering`, `assign_sibling_ordinals`, `numbering._roman_to_int` (с оговоркой), `_validate_*`, весь `section_helpers`, `DocumentIdentity.from_path` |
| **Упростить** | 14 | `StructureEvidence` (схлопнуть в 3 поля `StructureNode`) · `DocumentStructure.block_to_node` (до удаления) · `physical.py` (3 мёртвых импорта + 2 мёртвых локала в `_iter_docx_blocks` + `DocumentBlock.page_start/page_end`) · `DocumentLoader.load` (мёртвый параметр `workspace_root` + мёртвая ветка `else`) · `heading.detect_heading_candidates` (сделать `physical_doc` обязательным) · `hierarchy` (3 ручные пересборки `StructureNode` → `dataclasses.replace`; `StructureTreeBuilderConfig` с 3 полей до 1; `level=1` в 365) · `pdf_outline._walk_outline` (мёртвый параметр `reader`) + `mapped_to_heading_candidates`/`StructureAnchor` (удалить `diagnostics`/`char_offset`) · `numbering` (двойной вызов `parse_numbering`) · `repair.py` (ложный docstring + `RepairReport` с 1 читаемым полем из 7) · `validation.py` (`_is_ancestor` на `.get()`; решение по потреблению) · `identity.is_fresh` · `identity.from_path` (делегировать `from_path_with_mtime`) · `analysis.get_chunk` + `analysis.build` (через `dataclasses.replace`) · `title.resolve_title` (без двойного открытия файла) |
| **Удалить** | 24 | `block_ownership.py` — файл целиком (109 LOC, 4 символа: `_depth_of`, `build_block_ownership`, `owner_for_block`, `block_to_node`) · `structure.py:478-489` — `block_to_node` · `structure.py:297-311` — метод `DocumentStructure.block_to_node` · `structure.py:277-282` — `get_node` + `iter_nodes` · `heading.py:143-194, 292-329, 332-399, 402-559, 562-643` — `_extract_pdf_outline`+`_walk`, `apply_confidence_penalties`, `HeadingEvidence`+`total_delta`+`final_score`, `_is_short`, `_looks_like_heading_typography`, `_is_substantial_body`, `_collect_previous_heading_text`, `_numbering_consistency_with_neighbors`, `compute_evidence`, `_looks_like_explicit_legal_marker`, `apply_evidence_scoring`, `filter_above_threshold`, `_looks_like_heading`, `_is_docx_title_style`, `CONFIDENCE_THRESHOLD` (367 LOC, 14 символов) · `list_detection.py` — файл целиком (335 LOC, 11 символов) · `safety_merge.py` — файл целиком (147 LOC, 4 символа) · `block_lookup.py` — файл целиком (52 LOC, 4 символа) · `physical.py:163-233` — `_physical_cache_root`, `_identity_for`, `_build_structure_dict` (40 LOC) · `physical.py:81` — поле `DocumentBlock.block_id` · `identity.py:99-115` — `from_path_with_mtime` · `identity.py:51` — поле `physical_cache_key` · `analysis.py:71-81` — `DocumentAnalysis.to_dict` |
| **Перенести** | 2 | `physical._pick_title_from_text` → `title.py` (устранит двойное чтение метаданных и двойное открытие файла); `physical._iter_{pdf,docx,txt}_blocks` → публичные имена (снимает кросс-модульный доступ к приватному API соседнего модуля из `loader.py:20-23`) |
| **Слить с `<файл>`** | 1 | `physical._pick_title_from_text` + `title._metadata_title` → один чтец core-title |

> Счётчики считают **вердикты по символам**, группируя содержимое целого файла в один слот (например, «удалить `list_detection.py`» = 1 слот, хотя символов внутри 11). Итого по символам: 48 удалений, 18 упрощений, 47 оставлений.

**Покрытие:** 18/18 файлов, 20/20 классов, 44/44 методов, 32/32 функции модульного уровня, 5/5 вложенных функций. `НЕ РАЗОБРАНО` — нет.

**Оценка потенциала сокращения:** удаление даёт **≈ 1050 LOC** из 4200 (25%): `block_ownership.py` 109 + `list_detection.py` 335 + `safety_merge.py` 147 + `block_lookup.py` 52 + `heading.py` 367 + `physical.py` 40 + `identity.py` 17 + `structure.py` 22. Из них **≈ 0 LOC** требуют изменения production-кода потребителей (все удаляемые символы не имеют production-импортёров) — правки касаются только тестов (7 файлов) и docstring'ов.
