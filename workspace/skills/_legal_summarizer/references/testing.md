# Testing — `legal_summarizer`

Тестовая инфраструктура Skill. Цель — **596 passed, 4 skipped, 0 failed**
после архитектурного рефакторинга (baseline зафиксирован в
`docs/CHANGELOG.md` и `references/architecture.md`).

## Структура

```text
tests/
├── unit/                 # изолированные тесты подсистем
│   ├── cache/
│   ├── chunking/
│   ├── document/
│   ├── domain/
│   ├── execution/
│   ├── llm/
│   ├── output/
│   ├── planning/
│   └── retrieval/
├── integration/          # E2E прогон с mock LLM
├── architecture/         # boundary guards
├── test_*.py              # characterization tests (история фич)
└── test_structure_*.py   # структурные тесты
```

## Как запускать

```bash
cd workspace/skills/legal_summarizer
python -m pytest -q                  # все тесты
python -m pytest -q -k single_flight # single-flight subset
python -m pytest tests/architecture  # boundary guards
```

## Mock LLM

`tests/conftest.py` (через `_fake_llm` и `monkeypatch.setattr`) подменяет
`llm.client.chat` на синхронный mock, который:

* Понимает regex `DOCUMENT CHUNK \d+` в user message → возвращает
  N кратких саммари (для batch).
* Понимает «Объединённые краткие описания частей раздела» → возвращает
  финальное section summary.
* По умолчанию возвращает финальное document summary.

## Single-flight invariant

Тесты `test_single_flight.py`, `test_single_flight_concurrent.py` и
`test_single_flight_concurrent_safety.py` проверяют, что
`max_active_llm_calls == 1`:

* `test_concurrent_runs_peak_is_one` — два параллельных `run()` в разных
  потоках → `peak <= 1`.
* `test_lock_finally_releases` — исключение в LLM не удерживает lock.
* `test_retry_after_exception_peak_is_one` — retry-path сохраняет
  single-flight.

Реализация: единый `LLM_FLIGHT_LOCK` в `llm/single_flight.py`. Используется
в `execution.pipeline.process_context_batch` (map) и в `chat_locked`
(manual).

## Cache semantics

Тесты `test_idempotency_no_reexecution.py` и
`test_resume.py` проверяют:

* первый run создаёт cache;
* второй run с тем же `operation_id` использует cache;
* resume продолжает с первого pending chunk;
* `operation_id` не меняется между runs с теми же параметрами;
* изменение текста → новый `operation_id`.

## Architecture tests

`tests/architecture/test_layer_boundaries.py` — AST-обход каждого
`.py` под `legal_summarizer/` и проверка, что нет импортов между
запрещёнными слоями.

Правила:

| Layer | Запрещено импортировать |
| --- | --- |
| `document` | `retrieval`, `execution`, `llm`, `planning` |
| `chunking` | `execution`, `llm` |
| `retrieval` | `execution`, `llm` |
| `planning` | `llm` |
| `execution` | `document`, `retrieval`, `llm` |

`application`, `cache`, `llm`, `output` — leaves.

## Estimator consistency

Тесты `test_estimated_actual.py` и `test_estimate_*.py` проверяют:

```text
actual_llm_calls <= estimated_llm_calls
```

для:

* 1 section
* 2 sections
* 3 sections
* 4 sections
* 5 sections
* 9 sections
* 10 sections
* max rounds

Estimator (`application/estimation.py`) и actual reducer
(`execution/hierarchical.py`) используют один источник:
`execution/config.py::MID_REDUCE_GROUP_SIZE` и `MAX_REDUCE_ROUNDS`.

## Smoke-тесты CLI

```bash
python scripts/cli.py --help
python scripts/cli_query.py --operation-id <id> --field stats
```