---
name: legal_summarizer
description: Юридический анализ PDF/DOCX/TXT — вызывай ТОЛЬКО через `python skills/legal_summarizer/scripts/cli.py --file <path>`. `office_files.extract_metadata()` (раньше `summarize()`) — это НЕ саммари, а метаданные; не подменяй cli.
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
- Документ защищён паролем или является сканом без текстового слоя — skill
  вернёт ошибку «документ не содержит извлекаемого текста»; тогда попроси
  текстовую версию у пользователя.

## Запуск

CLI вызывается агентом напрямую через интерпретатор (без отдельных
`.bat`/`.sh`-обёрток — это антипаттерн проекта):

```bash
python workspace/skills/legal_summarizer/scripts/cli.py --file <path> --length brief|medium|detailed
```

Либо относительный путь из `workspace/`:

```bash
python skills/legal_summarizer/scripts/cli.py --file <path>
```

| Параметр | Обязательный | Описание |
|:---|:---:|:---|
| `--file` | да | Путь к документу: `.pdf`, `.docx`, `.txt` и др. форматы `office_files`. **Абсолютный** путь ИЛИ относительный от корня проекта с полным префиксом (см. ниже). |
| `--length` | нет | `brief` (150–250 слов), `medium` (400–600, по умолч.), `detailed` (800–1200) |
| `--context` | нет | История чата в JSON (для учёта фокуса пользователя) |
| `--max-chunks` | нет | Жёсткий лимит чанков для map-reduce. **По умолчанию `50`** — покрывает большинство документов (ГК РФ ≈ 20 чанков, обычные договоры 1-3). При превышении skill возвращает status=error с конкретной рекомендацией перейти на streaming. |
| `--batch-size` | нет | Включает streaming-режим: обработать за один вызов только указанное число чанков, вернуть partial-саммари + метаданные для resume. Используйте для больших документов (>50 чанков) — например, `--batch-size 5` для ГК РФ. |
| `--batch-index` | нет | Номер батча для resume (0 = сначала). Используется вместе с `--batch-size`. |

**Как передавать `--file`** (важно — `cli.py` не делает redirect сам):

- ✅ **Абсолютный путь:** `C:\Users\<user>\.nanobot\workspace\data_store\cache\sessions\<session_key>\<file>.pdf`
- ✅ **Относительный от корня проекта** (cwd=workspace-корень): `data_store/cache/sessions/<session_key>/<file>.pdf`
- ❌ Только имя файла `<file>.pdf` без префикса — будет `Файл не найден`,
  потому что в cwd такого файла нет. `SessionFileRedirectHook` работает
  только для `write_file`/`edit`, не для произвольных `exec`-команд.

Если не знаешь session_key — найди файл по basename в `data_store/cache/sessions/` и подставь полный относительный путь.

**Подсказка:** при прикладывании файла через канал агент получает полный
путь в маркере `[File path: ...]` рядом с `[File: <basename>]` (см.
`RuntimePatcher.patch_media_attachment_marker`). Бери путь прямо оттуда —
не перебирай каталоги через `find_files`/`Get-ChildItem`.

Для **websocket-канала** файлы лежат в `~/.nanobot/media/websocket/`
(не в `data_store/cache/sessions/`) — путь всё равно приходит полным,
абсолютным, прямо в `[File path: ...]`.

## Что вернёт skill

```json
{
  "mode": "summarize",
  "status": "success",
  "subject": "Это договор аренды: ...",
  "summary": "...",
  "length": "medium",
  "chars_in": 45230,
  "chunks": 5,
  "strategy": "map_reduce"
}
```

`subject` — одно предложение «о чём документ» (первая строка саммари).
`strategy` — `single` или `map_reduce`.

Дальше: покажи пользователю `subject` + `summary` как свой ответ. Не
переписывай `subject` без необходимости — пользователь ждёт именно
«о чём документ».

## Длинные документы

