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

**`profile` MUST NOT быть параметром** — профиль resolved ДО через `_initialize_settings(profile=...)`. `ApplicationContext` читает `SETTINGS["profile"]` через `_config.SETTINGS` (или через DI в будущем).

`enable_*` MUST NOT быть именованными параметрами в typed signature. Они принимаются через `**kwargs`. Если kwarg передан — используется + `warnings.warn(..., DeprecationWarning, stacklevel=2)`. Если не передан — читается из `SETTINGS["gateway"].*`.

**Альтернативы:**
- Оставить `enable_*` в typed signature — отвергнуто: typed signature MUST быть source of truth для API; deprecated kwargs только через `**kwargs`.
- Оставить `profile` в typed signature — отвергнуто: profile — configuration-resolution concern, не runtime concern. Передача profile через constructor-arg делает ApplicationContext aware о "почему" профиль, что неправильно.
- Полностью удалить `enable_*` (без transitional period) — отвергнуто: 30+ существующих вызовов в тестах/standalone utilities сломаются.

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

### D4. `CacheOwnershipCoordinator` — atomic claim + fencing

**Решение:** Новый модуль `lib/services/cache_ownership.py`. Класс `CacheOwnershipCoordinator` инкапсулирует **только** ownership coordination (НЕ открытие DuckDB, НЕ sync). API:

```python
class CacheAccessMode(enum.Enum):
    READ_WRITE = "READ_WRITE"  # OWNER
    READ_ONLY = "READ_ONLY"    # READER

class CacheOwnershipCoordinator:
    def __init__(self, worker_id: str, dsn: str, resource_key: str = "duckdb_cache", ttl_seconds: int = 60):
        ...

    def try_claim(self) -> CacheAccessMode:
        """Atomic через PG-транзакцию:
        INSERT INTO agent_cache_ownership (resource_key, owner_id, last_heartbeat_at, expires_at)
        VALUES ('duckdb_cache', $worker_id, NOW(), NOW() + INTERVAL '60 seconds')
        ON CONFLICT (resource_key) DO UPDATE
        SET owner_id = EXCLUDED.owner_id,
            acquired_at = NOW(),
            last_heartbeat_at = NOW(),
            expires_at = EXCLUDED.expires_at
        WHERE agent_cache_ownership.expires_at < NOW()
        RETURNING owner_id;
        - Если RETURNING даёт наш owner_id → READ_WRITE (мы owner);
        - Если RETURNING даёт чужой owner_id → READ_ONLY (другой процесс owner).
        """

    def heartbeat(self) -> None:
        """UPDATE last_heartbeat_at=NOW(), expires_at=NOW() + INTERVAL '60 seconds'
        WHERE resource_key='duckdb_cache' AND owner_id=worker_id.
        Вызывается каждые 30 сек."""

    def release(self) -> None:
        """DELETE FROM agent_cache_ownership WHERE resource_key='duckdb_cache' AND owner_id=worker_id.
        Вызывается при clean shutdown."""

    def is_still_owner(self) -> bool:
        """SELECT owner_id FROM agent_cache_ownership WHERE resource_key='duckdb_cache' AND expires_at > NOW();
        Если owner_id == self.worker_id → True.
        Используется sync-потоком для fencing (проверка перед каждой записью)."""
```

Таблица `agent_cache_ownership`:

```sql
CREATE TABLE agent_cache_ownership (
    resource_key VARCHAR PRIMARY KEY,           -- фиксированное значение: 'duckdb_cache'
    owner_id VARCHAR NOT NULL,                    -- worker_id текущего владельца
    acquired_at TIMESTAMP NOT NULL DEFAULT NOW(),
    last_heartbeat_at TIMESTAMP NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMP NOT NULL
);
```

**Ownership key** = `'duckdb_cache'` (фиксированная строка, НЕ per-process, НЕ per-role). MUST быть ровно **один** активный claim на ресурс `<local_path>/cache.duckdb`. PR KV `(resource_key)` гарантирует, что в таблице в любой момент максимум одна строка.

**Atomic claim:** `INSERT ... ON CONFLICT (resource_key) DO UPDATE ... WHERE expires_at < NOW() RETURNING owner_id`. Это атомарно на уровне PG row-level lock. Два процесса одновременно делают claim → один INSERT/UPDATE проходит (RETURNING даёт его owner_id), второй получает RETURNING с чужим owner_id → READ_ONLY.

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
        mode = coord.try_claim()  # atomic

        # === STAGE 3: open DuckDB in determined mode ===
        ctx.cache_store = DuckDbCacheStore.open(path=cache_path, mode=mode)

        # === STAGE 4: create sync only if OWNER ===
        if mode == CacheAccessMode.READ_WRITE:
            ctx.sync_service = PgDuckDbSyncService(
                cache_store=ctx.cache_store,
                heartbeat_callback=coord.heartbeat,
                fence_callback=coord.is_still_owner,  # sync поток проверяет перед каждой записью
                on_shutdown=lambda: coord.release(),
            )
        else:
            ctx.sync_service = None
    else:
        ctx.cache_store = None
        ctx.sync_service = None
