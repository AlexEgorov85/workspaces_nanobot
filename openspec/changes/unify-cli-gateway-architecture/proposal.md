## Why

`cli_agent.py` и `gateway.py` сегодня вызывают `ApplicationContext.create(...)` с разными параметрами (`enable_audit`, `enable_cron`, `print_llm_calls`, `storage_override`, `session_override`). Кроме того, **DuckDB-кэш разделён по ролям**: gateway пишет `<local_path>/cache.duckdb`, CLI пишет `<local_path>/cli.duckdb`. Это создаёт два независимых состояния локального data layer, что ломает принцип «единый operational data layer агента».

Также есть проблема composition layer: `ApplicationContext.create()` принимает 5 `enable_*`-kwargs как constructor-args, что размывает границу «entrypoint-specific runtime flags» vs «shared runtime». И Streamlit — server-only зависимость — зашит в `gateway.py`.

Цель change: **единый composition root** для CLI и gateway. Один `ApplicationContext.create(role=..., ...)` для обоих, отличается только параметр `role` и опциональные CLI-флаги. DuckDB — единый runtime-resource (один файл `cache.duckdb`), владение синком определяется на уровне runtime через PG-level claim (а не жёстко через `role`). Transport остаётся специфичным для каждого entrypoint'а (CLI = in-memory bus, gateway = PostgresChannel).

## What Changes

- **`ApplicationContext.create()` принимает обязательный kwarg `role: Literal["gateway", "cli", "utility"]`** и опциональные runtime-флаги для CLI: `storage_override`, `session_override`. Параметры `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` помечаются deprecated в kwargs — фабрика читает их из `SETTINGS["gateway"].*`, если kwargs не переданы.

  ```text
  ApplicationContext.create(
      script_dir=...,
      workspace_dir=...,
      role="cli" | "gateway" | "utility",
      # CLI-only:
      storage_override=None | "auto" | "postgres" | "file",
      session_override=None | "<name>",
  )
  ```

- **`role` определяет transport-composition**, не поведение `AgentLoop` и не владение DuckDB:

  | Сервис | `role="gateway"` | `role="cli"` | `role="utility"` |
  |---|---|---|---|
  | `AgentLoop` (hooks, runtime patches, skills, tools, memory) | ✅ | ✅ | ✅ |
  | `DbLoggingService` (если `gateway.enable_db_logging=True`) | ✅ | ✅ | ✅ |
  | `SessionManager` | ✅ | ✅ | ✅ |
  | `RuntimeEventsSubscriber` | ✅ | ✅ | ✅ |
  | `RuntimePatcher.apply_all()` | ✅ | ✅ | ✅ |
  | `DuckDbCacheStore` (открывает `<local_path>/cache.duckdb`) | ✅ | ✅ | ❌ |
  | `PostgresChannel` (worker pool) | ✅ | ❌ | ❌ |
  | `PgDuckDbSyncService` (sync — если claim выдан) | ✅ | ✅ | ❌ |
  | `CronService` (если `gateway.enable_cron=True`) | ✅ | ✅ | ❌ |
  | WebSocket port check (вызывается из entrypoint) | ✅ | ❌ | ❌ |
  | Console I/O (in-memory bus) | ❌ | ✅ | ❌ |

  DuckDB — **не gateway-specific resource**. Это runtime-resource: открывается в `ApplicationContext.create()` всегда (если `gateway.enable_audit=True`), владение sync'ом определяется через PG-level ownership claim (см. ниже).

- **PG-level ownership claim для DuckDB-sync.** Никто из entrypoint'ов не назначен жёстко producer'ом. При старте `PgDuckDbSyncService` пытается арендовать cache ownership через PG (через существующую таблицу `agent_worker_claims`, расширенную claim_type='cache', или отдельную таблицу `agent_cache_ownership`). Кто первый арендовал — тот producer; остальные процессы становятся consumer'ами (открывают cache read-only, sync не запускают).

  Сценарии:
  - **Только CLI запущен** → CLI захватывает ownership, становится producer, синхронизирует cache, skills работают.
  - **Только gateway запущен** → gateway захватывает ownership, становится producer.
  - **И CLI, и gateway запущены** → первый запущенный — producer; второй — consumer (читает snapshot первого).
  - **Producer умер** → следующий процесс при старте видит stale claim (TTL > N секунд без heartbeat) и захватывает ownership.

