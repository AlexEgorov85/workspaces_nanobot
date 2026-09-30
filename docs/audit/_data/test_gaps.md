# Product modules with no trace in any test file

**119 of 215 product modules.**

| Module | LOC | Dynamically loaded | Docstring |
|---|---:|---|---|
| `workspace/utils/db.py` | 1063 |  | Единый коннектор к PostgreSQL / Greenplum через psycopg2. Архитектура — «одна очередь + пу |
| `workspace/skills/legal_summarizer/scripts/execution/map_reduce.py` | 738 | yes | Map-reduce execution strategy: batching + LLM calls + reduce. Фактическая реализация ``map |
| `workspace/skills/legal_summarizer/scripts/document/heading.py` | 660 |  | Heading detection (candidates + scoring) — ``structure/heading.py``. Поведение НЕ меняется |
| `workspace/skills/legal_summarizer/scripts/chunking/chunker.py` | 623 |  | DocumentStructure-aware chunker. Chunker, который использует ``DocumentStructure`` как еди |
| `workspace/skills/legal_summarizer/scripts/document/hierarchy.py` | 520 |  | StructureTreeBuilder. Строит ``DocumentStructure`` из: * ``HeadingCandidate`` (из heading. |
| `workspace/skills/legal_summarizer/scripts/application/service.py` | 514 | yes | Orchestration facade для legal_summarizer. Public entry points: * ``run`` — canonical exec |
| `workspace/skills/legal_summarizer/scripts/application/brief_context.py` | 509 | yes | BriefContextBuilder: один структурный Chunk для всего документа. BRIEF CONTRACT: один доку |
| `workspace/skills/legal_summarizer/scripts/document/structure.py` | 506 |  | DocumentStructure — единый контракт семантической структуры документа. Это **canonical mod |
| `workspace/skills/legal_summarizer/scripts/application/execution_orchestration.py` | 489 | yes | Execution orchestration: координатор ``run_direct`` / ``run_map_reduce``. Тонкая прослойка |
| `workspace/skills/legal_summarizer/scripts/cache/document_cache.py` | 484 |  | DocumentCache — единственный владелец document-level cache protocol. Ответственность (жёст |
| `workspace/skills/legal_summarizer/scripts/chunking/structural_packing.py` | 471 |  | Hierarchical structural packing для legal_summarizer. Алгоритм (Phase 2): 1. Tables → atom |
| `workspace/skills/legal_summarizer/scripts/document/physical.py` | 456 |  | PhysicalDocument: нормализованный список блоков документа с координатами. Это **adapter**  |
| `workspace/utils/session_file_store.py` | 431 |  |  |
| `workspace/skills/legal_summarizer/scripts/chunking/chunks.py` | 394 |  | Structure-Aware Chunker для legal_summarizer. Преобразует ``DocumentBlock[]`` + ``SectionT |
| `workspace/skills/legal_summarizer/scripts/cache/manifest.py` | 388 | yes | Manifest v2 для legal_summarizer Phase 2B. Manifest хранит source of truth для resume (inv |
| `workspace/skills/legal_summarizer/scripts/document/pdf_outline.py` | 369 |  | PDF outline mapping. Критический bugfix: ``heading._extract_pdf_outline`` ставил ``block_i |
| `workspace/skills/legal_summarizer/scripts/application/pipeline_structure.py` | 352 | yes | DocumentStructure как SoT для всех downstream'ов. Этот модуль — **точка сборки** canonical |
| `workspace/skills/legal_summarizer/scripts/document/list_detection.py` | 335 |  | Numbered-list detection — ``structure/list_detection.py``. Различение нумерованных **разде |
| `workspace/skills/legal_summarizer/scripts/document/validation.py` | 332 |  | StructureValidator. Проверяет ``DocumentStructure`` на: * **Coverage**: каждый значимый `` |
| `workspace/skills/legal_summarizer/scripts/cli_query.py` | 330 |  | ``cli_query.py`` — follow-up-запросы по сохранённой operation_id. Нужен, чтобы агент мог о |
| `lib/core/skill_config.py` | 325 | yes | Runtime API для skill'ов: конфигурация, таблицы, FAISS. Параметризован по ``skill_name``.  |
| `workspace/utils/office_files.py` | 304 | yes |  |
| `workspace/skills/legal_summarizer/scripts/execution/hierarchical.py` | 294 | yes | All reduce functions (canonical copy in execution layer). |
| `tools/generate_comments_sql.py` | 291 |  | Генератор apply_all_comments.sql из schema.json + extra описаний. Требует на входе ``works |
| `tools/check_indexes.py` | 285 |  | ``tools/check_indexes.py`` — declared-vs-runtime diff для vector-индексов. Сравнивает: * ` |
| `tools/validate_component_specs.py` | 274 |  | Валидация структуры компонентных спецификаций OpenSpec (component-spec-validation). Провер |
| `workspace/skills/legal_summarizer/scripts/application/question_context.py` | 258 | yes | Question synthesis context builder. Собирает LLM-вход для ``llm_document_reduce(question=. |
| `tools/migrate.py` | 252 |  | migrate.py — runner миграций схемы (sql/migrations/V*.sql). Порядок версий определяется но |
| `workspace/skills/legal_summarizer/scripts/application/canonical.py` | 244 |  | Canonical run pipeline. Этот модуль — **production-flow**, использующий только canonical p |
| `workspace/skills/legal_summarizer/scripts/document/numbering.py` | 237 |  | Numbering parser для headings / list items / captions. Цель: **один модуль** для всего num |
| `workspace/skills/audit_analyzer/scripts/predefined/builder.py` | 229 |  | ``DynamicQueryBuilder`` — сборка SQL из шаблона скрипта. Перенесено из ``workspace/skills/ |
| `workspace/skills/audit_analyzer/scripts/predefined/validator.py` | 212 |  | ``ParameterValidator`` — проверка пользовательских параметров. Перенесено из ``workspace/s |
| `lib/utils/windows_terminal.py` | 210 | yes | Поддержка Windows-консоли (legacy cmd/PowerShell без VT). Диагноз (проверен на 0.3.5 + pro |
| `tools/check_worker_pool_integrity.py` | 201 |  | Диагностика целостности мульти-машинного пула воркеров. Проверяет инвариант ``processing ⇔ |
| `workspace/skills/legal_summarizer/scripts/application/estimation.py` | 200 |  | Estimation: верхняя граница LLM-вызовов и времени для конкретного запуска. Использует те ж |
| `workspace/skills/legal_summarizer/scripts/document/repair.py` | 200 |  | Structural repair pass. После построения иерархии запускается **repair** для исправления т |
| `lib/services/llm_client.py` | 199 |  | Единый HTTP-клиент к LLM (OpenAI-compatible /chat/completions). Консолидация: раньше кажды |
| `workspace/skills/audit_analyzer/scripts/predefined/mode.py` | 199 |  | ``predefined.run()`` — выполнение predefined SQL-скрипта через generic core. Канонический  |
| `workspace/skills/legal_summarizer/scripts/retrieval/context_expansion.py` | 196 |  | Semantic context expansion. Для выбранного chunk'а вернуть расширенный контекст через поис |
| `tools/audit_nanobot_contracts.py` | 194 |  | Аудит контрактов nanobot: AST нашего кода vs реальные символы nanobot 0.3.5. Что делает: 1 |
| `workspace/skills/legal_summarizer/scripts/planning/plan.py` | 194 |  | ExecutionPlan. Immutable план выполнения, который объединяет результаты chunks, batches и  |
| `workspace/skills/legal_summarizer/scripts/llm/calls.py` | 187 | yes | LLM-call wrappers: низкоуровневые обёртки для LLM (map batch / section reduce / document r |
| `tools/scan_nanobot_inventory.py` | 176 |  | Одноразовый сканер nanobot-зависимостей: строит JSON-инвентарь. Запуск: python tools/scan_ |
| `tools/smoke_post_cleanup.py` | 176 |  | Smoke-test runtime для opencode change post-0.3.5-patches-cleanup. Запускает gateway в фон |
| `tools/release_v252.py` | 169 |  | Создать GitHub Release для тега v2.5.2 через gh CLI (fallback: curl). Режимы: --dry-run —  |
| `workspace/skills/legal_summarizer/scripts/retrieval/followup.py` | 169 |  | First-run vs Follow-up split. Архитектурное разделение: * **First-run**: file → parse → st |
| `workspace/skills/legal_summarizer/scripts/application/brief_compression.py` | 168 |  | Детерминированная компрессия brief-секций. BRIEF CONTRACT: один документ → ровно один Chun |
| `workspace/skills/legal_summarizer/scripts/llm/prompts.py` | 167 | yes | LLM-prompt и parser для batch'ей. Каждый ``list[Chunk]`` → один LLM call. Output: свободны |
| `workspace/skills/legal_summarizer/scripts/document/analysis.py` | 166 |  | DocumentAnalysis cache architecture. Единый ``DocumentAnalysis`` — это immutable snapshot  |
| `workspace/skills/legal_summarizer/scripts/chunking/packing.py` | 164 |  | Controlled adjacent-section packing. Сейчас ``packing_impl.pack_chunks`` строго section-lo |
| `workspace/skills/legal_summarizer/scripts/planning/strategy.py` | 164 |  | Unified execution planner. Единственный селектор для выбора стратегии: * ``"direct"`` — од |
| `workspace/skills/legal_summarizer/scripts/output/presenter.py` | 159 |  | Форматирование результатов для вывода в stdout (JSON). Приводит вложенные dict-результаты  |
| `workspace/skills/legal_summarizer/scripts/execution/pipeline.py` | 157 | yes | Pipeline execution: один LLM batch + retry (без side-effects на cache). Содержит: * ``proc |
| `workspace/skills/legal_summarizer/scripts/llm/retry.py` | 155 |  | ChunkResultParseError + smart retry. Сейчас ошибка JSON в ответе LLM приводит к повторной  |
| `workspace/skills/audit_analyzer/scripts/predefined/db_loader.py` | 154 |  | Loader: ``public.agent_predefined_scripts`` (DB) → ``ScriptDefinition``. Единая точка вход |
| `workspace/skills/legal_summarizer/scripts/document/title.py` | 154 |  | Document title resolution. Источники title (по приоритету): 1. ``DOCX core_properties.titl |
| `workspace/skills/legal_summarizer/scripts/retrieval/query.py` | 154 |  | Question retrieval cascade. Целевой cascade: user query ↓ query normalization (query_norma |
| `workspace/skills/legal_summarizer/scripts/document/safety_merge.py` | 147 |  | Safety net merge для микро-секций. После хорошего heading detection + repair остаются **кр |
| `workspace/skills/legal_summarizer/scripts/retrieval/quality.py` | 143 |  | Quality metrics. Метрики качества: * ``retrieval_recall_at_K``: доля reference questions,  |
| `tools/demo_internal_fallback.py` | 142 |  | Демонстрация fallback-ответа при internal-ошибке AgentLoop. Запускать:: PYTHONIOENCODING=u |
| `workspace/skills/legal_summarizer/scripts/llm/single_flight.py` | 135 | yes | Single-flight invariant enforcement. ``max_active_llm_calls == 1``. Нельзя иметь параллель |
| `workspace/tools/example.py` | 131 |  | Шаблон кастомного tool'а проекта. Скопируйте файл, переименуйте класс и ``config_key``, до |
| `workspace/skills/legal_summarizer/scripts/retrieval/provenance.py` | 130 |  | Provenance checks. Каждый результат должен уметь показать: * document; * section; * subsec |
| `workspace/skills/legal_summarizer/scripts/retrieval/index.py` | 126 |  | RetrievalIndex. Минимальная реализация многоуровневого индекса для retrieval: * L0: physic |
| `workspace/skills/legal_summarizer/scripts/retrieval/records.py` | 124 |  | SemanticRecord — структурированный output LLM map. LLM map возвращает не просто свободный  |
| `workspace/skills/legal_summarizer/scripts/application/chunk_selection.py` | 122 | yes | Chunk selection policy для application layer. Единая точка ``select_chunks_for_mode(insp,  |
| `workspace/skills/legal_summarizer/scripts/retrieval/fallback.py` | 122 |  | Full-document fallback. Full-document fallback должен быть **последним** шагом retrieval c |
| `workspace/skills/legal_summarizer/scripts/retrieval/qa.py` | 121 |  | Reference QA для benchmark'ов. Для каждого benchmark-сценария набор reference questions с  |
| `tools/release_v251.py` | 120 |  | Создать GitHub Release для тега v2.5.1 через gh CLI (fallback: curl). |
| `workspace/skills/legal_summarizer/scripts/document/identity.py` | 118 |  | DocumentIdentity — единый идентификатор документа. Заменяет **две параллельные** реализаци |
| `workspace/skills/legal_summarizer/scripts/document/block_ownership.py` | 109 |  | Canonical block ownership. Чистые функции на ``DocumentStructure``, которые определяют «ка |
| `workspace/skills/legal_summarizer/scripts/chunking/importance_score.py` | 108 |  | Importance score для chunk. Deterministic score на основе: * is_title (короткий chunk с se |
| `workspace/skills/legal_summarizer/scripts/llm/client.py` | 106 | yes | LLM-клиент (OpenAI-compatible HTTP API) — тонкая обёртка над общим клиентом. Единая реализ |
| `workspace/skills/legal_summarizer/scripts/retrieval/candidate_aggregator.py` | 105 |  | Объединение heading-кандидатов. Если несколько источников (DOCX style + numbering + regex  |
| `tools/apply_test_profile_tables.py` | 97 |  | apply_test_profile_tables.py — создать 6 runtime-таблиц профиля test. Эти таблицы перечисл |
| `workspace/skills/legal_summarizer/scripts/llm/tokens.py` | 95 |  | TokenEstimator — единая оценка токенов. Заменяет **разные** формулы оценки токенов, которы |
| `workspace/skills/legal_summarizer/scripts/llm/sanitize.py` | 86 | yes | Sanitize LLM responses — очистка chain-of-thought блоков и извлечение subject. Некоторые L |
| `lib/services/llm_usage_store_factory.py` | 85 | yes | Фабрика для ``LLMUsageStore`` (upstream nanobot). Создаёт ``nanobot.llm_usage.store.LLMUsa |
| `workspace/skills/legal_summarizer/scripts/chunking/_text_helpers.py` | 84 | yes | Text-helper утилиты для chunk layer. Маленькие чистые функции для разметки chunk-блоков и  |
| `workspace/skills/legal_summarizer/scripts/retrieval/normalizer.py` | 83 |  | Query normalization. Отдельный модуль для query normalization: * case (lowercase); * punct |
| `workspace/skills/legal_summarizer/scripts/document/loader.py` | 81 |  | DocumentLoader — единственный canonical loader для legal_summarizer. Создаёт ``PhysicalDoc |
| `workspace/skills/audit_analyzer/scripts/predefined/models.py` | 77 |  | Типизированные модели для predefined SQL-скриптов. Перенесено из ``workspace/skills/audit_ |
| `workspace/skills/legal_summarizer/scripts/application/context_builder.py` | 76 | yes | Execution context: run-level snapshot (selected chunks + strategy + plan). |
| `workspace/skills/legal_summarizer/scripts/application/inspection.py` | 73 |  | Inspection: document-level снимок анализа документа. Один canonical pipeline на запуск: `` |
| `workspace/skills/legal_summarizer/scripts/llm/config.py` | 72 |  | Обёртка над ``lib.core.skill_config`` для текущего skill'а (legal_summarizer). Все функции |
| `lib/services/vector_index_service.py` | 71 |  | Единый сервисный слой работы с векторными индексами. Собирает в одном месте операции build |
| `lib/utils/retry.py` | 70 |  | Универсальный retry с exponential backoff. Единая реализация повтора вызова при перечислен |
| `workspace/hooks/debug_stream_diag.py` | 70 |  | DEBUG-HOOK: StreamDiagnosisHook — временный диагностический хук. Регистрируется через ``wo |
| `workspace/skills/legal_summarizer/scripts/llm/prompts_runtime.py` | 69 |  | Загрузка системных промптов и формирование length/question инструкций. Модуль НЕ называетс |
| `workspace/skills/legal_summarizer/scripts/application/manifest_builder.py` | 66 |  | Manifest builder: построение NormalizedManifest для cache/state. |
| `tools/extract_office_structure.py` | 64 |  |  |
| `workspace/skills/legal_summarizer/scripts/application/document_io.py` | 62 |  | Document IO: извлечение plain text из файла документа. Тонкая обёртка над ``workspace.util |
| `workspace/skills/legal_summarizer/scripts/document/section_helpers.py` | 62 |  | Canonical section helpers для DocumentStructure. Чистые функции на ``DocumentStructure``:  |
| `workspace/utils/structure_cache.py` | 62 |  |  |
| `lib/utils/project_version.py` | 60 | yes | project_version — версия текущего проекта. Версия проекта (в отличие от версии библиотеки  |
| `workspace/skills/audit_analyzer/scripts/predefined/__init__.py` | 60 |  | Predefined SQL-режим навыка ``audit_analyzer``. Public API: * :func:`predefined.run` — вып |
| `workspace/skills/legal_summarizer/scripts/retrieval/question.py` | 55 |  | Question via retrieval index. Follow-up ``question`` должен использовать ``DocumentAnalysi |
| `workspace/skills/legal_summarizer/scripts/document/block_lookup.py` | 52 |  | Lookup helpers — замена linear ``index()``. В legacy коде (``context_expansion.py``, ``cac |
| `workspace/utils/jsonb.py` | 52 |  | Безопасное декодирование JSONB-значений, приходящих из psycopg2/asyncpg. psycopg2 при ``re |
| `lib/utils/node_access.py` | 49 |  | Доступ к вложенным dict/AttrDict-структурам по цепочке пути. Много где в проекте повторяла |
| `workspace/skills/legal_summarizer/scripts/retrieval/canonical.py` | 47 |  | Canonical retrieval wrapper. Использует только ``DocumentAnalysis.retrieve`` и canonical ` |
| `workspace/skills/legal_summarizer/scripts/application/operation_id.py` | 44 |  | Детерминированный operation_id (canonical id для manifest). |
| `workspace/skills/legal_summarizer/scripts/chunking/order.py` | 44 |  | Order-preserving utilities. Даже при ranking/retrieval порядок документа не должен уничтож |
| `workspace/utils/clean_text.py` | 44 |  | Каноническая санитизация текста сообщений/результатов инструментов. PostgreSQL не принимае |
| `workspace/skills/legal_summarizer/scripts/execution/config.py` | 42 |  | Execution-level конфигурация для HierarchicalReducer. Эти параметры описывают **execution  |
| `workspace/tools/__init__.py` | 24 | yes | Кастомные tool'ы проекта (auto-discover). Модули в этой директории сканируются ``RuntimePa |
| `lib/events/__init__.py` | 12 |  | Project-local event types extending nanobot.events.AgentEvent. Импортируются через ``bus.s |
| `benchmarks/__init__.py` | 4 |  | Набор бенчмарков nanobot — автоматическая оценка качества агента. |
| `lib/lifecycle/__init__.py` | 1 |  | Lifecycle: цикла запуска/перезапуска и graceful shutdown. |
| `workspace/skills/legal_summarizer/scripts/chunking/__init__.py` | 1 |  |  |
| `workspace/skills/legal_summarizer/scripts/document/__init__.py` | 1 |  |  |
| `workspace/skills/legal_summarizer/scripts/execution/__init__.py` | 1 |  |  |
| `workspace/skills/legal_summarizer/scripts/planning/__init__.py` | 1 |  |  |
| `lib/__init__.py` | 0 |  |  |
| `lib/hooks/__init__.py` | 0 |  |  |
| `lib/session/__init__.py` | 0 |  |  |
| `workspace/hooks/__init__.py` | 0 |  |  |
| `workspace/skills/__init__.py` | 0 | yes |  |
| `workspace/skills/legal_summarizer/__init__.py` | 0 |  |  |
