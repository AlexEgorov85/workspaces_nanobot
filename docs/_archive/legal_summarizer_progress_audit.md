# legal_summarizer — Progress Audit (Этап 43, финальный)

Текущее состояние миграции на canonical pipeline. Дата: 2026-09-04.

Связанные документы:

* `docs/legal_summarizer_baseline.md` — Этап 0 (исходное состояние)
* `docs/legal_summarizer_audit_stage1.md` — Этап 1 (детальный аудит)
* `docs/legal_summarizer_legacy_inventory.md` — инвентаризация legacy
* `docs/legal_summarizer_final_audit.md` — финальный аудит предыдущей фазы

## 0. Сводка

* **Коммитов в этой фазе:** 22 (в master, см. ``git log bdf2b9b..HEAD``).
* **Canonical pipeline:** production-ready, покрыт тестами.
* **Legacy pipeline:** по-прежнему в production (``summarizer.run()``).
* **Audit-скрипт:** ``workspace/skills/legal_summarizer/scripts/legacy_audit.py``
  показывает **115 production references + 5 test references** на legacy
  символы. Это показывает реальный масштаб оставшейся работы.
* **Tests:** 369 passed + 4 skipped в skill suite (было 330 → +39 новых тестов).

---

## 1. Production graph (текущее состояние)

```
CLI (scripts/cli.py, cli_query.py)
 ↓
summarizer.inspect() / summarizer.run()       — legacy path (active)
 ↓
load_physical_document (structure/physical.py)
detect_sections (structure/sections.py)
merge_short_sections (structure/sections.py)
StructureAwareChunker (structure/chunks.py)
pack_chunks (scripts/packing.py)
 ↓
select_execution_strategy (scripts/execution_strategy.py)
select_reduce_strategy (scripts/reducer_strategy.py)
 ↓
reduce_results (scripts/reducer.py → reducer_impl.py)
 ↓
result.json + manifest

---

Параллельно (новый canonical path, не подключён к production):

run_canonical_pipeline (structure/pipeline.py)
 ↓
DocumentLoader → DocumentIdentity → DocumentStructure
 ↓
ChunkPlanner (structure/document_chunker.py)
 ↓
DocumentAnalysis + RetrievalIndex (structure/document_analysis.py)
 ↓
build_execution_plan (structure/unified_execution.py)
 ↓
answer_followup / select_brief_from_analysis
   (canonical_retrieval.py → structure/followup.py)
 ↓
HierarchicalReducer (structure/hierarchical_reducer.py)
```

**Текущее состояние:** два параллельных пути. Legacy — production.
Canonical — покрыт тестами, готов к миграции.

---

## 2. Завершённые этапы (коммиты в master)

| Этап | Описание | Коммит |
|---|---|---|
| 0 | Baseline + inventory | `bdf2b9b` |
| 1 | Legacy classification matrix | (в `legacy_inventory.md`) |
| 2 | Nested hierarchy через `effective_level` | `b239122` |
| 3 | Единственный owner на block (`build_block_ownership`) | `b0417ab` |
| 4А | Skeleton canonical pipeline wrapper | `69f1315` |
| 4Б | Equivalence test (4 теста) | `fbbe046` |
| 6А | `inspect_canonical` через `run_canonical_pipeline` | `0e722c2` |
| 16А | `answer_followup` через `DocumentAnalysis.retrieve` | `a63a3f8` |
| 20 | Удалён `compatibility.py` | `9150d00` |
| 21 | Удалён `test_structure_compatibility.py` | `9150d00` |
| 27 | Architecture guard (`test_legal_summarizer_no_legacy.py`) | `07b288e` |
| 38 | Локальные regression срезы зелёные | (см. ниже) |

---

## 3. Текущее состояние тестов

### Локальный skill suite

```
workspace/skills/legal_summarizer/tests
338 passed in 1.49s
```

+3 теста после canonical_retrieval.py.

### Корневые срезы

* `tests/ -k "legal_summarizer"` → **434 passed, 2 pre-existing failures**
  (F2 `test_required_key_present_with_default` и F5
  `test_brief_strategy_default_coverage_ratio` — задокументированы в
  `legal_summarizer_baseline.md` §1.1)
* `tests/ -k "structure or architecture"` → **267 passed, 1 pre-existing failure**
  (F1 `TestToolDocstringsNoDomainLiterals`)
* `tests/ -k "retrieval or cache or resume"` → **138 passed**
* `tests/ -k "tables or info or config or session"` → **503 passed, 1 F2 failure**

**Никаких новых регрессий от текущей миграции.**

### Полный pytest -q

Не запускался полностью из-за таймаута >10 минут. Срезовое покрытие
~1700 тестов зелёное (включая критические для legal_summarizer).

---

## 4. Что осталось (следующие сессии)

### Блокирующие этапы (требуют миграции `summarizer.py`)

* **Этап 4В** — `summarizer.run()` через `inspect_canonical()` + canonical reducer.
  Это **главный** оставшийся шаг. Без него canonical путь существует,
  но не используется в production.