- **Единый snapshot-путь: `<local_path>/cache.duckdb`** для всех процессов. Никаких `cli.duckdb` или других role-based путей.

- **`AgentLoop` остаётся transport-agnostic.** Никаких изменений в `nanobot.AgentLoop`, `nanobot.MessageBus`, `BaseChannel`. CLI продолжает использовать `bus.publish_inbound(InboundMessage(channel="cli", ...))` и `bus.consume_outbound()` — это даёт унификацию через общий bus.

- **`gateway.py` больше не спавнит Streamlit subprocess.** Удаляются `SubprocessManager.spawn_streamlit` (или весь модуль), `_streamlit_enabled()`, упоминания `streamlit_app.py` в `gateway.py`. `streamlit.*`-секция в `project.json` оставляется как permissive (pydantic `extra="allow"`), но runtime её игнорирует.

- **Cron:** контролируется `gateway.enable_cron` (default `True`). Работает и в CLI, и в gateway, если включено.

- **`/compact` в CLI остаётся локальным shortcut:** вызов `ContextCompactionService.compact(...)` напрямую из REPL.

## Capabilities

### New Capabilities

- `runtime/entrypoints`: контракт application entrypoint'ов (`cli_agent.py`, `gateway.py`, standalone utilities). Описывает: единая сигнатура `ApplicationContext.create(role=...)` (без `enable_*` в публичной kwargs), composition-rules для каждой роли, разделение «shared runtime» (AgentLoop, Skills, Tools, Memory, Session, Logging, DuckDB) и «role-specific transport» (CLI = Console I/O через in-memory bus, gateway = PostgresChannel). Контракт «DuckDB — runtime-resource с PG-level ownership claim» (никаких apply for `клип-унечный` snapshot; ownership выдаётся первому процессу через PG claim). Контракт запрета Streamlit в runtime-коде. AgentLoop MUST быть transport-agnostic.

### Modified Capabilities

- `runtime/context`: добавляется требование «`ApplicationContext.create()` MUST принимать обязательный kwarg `role`; флаги `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` MUST NOT входить в публичной обязательную сигнатуру, читаются из `SETTINGS["gateway"].*` если не переданы; deprecated kwargs MAY приниматься для обратной совместимости с тестами и интегрируемыми утилитами и MUST быть удалены в следующем MINOR после раскрытия».

- `data/cache-provider`: добавляется требование «`cache.duckdb` MUST быть единым runtime-ресурсом, открываемым в `ApplicationContext.create()` для `role="gateway"` и `role="cli"`. Владение sync'ом MUST определяться через PG-level ownership claim (см. `runtime/entrypoints`): первый захвативший claim процесс становится producer, остальные — consumer'ами. Никаких role-based путей (`cli.duckdb`, `gateway.duckdb`) — только `<local_path>/cache.duckdb`».

## Impact

- `lib/core/application_context.py`:
  - Добавить обязательный kwarg `role: Literal["gateway","cli","utility"] = "gateway"` (default для backward compat).
  - Параметры `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` помечаются deprecated в kwargs; если kwargs не передан — читается из `SETTINGS["gateway"].*`; если передан — используется kwargs + `warnings.warn(..., DeprecationWarning, stacklevel=2)`.
  - Composition matrix (см. таблицу выше) реализуется в `ApplicationContext.create()`.
  - `resolve_publish_path(role=role)` остаётся в API, но `role="cli"` и `role="gateway"` возвращают **один и тот же путь** `<local_path>/cache.duckdb` (для backward compat с существующими вызовами); `role="utility"` MAY возвращать тот же путь, но `CacheProvider` для utility не открывается (см. матрицу).

- `lib/services/pg_duckdb_sync_service.py` (или новый модуль `lib/services/cache_ownership.py`):
  - Новый модуль `cache_ownership.py::try_claim_cache_ownership(worker_id, ...) -> bool` использует PG-транзакцию с `agent_worker_claims` (расширенную `claim_type='cache'`) или отдельную таблицу `agent_cache_ownership`.
  - `PgDuckDbSyncService.start()` сначала вызывает `try_claim_cache_ownership(...)`; если `False` — service работает в consumer-mode (без фонового sync, только открывает snapshot для чтения).
  - Heartbeat claim каждые N секунд (default `30`); cleanup при shutdown.

