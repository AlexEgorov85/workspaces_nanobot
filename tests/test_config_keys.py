from __future__ import annotations

import json
from pathlib import Path

import pytest
from config import AGENT_SECTIONS, runtime_table  # noqa: F401

CONFIG_JSON = Path(__file__).resolve().parent.parent / "config.json"


def _read_config_json() -> dict:
    """Сырой ``config.json`` — как его видит потребитель файла."""
    return json.loads(CONFIG_JSON.read_text(encoding="utf-8"))


def _load_config_keys() -> dict:
    """Загрузить config.json в виде, который реально видит код.

    ``gateway.agent.*`` поднимается в корень тем же
    ``config._lift_agent_sections``, что и в
    ``resolve_application_config``, поэтому обязательные ключи
    проверяются по путям из ``SETTINGS`` (``logging.db.*`` и т.п.), а не
    по физическому расположению в файле.
    """
    from config import _lift_agent_sections, _strip_jsonc_comments
    data = json.loads(_strip_jsonc_comments(CONFIG_JSON.read_text(encoding="utf-8")))
    _lift_agent_sections(data)
    return data


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
    """Обязательные ключи, которые должны быть объявлены в config.json.

    Источник: Фазы 2-4 рефакторинга hardcoded-значений.
    Дополняется по мере добавления новых настроек.
    """
    return [
        # channels.postgres
        ("channels.postgres.poll_interval", 10.0),
        ("channels.postgres.queue_report_interval", 30.0),
        ("channels.postgres.flush_interval", 5.0),
        ("channels.postgres.processing_timeout", 600),
        ("channels.postgres.unstick_interval", 120.0),
        ("channels.postgres.max_concurrent", 2),
        ("channels.postgres.allow_from", ["*"]),
        ("channels.postgres.messages_table", runtime_table("session_messages")),
        ("channels.postgres.meta_table", runtime_table("session_meta")),
        ("channels.postgres.table_name", runtime_table("conversation_messages")),
        ("channels.postgres.schema", "public"),
        ("channels.postgres.max_stuck_retries", 3),
        ("channels.postgres.msg_ctx_max_size", 100),
        ("channels.postgres.worker_id", ""),
        ("channels.postgres.error_retry_delay", 60.0),
        # ``channels.postgres.media_cache_dir`` снят вместе с change'ом
        # ``2026-10-03-session-files``: канал берёт каталог сессии у резолвера,
        # а объявление держало красным страж конфигов. Проверка, что ключ
        # больше не читается, живёт в ``tests/test_postgres_channel.py``.
        # session_files
        # Корень каталогов сессий. Значение — НЕ развёрнутая ``${NANOBOT_WORKSPACE}``,
        # а литерал: ``_load_config_keys`` поднимает секцию тем же lift'ом, что и
        # ``SETTINGS``, но подстановку ``${VAR}`` не выполняет. Подстановка
        # проходит позже по всему ``cfg`` (шаг 5 в ``resolve_application_config``),
        # к тому моменту секция уже в корне — поэтому порядок не мешает.
        # Декларация читается как ``SETTINGS["session_files"]["root"]``
        # (``lib/services/session_files.py::DECLARED_ROOT_PATH``).
        ("session_files.root", "${NANOBOT_WORKSPACE}/data_store/sessions"),
        # общее поведение для всех каналов (Postgres, Redis, будущие)
        ("channels.document_text_threshold", 20000),
        # channels.postgres.pool
        ("channels.postgres.pool.min_conn", 1),
        ("channels.postgres.pool.max_conn", 4),
        ("channels.postgres.pool.pool_timeout", 5.0),
        # channels.redis удалён вместе с каналом: каналов один — PostgreSQL.
        # skills.audit_analyzer
        # Новая модель (Phase 7): tables[] + vector_indexes[] вместо db.* + vector_index.*
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
        # Секция ``gateway.sync.*`` удалена вместе со снимком
        # ``PgDuckDbSyncService`` (change ``drop-local-cache-read-from-pg``):
        # фонового режима синхронизации нет, кеш — разовая
        # операция. Наименое ограничение число потоков загрузки —
        # размер пула взят из ``channels.postgres.pool.max_conn``.
        # Storage-hybridization: upstream LLMUsageStore + cold-storage mirror.
        ("gateway.usage_store.enabled", True),
        ("gateway.session_cold_sync.enabled", True),
        ("gateway.session_cold_sync.sync_interval_sec", 30.0),
        ("gateway.session_cold_sync.batch_size", 50),
        ("gateway.session_cold_sync.stale_tolerance_seconds", 120),
        ("gateway.session_cold_sync.sync_lag_threshold_seconds", 3600),
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
        # gateway
        ("gateway.storage", "file"),
        ("gateway.persist_threshold", 50000),
        ("gateway.persist_max_files", 100),
        ("gateway.persist_max_age_hours", 0),
        ("gateway.llm_timeout", 300),
        ("gateway.exec_timeout", 0),
        ("gateway.log_level", "INFO"),
        # Глубина вывода объявляется ОДНИМ ключом. Четыре булева флага
        # (``print_llm_calls``/``print_worker_activity``/``print_db_activity``/
        # ``print_tools``) больше не выбирают глубину: их отсутствие
        # проверяет отдельный страж ``test_single_level_replaces_the_boolean_
        # flags`` в ``tests/test_operator_console_levels.py``.
        ("gateway.console_level", "turn"),
        ("gateway.restart_initial_delay_sec", 1.0),
        ("gateway.restart_max_delay_sec", 30.0),
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
        ("logging.db.table_name", runtime_table("gateway_logs")),
        ("logging.db.schema", "public"),
        ("logging.db.flush_interval_sec", 5.0),
        ("logging.db.batch_size", 100),
        ("logging.db.queue_maxsize", 10000),
        # logging.db.min_level намеренно НЕ в этом списке: его значение —
        # решение оператора, а этот страж фиксирует значения. Закрепив здесь
        # конкретный уровень, страж запрещал бы менятьverbosity, то есть
        # требование «уровень регулируется в одном месте, без правки кода»
        # превратилось бы в «уровень нельзя поменять вообще». Принадлежность
        # шкале проверяет test_min_level_is_a_declared_level ниже.
        ("logging.db.dialect", "postgres"),
        ("logging.db.connect_backoff_sec", 1.0),
        ("logging.db.connect_backoff_max_sec", 60.0),
        ("logging.db.summary_max_chars", 200),
        # enterprise_mcp — объявление фоновой ноги к операциям платформы.
        # Второе объявление, ``tools.mcpServers.enterprise``, живёт в файле
        # рядом: процессов платформы два, и это объявлено, а не вышло
        # случайно. Причины, которыми секция ``tools.mcpServers`` когда-то
        # оставляли пустой, были ложны — ``ENTERPRISE_EXEC_REQUIRE_CALL_META``
        # это флаг с дефолтом ``False``, а не требование, и ``list_tools``
        # внутри вполне обычный ``session.list_tools()``. Пути конкретной
        # машины нет — только подстановки из os.environ (_export_runtime_env).
        ("enterprise_mcp.enabled", True),
        ("enterprise_mcp.command", "${NANOBOT_PYTHON}"),
        ("enterprise_mcp.args", ["-m", "servers.enterprise.server"]),
        ("enterprise_mcp.cwd", "${NANOBOT_PROJECT_ROOT}/mcp-platform"),
        ("enterprise_mcp.tool_timeout_sec", 180.0),
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


class TestConfigFileShape:
    """config.json должен содержать все обязательные ключи с правильными дефолтами."""

    @classmethod
    def setup_class(cls):
        cls.data = _load_config_keys()
        cls.flat = dict(_walk(cls.data))

    @pytest.mark.parametrize("key_path,expected_default", _required_keys())
    def test_required_key_present_with_default(self, key_path, expected_default):
        assert key_path in self.flat, (
            f"Обязательный ключ {key_path!r} отсутствует в config.json"
        )
        actual = self.flat[key_path]
        assert actual == expected_default, (
            f"Ключ {key_path!r}: ожидалось {expected_default!r}, "
            f"получено {actual!r}"
        )

    def test_min_level_is_a_declared_level(self):
        """Порог обязан СУЩЕСТВОВАТЬ и быть уровнем общей шкалы.

        Значение здесь намеренно не закреплено: порог — единственный
        регулятор громкости журнала, и он по требованию заказчика меняется
        правкой этого файла. Закрепить значение значило бы запретить менять.

        Но «не закреплено» не значит «не проверено»: уровень обязан быть
        членом шкалы, объявленной писателем, иначе опечатка дойдёт до него
        и превратится в отказ — то есть в отказ поднять агента из-за одной
        буквы в конфиге.

        Шкала берётся у писателя агента, а не у платформы: платформенный
        пакет недоступен из агентских тестов по границе процессов. Равенство
        этой шкалы платформенной закреплено отдельно
        (``tests/test_journal_level_canonical.py``), так что цепочка
        «конфиг → шкала агента → шкала платформы» покрыта целиком.
        """
        from lib.services.db_logging_service import JOURNAL_LEVEL_RANKS

        key_path = "logging.db.min_level"
        assert key_path in self.flat, (
            f"Ключ {key_path!r} отсутствует в config.json: без него порог "
            f"нечем регулировать, и вернётся молчаливый дефолт"
        )
        actual = self.flat[key_path]
        assert actual in JOURNAL_LEVEL_RANKS, (
            f"{key_path}={actual!r} не входит в объявленную шкалу "
            f"{tuple(JOURNAL_LEVEL_RANKS)}; писатель откажет событие, "
            f"а не отбросит его"
        )


class TestConfigJsonIsStrictJson:
    """config.json — строгий JSON, а не JSONC.

    Его читает штатный ``json.loads`` (``ConfigService._pre_resolve_env_refs``)
    и pydantic-схема nanobot, поэтому комментарии в нём недопустимы.
    Объяснения к секциям живут в docstring ``config.py``.
    """

    def test_strict_json_valid(self):
        assert isinstance(_read_config_json(), dict)

    def test_accepted_by_nanobot_schema(self):
        """Корень ``config.json`` разбирает схема nanobot: она отвергает
        любой неизвестный ключ верхнего уровня (``ConfigLoadError``), то
        есть опечатка на верхнем уровне ломает старт gateway/CLI."""
        from nanobot.config.loader import load_config

        assert load_config(CONFIG_JSON) is not None

    @pytest.mark.parametrize("section", sorted(AGENT_SECTIONS))
    def test_agent_section_lives_under_gateway_agent(self, section):
        """Секции без места в корне схемы объявлены ровно один раз —
        под ``gateway.agent`` и подняты в корень при merge."""
        raw = _read_config_json()
        assert section in raw["gateway"]["agent"], (
            f"{section!r} должен быть объявлен в gateway.agent "
            f"(config.py:AGENT_SECTIONS)"
        )
        assert section not in raw, (
            f"{section!r} в корне config.json: схема nanobot его отвергнет"
        )
        assert section in _load_config_keys()

    def test_namespace_is_consumed_by_resolver(self):
        """``gateway.agent`` не протекает в SETTINGS."""
        assert "agent" not in _load_config_keys()["gateway"]

    def test_project_version_matches_module_reader(self):
        """``lib.utils.project_version`` читает ту же секцию через тот же
        lift — второго пути к значению быть не должно."""
        from lib.utils.project_version import project_version

        assert _load_config_keys()["project"]["version"] == project_version()


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
