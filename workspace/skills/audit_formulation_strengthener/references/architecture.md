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
│  ─ _ensure_registered() — регистрация в TableRegistry              │
└────────────────────────────┬───────────────────────────────────────┘
                             │
                             ▼
┌────────────────────────────────────────────────────────────────────┐
│  Pipeline orchestrator (_run_all в cli.py)                         │
│  analyze → search (map-reduce) → synthesize                        │
└─────┬───────────────────┬─────────────────────────┬────────────────┘
      ▼                   ▼                         ▼
┌──────────────┐  ┌──────────────────┐  ┌─────────────────────────┐
│ modes/       │  │ modes/           │  │ modes/                  │
│ analyze.py   │  │ search.py        │  │ synthesize.py           │
│              │  │                  │  │                         │
│ - 1 LLM call │  │ - N LLM calls    │  │ - 1 LLM call            │
│ - normalize  │  │   (map)          │  │ - normalize             │
│   violation  │  │ - filter +       │  │ - render md/txt/docx    │
│              │  │   re-rank        │  │                         │
└─────┬────────┘  └────────┬─────────┘  └────────────┬────────────┘
      │                   │                         │
      ▼                   ▼                         ▼
┌────────────────────────────────────────────────────────────────────┐
│  Общие зависимости                                                 │
│  - scripts/prompts.py: load_prompt(), render_prompt()             │
│  - scripts/llm.py: call_llm_json() с retry + JSON-парсинг         │
│  - scripts/vnd_io.py: prepare_vnd() — извлечение чанков ВНД        │
│  - scripts/output.py: make_error() — типизированные ошибки         │
│  - scripts/skill_config.py: get_*() — обёртки над core.skill_config│
└────────────────────────────┬───────────────────────────────────────┘
                             │
                             ▼
┌────────────────────────────────────────────────────────────────────┐
│  Внешние зависимости (переиспользуемые)                            │
│  - workspace.skills.legal_summarizer.scripts.application            │
│      .pipeline_structure.run_canonical_pipeline() — I/O + chunking│
│  - workspace.skills.legal_summarizer.scripts.chunking.chunks.Chunk │
│  - workspace.skills.legal_summarizer.scripts.llm                   │
│      .single_flight.guarded_chat — single-flight защита            │
│  - workspace.utils.office_files.extract_text                       │
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
            (N LLM, map)                      │  (1 LLM)             │
                       │                      │                      ▼
                       ▼                      │              Markdown + DOCX
                  Top-K findings              │              ┌────────────┐
                                              └──────────────│  report.md │
                                                                 │ report.txt│
                                                                 │ report.docx│
                                                                 └────────────┘
