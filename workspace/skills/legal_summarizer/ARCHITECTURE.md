# Architecture — `legal_summarizer` Phase 2B

Этот документ фиксирует **19 архитектурных invariants** Phase 2B
(Structure-Aware Context Batching). Любое изменение кода skill'а,
нарушающее invariant, требует явного обоснования в PR.

## Invariants

1. **`office_files` — единственный источник физического извлечения документа.**
   `workspace/utils/office_files.py` парсит PDF/DOCX/TXT/PPTX/XLSX/XLS/CSV.
   Skill **не дублирует** парсеры — только использует их публичный API.

2. **`PhysicalDocument` — normalization/adapter поверх office_files, не второй parser.**
   `scripts/structure/physical.py` импортирует только `office_files` +
   точечные обёртки для координат (`pypdf.PdfReader.pages[i]` ради
   `page_index`, `docx.Document.paragraphs[i].style` ради heading).
   Никаких собственных PDF/DOCX логик извлечения текста.

3. **`DocumentBlock.ordinal` задаёт единственный canonical document order.**
   Все downstream слои (section detection, chunker, packing, reduce)
   **никогда** не переупорядочивают блоки. `block_to_section`,
   `chunk.block_indices` — монотонны.

4. **Section detection — deterministic + confidence scoring (не бинарный regex).**
   Score per source:
   - DOCX `style == "Heading N"`: 0.95
   - PDF outline: 0.95
   - Regex `Статья N` / `Глава N` / `Раздел N` / `§ N`: 0.80-0.85
   - Regex `^\d+\.` / `^\d+\.\d+\.`: 0.65-0.70
   
   Threshold = 0.60. Ниже — не считается heading.

5. **PDF outline — primary source; regex только дополняет при неполноте.**
   Если PDF содержит outline — он используется как ground truth.
   Regex применяется только для sub-section path внутри outline-узла.

6. **Tables — отдельные `DocumentBlock(block_type="table")`.**
   Никогда не склеиваются с paragraph в один block.

7. **Chunk никогда не теряет `section_id` / `section_path` / `page_start` /
   `page_end` / `block_indices`.** Эти поля обязательны.

8. **Context packing идёт в document/section order (без перепрыгивания
   через sections).** Семантическая локальность > utilization.

9. **Agent history не попадает в map LLM (context=None всегда).**
   Skill LLM получает только system + user_body (с chunk contents
   + section/page metadata). Никакой Agent conversation в prompt.

10. **`partial_summary` НЕ используется для orchestration через Agent
    context.** Agent видит только финальный `result` от `run()`.
    Промежуточные per-batch результаты живут только в `manifest.json`
    на диске.

11. **Agent запускает одну операцию Skill.**
    `python cli.py --file <path> [--confirm]` — single call.
    Никаких `--batch-index`, `--partial-summary`, streaming-loop.

12. **Manifest — source of truth для resume.**
    `manifest.json` хранит весь прогресс. Resume читает manifest,
    не Agent conversation.

13. **Resume определяется по `chunk_states`.**
    `batches_done[]` — legacy alias для backward-compat в тестах.
    Source of truth — `chunk_states: {chunk_id: {...}}`.

14. **Reduce собирается заново из завершённых `chunk_states` на диске.**
    При resume `reduce` не перезапускает уже обработанные chunks;
    `partials` загружаются из `chunks/<chunk_id>.json`.

15. **Hierarchical reduce включается при `meaningful_sections >= 3`.**
    `meaningful` = section с chunk ≥100 chars ИЛИ (level ≤2 И heading непустое).

16. **Confirmation происходит ДО первого map LLM call.**
    `confirmation_required` ветка не делает LLM-вызовов.
    `confirmation_threshold_sec` сравнивается с max-оценкой
    `context_batches_total * estimated_chunk_duration_sec`.

17. **`max_chunks_for_execution` — safety limit, не механизм orchestration.**
    Если `chunks_total > max_chunks_for_execution`, skill возвращает
    `requires_continuation` и **не** пытается обрабатывать порциями.

18. **600 страниц обрабатываются внутри одного Skill execution.**
    Один `run()` обрабатывает весь документ. Никакого split на
    AgentLoop-итерации.

