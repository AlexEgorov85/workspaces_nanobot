## Why

`cli_agent.py` и `gateway.py` сегодня вызывают `ApplicationContext.create(...)` с разными параметрами (`enable_audit`, `enable_cron`, `print_llm_calls`, `storage_override`, `session_override`, `profile`). Кроме того, **DuckDB-кэш разделён по ролям**: gateway пишет `<local_path>/cache.duckdb`, CLI пишет `<local_path>/cli.duckdb`. Это создаёт два независимых состояния локального data layer, что ломает принцип «единый operational data layer агента».

Также есть проблема composition layer: `ApplicationContext.create()` принимает `enable_*`-kwargs как constructor-args, что размывает границу «entrypoint-specific runtime flags» vs «shared runtime». И профиль (profile) передаётся как runtime-arg в `ApplicationContext.create()`, хотя это configuration-resolution concern, а не runtime concern.

Цель change: **единый composition root** для CLI и gateway. Один `ApplicationContext.create(role=..., ...)` для обоих. Профиль (profile) MUST быть разрешён ДО `ApplicationContext.create()` через `config._initialize_settings(profile=...)`. DuckDB — единый runtime-resource (один файл `cache.duckdb`), владение sync'ом определяется через PG-level claim (а не жёстко через `role`). Transport остаётся специфичным для каждого entrypoint'а (CLI = in-memory bus, gateway = PostgresChannel).

## What Changes

- **`ApplicationContext.create()` принимает обязательный kwarg `role: Literal["gateway", "cli"]`** и опциональные runtime-флаги для CLI: `storage_override`, `session_override`. **`profile` MUST NOT быть в сигнатуре `ApplicationContext.create()`** — профиль MUST быть разрешён ДО `create()` через `config._initialize_settings(profile=...)`. Параметры `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` MUST NOT быть именованными параметрами в typed signature `create(...)`; они MAY приниматься только через `**kwargs` для backward compat (см. compatibility boundary ниже).

  ```text
  ApplicationContext.create(
      script_dir: Path,
      workspace_dir: Path,
      *,
      role: Literal["gateway", "cli"],
      # CLI-only:
      storage_override: str | None = None,
      session_override: str | None = None,
      **kwargs,  # deprecated: enable_db_logging, enable_audit, enable_cron, print_llm_calls
  )
  ```

- **`role` определяет entrypoint composition**, не поведение `AgentLoop` и не владение DuckDB. `role` MUST NOT представлять environment, profile, deployment mode, storage ownership, или runtime behavior. `role` MUST NOT определять DuckDB producer/consumer status.

  | Сервис | `role="gateway"` | `role="cli"` |
  |---|---|---|
  | `AgentLoop` (hooks, runtime patches, skills, tools, memory) | ✅ | ✅ |
  | `DbLoggingService` (если `gateway.enable_db_logging=True`) | ✅ | ✅ |
  | `SessionManager` | ✅ | ✅ |
  | `RuntimeEventsSubscriber` | ✅ | ✅ |
  | `RuntimePatcher.apply_all()` | ✅ | ✅ |
  | `DuckDbCacheStore` (открывает `<local_path>/cache.duckdb`) | ✅ | ✅ |
  | `PostgresChannel` (worker pool) | ✅ | ❌ |
  | `PgDuckDbSyncService` (sync — только если OWNER) | ✅ | ✅ |
  | `CronService` (если `gateway.enable_cron=True`) | ✅ | ❌ |
  | WebSocket port check (вызывается из entrypoint) | ✅ | ❌ |
  | Console I/O (in-memory bus) | ❌ | ✅ |

- **PG-level ownership claim для DuckDB-sync.** Никто из entrypoint'ов не назначен жёстко producer'ом. Lifecycle MUST быть строго:

  ```text
  1. resolve cache.duckdb path (через resolve_publish_path)
  2. attempt atomic PG ownership claim (CacheOwnershipCoordinator.try_claim)
  3. determine access mode:
     - READ_WRITE (OWNER)
     - READ_ONLY (READER)
  4. open DuckDB using the determined access mode (DuckDbCacheStore.open(mode=...))
  5. create PgDuckDbSyncService only for OWNER (READ_WRITE)
  ```

  Ownership MUST быть определён ДО открытия DuckDB в любом режиме. `PgDuckDbSyncService` MUST NOT решать DuckDB access mode — это ответственность `CacheOwnershipCoordinator`.

  Сценарии:
  - **Только CLI запущен** → CLI захватывает ownership, становится producer, синхронизирует cache, skills работают.
  - **Только gateway запущен** → gateway захватывает ownership, становится producer.
  - **И CLI, и gateway запущены** → первый запущенный — producer; второй — consumer.
  - **Producer умер (kill -9)** → claim становится stale через TTL → следующий процесс при старте перехватывает ownership.