```

## Границы ответственности

| Слой | Ответственность | НЕ делает |
|------|-----------------|-----------|
| `cli.py` | argparse, регистрация skill, оркестрация режимов, exit-codes | LLM-вызовы, парсинг |
| `modes/analyze.py` | Загрузка промпта, LLM-вызов, нормализация JSON | Чтение ВНД |
| `modes/search.py` | Загрузка ВНД, map-reduce, фильтр, re-rank | Финальный отчёт |
| `modes/synthesize.py` | Загрузка analyze/search, финальный LLM-вызов, рендер | Извлечение чанков |
| `prompts/*.md` | Текст system-промптов | Логика |
| `llm.py` | Единая точка LLM с retry и single-flight | Конкретные промпты |
| `vnd_io.py` | Извлечение чанков + кэш-ключ | LLM |
| `chunking/vnd_chunker.py` | Обёртка над `run_canonical_pipeline` | Собственный чанкер |
| `output.py` | Единый формат error-результата | Успешные пути |

## Алгоритмы

### analyze (1 LLM-вызов)
```
1. Валидация: violation не пустой
2. Загрузить prompts/analyze_system.md
3. Подставить {{VIOLATION_TEXT}}
4. LLM → JSON {normalized, key_concepts, severity, suggested_vnd_sections}
5. Нормализация: severity → whitelist, key_concepts → list[str]
6. Вернуть AnalyzeData
```

### search (N LLM-вызовов)
```
1. vnd_io.prepare_vnd() → chunks + cache_key
2. Для каждого chunk:
   - Подставить {{VIOLATION_TEXT}}, {{VND_FILE}}, {{VND_SECTION}}, {{CHUNK_TEXT}}
   - LLM → JSON {relation_type, relevance_score, why_matches}
   - При ошибке: chunks_failed += 1, continue
3. Фильтр: relevance_score >= MIN_RELEVANCE_SCORE (0.3)
4. Re-rank: top-K (10) по score убывание
5. Вернуть SearchData с vnd_findings
```

### synthesize (1 LLM-вызов)
```
1. Загрузить analyze/search (in-memory или из файла)
2. Извлечь normalized_violation, severity, key_concepts, vnd_findings
3. Загрузить prompts/synthesize_system.md
4. Подставить {{NORMALIZED_VIOLATION}}, {{SEVERITY}}, {{KEY_CONCEPTS_JSON}}, {{VND_FINDINGS_JSON}}
5. LLM → JSON {title, violation_summary, established_facts, ..., recommended_formulation}
6. Нормализация: relation_type → whitelist, verdict_category → whitelist
7. Render markdown (inline)
8. Опционально: сохранить в .md / .txt / .docx
9. Вернуть Report JSON + markdown text
```

## Решения дизайна

### Почему map-reduce, а не embeddings?
- Не нужно поддерживать vector-индексы для ВНД (ВНД приходят каждый раз заново).
- Максимально прозрачное «почему» через `why_matches` от LLM.
- Trade-off: дороже (N LLM-вызовов на N чанков) — но N ограничен размером ВНД.

### Почему `--estimate-only`?
- UX: пользователь видит «5 минут, 30 LLM-вызовов» **до** запуска.
- Confirmation_required pattern (см. `audit_analyzer`).
- Защита от случайного запуска на больших ВНД.

### Почему JSON остаётся внутренним контрактом?
- Тестируемость: каждый режим unit-тестируется отдельно.
- Отладка: можно смотреть промежуточные результаты.
- Кэшируемость: analyze_result и search_result можно сохранить на диск
  и переиспользовать при итерациях.

### Почему inline renderers, а не отдельный модуль `report/`?
- Этап 6 включает базовые md/txt/docx рендеры inline в `modes/synthesize.py`.
- Этап 7 (тесты) тестирует их через `modes/synthesize.run()`.
- Если потребуется отдельный модуль `report/` (например, для сложных шаблонов) —
  он будет вынесен в `scripts/report/` без breaking changes.

## Конфигурация

`project.json::skills.audit_formulation_strengthener`:

```json
{
  "enabled": true,
  "tables": [],
  "vector_indexes": [],
  "cli": {"default_mode": "all", "max_retries": 3, "timeout_sec": 120},
  "llm": {"max_tokens": 4096, "temperature": 0.1},
  "chunking": {"chunk_size": 100000, "chunk_overlap": 0, "single_call_threshold": 20000, "chunk_size_input_ratio": 0.5},
  "execution": {"confirmation_threshold_sec": 120, "estimated_chunk_duration_sec": 10, "max_chunks_for_execution": 50, "context_batching": false}
}
```

**Skill-specific константы** (в коде, не в JSON — см. `SkillSettings(extra="forbid")`):
- `modes/search.py::TOP_K_CANDIDATES = 10`
- `modes/search.py::MIN_RELEVANCE_SCORE = 0.3`

## Тестирование

- 42 теста в `tests/` (5 файлов).
- Все LLM-фазы замоканы.
- PDF/DOCX-парсинг замокан через `mock_prepare_vnd`.
- Тесты CLI через прямой вызов `main(argv)` + `redirect_stdout`.

См. `references/testing.md` для деталей.
