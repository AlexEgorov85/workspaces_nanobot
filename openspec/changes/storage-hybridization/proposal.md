## Why

Проект построен поверх `nanobot-ai 0.3.5`, но наш слой хранения
остаётся монолитным: `PGSessionManager` хранит **все** сессии в
PostgreSQL, `DbLoggingService` пишет **все** structured events
(включая LLM usage) в `agent_gateway_logs`. Это создаёт три
системные проблемы, которые блокируют развитие.

1. **Дублирование с upstream.** Upstream `nanobot 0.3.5` уже
   поставляет:
   - `nanobot.session.manager.SessionManager` + `JsonlSessionStore`
     — горячее хранилище сессий в JSONL (быстрый read/write на
     локальном диске, никаких сетевых задержек);
   - `nanobot.llm_usage.store.LLMUsageStore` — content-free
     metadata-only хранилище LLM usage (provider, model,
     duration_ms, tokens), наблюдаемое через
     `provider.set_llm_call_observer(record_llm_call)`;
   - observer-pipeline для записи LLM-вызовов без копирования
     payload в журнал (payload остаётся в `Session.messages`).
   Наш `PGSessionManager` (~410 строк) повторяет эту же логику,
   вынужден патчить `super().__init__()` и каждый апгрейд upstream
   ломает его сигнатуры.

2. **Семантическое смешение двух разных concerns в одном слое.**
   `DbLoggingService` сейчас пишет в `agent_gateway_logs` и
   content-rich события (`tool_call`/`tool_result` с payload), и
   metadata-only LLM-usage (где payload бессмысленен — upstream
   явно говорит: «request messages, response text, reasoning,
   and tool payloads deliberately do not belong to this
   contract»). Подключение upstream `LLMUsageStore` через
   observer-pipeline убирает это смешение: каждое семейство
   событий пишется в свой специализированный стор с правильной
   семантикой и retention-политикой.

3. **Барьер для multi-instance деплоя и disaster-recovery.**
   `PGSessionManager` хранит сессии в PG — это уже даёт
   durability. Но в upstream-сторону (`JsonlSessionStore`) это
   дополнительная точка отказа: если PG упал, **все** сессии
   (включая runtime-кэш для compaction и provider state)
   становятся недоступны. Upstream-стор должен быть
   автономным горячим путём, а PG — холодным backup/aggregator
   для multi-instance + observability.

## What Changes

### NEW: гибридная модель хранения сессий (горячий JSONL + холодный PG)

- `JsonlSessionStore` (upstream) становится **единственным
  горячим путём** для `get_or_create` / `save` / `read_session_*`
  / `fork_session_before_user_index` / `rename_model_preset`.
  Прямой `INSERT` в `agent_session_meta` /
  `agent_session_messages` остаётся только как **cold-storage
  mirror**, управляемый отдельным `SessionColdSyncService`.
- `PGSessionManager` остаётся, но меняет роль: теперь это
  `SessionManager` subclass, который **дополнительно**
  синхронизирует сессии в PG для multi-instance и
  observability, а не заменяет upstream JSONL-стор. Прямой SQL
  доступ к `agent_session_meta`/`agent_session_messages` в hot
  path запрещён (контракт `runtime/context` обновляется).
- Вводится `SessionColdSyncService` — фоновая задача в
  `ApplicationContext`, которая раз в N секунд зеркалит
  upstream JSONL → PG с `last-write-wins` по `updated_at`.
  В PG остаются таблицы `agent_session_meta` и
  `agent_session_messages` (без удаления схемы, без
  изменения DDL), но запись идёт только через этот
  sync-сервис.

### NEW: подключение upstream `LLMUsageStore` через observer-pipeline

- `LLMUsageStore` (`nanobot.llm_usage.store.LLMUsageStore`)
  инстанциируется в `ApplicationContext` и регистрируется
  через `provider.set_llm_call_observer(store.record)` (или
  эквивалентный hook в nanobot 0.3.5) при старте runtime.
- `DbLoggingService` продолжает писать content-rich события
  (`tool_call`, `tool_result`, `inbound`, `outbound`,
  `context_compacted`, `sync_*`) в `agent_gateway_logs` без
  изменений (контракт `logging-db` остаётся источником истины).
