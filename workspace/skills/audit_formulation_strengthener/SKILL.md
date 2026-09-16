---
name: audit_formulation_strengthener
description: Усиление формулировки отклонения по ВНД — вызывай ТОЛЬКО через `python workspace/skills/audit_formulation_strengthener/scripts/cli.py`. Принимает текст отклонения + один/несколько файлов ВНД (.pdf/.docx/.txt), возвращает человекочитаемый отчёт (.md/.docx/.txt) в строгом русском юридическом стиле: нормализация формулировки, релевантные пункты/абзацы/смысловые куски из ВНД с пояснением «почему соотносится с отклонением», вердикт о достаточности формулировки и рекомендация по усилению. Для длинных ВНД сначала вернёт confirmation_required.
metadata: {"nanobot":{"emoji":"⚖️","always":true}}
---

# Audit Formulation Strengthener — единственный путь: `cli.py`

> ⚠️ **ПРАВИЛО #1 (нарушать нельзя):** анализ формулировки выполняет
> ТОЛЬКО `python workspace/skills/audit_formulation_strengthener/scripts/cli.py`.
> Никаких прямых вызовов `workspace.utils.office_files.extract_text()`,
> `DocumentStructureChunker` или самостоятельного LLM-анализа —
> skill единственный владелец pipeline.

> ⚠️ **ПРАВИЛО #2 (обязательно для длинных ВНД):** если skill вернул
> `status="confirmation_required"`, **НИКОГДА не вызывай его сразу
> с `--confirm`** и **НИКОГДА не выбирай режим за пользователя**.
> Сначала покажи пользователю **меню из вариантов** из `options[]`
> (с `id`, `label`, `min/max_seconds`, `description`). Только **после
> явного выбора** пользователя вызывай `cli.py` повторно с нужными
> флагами. Если пользователь ответил «давай» без уточнений — по
> умолчанию `--mode all`.

## Когда вызывать

- Аудитор сформулировал отклонение/нарушение и просит:
  - «проверь формулировку по ВНД»,
  - «найди релевантные пункты ВНД»,
  - «усиль формулировку отклонения»,
  - «есть ли в ВНД основания для этого нарушения».
- В наличии один или несколько файлов ВНД (`.pdf` / `.docx` / `.txt`).

Когда **не** вызывать:

- ВНД отсутствуют — попроси у аудитора файлы.
- Файл ВНД защищён паролём или является сканом без текстового слоя — skill вернёт ошибку; попроси текстовую версию.
- Файл не офисный (изображение, архив, бинарник) — это не задача skill'а.
- Задача — саммари одного документа без привязки к отклонению (это `legal_summarizer`).
- Задача — анализ данных аудита из БД (это `audit_analyzer`).

## Запуск

> ℹ️ PYTHONPATH выставлять **не нужно** — `cli.py` сам подкладывает
> корень репо и `scripts/` в `sys.path`.

### Каноническая команда (полный пайплайн)

```bash
python workspace/skills/audit_formulation_strengthener/scripts/cli.py \
    --violation "Срок хранения персональных данных установлен 1 год" \
    --vnd vnd1.pdf --vnd vnd2.docx \
    --output report.md
```

- **`--violation`** — обязательный, текст отклонения в кавычках.
- **`--vnd`** — повторяемый аргумент, минимум один файл `.pdf`/`.docx`/`.txt`.
- **`--output`** — путь к итоговому файлу отчёта. Если не указан — отчёт в stdout.
- **`--output-format md|docx|txt`** — формат (по умолчанию `md`).
- **`--mode analyze|search|synthesize|all`** — режим (по умолчанию `all`).
- **`--internal-format json|report`** — формат вывода для `analyze`/`search` (по умолчанию `report` — для человека).
- **`--estimate-only`** — только оценка без LLM.
- **`--confirm`** — подтверждение для длинных ВНД.
- **`--max-chunks N`** — override `max_chunks_for_execution` из project.json.
- **`--analyze-result /path/to/analyze.json`** / **`--search-result`** — резюмировать финальный отчёт из кэшированных результатов.
- **`--timeout N`** — таймаут операции в секундах (по умолчанию из `cli.timeout_sec`).

