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
| `--length` | нет | `brief` (150–250 слов, первые 8 chunks), `detailed` (800–1200, весь документ). По умолч. — из `project.json`. |
| `--question` | нет | Конкретный вопрос по документу (взаимоисключающе с `--length`). Skill найдёт релевантные chunks (≤8) и ответит кратко. Если ничего не найдено — прочитает весь документ. |
| `--focus` | нет | Что особенно подсветить (попадает только в финальный reduce, не в map-чанки). |
| `--confirm` | нет | Подтвердить полную обработку. **Без флага** для длинных документов skill вернёт `confirmation_required` и завершит работу. |
| `--operation-id` | нет | Идентификатор ранее созданной operation (для resume). |

**Как передавать `--file`** (важно — `cli.py` не делает redirect сам):

- ✅ **Абсолютный путь:** `C:\Users\<user>\.nanobot\workspace\data_store\cache\sessions\<session_key>\<file>.pdf`
- ✅ **Относительный от корня проекта** (cwd=workspace-корень): `data_store/cache/sessions/<session_key>/<file>.pdf`
- ❌ Только имя файла `<file>.pdf` без префикса — будет `Файл не найден`.

**Подсказка:** при прикладывании документа канал передаёт агенту только
пути к файлам; текстовое описание вложения формирует единый механизм
`extract_documents` (см. `RuntimePatcher.patch_document_text_threshold`).
В промпте каждый документ выглядит так:

- маленький (≤ `channels.document_text_threshold`, по умолчанию 20000 символов):
  ```
  [File: <basename> (saved at <path>)]
  <извлечённый текст>
  ```
- большой (> порога):
  ```
  [File: <basename> (saved at <path>)]
  [text omitted (len=… > threshold=…)]
  ```

Путь к файлу присутствует **всегда** (и при полном тексте, и при обрезке) —
бери его прямо из заголовка `[File: … (saved at …)]` и передавай в
`cli.py --file <path>`. Не перебирай каталоги через `find_files`/
`Get-ChildItem`.

## Режимы работы

Skill поддерживает три режима (выбирается через `--length` или `--question`):

| Режим | Что делает | Время (ГК РФ, 20 chunks) | Когда использовать |
|---|---|---|---|
| `--length brief` | Первые 8 chunks, краткое саммари (~250 слов) | ~3–4 мин | «Расскажи кратко что в договоре» |
| `--length detailed` | Полный map-reduce: все chunks, подробное саммари (~1000 слов) | ~6–8 мин | «Расскажи подробно» |
| `--question "..."` | Ищет chunks по ключевым словам вопроса (≤8), отвечает кратко. Если ничего не нашёл — читает весь документ. | ~3–4 мин | «Что про штрафы за просрочку?», «Какой срок оплаты?» |

`--question` и `--length` — взаимоисключающие.

## Переиспользование результатов (document-cache)

Chunk-summaries сохраняются в document-cache автоматически при первом
вызове skill, когда путь к файлу содержит `data_store/cache/sessions/<key>/...`
(т.е. файл пришёл из session-папки). При повторном вопросе к тому же
документу в той же сессии skill подхватывает готовые summaries и
пропускает map-фазу (экономия 3–5 мин).

`<safe_session_key>` извлекается из пути тем же алгоритмом, что
использует `SessionFileRedirectHook` (см. `workspace/utils/session_key.py`).
Если путь не содержит session-папки — document-cache не используется
(полная обратная совместимость).

⚠️ НЕ сообщайте пользователю «кэширую» или «использую кэш» — это
техническая деталь. Просто вызывайте skill как обычно.

В payload `completed` есть поле `cache_stats` с `chunks_from_cache` /
`chunks_processed` — для диагностики, не для пользователя.

Кэш удаляется вместе с папкой сессии (политика SessionFileRedirectHook).

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
  "length": "brief",
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

Skill **не запускает** обработку. Возвращает `confirmation_required` с
готовым меню из двух вариантов для показа пользователю:

