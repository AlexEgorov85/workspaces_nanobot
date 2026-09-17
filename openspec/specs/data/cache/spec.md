# Data Cache

## Purpose
Define the cache subsystem contract: source of truth, snapshot lifecycle, publication, atomicity, and consumer contract. The cache is a local DuckDB-based fast-access layer for read-mostly data synced from PostgreSQL.

## Source of Truth

- Permanent invariants: `docs/TARGET_ARCHITECTURE.md` (Cache section).
- Implementation: `docs/ARCHITECTURE.md` and `lib/services/cache_provider*.py` (descriptive).

## Requirements

### Requirement: PostgreSQL is the source of truth

The system SHALL treat PostgreSQL as the source of truth for cached tables.

#### Scenario: Cache desync from PostgreSQL

- **WHEN** a cached row disagrees with PostgreSQL
- **THEN** PostgreSQL SHALL win on the next sync; the cache SHALL NOT silently preserve stale data indefinitely.

### Requirement: Local ext4 storage

The system SHALL persist the DuckDB cache file at `gateway.cache.local_path` (a local ext4 path, NOT an NFS path).

#### Scenario: NFS storage attempted

- **WHEN** `gateway.cache.local_path` resolves to an NFS mount
- **THEN** the cache provider SHALL fail fast at startup with a clear error rather than silently corrupting state.

### Requirement: Single consumer contract

The system SHALL expose cache operations exclusively through `CacheProvider` (`query_sql`, `get_schema`, `explain`, `search_vector`).

#### Scenario: Skill queries the cache

- **WHEN** a Skill needs to query a cached table
- **THEN** it SHALL go through `CacheProvider` and SHALL NOT open the DuckDB file directly.

## Negative Requirements

The system SHALL NOT:

- write the DuckDB cache file directly to an NFS mount (empirically fails with `PID 0` locking errors).
- introduce a second cache store implementation alongside `cache_provider.py`.
- silently fall back to PostgreSQL when the cache is invalid; consumers SHALL be told.
- bypass the cache provider from Skill code.
- duplicate cache state outside the single cache file path.
