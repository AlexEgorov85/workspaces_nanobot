# Representative documents for `legal_summarizer`

Этот каталог — справочник документов, на которых зафиксирован baseline
и которые используются characterization / regression тестами.

Здесь **не лежат** бинарные копии — это только реестр и метаданные.
Сами файлы подгружаются из известных источников (см. столбец `Source`)
или генерируются inline в pytest-фикстурах (см. столбец `Source`).

## Cases

| ID | Формат | Размер | Структура | Источник | Назначение |
|:--:|:------:|-------:|-----------|----------|------------|
| `tiny_txt` | TXT  | ≤1 KB | 1 блок, нет sections | inline-генерация | smoke для single-call пути (≤ `single_call_threshold`) |
| `tiny_docx` | DOCX | 2-3 KB | 3 параграфа + 1 таблица | inline-генерация (test_structure_physical) | проверка DocumentOrder для малого DOCX |
| `small_legal_docx` | DOCX | 8-15 KB | несколько Heading N + таблицы | inline-генерация | DOCX style heading (score 0.95), small map-reduce |
| `medium_pdf_no_outline` | PDF | 30-80 KB | 10-15 страниц без outline, headings только через regex | inline-генерация | regex detection: Статья/Глава/§ |
| `medium_pdf_with_outline` | PDF | 30-80 KB | 10-15 страниц с PDF outline | inline-генерация | outline primary source (score 0.95) |
| `large_pdf_gk_rf` | PDF | ~5.7 MB | 663 страницы ГК РФ (PDF outline + много статей) | `C:\Users\Алексей\Downloads\gkodeksrf.pdf` (внешний путь) | representative реальный документ, актуальный размер ГК РФ |
| `tables_heavy_pdf` | PDF | ~50 KB | 5 страниц, ≥3 таблицы | inline-генерация | table-as-block инвариант, row-split крупной таблицы |
| `numbered_lists_docx` | DOCX | 5-10 KB | смесь `1. Heading` (regex level 1, score 0.65) и `1) item 2) item 3) item` (list) | inline-генерация | защита от взрыва micro-sections + корректное определение list |
| `resume_interrupted_pdf` | PDF | 100+ KB | крупный документ для прерванного run | inline-генерация | resume-сценарий |

## Synthetic fixtures

Эти «документы» уже есть в коде как pytest-фикстуры; их описание
используется как эталон при регрессии.

- `_generate_long_legal_text(pages=600, chars_per_page=3000, sections_per_doc=25)`
  в `tests/test_e2e_600_page.py`. Использует headings вида `1. Общие
  положения …`. Известное baseline-падение:
  `test_600_page_executes_via_context_batching` — синтетика даёт ровно
  1 chunk/section, и `map_calls == chunks_total` (8 == 8). Packing не
  может группировать, потому что section-locality rule (invariant #8)
  запрещает мешать секции в один batch.

## Baseline метрики (зафиксировано 2026-09-01)

Запущены **все** тесты `workspace/skills/legal_summarizer` через корневой pytest:

```
tests/test_skill_legal_summarizer.py
tests/test_manifest.py
tests/test_prompts.py
tests/test_structure_sections.py
tests/test_structure_physical.py
tests/test_structure_chunks.py
tests/test_packing.py
tests/test_reducer.py
tests/test_text_splitter.py
tests/test_e2e_600_page.py
```

Полный прогон по всему `tests/` невозможен: модуль
`tests/test_context_compaction_log.py` падает на сборе с
`ImportError: cannot import name '_async_record' from lib.services.context_compaction`.
Это **внешний тест** к `legal_summarizer`, его регрессия вне scope
skill'а. Документируем как known-issue и используем ограниченный прогон
только по тестам skill'а для baseline.

| Метрика | Значение |
|---------|---------:|
| PASSED | 202 |
| FAILED | 1 (baseline-known) |
| SKIPPED | 0 |
| Duration | ~40–44 сек |

### Известные baseline-падения

| Тест | Причина | Действие |
|------|---------|----------|
| `test_e2e_600_page.py::test_600_page_executes_via_context_batching` | Синтетический документ с 25 секциями × 1 chunk = 1 chunk/batch ⇒ `map_calls == chunks_total`. Packing не может группировать, потому что section-locality rule (invariant #8) запрещает мешать секции в один batch. | Не регрессия. Будет решаться через locality-aware packing (adjacent section mixing) или adaptive strategy. |

## Что НЕ покрыто baseline

- Реальный прогон `gkodeksrf.pdf` через CLI: требует LLM-вызовы и
  долог (10+ минут). Baseline-сравнение LLM-вызовов делается на
  синтетике и через моки (`monkeypatch.llm.chat`). Реальный прогон —
  performance benchmark.
- DOCX/PDF c merge_short_sections edge cases — отдельные кейсы будут
  добавляться в characterization tests.
