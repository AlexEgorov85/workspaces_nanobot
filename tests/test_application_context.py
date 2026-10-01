"""Тесты ApplicationContext — с обильным мокингом nanobot/psycopg2/PGSessionManager."""

from __future__ import annotations

import os
import re
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def full_fake_modules(tmp_path):
    """Подменяем ВСЕ модули, от которых зависит ApplicationContext.create(role='gateway', )."""
    with patch.dict("sys.modules"):

        # nanobot.agent
        sol = types.ModuleType("nanobot")
        sol.agent = types.ModuleType("nanobot.agent")
        loop = types.ModuleType("nanobot.agent.loop")
        hook = types.ModuleType("nanobot.agent.hook")
        hook.AgentHook = type("AgentHook", (), {})
        hook.AgentHookContext = type("AgentHookContext", (), {})
        hook.AgentRunHookContext = type("AgentRunHookContext", (), {})
        # ``workspace/hooks/session_file_redirect_hook`` и ``lib/hooks/*``
        # импортируют имена из ``nanobot.agent``; нужны атрибуты в моке.
        sol.agent.AgentHook = hook.AgentHook
        sol.agent.AgentHookContext = hook.AgentHookContext
        sol.agent.AgentRunHookContext = hook.AgentRunHookContext
        agent_instance = MagicMock()
        loop.AgentLoop = MagicMock()
        loop.AgentLoop.from_config = MagicMock(return_value=agent_instance)
        # ``lib/core/agent_factory.py`` импортирует ещё и
        # ``nanobot.agent.tools.registry``. Фейк ``nanobot.agent`` — простой
        # ModuleType без подпакета ``tools``, поэтому подмодуль нужно
        # зарегистрировать явно, иначе импорт падает с «'nanobot.agent' is
        # not a package». Раньше это маскировалось тем, что реальный
        # ``nanobot.agent.tools`` уже лежал в ``sys.modules`` от других
        # тестов, и фейк проходил только в полном прогоне.
        tools_pkg = types.ModuleType("nanobot.agent.tools")
        tools_registry = types.ModuleType("nanobot.agent.tools.registry")
        tools_registry.ToolRegistry = MagicMock()
        sol.agent.tools = tools_pkg
        tools_pkg.registry = tools_registry
        sys.modules["nanobot"] = sol
        sys.modules["nanobot.agent"] = sol.agent
        sys.modules["nanobot.agent.loop"] = loop
        sys.modules["nanobot.agent.hook"] = hook
        sys.modules["nanobot.agent.tools"] = tools_pkg
        sys.modules["nanobot.agent.tools.registry"] = tools_registry

        # nanobot.bus
        sol.bus = types.ModuleType("nanobot.bus")
        bus = types.ModuleType("nanobot.bus.queue")
        bus.MessageBus = MagicMock()
        sys.modules["nanobot.bus"] = sol.bus
        sys.modules["nanobot.bus.queue"] = bus

        # nanobot.channels
        sol.channels = types.ModuleType("nanobot.channels")
        cm = types.ModuleType("nanobot.channels.manager")
        cm.ChannelManager = MagicMock()
        sys.modules["nanobot.channels"] = sol.channels
        sys.modules["nanobot.channels.manager"] = cm

        # nanobot.utils
        sol.utils = types.ModuleType("nanobot.utils")
        helpers = types.ModuleType("nanobot.utils.helpers")
        helpers.sync_workspace_templates = MagicMock()
        sys.modules["nanobot.utils"] = sol.utils
        sys.modules["nanobot.utils.helpers"] = helpers

        # nanobot.cli
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

        # nanobot.cron
        sol.cron = types.ModuleType("nanobot.cron")
        cron_svc = types.ModuleType("nanobot.cron.service")
        cron_svc.CronService = MagicMock()
        sys.modules["nanobot.cron"] = sol.cron
        sys.modules["nanobot.cron.service"] = cron_svc

        # nanobot.session
        sol.session = types.ModuleType("nanobot.session")
        sm = types.ModuleType("nanobot.session.manager")
        sm.SessionManager = MagicMock()
        sys.modules["nanobot.session"] = sol.session
        sys.modules["nanobot.session.manager"] = sm

        # config (SETTINGS)
        cfg_mod = types.ModuleType("config")
        settings = MagicMock()
        settings.gateway = MagicMock()
        settings.gateway.storage = "file"
        settings.gateway.persist_threshold = 0
        settings.gateway.llm_timeout = -1
        settings.gateway.exec_timeout = -1
        settings.channels = {"postgres": {"dsn": ""}, "redis": {"enabled": False}}
        settings.skills = MagicMock()
        settings.skills.audit_analyzer = MagicMock()
        settings.skills.audit_analyzer.get = MagicMock(return_value=False)
        settings.cli = {}
        settings.providers = MagicMock()
        cfg_mod.SETTINGS = settings
        cfg_mod._ACTIVE_PROFILE = "test"
        cfg_mod.ENV_REF_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

        # Профильно-разрешённый Resolver (нужен для ApplicationContext.create).
        # В тестах возвращает тот же `settings` — это эквивалентно
        # default-профилю без override.
        def _resolve_mode(profile=None):
            return profile or "test"

        cfg_mod._resolve_mode = _resolve_mode

        def _resolve_application_config(profile=None):
            return settings

        cfg_mod.resolve_application_config = _resolve_application_config

        class ConfigurationError(ValueError):
            pass

        cfg_mod.ConfigurationError = ConfigurationError
        # Запоминаем настоящий ``config`` и DSN в окружении до подмены
        # (восстанавливаются в teardown фикстуры).
        _prev_config_module = sys.modules.get("config")
        _prev_database_url = os.environ.get("DATABASE_URL")
        sys.modules["config"] = cfg_mod

        # workspace
        ws = str(Path(__file__).resolve().parent.parent / "workspace")
        if ws not in sys.path:
            sys.path.insert(0, ws)
        # Фреймворковые хуки (ToolAuditHook и др.) теперь живут в lib/hooks/
        # и импортируются как реальные модули — при фейковом nanobot.agent
        # (AgentHook/AgentHookContext выше) они импортируются успешно.
        # hook_loader.scan_and_register использует
        # importlib.util.spec_from_file_location, поэтому ему не нужны
        # top-level алиасы в sys.modules — он грузит модули по абсолютному пути.

        # session_file_store нужен для SessionStorageService; мокаем.
        sfr = types.ModuleType("session_file_store")
        sfr.SessionFileStore = MagicMock()
        sys.modules["session_file_store"] = sfr

        # lib.services
        for name in [
            "lib.session.pg_session_manager",
            "lib.channels.redis_channel",
            "lib.channels.postgres_channel",
        ]:
            m = types.ModuleType(name)
            sys.modules[name] = m

        # utils.db
        utils_mod = types.ModuleType("utils")
        utils_db = types.ModuleType("utils.db")
        utils_db.configure = MagicMock()
        utils_mod.db = utils_db
        sys.modules["utils"] = utils_mod
        sys.modules["utils.db"] = utils_db

        # utils.media — db_logging_bus делает ``from utils.media import
        # serialize``; без мока «utils» — голый ModuleType без __path__
        # (не пакет), и импорт падает «utils is not a package».
        utils_media = types.ModuleType("utils.media")
        utils_media.serialize = MagicMock(return_value=None)
        utils_mod.media = utils_media
        sys.modules["utils.media"] = utils_media

        # utils.session_file_store
        sfs = types.ModuleType("utils.session_file_store")
        sfs.SessionFileStore = MagicMock()
        sfs.prepare_content = MagicMock()
        sys.modules["utils.session_file_store"] = sfs

        yield {
            "settings": settings,
            "agent_instance": agent_instance,
            "config": runtime_config,
        }

        # Cleanup: вернуть настоящий модуль ``config``. Фикстура подменяет
        # его фейком в ``sys.modules``; без восстановления тесты, идущие
        # после (например, ``test_config.py``), читают фейковый ``SETTINGS``
        # и падают в зависимости от порядка запуска.
        if _prev_config_module is not None:
            sys.modules["config"] = _prev_config_module
        else:  # pragma: no cover
            sys.modules.pop("config", None)

        # ``SessionStorageService`` пишет resolved DSN в ``os.environ``
        # (это нужно, чтобы skill-subprocess унаследовал его). Фейковый DSN
        # не должен утекать в процесс pytest — иначе acceptance-тесты,
        # запускающие CLI в subprocess, наследуют его и падают.
        if _prev_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = _prev_database_url