19. **LLM-call accounting разделён на `map_calls` / `reduce_calls` /
    `retries`.** Не единый hard-assertion `total == batches + sections + 1`.

20. **`merge_short_sections` защищает от взрыва числа чанков от микро-секций.**
    Regex-детектор `_RE_NUMBERED_LEVEL_1` (score 0.65) срабатывает на
    списках вида «1. … 2. … 3. …» с микро-body. Без post-processing каждая
    строка становится отдельной секцией → раздувание `len(sections)`
    и пропорционально `len(chunks)`. После `detect_sections` всегда
    вызывается `merge_short_sections(tree, blocks, min_section_chars=200)`:
    секции `level ∈ {1, 2}` с суммарным `char_count < min_section_chars`
    сливаются с **соседней секцией того же parent_id** (предыдущая или
    следующая). Heading микро-секции приклеивается к heading целевой
    через `"; "`. Инварианты после merge:
    - `len(tree.sections)` строго уменьшается (или не меняется);
    - `block_to_section` покрывает все блоки без дыр;
    - каждый `block_to_section[ord]` указывает на существующий `sid`;
    - `SectionTree.root_id` остаётся прежним.

## Файловая структура Phase 2B

```
workspace/skills/legal_summarizer/
├── SKILL.md                            ← переписан
├── ARCHITECTURE.md                     ← этот файл
├── prompts/
│   ├── summarize_system.md             ← + multi-chunk JSON инструкция
│   ├── reduce_system.md                ← + section navigation инструкция
│   └── section_reduce_system.md        ← NEW
├── scripts/
│   ├── cli.py                          ← без изменений в CLI-интерфейсе
│   ├── summarizer.py                   ← переписан как тонкая оркестрация
│   ├── output.py                       ← + новые поля в completed
│   ├── skill_config.py                 ← + context_batching секция
│   ├── llm.py                          ← без изменений
│   ├── structure/
│   │   ├── __init__.py
│   │   ├── physical.py                 ← NEW: adapter
│   │   ├── sections.py                 ← NEW: confidence scoring
│   │   └── chunks.py                   ← NEW: structure-aware chunker
│   ├── packing.py                      ← NEW: section-locality greedy
│   └── reducer.py                      ← NEW: hierarchical + flat
└── tests/
    ├── test_structure_physical.py      ← NEW
    ├── test_structure_sections.py      ← NEW
    ├── test_structure_chunks.py        ← NEW
    ├── test_packing.py                 ← NEW
    ├── test_reducer.py                 ← NEW
    └── test_skill_legal_summarizer.py  ← адаптирован
```

## Manifest v2 (краткая схема)

```json
{
  "version": 2,
  "operation_id": "...",
  "status": "running" | "completed" | "failed",
  "chunks_total": 20,
  "context_batches_total": 10,
  "sections": {
    "s_001": {"level": 1, "heading": "...", "section_path": "1", "chunk_ids": ["001", "002"]}
  },
  "chunk_states": {
    "001": {"status": "completed", "context_batch_id": "cb_001", "section_id": "s_001", "section_path": "1", "page_start": 1, "page_end": 5, "result_path": "chunks/001.json", "duration_sec": 12.3}
  },
  "context_batches": {
    "cb_001": {"chunk_ids": ["001", "002"], "section_paths": ["1"], "status": "completed"}
  },
  "section_summaries": {"s_001": "..."},
  "actual_llm_calls": null,
  "estimated_llm_calls": 25,
  "batches_done": ["cb_001"],
  "batches_failed": [],
  "last_error": null
}
```

## Legacy manifest v1 (Phase 2/3)

```json
{
  "operation_id": "...",
  "status": "...",
  "chunks_total": 20,
  "batches_done": [0, 1, 2],
  "batches_failed": [],
  "last_error": null,
  "actual_llm_calls": 21
}
```

v1 → v2 нормализация **in-memory**, без перезаписи на диск.
Legacy operations всегда используют flat reduce (нет section_path).

## Chunk provenance и реконструкция

Каждый `Chunk` несёт `block_indices` (отсортированные ordinal'ы
`DocumentBlock`). Для split chunks (один block больше `max_chunk_chars` и
разбит через `_split_block_with_offsets`) дополнительно сохраняются
`source_char_start` / `source_char_end` — абсолютные offsets относительно
`DocumentBlock.content`. None для целого block (back-compat со старым
cache).

