# Structurally identical function bodies

Bodies normalised (names/strings replaced) then hashed. Strong signal of
copy-paste, generated scaffolding, or intentional parallel implementations.
**79 function bodies form clone groups.**


### `0342da2b898f07ed` — 2 copies

- workspace/skills/legal_summarizer/scripts/chunking/_text_helpers.py::fit_input (16L)
- workspace/skills/legal_summarizer/scripts/execution/hierarchical.py::deterministic_truncate (20L)

### `0f36065bca909ed1` — 8 copies

- lib/services/cache_provider.py:CacheProvider.preload_indexes (7L)
- lib/services/cache_provider.py:CacheProvider.search_vector (14L)
- lib/services/cache_provider.py:CacheProvider.query_sql (7L)
- lib/services/cache_provider.py:CacheProvider.explain (7L)
- lib/services/cache_provider.py:CacheProvider.get_schema (7L)
- lib/services/cache_provider.py:CacheIngestion.upsert_records (18L)
- lib/services/cache_provider.py:CacheIngestion.replace_records (11L)
- lib/services/cache_provider.py:CacheIngestion.ensure_schema (8L)

### `148bd2f353f1f608` — 2 copies

- workspace/skills/audit_analyzer/scripts/predefined/validator.py:ValidationError.__init__ (4L)
- workspace/skills/legal_summarizer/scripts/llm/retry.py:ChunkResultParseError.__init__ (5L)

### `1d65ca8c2e15184f` — 2 copies

- lib/utils/outbound_meta.py::is_dropped (10L)
- tools/architecture_guard.py::is_factory_pattern (5L)

### `24aa0244b7ddcd52` — 2 copies

- workspace/tools/example.py:ExampleTool.create (7L)
- workspace/tools/legal_summarizer_query.py:LegalSummarizerQueryTool.create (7L)

### `3969ef6e624f44d0` — 3 copies

- workspace/skills/legal_summarizer/scripts/document/block_ownership.py::block_to_node (12L)
- workspace/skills/legal_summarizer/scripts/document/structure.py:DocumentStructure.block_to_node (15L)
- workspace/skills/legal_summarizer/scripts/document/structure.py::block_to_node (12L)

### `4e2206fc9ed84e8b` — 2 copies

- lib/channels/postgres_channel.py::_resolve_sfs_base (14L)
- lib/channels/redis_channel.py::_resolve_sfs_base (7L)

### `4fa97ce345d3ddc6` — 2 copies

- workspace/skills/legal_summarizer/scripts/chunking/chunker.py::_collect_owner_section_ids (14L)
- workspace/skills/legal_summarizer/scripts/chunking/structural_packing.py::_collect_section_ids_for_range (14L)

### `5b6b9d7a07661c2f` — 2 copies

- lib/services/table_registry.py:TableResource.__post_init__ (6L)
- lib/services/table_registry.py:VectorResource.__post_init__ (6L)

### `67c685f78aa53ecd` — 2 copies

- workspace/skills/legal_summarizer/scripts/document/block_ownership.py::build_block_ownership (22L)
- workspace/skills/legal_summarizer/scripts/document/structure.py::build_block_ownership (22L)

### `76bd8296fd164d35` — 2 copies

- lib/hooks/tool_audit_hook.py:ToolAuditHook.drain (13L)
- lib/hooks/tool_audit_hook.py:ToolAuditHook.drain_calls (11L)

### `79086df31054c6d2` — 2 copies

- lib/services/llm_usage_store_factory.py::_default_sqlite_path (9L)
- lib/services/session_cold_sync_service.py::resolve_default_sqlite_path (6L)

### `792237f5ae1ad71f` — 2 copies

- lib/services/cache_load_service.py:CacheLoadService._db_run (7L)
- lib/services/db_logging_service.py:DbLoggingService._db_run (7L)

### `8bb107ce7b83109b` — 2 copies

- workspace/skills/legal_summarizer/scripts/document/analysis.py:DocumentAnalysis.get_chunk (5L)
- workspace/skills/legal_summarizer/scripts/planning/plan.py:ExecutionPlan.get_batch (5L)

### `9a3819d096df589e` — 2 copies

- lib/services/context_compaction.py:ContextCompactionService._current_session_key (6L)
- workspace/tools/history_search_tool.py::_current_session_key (7L)

### `a1acd9d1ac3f29b7` — 2 copies

