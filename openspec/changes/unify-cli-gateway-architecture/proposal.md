## Why

`cli_agent.py` и `gateway.py` сегодня вызывают `ApplicationContext.create(...)` с разными параметрами (`enable_audit`, `enable_cron`, `print_llm_calls`, `storage_override`, `session_override`, `profile`). Кроме того, **cache storage-кэш разделён по ролям** (gateway → `cache.cache storage`, CLI → `cli.cache storage`), и `enable_audit` смешивает два разных concerns: наличие cache runtime и audit sync.

Также есть проблема composition layer: `ApplicationContext.create()` принимает `enable_*`-kwargs и `profile` как constructor-args, что размывает границу «entrypoint-specific runtime flags» vs «shared runtime». И Streamlit — server-only зависимость — зашит в `gateway.py`.

Цель change: **единый composition root** для CLI и gateway. Один `ApplicationContext.create(role=..., ...)` для обоих. Профиль (profile) MUST быть разрешён ДО `create()` через `config._initialize_settings(profile=...)`. cache storage — единый runtime-resource (один файл `cache.cache storage`), lifecycle которого не зависит от `enable_audit`. Audit sync — отдельный concern, контролируется `enable_audit`. Владение sync'ом определяется через PG-level claim с generation/fencing token. Transport остаётся специфичным для каждого entrypoint'а (CLI = in-memory bus, gateway = PostgresChannel).

## What Changes

- **`ApplicationContext.create()` принимает обязательный kwarg `role: Literal["gateway", "cli"]`** и опциональные runtime-флаги для CLI: `storage_override`, `session_override`. **`profile` MUST NOT быть в сигнатуре** — профиль MUST быть разрешён ДО `create()`. Параметры `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` MUST NOT быть именованными параметрами в typed signature; они MAY приниматься только через `**kwargs` для backward compat.

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

- **`role` определяет entrypoint composition**, не поведение `AgentLoop` и не владение cache storage. `role` MUST NOT представлять environment, profile, deployment mode, storage ownership, или runtime behavior. `role` MUST NOT определять cache storage producer/consumer status — это ответственность `CacheOwnershipCoordinator`.

  | Сервис | `role="gateway"` | `role="cli"` |
  |---|---|---|
  | `AgentLoop` (hooks, runtime patches, skills, tools, memory) | ✅ | ✅ |
  | `DbLoggingService` (если `gateway.enable_db_logging=True`) | ✅ | ✅ |
  | `SessionManager` | ✅ | ✅ |
  | `RuntimeEventsSubscriber` | ✅ | ✅ |
  | `RuntimePatcher.apply_all()` | ✅ | ✅ |
  | `CacheProvider` (если `gateway.cache` настроен) | ✅ | ✅ |
  | `CacheOwnershipCoordinator` (если `gateway.cache` настроен) | ✅ | ✅ |
  | `cache storageCacheStore` (открывает `<local_path>/cache.cache storage` в режиме claim) | ✅ | ✅ |
  | `PostgresChannel` (worker pool) | ✅ | ❌ |
  | `CacheSyncService` (sync — если `enable_audit=True` И OWNER) | ✅ | ✅ (если OWNER) |
  | `CronService` (если `gateway.enable_cron=True`) | ✅ | ❌ |
  | WebSocket port check (вызывается из entrypoint) | ✅ | ❌ |
  | Console I/O (in-memory bus) | ❌ | ✅ |

- **Cache lifecycle и sync lifecycle РАЗДЕЛЕНЫ:**
  - **Cache runtime** (`CacheProvider`, `cache storageCacheStore`, `CacheOwnershipCoordinator`) создаётся, если `gateway.cache` секция настроена (наличие `gateway.cache.local_path`). Это shared runtime-resource.
  - **Sync** (`CacheSyncService`) создаётся ТОЛЬКО если `gateway.enable_audit=True` И claim = OWNER.
  - **`gateway.enable_audit` MUST NOT определять существование shared cache runtime**. Skills (`audit_analyzer`, `legal_summarizer`) MUST иметь доступ к cache независимо от `enable_audit`.

