# DocumentCache Refactoring — Этап 0: Inventory

**Дата создания:** начало refactoring.
**Назначение:** baseline для миграции document-level API в `DocumentCache`.
**Статус:** ✅ **ЗАВЕРШЕНО** — все этапы 1-19 выполнены, полный test suite зелёный (777 passed, 1 xfailed).

---

## Финальный статус

### Что сделано

1. ✅ Создан `scripts/cache/document_cache.py` с классом `DocumentCache(workspace_root, session_key)` (instance API).
2. ✅ Мигрированы production callers:
   - `scripts/application/pipeline_structure.py` (helper'ы `_try_load_cached_pipeline_result` + `_write_document_snapshot_after_pipeline` сохранены как тонкие cache-coordination helpers — TODO см. ниже).
   - `scripts/application/service.py` (cache-кусок `_try_question_via_document_cache`).
   - `scripts/application/question_context.py` (полная миграция на `DocumentCache`).
   - `scripts/application/execution_orchestration.py` (callbacks `write_chunk_summary`/`write_section_summary` → `DocumentCache`).
3. ✅ Мигрированы тесты document-level → `DocumentCache`:
   - `test_document_snapshot.py` (8/8 passed).
   - `test_document_cache_hit_miss.py` (6/6 passed).
   - `test_corrective_guards.py` (8/8 passed).
   - `test_section_summaries_persist.py` (4/4 passed).
   - `test_question_context.py` (9/9 passed).
   - `test_document_chunk_callback.py` (2/2 passed).
   - `test_question_via_document_cache.py` (6/6 passed).
   - `test_demo_workflow.py` (1/1 passed).
4. ✅ Удалены document-level API из `cache.manifest` (физическое удаление, не wrapper).
5. ✅ Создан AST guard `tests/architecture/test_document_cache_boundaries.py` (10/10 passed).
6. ✅ Обновлён `references/architecture.md` (новая секция "Cache — application boundary").

### Текущее состояние ownership

| Responsibility | Owner | Status |
| --- | --- | --- |
| Document-level snapshot read/write/invalidate | `DocumentCache` | ✅ единственный владелец |
| Document-level path layout (`sessions/<key>/documents/<doc_id>/...`) | `DocumentCache` | ✅ инкапсулировано |
| Document-level `_complete.marker` completeness | `DocumentCache` | ✅ |
| Document-level chunk/section summaries | `DocumentCache` | ✅ |
| Document-level atomic write через staging dir | `DocumentCache` | ✅ |
| Operation-level manifest | `cache.manifest` | ✅ единственный владелец |
| Operation-level chunks (`operations/<op_id>/chunks/*.json`) | `cache.manifest` | ✅ |
| Operation-level result.json | `cache.manifest` | ✅ |
| Operation-level partials (`load_cached_partials`) | `cache.manifest` | ✅ |

### Verification — финальный аудит

```
document-level legacy symbols в production (scripts/, исключая cache/) = 0
document-level legacy symbols в cache/document_cache.py                  = N/A (владелец)
document-level legacy symbols в cache/manifest.py                        = 0 (физически удалены)
document-level legacy symbols в tests/ (кроме имён тестовых функций)     = 0
document-level legacy symbols в references/                              = 0 (только в guard-документации)
test suite (legal_summarizer)                                            = 777 passed, 1 xfailed
AST guard                                                                = 10/10 passed
```

### НЕ выполнено (осознанные TODO)

- **Helper'ы `_try_load_cached_pipeline_result` + `_write_document_snapshot_after_pipeline` в `pipeline_structure.py`** — сохранены как тонкие cache-coordination helpers (не wrappers над `DocumentCache`). Они владеют **recovery в PipelineResult** (десериализация snapshot → in-memory объекты). Это отдельная ответственность, не cache storage protocol. Решение (inline / `application/snapshot_loader.py` / baseline) — отдельный refactor после стабилизации архитектуры.

- **`map_reduce.py`** — НЕ тронут (по явному правилу). Callback contract `WriteDocumentChunkSummaryFn` сохранён; реализация передаётся из `execution_orchestration.py` через `DocumentCache.write_chunk_summary`. Это dependency injection, не cache leak.

---

## Историческая секция (для traceability)

### 1. Document-level API (бывший scope миграции)

Изначальное местоположение: `scripts/cache/manifest.py`, секция «Document-level cache (cross-operation identity)».

Все следующие symbols были **физически удалены** из `cache.manifest`:

- `document_dir`, `document_physical_path`, `document_analysis_path`,
  `document_chunks_dir`, `document_chunk_result_path`,
  `document_section_result_path`, `_document_complete_marker_path`
- `is_document_cache_complete`, `read_document_snapshot`,
  `write_document_snapshot`, `invalidate_document_cache`
- `load_document_chunk_summaries`, `load_document_section_summaries`,
  `write_document_chunk_summary`, `write_document_section_summary`,
  `read_document_chunk_summary`, `read_document_section_summary`

Эти symbols **не должны** появляться в production коде или в новых тестах.
AST guard `tests/architecture/test_document_cache_boundaries.py` это контролирует.

### 2. Operation-level API (сохранён как был)

`cache.manifest` после рефакторинга содержит **только**:

- `NormalizedManifest`, `MANIFEST_VERSION_V2`
- `load_manifest`, `save_manifest`
- `manifest_path`, `manifest_root`, `chunks_dir`, `chunk_result_path`, `result_path`
- `write_chunk_result`, `read_chunk_result`, `write_result`, `read_result`
- `load_cached_partials`
- `skill_repo_root`
- приватные `_read_json`, `_atomic_write_json`

### 3. Production-импорты operation-level API (whitelisted)

- `scripts/application/service.py` — `load_manifest`, `read_result`, `save_manifest`, `write_result`, `NormalizedManifest`.
- `scripts/application/execution_orchestration.py` — `save_manifest`, `write_result`, `write_chunk_result`, `load_cached_partials`, `NormalizedManifest`.
- `scripts/application/manifest_builder.py` — `NormalizedManifest`.
- `scripts/document/physical.py` — `manifest_root` (для `_physical_cache_root`, operation-level).
- `scripts/cli_query.py` — `manifest_path`, `chunks_dir`, `_read_json`.

### 4. Existing legacy guard (не связан)

`tests/test_legal_summarizer_no_legacy.py::test_legacy_document_cache_removed` (lines 161-167) **требует отсутствия** `scripts/document_cache.py` (на верхнем уровне, не в подкаталоге `cache/`). Это ограничивает naming: **`DocumentCache` живёт в `scripts/cache/document_cache.py`**, не `scripts/document_cache.py`.

### 5. Conflicts and ambiguities (закрыты)

#### Закрыто: `_try_question_via_document_cache` в `service.py`

После прочтения (lines 77-294) функция содержала:
- cache-кусок (~30 строк) → мигрирован на `DocumentCache`.
- selection, question-context, LLM, idempotency write → оставлены в service.
- Функция остаётся в `service.py` (НЕ выделена в `question_shortcut.py`) — по решению «пока оставить».

#### Закрыто: `_physical_cache_root` в `document/physical.py`

Использует `manifest_root` (operation-level) для пути `manifest_root / "physical"`. Это **operation-level**, не document-level. Импорт `manifest_root` оставлен без изменений. `physical.py` не пишет в document-level cache.

#### Граничный случай: `cli_query.py`

Импортирует `from cache.manifest import _read_json, manifest_path` с `# type: ignore`. Это operation-level, не трогаем.

### 6. Architectural invariant (зафиксирован)

```text
scripts/cache/document_cache.py → DocumentCache → document-level cache
scripts/cache/manifest.py       → cache.manifest → operation-level resume state
```

- `DocumentCache` — единственный владелец: paths, marker, atomic write, snapshot read/write/invalidate, summary persistence.
- `cache.manifest` — единственный владелец: operation manifest, operation chunks, operation result, operation partials.
- Никакой production-модуль не знает про cache filesystem protocol.
- Никакого wrapper, alias, fallback, `getattr`, `try/except ImportError` для document-level API.
- AST guard (`tests/architecture/test_document_cache_boundaries.py`) обеспечивает invariant.

### 7. Документ

Этот файл **не удаляется**. Это audit trail рефакторинга — показывает исходное состояние, план, прогресс и финальный статус. Используется для traceability и для отслеживания решений TODO.

