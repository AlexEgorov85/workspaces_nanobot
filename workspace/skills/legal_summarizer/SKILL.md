---
name: legal_summarizer
description: Юридический анализ PDF/DOCX/TXT — вызывай ТОЛЬКО через `python workspace/skills/legal_summarizer/scripts/cli.py --file <path>`. Skill сам решает, нужен ли пользовательский confirm; для длинных документов (оценка > 2 минут) сначала вернёт confirmation_required. `office_files.extract_metadata()` (раньше `summarize()`) — это НЕ саммари, а метаданные; не подменяй cli.
metadata: {"nanobot":{"emoji":"📄","always":true}}
---

# Legal Summarizer — единственный путь: `cli.py`

> ⚠️ **ПРАВИЛО #1 (нарушать нельзя):** саммари делает ТОЛЬКО
> `python workspace/skills/legal_summarizer/scripts/cli.py --file <path>`. Никаких
> прямых вызовов `workspace.utils.office_files.extract_metadata()`,
> `extract_text()` или `from utils.office_files import …`.
>
> `office_files.extract_metadata()` (раньше `summarize()`) — это **НЕ
> саммари**: функция возвращает только метаданные (формат, размер, число
> страниц/таблиц, preview 500 символов) и НЕ делает LLM-анализ. Если
> ты её вызовешь, пользователь получит пустую болтовню о формате файла
> вместо анализа. Это самая частая ошибка при работе с этим skill'ом
> (см. инцидент 2026-08-27).

> ⚠️ **ПРАВИЛО #2 (обязательно для длинных документов):** если skill
> вернул `status="confirmation_required"`, **НИКОГДА не вызывай его
> сразу с `--confirm`** и **НИКОГДА не выбирай режим за пользователя**.
> Сначала покажи пользователю **меню из двух вариантов** из
> `options[]` (с `id`, `label`, `min/max_seconds`, `description`) и
> подскажи, что можно задать конкретный вопрос через `--question`.
> Только **после явного выбора** пользователя вызывай `cli.py` повторно
> с нужным `--length` или `--question` и `--confirm`.
> Если пользователь ответил «давай» без уточнений — по умолчанию
> `--length brief`.

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

> ⚠️ **Windows PowerShell:** используйте **абсолютный путь к cli.py**
> одним аргументом в `exec` (без `cd ... &&` — PowerShell не поддерживает `&&`):
>
> ```bash
> python "C:\Users\<user>\.nanobot\workspace\skills\legal_summarizer\scripts\cli.py" --file "<path>" [--flags...]
> ```

```bash
python workspace/skills/legal_summarizer/scripts/cli.py --file <path> [--flags...]
```

| Параметр | Обязательный | Описание |
|:---|:---:|:---|
| `--file` | да | Путь к `.pdf` / `.docx` / `.txt`. |
| `--length` | нет | `brief` (150–250 слов) или `detailed` (800–1200). |
| `--question` | нет | Конкретный вопрос (взаимоисключающе с `--length`). |
| `--focus` | нет | Тема для финального reduce (не утекает в map). |
| `--context` | нет | JSON с историей чата для LLM. Пример: `'[{"role":"user","content":"..."}]'`. |
| `--confirm` | нет | Подтвердить длинную обработку. |
| `--operation-id` | нет | Id для resume / idempotency. |
| `--max-chunks` | нет | Override `max_chunks_for_execution` из project.json. |
| `--estimate-only` | нет | Только оценить документ (без LLM-вызовов). |

Полный список — `cli.py --help`. Подробности — `references/contracts.md`.

## Протокол

### Короткий документ (≤ `single_call_threshold`)

```bash
python .../cli.py --file small.pdf
```

```json
{
  "status": "completed",
  "operation_id": "op_...",
  "subject": "Это договор аренды: ...",
  "length": "brief",
  "strategy": "direct"
}
```

### Длинный документ (> оценки порога)

Без `--confirm` skill **не запускает** обработку. Возвращает
`confirmation_required` с меню:

```json
{
  "status": "confirmation_required",
  "options": {
    "brief":    {"min_sec": 40,  "max_sec": 156, "words": 250, "label": "кратко"},
    "detailed": {"min_sec": 416, "max_sec": 624, "words": 1000, "label": "подробно"}
  },
  "supports_question": true
}
```

Покажи пользователю меню **коротким** текстом (≤ 250 символов):

```
Документ большой (~N симв). Какой вариант?

• Кратко (~X мин, ~250 слов) — суть и ключевые условия
• Подробно (~Y мин, ~1000 слов) — каждый раздел простым языком
• Или задайте конкретный вопрос — найду релевантные части
```

После выбора повтори `cli.py` с нужным `--length/--question --confirm`.

### Resume

Прерванный прогон можно продолжить:

```bash
python .../cli.py --file big.pdf --length detailed \
        --operation-id op_1787852665_930c0706a2fc --confirm
```

Уже записанные `chunks/*.json` НЕ переобрабатываются.

### Polling

cli.py печатает в самом начале:

