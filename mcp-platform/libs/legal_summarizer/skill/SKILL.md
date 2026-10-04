---
name: legal_summarizer
description: >-
  НЕ навык агента: каталог вне зоны загрузки SkillsLoader, файл в контекст
  модели не попадает. Документирует CLI capability для вызывающего с оболочкой
  (`python mcp-platform/libs/legal_summarizer/cli.py --file <path>`). Агенту
  оболочка недоступна (`tools.exec.enable = false`); из агента доступно
  только чтение состояния существующей операции — операция платформы
  `query_operation` по `operation_id`.
---

# Legal Summarizer — точка входа CLI capability

> **Это не навык агента.** Навыки читает `SkillsLoader` из
> `workspace/skills` и `workspace/plugins`; этот каталог он не смотрит, и
> файл никогда не попадал в контекст модели. Frontmatter `name:` /
> `metadata.nanobot` убран намеренно: с ним файл выглядел бы навыком
> (и выглядел так при `always: true`), и перенос каталога в `workspace/skills`
> тихо вооружил бы модель инструкцией запускать отключённую оболочку.
> На настоящий навык агента ссылается
> `workspace/skills/enterprise_mcp/SKILL.md` — там контракт вызовов.

> **Для агента недоступно.** `tools.exec.enable = false`, поэтому ни
> `cli.py`, ни опрос сессии через `write_stdin` невозможны. Из агента доступно
> только чтение состояния уже существующей операции — операция платформы
> `query_operation` по `operation_id`. Запуск нового прогона из агента
> невозможен; если он нужен — это отдельная операция платформы, а не вызов
> оболочки.

> **Осторожно с этим каталогом.** Здесь лежат системные промпты конвейера
> (`prompts/*.md`), которые грузит `libs/legal_summarizer/llm/prompts_runtime.py`
> на каждом суммари. Каталог нельзя сносить целиком: стражи
> `tests/legal_summarizer/architecture/test_skill_layout.py` требуют и
> `prompts/`, и `references/`. Снести можно только этот `SKILL.md` — и
> вместе с `test_skill_md_exists` в том стражe.

Ниже — документация CLI-точки входа capability для вызывающего, у которого
есть оболочка.

> ⚠️ **ПРАВИЛО #1 (нарушать нельзя):** саммари делает ТОЛЬКО
> `python -m libs.legal_summarizer.cli --file <path>`. Никаких
> прямых вызовов `office_files.extract_metadata()`,
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

> ℹ️ PYTHONPATH выставлять **не нужно** — `cli.py` сам подкладывает
> корень платформы в `sys.path`. Запускай его как модуль из корня
> платформы: `python -m libs.legal_summarizer.cli …` (абсолютный путь к файлу
> тоже работает — оба способа равнозначны).

> ⚠️ **ПРАВИЛО #4 (никакого retry-цикла):** если `cli.py` упал с
> `ImportError`/`timeout`/просто не печатает sentinel
> `__LEGAL_SUMMARIZER_DONE__` в течение `wait_timeout_ms=120000` —
> **НЕ ПЫТАЙСЯ запустить его ещё раз с тем же `--file` без
> `--operation-id`**. Повторный запуск создаст новую операцию
> (дублирование работы, плюс занятый фоновый процесс остаётся
> висеть). Если процесс перешёл в background (`session_id=...`),
> дождись sentinel одним `write_stdin(wait_timeout_ms=120000)`
> — **не более 2 попыток**. Если sentinel так и не пришёл — сообщи
> пользователю `operation_id` (если напечатан в первом `running` JSON)
> и остановись.

### Каноническая команда (Windows PowerShell)

```powershell
python "C:\Users\<user>\.nanobot\workspace\skills\legal_summarizer\scripts\cli.py" --file "<абсолютный_путь_к_pdf>" [--estimate-only | --length brief|detailed --confirm | --question "..." --confirm]
```

- **Один** аргумент с абсолютным путём к `cli.py` — без `cd ... &&`
  (PowerShell не поддерживает `&&`).
- Путь к файлу — **абсолютный** (берётся из media payload сообщения,
  либо из `data_store/sessions/<session_key>/files/<file>`).
- На первом запуске для длинного документа — **всегда** добавляй
  `--estimate-only`, чтобы получить `confirmation_required` и показать
  пользователю меню `brief/detailed/вопрос`.

### Каноническая команда (bash / Linux)

