---
name: legal_summarizer
description: Юридический анализ PDF/DOCX/TXT — вызывай ТОЛЬКО через `python skills/legal_summarizer/scripts/cli.py --file <path>`. Skill сам решает, нужен ли пользовательский confirm; для длинных документов (оценка > 2 минут) сначала вернёт confirmation_required. `office_files.extract_metadata()` (раньше `summarize()`) — это НЕ саммари, а метаданные; не подменяй cli.
metadata: {"nanobot":{"emoji":"📄","always":true}}
---

# Legal Summarizer — единственный путь: `cli.py`

> ⚠️ **ПРАВИЛО #1 (нарушать нельзя):** саммари делает ТОЛЬКО
> `python skills/legal_summarizer/scripts/cli.py --file <path>`. Никаких
> прямых вызовов `workspace.utils.office_files.extract_metadata()`,
> `extract_text()` или `from utils.office_files import …`.
>
> `office_files.extract_metadata()` (раньше `summarize()`) — это **НЕ
> саммари**: функция возвращает только метаданные (формат, размер, число
> страниц/таблиц, preview 500 символов) и НЕ делает LLM-анализ. Если
> ты её вызовешь, пользователь получит пустую болтовню о формате файла
> вместо анализа. Это самая частая ошибка при работе с этим skill'ом
> (см. инцидент 2026-08-27).

## Когда вызывать

- Пользователь прислал файл `.pdf` / `.docx` / `.txt` и просит «расскажи,
  что в договоре», «о чём акт», «объясни претензию», «суммаризуй»,
  «проанализируй документ».
- Задача: понять, о чём документ и какие в нём ключевые условия.

Когда **не** вызывать:

- Документ — НЕ юридический (финансовый отчёт, маркетинговая PDF) — пиши
  обычное саммари сам, без переписывания терминов.
- Документ защищён паролём или является сканом без текстового слоя — skill
  вернёт ошибку «документ не содержит извлекаемого текста»; тогда попроси
  текстовую версию у пользователя.

## Запуск

CLI вызывается агентом напрямую через интерпретатор (без отдельных
`.bat`/`.sh`-обёрток — это антипаттерн проекта):

```bash
python workspace/skills/legal_summarizer/scripts/cli.py --file <path> [--flags...]
```

Либо относительный путь из `workspace/`:

```bash
python skills/legal_summarizer/scripts/cli.py --file <path> [--flags...]
```

| Параметр | Обязательный | Описание |
|:---|:---:|:---|
| `--file` | да | Путь к документу: `.pdf`, `.docx`, `.txt`. **Абсолютный** путь ИЛИ относительный от корня проекта с полным префиксом (см. ниже). |
| `--length` | нет | `brief` (150–250 слов), `medium` (400–600, по умолч.), `detailed` (800–1200) |
| `--focus` | нет | Что особенно подсветить (попадает только в финальный reduce, не в map-чанки). |
| `--confirm` | нет | Подтвердить полную обработку. **Без флага** для длинных документов skill вернёт `confirmation_required` и завершит работу. |
| `--operation-id` | нет | Идентификатор ранее созданной operation (для resume). |

**Как передавать `--file`** (важно — `cli.py` не делает redirect сам):

- ✅ **Абсолютный путь:** `C:\Users\<user>\.nanobot\workspace\data_store\cache\sessions\<session_key>\<file>.pdf`
- ✅ **Относительный от корня проекта** (cwd=workspace-корень): `data_store/cache/sessions/<session_key>/<file>.pdf`
- ❌ Только имя файла `<file>.pdf` без префикса — будет `Файл не найден`.

**Подсказка:** при прикладывании файла через канал агент видит маркер
`[Attachment: <basename> (saved at <path>)]` рядом с пользовательским
сообщением. Если извлечённый текст документа превышает порог
`channels.document_text_threshold` (по умолчанию 20000 символов), в промпт
кладётся маркер `[File: <basename> — text omitted (len=… > threshold=…);
read at <path>]` — путь к файлу сохранён, чтобы можно было прочитать
самому. Бери путь прямо из этих маркеров — не перебирай каталоги через
`find_files`/`Get-ChildItem`.