- cli_agent.py::script_dir_for_runtime (10L)
- gateway.py::script_dir_for_runtime (11L)

### `a2a398a578c3a8ec` — 2 copies

- workspace/skills/legal_summarizer/scripts/cache/document_cache.py::_skill_repo_root (7L)
- workspace/skills/legal_summarizer/scripts/cache/manifest.py::skill_repo_root (7L)

### `acd84e033d20b7c0` — 2 copies

- workspace/skills/legal_summarizer/scripts/document/block_ownership.py::owner_for_block (24L)
- workspace/skills/legal_summarizer/scripts/document/structure.py::owner_for_block (24L)

### `af0e4dc7b5b4646c` — 2 copies

- benchmarks/evaluator.py::_check_tools (16L)
- benchmarks/evaluator.py::_check_skills (16L)

### `b4d294d76bce641e` — 3 copies

- lib/hooks/database_logging_hook.py::_current_request_sender_id (22L)
- lib/services/context_compaction.py::_current_request_sender_id (21L)
- workspace/tools/history_search_tool.py::_current_user_id (30L)

### `bcf29450f713bf68` — 2 copies

- lib/services/session_cold_sync_service.py:SessionColdSyncService._log_stale (31L)
- lib/services/session_cold_sync_service.py:SessionColdSyncService._log_lag_exceeded (27L)

### `be9e67d14d919c5b` — 2 copies

- lib/services/session_cold_sync_service.py:SessionColdSyncService._quote (5L)
- lib/session/pg_session_manager.py:PGSessionManager._quote (5L)

### `c280600df15ce5dc` — 2 copies

- workspace/skills/legal_summarizer/scripts/application/brief_context.py::_resolve_context_window_tokens (26L)
- workspace/skills/legal_summarizer/scripts/application/pipeline_structure.py::_read_context_window_tokens (28L)

### `c3f57a7ad13b0820` — 2 copies

- workspace/skills/audit_analyzer/scripts/predefined/db_loader.py:DBScriptProvider.query_sql (5L)
- workspace/skills/audit_analyzer/scripts/predefined/mode.py:CacheQueryService.query_sql (5L)

### `c92eb02090696b74` — 2 copies

- lib/services/transcription_service.py:TranscriptionService.provider (6L)
- lib/services/transcription_service.py:TranscriptionService.get_language (11L)

### `d45ae4d0bef74e2a` — 2 copies

- workspace/skills/legal_summarizer/scripts/document/block_ownership.py::_depth_of (8L)
- workspace/skills/legal_summarizer/scripts/document/structure.py::_depth_of (8L)

### `d70885bee92a95fc` — 2 copies

- lib/core/bus_factory.py:BusFactory.__init__ (7L)
- lib/services/preload_service.py:PreloadService.__init__ (7L)

### `db2ca56f98e072e5` — 4 copies

- workspace/tools/compact_context.py:CompactContextTool.description (8L)
- workspace/tools/example.py:ExampleTool.description (5L)
- workspace/tools/history_search_tool.py:HistorySearchTool.description (42L)
- workspace/tools/legal_summarizer_query.py:LegalSummarizerQueryTool.description (10L)

### `dfd7d9f4c1be771b` — 2 copies

- workspace/tools/example.py:ExampleTool._read_settings_section (29L)
- workspace/tools/history_search_tool.py:HistorySearchTool._read_settings_section (23L)

### `e1b49c614e9e5b97` — 2 copies

- lib/core/skill_config.py::build_cache_provider (27L)
- tools/check_indexes.py::_open_provider (10L)

### `e259d743deb0f944` — 2 copies

- lib/core/infra_registration.py::_settings (4L)
- tools/check_worker_pool_integrity.py::_connect (4L)

### `ece0770da8687d74` — 3 copies

- lib/services/runtime_patcher.py::_session_key_of (10L)
- lib/utils/outbound_meta.py::msg_session_key (9L)
- workspace/hooks/recent_files_hook.py:RecentFilesHook._bucket_key (4L)

### `f5fb36835a558e01` — 2 copies

- gateway.py::_gateway_print_llm_calls (12L)
- gateway.py::_gateway_print_worker_activity (13L)

### `f9f99bfc8cf3c1c1` — 2 copies

- workspace/hooks/debug_stream_diag.py:StreamDiagnosisHook.on_stream (5L)
- workspace/hooks/debug_stream_diag.py:StreamDiagnosisHook.emit_reasoning (5L)

