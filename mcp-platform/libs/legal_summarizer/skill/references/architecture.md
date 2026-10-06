# Architecture — `legal_summarizer`

> Это **единственный** технический архитектурный документ Skill. Старые
> `ARCHITECTURE.md` / `ARCHITECTURE_V2.md` удалены как конфликтующие
> источники истины.

`legal_summarizer` — самодостаточный Agent Skill (Anthropic Skills
модель). Домен живёт в платформе, в
`mcp-platform/libs/legal_summarizer/`: entry-points (`cli.py`,
`cli_query.py`) в корне пакета и 9 runtime-слоёв — в его
подкаталогах. Payload Skill — инструкции (`SKILL.md`), этот
`README.md`, prompts (`skill/prompts/`) и подробные документы
(`skill/references/`) — лежит в подкаталоге `skill/`.
Developer-only тесты вынесены отдельно от домена, в
`mcp-platform/tests/legal_summarizer/`.

## Skill layout

```text
mcp-platform/libs/legal_summarizer/
├── cli.py                       # entry point
├── cli_query.py                 # follow-up по operation_id
├── __init__.py
│
├── application/                 # orchestration, idempotency, manifest
├── cache/                       # manifest, DocumentCache, session_key
├── chunking/                    # DocumentStructure → Chunk[]
├── document/                    # PhysicalDocument → DocumentStructure
│   └── extractors/              # pdf / docx / txt
├── execution/                   # direct / map-reduce / hierarchical
├── llm/                         # chat wrapper, prompts, single-flight
├── output/                      # JSON-форматирование
├── planning/                    # strategy selection
├── retrieval/                   # query normalization, lexical, fallback
│
└── skill/                       # payload Skill (этот файл лежит здесь)
    ├── SKILL.md                 # инструкция агенту
    ├── README.md                # developer overview
    ├── prompts/                 # LLM-инструкции
    │   ├── reduce_system.md
    │   ├── section_reduce_system.md
    │   └── summarize_system.md
    └── references/              # подробные документы (этот файл и др.)
        ├── architecture.md
        ├── contracts.md
        └── testing.md

mcp-platform/tests/legal_summarizer/       # developer-only код
├── unit/
├── integration/
└── architecture/
```

## `src/` отсутствует намеренно

Skill — **self-contained Agent Skill**, а не отдельный Python distribution
package.

Отдельного `src/` не нужно: домен — обычный пакет
`libs.legal_summarizer` внутри платформы, и корнем импорта служит
корень платформы. `cli.py` добавляет в `sys.path` `_PLATFORM_ROOT`
(`Path(__file__).resolve().parents[2]`, то есть `mcp-platform/`),
поэтому его можно запускать и путём к файлу из корня репозитория
агента: `python mcp-platform/libs/legal_summarizer/cli.py`.

У `cli_query.py` тот же корень (`_PROJECT_ROOT`) и дополнительно
`_SCRIPTS_ROOT` (каталог самого пакета). Блок настройки `sys.path` обязан стоять
**до** `from libs.legal_summarizer.cache import manifest`: иначе запуск по пути к
файлу падал с `ModuleNotFoundError` — путь ещё не был готов, а импорт уже шёл
(`cli_query.py:28` против `cli_query.py:36`). Сейчас обе формы работают: и
`python mcp-platform/libs/legal_summarizer/cli_query.py` из корня репозитория, и
`python -m libs.legal_summarizer.cli_query` из `mcp-platform/`.

## Главный поток

```text
Agent
  │
  ▼
SKILL.md
  │
  ▼
cli.py
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
проверяет это правило через AST-обход всех `.py` под
`mcp-platform/libs/legal_summarizer/`.

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

Cache разделён на **два независимых уровня** с разными владельцами,
разными layout'ами и разной ответственностью:

```text
DocumentCache
    │
    └── document-level cache
        (sessions/<safe_session_key>/documents/<document_id>/...)
        cross-operation identity, baseline summaries

cache.manifest
    │
    └── operation-level resume state
        (operations/<operation_id>/manifest.json + chunks/ + result.json)
        per-operation idempotency, partials, final result
```

### `DocumentCache` — единственный владелец document-level storage

`cache/document_cache.py::DocumentCache` (instance API:
`DocumentCache(workspace_root, session_key)`).

Импортируется в:

* `application.pipeline_structure` — cache hit/miss вокруг canonical pipeline.
* `application.service` — `_try_question_via_document_cache` cache boundary.
* `application.question_context` — загрузка chunk/section summaries для
  question synthesis.
* `application.execution_orchestration` — callback factory для
  `map_reduce` (per-chunk summaries в document cache).
* `cache.document_cache` сам.
* `tests/*` (contract tests) + `tests/architecture/test_document_cache_boundaries.py`.

Производственные модули **не** знают про cache paths, marker, snapshot
filenames или layout — это инкапсулировано в `DocumentCache`.

### `cache.manifest` — единственный владелец operation-level storage

`cache/manifest.py` содержит **только** operation-level API:
`load_manifest`, `save_manifest`, `manifest_path`, `manifest_root`,
`chunks_dir`, `chunk_result_path`, `write_chunk_result`, `read_chunk_result`,
`result_path`, `write_result`, `read_result`, `load_cached_partials`,
`NormalizedManifest`, `MANIFEST_VERSION_V2`.

Импортируется в:

* `application.execution_orchestration` — operation manifest lifecycle.
* `application.service` — idempotency check + resume.
* `application.manifest_builder` — построение `NormalizedManifest`.
* `document.physical` — `manifest_root` для operation-level physical cache.
* `cli_query.py` — read manifest/chunks по `operation_id`.
* `cache.manifest` сам.

Document-level symbols (`is_document_cache_complete`,
`read_document_snapshot`, `write_document_snapshot`, ...) **физически
удалены** из `cache.manifest` и не должны появляться в production.
Архитектурный guard: `tests/architecture/test_document_cache_boundaries.py`.

`execution.pipeline` **не** импортирует ни `cache.manifest`, ни
`cache.document_cache`: возвращает `(batch_meta, chunk_results)`,
application решает — записывать ли и куда.

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