- **Layered ownership architecture:**
  ```text
  CacheOwnershipCoordinator   ← only: try_claim/heartbeat/release
        ↓
  CacheAccessMode            ← only: READ_WRITE / READ_ONLY
        ↓
  CacheProvider (abstract)   ← open(path, mode) + query/search/schema/close
        ↓
  cache storageCacheStore (impl)    ← concrete implementation
  ```
  ApplicationContext зависит только от `CacheProvider` (НЕ от `cache storageCacheStore`). `cache storageCacheStore.open(path, mode)` — factory method, возвращающий `CacheProvider` instance.

- **PG-level ownership claim + real fencing:**
  - **`CacheOwnershipCoordinator.try_claim()`** атомарно через PG возвращает `ClaimResult(acquired: bool, generation: int, owner_id: str)`. Реализация MAY использовать `INSERT ... ON CONFLICT (resource_key) DO UPDATE WHERE expires_at < NOW()`.
  - **Fencing через PG advisory lock** (НЕ generation check alone — TOCTOU race). И ownership takeover, И producer write MUST проходить через один и тот же coordination lock:
    ```sql
    -- в try_claim() takeover branch:
    BEGIN;
    SELECT pg_advisory_xact_lock(hash(resource_key));  -- serialize
    -- check generation + INSERT/UPDATE
    COMMIT;
    
    -- в producer write critical section:
    BEGIN;
    SELECT pg_advisory_xact_lock(hash(resource_key));  -- serialize
    -- verify generation + cache storage write
    COMMIT;
    ```
    Lock MUST быть held для полного критического раздела (ownership validation + cache storage mutation).
  - **`generation` semantics:** стартует с 1 при первой вставке; увеличивается на 1 при каждом takeover; strictly monotonically растёт. Generation — это fencing token, а НЕ самостоятельный write barrier.
  - **`release()` контракт:** MUST удалять ownership ТОЛЬКО если `(resource_key, owner_id, generation)` совпадают. No-op + WARNING при несовпадении.

- **Двухуровневая READ_ONLY защита:**
  - 1-й уровень: cache storage connection открывается через `cache storage.connect(path, read_only=True)` в `READ_ONLY` режиме (сама cache storage не позволит мутации).
  - 2-й уровень: `CacheProvider.query_sql(...)` MUST поднять `ReadOnlyAssertionError` при INSERT/UPDATE/DELETE в `READ_ONLY` режиме.
  - `query_sql()` контракт: executes any SQL statement; mutation statements allowed only in `READ_WRITE` mode.

- **Единый snapshot-путь: `<local_path>/cache.cache storage`** для всех процессов. **`gateway.cache.local_path` MUST быть shared runtime resource**, не profile-specific. ConfigurationResolver MUST reject per-profile override `gateway.cache.local_path`.

- **`AgentLoop` остаётся transport-agnostic.** CLI продолжает использовать `bus.publish_inbound(InboundMessage(channel="cli", ...))` и `bus.consume_outbound()`.

- **Cron = gateway-only.** `CronService` создаётся ТОЛЬКО при `role="gateway"` (если `gateway.enable_cron=True`). CLI НЕ запускает `CronService`.

- **`/compact` в CLI остаётся локальным shortcut.**

- **CLI = фиксированный профиль `test`.** `cli_agent.py` MUST NOT принимать `--profile` CLI-аргумент и MUST NOT читать профиль из env. CLI hardcode'ит `profile="test"` при вызове `config._initialize_settings(profile="test")`.

- **Удаление Streamlit — отдельный change `remove-streamlit-runtime`**. Текущий change содержит ТОЛЬКО запрет импорта `streamlit` в runtime-коде через Forbidden Behavior.