class TestCreate:
    def test_creates_context_with_all_services(self, full_fake_modules):
        from lib.core.application_context import ApplicationContext

        script = Path(__file__).resolve().parent.parent
        ctx = ApplicationContext.create(role='gateway', 
            script_dir=script,
            workspace_dir=script / "workspace",
            enable_db_logging=False,
            enable_audit=False,
        )
        assert ctx.config is not None
        assert ctx.settings is not None
        assert ctx.bus is not None
        assert ctx.agent is not None
        assert ctx.hooks
        assert ctx.tool_audit_hook in ctx.hooks
        assert ctx.runtime_patcher is not None
        assert ctx.transcription_service is not None
        assert ctx.db_logging_service is None
        # Снимком агент не владеет (фаза 5, п. 5.8): полей cache_* на
        # контексте больше нет, и их отсутствие — часть контракта, а не
        # «ещё не загрузилось».
        for gone in ("cache_store", "cache_provider", "cache_loader",
                     "preload_service"):
            assert not hasattr(ctx, gone), f"поле {gone!r} должно быть снято"

    def test_storage_file_when_no_dsn(self, full_fake_modules):
        from lib.core.application_context import ApplicationContext

        script = Path(__file__).resolve().parent.parent
        ctx = ApplicationContext.create(role='gateway', 
            script_dir=script,
            workspace_dir=script / "workspace",
            enable_db_logging=False,
            enable_audit=False,
        )
        assert ctx.storage_mode == "file"

    def test_pool_config_applied_from_settings(self, full_fake_modules):
        from unittest.mock import MagicMock

        utils_db = sys.modules["utils.db"]
        utils_db.set_pool_config = MagicMock()
        full_fake_modules["settings"].channels = {
            "postgres": {
                "dsn": "",
                "pool": {"min_conn": 1, "max_conn": 3, "pool_timeout": 7.5},
            },
        }
        from lib.core.application_context import ApplicationContext

        script = Path(__file__).resolve().parent.parent
        ApplicationContext.create(role='gateway', 
            script_dir=script,
            workspace_dir=script / "workspace",
            enable_db_logging=False,
            enable_audit=False,
        )
        utils_db.set_pool_config.assert_called_once_with(
            {
                "min_conn": 1,
                "max_conn": 3,
                "pool_timeout": 7.5,
                "print_activity": False,
            }
        )

    def test_storage_override_postgres_without_dsn(self, full_fake_modules):
        from lib.core.application_context import ApplicationContext

        script = Path(__file__).resolve().parent.parent
        full_fake_modules["settings"].gateway.storage = "file"
        # Override, но DSN пуст → SessionStorageService должен упасть,
        # но ApplicationContext делает fallback на "file".
        ctx = ApplicationContext.create(role='gateway', 
            script_dir=script,
            workspace_dir=script / "workspace",
            enable_db_logging=False,
            enable_audit=False,
            storage_override="postgres",
        )
        assert ctx.storage_mode == "file"

    def test_enable_cron_creates_cron_service(self, full_fake_modules):
        from lib.core.application_context import ApplicationContext

        script = Path(__file__).resolve().parent.parent
        ctx = ApplicationContext.create(role='gateway', 
            script_dir=script,
            workspace_dir=script / "workspace",
            enable_db_logging=False,
            enable_audit=False,
            enable_cron=True,
        )
        # cron_service passed to AgentLoop
        kwargs = __import__("nanobot.agent.loop", fromlist=["AgentLoop"]).AgentLoop.from_config.call_args.kwargs
        assert "cron_service" in kwargs

    def test_auto_scan_hooks_includes_session_file_redirect(self, full_fake_modules):
        """Регрессия: SessionFileRedirectHook должен попадать в ctx.hooks
        через auto-scan workspace/hooks/*.py. До фикса он был только в CLI
        (cli_agent.py вызывал scan_and_register вручную), и в gateway
        не работал.
        """
        from lib.core.application_context import ApplicationContext

        script = Path(__file__).resolve().parent.parent
        ctx = ApplicationContext.create(role='gateway', 
            script_dir=script,
            workspace_dir=script / "workspace",
            enable_db_logging=False,
            enable_audit=False,
        )
        # Берём по имени класса — hook_loader использует
        # spec_from_file_location, и isinstance через фейковый
        # sys.modules["session_file_redirect_hook"] уже не работает.
        redirect_hooks = [h for h in ctx.hooks if type(h).__name__ == "SessionFileRedirectHook"]
        assert len(redirect_hooks) == 1, (
            f"Ожидался один SessionFileRedirectHook в ctx.hooks, "
            f"найдено: {len(redirect_hooks)}. hooks={[type(h).__name__ for h in ctx.hooks]}"
        )
        # И он должен быть ПЕРЕД tool_audit_hook (порядок критичен: редирект
        # params["path"] должен случиться до того, как ToolAudit сохранит снимок).
        idx_redirect = ctx.hooks.index(redirect_hooks[0])
        idx_audit = ctx.hooks.index(ctx.tool_audit_hook)
        assert idx_redirect < idx_audit, (
            "SessionFileRedirectHook должен идти раньше ToolAuditHook, "
            f"но порядок: {[type(h).__name__ for h in ctx.hooks]}"
        )

    def test_agent_created_once_with_merged_hooks(self, full_fake_modules):
        """AgentLoop.from_config вызывается РОВНО ОДИН раз, и в его
        hooks= уже лежат и плагины (SessionFileRedirectHook), и
        фреймворковый ToolAuditHook. Раньше агент создавался дважды
        (AgentFactory + пересборка после auto-scan) — двойной лог
        ``Registered N tools``.
        """
        from lib.core.application_context import ApplicationContext

        script = Path(__file__).resolve().parent.parent
        ApplicationContext.create(role='gateway', 
            script_dir=script,
            workspace_dir=script / "workspace",
            enable_db_logging=False,
            enable_audit=False,
        )
        from_config = sys.modules["nanobot.agent.loop"].AgentLoop.from_config
        assert from_config.call_count == 1, (
            "Ожидался 1 вызов AgentLoop.from_config (плагины известны заранее), "
            f"получено: {from_config.call_count}"
        )
        kwargs = from_config.call_args.kwargs
        names = [type(h).__name__ for h in kwargs["hooks"]]
        assert "SessionFileRedirectHook" in names, names
        assert "ToolAuditHook" in names, names
        assert names.index("SessionFileRedirectHook") < names.index("ToolAuditHook"), (
            "SessionFileRedirectHook должен идти раньше ToolAuditHook, "
            f"но порядок: {names}"
        )


