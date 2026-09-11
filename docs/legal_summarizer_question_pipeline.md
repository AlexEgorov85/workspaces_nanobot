# legal_summarizer: переиспользование анализа между режимами

> **Статус:** проектный анализ, не «as-is». Описывает текущее поведение skill'а
> и предлагает варианты решения проблемы, изложенной в инциденте 2026-09-10:
> «skill вернул то же краткое саммари (`op_cb9363bcdcf4_06876f03_brief` уже
> completed, отдан кэш), а не целевой ответ на вопрос».
>
> **Аудитория:** мейнтейнеры `legal_summarizer`. После прочтения — выбрать
> вариант и перенести решения в `SKILL.md` / `docs/ARCHITECTURE.md`.

---

## 1. Текущее поведение (что мы имеем сейчас)

### 1.1. Какие режимы есть

| Режим CLI | Флаг | LLM-вызовов | Где живёт snapshot |
|---|---|---:|---|
| Краткое саммари | `--length brief` | 1 (`direct`) | **manifest** + `result.json` (`op_..._brief`); document-level cache **не создаётся** |
| Подробное саммари | `--length detailed` | N + reduce | manifest + `result.json` (`op_..._detailed`); **document-level cache создаётся** (через `run_canonical_pipeline`) |
| Ответ на вопрос | `--question "..."` | 1 (synthesize), если есть doc-cache; иначе full map-reduce | manifest + `result.json` (`op_..._q:...`); document-level cache используется, если уже есть |

Ключевой факт: **`brief` режим НЕ создаёт document-level cache** — он идёт через
`application.service._inspection_mod.inspect(...)` минуя `run_canonical_pipeline`.
Подробный и question режимы проходят через `run_canonical_pipeline`, который
снапшотит `physical` + `analysis` (структура + chunks + validation) в
`data_store/cache/sessions/<session_key>/documents/<document_id>/`.

### 1.2. Как считается `operation_id`

`scripts/application/operation_id.py:8-41`:

```python
text_blob = text.encode("utf-8", errors="replace")
h = hashlib.sha256(text_blob).hexdigest()[:12]
extras = f"\npath:{document_path or ''}\nlen:{length}\nq:{question or ''}"
extras_hash = hashlib.sha256(extras.encode("utf-8")).hexdigest()[:8]
return f"op_{h}_{extras_hash}_{length}"
```

`operation_id` детерминированно зависит от `(text, length, document_path, question)`.
То есть:

- `brief` + `detailed` для одного и того же файла → **разные `operation_id`** (разный `length` → разный `extras_hash`).
- `brief` без `--question` и `brief` с `--question` → **разные `operation_id`** (разный `q`).
- Один и тот же `(text, length, document_path, question)` → один и тот же `operation_id` → `service.run()` сразу отдаёт кэшированный `result.json` (`service.py:334-355`).

### 1.3. Как сейчас отрабатывается `--question`

`service.py:365-381` (перед основным pipeline):

```python
if (question is not None and document_path is not None and workspace_root is not None):
    cached_question_result = _try_question_via_document_cache(
        question=question, text=text, document_path=document_path,
        workspace_root=workspace_root, operation_id=operation_id,
        length=length, focus=focus, session_key=session_key,
    )
    if cached_question_result is not None:
        return cached_question_result
```

`_try_question_via_document_cache` (`service.py:77-294`):

1. Проверяет `is_document_cache_complete(document_id, workspace_root, session_key)`.
2. Если complete — читает snapshot, восстанавливает `DocumentAnalysis` in-memory.
3. Делает lexical retrieval через `select_chunks_for_mode` (выбирает ≤8 релевантных chunks).
4. Один LLM-вызов `llm_document_reduce` для синтеза ответа.
6. Сохраняет manifest + `result.json` под `operation_id`, выставляет
   `strategy="document_cache_question"`.

Если document-level cache отсутствует — `_try_question_via_document_cache` возвращает
`None`, и `service.run()` идёт на fallthrough в основной pipeline (`inspect()`,
`build_execution_context()`, ...). **Это самый дорогой путь** — он заново
прогоняет map-reduce (по сути делает работу, эквивалентную `detailed`).

### 1.4. Что произошло в инциденте 2026-09-10

