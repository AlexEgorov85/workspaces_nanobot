# Work brief `12-skill-legal-rest`

Product files: **31**, LOC: **3941**

## `workspace/skills/legal_summarizer/scripts/cli.py` — 443 LOC (code 327)
- module: `workspace.skills.legal_summarizer.scripts.cli`
- docstring: Точка входа: CLI с разбором аргументов и запуском суммаризации. Phase 2B API: ``summarizer.run(text, ...)`` возвращает dict со статусом ``completed`` / ``confirmation_required`` / ``requires_continuation`` / ``failed``. 
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: `tests/test_application_context.py`, `tests/test_application_context_logging.py`, `tests/test_application_context_single_application_point.py`, `tests/test_audit_analyzer_cli.py`, `tests/test_cli_agent.py`, `tests/test_gateway.py`, `tests/test_project_settings.py`, `workspace/skills/legal_summarizer/tests/test_skill_legal_summarizer.py`
- classes: 0, module functions: 8

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_setup_stdout_encoding` | 56-77 | `() -> None` | 3 | 1 | Переключить stdout на UTF-8 для кросс-платформенного вывода. В Windows по умолчанию stdout использует ``cp1251 |
| `_build_parser` | 80-144 | `() -> argparse.ArgumentParser` | 1 | 4 | — |
| `_emit` | 147-172 | `(payload: dict) -> None` | 2 | 3 | Напечатать JSON-payload в stdout и физически отправить его наружу. ``flush=True`` обязателен: внешний ``exec`` |
| `_emit_done` | 184-193 | `(payload: dict) -> None` | 1 | 1 | Напечатать финальный результат, затем sentinel завершения в stdout. Оба вызова физически отправляются наружу ( |
| `_emit_running_marker` | 196-247 | `(text: str) -> None` | 8 | 1 | Маркер старта длинного прогона — печатается ПЕРВЫМ в stdout. Агент при ``exec`` (или первом ``write_stdin``) в |
| `_error` | 250-258 | `(status_message: str, exc: Exception | None=None) -> dict` | 3 | 4 | — |
| `_ensure_registered` | 261-292 | `() -> None` | 4 | 2 | Зарегистрировать legal_summarizer и runtime-инфраструктуру в ``table_registry``. Standalone CLI не имеет ``App |
| `main` | 295-439 | `() -> None` | 17 | 32 | — |

## `workspace/skills/legal_summarizer/scripts/cli_query.py` — 330 LOC (code 284)
- module: `workspace.skills.legal_summarizer.scripts.cli_query`
- docstring: ``cli_query.py`` — follow-up-запросы по сохранённой operation_id. Нужен, чтобы агент мог отвечать на уточняющие вопросы по документу ("сколько статей?", "какие разделы?", "что в чанке 12?") **без перепарсинга PDF** через
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 9

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_build_parser` | 37-74 | `() -> argparse.ArgumentParser` | 1 | 4 | — |
| `_emit` | 77-79 | `(payload: dict) -> None` | 1 | 3 | Печатает JSON в UTF-8 (не sentinel — это короткий запрос). |
| `_resolve_workspace_root` | 82-87 | `(arg: str | None) -> Path` | 2 | 2 | Кросс-платформенный путь к корню репо. |
| `_load_manifest_with_diagnosis` | 97-148 | `(operation_id: str, workspace_root: Path) -> dict[str, Any] | None` | 3 | 1 | Прочитать manifest и вернуть доменную ошибку, если недоступен. Отличается от :func:`_load_manifest_or_none` те |
| `_manifest_error_message` | 151-180 | `(reason: str, operation_id: str, diag: dict[str, Any]) -> str` | 6 | 1 | Сформировать человекочитаемое сообщение для manifest-ошибки. |
| `_load_chunk_summaries` | 183-212 | `(operation_id: str, workspace_root: Path, *, max_summary_chars: int) -> list[dict[str, Any` | 13 | 1 | Прочитать per-chunk файлы; обрезать summary до ``max_summary_chars``. |
| `_build_sections_tree` | 215-229 | `(manifest: dict[str, Any]) -> list[dict[str, Any]]` | 10 | 1 | Плоский список секций с section_path + heading. |
| `_field_stats` | 232-254 | `(manifest: dict[str, Any]) -> dict[str, Any]` | 16 | 1 | Основные метрики для follow-up'ов (включая article_count). Возвращаемые ключи НЕ пересекаются с ``status``/``f |
| `main` | 257-326 | `() -> int` | 22 | 32 | — |

## `workspace/skills/legal_summarizer/scripts/retrieval/context_expansion.py` — 196 LOC (code 160)
- module: `workspace.skills.legal_summarizer.scripts.retrieval.context_expansion`
- docstring: Semantic context expansion. Для выбранного chunk'а вернуть расширенный контекст через поиск **neighbours по target index**: target ↓ immediate previous chunk immediate next chunk ↓ same subsection restriction ↓ parent he
- static importers (1): `workspace/skills/legal_summarizer/scripts/retrieval/followup.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 2, module functions: 3

### class `ContextExpansionConfig` — lines 44-61 (18 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Параметры context expansion. Attributes: max_neighbour_blocks: максимум neighbours (default 2 — один слева, один справа). max_context_tokens: token budget для neighbours (не считая target). include_same_subsection: если 
- name referenced in 3 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `ExpandedContext` — lines 65-73 (9 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Результат context expansion.
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_section_for_chunk` | 76-77 | `(chunk: Chunk) -> str` | 2 | 1 | — |
| `_can_use_neighbour` | 80-91 | `(candidate: Chunk, target: Chunk, cfg: ContextExpansionConfig) -> bool` | 2 | 1 | Можно ли использовать ``candidate`` как neighbour для ``target``. Если ``include_same_subsection`` — только из |
| `expand_context` | 94-189 | `(target: Chunk, chunks: tuple[Chunk, ...], struct: DocumentStructure, *, config: ContextEx` | 25 | 2 | Расширить контекст для ``target``. Алгоритм: 1. ``target_idx = index of target in sorted(chunks)``; 2. Поочерё |