`reconstruct_source_fragment(chunk, *, doc=None, blocks=None)` —
единственная точка восстановления точного исходного текста chunk'а:

* один block + None offsets → `block.content`;
* один block + offsets → `block.content[source_char_start:source_char_end]`;
* multi-block (greedy join) → конкатенация через `\n\n`;
* table chunk → `block.content` (таблица атомарна).

Эта функция используется как cache-assisted follow-up retrieval для
точного восстановления исходного текста из `PhysicalDocument` без
повторной обработки.

## Brief structural coverage

Для `--length brief` используется `select_brief_chunks_structured` с
round-robin по секциям: `effective_limit = min(max_chunks, ceil(total *
coverage_ratio))`. `coverage_ratio` берётся из
`chunking_config.brief_coverage_ratio` (default 0.5). round-robin
гарантирует покрытие всех секций, а не только начала документа.

Опциональный параметр `brief_truncate_chars_per_block` в
`chunking_config` включает bounded LLM-input: каждый выбранный chunk
обрезается до N символов через `apply_brief_text_budget`. Физический
документ **не обрезается** — `apply_brief_text_budget` создаёт новые
`Chunk` объекты через `dataclasses.replace`, оставляя `source_char_*`
и `block_indices` нетронутыми. Tables не обрезаются этим параметром.

## Document cache: provenance и freshness

Каждая запись в document-cache содержит:

```json
{
  "chunk_id": "017",
  "summary": "...",
  "section_id": "s_0007",
  "section_path": "7.3",
  "page_start": 42,
  "page_end": 45,
  "block_indices": [10, 11, 12],
  "block_types": ["paragraph"],
  "source_char_start": 1000,
  "source_char_end": 1850,
  "table_id": null,
  "chunk_text_preview": "..."
}
```

`meta.json` хранит `physical_cache_key` (sha256 от файла, обрезанный
через `fingerprint_file`). Перед использованием provenance
`cache_is_fresh` проверяет совпадение ключей — если файл изменился,
cache stale и provenance не используется.

Legacy cache без provenance-полей загружается (поля = `None`); для
retrieval такие записи получают score только за `summary`.

## Cache-assisted follow-up retrieval

При `--question` flow последовательность:

1. `select_cached_candidates(question, cache)` — lexical match по
   meaningful terms (prefix-match 4 символа) с confidence threshold
   (`min_top_score=3`).
2. Если candidates confident + cache свежий →
   `reconstruct_candidate_source(candidate, doc, is_fresh=True)` —
   точная реконструкция текста через `reconstruct_source_fragment`.
3. `expand_followup_context(target_block_index, doc,
   target_source_text=...)` — bounded window (target + 1 prev + 1 next,
   max 8000 chars). Tables получают отдельное правило (heading before +
   table + paragraph after).
4. Возвращается `list[Chunk]` для подстановки в существующий pipeline.

При stale cache, weak match, no match или ошибке реконструкции —
fallback на существующий `_relaxed_lexical_fallback` → bounded
top-of-document → keyword match. Existing fallbacks не отключаются.

## Файловая структура (обновлено)

```
workspace/skills/legal_summarizer/
├── ARCHITECTURE.md
├── SKILL.md
├── prompts/
└── scripts/
    ├── cli.py
    ├── summarizer.py                ← + cache-assisted follow-up integration
    ├── brief_representation.py      ← NEW: apply_brief_text_budget
    ├── brief_strategy.py
    ├── output.py
    ├── skill_config.py
    ├── llm.py
    ├── document_cache.py            ← + provenance + freshness helpers
    ├── cache_followup.py            ← NEW: retrieve_followup_context_via_cache
    ├── cached_retrieval.py          ← NEW: select_cached_candidates
    ├── provenance_reconstruction.py← NEW: reconstruct_candidate_source
    ├── context_expansion.py         ← NEW: expand_followup_context
    ├── fingerprint.py
    ├── structure/
    │   ├── __init__.py
    │   ├── physical.py
    │   ├── sections.py
    │   └── chunks.py                ← + source_char_* + reconstruct_source_fragment
    ├── packing.py
    ├── reducer.py
    └── tests/
```