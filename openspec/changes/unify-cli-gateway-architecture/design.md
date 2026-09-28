## Context

См. `proposal.md` — почему нужна унификация. Здесь — технические решения.

Текущая архитектура (фактическая):

```text
                              ApplicationContext.create(...)
                                       │
                  ┌────────────────────┴────────────────────┐
                  │                                          │
            cli_agent.py                              gateway.py
        (enable_audit=False,                      (enable_audit=True,
         enable_cron=True)                          enable_cron=False)
                  │                                          │
        bus.publish_inbound(                     PostgresChannel (worker pool)
        InboundMessage(channel="cli"...))        bus.publish_inbound(
                  │                              InboundMessage(channel="postgres"...))
                  └──────────┬───────────────────────────────────┘
                             │
                          AgentLoop (общий)
                             │
                       MessageBus (in-memory, общий)
                             │
                          skills / tools / memory / logging
```

**Ключевая проблема:** lifecycle cache открывался в RW режиме ДО определения ownership, что позволяло consumer-процессу теоретически записать в файл. Решается новой `CacheOwnershipCoordinator` — она определяет access mode (READ_WRITE/READ_ONLY) ДО открытия concrete cache adapter.

## Goals / Non-Goals

**Goals:**
- Единая typed signature `ApplicationContext.create(role=..., ...)` для CLI и gateway; `enable_*` через `**kwargs`.
- `profile` MUST быть resolved ДО `ApplicationContext.create()` через `_initialize_settings(profile=...)` (НЕ через constructor-arg).
- `CacheOwnershipCoordinator` определяет access mode локального cache resource (READ_WRITE или READ_ONLY) ДО открытия.
- `CacheProvider` открывается в режиме, возвращённом coordinator; `READ_ONLY` блокирует мутации.
- `CacheSyncService` создаётся ТОЛЬКО если `enable_audit=True` И claim = OWNER.
- Единый snapshot-файл для всех процессов; ownership через PG-level claim (отдельная таблица `agent_cache_ownership` с фиксированным `resource_key='local_cache'`).
- Atomic claim через `INSERT ... ON CONFLICT`.
- Fencing старого producer: после потери ownership он прекращает mutation.
- Cron = gateway-only (CLI НЕ запускает `CronService`).
- AgentLoop остаётся transport-agnostic; transport (CLI = in-memory bus, gateway = PostgresChannel) НЕ меняется.
- CLI = фиксированный профиль `test`; gateway сохраняет `--profile`.
- `CacheProvider`, `CacheOwnershipCoordinator`, `CacheSyncService` MUST NOT зависеть от конкретной cache implementation (текущая: DuckDB; future: SQLite).

**Non-Goals:**
- Изменение `nanobot.MessageBus`, `nanobot.AgentLoop`, `BaseChannel`.
- Заставлять CLI использовать `PostgresChannel` для I/O.
- Создание `TerminalChannel` или другого нового transport-класса.
- Изменение схемы `agent_messages` / `agent_conversation_messages`.
- Передача worker pool задач между CLI и gateway.
- Удаление Streamlit — отдельный change `remove-streamlit-runtime`.
- `role="utility"` — НЕ включается в этот change; standalone utilities остаются на `**kwargs`.
- Изменение `gateway.compact.*` / других `gateway.*` ключей, не упомянутых в proposal.

## Decisions

### D1. `ApplicationContext.create(role=...)` — typed signature без profile

**Решение:** Typed signature:

```python
def create(
    script_dir: Path,
    workspace_dir: Path,
    *,
    role: Literal["gateway", "cli"],
    storage_override: str | None = None,
    session_override: str | None = None,
    **kwargs,  # deprecated: enable_db_logging, enable_audit, enable_cron, print_llm_calls
) -> ApplicationContext: ...
```

**`profile` MUST NOT быть параметром** — профиль resolved ДО через `_initialize_settings(profile=...)`.

**`ApplicationContext.create()` MUST потреблять (consume) уже-resolved global `SETTINGS`** через `import config as _config; ctx_settings = _config.SETTINGS`. `ApplicationContext.create()` MUST NOT resolve profile самостоятельно и MUST NOT принимать `settings` как параметр.

`enable_*` MUST NOT быть именованными параметрами в typed signature. Они принимаются через `**kwargs`. Если kwarg передан — используется + `warnings.warn(..., DeprecationWarning, stacklevel=2)`. Если не передан — читается из `SETTINGS["gateway"].*`.

