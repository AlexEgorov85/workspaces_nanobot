# unify-cli-gateway-architecture

CLI и Gateway используют единый `ApplicationContext` и один `AgentLoop` runtime.

## Архитектура

```text
                    ApplicationContext
                           │
              ┌────────────┴────────────┐
              │                         │
          CLI entrypoint          Gateway entrypoint
              │                         │
       Console I/O              PostgresChannel
              │                         │
       in-memory bus            in-memory bus
              │                         │
              └────────────┬────────────┘
                           │
                      AgentLoop (общий)
                           │
         ┌─────────────────┼─────────────────┐
         │                 │                 │
      Skills            Tools            Memory
                           │
                     Data Runtime
                           │
                 CacheOwnershipCoordinator
                           │
                 ┌────────┴────────┐
                 │                 │
             OWNER (RW)        READER (RO)
                 │                 │
                 └───┬─────────┬───┘
                     │         │
                 CacheProvider  CacheProvider
                 (interface)   (interface)
                     │         │
                 ┌───┴────────┴───┐
                 │               │
            DuckDbCacheStore (current impl.)
                 │               │
            <local_path>/cache.duckdb
```

**CLI:**
- `terminal → in-memory MessageBus → AgentLoop`
- profile = `test` (hardcoded, без `--profile` CLI-аргумента)

**Gateway:**
- `PostgresChannel → in-memory MessageBus → AgentLoop`
- profile через `--profile` argv

**Cache:**
- Один snapshot-файл (`<local_path>/cache.duckdb`) для всех процессов.
- Ownership определяется через PostgreSQL claim (`agent_cache_ownership`, resource_key='local_cache').
- Один producer (READ_WRITE), остальные процессы read-only consumers (READ_ONLY).
- Atomic claim через `INSERT ... ON CONFLICT (resource_key) DO UPDATE`.
- Fencing: старый producer прекращает записи после потери ownership.

**Текущая concrete cache implementation:** DuckDB (`DuckDbCacheStore`).

Future: SQLite (`SQLiteCacheStore`) может заменить без изменения ownership contract или `CacheProvider` interface.