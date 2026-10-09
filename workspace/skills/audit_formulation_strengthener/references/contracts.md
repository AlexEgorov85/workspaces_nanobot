# Контракты данных между режимами

Все режимы возвращают **tuple** `(result_dict, report_text_or_None)`:

```python
type ModeResult = tuple[dict[str, Any], str | None]
```

`report_text` заполнен **только** в режиме `synthesize` (markdown-рендер отчёта).
Для `analyze` и `search` всегда `None`.

## Общий формат успеха

```json
{
  "status": "success",
  "data": { ... специфичный для режима ... }
}
```

## Общий формат ошибки

```json
{
  "status": "error",
  "data": {
    "error_type": "<machine-readable>",
    "message": "<human-readable>"
  }
}
```

`error_type` — стабильная категория для парсинга и для маппинга в exit-код CLI:

| error_type | Exit code | Когда возникает |
|------------|-----------|-----------------|
| `no_vnd` | 2 | Не указаны файлы ВНД |
| `vnd_not_found` | 2 | Указанный файл не существует или исчез во время обработки |
| `vnd_unreadable` | 2 | Файл защищён паролём / повреждён / не извлекается `extract_text` |
| `vnd_empty` | 2 | Файл без текстового слоя (скан без OCR) или пустой |
| `too_many_chunks` | 2 | `chunks_total > execution.max_chunks_for_execution` |
| `empty_violation` | 2 | Пустая/whitespace-only формулировка отклонения |
| `analyze_result_unreadable` | 2 | `--analyze-result` указан, но файл не читается / не парсится |
| `search_result_unreadable` | 2 | `--search-result` указан, но файл не читается / не парсится |
| `unknown_mode` | 2 | Несуществующий режим (защита от багов в CLI-роутинге) |
| `prompt_missing` | 1 | Файл промпта не найден в `prompts/` |
| `prompt_unresolved_var` | 1 | В шаблоне остались неразрешённые `{{...}}` |
| `json_parse_failed` | 1 | LLM вернул невалидный JSON после двух попыток |
| `schema_mismatch` | 1 | LLM вернул JSON без обязательных полей |
| `llm_error` | 1 | Сетевая/системная ошибка LLM (или все чанки упали в search) |
| `io_error` | 1 | Не удалось записать отчёт в файл (например, нет прав) |
| `internal_error` | 1 | Непредвиденное исключение в CLI |

Exit-коды: **0** — успех, **1** — ошибка обработки, **2** — ошибка входных данных.

## Estimate-only: форма и маркер `estimate: true`

Каждый режим поддерживает `--estimate-only` (0 LLM-вызовов). Форма ответа
**всегда** содержит маркер `estimate: true` внутри `data`:

```json
{
  "status": "success",
  "data": {
    "estimate": true,
    "//": "...режим-специфичные поля...",
    "llm_calls_planned": <int>
  }
}
```

Арифметика `llm_calls_planned`:

| Режим | `llm_calls_planned` | Примечание |
|---|---|---|
| `analyze` | `1` | Один LLM-вызов на нормализацию. Не читает ВНД. |
| `search` | `N` | Только map по чанкам (`N = chunks_total`). Analyze-фаза пропускается (используется `--analyze-result` или сырой violation). |
| `synthesize` | `1 + N + 1` | Один analyze + N map + один финальный синтез. **Делает parse+chunk ВНД ради N** (0 LLM-вызовов, но I/O происходит). Без `--vnd` → `no_vnd`. |
| `all` | `1 + N + 1` | Полная картина одного запуска. Форма как у `synthesize-estimate + vnd_count`. |

Поведение `--estimate-only + --output`: `--output` **игнорируется** (нет артефакта — нет файла). Оценка только в stdout.

---

## analyze → search → synthesize

### `analyze.run()` → AnalyzeData

**Успех:**
```json
{
  "status": "success",
  "data": {
    "raw_text": "<исходная формулировка>",
    "normalized": "<нормализованная формулировка, 1-2 предложения>",
    "key_concepts": ["<концепт 1>", "<концепт 2>", "..."],
    "severity": "высокая" | "средняя" | "низкая",
    "suggested_vnd_sections": ["<раздел 1>", "..."]
  }
}
```

