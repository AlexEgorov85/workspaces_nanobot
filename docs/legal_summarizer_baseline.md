# legal_summarizer — Baseline (Этап 0)

Фиксируется перед началом рефакторинга по `PLAN.md` (см. `/PLAN.md`).
Цель документа — отличить **новые** failures от **существующих** при
последующих этапах.

Дата фиксации: 2026-09-04.
Окружение: Windows 10, Python 3.14, venv `C:\Users\Алексей\.nanobot\.venv`,
зависимости — из корневого `requirements.txt` (без дополнительных установок).

---

## 1. Текущий test suite

Команда: `pytest -q --tb=no` (полный прогон).

| Метрика | Значение |
|---|---:|
| Всего тестов | 2691 |
| Passed | 2672 |
| Failed | 5 |
| Skipped | 14 |
| Warnings | 2 |
| Время | ~8 мин 47 сек (527 сек) |

### 1.1. Существующие failed (pre-existing)

Все 5 failures существовали до старта рефакторинга. Это **baseline**, не
регрессия. Их описание зафиксировано ниже — чтобы новые failures можно было
отличить.

| # | Тест | Категория | Природа |
|---|---|---|---|
| F1 | `tests/test_architecture_tool_domain_free.py::TestToolDocstringsNoDomainLiterals::test_no_domain_tokens_in_docstrings[workspace\\tools\\run_predefined_script.py]` | Архитектура / TARGET §22.3 | В docstring `workspace/tools/run_predefined_script.py` встречается доменный токен `audit_analyzer`. Generic infrastructure не должен ссылаться на конкретные домены skill'ов. К рефакторингу `legal_summarizer` относится косвенно (только если новые модули нарушат ту же политику). |
| F2 | `tests/test_config_keys.py::TestProjectJsonShape::test_required_key_present_with_default[skills.legal_summarizer.cli.default_length-medium]` | Конфиг / `REQUIRED_KEYS` | В `project.json::skills.legal_summarizer.cli.default_length` записано `"brief"`, а ожидается дефолт `"medium"`. См. ниже §3 (Brief behavior). Требует синхронизации `REQUIRED_KEYS` ↔ `project.json` либо обновления дефолта. К рефакторингу относится напрямую (Этап 31). |
| F3 | `tests/test_e2e_600_page.py::test_600_page_executes_via_context_batching` | Performance / acceptance | Assertion: `map_calls < chunks_total` (8 < 8 — равенство). В текущем имплементации при 600-страничном документе каждый batch = один chunk → `map_calls == chunks_total`. Это **прямо связано** с проблемой «adjacent section packing» (Этап 22) и с оценкой DIRECT threshold (Этап 28). Считать pre-existing: в текущем коде упало до моих. |
| F4 | `tests/test_history_search_tool.py::test_search_current_session_filters_by_session` | Tool | **Flaky**: в полном прогоне упал (из-за pollution предыдущих тестов на сессионную область), в изоляции — `passed`. Не относится к рефакторингу `legal_summarizer`. |
| F5 | `tests/test_skill_legal_summarizer_characterization.py::test_brief_strategy_default_coverage_ratio` | Brief | Assertion: `brief_coverage_ratio == 0.5`, фактически `0.2`. Прямо относится к Этапу 31/63 (brief importance-aware sampling). Считать pre-existing. |

### 1.2. Skipped

14 тестов `pytest.skip` — без изменений.

### 1.3. Warnings (2, оба неблокирующие)

* `PytestRemovedIn10Warning` — class-scoped fixture как instance-method в
  `tests/test_skill_legal_summarizer.py::TestSkillMarkdownContract::test_cli_invocation_in_first_lines`.
  Deprecated, но не блокирует.
* `SyntaxWarning` — invalid `\s` escape sequence в
  `workspace/skills/legal_summarizer/scripts/document_cleanup.py:16`.
  Желательно исправить в рамках Этапа 78 (dead code).

---

## 2. CLI skill (контракт v0)

Зафиксирован по `python workspace/skills/legal_summarizer/scripts/cli.py --help`.

```text
usage: cli.py [-h] --file FILE [--length {brief,detailed}]
              [--question QUESTION] [--focus FOCUS] [--context CONTEXT]
              [--confirm] [--max-chunks MAX_CHUNKS] [--estimate-only]
              [--operation-id OPERATION_ID]
```

Параметры:

* `--file` (обязательный) — путь к `.pdf/.docx/.txt`.
* `--length` — `brief|detailed`, по умолчанию берётся из `project.json`.
* `--question` — конкретный вопрос (взаимоисключающе с `--length`).
* `--focus` — instruction для финального reduce.
* `--context` — контекст чата в JSON.
* `--confirm` — обязателен для длинных документов.
* `--max-chunks` — override `max_chunks_for_execution`.
* `--estimate-only` — только inspection + estimate (без LLM).
* `--operation-id` — explicit ID для resume.

Этот CLI **не должен сломаться** при рефакторинге (см. PLAN §1, §58).

---