**Альтернативы:**
- Оставить `enable_*` в typed signature — отвергнуто: typed signature MUST быть source of truth для API; deprecated kwargs только через `**kwargs`.
- Оставить `profile` в typed signature — отвергнуто: profile — configuration-resolution concern, не runtime concern. Передача profile через constructor-arg делает ApplicationContext aware о "почему" профиль, что неправильно.
- Передавать `settings` как параметр в `create()` — отвергнуто: это уже реализовано через global `config.SETTINGS`; менять форму под нарушение существующего подхода.

**Staged implementation** (для weak-agent plan):
1. Stage A: typed signature change (`role`, `**kwargs`, remove `enable_*` and `profile` from named).
2. Stage B: composition rules по `role`.
3. Stage C: `CacheOwnershipCoordinator` + `agent_cache_ownership` table.
4. Stage D: `CacheProvider` interface + concrete adapter factory (e.g., `DuckDbCacheStore.open(path, mode)`).
5. Stage E: `CacheSyncService` integration + fencing token.
6. Stage F: `--profile` removal from CLI.
7. Stage G: deprecated kwargs removal в следующем MINOR.

Не смешивать в один гигантский diff `ApplicationContext.create()`.

### D2. `role` определяет entrypoint composition, не AgentLoop и не ownership

**Решение:** Composition matrix:

| Сервис | `role="gateway"` | `role="cli"` |
|---|---|---|
| `AgentLoop` (с hooks, runtime patches, skills, tools, memory) | ✅ | ✅ |
| `DbLoggingService` (если `gateway.enable_db_logging=True`) | ✅ | ✅ |
| `SessionManager` / `PGSessionManager` | ✅ | ✅ |
| `RuntimeEventsSubscriber` | ✅ | ✅ |
| `RuntimePatcher.apply_all()` | ✅ | ✅ |
| `CacheProvider` (если `gateway.cache` настроен) | ✅ | ✅ |
| `CacheOwnershipCoordinator` (если `gateway.cache` настроен) | ✅ | ✅ |
| concrete `CacheProvider` implementation (factory: `open(path, mode)`) | ✅ | ✅ |
| `PostgresChannel` (worker pool) | ✅ | ❌ |
| `CacheSyncService` (sync — если `enable_audit=True` И OWNER) | ✅ | ✅ (если OWNER) |
| `CronService` (если `gateway.enable_cron=True`) | ✅ | ❌ |
| WebSocket port check (вызывается из entrypoint) | ✅ | ❌ |
| Console I/O (in-memory bus) | ❌ | ✅ |

**Cache lifecycle MUST быть отделён от `gateway.enable_audit`.** Cache runtime (`CacheProvider`, concrete implementation, `CacheOwnershipCoordinator`) создаётся, если `gateway.cache` секция настроена (наличие `gateway.cache.local_path`). `gateway.enable_audit` MUST NOT определять существование cache — он контролирует ТОЛЬКО audit sync (`CacheSyncService`).

`role` MUST NOT представлять environment, profile, deployment mode, storage ownership, или runtime behavior. `role` MUST NOT определять cache producer/consumer status — это ответственность `CacheOwnershipCoordinator`.

CLI REPL: `bus.publish_inbound(InboundMessage(channel="cli", ...))` + `bus.consume_outbound()` — текущее поведение `lib/cli/console_loop.py`. Никаких изменений в REPL-цикле.

### D3. Единый snapshot-путь

**Решение:** `<local_path>/cache.duckdb` для всех процессов (через concrete `CacheProvider` implementation — текущая: DuckDB; future: SQLite). `resolve_publish_path(role)` сохранён для backward compat, но `role="cli"` и `role="gateway"` возвращают **один и тот же путь**.

### D4. `CacheOwnershipCoordinator` — atomic claim + real fencing через advisory lock

**Решение:** Новый модуль `lib/services/cache_ownership.py`. Класс `CacheOwnershipCoordinator` инкапсулирует **только** ownership coordination логического cache resource (НЕ открытие cache adapter, НЕ sync, НЕ cache I/O). API:

```python
class CacheAccessMode(enum.Enum):
    READ_WRITE = "READ_WRITE"  # OWNER
    READ_ONLY = "READ_ONLY"    # READER

class ClaimResult:
    """Outcome of try_claim()."""
    acquired: bool                            # True → мы OWNER
    generation: int                           # my_generation (если acquired) или current_generation
    owner_id: str                             # наш worker_id или current owner_id
    current_owner_id: str | None = None       # для логирования при False
    current_generation: int | None = None      # для логирования при False

class CacheOwnershipCoordinator:
    def __init__(self, worker_id: str, dsn: str, resource_key: str = "local_cache", ttl_seconds: int = 60):
        ...

    def try_claim(self) -> ClaimResult:
        """Atomic через PG. Два outcomes:
          1. acquired=True → мы OWNER (мы инкрементировали/установили generation)
          2. acquired=False → другой процесс OWNER (current_* поля для логирования)

        Implementation MAY use:
          INSERT ... ON CONFLICT (resource_key) DO UPDATE
          SET generation = COALESCE(agent_cache_ownership.generation, 0) + 1,
              ...
          WHERE agent_cache_ownership.expires_at < NOW()
          RETURNING ...
        """

    def heartbeat(self) -> bool:
        """UPDATE SET last_heartbeat_at=NOW(), expires_at=NOW() + INTERVAL '60 seconds'
        WHERE resource_key=$1 AND owner_id=$2 AND generation=$3.
        Returns True если успешно (хотя бы 1 row), False если WHERE не match (generation изменился)."""

    def release(self) -> bool:
        """DELETE FROM agent_cache_ownership
        WHERE resource_key=$1 AND owner_id=$2 AND generation=$3
        RETURNING resource_key.
        Returns True если успешно, False если не наш / generation mismatch.
        Caller MUST логировать WARNING при False."""

    def acquire_write_fence(self) -> ContextManager[None]:
        """Context manager для fencing producer writes.

        Внутри:
          BEGIN PG;
            SELECT pg_advisory_xact_lock(hashtext($resource_key));
            -- (caller verifies generation + executes mutation через CacheProvider);
          COMMIT;

        Lock MUST mutually exclude с ownership takeover в try_claim().
        Альтернативный механизм с той же семантикой mutual exclusion допустим.
        """
```

**Mandatory contract — generation НЕ является самостоятельным write barrier.** Только в комбинации с `pg_advisory_xact_lock` generation обеспечивает fencing.

```text
Fencing MUST обеспечивать mutual exclusion между:
1. Ownership takeover (try_claim() DO UPDATE branch — инкремент generation);
2. Producer mutation critical section (validate generation + execute mutation через CacheProvider).

Если используется PG advisory lock:
  - try_claim() takeover branch внутри одной PG-транзакции с pg_advisory_xact_lock(hashtext(resource_key));
  - Producer mutation внутри одной PG-транзакции с pg_advisory_xact_lock(hashtext(resource_key));
  - Lock MUST быть held для полного критического раздела (ownership validation AND CacheProvider mutation).
```

Таблица `agent_cache_ownership`:

```sql
CREATE TABLE agent_cache_ownership (
    resource_key VARCHAR PRIMARY KEY,           -- фиксированное значение: 'local_cache'
    owner_id VARCHAR NOT NULL,                    -- worker_id текущего владельца
    generation BIGINT NOT NULL DEFAULT 1,         -- fencing token (starts at 1, strictly monotonic)
    acquired_at TIMESTAMP NOT NULL DEFAULT NOW(),
    last_heartbeat_at TIMESTAMP NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMP NOT NULL
);
```

**Ownership key** = `'local_cache'` — идентификатор логического cache resource, НЕ per-process, НЕ per-role, НЕ per-storage. Этот ключ НЕ ДОЛЖЕН содержать название СУБД, формата файла или concrete adapter. MUST быть ровно **один** активный claim на ресурс `local_cache`.

**Generation semantics:** starts at 1 при первой вставке строки; инкрементируется на 1 при каждом takeover. Strictly monotonically increasing.

**Lifecycle в `ApplicationContext.create()`:**

