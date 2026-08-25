from __future__ import annotations

import json
from pathlib import Path

import pytest

PROJECT_JSON = Path(__file__).resolve().parent.parent / "project.json"


def _load_project_keys() -> dict:
    """Загрузить project.json (с поддержкой JSONC-комментариев из config.py)."""
    from config import _strip_jsonc_comments
    raw = PROJECT_JSON.read_text(encoding="utf-8")
    return json.loads(_strip_jsonc_comments(raw))


def _walk(node, prefix=()):
    """Рекурсивно собрать все dict-пути в JSON-дереве."""
    out = []
    if isinstance(node, dict):
        for k, v in node.items():
            new_prefix = prefix + (k,)
            if isinstance(v, dict):
                out.extend(_walk(v, new_prefix))
            else:
                out.append((".".join(new_prefix), v))
    return out


def _required_keys():
    """Обязательные ключи, которые должны быть объявлены в project.json.

    Источник: Фазы 2-4 рефакторинга hardcoded-значений.
    Дополняется по мере добавления новых настроек.
    """
    return [
        # channels.postgres
        ("channels.postgres.poll_interval", 10.0),
        ("channels.postgres.flush_interval", 5.0),
        ("channels.postgres.processing_timeout", 600),
        ("channels.postgres.unstick_interval", 120.0),
        ("channels.postgres.max_concurrent", 2),
        ("channels.postgres.allow_from", ["*"]),
        ("channels.postgres.messages_table", "agent_session_messages"),
        ("channels.postgres.meta_table", "agent_session_meta"),
        ("channels.postgres.table_name", "agent_conversation_messages"),
        ("channels.postgres.schema", "public"),
        ("channels.postgres.max_stuck_retries", 3),
        ("channels.postgres.msg_ctx_max_size", 100),
        ("channels.postgres.worker_id", ""),
        ("channels.postgres.claims_table", "agent_worker_claims"),
        ("channels.postgres.lease_interval", 15.0),
        ("channels.postgres.error_retry_delay", 60.0),
        ("channels.postgres.claim_strategy", "single"),
        ("channels.postgres.media_cache_dir", "data_store/cache/sessions"),
        # channels.postgres.pool
        ("channels.postgres.pool.min_conn", 1),
        ("channels.postgres.pool.max_conn", 4),
        ("channels.postgres.pool.pool_timeout", 5.0),
        # channels.redis
        ("channels.redis.poll_timeout", 5.0),
        ("channels.redis.max_concurrent", 1),
        ("channels.redis.allow_from", ["*"]),
        ("channels.redis.error_backoff_sec", 1.0),
        ("channels.redis.reply_to_max_size", 10000),
        ("channels.redis.reply_to_trim_to", 5000),
        # skills.audit_analyzer
        # Новая модель (Phase 7): tables[] + vector_indexes[] вместо db.* + vector_index.*
        ("skills.audit_analyzer.llm.max_tokens", 8192),
        ("skills.audit_analyzer.llm.temperature", 0.1),
        ("skills.audit_analyzer.tables", [
            {"name": "oarb.audit_reports", "tracking_column": "updated_at"},
            {"name": "oarb.audits", "tracking_column": "updated_at"},
            {"name": "oarb.report_items", "tracking_column": "updated_at"},
            {"name": "oarb.violations", "tracking_column": "updated_at"},
            {"name": "public.agent_predefined_scripts", "tracking_column": "updated_at", "label": "scripts_registry"},
        ]),
        ("skills.audit_analyzer.vector_indexes", [
            {"name": "audits_index", "source": "oarb.audits"},
            {"name": "violations_index", "source": "oarb.violations"},
            {"name": "audit_reports_index", "source": "oarb.audit_reports"},
        ]),
        # Sync-параметры глобальные, живут в gateway.sync.* (Phase 6 рефакторинга).
        ("gateway.sync.poll_interval_sec", 14400),
        ("gateway.sync.full_resync_every", 10),
        ("gateway.sync.max_queue_size", 10000),
        ("gateway.sync.reconnect_backoff_sec", 1.0),
        ("gateway.sync.reconnect_backoff_max_sec", 60.0),
        ("skills.audit_analyzer.embedding.base_url", "http://localhost:11434/api/embed"),
        ("skills.audit_analyzer.embedding.model", "mxbai-embed-large:latest"),
        ("skills.audit_analyzer.embedding.dimension", 1024),
        ("skills.audit_analyzer.embedding.http_timeout_sec", 60),
        ("skills.audit_analyzer.cache.enabled", True),
        ("skills.audit_analyzer.cache.engine", "duckdb"),
        ("skills.audit_analyzer.cache.max_age_sec", 3600),
        ("skills.audit_analyzer.cache.refresh_interval_sec", 3600),
        ("skills.audit_analyzer.cli.default_mode", "predefined"),
        ("skills.audit_analyzer.cli.default_format", "json"),
        ("skills.audit_analyzer.cli.max_retries", 3),
        ("skills.audit_analyzer.cli.timeout_sec", 60),
        # cli
        ("cli.show_reasoning", True),
        ("cli.llm_timeout", 300),
        ("cli.exec_timeout", 60),
        ("cli.max_iterations", 200),
        ("cli.log_level", "WARNING"),
        ("cli.repl_idle_timeout_sec", 1.0),
        ("cli.show_context_window", True),
        # benchmark
        ("benchmark.db_schema", "public"),
        ("benchmark.runs_table", "agent_benchmark_runs"),
        ("benchmark.results_table", "agent_benchmark_results"),
        # streamlit
        ("streamlit.enabled", False),
        ("streamlit.max_wait", 600),
        ("streamlit.poll_interval", 10.0),
        ("streamlit.files_dir", "data_store/streamlit_files"),
        ("streamlit.error_window_sec", 300),
        # gateway
        ("gateway.storage", "auto"),
        ("gateway.persist_threshold", 5000),
        ("gateway.persist_max_files", 100),
        ("gateway.persist_max_age_hours", 0),
        ("gateway.llm_timeout", 300),
        ("gateway.exec_timeout", 60),
        ("gateway.log_level", "INFO"),
        ("gateway.print_llm_calls", True),
        ("gateway.print_worker_activity", False),
        ("gateway.print_db_activity", False),
        ("gateway.restart_initial_delay_sec", 1.0),
        ("gateway.restart_max_delay_sec", 30.0),
        ("gateway.streamlit_port", 8501),
        ("gateway.streamlit_log_filename", "streamlit.log"),
        ("gateway.subprocess_shutdown_timeout_sec", 5.0),
        # gateway.duckdb_query / gateway.vector_search — инфраструктурные tools.
        ("gateway.duckdb_query.enable", True),
        ("gateway.duckdb_query.max_rows", 1000),
        ("gateway.duckdb_query.max_result_chars", 50000),
        ("gateway.duckdb_query.query_timeout_sec", 30),
        ("gateway.vector_search.enable", True),
        ("gateway.vector_search.default_top_k", 5),
        ("gateway.vector_search.max_top_k", 50),
        ("gateway.vector_search.default_threshold", 0.0),
        ("gateway.vector_search.max_query_chars", 4000),
        ("gateway.vector_search.max_result_chars", 16000),
        ("gateway.vector_search.timeout_sec", 30),
        ("gateway.vector_index.enable", True),
        ("gateway.vector_index.default_root", "data_store/vectors"),
        ("gateway.vector_index.backend", "faiss"),
        ("gateway.vector_index.storage_tables", ["oarb.audit_vectors"]),
        ("gateway.vector_index.cache_tables", [
            "oarb.audit_reports",
            "oarb.audits",
            "oarb.report_items",
            "oarb.violations",
            "oarb.audit_vectors",
        ]),
        # logging.db
        ("logging.db.enabled", True),
        ("logging.db.table_name", "agent_gateway_logs"),
        ("logging.db.schema", "public"),
        ("logging.db.flush_interval_sec", 5.0),
        ("logging.db.batch_size", 100),
        ("logging.db.queue_maxsize", 10000),
        ("logging.db.min_level", "INFO"),
        ("logging.db.dialect", "postgres"),
        ("logging.db.connect_backoff_sec", 1.0),
        ("logging.db.connect_backoff_max_sec", 60.0),
        ("logging.db.summary_max_chars", 200),
    ]


