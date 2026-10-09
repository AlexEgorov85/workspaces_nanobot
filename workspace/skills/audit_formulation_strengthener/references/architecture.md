# Архитектура skill'а `audit_formulation_strengthener`

## Назначение

Skill принимает **текст отклонения** + **файлы ВНД** (`.pdf`/`.docx`/`.txt`) и возвращает
**человекочитаемый отчёт** в строгом русском юридическом стиле с релевантными фрагментами
ВНД и рекомендацией по усилению формулировки.

**Акт НЕ передаётся** — только отклонение и ВНД.

## Слои

```
┌────────────────────────────────────────────────────────────────────┐
│  CLI  (scripts/cli.py)                                             │
│  ─ argparse: --violation, --vnd ×N, --mode, --output, --estimate-only│
│  ─ Единственная точка модификации sys.path (корень репо)            │
│  ─ Exit-коды: 0 success, 1 processing errors, 2 input errors         │
└────────────────────────────┬───────────────────────────────────────┘
                             │
                             ▼
┌────────────────────────────────────────────────────────────────────┐
│  Pipeline orchestrator (_run_all в cli.py)                         │
│  analyze → search (map по чанкам → фильтр/top-K) → synthesize      │
└─────┬───────────────────┬─────────────────────────┬────────────────┘
      ▼                   ▼                         ▼
┌──────────────┐  ┌──────────────────┐  ┌─────────────────────────┐
│ modes/       │  │ modes/           │  │ modes/                  │
│ analyze.py   │  │ search.py        │  │ synthesize.py           │
│              │  │                  │  │                         │
│ - 1 LLM call │  │ - N LLM calls    │  │ - 1 LLM call            │
│ - normalize  │  │   (map по        │  │ - validate citations    │
│   violation  │  │   чанкам)        │  │ - render md/txt/docx    │
│              │  │ - filter (≥0.3)  │  │                         │
│              │  │ - top-K (10)     │  │                         │
└─────┬────────┘  └────────┬─────────┘  └────────────┬────────────┘
      │                   │                         │
      ▼                   ▼                         ▼
┌────────────────────────────────────────────────────────────────────┐
│  Общие зависимости (внутри skill'а)                                │
│  - scripts/prompts.py: load_prompt(), render_prompt() (с проверкой │
│    неразрешённых {{...}})                                          │
│  - scripts/llm.py: chat(), chat_json() (тонкая обёртка над         │
│    lib.services.llm_client.call_llm + 1 retry на невалидном JSON)  │
│  - scripts/vnd_io.py: prepare_vnd() — extract_text + split_text    │
│  - scripts/output.py: make_error() — типизированные ошибки         │
│  - scripts/skill_config.py: get_*() — обёртки над core.skill_config│
└────────────────────────────┬───────────────────────────────────────┘
                             │
                             ▼
┌────────────────────────────────────────────────────────────────────┐
│  Переиспользуемые сервисы (только lib/ и workspace/utils/)         │
│  - lib.services.llm_client.call_llm — транспорт LLM                │
│  - lib.services.text_splitter.split_text — чанкование               │
│  - lib.core.skill_config.{get_llm_config, get_cli_config,           │
│      get_max_retries, get_chunking_config, get_tool_config}         │
│  - lib.utils.text_utils.sanitize_value — JSON-safe нормализация     │
│  - workspace.utils.office_files.extract_text — извлечение текста   │
└────────────────────────────────────────────────────────────────────┘
```

## Поток данных

```
violation_text ───┐
                  │
vnd_paths ────────┼──► vnd_io.prepare_vnd() ──► VndBundle(chunks)
                  │                              │
                  │                              ▼
                  ▼                          ┌──────────────┐
            analyze.run() ──► AnalyzeData ──►│              │
            (1 LLM)                          │              │
                                              │              ▼
            search.run() ──► SearchData ─────►│  synthesize.run() ──► Report JSON
            (N LLM, map по чанкам)            │  (1 LLM)             │
                       │                      │                      ▼
                       ▼                      │              Markdown + DOCX
                  Top-K findings              │              ┌────────────┐
                  F1..FN                      └──────────────│  report.md │
                                                                 │ report.txt│
                                                                 │ report.docx│
                                                                 └────────────┘
```

## Границы ответственности

| Слой | Ответственность | НЕ делает |
|------|-----------------|-----------|
| `cli.py` | argparse, регистрация skill, оркестрация режимов, exit-коды | LLM-вызовы, парсинг |
| `modes/analyze.py` | Загрузка промпта, LLM-вызов, нормализация JSON | Чтение ВНД |
| `modes/search.py` | Загрузка ВНД, map по чанкам, фильтр, top-K, evidence_id | Финальный отчёт |
| `modes/synthesize.py` | Загрузка analyze/search, LLM-вызов, валидация цитат, рендер | Извлечение чанков |
| `prompts/*.md` | Текст system-промптов | Логика |
| `llm.py` | Единая точка LLM с chat + chat_json (1 retry) | Конкретные промпты |
| `vnd_io.py` | Извлечение чанков + size_estimate | LLM |
| `report/*.py` | Рендер md/plain/docx | Бизнес-логика |
| `output.py` | Единый формат error-результата | Успешные пути |

