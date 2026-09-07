# Architecture — `legal_summarizer`

> Это **единственный** технический архитектурный документ Skill. Старые
> `ARCHITECTURE.md` / `ARCHITECTURE_V2.md` удалены как конфликтующие
> источники истины.

`legal_summarizer` — самодостаточный Agent Skill (Anthropic Skills
модель). Skill упакован как набор инструкций (`SKILL.md`), скриптов
(`scripts/`), runtime-реализации (`legal_summarizer/`), prompts
(`prompts/`) и developer-only тестов (`tests/`).

## Skill layout

```text
legal_summarizer/
├── SKILL.md                # инструкция агенту
├── README.md               # developer overview
│
├── legal_summarizer/       # runtime Python-пакет
│   ├── application/
│   ├── cache/
│   ├── chunking/
│   ├── document/
│   ├── execution/
│   ├── llm/
│   ├── output/
│   ├── planning/
│   └── retrieval/
│
├── scripts/                # точки запуска
│   ├── cli.py
│   └── cli_query.py
│
├── prompts/                # LLM-инструкции
│
├── references/             # подробные документы (этот файл и др.)
│
└── tests/                  # developer-only код
    ├── unit/
    ├── integration/
    └── architecture/
```

## `src/` отсутствует намеренно

Skill — **self-contained Agent Skill**, а не отдельный Python distribution
package. `legal_summarizer/` лежит в корне Skill и импортируется по
`sys.path` из `scripts/cli.py` (см. там `_SKILL_ROOT / "legal_summarizer"`).

## Главный поток

```text
Agent
  │
  ▼
SKILL.md
  │
  ▼
scripts/cli.py
  │
  ▼
application/service.run()
  │
  ├───────────────┐
  ▼               ▼
document       retrieval
  │               │
  ▼               │
chunking          │
  │               │
  └───────┬───────┘
          ▼
       planning
          │
          ▼
       execution
          │
          ▼
          llm
          │
          ▼
    single_flight
          │
          ▼
       LLM API
```

`cache` — application-level persistence boundary (не часть execution).

## Архитектурные слои

| Слой | Ответственность |
| --- | --- |
| `application` | orchestration use case, idempotency, manifest lifecycle, cache decisions |
| `document` | PhysicalDocument → DocumentStructure → DocumentAnalysis (canonical SoT) |
| `chunking` | DocumentStructure → Chunk[] (per-section locality) |
| `retrieval` | query normalization, lexical/ranking, fallback |
| `planning` | strategy selection (direct / map_reduce_flat / map_reduce_hierarchical) |
| `execution` | direct / map-reduce / hierarchical reducer (LLM вызовы) |
| `llm` | chat wrapper, prompts, single-flight boundary, retry/sanitize |
| `cache` | disk-манифест: chunk results, section summaries, result.json |
| `output` | JSON-форматирование финального результата |

## Зависимости между слоями (canonical)

```text
CLI
  ↓
application
  ↓
┌────────────┬────────────┬────────────┬────────────┐
document   chunking   retrieval   planning
                                  ↓
                              execution
                                  ↓
                                 llm
```

Cache вызывается **только** на application-level. Output — на
application/output boundary.

### Запрещённые зависимости

```text
document       → retrieval, execution, llm, planning
chunking        → application, execution, llm, cache
retrieval       → application, execution, llm
planning        → application, llm
execution       → application, retrieval, llm.client (lock)
llm             → application
```

Для LLM разрешено:

```text
llm → client
llm → prompts
llm → single_flight
llm → tokens
```

## Document — единый source of truth

`DocumentStructure` (`document/structure.py`) — единственная canonical
модель структуры документа. Все подсистемы получают её напрямую:

```python
def run(...) -> dict:
    insp = inspect(text, document_path=document_path)
    ctx = build_execution_context(insp, length=length, question=question)
    run_direct(...) / run_map_reduce(...)
```

`DocumentStructure.to_dict()` разрешён **только** на serialization
boundary (manifest / JSON output). Внутренние API принимают
`DocumentStructure | None`, не `dict | None`.

## Cache — application boundary

`cache.manifest` импортируется **только** в:

* `application.execution_orchestration` — решает, сохранять ли partials.
* `application.service` — idempotency + resume.
* `cache.manifest` сам.

`execution.pipeline` **не** импортирует `cache.manifest`: возвращает
`(batch_meta, chunk_results)`, application решает — записывать ли.

## Single-flight — единый boundary

Один `LLM_FLIGHT_LOCK` в `llm/single_flight.py`. Используется:

* `execution.pipeline.process_context_batch` — map-phase.
* `llm.calls.chat_locked` — manual вызовы (back-compat).

`threading.Lock` импортируется **только** в `llm/single_flight.py`.
`execution.pipeline` импортирует `LLM_FLIGHT_LOCK` напрямую (без
`import threading`).

## DocumentIdentity

`DocumentIdentity` живёт в `document/identity.py`. Используется
`PhysicalDocument` для freshness-check.

## TokenEstimator

`TokenEstimator` живёт в `llm/tokens.py` (не в document/). Общий для
chunking, packing, estimation, reducer.

## Numbering parser

`parse_numbering` + `assign_sibling_ordinals` живут в
`document/numbering.py`. Используются heading detection и structure
validation.

## Tests

`tests/` поделён на:

* `tests/unit/` — изолированные тесты подсистем.
* `tests/integration/` — E2E прогон с mock LLM.
* `tests/architecture/` — boundary guards.

## Известные ограничения

* `application.execution_orchestration.run_map_reduce` — длинный
  (~300 строк). Внутри одной цельной ответственности (map → reduce
  orchestration), что допустимо.
* `tests/architecture/test_layer_boundaries.py` — старый boundary test,
  обновлён на новый package path.