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

**Ключевая проблема v2 (этот change её решает):** lifecycle DuckDB открыт в RW режиме ДО определения ownership. Это означает, что consumer-процесс мог бы теоретически записать в файл, если бы `try_claim()` вернула `False` ПОСЛЕ открытия. Решается новой `CacheOwnershipCoordinator` — она определяет режим ДО создания `DuckDbCacheStore`.

## Goals / Non-Goals

**Goals:**
- Единая typed signature `ApplicationContext.create(role=..., ...)` для CLI и gateway; `enable_*` через `**kwargs`.
- `profile` MUST быть resolved ДО `ApplicationContext.create()` через `_initialize_settings(profile=...)` (НЕ через constructor-arg).
- `CacheOwnershipCoordinator` определяет режим DuckDB (READ_WRITE или READ_ONLY) ДО открытия.
- `DuckDbCacheStore` открывается в режиме, возвращённом coordinator; `READ_ONLY` блокирует мутации.
- `PgDuckDbSyncService` создаётся только при `READ_WRITE`.
- Единый `cache.duckdb` для всех процессов; ownership через PG-level claim (отдельная таблица `agent_cache_ownership` с фиксированным `resource_key='duckdb_cache'`).
- Atomic claim через `INSERT ... ON CONFLICT`.
- Fencing старого producer: после потери ownership он прекращает записи.
- Cron = gateway-only (CLI НЕ запускает `CronService`).
- AgentLoop остаётся transport-agnostic; transport (CLI = in-memory bus, gateway = PostgresChannel) НЕ меняется.
- CLI = фиксированный профиль `test`; gateway сохраняет `--profile`.

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
4. Stage D: `CacheProvider.open(mode=...)` + `DuckDbCacheStore.open(mode=...)`.
5. Stage E: `PgDuckDbSyncService` integration + fencing token.
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
| `DuckDbCacheStore` (открывает `<local_path>/cache.duckdb` в режиме claim) | ✅ | ✅ |
| `PostgresChannel` (worker pool) | ✅ | ❌ |
| `PgDuckDbSyncService` (sync — только если claim = OWNER) | ✅ | ✅ |
| `CronService` (если `gateway.enable_cron=True`) | ✅ | ❌ |
| WebSocket port check (вызывается из entrypoint) | ✅ | ❌ |
| Console I/O (in-memory bus) | ❌ | ✅ |

`role` MUST NOT представлять environment, profile, deployment mode, storage ownership, или runtime behavior. `role` MUST NOT определять DuckDB producer/consumer status — это ответственность `CacheOwnershipCoordinator`.

CLI REPL: `bus.publish_inbound(InboundMessage(channel="cli", ...))` + `bus.consume_outbound()` — текущее поведение `lib/cli/console_loop.py`. Никаких изменений в REPL-цикле.

### D3. Единый snapshot-путь

**Решение:** `<local_path>/cache.duckdb` для всех процессов. `resolve_publish_path(role)` сохранён для backward compat, но `role="cli"` и `role="gateway"` возвращают **один и тот же путь**.

### D4. `CacheOwnershipCoordinator` — atomic claim + generation/fencing token

**Решение:** Новый модуль `lib/services/cache_ownership.py`. Класс `CacheOwnershipCoordinator` инкапсулирует **только** ownership coordination (НЕ открытие DuckDB, НЕ sync). API:

```python
class CacheAccessMode(enum.Enum):
    READ_WRITE = "READ_WRITE"  # OWNER
    READ_ONLY = "READ_ONLY"    # READER

class CacheOwnershipResult:
    """Outcome of try_claim()."""
    mode: CacheAccessMode
    my_generation: int | None    # set when mode == READ_WRITE
    current_owner_id: str | None  # for logging when mode == READ_ONLY
    current_generation: int | None

class CacheOwnershipCoordinator:
    def __init__(self, worker_id: str, dsn: str, resource_key: str = "duckdb_cache", ttl_seconds: int = 60):
        ...

    def try_claim(self) -> CacheOwnershipResult:
        """Atomic через PG-транзакцию с двумя outcomes.

        SQL:
          INSERT INTO agent_cache_ownership (resource_key, owner_id, generation, last_heartbeat_at, expires_at)
          VALUES ($1, $2, COALESCE((SELECT generation FROM agent_cache_ownership WHERE resource_key = $1), 0) + 1, NOW(), NOW() + INTERVAL '60 seconds')
          ON CONFLICT (resource_key) DO UPDATE
          SET owner_id = EXCLUDED.owner_id,
              generation = agent_cache_ownership.generation + 1,
              acquired_at = NOW(),
              last_heartbeat_at = NOW(),
              expires_at = EXCLUDED.expires_at
          WHERE agent_cache_ownership.expires_at < NOW()
          RETURNING (xmax = 0) AS inserted, owner_id, generation;

        Outcomes:
          1. RETURNING returned 1 row, inserted=true → claim acquired → READ_WRITE → my_generation = returned generation
          2. RETURNING returned 0 rows (DO UPDATE не сработал, expires_at > NOW())
             → claim not acquired → READ_ONLY → отдельный SELECT для чтения current owner metadata (owner_id, generation) для логирования
        """

    def heartbeat(self) -> None:
        """UPDATE last_heartbeat_at=NOW(), expires_at=NOW() + INTERVAL '60 seconds'
        WHERE resource_key=$1 AND owner_id=$2 AND generation=$3.
        Вызывается каждые 30 сек.
        Если WHERE clause не match (generation изменился) → heartbeat fails → sync stops."""

    def release(self) -> None:
        """DELETE FROM agent_cache_ownership
        WHERE resource_key=$1 AND owner_id=$2 AND generation=$3
        RETURNING resource_key.
        No-op + WARNING если RETURNING 0 rows."""

    def is_still_owner(self) -> bool:
        """SELECT generation FROM agent_cache_ownership
        WHERE resource_key=$1 AND owner_id=$2 AND generation=$3 AND expires_at > NOW().
        Returns True только если (owner_id, generation) match AND claim не expired."""
```

Таблица `agent_cache_ownership`:

```sql
CREATE TABLE agent_cache_ownership (
    resource_key VARCHAR PRIMARY KEY,           -- фиксированное значение: 'duckdb_cache'
    owner_id VARCHAR NOT NULL,                    -- worker_id текущего владельца
    generation BIGINT NOT NULL DEFAULT 1,         -- fencing token (монотонно растёт при takeover)
    acquired_at TIMESTAMP NOT NULL DEFAULT NOW(),
    last_heartbeat_at TIMESTAMP NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMP NOT NULL
);
```

**Ownership key** = `'duckdb_cache'` (фиксированная строка, НЕ per-process, НЕ per-role). MUST быть ровно **один** активный claim на ресурс `<local_path>/cache.duckdb`.

**Atomic claim MUST различать явно через case inspection:**
1. `RETURNING` вернул 1 row, `inserted=true` → claim acquired → OWNER (READ_WRITE).
2. `RETURNING` вернул 0 rows → claim не acquired → READER (READ_ONLY); для логирования делается отдельный `SELECT owner_id, generation FROM agent_cache_ownership WHERE resource_key = $1`.

**Двухуровневое fencing:**
1. **Generation check** в sync-потоке ПЕРЕД каждой записью в DuckDB:
   ```sql
   SELECT generation FROM agent_cache_ownership
   WHERE resource_key = 'duckdb_cache' AND expires_at > NOW();
   ```
   Если возвращённое `generation != my_generation` → ownership LOST → stop sync.

2. **Heartbeat с generation check** в WHERE:
   ```sql
   UPDATE agent_cache_ownership
   SET last_heartbeat_at=NOW(), expires_at=NOW() + INTERVAL '60 seconds'
   WHERE resource_key=$1 AND owner_id=$2 AND generation=$3;
   ```
   Если `WHERE` не match (generation изменился) → heartbeat fails → sync stops.

Это даёт **атомарную гарантию**: между check и write может пройти takeover, но `generation` в sync-потоке mismatch с тем, что в PG → sync останавливается.

**Lifecycle в `ApplicationContext.create()`:**