- **Ownership contract (atomic claim + fencing):**
  - **Ownership key:** ровно один активный claim на resource `<local_path>/cache.duckdb` (фиксированный ключ `'duckdb_cache'`). НЕ per-process, НЕ per-role.
  - **Atomic claim:** `INSERT ... ON CONFLICT (resource_key) DO UPDATE SET ... WHERE agent_cache_ownership.expires_at < NOW() RETURNING owner_id`. Два процесса одновременно делают claim → ровно один получает `READ_WRITE`, остальные — `READ_ONLY`.
  - **Fencing старого producer:** если процесс потерял ownership (heartbeat expired или его claim был перезаписан другим процессом), он MUST прекратить любые записи в DuckDB и остановить `PgDuckDbSyncService`. Sync поток MAY проверять `coord.is_still_owner()` перед каждой записью или подписаться на уведомление о потере ownership.

- **Единый snapshot-путь: `<local_path>/cache.duckdb`** для всех процессов. Никаких `cli.duckdb` или других role-based путей.

- **`AgentLoop` остаётся transport-agnostic.** Никаких изменений в `nanobot.AgentLoop`, `nanobot.MessageBus`, `BaseChannel`. CLI продолжает использовать `bus.publish_inbound(InboundMessage(channel="cli", ...))` и `bus.consume_outbound()`.

- **Cron = gateway-only.** `CronService` создаётся ТОЛЬКО при `role="gateway"` (если `gateway.enable_cron=True`). CLI НЕ запускает `CronService`. BREAKING change.

- **`/compact` в CLI остаётся локальным shortcut:** вызов `ContextCompactionService.compact(...)` напрямую из REPL.

- **CLI = фиксированный профиль `test`.** `cli_agent.py` MUST NOT принимать `--profile` CLI-аргумент и MUST NOT читать профиль из env. CLI hardcode'ит `profile="test"` при вызове `config._initialize_settings(profile="test")`. CLI — локальный test/dev entrypoint. Gateway entrypoint сохраняет `--profile` механизм.

- **Удаление Streamlit — отдельный change `remove-streamlit-runtime`** (НЕ часть текущего change). Текущий change содержит ТОЛЬКО запрет импорта `streamlit` в runtime-коде через Forbidden Behavior.

## Capabilities

### New Capabilities

- `runtime/entrypoints`: контракт application entrypoint'ов (`cli_agent.py`, `gateway.py`). Описывает: единая typed signature `ApplicationContext.create(role=...)` (без `profile` и без `enable_*` в публичных kwargs; `enable_*` MAY приниматься через `**kwargs` для backward compat), composition-rules для каждой роли. Контракт `CacheOwnershipCoordinator` (НИКОГДА не открывать DuckDB в RW режиме до определения ownership; explicit numbered lifecycle). Контракт «DuckDB — runtime-resource с PG-level ownership claim через отдельную таблицу `agent_cache_ownership` с ownership key = `duckdb_cache`, atomic INSERT ... ON CONFLICT, fencing старого producer». Контракт запрета Streamlit в runtime-коде. AgentLoop MUST быть transport-agnostic.

### Modified Capabilities

- `runtime/context`: добавляется требование «`ApplicationContext.create()` MUST иметь typed signature БЕЗ `profile` (профиль resolved до через `_initialize_settings(profile=...)`); MUST NOT содержать именованные параметры `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` (MAY приниматься через `**kwargs` для backward compat с DeprecationWarning). CLI и gateway вызывают `ApplicationContext.create(...)` с одинаковой typed signature; различие только в `role` и CLI-runtime-флагах `storage_override`, `session_override`. Deprecated kwargs через `**kwargs` MUST быть удалены в MINOR релизе после раскрытия этого change».