## 3. Режимы работы (Phase 2B — Structure-Aware Context Batching)

| Режим | Что делает | Когда |
|---|---|---|
| `single` | Один LLM-вызов | `chars_in ≤ single_call_threshold` |
| `map_reduce_flat` | map → flat reduce | default для medium docs |
| `map_reduce_hierarchical` | map → hierarchical reduce (multi-round) | `meaningful_sections >= 3` |

Примечание: в текущей `tests/test_e2e_600_page.py` уже зафиксировано, что при
600-страничном документе стратегия даёт `map_calls == chunks_total` — это
**baseline-проблема**, которую должен решить Этап 22 (adjacent packing) +
Этап 28 (DIRECT threshold benchmark).

---

## 4. Существующая структура кода (для аудита в Этапе 1)

`workspace/skills/legal_summarizer/scripts/`:

| Модуль | Строк | Назначение |
|---|---:|---|
| `summarizer.py` | 1773 | **Главный оркестратор** pipeline. Содержит иерархический reduce, packing, LLM-trim, map, reducer selection — несколько responsibilities в одном файле. Кандидат на разбиение (Этап 59). |
| `cli.py` | 405 | CLI entry-point + confirmation flow. |
| `cli_query.py` | 251 | Follow-up запросы к сохранённой `operation_id`. |
| `manifest.py` | 340 | Manifest v2 (chunk_states, batches, sections). |
| `prompts.py` | 211 | Промпты для map/reduce/section_reduce. |
| `pipeline.py` | 169 | (Заглушка / re-export — уточнить в Этапе 1). |
| `reducer_impl.py` | 284 | Hierarchical reduce implementation. |
| `context_expansion.py` | 275 | Расширение контекста для question-mode. |
| `cached_retrieval.py` | 208 | Кэшированный retrieval для question. |
| `cache_followup.py` | 217 | Follow-up cache. |
| `brief_strategy.py` | 160 | Стратегия brief selection. |
| `brief_representation.py` | 180 | Форматирование brief. |
| `document_cache.py` | 200 | Document cache (L0?). |
| `document_cleanup.py` | 191 | Header/footer classification (см. F5-warning). |
| `document_stats.py` | 127 | Документные метрики. |
| `execution_strategy.py` | 81 | Выбор single/map_reduce. |
| `packing.py` | 50 | Public API packing. |
| `packing_impl.py` | 213 | Реализация packing. |
| `packing_models.py` | 84 | Модели packing. |
| `reducer.py` | 59 | Public API reducer. |
| `reducer_strategy.py` | 103 | Выбор reducer. |
| `reducer_models.py` | 94 | Модели reducer. |
| `token_budget.py` | 139 | Token budget. |
| `manifest.py`, `prompts_runtime.py`, `llm.py`, `llm_calls.py` | разн. | LLM-клиент и runtime. |
| `fingerprint.py` | 107 | Document fingerprint. |
| `provenance_reconstruction.py` | 123 | Reconstruction provenance для tool. |
| `sanitize.py`, `output.py`, `skill_config.py` | разн. | Утилиты. |

`workspace/skills/legal_summarizer/scripts/structure/`:

| Модуль | Строк | Назначение |
|---|---:|---|
| `chunks.py` | 555 | StructureAwareChunker. |
| `heading.py` | 513 | Heading candidate detector + evidence scoring. |
| `physical.py` | 509 | Adapter над `office_files` + per-block coords. |
| `sections.py` | 222 | Section detection (DOCX Heading, PDF outline, regex). |
| `tree.py` | 231 | SectionTree + sibling-numbering. |
| `list_detection.py` | 201 | List-vs-heading detection. |
| `__init__.py` | 11 | Re-exports. |

Суммарно: ~7500 строк в `scripts/` (включая `summarizer.py` на 1773).

---

## 5. Известные архитектурные проблемы (pre-existing, для Этапа 1)

По коду и `SKILL.md`/`ARCHITECTURE.md` (см. ниже) уже видны следующие
слабые места. Они будут детально разобраны в Этапе 1 (аудит), здесь только
краткая фиксация, чтобы baseline знал, от чего отталкиваемся:

1. **`summarizer.py` на 1773 строки** — несколько responsibilities, плохая
   cohesion. Цель Этапов 23, 24, 59.
2. **PDF outline mapping** — кандидаты с `block_index = -1` отбрасываются
   `build_section_tree()` → outline фактически не работает. Этап 11.
3. **Sibling numbering** — глобальный `path_counter_by_level` вместо
   локального относительно parent. Этап 13.
4. **`summarizer._hierarchical_reduce_rounds()` + `reducer_impl.py`** —
   две реализации reduce. Этап 24.
5. **Token estimation** разная в chunking/packing/execution/reduce. Этап 20.
6. **`index()` linear lookup** в `context_expansion.py`. Этап 44.
7. **Brief coverage_ratio = 0.2** при тесте на 0.5. Этапы 31, 32.
8. **600-страничный документ = `map_calls == chunks_total`** без adjacent
   packing. Этап 22.