```python
def create(...):
    # ... ConfigService, runtime patches, etc.

    # === STAGE 1: resolve path ===
    cache_path = resolve_publish_path(workspace_path, cache_cfg, role=role)

    # === STAGE 2: atomic claim (если gateway.cache настроен) ===
    if gateway.cache_is_configured():
        coord = CacheOwnershipCoordinator(
            worker_id=f"{role}_{os.getpid()}",
            dsn=...,
            resource_key="local_cache",
            ttl_seconds=60,
        )
        result = coord.try_claim()  # atomic

        # === STAGE 3: open cache через concrete factory ===
        # `CacheProvider` НЕ ИМЕЕТ метода `open()` — это ответственность
        # concrete adapter factory (для текущей реализации: `DuckDbCacheStore.open`).
        ctx.cache_provider: CacheProvider = DuckDbCacheStore.open(  # concrete_factory
            path=cache_path,
            mode=CacheAccessMode.READ_WRITE if result.acquired else CacheAccessMode.READ_ONLY,
        )

        # === STAGE 4: create sync only if OWNER AND enable_audit=True ===
        if result.acquired and gateway.enable_audit:
            ctx.sync_service = CacheSyncService(
                cache_provider=ctx.cache_provider,
                my_generation=result.generation,
                heartbeat_callback=coord.heartbeat,
                release_callback=lambda: coord.release(),
                fence_callback=coord.acquire_write_fence,  # context manager
            )
        else:
            ctx.sync_service = None
    else:
        ctx.cache_provider = None
        ctx.sync_service = None
```

**Fencing в producer mutation critical section:**

```python
def _mutation_with_fence(self, mutation):
    with self.coord.acquire_write_fence() as lock:
        # внутри lock: проверка generation + execute mutation через CacheProvider
        with self.coord.transaction() as tx:  # PG coordination transaction
            current_gen = tx.execute(
                "SELECT generation FROM agent_cache_ownership WHERE resource_key = 'local_cache' AND expires_at > NOW()"
            ).scalar()
            if current_gen != self.my_generation:
                raise OwnershipLostError(...)
            self.cache_provider.execute(mutation)  # НЕ cache_store.execute_sql
```

**Важно:** `CacheProvider` НЕ предоставляет `connection.transaction()` или `execute_sql()` (это были бы concrete-storage API). Конкретный пример выше — абстрактный. Real implementation:

- `DuckDbCacheStore` может реализовать transaction через `duckdb.connect()` context manager.
- `SQLiteCacheStore` (future) может использовать `sqlite3` transaction API.

Главное: `CacheSyncService` зависит только от `CacheProvider` interface (`provider.execute(mutation)`), не от concrete storage API.

**Producer mutation MUST выполняться через fenced write operation CacheProvider.**

Порядок:
1. BEGIN PostgreSQL coordination transaction.
2. Acquire resource-scoped advisory lock.
3. Verify owner_id + generation.
4. Выполнить одну mutation через `CacheProvider`.
5. Завершить PostgreSQL coordination transaction.

PostgreSQL transaction MUST оставаться открытой на всём протяжении critical section. Между шагами 3 и 4 НЕ ДОЛЖНО быть COMMIT/ROLLBACK координационной PostgreSQL transaction.

**Альтернативы (отвергнуты):**
- Только `is_still_owner()` без advisory lock — TOCTOU race между check и write.
- File lock на snapshot-файле — concrete storage уже использует flock; layering поверх fragile.
- Generation token без advisory lock — НЕ fencing, только token.
- `CacheProvider` сам делает ownership check — НЕДОПУСТИМО. Ownership ответственность `CacheOwnershipCoordinator`.

### D11. Локальное filesystem storage + shared cache across profiles

**Решение:** Система ДОЛЖНА хранить snapshot-файл на локальной filesystem с требуемой cache storage locking semantics (POSIX `fcntl` flock, etc.). Network/shared filesystem (NFS, SMB, etc.) — запрещён. Конкретная FS не специфицируется (ext4 — Linux default, APFS — macOS, NTFS — Windows).

```text
Logical cache resource
        │
Текущий physical storage
        │
<local_path>/cache.duckdb  (текущая реализация)
<local_path>/cache.sqlite  (будущая реализация, например)
```

**`cache.duckdb` является текущим physical storage implementation, а не частью `CacheProvider` contract.** Изменение имени или формата physical storage НЕ ДОЛЖНО менять ownership contract, `CacheProvider` interface или `CacheSyncService`.

**Concrete cache storage MUST reject unsupported network/shared filesystem paths before opening the storage.** Для текущей DuckDB implementation NFS/SMB и другие network/shared filesystems MUST быть rejected. Для будущих реализаций правила аналогичны (storage без локального filesystem locking semantics недопустим).