Полный список — `cli.py --help`. Подробности по JSON-контрактам — `references/contracts.md`.

## Протокол

### Короткое ВНД (≤ `single_call_threshold`)

```bash
python .../cli.py --violation "..." --vnd short.pdf
```

Skill выполняет все три фазы (`analyze` → `search` → `synthesize`)
внутри одного запуска. В stdout (или файл `--output`) — готовый
человекочитаемый отчёт `.md`.

### Длинное ВНД (> оценки порога)

Без `--confirm` skill **не запускает** обработку. Возвращает
`confirmation_required` с меню:

```json
{
  "status": "confirmation_required",
  "options": {
    "all": {"min_sec": 40, "max_sec": 156, "label": "полный отчёт"},
    "analyze_only": {"min_sec": 15, "max_sec": 30, "label": "только нормализация"}
  }
}
```

Покажи пользователю меню **коротким** текстом и дождись явного выбора.
Только после выбора повтори `cli.py` с `--mode <chosen> --confirm`.

### Resume

Прерванный прогон можно продолжить из кэшированных результатов:

```bash
python .../cli.py --mode synthesize \
    --violation "..." --vnd vnd1.pdf \
    --analyze-result /tmp/analyze.json \
    --search-result /tmp/search.json
```

## Структура финального отчёта (для пользователя)

Отчёт в `.md` содержит разделы:

1. **Сведения об отклонении** — исходная формулировка, нормализованная, ключевые признаки, категория тяжести.
2. **Установленные нарушения требований ВНД** — по одному подразделу на релевантный фрагмент: файл ВНД, заголовок раздела, дословная цитата, объяснение «почему соотносится с отклонением».
3. **Вывод о достаточности формулировки** — вердикт (достаточно / требует усиления), перечень недостающих элементов.
4. **Рекомендуемая усиленная формулировка** — готовая формулировка, которую можно вставить в акт.
5. **Дата составления заключения**.

Стиль — строгий русский юридический: «установлено, что», «в ходе проверки выявлено», «не соответствует требованиям», «данное обстоятельство свидетельствует о нарушении».

## Что внутри

- `scripts/cli.py` — CLI entry point, маршрутизация режимов.
- `scripts/skill_config.py` — обёртки `lib.core.skill_config`.
- `scripts/output.py` — формат `{mode, status, data}` + `make_error`.
- `scripts/llm.py` — single-flight LLM-вызовы + JSON retry.
- `scripts/vnd_io.py` — multi-file извлечение текста ВНД + чанкование (этап 3).
- `scripts/chunking/vnd_chunker.py` — обёртка над `DocumentStructureChunker` из `legal_summarizer` (этап 3).
- `scripts/modes/analyze.py` — нормализация отклонения (этап 4).
- `scripts/modes/search.py` — поиск релевантных фрагментов ВНД (этап 5).
- `scripts/modes/synthesize.py` — финальный отчёт (этапы 6–7).
- `scripts/report/` — builder + renderers (md/docx/txt) (этап 7).
- `prompts/` — system-промпты для LLM (этапы 4–6).
- `references/` — архитектура, контракты, тестирование.
- `tests/` — unit + contract + e2e тесты.

## Что НЕ делать

- ❌ **НЕ вызывать `workspace.utils.office_files.extract_text()` напрямую** — это I/O-утилита, skill сам извлекает текст.
- ❌ Не делать LLM-анализ отклонения самостоятельно — только через `cli.py`.
- ❌ Не вызывать `DocumentStructureChunker` напрямую (импорты через `skill.scripts.chunking`).
- ❌ Не интерпретировать `status="confirmation_required"` как готовый результат — нужен явный выбор пользователя.
- ❌ Не вызывать skill «в цикле» (`--confirm` → `status=partial` → ещё раз `--confirm`).

## Подробности

| Документ | Что внутри |
| --- | --- |
| `references/architecture.md` | Слои, dependency direction, invariants. |
| `references/contracts.md` | JSON-контракты всех режимов. |
| `references/testing.md` | Тестовая инфраструктура, mock LLM. |