1. Пользователь прислал ГК РФ, попросил «расскажи что в договоре».
2. Агент в чате вызвал skill **без `--question`** → `brief` режим.
3. Создался `operation_id=op_cb9363bcdcf4_06876f03_brief`, manifest + `result.json` с кратким саммари. Document-level cache **не создался** (`brief` идёт мимо `run_canonical_pipeline`).
4. Пользователь в чате: «а что гарантирует ГК РФ?».
5. Агент вызвал skill **без `--question`** (вопрос остался в чате, а не в CLI) → тот же `(text, length, path, question=None)` → тот же `operation_id` → `service.run()` отдал кэшированное `brief`-саммари.

**Корневая причина:** skill не знает о вопросе. Кэш работает корректно по
контракту; контракт неполон — он не покрывает «вопрос пришёл из чата».

### 1.5. Что внутри brief chunk'а (важно для понимания дальнейших вариантов)

`brief` НЕ создаёт обычных chunks через `ChunkPlanner.plan`. Вместо этого
`BriefContextBuilder.build_brief_chunk` (`scripts/application/brief_context.py:349-438`)
собирает **один синтетический** `Chunk` с `chunk_id="001"`:

```text
DOCUMENT STRUCTURE
<рекурсивный outline всех значимых structural nodes, до structure_max_chars>

DOCUMENT CONTENT
[Preamble]
<preamble blocks>

[<Section heading>]
<все physical blocks subtree в document order>

...

[BRIEF: section content truncated]   ← для каждой усечённой секции
```

**Критичные свойства:**

| Свойство | Значение |
|---|---|
| `chunk_id` | `"001"` (фиксированный, не из `ChunkPlanner`) |
| Размер | `char_count <= max_chars` (по умолчанию 30000; динамически от `contextWindowTokens * input_ratio * chars_per_token`) |
| `analysis.chunks` (canonical) | **НЕ используется** для построения brief chunk'а |
| Document-level cache (snapshot) | **НЕ создаётся** в `brief` режиме |
| Секции | Источник — `DocumentStructure` + `PhysicalDocument`. Секции **могут быть усечены** по budget → в конце усечённой секции маркер `[BRIEF: section content truncated]` |
| Таблицы | Передаются атомарно (на уровне блока, не строки) |

Это значит:

- **Brief chunk ≠ canonical chunk.** У него другой `chunk_id` (`"001"` против
  плановых `"001"`, `"002"`, ...), другой формат текста, другие метаданные.
- **Brief chunk нельзя использовать для question synthesis напрямую.** даже
  если бы он лежал в document-level cache, его формат — outline + усечённые
  секции, а `build_question_context` ожидает canonical chunks + per-section
  / per-chunk summaries.
- **Brief chunk может НЕ содержать ответа на вопрос.** Outline структуры +
  preamble + первый уровень секций (часто с маркерами truncation) — для
  длинного ГК РФ (236 секций, ~300к символов) это **малая доля** документа.
  Вопрос «что гарантирует ГК РФ» может относиться к секции, которая не
  вошла в budget → маркер `[BRIEF: section content truncated]`.

---

## 2. Варианты решения

### Вариант A: «Skill понимает вопрос из контекста чата»

#### A.1 — Эвристика по последнему user-message

В `cli.py` или в `service.run()` добавить: если `args.question is None` и
передан `--context` (JSON-список сообщений), взять последнее `role="user"`
сообщение и, если оно выглядит как вопрос (есть `?` или начинается с
вопросительного слова), использовать его как `question`.

- **Плюсы:** минимальные изменения в pipeline; не требует менять поведение агента.
- **Минусы:** хрупкая эвристика, false positives («объясни статью 123» — это вопрос или просьба?). Пользователь может спрашивать в чате **не про документ** (например, «а ты кто?»), а skill решит, что это вопрос к документу, и сделает дорогой прогон.
- **Минусы:** контракт «question нужно передавать явно» размывается.
- **Когда уместно:** если skill используется **только** нано-агентом в чате, а LLM-агент **не способен** надёжно передавать `--question`.

#### A.2 — Агент всегда передаёт `--question` явно

Перенести ответственность на агента: при любом вопросе пользователя к документу
агент обязан вызвать skill с `--question "<перефразированный вопрос>"`. SKILL.md
уже говорит об этом, но не настаивает.