- SQL schema (`sql/` или `sql/migrations/`):
  - Расширение `agent_worker_claims` колонкой `claim_type VARCHAR DEFAULT 'task' NOT NULL` или создание отдельной таблицы `agent_cache_ownership(worker_id PRIMARY KEY, acquired_at TIMESTAMP, last_heartbeat_at TIMESTAMP, status VARCHAR)`.
  - Миграция через `tools/migrate.py --apply`.

- `gateway.py`:
  - Убрать kwargs `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` из `ApplicationContext.create(...)`.
  - Передавать `role="gateway"`.
  - Удалить блок `_streamlit_enabled()` + `subprocess_manager.spawn_streamlit(...)` + импорт `SubprocessManager`.
  - `_check_websocket_port_available` остаётся (server-only pre-startup check).

- `cli_agent.py`:
  - Убрать те же kwargs из `ApplicationContext.create(...)`.
  - Передавать `role="cli"`.
  - `lib/cli/console_loop.py::run_repl` остаётся без изменений: `bus.publish_inbound(InboundMessage(channel="cli", ...))` + `bus.consume_outbound()`.

- `lib/services/cache_provider_impl.py::resolve_publish_path`:
  - `role` параметр сохраняется для backward compat, но `role="cli"` и `role="gateway"` возвращают **`<local_path>/cache.duckdb`** (один путь для обоих).

- Standalone utilities (`tools/build_vectors.py` и др.):
  - Все вызовы `ApplicationContext.create(...)` должны передавать `role="utility"` явно (или default `role="gateway"`, если утилита только читает snapshot).

- `lib/services/subprocess_manager.py::spawn_streamlit`:
  - Удаляется; если других методов нет — модуль удаляется целиком.

- `streamlit_app.py`: удаляется файл целиком.

- Тесты:
  - Существующие тесты с `ApplicationContext.create(enable_audit=True/False, ...)` — продолжают работать (deprecated kwargs поддерживаются).
  - `tests/test_application_context_role.py` (новый) — composition matrix.
  - `tests/test_cache_ownership_claim.py` (новый) — `try_claim_cache_ownership` testable: первый процесс — producer, второй — consumer; stale claim takeover.
  - `tests/test_cache_provider_role_paths.py` (новый) — `resolve_publish_path(role="cli") == resolve_publish_path(role="gateway") == cache.duckdb`.
  - `tests/test_streamlit_removed.py` (новый) — отсутствие Streamlit-импортов.

- Документация:
  - `AGENTS.md` (этот файл) — секция «Project Layout» обновляется.
  - `README.md` — удаление Streamlit.
  - `docs/ARCHITECTURE.md` — единый composition root, runtime-level cache coordination.
  - `docs/INTERNAL_API.md` — секция «Role-based composition», «PG-level Cache Claim».
  - `CHANGELOG.md` — `Added`/`Removed`/`Changed`.

## Open Questions

Переносятся в `design.md`:

1. **PG-level claim mechanism: extend `agent_worker_claims` или новая таблица `agent_cache_ownership`?** — design.md определит.
2. **Heartbeat интервал и TTL для stale-claim takeover** — design.md определит конкретные числа (default 30 sec heartbeat, 90 sec TTL).
3. **`role="utility"` — открывает ли cache_store для чтения?** — design.md определит (вероятно NO — embedding-утилиты обычно читают PG напрямую, не cache).

## Что принципиально НЕ делается в этом change

- Изменение `nanobot.MessageBus`, `nanobot.AgentLoop`, `BaseChannel` (transport layer).
- Заставлять CLI использовать `PostgresChannel` для I/O (CLI = in-memory bus).
- Создание `TerminalChannel` или другого нового transport-класса.
- Изменение схемы `agent_messages` / `agent_conversation_messages`.
- Передача worker pool задач между CLI и gateway (worker pool остаётся gateway-only).
- Рефакторинг `lib/services/db_logging_bus.py`, `lib/services/runtime_events_subscriber.py`.
- Изменение `gateway.compact.*` / других `gateway.*` ключей, не упомянутых в proposal.
- Полный отказ от in-memory bus — он остаётся для `agent.run()` в обоих режимах.
- Удаление `WebSocket` канала как такового — он остаётся gateway transport.
- Переписывание `lib/cli/console_loop.py::run_repl`.