## `workspace/skills/legal_summarizer/scripts/planning/plan.py` — 194 LOC (code 165)
- module: `workspace.skills.legal_summarizer.scripts.planning.plan`
- docstring: ExecutionPlan. Immutable план выполнения, который объединяет результаты chunks, batches и token estimation. Используется всеми downstream'ами: * **inspect** — оценить работу (без LLM). * **run** — выполнить все батчи. * 
- static importers (6): `workspace/skills/legal_summarizer/scripts/application/canonical.py`, `workspace/skills/legal_summarizer/scripts/application/context_builder.py`, `workspace/skills/legal_summarizer/scripts/application/estimation.py`, `workspace/skills/legal_summarizer/scripts/application/execution_orchestration.py`, `workspace/skills/legal_summarizer/scripts/execution/map_reduce.py`, `workspace/skills/legal_summarizer/scripts/planning/strategy.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 2, module functions: 3

### class `PlannedBatch` — lines 27-52 (26 LOC), 1 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Один batch в ``ExecutionPlan``. Attributes: batch_id: стабильный идентификатор (``"cb_000"``). chunk_ids: tuple of ``Chunk.chunk_id`` (порядок — document order). token_estimate: оценка токенов через ``TokenEstimator``. s
- name referenced in 5 file(s); tests: 4

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `to_dict` | 45-52 | `(self) -> dict[str, Any]` | 1 | 0 | 24 | — |

### class `ExecutionPlan` — lines 56-101 (46 LOC), 2 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Immutable план выполнения документа. Attributes: document_id: ``DocumentIdentity.document_id``. strategy: ``"direct"`` | ``"map_flat"`` | ``"map_hierarchical"``. chunks: tuple of ``Chunk`` (в document order). batches: tu
- name referenced in 10 file(s); tests: 3

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `get_batch` | 83-87 | `(self, batch_id: str) -> PlannedBatch | None` | 3 | 0 | 1 | — |
| `to_dict` | 89-101 | `(self) -> dict[str, Any]` | 3 | 0 | 24 | — |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_make_batch_id` | 104-105 | `(idx: int) -> str` | 1 | 1 | — |
| `build_direct_plan` | 108-136 | `(chunks: tuple[Chunk, ...], *, document_id: str, token_estimator, estimated_llm_calls: int` | 5 | 2 | Построить ``ExecutionPlan`` для DIRECT стратегии (single call). |
| `build_map_plan` | 139-186 | `(chunks: tuple[Chunk, ...], *, document_id: str, strategy: str, batches_input: list[tuple[` | 9 | 2 | Построить ``ExecutionPlan`` для MAP_FLAT / MAP_HIERARCHICAL. Args: batches_input: список tuple chunk_ids (поря |

## `workspace/skills/legal_summarizer/scripts/llm/calls.py` — 187 LOC (code 161)
- module: `workspace.skills.legal_summarizer.scripts.llm.calls`
- docstring: LLM-call wrappers: низкоуровневые обёртки для LLM (map batch / section reduce / document reduce), плюс утилитарная ``doc_context``. NOTE: legacy импорт ``ContextBatch`` удалён. ``llm_batch`` теперь принимает ``list[Chunk
- static importers (4): `workspace/skills/legal_summarizer/scripts/application/execution_orchestration.py`, `workspace/skills/legal_summarizer/scripts/application/service.py`, `workspace/skills/legal_summarizer/scripts/execution/map_reduce.py`, `workspace/skills/legal_summarizer/scripts/execution/pipeline.py`
- string/dynamic refs: 4
- test files touching it: — none —
- classes: 0, module functions: 5

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `chat_locked` | 34-40 | `(messages, *, context=None) -> str` | 1 | 2 | Сериализованный ``llm.chat`` через единый ``guarded_chat`` API. Back-compat public API: старый код вызывает `` |
| `doc_context` | 43-65 | `(structure: DocumentStructure | None, *, with_begin_end: bool=False) -> str` | 7 | 1 | Сформировать doc-context (title + опционально begin/end) для user_body. Принимает canonical ``DocumentStructur |
| `llm_batch` | 68-100 | `(chunks: Iterable[Chunk], *, chunks_total: int, structure: DocumentStructure | None, lengt` | 2 | 3 | Сгруппировать LLM вызов для батча Chunk'ов. Возвращает dict[chunk_id, summary]. Single-flight: lock берётся ** |
| `llm_section_reduce` | 103-139 | `(section_path: str, section_heading: str, joined_text: str, *, length: str, question: str ` | 1 | 1 | Per-section reduce: объединить partials в финальную section_summary. Single-flight: см. ``llm_batch``. ``max_t |
| `llm_document_reduce` | 142-178 | `(section_summaries_text: str, *, length: str, focus: str | None, structure: DocumentStruct` | 3 | 4 | Document-level reduce: объединить section_summaries в финальный документ. Single-flight: см. ``llm_batch``. `` |

## `workspace/skills/legal_summarizer/scripts/retrieval/followup.py` — 169 LOC (code 139)
- module: `workspace.skills.legal_summarizer.scripts.retrieval.followup`
- docstring: First-run vs Follow-up split. Архитектурное разделение: * **First-run**: file → parse → structure → chunk → semantic map → cache. Долго (LLM-вызовы). Результат — ``DocumentAnalysis``. * **Follow-up**: cached analysis → r
- static importers (2): `workspace/skills/legal_summarizer/scripts/retrieval/canonical.py`, `workspace/skills/legal_summarizer/scripts/retrieval/question.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 2, module functions: 2

### class `FollowupConfig` — lines 49-54 (6 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Параметры follow-up запроса (режим ``question``).
- name referenced in 3 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `FollowupResult` — lines 58-74 (17 LOC), 1 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Результат follow-up запроса.
- name referenced in 3 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `to_dict` | 67-74 | `(self) -> dict[str, Any]` | 1 | 0 | 24 | — |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `build_first_run_analysis` | 77-86 | `(*, analysis: DocumentAnalysis) -> DocumentAnalysis` | 1 | 1 | First-run: возвращает готовый ``DocumentAnalysis``. Этот wrapper нужен для явной семантики в коде — отличить f |
| `build_followup_response` | 89-161 | `(analysis: DocumentAnalysis, query: str | None=None, *, mode: str='question', config: Foll` | 16 | 4 | Follow-up: retrieval → expansion → (low conf) full-doc fallback. Args: analysis: ``DocumentAnalysis`` из cache |

## `workspace/skills/legal_summarizer/scripts/llm/prompts.py` — 167 LOC (code 128)
- module: `workspace.skills.legal_summarizer.scripts.llm.prompts`
- docstring: LLM-prompt и parser для batch'ей. Каждый ``list[Chunk]`` → один LLM call. Output: свободный текст с маркерами ``DOCUMENT CHUNK N: <саммари>``. LLM не тратит токены на обвязку/идентификаторы (chunk_id, section) — они изве
- static importers (2): `workspace/skills/legal_summarizer/scripts/execution/pipeline.py`, `workspace/skills/legal_summarizer/scripts/llm/calls.py`
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 1, module functions: 2

### class `ChunkResultParseError` — lines 73-74 (2 LOC), 0 methods
- bases: Exception
- decorators: —
- docstring: Ответ LLM не содержит маркеров DOCUMENT CHUNK N для всех чанков.
- name referenced in 4 file(s); tests: 2

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `build_batch_user_message` | 77-115 | `(chunks: Sequence[Chunk], *, chunks_total: int) -> str` | 18 | 1 | Построить user_body для LLM-вызова одного батча. |
| `parse_batch_response` | 118-160 | `(chunks: Sequence[Chunk], llm_text: str) -> dict[str, str]` | 10 | 1 | Распарсить ответ LLM, сопоставляя саммари с chunk_id по позиции. LLM пишет блоки вида:: DOCUMENT CHUNK 1: <сам |

## `workspace/skills/legal_summarizer/scripts/planning/strategy.py` — 164 LOC (code 129)
- module: `workspace.skills.legal_summarizer.scripts.planning.strategy`
- docstring: Unified execution planner. Единственный селектор для выбора стратегии: * ``"direct"`` — один LLM-вызов. * ``"map_flat"`` — map → flat reduce. * ``"map_hierarchical"`` — map → hierarchical reduce (multi-round). Правила: *
- static importers (3): `workspace/skills/legal_summarizer/scripts/application/canonical.py`, `workspace/skills/legal_summarizer/scripts/application/context_builder.py`, `workspace/skills/legal_summarizer/scripts/application/service.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 3

### class `ExecutionPolicy` — lines 36-56 (21 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Параметры unified execution strategy. Все параметры, влияющие на построение ExecutionPlan: * ``direct_threshold_tokens``: ниже → ``"direct"``. * ``hierarchical_section_threshold``: число meaningful sections. * ``chars_pe
- name referenced in 8 file(s); tests: 5

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_count_meaningful_sections` | 59-85 | `(struct: DocumentStructure) -> int` | 7 | 2 | Число meaningful sections. Критерий: * ``node_type == "section"``; * ``title.strip() != ""``; * имеет валидный |
| `select_strategy` | 88-110 | `(struct: DocumentStructure, chunks: tuple, *, policy: ExecutionPolicy | None=None) -> str` | 5 | 9 | Выбрать стратегию (``"direct"``/``"map_flat"``/``"map_hierarchical"``). Один ExecutionPlanner, одно решение. |
| `build_execution_plan` | 113-157 | `(struct: DocumentStructure, chunks: tuple, *, document_id: str, policy: ExecutionPolicy | ` | 3 | 13 | Построить ``ExecutionPlan`` для выбранной стратегии. Это unified API — заменяет разрозненные legacy селекторы  |

## `workspace/skills/legal_summarizer/scripts/output/presenter.py` — 159 LOC (code 129)
- module: `workspace.skills.legal_summarizer.scripts.output.presenter`
- docstring: Форматирование результатов для вывода в stdout (JSON). Приводит вложенные dict-результаты от ``summarizer.run()`` к плоскому единообразному формату для сериализации в JSON. JSON-сериализация значений (datetime/Decimal/Na
- static importers (1): `workspace/skills/legal_summarizer/scripts/cli.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 2

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `build_confirmation_options` | 31-71 | `(*, chars_in: int, min_seconds: float, max_seconds: float) -> dict[str, Any]` | 6 | 2 | Сформировать компактный payload ``confirmation_required``. Payload компактный (~400 chars) чтобы агент не гене |
| `prepare_output` | 74-156 | `(result: dict) -> dict` | 56 | 6 | Привести результат ``summarizer.run()`` к плоскому формату. Args: result: dict от ``summarizer.run()`` (``stat |

## `workspace/skills/legal_summarizer/scripts/llm/retry.py` — 155 LOC (code 123)
- module: `workspace.skills.legal_summarizer.scripts.llm.retry`
- docstring: ChunkResultParseError + smart retry. Сейчас ошибка JSON в ответе LLM приводит к повторной отправке **всего batch'а** — это дорого. Smart retry: 1. Local parse/repair (без LLM-вызова) — попытка извлечь valid JSON. 2. Repa
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 2, module functions: 4

### class `ChunkResultParseError` — lines 26-38 (13 LOC), 1 methods
- bases: Exception
- decorators: —
- docstring: Raised when batch response cannot be parsed to valid JSON. Attributes: chunk_ids: проблемные chunk_ids (если удалось извлечь из response). raw_response: оригинальный текст от LLM.
- name referenced in 4 file(s); tests: 2

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 34-38 | `(self, message: str, *, chunk_ids: tuple[str, ...]=(), raw_response: str='') -> None` | 1 | 0 | 6 | — |

### class `ParsedBatchResult` — lines 42-48 (7 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Результат parsing batch response.
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_strip_code_fence` | 55-59 | `(text: str) -> str` | 2 | 1 | — |
| `_extract_first_json_object` | 62-74 | `(text: str) -> dict[str, Any] | None` | 4 | 1 | — |
| `parse_batch_response_local` | 77-127 | `(response_text: str, expected_chunk_ids: tuple[str, ...]) -> ParsedBatchResult` | 14 | 1 | Попытаться распарсить response без LLM-вызова. Если JSON валидный и содержит summaries для всех expected_chunk |
| `build_repair_prompt` | 130-147 | `(original_response: str, failed_chunk_ids: tuple[str, ...]) -> str` | 1 | 2 | Создать **точечный** repair prompt только для failed chunks. Это решает проблему: вместо повторной отправки вс |

## `workspace/skills/legal_summarizer/scripts/retrieval/query.py` — 154 LOC (code 120)
- module: `workspace.skills.legal_summarizer.scripts.retrieval.query`
- docstring: Question retrieval cascade. Целевой cascade: user query ↓ query normalization (query_normalizer) ↓ stopword removal (минимальный, без LLM) ↓ normalized lexical search (substring match) ↓ sparse ranking (score, BM25-lite)
- static importers (5): `workspace/skills/legal_summarizer/scripts/application/chunk_selection.py`, `workspace/skills/legal_summarizer/scripts/document/analysis.py`, `workspace/skills/legal_summarizer/scripts/retrieval/followup.py`, `workspace/skills/legal_summarizer/scripts/retrieval/index.py`, `workspace/skills/legal_summarizer/scripts/retrieval/normalizer.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 2, module functions: 3

### class `RetrievalHit` — lines 36-43 (8 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Один кандидат из retrieval cascade.
- name referenced in 2 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `RetrievalConfig` — lines 47-54 (8 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Параметры retrieval cascade.
- name referenced in 6 file(s); tests: 2

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `tokenize` | 67-77 | `(text: str) -> list[str]` | 4 | 2 | Нормализация + tokenization. Без LLM. Приводит к lowercase, выбрасывает стоп-слова, оставляет только word-toke |
| `score_chunk` | 80-120 | `(chunk: Chunk, terms: list[str], *, config: RetrievalConfig) -> RetrievalHit` | 14 | 3 | Посчитать score для chunk'а по термам. Score = sum(weight * term_frequency_in_section). ``section_title_hit``/ |
| `retrieve_chunks` | 123-145 | `(chunks: Iterable[Chunk], query: str, *, config: RetrievalConfig | None=None) -> list[Retr` | 5 | 3 | Cascade retrieval: normalize → score → top-K. Retrieval ranking (не first-match). |

## `workspace/skills/legal_summarizer/scripts/retrieval/quality.py` — 143 LOC (code 120)
- module: `workspace.skills.legal_summarizer.scripts.retrieval.quality`
- docstring: Quality metrics. Метрики качества: * ``retrieval_recall_at_K``: доля reference questions, для которых retrieval вернул хотя бы один expected keyword в top-K chunks. * ``section_hit_rate``: доля questions, для которых ret
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 3

### class `QualityMetrics` — lines 41-69 (29 LOC), 2 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Сводка quality metrics.
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `tokens_per_call` | 53-57 | `(self) -> float` | 2 | 0 | 2 | Среднее input tokens на LLM-вызов. |
| `summary` | 59-69 | `(self) -> dict[str, object]` | 1 | 0 | 7 | — |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `compute_retrieval_recall` | 72-95 | `(index: RetrievalIndex, qa_set: ReferenceQASet, *, top_k: int=5) -> float` | 8 | 1 | Доля reference questions, для которых retrieval дал hit в top-K. |
| `compute_provenance_correctness` | 98-104 | `(chains: list[ProvenanceChain]) -> float` | 4 | 1 | Доля chains с is_complete() == True. |
| `compute_quality_metrics` | 107-140 | `(*, index: RetrievalIndex | None=None, qa_set: ReferenceQASet | None=None, chains: list[Pr` | 7 | 1 | Вычислить quality metrics. |

## `workspace/skills/legal_summarizer/scripts/llm/single_flight.py` — 135 LOC (code 97)
- module: `workspace.skills.legal_summarizer.scripts.llm.single_flight`
- docstring: Single-flight invariant enforcement. ``max_active_llm_calls == 1``. Нельзя иметь параллельных LLM-вызовов, даже если pipeline содержит несколько батчей. Единая реализация cross-thread single-flight boundary: * ``LLM_FLIG
- static importers (2): `workspace/skills/legal_summarizer/scripts/execution/pipeline.py`, `workspace/skills/legal_summarizer/scripts/llm/calls.py`
- string/dynamic refs: 2
- test files touching it: — none —
- classes: 2, module functions: 2

### class `SingleFlightTracker` — lines 28-71 (44 LOC), 3 methods
- bases: object
- decorators: dataclass
- docstring: Tracker для ``max_active_llm_calls == 1``. Использование:: tracker = SingleFlightTracker() with tracker.llm_call(): ...do work... ``violation_count`` инкрементируется, если две ``with`` блока вложены или активны одноврем
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `llm_call` | 46-64 | `(self) -> Any` | 2 | 0 | 2 | — |
| `active` | 67-68 | `(self) -> int` | 1 | 0 | 2 | — |
| `is_safe` | 70-71 | `(self) -> bool` | 1 | 0 | 1 | — |

### class `SingleFlightViolation` — lines 74-75 (2 LOC), 0 methods
- bases: RuntimeError
- decorators: —
- docstring: Raised when multiple LLM calls overlap.
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `assert_single_flight` | 78-92 | `(fn, *args, tracker: SingleFlightTracker | None=None, **kwargs) -> tuple[Any, SingleFlight` | 3 | 1 | Запустить ``fn`` под single-flight guard. Returns: tuple ``(result, tracker)``. |
| `guarded_chat` | 104-123 | `(callable_, *args, **kwargs)` | 2 | 1 | Сериализованный LLM-вызов через единый single-flight boundary. Это **public API** для всех потребителей LLM bo |

## `workspace/skills/legal_summarizer/scripts/retrieval/provenance.py` — 130 LOC (code 109)
- module: `workspace.skills.legal_summarizer.scripts.retrieval.provenance`
- docstring: Provenance checks. Каждый результат должен уметь показать: * document; * section; * subsection; * page; * block; * chunk. Этот модуль предоставляет ``ProvenanceChain`` — связку document → section → chunk → page/block для
- static importers (1): `workspace/skills/legal_summarizer/scripts/retrieval/quality.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 1

### class `ProvenanceChain` — lines 34-81 (48 LOC), 2 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Полная provenance chain для ответа. Attributes: document_id: ``DocumentIdentity.document_id``. document_path: ``PhysicalDocument.path``. section_id: ``StructureNode.node_id``. section_title: heading. section_path: e.g. `
- name referenced in 3 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `to_dict` | 58-69 | `(self) -> dict[str, Any]` | 1 | 0 | 24 | — |
| `is_complete` | 71-81 | `(self) -> bool` | 2 | 0 | 9 | ``True`` если все критичные поля заполнены (acceptance). |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `build_provenance_chain` | 84-127 | `(chunk: Chunk, *, doc: PhysicalDocument, struct: DocumentStructure, document_id: str) -> P` | 24 | 3 | Построить полную provenance chain для ``chunk``. Возвращает ``None`` если chunk не найден в PhysicalDocument. |

## `workspace/skills/legal_summarizer/scripts/retrieval/index.py` — 126 LOC (code 107)
- module: `workspace.skills.legal_summarizer.scripts.retrieval.index`
- docstring: RetrievalIndex. Минимальная реализация многоуровневого индекса для retrieval: * L0: physical parse cache (PhysicalDocument). * L1: structure/chunk cache (DocumentStructure + Chunks). * L2: semantic analysis cache (Semant
- static importers (2): `workspace/skills/legal_summarizer/scripts/document/analysis.py`, `workspace/skills/legal_summarizer/scripts/retrieval/quality.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 0

### class `RetrievalIndex` — lines 36-123 (88 LOC), 3 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: In-memory retrieval index (L3 metadata over L0/L1/L2). Attributes: document_id: идентификатор документа. chunks: tuple of ``Chunk``. structure: ``DocumentStructure``. physical: ``PhysicalDocument`` (L0 reference). term_t
- name referenced in 5 file(s); tests: 3

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `retrieve` | 53-76 | `(self, query: str, *, config: RetrievalConfig | None=None) -> list[RetrievalHit]` | 10 | 0 | 4 | Поиск через inverted index + sparse ranking. |
| `build` | 79-116 | `(cls, *, chunks: Iterable[Chunk], structure: DocumentStructure, physical: PhysicalDocument` | 8 | 0 | 14 | Построить inverted index из chunks. ``L3`` строится один раз — повторные вызовы ``retrieve`` не пересобирают i |
| `to_dict` | 118-123 | `(self) -> dict[str, Any]` | 1 | 0 | 24 | — |

## `workspace/skills/legal_summarizer/scripts/retrieval/records.py` — 124 LOC (code 105)
- module: `workspace.skills.legal_summarizer.scripts.retrieval.records`
- docstring: SemanticRecord — структурированный output LLM map. LLM map возвращает не просто свободный текст, а структурированный record, который downstream может ранжировать, фильтровать и использовать для retrieval. Минимальная схе
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 2, module functions: 0

### class `Provenance` — lines 33-47 (15 LOC), 1 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Provenance для SemanticRecord.
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `to_dict` | 41-47 | `(self) -> dict[str, Any]` | 1 | 0 | 24 | — |

### class `SemanticRecord` — lines 51-121 (71 LOC), 2 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Структурированный output LLM map. Attributes: chunk_id: id chunk'а, для которого создан record. section_id: ``StructureNode.node_id`` из DocumentStructure. summary: основной текст саммари (1-3 предложения). facts: tuple 
- name referenced in 3 file(s); tests: 2

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `to_dict` | 82-98 | `(self) -> dict[str, Any]` | 2 | 0 | 24 | — |
| `from_minimal` | 101-121 | `(cls, chunk_id: str, section_id: str, summary: str, *, provenance: Provenance | None=None,` | 1 | 0 | 2 | Создать минимальный record — только summary. Полезно для малых моделей или старых prompt'ов, которые возвращаю |

## `workspace/skills/legal_summarizer/scripts/retrieval/fallback.py` — 122 LOC (code 98)
- module: `workspace.skills.legal_summarizer.scripts.retrieval.fallback`
- docstring: Full-document fallback. Full-document fallback должен быть **последним** шагом retrieval cascade — не default'ом. Этот модуль предоставляет явный helper для controlled fallback, когда confidence низкая. Confidence levels
- static importers (1): `workspace/skills/legal_summarizer/scripts/retrieval/followup.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 2, module functions: 2

### class `FullDocFallbackConfig` — lines 26-31 (6 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Параметры controlled full-document fallback.
- name referenced in 3 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `RetrievalDecision` — lines 80-86 (7 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Решение retrieval cascade.
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `full_document_fallback` | 34-76 | `(chunks: tuple[Chunk, ...], *, config: FullDocFallbackConfig | None=None, estimator: Token` | 11 | 2 | Вернуть **controlled** subset документа как последний resort. Не отправляет весь документ в LLM. Выбирает: * f |
| `decide_retrieval` | 89-114 | `(hits: tuple[Chunk, ...], *, high_threshold: int=3, medium_threshold: int=2) -> RetrievalD` | 4 | 2 | Решить confidence level на основе числа hits. |

## `workspace/skills/legal_summarizer/scripts/retrieval/qa.py` — 121 LOC (code 100)
- module: `workspace.skills.legal_summarizer.scripts.retrieval.qa`
- docstring: Reference QA для benchmark'ов. Для каждого benchmark-сценария набор reference questions с expected answer и expected_section_id. Используется для метрик retrieval recall@K, section hit rate, provenance correctness. Refer
- static importers (1): `workspace/skills/legal_summarizer/scripts/retrieval/quality.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 2, module functions: 2

### class `ReferenceQuestion` — lines 31-37 (7 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Один reference вопрос.
- name referenced in 3 file(s); tests: 2

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `ReferenceQASet` — lines 41-45 (5 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Набор reference questions для одного benchmark-документа.
- name referenced in 4 file(s); tests: 2

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `standard_qa_set` | 48-84 | `(document_name: str='legal-doc') -> ReferenceQASet` | 1 | 2 | Стандартный набор reference questions. |
| `evaluate_retrieval` | 87-113 | `(retrieved_chunk_texts: tuple[str, ...], question: ReferenceQuestion) -> dict[str, Any]` | 6 | 2 | Оценить retrieved chunks против reference question. Returns: dict с полями: * ``hit`` — найдены ли expected ke |

## `workspace/skills/legal_summarizer/scripts/llm/client.py` — 106 LOC (code 90)
- module: `workspace.skills.legal_summarizer.scripts.llm.client`
- docstring: LLM-клиент (OpenAI-compatible HTTP API) — тонкая обёртка над общим клиентом. Единая реализация — ``lib.services.llm_client.call_llm`` (та же, что использует ``audit_analyzer`` и бенчмарк). Этот модуль сохраняет прежний п
- static importers (0): — NONE —
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 0, module functions: 2

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_trace` | 30-37 | `(stage: str, **fields) -> None` | 4 | 1 | — |
| `chat` | 40-105 | `(messages: list[dict], *, context: list[dict] | None=None, **kwargs) -> str` | 19 | 4 | Отправить сообщения в LLM и получить текстовый ответ. Поддерживает опциональный ``context`` — историю чата, ко |

## `workspace/skills/legal_summarizer/scripts/retrieval/candidate_aggregator.py` — 105 LOC (code 84)
- module: `workspace.skills.legal_summarizer.scripts.retrieval.candidate_aggregator`
- docstring: Объединение heading-кандидатов. Если несколько источников (DOCX style + numbering + regex + PDF outline) говорят об одном и том же ``DocumentBlock``, нельзя создавать несколько heading-кандидатов — это раздувает структур
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 1

### class `AggregatedCandidate` — lines 34-55 (22 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Один объединённый heading-кандидат. Attributes: block_index: ordinal ``DocumentBlock`` (для outline кандидатов ``block_index`` остаётся ``-1``, и downstream mapping обязан решить эту ситуацию. text: текст кандидата (один
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `aggregate_by_block` | 58-102 | `(candidates: Iterable[HeadingCandidate]) -> list[AggregatedCandidate]` | 11 | 1 | Объединить ``HeadingCandidate`` по ``block_index``. Outline-кандидаты (с ``block_index = -1``) не агрегируются |

## `workspace/skills/legal_summarizer/scripts/llm/tokens.py` — 95 LOC (code 72)
- module: `workspace.skills.legal_summarizer.scripts.llm.tokens`
- docstring: TokenEstimator — единая оценка токенов. Заменяет **разные** формулы оценки токенов, которые сейчас раскиданы по: * ``chunks.ChunkConfig.chars_per_token = 3.5`` * ``chunks._split_block_with_offsets`` (явная формула) * ``t
- static importers (5): `workspace/skills/legal_summarizer/scripts/application/canonical.py`, `workspace/skills/legal_summarizer/scripts/chunking/packing.py`, `workspace/skills/legal_summarizer/scripts/planning/strategy.py`, `workspace/skills/legal_summarizer/scripts/retrieval/context_expansion.py`, `workspace/skills/legal_summarizer/scripts/retrieval/fallback.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 2, module functions: 0

### class `TokenEstimatorConfig` — lines 34-38 (5 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Параметры TokenEstimator'а.
- name referenced in 11 file(s); tests: 4

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `TokenEstimator` — lines 41-92 (52 LOC), 4 methods
- bases: object
- decorators: —
- docstring: Единый TokenEstimator. Используется chunker'ом, packing'ом, execution strategy, reducer'ом, brief strategy, document_stats — все через единый API.
- name referenced in 10 file(s); tests: 4

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 48-49 | `(self, config: TokenEstimatorConfig | None=None) -> None` | 2 | 0 | 6 | — |
| `estimate` | 51-61 | `(self, text: str) -> int` | 4 | 1 | 6 | Оценить токены для ``text``. Возвращает ceil(len(text) / chars_per_token), минимум 1 для непустого текста. |
| `estimate_many` | 63-65 | `(self, texts: list[str]) -> int` | 3 | 0 | 4 | Суммарная оценка токенов для списка текстов. |
| `available` | 67-92 | `(self, context_limit: int, system_tokens: int, output_tokens: int, *, safety_margin_ratio:` | 3 | 0 | 1 | Сколько токенов доступно для контента. Args: context_limit: максимум контекстного окна модели. system_tokens:  |

## `workspace/skills/legal_summarizer/scripts/llm/sanitize.py` — 86 LOC (code 70)
- module: `workspace.skills.legal_summarizer.scripts.llm.sanitize`
- docstring: Sanitize LLM responses — очистка chain-of-thought блоков и извлечение subject. Некоторые LLM (DeepSeek/Qwen) выдают CoT прямо в финальном ответе: ``<think>reasoning</think>answer``. Без очистки ``extract_subject`` подбер
- static importers (3): `workspace/skills/legal_summarizer/scripts/application/execution_orchestration.py`, `workspace/skills/legal_summarizer/scripts/application/service.py`, `workspace/skills/legal_summarizer/scripts/execution/map_reduce.py`
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 0, module functions: 2

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `strip_think_blocks` | 32-48 | `(text: str) -> str` | 5 | 4 | Убрать ``<think>…</think>`` блоки из текста (CoT от моделей). |
| `extract_subject` | 51-76 | `(summary: str) -> str` | 7 | 3 | Извлечь subject (первая непустая строка) из summary. Убирает markdown-заголовок (``# Тема`` → ``Тема``). Если  |

## `workspace/skills/legal_summarizer/scripts/retrieval/normalizer.py` — 83 LOC (code 63)
- module: `workspace.skills.legal_summarizer.scripts.retrieval.normalizer`
- docstring: Query normalization. Отдельный модуль для query normalization: * case (lowercase); * punctuation; * whitespace; * stopwords; * legal aliases (если применимо). Не использует LLM — детерминированный. Сейчас функция ``token
- static importers (1): `workspace/skills/legal_summarizer/scripts/retrieval/index.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 3

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `normalize_query` | 37-47 | `(query: str) -> str` | 2 | 2 | Нормализовать query: lowercase, strip punctuation, collapse whitespace. Не выбрасывает стоп-слова (это делает  |
| `expand_with_aliases` | 50-67 | `(text: str) -> list[str]` | 4 | 1 | Вернуть оригинал + все legal aliases. Используется, если downstream хочет матчить как по query, так и по юриди |
| `tokenize_normalized` | 70-76 | `(query: str) -> list[str]` | 4 | 2 | Удобный API: normalize → tokenize → drop stopwords. |

## `workspace/skills/legal_summarizer/scripts/llm/config.py` — 72 LOC (code 44)
- module: `workspace.skills.legal_summarizer.scripts.llm.config`
- docstring: Обёртка над ``lib.core.skill_config`` для текущего skill'а (legal_summarizer). Все функции параметризованы в ``lib.core.skill_config`` по ``skill_name``. Здесь — тонкие обёртки, чтобы внутренний код skill'а мог продолжат
- static importers (6): `workspace/skills/legal_summarizer/scripts/application/chunk_selection.py`, `workspace/skills/legal_summarizer/scripts/application/estimation.py`, `workspace/skills/legal_summarizer/scripts/application/service.py`, `workspace/skills/legal_summarizer/scripts/chunking/chunker.py`, `workspace/skills/legal_summarizer/scripts/cli.py`, `workspace/skills/legal_summarizer/scripts/llm/client.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 8

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `get_llm_config` | 30-31 | `() -> dict[str, Any]` | 1 | 4 | — |
| `get_cli_config` | 34-35 | `() -> dict[str, Any]` | 1 | 7 | — |
| `get_max_retries` | 38-39 | `() -> int` | 1 | 3 | — |
| `get_chunking_config` | 42-43 | `() -> dict[str, Any]` | 1 | 5 | — |
| `get_brief_context_config` | 46-52 | `() -> dict[str, Any]` | 1 | 2 | Параметры BriefContextBuilder (``skills.legal_summarizer.brief_context.*``). Тонкая обёртка над ``lib.core.ski |
| `get_execution_config` | 55-63 | `() -> dict[str, Any]` | 4 | 4 | Прямой доступ к ``SETTINGS['skills']['legal_summarizer']['execution']``. Обёртка через ``_lib.get_execution_co |
| `get_default_length` | 66-67 | `() -> str` | 2 | 2 | — |
| `get_timeout_sec` | 70-71 | `() -> float` | 2 | 0 | — |

## `workspace/skills/legal_summarizer/scripts/llm/prompts_runtime.py` — 69 LOC (code 52)
- module: `workspace.skills.legal_summarizer.scripts.llm.prompts_runtime`
- docstring: Загрузка системных промптов и формирование length/question инструкций. Модуль НЕ называется ``prompts.py``, чтобы не конфликтовать с существующим ``scripts/prompts.py`` (там лежит ``build_batch_user_message`` и парсер LL
- static importers (2): `workspace/skills/legal_summarizer/scripts/application/service.py`, `workspace/skills/legal_summarizer/scripts/llm/calls.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 2

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `load_prompt` | 22-30 | `(name: str) -> str` | 4 | 1 | Прочитать системный промпт из ``prompts/<name>.md``. |
| `system_instruction` | 51-60 | `(length: str, question: str | None) -> str` | 3 | 1 | Подготовить инструкцию для {length_instruction} в системном промпте. Если передан ``question`` — инструкция фо |

## `workspace/skills/legal_summarizer/scripts/retrieval/question.py` — 55 LOC (code 42)
- module: `workspace.skills.legal_summarizer.scripts.retrieval.question`
- docstring: Question via retrieval index. Follow-up ``question`` должен использовать ``DocumentAnalysis.retrieve`` (через inverted index), не substring first-match. Этот модуль — convenience: ``answer_question_from_analysis``.
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 1

### class `QuestionResponse` — lines 24-30 (7 LOC), 0 methods
- bases: object
- decorators: dataclass(frozen=True)
- docstring: Результат question через retrieval index.
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `answer_question_from_analysis` | 33-52 | `(analysis: DocumentAnalysis, query: str, *, config: FollowupConfig | None=None) -> Questio` | 1 | 1 | Ответить на question через cached analysis. Использует ``DocumentAnalysis.retrieve`` (inverted index + sparse  |

## `workspace/skills/legal_summarizer/scripts/retrieval/canonical.py` — 47 LOC (code 37)
- module: `workspace.skills.legal_summarizer.scripts.retrieval.canonical`
- docstring: Canonical retrieval wrapper. Использует только ``DocumentAnalysis.retrieve`` и canonical ``build_followup_response`` (через ``structure.followup``) для ``mode="question"``. Архитектурное замечание (brief-refactor): ``sel
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 1

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `answer_followup` | 25-41 | `(analysis: DocumentAnalysis, query: str, *, config: FollowupConfig | None=None) -> Followu` | 1 | 3 | Ответить на follow-up question через canonical analysis. Не делает повторного parsing/structure/chunking — раб |

## `workspace/skills/legal_summarizer/scripts/llm/__init__.py` — 1 LOC (code 0)
- module: `workspace.skills.legal_summarizer.scripts.llm`
- docstring: — NONE —
- static importers (1): `workspace/skills/legal_summarizer/scripts/llm/calls.py`
- string/dynamic refs: 0
- test files touching it: `tests/integration/test_worker_pool_real_bot.py`, `tests/test_project_settings.py`, `workspace/skills/legal_summarizer/tests/test_confirmation_context.py`, `workspace/skills/legal_summarizer/tests/test_e2e_600_page.py`, `workspace/skills/legal_summarizer/tests/test_estimate_confirmation.py`, `workspace/skills/legal_summarizer/tests/test_execution_context_snapshot.py`, `workspace/skills/legal_summarizer/tests/test_final_invariants.py`, `workspace/skills/legal_summarizer/tests/test_recovered_invariants.py`, `workspace/skills/legal_summarizer/tests/test_single_flight_concurrent_safety.py`
- classes: 0, module functions: 0

## `workspace/skills/legal_summarizer/scripts/output/__init__.py` — 1 LOC (code 0)
- module: `workspace.skills.legal_summarizer.scripts.output`
- docstring: — NONE —
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: `tests/test_benchmarks_runner.py`
- classes: 0, module functions: 0

## `workspace/skills/legal_summarizer/scripts/planning/__init__.py` — 1 LOC (code 0)
- module: `workspace.skills.legal_summarizer.scripts.planning`
- docstring: — NONE —
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 0

## `workspace/skills/legal_summarizer/scripts/retrieval/__init__.py` — 1 LOC (code 0)
- module: `workspace.skills.legal_summarizer.scripts.retrieval`
- docstring: — NONE —
- static importers (0): — NONE —
- string/dynamic refs: 0
- test files touching it: `workspace/skills/legal_summarizer/tests/test_legal_summarizer_no_legacy.py`, `workspace/skills/legal_summarizer/tests/test_structure_architecture_guard.py`, `workspace/skills/legal_summarizer/tests/test_structure_llm_invariant.py`
- classes: 0, module functions: 0