- **Плюсы:** строгий контракт, skill остаётся stateless, никаких эвристик.
- **Плюсы:** естественная интеграция с документ-кешем (повторный вопрос — кэш question mode → 1 LLM-вызов).
- **Минусы:** требует доработки агента (промпт или поведение); если агент забудет — мы вернёмся к инциденту.
- **Минусы:** нужен аудит всех мест вызова skill'а в агенте.
- **Когда уместно:** если хотим чистый контракт и готовы вложиться в обучение агента.

---

### Вариант B: «Один document-level cache для всех режимов»

#### B.1 — `brief` тоже пишет document-level cache

Сейчас `brief` идёт через `_inspection_mod.inspect()` (без snapshot). Сделать
так, чтобы `brief` тоже вызывал `run_canonical_pipeline` (или эквивалент),
который пишет snapshot. Snapshot создаёт `physical` + `analysis` (структура
+ chunks + validation) — это **не зависит от `length`**. Brief саммари —
это уже **поверх** snapshot (через `BriefContextBuilder`).

- **Плюсы:** повторный `--question` для документа, по которому делали `brief`,
  автоматически пойдёт через `_try_question_via_document_cache` (1 LLM-вызов).
- **Плюсы:** `brief` + `detailed` для одного файла — `detailed` тоже найдёт
  готовый snapshot и не будет заново парсить PDF/DOCX.
- **Минусы:** `brief` теперь делает inspect/chunking (один раз, не LLM). Это
  уже происходит в `_inspection_mod.inspect()` — то есть overhead только в
  записи snapshot на диск (~миллисекунды).
- **Минусы:** меняется место, где живёт `DocumentAnalysis` для `brief` —
  раньше `inspect()` отдавал свой объект, теперь `run_canonical_pipeline`.
  Нужно проверить, что downstream-потребители не сломаются.
- **Когда уместно:** это базовая инфраструктурная починка — без неё
  document-cache question mode работает только после `detailed`.

#### B.2 — Хранить все результаты (brief/detailed/question) в одном `result.json`

Перейти от «один `operation_id` → один `result.json`» к «один
`(text, document_path)` → один `result.json` с полями `brief`, `detailed`,
`questions[]`».

- **Плюсы:** один файл на документ — проще отлаживать, видно все три режима.
- **Плюсы:** не плодятся папки `op_..._brief` и `op_..._question`.
- **Плюсы:** пользователь в одном месте видит «вот моё краткое саммари, вот
  подробное, а вот ответы на вопросы».
- **Плюсы:** переиспользование analysis между режимами становится очевидным
  (один manifest = один document-cache).
- **Минусы:** меняется схема `result.json` — нужно мигрировать существующие
  манифесты или поддерживать обратную совместимость.
- **Минусы:** `operation_id` теряет смысл «уникального идентификатора
  операции». Нужно ввести `document_id` как первичный ключ и хранить список
  операций.
- **Минусы:** idempotency-check (`if existing.status == "completed": return cached`)
  становится сложнее: «если brief уже есть — вернуть его; если вопрос
  уже задавался — вернуть его; иначе запустить pipeline».
- **Когда уместно:** если хотим радикально упростить модель и готовы к
  миграции схемы.

#### B.3 — `document_id` как ключ кэша, `operation_id` только для трейсинга

Сохранить отдельные `operation_id` для каждого прогона (как сейчас), но:

- Каждый прогон (brief/detailed/question) **всегда** пишет document-level cache (B.1).
- Idempotency-check работает не только по `operation_id`, но и по
  `(document_id, mode, question_hash)` — чтобы «тот же вопрос» = кэш,
  «похожий вопрос» = новый прогон через retrieval.

- **Плюсы:** меньше инвазивно, чем B.2 — `operation_id` остаётся первичным ключом.
- **Плюсы:** поддерживает «повторный вопрос = кэш, новый вопрос = 1 LLM».
- **Минусы:** второй ключ кэширования добавляет сложность в idempotency-check.
- **Когда уместно:** компромисс между B.1 и B.2.

---

### Вариант C: «Не делать дорогой прогон, если вопрос можно ответить без LLM»