```bash
python mcp-platform/libs/legal_summarizer/cli.py --file "<path>" [--flags...]
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

> Перед запуском прочти [«Запуск»](#запуск) — там про `$env:PYTHONPATH`,
> абсолютные пути и отсутствие `cd ... &&`.

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

> ⚠️ **ПРАВИЛО #5 (потолок `write_stdin`):** `yield_time_ms <= 30000`,
> `wait_timeout_ms <= 120000` — это потолок параметров tool `write_stdin`
> (см. ошибки в логе инцидента 2026-09-10). Если процесс не допечатал
> sentinel за один `write_stdin(wait_timeout_ms=120000)` — **максимум
> одна повторная попытка** `write_stdin` с тем же `session_id`.
> Дальше — сообщи пользователю, что обработка превысила ожидаемое время,
> и прекрати вызовы. Не плоди 5+ итераций `write_stdin`.

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
mcp_enterprise_query_operation(operation_id="<op_id>", field="stats")    # метрики + article_count
mcp_enterprise_query_operation(operation_id="<op_id>", field="chunks")   # список chunks + summaries
mcp_enterprise_query_operation(operation_id="<op_id>", field="sections")  # список sections
mcp_enterprise_query_operation(operation_id="<op_id>", field="tree")      # иерархия sections
mcp_enterprise_query_operation(operation_id="<op_id>", field="all")      # весь manifest.json
```

Подробности — `workspace/TOOLS.md` раздел `mcp_enterprise_query_operation`.

### Контракт отказа по операции `query_operation`

Вызов идёт по протоколу MCP **в том же процессе**, где живёт capability.
Подпроцесса нет, его stdout не разбирается, а кодов возврата у вызова нет —
есть признак `isError` и конверт. Всё, что ниже раньше описывало wrapper и
exit code, снято вместе с агентской обёрткой (change
`2026-10-03-mcp-native-tools`, п. D6): `cli_failed`, `cli_not_found`,
`subprocess_error`, `empty_response` и `invalid_json` не существуют ни в коде,
ни в контракте, и искать их в отказе бессмысленно.

| ситуация | `error_type` домена | код конверта |
| --- | --- | --- |
| манифеста нет | `manifest_not_found` | `not_found` |
| манифест не парсится | `manifest_corrupted` | `internal` |
| версия манифеста не 2 | `manifest_unsupported_version` | `upstream_unavailable` |
| `field` не из перечня | `invalid_field` | `invalid_params` |

**Правила:**

- Успех — единственный путь без отказа: тело ответа с `status = "ok"`.
- Отказ приходит конвертом с кодом из таблицы. Читать надо код, а не текст:
  текст меняется, а код объявлен один раз и сверяется стражем с перечнем
  домена — рассогласоваться они не могут молча.
- `field` вне перечня **отказывает**, а не отдаёт `manifest` целиком. Раньше
  значение не из шести уходило в ветку `all`, и модель получала
  `status = "ok"` с целым документом вместо отказа: опечатка в имени поля
  стоила ответа на вопрос и не давала ни отказа, ни намёка на ошибку.

#### Семантика `chunks_total` vs `field=chunks`

Это **независимые источники**:

- `chunks_total` (поле `--field stats`) — логический/плановый счётчик
  чанков из manifest; отражает `chunks_total: N` в `manifest.json`,
  подсчитанный при планировании прогона.
- `chunks` (поле `--field chunks`) — массив **физических** partial-файлов
  в `<op>/chunks/*.json`, обрезанных по `--max-chunk-summary-chars`.

Их расхождение (`chunks_total != len(chunks)`) — **не баг**: часть
запланированных чанков может быть не выполнена (status=`partial`),
а физические partial-файлы могут иметь дополнительные версии. Tool
возвращает оба источника независимо и **не пытается их согласовать**.


## Что внутри

Capability состоит из:

* `servers/enterprise/capabilities/legal_summarizer/service/main.py` —
  единственное место, где домен встречается с остальной платформой; отдаёт
  операцию `query_operation`.
* `servers/enterprise/capabilities/legal_summarizer/tools/query_operation.py` —
  сама операция: схема строится из сигнатуры обработчика, домен вызывается
  в том же процессе.
* `../cli.py` — CLI-оболочка суммаризации, для ручного запуска.
* `../cli_query.py` — оболочка над доменной `query_operation` для ручного
  запуска; в пути вызова модели не стоит.
* девять runtime-слоёв рядом с доменом:
  `application/`, `cache/`, `chunking/`, `document/`, `execution/`,
  `llm/`, `output/`, `planning/`, `retrieval/`.
* `prompts/` — LLM-инструкции (summarize / section_reduce / reduce). Читает их
  `llm/prompts_runtime.py`; каталог лежит здесь, рядом с этим файлом, а не в
  корне домена.
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