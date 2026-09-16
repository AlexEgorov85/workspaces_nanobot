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

`error_type` — стабильная категория для парсинга:

| error_type | Режим | Когда |
|------------|-------|-------|
| `empty_violation` | analyze | Пустая/whitespace-only формулировка |
| `prompt_missing` | analyze, search, synthesize | Не найден файл промпта |
| `json_parse_failed` | analyze, synthesize | LLM вернул невалидный JSON после всех retry |
| `schema_mismatch` | analyze, synthesize | LLM вернул JSON без обязательных полей |
| `llm_error` | analyze, synthesize | Сетевая/системная ошибка LLM |
| `no_vnd` | search | Не указаны файлы ВНД |
| `vnd_not_found` | search | Указанный файл не существует |
| `vnd_unreadable` | search | Файл защищён паролем / не содержит текстового слоя |
| `vnd_empty` | search | ВНД извлечены, но чанков 0 |
| `io_error` | synthesize | Не удалось записать отчёт в файл |

---

## analyze → search → synthesize

### `analyze.run()` → AnalyzeData

```json
{
  "status": "success",
  "data": {
    "raw_text": "<исходная формулировка>",
    "normalized": "<нормализованная формулировка, 1-2 предложения>",
    "key_concepts": ["<концепт 1>", "<концепт 2>", "..."],
    "severity": "высокая" | "средняя" | "низкая",
    "suggested_vnd_sections": ["<раздел 1>", "..."],
    "extras": { "notes": "...", "..." }   // опционально, любые доп. поля
  }
}
```

**Нормализация:**
- `severity` всегда в whitelist `{"высокая", "средняя", "низкая"}`. Невалидное → `"средняя"`.
- `key_concepts` — `list[str]`, фильтруются только строковые элементы.
- `suggested_vnd_sections` — `list[str]`, может быть пустым.
- `normalized` обязательно, не пустое.

---

### `search.run()` → SearchData

```json
{
  "status": "success",
  "data": {
    "vnd_findings": [
      {
        "source_file": "<путь к файлу ВНД>",
        "chunk_index": <int>,
        "section_title": "<заголовок раздела или пустая строка>",
        "section_path": "<путь по дереву разделов или пустая строка>",
        "text_excerpt": "<excerpt чанка, до 2000 символов>",
        "relation_type": "прямое_противоречие" | "прямое_подтверждение" | "косвенное_отношение" | "контекст" | "нерелевантно",
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
    "cache_key": "<SHA-256 hex>",
    "normalized_violation_used": "<использованная формулировка>"
  }
}
```

**Нормализация:**
- `relation_type` всегда в whitelist. Невалидное → `"контекст"`.
- `relevance_score` clamp к `[0.0, 1.0]`, округлён до 3 знаков.
- Чанки с `score < MIN_RELEVANCE_SCORE (0.3)` **отбрасываются**.
- Сортировка: по `relevance_score` убывание, берётся top-`TOP_K_CANDIDATES (10)`.

**Graceful degradation:** при ошибке LLM на одном чанке — `chunks_failed += 1`, прогон продолжается.

---

### `synthesize.run()` → Report

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
        "source_file": "<путь к файлу>",
        "section_title": "<заголовок раздела>",
        "section_path": "<путь по дереву>",
        "excerpt": "<цитата, 1-3 предложения>",
        "relation_type": "прямое_противоречие" | "прямое_подтверждение" | "косвенное_отношение" | "контекст",
        "relation_explanation": "<объяснение, 1-2 предложения>"
      }
    ],
    "verdict": {
      "category": "высокая" | "средняя" | "низкая",
      "verdict_text": ["<абзац>"]
    },
    "recommended_formulation": ["<абзац 1>", "<абзац 2>"],
    "date_iso": "<ISO-8601 UTC>",
    "normalized_violation": "<использованная нормализованная формулировка>",
    "severity": "<severity из analyze>",
    "source_findings_count": <int>
  },
  "saved_to": "<абсолютный путь к файлу, если --output указан>"
}
```

**Нормализация:**
- `verdict.category` — fallback на severity из analyze; невалидное → `"средняя"`.
- `vnd_citations[i].relation_type` — невалидное → `"контекст"`.
- `*_summary`/`*_facts`/`*_analysis`/`*_formulation` — `list[str]`, пустые строки отбрасываются.

**Markdown-рендер** (для CLI):

```markdown
# <title>

**Дата:** ...  **Категория тяжести:** ...

## 1. Краткое изложение отклонения
## 2. Установленные факты (по ВНД)
## 3. Анализ отклонения
## 4. Релевантные фрагменты ВНД
   ### 1. <section>
   **Источник:** ... **Тип соотнесения:** ...
   > <excerpt>
   <relation_explanation>
## 5. Итоговая классификация
## 6. Рекомендуемая усиленная формулировка

---
_Отчёт сгенерирован skill'ом audit_formulation_strengthener._
```

---

## Кэширование

`cache_key` — детерминированный SHA-256:

```python
hashlib.sha256()
    .update(violation.encode("utf-8"))
    .update(b"\x00")
    .update(p.encode("utf-8"))
    .update(b"\x00")
    .update(next_p.encode("utf-8"))
    ...
```

Пути **сортируются** перед хешированием, так что порядок `--vnd` не влияет на ключ.

Использование:
- В CLI: `--analyze-result <path>` и `--search-result <path>` — загрузка кэшированных
  результатов предыдущих фаз.
- В `_run_all`: передача результатов in-memory между фазами (без чтения с диска).

---

## Совместимость

Контракт версионируется через стабильные `error_type` и **имена полей** в `data.*`.

**Breaking changes** (требуют bump версии skill'а):
- Переименование любого ключа в `data.*`.
- Удаление `error_type` из множества.
- Изменение whitelist значений (`severity`, `relation_type`).

**Non-breaking:**
- Добавление новых ключей в `data.*` (например, `extras`).
- Добавление новых `error_type`.
- Изменение формата markdown-рендера (если не нарушает структуру разделов 1-6).