```json
{
  "mode": "summarize",
  "status": "confirmation_required",
  "summary": {"chars_in": 2531314},
  "options": [
    {
      "id": "brief",
      "label": "краткая выжимка",
      "min_seconds": 187,
      "max_seconds": 343,
      "words_estimate": 250,
      "description": "Прочитаю только ключевые разделы (введение, подписи, заголовки всех частей). Детали могу пропустить."
    },
    {
      "id": "detailed",
      "label": "подробный анализ",
      "min_seconds": 416,
      "max_seconds": 624,
      "words_estimate": 1000,
      "description": "Прочитаю весь документ, опишу каждый раздел простым языком."
    }
  ],
  "supports_question": true,
  "hint": "Покажите пользователю меню из двух вариантов (brief / detailed) и упомяните, что можно задать конкретный вопрос через --question."
}
```

Покажи пользователю меню из **трёх вариантов** (НЕ задавай бинарный
вопрос «продолжить?» — это устаревшее поведение, агент должен дать выбор):

> Документ большой (~450 тыс. символов). Какой вариант анализа?
>
> • **Кратко** (~3–4 мин, ~250 слов) — прочитаю только ключевые разделы (введение, подписи, заголовки всех частей). Детали могу пропустить.
> • **Подробно** (~6–8 мин, ~800–1200 слов) — прочитаю весь документ, опишу каждый раздел простым языком.
> • Или **задайте конкретный вопрос** по документу — найду релевантные части и отвечу кратко (~3–4 мин).

> ⚠️ Показывай пользователю только label и время. **НЕ показывай**
> `chunks_total`, `context_batches_total`, `estimated_llm_calls`,
> `chunks_from_cache` и любые технические числа — это раздражает
> (инцидент 2026-08-28).

Дождись выбора пользователя, затем повтори вызов:

- «Кратко» →
  ```bash
  python .../cli.py --file big.pdf --length brief --confirm
  ```
- «Подробно» →
  ```bash
  python .../cli.py --file big.pdf --length detailed --confirm
  ```
- конкретный вопрос «что про штрафы?» →
  ```bash
  python .../cli.py --file big.pdf --question 'что про штрафы?' --confirm
  ```

Если пользователь не указал вариант явно (например, ответил «давай» без
уточнений) — по умолчанию `--length brief`.

**Протокол polling (важно — иначе ~14 лишних LLM-вызовов).**

**Запуск через `exec` — обязательно БЕЗ таймаута.** НЕ передавайте параметр
`timeout` в вызов `exec` (или передавайте `timeout=0`). Дефолтный таймаут
exec — 60 сек, и сессия будет убита ровно на 60-й секунде, ДО завершения
прогона и печати финального маркера. nanobot жёстко ограничивает per-call
таймаут значением `_MAX_TIMEOUT=600` сек, поэтому явный `timeout > 0`
тоже опасен для документов длиннее ~10 минут — просто не указывайте его,
чтобы сессия жила, пока процесс не завершится сам.

Сразу при старте cli.py печатает в stdout **первой строкой** маркер:

```json
{
  "mode": "summarize",
  "status": "running",
  "estimated_total_sec": 440,
  "poll_interval_hint_sec": 75,
  "done_marker": "__LEGAL_SUMMARIZER_DONE__",
  "hint": "Обработка займёт примерно 440 сек. НЕ опрашивайте по таймеру — это лишние LLM-вызовы. Дождитесь конца ОДНИМ блокирующим write_stdin: wait_for=\"__LEGAL_SUMMARIZER_DONE__\", wait_timeout_ms=120000 (максимум nanobot). Навык напечатает «__LEGAL_SUMMARIZER_DONE__» в stdout в самом конце (успех/ошибка/confirmation). Если вернулось «Wait target not observed» (прогон >120 сек), вызовите write_stdin ещё раз с тем же wait_for — вызовов будет минимум."
}
```

**Обязательно:** когда `exec`/`write_stdin` возвращает этот маркер —