## Протокол (Phase 2B — Structure-Aware Context Batching)

### Короткий документ (≤ `single_call_threshold`)

```bash
python .../cli.py --file small.pdf
```

Skill сам выполняет один вызов LLM и возвращает:

```json
{
  "mode": "summarize",
  "status": "completed",
  "operation_id": "op_...",
  "subject": "Это договор аренды: ...",
  "summary": "...",
  "length": "medium",
  "chars_in": 4523,
  "chunks": 1,
  "context_batches": 0,
  "sections": 0,
  "strategy": "single"
}
```

Покажи `subject` + `summary` пользователю.

### Длинный документ (> оценки порога)

```bash
python .../cli.py --file big.pdf
```

Skill **не запускает** обработку. Возвращает:

```json
{
  "mode": "summarize",
  "status": "confirmation_required",
  "operation_id": "op_...",
  "summary": {
    "chars_in": 452300,
    "chunks_total": 20,
    "context_batches_total": 10,
    "estimated_llm_calls": 11,
    "strategy": "map_reduce",
    "title": "..."
  },
  "estimate": {
    "min_seconds": 320,
    "max_seconds": 480,
    "confirmation_threshold_sec": 120
  },
  "hint": "Передайте --confirm для запуска полной обработки."
}
```

Скажи пользователю:

> Документ содержит 20 чанков (10 батчей). Полный анализ займёт примерно 5–8 минут. Продолжить?

После ответа «Да» повтори вызов с `--confirm`:

```bash
python .../cli.py --file big.pdf --confirm
```

Skill выполнит ВСЕ батчи внутри одного вызова (без polling,
без возвратов в AgentLoop между батчами) и вернёт:

```json
{
  "mode": "summarize",
  "status": "completed",
  "operation_id": "op_...",
  "subject": "...",
  "summary": "...",
  "stats": {
    "chars_in": 452300,
    "chunks_total": 20,
    "context_batches_total": 10,
    "sections_total": 8,
    "meaningful_sections": 5,
    "map_calls": 10,
    "section_reduce_calls": 5,
    "section_trim_calls": 0,
    "document_reduce_calls": 1,
    "reduce_calls": 6,
    "total_llm_calls": 16,
    "retries": 0,
    "duration_sec": 387.4,
    "strategy": "map_reduce_hierarchical"
  }
}
```

`strategy` принимает значения: `single`, `map_reduce_flat`,
`map_reduce_hierarchical`. Hierarchical включается автоматически при
`meaningful_sections >= 3` (топ-раздел с body ≥100 chars или level≤2).

### С пользовательским focus

```bash
python .../cli.py --file contract.pdf --focus 'сроки и штрафы за просрочку'
```

Focus попадает **только в финальный reduce** — не утекает в каждую
map-коммуникацию (это ключевая изоляция от Agent history).

### Safety net: слишком большой документ

Если `chunks_total > execution.max_chunks_for_execution` (по умолч. 50),
skill вернёт `status="requires_continuation"`. Это сознательная защита
от runaway-job'ов.

### Resume

Если процесс был прерван (Ctrl-C, OOM, краш) посреди полной обработки,
manifest остаётся в статусе `running` с частично выполненными
`chunks/<chunk_id>.json`. Повторный запуск с тем же `--operation-id --confirm`
**подхватывает уже готовые чанки** и продолжает с первого отсутствующего:

```bash
python .../cli.py --file big.pdf --length detailed \
        --operation-id op_1787852665_930c0706a2fc_brief --confirm
```

Skill читает `manifest.json`, находит выполненные `chunk_states` и
продолжает с первого pending. Уже записанные partials НЕ переобрабатываются.

**Идемпотентность для completed:** если operation уже `status="completed"`
(есть `result.json`), повторный вызов с тем же `--operation-id` **возвращает
кэш** без обращения к LLM.

**Новый operation_id без `--operation-id`:** каждый вызов `run()` без явного
`--operation-id` создаёт **новый** `operation_id`.