## Алгоритмы

### analyze (1 LLM-вызов)

```
1. Валидация: violation не пустой
2. Загрузить prompts/analyze_system.md
3. Подставить {{VIOLATION_TEXT}}
4. LLM → JSON {normalized, key_concepts, severity, suggested_vnd_sections}
5. Нормализация: severity → whitelist {низкая, средняя, высокая};
   key_concepts → list[str] с фильтром
6. Вернуть AnalyzeData
```

### search (N LLM-вызовов — map по чанкам → фильтр/top-K)

```
1. vnd_io.prepare_vnd() → chunks + size_estimate
2. (опц.) Резолвить normalized_violation из analyze_result / analyze_result_path;
   fallback — сырой violation
3. Для каждого чанка:
   - Подставить {{VIOLATION_TEXT}}, {{VND_FILE}}, {{CHUNK_TEXT}}
   - LLM → JSON {relation_type, relevance_score, why_matches}
   - При ошибке парсинга/валидации: chunks_failed += 1, continue
4. Фильтр: relevance_score >= MIN_RELEVANCE_SCORE (0.3)
5. Re-rank: top-K (10) по score убывание
6. Каждой находке присвоить evidence_id "F1..FN"
7. Если ВСЕ чанки упали на LLM — status=error, error_type=llm_error
8. Вернуть SearchData с vnd_findings
```

### synthesize (1 LLM-вызов)

```
1. Загрузить analyze/search (in-memory или из файла)
2. Извлечь normalized_violation, severity, key_concepts, vnd_findings
3. Построить evidence registry F1..FN (≤1500 симв excerpt + relation_type)
4. Загрузить prompts/synthesize_system.md
5. Подставить {{NORMALIZED_VIOLATION}}, {{SEVERITY}}, {{KEY_CONCEPTS_JSON}},
   {{EVIDENCE_JSON}}
6. LLM → JSON {title, violation_summary, established_facts, deviation_analysis,
   vnd_citations[{evidence_id, excerpt, relation_explanation}], verdict,
   recommended_formulation}
7. Валидация цитат:
   - evidence_id должен быть в реестре
   - excerpt — подстрока text_excerpt находки (после .strip() обеих сторон,
     БЕЗ whitespace-collapse)
   - relation_type берётся из находки (не из LLM)
8. Отброшенные → citations_dropped + строка в отчёте
9. Render markdown (inline-вызов report.markdown.render_markdown)
10. Опционально: сохранить в .md / .txt / .docx
11. Вернуть Report JSON + markdown text
```

## Инварианты

1. **Skill полностью автономен от других скиллов.** В `scripts/` нет ни одного
   импорта из `workspace.skills.*` (включая `legal_summarizer`, `audit_analyzer`).
   Только `lib.*` и `workspace.utils.*`.
2. **Единственная модификация `sys.path` в production-коде** — в `cli.py` (корень репо).
3. **Только ключи существующей `SkillSettings`-схемы** в `project.json`. Никаких
   новых полей. `SkillSettings(extra="forbid")` валидирует это при старте.
4. **Skill-specific константы — в Python-коде, именованные, документированные:**
   `modes/search.py::TOP_K_CANDIDATES = 10`,
   `modes/search.py::MIN_RELEVANCE_SCORE = 0.3`.
5. **Контракт-исключения живут в скилле.** `JsonParseError` определён в
   `scripts/llm.py`, не в `lib/`. Это контракт потребителя, не инфраструктура транспорта.
6. **Без секций.** `text_splitter.split_text` не знает про разделы документа.
   Цитата = `(source_file, chunk_index, text_excerpt)`.
7. **Без SHA-256 cache_key.** Прежнее поле было мёртвым (без реального
   cache-провайдера) и удалено.

## Конфигурация

`project.json::skills.audit_formulation_strengthener`:

```json
{
  "enabled": true,
  "tables": [],
  "vector_indexes": [],
  "cli":   { "default_mode": "all", "timeout_sec": 120, "max_retries": 3 },
  "llm":   { "max_tokens": 4096, "temperature": 0.1 },
  "chunking": { "chunk_size": 6000, "chunk_overlap": 400 },
  "execution": { "max_chunks_for_execution": 50 }
}
```

- `chunking.chunk_size: 6000` — под LLM-вызов по одному чанку (search map).
- `execution.max_chunks_for_execution: 50` — safety net: суммарное число чанков
  больше этого → `too_many_chunks` (явная ошибка).
- Все остальные ключи (`tables`, `vector_indexes`, `execution.confirmation_*`,
  `chunking.chunk_size_input_ratio`, `chunking.single_call_threshold`) —
  отсутствуют умышленно.

## Тестирование

- Тесты в `tests/`. Все LLM-фазы замоканы на границе `scripts.llm.chat_json/chat`.
- VND через `extract_text` замокан через `mock_prepare_vnd` (tmp-файлы).
- Архитектурные инварианты проверяются в `tests/test_architecture.py`
  (AST + plain string по `scripts/**/*.py`).

См. `references/testing.md` для деталей.