1. НЕ опрашивайте по таймеру (каждый опрос = лишний LLM-вызов агента). Дождитесь завершения ОДНИМ блокирующим вызовом `write_stdin`.
2. Передайте `wait_for = done_marker` (строка `"__LEGAL_SUMMARIZER_DONE__"`) и `wait_timeout_ms = 120000` (жёсткий максимум nanobot). `yield_time_ms` оставьте дефолтным.
3. `write_stdin` вернётся, как только навык напечатает sentinel в stdout — это значит прогон завершён (успех/`partial`/`confirmation`/`error`). Финальный JSON-результат придёт в том же выводе сразу перед sentinel.
4. Если прогон дольше 120 сек, `write_stdin` вернётся с `Wait target not observed` (процесс ещё жив). Тогда вызовите `write_stdin` повторно с тем же `wait_for`/`wait_timeout_ms` — это даст минимум LLM-вызовов (по одному на каждые ~120 сек работы), а не по одному каждые 30 сек.
5. Progress-строки (`[legal_summarizer] batch cb_NNN ...`) идут в **stderr** и sentinel не содержат — `wait_for` на sentinel не сработает ложно.

Почему блокирующее ожидание, а не опрос: инцидент 2026-08-28 — агент опрашивал `write_stdin` каждые 30 сек (лимит `yield_time_ms ≤ 30000` в nanobot), что дало ~14 LLM-вызовов за 7 мин. `wait_for` на финальный sentinel + `wait_timeout_ms=120000` сокращает их до минимума (1 вызов на прогон ≤120 сек, +1 на каждые следующие 120 сек).

Skill выполнит ВСЕ батчи внутри одного вызова (без polling между
батчами внутри run()) и в конце вернёт:

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

## Follow-up запросы по сохранённой operation_id

После прогона результат (`status: completed`/`partial`) содержит
`operation_id` (`result.operation_id`) — это ключ к сохранённым данным
навыка (`data_store/cache/skills/legal_summarizer/<operation_id>/`):
manifest.json, result.json, chunks/*.json.

Для follow-up вопросов («сколько статей?», «какие разделы?», «что в чанке N?»)
**не перепарсивай PDF** через `exec`+pdfplumber — это долго (200+ сек) и
часто падает на кириллице в Windows-cp1251. Используй кастомный tool
`legal_summarizer_query`:

```python
legal_summarizer_query(operation_id="<op_id>", field="stats")           # базовые метрики
legal_summarizer_query(operation_id="<op_id>", field="articles")        # только article_count
legal_summarizer_query(operation_id="<op_id>", field="sections")        # список section_path
legal_summarizer_query(operation_id="<op_id>", field="chunks")          # чанки с summary
legal_summarizer_query(operation_id="<op_id>", field="tree")            # иерархия sections
legal_summarizer_query(operation_id="<op_id>", field="all")             # весь manifest
```

Поля `field`:

- `stats` — `chars_in`, `chunks_total`, `context_batches_total`, `article_count`,
  `duration_sec`, `started_at`, `completed_at`, `batches_done/failed`.
- `articles` — только `article_count` (число статей в документе, считается
  один раз по полному тексту при прогоне через regex `Статья\s+\d+(?:\.\d+)?`).
- `chunks` — массив с `chunk_id`, `section_id`, `section_path`, `page_start/end`,
  `summary` (обрезанное до `max_chunk_summary_chars`).
- `sections` — плоский список секций: `section_id`, `section_path`, `heading`.
- `tree` — то же, но отсортировано по `section_path` (псевдо-иерархия).
- `all` — полный manifest.json (для отладки).

Подробнее — `workspace/TOOLS.md` раздел «legal_summarizer_query».
Tool работает кросс-платформенно (Windows + Linux): `subprocess.run([...],
shell=False, encoding="utf-8")` + абсолютные пути. Кириллица в путях
работает, потому что на entry-points выставлены `PYTHONUTF8=1` и
`PYTHONIOENCODING=utf-8`.

**Что делать, если tool вернул `manifest_not_found`:** операция ещё не
завершилась (статус `running`) или прогон был удалён. Попроси пользователя
подождать или повторить `--confirm`.

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