**`gateway.cache.local_path` MUST быть shared runtime resource**, не profile-specific value. Если CLI работает с `profile="test"`, а gateway с `profile="prod"` — оба процесса MUST резолвить snapshot в один и тот же физический путь. Профили НЕ ДОЛЖНЫ переопределять `gateway.cache.local_path`. Если `profiles/test.jsonc` и `profiles/prod.jsonc` имеют разные значения `gateway.cache.local_path` — ConfigurationResolver MUST reject это как ошибку конфигурации.

### D12. Двухуровневая защита READ_ONLY + query_sql semantics

**Общий контракт (storage-independent):**

`CacheProvider` в `READ_ONLY` mode MUST NOT выполнять mutations. `query_sql()` MUST принимать только DML statements: `SELECT`, `INSERT`, `UPDATE`, `DELETE`.

Ошибки:
- DDL statements (`CREATE`, `ALTER`, `DROP`, `TRUNCATE`, `CREATE INDEX`, `DROP INDEX`) MUST быть отклонены в любом mode с dedicated `UnsupportedSqlError`.
- `INSERT`/`UPDATE`/`DELETE` в `READ_ONLY` mode MUST поднимать `ReadOnlyAssertionError` до выполнения.
- `SELECT` MUST работать нормально в любом mode.

**Concrete DuckDB implementation:**

`DuckDbCacheStore` MUST открыть DuckDB connection с реальным read-only режимом, когда `mode=READ_ONLY`:

```python
duckdb.connect(path, read_only=True)
```

Это — **первый уровень защиты**: сама DuckDB connection не позволяет INSERT/UPDATE/DELETE.

`CacheProvider` MUST иметь **второй уровень защиты** (assertion guard): при попытке мутации через `CacheProvider.query_sql(...)` с `mode=READ_ONLY` MUST поднять `ReadOnlyAssertionError`. При попытке DDL MUST поднять `UnsupportedSqlError`.

### D13. kill -9 recovery — acceptance criterion, не implementation detail

**Решение:** Spec описывает гарантию, не реализацию:

> После unclean termination (kill -9, OOM, crash) следующий owner MAY перехватить ownership через `try_claim()` (получит `my_generation > previous_generation`).
> Acceptance criterion: следующий процесс MUST иметь возможность reopen существующий snapshot-файл если cache storage считает его recoverable.

Implementation может использовать штатный concrete-storage механизм (DuckDB ATTACH + WAL replay для SQLite и т.п.). Не зашиваем конкретный механизм.

### D14. Layered architecture CacheProvider (без `open()`)

**Решение:** API MUST быть layered:

```text
CacheOwnershipCoordinator     ← try_claim / heartbeat / release / acquire_write_fence (только ownership)
        ↓
CacheAccessMode              ← READ_WRITE / READ_ONLY (enum)
        ↓
CacheProvider (ABC)          ← interface: query_sql / search_vector / get_schema / close
        ↓
Concrete implementation      ← текущая: DuckDbCacheStore; future: SQLiteCacheStore
        ↓
Cache storage file (ext4/APFS/NTFS)
```

**`CacheProvider` MUST NOT иметь метода `open()`.** Concrete implementation создаётся composition root через concrete factory.

Для текущей реализации:
```python
# в ApplicationContext.create() — это composition-time, НЕ runtime-consumer код
provider: CacheProvider = DuckDbCacheStore.open(
    path=cache_path,
    mode=access_mode,
)

ctx.cache_provider = provider  # хранится как CacheProvider
```

После создания runtime все потребители cache работают только через `CacheProvider`. `DuckDbCacheStore` НЕ ДОЛЖЕН использоваться как тип зависимости в runtime-consumer коде (AgentLoop, Skills, Tools, `CacheSyncService`, `CacheOwnershipCoordinator`).

**ApplicationContext MUST зависеть только от `CacheProvider`**, НЕ от `DuckDbCacheStore`. В `ApplicationContext.create()` MUST использоваться `ctx.cache_provider = <concrete_factory>.open(...)`, и `DuckDbCacheStore` не должен появляться в полях `ctx`.

**Кто держит fencing boundary:** `CacheSyncService` (producer) вызывает `coord.acquire_write_fence()` — ownership/fencing НЕ ЯВЛЯЕТСЯ обязанностью `CacheProvider`. См. D4.