- **`**kwargs` compatibility boundary (deprecated `enable_*`):**
  - Принимаются ТОЛЬКО на outer boundary `create(**kwargs)`.
  - Deprecated kwargs MUST NOT пропагидироваться внутрь service constructors.
  - **No new production code MAY call `ApplicationContext.create()` with deprecated kwargs.**
  - Deprecated kwargs MUST быть удалены в следующем MINOR релизе (отдельный change `remove-deprecated-enable-kwargs`).

## Capabilities

### New Capabilities

- `runtime/entrypoints`: контракт application entrypoint'ов (`cli_agent.py`, `gateway.py`). Описывает: единая typed signature `ApplicationContext.create(role=...)` (без `profile` и без `enable_*` в публичных kwargs; `enable_*` MAY приниматься через `**kwargs` для backward compat), composition-rules для каждой роли. Контракт `CacheOwnershipCoordinator` (НИКОГДА не открывать cache storage в RW режиме до определения ownership; explicit numbered lifecycle). Контракт «cache storage — runtime-resource с PG-level ownership claim через отдельную таблицу `agent_cache_ownership` с ownership key = `local_cache`, atomic claim, fencing через advisory lock». Контракт запрета Streamlit в runtime-коде. AgentLoop MUST быть transport-agnostic.

### Modified Capabilities

- `runtime/context`: добавляется требование «`ApplicationContext.create()` MUST иметь typed signature БЕЗ `profile` (профиль resolved до через `_initialize_settings(profile=...)`); MUST NOT содержать именованные параметры `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` (MAY приниматься через `**kwargs` для backward compat с DeprecationWarning). CLI и gateway вызывают `ApplicationContext.create(...)` с одинаковой typed signature. Deprecated kwargs через `**kwargs` MUST быть удалены в MINOR релизе после раскрытия этого change».

- `data/cache-provider`: добавляется требование «`cache.cache storage` MUST быть единым runtime-ресурсом, открываемым в `ApplicationContext.create()` для `role="gateway"` и `role="cli"` в режиме `READ_WRITE` или `READ_ONLY` в зависимости от результата `CacheOwnershipCoordinator.try_claim()`. Lifecycle cache MUST быть отделён от `gateway.enable_audit`: cache exists если `gateway.cache` настроен, sync существует только если `enable_audit=True`. `CacheProvider` MUST предоставлять API с явным `mode=READ_WRITE|READ_ONLY` параметром и абстрактным `open(path, mode) -> CacheProvider`; `READ_ONLY` блокирует все мутации через (a) cache storage `read_only=True` connection и (b) `CacheProvider` assertion guard. `CacheProvider` MUST reject путь на NFS до открытия cache storage. `gateway.cache.local_path` MUST быть shared runtime resource (не profile-specific)».

- `configuration/profiles`: добавляется требование «`cli_agent.py` MUST NOT принимать `--profile` CLI-аргумент и MUST NOT читать профиль из переменных окружения; CLI MUST hardcode `profile="test"` при вызове `config._initialize_settings(profile="test")`. CLI — локальный test/dev entrypoint. Gateway MAY принимать `--profile`. После resolution runtime-компоненты НЕ ДОЛЖНЫ ветвиться по `profile == "test"`. `ApplicationContext.create()` MUST NOT принимать `profile` как параметр».

## Impact

- `lib/core/application_context.py`:
  - Убрать из typed signature `profile`, `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls`.
  - Добавить обязательный kwarg `role: Literal["gateway","cli"]`.
  - Добавить `**kwargs` для backward compat (только deprecated `enable_*`).
  - Обновить docstring с явным указанием: `profile` MUST быть resolved до через `_initialize_settings(profile=...)`.