Все эти проблемы **pre-existing**, не регрессии моих изменений.

---

## 6. Архитектурная документация (текущее состояние)

* `workspace/skills/legal_summarizer/SKILL.md` (517 строк) — внешний
  контракт skill. Краткая pipeline-схема в нём есть (строки 414–441),
  но она уже частично устаревшая (см. Этап 47, 79).
* `workspace/skills/legal_summarizer/ARCHITECTURE.md` — существует,
  в нём зафиксированы invariants. По PLAN §48 ожидается расхождение
  между заявленным и фактическим числом invariants. Уточнить в Этапе 1.
* `docs/ARCHITECTURE.md` — корневой документ об архитектуре проекта.
  См. раздел «legal_summarizer — внутренняя структура» (ссылка из SKILL.md).
* `docs/TARGET_ARCHITECTURE.md` — нормативный контракт. Источник правил.
* `docs/refactor_baseline.md` — существующий wip-заметки рефакторингов.

---

## 7. Тесты по подсистемам

Прямые тесты legal_summarizer (найдены по имени файла/модуля):

| Тест-файл | Назначение |
|---|---|
| `tests/test_skill_legal_summarizer.py` | Smoke: SKILL.md контракт, CLI invocation, basic run. |
| `tests/test_skill_legal_summarizer_characterization.py` | Characterization tests (9+ штук). |
| `tests/test_legal_summarizer_brief_budget.py` | Brief-budget. |
| `tests/test_legal_summarizer_empty_reduce.py` | Empty-reduce edge case. |
| `tests/test_legal_summarizer_incident_integration.py` | Инцидент-регрессия. |
| `tests/test_legal_summarizer_pr2.py` | PR2 acceptance. |
| `tests/test_legal_summarizer_running_subprocess.py` | Running subprocess. |
| `tests/test_legal_summarizer_single_flight.py` | Single-flight LLM invariant. |
| `tests/test_structure_chunks.py` | Structure chunks. |
| `tests/test_structure_physical.py` | Physical extraction. |
| `tests/test_structure_sections.py` | Section detection. |
| `tests/test_packing.py` | Context packing. |
| `tests/test_reducer.py` | Hierarchical/flat reducer. |
| `tests/test_resume_scenarios.py` | Resume + 7 scenarios. |
| `tests/test_tables.py` | Atomic tables (9). |
| `tests/test_information_preservation.py` | Info-preservation (10). |
| `tests/test_skill_tool_independence.py` | Skills не импортируют tools. |
| `tests/test_skill_tool_integration.py` | Skills↔Tools интеграция. |
| `tests/test_resource_universality.py` | Resource universality. |
| `tests/test_parallel_modes.py` | Параллельные режимы. |
| `tests/test_single_mode_audit.py` | Single-mode audit. |
| `tests/test_e2e_600_page.py` | 600-page e2e. |
| `tests/contract/*` (раздел) | Контрактные тесты nanobot 0.3.0. |

---

## 8. Benchmarks

`benchmarks/` (содержит `runner.py`, `evaluator.py`, `scorer.py`,
`reporter.py`, `loader.py`, `hooks.py`, `db.py`, `items/`, `results/`,
`.benchmarks/`).

Используется `python benchmarks/runner.py --tags simple` для smoke-прогона
(см. Release Process в корневом `AGENTS.md`).

Бенчмарк-документы для legal_summarizer — найти в `benchmarks/items/`.
Финальный benchmark для DIRECT threshold — план в Этапе 28 / 51 / 52.

---

## 9. Acceptance matrix (из SKILL.md — фиксируется как reference)

| Сценарий | LLM-вызовы | Acceptance |
|---|---|---|
| Small doc (≤12000 chars, single) | 1 | ≤ 1 ✅ |
| Medium doc (default, no opt-in) | 2 | ≥ 1 ✅ |
| Large doc (default) | 17 | ≥ 2 ✅ |
| Medium doc (opt-in DIRECT) | **1** | ≤ baseline (2) ✅ |
| Quality (small, honest mock) | 100% facts | ≥ 80% ✅ |

Текущее состояние этих acceptance — **baseline**, не регрессия. Все
изменения после рефакторинга должны улучшать или сохранять их.

---

## 10. Definition of done для baseline

Baseline считается зафиксированным, когда выполнены все пункты:

- [x] Полный прогон тестов выполнен и записан.
- [x] Все 5 failures описаны с природой (pre-existing vs my-regression).
- [x] CLI контракт зафиксирован.
- [x] Существующие режимы (single/map_reduce_flat/map_reduce_hierarchical) зафиксированы.
- [x] Карта модулей (LOC + назначение) составлена.
- [x] Список pre-existing архитектурных проблем (8 пунктов) зафиксирован.
- [x] Существующая документация (`SKILL.md`, `ARCHITECTURE.md`) упомянута.

Следующий этап — **Этап 1 (аудит кода)** — начинается с детального
разбора модулей по §7 плана и составления карты
parsing/structure/chunking/packing/reduce/retrieval.