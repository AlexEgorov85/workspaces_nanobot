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
from typing import Any, Optional

logger = logging.getLogger(__name__)


class ApplicationContext:
    """Контекст приложения: конфиг + все сервисы."""

    # Пути
    script_dir: Path
    workspace_dir: Path

    # Конфигурация
    config: Any
    settings: Any

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
    db_logging_service: Optional[Any] = None
    audit_sync_service: Optional[Any] = None
    audit_memory_store: Optional[Any] = None

    # Помощники
    config_service: Any = None
    runtime_patcher: Any = None
    transcription_service: Any = None
    session_storage_service: Any = None
    subprocess_manager: Any = None
    preload_service: Any = None

    # Lifecycle
    _started: bool = False
    _shutdown: Optional[Any] = None  # ShutdownCoordinator

    @classmethod
    def create(
        cls,
        script_dir: Path,
        workspace_dir: Path,
        *,
        enable_db_logging: bool = True,
        enable_audit: bool = True,
        enable_cron: bool = False,
        storage_override: Optional[str] = None,
        session_override: Optional[str] = None,
    ) -> "ApplicationContext":
        """Собрать контекст приложения.

        Args:
            script_dir: корень проекта (где лежит config.json).
            workspace_dir: корень workspace.
            enable_db_logging: инициализировать DbLoggingService.
            enable_audit: инициализировать AuditSyncService + AuditMemoryStore.
            enable_cron: подключить CronService (CLI).
            storage_override: режим хранилища из CLI (auto/postgres/file).
            session_override: имя сессии (CLI).
        """
        ctx = cls()
        ctx.script_dir = Path(script_dir)
        ctx.workspace_dir = Path(workspace_dir)

        # 1. ConfigService + загрузка конфига
        # ConfigService.load() сам подставляет ${VAR} плейсхолдеры из
        # SETTINGS.providers.*.api_key (если ${VAR} — это *_API_KEY и
        # .secrets.env задал api_key=... через "# providers: <name>").
        ctx.config_service = _make_config_service(ctx.script_dir, ctx.workspace_dir)
        ctx.config = ctx.config_service.load()
        ctx.settings = ctx.config_service.settings

        # 2. Таймауты
        ctx.config_service.apply_timeouts(
            ctx.config,
            llm_timeout=_gateway_int(ctx.settings, "gateway", "llm_timeout", default=300),
            exec_timeout=_gateway_int(ctx.settings, "gateway", "exec_timeout", default=60),
            max_iterations=_cli_int(ctx.settings, "max_iterations", default=200),
        )

        # 3. SessionStorageService
        from lib.services.session_storage import SessionStorageService

        ctx.session_storage_service = SessionStorageService(
            session_manager_json=ctx.script_dir / "session_manager.json",
        )
        pg_section = ctx.config_service.settings_section("channels").get(
            "postgres", {}
        )
        try:
            storage_mode, session_manager = ctx.session_storage_service.create(
                ctx.config,
                storage=storage_override
                or _gateway_str(ctx.settings, "gateway", "storage", default="auto"),
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

        # 5. AuditSyncService + AuditMemoryStore
        if enable_audit:
            ctx.audit_sync_service, ctx.audit_memory_store = _make_audit_services(ctx)

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

        agent_factory = AgentFactory()
        ctx.agent, ctx.hooks = agent_factory.create(
            ctx.config,
            ctx.bus,
            session_manager=ctx.session_manager,
            cron_service=cron_service,
            db_logging_service=ctx.db_logging_service,
            agent_id=agent_id,
        )
        ctx.tool_audit_hook = ctx.hooks[0]

        # 7. RuntimePatcher
        from lib.services.runtime_patcher import RuntimePatcher

        ctx.runtime_patcher = RuntimePatcher()
        ctx.runtime_patcher.apply_all(
            ctx.config, ctx.settings, ctx.workspace_dir,
            ctx.agent, ctx.tool_audit_hook,
            db_logging_service=ctx.db_logging_service,
            session_manager=ctx.session_manager,
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

        if self.db_logging_service is not None:
            self.db_logging_service.start()
            self._shutdown.register("db_logging_service", self.db_logging_service)

        if self.audit_sync_service is not None:
            try:
                self.audit_sync_service.start(initial_load=True)
                self._shutdown.register("audit_sync_service", self.audit_sync_service)
            except Exception as exc:
                logger.warning("AuditSyncService not started: %s", exc)

        self._started = True

    def stop(self) -> None:
        """Корректно остановить все фоновые сервисы."""
        if not self._started:
            return
        if self._shutdown is not None:
            self._shutdown.shutdown_all()
        self._started = False


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


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


def _make_db_logging(ctx: "ApplicationContext") -> Optional[Any]:
    """Собрать ``DbLoggingService`` из секции ``logging.db`` в settings.

    Возвращает ``None`` если:
      * ``logging.db.enabled != True`` (явно отключено);
      * нет DSN в ``channels.postgres`` (некуда писать);
      * psycopg2 не импортируется (битое окружение).

    DSN берётся из ``channels.postgres.dsn`` (тот же, что для
    PGSessionManager и PostgresChannel). Fallback на JSONL-файл
    включается в ``DbLoggingService.stop()``-логике автоматически.
    """
    try:
        from lib.services.db_logging_service import DbLoggingService
        from lib.services.config_service import ConfigService  # noqa: F401
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

    fallback_path_raw = db_cfg.get("fallback_path", "logs/agent_gateway_logs_fallback.jsonl")
    fallback_path = Path(fallback_path_raw)
    if not fallback_path.is_absolute():
        fallback_path = Path(ctx.script_dir) / fallback_path

    return DbLoggingService(
        dsn=dsn,
        table_name=db_cfg.get("table_name", "agent_gateway_logs"),
        question_runs_table=db_cfg.get("question_runs_table", "agent_question_runs"),
        schema=db_cfg.get("schema", "public"),
        dialect=db_cfg.get("dialect", "postgres"),
        flush_interval_sec=float(db_cfg.get("flush_interval_sec", 5.0)),
        batch_size=int(db_cfg.get("batch_size", 100)),
        queue_maxsize=int(db_cfg.get("queue_maxsize", 10000)),
        min_level=db_cfg.get("min_level", "INFO"),
        fallback_path=fallback_path,
        connect_backoff_sec=float(db_cfg.get("connect_backoff_sec", 1.0)),
        connect_backoff_max_sec=float(db_cfg.get("connect_backoff_max_sec", 60.0)),
        summary_max_chars=int(db_cfg.get("summary_max_chars", 200)),
    )


def _make_audit_services(ctx: "ApplicationContext") -> tuple:
    """Собрать (AuditSyncService, AuditMemoryStore) для audit_analyzer.

    Читает секцию ``skills.audit_analyzer`` (там же, где CLI preload
    читает) и создаёт пару ``sync_service`` + ``store``. ``store``
    держит in-memory DuckDB-зеркало таблиц из PG, ``sync_service``
    инкрементально догружает изменения.

    Возвращает ``(None, None)`` если:
      * ``in_memory_enabled != True`` (явно отключено);
      * нет DSN (некуда подключаться);
      * не указаны ни ``db_tables`` ни ``mode_vector_db_table``
        (нечего синхронизировать);
      * ``audit_memory_store`` или ``audit_sync_service`` не импортируются.

    Publish path для snapshot'a — ``workspace/skills/audit_analyzer/<cp>``
    (CLI skill читает этот файл на чтение; gateway пишет атомарно через
    temp+os.replace).
    """
    pg = ctx.config_service.settings_section("channels").get("postgres", {})
    dsn = ""
    if isinstance(pg, dict):
        dsn = pg.get("dsn", "") or ""
    cfg = ctx.config_service.settings_section("skills").get("audit_analyzer", {})
    if not cfg.get("in_memory_enabled", False) or not dsn:
        return None, None
    try:
        from lib.services.audit_memory_store import AuditMemoryStore
        from lib.services.audit_sync_service import AuditSyncService
    except Exception:
        return None, None

    tables = [t for t in (cfg.get("db_tables", []) or []) if t]
    vector_table = cfg.get("mode_vector_db_table", "") or ""
    schema = cfg.get("db_schema", "oarb") or "oarb"
    if not vector_table and not tables:
        return None, None

    publish_path = ""
    cp = cfg.get("in_memory_cache_path", "") or ""
    if cp:
        p = Path(cp)
        publish_path = (
            str(ctx.config.workspace_path / "skills" / "audit_analyzer" / cp)
            if not p.is_absolute() else str(p)
        )

    store = AuditMemoryStore(
        cache_path="",
        publish_path=publish_path,
        schema=schema,
        tables=tables or None,
        vector_db_table=vector_table,
        embedding_base_url=cfg.get("embedding_base_url", "") or "",
        embedding_model=cfg.get("embedding_model", "mxbai-embed-large:latest"),
    )
    sync = AuditSyncService(
        dsn=dsn,
        schema=schema,
        tables=(tables + [vector_table]) if vector_table else tables,
        vector_table=vector_table,
        poll_interval_sec=float(cfg.get("poll_interval_sec", 60)),
        write_table=cfg.get("sync_write_table", "audit_interactions"),
        write_schema=schema,
        max_queue_size=int(cfg.get("sync_max_queue_size", 10000)),
        reconnect_backoff=float(cfg.get("reconnect_backoff_sec", 1.0)),
        reconnect_backoff_max=float(cfg.get("reconnect_backoff_max_sec", 60.0)),
        full_resync_every=int(cfg.get("full_resync_every", 10)),
    )
    return sync, store


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


def _gateway_int(settings: Any, *path: str, default: int) -> int:
    """Достать int-значение из вложенного dict/AttrDict по цепочке ``path``.

    Если на любом уровне атрибут отсутствует или не приводится к int —
    возвращает ``default``. Безопасный аксессор для ``SETTINGS.gateway.*``
    и аналогичных секций, где значения могут быть None или битыми.
    """
    for key in path:
        try:
            settings = settings.get(key) if isinstance(settings, dict) else getattr(settings, key)
        except (AttributeError, KeyError):
            return default
    try:
        return int(settings) if settings is not None else default
    except (TypeError, ValueError):
        return default


def _gateway_str(settings: Any, *path: str, default: str) -> str:
    """Достать str-значение из вложенного dict/AttrDict по цепочке ``path``.

    Пустая строка и ``None`` считаются отсутствием → возвращается
    ``default``. Используется для ``SETTINGS.gateway.storage`` и т.п.
    """
    for key in path:
        try:
            settings = settings.get(key) if isinstance(settings, dict) else getattr(settings, key)
        except (AttributeError, KeyError):
            return default
    return str(settings) if settings else default


def _cli_int(settings: Any, key: str, default: int) -> int:
    """Достать int-значение из ``settings.cli.<key>``.

    Специализированная версия ``_gateway_int`` для секции ``cli`` —
    CLI-таймауты и лимиты (``max_iterations``, ``llm_timeout``) заданы
    именно в ``project.json`` → ``SETTINGS.cli.*``. Все исключения
    глотаются с возвратом ``default`` — config может быть неполным.
    """
    try:
        node = settings.get("cli") if isinstance(settings, dict) else getattr(settings, "cli", None)
        if node is None:
            return default
        v = node.get(key) if isinstance(node, dict) else getattr(node, key, None)
        return int(v) if v is not None else default
    except (AttributeError, KeyError, TypeError, ValueError):
        return default