- `data/cache-provider`: добавляется требование «`cache.duckdb` MUST быть единым runtime-ресурсом, открываемым в `ApplicationContext.create()` для `role="gateway"` и `role="cli"` в режиме `READ_WRITE` или `READ_ONLY` в зависимости от результата `CacheOwnershipCoordinator.try_claim()`. Никаких role-based путей (`cli.duckdb`, `gateway.duckdb`). `CacheProvider` MUST предоставлять API с явным `mode=READ_WRITE|READ_ONLY` параметром; `READ_ONLY` MUST блокировать все мутации (INSERT/UPDATE/DELETE). `CacheProvider` MUST reject путь на NFS до открытия DuckDB».

- `configuration/profiles`: добавляется требование «`cli_agent.py` MUST NOT принимать `--profile` CLI-аргумент и MUST NOT читать профиль из переменных окружения; CLI MUST hardcode `profile="test"` при вызове `config._initialize_settings(profile="test")`. CLI — локальный test/dev entrypoint. Gateway MAY принимать `--profile`. После resolution runtime-компоненты НЕ ДОЛЖНЫ ветвиться по `profile == "test"`. `ApplicationContext.create()` MUST NOT принимать `profile` как параметр — профиль MUST быть resolved до `create()` через `_initialize_settings(profile=...)`».

## Impact

- `lib/core/application_context.py`:
  - Убрать из typed signature `profile`, `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls`.
  - Добавить обязательный kwarg `role: Literal["gateway","cli"]`.
  - Добавить `**kwargs` для backward compat (deprecated `enable_*`).
  - Реализовать composition matrix (см. таблицу выше).
  - DuckDB lifecycle (см. design.md D4): MUST создавать `CacheOwnershipCoordinator` → `try_claim()` → `DuckDbCacheStore.open(mode=...)` → (если OWNER) `PgDuckDbSyncService`.

- Новый модуль `lib/services/cache_ownership.py`:
  - `class CacheAccessMode(enum.Enum)`: `READ_WRITE`, `READ_ONLY`.
  - `class CacheOwnershipCoordinator`:
    - `__init__(worker_id: str, dsn: str, resource_key: str = "duckdb_cache", ttl_seconds: int = 60)`.
    - `def try_claim(self) -> CacheAccessMode` — atomic claim через PG (single row keyed by `resource_key`, INSERT ... ON CONFLICT DO UPDATE).
    - `def heartbeat(self) -> None` — обновляет `last_heartbeat_at` и `expires_at`.
    - `def release(self) -> None` — DELETE row.
    - `def is_still_owner(self) -> bool` — проверка текущего владельца; используется sync-потоком для fencing.

- SQL schema:
  - Новая таблица `agent_cache_ownership` через миграцию `sql/migrations/<NN>_create_agent_cache_ownership.sql`:
    ```sql
    CREATE TABLE agent_cache_ownership (
        resource_key VARCHAR PRIMARY KEY,           -- fixed: 'duckdb_cache'
        owner_id VARCHAR NOT NULL,                    -- worker_id of current owner
        acquired_at TIMESTAMP NOT NULL DEFAULT NOW(),
        last_heartbeat_at TIMESTAMP NOT NULL DEFAULT NOW(),
        expires_at TIMESTAMP NOT NULL
    );
    ```
  - Применяется через `tools/migrate.py --apply`.

- `lib/services/cache_provider.py` / `lib/services/cache_provider_impl.py`:
  - `CacheProvider.open(path: str, mode: CacheAccessMode) -> CacheProvider` — явный параметр.
  - `READ_ONLY` блокирует INSERT/UPDATE/DELETE (`ReadOnlyAssertionError`).
  - `READ_WRITE` разрешает мутации.

- `lib/services/pg_duckdb_sync_service.py`:
  - Создаётся ТОЛЬКО при `mode=READ_WRITE`. В `READ_ONLY` режиме = `None`.
  - Sync-поток MUST проверять `coord.is_still_owner()` перед каждой записью (fencing); если `False` → остановить sync.

- `gateway.py`:
  - Убрать kwargs `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` из `ApplicationContext.create(...)`.
  - Передавать `role="gateway"`. НЕ передавать `profile` (resolved до).
  - `_check_websocket_port_available` остаётся (server-only pre-startup check).