class TestGetSetting:
    """Проверка безопасного аксессора из config.py."""

    def test_existing_key(self):
        from config import SETTINGS, get_setting
        SETTINGS["test_get_setting_section"] = {"k": 42}
        try:
            assert get_setting("test_get_setting_section", "k") == 42
        finally:
            del SETTINGS["test_get_setting_section"]

    def test_missing_returns_default(self):
        from config import get_setting
        assert get_setting("nonexistent_section_xyz", "key", default="X") == "X"
        assert get_setting("nonexistent_section_xyz", default=None) is None

    def test_partial_path_returns_default(self):
        from config import SETTINGS, get_setting
        SETTINGS["partial_section"] = {"a": 1}
        try:
            assert get_setting("partial_section", "a", "b", default="X") == "X"
        finally:
            del SETTINGS["partial_section"]


class TestRequireSetting:
    """Строгий аксессор: отсутствие ключа — ошибка, а не тихий fallback."""

    def test_missing_raises_configuration_error(self):
        from config import ConfigurationError, require_setting
        with pytest.raises(ConfigurationError):
            require_setting("nonexistent_section_xyz", "key")

    def test_partial_path_raises(self):
        from config import SETTINGS, ConfigurationError, require_setting
        SETTINGS["partial_section"] = {"a": {"b": 1}}
        try:
            with pytest.raises(ConfigurationError):
                require_setting("partial_section", "a", "c")
        finally:
            del SETTINGS["partial_section"]

    def test_existing_key_returns_value(self):
        from config import SETTINGS, require_setting
        SETTINGS["test_req_section"] = {"k": 42}
        try:
            assert require_setting("test_req_section", "k") == 42
        finally:
            del SETTINGS["test_req_section"]


class TestProjectJsonShape:
    """project.json должен содержать все обязательные ключи с правильными дефолтами."""

    @classmethod
    def setup_class(cls):
        cls.data = _load_project_keys()
        cls.flat = dict(_walk(cls.data))

    @pytest.mark.parametrize("key_path,expected_default", _required_keys())
    def test_required_key_present_with_default(self, key_path, expected_default):
        assert key_path in self.flat, (
            f"Обязательный ключ {key_path!r} отсутствует в project.json"
        )
        actual = self.flat[key_path]
        assert actual == expected_default, (
            f"Ключ {key_path!r}: ожидалось {expected_default!r}, "
            f"получено {actual!r}"
        )


class TestJsoncParsable:
    def test_jsonc_valid(self):
        data = _load_project_keys()
        assert isinstance(data, dict)