```python
def create(...):
    # ... ConfigService, runtime patches, etc.

    # === STAGE 1: resolve path ===
    cache_path = resolve_publish_path(workspace_path, cache_cfg, role=role)

    # === STAGE 2: atomic claim ===
    if enable_audit:
        coord = CacheOwnershipCoordinator(
            worker_id=f"{role}_{os.getpid()}",
            dsn=...,
            resource_key="duckdb_cache",
            ttl_seconds=60,
        )
        result = coord.try_claim()  # atomic

        # === STAGE 3: open DuckDB in determined mode ===
        ctx.cache_store = DuckDbCacheStore.open(
            path=cache_path,
            mode=result.mode,
        )

        # === STAGE 4: create sync only if OWNER (READ_WRITE) ===
        if result.mode == CacheAccessMode.READ_WRITE:
            ctx.sync_service = PgDuckDbSyncService(
                cache_store=ctx.cache_store,
                my_generation=result.my_generation,
                heartbeat_callback=coord.heartbeat,
                release_callback=lambda: coord.release(),
                fence_callback=lambda: coord.is_still_owner(),
            )
        else:
            ctx.sync_service = None
    else:
        ctx.cache_store = None
        ctx.sync_service = None
```

**Альтернативы:**
- Extend `agent_worker_claims` (через `claim_type` колонку) — отвергнуто: разная семантика. Отдельная таблица делает контракт явным.
- File lock на `cache.duckdb` — отвергнуто: DuckDB уже использует flock; layering поверх — fragile.
- Per-process или per-role ownership key — отвергнуто: пользователь явно сказал «один owner на ресурс».
- Только `is_still_owner()` без generation — отвергнуто: TOCTOU race между check и write (sync успевает записать одну строку после takeover).
- Advisory lock без generation — отвергнуто: pg_advisory_lock не интегрирован с ownership record, теряется traceability.

### D11. local filesystem (НЕ ext4) для DuckDB + shared cache across profiles

**Решение:** Система ДОЛЖНА хранить DuckDB-файл кэша на локальной filesystem с требуемой DuckDB locking semantics (POSIX `fcntl` flock, etc.). Network/shared filesystem (NFS, SMB, etc.) — запрещён. Конкретная FS не специфицируется.

`CacheProvider` MUST reject путь на NFS или другую network filesystem ДО открытия DuckDB — fail-fast с явной ошибкой.

**`gateway.cache.local_path` MUST быть shared runtime resource**, не profile-specific value. Если CLI работает с `profile="test"`, а gateway с `profile="prod"` — оба процесса MUST резолвить `cache.duckdb` в один и тот же физический путь. Профили НЕ ДОЛЖНЫ переопределять `gateway.cache.local_path`. Если `profiles/test.jsonc` и `profiles/prod.jsonc` имеют разные значения `gateway.cache.local_path` — ConfigurationResolver MUST reject это как ошибку конфигурации.

### D12. Двухуровневая защита READ_ONLY

**Решение:** `DuckDbCacheStore` MUST открывать DuckDB connection с реальным read-only режимом, когда `mode=READ_ONLY`:

```python
duckdb.connect(path, read_only=True)
```

Это — **первый уровень защиты**: сама DuckDB connection не позволяет INSERT/UPDATE/DELETE.

`CacheProvider` MUST иметь **второй уровень защиты** (assertion guard): при попытке мутации через `CacheProvider.query_sql(...)` с `mode=READ_ONLY` MUST поднять `ReadOnlyAssertionError` или `PermissionError`.

Оба уровня защиты MUST присутствовать одновременно (defense in depth).

### D13. kill -9 recovery — acceptance criterion, не implementation detail

**Решение:** Spec описывает гарантию, не реализацию:

> После unclean termination (kill -9, OOM, crash) следующий owner MAY перехватить ownership через `try_claim()` (получит `my_generation > previous_generation`).
> Acceptance criterion: следующий процесс MUST иметь возможность reopen существующий `cache.duckdb` если DuckDB считает БД recoverable.

Implementation может использовать штатное DuckDB ATTACH + WAL replay + auto-recovery. Не зашиваем конкретный механизм.

### D5. Streamlit removal — отдельный change

**Решение:** Удаление `streamlit_app.py`, `SubprocessManager.spawn_streamlit`, `_streamlit_enabled()` — НЕ часть текущего change. Это отдельный change `remove-streamlit-runtime`.