- Допускается **параллельная** запись LLM-usage в оба стора
  (upstream `LLMUsageStore` + `DbLoggingService` с
  `event_type="llm_usage"` metadata-полями), но это
  разные concerns: upstream — для UI-графиков и cost-tracking;
  `DbLoggingService` — для history_search и audit-trail.
  Контракт: `LLMUsageStore` — content-free per LLM call;
  `DbLoggingService` — content-rich для search/audit. См.
  `storage/usage-store`.

### MODIFIED: `runtime/context` — дополнение через ADDED Requirements

- Существующее требование «Состояние сессии вне
  ApplicationContext» сохраняется без изменений (текст
  заголовка и тела остаётся прежним, чтобы избежать
  конфликта MODIFIED-merge в OpenSpec — он требует
  точного совпадения заголовка).
- Дополнение делается через **ADDED Requirements** (а не
  через MODIFIED): добавляются два новых требования:
  - «Session hot-path state принадлежит upstream SessionManager»:
    hot-path owner сессий — upstream
    `nanobot.session.manager.SessionManager` (JSONL), а не
    кастомный writer через прямой SQL.
  - «Session cold-storage mirror в PostgreSQL»: PostgreSQL
    используется как cold-storage mirror через отдельный
    `SessionColdSyncService` (не primary storage).
- Существующее требование продолжает действовать
  параллельно с новыми. Семантическая связь
  фиксируется текстом обоих ADDED Requirements
  (явное упоминание upstream `SessionManager`).
  См. delta в `specs/runtime/context/spec.md`.

### Замечание про `logging-db`

Изменение `logging-db` (через параллельный change
`unify-agent-event-logging-pipeline`) уже фиксирует контракт
`DbLoggingService` как единственного writer `agent_gateway_logs`
и описывает `try_log_event` / `log_sync_event` API.
Текущий change `storage-hybridization` НЕ модифицирует
`logging-db` напрямую — он ссылается на её требования как на
внешний контракт и добавляет раздельные concerns в собственную
спеку `storage/usage-store`. Это исключает конфликт delta'ов
между двумя параллельными change'ами.

### Capabilities

### New Capabilities

- `storage/session-hybridization` — нормативный контракт на
  гибридную модель хранения сессий: upstream JSONL как hot path,
  PG как cold-storage mirror, маршрутизация между ними,
  contract для `SessionColdSyncService`.
- `storage/usage-store` — нормативный контракт на использование
  upstream `LLMUsageStore` для metadata-only LLM-usage;
  observer-pipeline; отсутствие конфликта с `DbLoggingService`.

### Modified Capabilities

- `runtime/context` — дополнение через ADDED Requirements:
  upstream `SessionManager` становится hot-path владельцем
  session state; `PGSessionManager` переходит в роль
  cold-storage mirror (не primary owner). Существующее
  требование «Состояние сессии вне ApplicationContext»
  сохраняется без изменений текста.

### Impact

- `lib/session/pg_session_manager.py` — переработка:
  убираются прямые `INSERT/UPDATE` в hot path, остаётся
  зеркалирование через `SessionColdSyncService`. Класс
  переименовывается в `PGSessionMirror` (отражает роль) или
  сохраняет имя с обновлённым docstring (см. design D2).
- `lib/services/session_storage.py` — фабрика остаётся
  (выбор `PGSessionManager` vs `SessionManager` по DSN), но
  меняется семантика: оба варианта теперь используют upstream
  `SessionManager` как hot path; `PGSessionManager` добавляет
  mirror-операции.
- `lib/core/application_context.py` — добавляется
  `_make_session_cold_sync_service()`, регистрация в `start()`,
  shutdown через `_lifecycle.shutdown`.
- `lib/core/agent_factory.py`, `lib/services/channel_factory.py`,
  `lib/services/runtime_patcher.py`, `workspace/hooks/active_files_hook.py` —
  docstring-правки: `PGSessionManager` теперь «cold-storage mirror
  поверх upstream `SessionManager`», а не «замена upstream».
- `lib/services/db_logging_service.py` — без изменений в hot
  path; добавление `register_llm_observer()` хука для
  `LLMUsageStore` (см. design D3).
