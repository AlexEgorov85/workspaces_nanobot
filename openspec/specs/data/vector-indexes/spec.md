# Vector Indexes

## Purpose
Define the logical model for vector indexes: configuration source, index lifecycle, Skill/service access, and failure behavior. Vector indexes are managed centrally and exposed through `CacheProvider.search_vector`.

## Source of Truth

- Permanent invariants: `docs/TARGET_ARCHITECTURE.md` (Vector section).
- Implementation: `docs/VECTOR_INDEXES.md` and `lib/services/vector_index_service.py` (descriptive).

## Requirements

### Requirement: Single configuration source

The system SHALL read vector index configuration from `gateway.vector.index.indexes.*` in `project.json` only.

#### Scenario: Index configuration

- **WHEN** a vector index is added or modified
- **THEN** its declaration SHALL live under `gateway.vector.index.indexes.<name>` in `project.json`.

### Requirement: Storage table registered through infra API

The system SHALL persist vector embeddings in the table registered via `lib.core.infra_registration.register_vector_storage`.

#### Scenario: Vector storage table

- **WHEN** `gateway.vector.index.storage_table` is set
- **THEN** that table SHALL be registered through `register_vector_storage` so that `TableRegistry` knows about it for sync.

### Requirement: FAISS-backed

The system SHALL build vector indexes using FAISS, invoked through `tools/build_vectors.py` and `lib/services/vector_index_service.py`.

#### Scenario: Index build

- **WHEN** a vector index is built
- **THEN** the FAISS index SHALL be persisted under `<gateway.vector.index.default_root>/<index_name>` and SHALL be loaded on demand at query time.

### Requirement: Single access path

The system SHALL expose vector search exclusively through `CacheProvider.search_vector`.

#### Scenario: Skill performs vector search

- **WHEN** a Skill needs a vector similarity query
- **THEN** it SHALL call `CacheProvider.search_vector` and SHALL NOT load FAISS indexes directly.

## Negative Requirements

The system SHALL NOT:

- read vector index configuration from the legacy `public.agent_vector_index_config` table (kept for historical reference only; not authoritative).
- introduce a second vector storage backend alongside FAISS without an explicit OpenSpec change.
- silently fall back to a non-FAISS backend on FAISS errors.
- bypass `CacheProvider.search_vector` from Skill code.
- read the legacy `gateway.vector_index.*` configuration key (removed; runtime-mute if present).