#### C.1 — Сначала попробовать retrieval без LLM

В `_try_question_via_document_cache`, **даже если document-level cache нет**:

1. Сделать `inspect()` (чанкинг, без LLM).
2. `select_chunks_for_mode(question)` — выбрать релевантные chunks.
3. Попробовать сгенерировать ответ **без LLM**: извлечь цитаты из chunks,
   показать их пользователю с пометкой «это фрагменты, без синтеза».
4. Если LLM-синтез всё-таки нужен — один вызов `llm_document_reduce`.

- **Плюсы:** дёшево (без LLM), прозрачно (показываем реальные цитаты).
- **Минусы:** меняет UX — пользователь получает «сырой» ответ.
- **Плюсы:** не плодит manifest без результата.
- **Когда уместно:** если вопрос можно свести к «найди релевантное место
  в документе и процитируй».

#### C.2 — Только `extract` (подсветка релевантных chunks)

Вместо синтеза нового ответа — показать пользователю «вот эти 3 chunks
релевантны на 87% / 64% / 51% — посмотри статью N».

- **Плюсы:** 0 LLM-вызовов на вопрос.
- **Минусы:** UX хуже, чем LLM-синтез.
- **Когда уместно:** для документов, где пользователь сам готов читать.

---

## 3. Сводная таблица вариантов

| Вариант | Меняет pipeline | LLM-вызовов на «вопрос после brief» | False positives | Сложность | Обратная совместимость |
|---|---|---:|---|---|---|
| A.1 — эвристика из чата | нет | 0 (кэш) или full (fallthrough) | высокий риск | низкая | полная |
| A.2 — агент передаёт явно | нет | 0 (кэш) или full | нет | средняя (нужен аудит агента) | полная |
| **B.1** — brief пишет doc-cache | **да** (минимально) | 1 (через doc-cache) | нет | низкая | полная |
| B.2 — единый result.json | сильно | 1 (через doc-cache) | нет | высокая | нужна миграция |
| B.3 — двойной ключ кэша | сильно | 1 (через doc-cache) | нет | средняя | полная |
| C.1 — retrieval без LLM | да | 0 | нет | средняя | полная |
| C.2 — только подсветка | да | 0 | нет | низкая | полная |

---

## 4. Что рекомендуется (для последующего обсуждения)

### Минимальный фикс (если нужно быстро): A.2 + документация

- Дописать в `SKILL.md` правило: «при любом вопросе пользователя по
  загруженному документу агент ОБЯЗАН вызывать skill с `--question "<вопрос>"`,
  а не надеяться, что skill поймёт из чата».
- Проверить, что opencode-агент действительно это делает (см. историю
  вызовов в `agent_gateway_logs`).

### Правильный фикс: B.1 + A.2

1. **`brief` режим тоже создаёт document-level cache** (вариант B.1).
   Минимальные изменения: после `_inspection_mod.inspect(...)` в `service.run()`
   вызвать `run_canonical_pipeline(...)` (или сразу использовать
   `run_canonical_pipeline` для `brief` тоже) — он сам запишет snapshot
   через `_write_document_snapshot_after_pipeline`.
2. **Агент передаёт `--question` явно** (вариант A.2).
3. **`_try_question_via_document_cache`** уже работает (написан); его нужно
   только проверить, что он попадает в happy-path теперь, когда `brief`
   тоже создаёт snapshot.

### Долгосрочно: B.2 (единый result.json)

Если нужно радикально упростить — перейти на схему «один документ = один
`result.json` с полями `brief` / `detailed` / `questions[]`». Это можно
сделать второй итерацией, после стабилизации B.1.

---

## 5. Сценарии, которые нужно покрыть тестами (после выбора варианта)

| Сценарий | Ожидаемое поведение |
|---|---|
| `brief` → `--question "..."` (тот же файл) | 1 LLM-вызов через doc-cache, новый `operation_id` |
| `brief` → `detailed` (тот же файл) | doc-cache hit на inspect, новый `operation_id` для detailed |
| `detailed` → `--question "..."` (тот же файл) | 1 LLM-вызов через doc-cache |
| `--question "A"` → `--question "B"` (тот же файл) | 2 разных `operation_id`, оба через doc-cache |
| `--question "A"` → `--question "A"` (тот же файл) | кэш, 0 LLM-вызовов |
| `brief` (file mtime changed) → `--question "..."` | snapshot инвалидирован, fallthrough на полный pipeline |
| `brief` (no document_path / no workspace_root) → `--question "..."` | fallthrough на полный pipeline |