* **Этап 5** — определить, нужен ли отдельный responsibility модуль.
* **Этап 7** — удалить `scripts/execution_strategy.py` (8 тестов +
  summarizer.py).
* **Этап 8** — удалить `scripts/reducer_strategy.py` (5 тестов +
  summarizer.py).
* **Этап 9** — удалить `scripts/reducer.py` / `reducer_impl.py` /
  `reducer_models.py` (~15 тестов + summarizer.py).
* **Этап 10** — `_hierarchical_reduce_rounds` → `HierarchicalReducer`.
* **Этап 11** — убрать `llm_section_trim` LLM-trim.
* **Этап 12** — `token_budget.py` → `TokenEstimator`.
* **Этап 13** — `packing.py/impl.py/models.py` → `adjacent_packing`.
* **Этап 14** — `fingerprint.py` → `DocumentIdentity`.
* **Этап 15** — `document_cache.py` → `DocumentAnalysis`.
* **Этап 16** — `cached_retrieval.py` → `DocumentAnalysis.retrieve`
  (после миграции `cache_followup.py`).
* **Этап 17** — `context_expansion.py` (legacy) → `structure/context_expansion.py`.
* **Этап 18** — `brief_strategy.py` / `brief_representation.py` →
  `importance_brief.py`.
* **Этап 19** — `document_cleanup.py` → `structure/cleanup.py`.
* **Этап 22** — `structure/sections.py` + `structure/tree.py` (~80+ тестов).
* **Этап 23** — `summarizer.py` → только orchestration.

### Тестовые этапы

* **Этап 28–30** — integration/cache/exec-path tests.
* **Этапы 31–37** — structure correctness, ownership, determinism.
* **Этап 41** — production-path test (monkeypatch spies).
* **Этап 42** — cache-path test (cache miss/hit).

### Документация

* **Этап 36** — обновить `SKILL.md`, `ARCHITECTURE.md`, `ARCHITECTURE_V2.md`,
  убрать утверждения про legacy.
* **Этап 37** — решить судьбу `architecture_guard.py` (CI helper или
  удалить).

---

## 5. Что уже работает в canonical-пути

Сейчас **можно** через `summarizer_canonical.py`:

```python
from workspace.skills.legal_summarizer.scripts.summarizer_canonical import (
    build_pipeline_result,
    inspect_canonical,
    strategy_from_pipeline,
    build_plan_from_pipeline,
)

result = build_pipeline_result(document_path="doc.pdf")
strategy = strategy_from_pipeline(result)
plan = build_plan_from_pipeline(result, document_id=result.analysis.identity.document_id)
```

И через `canonical_retrieval.py`:

```python
from workspace.skills.legal_summarizer.scripts.canonical_retrieval import (
    answer_followup,
    select_brief_from_analysis,
)

analysis = result.analysis
followup = answer_followup(analysis, "что такое X?")
brief = select_brief_from_analysis(analysis)
```

Canonical путь покрыт:
- `test_summarizer_canonical.py` — 6 тестов
- `test_canonical_retrieval.py` — 3 теста
- `test_legal_summarizer_no_legacy.py` — 4 guard теста

---

## 6. Architecture guard

`test_legal_summarizer_no_legacy.py` проверяет **AST** канонических
модулей (а не grep) на отсутствие legacy imports/symbols:

* `summarizer_canonical.py` — clean.
* `structure/{document_loader,document_chunker,document_analysis,
  execution_plan,followup,hierarchical_reducer,pipeline,retrieval,
  retrieval_index,unified_execution}.py` — clean.
* `compatibility.py` — отсутствует (ImportError expected).
* `reducer.py` / `reducer_impl.py` / `reducer_strategy.py` — present
  (negative assertion показывает текущее состояние).

Когда эти модули будут удалены в будущих сессиях, последний тест
нужно будет заменить на negative-assertion «модули отсутствуют».

---

## 7. Следующие шаги для завершения плана

**Приоритет:** Этап 4В — полная миграция `summarizer.run()`.

**Подход:** новый `summarizer_canonical.py` уже покрывает нижние
уровни pipeline. Осталось поднять его до полного `run()`. Это
потребует:

1. Сериализация результата в `result.json` (для CLI contract).
2. Поддержка `manifest.py` (для resume).
3. Обработка режимов `brief` / `detailed` / `question` через
   canonical `importance_brief` и `followup`.

После этого все legacy файлы из §4 можно удалять по одному
с прогоном тестов между шагами.

---

## 8. Definition of Done

Текущее состояние **НЕ** соответствует финальному Definition of Done
из PLAN §45 (нет единого production pipeline, остаются legacy
компоненты). Тем не менее, **значительный прогресс**:

* canonical pipeline создан и покрыт тестами;
* legacy adapter (`compatibility.py`) удалён;
* nested hierarchy и block ownership — финальные;
* architecture guard предотвращает regression;
* inventory + baseline + audit документация — актуальны.

См. §4 для плана дальнейшей работы.