```json
{
  "status": "running",
  "estimated_total_sec": 440,
  "poll_interval_hint_sec": 75
}
```

В самом конце — sentinel `__LEGAL_SUMMARIZER_DONE__`. Дожидайся его
одним блокирующим `exec` (или `write_stdin(wait_for=..., wait_timeout_ms=120000)`).
Не опрашивай по таймеру — это лишние LLM-вызовы.

## Follow-up вопросы

После успешного прогона результат содержит `result.operation_id`. Для
follow-up вопросов по **тому же документу** используй `--question` через
**document-level cache** (быстрый путь, без повторного map-LLM):

```bash
python .../cli.py --file contract.pdf --question "Какие штрафы за нарушение срока?" --confirm
```

Это работает через `strategy: "document_cache_question"` (1 LLM-вызов для
synthesis). Document-level cache хранит только question-independent
baseline summaries (chunk + section), которые используются как cheap
context вместе с chunk.text. Подробности — `references/architecture.md`
раздел «Document-level cache».

Когда `--question` использовать **нельзя** (например, документ ещё не
обработан / mtime изменился / нет workspace_root) — fallthrough на обычный
pipeline (новый map-reduce с полным LLM-анализом выбранных chunks).

Для read-only агрегации manifest (без LLM) используй кастомный tool
`legal_summarizer_query`:

```python
legal_summarizer_query(operation_id="<op_id>", field="stats")    # метрики + article_count
legal_summarizer_query(operation_id="<op_id>", field="chunks")   # список chunks + summaries
legal_summarizer_query(operation_id="<op_id>", field="sections")  # список sections
legal_summarizer_query(operation_id="<op_id>", field="tree")      # иерархия sections
legal_summarizer_query(operation_id="<op_id>", field="all")      # весь manifest.json
```

Подробности — `workspace/TOOLS.md` раздел «legal_summarizer_query».

**Resume прерванного прогона** (`status="partial"`): используй тот же `--operation-id --confirm`
для **тот же операции** (`text + length + question`). Уже записанные
`chunks/*.json` НЕ переобрабатываются. Это **не** для follow-up вопросов —
`--question` создаёт новый `operation_id` (новый прогон).

## Что внутри

Skill состоит из:

* `scripts/cli.py` — CLI entry point.
* `scripts/cli_query.py` — follow-up по `operation_id`.
* `scripts/` — executable runtime Skill (9 runtime-слоёв:
  `application/`, `cache/`, `chunking/`, `document/`, `execution/`,
  `llm/`, `output/`, `planning/`, `retrieval/`).
* `prompts/` — LLM-инструкции (summarize / section_reduce / reduce).
* `references/` — подробные документы: `architecture.md`, `contracts.md`,
  `testing.md`.

### Режим `--length brief`: всегда ровно один структурный Chunk

`brief` режим — это **не** выборка N canonical chunks.
Это **компактное структурное представление всего документа**,
собранное в **ровно один** `Chunk` через
`application.brief_context.BriefContextBuilder.build_brief_chunk`:

* Источники: `DocumentAnalysis.physical` + `DocumentAnalysis.structure`
  напрямую. `analysis.chunks` (canonical) **не используется**.
* Структура `chunk.text`:
  ```text
  DOCUMENT STRUCTURE
  <рекурсивный outline всех значимых structural nodes>

  DOCUMENT CONTENT
  [Preamble]
  <preamble blocks>
  [<Section heading>]
  <все physical blocks subtree в document order>
  ```
* `len(ctx.chunks) == 1` → `strategy="direct"`, `plan=None`
  (см. `application/context_builder.py`). Никакого map-reduce.
* При превышении `max_chars` (рассчитывается динамически от
  `agents.defaults.contextWindowTokens` ×
  `chunking.brief_input_ratio` × `brief_context.chars_per_token`)
  сжимается **текст** секций (через
  `application.brief_compression`), но сами секции целиком
  **не удаляются**. Сокращённые секции получают маркер
  `[BRIEF: section content truncated]`.
* Таблицы передаются атомарно (на уровне блока, не строки).

## Что НЕ делать

- ❌ **НЕ вызывать `workspace.utils.office_files.extract_metadata()`**
  (раньше `summarize()`) — она не делает саммари.
- ❌ Не вызывать `extract_text()` и не делать саммари самому.
- ❌ Не вызывать skill для не-юридических документов.
- ❌ Не вызывать skill «в цикле» (--confirm → status=partial → ещё раз
  --confirm). Skill выполняет всю работу внутри одного вызова.
- ❌ Не запрашивать `--batch-index` или `--partial-summary` — старый
  streaming API удалён. Manifest на диске — единственный source of truth
  для прогресса.

## Подробности

| Документ | Что внутри |
| --- | --- |
| `references/architecture.md` | Слои, dependency direction, invariants. |
| `references/contracts.md` | JSON-контракты, operation_id, manifest, cache semantics. |
| `references/testing.md` | Тестовая инфраструктура, mock LLM, single-flight tests. |