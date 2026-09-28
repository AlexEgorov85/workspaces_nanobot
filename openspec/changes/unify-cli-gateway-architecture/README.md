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
              PG → DuckDB      DuckDB RO
```

**CLI:**
- `terminal → in-memory MessageBus → AgentLoop`
- profile = `test` (hardcoded, без `--profile` CLI-аргумента)

**Gateway:**
- `PostgresChannel → in-memory MessageBus → AgentLoop`
- profile через `--profile` argv

**DuckDB:**
- Один файл `cache.duckdb` для всех процессов.
- Ownership sync определяется через PostgreSQL claim (`agent_cache_ownership`).
- Один producer (READ_WRITE), остальные процессы read-only consumers (READ_ONLY).
- Atomic claim через `INSERT ... ON CONFLICT (resource_key) DO UPDATE`.
- Fencing: старый producer прекращает записи после потери ownership.