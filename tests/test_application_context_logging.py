"""Runtime-тесты проброса ``logging.db.flush_interval_sec`` из
типизированной конфигурации (``LoggingDbSettings``) в ``DbLoggingService``.

Закрывает task 2.2 спеки ``improve-history-search-pagination-and-logging``:
acceptance-критерий —

> runtime-тест ``tests/test_application_context_logging.py
> ::test_db_logging_service_receives_flush_interval_from_settings``
> собирает ``ApplicationContext`` с ``SETTINGS["logging"]["db"]
> ["flush_interval_sec"] = 2.0`` и проверяет
> ``assert ctx.db_logging_service._flush_interval == 2.0``.

Подход:

  * собираем минимальный bootstrap ``ApplicationContext`` через
    фикстуру ``full_fake_modules`` (как в ``tests/test_application_context.py``),
    но разрешаем ``enable_db_logging=True`` и закрываем
    ``DbLoggingService._db_run`` так, чтобы worker-thread не пытался
    реально открыть psycopg2-соединение;
  * подменяем ``SETTINGS["logging"]["db"]["flush_interval_sec"]`` через
    mock-объект, передаваемый в ``config.SETTINGS``;
  * проверяем, что после ``ApplicationContext.create`` значение попало
    в ``ctx.db_logging_service._flush_interval`` (атрибут сервиса).
"""

from __future__ import annotations

