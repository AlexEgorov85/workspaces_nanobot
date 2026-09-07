# Contracts — `legal_summarizer`

Этот документ описывает **стабильные контракты** Skill: CLI, JSON
payloads, operation_id semantics.

## CLI — `scripts/cli.py`

```bash
python workspace/skills/legal_summarizer/scripts/cli.py \
    --file <path> [--length brief|detailed] [--question "..."] \
    [--focus "..."] [--confirm] [--operation-id <id>] \
    [--max-chunks N] [--estimate-only]
```

| Параметр | Обязательный | Описание |
| --- | --- | --- |
| `--file` | да | `.pdf` / `.docx` / `.txt`. Абсолютный или относительный от корня. |
| `--length` | нет | `brief` (150–250 слов) / `detailed` (800–1200 слов). |
| `--question` | нет | Конкретный вопрос по документу. |
| `--focus` | нет | Тема для финального reduce (не утекает в map). |
| `--confirm` | нет | Подтвердить длинную операцию. |
| `--operation-id` | нет | Idempotency + resume. |
| `--max-chunks` | нет | Override `max_chunks_for_execution`. |
| `--estimate-only` | нет | Только оценить, без LLM. |

`--question` и `--length` — взаимоисключающие.

## CLI — `scripts/cli_query.py`

Follow-up запросы по сохранённой `operation_id` без перепарсинга PDF:

```bash
python scripts/cli_query.py --operation-id <id> \
    [--field stats|articles|chunks|sections|tree|all]
```

## JSON contract

### `completed` / `partial`

```json
{
  "mode": "summarize",
  "status": "completed",
  "operation_id": "op_...",
  "subject": "Это договор аренды:...",
  "summary": "...",
  "length": "brief",
  "chars_in": 4523,
  "chunks": 1,
  "context_batches": 0,
  "sections": 0,
  "strategy": "single",
  "title": "...",
  "stats": {
    "chars_in": 452300,
    "chunks_total": 20,
    "duration_sec": 387.4,
    "strategy": "map_reduce_hierarchical"
  }
}
```

`stats.map_calls`, `section_reduce_calls`, `total_llm_calls`,
`reduce_calls`, `retries` — фильтруются на output boundary
(`output.presenter._HIDDEN_LLM_CALL_COUNTERS`) чтобы не раздражать
пользователя.

### `confirmation_required`

```json
{
  "status": "confirmation_required",
  "chars_in": 2531314,
  "options": {
    "brief":    {"min_sec": 40, "max_sec": 156, "words": 250, "label": "кратко"},
    "detailed": {"min_sec": 416, "max_sec": 624, "words": 1000, "label": "подробно"}
  },
  "supports_question": true
}
```

### `requires_continuation`

```json
{
  "status": "requires_continuation",
  "operation_id": "op_...",
  "summary": {"chars_in": ..., "chunks_total": ..., "title": ...},
  "hint": "Выбранная выборка (N chunks) превышает max_chunks_for_execution=M."
}
```

### `failed`

```json
{
  "status": "failed",
  "operation_id": "op_...",
  "error": {"code": "EMPTY_DOCUMENT", "message": "Документ не содержит текста"}
}
```

## operation_id

```text
full document text → SHA-256 → operation_id
```

* **Детерминированный** для одного и того же `(text, length, document_path, question)`.
* Изменение текста → новый `operation_id`.
* Изменение `length` / `question` → новый `operation_id`.
* Параметр `--operation-id` позволяет **resume** прерванной операции.

## Manifest

На диске сохраняется:

```text
data_store/cache/skills/legal_summarizer/<operation_id>/
├── manifest.json         # структура + chunk_states + section_summaries
├── chunks/<chunk_id>.json
└── result.json           # финальный JSON completed/partial
```

## Cache semantics

* **Idempotency**: `run()` с тем же `operation_id` после `status="completed"`
  возвращает cached result без LLM-вызовов.
* **Resume**: прерванный `run` можно продолжить с тем же
  `--operation-id --confirm`. Уже записанные `chunks/*.json` НЕ
  переобрабатываются.
* **Document cache**: при первом прогоне через session-папку
  (`data_store/cache/sessions/<key>/...`) chunk-summaries
  сохраняются на диск и переиспользуются для follow-up вопросов.