Тексты длиннее `single_call_threshold` (по умолч. 20 000 символов)
обрабатываются map-reduce: разбиение на чанки → саммари каждого →
объединение. Пороги — в `project.json` → `skills.legal_summarizer.chunking`.

**Оценка времени:**

- 1 чанк → одна LLM-коммуникация (~5–60 сек, зависит от провайдера).
- Каждые 5 чанков skill пишет progress в stderr
  (`[legal_summarizer] chunk N/M (XX%)`), чтобы агент видел, что skill
  не завис.
- LLM-клиент (`lib.services.llm_client`) уже имеет встроенный retry
  с exponential backoff — искусственного sleep между чанками нет.

**Что делать с очень большими документами** (>1 МБ текста после
`extract_text`, например ГК РФ, кодексы):

- По умолчанию `--max-chunks 50` покрывает ГК РФ целиком (≈20 чанков).
  Skill доработает за один вызов, но это ~9 минут чистого LLM-времени.
- Если хочется **получить ответ агента раньше** — используйте
  `--batch-size 5 --batch-index 0`. Skill обработает 5 чанков, вернёт
  partial_summary и метаданные для следующего вызова. См. секцию
  «Streaming-режим» ниже.
- Если chunks_total оказалось больше 50 (например, свод законов) — skill
  вернёт status=error с рекомендацией batch-size. Не молчит, не висит.

## Streaming-режим (batch)

Для очень больших документов, когда `map-reduce` целиком занимает десятки
минут, агент может обрабатывать документ порциями:

```bash
# Батч 0: первые 3 чанка
python ...cli.py --file doc.pdf --length brief --batch-size 3 --batch-index 0

# Батч 1: следующие 3 чанка (с передачей partial в --context)
python ...cli.py --file doc.pdf --length brief --batch-size 3 --batch-index 1 \
        --context "$(cat partial.json)"
```

Каждый батч возвращает:

```json
{
  "status": "partial" | "complete",
  "data": {
    "partial_summary": "...",
    "chunks_in_batch": 3,
    ...
  },
  "stream": {
    "chunks_total": 33,
    "chunks_done": 3,
    "next_batch_index": 1,
    "next_resume_hint": "передайте --batch-index 1 для продолжения"
  }
}
```

`status=partial` означает — есть следующий батч; `status=complete` —
последний, саммари финальное.

Агент сам решает: остановиться после частичного результата
(если ответ уже достаточен), продолжить со следующего батча, или
завершить сессию.

## Что внутри

- Извлечение текста: `workspace.utils.office_files.extract_text` (общий
  слой для всех офисных форматов). Skill использует его ВНУТРИ себя,
  агенту вызывать `extract_text()` напрямую **не нужно**.
- Чанкинг: `lib.services.text_splitter.split_text`.
- LLM-клиент: `lib.services.llm_client.call_llm` (с ретраями).
- Промпты: `workspace/skills/legal_summarizer/prompts/summarize_system.md`,
  `reduce_system.md`.

## Что не делать

- ❌ **НЕ вызывать `workspace.utils.office_files.extract_metadata()`**
  (раньше называлось `summarize()`) — она не делает саммари
  (см. ПРАВИЛО #1 в начале). Если встретишь код или старые инструкции,
  где упоминается `office_files.summarize()`, знай: это устаревшее имя
  той же `extract_metadata()`; результат всё равно метаданные, не саммари.
- ❌ Не вызывать `extract_text()` и не пытаться самому делать саммари
  из извлечённого текста — skill делает это аккуратно с правильными
  промптами и chunking'ом, ты потратишь больше токенов и качество будет хуже.
- ❌ Не вызывать skill для не-юридических документов — обычный пересказ
  пиши сам.
- ❌ Не редактировать `subject` от LLM без необходимости — пользователь
  ждёт именно «о чём документ».
- ❌ Не подставлять пользовательский текст в `system` промпт — это ломает
  формат саммари.