import re
import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def minimal_fake_modules(tmp_path):
    """Минимальный bootstrap для ``ApplicationContext.create`` с
    ``enable_db_logging=True``. Подменяем зависимости от nanobot /
    psycopg2, чтобы не требовать реальной инфраструктуры.
    """
    with patch.dict("sys.modules"):

        sol = types.ModuleType("nanobot")
        sol.agent = types.ModuleType("nanobot.agent")
        loop = types.ModuleType("nanobot.agent.loop")
        hook = types.ModuleType("nanobot.agent.hook")
        hook.AgentHook = type("AgentHook", (), {})
        hook.AgentHookContext = type("AgentHookContext", (), {})
        hook.AgentRunHookContext = type("AgentRunHookContext", (), {})
        sol.agent.AgentHook = hook.AgentHook
        sol.agent.AgentHookContext = hook.AgentHookContext
        sol.agent.AgentRunHookContext = hook.AgentRunHookContext
        agent_instance = MagicMock()
        loop.AgentLoop = MagicMock()
        loop.AgentLoop.from_config = MagicMock(return_value=agent_instance)
        sys.modules["nanobot"] = sol
        sys.modules["nanobot.agent"] = sol.agent
        sys.modules["nanobot.agent.loop"] = loop
        sys.modules["nanobot.agent.hook"] = hook

        sol.bus = types.ModuleType("nanobot.bus")
        bus = types.ModuleType("nanobot.bus.queue")
        bus.MessageBus = MagicMock()
        sys.modules["nanobot.bus"] = sol.bus
        sys.modules["nanobot.bus.queue"] = bus

        sol.channels = types.ModuleType("nanobot.channels")
        cm = types.ModuleType("nanobot.channels.manager")
        cm.ChannelManager = MagicMock()
        sys.modules["nanobot.channels"] = sol.channels
        sys.modules["nanobot.channels.manager"] = cm

        sol.utils = types.ModuleType("nanobot.utils")
        helpers = types.ModuleType("nanobot.utils.helpers")
        helpers.sync_workspace_templates = MagicMock()
        sys.modules["nanobot.utils"] = sol.utils
        sys.modules["nanobot.utils.helpers"] = helpers

        sol.cli = types.ModuleType("nanobot.cli")
        commands = types.ModuleType("nanobot.cli.commands")
        runtime_config = MagicMock()
        runtime_config.workspace_path = tmp_path
        runtime_config.providers.openai.api_key = None
        runtime_config.providers.groq.api_key = None
        runtime_config.providers.openai.api_base = None
        runtime_config.providers.groq.api_base = None
        runtime_config.channels.send_progress = True
        runtime_config.channels.send_tool_hints = False
        runtime_config.channels.show_reasoning = True
        runtime_config.channels.transcription_provider = "groq"
        runtime_config.channels.transcription_language = None
        runtime_config.agents.defaults.max_tool_iterations = 200
        runtime_config.tools.exec.timeout = 60
        commands._load_runtime_config = MagicMock(return_value=runtime_config)
        sys.modules["nanobot.cli"] = sol.cli
        sys.modules["nanobot.cli.commands"] = commands

        sol.cron = types.ModuleType("nanobot.cron")
        cron_svc = types.ModuleType("nanobot.cron.service")
        cron_svc.CronService = MagicMock()
        sys.modules["nanobot.cron"] = sol.cron
        sys.modules["nanobot.cron.service"] = cron_svc

        sol.session = types.ModuleType("nanobot.session")
        sm = types.ModuleType("nanobot.session.manager")
        sm.SessionManager = MagicMock()
        sys.modules["nanobot.session"] = sol.session
        sys.modules["nanobot.session.manager"] = sm

        cfg_mod = types.ModuleType("config")
        # ``SETTINGS`` — обычный dict-like объект (``LazySettings``
        # читается через ``__getitem__``/``get``). MagicMock здесь
        # провалиден, потому что ``SETTINGS[\"profile\"]`` возвращает
        # ``MagicMock`` вместо строки — ApplicationContext падает с
        # fail-fast ConfigurationError. Делаем простой класс с
        # dict-семантикой.
        class _FakeSettings(dict):
            def __getattr__(self, name):
                try:
                    return self[name]
                except KeyError:
                    raise AttributeError(name)

        settings = _FakeSettings()
        settings["profile"] = "test"
        settings["gateway"] = {
            "storage": "file",
            "persist_threshold": 0,
            "llm_timeout": 60,
            "exec_timeout": 60,
        }
        # DSN непустой → ``_make_db_logging`` создаст ``DbLoggingService``.
        settings["channels"] = {
            "postgres": {"dsn": "postgresql://test"},
            "redis": {"enabled": False},
        }
        settings["skills"] = {
            "audit_analyzer": {"enabled": False},
        }
        settings["cli"] = {}
        settings["providers"] = {}
        # Дефолт-секция ``logging.db`` — bootstrap подменит для конкретного теста.
        settings["logging"] = {
            "db": {
                "enabled": True,
                "table_name": "agent_gateway_logs",
                "question_runs_table": "agent_question_runs",
                "schema": "public",
                "flush_interval_sec": 5.0,
            },
        }
        cfg_mod.SETTINGS = settings
        cfg_mod._ACTIVE_PROFILE = "test"
        cfg_mod.ENV_REF_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

        def _resolve_mode(profile=None):
            return profile or "test"

        cfg_mod._resolve_mode = _resolve_mode

        def _resolve_application_config(profile=None):
            return settings

        cfg_mod.resolve_application_config = _resolve_application_config

        class ConfigurationError(ValueError):
            pass

        cfg_mod.ConfigurationError = ConfigurationError
        sys.modules["config"] = cfg_mod

        # ``lib.core.project_settings`` импортирует ``ConfigurationError``
        # из ``config`` на module-level. Если тесты выше (``test_config_keys.py``)
        # уже импортировали реальный ``lib.core.project_settings``, он
        # держит ссылку на класс из реального модуля. Подменяем её
        # на фейк, чтобы ``from config import ConfigurationError`` и
        # ``raise ConfigurationError(...)`` внутри ``validate_project_settings``
        # использовали один и тот же класс.
        _real_ConfigurationError = None
        try:
            import lib.core.project_settings as _ps
            _real_ConfigurationError = _ps.ConfigurationError
            _ps.ConfigurationError = ConfigurationError
        except Exception:
            pass

        ws = str(Path(__file__).resolve().parent.parent / "workspace")
        if ws not in sys.path:
            sys.path.insert(0, ws)

        sfr = types.ModuleType("session_file_store")
        sfr.SessionFileStore = MagicMock()
        sys.modules["session_file_store"] = sfr

        for name in [
            "lib.session.pg_session_manager",
            "lib.channels.redis_channel",
            "lib.channels.postgres_channel",
        ]:
            m = types.ModuleType(name)
            sys.modules[name] = m

        utils_mod = types.ModuleType("utils")
        utils_db = types.ModuleType("utils.db")
        utils_db.configure = MagicMock()
        # ``DbLoggingService._db_run`` зовёт ``utils.db.run(fn)`` —
        # мокаем no-op'ом, чтобы worker-цикл не открывал реальный psycopg2.
        utils_db.run = MagicMock(return_value=None)
        # Дополнительно: ``_make_sync_services`` и другие точки
        # могут обращаться к ``fetch`` / ``execute`` — мокаем.
        utils_db.fetch = MagicMock(return_value=[])
        utils_db.execute = MagicMock(return_value=None)
        utils_db.shutdown = MagicMock()
        utils_db.start = MagicMock()
        utils_mod.db = utils_db
        sys.modules["utils"] = utils_mod
        sys.modules["utils.db"] = utils_db

        utils_media = types.ModuleType("utils.media")
        utils_media.serialize = MagicMock(return_value=None)
        utils_mod.media = utils_media
        sys.modules["utils.media"] = utils_media

        sfs = types.ModuleType("utils.session_file_store")
        sfs.SessionFileStore = MagicMock()
        sfs.prepare_content = MagicMock()
        sys.modules["utils.session_file_store"] = sfs

        yield {
            "settings": settings,
            "agent_instance": agent_instance,
            "config": runtime_config,
            "_real_ConfigurationError": _real_ConfigurationError,
        }

        # Cleanup: вернуть дефолт ``flush_interval_sec`` после теста,
        # чтобы не загрязнять следующий (у ``_FakeSettings`` нет
        # фикстуры reset).
        settings["logging"]["db"]["flush_interval_sec"] = 5.0

        # Cleanup: вернуть ``ConfigurationError`` в модулях, которые
        # импортировали его из реального ``config`` на module-level
        # (например, ``lib.core.project_settings``). Без этого при
        # запуске после ``test_config_keys.py`` тест
        # ``test_out_of_range_value_rejected_by_pydantic`` падает: raise
        # использует класс из реального модуля, а ``pytest.raises``
        # ловит фейк (другой класс).
        if _real_ConfigurationError is not None:
            try:
                import lib.core.project_settings as _ps
                _ps.ConfigurationError = _real_ConfigurationError
            except Exception:
                pass