- `tools/migrate_sessions_to_sqlite.py` — **не создаётся**:
  upstream `SessionManager` уже JSONL, миграция не нужна.
  Существующие данные в `agent_session_meta` /
  `agent_session_messages` остаются на диске для reference;
  новые сессии пишутся в upstream JSONL.
- `tools/export_usage_from_sqlite.py` — **не создаётся**:
  upstream `LLMUsageStore` сам управляет своим стором; мы
  только подключаемся к нему через observer.
- Контрактные тесты:
  - `tests/contract/test_session_manager_api.py` —
    фиксирует, что `SessionManager.get_or_create` /
    `save` / `list_sessions` существуют и работают на
    JSONL-сторе.
  - `tests/contract/test_usage_store_api.py` —
    фиксирует, что `LLMUsageStore.__init__(path)` /
    `record(LLMCallRecord)` / `recent_calls()` существуют и
    работают на SQLite-сторе.
  - `tests/contract/test_llm_observer_api.py` —
    фиксирует, что `LLMUsageStore.record` совместим с
    observer-hook в nanobot 0.3.5 (или эквивалентный путь
    подключения).
- `tests/test_pg_session_manager.py` — обновляется под новую
  роль (mirror-only). Тесты на запись в hot path через
  прямой SQL удаляются; тесты на mirror-операции
  (`sync_to_pg`, `load_from_jsonl`) добавляются.
- Документация:
  - `docs/architecture/storage-layers.md` — новая секция
    «Session storage: hot JSONL + cold PG mirror».
  - `docs/architecture/usage-tracking.md` — новая секция
    «LLM usage: upstream LLMUsageStore + DbLoggingService».
  - `docs/MIGRATION.md` — раздел про переход session
    storage на upstream JSONL (с указанием, что PG-таблицы
    остаются как cold-storage).
  - `CHANGELOG.md` — блок `## [Unreleased]`: `Changed`
    (session storage: hybrid model), `Added`
    (SessionColdSyncService, LLMUsageStore observer).

### Вне scope

- Изменение схемы `agent_session_meta`, `agent_session_messages`
  (таблицы остаются как cold-storage).
- Изменение схемы `agent_gateway_logs`, `agent_question_runs`
  (logging-db контракт не меняется в части схемы).
- Перенос vector store / worker-leases на upstream
  (`agent_vector_index_store` остаётся в PG — нужен BYTEA;
  `agent_worker_claims` остаётся в PG — нужен
  `FOR UPDATE SKIP LOCKED`).
- Изменение архитектуры vector pipeline: FAISS-индексы
  остаются **in-memory cache** над PG BYTEA-стором (см.
  спеку `data/vector-indexes`). При старте gateway FAISS
  rebuilds из `agent_vector_index_store` (см.
  `lib/services/preload_service.py`). Никаких двойных
  источников истины.
- Изменение `DbLoggingService` (контракт остаётся
  source-of-truth для content-rich audit; добавление
  `LLMUsageStore` идёт параллельно, не вместо).
- Изменение domain skills (`audit_analyzer`,
  `legal_summarizer`, `office_files`).
- Изменение профилей `prod`/`test`.
- Изменение benchmark suite.
- **Миграция legacy PG-сессий** в JSONL: исторические данные
  в `agent_session_meta` / `agent_session_messages`,
  созданные предыдущими версиями `PGSessionManager`,
  остаются read-only в PG и НЕ зеркалируются в JSONL.
  Доступ к ним — через отдельный `PGSessionLegacyReader`
  (см. `docs/MIGRATION.md` § «Legacy PG sessions»).
  Это явный и обратимый выбор; миграция — отдельный
  будущий change, если потребуется.

**UX-impact:** после deploy пользователи **не увидят** историю
сессий, созданных до этого change, в UI Streamlit / CLI /
Telegram. Это явный breaking change для пользователей;
документируется в `CHANGELOG.md` под `## [Unreleased]` →
`Changed` (с пометкой «legacy session history is not
visible after upgrade; run `tools/dump_legacy_session.py`
to inspect or migrate manually»). Оператор deploy должен
предупредить пользователей **до** deploy. Автоматическая
миграция legacy → JSONL — отдельный будущий change.
