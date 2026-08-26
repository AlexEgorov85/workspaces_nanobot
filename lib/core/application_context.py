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
        """
        ctx = cls()
        ctx.script_dir = Path(script_dir)
        ctx.workspace_dir = Path(workspace_dir)

        # Сбросить ``TableRegistry`` — это singleton, и при повторном
        # ``create()`` в одном процессе (тесты, streamlit-reload, gateway
        # перезапуск конфига) старые регистрации остались бы и смешались
        # с новыми. ``_make_sync_services`` и ``_auto_register_skills``
        # ниже заполнят реестр заново.
        from lib.services.table_registry import table_registry
        table_registry.clear()

        # 1. ConfigService + загрузка конфига
        # ConfigService.load() сам подставляет ${VAR} плейсхолдеры из
        # SETTINGS.providers.*.api_key (если ${VAR} — это *_API_KEY и
        # .secrets.env задал api_key=... через "# providers: <name>").
        ctx.config_service = _make_config_service(ctx.script_dir, ctx.workspace_dir)
        ctx.config = ctx.config_service.load()
        ctx.settings = ctx.config_service.settings

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

        ctx.session_storage_service = SessionStorageService(
            session_manager_json=ctx.script_dir / "session_manager.json",
        )
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
        ctx.preload_service = _make_preload(ctx.settings)

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

    def stop(self) -> None:
        """Корректно остановить все фоновые сервисы."""
        if not self._started:
            return
        if self._shutdown is not None:
            self._shutdown.shutdown_all()
        # После остановки сервисов закрываем общий пул соединений.
        _stop_db_pool()
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

        Console().print(f"[green]✓[/green] Hooks connected: {label}")
    except Exception:
        # Старые Windows-консоли (cp1251) не умеют ✓ (U+2713) — выводим
        # тот же список обычным print, чтобы информация не пропадала.
        print(f"Hooks connected: {label}")


def _make_config_service(script_dir: Path, workspace_dir: Path) -> Any:
    """Создать ``ConfigService``, привязанный к корню проекта.

    Использует lazy-import, чтобы не зависеть от ``config.py`` на
    старте (если config битый, ошибка проявится в ``.load()``).
    """
    from lib.services.config_service import ConfigService

    return ConfigService(script_dir=script_dir, workspace_dir=workspace_dir)


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
            "logging.db.table_name и logging.db.question_runs_table "
            "обязательны для DbLoggingService (нет авто-дефолтов в коде). "
            f"table_name={table_name!r}, question_runs_table={question_runs_table!r}"
        )

    return DbLoggingService(
        dsn=dsn,
        table_name=table_name,
        question_runs_table=question_runs_table,
        schema=db_cfg.get("schema", "public"),
        dialect=db_cfg.get("dialect", "postgres"),
        flush_interval_sec=float(db_cfg.get("flush_interval_sec", 5.0)),
        batch_size=int(db_cfg.get("batch_size", 100)),
        queue_maxsize=int(db_cfg.get("queue_maxsize", 10000)),
        min_level=db_cfg.get("min_level", "INFO"),
        connect_backoff_sec=float(db_cfg.get("connect_backoff_sec", 1.0)),
        connect_backoff_max_sec=float(db_cfg.get("connect_backoff_max_sec", 60.0)),
        summary_max_chars=int(db_cfg.get("summary_max_chars", 200)),
    )


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

    if not table_registry.resources() or not dsn:
        return None, None

    from lib.services.duckdb_cache_store import DuckDbCacheStore
    from lib.services.pg_duckdb_sync_service import PgDuckDbSyncService

    all_table_names = list(table_registry.table_names())
    vector_names = list(table_registry.vector_names())

    if not all_table_names and not vector_names:
        return None, None

    schemas: list[str] = []
    for r in (*table_registry.table_resources(), *table_registry.vector_resources()):
        if "." in r.name:
            sch = r.name.split(".", 1)[0]
            if sch and sch not in schemas:
                schemas.append(sch)

    publish_path = str(table_registry.snapshot_path(ctx.config.workspace_path))

    emb = table_registry.embedding_config()
    embedding_base_url = emb.get("base_url", "")
    embedding_model = emb.get("model", "mxbai-embed-large:latest")
    embedding_dimension = int(emb.get("dimension", 1024))

    sync_cfg = (ctx.config_service.settings_section("gateway") or {}).get("sync") or {}
    poll_interval_sec = float(sync_cfg.get("poll_interval_sec", 0) or 0)
    max_queue_size = int(sync_cfg.get("max_queue_size", 0) or 0)
    reconnect_backoff = float(sync_cfg.get("reconnect_backoff_sec", 0) or 0)
    reconnect_backoff_max = float(sync_cfg.get("reconnect_backoff_max_sec", 0) or 0)
    full_resync_every = int(sync_cfg.get("full_resync_every", 0) or 0)

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
    )
    return sync, store




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
    ``public.agent_vector_index_config`` (runtime-БД).

    Embedding-конфиг (``gateway.vector.embedding``) кладётся в
    ``TableRegistry.set_embedding_config(...)`` отдельно — это
    runtime-конфиг без PG-ресурсов.
    """
    from lib.core.infra_registration import register_vector_storage
    from lib.core.skill_registration import register_embedding_config

    register_vector_storage()
    register_embedding_config()


def _make_transcription(config: Any) -> Any:
    """Создать ``TranscriptionService`` для настройки Postgres-канала.

    ``TranscriptionService`` достаёт API-ключ/URL/язык провайдера
    (``openai`` / ``groq``) из ``config.channels.transcription_provider``
    и ``config.providers.*.api_key``.
    """
    from lib.services.transcription_service import TranscriptionService

    return TranscriptionService(config)


def _make_preload(settings: Any) -> Any:
    """Создать ``PreloadService`` (для gateway — FAISS preload, для CLI — кеш навыка).

    ``settings`` — полные ``SETTINGS`` (для чтения ``skills.audit_analyzer``).
    """
    from lib.services.preload_service import PreloadService

    return PreloadService(settings=settings)


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
