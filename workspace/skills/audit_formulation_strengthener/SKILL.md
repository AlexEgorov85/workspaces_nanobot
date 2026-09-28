---
name: audit_formulation_strengthener
description: Усиление формулировки отклонения по ВНД — вызывай ТОЛЬКО через `python workspace/skills/audit_formulation_strengthener/scripts/cli.py`. Принимает текст отклонения + один/несколько файлов ВНД (.pdf/.docx/.txt), возвращает человекочитаемый отчёт (.md/.docx/.txt) в строгом русском юридическом стиле: нормализация формулировки, релевантные пункты/абзацы/смысловые куски из ВНД с пояснением «почему соотносится с отклонением», вердикт о достаточности формулировки и рекомендация по усилению.
metadata: {"nanobot":{"emoji":"⚖️","always":true}}
---

# Audit Formulation Strengthener — единственный путь: `cli.py`

> ⚠️ **ПРАВИЛО (нарушать нельзя):** анализ формулировки выполняет
> ТОЛЬКО `python workspace/skills/audit_formulation_strengthener/scripts/cli.py`.
> Никаких прямых вызовов `workspace.utils.office_files.extract_text()`,
> `lib.services.text_splitter.split_text` или самостоятельного
> LLM-анализа — skill единственный владелец pipeline.

## Когда вызывать

- Аудитор сформулировал отклонение/нарушение и просит:
  - «проверь формулировку по ВНД»,
  - «найди релевантные пункты ВНД»,
  - «усиль формулировку отклонения»,
  - «есть ли в ВНД основания для этого нарушения».
- В наличии один или несколько файлов ВНД (`.pdf` / `.docx` / `.txt`).

Когда **не** вызывать:

- ВНД отсутствуют — попроси у аудитора файлы.
- Файл ВНД защищён паролём или является сканом без текстового слоя — skill вернёт ошибку `vnd_unreadable` или `vnd_empty`; попроси текстовую версию.
- Файл не офисный (изображение, архив, бинарник) — это не задача skill'а.
- Задача — саммари одного документа без привязки к отклонению (это `legal_summarizer`).
- Задача — анализ данных аудита из БД (это `audit_analyzer`).

## Запуск

> ℹ️ PYTHONPATH выставлять **не нужно** — `cli.py` сам подкладывает
> корень репо в `sys.path` (единственная точка модификации в скилле).

### Каноническая команда (полный пайплайн)

```bash
python workspace/skills/audit_formulation_strengthener/scripts/cli.py \
    --violation "Срок хранения персональных данных установлен 1 год" \
    --vnd vnd1.pdf --vnd vnd2.docx \
    --output report.md
```

### Флаги

- **`--violation`** — обязательный, текст отклонения в кавычках.
- **`--vnd`** — повторяемый аргумент; для `search`/`synthesize`/`all` нужен хотя бы один файл `.pdf`/`.docx`/`.txt`. Пустой `--vnd` → JSON `no_vnd` в stdout + exit 2.
- **`--mode analyze|search|synthesize|all`** — режим (по умолчанию `all`).
- **`--output-format md|docx|txt`** — формат файла отчёта при `--output` (по умолчанию `md`).
- **`--internal-format json|report`** — формат вывода для `synthesize` (по умолчанию `report`). Для `analyze`/`search` всегда JSON.
- **`--output`** — путь к итоговому файлу. Если не указан — stdout. **Игнорируется вместе с `--estimate-only`** (нет артефакта — нет файла).
- **`--estimate-only`** — только оценка без LLM. Работает во всех режимах. Для `all` — форма `synthesize-estimate + vnd_count` (`1+N+1` вызовов). Exit 0, JSON в stdout.
- **`--analyze-result /path/to/analyze.json`** / **`--search-result`** — резюмировать финальный отчёт из кэшированных результатов предыдущих фаз.

Полный список — `cli.py --help`. JSON-контракты всех режимов — `references/contracts.md`.

## Протокол

### Короткое ВНД

```bash
python .../cli.py --violation "..." --vnd short.pdf
```