- `cli_agent.py`:
  - Убрать `--profile` argparse argument.
  - Hardcode `config._initialize_settings(profile="test")`.
  - Убрать kwargs `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` из `ApplicationContext.create(...)`.
  - Передавать `role="cli"`.
  - `lib/cli/console_loop.py::run_repl` остаётся без изменений.

- Standalone utilities (`tools/build_vectors.py`, `tools/check_worker_pool_integrity.py`, и др.):
  - НЕ модифицируются в этом change. Они продолжают использовать deprecated kwargs через `**kwargs` (`ApplicationContext.create(..., enable_audit=False, ...)`). Миграция utilities на typed signature — отдельный future change.

- Тесты:
  - `tests/test_application_context_role.py` (новый) — composition matrix.
  - `tests/test_cache_ownership_claim.py` (новый) — atomic claim: первый = OWNER, второй = READER; stale takeover; concurrent claim → exactly one owner; old owner fencing.
  - `tests/test_cache_provider_mode.py` (новый) — `READ_ONLY` блокирует мутации.
  - `tests/test_cache_provider_role_paths.py` (новый) — `resolve_publish_path(role="cli") == resolve_publish_path(role="gateway") == cache.duckdb`.
  - `tests/test_cli_uses_in_memory_bus.py` (новый) — REPL использует bus, не PostgresChannel.
  - `tests/test_agent_loop_transport_agnostic.py` (новый) — AgentLoop работает только через bus.
  - `tests/test_streamlit_imports_removed.py` (новый) — `grep` подтверждает отсутствие Streamlit в runtime-коде.
  - `tests/test_cli_no_profile.py` (новый) — CLI не принимает `--profile`, hardcodes test, ignores env.
  - `tests/test_gateway_accepts_profile.py` (новый) — gateway принимает `--profile=prod` и `--profile=test`.
  - Существующие тесты с `ApplicationContext.create(enable_audit=True/False, ...)` — продолжают работать (deprecated kwargs через `**kwargs`).

- Документация:
  - `README.md` — обновить описание архитектуры (см. user feedback #11).
  - `AGENTS.md` (этот файл) — секция «Project Layout» обновляется.
  - `docs/ARCHITECTURE.md` — секции «Composition Root», «CacheOwnershipCoordinator», «DuckDB ownership», «CLI profile».
  - `docs/INTERNAL_API.md` — секции «Role-based composition», «CacheOwnershipCoordinator», «CacheProvider API», «CLI = fixed test profile».
  - `CHANGELOG.md` — `Added` (composition unification, CacheOwnershipCoordinator, CacheProvider mode API); `Changed` (BREAKING: cron = gateway-only; CLI = fixed test profile, не принимает --profile); `Deprecated` (`enable_*` через `**kwargs`).

## Open Questions

Решены в proposal.md Open Questions:

1. **PG-level claim mechanism: extend `agent_worker_claims` или новая таблица `agent_cache_ownership`?** — **Новая таблица** с `resource_key` (NOT per-process).
2. **Heartbeat интервал и TTL** — heartbeat 30 сек, TTL 60 сек (design.md D4).
3. **`role="utility"`** — НЕ включается в этот change; standalone utilities остаются на `**kwargs`.
4. **Streamlit removal** — отдельный change `remove-streamlit-runtime`.

## Что принципиально НЕ делается в этом change

- Изменение `nanobot.MessageBus`, `nanobot.AgentLoop`, `BaseChannel` (transport layer).
- Заставлять CLI использовать `PostgresChannel` для I/O (CLI остаётся на in-memory bus).
- Создание `TerminalChannel` или другого нового transport-класса.
- Изменение схемы `agent_messages` / `agent_conversation_messages`.
- Передача worker pool задач между CLI и gateway (worker pool остаётся gateway-only).
- Удаление Streamlit — отдельный change `remove-streamlit-runtime`.
- Добавление `role="utility"` — standalone utilities остаются на deprecated kwargs.
- Изменение `gateway.compact.*` / других `gateway.*` ключей, не упомянутых в proposal.
- Полный отказ от in-memory bus — он остаётся для `agent.run()` в обоих режимах.
- Удаление `WebSocket` канала как такового — он остаётся gateway transport.
- Переписывание `lib/cli/console_loop.py::run_repl`.