**Альтернативы (отвергнуты):**
- `ctx.cache_store = DuckDbCacheStore(...)` (direct field) — отвергнуто: пропускает `CacheProvider` abstraction.
- `CacheProvider.open(...)` — отвергнуто: открывает concrete implementation в interface.
- `CacheProvider` сам делает ownership check — НЕДОПУСТИМО. Ownership — ответственность `CacheOwnershipCoordinator`.

### D5. Streamlit removal — отдельный change

**Решение:** Удаление `streamlit_app.py`, `SubprocessManager.spawn_streamlit`, `_streamlit_enabled()` — НЕ часть текущего change. Это отдельный change `remove-streamlit-runtime`.

Текущий change содержит ТОЛЬКО запрет импорта `streamlit` в runtime-коде через `runtime/entrypoints::Forbidden Behavior`.

### D6. WebSocket port check остаётся в gateway

**Решение:** WebSocket port check — server-only pre-startup проверка. Остаётся в `gateway.py::_entrypoint_main`. CLI НЕ выполняет эту проверку.

### D7. Cron = gateway-only (с жёсткой формулировкой)

**Решение:** `gateway.enable_cron` имеет effect ТОЛЬКО при `role="gateway"`. При `role="cli"` значение `gateway.enable_cron` MUST быть ignored. При `role="cli"` `CronService` MUST NOT создаваться.

`CronService` создаётся ТОЛЬКО при `role="gateway"` (если `gateway.enable_cron=True`). CLI НЕ запускает `CronService`. Решает проблему «два процесса выполняют один jobs.json дважды».

**Breaking change:** пользователи, у которых сейчас cron работал в CLI, теряют эту функциональность. Документируется в CHANGELOG.

### D8. CLI = фиксированный профиль test

**Решение:** `cli_agent.py` MUST NOT принимать `--profile` CLI-аргумент. CLI hardcode'ит `profile="test"` при вызове `config._initialize_settings(profile="test")`. CLI MUST NOT читать профиль из env.

CLI — локальный test/dev entrypoint, не production deployment interface. Не нужно создавать ложную универсальность (`cli --profile prod`). Это уменьшает поверхность конфигурации и количество комбинаций для тестирования.

Gateway MAY принимать `--profile` (текущее поведение сохраняется).

**Breaking change:** пользователи, которые запускали `cli_agent.py --profile=prod`, должны перейти на gateway. Документируется в CHANGELOG.

### D9. `--storage` в CLI: `auto`/`postgres`/`file`

**Решение:** `--storage=auto` (default) → `storage_override=None`. `--storage=postgres` → явно postgres-storage. `--storage=file` → file-storage.

### D10. `/compact` в CLI — локальный shortcut

**Решение:** `lib/cli/console_loop.py::_run_cli_compact` остаётся без изменений. CLI вызывает `ContextCompactionService.compact(session_key="cli:<session>", idle=True, force=True)` напрямую.

### D13. Тесты — backward compatibility и новые сценарии

**Решение:**
- Существующие тесты с `ApplicationContext.create(enable_audit=True/False, ...)` — продолжают работать (deprecated kwargs через `**kwargs`).
- Новые тесты:
  - `tests/test_application_context_role.py` — composition matrix, signature checks (no `profile`, no `enable_*` в named params).
  - `tests/test_cli_uses_in_memory_bus.py` — REPL использует bus, не PostgresChannel.
  - `tests/test_cache_ownership_claim.py` — atomic claim, concurrent claim, stale takeover, fencing с generation.
  - `tests/test_cache_provider_mode.py` — `READ_ONLY` блокирует мутации; реальный read-only connection concrete adapter; DDL отклоняется в любом mode.
  - `tests/test_cache_provider_role_paths.py` — `resolve_publish_path(role="cli") == resolve_publish_path(role="gateway") == cache.duckdb`.
  - `tests/test_agent_loop_transport_agnostic.py` — AgentLoop работает только через bus.
  - `tests/test_streamlit_imports_removed.py` — отсутствие Streamlit-импортов (AST-based, не grep).
  - `tests/test_cli_no_profile.py` — CLI rejects `--profile`, hardcodes test, ignores env.
  - `tests/test_gateway_accepts_profile.py` — gateway accepts `--profile`.
  - `tests/test_shared_cache_path_across_profiles.py` — `gateway.cache.local_path` MUST быть одинаковый независимо от profile.

## Risks / Trade-offs

