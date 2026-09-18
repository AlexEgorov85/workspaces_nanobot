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
        # общее поведение для всех каналов (Postgres, Redis, будущие)
        ("channels.document_text_threshold", 20000),
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
        ("skills.legal_summarizer.enabled", True),
        ("skills.legal_summarizer.cli.default_length", "brief"),
        ("skills.legal_summarizer.cli.timeout_sec", 120),
        ("skills.legal_summarizer.cli.max_retries", 3),
        ("skills.legal_summarizer.llm.max_tokens", 8192),
        ("skills.legal_summarizer.llm.temperature", 0.1),
        ("skills.legal_summarizer.chunking.chunk_size", 100000),
        ("skills.legal_summarizer.chunking.chunk_overlap", 0),
        ("skills.legal_summarizer.chunking.single_call_threshold", 20000),
        ("skills.legal_summarizer.chunking.chunk_size_input_ratio", 0.5),
        ("skills.legal_summarizer.chunking.brief_input_ratio", 0.13),
        ("skills.legal_summarizer.brief_context.max_chars_fallback", 30000),
        ("skills.legal_summarizer.brief_context.chars_per_token", 3.5),
        ("skills.legal_summarizer.brief_context.structure_max_chars", 12000),
        ("skills.audit_analyzer.tables", [
            {"name": "oarb.audit_reports"},
            {"name": "oarb.audits"},
            {"name": "oarb.report_items"},
            {"name": "oarb.violations"},
            {"name": "public.agent_predefined_scripts", "label": "scripts_registry"},
        ]),
        ("skills.audit_analyzer.vector_indexes", [
            {"name": "audits_index"},
            {"name": "violations_index"},
            {"name": "audit_reports_index"},
        ]),
        # Sync-параметры глобальные, живут в gateway.sync.* (Phase 6 рефакторинга).
        ("gateway.sync.poll_interval_sec", 14400),
        ("gateway.sync.full_resync_every", 10),
        ("gateway.sync.max_queue_size", 10000),
        ("gateway.sync.reconnect_backoff_sec", 1.0),
        ("gateway.sync.reconnect_backoff_max_sec", 60.0),
        # Embedding-параметры захардкожены в cache_provider_impl (модульные
        # константы); секция gateway.vector.embedding удалена. Бearer-токен —
        # переменная окружения OS EMBED_TOKEN. Индексы декларируются в
        # gateway.vector.index.indexes (перенесено из PG-реестра
        # agent_vector_index_config, который больше не читается кодом).
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
        ("streamlit.enabled", True),
        ("streamlit.max_wait", 600),
        ("streamlit.poll_interval", 10.0),
        ("streamlit.files_dir", "data_store/streamlit_files"),
        ("streamlit.error_window_sec", 300),
        # gateway
        ("gateway.storage", "file"),
        ("gateway.persist_threshold", 50000),
        ("gateway.persist_max_files", 100),
        ("gateway.persist_max_age_hours", 0),
        ("gateway.llm_timeout", 300),
        ("gateway.exec_timeout", 0),
        ("gateway.log_level", "INFO"),
        ("gateway.print_llm_calls", True),
        ("gateway.print_worker_activity", False),
        ("gateway.print_db_activity", False),
        ("gateway.restart_initial_delay_sec", 1.0),
        ("gateway.restart_max_delay_sec", 30.0),
        ("gateway.streamlit_port", 8501),
        ("gateway.streamlit_log_filename", "streamlit.log"),
        ("gateway.subprocess_shutdown_timeout_sec", 5.0),
        # gateway.duckdb_query / gateway.vector_search — удалены (этап 18):
        # Agent-facing tools (duckdb_query_tool.py, vector_search_tool.py)
        # удалены; Agent работает через Core capability (CacheProvider).
        ("gateway.vector.index.enable", True),
        ("gateway.vector.index.default_root", "data_store/vectors"),
        ("gateway.vector.index.backend", "faiss"),
        ("gateway.vector.index.storage_table", "oarb.audit_vectors"),
        ("gateway.vector.index.indexes.audits_index.table", "oarb.audits"),
        ("gateway.vector.index.indexes.audits_index.pk", "id"),
        ("gateway.vector.index.indexes.audits_index.metric", "cosine"),
        ("gateway.vector.index.indexes.audits_index.enabled", True),
        ("gateway.vector.index.indexes.violations_index.table", "oarb.violations"),
        ("gateway.vector.index.indexes.violations_index.pk", "id"),
        ("gateway.vector.index.indexes.violations_index.metric", "cosine"),
        ("gateway.vector.index.indexes.audit_reports_index.table", "oarb.audit_reports"),
        ("gateway.vector.index.indexes.audit_reports_index.pk", "id"),
        ("gateway.vector.index.indexes.audit_reports_index.metric", "cosine"),
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


class TestLoggingDbFlushIntervalValidation:
    """``logging.db.flush_interval_sec`` валидируется ``LoggingDbSettings``.

    Диапазон ``0.5 ≤ value ≤ 60.0``. Дефолт — ``5.0``. Вне диапазона —
    ``pydantic.ValidationError`` (это уровень модели, а не
    ``ConfigurationError``).
    """

    def test_default_is_five(self):
        from lib.core.project_settings import LoggingDbSettings
        # Спека change требует: ``LoggingDbSettings().
        # flush_interval_sec == 5.0`` — типизированная модель ЯВЛЯЕТСЯ
        # источником default-value (не ``ApplicationContext``).
        assert LoggingDbSettings().flush_interval_sec == 5.0

    def test_in_range(self):
        from lib.core.project_settings import LoggingDbSettings
        assert LoggingDbSettings(flush_interval_sec=5.0).flush_interval_sec == 5.0
        assert LoggingDbSettings(flush_interval_sec=0.5).flush_interval_sec == 0.5
        assert LoggingDbSettings(flush_interval_sec=60.0).flush_interval_sec == 60.0

    def test_below_minimum_raises(self):
        from lib.core.project_settings import LoggingDbSettings
        with pytest.raises(Exception) as exc_info:
            LoggingDbSettings(flush_interval_sec=0.1)
        assert "flush_interval_sec" in str(exc_info.value)

    def test_above_maximum_raises(self):
        from lib.core.project_settings import LoggingDbSettings
        with pytest.raises(Exception) as exc_info:
            LoggingDbSettings(flush_interval_sec=70.0)
        assert "flush_interval_sec" in str(exc_info.value)
