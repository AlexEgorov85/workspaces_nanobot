"""ApplicationContext — единая точка создания и связывания сервисов.

Создаёт все общие сервисы (конфиг, БД-логирование, аудит-сервисы,
шина сообщений, хранилище сессий, агент) и публикует их атрибутами.
Точки входа (gateway.py / cli_agent.py) — тонкие оркестраторы,
использующие ``ctx`` для запуска/остановки.

Все тяжёлые зависимости (nanobot, psycopg2) импортируются лениво —
модуль безопасно импортировать даже в тестовых средах.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ApplicationContext:
    """Контекст приложения: конфиг + все сервисы."""

    # Пути
    script_dir: Path
    workspace_dir: Path

    # Конфигурация
    config: Any
    settings: Any
    project_settings: Any = None

    # Шина
    bus: Any

    # Агент и его состояние
    agent: Any
    tool_audit_hook: Any
    hooks: list

    # Хранилище сессий
    session_manager: Any
    storage_mode: str

    # Сервисы (опциональные)
    db_logging_service: Any | None = None
    sync_service: Any | None = None
    cache_store: Any | None = None

    # Помощники
    config_service: Any = None
    runtime_patcher: Any = None
    runtime_health: Any = None
    runtime_readiness: Any = None
    transcription_service: Any = None
    session_storage_service: Any = None
    subprocess_manager: Any = None
    preload_service: Any = None

    # Per-turn hook factories (для DatabaseLoggingHook и т.п.), которые
    # ``AgentFactory`` собрала из конфигурации и передала в
    # ``AgentLoop.from_config(hook_factories=...)``. Нужны для
    # пересборки ``AgentLoop`` после auto-scan проектных хуков.
    hook_factories: list = None  # type: ignore[assignment]

    # Lifecycle
    _started: bool = False
    _shutdown: Any | None = None  # ShutdownCoordinator

    @classmethod
    def create(
        cls,
        script_dir: Path,
        workspace_dir: Path,
        *,
        enable_db_logging: bool = True,
        enable_audit: bool = True,
        enable_cron: bool = False,
        storage_override: str | None = None,
        session_override: str | None = None,
        print_llm_calls: bool = False,
        profile: str | None = None,
    ) -> ApplicationContext:
        """Собрать контекст приложения.

        Args:
            script_dir: корень проекта (где лежит config.json).
            workspace_dir: корень workspace.
            enable_db_logging: инициализировать DbLoggingService.
            enable_audit: инициализировать PgDuckDbSyncService + DuckDbCacheStore.
            enable_cron: подключить CronService (CLI).
            storage_override: режим хранилища из CLI (auto/postgres/file).
            session_override: имя сессии (CLI).
            print_llm_calls: выводить в терминал токены LLM-итераций
                (включается только в CLI-REPL через DatabaseLoggingHook).
            profile: активный профиль конфигурации (``"prod"`` / ``"test"``).
                Должен совпадать с уже инициализированным через
                ``config._initialize_settings(profile)`` из application
                entrypoint. ``None`` — fallback на ``config.SETTINGS["profile"]``
                (если ленивый proxy уже инициализирован entrypoint'ом).

        Raises:
            ConfigurationError: если ``_initialize_settings(profile)`` ещё не
                выполнен (proxy остался uninitialized).
        """
        ctx = cls()
        ctx.script_dir = Path(script_dir)
        ctx.workspace_dir = Path(workspace_dir)
        ctx.profile = profile

        # Сбросить ``TableRegistry`` — это singleton, и при повторном
        # ``create()`` в одном процессе (тесты, streamlit-reload, gateway
        # перезапуск конфига) старые регистрации остались бы и смешались
        # с новыми. ``_make_sync_services`` и ``_auto_register_skills``
        # ниже заполнят реестр заново.
        from lib.services.table_registry import table_registry
        table_registry.clear()

        # 1. ConfigService + загрузка конфига.
        #
        # Один источник истины — глобальный ``SETTINGS`` (``_LazySettings``),
        # уже построенный через ``_initialize_settings(profile)`` из application
        # entrypoint. ``ApplicationContext`` **не** делает повторный
        # resolve/resolver; это просто читает опубликованный ``SETTINGS``
        # и оборачивает его в ``ConfigService``.
        #
        # Если кто-то вызвал ``ApplicationContext.create`` без
        # предварительного entrypoint init — proxy поднимет
        # ``ConfigurationError`` через ``__getitem__`` ниже, и тест/
        # caller увидит ту же ошибку, что и entrypoint нарушение
        # lifecycle (fail-fast).
        import config as _config
        ctx_settings = _config.SETTINGS
        # Touching ``["profile"]`` материализует ConfigurationError на
        # uninitialized proxy, но не делает duplicated work в happy-path.
        resolved_profile = ctx_settings["profile"]
        if profile is not None and profile != resolved_profile:
            # entrypoint передал ``profile``, отличный от уже
            # инициализированного. Раньше это могло быть env → CLI;
            # теперь это явное нарушение lifecycle — fail-fast.
            from config import ConfigurationError
            raise ConfigurationError(
                f"ApplicationContext.create(profile={profile!r}) called "
                f"but SETTINGS already initialized for profile={resolved_profile!r}. "
                "Application entrypoint must pass the same --profile value as "
                "was passed to config._initialize_settings()."
            )
        ctx.profile = resolved_profile

        ctx.config_service = _make_config_service(
            ctx.script_dir, ctx.workspace_dir, settings_override=ctx_settings
        )
        ctx.config = ctx.config_service.load()
        ctx.settings = ctx_settings

        # 1a. Fail-fast валидация проектных настроек (типы/значения).
        from lib.core.project_settings import validate_project_settings

        ctx.project_settings = validate_project_settings(ctx.settings)

        # 2. Таймауты
        ctx.config_service.apply_timeouts(
            ctx.config,
            llm_timeout=ctx.config_service.get_int("gateway", "llm_timeout", default=300),
            exec_timeout=ctx.config_service.get_int("gateway", "exec_timeout", default=60),
            max_iterations=ctx.config_service.get_int("cli", "max_iterations", default=200),
        )

        # 3. SessionStorageService
        from lib.services.session_storage import SessionStorageService

        # Параметр session_manager_json удалён: теперь override из
        # session_manager.json применяется централизованно в
        # ConfigurationResolver (см. config.resolve_application_config,
        # шаг 2 порядка merge).
        ctx.session_storage_service = SessionStorageService()
        pg_section = ctx.config_service.settings_section("channels").get(
            "postgres", {}
        )

        # Конфигурация общего пула соединений (channels.postgres.pool) —
        # применяется ДО создания сервисов, чтобы воркеры пула использовали
        # заданные min_conn/max_conn/pool_timeout и т.п.
        if isinstance(pg_section, dict) and isinstance(pg_section.get("pool"), dict):
            _db_print = bool(
                ctx.config_service.settings_section("gateway").get(
                    "print_db_activity", False
                )
            )
            _configure_db_pool(
                pg_section.get("pool", {}), print_activity=_db_print
            )

        try:
            storage_mode, session_manager = ctx.session_storage_service.create(
                ctx.config,
                storage=storage_override
                or ctx.config_service.get_str("gateway", "storage", default="auto"),
                pg=pg_section,
                configure_db=True,
                return_file_manager=not enable_cron,
            )
        except Exception as exc:
            logger.warning("SessionStorageService failed: %s", exc)
            storage_mode, session_manager = "file", None

        ctx.storage_mode = storage_mode
        ctx.session_manager = session_manager

        # 4. DbLoggingService
        if enable_db_logging:
            ctx.db_logging_service = _make_db_logging(ctx)

        # 5. PgDuckDbSyncService + DuckDbCacheStore
        if enable_audit:
            _auto_register_skills(ctx)
            _register_infra_resources(ctx)
            ctx.sync_service, ctx.cache_store = _make_sync_services(ctx)

        # 6. BusFactory + AgentFactory
        from lib.core.bus_factory import BusFactory
        from lib.services.db_logging_bus import (
            make_inbound_logger,
            make_outbound_logger,
        )

        inbound_logger = None
        outbound_logger = None
        # Идентификатор агента — для колонки agent_id в логах
        # (подагенты получают parent_agent_id = этот id).
        agent_id = _resolve_agent_id(ctx.config)
        if ctx.db_logging_service is not None:
            inbound_logger = make_inbound_logger(ctx.db_logging_service, agent_id)
            outbound_logger = make_outbound_logger(ctx.db_logging_service, agent_id)

        bus_factory = BusFactory(
            inbound_logger=inbound_logger,
            outbound_logger=outbound_logger,
        )
        ctx.bus = bus_factory.create()

        from lib.core.agent_factory import AgentFactory

        cron_service = None
        if enable_cron:
            cron_service = _make_cron_service(ctx.config)

        # 6a. Auto-scan проектных хуков из ``workspace/hooks/*.py`` (ПЛАГИНЫ).
        # Фреймворковые хуки (``ToolAudit``, ``DatabaseLogging`` — живут
        # в ``lib/hooks/``) провязывает ``AgentFactory``; плагины
        # (например, ``SessionFileRedirectHook``, ``RecentFilesHook``)
        # сканируются здесь единым механизмом для всех точек входа
        # (gateway, cli_agent, streamlit). Сканирование идёт ДО создания
        # ``AgentLoop``, чтобы агент создавался ровно один раз с полным
        # списком хуков (иначе был двойной лог ``Registered N tools``).
        # Если папки ``hooks/`` нет или она пуста (например, в юнит-тестах) —
        # пропускаем без ошибки.
        project_hooks: list = []
        try:
            from lib.cli.hook_loader import scan_and_register

            project_hooks = scan_and_register(
                ctx.workspace_dir / "hooks", ctx.workspace_dir
            )
        except Exception as exc:
            logger.warning("hook_loader.scan_and_register failed: %s", exc)

        agent_factory = AgentFactory()
        ctx.agent, ctx.hooks, ctx.hook_factories = agent_factory.create(
            ctx.config,
            ctx.bus,
            session_manager=ctx.session_manager,
            cron_service=cron_service,
            db_logging_service=ctx.db_logging_service,
            agent_id=agent_id,
            project_hooks=project_hooks or None,
            print_llm_calls=print_llm_calls,
        )

        # ToolAuditHook — фреймворковый, входит в ``ctx.hooks`` последним
        # (после плагинов). Нужен RuntimePatcher'у для внедрения аудита.
        ctx.tool_audit_hook = next(
            (h for h in ctx.hooks if type(h).__name__ == "ToolAuditHook"),
            None,
        )

        # Единственная точка вывода полного списка подключённых хуков:
        # плагины + фреймворковые (ToolAuditHook) + per-turn factories
        # (DatabaseLoggingHook). Печатается один раз — двойных сообщений
        # нет (сканер успех молчит).
        _log_connected_hooks(ctx)

        # 6b. RuntimeHealth / RuntimeReadiness — operational status.
        # Health: пульс процесса (liveness). Readiness: PG/duckdb/vector.
        # Регистрируется ПОСЛЕ хуков и bus_factory, потому что readiness
        # проверяет состояние уже созданных сервисов.
        from lib.services.runtime_health import (
            RuntimeHealth,
            RuntimeReadiness,
            ComponentStatus,
        )

        ctx.runtime_health = RuntimeHealth()
        ctx.runtime_readiness = RuntimeReadiness()
        _register_readiness_checks(ctx)

        # 7. RuntimePatcher
        from lib.services.runtime_patcher import RuntimePatcher

        # Найти RecentFilesHook среди зарегистрированных хуков (если
        # был подключён через auto-scan). Используется RuntimePatcher'ом
        # для auto-attach созданных файлов в ``OutboundMessage.media``.
        recent_files_hook = None
        for h in ctx.hooks:
            cls_name = type(h).__name__
            if cls_name == "RecentFilesHook":
                recent_files_hook = h
                break

        ctx.runtime_patcher = RuntimePatcher()
        patch_report = ctx.runtime_patcher.apply_all(
            ctx.config, ctx.settings, ctx.workspace_dir,
            ctx.agent, ctx.tool_audit_hook,
            recent_files_hook=recent_files_hook,
            db_logging_service=ctx.db_logging_service,
            session_manager=ctx.session_manager,
            cache_store=ctx.cache_store,
        )
        ctx.runtime_patch_report = patch_report
        logger.info(
            "Runtime patches:\n%s",
            patch_report.render(specs=RuntimePatcher.patch_specs()),
        )
        if patch_report.failed:
            logger.warning(
                "%d runtime patch(es) failed: %s",
                len(patch_report.failed),
                [name for name, _ in patch_report.failed],
            )

        # 8. Помощники
        ctx.transcription_service = _make_transcription(ctx.config)
        ctx.preload_service = _make_preload(ctx.settings, ctx.db_logging_service)

        ctx.runtime_health.mark_started()
        return ctx

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Запустить фоновые сервисы (БД-логирование, аудит)."""
        if self._started:
            return
        from lib.lifecycle.shutdown_coordinator import ShutdownCoordinator

        self._shutdown = ShutdownCoordinator()
        if self.runtime_health is not None:
            self.runtime_health.mark_started()

        # Переопределения системных шаблонов nanobot из workspace/overrides/
        # (например, русская инструкция Consolidator). Безопасно-идемпотентно;
        # при отсутствии каталога молча пропускается.
        try:
            from lib.services.consolidator_locale import apply_template_overrides

            if apply_template_overrides():
                logger.info("Template overrides active: workspace/overrides")
        except Exception as exc:
            logger.warning("Template overrides not applied: %s", exc)

        # Стартуем общий пул соединений (воркеры подключаются лениво при
        # первой задаче, но пул уже создан и подхватил pool-конфиг).
        _start_db_pool()

        if self.db_logging_service is not None:
            self.db_logging_service.start()
            self._shutdown.register("db_logging_service", self.db_logging_service)

        if self.sync_service is not None:
            try:
                self.sync_service.start(initial_load=True)
                self._shutdown.register("sync_service", self.sync_service)
            except Exception as exc:
                logger.warning("PgDuckDbSyncService not started: %s", exc)

        self._started = True

        # Финальный readiness snapshot для startup-лога.
        if self.runtime_readiness is not None:
            report = self.runtime_readiness.check()
            logger.info(
                "Readiness: %s | components=%s",
                report.status,
                ", ".join(
                    f"{c.name}={'UP' if c.status == 'UP' else 'DOWN'}"
                    for c in report.components
                ),
            )
            if report.status == "NOT_READY":
                logger.warning(
                    "Required dependencies are down; gateway starts in NOT_READY state"
                )

    def stop(self) -> None:
        """Корректно остановить все фоновые сервисы."""
        if not self._started:
            return
        if self._shutdown is not None:
            self._shutdown.shutdown_all()
        # После остановки сервисов закрываем общий пул соединений.
        _stop_db_pool()
        if self.runtime_health is not None:
            self.runtime_health.mark_stopped()
        self._started = False


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _log_connected_hooks(ctx: ApplicationContext) -> None:
    """Однократно вывести полный список подключённых хуков.

    Единая точка вывода: плагины ``workspace/hooks/`` + фреймворковые
    хуки (``ToolAuditHook`` — в ``ctx.hooks``) + per-turn hook factories
    (``DatabaseLoggingHook`` — в ``ctx.hook_factories``). Вызывается один
    раз после создания ``AgentLoop``, поэтому двойных сообщений нет.
    """
    names = [type(h).__name__ for h in ctx.hooks]
    if ctx.hook_factories:
        names.append(f"{len(ctx.hook_factories)} hook factory (per-turn)")
    label = ", ".join(names) or "(no hooks connected)"
    try:
        from rich.console import Console

        Console().print(f"[green]\u2713[/green] Hooks connected: {label}")
    except Exception:
        # Старые Windows-консоли (cp1251) не умеют ✓ (U+2713) — выводим
        # тот же список обычным print, чтобы информация не пропадала.
        print(f"Hooks connected: {label}")


def _register_readiness_checks(ctx: ApplicationContext) -> None:
    """Зарегистрировать проверки зависимостей для RuntimeReadiness.

    Профили:
      * ``postgres`` — required. Если БД доступна — UP. Если storage
        fallback на file-mode — DOWN (НЕ NOT_READY, потому что система
        работает, но в degraded mode).
      * ``duckdb_cache`` — required. Если cache_store готов — UP.
      * ``vector_search`` — optional. Если cache_store не имеет
        FAISS-индексов — DOWN, но это не блокирует READY.

    Все проверки идемпотентны и быстрые (< 1 сек каждая).
    """
    from lib.services.runtime_health import (
        ComponentStatus,
        compute_overall_status,
    )

    def check_postgres() -> ComponentStatus | None:
        sm = getattr(ctx, "session_manager", None)
        if sm is None:
            return ComponentStatus(
                name="postgres", required=True, status="DOWN",
                detail="no session_manager",
            )
        # Проверяем тип storage. PGSessionManager — есть PG; file fallback — DOWN.
        cls = type(sm).__name__
        if not ("PG" in cls or "Postgres" in cls):
            return ComponentStatus(
                name="postgres", required=True, status="DOWN",
                detail=f"storage degraded to {cls}",
            )
        # Реальный ping через пул соединений, а не только тип storage-класса.
        # Используем прямой submit с таймаутом, чтобы при недоступном PG
        # readiness-чек не зависал на внутренних backoff-ретраях воркера
        # (psycopg2.connect + connect_max_retries могут занять десятки секунд,
        # а ``fetch().get()`` блокирует навсегда).
        try:
            import sys
            from pathlib import Path

            _ws = Path(__file__).resolve().parents[2] / "workspace"
            if str(_ws) not in sys.path:
                sys.path.insert(0, str(_ws))
            from utils.db import _get_manager, _Job

            def _ping(conn: Any) -> Any:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    return cur.fetchone()

            job = _get_manager()._submit(_Job(_ping, tag="readiness.postgres"))
            result = job.result.get(timeout=2.0)
            if result is None:
                return ComponentStatus(
                    name="postgres", required=True, status="DOWN",
                    detail="pg ping timeout (2s)",
                )
            return None  # UP без detail
        except Exception as exc:
            return ComponentStatus(
                name="postgres", required=True, status="DOWN",
                detail=f"pg ping failed: {type(exc).__name__}: {exc}",
            )

    def check_duckdb_cache() -> ComponentStatus | None:
        cs = getattr(ctx, "cache_store", None)
        if cs is None:
            return ComponentStatus(
                name="duckdb_cache", required=True, status="DOWN",
                detail="no cache_store (sync disabled or no resources)",
            )
        is_ready = getattr(cs, "is_ready", None)
        if callable(is_ready):
            try:
                if is_ready():
                    return None
                return ComponentStatus(
                    name="duckdb_cache", required=True, status="DOWN",
                    detail="cache_store.is_ready()=False",
                )
            except Exception as exc:
                return ComponentStatus(
                    name="duckdb_cache", required=True, status="DOWN",
                    detail=f"is_ready failed: {type(exc).__name__}: {exc}",
                )
        return None  # нет is_ready — считаем UP

    def check_vector_search() -> ComponentStatus | None:
        cs = getattr(ctx, "cache_store", None)
        if cs is None:
            return ComponentStatus(
                name="vector_search", required=False, status="DOWN",
                detail="no cache_store; vector_search tool won't work",
            )
        if not hasattr(cs, "search_vector"):
            return ComponentStatus(
                name="vector_search", required=False, status="DOWN",
                detail="cache_store has no search_vector method",
            )
        return None

    ctx.runtime_readiness.register("postgres", check_postgres, required=True)
    ctx.runtime_readiness.register("duckdb_cache", check_duckdb_cache, required=True)
    ctx.runtime_readiness.register("vector_search", check_vector_search, required=False)


def _make_config_service(
    script_dir: Path,
    workspace_dir: Path,
    *,
    settings_override: Any | None = None,
) -> Any:
    """Создать ``ConfigService``, привязанный к корню проекта.

    Использует lazy-import, чтобы не зависеть от ``config.py`` на
    старте (если config битый, ошибка проявится в ``.load()``).

    ``settings_override`` — готовый resolved SETTINGS (от Resolver).
    Если не передан — ConfigService возвращает глобальный
    ``config.SETTINGS``. Оба пути Resolver-разрешённые.
    """
    from lib.services.config_service import ConfigService

    return ConfigService(
        script_dir=script_dir,
        workspace_dir=workspace_dir,
        settings_override=settings_override,
    )


def _resolve_agent_id(config: Any) -> str:
    """Получить идентификатор агента из конфигурации (или ``"main"``)."""
    try:
        agents = getattr(config, "agents", None)
        if agents is not None:
            defaults = getattr(agents, "defaults", None) or {}
            name = defaults.get("name") if isinstance(defaults, dict) else getattr(defaults, "name", None)
            if name:
                return str(name)
    except Exception:
        pass
    return "main"


def _make_db_logging(ctx: ApplicationContext) -> Any | None:
    """Собрать ``DbLoggingService`` из секции ``logging.db`` в settings.

    Возвращает ``None`` если:
      * ``logging.db.enabled != True`` (явно отключено);
      * нет DSN в ``channels.postgres`` (некуда писать);
      * psycopg2 не импортируется (битое окружение).

    DSN берётся из ``channels.postgres.dsn`` (тот же, что для
    PGSessionManager и PostgresChannel). Резервной записи в JSONL-файл
    нет: при недоступности БД события выбрасываются.
    """
    try:
        from lib.services.config_service import ConfigService  # noqa: F401
        from lib.services.db_logging_service import DbLoggingService
    except Exception as exc:
        logger.warning("DbLoggingService unavailable: %s", exc)
        return None

    log_cfg = ctx.config_service.settings_section("logging")
    db_cfg = log_cfg.get("db", {}) if isinstance(log_cfg, dict) else {}
    if not db_cfg.get("enabled", False):
        return None

    pg = ctx.config_service.settings_section("channels").get("postgres", {})
    dsn = ""
    if isinstance(pg, dict):
        dsn = pg.get("dsn", "") or ""
    if not dsn:
        return None

    table_name = db_cfg.get("table_name", "")
    question_runs_table = db_cfg.get("question_runs_table", "")
    if not table_name or not question_runs_table:
        from config import ConfigurationError

        raise ConfigurationError(
            "конфиг logging.db (table_name и question_runs_table) "
            "обязательны для DbLoggingService (нет авто-дефолтов в коде). "
            f"table_name={table_name!r}, question_runs_table={question_runs_table!r}"
        )

    # ``logging.db.flush_interval_sec`` (см. ``LoggingDbSettings``):
    # диапазон ``0.5 ≤ value ≤ 60.0`` сек, дефолт ``5.0``. Значение
    # уже валидировано pydantic на старте ``ApplicationContext.create``
    # через ``validate_project_settings`` (шаг 1a), и ``LoggingDbSettings.
    # _default_flush_interval_sec`` подменяет ``None`` на ``5.0``.
    # Здесь читаем уже валидный ``float`` из типизированной проекции —
    # единственный путь разрешения конфигурации (см. ``docs/PROFILES.md``
    # § «Configuration resolver chain»); ``project_settings`` всегда
    # инициализирован к моменту этого шага (fail-fast на шаге 1a).
    flush_interval_sec = (
        ctx.project_settings.logging.db.flush_interval_sec
    )

    return DbLoggingService(
        dsn=dsn,
        table_name=table_name,
        question_runs_table=question_runs_table,
        schema=db_cfg.get("schema", "public"),
        dialect=db_cfg.get("dialect", "postgres"),
        flush_interval_sec=flush_interval_sec,
        batch_size=int(db_cfg.get("batch_size", 100)),
        queue_maxsize=int(db_cfg.get("queue_maxsize", 10000)),
        min_level=db_cfg.get("min_level", "INFO"),
        connect_backoff_sec=float(db_cfg.get("connect_backoff_sec", 1.0)),
        connect_backoff_max_sec=float(db_cfg.get("connect_backoff_max_sec", 60.0)),
        summary_max_chars=int(db_cfg.get("summary_max_chars", 200)),
        retention_days=int(db_cfg.get("retention_days", 90)),
        purge_interval_sec=float(db_cfg.get("purge_interval_sec", 3600.0)),
    )


def _default_local_cache_dir() -> "Path":
    """Безопасный default для runtime-кеша: ``~/.cache/nanobot/duckdb``.

    DuckDB ATTACH берёт эксклюзивный flock, который NFS не отдаёт
    (``"Conflicting lock is held in PID 0"`` на свежем файле после ``rm``).
    Поэтому default для snapshot'а — **локальный** кеш-пользовательский
    каталог (POSIX ``fcntl`` работает штатно на ext4/tmpfs/overlay2/xfs).
    На Windows ``Path.home()`` указывает на ``%USERPROFILE%`` (``C:\\Users\\X\\``);
    на Linux/macOS — ``/home/X`` / ``/Users/X``.

    Структура каталога — ``<home>/.cache/nanobot/duckdb/cache.duckdb``.
    Совпадает с XDG Base Directory Specification для user-level cache
    (``$XDG_CACHE_HOME`` или ``~/.cache``).
    """
    from pathlib import Path

    return Path.home() / ".cache" / "nanobot" / "duckdb"


def resolve_publish_path(workspace_path, cache_cfg: dict | None = None) -> str:
    """**ЕДИНЫЙ** механизм вычисления пути к ``cache.duckdb``.

    **Безопасный default**: ``~/.cache/nanobot/duckdb/cache.duckdb``.
    Решение осознанное: DuckDB ATTACH берёт эксклюзивный flock, который
    NFS не отдаёт (``"Conflicting lock is held in PID 0"`` на свежем файле
    после ``rm`` — проверено эмпирически).

    Управление через ``cache_cfg`` (как срез из
    ``project.json::gateway.cache``):

    * ``local_path`` (str, опц.) — абсолютный/относительный (от workspace)
      путь к каталогу на локальной ФС, где будет лежать ``cache.duckdb``.
      Полезно, когда у ``~/.cache`` нет места или нужна отдельная ФС.

    **Никаких escape-hatch'ей и режимов совместимости.** Один механизм,
    один путь: либо явный ``gateway.cache.local_path``, либо default
    ``~/.cache/nanobot/duckdb/cache.duckdb``. Legacy
    ``<workspace>/data_store/duckdb/cache.duckdb`` на NFS **не
    поддерживается** и больше не доступен через эту функцию — он
    приводил к расхождению между gateway и CLI/skill.

    **Согласованность gateway ↔ CLI/skill.** Эту функцию вызывают:

    1. **Gateway** (``_make_sync_services``) — пишет снимок после
       каждого sync-цикла.
    2. **CLI / skill / vector_index_service**
       (``build_cache_provider``, ``get_in_memory_cache_path``) —
       читает снимок через ``PostgresDuckDbProvider``.

    Если оба слоя дадут разные пути — gateway пишет в одно место,
    CLI читает из другого, и скилл видит устаревший/пустой снимок.
    До v2.5.2 ``build_cache_provider`` хардкодил
    ``table_registry.snapshot_path(workspace_root)``, который расходился
    с новым safe default после деплоя. v2.5.2+ обе точки вызывают
    эту pure-функцию с одними и теми же ``gateway.cache.*``.

    Args:
        workspace_path: путь к workspace (для разрешения относительного
            ``local_path``).
        cache_cfg: dict — подсекция ``gateway.cache`` из project.json
            (или ``None``/пустой dict, если не задана).

    Returns:
        str-путь к ``cache.duckdb`` (всегда на локальной ФС).

    Raises:
        OSError: если ни явный путь, ни default, ни workspace-local
            fallback не могут быть созданы. **Не молчит** — падает
            громко, чтобы проблема была видна сразу.
    """
    from pathlib import Path

    if not isinstance(cache_cfg, dict):
        cache_cfg = {}

    # 1) Явный override: gateway.cache.local_path.
    local_path = cache_cfg.get("local_path")
    if isinstance(local_path, str) and local_path.strip():
        p = Path(local_path).expanduser()
        if not p.is_absolute() and workspace_path:
            p = Path(workspace_path) / p
        p.mkdir(parents=True, exist_ok=True)
        return str(p / "cache.duckdb")

    # 2) Default: ~/.cache/nanobot/duckdb/cache.duckdb на локальной ФС.
    default = _default_local_cache_dir()
    default.mkdir(parents=True, exist_ok=True)
    return str(default / "cache.duckdb")


def _warn_if_publish_path_on_nfs(publish_path: str) -> None:
    """Если ``publish_path`` живёт на NFS — напечатать громкое предупреждение.

    Используется ``/proc/mounts`` (только Linux). На других платформах
    функция — no-op.

    Это защита от регрессии: даже если пользователь положил workspace на
    NFS-шару и ``local_path`` через symlink указывает на NFS (либо
    ``~/.cache`` оказался на NFS), мы ему скажем: «вот что сейчас
    произойдёт — ATTACH будет падать с PID 0». Лучше увидеть это на
    старте, чем ловить в рантайме.
    """
    import logging
    import platform
    import sys
    from pathlib import Path

    if platform.system().lower() not in ("linux", "linux2"):
        return  # Windows/macOS — не делаем NFS-детект; фолбэк на реальный фейл

    try:
        p = Path(publish_path).resolve()
    except OSError:
        return  # путь ещё не существует — не наша забота

    mounts = Path("/proc/mounts")
    if not mounts.exists():
        return

    target = str(p)
    try:
        for raw in mounts.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = raw.split()
            if len(parts) < 3:
                continue
            mount_point, fstype = parts[1], parts[2]
            # ``mount_point`` — каталог; ищем самый длинный match
            if target == mount_point or target.startswith(mount_point.rstrip("/") + "/"):
                if "nfs" in fstype.lower():
                    logging.getLogger(__name__).warning(
                        "\n[cache] publish_path=%s лежит на %s (%s).\n"
                        "        DuckDB ATTACH не работает поверх NFS — каждый sync-цикл\n"
                        "        будет падать с 'Conflicting lock is held in PID 0'.\n"
                        "        Исправьте одним из способов:\n"
                        "          1) оставьте default (кеш автоматически уйдёт в ~/.cache/nanobot/duckdb);\n"
                        "          2) задайте gateway.cache.local_path на локальную ФС в project.json;\n"
                        "          3) уберите NFS из текущего пути (symlink / монтирование).\n",
                        publish_path, fstype, mount_point,
                    )
                    # Дополнительно — в stdout через print, чтобы пользователь
                    # гарантированно увидел даже если logging не настроен.
                    print(
                        f"[cache] WARNING: {publish_path} is on {fstype} "
                        f"({mount_point}); DuckDB ATTACH will fail with 'PID 0'. "
                        f"Check gateway.cache.local_path.",
                        file=sys.stderr,
                    )
                return
    except OSError:
        return  # /proc недоступен — молча пропускаем


def _make_sync_services(ctx: ApplicationContext) -> tuple:
    """Собрать ``(PgDuckDbSyncService, DuckDbCacheStore)``.

    Список таблиц берётся из ``TableRegistry`` (skills + infra).
    Sync-параметры — из ``gateway.sync.*``. Snapshot — общий
    ``<workspace>/data_store/duckdb/cache.duckdb``.

    Возвращает ``(None, None)`` если реестр пуст или нет DSN.
    """
    from lib.services.table_registry import table_registry

    pg = ctx.config_service.settings_section("channels").get("postgres", {})
    dsn = ""
    if isinstance(pg, dict):
        dsn = pg.get("dsn", "") or ""

    if not table_registry.resources():
        logger.warning(
            "PgDuckDbSyncService skipped: TableRegistry пуст "
            "(нет ни одной зарегистрированной таблицы через _auto_register_skills "
            "или _register_infra_resources). "
            "Проверьте секции project.json::skills.* и gateway.vector.index.*."
        )
        _record_sync_skipped(
            ctx.db_logging_service,
            event_type="sync_skipped_registry_empty",
            reason="TableRegistry пуст",
            detail="Нет ни одной зарегистрированной таблицы — проверьте project.json::skills.* и gateway.vector.index.*",
        )
        return None, None
    if not dsn:
        logger.warning(
            "PgDuckDbSyncService skipped: channels.postgres.dsn не задан "
            "(пустая строка или отсутствует ключ в project.json)."
        )
        _record_sync_skipped(
            ctx.db_logging_service,
            event_type="sync_skipped_no_dsn",
            reason="channels.postgres.dsn не задан",
            detail="DATABASE_URL пустой или отсутствует ключ в project.json — sync не сможет подключиться к PG",
        )
        return None, None

    from lib.services.duckdb_cache_store import DuckDbCacheStore
    from lib.services.pg_duckdb_sync_service import PgDuckDbSyncService

    all_table_names = list(table_registry.table_names())
    vector_names = list(table_registry.vector_names())

    if not all_table_names and not vector_names:
        logger.warning(
            "PgDuckDbSyncService skipped: в TableRegistry есть ресурсы, но ни одного "
            "имени в table_names()/vector_names() — несоответствие регистрации."
        )
        _record_sync_skipped(
            ctx.db_logging_service,
            event_type="sync_skipped_no_table_names",
            reason="в TableRegistry есть ресурсы, но table_names()/vector_names() пусты",
            detail="Несоответствие регистрации — проверьте register() vs register_infra()",
        )
        return None, None

    schemas: list[str] = []
    for r in (*table_registry.table_resources(), *table_registry.vector_resources()):
        if "." in r.name:
            sch = r.name.split(".", 1)[0]
            if sch and sch not in schemas:
                schemas.append(sch)

    logger.info(
        "PgDuckDbSyncService assembling: tables=%d vectors=%d schemas=%s dsn_set=%s",
        len(all_table_names),
        len(vector_names),
        schemas,
        bool(dsn),
    )
    logger.info(
        "PgDuckDbSyncService tables=%s vector_tables=%s",
        all_table_names,
        vector_names,
    )

    gateway_cfg = (ctx.config_service.settings_section("gateway") or {})
    if not isinstance(gateway_cfg, dict):
        gateway_cfg = {}
    cache_cfg = gateway_cfg.get("cache") if isinstance(gateway_cfg.get("cache"), dict) else {}
    publish_path = resolve_publish_path(ctx.config.workspace_path, cache_cfg)
    _warn_if_publish_path_on_nfs(publish_path)

    from lib.services.cache_provider_impl import read_embedding_config

    emb = read_embedding_config()
    embedding_base_url = emb.get("base_url", "")
    embedding_model = emb.get("model", "mxbai-embed-large:latest")
    embedding_dimension = int(emb.get("dimension", 1024))

    sync_cfg = (ctx.config_service.settings_section("gateway") or {}).get("sync") or {}
    poll_interval_sec = float(sync_cfg.get("poll_interval_sec", 0) or 0)
    max_queue_size = int(sync_cfg.get("max_queue_size", 0) or 0)
    reconnect_backoff = float(sync_cfg.get("reconnect_backoff_sec", 0) or 0)
    reconnect_backoff_max = float(sync_cfg.get("reconnect_backoff_max_sec", 0) or 0)
    full_resync_every = int(sync_cfg.get("full_resync_every", 0) or 0)

    # ``gateway.vector.index.storage_table`` — единственный источник
    # векторных данных (сырые эмбеддинги + метаданные чанков; см.
    # ``DuckDbCacheStore._vector_db_table``). После change
    # ``remove-vector-index-store`` persisted FAISS-кеш удалён; FAISS-индекс
    # собирается в памяти из DuckDB-снапшота storage_table (preload_indexes
    # при старте gateway).
    sync_tables = list(dict.fromkeys(all_table_names + vector_names))

    store = DuckDbCacheStore(
        cache_path="",
        publish_path=publish_path,
        schema=schemas[0] if schemas else "main",
        tables=all_table_names or None,
        vector_db_table=vector_names[0] if vector_names else "",
        embedding_base_url=embedding_base_url,
        embedding_model=embedding_model,
        embedding_dimension=embedding_dimension,
        db_logging_service=ctx.db_logging_service,
    )
    sync = PgDuckDbSyncService(
        dsn=dsn,
        schema=schemas[0] if schemas else "main",
        tables=sync_tables,
        vector_table=vector_names[0] if vector_names else "",
        poll_interval_sec=poll_interval_sec,
        max_queue_size=max_queue_size,
        reconnect_backoff=reconnect_backoff,
        reconnect_backoff_max=reconnect_backoff_max,
        full_resync_every=full_resync_every,
        db_logging_service=ctx.db_logging_service,
    )
    return sync, store


def _record_sync_skipped(
    db_logging_service: Any,
    event_type: str,
    reason: str,
    detail: str,
) -> None:
    """Записать в ``agent_gateway_logs`` причину, по которой sync не стартанул.

    Используется в ``_make_sync_services`` при ранних return'ах с тихими
    причинами отказа. Идемпотентно и безопасно для вызова до старта
    ``DbLoggingService`` — единый конвейер через
    ``DbLoggingService.try_log_event``.
    """
    from lib.services.db_logging_service import LogEvent, try_log_event

    log_event = LogEvent(
        event_type=event_type,
        level="WARN",
        session_id="gateway:sync",
        channel=None,
        actor="sync",
        name=event_type,
        summary=f"PgDuckDbSyncService skipped: {reason}",
        payload={"reason": reason, "detail": detail},
    )
    try_log_event(
        db_logging_service,
        log_event,
        producer="ApplicationContext",
        event_type=event_type,
    )




def _auto_register_skills(ctx: ApplicationContext) -> None:
    """Зарегистрировать skills из ``project.json::skills.*`` в ``table_registry``.

    Делегирует ``lib.core.skill_registration.register_skill_from_config``.
    """
    from lib.core.skill_registration import register_skill_from_config

    skills = ctx.config_service.settings_section("skills") or {}
    if not isinstance(skills, dict):
        return

    for name, cfg in skills.items():
        register_skill_from_config(name, cfg)


_INFRA_KEY_VECTOR_STORAGE = "vector_index.storage"


def _register_infra_resources(ctx: ApplicationContext) -> None:
    """Зарегистрировать инфраструктурные ресурсы runtime'а.

    Делегирует ``lib.core.infra_registration`` — общую логику для runtime
    и standalone-утилит (``tools/build_vectors.py``).

    Какие индексы строить и из каких source-таблиц — описывается в
    ``project.json::gateway.vector.index.indexes`` (см.
    ``VectorIndexSettings.indexes`` и ``read_vector_index_config``).

    Embedding-параметры захардкожены в ``cache_provider_impl`` —
    отдельная регистрация не нужна.
    """
    from lib.core.infra_registration import register_vector_storage

    register_vector_storage()


def _make_transcription(config: Any) -> Any:
    """Создать ``TranscriptionService`` для настройки Postgres-канала.

    ``TranscriptionService`` достаёт API-ключ/URL/язык провайдера
    (``openai`` / ``groq``) из ``config.channels.transcription_provider``
    и ``config.providers.*.api_key``.
    """
    from lib.services.transcription_service import TranscriptionService

    return TranscriptionService(config)


def _make_preload(
    settings: Any,
    db_logging_service: Any | None = None,
) -> Any:
    """Создать ``PreloadService`` (для gateway — FAISS preload, для CLI — кеш навыка).

    ``settings`` — полные ``SETTINGS`` (для чтения ``skills.audit_analyzer``).
    ``db_logging_service`` — для записи vector-preload health-события в
    ``agent_gateway_logs`` (graceful degrade, если отсутствует).
    """
    from lib.services.preload_service import PreloadService

    return PreloadService(
        settings=settings,
        db_logging_service=db_logging_service,
    )


def _make_cron_service(config: Any) -> Any:
    """Создать ``CronService`` для CLI-режима (только там он нужен).

    ``CronService`` хранит задачи в ``workspace/cron/jobs.json`` —
    путь относительно ``config.workspace_path``. Если директории нет,
    CronService создаст её при первом сохранении задачи.
    """
    from nanobot.cron.service import CronService

    return CronService(config.workspace_path / "cron" / "jobs.json")


# ----------------------------------------------------------------------
# Общий пул соединений utils.db
# ----------------------------------------------------------------------


def _configure_db_pool(pool_cfg: dict, print_activity: bool = False) -> None:
    """Применить ``channels.postgres.pool`` к общему пулу ``utils.db``.

    ``pool_cfg`` — словарь с ключами ``min_conn/max_conn/pool_timeout/
    queue_maxsize/reconnect_backoff_sec/reconnect_backoff_max_sec/
    connect_max_retries/idle_timeout_sec/job_max_retries``. Неизвестные
    ключи игнорируются (``set_pool_config`` принимает только известные).

    ``print_activity`` — вывод активности db-worker'ов (гейт
    ``gateway.print_db_activity``), кладётся в конфиг пула как
    ``print_activity``.
    """
    try:
        from utils.db import set_pool_config

        merged = dict(pool_cfg)
        merged["print_activity"] = bool(print_activity)
        set_pool_config(merged)
    except Exception as exc:
        logger.warning("utils.db pool config ignored: %s", exc)


def _start_db_pool() -> None:
    """Запустить общий пул ``utils.db`` (воркеры подключаются лениво)."""
    try:
        from utils.db import start

        start()
    except Exception as exc:
        logger.warning("utils.db pool start failed: %s", exc)


def _stop_db_pool() -> None:
    """Остановить общий пул ``utils.db`` и закрыть все соединения."""
    try:
        from utils.db import shutdown

        shutdown()
    except Exception as exc:
        logger.warning("utils.db pool shutdown failed: %s", exc)
