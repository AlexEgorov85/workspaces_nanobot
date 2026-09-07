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
| `--length` | нет | `brief` (150–250 слов), `detailed` (800–1200). |
| `--question` | нет | Конкретный вопрос (взаимоисключающе с `--length`). |
| `--focus` | нет | Тема для финального reduce (не утекает в map). |
| `--confirm` | нет | Подтвердить длинную обработку. |
| `--operation-id` | нет | Id для resume / idempotency. |

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
  "strategy": "single"
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

## Follow-up запросы

После прогона результат содержит `result.operation_id` — это ключ к
сохранённым данным (`data_store/cache/skills/legal_summarizer/<operation_id>/`).

Для follow-up вопросов используй кастомный tool `legal_summarizer_query`:

```python
legal_summarizer_query(operation_id="<op_id>", field="stats")
legal_summarizer_query(operation_id="<op_id>", field="chunks")
legal_summarizer_query(operation_id="<op_id>", field="sections")
legal_summarizer_query(operation_id="<op_id>", field="tree")
legal_summarizer_query(operation_id="<op_id>", field="all")
```

Подробности — `workspace/TOOLS.md` раздел «legal_summarizer_query».

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