---

## 6. Критичный кейс: «был только brief, потом вопрос»

Это **самый частый** реальный сценарий (см. инцидент 2026-09-10) и самый
коварный. Разберём отдельно.

### 6.1. Что сейчас происходит

1. Пользователь делает `brief` режим → создаётся `op_..._brief` (manifest + result).
   Document-level cache **не создаётся**.
2. Пользователь задаёт `--question "..."` → а) `--question` доходит до
   `make_operation_id` → новый `operation_id`; б) `_try_question_via_document_cache`
   сразу видит `is_document_cache_complete == False` → возвращает `None`;
   в) `service.run()` идёт на fallthrough в основной pipeline.
3. Fallthrough делает `inspect()` (`_inspection_mod.inspect`) — НЕ
   `run_canonical_pipeline`. Это значит:
   - **Никакого document-level snapshot'а не создаётся даже сейчас.**
   - Создаётся новый manifest под `op_..._q:..._brief` (с `length=brief`! —
     потому что `--length` не передан, берётся дефолт).
   - Идёт обычный map-reduce: `select_chunks_for_mode(question=...)` →
     retrieval → ≤8 релевантных chunks → один `llm_document_reduce` для синтеза.

На первый взгляд это работает: question синтезируется из canonical chunks
через retrieval. Но есть тонкости.

### 6.2. Тонкость #1: `inspect()` vs `run_canonical_pipeline`

Сейчас fallthrough в `service.run()` использует `inspect()`, который создаёт
**только `Inspection` объект** в памяти (chunks + structure + analysis),
**но не пишет snapshot на диск**. Значит:

- Следующий `--question` для того же документа снова увидит
  `is_document_cache_complete == False` → снова fallthrough → снова
  `inspect()` в памяти → снова без snapshot'а.
- То есть **document-level cache question shortcut принципиально недостижим**
  для пользователей, которые начинают с `brief`.

**Чтобы shortcut заработал, нужно при первом прогоне любого режима**
(brief / detailed / question) **записать document-level snapshot.** То есть
`inspect()` нужно либо заменить на `run_canonical_pipeline` для всех режимов,
либо после `inspect()` явно вызвать `_write_document_snapshot_after_pipeline`.

### 6.3. Тонкость #2: brief chunk НЕ равен canonical chunks

Даже если бы мы сохранили brief chunk в document-level cache, его **нельзя
использовать как источник для question synthesis**:

| Что ожидает `build_question_context` | Что предоставляет brief chunk |
|---|---|
| Canonical chunks с `chunk_id` из `ChunkPlanner` (плановые id, по структуре документа) | Один синтетический `Chunk` с `chunk_id="001"`, формат outline + усечённые секции |
| Per-section summary (level 1 cache) | Отсутствует — brief не делает LLM-summary секций |
| Per-chunk summary (level 2 cache) | Отсутствует — brief не делает LLM-summary chunks |
| `Chunk.text` (level 3 cache, lossless fallback) | Усечённый текст + маркеры `[BRIEF: section content truncated]` |

**Вывод:** даже при наличии `result.json` с brief-саммари, этот саммари —
это **не источник данных для question**, а **продукт**. Сохранять нужно
**canonical chunks + structure + physical**, а не brief chunk.

### 6.4. Тонкость #3: «а если brief chunk содержит ответ?»

Часто можно услышать аргумент: «brief chunk большой (30000 символов),
почти весь документ там умещается — почему не использовать его для question?».

Короткий ответ: **потому что brief chunk не lossless**.

- Для ГК РФ (300к символов, 236 секций) `max_chars ≈ 30000` — это **~10%** документа.
- `BriefContextBuilder` распределяет budget между секциями через `allocate_budget`,
  секции сжимаются (truncation) — усечённая секция получает маркер
  `[BRIEF: section content truncated]`.