```

**Fencing:** `PgDuckDbSyncService` MUST проверять `coord.is_still_owner()` перед каждой записью в DuckDB. Если `False` → немедленно остановить sync (raise exception или set internal flag, итерации прекращаются).

**Альтернативы:**
- Extend `agent_worker_claims` (через `claim_type` колонку) — отвергнуто: разная семантика (task = claim на сообщение для обработки; cache = claim на long-lived resource ownership). Отдельная таблица делает контракт явным.
- File lock на `cache.duckdb` — отвергнуто: DuckDB уже использует flock; layering поверх — fragile; TTL/stale detection через PG проще.
- Per-process или per-role ownership key — отвергнуто: пользователь явно сказал «один owner на ресурс», не на процесс/роль.
- Per-row claim (multiple owners, but один writer) — отвергнуто: модель ownership — single owner per resource, не multi-owner with one-writer semantics.

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

### D11. local filesystem (НЕ ext4) для DuckDB

**Решение:** Система ДОЛЖНА хранить DuckDB-файл кэша на локальной filesystem с требуемой DuckDB locking semantics (POSIX `fcntl` flock, etc.). Network/shared filesystem (NFS, SMB, etc.) — запрещён. Конкретная FS не специфицируется.

`CacheProvider` MUST reject путь на NFS или другую network filesystem ДО открытия DuckDB — fail-fast с явной ошибкой.

### D12. kill -9 recovery для DuckDB

**Решение:** При аварийном завершении producer-процесса (`kill -9`, OOM, crash) DuckDB может остаться в inconsistent state. `DuckDbCacheStore.open(mode=READ_WRITE)` для нового owner MUST:

1. Попытаться ATTACH к существующему `cache.duckdb`.
2. Если ATTACH fails — DuckDB выполняет auto-recovery (WAL replay) автоматически.
3. Если recovery fails — система логирует ERROR и предлагает пользователю `rm cache.duckdb && restart`.

Ownership claim в PG имеет TTL 60 сек — после этого другой процесс может перехватить. DuckDB recovery занимает секунды (auto-recovery при ATTACH). Никаких дополнительных механизмов не требуется.

### D13. Тесты — backward compatibility и новые сценарии

**Решение:**
- Существующие тесты с `ApplicationContext.create(enable_audit=True/False, ...)` — продолжают работать (deprecated kwargs через `**kwargs`).
- Новые тесты:
  - `tests/test_application_context_role.py` — composition matrix, signature checks (no `profile`).
  - `tests/test_cli_uses_in_memory_bus.py` — REPL использует bus, не PostgresChannel.
  - `tests/test_cache_ownership_claim.py` — atomic claim, concurrent claim, stale takeover, fencing.
  - `tests/test_cache_provider_mode.py` — `READ_ONLY` блокирует мутации.
  - `tests/test_cache_provider_role_paths.py` — `resolve_publish_path(role="cli") == resolve_publish_path(role="gateway") == cache.duckdb`.
  - `tests/test_agent_loop_transport_agnostic.py` — AgentLoop работает только через bus.
  - `tests/test_streamlit_imports_removed.py` — отсутствие Streamlit-импортов.
  - `tests/test_cli_no_profile.py` — CLI rejects `--profile`, hardcodes test, ignores env.
  - `tests/test_gateway_accepts_profile.py` — gateway accepts `--profile`.

## Risks / Trade-offs

- **[Risk]** Standalone-утилиты (`tools/build_vectors.py` и др.) забывают перейти на новую сигнатуру — НЕ блокирует этот change; они используют `**kwargs` для backward compat. → **Mitigation:** CHANGELOG документирует deprecation, отдельный change для миграции utilities.
- **[Risk]** Два процесса стартуют одновременно и оба пытаются INSERT в `agent_cache_ownership` → атомарный claim гарантирует, что один получает `READ_WRITE`, другой `READ_ONLY`. → **Mitigation:** `INSERT ... ON CONFLICT (resource_key) DO UPDATE ... RETURNING owner_id` — PG row-level lock. Verify в `tests/test_cache_ownership_claim.py::test_concurrent_claim_exactly_one_owner`.
- **[Risk]** Cron = gateway-only — пользователи CLI теряют cron. → **Mitigation:** документируется в CHANGELOG; альтернатива — запустить gateway (always-on).
- **[Risk]** `kill -9` оставляет DuckDB connection в PG, но `cache.duckdb` может быть corrupted. → **Mitigation:** DuckDB ATTACH auto-recovery (WAL replay); если fails — логирование ERROR + инструкция `rm cache.duckdb && restart`.
- **[Risk]** Fencing — старый producer может записать одну строку после перехвата ownership (TOCTOU race). → **Mitigation:** `coord.is_still_owner()` проверяется перед КАЖДОЙ записью; latency = несколько ms. Test `test_old_owner_fencing` проверяет, что после `coord.release()` (или после heartbeat expiry в другом процессе) sync немедленно останавливается.

## Migration Plan

**Шаг 1 (текущий MINOR):** Реализация всех решений. DeprecationWarning в логи для `**kwargs` `enable_*`. Cron BREAKING (CLI не запускает). CLI = fixed test profile. CacheOwnershipCoordinator + PG-level claim + fencing. Streamlit удаление — отдельный change.

**Шаг 2 (следующий MINOR):** Удаление deprecated `enable_*` kwargs из `**kwargs`-обработки в `ApplicationContext.create()` (отдельный change `remove-deprecated-enable-kwargs`).

**Шаг 3 (отдельный change):** Удаление Streamlit (`remove-streamlit-runtime`).

**Шаг 4 (отдельный change, опционально):** Миграция standalone utilities с `**kwargs` на typed signature.

**Rollback:** Если после раскрытия обнаружена критическая регрессия — revert на коммит до раскрытия.

## Open Questions

- Решены в proposal.md Open Questions:
  1. **PG-level claim mechanism: extend `agent_worker_claims` или новая таблица `agent_cache_ownership`?** — **Новая таблица** с фиксированным `resource_key='duckdb_cache'`.
  2. **Heartbeat интервал и TTL** — heartbeat 30 сек, TTL 60 сек (D4).
  3. **kill -9 recovery** — DuckDB ATTACH auto-recovery (D12).
  4. **`role="utility"`** — НЕ включается (см. «Что НЕ делается»).
  5. **Streamlit removal** — отдельный change (D5).