### Legacy manifest (Phase 2/3)

Операции, начатые до Phase 2B, могут иметь manifest в формате v1
(`batches_done: [int]`). При resume они нормализуются **in-memory** в
формат v2 без перезаписи на диск. Legacy operations всегда используют
**flat reduce** (нет section_path в chunk states).

## Метрики (Phase 3)

Stats разделены на:

* `map_calls` — количество batch-вызовов LLM
* `section_reduce_calls` — количество per-section reduce
* `section_trim_calls` — количество вызовов trim для крупных section_summaries
* `document_reduce_calls` — финальный document reduce (0 для single)
* `retries` — повторы при парсинге
* `total_llm_calls = map + reduce + retries`

## Архитектура

См. `ARCHITECTURE.md` для деталей и 19 архитектурных инвариантов.

Краткая схема:

```
file
  ↓
office_files → PhysicalDocument (adapter, не parser)
  ↓
DeterministicSectionDetector (confidence scoring, PDF outline priority)
  ↓
StructureAwareChunker (atomic tables, per-section chunking)
  ↓
pack_chunks (section-locality greedy, token budget)
  ↓
estimate_execution (confirmation?)
  ↓
executor (внутри одного run(), без возвратов в AgentLoop):
  ↓
  for batch in context_batches: process_context_batch
  ↓
  reduce_hierarchical | reduce_flat (по meaningful_sections)
  ↓
result.json + manifest.completed
```

## Что внутри

- Извлечение текста: `workspace.utils.office_files.extract_text`.
- Структура документа: `workspace/utils/office_files.py` (`detect_format`,
  `extract_tables`).
- Physical Document Model: `workspace/skills/legal_summarizer/scripts/structure/physical.py`
  (adapter над office_files + точечные обёртки для page/paragraph координат).
- Section Detection: `workspace/skills/legal_summarizer/scripts/structure/sections.py`
  (DOCX Heading styles, PDF outline, regex для русских юр. headings, threshold=0.60).
- Structure-Aware Chunker: `workspace/skills/legal_summarizer/scripts/structure/chunks.py`
  (атомарные tables, split by rows с `table_id`/`row_start`/`row_end`).
- Context Packer: `workspace/skills/legal_summarizer/scripts/packing.py`
  (section-locality greedy, token budget).
- Manifest v2: `workspace/skills/legal_summarizer/scripts/manifest.py`
  (chunk_states, context_batches, sections, section_summaries).
- LLM-prompt + parser: `workspace/skills/legal_summarizer/scripts/prompts.py`
  (multi-chunk JSON с валидацией всех chunk_id).
- Reducer (hierarchical/flat): `workspace/skills/legal_summarizer/scripts/reducer.py`.
- LLM-клиент: `lib.services.llm_client.call_llm` (с ретраями).
- Промпты: `workspace/skills/legal_summarizer/prompts/{summarize,reduce,section_reduce}_system.md`.
- Operation manifest на диске:
  `workspace/data_store/cache/skills/legal_summarizer/<operation_id>/`
  (`manifest.json`, `chunks/<chunk_id>.json`, `result.json`).

## Что не делать

- ❌ **НЕ вызывать `workspace.utils.office_files.extract_metadata()`**
  (раньше называлось `summarize()`) — она не делает саммари.
- ❌ Не вызывать `extract_text()` и не пытаться самому делать саммари
  из извлечённого текста — skill делает это аккуратно с правильными
  промптами и chunking'ом.
- ❌ Не вызывать skill для не-юридических документов.
- ❌ Не редактировать `subject` от LLM без необходимости.
- ❌ Не подставлять пользовательский текст в `system` промпт — это ломает
  формат саммари. Используй `--focus` для передачи focus-предпочтений.
- ❌ Не вызывать skill «в цикле» (--confirm → status=partial → ещё раз
  --confirm). Skill выполняет всю работу внутри одного вызова.
- ❌ Не запрашивать `--batch-index` или `--partial-summary` — старый
  streaming API полностью удалён. Manifest на диске — единственный
  source of truth для прогресса.