**Нормализация:**
- `severity` всегда в whitelist `{"высокая", "средняя", "низкая"}`. Невалидное → `"средняя"`.
- `key_concepts` — `list[str]` (только строковые элементы после фильтра).
- `suggested_vnd_sections` — `list[str]`, может быть пустым.
- `normalized` обязательно, не пустое.

**Estimate** (отличается маркером `estimate: true` и числом LLM-вызовов):
```json
{
  "status": "success",
  "data": {
    "estimate": true,
    "violation_chars": <int>,
    "vnd_count": <int>,
    "llm_calls_planned": 1
  }
}
```

---

### `search.run()` → SearchData

**Успех:**
```json
{
  "status": "success",
  "data": {
    "vnd_findings": [
      {
        "evidence_id": "F1" | "F2" | ... | "FN",
        "source_file": "<путь к файлу ВНД>",
        "chunk_index": <int, глобальный индекс чанка>,
        "text_excerpt": "<excerpt чанка, до 2000 символов с маркером>",
        "relation_type": "прямое_противоречие" | "прямое_подтверждение" | "косвенное_отношение" | "контекст",
        "relevance_score": <float, 0.0-1.0, округлённый до 3 знаков>,
        "why_matches": "<объяснение, 1-3 предложения>"
      }
    ],
    "vnd_chunks_total": <int, всего чанков>,
    "chunks_processed": <int, успешно обработанных>,
    "chunks_failed": <int, с ошибками LLM>,
    "chunks_relevant_total": <int, прошедших порог>,
    "top_k_candidates": 10,
    "min_relevance_score": 0.3,
    "normalized_violation_used": "<фактически переданный в LLM-промпт текст отклонения>"
  }
}
```

**Нормализация:**
- `relation_type` всегда в whitelist (4 значения). Невалидное → `"контекст"`.
- `relevance_score` clamp к `[0.0, 1.0]`, округлён до 3 знаков.
- Чанки с `score < MIN_RELEVANCE_SCORE (0.3)` **отбрасываются**.
- Сортировка: по `relevance_score` убывание, берётся top-`TOP_K_CANDIDATES (10)`.
- `evidence_id` — сквозные `F1..FN` по успешно прошедшим фильтр находкам.

**Graceful degradation:** при ошибке LLM на одном чанке — `chunks_failed += 1`, прогон продолжается.
Если **все** чанки упали — `status=error, error_type=llm_error`.

**`normalized_violation_used`:** строка, фактически переданная в LLM-промпт на map-фазе.
Это либо `normalized` из `analyze_result` (приоритет), либо `analyze_result.normalized` из файла
`--analyze-result`, либо **сырой текст `violation`** (fallback, если ни один из них не задан).
Не путать с `analyze_result.data.normalized` — это нормализованная формулировка из analyze-фазы,
а `normalized_violation_used` отражает факт использования (или неиспользования) нормализации.

**Estimate:**
```json
{
  "status": "success",
  "data": {
    "estimate": true,
    "size_estimate": {
      "files": <int>,
      "chunks_total": <int>,
      "chunks_per_file": [<int>, ...],
      "chars_total": <int>
    },
    "llm_calls_planned": <N, chunks_total>
  }
}
```

---

### `synthesize.run()` → Report

**Успех:**
```json
{
  "status": "success",
  "data": {
    "title": "<краткий заголовок отчёта>",
    "violation_summary": ["<абзац 1>", "<абзац 2>"],
    "established_facts": ["<абзац>"],
    "deviation_analysis": ["<абзац>"],
    "vnd_citations": [
      {
        "evidence_id": "F1",
        "source_file": "<путь к файлу>",
        "chunk_index": <int>,
        "excerpt": "<цитата, точная подстрока text_excerpt находки>",
        "relation_type": "<из находки, не из LLM>",
        "relation_explanation": "<объяснение, 1-2 предложения>"
      }
    ],
    "citations_dropped": <int, число отклонённых при валидации>,
    "verdict": {
      "category": "высокая" | "средняя" | "низкая",
      "verdict_text": ["<абзац>"]
    },
    "recommended_formulation": ["<абзац 1>", "<абзац 2>"],
    "normalized_violation": "<использованная нормализованная формулировка>",
    "severity": "<severity из analyze>",
    "source_findings_count": <int, всего в реестре>,
    "date_iso": "<ISO-8601 UTC>"
  },
  "saved_to": "<абсолютный путь к файлу, если --output указан>"
}
```