- Outline (DOCUMENT STRUCTURE) даёт **только заголовки**, не содержание.
- Preamble + первый уровень секций (top_level) — это обычно введение и
  оглавление; конкретный вопрос («что гарантирует ГК РФ») может относиться
  к статье из секции 2-го или 3-го уровня, которая **не вошла** в budget.

Если попробовать «скормить» его LLM вместе с вопросом, получим:

1. LLM ответит по **усечённому контексту** — может придумать или быть неточным.
2. Никаких гарантий полноты — пользователь не знает, что часть документа не попала.
3. Нарушается принцип lossless: «source text никогда не опускаем» (см. `question_context.py:21-23`).

**Правильный подход:** использовать **canonical chunks** (полные секции/части
документа, найденные retrieval'ом) + summaries из document-level cache.

### 6.5. Что делать: варианты для кейса «brief → question»

#### Вариант 6.A — Принудительно создавать document-level snapshot всегда

При первом прогоне (любом — brief / detailed / question) записать snapshot.
Реализация:

1. В `service.run()` после `_inspection_mod.inspect(...)` (line ~388) или
   вместо него вызвать `run_canonical_pipeline(...)`.
2. `run_canonical_pipeline` уже сам вызывает
   `_write_document_snapshot_after_pipeline` (см. `pipeline_structure.py:330`).
3. Дальше `_try_question_via_document_cache` начинает работать для всех
   режимов, потому что snapshot есть.

- **Плюсы:** одна точка изменения, snapshot создаётся всегда.
- **Плюсы:** `brief` pipeline получает `DocumentAnalysis` с правильными
  canonical chunks, что упрощает `select_chunks_for_mode` (там сейчас
  `brief` ветка строит synthetic chunk — это исключение).
- **Минусы:** `brief` режим начинает делать `ChunkPlanner.plan(...)` (один
  раз, без LLM). Это уже происходит в `inspect()` через
  `run_canonical_pipeline`-подобный путь — overhead минимальный.
- **Минусы:** нужно проверить, что downstream (`_try_question_via_document_cache`,
  `build_execution_context`, `select_chunks_for_mode` для `length=brief`)
  корректно работает с `DocumentAnalysis` от `run_canonical_pipeline`.

#### Вариант 6.B — Для `brief → question` сразу идти на полный pipeline

Если snapshot'а нет (а после `brief` его нет) — не делать «дешёвый» shortcut,
а сразу делать полный `inspect()` + map-reduce (как сейчас в fallthrough).
То есть **оставить как есть**, но явно зафиксировать в `SKILL.md`:
«после `brief` повторный `--question` всегда стоит N LLM-вызовов (full pipeline)».

- **Плюсы:** ноль изменений в коде.
- **Плюсы:** пользователь знает цену — это прозрачно.
- **Минусы:** каждый вопрос после brief — это N LLM-вызовов, что дорого.
- **Минусы:** нет накопления анализа между сессиями.

#### Вариант 6.C — Brief всегда запускает canonical pipeline + summary

Радикальный: `brief` режим = `run_canonical_pipeline` (который пишет snapshot)
+ `llm_chunk_summary` для каждого chunk + `llm_section_summary` для каждой
секции (через map). То есть brief **не** делает `BriefContextBuilder`, а
использует canonical chunks + summaries.

- **Плюсы:** `brief → question` = 1 LLM-вызов (через document-cache question mode).
- **Плюсы:** данные переиспользуются на 100%.
- **Минусы:** `brief` становится **дороже** (несколько LLM-вызовов вместо одного), потому что нужно посчитать chunk summaries.
- **Минусы:** ломает существующий контракт «brief = 1 LLM-вызов, 150-250 слов».

### 6.6. Сводка по «brief → question»

| Вариант | Стоимость первого brief | Стоимость первого question после brief | Количество кода |
|---|---:|---:|---:|
| 6.A — snapshot всегда | 1 LLM | 1 LLM (через doc-cache) | ~20 строк (заменить `inspect()` на `run_canonical_pipeline` + проверить downstream) |
| 6.B — оставить как есть | 1 LLM | N LLM (fallthrough) | 0 строк |
| 6.C — brief через canonical | N+1 LLM | 1 LLM (через doc-cache) | >100 строк |

**Рекомендация для этого кейса — 6.A.** Это базовая инфраструктурная починка:
snapshot создаётся один раз для документа, дальше всё переиспользует.

### 6.7. Что произойдёт, если после brief → question ответ неполный

Допустим, реализовали 6.A. Тогда:

1. `brief` создал `result.json` с кратким саммари + создал snapshot
   (canonical chunks + structure + physical).
2. `--question "что гарантирует"` → `_try_question_via_document_cache`:
   - snapshot complete → идём дальше.
   - `select_chunks_for_mode(question=...)` → `insp.analysis.retrieve(question, max_results=8)` → ≤8 релевантных chunks.
   - Если retrieval **не нашёл ничего** (вопрос про что-то, что не попало в
     retrieval index из-за стоп-слов или слишком общей формулировки) →
     `relaxed_lexical_fallback` (поиск по префиксам слов длиной ≥4).
   - Если и fallback не нашёл → `bounded top-of-document fallback` —
     `insp.chunks[:question_fallback_max_chunks]` (по умолчанию 16, берётся
     из конфига `execution.question_fallback_max_chunks`).
   - **Гарантия полноты:** fallback берёт chunks с начала документа, а не
     с конца. Это значит, что для «вопросов про начало» ответ полный, для
     «вопросов про середину/конец» — может быть неполный.
3. `build_question_context` собирает LLM-вход из выбранных chunks:
   - Level 1 (section summary) — если есть.
   - Level 2 (chunk summary) — если есть.
   - Level 3 (chunk.text, source) — **всегда** (lossless fallback).
   - Если превышен `DOCUMENT_REDUCE_INPUT_BUDGET_CHARS` — обрезаются
     source text'ы до равной доли, **summaries не обрезаются**.
4. Один LLM-вызов `llm_document_reduce` для синтеза.
5. Результат сохраняется в manifest + `result.json` под
   `operation_id=op_..._q:что гарантирует_brief`.

**Если ответ неполный** (например, «что гарантирует» — а в документе это
рассредоточено по 50 местам, retrieval выбрал 8 chunks, но LLM не увидел
статью N):

- Пользователь может **переформулировать** вопрос → новый `operation_id` →
  новый retrieval → может найти другие chunks.
- Пользователь может **сделать `detailed`** → `_try_question_via_document_cache`
  skip (потому что уже есть snapshot) → `inspect()` → `select_chunks_for_mode`
  для detailed = **все chunks** → map-reduce с полным покрытием → ответ
  точно полный (но дорогой).
- **Нет автоматического механизма** «если retrieval пропустил, расширить
  до всех chunks». Это разумный fallback для follow-up вопроса, если
  пользователь подозревает, что ответ неполный.

**Открытый вопрос:** стоит ли добавить режим `--question "..." --question-fallback=detailed`,
который при пустом retrieval / слишком общих формулировках сразу идёт на
full detailed pipeline? Или пользователь должен сам это сделать через два
вызова (brief → detailed)?

---

## 7. Открытые вопросы (для решения вместе)

1. **Brief-сжатие при создании document-level cache:** хранить ли в snapshot
   **все** chunks, или только те, что вошли в brief (т.к. brief может сжимать
   секции через `brief_compression`)?
   Предположение: snapshot хранит исходные chunks, без сжатия — это
   document-level cache, не зависящий от режима.

2. **session_key в document-level cache:** сейчас snapshot живёт под
   `data_store/cache/sessions/<session_key>/documents/<document_id>/`. То есть
   у каждой сессии свой кэш для одного и того же файла. Это правильно?
   Аргумент «за»: изоляция сессий, нет утечки данных между чатами.
   Аргумент «против»: если файл загружали в 5 чатах — будет 5 копий snapshot.

3. **Срок жизни document-level cache:** сейчас он не очищается. Стоит ли
   привязать его к mtime файла (snapshot удаляется, если файл перезаписан)?
   `DocumentIdentity.is_fresh` уже это делает через `invalidate_document_cache`.

4. **Сколько раз имеет смысл делать разные режимы для одного файла?**
   Если пользователь задал 50 вопросов — это 50 `operation_id`, 50
   манифестов, 50 `result.json`. Не разрастается ли это? Может быть,
   `questions[]` хранить как массив в одном `result.json` (вариант B.2)?