class TestTableRegistryReset:
    """``ApplicationContext.create(role='gateway', )`` сбрасывает singleton
    ``table_registry`` в начале, чтобы при повторном создании context
    в одном процессе (например, в тестах) не утекали ресурсы
    от предыдущего context.

    Без фикса: после первой ``create()`` с skill "A" вторая ``create()``
    с skill "B" видела ресурсы обоих.
    """

    def test_create_resets_table_registry(self, full_fake_modules):
        from lib.core.application_context import ApplicationContext
        from lib.services.table_registry import (
            SkillRegistration,
            TableResource,
            table_registry,
        )

        table_registry.register(
            SkillRegistration(
                name="leftover_skill",
                resources=(TableResource(name="public.leftover"),),
            )
        )
        assert "leftover_skill" in table_registry.names()

        script = Path(__file__).resolve().parent.parent
        ApplicationContext.create(role='gateway', 
            script_dir=script,
            workspace_dir=script / "workspace",
            enable_db_logging=False,
            enable_audit=False,
        )

        assert "leftover_skill" not in table_registry.names(), (
            "ApplicationContext.create(role='gateway', ) должен сбрасывать TableRegistry "
            "в начале; остались ресурсы от предыдущего context"
        )


class TestResolvePublishPath:
    """``resolve_cache_path`` — **единый механизм** вычисления пути к
    ``cache.duckdb`` (используется gateway И CLI/skill).

    Главная инвариантa: даже **без** настройки ``project.json`` снимок
    ``cache.duckdb`` уходит на ЛОКАЛЬНУЮ ФС (``~/.cache/nanobot/duckdb``),
    а не на legacy-путь ``<workspace>/data_store/duckdb/`` — потому что
    последний на NFS приводит к падению ATTACH с ``"PID 0"``.

    Нет escape-hatch'ей, нет backwards-compat shim'ов: один механизм,
    одно поведение.
    """

    def test_default_uses_local_cache_under_home(self, tmp_path):
        """Без ``gateway.cache.*`` путь уходит на ``~/.cache/nanobot/duckdb``.

        Подменяем ``Path.home()`` через ``tmp_path``, чтобы тест был
        детерминирован и не зависел от реальной ``$HOME`` на CI.
        """
        from lib.services.cache_provider_impl import resolve_cache_path

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = resolve_cache_path(str(tmp_path / "workspace"), None)

        assert result == str(
            tmp_path / ".cache" / "nanobot" / "duckdb" / "cache.duckdb"
        ), result
        assert Path(result).parent.exists()

    def test_default_uses_local_cache_under_home_with_empty_cfg(self, tmp_path):
        """Пустой cache_cfg → то же поведение, что и None."""
        from lib.services.cache_provider_impl import resolve_cache_path

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = resolve_cache_path(str(tmp_path / "workspace"), {})

        assert result == str(
            tmp_path / ".cache" / "nanobot" / "duckdb" / "cache.duckdb"
        ), result

    def test_local_path_absolute(self, tmp_path):
        from lib.services.cache_provider_impl import resolve_cache_path

        custom = tmp_path / "my-cache"
        result = resolve_cache_path(
            str(tmp_path / "ws"), {"local_path": str(custom)}
        )
        assert result == str(custom / "cache.duckdb"), result
        assert Path(result).parent.exists()

    def test_local_path_relative_resolved_from_workspace(self, tmp_path):
        from lib.services.cache_provider_impl import resolve_cache_path

        ws = tmp_path / "ws"
        ws.mkdir()
        result = resolve_cache_path(
            str(ws), {"local_path": "subdir/duckdb"}
        )
        assert result == str(ws / "subdir" / "duckdb" / "cache.duckdb"), result

    def test_local_path_unwritable_raises(self, tmp_path):
        """Если ``local_path`` нельзя создать — громкая OSError, не silent fallback."""
        from lib.services.cache_provider_impl import resolve_cache_path

        # ``local_path`` указывает на невозможный путь (файл как родитель).
        impossible = tmp_path / "a_file_not_dir"
        impossible.write_text("x")
        with pytest.raises(OSError):
            resolve_cache_path(
                str(tmp_path / "ws"),
                {"local_path": str(impossible / "x")},
            )

    def test_unknown_keys_are_silently_ignored(self, tmp_path):
        """Любой неизвестный ключ в cache_cfg (типа ``use_workspace_path`` из старой версии) — игнорируется."""
        from lib.services.cache_provider_impl import resolve_cache_path

        # Старые user-конфиги могут содержать use_workspace_path / publish_to_workspace
        # — больше нет shim'ов, эти ключи молча игнорируются.
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = resolve_cache_path(
                str(tmp_path / "ws"),
                {
                    "use_workspace_path": True,  # legacy, должно быть проигнорировано
                    "publish_to_workspace": True,  # задел, не реализован
                    "embiggen": True,  # откровенный мусор
                },
            )
        assert result == str(
            tmp_path / ".cache" / "nanobot" / "duckdb" / "cache.duckdb"
        ), result