**Нормализация:**
- `verdict.category` — fallback на severity из analyze; невалидное → `"средняя"`.
- `vnd_citations[i].relation_type` — **берётся из находки**, не из LLM. LLM возвращает
  только `excerpt` и `relation_explanation`.
- `*_summary`/`*_facts`/`*_analysis`/`*_formulation` — `list[str]`, пустые строки отбрасываются.

**Валидация цитат (пункт 6 ревью):**

LLM может вернуть цитаты, не проходящие проверку. Каждая цитата проверяется:

1. `evidence_id` присутствует в реестре находок (F1..FN). Неизвестный id → отбрасывается.
2. `excerpt` после `.strip()` обеих сторон — **подстрока** `text_excerpt` соответствующей
   находки. **БЕЗ whitespace-collapse** (это бы сломало гарантию точности). Любое
   расхождение (правка, перефразирование, добавление) → отбрасывается.
3. `relation_type` берётся из находки (не из LLM). Это гарантирует единый whitelist
   relation_type на весь skill (search + synthesize используют один и тот же набор).

Отброшенные цитаты инкрементируют `citations_dropped` и попадают в финальный отчёт
строкой «Отклонено проверкой: N цитат не прошли сверку с источником».
Если **все** цитаты отброшены — статус остаётся `success` (не error), в отчёте
плейсхолдер «*(секция не заполнена: все N цитат отклонены проверкой)*».

**Estimate:**
```json
{
  "status": "success",
  "data": {
    "estimate": true,
    "violation_chars": <int>,
    "size_estimate": {
      "files": <int>,
      "chunks_total": <int>,
      "chunks_per_file": [<int>, ...],
      "chars_total": <int>
    },
    "llm_calls_planned": <1 + chunks_total + 1>
  }
}
```

`synthesize` для estimate-only **выполняет parse+chunk ВНД** ради `chunks_total`
(0 LLM-вызовов, но файловый I/O происходит). Без `--vnd` → `no_vnd`.

---

## Markdown-рендер (для CLI)

`scripts/report/markdown.py::render_markdown(data) -> str` рендерит 6 секций
(единые с SKILL.md и `docs/skill-tool-architecture.md` §11):

```markdown
# <title>

**Дата:** ...  **Категория тяжести:** ...
**Валидированных доказательств:** N (из M кандидатов)
**Отклонено проверкой:** K цитат не прошли сверку с источником
> **Нормализованная формулировка:** ...

## 1. Краткое изложение отклонения
## 2. Установленные факты (по ВНД)
## 3. Анализ отклонения
## 4. Релевантные фрагменты ВНД (валидированные цитаты)
   ### 1. [F1] <source_file> — чанк <chunk_index>
   **Тип соотнесения:** <relation_type>
   > <excerpt>
   <relation_explanation>
## 5. Итоговая классификация
## 6. Рекомендуемая усиленная формулировка

---
_Отчёт сгенерирован skill'ом `audit_formulation_strengthener`._
```

`plain.py::render_plain(data) -> str` снимает markdown-разметку (заголовки, bold, italic,
blockquotes, code, hr). `docx_render.py::write_docx(data, path)` пишет DOCX через
python-docx; при отсутствии библиотеки — RuntimeError → `io_error`.

---

## Совместимость

Контракт версионируется через стабильные `error_type` и **имена полей** в `data.*`.

**Breaking changes** (требуют bump версии skill'а):
- Переименование любого ключа в `data.*`.
- Удаление `error_type` из множества.
- Изменение whitelist значений (`severity`, `relation_type`, `verdict.category`).
- Изменение правил валидации цитат.

**Non-breaking:**
- Добавление новых ключей в `data.*` (например, `citations_dropped` — добавлено в этой версии).
- Добавление новых `error_type`.
- Изменение формата markdown-рендера (если не нарушает структуру 6 разделов).
- Переход на другую модель LLM (если контракт JSON сохраняется).