Skill выполняет все три фазы (`analyze` → `search` → `synthesize`)
внутри одного запуска. В stdout (или файл `--output`) — готовый
человекочитаемый отчёт `.md`.

### Оценка размера перед запуском

```bash
python .../cli.py --mode all --estimate-only --violation "..." --vnd file.pdf
```

Возвращает JSON с `size_estimate` (files / chunks_total / chunks_per_file /
chars_total) и `llm_calls_planned: 1+N+1`. **Без LLM-вызовов** — 0 сетевого
трафика. Полезно показать аудитору «это будет стоить примерно столько-то».

### Resume (прерванный прогон)

```bash
python .../cli.py --mode synthesize \
    --violation "..." --vnd vnd1.pdf \
    --analyze-result /tmp/analyze.json \
    --search-result /tmp/search.json
```

Resume читает кэшированные JSON; не требует повторных LLM-вызовов
для уже выполненных фаз.

## Структура финального отчёта

Отчёт в `.md` (или `.txt` / `.docx`) содержит 6 разделов:

1. **Краткое изложение отклонения** — нормализованная формулировка.
2. **Установленные факты (по ВНД)** — факты, извлечённые LLM из релевантных фрагментов.
3. **Анализ отклонения** — логический разбор.
4. **Релевантные фрагменты ВНД (валидированные цитаты)** — каждая цитата
   проверена против реестра находок (id из реестра, excerpt — точная
   подстрока, relation_type из находки). Отброшенные цитаты отражены в
   строке «Отклонено проверкой: N цитат не прошли сверку с источником».
5. **Итоговая классификация** — категория (`высокая`/`средняя`/`низкая`) и обоснование.
6. **Рекомендуемая усиленная формулировка** — готовая формулировка, которую можно вставить в акт.

Стиль — строгий русский юридический: «установлено, что», «в ходе проверки выявлено», «не соответствует требованиям», «данное обстоятельство свидетельствует о нарушении».

## Что внутри

| Файл | Назначение |
| --- | --- |
| `scripts/cli.py` | CLI entry point, маршрутизация режимов, exit-коды |
| `scripts/skill_config.py` | 4 обёртки над `lib.core.skill_config` |
| `scripts/output.py` | Формат `{mode, status, data}` + `make_error` + `sanitize_output` |
| `scripts/llm.py` | Тонкая обёртка над `lib.services.llm_client.call_llm` + `chat_json` с одной повторной попыткой и локальным `JsonParseError` |
| `scripts/vnd_io.py` | Извлечение текста (`extract_text`) + чанкование (`split_text`) + оценка размера |
| `scripts/prompts.py` | `load_prompt` + `render_prompt` с проверкой неразрешённых `{{...}}` |
| `scripts/modes/analyze.py` | Нормализация отклонения (1 LLM-вызов) |
| `scripts/modes/search.py` | Поиск релевантных фрагментов (map → фильтр → top-K, N LLM-вызовов) |
| `scripts/modes/synthesize.py` | Финальный отчёт с валидацией цитат (1 LLM-вызов) |
| `scripts/report/` | Реальные рендереры: `markdown.py`, `plain.py`, `docx_render.py` |
| `prompts/` | System-промпты для трёх фаз |
| `references/` | Архитектура, контракты, тестирование |

## Что НЕ делать

- ❌ **НЕ вызывать `workspace.utils.office_files.extract_text()` напрямую** — это I/O-утилита, skill сам извлекает текст.
- ❌ Не делать LLM-анализ отклонения самостоятельно — только через `cli.py`.
- ❌ Не вызывать `lib.services.text_splitter.split_text` напрямую для ВНД — skill сам чанкует.
- ❌ Не модифицировать `prompts/*.md` без обновления контракта в `references/contracts.md`.

## Подробности

| Документ | Что внутри |
| --- | --- |
| `references/architecture.md` | Слои, dependency direction, инварианты. |
| `references/contracts.md` | JSON-контракты всех режимов (включая estimate-only), error_type → exit-code, контракт evidence_id и правила валидации цитат, единый whitelist relation_type. |
| `references/testing.md` | Тестовая инфраструктура, mock LLM на границе `scripts.llm`. |