class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._row = ("1",)

    def execute(self, sql, params=None):
        # Allow _ensure_schema's existence-check to succeed.
        if "information_schema.tables" in (sql or ""):
            return
        # Suppress other SQL (purge etc.) — no-op.

    def fetchone(self):
        return self._row

    def fetchall(self):
        return []

    def close(self):
        pass


class _FakeConn:
    def cursor(self):
        return _FakeCursor(self)

    def close(self):
        pass

    @property
    def closed(self):
        return False


class TestFlushIntervalSecPropagation:
    """Flush interval из SETTINGS доходит до ``DbLoggingService``."""

    def test_db_logging_service_receives_flush_interval_from_settings(
        self, minimal_fake_modules,
    ):
        """Спека 2.2: ``SETTINGS[\"logging\"][\"db\"][\"flush_interval_sec\"] = 2.0``
        → ``ctx.db_logging_service._flush_interval == 2.0``.
        """
        from config import SETTINGS

        SETTINGS["logging"]["db"]["flush_interval_sec"] = 2.0

        # ``utils.db.run`` уже мокнут в фикстуре ``minimal_fake_modules``
        # (no-op возвращает ``None``), так что worker-цикл DbLoggingService
        # не откроет реальный psycopg2.
        from lib.core.application_context import ApplicationContext

        script = Path(__file__).resolve().parent.parent
        ctx = ApplicationContext.create(
            script_dir=script,
            workspace_dir=script / "workspace",
            enable_db_logging=True,
            enable_audit=False,
            profile="test",
        )

        try:
            assert ctx.db_logging_service is not None, (
                "_make_db_logging должен вернуть инстанс при непустом DSN"
            )
            assert ctx.db_logging_service._flush_interval == 2.0, (
                "flush_interval_sec из SETTINGS не дошёл до DbLoggingService"
            )
        finally:
            try:
                if ctx.db_logging_service is not None:
                    ctx.db_logging_service.stop(timeout_sec=0.5)
            except Exception:
                pass

    def test_db_logging_service_uses_default_when_absent(
        self, minimal_fake_modules,
    ):
        """При отсутствии ключа в SETTINGS ``flush_interval_sec``
        всё равно 5.0 — типизированная модель подставляет дефолт
        через ``_default_flush_interval_sec``.
        """
        from config import SETTINGS

        # Удаляем ключ, чтобы провалидировать путь подстановки дефолта.
        SETTINGS["logging"]["db"].pop("flush_interval_sec", None)

        from lib.core.application_context import ApplicationContext

        script = Path(__file__).resolve().parent.parent
        ctx = ApplicationContext.create(
            script_dir=script,
            workspace_dir=script / "workspace",
            enable_db_logging=True,
            enable_audit=False,
            profile="test",
        )

        try:
            assert ctx.db_logging_service is not None
            assert ctx.db_logging_service._flush_interval == 5.0, (
                "В отсутствие ключа дефолт 5.0 должен прийти из "
                "LoggingDbSettings._default_flush_interval_sec"
            )
        finally:
            try:
                ctx.db_logging_service.stop(timeout_sec=0.5)
            except Exception:
                pass

    def test_out_of_range_value_rejected_by_pydantic(
        self, minimal_fake_modules,
    ):
        """Значение вне диапазона ``0.5..60.0`` отвергается
        ``LoggingDbSettings`` до создания DbLoggingService.
        """
        from config import SETTINGS, ConfigurationError

        SETTINGS["logging"]["db"]["flush_interval_sec"] = 0.1

        from lib.core.application_context import ApplicationContext

        script = Path(__file__).resolve().parent.parent
        with pytest.raises(ConfigurationError) as exc_info:
            ApplicationContext.create(
                script_dir=script,
                workspace_dir=script / "workspace",
                enable_db_logging=True,
                enable_audit=False,
                profile="test",
            )
        # Сообщение должно явно указывать на ``flush_interval_sec``,
        # чтобы оператор понимал, какой ключ не прошёл валидацию.
        assert "flush_interval_sec" in str(exc_info.value), (
            f"ConfigurationError должен упоминать flush_interval_sec, "
            f"получено: {exc_info.value}"
        )