Текущий change содержит ТОЛЬКО запрет импорта `streamlit` в runtime-коде через `runtime/entrypoints::Forbidden Behavior`.

### D6. WebSocket port check остаётся в gateway

**Решение:** WebSocket port check — server-only pre-startup проверка. Остаётся в `gateway.py::_entrypoint_main`. CLI НЕ выполняет эту проверку.

### D7. Cron = gateway-only

**Решение:** `CronService` создаётся ТОЛЬКО при `role="gateway"` (если `gateway.enable_cron=True`). CLI НЕ запускает `CronService`. Решает проблему «два процесса выполняют один jobs.json дважды».

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
  - `tests/test_cache_provider_mode.py` — `READ_ONLY` блокирует мутации; реальный DuckDB read_only connection.
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
- **[Risk]** `kill -9` оставляет DuckDB connection в PG, но `cache.duckdb` может быть corrupted. → **Mitigation:** DuckDB ATTACH auto-recovery (WAL replay); если fails — логирование ERROR + инструкция `rm cache.duckdb && restart`. Acceptance criterion: после unclean termination + takeover новый owner может продолжить работу без ручного восстановления.
- **[Risk]** Fencing — старый producer может записать одну строку после перехвата ownership (TOCTOU race). → **Mitigation:** двухуровневое fencing: (1) generation check атомарно с write через PG-транзакцию; (2) heartbeat с generation check в WHERE clause. `is_still_owner()` сам по себе НЕДОСТАТОЧЕН — это только первая проверка перед write. Без generation race существует. Verify в `tests/test_cache_ownership_claim.py::test_old_owner_fencing_with_generation`.
- **[Risk]** Профили переопределяют `gateway.cache.local_path` → два разных физических cache.duckdb → ownership confusion. → **Mitigation:** ConfigurationResolver MUST reject `gateway.cache.local_path` per-profile override; `gateway.cache.local_path` MUST быть shared runtime resource.
- **[Risk]** READ_ONLY через только assertion — обход через прямой SQL. → **Mitigation:** двухуровневая защита: DuckDB connection в реальном read_only mode (`duckdb.connect(read_only=True)`) И `CacheProvider` assertion guard. Verify в `tests/test_cache_provider_mode.py::test_read_only_connection_blocks_mutations`.

## Migration Plan

**Шаг 1 (текущий MINOR):** Реализация всех решений поэтапно (см. D1.7 staged implementation). DeprecationWarning в логи для `**kwargs` `enable_*`. Cron BREAKING (CLI не запускает). CLI = fixed test profile. CacheOwnershipCoordinator + PG-level claim + generation/fencing token. Streamlit удаление — отдельный change.

**Шаг 2 (следующий MINOR):** Удаление deprecated `enable_*` kwargs из `**kwargs`-обработки в `ApplicationContext.create()` (отдельный change `remove-deprecated-enable-kwargs`).

**Шаг 3 (отдельный change):** Удаление Streamlit (`remove-streamlit-runtime`).

**Шаг 4 (отдельный change, опционально):** Миграция standalone utilities с `**kwargs` на typed signature.

**Шаг 5 (отдельный change, опционально):** ConfigurationResolver reject per-profile `gateway.cache.local_path` override (если не сделано в Шаг 1).

**Rollback:** Если после раскрытия обнаружена критическая регрессия — revert на коммит до раскрытия.

## Open Questions

- Решены в proposal.md Open Questions:
  1. **PG-level claim mechanism: extend `agent_worker_claims` или новая таблица `agent_cache_ownership`?** — **Новая таблица** с фиксированным `resource_key='duckdb_cache'` и `generation` (fencing token).
  2. **Heartbeat интервал и TTL** — heartbeat 30 сек, TTL 60 сек (D4).
  3. **kill -9 recovery** — acceptance criterion, не implementation detail (D13).
  4. **`role="utility"`** — НЕ включается (см. «Что НЕ делается»).
  5. **Streamlit removal** — отдельный change (D5).
  6. **Fencing mechanism** — generation token (D4). Не advisory lock (теряется traceability), не `is_still_owner()` только (TOCTOU race).