# Architecture — `legal_summarizer`

> Это **единственный** технический архитектурный документ Skill. Старые
> `ARCHITECTURE.md` / `ARCHITECTURE_V2.md` удалены как конфликтующие
> источники истины.

`legal_summarizer` — самодостаточный Agent Skill (Anthropic Skills
модель). Skill упакован как набор инструкций (`SKILL.md`), скриптов
(`scripts/`), runtime-реализации (непосредственно в `scripts/`,
9 runtime-слоёв), prompts (`prompts/`) и developer-only тестов
(`tests/`).

## Skill layout

```text
legal_summarizer/
├── SKILL.md                # инструкция агенту
├── README.md               # developer overview
│
├── scripts/                # executable runtime Skill
│   ├── cli.py              # entry point
│   ├── cli_query.py        # follow-up по operation_id
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
package.

Runtime расположен непосредственно в `scripts/`, который является
Python import root для standalone CLI: при запуске
`python workspace/skills/legal_summarizer/scripts/cli.py` каталог
`scripts/` явно добавляется в `sys.path` (см. `_SCRIPTS_ROOT` в `cli.py`).

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

Поток импортов направлен **снизу вверх** — от leaves к application:

```text
CLI
  ↓
application
  ↓
┌────────────┬────────────┬────────────┬────────────┐
document   chunking   retrieval   planning
    │           │           │           │
    └───────────┴───────────┴─────┬─────┘
                                   ▼
                              execution
                                   │
                                   ▼
                                 llm
```

Cache вызывается **только** на application-level. Output — на
application/output boundary.

### Запрещённые зависимости (обратное направление)

```text
document       → retrieval, execution, planning, application, llm, cache, output
chunking        → retrieval, execution, planning, application, llm, cache, output
retrieval       → execution, planning, application, llm, cache, output
planning        → execution, application, llm, cache, output
execution       → application, cache, output
```

`application` может импортировать всё. `llm` / `cache` / `output` —
leaves (не импортируют внутренние слои).

Импорты **вниз** (например, `document → execution`) запрещены — это
архитектурное нарушение. Тест `tests/architecture/test_layer_boundaries.py`
проверяет это правило через AST-обход всех `.py` под `scripts/`.

### Single-flight — единственное исключение

`execution.pipeline` импортирует `llm.single_flight.LLM_FLIGHT_LOCK` —
это **технический cross-cutting gate** (как `threading.Lock`), а не
domain-зависимость. Это явное исключение из общего правила «execution
не зависит от llm». Для остальных обращений к LLM
рекомендуется `llm/single_flight.py::guarded_chat` — public API,
используемый `llm.calls`.

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

* `execution.pipeline.process_context_batch` — map-phase
  (через прямой lock — для совместимости с mock-тестами).
* `llm.calls.chat_locked` — manual вызовы (back-compat).
* `llm.single_flight.guarded_chat` — рекомендуемый public API для
  нового кода.

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