class TestSingleMechanism:
    """В проекте существует ровно ОДИН резолвер пути к снимку.

    Исторически тут проверялось, что gateway и ``build_cache_provider``
    возвращают один и тот же путь. После фазы 5 такой тест стал бы
    тавтологией — обе точки вызывали бы одну и ту же функцию, то есть
    сравнивать было бы нечего. Исходный баг был не в том, что пути
    разошлись, а в том, что резолвер **размножился**: skill-слой держал
    собственную копию и gateway писал в один файл, а читался другой.

    Поэтому страж смотрит на корень проблемы: определить второй резолвер
    негде. Проверяется статически по всему дереву агента, потому что
    копия может появиться в любом новом модуле.
    """

    #: Единственное допустимое место определения. Функция переехала сюда из
    #: ``application_context`` в фазе 5, потому что composition root больше
    #: не открывает снимок, а резолвер нужен фабрике провайдера и
    #: standalone-утилитам сборки индексов.
    HOME = "lib/services/cache_provider_impl.py"

    #: Имена, которые ищет страж. ``resolve_publish_path`` — историческое
    #: имя, ``resolve_snapshot_path`` — платформенное (``snapshot/store.py``).
    #: Все три в списке: вернуться может любое, и любое молча размножит
    #: резолвер, пока кто-то не посмотрит на импорт.
    KNOWN = ("resolve_cache_path", "resolve_publish_path", "resolve_snapshot_path")

    def test_only_one_cache_path_resolver_exists(self) -> None:
        import ast

        root = Path(__file__).resolve().parent.parent
        # ``.worktrees`` — параллельные чекауты того же репозитория, не
        # часть этого дерева (их код меняется в других ветках работы, и
        # считать его за «второй резолвер» нельзя).
        skip = {".git", ".worktrees", "__pycache__", "mcp-platform",
                "data_store", "node_modules"}
        found: list[str] = []
        for path in root.rglob("*.py"):
            rel = path.relative_to(root)
            if any(part in skip for part in rel.parts):
                continue
            # tombstone'ы (имя с подчёркиванием) не считаются реализацией
            if any(part.startswith("_") for part in rel.parts):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError, OSError):
                continue
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and node.name in self.KNOWN:
                    found.append(f"{rel.as_posix()}:{node.name}")

        assert len(found) == 1 and found[0] == f"{self.HOME}:resolve_cache_path", (
            "резолвер пути к снимку должен быть ровно один, с именем "
            f"resolve_cache_path, в {self.HOME}; найдено: {found} — вторая "
            "копия или переименование означает, что писать будут в один файл, "
            "а читать из другого"
        )