- Новый модуль `lib/services/cache_ownership.py`:
  - `class CacheAccessMode(enum.Enum)`: `READ_WRITE`, `READ_ONLY`.
  - `class ClaimResult`: `acquired: bool`, `generation: int`, `owner_id: str`, `current_owner_id: str | None`, `current_generation: int | None`.
  - `class CacheOwnershipCoordinator`:
    - `__init__(worker_id: str, dsn: str, resource_key: str = "local_cache", ttl_seconds: int = 60)`.
    - `def try_claim(self) -> ClaimResult` — atomic claim через PG, может использовать `INSERT ... ON CONFLICT (resource_key) DO UPDATE ... WHERE expires_at < NOW() RETURNING ...`. Implementation MAY use `pg_advisory_xact_lock(hash(resource_key))` для mutual exclusion с producer writes.
    - `def heartbeat(self) -> None` — update с `WHERE generation=$my_generation`.
    - `def release(self) -> None` — `DELETE WHERE resource_key=$1 AND owner_id=$2 AND generation=$3 RETURNING resource_key`. No-op + WARNING при 0 rows.

- SQL schema:
  - Новая таблица `agent_cache_ownership` через миграцию.
  - Колонка `generation BIGINT NOT NULL DEFAULT 1`.

- `lib/services/cache_provider.py` / `lib/services/cache_provider_impl.py`:
  - `CacheProvider` (ABC) gains abstract `open(path: str, mode: CacheAccessMode) -> CacheProvider` classmethod/factory method.
  - `cache storageCacheStore.open(path: str, mode: CacheAccessMode) -> CacheProvider` — concrete factory. Возвращает `CacheProvider` instance с `cache storage.connect(path, read_only=(mode==READ_ONLY))`.
  - `CacheProvider.query_sql()` — executes any SQL; в `READ_ONLY` режиме INSERT/UPDATE/DELETE поднимают `ReadOnlyAssertionError`.

- `lib/services/pg_cache storage_sync_service.py`:
  - Создаётся ТОЛЬКО при `gateway.enable_audit=True` И `mode=READ_WRITE`.
  - Sync-поток MUST использовать `coord.acquire_write_fence()` (PG advisory lock + generation check) перед каждой записью.

- `gateway.py`:
  - Убрать kwargs `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` из `ApplicationContext.create(...)`.
  - Передавать `role="gateway"`. НЕ передавать `profile`.
  - `_check_websocket_port_available` остаётся.

- `cli_agent.py`:
  - Убрать `--profile` argparse argument.
  - Hardcode `config._initialize_settings(profile="test")`.
  - Убрать kwargs из `ApplicationContext.create(...)`.
  - Передавать `role="cli"`.
  - `lib/cli/console_loop.py::run_repl` остаётся без изменений.

- Standalone utilities: НЕ модифицируются в этом change; используют `**kwargs` для backward compat.

- Документация:
  - `README.md` обновляется.
  - `AGENTS.md` обновляется.
  - `docs/ARCHITECTURE.md` обновляется.
  - `CHANGELOG.md` обновляется.

## Open Questions

Решены в proposal.md Open Questions:

1. **PG-level claim mechanism** — Новая таблица `agent_cache_ownership` с фиксированным `resource_key='local_cache'` и `generation`.
2. **Heartbeat интервал и TTL** — heartbeat 30 сек, TTL 60 сек.
3. **kill -9 recovery** — acceptance criterion: новый owner может reopen cache если cache storage recoverable.
4. **`role="utility"`** — НЕ включается.
5. **Streamlit removal** — отдельный change.
6. **Fencing mechanism** — PG advisory lock + generation token. Generation НЕ является самостоятельным write barrier.

## Что принципиально НЕ делается в этом change

- Изменение `nanobot.MessageBus`, `nanobot.AgentLoop`, `BaseChannel`.
- Заставлять CLI использовать `PostgresChannel` для I/O.
- Создание `TerminalChannel`.
- Изменение схемы `agent_messages` / `agent_conversation_messages`.
- Передача worker pool задач между CLI и gateway.
- Удаление Streamlit — отдельный change.
- Добавление `role="utility"`.
- Изменение `gateway.compact.*` / других `gateway.*` ключей, не упомянутых в proposal.
- Полный отказ от in-memory bus — он остаётся для `agent.run()`.
- Переписывание `lib/cli/console_loop.py::run_repl`.


