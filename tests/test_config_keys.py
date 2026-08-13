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
        ("channels.postgres.poll_interval", 2.0),
        ("channels.postgres.flush_interval", 2.0),
        ("channels.postgres.processing_timeout", 120),
        ("channels.postgres.max_concurrent", 1),
        ("channels.postgres.allow_from", ["*"]),
        ("channels.postgres.messages_table", "session_messages"),
        ("channels.postgres.meta_table", "session_meta"),
        ("channels.postgres.table_name", "conversation_messages"),
        ("channels.postgres.schema", "public"),
        ("channels.postgres.max_stuck_retries", 3),
        ("channels.postgres.msg_ctx_max_size", 100),
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
        ("skills.audit_analyzer.llm_provider", "openai-compatible"),
        ("skills.audit_analyzer.llm_model", "gpt-4o-mini"),
        ("skills.audit_analyzer.llm_api_base", "https://api.openai.com/v1"),
        ("skills.audit_analyzer.llm_max_tokens", 8192),
        ("skills.audit_analyzer.llm_temperature", 0.1),
        ("skills.audit_analyzer.db_schema", "oarb"),
        ("skills.audit_analyzer.in_memory_enabled", True),
        ("skills.audit_analyzer.in_memory_engine", "duckdb"),
        ("skills.audit_analyzer.in_memory_cache_path", "cache/audit_cache.duckdb"),
        ("skills.audit_analyzer.poll_interval_sec", 60),
        ("skills.audit_analyzer.full_resync_every", 10),
        ("skills.audit_analyzer.sync_write_table", "audit_interactions"),
        ("skills.audit_analyzer.sync_max_queue_size", 10000),
        ("skills.audit_analyzer.reconnect_backoff_sec", 1.0),
        ("skills.audit_analyzer.reconnect_backoff_max_sec", 60.0),
        ("skills.audit_analyzer.embedding_base_url", "http://localhost:11434/api/embed"),
        ("skills.audit_analyzer.embedding_model", "mxbai-embed-large:latest"),
        ("skills.audit_analyzer.embedding_dimension", 1024),
        ("skills.audit_analyzer.embedding_http_timeout_sec", 60),
        ("skills.audit_analyzer.mode_vector_db_table", "oarb.audit_vectors"),
        ("skills.audit_analyzer.mode_vector_store_table", "oarb.vector_index_store"),
        ("skills.audit_analyzer.vector_index_default_path", "data_store/vectors/audits_index"),
        ("skills.audit_analyzer.cli_default_mode", "predefined"),
        ("skills.audit_analyzer.cli_default_format", "json"),
        ("skills.audit_analyzer.cli_max_retries", 3),
        ("skills.audit_analyzer.cli_timeout_sec", 60),
        ("skills.audit_analyzer.text_chunk_size", 500),
        ("skills.audit_analyzer.text_chunk_overlap", 80),
        ("skills.audit_analyzer.cache_max_age_sec", 3600),
        ("skills.audit_analyzer.cache_refresh_interval_sec", 3600),
        ("skills.audit_analyzer.build_batch_pause_sec", 0.5),
        # cli
        ("cli.show_reasoning", True),
        ("cli.llm_timeout", 300),
        ("cli.exec_timeout", 60),
        ("cli.max_iterations", 200),
        ("cli.log_level", "WARNING"),
        ("cli.repl_idle_timeout_sec", 1.0),
        # benchmark
        ("benchmark.db_schema", "public"),
        ("benchmark.runs_table", "benchmark_runs"),
        ("benchmark.results_table", "benchmark_results"),
        # streamlit
        ("streamlit.max_wait", 600),
        ("streamlit.poll_interval", 1.0),
        ("streamlit.files_dir", "data_store/streamlit_files"),
        ("streamlit.failed_window_sec", 300),
        # gateway
        ("gateway.storage", "auto"),
        ("gateway.persist_threshold", 5000),
        ("gateway.persist_max_files", 100),
        ("gateway.persist_max_age_hours", 0),
        ("gateway.llm_timeout", 300),
        ("gateway.exec_timeout", 60),
        ("gateway.log_level", "INFO"),
        ("gateway.restart_initial_delay_sec", 1.0),
        ("gateway.restart_max_delay_sec", 30.0),
        ("gateway.streamlit_port", 8501),
        ("gateway.streamlit_log_filename", "streamlit.log"),
        ("gateway.subprocess_shutdown_timeout_sec", 5.0),
        # logging.db
        ("logging.db.enabled", True),
        ("logging.db.table_name", "gateway_logs"),
        ("logging.db.schema", "public"),
        ("logging.db.flush_interval_sec", 5.0),
        ("logging.db.batch_size", 100),
        ("logging.db.queue_maxsize", 10000),
        ("logging.db.min_level", "INFO"),
        ("logging.db.dialect", "postgres"),
        ("logging.db.fallback_path", "logs/gateway_logs_fallback.jsonl"),
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
