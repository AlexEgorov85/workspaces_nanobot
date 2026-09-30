# Product modules with no static importer

`dynamic` = referenced only as a string (importlib / plugin scanning).
A module can legitimately have zero importers (entry points, test-profile
fixtures, module registries, stdlib-style plugin roots). Judge before deleting.

**57 of 215 product modules have no static importer.**

| Module | LOC | Dynamic-only refs | Docstring |
|---|---:|---|---|
| `workspace/utils/db.py` | 1063 | 0 | Единый коннектор к PostgreSQL / Greenplum через psycopg2. Архитектура — «одна очередь + пу |
| `workspace/skills/audit_analyzer/scripts/cli.py` | 604 | 0 | Точка входа: CLI с разбором аргументов и маршрутизацией по режимам. CLI — единая точка выз |
| `workspace/skills/legal_summarizer/scripts/cli.py` | 443 | 0 | Точка входа: CLI с разбором аргументов и запуском суммаризации. Phase 2B API: ``summarizer |
| `workspace/utils/session_file_store.py` | 431 | 0 |  |
| `workspace/skills/legal_summarizer/scripts/cli_query.py` | 330 | 0 | ``cli_query.py`` — follow-up-запросы по сохранённой operation_id. Нужен, чтобы агент мог о |
| `lib/core/skill_config.py` | 325 | 2 | Runtime API для skill'ов: конфигурация, таблицы, FAISS. Параметризован по ``skill_name``.  |
| `tools/generate_comments_sql.py` | 291 | 0 | Генератор apply_all_comments.sql из schema.json + extra описаний. Требует на входе ``works |
| `tools/check_indexes.py` | 285 | 0 | ``tools/check_indexes.py`` — declared-vs-runtime diff для vector-индексов. Сравнивает: * ` |
| `tools/validate_component_specs.py` | 274 | 0 | Валидация структуры компонентных спецификаций OpenSpec (component-spec-validation). Провер |
| `tools/migrate.py` | 252 | 0 | migrate.py — runner миграций схемы (sql/migrations/V*.sql). Порядок версий определяется но |
| `workspace/skills/legal_summarizer/scripts/application/canonical.py` | 244 | 0 | Canonical run pipeline. Этот модуль — **production-flow**, использующий только canonical p |
| `tools/check_worker_pool_integrity.py` | 201 | 0 | Диагностика целостности мульти-машинного пула воркеров. Проверяет инвариант ``processing ⇔ |
| `tools/audit_nanobot_contracts.py` | 194 | 0 | Аудит контрактов nanobot: AST нашего кода vs реальные символы nanobot 0.3.5. Что делает: 1 |
| `tools/scan_nanobot_inventory.py` | 176 | 0 | Одноразовый сканер nanobot-зависимостей: строит JSON-инвентарь. Запуск: python tools/scan_ |
| `tools/smoke_post_cleanup.py` | 176 | 0 | Smoke-test runtime для opencode change post-0.3.5-patches-cleanup. Запускает gateway в фон |
| `tools/release_v252.py` | 169 | 0 | Создать GitHub Release для тега v2.5.2 через gh CLI (fallback: curl). Режимы: --dry-run —  |
| `workspace/skills/legal_summarizer/scripts/llm/retry.py` | 155 | 0 | ChunkResultParseError + smart retry. Сейчас ошибка JSON в ответе LLM приводит к повторной  |
| `workspace/skills/legal_summarizer/scripts/document/safety_merge.py` | 147 | 0 | Safety net merge для микро-секций. После хорошего heading detection + repair остаются **кр |
| `workspace/skills/legal_summarizer/scripts/retrieval/quality.py` | 143 | 0 | Quality metrics. Метрики качества: * ``retrieval_recall_at_K``: доля reference questions,  |
| `tools/demo_internal_fallback.py` | 142 | 0 | Демонстрация fallback-ответа при internal-ошибке AgentLoop. Запускать:: PYTHONIOENCODING=u |
| `workspace/tools/example.py` | 131 | 0 | Шаблон кастомного tool'а проекта. Скопируйте файл, переименуйте класс и ``config_key``, до |
| `workspace/skills/legal_summarizer/scripts/retrieval/records.py` | 124 | 0 | SemanticRecord — структурированный output LLM map. LLM map возвращает не просто свободный  |
| `tools/release_v251.py` | 120 | 0 | Создать GitHub Release для тега v2.5.1 через gh CLI (fallback: curl). |
| `workspace/skills/legal_summarizer/scripts/document/block_ownership.py` | 109 | 0 | Canonical block ownership. Чистые функции на ``DocumentStructure``, которые определяют «ка |
| `workspace/skills/legal_summarizer/scripts/chunking/importance_score.py` | 108 | 0 | Importance score для chunk. Deterministic score на основе: * is_title (короткий chunk с se |
| `workspace/skills/legal_summarizer/scripts/llm/client.py` | 106 | 1 | LLM-клиент (OpenAI-compatible HTTP API) — тонкая обёртка над общим клиентом. Единая реализ |
| `workspace/skills/legal_summarizer/scripts/retrieval/candidate_aggregator.py` | 105 | 0 | Объединение heading-кандидатов. Если несколько источников (DOCX style + numbering + regex  |
| `tools/apply_test_profile_tables.py` | 97 | 0 | apply_test_profile_tables.py — создать 6 runtime-таблиц профиля test. Эти таблицы перечисл |
| `workspace/hooks/debug_stream_diag.py` | 70 | 0 | DEBUG-HOOK: StreamDiagnosisHook — временный диагностический хук. Регистрируется через ``wo |
| `tools/extract_office_structure.py` | 64 | 0 |  |
| `workspace/utils/structure_cache.py` | 62 | 0 |  |
| `workspace/skills/legal_summarizer/scripts/retrieval/question.py` | 55 | 0 | Question via retrieval index. Follow-up ``question`` должен использовать ``DocumentAnalysi |
| `workspace/skills/legal_summarizer/scripts/document/block_lookup.py` | 52 | 0 | Lookup helpers — замена linear ``index()``. В legacy коде (``context_expansion.py``, ``cac |
| `workspace/utils/jsonb.py` | 52 | 0 | Безопасное декодирование JSONB-значений, приходящих из psycopg2/asyncpg. psycopg2 при ``re |
| `workspace/skills/legal_summarizer/scripts/retrieval/canonical.py` | 47 | 0 | Canonical retrieval wrapper. Использует только ``DocumentAnalysis.retrieve`` и canonical ` |
| `workspace/skills/legal_summarizer/scripts/chunking/order.py` | 44 | 0 | Order-preserving utilities. Даже при ranking/retrieval порядок документа не должен уничтож |
| `workspace/utils/clean_text.py` | 44 | 0 | Каноническая санитизация текста сообщений/результатов инструментов. PostgreSQL не принимае |
| `workspace/tools/__init__.py` | 24 | 5 | Кастомные tool'ы проекта (auto-discover). Модули в этой директории сканируются ``RuntimePa |
| `lib/events/__init__.py` | 12 | 0 | Project-local event types extending nanobot.events.AgentEvent. Импортируются через ``bus.s |
| `workspace/skills/audit_analyzer/scripts/__init__.py` | 7 | 0 | CLI-обвязка навыка ``audit_analyzer``. Точка входа: ``python scripts/cli.py --mode ...``.  |
| `benchmarks/__init__.py` | 4 | 0 | Набор бенчмарков nanobot — автоматическая оценка качества агента. |
| `lib/lifecycle/__init__.py` | 1 | 0 | Lifecycle: цикла запуска/перезапуска и graceful shutdown. |
| `tools/__init__.py` | 1 | 0 | Project-level dev tooling (not part of any skill package). |
| `workspace/skills/legal_summarizer/scripts/application/__init__.py` | 1 | 0 |  |
| `workspace/skills/legal_summarizer/scripts/cache/__init__.py` | 1 | 0 |  |
| `workspace/skills/legal_summarizer/scripts/chunking/__init__.py` | 1 | 0 |  |
| `workspace/skills/legal_summarizer/scripts/document/__init__.py` | 1 | 0 |  |
| `workspace/skills/legal_summarizer/scripts/execution/__init__.py` | 1 | 0 |  |
| `workspace/skills/legal_summarizer/scripts/output/__init__.py` | 1 | 0 |  |
| `workspace/skills/legal_summarizer/scripts/planning/__init__.py` | 1 | 0 |  |
| `workspace/skills/legal_summarizer/scripts/retrieval/__init__.py` | 1 | 0 |  |
| `lib/__init__.py` | 0 | 0 |  |
| `lib/hooks/__init__.py` | 0 | 0 |  |
| `lib/session/__init__.py` | 0 | 0 |  |
| `workspace/hooks/__init__.py` | 0 | 0 |  |
| `workspace/skills/__init__.py` | 0 | 3 |  |
| `workspace/skills/legal_summarizer/__init__.py` | 0 | 0 |  |