- **[Risk]** Standalone-утилиты (`tools/build_vectors.py` и др.) забывают перейти на новую сигнатуру — НЕ блокирует этот change; они используют `**kwargs` для backward compat. → **Mitigation:** CHANGELOG документирует deprecation, отдельный change для миграции utilities.
- **[Risk]** Два процесса стартуют одновременно и оба пытаются INSERT в `agent_cache_ownership` → атомарный claim гарантирует, что один получает `READ_WRITE`, другой `READ_ONLY`. → **Mitigation:** `INSERT ... ON CONFLICT (resource_key) DO UPDATE WHERE expires_at < NOW() RETURNING (xmax=0) AS inserted` — PG row-level lock. Verify в `tests/test_cache_ownership_claim.py::test_concurrent_claim_exactly_one_owner`.
- **[Risk]** Cron = gateway-only — пользователи CLI теряют cron. → **Mitigation:** документируется в CHANGELOG; альтернатива — запустить gateway (always-on).
- **[Risk]** `kill -9` оставляет PG claim, но snapshot-файл может быть corrupted. → **Mitigation:** concrete storage auto-recovery (DuckDB ATTACH + WAL replay, SQLite и т.п.); если fails — логирование ERROR + инструкция удалить snapshot-файл и restart. Acceptance criterion: после unclean termination + takeover новый owner может продолжить работу без ручного восстановления.
- **[Risk]** Fencing — старый producer может записать одну строку после перехвата ownership (TOCTOU race). → **Mitigation:** двухуровневое fencing: (1) generation check атомарно с write через PG-транзакцию; (2) heartbeat с generation check в WHERE clause. `is_still_owner()` сам по себе НЕДОСТАТОЧЕН — это только первая проверка перед write. Без generation race существует. Verify в `tests/test_cache_ownership_claim.py::test_old_owner_fencing_with_generation`.
- **[Risk]** Профили переопределяют `gateway.cache.local_path` → два разных физических snapshot-файла → ownership confusion. → **Mitigation:** ConfigurationResolver MUST reject `gateway.cache.local_path` per-profile override; `gateway.cache.local_path` MUST быть shared runtime resource.
- **[Risk]** READ_ONLY через только assertion — обход через прямой SQL. → **Mitigation:** двухуровневая защита: (1) concrete adapter открывает connection в реальном read-only режиме (DuckDB: `duckdb.connect(read_only=True)`); (2) `CacheProvider` assertion guard. Verify в `tests/test_cache_provider_mode.py::test_read_only_connection_blocks_mutations`.

## Migration Plan

**Шаг 1 (текущий MINOR):** Реализация всех решений поэтапно (см. D1.7 staged implementation). DeprecationWarning в логи для `**kwargs` `enable_*`. Cron BREAKING (CLI не запускает). CLI = fixed test profile. CacheOwnershipCoordinator + PG-level claim + generation/fencing token. Streamlit удаление — отдельный change.

**Шаг 2 (следующий MINOR):** Удаление deprecated `enable_*` kwargs из `**kwargs`-обработки в `ApplicationContext.create()` (отдельный change `remove-deprecated-enable-kwargs`).

**Шаг 3 (отдельный change):** Удаление Streamlit (`remove-streamlit-runtime`).

**Шаг 4 (отдельный change, опционально):** Миграция standalone utilities с `**kwargs` на typed signature.

**Шаг 5 (отдельный change, опционально):** ConfigurationResolver reject per-profile `gateway.cache.local_path` override (если не сделано в Шаг 1).

**Rollback:** Если после раскрытия обнаружена критическая регрессия — revert на коммит до раскрытия.

## Open Questions

- Решены в proposal.md Open Questions:
  1. **PG-level claim mechanism: extend `agent_worker_claims` или новая таблица `agent_cache_ownership`?** — **Новая таблица** с фиксированным `resource_key='local_cache'` и `generation` (fencing token).
  2. **Heartbeat интервал и TTL** — heartbeat 30 сек, TTL 60 сек (D4).
  3. **kill -9 recovery** — acceptance criterion, не implementation detail (D13).
  4. **`role="utility"`** — НЕ включается (см. «Что НЕ делается»).
  5. **Streamlit removal** — отдельный change (D5).
  6. **Fencing mechanism** — generation token + PG advisory lock (D4). Не `is_still_owner()` только (TOCTOU race); fencing boundary держит `CacheSyncService` (Variant A).
  7. **`CacheProvider.open()`** — отвергнуто. Concrete factory `DuckDbCacheStore.open()` в composition root.